import json

from dbos import SQLAlchemyDatasource
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.models import StepType, Workflow, WFNode, WFEdge, WFRun, RunStep, engine, DATABASE_URL

ds = SQLAlchemyDatasource.create(DATABASE_URL, engine=engine)

@ds.transaction(isolation_level="READ COMMITTED")
def create_workflow_from_config(workflow_id: str, wf_config: dict) -> int:
    """Initializes a new DAG workflow from a JSON configuration.

    Inputs:
        workflow_id (str): The DBOS workflow ID mapping this run to a durable DBOS workflow.
        wf_config (dict): The JSON DAG configuration containing nodes (with types, labels, and inputs) and edges (dependencies).

    Outputs:
        int: The unique run ID (`wf_run_id`) created for the workflow.
    """
    session = ds.sql_session()
    
    # 1. Create workflow
    workflow = Workflow(name="Custom DAG Template", description="Parsed from JSON POST")
    session.add(workflow)
    session.flush()
    
    # 2. Populate WF Nodes
    nodes_map = {}
    for node_data in wf_config.get("nodes", []):
        step_type_key = node_data["type"]
        wf_node = WFNode(
            workflow_id=workflow.workflow_id,
            step_type_key=step_type_key,
            node_label=node_data["id"]
        )
        session.add(wf_node)
        session.flush()
        nodes_map[node_data["id"]] = wf_node.wf_node_id
        
    # 3. Populate WF Edges
    for edge_data in wf_config.get("edges", []):
        wf_edge = WFEdge(
            workflow_id=workflow.workflow_id,
            source_wf_node_id=nodes_map[edge_data["from"]],
            target_wf_node_id=nodes_map[edge_data["to"]]
        )
        session.add(wf_edge)
    session.flush()
        
    # 4. Create WF Run
    wf_run = WFRun(
        workflow_id=workflow.workflow_id,
        dbos_workflow_id=workflow_id,
        status="RUNNING"
    )
    session.add(wf_run)
    session.flush()
    
    # 5. Create Run Steps
    for node_label, wf_node_id in nodes_map.items():
        # Check if custom configs/inputs are passed
        node_cfg = next(n for n in wf_config["nodes"] if n["id"] == node_label)
        run_step = RunStep(
            wf_run_id=wf_run.wf_run_id,
            wf_node_id=wf_node_id,
            status="pending",
            inputs=json.dumps(node_cfg.get("inputs", {}) if "inputs" in node_cfg else {"dataset_url": node_cfg.get("dataset_url", "")})
        )
        session.add(run_step)
        
    return wf_run.wf_run_id

@ds.transaction(isolation_level="READ COMMITTED")
def get_run_steps_status(wf_run_id: int) -> dict:
    """Retrieves the execution status of all nodes/steps in a pipeline run.

    Inputs:
        wf_run_id (int): The database ID of the WF Run.

    Outputs:
        dict: A dictionary mapping node labels (str) to their current status (str, e.g., 'pending', 'running', 'completed', 'failed').
    """
    session = ds.sql_session()
    steps = session.execute(
        select(RunStep)
        .where(RunStep.wf_run_id == wf_run_id)
        .options(joinedload(RunStep.wf_node))
        .order_by(RunStep.run_step_id)
    ).scalars().all()

    return {step.wf_node.node_label: step.status for step in steps}

@ds.transaction(isolation_level="READ COMMITTED")
def update_run_step_status(wf_run_id: int, node_label: str, status: str, tapis_job_uuid: str = None, tapis_job_status: str = None, error_message: str = None):
    """Updates the status and metadata of a specific node/step within a workflow run.

    Inputs:
        wf_run_id (int): The database ID of the WF Run.
        node_label (str): The label of the node to update.
        status (str): The new status for the step (e.g., 'pending', 'running', 'completed', 'failed').
        tapis_job_uuid (str, optional): The UUID of the mock Tapis job executing this step.
        tapis_job_status (str, optional): The current status returned by the Tapis API.
        error_message (str, optional): The error description if the step execution fails.

    Outputs:
        None
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WFNode)
        .join(Workflow).join(WFRun)
        .where(WFRun.wf_run_id == wf_run_id, WFNode.node_label == node_label)
    ).scalars().one()
    run_step = session.execute(
        select(RunStep).where(RunStep.wf_run_id == wf_run_id, RunStep.wf_node_id == wf_node.wf_node_id)
    ).scalars().one()

    run_step.status = status
    if tapis_job_uuid is not None:
        run_step.tapis_job_uuid = tapis_job_uuid
    if tapis_job_status is not None:
        run_step.tapis_job_status = tapis_job_status
    if error_message is not None:
        run_step.error_message = error_message

@ds.transaction(isolation_level="READ COMMITTED")
def get_tapis_info_from_wf_node(run_id: int, node_label: str):
    """Retrieves the Tapis application ID mapped to the step type of a specific node.

    Inputs:
        run_id (int): The database ID of the pipeline run.
        node_label (str): The label of the node.

    Outputs:
        str: The Tapis application ID associated with the node's step type.
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WFNode)
        .join(Workflow).join(WFRun)
        .options(joinedload(WFNode.step_type))
        .where(WFRun.wf_run_id == run_id, WFNode.node_label == node_label)
    ).scalars().one()

    return wf_node.step_type.tapis_app_id

