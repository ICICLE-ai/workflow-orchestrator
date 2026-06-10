from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, Session
import os
import uvicorn

from models import Base, StepTypeRegistry, StepTypePort, WorkflowTemplate, PipelineRun, AppUser, WfNode, WfEdge
import auth
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import json
import glob

# Database Configuration
# Using psycopg2 driver
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5433")
DB_NAME = os.getenv("DB_NAME", "harvest")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")

SQLALCHEMY_DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

app = FastAPI(title="Harvest Tapis Backend")

# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def sync_step_registry(db: Session):
    print("Syncing step registry from JSON files...")
    step_files = glob.glob(os.path.join(os.path.dirname(__file__), "steps", "*", "step.json"))
    for file_path in step_files:
        with open(file_path, "r") as f:
            data = json.load(f)
            
        step_key = data["step_type_key"]
        
        # Upsert Registry entry (always safe)
        registry_entry = db.query(StepTypeRegistry).filter_by(step_type_key=step_key).first()
        if not registry_entry:
            registry_entry = StepTypeRegistry(step_type_key=step_key)
            db.add(registry_entry)
            
        registry_entry.display_name = data.get("display_name", step_key)
        registry_entry.description = data.get("description", "")
        registry_entry.config_schema = data.get("config_schema", {})
        db.commit()
        print(f"  Synced registry: {step_key} (config_schema keys: {list(data.get('config_schema', {}).keys())})")
        
        # Upsert Ports (don't delete — just add missing ones)
        for direction, port_list in [("input", data.get("inputs", [])), ("output", data.get("outputs", []))]:
            for p in port_list:
                existing = db.query(StepTypePort).filter_by(
                    step_type_key=step_key, port_name=p["name"], direction=direction
                ).first()
                if not existing:
                    try:
                        db.add(StepTypePort(step_type_key=step_key, port_name=p["name"], data_type=p["type"], direction=direction))
                        db.commit()
                    except Exception as e:
                        db.rollback()
                        print(f"  Warning: Could not add port {p['name']} for {step_key}: {e}")
    
    # Prune: deactivate steps that no longer have a JSON file
    json_keys = set()
    for file_path in step_files:
        with open(file_path, "r") as f:
            json_keys.add(json.load(f)["step_type_key"])
    
    all_db_steps = db.query(StepTypeRegistry).all()
    for db_step in all_db_steps:
        if db_step.step_type_key not in json_keys:
            if db_step.is_active:
                db_step.is_active = False
                db.commit()
                print(f"  Deactivated stale step: {db_step.step_type_key}")
        else:
            if not db_step.is_active:
                db_step.is_active = True
                db.commit()
                print(f"  Reactivated step: {db_step.step_type_key}")
    
    print("Step registry sync complete!")

@app.on_event("startup")
def on_startup():
    print("Creating database schema...")
    try:
        # Enable PostGIS extension before creating tables with Geometry
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        
        # NOTE: In production, you would use Alembic migrations instead of create_all
        Base.metadata.create_all(bind=engine)
        print("Database schema created.")
        
        # Sync Dynamic Steps
        db = SessionLocal()
        sync_step_registry(db)
        db.close()
        
    except Exception as e:
        print(f"Warning: Could not initialize database schema locally: {e}")
        print("This is expected if PostGIS is not installed natively. Please use Docker for DB.")

# Include the Authentication Router
app.include_router(auth.router)

@app.get("/")
def read_root():
    return {"message": "Welcome to the Harvest Tapis Backend API"}

# --- Pydantic Models for Workflow Template Saving ---
class NodeModel(BaseModel):
    id: str
    type: str
    position: Dict[str, float]
    data: Dict[str, Any]

class EdgeModel(BaseModel):
    id: str
    source: str
    target: str
    sourceHandle: Optional[str] = None
    targetHandle: Optional[str] = None

class WorkflowTemplateCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    category: Optional[str] = "Custom"
    nodes: List[NodeModel]
    edges: List[EdgeModel]

# --- API Endpoints ---

