"""DBOS workflows that execute a saved template DAG.

Port of dbos-example/app/workflows.py. The orchestration logic (dependency
graph, event-driven waiting, per-node child workflows) is unchanged; only the
transaction layer it calls was rewritten to target the Harvest schema, and the
run is created from an EXISTING template rather than from the POST body.
"""
import json

from dbos import DBOS, SetWorkflowID

from engine.transactions import (
    create_run_for_template,
    get_step_input,
    update_step_inputs,
    get_node_tapis_template,
    get_node_config_schema,
    get_node_output_ports,
    get_node_step_type,
    get_config_schema_defaults,
    get_incoming_edges,
    get_run_archive_context,
    get_run_steps_status,
    get_node_output,
    complete_run_step,
    update_run_status,
    update_run_step_status,
)
from engine.tapis import TapisV3, copy_tapis_path, resolve_latest_file, use_real_tapis
from engine import inline_steps
from engine import job_spec
from engine import secrets as secrets_store
from engine import single_job


# Context keys owned by get_run_archive_context and derived from each other —
# a node's own config may not override them. See their use in
# execute_node_workflow below.
_AUTHORITATIVE_CTX_KEYS = (
    "exec_system",
    "exec_queue",
    "archive_system",
    "archive_dir",
    "archive_uri",
    "exec_system_exec_dir",
    "exec_system_input_dir",
    "exec_system_output_dir",
)


def _resolve_inputs(run_id: int, node_key: str, node_config: dict) -> dict:
    """Resolve a node's input-port values from the edges feeding into it.

    For each incoming edge, look up the upstream step's output for the connected
    source port and bind it to this node's target input port. Values are Tapis
    URIs (paths), so the rendered job's fileInputs sourceUrls point at real
    upstream outputs — this is the edge-driven data flow. The node's own config
    (e.g. epochs) is merged in too, so config-derived placeholders resolve.
    """
    # Start from config_schema defaults, then let the node's own config override
    # them, then edge inputs override those. This ensures params the user didn't
    # set still get their declared default (e.g. imgsz=640).
    resolved = get_config_schema_defaults(node_key)
    resolved.update(node_config or {})
    for edge in get_incoming_edges(run_id, node_key):
        src_outputs = get_node_output(run_id, edge["source_node_key"])
        src_port = edge["source_port"]
        tgt_port = edge["target_port"]
        if tgt_port is None:
            continue
        # Prefer the output named exactly like the source port; else, if the
        # upstream produced a single output, use it; else pass the dict through.
        if src_port in src_outputs:
            resolved[tgt_port] = src_outputs[src_port]
        elif len(src_outputs) == 1:
            resolved[tgt_port] = next(iter(src_outputs.values()))
        else:
            resolved[tgt_port] = src_outputs
    return resolved


def _parse_tapis_uri(uri: str):
    """Split a tapis://system/path URI into (system_id, path).

    'tapis://pitzer-tapis/fs/ess/PAS2699/out' -> ('pitzer-tapis', '/fs/ess/PAS2699/out').
    A bare path (no tapis:// prefix) returns ('', path). Trailing/leading slashes
    on the path are preserved as an absolute path.
    """
    if not uri:
        return "", ""
    if uri.startswith("tapis://"):
        rest = uri[len("tapis://"):]
        parts = rest.split("/", 1)
        system = parts[0]
        path = "/" + parts[1] if len(parts) > 1 else "/"
        return system, path
    return "", uri


