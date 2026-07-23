from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session
import os
import uvicorn

from db import engine, SessionLocal, get_db, DATABASE_URL
from models import Base, StepTypeRegistry, StepTypePort, WorkflowTemplate, PipelineRun, AppUser, WfNode, WfEdge, RunEdge, PortDataType
import auth
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import json
import glob

from dbos import DBOS, DBOSConfig
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Harvest Tapis Backend")

# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DBOS durable-execution engine ---
# Shares the harvest Postgres but keeps its internal bookkeeping in a dedicated
# 'dbos' schema so it never collides with the application's public tables.
# Passing fastapi=app integrates DBOS with the app lifecycle (launch/recovery).
# Importing engine.workflows registers the @DBOS.workflow / @DBOS.step functions.
dbos_config: DBOSConfig = {
    "name": "harvest-orchestrator",
    "system_database_url": DATABASE_URL,
    "dbos_system_schema": "dbos",
}
DBOS(fastapi=app, config=dbos_config)

from engine.workflows import dag_orchestrator_workflow  # noqa: E402  (after DBOS init)
from engine.transactions import get_run_details, get_run_graph_data  # noqa: E402
from engine.graph import render_ascii_graph  # noqa: E402

def _delete_port_with_dependents(db: Session, port):
    """Delete a step-type port, first removing any saved workflow/run edges that
    reference it. A port that was renamed or removed in step.json may still be
    wired into saved templates (wf_edge) or executed runs (run_edge); the foreign
    keys would otherwise block the delete and the whole step's port sync would be
    skipped. We drop those dangling edges and log a warning so the operator knows
    a saved workflow was altered."""
    dependents = db.query(WfEdge).filter(
        (WfEdge.source_port_id == port.port_id) | (WfEdge.target_port_id == port.port_id)
    ).all()
    dependents += db.query(RunEdge).filter(
        (RunEdge.source_port_id == port.port_id) | (RunEdge.target_port_id == port.port_id)
    ).all()

    if dependents:
        print(f"  Warning: removing stale port '{port.step_type_key}.{port.port_name}' "
              f"({port.direction}) drops {len(dependents)} saved edge(s) that referenced it.")
        for edge in dependents:
            db.delete(edge)
        db.flush()  # ensure edge deletes are issued before the port delete

    db.delete(port)


def validate_step_output_contract(data: dict) -> list:
    """Enforce the output-port contract so complex graphs stay consistent.

    Each output port must map to exactly ONE artifact of its declared type:
      - A step with MORE THAN ONE output port must give each a distinct,
        non-empty `output_path` (so ports don't collide on the same location).
      - No two output ports may share an `output_path`.
      - A single-file (scalar) output type SHOULD point at a file path, and a
        directory type (`*_dir`) at a directory — we warn but don't hard-fail on
        this heuristic since some containers legitimately vary.

    Returns a list of hard-error strings (empty == valid). Steps that fail are
    skipped by the sync so a broken definition never enters the registry.
    """
    outs = data.get("outputs", []) or []
    errors = []
    paths = [o.get("output_path") for o in outs]

    if len(outs) > 1:
        for o in outs:
            if not o.get("output_path"):
                errors.append(
                    f"output port '{o.get('name')}' has no output_path — a step with "
                    f"multiple outputs must give each its own distinct output_path")
        non_empty = [p for p in paths if p]
        if len(set(non_empty)) != len(non_empty):
            errors.append("two or more output ports share the same output_path")

    # A path may not be shared even across a mix of empty/non-empty.
    seen = {}
    for o in outs:
        p = o.get("output_path")
        if p and p in seen:
            errors.append(f"output ports '{seen[p]}' and '{o.get('name')}' share output_path '{p}'")
        if p:
            seen[p] = o.get("name")
    return errors


def sync_port_data_types(db: Session, step_files: list):
    """Ensure every port data type referenced by a step's ports exists in
    port_data_type before ports are synced.

    step_type_port.data_type is a foreign key into port_data_type. The step.json
    files are the single source of truth for which data types exist, so we derive
    the set of referenced types from them and create any that are missing. This
    keeps sync_step_registry self-sufficient on a fresh database instead of
    depending on the separate seed_db.py script having been run first.
    """
    referenced = set()
    for file_path in step_files:
        with open(file_path, "r") as f:
            data = json.load(f)
        for port in data.get("inputs", []) + data.get("outputs", []):
            if port.get("type"):
                referenced.add(port["type"])

    existing = {t.type_key for t in db.query(PortDataType.type_key).all()}
    missing = referenced - existing
    for type_key in sorted(missing):
        db.add(PortDataType(type_key=type_key))
    if missing:
        db.commit()
        print(f"  Seeded {len(missing)} missing port data type(s): {', '.join(sorted(missing))}")


