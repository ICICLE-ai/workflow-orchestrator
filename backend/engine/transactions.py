"""Database transactions for the DBOS execution engine, written against the
main Harvest schema (models.py).

Port of dbos-example/app/transactions.py. The big difference: dbos-example
*created* a fresh workflow + nodes + edges from the POST body on every run. Here
the template already exists as wf_node/wf_edge rows (saved via the existing
/api/workflow-templates endpoints), so a run only creates pipeline_run +
run_step rows that reference that existing template. The DAG node key is the
string form of wf_node.node_id.

Each function is a DBOS datasource transaction so the orchestrator's reads and
writes are durable and isolated.
"""
import json

from dbos import SQLAlchemyDatasource
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from models import (
    StepTypeRegistry,
    StepTypePort,
    WfNode,
    WfEdge,
    PipelineRun,
    RunStep,
    AppUser,
)
from db import DATABASE_URL, engine

ds = SQLAlchemyDatasource.create(DATABASE_URL, engine=engine)


def _run_step(session, run_id: int, node_key: str) -> RunStep:
    """Fetch the RunStep for a given run and DAG node key (str(node_id))."""
    return session.execute(
        select(RunStep).where(
            RunStep.run_id == run_id,
            RunStep.node_id == int(node_key),
        )
    ).scalars().one()


@ds.transaction(isolation_level="READ COMMITTED")
def create_run_for_template(dbos_workflow_id: str, dag_config: dict) -> int:
    """Create a pipeline_run + one run_step per node for an EXISTING template.

    Inputs:
        dbos_workflow_id (str): DBOS workflow id orchestrating this run.
        dag_config (dict): {
            "template_version_id": int,
            "nodes": [{"id": <node_id>, "type": <step_type_key>, "inputs": {...}}],
            "edges": [{"from": <node_id>, "to": <node_id>}],
        }

    Outputs:
        int: the new pipeline_run.run_id.
    """
    session = ds.sql_session()

    # The HTTP layer creates the pipeline_run + run_step rows synchronously
    # (before starting this workflow) so it can return a real run_id to the
    # caller immediately. When that's the case, reuse it instead of creating
    # a duplicate run.
    existing_run_id = dag_config.get("run_id")
    if existing_run_id is not None:
        return existing_run_id

    template_version_id = dag_config["template_version_id"]

    # Owner of the run: the user who launched it (passed through dag_config by the
    # execute endpoint). The engine resolves this user's Tapis token when
    # submitting jobs. Fall back to mock_user so the NOT NULL user_id is satisfied
    # in dev/seed paths that don't carry an owner_id.
    owner_id = dag_config.get("owner_id")
    if owner_id is None:
        user = session.execute(
            select(AppUser).where(AppUser.username == "mock_user")
        ).scalars().first()
        owner_id = user.user_id if user else None

    run = PipelineRun(
        template_version_id=template_version_id,
        user_id=owner_id,
        name=dag_config.get("name", "DAG Run"),
        status="RUNNING",
        dbos_workflow_id=dbos_workflow_id,
        frozen_config=dag_config,
    )
    session.add(run)
    session.flush()  # get run_id

    # One run_step per node. node["id"] is the wf_node.node_id.
    for node in dag_config.get("nodes", []):
        run_step = RunStep(
            run_id=run.run_id,
            node_id=int(node["id"]),
            step_label=node.get("label", str(node["id"])),
            status="pending",
            config=node.get("inputs", {}) or {},
            outputs={},
        )
        session.add(run_step)

    return run.run_id


@ds.transaction(isolation_level="READ COMMITTED")
def get_run_steps_status(run_id: int) -> dict:
    """Map DAG node key -> status for every step in the run."""
    session = ds.sql_session()
    steps = session.execute(
        select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.run_step_id)
    ).scalars().all()
    return {str(s.node_id): s.status for s in steps}


@ds.transaction(isolation_level="READ COMMITTED")
def update_run_step_status(
    run_id: int,
    node_key: str,
    status: str,
    tapis_job_uuid: str = None,
    tapis_job_status: str = None,
    error_message: str = None,
):
    """Update status and optional Tapis/error metadata of a single step."""
    session = ds.sql_session()
    run_step = _run_step(session, run_id, node_key)
    run_step.status = status
    if tapis_job_uuid is not None:
        run_step.tapis_job_uuid = tapis_job_uuid
    if tapis_job_status is not None:
        run_step.tapis_job_status = tapis_job_status
    if error_message is not None:
        run_step.error_message = error_message