def _source_node_outputs(run_id: int, node_key: str, node_config: dict, resolved: dict) -> dict:
    """Outputs for a source (data-provider) node.

    A source node carries a user-entered `path` in its config and declares one
    or more output ports (e.g. source_image_dir -> 'images'). By default we
    expose that `path` on every output port so a downstream step's
    _resolve_inputs binds it into the step's fileInputs.

    A port whose name already matches a key in `resolved` uses that value
    instead of `path` — this lets a design-time step pass an UPSTREAM input
    straight through on one port while exposing its own config `path` on
    another. E.g. smart_labeler wires an 'images' input (an upstream image
    directory) and has its own 'path' config (where it writes
    annotations.json): its 'images' output passes the wired directory
    through unchanged, while its 'annotations' output exposes `path`.

    A source step whose config_schema declares a "system" field (e.g.
    source_geopackage, source_shapefile) lets the user enter a bare path
    (no tapis:// scheme) and pick the Tapis system separately, rather than
    hand-typing a full URI. If `path` isn't already a URI (no "://"),
    `system` is prefixed on here so every downstream consumer (job
    fileInputs, the geospatial resource's URI parser, ...) sees the same
    tapis://system/path shape a job step's own outputs use.
    """
    from engine.local_bundle import NODE_LOCAL_SYSTEM

    path = (node_config or {}).get("path", "")
    system = (node_config or {}).get("system", "")
    # A "My node" path has no Tapis system to join it to. _launch_run refuses
    # such a workflow before it gets here; this keeps the value honest anyway.
    if system and system != NODE_LOCAL_SYSTEM and path and "://" not in path:
        path = f"tapis://{system}/{path.lstrip('/')}"
    outputs = {"path": path}
    for port in get_node_output_ports(node_key):
        name = port["name"]
        outputs[name] = resolved[name] if name in resolved else path
    return outputs


def _run_sink_node(run_id: int, node_key: str, node_config: dict) -> dict:
    """Execute a sink node: copy its single incoming artifact to the user path.

    A sink has one input fed by an upstream output port. That port already
    resolved to a concrete Tapis URI (in _resolve_inputs, stored on run_step
    config as the 'data' input). We copy that source path to the sink's
    configured destination path via the Tapis Files API. This is what makes a
    specific output (e.g. just 'predictions') land at a user-chosen location,
    independent of where the producing step archived.
    """
    # The sink's settings hold a system plus a bare path (a tapis_path field),
    # joined here exactly as the local bundle's _platform_source_path does; an
    # older template's full tapis:// URI in `path` still works as-is.
    cfg = node_config or {}
    path, system = str(cfg.get("path") or ""), str(cfg.get("system") or "")
    dest = f"tapis://{system}/{path.lstrip('/')}" if system and path and "://" not in path else path
    # The resolved input value for this sink's 'data' port (set by _resolve_inputs).
    resolved = get_step_input(run_id, node_key)  # includes resolved input ports
    src = resolved.get("data") or resolved.get("path") or ""
    # Raising fails the step with this message, instead of a green node that
    # copied nothing.
    if not path:
        raise RuntimeError("This sink has no destination: open its settings and choose a system and path.")
    if system == "node-local":
        raise RuntimeError("This sink writes to 'My node', which only a run on your own node can reach. "
                           "Pick a Tapis system for platform runs.")
    if "://" not in dest:
        raise RuntimeError(f"This sink's destination '{path}' has no Tapis system: open its settings and pick one.")
    if not src:
        raise RuntimeError("Nothing reached this sink's input: check that it is wired to an upstream output.")
    # replace=True: a folder output replaces the destination folder's contents,
    # so it reflects only this run; a single file only replaces itself.
    ok = copy_tapis_path(src, dest, run_id, replace=True)
    print(f"[sink] copy {src} -> {dest} (replace): {'OK' if ok else 'FAILED'}")
    if not ok:
        raise RuntimeError(f"Copying {src} to {dest} failed; the backend log has the Tapis error.")
    return {"path": dest, "source": src, "copied": True}