def sync_step_registry(db: Session):
    print("Syncing step registry from JSON files...")
    step_files = glob.glob(os.path.join(os.path.dirname(__file__), "steps", "*", "step.json"))
    for file_path in step_files:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        step_key = data["step_type_key"]

        # Enforce the output-port contract; skip steps that violate it so a
        # broken definition can't corrupt downstream edge routing.
        contract_errors = validate_step_output_contract(data)
        if contract_errors:
            print(f"  ✗ SKIPPING step '{step_key}' — invalid output contract:")
            for e in contract_errors:
                print(f"      - {e}")
            continue
        
        # Upsert Registry entry (always safe)
        registry_entry = db.query(StepTypeRegistry).filter_by(step_type_key=step_key).first()
        if not registry_entry:
            registry_entry = StepTypeRegistry(step_type_key=step_key)
            db.add(registry_entry)
            
        registry_entry.display_name = data.get("display_name", step_key)
        registry_entry.description = data.get("description", "")
        registry_entry.category = data.get("category", "general")
        registry_entry.icon = data.get("icon", "default")
        registry_entry.config_schema = data.get("config_schema", {})
        # Optional: the Tapis app this step submits to when executed by the DBOS
        # engine. Sourced from step.json (single source of truth); left as-is in
        # the DB if absent so it can be set manually for steps not yet mapped.
        if data.get("tapis_app_id") is not None:
            registry_entry.tapis_app_id = data["tapis_app_id"]
        # Optional: full Tapis job-spec template (with ${...} placeholders) used
        # to submit the real job. Mirrored from step.json each sync.
        registry_entry.tapis_job = data.get("tapis_job")
        db.commit()
        print(f"  Synced registry: {step_key} (config_schema keys: {list(data.get('config_schema', {}).keys())})")
        
        # Sync Ports: make the DB an exact mirror of the JSON definition.
        # The JSON file is the source of truth, so add new ports, update changed
        # ones, and remove ports that were renamed/deleted in the JSON. Without
        # the removal step, ports renamed during development accumulate forever
        # and the node would render extra/wrong handles.
        json_ports = {}  # (port_name, direction) -> {"type", "required"}
        for direction, port_list in [("input", data.get("inputs", [])), ("output", data.get("outputs", []))]:
            for p in port_list:
                # Inputs default to required; a port is optional only if step.json
                # sets "required": false. (Outputs' is_required is not meaningful.)
                json_ports[(p["name"], direction)] = {
                    "type": p["type"],
                    "required": p.get("required", True),
                    "output_path": p.get("output_path"),
                }

        try:
            existing_ports = db.query(StepTypePort).filter_by(step_type_key=step_key).all()
            existing_by_key = {(p.port_name, p.direction): p for p in existing_ports}

            # Remove ports no longer present in the JSON (dropping any saved
            # edges that still reference them, with a logged warning).
            for key, port in existing_by_key.items():
                if key not in json_ports:
                    _delete_port_with_dependents(db, port)

            # Add new ports, and update changed data type / required flag.
            for (port_name, direction), spec in json_ports.items():
                existing = existing_by_key.get((port_name, direction))
                if existing is None:
                    db.add(StepTypePort(
                        step_type_key=step_key, port_name=port_name,
                        data_type=spec["type"], direction=direction,
                        is_required=spec["required"],
                        output_path=spec.get("output_path"),
                    ))
                else:
                    if existing.data_type != spec["type"]:
                        existing.data_type = spec["type"]
                    if existing.is_required != spec["required"]:
                        existing.is_required = spec["required"]
                    if existing.output_path != spec.get("output_path"):
                        existing.output_path = spec.get("output_path")

            db.commit()
        except Exception as e:
            db.rollback()
            print(f"  Warning: Could not sync ports for {step_key}: {e}")
    
    # Prune: deactivate steps that no longer have a JSON file
    json_keys = set()
    for file_path in step_files:
        with open(file_path, "r") as f:
            json_keys.add(json.load(f)["step_type_key"])
    
    all_db_steps = db.query(StepTypeRegistry).all()
    for db_step in all_db_steps:
        if db_step.step_type_key not in json_keys:
            # Stale step: deactivate it and remove its ports so they can't leak
            # into the API response for any step that reuses a port name. Drop any
            # saved edges referencing those ports first (with a logged warning).
            try:
                stale_ports = db.query(StepTypePort).filter_by(step_type_key=db_step.step_type_key).all()
                for port in stale_ports:
                    _delete_port_with_dependents(db, port)
                if db_step.is_active:
                    db_step.is_active = False
                    print(f"  Deactivated stale step: {db_step.step_type_key}")
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"  Warning: Could not prune stale step {db_step.step_type_key}: {e}")
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

