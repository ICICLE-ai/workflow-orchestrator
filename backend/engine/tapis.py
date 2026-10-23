"""DBOS step wrappers for Tapis job submission.

Submits real Tapis V3 jobs when a valid token is configured (see tapis_auth);
otherwise falls back to the mock client so the system stays runnable without
credentials. Both submit and status are @DBOS.step() so DBOS records their
results durably and won't re-run them on workflow recovery.

The real contract mirrors the legacy harvest-webservers code:
  POST {base}/v3/jobs/submit        with the full job spec dict
  GET  {base}/v3/jobs/{uuid}/status -> result.status
"""
import fnmatch
import json
import os
import time

import httpx
from dbos import DBOS

from engine import tapis_auth
from engine.mock_tapis import tapis_client as _mock_client


def use_real_tapis() -> bool:
    """Real Tapis only when not forced to mock AND we have a way to authenticate:
    a direct env token (TAPIS_ACCESS_TOKEN) OR a confidential OAuth client.

    Whether a given *user* has a valid token is resolved per-run at call time (via
    tapis_auth.get_token_for_run); this only decides real-vs-mock at the client
    level so local dev without credentials keeps using the mock.
    """
    if os.getenv("TAPIS_USE_MOCK", "false").lower() == "true":
        return False
    return tapis_auth.env_token_mode() or tapis_auth.oauth_client_configured()


class TapisAuthError(RuntimeError):
    """Raised when a run's owner has no usable Tapis token, so a real job cannot
    be submitted. Surfaced to the user as a step failure asking them to re-login."""


def _redact(text: str, secret_values: list[str] | None) -> str:
    """Replace any occurrence of a resolved secret value with a placeholder,
    so a rendered job spec containing a "secret"-typed field (see
    engine.workflows._resolve_secrets) never reaches stdout/error text —
    including Tapis' own validation-error response, which can echo submitted
    fields back."""
    for value in secret_values or ():
        if value:
            text = text.replace(value, "***REDACTED***")
    return text


def _require_run_token(run_id: int) -> str:
    """Resolve the run owner's Tapis access token, or raise TapisAuthError."""
    token = tapis_auth.get_token_for_run(run_id)
    if not token:
        raise TapisAuthError(
            "Tapis authentication required — the run owner's session has expired. "
            "Please log in again and re-run."
        )
    return token


class TapisV3:
    """Static DBOS step interface for Tapis v3 (real or mock)."""

    @staticmethod
    @DBOS.step()
    def submit_job(job_spec: dict, run_id: int, redact: list[str] | None = None) -> str:
        """Submit a fully-rendered Tapis job spec as the run owner; return the UUID.

        job_spec is the step's `tapis_job` template with all ${...} placeholders
        already substituted (see engine.job_spec). run_id identifies the run whose
        owner's Tapis token authorizes the submission. In mock mode the spec's
        appId is used to drive the simulated job.

        redact: real values of any "secret"-typed config field this job spec had
        substituted in (see engine.workflows._resolve_secrets) — masked out of
        the debug logging below, which otherwise dumps the full rendered spec
        (and Tapis' own response, which can echo submitted fields back).
        """
        if use_real_tapis():
            token = _require_run_token(run_id)
            url = f"{tapis_auth.TAPIS_BASE_URL}/v3/jobs/submit"
            headers = {"X-Tapis-Token": token, "Content-Type": "application/json"}
            print(f"[TapisV3] Submitting REAL job '{job_spec.get('name')}' appId={job_spec.get('appId')}")
            resp = httpx.post(url, json=job_spec, headers=headers, timeout=60)
            if resp.status_code >= 400:
                # Tapis puts the validation reason in the body — surface it instead
                # of a bare "400". Also dump the rendered spec so unresolved ${...}
                # placeholders / bad fields are visible in the logs.
                resp_text = _redact(resp.text, redact)
                spec_text = _redact(json.dumps(job_spec, default=str), redact)
                print(f"[TapisV3] Tapis REJECTED job (HTTP {resp.status_code}): {resp_text[:1500]}")
                print(f"[TapisV3] Rendered job_spec was: {spec_text[:2000]}")
                raise RuntimeError(
                    f"Tapis job submit failed (HTTP {resp.status_code}): {resp_text[:600]}"
                )
            return resp.json()["result"]["uuid"]

        # Mock path
        print(f"[TapisV3] Submitting MOCK job appId={job_spec.get('appId')}")
        response = _mock_client.jobs.submitJob(
            name=job_spec.get("name", "job"),
            appId=job_spec.get("appId", "generic-pipeline"),
            appVersion=job_spec.get("appVersion", "1.0.0"),
            parameterSet=job_spec.get("parameterSet", {}),
        )
        return response.uuid

    @staticmethod
    @DBOS.step()
    def check_job_status(job_uuid: str, run_id: int) -> str:
        """Return the current status string for a job (real or mock).

        Tapis statuses are normalized to the engine's vocabulary: a terminal
        'FINISHED' for success, 'FAILED'/'CANCELLED' for failure, otherwise an
        in-progress status string. run_id resolves the run owner's token.
        """
        if use_real_tapis():
            url = f"{tapis_auth.TAPIS_BASE_URL}/v3/jobs/{job_uuid}/status"
            resp = httpx.get(url, headers={"X-Tapis-Token": _require_run_token(run_id)}, timeout=60)
            # A 401 mid-run usually means the access token expired during a long
            # job. get_token_for_run refreshes from the owner's refresh token, so
            # re-resolving and retrying once recovers a transient auth lapse before
            # it gets mistaken for a job failure.
            if resp.status_code == 401:
                fresh = tapis_auth.get_token_for_run(run_id)
                if fresh:
                    resp = httpx.get(url, headers={"X-Tapis-Token": fresh}, timeout=60)
            resp.raise_for_status()
            return resp.json()["result"]["status"]

        return _mock_client.jobs.getJobStatus(jobUuid=job_uuid).status


