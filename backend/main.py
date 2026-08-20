from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session
import os
import uuid
import uvicorn

from db import engine, SessionLocal, get_db, DATABASE_URL, DB_HOST, DB_PORT, DB_NAME, DB_USER
from models import Base, StepTypeRegistry, StepTypePort, WorkflowTemplate, PipelineRun, RunStep, AppUser, WfNode, WfEdge, RunEdge, PortDataType, Secret
import auth
import geospatial
import annotation_adapter
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import json
import glob
import re

from dbos import DBOS, DBOSConfig, SetWorkflowID
from fastapi.responses import PlainTextResponse, Response
import httpx

API_DESCRIPTION = """\
Backend for the **No-Code Workflow Studio** — build ML workflows on a visual
canvas and execute them as a durable DAG against Tapis-managed HPC resources.

### Authentication

Every endpoint except `GET /` requires an authenticated user. Two credentials
are accepted, checked in this order:

1. **`X-Tapis-Token`** header — a Tapis access token. Used when the Studio is
   embedded in TapisUI, where the host supplies the token.
2. **`session`** cookie — set by this app's own Tapis OAuth login
   (`GET /login` → `GET /oauth2/callback`).

A token that fails verification falls through to the session rather than
hard-failing. `GET /auth-debug` reports which credential a request carried and
why it was or wasn't accepted.

### Ownership

Templates and runs are scoped to the calling user.

* **Runs** are owner-only. Another user's run returns `404`.
* **Templates** are owner-only until published (`POST
  /api/workflow-templates/{template_id}/publish`), which grants every user
  read, run and clone access — never write. Only the owner can add a version.

Resources you may not access return **`404`, not `403`**, so a response never
confirms that an id exists.
"""

OPENAPI_TAGS = [
    {"name": "workflow-templates", "description":
        "Create, read, version, publish and clone workflow templates. A *template* "
        "is a named graph; each save creates a new version sharing one `template_id`."},
    {"name": "pipeline-runs", "description":
        "Launch, monitor, and stop executions. A *run* freezes a template version's "
        "config at launch, so editing the template later never changes a run in flight."},
    {"name": "step-registry", "description":
        "The catalogue of available step types and port data types, synced from "
        "`backend/steps/*/step.json` at startup."},
    {"name": "secrets", "description":
        "Team-scoped API tokens (Weights & Biases, Hugging Face, ...). Only a "
        "secret's KEY is ever returned; values are decrypted server-side at job "
        "submission time."},
    {"name": "tapis", "description":
        "Thin proxies over the Tapis API using the caller's own OAuth token — "
        "file browsing, uploads, system queues, and identity."},
    {"name": "geospatial", "description":
        "GeoPackage-backed layers for a completed run, served on demand as GeoJSON, "
        "zipped Shapefile, or raw GeoPackage."},
    {"name": "geospatial-preview", "description":
        "The same conversions against a `tapis://` URI given directly, so a source "
        "node's static GeoPackage can be previewed before the workflow has ever run."},
    {"name": "annotation-adapter", "description":
        "Convert annotations between native, COCO, YOLO and GeoPackage formats."},
    {"name": "auth", "description":
        "Tapis OAuth login/logout, current identity, and credential diagnostics."},
    {"name": "meta", "description": "Service health and configuration."},
]

app = FastAPI(
    title="No-Code Workflow Studio API",
    description=API_DESCRIPTION,
    version="0.1.0",
    openapi_tags=OPENAPI_TAGS,
    license_info={"name": "MIT"},
    contact={"name": "ICICLE-ai / workflow-orchestrator",
             "url": "https://github.com/ICICLE-ai/workflow-orchestrator"},
)