@app.get("/api/port-data-types")
def get_port_data_types(db: Session = Depends(get_db)):
    types = db.query(PortDataType).all()
    return [
        {
            "type_key": t.type_key,
            "parent_type": t.parent_type,
            "description": t.description,
            "coerce_from": t.coerce_from or []
        } for t in types
    ]

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

def _validate_no_hanging_inputs(template: WorkflowTemplateCreate, db: Session):
    """Reject templates with unsatisfied required inputs.

    Every REQUIRED input port on every node must be fed by an incoming edge
    (from an upstream node's output or a data-source node). Optional ports
    (is_required=False) may be left unconnected. Raises HTTP 400 with a
    human-readable list of the unsatisfied inputs so the designer knows exactly
    what to wire up.
    """
    # Required input ports per step type: {step_type_key: set(port_name)}
    ports = db.query(StepTypePort).filter(StepTypePort.direction == "input").all()
    required_inputs = {}
    for p in ports:
        if p.is_required:
            required_inputs.setdefault(p.step_type_key, set()).add(p.port_name)

    # node id -> step_type_key (frontend node type is the step_type_key)
    node_type = {n.id: n.type for n in template.nodes}

    # Which (node id, target port) pairs are satisfied by an edge.
    satisfied = set()
    for e in template.edges:
        if e.targetHandle is not None:
            satisfied.add((e.target, e.targetHandle))

    problems = []
    for n in template.nodes:
        needed = required_inputs.get(n.type, set())
        for port_name in sorted(needed):
            if (n.id, port_name) not in satisfied:
                label = n.data.get("node_label") or n.type
                problems.append(f'"{label}" is missing required input "{port_name}"')

    if problems:
        raise HTTPException(
            status_code=400,
            detail=(
                "Workflow is incomplete and cannot be saved — every required input "
                "must be connected to an upstream output or a data source. "
                "Unresolved inputs: " + "; ".join(problems)
            ),
        )


@app.post("/api/workflow-templates")
def create_workflow_template(template: WorkflowTemplateCreate, db: Session = Depends(get_db)):
    _validate_no_hanging_inputs(template, db)

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
    _validate_no_hanging_inputs(template, db)

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
    # Join the template name so the history page can show which workflow ran.
    templates = {t.template_version_id: t.name for t in db.query(WorkflowTemplate).all()}
    return [
        {
            "run_id": r.run_id,
            "name": r.name,
            "status": r.status,
            "created_at": r.created_at,
            "template_version_id": r.template_version_id,
            "template_name": templates.get(r.template_version_id),
            "dbos_workflow_id": r.dbos_workflow_id,
        } for r in runs
    ]