@ds.transaction(isolation_level="READ COMMITTED")
def complete_run_step(wf_run_id: int, node_label: str, outputs: dict):
    """Marks a step as successfully completed and saves its outputs.

    Inputs:
        run_id (int): The database ID of the pipeline run.
        node_label (str): The label of the successfully completed node.
        outputs (dict): A dictionary representing the output variables and values produced by this step.

    Outputs:
        None
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WFNode)
        .join(Workflow).join(WFRun)
        .where(WFRun.wf_run_id == wf_run_id, WFNode.node_label == node_label)
    ).scalars().one()
    run_step = session.execute(
        select(RunStep).where(RunStep.wf_run_id == wf_run_id, RunStep.wf_node_id == wf_node.wf_node_id)
    ).scalars().one()

    run_step.status = "completed"
    run_step.outputs = json.dumps(outputs)

@ds.transaction(isolation_level="READ COMMITTED")
def get_wf_node_output(wf_run_id: int, node_label: str) -> dict:
    """Retrieves the serialized outputs of a completed node.

    Inputs:
        run_id (int): The database ID of the pipeline run.
        node_label (str): The label of the node whose outputs are to be retrieved.

    Outputs:
        dict: The deserialized outputs of the node as a dictionary. Returns an empty dict if no outputs exist.
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WFNode)
        .join(Workflow).join(WFRun)
        .where(WFRun.wf_run_id == wf_run_id, WFNode.node_label == node_label)
    ).scalars().one()
    run_step = session.execute(
        select(RunStep).where(RunStep.wf_run_id == wf_run_id, RunStep.wf_node_id == wf_node.wf_node_id)
    ).scalars().one()

    if run_step.outputs:
        return json.loads(run_step.outputs)
    return {}

@ds.transaction(isolation_level="READ COMMITTED")
def update_wf_run_status(wf_run_id: int, status: str):
    """Updates the overall status of the workflow run.

    Inputs:
        wf_run_id (int): The database ID of the workflow run.
        status (str): The new overall status of the run.

    Outputs:
        None
    """
    session = ds.sql_session()
    wf_run = session.execute(select(WFRun).where(WFRun.wf_run_id == wf_run_id)).scalars().one()
    wf_run.status = status

