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

    template_version_id = dag_config["template_version_id"]

    # Resolve an owner so the NOT NULL user_id on pipeline_run is satisfied.
    user = session.execute(
        select(AppUser).where(AppUser.username == "mock_user")
    ).scalars().first()

    run = PipelineRun(
        template_version_id=template_version_id,
        user_id=user.user_id if user else None,
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
def get_run_archive_context(run_id: int) -> dict:
    """Return run-level substitution values: slurm_account + archive location.

    For a first real run these come from the run's frozen_config (so they can be
    passed at execute time) with safe defaults matching the legacy setup.
    """
    session = ds.sql_session()
    run = session.execute(
        select(PipelineRun).where(PipelineRun.run_id == run_id)
    ).scalars().one()
    cfg = run.frozen_config or {}
    archive_system = cfg.get("archive_system", "expanse-tapis")
    archive_dir = cfg.get("archive_dir", f"harvest_jobs/run-{run_id}")
    return {
        "slurm_account": cfg.get("slurm_account", ""),
        "archive_system": archive_system,
        "archive_dir": archive_dir,
        "archive_uri": f"tapis://{archive_system}/{archive_dir}",
        "run_id": run_id,
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