@ds.transaction(isolation_level="READ COMMITTED")
def get_tapis_app_for_node(run_id: int, node_key: str) -> str:
    """Return the Tapis app id for a node, via wf_node -> step_type_registry.

    Falls back to 'generic-pipeline' when a step type has no tapis_app_id yet,
    so steps still execute against the mock during development.
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WfNode)
        .options(joinedload(WfNode.template))
        .where(WfNode.node_id == int(node_key))
    ).scalars().one()

    step_type = session.execute(
        select(StepTypeRegistry).where(
            StepTypeRegistry.step_type_key == wf_node.step_type_key
        )
    ).scalars().one()

    return step_type.tapis_app_id or "generic-pipeline"


@ds.transaction(isolation_level="READ COMMITTED")
def get_node_tapis_template(node_key: str) -> dict:
    """Return the tapis_job template for a node's step type (or {} if none)."""
    session = ds.sql_session()
    wf_node = session.execute(
        select(WfNode).where(WfNode.node_id == int(node_key))
    ).scalars().one()
    step_type = session.execute(
        select(StepTypeRegistry).where(
            StepTypeRegistry.step_type_key == wf_node.step_type_key
        )
    ).scalars().one()
    return step_type.tapis_job or {}


@ds.transaction(isolation_level="READ COMMITTED")
def get_node_step_type(node_key: str) -> str:
    """Return a node's step_type_key (e.g. 'sink_path', 'yolo_inference')."""
    session = ds.sql_session()
    wf_node = session.execute(
        select(WfNode).where(WfNode.node_id == int(node_key))
    ).scalars().one()
    return wf_node.step_type_key


@ds.transaction(isolation_level="READ COMMITTED")
def get_config_schema_defaults(node_key: str) -> dict:
    """Return {param: default} for the node's step-type config_schema.

    Applied so a config param the user didn't set still gets its declared default
    (e.g. imgsz=640), rather than rendering to an empty ${imgsz} and breaking the
    job's arg parsing."""
    session = ds.sql_session()
    wf_node = session.execute(
        select(WfNode).where(WfNode.node_id == int(node_key))
    ).scalars().one()
    step_type = session.execute(
        select(StepTypeRegistry).where(
            StepTypeRegistry.step_type_key == wf_node.step_type_key
        )
    ).scalars().one_or_none()
    schema = (step_type.config_schema or {}) if step_type else {}
    return {k: v.get("default") for k, v in schema.items() if isinstance(v, dict) and "default" in v}


@ds.transaction(isolation_level="READ COMMITTED")
def get_node_config_schema(node_key: str) -> dict:
    """Return a node's step-type config_schema, raw (unlike
    get_config_schema_defaults, which only extracts defaults). Used to find
    "secret"-typed fields so their value (a secret KEY) can be resolved to the
    real value at job-submission time."""
    session = ds.sql_session()
    wf_node = session.execute(
        select(WfNode).where(WfNode.node_id == int(node_key))
    ).scalars().one()
    step_type = session.execute(
        select(StepTypeRegistry).where(
            StepTypeRegistry.step_type_key == wf_node.step_type_key
        )
    ).scalars().one_or_none()
    return (step_type.config_schema or {}) if step_type else {}


