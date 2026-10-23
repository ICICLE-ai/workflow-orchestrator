"""Pre-launch verification for a LIVE (Tapis) run — the counterpart of the
local bundle's "Check workflow".

The local check can only inspect the plan; a Tapis run fails on things only
Tapis knows. Every check here exists because a real launch failed on it, after
the run had already been created and partly executed:

  * an archive location the user can't create directories in
    (FILES_CLIENT_SSH_PERM_DENIED at mkdir — a shared folder owned by someone
    else, or a typo'd charge account pointing at a scratch dir that isn't
    theirs);
  * a Tapis app that isn't registered (TAPIS_APPCLIENT_NOT_FOUND);
  * a queue the exec system doesn't offer, or a request over its limits;
  * a source path that doesn't exist.

It resolves everything with the SAME helpers a launch uses
(resolve_node_exec_target, resolve_archive_base, single_job.plan), so the
answer is about what the launch will actually do, not a re-derivation.

Side effects: one — the archive base directory (<work_dir>/wf_runs, or the
configured archive dir) is created if missing. That is the first directory any
run creates there anyway, and actually creating it is the only reliable test
of "can I write here": Tapis' listing shows a folder's owner uid and mode, but
not whether the caller is that uid or in its group.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy.orm import Session

from engine import inline_steps, tapis_auth
from engine.tapis import split_tapis_uri, use_real_tapis
from engine.transactions import resolve_archive_base, resolve_node_exec_target
from models import StepTypeRegistry, WfNode, WorkflowTemplate

TIMEOUT = 20

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"

# Per-job resource keys and the batchLogicalQueue limit each is checked against.
_LIMITS = (
    ("nodeCount", "maxNodeCount", "nodes"),
    ("coresPerNode", "maxCoresPerNode", "cores per node"),
    ("memoryMB", "maxMemoryMB", "MB memory"),
    ("maxMinutes", "maxMinutes", "minutes"),
)


def _check(key: str, title: str, status: str, detail: str = "", group: str = "") -> dict:
    return {"key": key, "title": title, "status": status, "detail": detail, "group": group}


def _tapis_error(resp: httpx.Response) -> str:
    try:
        msg = (resp.json() or {}).get("message") or ""
    except Exception:
        msg = resp.text or ""
    return f"HTTP {resp.status_code}" + (f": {msg[:300]}" if msg else "")


class _Tapis:
    def __init__(self, token: str):
        self.base = tapis_auth.TAPIS_BASE_URL
        self.headers = {"X-Tapis-Token": token}

    def get(self, path: str, **params) -> httpx.Response:
        return httpx.get(f"{self.base}{path}", headers=self.headers, params=params or None, timeout=TIMEOUT)

    def mkdir(self, system: str, path: str) -> httpx.Response:
        return httpx.post(
            f"{self.base}/v3/files/ops/{system}",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"path": path},
            timeout=TIMEOUT,
        )


def _job_steps(db: Session, template: WorkflowTemplate) -> list[dict]:
    """The nodes that will submit a Tapis job, with what each will request."""
    nodes = db.query(WfNode).filter(WfNode.template_version_id == template.template_version_id).all()
    registry = {
        r.step_type_key: r
        for r in db.query(StepTypeRegistry).filter(
            StepTypeRegistry.step_type_key.in_({n.step_type_key for n in nodes})
        )
    }
    steps = []
    for n in nodes:
        reg = registry.get(n.step_type_key)
        if reg is None or not reg.tapis_job or reg.submits_job is False:
            continue
        if inline_steps.get_handler(n.step_type_key) is not None:
            continue  # executes in-process, no Tapis job
        cfg = n.default_config or {}
        tpl = reg.tapis_job or {}
        request = {}
        for key, _, _ in _LIMITS:
            value = cfg.get(key)
            if value in (None, ""):
                value = tpl.get(key)
            try:
                request[key] = int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                request[key] = None
        steps.append({
            "node_id": n.node_id,
            "label": n.node_label or reg.display_name or n.step_type_key,
            "config": cfg,
            "resources": reg.resources or {},
            "app": (tpl.get("appId") or reg.tapis_app_id, tpl.get("appVersion")),
            "request": request,
        })
    return steps


def _source_locations(db: Session, template: WorkflowTemplate, category: str = "source") -> list[dict]:
    """Source nodes' configured data locations (the only inputs that exist
    before the run — everything else is produced by an upstream step). With
    category="sink", sink nodes' destinations instead."""
    nodes = db.query(WfNode).filter(WfNode.template_version_id == template.template_version_id).all()
    cats = {
        r.step_type_key: r.category
        for r in db.query(StepTypeRegistry).filter(
            StepTypeRegistry.step_type_key.in_({n.step_type_key for n in nodes})
        )
    }
    out = []
    for n in nodes:
        if cats.get(n.step_type_key) != category:
            continue
        cfg = n.default_config or {}
        path = str(cfg.get("path") or "")
        system = str(cfg.get("system") or "")
        if "://" in path:
            system, path = split_tapis_uri(path)
        out.append({"label": n.node_label or n.step_type_key, "system": system or "", "path": path})
    return out