# Enable CORS for the frontend. Always includes the plain-localhost dev
# defaults, plus FRONTEND_URL (already used for the OAuth redirect — same
# origin the browser actually calls from, just not previously in this list)
# and any extra origins from CORS_ALLOWED_ORIGINS (comma-separated) — e.g. a
# tunnel URL if the frontend itself is served remotely, not just the backend.
# A mismatch here fails EVERY cross-origin call with a genuine browser CORS
# error, not just the one you happened to test last.
_cors_default_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
_cors_frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
_cors_extra_origins = [o.strip().rstrip("/") for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
_cors_allowed_origins = list(dict.fromkeys(
    _cors_default_origins + ([_cors_frontend_url] if _cors_frontend_url else []) + _cors_extra_origins
))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Signed, httponly session cookie that carries the logged-in username after the
# Tapis OAuth flow (see auth.py). Defaults (same_site="lax", not Secure) suit an
# all-localhost dev setup. When the frontend and backend are on different hosts
# (e.g. frontend on localhost, backend behind a cloudflared tunnel), the browser
# only sends the cookie on cross-site API calls if it's SameSite=None; Secure —
# set SESSION_COOKIE_SAMESITE=none and SESSION_COOKIE_SECURE=true in that case.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-insecure-session-secret-change-me"),
    same_site=os.getenv("SESSION_COOKIE_SAMESITE", "lax"),
    https_only=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    """FastAPI dependency: resolve the signed-in AppUser, or reject with 401.
    Attach to any route that requires auth.

    Accepts either credential (see auth.resolve_current_user): the host-supplied
    X-Tapis-Token when this app is embedded in TapisUI, otherwise this app's own
    session cookie from the Tapis OAuth login.
    """
    return auth.require_current_user(request, db)


# --- Ownership scoping -------------------------------------------------------
# Authentication only establishes WHO is calling. These establish WHAT they may
# see. Every route that reads or acts on a template or a run must go through one
# of these — a plain `db.query(WorkflowTemplate)` in a request handler is a bug,
# because it returns every user's rows.
#
# Templates and runs already record ownership at write time (owner_id / user_id,
# set from the authenticated user), so this is purely a read-side restriction.
#
# Missing/unknown ids return 404 rather than 403: a 403 confirms that a
# template_version_id or run_id exists, which is itself a disclosure. The caller
# cannot distinguish "no such run" from "not your run", which is the intent.


def visible_templates(db: Session, user: AppUser):
    """Base query for templates `user` may READ: their own, anything published
    public, plus anything explicitly shared with their team.

    Visibility is not permission to modify. Read access allows opening, running
    and cloning; adding a version is owner-only (see owned_template_or_404).

    The `team_id.isnot(None)` guard is load bearing. SQLAlchemy renders
    `team_id == None` as `team_id IS NULL`, so for a user with no team the
    sharing clause would otherwise match every team-less shared template.
    """
    return db.query(WorkflowTemplate).filter(
        or_(
            WorkflowTemplate.owner_id == user.user_id,
            WorkflowTemplate.is_public.is_(True),
            and_(
                WorkflowTemplate.is_shared.is_(True),
                WorkflowTemplate.team_id.isnot(None),
                WorkflowTemplate.team_id == user.team_id,
            ),
        )
    )


def owned_templates(db: Session, user: AppUser):
    """Base query for templates `user` may MODIFY — strictly their own.

    Public and team-shared templates are readable but not writable: the version
    list endpoint shows the LATEST version, so letting a non-owner add one would
    change what the owner opens next. Non-owners clone instead.
    """
    return db.query(WorkflowTemplate).filter(WorkflowTemplate.owner_id == user.user_id)


def template_or_404(db: Session, user: AppUser, template_version_id: int) -> WorkflowTemplate:
    """One template version `user` may read, or 404."""
    template = visible_templates(db, user).filter(
        WorkflowTemplate.template_version_id == template_version_id
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


def owned_template_or_404(db: Session, user: AppUser, template_id: int) -> WorkflowTemplate:
    """Any version of a template lineage `user` OWNS, or 404. For writes.

    404 rather than 403 even when the template exists and is merely someone
    else's: a public template's existence is already known to the caller, but
    which account owns it is not, and 403-vs-404 here would answer that.
    """
    template = owned_templates(db, user).filter(
        WorkflowTemplate.template_id == template_id
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


def visible_runs(db: Session, user: AppUser):
    """Base query for runs `user` may see.

    Runs are owner-only, deliberately narrower than templates: a shared template
    is a design others may reuse, whereas a run carries that user's resolved
    config, their Tapis paths, and their job output locations.
    """
    return db.query(PipelineRun).filter(PipelineRun.user_id == user.user_id)


def run_or_404(db: Session, user: AppUser, run_id: int) -> PipelineRun:
    """One run `user` owns, or 404."""
    run = visible_runs(db, user).filter(PipelineRun.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


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
    # Port data types must exist before ports reference them (FK constraint).
    sync_port_data_types(db, step_files)
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
        # Whether this step submits a Tapis job. step.json can set this explicitly
        # (design-time-only steps like smart_labeler/geospatial_map do); otherwise
        # it's inferred from whether a tapis_job template is present.
        registry_entry.submits_job = data.get("submits_job", data.get("tapis_job") is not None)
        # Compute requirement ({"gpu": bool}) — see StepTypeRegistry.resources.
        registry_entry.resources = data.get("resources") or {}
        # Palette visibility. Mirrored every sync (not just on insert) so
        # flipping "hidden" in step.json takes effect on the next restart,
        # in both directions.
        registry_entry.hidden = bool(data.get("hidden", False))
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
                    "file_glob": p.get("file_glob"),
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
                        file_glob=spec.get("file_glob"),
                    ))
                else:
                    if existing.data_type != spec["type"]:
                        existing.data_type = spec["type"]
                    if existing.is_required != spec["required"]:
                        existing.is_required = spec["required"]
                    if existing.output_path != spec.get("output_path"):
                        existing.output_path = spec.get("output_path")
                    if existing.file_glob != spec.get("file_glob"):
                        existing.file_glob = spec.get("file_glob")

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
    """Create/patch the schema and sync the step registry from steps/*/step.json.

    A failure here is FATAL on purpose. This used to swallow every exception
    with a warning, which meant a backend that couldn't reach its database — or
    whose schema was never created — still booted and served happily. Requests
    with no credential don't touch the database at all (see auth.py's
    resolve_current_user_with_mode), so they kept returning clean 401s, and the
    first query behind a real credential blew up as a 500. Because Starlette's
    error handler sits outside CORSMiddleware, that 500 carries no
    Access-Control-Allow-Origin and the browser reports it as a CORS failure —
    an error message pointing nowhere near the actual cause. Crashing at boot
    with the real exception in the logs is far easier to diagnose.
    """
    from sqlalchemy import text

    print("Creating database schema...")
    try:
        # NOTE: In production, you would use Alembic migrations instead of create_all
        Base.metadata.create_all(bind=engine)
        # create_all only adds *missing tables*, not new columns on existing ones.
        # The per-user Tapis token columns were added after initial deploys, so
        # patch existing app_user tables idempotently (no Alembic in this project).
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE app_user "
                "ADD COLUMN IF NOT EXISTS tapis_access_token VARCHAR, "
                "ADD COLUMN IF NOT EXISTS tapis_refresh_token VARCHAR, "
                "ADD COLUMN IF NOT EXISTS tapis_token_expires_at TIMESTAMPTZ;"
            ))
            conn.execute(text(
                "ALTER TABLE workflow_template "
                "ADD COLUMN IF NOT EXISTS allocation_account VARCHAR, "
                "ADD COLUMN IF NOT EXISTS is_draft BOOLEAN DEFAULT FALSE, "
                # NOT NULL DEFAULT FALSE so existing rows backfill to "private".
                # Defaulting to public would republish every template already in
                # the database the moment this deploys.
                "ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;"
            ))
            conn.execute(text(
                "ALTER TABLE step_type_registry "
                "ADD COLUMN IF NOT EXISTS submits_job BOOLEAN DEFAULT TRUE, "
                "ADD COLUMN IF NOT EXISTS hidden BOOLEAN DEFAULT FALSE, "
                "ADD COLUMN IF NOT EXISTS resources JSON DEFAULT '{}'::json;"
            ))
            conn.execute(text(
                "ALTER TABLE step_type_port "
                "ADD COLUMN IF NOT EXISTS file_glob VARCHAR;"
            ))
    except Exception as e:
        raise RuntimeError(
            f"Database schema initialization failed against "
            f"{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}: {type(e).__name__}: {e}. "
            "Check that the database is reachable and that DB_HOST / DB_PORT / "
            "DB_NAME / DB_USER / DB_PASSWORD are set for this deployment."
        ) from e
    print("Database schema created.")

    # Sync Dynamic Steps
    db = SessionLocal()
    try:
        sync_step_registry(db)
    finally:
        db.close()

    # Launch the DBOS durable-execution engine. The fastapi=app integration is
    # meant to launch DBOS on the ASGI lifespan startup event, but that hook does
    # not fire reliably alongside the deprecated @app.on_event("startup"); without
    # launch, /execute fails with "System database accessed before DBOS was
    # launched". DBOS.launch() is idempotent (guards on _launched), so if the
    # library's own hook does fire it's a harmless no-op.
    try:
        DBOS.launch()
        print("DBOS launched.")
    except Exception as e:
        print(f"Warning: DBOS.launch() failed: {e}")