@ds.transaction(isolation_level="READ COMMITTED")
def get_node_output_ports(node_key: str) -> list:
    """Return this node's output ports as {name, output_path, file_glob} dicts.

    output_path is the artifact's subpath within the step's job output dir
    (e.g. 'predictions.json'); None for source nodes / single-artifact steps.
    file_glob, when set, means output_path names a DIRECTORY containing one
    dynamically (e.g. timestamp-)named file rather than the artifact itself —
    see engine.tapis.resolve_latest_file and _derive_outputs in
    engine/workflows.py, which resolve it into the actual file's path.
    Used to expose each output port's specific path to downstream nodes/sinks.
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WfNode).where(WfNode.node_id == int(node_key))
    ).scalars().one()
    ports = session.execute(
        select(StepTypePort).where(
            StepTypePort.step_type_key == wf_node.step_type_key,
            StepTypePort.direction == "output",
        )
    ).scalars().all()
    return [{"name": p.port_name, "output_path": p.output_path, "file_glob": p.file_glob} for p in ports]


@ds.transaction(isolation_level="READ COMMITTED")
def get_incoming_edges(run_id: int, node_key: str) -> list:
    """Return the edges feeding into a node, resolved to port names.

    Each item: {source_node_key, source_port, target_port}. Port names come from
    step_type_port via the edge's source_port_id/target_port_id (the main
    schema's FK model). This is what lets a target input port be filled from the
    connected source output port — edge-driven data flow.
    """
    session = ds.sql_session()
    run = session.execute(
        select(PipelineRun).where(PipelineRun.run_id == run_id)
    ).scalars().one()

    edges = session.execute(
        select(WfEdge).where(
            WfEdge.template_version_id == run.template_version_id,
            WfEdge.target_node_id == int(node_key),
        )
    ).scalars().all()

    # Resolve port ids -> names in one lookup.
    port_ids = set()
    for e in edges:
        port_ids.add(e.source_port_id)
        port_ids.add(e.target_port_id)
    ports = {}
    if port_ids:
        for p in session.execute(
            select(StepTypePort).where(StepTypePort.port_id.in_(port_ids))
        ).scalars().all():
            ports[p.port_id] = p.port_name

    return [
        {
            "source_node_key": str(e.source_node_id),
            "source_port": ports.get(e.source_port_id),
            "target_port": ports.get(e.target_port_id),
        }
        for e in edges
    ]


@ds.transaction(isolation_level="READ COMMITTED")
def get_downstream_sink_path(run_id: int, node_key: str) -> str:
    """If this step feeds a sink node, return that sink's configured path.

    A sink node (any step type with category 'sink' — sink_path, sink_model,
    sink_image_dir, etc.) is a data-provider in reverse: it names WHERE this
    step's output should be written. We look for an outgoing edge to any sink
    node and return the path from that sink's run_step.config. Returns '' if
    there is no downstream sink.
    """
    session = ds.sql_session()
    run = session.execute(
        select(PipelineRun).where(PipelineRun.run_id == run_id)
    ).scalars().one()

    # Outgoing edges from this node.
    edges = session.execute(
        select(WfEdge).where(
            WfEdge.template_version_id == run.template_version_id,
            WfEdge.source_node_id == int(node_key),
        )
    ).scalars().all()

    for e in edges:
        target = session.execute(
            select(WfNode).where(WfNode.node_id == e.target_node_id)
        ).scalars().one()
        step_type = session.execute(
            select(StepTypeRegistry).where(
                StepTypeRegistry.step_type_key == target.step_type_key
            )
        ).scalars().one_or_none()
        if step_type and step_type.category == "sink":
            sink_step = session.execute(
                select(RunStep).where(
                    RunStep.run_id == run_id, RunStep.node_id == target.node_id
                )
            ).scalars().one_or_none()
            path = (sink_step.config or {}).get("path", "") if sink_step else ""
            if path:
                return path
    return ""


# OSC systems share one scratch layout, keyed by the run's Slurm allocation —
# mirrors frontend/app/lib/tapis.ts's OSC_SCRATCH_SYSTEMS grouping.
_OSC_EXEC_SYSTEMS = {"pitzer-tapis", "cardinal-tapis", "ascend-tapis"}


def _exec_system_dirs(exec_system: str, slurm_account: str) -> tuple:
    """Return (execSystemExecDir, execSystemInputDir, execSystemOutputDir) for a
    Tapis job targeting `exec_system`, or (None, None, None) when that system
    doesn't use these fields (Tapis applies the app's own default layout
    instead) — job_spec.render() drops a field entirely when its context value
    is falsy, rather than rendering a broken/empty path.

    ${JobUUID} is a Tapis-side macro (filled in by Tapis at submission, not by
    us) — left as literal text here since our own ${...} substitution only
    replaces keys actually present in the context dict, and "JobUUID" never is.
    """
    if exec_system in _OSC_EXEC_SYSTEMS:
        return (
            f"/fs/scratch/{slurm_account}/harvest_jobs/${{JobUUID}}",
            f"/fs/scratch/{slurm_account}/harvest_jobs/${{JobUUID}}",
            f"/fs/scratch/{slurm_account}/harvest_jobs/${{JobUUID}}/output",
        )
    if exec_system == "expanse-tapis-static":
        return ("/jobs/${JobUUID}", "/jobs/${JobUUID}", "/jobs/${JobUUID}")
    # expanse-tapis (and anything unrecognized) — omit, let the Tapis app's own
    # default exec/input/output dirs apply.
    return (None, None, None)


def _default_work_dir(exec_system: str, slurm_account: str, username: str = "") -> str:
    """Base scratch/project dir for `exec_system`. Python twin of
    frontend/app/lib/tapis.ts's defaultWorkDir — needed here now that exec
    system varies PER NODE: a single run-level work_dir can't serve a run whose
    GPU steps are on Expanse and CPU steps on OSC, since the two sites have
    entirely different scratch layouts. Returns "" for an unrecognized system,
    which get_run_archive_context treats the same as an unset work_dir.
    """
    if exec_system in _OSC_EXEC_SYSTEMS:
        return f"/fs/scratch/{slurm_account}/jobs/"
    if exec_system == "expanse-tapis-static":
        return "/jobs/"
    if exec_system == "expanse-tapis":
        return f"/expanse/lustre/scratch/{username}/temp_project/jobs/"
    return ""


# Per-node exec overrides live in the node's config_values under these
# camelCase keys — deliberately NOT the snake_case names used for ${...}
# substitution. workflows.py builds its context as
# {**get_run_archive_context(...), **resolved}, spreading node config LAST, so
# a node key literally named "exec_system" would shadow ${exec_system} while
# archive_dir / archive_uri / execSystem*Dir stayed derived from the RUN-level
# system — a job running on system A with exec dirs computed for system B.
# Reading camelCase here (and resolving everything downstream from it) keeps
# one source of truth, and matches the existing nodeCount/coresPerNode/gpus
# reserved keys, which are camelCase for the same "not a placeholder" reason.
_NODE_EXEC_SYSTEM_KEY = "execSystem"
_NODE_EXEC_QUEUE_KEY = "execQueue"


def resolve_node_exec_target(cfg: dict, node_config: dict, step_resources: dict) -> tuple:
    """Pick (exec_system, exec_queue) for one node, most specific first:

      1. the node's own execSystem/execQueue override (Run Configuration modal)
      2. the run's GPU pair, when the step's step.json declares resources.gpu
      3. the run's CPU pair (also the fallback when no GPU pair was given)

    `cfg` is the run's frozen_config, `node_config` the node's config_values,
    `step_resources` the step_type_registry.resources mirror of step.json.
    """
    cpu_system = cfg.get("exec_system") or "expanse-tapis"
    cpu_queue = cfg.get("exec_queue") or ""
    if (step_resources or {}).get("gpu"):
        exec_system = cfg.get("gpu_exec_system") or cpu_system
        exec_queue = cfg.get("gpu_exec_queue") or cpu_queue
    else:
        exec_system, exec_queue = cpu_system, cpu_queue

    node_config = node_config or {}
    # An explicit per-node choice always wins, including over the GPU routing.
    if node_config.get(_NODE_EXEC_SYSTEM_KEY):
        exec_system = node_config[_NODE_EXEC_SYSTEM_KEY]
        # A queue name is only meaningful on the system it belongs to, so a
        # node that overrode the system but not the queue must NOT keep the
        # run-level queue (e.g. OSC's "gpu" doesn't exist on Expanse). Blank
        # lets Tapis apply the system's own default queue instead of failing
        # on a name that isn't there.
        exec_queue = node_config.get(_NODE_EXEC_QUEUE_KEY) or ""
    elif node_config.get(_NODE_EXEC_QUEUE_KEY):
        exec_queue = node_config[_NODE_EXEC_QUEUE_KEY]
    return exec_system, exec_queue


@ds.transaction(isolation_level="READ COMMITTED")
def get_run_archive_context(run_id: int, node_key: str = None) -> dict:
    """Return substitution values for a step's Tapis job spec.

    Exec target is resolved PER NODE (see resolve_node_exec_target): a node's
    own execSystem/execQueue override wins, else a step declaring
    resources.gpu takes the run's GPU pair, else the run's CPU pair. So one run
    can put zero_shot_annotation on a GPU queue and flight_plan on a CPU queue
    without either step.json naming a site. execSystem{Exec,Input,Output}Dir
    follow THIS node's exec system.

    Archive location stays run-level, so every artifact lands on one system and
    no DAG edge becomes a cross-site transfer. For OUTPUT location we use a
    per-run workspace so every step's artifacts are isolated and downstream
    steps can find them deterministically:

        workspace = <work_dir>/wf_runs/<run_id>                  (on the exec/archive system)
        this step archives to  <workspace>/<step_type_key>/<node_id>

    step_type_key groups a run's steps by which step they are (e.g.
    'geospatial', 'yolo_inference') before the node id, purely for a more
    readable archive tree — node_id alone already uniquely identifies the step
    within the run; run_id is still what keeps two runs from colliding.

    The archive dir is always derived per-node (run_id/node_id is appended
    beneath whatever base is chosen — never taken from run options directly).
    That base defaults to work_dir + '/wf_runs', but frozen_config['archive_dir']
    (RunOptions.archive_dir) overrides it outright when set, letting a run
    archive somewhere other than under its exec system's own work_dir.

    exec_system_exec_dir / exec_system_input_dir / exec_system_output_dir feed
    the Tapis job's execSystemExecDir/execSystemInputDir/execSystemOutputDir
    fields (see _exec_system_dirs) — computed from this node's resolved
    exec_system, so no step.json needs to declare them itself.
    """
    session = ds.sql_session()
    run = session.execute(
        select(PipelineRun).where(PipelineRun.run_id == run_id)
    ).scalars().one()
    cfg = run.frozen_config or {}

    slurm_account = cfg.get("slurm_account", "")
    username = cfg.get("tapis_username", "")
    archive_dir_override = cfg.get("archive_dir", "")

    # The node being rendered, plus its step's declared compute requirement —
    # both needed before exec_system can be picked, since exec target is now
    # per-node (node override -> step's gpu hint -> the run's CPU/GPU pair).
    node = None
    if node_key is not None:
        node = session.execute(
            select(WfNode).where(WfNode.node_id == int(node_key))
        ).scalars().one()
    step_resources = {}
    if node is not None:
        registry = session.execute(
            select(StepTypeRegistry).where(StepTypeRegistry.step_type_key == node.step_type_key)
        ).scalars().first()
        step_resources = (registry and registry.resources) or {}

    exec_system, exec_queue = resolve_node_exec_target(
        cfg, (node.default_config if node is not None else {}) or {}, step_resources
    )

    # Archive stays RUN-level (one home for every artifact) even though exec
    # target varies per node: a downstream step's inputs are then always on the
    # same system, so no edge in the DAG turns into a cross-site transfer of a
    # full image directory. Only compute moves.
    archive_system = cfg.get("archive_system") or cfg.get("exec_system") or exec_system
    # work_dir therefore belongs to the ARCHIVE system, not this node's exec
    # system — it's only ever used as the archive base below. Derived from
    # archive_system when the run didn't set one, since a run spanning two
    # sites has no single correct scratch path to inherit.
    work_dir = cfg.get("work_dir", "") or _default_work_dir(archive_system, slurm_account, username)

    # Per-run workspace, then per-node archive dir within it — /run_id/node_id
    # is always appended (never taken from run options) so every step's output
    # stays isolated and downstream steps can find it deterministically. The
    # base it's appended to is archive_dir when the run set one, else derived
    # from work_dir as before.
    if archive_dir_override:
        ws_base = archive_dir_override.rstrip("/")
    elif work_dir:
        ws_base = work_dir.rstrip("/") + "/wf_runs"
    else:
        ws_base = "wf_runs"
    workspace = f"{ws_base}/{run_id}"
    # Per-node dir for a specific step, grouped by step type; the workspace
    # root when no node is given (e.g. the run-level archive base). `node` was
    # already loaded above — exec target resolution needs it before this point.
    if node is not None:
        archive_dir = f"{workspace}/{node.step_type_key}/{node_key}"
    else:
        archive_dir = workspace

    exec_dir, input_dir, output_dir = _exec_system_dirs(exec_system, slurm_account)

    return {
        "run_id": run_id,
        "slurm_account": slurm_account,
        "exec_system": exec_system,
        "exec_queue": exec_queue,
        "work_dir": work_dir,
        "workspace": workspace,
        "archive_system": archive_system,
        "archive_dir": archive_dir,
        # Avoid a double slash when archive_dir is absolute (starts with '/').
        "archive_uri": f"tapis://{archive_system}/{archive_dir.lstrip('/')}",
        "exec_system_exec_dir": exec_dir,
        "exec_system_input_dir": input_dir,
        "exec_system_output_dir": output_dir,
    }


@ds.transaction(isolation_level="READ COMMITTED")
def complete_run_step(run_id: int, node_key: str, outputs: dict):
    """Mark a step completed and persist its outputs."""
    session = ds.sql_session()
    run_step = _run_step(session, run_id, node_key)
    run_step.status = "completed"
    run_step.outputs = outputs or {}


@ds.transaction(isolation_level="READ COMMITTED")
def get_node_output(run_id: int, node_key: str) -> dict:
    """Return a completed node's outputs (empty dict if none)."""
    session = ds.sql_session()
    run_step = _run_step(session, run_id, node_key)
    return run_step.outputs or {}