def _resolve_secrets(node_key: str, ctx: dict, team_id: int | None) -> tuple[dict, list[str]]:
    """Substitute "secret"-typed config fields with their real decrypted value,
    for job-template rendering ONLY.

    A "secret" field's value in ctx (from node config / _resolve_inputs) is a
    KEY reference (e.g. "WANDB_API_KEY"), not the value itself — that's what's
    persisted onto run_step.config. Here we look up the actual value (scoped to
    the run owner's team) and return an updated ctx for job_spec.render, plus
    the list of real values substituted so the caller can redact them from any
    logging of the rendered job spec (see engine.tapis.submit_job).

    This is the config_schema "type": "secret" path — the user PICKS a secret
    via a UI dropdown, and the step.json only ever sees a field name. For a
    step author hardcoding a specific, always-the-same secret directly in the
    tapis_job template (no per-node UI at all), see job_spec.resolve_secret_refs
    and its ${secrets.KEY} syntax instead.
    """
    schema = get_node_config_schema(node_key)
    secret_fields = [k for k, v in schema.items() if isinstance(v, dict) and v.get("type") == "secret"]
    if not secret_fields:
        return ctx, []

    resolved_ctx = dict(ctx)
    values = []
    for field in secret_fields:
        key_ref = ctx.get(field)
        if not key_ref:
            continue
        value = secrets_store.resolve_secret(team_id, key_ref)
        if value is not None:
            resolved_ctx[field] = value
            values.append(value)
    return resolved_ctx, values


def _derive_outputs(run_id: int, node_key: str, ctx: dict, job_uuid: str) -> dict:
    """Compute each output PORT's Tapis URI under this step's archive dir.

    The step archived its whole job output to ctx['archive_uri']
    (= tapis://<sys>/<workspace>/<node_id>). Each declared output port maps to a
    specific artifact via its output_path (e.g. predictions -> predictions.json),
    so a downstream node/sink connecting to that port gets exactly that path.
    Ports without an output_path fall back to the whole archive dir.

    A port with a `file_glob` (see StepTypePort.file_glob) has an output_path
    that's a DIRECTORY holding one dynamically-named file (e.g. a tool that
    stamps its own output filename with a timestamp) rather than the artifact
    itself — the directory's real content is only knowable now that the job
    has actually run, so it's resolved here via engine.tapis.resolve_latest_file
    (a real Tapis Files listing) rather than left as a static template
    substitution like every other port. Falls back to the plain directory URI
    if nothing matches (or in mock mode) — a downstream consumer then sees a
    directory instead of a file, same as before this existed, rather than the
    whole step failing over a listing hiccup.
    """
    archive_uri = (ctx.get("archive_uri") or "").rstrip("/")
    outputs = {"archive_uri": archive_uri, "job_uuid": job_uuid}
    for port in get_node_output_ports(node_key):
        sub = job_spec.render_path(port.get("output_path"), ctx)
        dir_uri = f"{archive_uri}/{sub}" if sub else archive_uri
        pattern = port.get("file_glob")
        if pattern:
            resolved = resolve_latest_file(dir_uri, run_id, pattern)
            outputs[port["name"]] = resolved or dir_uri
        else:
            outputs[port["name"]] = dir_uri
    return outputs


