from dbos import DBOS, SetWorkflowID

from app.integrations.TapisV3 import TapisV3
from app.transactions import (
    complete_run_step,
    create_workflow_from_config,
    get_run_steps_status,
    get_tapis_info_from_wf_node,
    get_wf_node_output,
    get_wf_step_input,
    update_run_step_status,
    update_wf_run_status,
    update_wf_step_inputs,
)


async def _resolve_inputs(wf_run_id: int, input_specs: dict) -> dict:
    """Resolves data dependencies by substituting parent-step output references into inputs.

    Inputs:
        run_id (int): The database ID of the pipeline run.
        input_specs (dict): A dictionary of raw input specifications, potentially containing references.

    Outputs:
        dict: A dictionary of fully resolved input values ready for execution.
    """
    resolved = {}
    for param_name, spec_value in input_specs.items():
        if isinstance(spec_value, str) and "." in spec_value:
            parent_node_label, key = spec_value.split(".", 1)
            parent_outputs = await get_wf_node_output(wf_run_id, parent_node_label)
            resolved[param_name] = parent_outputs.get(key)
        else:
            resolved[param_name] = spec_value
    return resolved


@DBOS.workflow()
async def execute_node_workflow(node_label: str, run_id: int, orchestrator_workflow_id: str):
    """A DBOS workflow that handles the execution of a single node in the DAG.

    Inputs:
        node_label (str): The label identifying the node to execute.
        run_id (int): The database ID of the workflow run.
        orchestrator_workflow_id (str): The DBOS workflow ID of the orchestrator to notify upon completion.

    Outputs:
        dict: The resulting output dictionary of the executed node on success.
    """
    # Update step to running
    await update_run_step_status(run_id, node_label, "running")

    # Get step inputs
    input_specs = await get_wf_step_input(run_id, node_label)
    resolved = await _resolve_inputs(run_id, input_specs)
    await update_wf_step_inputs(run_id, node_label, resolved)

    # Get Tapis app info
    tapis_app_id = await get_tapis_info_from_wf_node(run_id, node_label)

    # Submit job to Mock Tapis
    try:
        args = [str(v) for v in resolved.values()]

        job_uuid = await TapisV3.submit_job(
            app_id=tapis_app_id, app_version="1.0.0", name=f"run-{run_id}-{node_label}", args=args
        )

        # Update tapis status
        await update_run_step_status(
            run_id, node_label, "running", tapis_job_uuid=job_uuid, tapis_job_status="PENDING"
        )

        # Poll until Tapis job is finished or failed
        # TODO make this non-blocking instead of polling
        while True:
            status = await TapisV3.check_job_status(job_uuid)
            await update_run_step_status(run_id, node_label, "running", tapis_job_status=status)
            if status == "FINISHED":
                break
            elif status in ["FAILED", "CANCELLED"]:
                raise RuntimeError(f"Tapis Job {job_uuid} failed with status: {status}")
            await DBOS.sleep_async(3)

        # Mock outputs
        if tapis_app_id == "preprocessing-pipeline":
            outputs = {"dataset_path": f"tapis-outputs/{job_uuid}/preprocessed_dataset"}
        elif tapis_app_id == "training-pipeline":
            outputs = {"model_path": f"tapis-outputs/{job_uuid}/model.tar.gz"}
        elif tapis_app_id == "inference-pipeline":
            outputs = {"model_accuracy": 0.92}
        else:
            outputs = {"output_path": f"tapis-outputs/{job_uuid}/out"}

        # Complete run step
        await complete_run_step(run_id, node_label, outputs)

        # Notify orchestrator of completion
        await DBOS.send_async(
            destination_id=orchestrator_workflow_id, message=node_label, topic="step_complete"
        )
        return outputs

    except Exception as e:
        # Update run step to failed
        await update_run_step_status(run_id, node_label, "failed", error_message=str(e))
        # Notify orchestrator of failure
        await DBOS.send_async(
            destination_id=orchestrator_workflow_id, message=node_label, topic="step_complete"
        )
        raise e


@DBOS.workflow()
async def dag_orchestrator_workflow(dag_config: dict):
    """The orchestrator DBOS workflow managing execution of the entire DAG pipeline. This is the main workflow.

    What it does & How it works:
        1. Initializes the DAG run database records.
        2. Parses nodes, edges, and dependencies.
        3. Loops to monitor and advance node execution:
           a. Checks statuses. If any task failed, propagates failures to downstream pending tasks and throws.
           b. If all tasks completed, breaks out of loop.
           c. Identifies pending tasks whose dependencies are fully completed.
           d. Starts execution sub-workflows (`execute_node_workflow`) for ready tasks concurrently,
              using `DBOS.start_workflow`.
           e. Uses event-driven sleep (`DBOS.recv`) to wait for a completion signal from child tasks,
              preventing active busy waiting.
        4. Marks the run status as 'COMPLETED' upon successful traversal.

    Inputs:
        dag_config (dict): The complete JSON DAG configuration containing nodes and edges.

    Outputs:
        dict: A status dictionary containing `{"status": "SUCCESS"}` upon successful pipeline completion.
    """
    w_id = DBOS.workflow_id

    # Create workflow run
    run_id = await create_workflow_from_config(w_id, dag_config)
    nodes = {node["id"]: node for node in dag_config.get("nodes", [])}
    edges = dag_config.get("edges", [])

    # Create dependency graph
    deps = {node_id: set() for node_id in nodes}
    for edge in edges:
        deps[edge["to"]].add(edge["from"])

    # Loop until all nodes are completed or failed
    while True:
        statuses = await get_run_steps_status(run_id)

        all_done = True
        for node_id, status in statuses.items():
            if status == "failed":
                # Workflow failed - TODO: verify code
                for node_id, status in statuses.items():
                    if status == "pending":
                        node_deps = deps[node_id]
                        if any(statuses.get(d) == "failed" for d in node_deps):
                            await update_run_step_status(
                                run_id, node_id, "failed", error_message="Dependency failed"
                            )
                await update_wf_run_status(run_id, "FAILED")
                raise RuntimeError("DAG execution failed due to task failure.")
            elif status != "completed":
                all_done = False

        if all_done:
            break

        # Spawn ready nodes
        for node_id, status in statuses.items():
            if status == "pending":
                node_deps = deps[node_id]
                if all(statuses.get(d) == "completed" for d in node_deps):
                    # Node dependencies are completed
                    with SetWorkflowID(f"{w_id}-{node_id}"):
                        await DBOS.start_workflow_async(execute_node_workflow, node_id, run_id, w_id)

        # Event-driven wait: block until notified by execute_node_workflow of a step completion
        try:
            await DBOS.recv_async(topic="step_complete", timeout_seconds=30)
        except Exception as e:
            print(f"Error: {e}")
            pass

    await update_wf_run_status(run_id, "COMPLETED")
    return {"status": "SUCCESS"}
