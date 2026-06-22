from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from dbos import DBOS, DBOSConfig
import uvicorn

from app.models import init_db, DATABASE_URL
from app.workflows import dag_orchestrator_workflow
from app.transactions import (
    mock_step_types,
    get_run_details,
    get_workflow_run_graph_data
)
from app.utils.graph import render_ascii_graph

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    mock_step_types()
    yield

app = FastAPI(title="DBOS + FastAPI AI Workflow with Tapis", lifespan=lifespan)

@app.post("/workflow/run")
def trigger_workflow(dag_config: dict):
    """FastAPI endpoint to start a new DAG workflow execution.

    What it does & How it works:
        Triggers `dag_orchestrator_workflow` asynchronously using `DBOS.start_workflow`
        with the provided DAG configuration and returns the generated workflow ID.

    Inputs:
        dag_config (dict): The JSON DAG configuration defining nodes, inputs, and edges.

    Outputs:
        dict: A response containing a success message and the initiated DBOS workflow ID.
    """
    handle = DBOS.start_workflow(dag_orchestrator_workflow, dag_config)
    return {
        "message": "DAG Workflow started",
        "workflow_id": handle.get_workflow_id()
    }

@app.get("/workflow/{workflow_id}")
def get_workflow_status(workflow_id: str, format: str = "json"):
    """FastAPI endpoint to retrieve the current execution status and database details of a workflow run.

    What it does & How it works:
        Queries the DBOS system for the lifecycle status of the workflow. If it doesn't exist,
        raises a 404 HTTP Exception. Otherwise, calls `get_run_details` to retrieve the database
        records and returns a combined dictionary.

    Inputs:
        workflow_id (str): The unique DBOS workflow ID of the workflow run.
        format (str): The response format ('json' or 'text'). Defaults to 'json'.

    Outputs:
        dict: A response containing the workflow ID, overall DBOS lifecycle state, database record, and progress graph.
    """
    status = DBOS.get_workflow_status(workflow_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
        
    db_details = get_run_details(workflow_id)
    
    # Generate ASCII graph
    graph_data = get_workflow_run_graph_data(workflow_id)
    progress_graph = ""
    if graph_data:
        progress_graph = render_ascii_graph(
            graph_data["nodes"],
            graph_data["edges"],
            graph_data["statuses"]
        )
        
    if format == "text":
        return PlainTextResponse(progress_graph)
        
    return {
        "workflow_id": workflow_id,
        "workflow_state": status.status,
        "database_record": db_details,
        "progress_graph": progress_graph
    }

config: DBOSConfig = {
    "name": "dbos-example",
    "system_database_url": DATABASE_URL,
}

DBOS(fastapi=app, config=config)

if __name__ == "__main__":
    DBOS.launch()
    print("Starting FastAPI app on http://localhost:8000...")