@ds.transaction(isolation_level="READ COMMITTED")
def get_wf_step_input(wf_run_id: int, node_label: str) -> dict:
    """Retrieves the inputs configured for a specific step.

    Inputs:
        wf_run_id (int): The database ID of the workflow run.
        node_label (str): The label of the node whose inputs are being requested.

    Outputs:
        dict: The inputs configuration dictionary for the step. Returns an empty dict if none exist.
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WFNode)
        .join(Workflow).join(WFRun)
        .where(WFRun.wf_run_id == wf_run_id, WFNode.node_label == node_label)
    ).scalars().one()
    run_step = session.execute(
        select(RunStep).where(RunStep.wf_run_id == wf_run_id, RunStep.wf_node_id == wf_node.wf_node_id)
    ).scalars().one()

    return json.loads(run_step.inputs) if run_step.inputs else {}

@ds.transaction(isolation_level="READ COMMITTED")
def update_wf_step_inputs(wf_run_id: int, node_label: str, resolved_inputs: dict):
    """Saves the resolved parameter inputs back to a step's record.

    Inputs:
        wf_run_id (int): The database ID of the workflow run.
        node_label (str): The label of the node whose resolved inputs are being updated.
        resolved_inputs (dict): The dictionary of inputs with resolved parent-step references.

    Outputs:
        None
    """
    session = ds.sql_session()
    wf_node = session.execute(
        select(WFNode)
        .join(Workflow).join(WFRun)
        .where(WFRun.wf_run_id == wf_run_id, WFNode.node_label == node_label)
    ).scalars().one()
    run_step = session.execute(
        select(RunStep).where(RunStep.wf_run_id == wf_run_id, RunStep.wf_node_id == wf_node.wf_node_id)
    ).scalars().one()

    run_step.inputs = json.dumps(resolved_inputs)

@ds.transaction(isolation_level="READ COMMITTED")
def get_run_details(workflow_id: str) -> dict:
    """Retrieves all RunStep records for a workflow run.

    Inputs:
        workflow_id (str): The unique DBOS workflow ID of the workflow run.

    Outputs:
        dict: A detailed status summary containing run status, creation time, and details for each step.
              Returns an empty dict if the run is not found.
    """
    session = ds.sql_session()
    wf_run = session.execute(select(WFRun).where(WFRun.dbos_workflow_id == workflow_id)).scalars().first()
    if not wf_run:
        return {}
    run_steps = session.execute(
        select(RunStep)
        .where(RunStep.wf_run_id == wf_run.wf_run_id)
        .options(joinedload(RunStep.wf_node))
    ).scalars().all()
    return {
        "run_status": wf_run.status,
        "created_at": wf_run.created_at.isoformat() if wf_run.created_at else None,
        "steps": [
            {
                "node_id": s.wf_node.node_label,
                "status": s.status,
                "tapis_job_uuid": s.tapis_job_uuid,
                "tapis_job_status": s.tapis_job_status,
                "inputs": json.loads(s.inputs) if s.inputs else None,
                "outputs": json.loads(s.outputs) if s.outputs else None,
                "error_message": s.error_message
            }
            for s in run_steps
        ]
    }

@ds.transaction(isolation_level="READ COMMITTED")
def get_workflow_run_graph_data(workflow_id: str) -> dict:
    """Retrieves nodes, edges, and step statuses for a workflow run.

    Inputs:
        workflow_id (str): The unique DBOS workflow ID of the workflow run.

    Outputs:
        dict: A dictionary containing nodes (list), edges (list of tuples), and statuses (dict).
              Returns an empty dict if the run is not found.
    """
    session = ds.sql_session()
    wf_run = session.execute(
        select(WFRun)
        .where(WFRun.dbos_workflow_id == workflow_id)
        .options(joinedload(WFRun.workflow))
    ).scalars().first()
    if not wf_run:
        return {}
    
    nodes = session.execute(
        select(WFNode)
        .where(WFNode.workflow_id == wf_run.workflow_id)
    ).scalars().all()
    
    edges = session.execute(
        select(WFEdge)
        .where(WFEdge.workflow_id == wf_run.workflow_id)
        .options(joinedload(WFEdge.source_wf_node), joinedload(WFEdge.target_wf_node))
    ).scalars().all()
    
    run_steps = session.execute(
        select(RunStep)
        .where(RunStep.wf_run_id == wf_run.wf_run_id)
        .options(joinedload(RunStep.wf_node))
    ).scalars().all()
    
    return {
        "nodes": [n.node_label for n in nodes],
        "edges": [(e.source_wf_node.node_label, e.target_wf_node.node_label) for e in edges],
        "statuses": {s.wf_node.node_label: s.status for s in run_steps}
    }

@ds.transaction(isolation_level="READ COMMITTED")
def mock_step_types():
    """Seeds the database with predefined step types.

    Inputs:
        None

    Outputs:
        None
    """
    session = ds.sql_session()
    
    # Seed step types mapping keys to tapis apps
    types_data = [
        {"key": "preprocess", "app": "preprocessing-pipeline", "name": "Preprocessing"},
        {"key": "train", "app": "training-pipeline", "name": "Training"},
        {"key": "inference", "app": "inference-pipeline", "name": "Inference"}
    ]
    
    for t in types_data:
        existing = session.execute(select(StepType).where(StepType.step_type_key == t["key"])).scalars().first()
        if not existing:
            session.add(StepType(
                step_type_key=t["key"],
                tapis_app_id=t["app"],
                display_name=t["name"]
            ))
