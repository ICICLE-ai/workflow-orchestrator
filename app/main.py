# ruff: noqa: E402
from dotenv import load_dotenv

load_dotenv()

import os
from contextlib import asynccontextmanager

import uvicorn
from dbos import DBOS, DBOSConfig
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from app.models import init_db
from app.transactions import get_run_details, get_workflow_run_graph_data, mock_step_types
from app.utils.graph import render_ascii_graph
from app.workflows import dag_orchestrator_workflow


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    from app.transactions import ads

    await ads.run_migrations()

    await mock_step_types()
    yield


app = FastAPI(title="DBOS + FastAPI AI Workflow with Tapis", lifespan=lifespan)


@app.post("/workflow/run")
async def trigger_workflow(dag_config: dict):
    handle = await DBOS.start_workflow_async(dag_orchestrator_workflow, dag_config)
    return {"message": "DAG Workflow started", "workflow_id": handle.get_workflow_id()}


@app.get("/workflow/{workflow_id}")
async def get_workflow_status(workflow_id: str, format: str = "json"):
    status = await DBOS.get_workflow_status_async(workflow_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    db_details = await get_run_details(workflow_id)

    graph_data = await get_workflow_run_graph_data(workflow_id)
    progress_graph = ""
    if graph_data:
        progress_graph = render_ascii_graph(graph_data["nodes"], graph_data["edges"], graph_data["statuses"])

    if format == "text":
        return PlainTextResponse(progress_graph)

    return {
        "workflow_id": workflow_id,
        "workflow_state": status.status,
        "database_record": db_details,
        "progress_graph": progress_graph,
    }


config: DBOSConfig = {
    "name": "workflow-orchestrator",
    "system_database_url": os.environ.get("DBOS_DATABASE_URL"),
}

DBOS(fastapi=app, config=config)

if __name__ == "__main__":
    DBOS.launch()
    print("Starting FastAPI app on http://localhost:8000...")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