@app.get("/api/step-types")
def get_step_types(db: Session = Depends(get_db)):
    steps = db.query(StepTypeRegistry).filter(StepTypeRegistry.is_active == True).all()
    ports = db.query(StepTypePort).all()
    
    result = []
    for step in steps:
        step_ports = [p for p in ports if p.step_type_key == step.step_type_key]
        inputs = [{"port_name": p.port_name, "data_type": p.data_type, "is_required": p.is_required} for p in step_ports if p.direction == "input"]
        outputs = [{"port_name": p.port_name, "data_type": p.data_type, "is_required": p.is_required} for p in step_ports if p.direction == "output"]
        
        result.append({
            "step_type_key": step.step_type_key,
            "display_name": step.display_name,
            "description": step.description,
            "category": step.category,
            "icon": step.icon,
            "config_schema": step.config_schema,
            "inputs": inputs,
            "outputs": outputs
        })
    return result

@app.get("/api/workflow-templates")
def list_workflow_templates(db: Session = Depends(get_db)):
    # Group by template_id to get the latest version of each template
    subquery = db.query(
        WorkflowTemplate.template_id,
        func.max(WorkflowTemplate.version).label("max_version")
    ).group_by(WorkflowTemplate.template_id).subquery()

    templates = db.query(WorkflowTemplate).join(
        subquery,
        (WorkflowTemplate.template_id == subquery.c.template_id) &
        (WorkflowTemplate.version == subquery.c.max_version)
    ).all()
    
    return [
        {
            "template_version_id": t.template_version_id,
            "template_id": t.template_id,
            "version": t.version,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "created_at": t.created_at
        } for t in templates
    ]

@app.get("/api/workflow-templates/{template_id}/history")
def get_workflow_template_history(template_id: int, db: Session = Depends(get_db)):
    versions = db.query(WorkflowTemplate).filter(WorkflowTemplate.template_id == template_id).order_by(WorkflowTemplate.version.desc()).all()
    if not versions:
        raise HTTPException(status_code=404, detail="Template history not found")
        
    return [
        {
            "template_version_id": t.template_version_id,
            "template_id": t.template_id,
            "version": t.version,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "created_at": t.created_at
        } for t in versions
    ]

