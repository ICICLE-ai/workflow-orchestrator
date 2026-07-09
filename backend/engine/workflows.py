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
    get_incoming_edges,
    get_downstream_sink_path,
    get_run_archive_context,
    get_run_steps_status,
    get_node_output,
    complete_run_step,
    update_run_status,
    update_run_step_status,
)
from engine.tapis import TapisV3
from engine import job_spec


def _resolve_inputs(run_id: int, node_key: str, node_config: dict) -> dict:
    """Resolve a node's input-port values from the edges feeding into it.

    For each incoming edge, look up the upstream step's output for the connected
    source port and bind it to this node's target input port. Values are Tapis
    URIs (paths), so the rendered job's fileInputs sourceUrls point at real
    upstream outputs — this is the edge-driven data flow. The node's own config
    (e.g. epochs) is merged in too, so config-derived placeholders resolve.
    """
    resolved = dict(node_config or {})
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
    for port_name in get_node_output_ports(node_key):
        outputs[port_name] = path
    return outputs


def _derive_outputs(node_key: str, template: dict, ctx: dict, job_uuid: str) -> dict:
    """Compute this step's output values as Tapis URIs under its archive dir.

    Each declared output port becomes a path the next step can consume. We key
    them by the job's archive location so downstream fileInputs resolve to real
    produced data. (Output port names come from the rendered template's notion of
    outputs is not present, so we expose a generic archive uri plus per-call uuid.)
    """
    archive_uri = ctx.get("archive_uri", "")
    return {"archive_uri": archive_uri, "output_dir": f"{archive_uri}/output", "job_uuid": job_uuid}


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
    #    "source" node (data-provider): it runs no Tapis job — it simply exposes
    #    its configured `path` on its output port(s) for downstream steps, then
    #    completes immediately.
    template = get_node_tapis_template(node_key)
    if not template:
        outputs = _source_node_outputs(run_id, node_key, node_config)
        complete_run_step(run_id, node_key, outputs)
        DBOS.send(destination_id=orchestrator_workflow_id, message=node_key, topic="step_complete")
        return outputs

    # 3. Build the substitution context and render the Tapis job spec.
    ctx = {**get_run_archive_context(run_id), **resolved}

    # If this step feeds a "Write to Path" (sink_path) node, that sink's path
    # becomes where the step's output is archived — overriding the run-level
    # archive location. This is how a sink node writes output to a chosen path.
    sink_path = get_downstream_sink_path(run_id, node_key)
    if sink_path:
        sys_id, sub_dir = _parse_tapis_uri(sink_path)
        if sys_id:
            ctx["archive_system"] = sys_id
        ctx["archive_dir"] = sub_dir
        ctx["archive_uri"] = sink_path

    rendered = job_spec.render(template, ctx)
    rendered.setdefault("name", f"run-{run_id}-{node_key}")

    try:
        # 3. Submit and poll.
        job_uuid = TapisV3.submit_job(rendered)
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
                status = TapisV3.check_job_status(job_uuid)
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

        # 4. Record outputs (Tapis URIs) for downstream steps.
        outputs = _derive_outputs(node_key, template, ctx, job_uuid)
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
