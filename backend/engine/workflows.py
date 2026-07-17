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


def _source_node_outputs(run_id: int, node_key: str, node_config: dict) -> dict:
    """Outputs for a source (data-provider) node.

    A source node carries a user-entered `path` in its config and declares one
    or more output ports (e.g. source_image_dir -> 'images'). We expose that path
    on every output port so a downstream step's _resolve_inputs binds it into the
    step's fileInputs. Also expose it under 'path' for convenience.
    """
    path = (node_config or {}).get("path", "")
    outputs = {"path": path}
    for port in get_node_output_ports(node_key):
        outputs[port["name"]] = path
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
            outputs = _source_node_outputs(run_id, node_key, node_config)
        complete_run_step(run_id, node_key, outputs)
        DBOS.send(destination_id=orchestrator_workflow_id, message=node_key, topic="step_complete")
        return outputs

    # 3. Build the substitution context. The step archives to its own per-node
    #    dir in the run workspace; per-port output paths are derived from that.
    ctx = {**get_run_archive_context(run_id, node_key), **resolved}
    rendered = job_spec.render(template, ctx)
    rendered.setdefault("name", f"run-{run_id}-{node_key}")

    try:
        # 3. Submit and poll.
        job_uuid = TapisV3.submit_job(rendered, run_id)
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

        all_done = True
        for node_key, status in statuses.items():
            if status == "failed":
                # Propagate failure to pending downstream nodes, then abort.
                for n_key, n_status in statuses.items():
                    if n_status == "pending":
                        if any(statuses.get(d) == "failed" for d in deps[n_key]):
                            update_run_step_status(run_id, n_key, "failed", error_message="Dependency failed")
                update_run_status(run_id, "FAILED")
                raise RuntimeError("DAG execution failed due to task failure.")
            elif status != "completed":
                all_done = False

        if all_done:
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