def cancel_tapis_job(job_uuid: str, run_id: int) -> bool:
    """Best-effort cancel of a running Tapis job, authorized as the run owner.
    Not a DBOS step — called from the HTTP layer (outside a workflow) to stop a
    run. Returns True if the cancel request was accepted, False otherwise (already
    terminal, gone, or no creds)."""
    if not job_uuid:
        return False
    if not use_real_tapis():
        return True  # mock jobs have nothing to cancel remotely
    try:
        token = tapis_auth.get_token_for_run(run_id)
        if not token:
            return False
        url = f"{tapis_auth.TAPIS_BASE_URL}/v3/jobs/{job_uuid}/cancel"
        resp = httpx.post(url, headers={"X-Tapis-Token": token}, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        print(f"[tapis] cancel_tapis_job {job_uuid} failed: {type(e).__name__}")
        return False


def split_tapis_uri(uri: str):
    """tapis://system/abs/path -> (system, /abs/path). Bare path -> (None, path)."""
    if uri.startswith("tapis://"):
        rest = uri[len("tapis://"):]
        sys_id, _, path = rest.partition("/")
        return sys_id, "/" + path
    return None, uri


def _clear_tapis_dir_contents(sys_id: str, dir_path: str, headers: dict) -> None:
    """Delete every child of a destination directory, leaving the dir itself.

    Used to make a sink destination reflect ONLY the current run's wired output:
    stale artifacts from prior runs (which may have used a different output
    layout) are removed before the fresh copy lands. We delete the dir's
    *contents* rather than the dir node so its existence/permissions/parent
    linkage survive. If the dir doesn't exist yet, listing 404s and we no-op.
    """
    base = tapis_auth.TAPIS_BASE_URL
    list_url = f"{base}/v3/files/ops/{sys_id}{dir_path}"
    try:
        r = httpx.get(list_url, headers=headers, timeout=60)
    except Exception as e:
        print(f"[tapis] clear-dir list error {dir_path}: {type(e).__name__}")
        return
    if r.status_code == 404:
        return  # nothing there yet — clean by definition
    if r.status_code != 200:
        print(f"[tapis] clear-dir list HTTP {r.status_code}: {r.text[:150]}")
        return
    for item in r.json().get("result", []):
        name = item.get("name")
        if not name or name in (".", ".."):
            continue
        child = f"{dir_path.rstrip('/')}/{name}"
        try:
            d = httpx.delete(f"{base}/v3/files/ops/{sys_id}{child}",
                             headers=headers, timeout=120)
            if d.status_code not in (200, 204):
                print(f"[tapis] clear-dir delete {name} HTTP {d.status_code}")
        except Exception as e:
            print(f"[tapis] clear-dir delete {name} error: {type(e).__name__}")


def _tapis_path_type(sys_id: str, path: str, headers: dict) -> str | None:
    """"file", "dir", or None when `path` doesn't exist (or can't be checked).

    Found by listing the PARENT and matching the name: listing a directory
    itself returns its children, which doesn't say what the path is.
    """
    path = path.rstrip("/") or "/"
    if path == "/":
        return "dir"
    parent, _, name = path.rpartition("/")
    try:
        r = httpx.get(f"{tapis_auth.TAPIS_BASE_URL}/v3/files/ops/{sys_id}{parent or '/'}",
                      headers=headers, timeout=60)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    for item in r.json().get("result", []):
        if item.get("name") == name:
            return "dir" if item.get("type") == "dir" else "file"
    return None


def _tapis_children(sys_id: str, dir_path: str, headers: dict) -> list[str]:
    r = httpx.get(f"{tapis_auth.TAPIS_BASE_URL}/v3/files/ops/{sys_id}{dir_path}", headers=headers, timeout=60)
    r.raise_for_status()
    return [i["name"] for i in r.json().get("result", []) if i.get("name") not in (None, ".", "..")]


def _tapis_mkdir(sys_id: str, dir_path: str, headers: dict) -> None:
    httpx.post(f"{tapis_auth.TAPIS_BASE_URL}/v3/files/ops/{sys_id}", json={"path": dir_path},
               headers=headers, timeout=60)  # idempotent


def _tapis_transfer(pairs: list[tuple[str, str]], headers: dict, timeout_s: int = 3600) -> str | None:
    """Copy (src_uri, dest_uri) pairs between systems with the Transfers
    service and wait for it to finish. None on success, else the reason."""
    base = tapis_auth.TAPIS_BASE_URL
    body = {"tag": "wf-sink", "elements": [{"sourceURI": s, "destinationURI": d} for s, d in pairs]}
    r = httpx.post(f"{base}/v3/files/transfers", json=body, headers=headers, timeout=60)
    if r.status_code not in (200, 201):
        return f"transfer request failed (HTTP {r.status_code}): {r.text[:200]}"
    uuid = (r.json().get("result") or {}).get("uuid")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        s = httpx.get(f"{base}/v3/files/transfers/{uuid}", headers=headers, timeout=60)
        status = ((s.json().get("result") or {}).get("status") or "") if s.status_code == 200 else ""
        if status == "COMPLETED":
            return None
        if status in ("FAILED", "CANCELLED", "FAILED_OPT", "PAUSED"):
            return f"transfer {uuid} ended {status}: {(s.json().get('result') or {}).get('errorMessage') or ''}"
    return f"transfer {uuid} did not finish within {timeout_s}s"


@DBOS.step()
def copy_tapis_path(src_uri: str, dest_uri: str, run_id: int, replace: bool = False) -> bool:
    """Copy a file or directory to a user-chosen Tapis location, as the run owner.

    Used by sink nodes to place a specific upstream artifact somewhere chosen.
    A DBOS step so the copy is recorded durably and not repeated on recovery.

    Where it lands:
      * a DIRECTORY source: its contents become the destination folder's
        contents (with ``replace``, whatever was there first is removed, so the
        folder reflects only this run);
      * a FILE source, destination an existing folder or ending in "/": copied
        into that folder under its own name, leaving the rest of it alone;
      * a FILE source otherwise: written at exactly the destination path.

    Each item is copied to an explicit, not-yet-existing target, so the result
    never depends on how COPY treats an existing destination folder. Same
    system: the Files COPY operation; across systems (e.g. pitzer-tapis to
    cardinal-tapis, even on a shared filesystem): the Transfers service.
    Returns True on success; the reason for a failure is printed.
    """
    if not use_real_tapis():
        print(f"[tapis] (mock) copy {src_uri} -> {dest_uri}{' (replace)' if replace else ''}")
        return True
    src_sys, src_path = split_tapis_uri(src_uri)
    dest_sys, dest_path = split_tapis_uri(dest_uri)
    if not src_sys or not dest_sys:
        print(f"[tapis] copy needs tapis://system/path on both sides, got {src_uri!r} -> {dest_uri!r}")
        return False
    token = _require_run_token(run_id)
    headers = {"X-Tapis-Token": token, "Content-Type": "application/json"}
    base = tapis_auth.TAPIS_BASE_URL
    try:
        src_type = _tapis_path_type(src_sys, src_path, headers)
        if src_type is None:
            print(f"[tapis] copy source not found: {src_uri}")
            return False
        dest_dir = dest_path.rstrip("/") or "/"
        dest_type = _tapis_path_type(dest_sys, dest_dir, headers)

        if src_type == "dir":
            _tapis_mkdir(dest_sys, dest_dir, headers)
            if replace:
                _clear_tapis_dir_contents(dest_sys, dest_dir, headers)
            pairs = [(f"{src_path.rstrip('/')}/{c}", f"{dest_dir.rstrip('/')}/{c}")
                     for c in _tapis_children(src_sys, src_path, headers)]
        else:
            if dest_type == "dir" or dest_path.endswith("/"):
                _tapis_mkdir(dest_sys, dest_dir, headers)
                target = f"{dest_dir.rstrip('/')}/{src_path.rstrip('/').rsplit('/', 1)[-1]}"
            else:
                _tapis_mkdir(dest_sys, dest_dir.rsplit("/", 1)[0] or "/", headers)
                target = dest_dir
            if replace and _tapis_path_type(dest_sys, target, headers) == "file":
                httpx.delete(f"{base}/v3/files/ops/{dest_sys}{target}", headers=headers, timeout=120)
            pairs = [(src_path, target)]

        if src_sys == dest_sys:
            for s_path, d_path in pairs:
                resp = httpx.put(f"{base}/v3/files/ops/{src_sys}{s_path}",
                                 json={"operation": "COPY", "newPath": d_path},
                                 headers=headers, timeout=600)
                if resp.status_code != 200:
                    print(f"[tapis] copy {s_path} -> {d_path} failed HTTP {resp.status_code}: {resp.text[:200]}")
                    return False
            return True
        problem = _tapis_transfer(
            [(f"tapis://{src_sys}{s}", f"tapis://{dest_sys}{d}") for s, d in pairs], headers)
        if problem:
            print(f"[tapis] {problem}")
            return False
        return True
    except Exception as e:
        print(f"[tapis] copy_tapis_path error: {type(e).__name__}: {e}")
        return False


@DBOS.step()
def resolve_latest_file(dir_uri: str, run_id: int, pattern: str) -> str | None:
    """Find the file inside `dir_uri` (a tapis://system/path directory) whose
    bare name matches `pattern` (fnmatch, e.g. 'annotations_*.json'), and
    return its full tapis://system/path URI.

    For an output port whose step.json declares a `file_glob` (see
    StepTypePort.file_glob / _derive_outputs in engine/workflows.py): some
    tools write their own dynamically (e.g. timestamp-)named file into a
    directory rather than a fixed filename, so the port's static output_path
    can only name that containing directory — this resolves the actual file
    within it once the job has actually run and the name is knowable.

    Ties are broken by name, descending — correct for zero-padded
    timestamp-suffixed names (e.g. ...YYYYMMDD_HHMMSS.json), where the latest
    file also sorts last; not a guarantee for arbitrary naming.

    Returns None if the directory can't be listed, is empty, or nothing
    matches — the caller falls back to the directory URI itself rather than
    treating this as fatal. In mock mode, returns None immediately (nothing
    was really written for real listing to find).
    """
    if not use_real_tapis():
        return None
    sys_id, path = split_tapis_uri(dir_uri)
    if not sys_id:
        return None
    token = _require_run_token(run_id)
    try:
        resp = httpx.get(
            f"{tapis_auth.TAPIS_BASE_URL}/v3/files/ops/{sys_id}{path}",
            headers={"X-Tapis-Token": token}, timeout=60,
        )
    except Exception as e:
        print(f"[tapis] resolve_latest_file list error {dir_uri}: {type(e).__name__}")
        return None
    if resp.status_code != 200:
        print(f"[tapis] resolve_latest_file list HTTP {resp.status_code}: {resp.text[:150]}")
        return None
    names = [
        item.get("name") for item in resp.json().get("result", [])
        if item.get("name") and item.get("type") != "dir" and fnmatch.fnmatch(item["name"], pattern)
    ]
    if not names:
        print(f"[tapis] resolve_latest_file: no file in {dir_uri} matched {pattern!r}")
        return None
    best = sorted(names)[-1]
    return f"tapis://{sys_id}{path.rstrip('/')}/{best}"
