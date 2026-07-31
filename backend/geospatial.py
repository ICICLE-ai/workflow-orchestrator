"""Geospatial resource: GeoPackage-backed layers, exposed as on-demand
GeoJSON / zipped-Shapefile / raw-GeoPackage downloads.

Two routers, sharing the same GDAL/OGR (geopandas + pyogrio) conversion core
(the _*_at helpers, each parameterized by a plain (system, path) Tapis
location) but differing in how that location is resolved:

  - `router` (mounted at /api/pipeline-runs/{run_id}/geospatial/...): for a
    specific completed run. The GeoPackage can come from either step type that
    declares a 'geopackage'-typed output port — the 'geospatial' job step
    (generates one from detection results + imagery) or the static
    'source_geopackage' step (a user-supplied .gpkg path) — see
    _find_gpkg_producing_node, which looks the port up by data_type rather
    than a hardcoded step_type_key.
  - `preview_router` (mounted at /api/geospatial-preview/...): design-time,
    no run required. Takes a `uri` query param (a tapis://system/path URI)
    directly, so a 'source_geopackage' node's static path can be previewed
    from the canvas before the workflow has ever been run — a 'geospatial'
    job step has no output to preview until it actually runs, so this only
    does anything useful wired to a source node.

Internal storage format is GeoPackage (.gpkg). Shapefile and GeoJSON are
transport formats only, ever produced on demand here and never stored.

`router` sits alongside the other /api/pipeline-runs/{run_id}/... routes
(detail, stop, step/{node_id}/logs) in main.py — run_id is this codebase's
resource id for a pipeline run. A run is assumed to have at most one
geopackage-producing node (see _find_gpkg_producing_node); a run with two
would silently pick the first WfNode match.
"""
import os
import tempfile
import zipfile

import geopandas as gpd
import httpx
import pyogrio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from db import get_db
from models import AppUser, PipelineRun, RunStep, StepTypeRegistry, StepTypePort, WfNode
from engine import tapis_auth

router = APIRouter(prefix="/api/pipeline-runs/{run_id}/geospatial", tags=["geospatial"])
preview_router = APIRouter(prefix="/api/geospatial-preview", tags=["geospatial-preview"])

GEOPACKAGE_DATA_TYPE = "geopackage"


def _current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    """Session-cookie auth — same body as main.get_current_user, duplicated here
    (rather than imported) to avoid a circular import: main imports this module
    to mount its routers."""
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(AppUser).filter(AppUser.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session user no longer exists")
    return user


def _user_tapis_token(user: AppUser, db: Session) -> str:
    token = tapis_auth.get_token_for_user(user, db)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="No valid Tapis token — log in with a real Tapis account.",
        )
    return token


def _split_tapis_uri(uri: str):
    """tapis://system/abs/path -> (system, /abs/path). Bare/missing -> (None, uri)."""
    if not uri or not uri.startswith("tapis://"):
        return None, uri or ""
    rest = uri[len("tapis://"):]
    system, _, path = rest.partition("/")
    return system, "/" + path


def _resolve_uri_param(uri: str):
    """Split a `uri` query param (preview_router) into (system, path), rejecting
    anything that isn't a proper tapis://system/path URI."""
    system, path = _split_tapis_uri(uri)
    if not system:
        raise HTTPException(
            status_code=422,
            detail=f"'uri' must be a tapis://system/path URI (got {uri!r}).",
        )
    return system, path


def _find_gpkg_producing_node(run_id: int, db: Session):
    """Locate this run's GeoPackage-producing node and return
    (RunStep, output_port_name, step_type_key).

    Looked up by output PORT DATA TYPE ('geopackage'), not a hardcoded
    step_type_key, so it matches whichever step actually produced this run's
    GeoPackage — the 'geospatial' job step or a static 'source_geopackage' node
    (see module docstring). Assumes at most one such node per run.
    """
    run = db.query(PipelineRun).filter(PipelineRun.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    gpkg_ports = db.query(StepTypePort).filter(
        StepTypePort.direction == "output",
        StepTypePort.data_type == GEOPACKAGE_DATA_TYPE,
    ).all()
    port_name_by_step_type = {p.step_type_key: p.port_name for p in gpkg_ports}
    if not port_name_by_step_type:
        raise HTTPException(status_code=500, detail="No step type declares a 'geopackage' output port")

    node = db.query(WfNode).filter(
        WfNode.template_version_id == run.template_version_id,
        WfNode.step_type_key.in_(port_name_by_step_type.keys()),
    ).first()
    if not node:
        raise HTTPException(status_code=404, detail="This run has no GeoPackage-producing step")

    step = db.query(RunStep).filter(
        RunStep.run_id == run_id, RunStep.node_id == node.node_id
    ).first()
    if not step:
        raise HTTPException(status_code=404, detail="GeoPackage-producing step has not run yet")

    return step, port_name_by_step_type[node.step_type_key], node.step_type_key


def _gpkg_tapis_location(run_id: int, db: Session):
    step, port_name, _step_type_key = _find_gpkg_producing_node(run_id, db)
    if (step.status or "").lower() != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"GeoPackage-producing step is '{step.status}', not completed — no GeoPackage available yet.",
        )
    gpkg_uri = (step.outputs or {}).get(port_name)
    if not gpkg_uri:
        raise HTTPException(status_code=404, detail="GeoPackage-producing step produced no output")
    system, path = _split_tapis_uri(gpkg_uri)
    if not system:
        raise HTTPException(
            status_code=500,
            detail=(
                f"GeoPackage output has no Tapis system (got {gpkg_uri!r}, expected "
                "tapis://system/path). If this came from a source_geopackage node, "
                "open its settings and pick a Tapis system, then re-run."
            ),
        )
    return system, path