@DBOS.workflow()
def execute_node_workflow(node_key: str, run_id: int, orchestrator_workflow_id: str):
    """Execute a single DAG node: resolve edge inputs, render + submit the Tapis
    job spec, poll to completion, record outputs."""
    update_run_step_status(run_id, node_key, "running")

    # 1. Resolve this node's inputs from incoming edges + its own config.
    node_config = get_step_input(run_id, node_key)
    resolved = _resolve_inputs(run_id, node_key, node_config)
    update_step_inputs(run_id, node_key, resolved)

    # 2. Fetch the step's Tapis job template. A step with NO template is a
    #    data-provider/consumer node handled specially:
    #      - sink node:   copies its incoming artifact to its configured `path`
    #      - inline step: does real work here in-process (engine.inline_steps)
    #      - source node: exposes its configured `path` on its output port(s)
    template = get_node_tapis_template(node_key)
    if not template:
        step_type = get_node_step_type(node_key)
        try:
            if step_type and step_type.startswith("sink"):
                outputs = _run_sink_node(run_id, node_key, node_config)
            elif inline_steps.get_handler(step_type):
                # A no-Tapis-job step whose work still has to happen DURING the
                # run — e.g. annotation_format_adapter converting a file an
                # upstream step only just produced. Running it here (rather than
                # leaving it to a design-time button) is what makes its declared
                # output actually exist by the time a downstream job stages it.
                outputs = inline_steps.run_inline_step(
                    run_id, node_key, step_type, resolved, get_node_output_ports(node_key)
                )
            else:
                outputs = _source_node_outputs(run_id, node_key, node_config, resolved)
        except Exception as e:
            # This branch returns before reaching the job path's own handler
            # below, and an inline step can genuinely fail (unreadable source,
            # unset destination, expired token). Without the same "mark failed +
            # signal the orchestrator" treatment, the DAG would wait forever on a
            # step that already died.
            update_run_step_status(run_id, node_key, "failed", error_message=str(e))
            DBOS.send(destination_id=orchestrator_workflow_id, message=node_key, topic="step_complete")
            raise
        complete_run_step(run_id, node_key, outputs)
        DBOS.send(destination_id=orchestrator_workflow_id, message=node_key, topic="step_complete")
        return outputs

    # 3. Build the substitution context. The step archives to its own per-node
    #    dir in the run workspace; per-port output paths are derived from that.
    archive_ctx = get_run_archive_context(run_id, node_key)
    # `resolved` (config + edge inputs) spreads last so a step's own config
    # keys win for its own ${...} placeholders — but the exec/archive keys
    # below are authoritative and must NOT be shadowed. get_run_archive_context
    # derives archive_dir, archive_uri and execSystem*Dir FROM the resolved
    # exec system, so a stray config key of the same name would produce a job
    # running on one system with directories computed for another. Per-node
    # exec overrides are camelCase (execSystem/execQueue) precisely so they
    # never collide here; this re-assert is the belt-and-braces for a step.json
    # that names a config field after one of these by accident.
    ctx = {**archive_ctx, **resolved, **{k: archive_ctx[k] for k in _AUTHORITATIVE_CTX_KEYS if k in archive_ctx}}
    team_id = secrets_store.get_run_team_id(run_id)

    # Rendering is inside the try along with submission: a step whose spec can't
    # be built (or whose inputs can't be materialized) has failed just as surely
    # as one Tapis rejected, and needs the same "mark failed + signal the
    # orchestrator" treatment — otherwise the DAG waits forever on a node that
    # already raised.
    try:
        # Some job steps need a file materialized on Tapis before the job can
        # stage it — a pipeline the panel edits in the browser and keeps on the
        # node config, say (see engine.inline_steps.PRE_SUBMIT_HANDLERS). Runs
        # with the full context available so it can place the file relative to
        # this run's archive dir, and its overrides land in ctx before render()
        # so the template's placeholder resolves to what was actually written.
        ctx.update(inline_steps.run_pre_submit(run_id, get_node_step_type(node_key), ctx))

        # "secret"-typed config fields hold a KEY reference in ctx (and in whatever
        # gets persisted above) — swap in the real value for rendering only, and
        # keep the values around to redact from any logging of the rendered spec.
        render_ctx, secret_values = _resolve_secrets(node_key, ctx, team_id)
        rendered = job_spec.render(template, render_ctx)
        # A step.json can also hardcode a specific secret directly in the template
        # via ${secrets.KEY} (see job_spec.resolve_secret_refs) — no config_schema
        # field or per-node UI involved. Resolved after render() since it doesn't
        # depend on any resolved config value, just the literal ${secrets.KEY} text.
        rendered, secret_ref_values = job_spec.resolve_secret_refs(rendered, team_id)
        secret_values = secret_values + secret_ref_values
        # Per-step compute resources (nodeCount, coresPerNode, memoryMB, maxMinutes,
        # gpus), set via the canvas's Run Configuration panel and carried in the
        # node's own config alongside its business params.
        job_spec.apply_resource_overrides(rendered, resolved)
        rendered.setdefault("name", f"run-{run_id}-{node_key}")

        # 3. Submit and poll.
        job_uuid = TapisV3.submit_job(rendered, run_id, redact=secret_values)
        update_run_step_status(
            run_id, node_key, "running",
            tapis_job_uuid=job_uuid, tapis_job_status="PENDING",
        )

        # TODO: make this event-driven instead of polling.
        # Only a real terminal Tapis status fails the step. A transient
        # status-check error (network blip, brief token expiry that survived the
        # in-call refresh) must NOT be mistaken for a job failure — otherwise a
        # long-running job gets falsely marked failed while it's still running.
        consecutive_poll_errors = 0
        while True:
            try:
                status = TapisV3.check_job_status(job_uuid, run_id)
                consecutive_poll_errors = 0
            except Exception as poll_err:
                consecutive_poll_errors += 1
                print(f"[workflow] status poll error for {job_uuid} "
                      f"(attempt {consecutive_poll_errors}): {type(poll_err).__name__}")
                # Give up only after many consecutive failures (~5 min of retries).
                if consecutive_poll_errors >= 100:
                    raise RuntimeError(
                        f"Lost contact with Tapis while polling job {job_uuid}: {poll_err}")
                DBOS.sleep(3)
                continue

            update_run_step_status(run_id, node_key, "running", tapis_job_status=status)
            if status == "FINISHED":
                break
            elif status in ["FAILED", "CANCELLED"]:
                raise RuntimeError(f"Tapis Job {job_uuid} failed with status: {status}")
            DBOS.sleep(3)

        # 4. Record each output port's path (Tapis URI) for downstream steps.
        outputs = _derive_outputs(run_id, node_key, ctx, job_uuid)
        print(f"[workflows] node {node_key} (run {run_id}) derived outputs: {outputs}")
        complete_run_step(run_id, node_key, outputs)

        DBOS.send(destination_id=orchestrator_workflow_id, message=node_key, topic="step_complete")
        return outputs

    except Exception as e:
        update_run_step_status(run_id, node_key, "failed", error_message=str(e))
        DBOS.send(destination_id=orchestrator_workflow_id, message=node_key, topic="step_complete")
        raise e


