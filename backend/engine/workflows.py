"""DBOS workflows that execute a saved template DAG.

Port of dbos-example/app/workflows.py. The orchestration logic (dependency
graph, event-driven waiting, per-node child workflows) is unchanged; only the
transaction layer it calls was rewritten to target the Harvest schema, and the
run is created from an EXISTING template rather than from the POST body.
"""
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
from engine.tapis import TapisV3, copy_tapis_path
from engine import job_spec
from engine import secrets as secrets_store


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
    path = (node_config or {}).get("path", "")
    system = (node_config or {}).get("system", "")
    if system and path and "://" not in path:
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
    dest = (node_config or {}).get("path", "")
    # The resolved input value for this sink's 'data' port (set by _resolve_inputs).
    resolved = get_step_input(run_id, node_key)  # includes resolved input ports
    src = resolved.get("data") or resolved.get("path") or ""
    if not src or not dest:
        return {"path": dest, "copied": False, "note": "missing source or destination"}
    # replace=True clears the destination first, so the sink dir reflects only
    # this run's wired output — no residue from prior runs with older layouts.
    ok = copy_tapis_path(src, dest, run_id, replace=True)
    print(f"[sink] copy {src} -> {dest} (replace): {'OK' if ok else 'FAILED'}")
    return {"path": dest, "source": src, "copied": bool(ok)}


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


def _derive_outputs(run_id: str, node_key: str, ctx: dict, job_uuid: str) -> dict:
    """Compute each output PORT's Tapis URI under this step's archive dir.

    The step archived its whole job output to ctx['archive_uri']
    (= tapis://<sys>/<workspace>/<node_id>). Each declared output port maps to a
    specific artifact via its output_path (e.g. predictions -> predictions.json),
    so a downstream node/sink connecting to that port gets exactly that path.
    Ports without an output_path fall back to the whole archive dir.
    """
    archive_uri = (ctx.get("archive_uri") or "").rstrip("/")
    outputs = {"archive_uri": archive_uri, "job_uuid": job_uuid}
    for port in get_node_output_ports(node_key):
        sub = port.get("output_path")
        outputs[port["name"]] = f"{archive_uri}/{sub}" if sub else archive_uri
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
    #      - source node: exposes its configured `path` on its output port(s)
    #      - sink node:   copies its incoming artifact to its configured `path`
    template = get_node_tapis_template(node_key)
    if not template:
        step_type = get_node_step_type(node_key)
        if step_type and step_type.startswith("sink"):
            outputs = _run_sink_node(run_id, node_key, node_config)
        else:
            outputs = _source_node_outputs(run_id, node_key, node_config, resolved)
        complete_run_step(run_id, node_key, outputs)
        DBOS.send(destination_id=orchestrator_workflow_id, message=node_key, topic="step_complete")
        return outputs

    # 3. Build the substitution context. The step archives to its own per-node
    #    dir in the run workspace; per-port output paths are derived from that.
    ctx = {**get_run_archive_context(run_id, node_key), **resolved}
    team_id = secrets_store.get_run_team_id(run_id)
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

    try:
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


@DBOS.workflow()
def dag_orchestrator_workflow(dag_config: dict):
    """Orchestrate execution of a full template DAG.

    dag_config carries template_version_id plus the nodes/edges built from the
    saved template. Creates the run records, then advances ready nodes (all deps
    completed) as concurrent child workflows, waiting on completion signals.
    """
    w_id = DBOS.workflow_id

    run_id = create_run_for_template(w_id, dag_config)
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
