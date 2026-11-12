"""Import a run executed OFF-platform (runner/, "Deploy to my node") as a run.

The runner never calls back to the platform while it works (see
local_bundle.py). Instead it ends every run by packing run-<id>-report.zip:

    report.json                         what ran: status, per-step results,
                                        the bundle's template id and edges
    logs/<node_id>.log                  tail of each job step's runner.log
    artifacts/<node_id>/<port>/...      that step's small output files

Uploading that zip creates an ordinary PipelineRun + RunStep rows against the
template version the bundle was exported from (the bundle's node ids ARE that
version's wf_node ids), so the run page draws it on the same canvas with no
special casing.

WHERE THE FILES LIVE
--------------------
The step panels read their data through /api/tapis-files/{list,content} with a
{system, path} pair taken from a step's resolved config. The node's own paths
mean nothing here, so an imported run's packed files are stored on the backend
and exposed under a synthetic system id, `wf-local-run-<run_id>`: a captured
output becomes `tapis://wf-local-run-<run_id>/<node_id>/<port>/...`, and the
two file endpoints serve that system from storage (owner-checked) instead of
asking Tapis. Every panel that reads a run's outputs therefore works on an
imported run unchanged. An output that was NOT packed (weights, image folders)
keeps its path on the node, which the UI already treats as unbrowsable.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from models import PipelineRun, RunArtifact, RunStep, StepTypePort, WfNode

SOURCE = "local"
SYSTEM_PREFIX = "wf-local-run-"
_SYSTEM = re.compile(rf"^{re.escape(SYSTEM_PREFIX)}(\d+)$")

STORAGE_DIR = Path(os.getenv("WF_LOCAL_RUNS_DIR") or Path(__file__).resolve().parent.parent / "data" / "local_runs")
MAX_UPLOAD_BYTES = int(float(os.getenv("WF_LOCAL_RUN_MAX_UPLOAD_MB", "200")) * 1024 * 1024)
# Uncompressed, so a small zip can't unpack into something enormous.
MAX_UNPACKED_BYTES = 2 * MAX_UPLOAD_BYTES

# The runner's step statuses -> RunStep's vocabulary (see runs.tsx stepColor).
_STEP_STATUS = {"completed": "completed", "failed": "failed", "skipped": "skipped"}


class PackageError(ValueError):
    """The upload is not a usable run package; the message says why."""


def system_for(run_id: int) -> str:
    return f"{SYSTEM_PREFIX}{run_id}"


def run_id_for_system(system: str) -> int | None:
    m = _SYSTEM.match(system or "")
    return int(m.group(1)) if m else None


def run_dir(run_id: int) -> Path:
    return STORAGE_DIR / str(run_id)


def stored_path(run_id: int, path: str) -> Path:
    """A path under an imported run's storage, refusing anything that escapes it."""
    base = run_dir(run_id).resolve()
    target = (base / "artifacts" / path.lstrip("/")).resolve()
    if not target.is_relative_to(base / "artifacts"):
        raise FileNotFoundError(path)
    return target


def is_local(run: PipelineRun) -> bool:
    return (run.frozen_config or {}).get("source") == SOURCE


# --- reading the package -----------------------------------------------------