# Include the Authentication Router
app.include_router(auth.router)

# Include the Geospatial resource routers — run-scoped
# (/api/pipeline-runs/{run_id}/geospatial/...: labels/config/geojson/
# shapefile-download/gpkg-download for a run's GeoPackage output) and
# design-time preview (/api/geospatial-preview/...: same conversions for a
# static source_geopackage node's path, no run required). See geospatial.py.
app.include_router(geospatial.router)
app.include_router(geospatial.preview_router)

# The annotation_format_adapter step's converter — inline data reshaping (no
# Tapis job), see annotation_adapter.py's module docstring.
app.include_router(annotation_adapter.router)

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
    allocation_account: Optional[str] = "uot260"
    nodes: List[NodeModel]
    edges: List[EdgeModel]

# --- API Endpoints ---

@app.get("/api/step-types")
def get_step_types(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
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
            "submits_job": step.submits_job,
            # Palette visibility only — hidden steps are still returned so
            # saved templates using them keep resolving (see
            # StepTypeRegistry.hidden).
            "hidden": bool(step.hidden),
            # Drives the Run Configuration modal's default exec target (a
            # gpu:true step defaults to the run's GPU pair) — see
            # RunConfigModal.tsx.
            "resources": step.resources or {},
            "inputs": inputs,
            "outputs": outputs
        })
    return result