@app.get("/api/workflow-templates/{template_version_id}")
def get_workflow_template(template_version_id: int, db: Session = Depends(get_db)):
    template = db.query(WorkflowTemplate).filter(WorkflowTemplate.template_version_id == template_version_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
        
    nodes = db.query(WfNode).filter(WfNode.template_version_id == template_version_id).all()
    edges = db.query(WfEdge).filter(WfEdge.template_version_id == template_version_id).all()
    
    ports = db.query(StepTypePort).all()
    port_by_id = {p.port_id: p for p in ports}
    
    edge_list = []
    for e in edges:
        source_port = port_by_id.get(e.source_port_id)
        target_port = port_by_id.get(e.target_port_id)
        edge_list.append({
            "id": f"e_{e.source_node_id}_{e.target_node_id}",
            "source": str(e.source_node_id),
            "target": str(e.target_node_id),
            "sourceHandle": source_port.port_name if source_port else None,
            "targetHandle": target_port.port_name if target_port else None
        })

    return {
        "template_version_id": template.template_version_id,
        "template_id": template.template_id,
        "version": template.version,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "nodes": [{"id": str(n.node_id), "type": "customNode", "position": {"x": n.position_x, "y": n.position_y}, "data": {"nodeType": n.step_type_key, "config_values": n.default_config}} for n in nodes],
        "edges": edge_list
    }

@app.post("/api/workflow-templates")
def create_workflow_template(template: WorkflowTemplateCreate, db: Session = Depends(get_db)):
    # 1. Get next template_id
    max_id = db.query(func.max(WorkflowTemplate.template_id)).scalar() or 0
    next_id = max_id + 1
    
    user = db.query(AppUser).filter(AppUser.username == "mock_user").first()
    owner_id = user.user_id if user else None

    new_template = WorkflowTemplate(
        template_id=next_id,
        version=1,
        name=template.name,
        description=template.description,
        category=template.category,
        owner_id=owner_id
    )
    db.add(new_template)
    db.commit()
    db.refresh(new_template)
    
    # Insert Nodes and map IDs
    node_mapping = {}
    for n in template.nodes:
        wf_node = WfNode(
            template_version_id=new_template.template_version_id,
            step_type_key=n.type,
            position_x=n.position.get("x", 0),
            position_y=n.position.get("y", 0),
            default_config=n.data.get("config_values", {})
        )
        db.add(wf_node)
        db.flush() # get the node_id
        node_mapping[n.id] = {
            "db_id": wf_node.node_id,
            "step_type_key": wf_node.step_type_key
        }
        
    # Insert Edges
    ports = db.query(StepTypePort).all()
    port_lookup = {(p.step_type_key, p.port_name, p.direction): p.port_id for p in ports}
    
    for e in template.edges:
        src_info = node_mapping.get(e.source)
        tgt_info = node_mapping.get(e.target)
        if not src_info or not tgt_info:
            continue
            
        src_port_id = port_lookup.get((src_info["step_type_key"], e.sourceHandle, "output"))
        tgt_port_id = port_lookup.get((tgt_info["step_type_key"], e.targetHandle, "input"))
        
        if src_port_id and tgt_port_id:
            wf_edge = WfEdge(
                template_version_id=new_template.template_version_id,
                source_node_id=src_info["db_id"],
                target_node_id=tgt_info["db_id"],
                source_port_id=src_port_id,
                target_port_id=tgt_port_id
            )
            db.add(wf_edge)
    
    db.commit()
    return {"message": "Template created successfully", "template_version_id": new_template.template_version_id}

@app.post("/api/workflow-templates/{template_id}/versions")
def create_template_version(template_id: int, template: WorkflowTemplateCreate, db: Session = Depends(get_db)):
    max_version = db.query(func.max(WorkflowTemplate.version)).filter(WorkflowTemplate.template_id == template_id).scalar() or 0
    next_version = max_version + 1
    
    user = db.query(AppUser).filter(AppUser.username == "mock_user").first()
    owner_id = user.user_id if user else None

    new_template = WorkflowTemplate(
        template_id=template_id,
        version=next_version,
        name=template.name,
        description=template.description,
        category=template.category,
        owner_id=owner_id
    )
    db.add(new_template)
    db.commit()
    db.refresh(new_template)
    
    node_mapping = {}
    for n in template.nodes:
        wf_node = WfNode(
            template_version_id=new_template.template_version_id,
            step_type_key=n.type,
            position_x=n.position.get("x", 0),
            position_y=n.position.get("y", 0),
            default_config=n.data.get("config_values", {})
        )
        db.add(wf_node)
        db.flush()
        node_mapping[n.id] = {
            "db_id": wf_node.node_id,
            "step_type_key": wf_node.step_type_key
        }
        
    ports = db.query(StepTypePort).all()
    port_lookup = {(p.step_type_key, p.port_name, p.direction): p.port_id for p in ports}
    
    for e in template.edges:
        src_info = node_mapping.get(e.source)
        tgt_info = node_mapping.get(e.target)
        if not src_info or not tgt_info:
            continue
            
        src_port_id = port_lookup.get((src_info["step_type_key"], e.sourceHandle, "output"))
        tgt_port_id = port_lookup.get((tgt_info["step_type_key"], e.targetHandle, "input"))
        
        if src_port_id and tgt_port_id:
            wf_edge = WfEdge(
                template_version_id=new_template.template_version_id,
                source_node_id=src_info["db_id"],
                target_node_id=tgt_info["db_id"],
                source_port_id=src_port_id,
                target_port_id=tgt_port_id
            )
            db.add(wf_edge)
    
    db.commit()
    return {"message": f"Version {next_version} saved successfully", "template_version_id": new_template.template_version_id}

@app.get("/api/pipeline-runs")
def list_pipeline_runs(db: Session = Depends(get_db)):
    runs = db.query(PipelineRun).order_by(PipelineRun.created_at.desc()).all()
    return [
        {
            "run_id": r.run_id,
            "name": r.name,
            "status": r.status,
            "created_at": r.created_at
        } for r in runs
    ]

class NodeExecutionRequest(BaseModel):
    template_version_id: int
    node_id: str
    config_values: dict

@app.post("/api/pipeline-runs/execute-node")
def execute_single_node(req: NodeExecutionRequest, db: Session = Depends(get_db)):
    # In a real app, this would submit a Tapis job
    # For now, we simulate execution
    import time
    time.sleep(1) # simulate some processing
    
    return {
        "message": "Node execution completed successfully",
        "node_id": req.node_id,
        "status": "COMPLETED",
        "config_used": req.config_values
    }

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