# --- Single-job execution mode -----------------------------------------------
# One Tapis job runs the whole pipeline, instead of one job per node. The plan is
# built and frozen onto the run at launch (engine/single_job.py, called from
# main._launch_run), so everything here works from dag_config["single_job"] and
# never re-derives it — what executes is exactly what the user was shown before
# they launched.


def _upload_pipeline_plan(plan: dict, run_id: int) -> str:
    """Put the bundle on Tapis so the job can stage it, and return its URI.

    The plan file lands beside the run's archive dir, which makes it part of the
    run's record: the exact graph this job executed stays recoverable next to its
    outputs, rather than living only in a database column.
    """
    from annotation_adapter import _tapis_upload

    system = plan["exec"]["archive_system"]
    path = plan["pipeline_path"]
    uri = f"tapis://{system}/{path.lstrip('/')}"
    if not use_real_tapis():
        print(f"[single-job] MOCK mode — not uploading the plan; pretending it is at {uri}")
        return uri
    token = _require_token(run_id)
    _tapis_upload(system, path, json.dumps(plan["bundle"], indent=2).encode("utf-8"),
                  token, "application/json")
    print(f"[single-job] uploaded plan -> {uri}")
    return uri


def _require_token(run_id: int) -> str:
    from engine.tapis import TapisAuthError
    from engine import tapis_auth

    token = tapis_auth.get_token_for_run(run_id)
    if not token:
        raise TapisAuthError(
            "Tapis authentication required — the run owner's session has expired. "
            "Please log in again and re-run."
        )
    return token