@app.get("/api/port-data-types")
def get_port_data_types(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
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
def list_workflow_templates(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    # Group by template_id to get the latest version of each template.
    #
    # Drafts (versions created by "run without saving" — see
    # WorkflowTemplate.is_draft) are excluded from BOTH sides: from the
    # max-version subquery, so an ad-hoc run doesn't decide which version is
    # "latest", and from the selected rows, so it can't be the one listed. Miss
    # either half and every run-without-saving silently becomes the template
    # everyone opens next.
    not_draft = WorkflowTemplate.is_draft.isnot(True)

    # Scoped to this user (see visible_templates). The subquery is scoped too,
    # not just the outer select: picking the max version across EVERY user's
    # rows and then filtering would silently drop a template whose latest
    # version belongs to someone else.
    visible = visible_templates(db, user).filter(not_draft).subquery()

    subquery = db.query(
        visible.c.template_id,
        func.max(visible.c.version).label("max_version")
    ).group_by(visible.c.template_id).subquery()

    # Scoped on BOTH sides. template_id happens to be globally unique today
    # (assigned as max+1 across all rows), so an unscoped outer select would be
    # safe by accident; scoping it means this stays correct if that ever changes.
    templates = visible_templates(db, user).filter(not_draft).join(
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
            "created_at": t.created_at,
            # The UI needs both: is_public drives the "Public" badge, is_owner
            # decides whether it offers Save (owner) or Clone (everyone else).
            "is_public": bool(t.is_public),
            "is_owner": t.owner_id == user.user_id,
        } for t in templates
    ]

@app.get("/api/workflow-templates/{template_id}/history")
def get_workflow_template_history(template_id: int, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    versions = visible_templates(db, user).filter(
        WorkflowTemplate.template_id == template_id
    ).order_by(WorkflowTemplate.version.desc()).all()
    if not versions:
        raise HTTPException(status_code=404, detail="Template history not found")
        
    # Drafts ARE listed here, flagged — unlike the template list, which hides
    # them. A draft is what a "run without saving" actually executed, so keeping
    # it visible in history is what makes that run traceable back to a graph.
    return [
        {
            "template_version_id": t.template_version_id,
            "template_id": t.template_id,
            "version": t.version,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "is_draft": bool(t.is_draft),
            "is_public": bool(t.is_public),
            "is_owner": t.owner_id == user.user_id,
            "created_at": t.created_at
        } for t in versions
    ]

@app.get("/api/workflow-templates/{template_version_id}")
def get_workflow_template(template_version_id: int, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    template = template_or_404(db, user, template_version_id)

    nodes = db.query(WfNode).filter(WfNode.template_version_id == template_version_id).all()
    edges = db.query(WfEdge).filter(WfEdge.template_version_id == template_version_id).all()
    
    ports = db.query(StepTypePort).all()
    port_by_id = {p.port_id: p for p in ports}
    
    edge_list = []
    for e in edges:
        source_port = port_by_id.get(e.source_port_id)
        target_port = port_by_id.get(e.target_port_id)
        # The id MUST include the ports, not just the node pair. Two nodes can be
        # joined by several edges at once — smart_labeler -> zero_shot_annotation
        # wires 'images'->'images' AND 'annotations'->'annotation_file', which is
        # exactly what smart_labeler's passthrough 'images' output exists for.
        # React Flow keys its edge store by id, so a node-pair-only id made every
        # such edge collide: on reload only ONE of them survived, silently
        # dropping the others from the canvas (and from CustomNode's
        # connectedInputs, so the downstream panel showed no wired value). The DB
        # rows were always correct — only this response collapsed them, which is
        # why the wiring worked until the template was saved and reopened.
        # Port IDs (not names) keep it unique even for a port pair that shares a
        # name across directions.
        edge_list.append({
            "id": f"e_{e.source_node_id}_{e.source_port_id}_{e.target_node_id}_{e.target_port_id}",
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
        "allocation_account": template.allocation_account,
        # is_owner is false when this is someone else's public template: the
        # canvas is still fully readable and runnable, but Save must offer
        # "Clone to my workspace" rather than writing a version.
        "is_public": bool(template.is_public),
        "is_owner": template.owner_id == user.user_id,
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
def create_workflow_template(template: WorkflowTemplateCreate, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    _validate_no_hanging_inputs(template, db)

    # 1. Get next template_id
    max_id = db.query(func.max(WorkflowTemplate.template_id)).scalar() or 0
    next_id = max_id + 1

    owner_id = user.user_id

    new_template = WorkflowTemplate(
        template_id=next_id,
        version=1,
        name=template.name,
        description=template.description,
        category=template.category,
        allocation_account=template.allocation_account,
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
def create_template_version(
    template_id: int,
    template: WorkflowTemplateCreate,
    draft: bool = False,
    db: Session = Depends(get_db),
    user: AppUser = Depends(get_current_user),
):
    """Persist the canvas as a new version of `template_id`.

    draft=true marks it WorkflowTemplate.is_draft — the "run these changes
    without saving a version" path. The rows are identical in every other
    respect (the engine needs real wf_node/wf_edge rows to run anything at all;
    see the is_draft docstring), they're just excluded from the template list so
    an ad-hoc run doesn't become the template everyone else opens.

    Validation applies to drafts too: a workflow with unsatisfied required
    inputs can't run, so there's nothing to be gained by letting it through.
    """
    _validate_no_hanging_inputs(template, db)

    # OWNER-only, deliberately stricter than read access. A public template is
    # readable and runnable by everyone, but a version added by a non-owner
    # would become what the owner opens next (the list shows the LATEST
    # version). Non-owners use POST {template_version_id}/clone instead.
    owned_template_or_404(db, user, template_id)

    max_version = db.query(func.max(WorkflowTemplate.version)).filter(WorkflowTemplate.template_id == template_id).scalar() or 0
    next_version = max_version + 1

    owner_id = user.user_id

    new_template = WorkflowTemplate(
        template_id=template_id,
        version=next_version,
        name=template.name,
        description=template.description,
        category=template.category,
        allocation_account=template.allocation_account,
        owner_id=owner_id,
        is_draft=draft,
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
    return {
        "message": (
            f"Unsaved changes captured for this run (not added to version history)"
            if draft else f"Version {next_version} saved successfully"
        ),
        "template_version_id": new_template.template_version_id,
        "version": next_version,
        "is_draft": draft,
    }


# --- Publishing ---------------------------------------------------------------
# Publishing makes a template readable, runnable and clonable by every
# authenticated user. It never grants write access — see owned_template_or_404.


def _set_public(db: Session, user: AppUser, template_id: int, public: bool) -> dict:
    """Flip is_public across a template's whole version lineage.

    Applied to every row sharing `template_id`, not just the latest version:
    visibility is evaluated per row, so publishing only the newest would leave
    the history endpoint showing a partial list and would unpublish itself the
    next time the owner saved a version.
    """
    owned_template_or_404(db, user, template_id)
    updated = db.query(WorkflowTemplate).filter(
        WorkflowTemplate.template_id == template_id
    ).update({WorkflowTemplate.is_public: public}, synchronize_session=False)
    db.commit()
    return {
        "template_id": template_id,
        "is_public": public,
        "versions_updated": updated,
        "message": f"Template {'published' if public else 'unpublished'}.",
    }


@app.post("/api/workflow-templates/{template_id}/publish")
def publish_template(template_id: int, db: Session = Depends(get_db),
                     user: AppUser = Depends(get_current_user)):
    """Make every version of this template visible to all authenticated users.
    Owner only."""
    return _set_public(db, user, template_id, True)


@app.post("/api/workflow-templates/{template_id}/unpublish")
def unpublish_template(template_id: int, db: Session = Depends(get_db),
                       user: AppUser = Depends(get_current_user)):
    """Return this template to owner-only visibility. Owner only.

    Existing clones are unaffected — a clone is an independent template owned by
    whoever made it, not a reference back to this one.
    """
    return _set_public(db, user, template_id, False)


@app.post("/api/workflow-templates/{template_version_id}/clone")
def clone_template(template_version_id: int, db: Session = Depends(get_db),
                   user: AppUser = Depends(get_current_user)):
    """Copy a template version the caller can READ into a new template they own.

    This is how a non-owner builds on a public template: they get version 1 of a
    fresh lineage, private to them, with the original left untouched. Copied
    from the persisted wf_node/wf_edge rows rather than from a request body, so
    the clone reflects what is actually stored rather than whatever a client
    chose to send.
    """
    source = template_or_404(db, user, template_version_id)

    max_id = db.query(func.max(WorkflowTemplate.template_id)).scalar() or 0
    clone = WorkflowTemplate(
        template_id=max_id + 1,
        version=1,
        name=f"{source.name} (copy)",
        description=source.description,
        category=source.category,
        allocation_account=source.allocation_account,
        owner_id=user.user_id,
        # A clone always starts private, whatever the source was. Republishing
        # is the new owner's decision to make, not one inherited from someone
        # else's template.
        is_public=False,
        is_draft=False,
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)

    # Node ids are per-row, so edges have to be remapped onto the new ids.
    # Port ids are keyed by step type, not by node, so they carry over as-is.
    node_id_map = {}
    for n in db.query(WfNode).filter(WfNode.template_version_id == template_version_id).all():
        copy = WfNode(
            template_version_id=clone.template_version_id,
            step_type_key=n.step_type_key,
            node_label=n.node_label,
            default_config=n.default_config,
            position_x=n.position_x,
            position_y=n.position_y,
        )
        db.add(copy)
        db.flush()
        node_id_map[n.node_id] = copy.node_id

    for e in db.query(WfEdge).filter(WfEdge.template_version_id == template_version_id).all():
        src, tgt = node_id_map.get(e.source_node_id), node_id_map.get(e.target_node_id)
        if src is None or tgt is None:
            continue  # edge referencing a node that no longer exists; skip it
        db.add(WfEdge(
            template_version_id=clone.template_version_id,
            source_node_id=src,
            target_node_id=tgt,
            source_port_id=e.source_port_id,
            target_port_id=e.target_port_id,
            condition_expr=e.condition_expr,
            condition_desc=e.condition_desc,
        ))

    db.commit()
    return {
        "message": f"Cloned '{source.name}' into your workspace.",
        "template_id": clone.template_id,
        "template_version_id": clone.template_version_id,
        "name": clone.name,
        "nodes_copied": len(node_id_map),
    }


@app.get("/api/pipeline-runs")
def list_pipeline_runs(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    runs = visible_runs(db, user).order_by(PipelineRun.created_at.desc()).all()
    # Join the template name so the history page can show which workflow ran.
    # Looked up by the ids these runs actually reference rather than by loading
    # every template in the database — a run's own template may since have been
    # unshared, and this is a label lookup, not an access grant.
    template_ids = {r.template_version_id for r in runs if r.template_version_id is not None}
    templates = {
        t.template_version_id: t.name
        for t in db.query(WorkflowTemplate).filter(
            WorkflowTemplate.template_version_id.in_(template_ids)
        ).all()
    } if template_ids else {}
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
def get_pipeline_run_detail(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Per-run detail by run_id: overall status + each step's status/outputs.
    Used by the history page to show which node is running/completed/failed."""
    from models import RunStep, WfNode
    run = run_or_404(db, user, run_id)
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
        # Run-level Tapis options this run was launched with (exec_system,
        # work_dir, slurm_account, ...) — lets the UI show "what was this run
        # configured with" and lets a re-run reuse the same options.
        "frozen_config": run.frozen_config,
        "steps": [
            {
                "node_id": s.node_id,
                "step_type": node_keys.get(s.node_id, "?"),
                "status": s.status,
                "tapis_job_uuid": s.tapis_job_uuid,
                "tapis_job_status": s.tapis_job_status,
                "error_message": s.error_message,
                "config": s.config,
                "outputs": s.outputs,
            } for s in steps
        ],
    }

@app.get("/api/pipeline-runs/{run_id}/step/{node_id}/logs")
def get_step_logs(run_id: int, node_id: int, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Fetch detailed failure logs for a step: the DBOS error we recorded, plus
    the underlying Tapis job's lastMessage and its container stdout/stderr
    (tapisjob.out). Best-effort — degrades gracefully if the token is stale or
    the job output isn't reachable."""
    from models import RunStep
    import httpx as _httpx
    from engine import tapis_auth

    # Ownership is checked on the RUN before any step lookup. These logs are
    # container stdout/stderr and Tapis job messages — among the most revealing
    # output in the system.
    run_or_404(db, user, run_id)

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

    token = tapis_auth.get_token_for_run(run_id)
    if not token:
        result["logs_note"] = "No valid Tapis token for this run's owner; cannot fetch job logs."
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
def execute_single_node(req: NodeExecutionRequest, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    # Still a simulation (it only echoes the request back), but the access check
    # belongs here now rather than whenever this grows a real job submission.
    template_or_404(db, user, req.template_version_id)

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

def _build_dag_config(db: Session, template_version_id: int, user: AppUser) -> dict:
    """Read a saved template's nodes/edges and shape them into the dag_config the
    DBOS orchestrator expects: {template_version_id, nodes:[{id,type,inputs}], edges:[{from,to}]}.

    Node ids are the string form of wf_node.node_id (the DAG node key the engine
    uses throughout). Each node's inputs come from its saved default_config.
    """
    # Scoped here rather than at the call site so a future caller cannot execute
    # a template its user can't see. Launching someone else's workflow would
    # also run it under THIS user's Tapis token and allocation.
    template = template_or_404(db, user, template_version_id)

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
    # GPU target for this run. A step whose step.json declares
    # "resources": {"gpu": true} (zero_shot_annotation, training, …) routes
    # here instead of the CPU pair above, so one run can span both without any
    # step hardcoding a site — see engine.transactions.resolve_node_exec_target.
    # Falls back to the CPU pair when unset, matching pre-split behaviour.
    gpu_exec_system: Optional[str] = None
    gpu_exec_queue: Optional[str] = None
    work_dir: Optional[str] = None
    archive_system: Optional[str] = None
    # Base directory step outputs archive under (run_id/node_id is still
    # appended beneath it for isolation). Optional override — when omitted,
    # the base is derived from work_dir instead (see get_run_archive_context).
    archive_dir: Optional[str] = None


@app.post("/api/pipeline-runs/{template_version_id}/execute")
def execute_workflow(template_version_id: int, options: Optional[RunOptions] = None, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Kick off durable execution of a saved workflow template.

    Builds the DAG from the persisted template and starts the DBOS orchestrator
    asynchronously. The pipeline_run + run_step rows are created synchronously
    here (not inside the orchestrator's first transaction) so this call can
    return a real run_id immediately — the frontend navigates straight to
    /runs/{run_id} instead of polling a bare workflow id for one to appear.

    Optional run-level Tapis values (slurm_account, archive_system, work_dir, …)
    are merged into the dag_config, which the orchestrator stores as frozen_config
    and the engine reads when rendering each step's Tapis job spec. The per-step
    archive location is derived automatically (work_dir/wf_runs/<run_id>/<node>).
    """
    dag_config = _build_dag_config(db, template_version_id, user)
    if options is not None:
        for key, value in options.model_dump(exclude_none=True).items():
            dag_config[key] = value

    # Default the charge account from the template's allocation_account when the
    # run didn't specify one (RunOptions.slurm_account still overrides).
    # _build_dag_config above already proved this user may see the template, so
    # this cannot 404 here; it goes through the scoped helper anyway so no
    # unscoped template query exists in this file.
    template = template_or_404(db, user, template_version_id)
    dag_config.setdefault("slurm_account", template.allocation_account or "uot260")

    # Record the launching user so the run is owned by them and the engine can
    # resolve their Tapis token (get_token_for_run) when submitting jobs. The
    # username is also needed to derive expanse-tapis's per-user scratch path
    # when a run spans systems and can't inherit one work_dir (see
    # engine.transactions._default_work_dir).
    dag_config["owner_id"] = user.user_id
    dag_config.setdefault("tapis_username", user.username)

    # Pick the DBOS workflow id ourselves (instead of letting DBOS generate one)
    # so we can create the pipeline_run row referencing it before the workflow
    # starts, and pass the resulting run_id back in this same response.
    dbos_workflow_id = f"dag-{uuid.uuid4()}"

    run = PipelineRun(
        template_version_id=template_version_id,
        user_id=user.user_id,
        name=dag_config.get("name", "DAG Run"),
        status="RUNNING",
        dbos_workflow_id=dbos_workflow_id,
        frozen_config=dag_config,
    )
    db.add(run)
    db.flush()  # assign run.run_id
    for node in dag_config.get("nodes", []):
        db.add(RunStep(
            run_id=run.run_id,
            node_id=int(node["id"]),
            step_label=node.get("label", str(node["id"])),
            status="pending",
            config=node.get("inputs", {}) or {},
            outputs={},
        ))
    db.commit()

    # Tell the orchestrator to reuse this run_id instead of creating its own
    # (see create_run_for_template's run_id short-circuit).
    dag_config["run_id"] = run.run_id

    with SetWorkflowID(dbos_workflow_id):
        DBOS.start_workflow(dag_orchestrator_workflow, dag_config)

    return {
        "message": "Workflow execution started",
        "run_id": run.run_id,
        "dbos_workflow_id": dbos_workflow_id,
        "template_version_id": template_version_id,
    }


@app.post("/api/pipeline-runs/{run_id}/stop")
def stop_pipeline_run(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Stop a running pipeline run: cancel the DBOS workflow (and its child
    node-workflows), cancel any in-flight Tapis job, and mark the run + its
    non-terminal steps as cancelled."""
    from models import RunStep
    from engine.tapis import cancel_tapis_job

    # Stopping is a write: without this any authenticated user could cancel
    # anyone's in-flight run and its Tapis jobs.
    run = run_or_404(db, user, run_id)

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
            if cancel_tapis_job(s.tapis_job_uuid, run_id):
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
def get_workflow_run_status(dbos_workflow_id: str, format: str = "json", db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Return live status of a workflow run: overall DBOS state, per-step detail,
    and an ASCII progress graph. `format=text` returns just the graph as text."""
    # Keyed by the DBOS workflow id rather than run_id, so ownership is resolved
    # through the pipeline_run row that records it. Checked BEFORE asking DBOS
    # for anything: the response carries per-step detail and the run's graph.
    owned = visible_runs(db, user).filter(
        PipelineRun.dbos_workflow_id == dbos_workflow_id
    ).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Workflow run not found")

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


# --- Tapis Files proxy (Image Preprocessing Studio: browse a directory + save) ---
# Proxies the Tapis Files API using the logged-in user's OAuth token so the studio
# panel can browse a storage system's images and write operations.json to a path.
# Requires a real Tapis session — in mock/logged-out state get_token_for_user
# returns None and these return 401 (the panel surfaces "log in with Tapis").

def _user_tapis_token(user: AppUser, db: Session) -> str:
    from engine import tapis_auth
    token = tapis_auth.get_token_for_user(user, db)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="No valid Tapis token — log in with a real Tapis account to browse files.",
        )
    return token


def _tapis_files_url(kind: str, system: str, path: str) -> str:
    # kind is "ops" (list/write) or "content" (download). Tapis file paths may
    # contain slashes; they belong in the URL path after the system id.
    from engine import tapis_auth
    return f"{tapis_auth.TAPIS_BASE_URL}/v3/files/{kind}/{system}/{path.lstrip('/')}"


@app.get("/api/tapis-files/list")
def tapis_files_list(system: str, path: str = "/", db: Session = Depends(get_db),
                     user: AppUser = Depends(get_current_user)):
    """List a directory on a Tapis storage system as the current user. Returns
    Tapis' file objects: [{name, path, size, type: 'file'|'dir', mimeType}]."""
    token = _user_tapis_token(user, db)
    try:
        resp = httpx.get(_tapis_files_url("ops", system, path),
                         headers={"X-Tapis-Token": token}, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Tapis rejected the token (expired or unauthorized).")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Path not found on Tapis.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Tapis list failed (HTTP {resp.status_code}).")
    return {"result": resp.json().get("result", [])}


@app.get("/api/tapis-files/content")
def tapis_files_content(system: str, path: str, db: Session = Depends(get_db),
                        user: AppUser = Depends(get_current_user)):
    """Stream a file's bytes from a Tapis storage system as the current user."""
    token = _user_tapis_token(user, db)
    try:
        resp = httpx.get(_tapis_files_url("content", system, path),
                         headers={"X-Tapis-Token": token}, timeout=60)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code != 200:
        code = resp.status_code if resp.status_code in (401, 404) else 502
        raise HTTPException(status_code=code, detail=f"Tapis content failed (HTTP {resp.status_code}).")
    return Response(content=resp.content,
                    media_type=resp.headers.get("content-type", "application/octet-stream"))


class TapisFileWrite(BaseModel):
    system: str
    path: str
    content: str  # text to write (e.g. serialized operations.json)


@app.post("/api/tapis-files/upload")
def tapis_files_upload(body: TapisFileWrite, db: Session = Depends(get_db),
                       user: AppUser = Depends(get_current_user)):
    """Write text content to a Tapis storage path as the current user (multipart
    insert). Used to save the studio's operations.json where the job expects it."""
    token = _user_tapis_token(user, db)
    filename = body.path.rstrip("/").split("/")[-1] or "operations.json"
    try:
        resp = httpx.post(
            _tapis_files_url("ops", body.system, body.path),
            headers={"X-Tapis-Token": token},
            files={"file": (filename, body.content.encode("utf-8"), "application/json")},
            timeout=60,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code not in (200, 201):
        code = 401 if resp.status_code == 401 else 502
        raise HTTPException(status_code=code, detail=f"Tapis upload failed (HTTP {resp.status_code}): {resp.text[:200]}")
    return {"ok": True, "path": body.path}


@app.get("/api/tapis-systems/{system_id}/queues")
def list_tapis_system_queues(system_id: str, db: Session = Depends(get_db),
                             user: AppUser = Depends(get_current_user)):
    """List a Tapis exec system's batch scheduler queues, so the Run Settings UI
    can offer a real queue dropdown instead of free text. Tapis exec systems
    (pitzer-tapis, expanse-tapis, ...) are shared/public system definitions —
    any authenticated user's token can read them, no special ownership needed."""
    from engine import tapis_auth
    token = _user_tapis_token(user, db)
    url = f"{tapis_auth.TAPIS_BASE_URL}/v3/systems/{system_id}"
    try:
        resp = httpx.get(url, headers={"X-Tapis-Token": token}, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Tapis rejected the token (expired or unauthorized).")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"System '{system_id}' not found on Tapis.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Tapis system lookup failed (HTTP {resp.status_code}).")
    result = resp.json().get("result", {}) or {}
    return {
        "system_id": system_id,
        "default_queue": result.get("batchDefaultLogicalQueueName"),
        # Raw Tapis batchLogicalQueue objects (name, hpcQueueName, max/min node
        # count, cores per node, memory, minutes, ...) — passed through as-is
        # so the frontend can show whatever fields Tapis actually returns.
        "queues": result.get("batchLogicalQueues", []) or [],
    }


### Secrets — team-scoped API tokens (Weights & Biases, Hugging Face, ...)
# referenced by KEY from a step's config_schema ("type": "secret"). Managed from
# the dashboard's settings dropdown; values are write-only through this API —
# GET never returns them, and they're decrypted only by engine.secrets at
# job-submission time (see workflows.py's _resolve_secrets).

_SECRET_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _validate_secret_key(key: str) -> str:
    key = (key or "").strip()
    if not key or not _SECRET_KEY_RE.match(key):
        raise HTTPException(
            status_code=422,
            detail="Secret key must start with a letter and contain only letters, numbers, and underscores.",
        )
    return key


class SecretCreate(BaseModel):
    key: str
    value: str
    description: Optional[str] = ""


class SecretUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None


def _secret_out(row: Secret) -> dict:
    return {
        "key": row.key,
        "description": row.description or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.get("/api/secrets")
def list_secrets(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """List the current user's team's secrets — keys/descriptions only, never values."""
    rows = db.query(Secret).filter(Secret.team_id == user.team_id).order_by(Secret.key).all()
    return {"secrets": [_secret_out(r) for r in rows]}


@app.post("/api/secrets")
def create_secret(body: SecretCreate, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Add a new secret for the current user's team."""
    from engine import secrets as secrets_store

    key = _validate_secret_key(body.key)
    if not body.value:
        raise HTTPException(status_code=422, detail="Secret value is required.")
    existing = db.query(Secret).filter(Secret.team_id == user.team_id, Secret.key == key).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"A secret named '{key}' already exists.")
    row = Secret(
        team_id=user.team_id,
        key=key,
        description=(body.description or "").strip(),
        encrypted_value=secrets_store.encrypt_value(body.value),
        created_by_id=user.user_id,
    )
    db.add(row)
    db.commit()
    return _secret_out(row)


@app.put("/api/secrets/{key}")
def update_secret(key: str, body: SecretUpdate, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Update a secret's value and/or description. Omit value to change only the description."""
    from engine import secrets as secrets_store

    row = db.query(Secret).filter(Secret.team_id == user.team_id, Secret.key == key).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"No secret named '{key}'.")
    if body.value:
        row.encrypted_value = secrets_store.encrypt_value(body.value)
    if body.description is not None:
        row.description = body.description.strip()
    db.commit()
    return _secret_out(row)


def _delete_secret(key: str, db: Session, user: AppUser) -> dict:
    row = db.query(Secret).filter(Secret.team_id == user.team_id, Secret.key == key).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"No secret named '{key}'.")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.delete("/api/secrets/{key}")
def delete_secret(key: str, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    return _delete_secret(key, db, user)


# POST alias for the same delete — DELETE always triggers a CORS preflight
# (unlike GET/simple POST), and some tunnels/reverse proxies between the
# frontend and this backend don't forward less-common HTTP methods (or their
# preflight) correctly, surfacing as a browser-side "CORS error" even though
# this endpoint itself is fine. The frontend uses this alias so delete rides
# the same POST path already proven to work (e.g. "Add secret"); the DELETE
# route above stays for any client that can use it directly.
@app.post("/api/secrets/{key}/delete")
def delete_secret_via_post(key: str, db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    return _delete_secret(key, db, user)


@app.get("/api/tapis/token")
def tapis_token(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Hand the current user's raw Tapis access token to the browser, for step
    panels (e.g. smart_labeler) that embed a third-party component making Tapis
    calls directly from client JS instead of through our /api/tapis-files proxy.
    Requires the Tapis tenant to allow CORS from this frontend's origin.

    `expires_at` is returned alongside so the caller can tell when its copy has
    gone stale. Without it the browser's only options were to hold the token
    forever (which broke whenever the Tapis session was re-authenticated behind
    an open page — the panel kept presenting a dead JWT straight to Tapis) or to
    decode the JWT client-side, which means shipping a JWT library to answer a
    question the server already knows. Null when the token carries no readable
    `exp`; treat that as "unknown, re-fetch on use".

    The token itself is resolved through get_token_for_user, so it reflects any
    refresh that happened server-side, and _sync_inbound_token has already
    written this request's own (newer) inbound token to the user before we get
    here — meaning a re-fetch after a Tapis re-auth returns the NEW token.
    """
    from engine import tapis_auth
    token = _user_tapis_token(user, db)
    return {"token": token, "expires_at": tapis_auth.token_expiry(token)}


@app.get("/api/tapis/whoami")
def tapis_whoami(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """Diagnostic: report how Tapis auth is configured (mode) and whether the
    resolved token actually validates against Tapis. Reaching this endpoint at all
    means the APP session is fine; a bad token here means the Tapis credential is
    the problem, not the login."""
    from engine import tapis_auth
    info = tapis_auth.describe_credentials()
    token = tapis_auth.get_token_for_user(user, db)
    if not token:
        info["token_present"] = False
        return info
    check = tapis_auth.validate_token(token)
    info["token_present"] = True
    info["token_valid"] = check["valid"]
    info["tapis_username"] = check["username"]
    info["token_detail"] = check["detail"]
    return info


# --- OpenAPI ------------------------------------------------------------------
# The spec is GENERATED from the live routes, never hand-written, so it cannot
# drift from the code. This pass adds what FastAPI can't infer: the auth schemes,
# which operations are public, tags for routes declared directly on `app`
# (routers carry their own), and the error responses every authenticated
# operation shares.
#
# Export a copy with:  python -m scripts.export_openapi
# Browse it live at:   /docs (Swagger UI)  ·  /redoc  ·  /openapi.json

# Operations reachable without a credential — the login handshake plus health.
# Everything else requires one, so this is a deny-by-default list: a new route
# is treated as authenticated unless it is named here.
_PUBLIC_OPERATIONS = {
    ("/", "get"),
    ("/login", "get"),
    ("/logout", "get"),
    ("/oauth2/callback", "get"),
    # Reports only what the CALLER supplied and names no other user; it exists
    # precisely for debugging a request that cannot authenticate.
    ("/auth-debug", "get"),
    # Returns the current identity, or null when signed out — it answers "am I
    # logged in", so requiring login would defeat it.
    ("/me", "get"),
}

# Path prefix -> tag, for operations declared on `app` itself. Longest prefix
# wins, so /api/pipeline-runs/{run_id}/geospatial/... keeps the tag its own
# router already set rather than being relabelled a pipeline-run.
_TAG_BY_PREFIX = [
    ("/api/workflow-templates", "workflow-templates"),
    ("/api/pipeline-runs", "pipeline-runs"),
    ("/api/step-types", "step-registry"),
    ("/api/port-data-types", "step-registry"),
    ("/api/secrets", "secrets"),
    ("/api/tapis-files", "tapis"),
    ("/api/tapis-systems", "tapis"),
    ("/api/tapis", "tapis"),
    ("/login", "auth"),
    ("/logout", "auth"),
    ("/oauth2", "auth"),
    ("/auth-debug", "auth"),
    ("/me", "auth"),
    ("/", "meta"),
]


def custom_openapi():
    """Build (and cache) the enriched OpenAPI schema."""
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        license_info=app.license_info,
        contact=app.contact,
    )

    schema.setdefault("components", {})["securitySchemes"] = {
        "tapisToken": {
            "type": "apiKey", "in": "header", "name": "X-Tapis-Token",
            "description": "Tapis access token. Preferred when the Studio is "
                           "embedded in TapisUI, which supplies it.",
        },
        "sessionCookie": {
            "type": "apiKey", "in": "cookie", "name": "session",
            "description": "Signed session cookie from this app's own Tapis "
                           "OAuth login (GET /login).",
        },
    }
    # A list of two single-key requirements means OR: either credential alone
    # satisfies the operation. One dict with two keys would mean AND.
    default_security = [{"tapisToken": []}, {"sessionCookie": []}]

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue

            if not operation.get("tags"):
                for prefix, tag in _TAG_BY_PREFIX:
                    if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                        operation["tags"] = [tag]
                        break

            if (path, method) in _PUBLIC_OPERATIONS:
                operation["security"] = []   # explicitly public
                continue

            operation["security"] = default_security
            operation.setdefault("responses", {}).setdefault("401", {
                "description": "Not authenticated — no valid X-Tapis-Token or session cookie.",
            })
            # Anything addressed by an id can also be someone else's. That is
            # reported as 404 rather than 403 by design; see the description.
            if "{" in path:
                operation["responses"].setdefault("404", {
                    "description": "Not found, or not visible to the calling user.",
                })

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


if __name__ == "__main__":
    # reload spawns a child process, which breaks step debuggers (PyCharm/pdb):
    # the debugger attaches to the parent while the app runs in the child.
    # Default to no reload so `python main.py` is debuggable; opt back in with
    # UVICORN_RELOAD=true for normal hot-reload dev.
    reload = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    uvicorn.run("main:app", host="127.0.0.1", port=8002, reload=reload)