def run(db: Session, template: WorkflowTemplate, cfg: dict, token: str | None) -> dict:
    """Run every check for launching `template` with run config `cfg` (the
    RunOptions plus the defaults _launch_run applies). Returns
    {ok, checks: [...], archive: {system, path}}."""
    checks: list[dict] = []

    if not use_real_tapis():
        checks.append(_check("tapis", "Tapis connection", SKIP,
                             "Mock mode — nothing to verify against.", "Account"))
        return {"ok": True, "checks": checks, "archive": None}
    if not token:
        checks.append(_check("tapis", "Tapis login", FAIL,
                             "No valid Tapis session — log in again.", "Account"))
        return {"ok": False, "checks": checks, "archive": None}

    tapis = _Tapis(token)
    single_job = (cfg.get("execution_mode") or "per_step") == "single_job"
    steps = _job_steps(db, template)

    account = str(cfg.get("slurm_account") or "")
    checks.append(_check(
        "account", "Charge account", PASS if account else WARN,
        account or "No charge account set — the system's default allocation is used, if it has one.",
        "Account",
    ))

    # --- Where each job runs, and what it asks for -------------------------
    # (system, queue) -> [(label, request)] in per-step mode; one envelope in
    # single-job mode, exactly as the launch would submit it.
    targets: dict[tuple, list] = {}
    apps: set[tuple] = set()
    if single_job:
        from engine import single_job as planner

        plan = planner.plan(db, template, cfg)
        for b in plan["blockers"]:
            checks.append(_check("single_job", "Single-job mode", FAIL, b, "Plan"))
        if not plan["blockers"]:
            checks.append(_check("single_job", "Single-job mode", PASS,
                                 f"{len(plan['steps'])} steps in one job.", "Plan"))
        r = plan["resources"]
        exec_ = plan["exec"]
        targets[(exec_["exec_system"], exec_["exec_queue"])] = [
            ("the merged job", {k: r.get(k) for k, _, _ in _LIMITS})
        ]
        apps.add((plan["app"]["id"], plan["app"]["version"]))
    else:
        if not steps:
            checks.append(_check("steps", "Steps", WARN, "No step in this workflow submits a Tapis job.", "Plan"))
        for s in steps:
            system, queue = resolve_node_exec_target(cfg, s["config"], s["resources"])
            targets.setdefault((system, queue), []).append((s["label"], s["request"]))
            if s["app"][0]:
                apps.add(s["app"])

    archive_system, _, ws_base = resolve_archive_base(cfg, cfg.get("exec_system") or "")

    # --- The Tapis lookups, in parallel -------------------------------------
    systems = sorted({sys for sys, _ in targets})
    sources = _source_locations(db, template)

    def fetch_system(sys_id):
        try:
            return sys_id, tapis.get(f"/v3/systems/{sys_id}")
        except Exception as e:
            return sys_id, e

    def fetch_app(app):
        app_id, version = app
        try:
            path = f"/v3/apps/{app_id}/{version}" if version else f"/v3/apps/{app_id}"
            return app, tapis.get(path)
        except Exception as e:
            return app, e

    def fetch_source(src):
        if not src["path"] or not src["system"]:
            return src, None
        try:
            return src, tapis.get(f"/v3/files/ops/{src['system']}/{src['path'].lstrip('/')}", limit=1)
        except Exception as e:
            return src, e

    def make_archive():
        try:
            return tapis.mkdir(archive_system, ws_base)
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=8) as pool:
        sys_futs = [pool.submit(fetch_system, s) for s in systems]
        app_futs = [pool.submit(fetch_app, a) for a in sorted(apps, key=str)]
        src_futs = [pool.submit(fetch_source, s) for s in sources]
        arc_fut = pool.submit(make_archive) if archive_system else None
        sys_results = dict(f.result() for f in sys_futs)
        app_results = [f.result() for f in app_futs]
        src_results = [f.result() for f in src_futs]
        arc_result = arc_fut.result() if arc_fut else None

    # Login: any authenticated call coming back 401 means the token is dead.
    all_responses = [r for r in sys_results.values() if isinstance(r, httpx.Response)]
    all_responses += [r for _, r in app_results if isinstance(r, httpx.Response)]
    if any(r.status_code == 401 for r in all_responses):
        checks.insert(0, _check("tapis", "Tapis login", FAIL,
                                "Tapis rejected your session (expired) — log in again.", "Account"))
        return {"ok": False, "checks": checks, "archive": None}
    checks.insert(0, _check("tapis", "Tapis login", PASS, "Session accepted by Tapis.", "Account"))

    # --- Systems, queues and limits ------------------------------------------
    for (sys_id, queue), jobs in sorted(targets.items(), key=str):
        title = f"{sys_id}" + (f" / {queue}" if queue else "")
        resp = sys_results.get(sys_id)
        if isinstance(resp, Exception):
            checks.append(_check(f"sys:{sys_id}", title, FAIL, f"Could not reach Tapis ({type(resp).__name__}).", "Compute"))
            continue
        if resp.status_code != 200:
            reason = "doesn't exist or you don't have access" if resp.status_code in (403, 404) else "lookup failed"
            checks.append(_check(f"sys:{sys_id}", title, FAIL, f"System {reason} — {_tapis_error(resp)}", "Compute"))
            continue
        system = resp.json().get("result") or {}
        queues = {q.get("name"): q for q in system.get("batchLogicalQueues") or []}
        name = queue or system.get("batchDefaultLogicalQueueName") or ""
        q = queues.get(name)
        if queues and q is None:
            checks.append(_check(f"queue:{sys_id}:{queue}", title, FAIL,
                                 f"Queue '{name}' isn't offered by {sys_id}. Available: {', '.join(sorted(queues))}.",
                                 "Compute"))
            continue
        over = []
        for label, req in jobs:
            for key, limit_key, unit in _LIMITS:
                want, limit = req.get(key), (q or {}).get(limit_key)
                if want is not None and limit not in (None, -1) and want > limit:
                    over.append(f"{label}: {want} {unit} (queue max {limit})")
        who = ", ".join(sorted({label for label, _ in jobs}))
        if over:
            checks.append(_check(f"limits:{sys_id}:{name}", title, FAIL,
                                 "Over the queue's limits — " + "; ".join(over), "Compute"))
        else:
            checks.append(_check(f"limits:{sys_id}:{name}", title, PASS,
                                 f"Queue '{name}' fits {who}.", "Compute"))

    # --- Apps ------------------------------------------------------------------
    for (app_id, version), resp in app_results:
        title = f"App {app_id}" + (f" {version}" if version else "")
        if isinstance(resp, Exception):
            checks.append(_check(f"app:{app_id}", title, FAIL, f"Could not reach Tapis ({type(resp).__name__}).", "Apps"))
        elif resp.status_code == 200:
            checks.append(_check(f"app:{app_id}", title, PASS, "Registered and visible to you.", "Apps"))
        else:
            reason = "not registered on Tapis" if resp.status_code == 404 else "not accessible"
            checks.append(_check(f"app:{app_id}", title, FAIL, f"App {reason} — {_tapis_error(resp)}", "Apps"))

    # --- Inputs ----------------------------------------------------------------
    for src, resp in src_results:
        title = f"Input: {src['label']}"
        if not src["path"]:
            checks.append(_check(f"src:{src['label']}", title, FAIL, "No path configured.", "Data"))
        elif not src["system"]:
            checks.append(_check(f"src:{src['label']}", title, WARN,
                                 f"{src['path']} has no Tapis system, so it can't be checked.", "Data"))
        elif isinstance(resp, Exception):
            checks.append(_check(f"src:{src['label']}", title, WARN, f"Could not reach Tapis ({type(resp).__name__}).", "Data"))
        elif resp.status_code == 200:
            checks.append(_check(f"src:{src['label']}", title, PASS, f"{src['system']}:{src['path']}", "Data"))
        else:
            reason = "not found" if resp.status_code == 404 else "not readable by you"
            checks.append(_check(f"src:{src['label']}", title, FAIL,
                                 f"{src['system']}:{src['path']} is {reason} — {_tapis_error(resp)}", "Data"))

    # --- Sink destinations -----------------------------------------------------
    # Checked from the settings alone: the copy creates missing folders, so the
    # failures worth catching before launch are a missing path or system.
    for sink in _source_locations(db, template, category="sink"):
        title = f"Output: {sink['label']}"
        if not sink["path"]:
            checks.append(_check(f"sink:{sink['label']}", title, FAIL,
                                 "No destination set: open the sink and choose a system and path.", "Data"))
        elif not sink["system"]:
            checks.append(_check(f"sink:{sink['label']}", title, FAIL,
                                 f"{sink['path']} has no Tapis system: open the sink and pick one.", "Data"))
        elif sink["system"] == "node-local":
            checks.append(_check(f"sink:{sink['label']}", title, FAIL,
                                 "Writes to 'My node', which a platform run can't reach: pick a Tapis system.", "Data"))
        else:
            checks.append(_check(f"sink:{sink['label']}", title, PASS, f"{sink['system']}:{sink['path']}", "Data"))

    # --- Archive location ------------------------------------------------------
    arc_title = "Output folder"
    if not archive_system:
        checks.append(_check("archive", arc_title, FAIL, "No archive system set.", "Data"))
    elif isinstance(arc_result, Exception):
        checks.append(_check("archive", arc_title, WARN,
                             f"Could not reach Tapis ({type(arc_result).__name__}).", "Data"))
    elif arc_result.status_code in (200, 201):
        checks.append(_check("archive", arc_title, PASS,
                             f"Writable: {archive_system}:{ws_base}", "Data"))
    else:
        hint = ""
        if arc_result.status_code == 403:
            hint = (" You can't create folders there — set Archive dir (Storage & archive) to a "
                    "folder you own, or check the charge account is spelled right.")
        checks.append(_check("archive", arc_title, FAIL,
                             f"Can't create {archive_system}:{ws_base} — {_tapis_error(arc_result)}.{hint}", "Data"))

    return {
        "ok": not any(c["status"] == FAIL for c in checks),
        "checks": checks,
        "archive": {"system": archive_system, "path": ws_base},
    }
