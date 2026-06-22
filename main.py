import os
import random
from fastapi import FastAPI, HTTPException
from dbos import DBOS, DBOSConfig
import uvicorn
from sqlalchemy.orm import sessionmaker

from models import PipelineRun, engine, init_db, DATABASE_URL
from mock_tapis import tapis_client

# DBOS Steps
@DBOS.step()
def preprocess_data(dataset_url: str) -> str:
    print(f"[Step 1] Preprocessing dataset from {dataset_url}...")
    import time
    time.sleep(5)
    return "preprocessed_dataset_path_s3"

@DBOS.step()
def submit_tapis_training_job(dataset_path: str) -> str:
    print("[Step 2] Submitting training job to Tapis...")
    response = tapis_client.jobs.submitJob(
        name="ai-model-training",
        appId="tensorflow-train-app",
        appVersion="1.0.0",
        parameterSet={"args": [dataset_path]}
    )
    return response.uuid

@DBOS.step()
def check_tapis_job_status(job_uuid: str) -> str:
    return tapis_client.jobs.getJobStatus(jobUuid=job_uuid).status

@DBOS.step()
def run_inference(model_path: str) -> float:
    print(f"[Step 4] Running model evaluation/inference for: {model_path}...")
    import time
    time.sleep(2)
    return round(random.uniform(0.85, 0.99), 4)

@DBOS.transaction()
def init_pipeline_run(workflow_id: str, dataset_url: str):
    session = DBOS.sql_session
    run = PipelineRun(
        workflow_id=workflow_id,
        dataset_url=dataset_url,
        status="PREPROCESSING"
    )
    session.add(run)

@DBOS.transaction()
def update_pipeline_tapis_job(workflow_id: str, job_uuid: str, status: str):
    session = DBOS.sql_session
    run = session.query(PipelineRun).filter_by(workflow_id=workflow_id).one()
    run.tapis_job_uuid = job_uuid
    run.status = status

@DBOS.transaction()
def finalize_pipeline_run(workflow_id: str, accuracy: float, status: str):
    session = DBOS.sql_session
    run = session.query(PipelineRun).filter_by(workflow_id=workflow_id).one()
    run.model_accuracy = accuracy
    run.status = status

@DBOS.workflow()
def ai_pipeline_workflow(dataset_url: str):
    w_id = DBOS.workflow_id
    
    # 1. Initialize DB Record & Preprocess
    init_pipeline_run(w_id, dataset_url)
    clean_dataset = preprocess_data(dataset_url)
    
    # 2. Submit Tapis Job
    update_pipeline_tapis_job(w_id, None, "SUBMITTING_TRAINING")
    job_uuid = submit_tapis_training_job(clean_dataset)
    update_pipeline_tapis_job(w_id, job_uuid, "TRAINING")
    
    # 3. Poll Tapis Job status
    while True:
        status = check_tapis_job_status(job_uuid)
        if status == "FINISHED":
            break
        elif status in ["FAILED", "CANCELLED"]:
            finalize_pipeline_run(w_id, 0.0, "FAILED")
            raise RuntimeError(f"Tapis Job {job_uuid} failed with status: {status}")
        
        # Durable sleep between polling checks
        DBOS.sleep(3)
    
    # 4. Run inference
    update_pipeline_tapis_job(w_id, job_uuid, "INFERENCE")
    model_path = f"tapis-outputs/{job_uuid}/model.tar.gz"
    accuracy = run_inference(model_path)
    
    # 5. Finalize
    finalize_pipeline_run(w_id, accuracy, "COMPLETED")
    return {"accuracy": accuracy, "model_path": model_path}

# FastAPI Endpoints & App Setup
app = FastAPI(title="DBOS + FastAPI AI Pipeline with Tapis")

@app.post("/pipeline/run")
def trigger_pipeline(dataset_url: str):
    handle = DBOS.start_workflow(ai_pipeline_workflow, dataset_url)
    return {
        "message": "AI Pipeline started",
        "workflow_id": handle.get_workflow_id()
    }

@app.get("/pipeline/{workflow_id}")
def get_pipeline_status(workflow_id: str):
    status = DBOS.get_workflow_status(workflow_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
        
    Session = sessionmaker(bind=engine)
    with Session() as session:
        run = session.query(PipelineRun).filter_by(workflow_id=workflow_id).first()
        
    db_details = {}
    if run:
        db_details = {
            "dataset_url": run.dataset_url,
            "tapis_job_uuid": run.tapis_job_uuid,
            "pipeline_status": run.status,
            "model_accuracy": run.model_accuracy,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None
        }
        
    return {
        "workflow_id": workflow_id,
        "workflow_state": status.status,
        "database_record": db_details
    }

if __name__ == "__main__":
    config: DBOSConfig = {
        "name": "dbos-ai-pipeline",
        "system_database_url": DATABASE_URL,
        "application_database_url": DATABASE_URL,
    }
    
    # Initialize application schemas
    init_db()
    
    # Bind DBOS and launch durable runtime
    DBOS(config=config)
    DBOS.launch()
    
    print("Starting FastAPI app on http://localhost:8000...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