def _fetch_run_report(plan: dict, run_id: int) -> dict | None:
    """The runner's own report for this job, read back out of the archive.

    The runner writes run-<id>-report.json under its workdir with each step's
    status, timing and resolved output paths (see runner/wf_runner's
    write_report). Reading it is what lets a single job still populate PER-STEP
    detail in the UI rather than one opaque green bar.

    Best effort on purpose: a missing or unreadable report must not fail a run
    whose job Tapis has already reported FINISHED. The caller falls back to the
    output locations baked into the plan, which are correct by construction —
    the report only ever refines them (a file_glob port resolved to its real
    filename, say).
    """
    if not use_real_tapis():
        return None
    from annotation_adapter import _tapis_get_bytes

    system = plan["exec"]["archive_system"]
    path = f"{plan['exec']['archive_dir'].rstrip('/')}/run-{run_id}-report.json"
    try:
        raw = _tapis_get_bytes(system, path, _require_token(run_id))
        return json.loads(raw)
    except Exception as e:
        print(f"[single-job] could not read the run report at tapis://{system}{path}: "
              f"{type(e).__name__}: {e} — falling back to the plan's own output paths.")
        return None


def _archived(plan: dict, run_id: int, plan_path: str) -> str:
    """A path in the plan -> the tapis:// URI it archived to."""
    return single_job.archived_uri(
        plan["exec"]["archive_system"], plan["exec"]["archive_dir"], plan_path, run_id,
    )


def _single_job_spec(plan: dict, run_id: int, pipeline_uri: str) -> tuple:
    """The frozen job spec, finished off with the two things planning can't know:
    where the uploaded plan ended up, and the real values of the secrets it names.

    Returns (spec, secret_values) — the values so the caller can keep them out of
    any logging of the spec, exactly as the per-step path does.

    NOT job_spec.render(): that helper also rewrites the fields a per-step job
    derives from its render context, and would DELETE execSystem{Exec,Input,Output}Dir
    here (it drops each one whose context value is absent, and this context has
    only the plan URI). Those fields are already final in the frozen spec, and
    losing them is not neutral — Tapis fills anything a job omits from the APP
    definition, so the job would silently inherit whatever paths wf-runner was
    registered with instead of the ones computed for this run's exec system. See
    engine.transactions._exec_system_dirs. The plan has exactly one placeholder,
    so substitute that one and nothing else.
    """
    spec = json.loads(json.dumps(plan["job"]).replace("${pipeline_uri}", pipeline_uri))
    team_id = secrets_store.get_run_team_id(run_id)
    spec, secret_values = job_spec.resolve_secret_refs(spec, team_id)
    spec["name"] = f"wf-pipeline-{run_id}"
    return spec, secret_values


def _complete_single_job(plan: dict, run_id: int, job_uuid: str) -> None:
    """Record per-step results once the one job has finished.

    Every node gets a real status and real outputs, so the run page reads the
    same whichever mode produced it:

      * a job step's outputs come from the runner's report when it could be read,
        translated from plan paths to their archived URIs, else from the plan's
        own baked paths;
      * a source node republishes its configured location, as it would in
        per-step mode;
      * a sink is COPIED to its configured destination here. The runner's own copy
        only reached the job directory — off platform the data root is the user's
        real destination, but on platform it is a location inside the job, so this
        is the step that actually delivers the artifact.
    """
    report = _fetch_run_report(plan, run_id)
    reported = {}
    if report:
        reported = {str(s.get("node_id")): s for s in report.get("steps") or [] if s.get("node_id")}

    for node in plan["bundle"].get("nodes", []):
        node_key = str(node["node_id"])
        kind = node.get("kind")
        if kind == "job":
            baked = {port: _archived(plan, run_id, path) for port, path in (node.get("outputs") or {}).items()}
            step_report = reported.get(node_key) or {}
            for port, actual in (step_report.get("outputs") or {}).items():
                # The report's paths are the authoritative ones: a port whose
                # artifact the step named itself at run time (file_glob) only
                # resolves once it has run.
                baked[port] = _archived(plan, run_id, actual)
            baked["archive_uri"] = _archived(plan, run_id, node.get("job_dir") or "")
            baked["job_uuid"] = job_uuid
            status = (step_report.get("status") or "completed").lower()
            if status not in ("completed", "skipped", "dry-run"):
                update_run_step_status(run_id, node_key, "failed",
                                       error_message=step_report.get("error") or "Step failed inside the pipeline job")
                continue
            complete_run_step(run_id, node_key, baked)
        elif kind == "sink":
            dest = node.get("platform_dest") or ""
            src = _archived(plan, run_id, node.get("source") or "")
            copied = False
            if src and dest and use_real_tapis():
                copied = bool(copy_tapis_path(src, dest, run_id, replace=True))
                print(f"[single-job] sink copy {src} -> {dest}: {'OK' if copied else 'FAILED'}")
            complete_run_step(run_id, node_key, {"path": dest, "source": src, "copied": copied})
        else:
            # Source / design-time passthrough: same outputs per-step mode would
            # publish — its own configured location on every declared port.
            path = node.get("platform_path") or ""
            outputs = {"path": path}
            for port in (node.get("outputs") or {}):
                outputs.setdefault(port, path)
            complete_run_step(run_id, node_key, outputs)