def open_package(data: bytes) -> tuple[dict, zipfile.ZipFile]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise PackageError(f"package is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackageError("not a zip file — upload the run-<id>-report.zip the runner wrote")
    if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED_BYTES:
        raise PackageError("package unpacks to more than the allowed size")
    try:
        report = json.loads(zf.read("report.json"))
    except KeyError:
        raise PackageError("package has no report.json")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PackageError("report.json is not valid JSON")
    if not isinstance(report, dict) or not isinstance(report.get("steps"), list):
        raise PackageError("report.json is missing its steps")
    tvid = (report.get("template") or {}).get("template_version_id")
    if not isinstance(tvid, int):
        raise PackageError("report.json does not name the template it ran — re-export the bundle and run it again")
    if report.get("status") == "dry-run":
        raise PackageError("this is a dry run's report; nothing ran")
    return report, zf


def template_version_id(report: dict) -> int:
    return report["template"]["template_version_id"]


def find_existing(runs_query, report: dict) -> PipelineRun | None:
    """The run this same package was already imported as, if any.

    Keyed on the runner's run id and start time within the template version, so
    re-uploading a package is harmless while two runs sharing a --run-id label
    still import separately.
    """
    key = (str(report.get("run_id")), str(report.get("started_at")))
    for run in runs_query.filter(PipelineRun.template_version_id == template_version_id(report)).all():
        fc = run.frozen_config or {}
        if fc.get("source") == SOURCE and (str(fc.get("local_run_id")), str(fc.get("local_started_at"))) == key:
            return run
    return None


# --- creating the run ----------------------------------------------------------

def _ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _safe_member(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p


def create_run(db: Session, user, template, report: dict, zf: zipfile.ZipFile) -> PipelineRun:
    nodes = {str(n.node_id): n for n in db.query(WfNode).filter(
        WfNode.template_version_id == template.template_version_id).all()}
    steps = {str(s.get("node_id")): s for s in report["steps"]}
    unknown = sorted(set(steps) - set(nodes))
    if unknown:
        raise PackageError(
            f"steps {', '.join(unknown)} are not in version {template.version} of '{template.name}' — "
            "was the bundle exported from a different version?"
        )

    status = str(report.get("status") or "").lower()
    run = PipelineRun(
        template_version_id=template.template_version_id,
        user_id=user.user_id,
        name=template.name,
        status={"completed": "COMPLETED", "failed": "FAILED"}.get(status, "FAILED"),
        started_at=_ts(report.get("started_at")),
        completed_at=_ts(report.get("finished_at")),
        frozen_config={
            "source": SOURCE,
            "name": template.name,
            "local_run_id": report.get("run_id"),
            "local_started_at": report.get("started_at"),
            "host": report.get("host"),
            "runner_version": report.get("runner_version"),
            "seconds": report.get("seconds"),
        },
    )
    db.add(run)
    db.flush()  # assign run.run_id, which the stored URIs embed

    try:
        _unpack(run.run_id, zf)
        system = system_for(run.run_id)

        # Each port's location as the PLATFORM will read it: the stored copy
        # when the runner packed something there, else the path on the node.
        outputs: dict[str, dict] = {}
        for nid, step in steps.items():
            ports = {}
            for port, location in (step.get("outputs") or {}).items():
                art = (step.get("artifacts") or {}).get(port) or {}
                if art.get("files"):
                    rel = f"/{nid}/{port}"
                    if art.get("kind") == "file":
                        rel += f"/{art['files'][0]}"
                    ports[port] = f"tapis://{system}{rel}"
                else:
                    ports[port] = location
            outputs[nid] = ports

        # A step's config as the run page shows it: its own design config plus
        # every wired input resolved to the upstream output (what
        # workflows._resolve_inputs records on the platform). This is what lets
        # a design-time panel such as Visualization open on the run's results.
        for nid, node in nodes.items():
            config = dict(node.default_config or {})
            for edge in report.get("edges") or []:
                if str(edge.get("to")) != nid or not edge.get("to_port"):
                    continue
                src = outputs.get(str(edge.get("from")), {})
                if edge.get("from_port") in src:
                    config[edge["to_port"]] = src[edge["from_port"]]
                elif len(src) == 1:
                    config[edge["to_port"]] = next(iter(src.values()))

            step = steps.get(nid)
            # Not in the report: the run stopped before reaching it.
            step_status = _STEP_STATUS.get(str((step or {}).get("status")), "pending") if step else "pending"
            row = RunStep(
                run_id=run.run_id,
                node_id=node.node_id,
                step_label=(step or {}).get("label") or node.node_label or node.step_type_key,
                status=step_status,
                config=config,
                outputs=outputs.get(nid, {}),
                error_message=(step or {}).get("error"),
                percent_complete=100 if step_status == "completed" else 0,
            )
            db.add(row)
            db.flush()
            for port, art in ((step or {}).get("artifacts") or {}).items():
                port_row = db.query(StepTypePort).filter(
                    StepTypePort.step_type_key == node.step_type_key,
                    StepTypePort.port_name == port,
                    StepTypePort.direction == "output",
                ).first()
                if not port_row or not art.get("files"):
                    continue
                db.add(RunArtifact(
                    run_step_id=row.run_step_id,
                    port_id=port_row.port_id,
                    artifact_type=port_row.data_type,
                    uri=outputs[nid][port],
                    tapis_system_id=system,
                    metadata_json={
                        "node_path": art.get("path"),
                        "files": art.get("files"),
                        "skipped": art.get("skipped"),
                        "skipped_total": art.get("skipped_total"),
                    },
                ))
        db.commit()
    except Exception:
        db.rollback()
        shutil.rmtree(run_dir(run.run_id), ignore_errors=True)
        raise
    return run


def _unpack(run_id: int, zf: zipfile.ZipFile) -> None:
    dest = run_dir(run_id)
    if dest.exists():
        # A run id the database reused after a rolled-back import.
        shutil.rmtree(dest)
    for info in zf.infolist():
        member = _safe_member(info.filename)
        if member is None or info.is_dir() or member.parts[0] not in ("logs", "artifacts"):
            continue
        target = dest.joinpath(*member.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_bytes(zf.read("report.json"))


# --- reading it back ---------------------------------------------------------

def step_log(run_id: int, node_id: int) -> str | None:
    path = run_dir(run_id) / "logs" / f"{node_id}.log"
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def list_dir(run_id: int, path: str) -> list[dict]:
    """A stored folder in the shape /api/tapis-files/list returns."""
    target = stored_path(run_id, path)
    if not target.is_dir():
        raise FileNotFoundError(path)
    base = stored_path(run_id, "/")
    return [
        {
            "name": child.name,
            "path": "/" + child.relative_to(base).as_posix(),
            "size": child.stat().st_size if child.is_file() else 0,
            "type": "dir" if child.is_dir() else "file",
        }
        for child in sorted(target.iterdir())
    ]