@app.get("/api/pipeline-runs/{run_id}/detail")
def get_pipeline_run_detail(run_id: int, db: Session = Depends(get_db)):
    """Per-run detail by run_id: overall status + each step's status/outputs.
    Used by the history page to show which node is running/completed/failed."""
    from models import RunStep, WfNode
    run = db.query(PipelineRun).filter(PipelineRun.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    steps = db.query(RunStep).filter(RunStep.run_id == run_id).order_by(RunStep.run_step_id).all()
    # Map node_id -> step_type_key for readable labels.
    node_keys = {n.node_id: n.step_type_key for n in db.query(WfNode).filter(
        WfNode.template_version_id == run.template_version_id).all()}
    return {
        "run_id": run.run_id,
        "name": run.name,
        "status": run.status,
        "created_at": run.created_at,
        "template_version_id": run.template_version_id,
        "dbos_workflow_id": run.dbos_workflow_id,
        "steps": [
            {
                "node_id": s.node_id,
                "step_type": node_keys.get(s.node_id, "?"),
                "status": s.status,
                "tapis_job_uuid": s.tapis_job_uuid,
                "tapis_job_status": s.tapis_job_status,
                "error_message": s.error_message,
                "outputs": s.outputs,
            } for s in steps
        ],
    }

@app.get("/api/pipeline-runs/{run_id}/step/{node_id}/logs")
def get_step_logs(run_id: int, node_id: int, db: Session = Depends(get_db)):
    """Fetch detailed failure logs for a step: the DBOS error we recorded, plus
    the underlying Tapis job's lastMessage and its container stdout/stderr
    (tapisjob.out). Best-effort — degrades gracefully if the token is stale or
    the job output isn't reachable."""
    from models import RunStep
    import httpx as _httpx
    from engine import tapis_auth

    step = db.query(RunStep).filter(
        RunStep.run_id == run_id, RunStep.node_id == node_id
    ).first()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    result = {
        "node_id": node_id,
        "status": step.status,
        "tapis_job_uuid": step.tapis_job_uuid,
        "tapis_job_status": step.tapis_job_status,
        "error_message": step.error_message,   # DBOS-level error we captured
        "tapis_last_message": None,            # Tapis' own summary of the outcome
        "job_output": None,                    # container stdout/stderr
        "logs_note": None,
    }

    uuid = step.tapis_job_uuid
    if not uuid:
        result["logs_note"] = "This step ran no Tapis job (e.g. a data source/sink), so there are no job logs."
        return result

    token = tapis_auth.get_token()
    if not token:
        result["logs_note"] = "No valid Tapis token configured; cannot fetch job logs."
        return result

    base = tapis_auth.TAPIS_BASE_URL
    headers = {"X-Tapis-Token": token}
    try:
        jr = _httpx.get(f"{base}/v3/jobs/{uuid}", headers=headers, timeout=30)
        if jr.status_code == 200:
            jd = jr.json().get("result", {})
            result["tapis_last_message"] = jd.get("lastMessage")
            result["tapis_job_status"] = jd.get("status") or result["tapis_job_status"]
        # Container stdout/stderr — the most useful thing for a real failure.
        out = _httpx.get(f"{base}/v3/jobs/{uuid}/output/download/tapisjob.out",
                         headers=headers, timeout=30)
        if out.status_code == 200:
            text = out.text
            # Keep the tail — the error is almost always at the end.
            result["job_output"] = text[-8000:] if len(text) > 8000 else text
        else:
            result["logs_note"] = f"Container output not available (HTTP {out.status_code})."
    except Exception as e:
        result["logs_note"] = f"Could not reach Tapis for logs: {type(e).__name__}"

    return result


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

# --- Whole-workflow execution via the DBOS engine ---

def _build_dag_config(db: Session, template_version_id: int) -> dict:
    """Read a saved template's nodes/edges and shape them into the dag_config the
    DBOS orchestrator expects: {template_version_id, nodes:[{id,type,inputs}], edges:[{from,to}]}.

    Node ids are the string form of wf_node.node_id (the DAG node key the engine
    uses throughout). Each node's inputs come from its saved default_config.
    """
    template = db.query(WorkflowTemplate).filter(
        WorkflowTemplate.template_version_id == template_version_id
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    nodes = db.query(WfNode).filter(WfNode.template_version_id == template_version_id).all()
    edges = db.query(WfEdge).filter(WfEdge.template_version_id == template_version_id).all()

    if not nodes:
        raise HTTPException(status_code=400, detail="Template has no nodes to execute")

    return {
        "template_version_id": template_version_id,
        "name": template.name,
        "nodes": [
            {
                "id": str(n.node_id),
                "type": n.step_type_key,
                "label": n.node_label or n.step_type_key,
                "inputs": n.default_config or {},
            }
            for n in nodes
        ],
        "edges": [
            {"from": str(e.source_node_id), "to": str(e.target_node_id)}
            for e in edges
        ],
    }


class RunOptions(BaseModel):
    # Run-level Tapis values substituted into job specs (${slurm_account} etc.)
    # and stored on the run's frozen_config. All optional; sensible defaults
    # apply when omitted (see engine.transactions.get_run_archive_context).
    slurm_account: Optional[str] = None
    # Exec-system selection — lets a run target OSC (pitzer-tapis / ascend-tapis)
    # or Expanse (expanse-tapis) without code changes. exec_queue is the site's
    # queue name (OSC: cpu/gpu/nextgen; Expanse: tapisGPUshared/...). work_dir is
    # the base scratch/project path for OSC execSystem*Dir fields.
    exec_system: Optional[str] = None
    exec_queue: Optional[str] = None
    work_dir: Optional[str] = None
    archive_system: Optional[str] = None


@app.post("/api/pipeline-runs/{template_version_id}/execute")
def execute_workflow(template_version_id: int, options: Optional[RunOptions] = None, db: Session = Depends(get_db)):
    """Kick off durable execution of a saved workflow template.

    Builds the DAG from the persisted template and starts the DBOS orchestrator
    asynchronously. Returns the DBOS workflow id, which the frontend polls via
    the status endpoint. The pipeline_run row (with dbos_workflow_id) is created
    inside the orchestrator's first transaction.

    Optional run-level Tapis values (slurm_account, archive_system, work_dir, …)
    are merged into the dag_config, which the orchestrator stores as frozen_config
    and the engine reads when rendering each step's Tapis job spec. The per-step
    archive location is derived automatically (work_dir/wf_runs/<run_id>/<node>).
    """
    dag_config = _build_dag_config(db, template_version_id)
    if options is not None:
        for key, value in options.model_dump(exclude_none=True).items():
            dag_config[key] = value

    handle = DBOS.start_workflow(dag_orchestrator_workflow, dag_config)
    return {
        "message": "Workflow execution started",
        "dbos_workflow_id": handle.get_workflow_id(),
        "template_version_id": template_version_id,
    }


@app.post("/api/pipeline-runs/{run_id}/stop")
def stop_pipeline_run(run_id: int, db: Session = Depends(get_db)):
    """Stop a running pipeline run: cancel the DBOS workflow (and its child
    node-workflows), cancel any in-flight Tapis job, and mark the run + its
    non-terminal steps as cancelled."""
    from models import RunStep
    from engine.tapis import cancel_tapis_job

    run = db.query(PipelineRun).filter(PipelineRun.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    if (run.status or "").upper() in terminal:
        return {"message": f"Run is already {run.status}; nothing to stop.", "run_id": run_id}

    # 1. Cancel the DBOS orchestrator + its child workflows (unwinds the polling loops).
    if run.dbos_workflow_id:
        try:
            DBOS.cancel_workflow(run.dbos_workflow_id, cancel_children=True)
        except Exception as e:
            print(f"[stop] DBOS.cancel_workflow failed for {run.dbos_workflow_id}: {type(e).__name__}")

    # 2. Cancel any in-flight Tapis jobs for this run's steps.
    steps = db.query(RunStep).filter(RunStep.run_id == run_id).all()
    cancelled_jobs = 0
    for s in steps:
        if s.tapis_job_uuid and (s.status or "") not in ("completed", "failed"):
            if cancel_tapis_job(s.tapis_job_uuid):
                cancelled_jobs += 1

    # 3. Mark run + non-terminal steps as cancelled.
    run.status = "CANCELLED"
    for s in steps:
        if (s.status or "") not in ("completed", "failed"):
            s.status = "cancelled"
    db.commit()

    return {
        "message": "Run stopped.",
        "run_id": run_id,
        "tapis_jobs_cancelled": cancelled_jobs,
    }


@app.get("/api/pipeline-runs/status/{dbos_workflow_id}")
def get_workflow_run_status(dbos_workflow_id: str, format: str = "json"):
    """Return live status of a workflow run: overall DBOS state, per-step detail,
    and an ASCII progress graph. `format=text` returns just the graph as text."""
    status = DBOS.get_workflow_status(dbos_workflow_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    db_details = get_run_details(dbos_workflow_id)

    graph_data = get_run_graph_data(dbos_workflow_id)
    progress_graph = ""
    if graph_data:
        progress_graph = render_ascii_graph(
            graph_data["nodes"], graph_data["edges"], graph_data["statuses"]
        )

    if format == "text":
        return PlainTextResponse(progress_graph)

    return {
        "dbos_workflow_id": dbos_workflow_id,
        "workflow_state": status.status,
        "database_record": db_details,
        "progress_graph": progress_graph,
    }


if __name__ == "__main__":
    # reload spawns a child process, which breaks step debuggers (PyCharm/pdb):
    # the debugger attaches to the parent while the app runs in the child.
    # Default to no reload so `python main.py` is debuggable; opt back in with
    # UVICORN_RELOAD=true for normal hot-reload dev.
    reload = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    uvicorn.run("main:app", host="127.0.0.1", port=8002, reload=reload)