def run_pipeline_as_single_job(dag_config: dict, run_id: int) -> dict:
    """Execute the whole DAG as ONE Tapis job.

    Deliberately linear, with no per-node child workflows: there is one job to
    submit and one to poll, and the ordering of the steps inside it is the
    runner's business (the plan carries the topological order). The DAG-advancing
    machinery in dag_orchestrator_workflow has nothing to do here.
    """
    plan = dag_config.get("single_job")
    if not plan:
        raise RuntimeError(
            "This run was launched in single-job mode but carries no frozen plan. "
            "Re-launch it from the template."
        )
    if plan.get("blockers"):
        # Launch already refuses these (main._launch_run); re-checked here because
        # a resume replays a frozen config that could predate a step change.
        raise RuntimeError("Cannot run as a single job: " + " ".join(plan["blockers"]))

    job_keys = [s["node_key"] for s in plan.get("steps", [])]
    print(f"[single-job] run {run_id}: {len(job_keys)} job step(s), "
          f"{plan['resources']['nodeCount']} node(s) x {plan['resources']['coresPerNode']} core(s), "
          f"{plan['resources']['memoryMB']}MB, {plan['resources']['maxMinutes']}min, "
          f"gpus={plan['resources']['gpus']} on {plan['exec']['exec_system']}"
          f"/{plan['exec']['exec_queue'] or 'default'}")

    try:
        pipeline_uri = _upload_pipeline_plan(plan, run_id)
        spec, secret_values = _single_job_spec(plan, run_id, pipeline_uri)
        job_uuid = TapisV3.submit_job(spec, run_id, redact=secret_values)
    except Exception as e:
        # Nothing was submitted, so every step failed before it started. Marking
        # them all (rather than only the first) is what keeps the run page honest.
        for node_key in job_keys:
            update_run_step_status(run_id, node_key, "failed", error_message=str(e))
        update_run_status(run_id, "FAILED")
        raise

    # The single job IS every step's job, so each one carries its uuid — that is
    # what lets Stop cancel the pipeline and the run page link to the job.
    for node_key in job_keys:
        update_run_step_status(run_id, node_key, "running",
                               tapis_job_uuid=job_uuid, tapis_job_status="PENDING")

    consecutive_poll_errors = 0
    while True:
        try:
            status = TapisV3.check_job_status(job_uuid, run_id)
            consecutive_poll_errors = 0
        except Exception as poll_err:
            consecutive_poll_errors += 1
            print(f"[single-job] status poll error for {job_uuid} "
                  f"(attempt {consecutive_poll_errors}): {type(poll_err).__name__}")
            if consecutive_poll_errors >= 100:
                for node_key in job_keys:
                    update_run_step_status(run_id, node_key, "failed",
                                           error_message=f"Lost contact with Tapis: {poll_err}")
                update_run_status(run_id, "FAILED")
                raise RuntimeError(f"Lost contact with Tapis while polling job {job_uuid}: {poll_err}")
            DBOS.sleep(3)
            continue

        for node_key in job_keys:
            update_run_step_status(run_id, node_key, "running", tapis_job_status=status)
        if status == "FINISHED":
            break
        if status in ("FAILED", "CANCELLED"):
            for node_key in job_keys:
                update_run_step_status(run_id, node_key, "failed",
                                       error_message=f"Pipeline job {job_uuid} ended as {status}")
            update_run_status(run_id, "FAILED")
            raise RuntimeError(f"Tapis Job {job_uuid} failed with status: {status}")
        DBOS.sleep(3)

    _complete_single_job(plan, run_id, job_uuid)

    statuses = get_run_steps_status(run_id)
    if any(s == "failed" for s in statuses.values()):
        update_run_status(run_id, "FAILED")
        raise RuntimeError("Pipeline job finished but one or more steps reported failure.")
    update_run_status(run_id, "COMPLETED")
    return {"status": "SUCCESS", "job_uuid": job_uuid, "mode": "single_job"}