# --- conversion core, parameterized by a plain (system, path) location -----
# Shared by both routers: `router`'s handlers resolve (system, path) from a
# run's RunStep output; `preview_router`'s handlers resolve it straight from
# the `uri` query param. Neither knows about the other's resolution path.

def _fetch_gpkg_at(system: str, path: str, user: AppUser, db: Session, dest_path: str) -> None:
    """Download a GeoPackage from Tapis storage to a local temp path."""
    token = _user_tapis_token(user, db)
    url = f"{tapis_auth.TAPIS_BASE_URL}/v3/files/content/{system}{path}"
    try:
        resp = httpx.get(url, headers={"X-Tapis-Token": token}, timeout=60)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Tapis rejected the token (expired or unauthorized).")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="GeoPackage not found on Tapis storage.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Tapis download failed (HTTP {resp.status_code}).")
    with open(dest_path, "wb") as f:
        f.write(resp.content)


def _list_labels_at(system: str, path: str, user: AppUser, db: Session) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        gpkg_path = os.path.join(tmp, "geo.gpkg")
        _fetch_gpkg_at(system, path, user, db, gpkg_path)
        try:
            layers = pyogrio.list_layers(gpkg_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not read GeoPackage: {e}")
    return {"labels": [row[0] for row in layers]}


def _list_missing_at(system: str, path: str, user: AppUser, db: Session) -> dict:
    """See list_missing's docstring for the 'missing' layer-name assumption."""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg_path = os.path.join(tmp, "geo.gpkg")
        _fetch_gpkg_at(system, path, user, db, gpkg_path)
        try:
            layer_names = {row[0] for row in pyogrio.list_layers(gpkg_path)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not read GeoPackage: {e}")
        if "missing" not in layer_names:
            return {"missing": []}
        try:
            df = gpd.read_file(gpkg_path, layer="missing")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not read 'missing' layer: {e}")
    records = df.drop(columns=[c for c in ("geometry",) if c in df.columns]).to_dict(orient="records")
    return {"missing": records}


def _geojson_at(system: str, path: str, label: str, user: AppUser, db: Session) -> Response:
    with tempfile.TemporaryDirectory() as tmp:
        gpkg_path = os.path.join(tmp, "geo.gpkg")
        _fetch_gpkg_at(system, path, user, db, gpkg_path)
        try:
            gdf = gpd.read_file(gpkg_path, layer=label)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Layer '{label}' not found or unreadable: {e}")
        geojson_str = gdf.to_json()
    return Response(content=geojson_str, media_type="application/json")


def _export_shapefile_zip_at(system: str, path: str, label: str, user: AppUser, db: Session, zip_stem: str) -> Response:
    with tempfile.TemporaryDirectory() as tmp:
        gpkg_path = os.path.join(tmp, "geo.gpkg")
        _fetch_gpkg_at(system, path, user, db, gpkg_path)
        try:
            gdf = gpd.read_file(gpkg_path, layer=label)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Layer '{label}' not found or unreadable: {e}")

        shp_dir = os.path.join(tmp, "shp")
        os.makedirs(shp_dir, exist_ok=True)
        shp_path = os.path.join(shp_dir, f"{label}.shp")
        try:
            gdf.to_file(shp_path, driver="ESRI Shapefile")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not convert layer '{label}' to Shapefile: {e}")

        zip_path = os.path.join(tmp, f"{zip_stem}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(shp_dir):
                zf.write(os.path.join(shp_dir, fname), arcname=fname)

        with open(zip_path, "rb") as f:
            data = f.read()

    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_stem}.zip"'},
    )


def _raw_gpkg_response_at(system: str, path: str, user: AppUser, db: Session, filename: str) -> Response:
    with tempfile.TemporaryDirectory() as tmp:
        gpkg_path = os.path.join(tmp, "geo.gpkg")
        _fetch_gpkg_at(system, path, user, db, gpkg_path)
        with open(gpkg_path, "rb") as f:
            data = f.read()
    return Response(
        content=data,
        media_type="application/geopackage+sqlite3",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- run-scoped routes ------------------------------------------------------

@router.get("/labels")
def list_labels(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    """Available layer names in the run's GeoPackage."""
    system, path = _gpkg_tapis_location(run_id, db)
    return _list_labels_at(system, path, user, db)


@router.get("/config")
def get_config(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    """Generator/spray-mode config metadata for the step that produced this run's
    GeoPackage (generatorMode, sprayMode, levels, thresholds). A static
    'source_geopackage' node has no such fields — its config_schema has none of
    these keys, so every value here comes back null."""
    step, _port_name, step_type_key = _find_gpkg_producing_node(run_id, db)
    cfg = step.config or {}
    step_type = db.query(StepTypeRegistry).filter(
        StepTypeRegistry.step_type_key == step_type_key
    ).first()
    schema = (step_type.config_schema or {}) if step_type else {}

    def default(key):
        entry = schema.get(key)
        return entry.get("default") if isinstance(entry, dict) else None

    return {
        "generatorMode": cfg.get("generator_mode", default("generator_mode")),
        "sprayMode": cfg.get("spray_mode", default("spray_mode")),
        "levels": cfg.get("spray_levels", default("spray_levels")),
        "thresholds": cfg.get("spray_thresholds", default("spray_thresholds")),
    }


@router.get("/missing")
def list_missing(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    """Expected entries with no corresponding source imagery/data.

    ASSUMPTION (unconfirmed against the real Tapis app, which lives outside this
    repo): entries are recorded as a non-spatial attribute layer literally named
    'missing' inside the GeoPackage. If no such layer exists, an empty list is
    returned rather than treated as an error.
    """
    system, path = _gpkg_tapis_location(run_id, db)
    return _list_missing_at(system, path, user, db)


@router.get("/geojson/{label}")
def get_geojson(run_id: int, label: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    """Read a GeoPackage layer and convert it to GeoJSON on demand — this is what
    the Leaflet map consumes; raw GeoPackage bytes are never returned here."""
    system, path = _gpkg_tapis_location(run_id, db)
    return _geojson_at(system, path, label, user, db)


@router.get("/download/{label}")
def download_shapefile(run_id: int, label: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    """Convert a GeoPackage layer to a zipped ESRI Shapefile bundle on demand."""
    system, path = _gpkg_tapis_location(run_id, db)
    return _export_shapefile_zip_at(system, path, label, user, db, f"{label}_shapefile")


@router.get("/download-farm/{label}")
def download_farm_boundary(run_id: int, label: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    """Same conversion as /download, for the farm/field boundary layer.

    ASSUMPTION (unconfirmed): {label} names the same GeoPackage layer as
    /download/{label} — this endpoint only changes the output filename
    convention. If boundary geometry actually lives under one fixed layer name
    independent of {label}, point this at that fixed name once the real
    GeoPackage schema (produced by the Tapis app outside this repo) is known.
    """
    system, path = _gpkg_tapis_location(run_id, db)
    return _export_shapefile_zip_at(system, path, label, user, db, f"{label}_farm_boundary")


@router.get("/download-gpkg/{label}")
def download_gpkg(run_id: int, label: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    """Stream the run's raw GeoPackage file back unmodified, for QGIS/ArcGIS.

    A GeoPackage is one file holding every layer, so {label} does not subset
    it — the whole file is returned regardless of which label was requested;
    {label} only names the downloaded file.
    """
    system, path = _gpkg_tapis_location(run_id, db)
    return _raw_gpkg_response_at(system, path, user, db, f"{label}.gpkg")


# --- design-time preview routes (no run required) ---------------------------
# For a static 'source_geopackage' node, wired straight into geospatial_map on
# the canvas — same conversions as above, but resolved from a `uri` query
# param (tapis://system/path) instead of a run's RunStep output. A 'geospatial'
# job step has nothing to preview here until it actually runs.

@preview_router.get("/labels")
def preview_labels(uri: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    system, path = _resolve_uri_param(uri)
    return _list_labels_at(system, path, user, db)


@preview_router.get("/missing")
def preview_missing(uri: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    system, path = _resolve_uri_param(uri)
    return _list_missing_at(system, path, user, db)


@preview_router.get("/geojson/{label}")
def preview_geojson(label: str, uri: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    system, path = _resolve_uri_param(uri)
    return _geojson_at(system, path, label, user, db)


@preview_router.get("/download/{label}")
def preview_download_shapefile(label: str, uri: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    system, path = _resolve_uri_param(uri)
    return _export_shapefile_zip_at(system, path, label, user, db, f"{label}_shapefile")


@preview_router.get("/download-farm/{label}")
def preview_download_farm_boundary(label: str, uri: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    system, path = _resolve_uri_param(uri)
    return _export_shapefile_zip_at(system, path, label, user, db, f"{label}_farm_boundary")


@preview_router.get("/download-gpkg/{label}")
def preview_download_gpkg(label: str, uri: str, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    system, path = _resolve_uri_param(uri)
    return _raw_gpkg_response_at(system, path, user, db, f"{label}.gpkg")