@ds.transaction(isolation_level="READ COMMITTED")
def get_step_input(run_id: int, node_key: str) -> dict:
    """Return the configured inputs for a step (from run_step.config)."""
    session = ds.sql_session()
    run_step = _run_step(session, run_id, node_key)
    return run_step.config or {}


@ds.transaction(isolation_level="READ COMMITTED")
def update_step_inputs(run_id: int, node_key: str, resolved_inputs: dict):
    """Persist resolved inputs (parent references substituted) onto run_step.config."""
    session = ds.sql_session()
    run_step = _run_step(session, run_id, node_key)
    run_step.config = resolved_inputs or {}


@ds.transaction(isolation_level="READ COMMITTED")
def update_run_status(run_id: int, status: str):
    """Update overall pipeline_run.status."""
    session = ds.sql_session()
    run = session.execute(
        select(PipelineRun).where(PipelineRun.run_id == run_id)
    ).scalars().one()
    run.status = status


@ds.transaction(isolation_level="READ COMMITTED")
def get_run_details(dbos_workflow_id: str) -> dict:
    """Return run status + per-step detail, keyed by DBOS workflow id."""
    session = ds.sql_session()
    run = session.execute(
        select(PipelineRun).where(PipelineRun.dbos_workflow_id == dbos_workflow_id)
    ).scalars().first()
    if not run:
        return {}
    steps = session.execute(
        select(RunStep).where(RunStep.run_id == run.run_id).order_by(RunStep.run_step_id)
    ).scalars().all()
    return {
        "run_id": run.run_id,
        "run_status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "steps": [
            {
                "node_id": str(s.node_id),
                "status": s.status,
                "tapis_job_uuid": s.tapis_job_uuid,
                "tapis_job_status": s.tapis_job_status,
                "inputs": s.config or None,
                "outputs": s.outputs or None,
                "error_message": s.error_message,
            }
            for s in steps
        ],
    }


@ds.transaction(isolation_level="READ COMMITTED")
def get_run_graph_data(dbos_workflow_id: str) -> dict:
    """Return nodes/edges/statuses for ASCII rendering, keyed by DBOS workflow id."""
    session = ds.sql_session()
    run = session.execute(
        select(PipelineRun).where(PipelineRun.dbos_workflow_id == dbos_workflow_id)
    ).scalars().first()
    if not run:
        return {}

    nodes = session.execute(
        select(WfNode).where(WfNode.template_version_id == run.template_version_id)
    ).scalars().all()

    edges = session.execute(
        select(WfEdge).where(WfEdge.template_version_id == run.template_version_id)
    ).scalars().all()

    steps = session.execute(
        select(RunStep).where(RunStep.run_id == run.run_id)
    ).scalars().all()

    return {
        "nodes": [str(n.node_id) for n in nodes],
        "edges": [(str(e.source_node_id), str(e.target_node_id)) for e in edges],
        "statuses": {str(s.node_id): s.status for s in steps},
    }