@DBOS.workflow()
def dag_orchestrator_workflow(dag_config: dict):
    """Orchestrate execution of a full template DAG.

    dag_config carries template_version_id plus the nodes/edges built from the
    saved template. Creates the run records, then advances ready nodes (all deps
    completed) as concurrent child workflows, waiting on completion signals.
    """
    w_id = DBOS.workflow_id

    run_id = create_run_for_template(w_id, dag_config)

    # Single-job mode replaces this orchestration entirely — one job, submitted
    # and polled once (see run_pipeline_as_single_job). Branching here rather
    # than at the HTTP layer keeps one entry point for launch, stop and resume:
    # all three already start THIS workflow, and each stays correct for either
    # mode without knowing which it is.
    if (dag_config.get("execution_mode") or "per_step") == "single_job":
        return run_pipeline_as_single_job(dag_config, run_id)

    nodes = {str(node["id"]): node for node in dag_config.get("nodes", [])}
    edges = dag_config.get("edges", [])

    # Dependency graph: node -> set of upstream node keys.
    deps = {node_key: set() for node_key in nodes}
    for edge in edges:
        deps[str(edge["to"])].add(str(edge["from"]))

    # TODO: make this event-driven rather than a polling loop.
    while True:
        statuses = get_run_steps_status(run_id)

        if any(status == "failed" for status in statuses.values()):
            # A real failure occurred. Every pending node reachable from a
            # failed (or already-blocked) node can never run — mark it
            # "blocked" rather than "failed" so the UI can tell "this step
            # broke" apart from "skipped because an upstream step broke".
            # Loop to fixed point since blocking must cascade transitively
            # (grandchildren of the failed node are pending too).
            changed = True
            while changed:
                changed = False
                for node_key, status in statuses.items():
                    if status == "pending" and any(
                        statuses.get(d) in ("failed", "blocked") for d in deps[node_key]
                    ):
                        update_run_step_status(run_id, node_key, "blocked", error_message="Upstream step failed")
                        statuses[node_key] = "blocked"
                        changed = True
            update_run_status(run_id, "FAILED")
            raise RuntimeError("DAG execution failed due to task failure.")

        if all(status == "completed" for status in statuses.values()):
            break

        # Spawn nodes whose dependencies are all completed.
        for node_key, status in statuses.items():
            if status == "pending":
                if all(statuses.get(d) == "completed" for d in deps[node_key]):
                    with SetWorkflowID(f"{w_id}-{node_key}"):
                        DBOS.start_workflow(execute_node_workflow, node_key, run_id, w_id)

        # Block until a child signals completion (or time out and re-check).
        try:
            DBOS.recv(topic="step_complete", timeout_seconds=30)
        except Exception:
            pass

    update_run_status(run_id, "COMPLETED")
    return {"status": "SUCCESS"}
