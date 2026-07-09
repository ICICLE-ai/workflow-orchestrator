"""DBOS step wrappers for Tapis job submission.

Submits real Tapis V3 jobs when a valid token is configured (see tapis_auth);
otherwise falls back to the mock client so the system stays runnable without
credentials. Both submit and status are @DBOS.step() so DBOS records their
results durably and won't re-run them on workflow recovery.

The real contract mirrors the legacy harvest-webservers code:
  POST {base}/v3/jobs/submit        with the full job spec dict
  GET  {base}/v3/jobs/{uuid}/status -> result.status
"""
import os

import httpx
from dbos import DBOS

from engine import tapis_auth
from engine.mock_tapis import tapis_client as _mock_client


def _use_real() -> bool:
    """Real Tapis only when not forced to mock AND a usable token exists."""
    if os.getenv("TAPIS_USE_MOCK", "false").lower() == "true":
        return False
    return tapis_auth.get_token() is not None


class TapisV3:
    """Static DBOS step interface for Tapis v3 (real or mock)."""

    @staticmethod
    @DBOS.step()
    def submit_job(job_spec: dict) -> str:
        """Submit a fully-rendered Tapis job spec; return the job UUID.

        job_spec is the step's `tapis_job` template with all ${...} placeholders
        already substituted (see engine.job_spec). In mock mode the spec's appId
        is used to drive the simulated job.
        """
        if _use_real():
            token = tapis_auth.get_token()
            url = f"{tapis_auth.TAPIS_BASE_URL}/v3/jobs/submit"
            headers = {"X-Tapis-Token": token, "Content-Type": "application/json"}
            print(f"[TapisV3] Submitting REAL job '{job_spec.get('name')}' appId={job_spec.get('appId')}")
            resp = httpx.post(url, json=job_spec, headers=headers, timeout=60)
            resp.raise_for_status()
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
    def check_job_status(job_uuid: str) -> str:
        """Return the current status string for a job (real or mock).

        Tapis statuses are normalized to the engine's vocabulary: a terminal
        'FINISHED' for success, 'FAILED'/'CANCELLED' for failure, otherwise an
        in-progress status string.
        """
        if _use_real():
            url = f"{tapis_auth.TAPIS_BASE_URL}/v3/jobs/{job_uuid}/status"
            resp = httpx.get(url, headers={"X-Tapis-Token": tapis_auth.get_token()}, timeout=60)
            # A 401 mid-run usually means the token expired during a long job.
            # Force a token refresh and retry once before surfacing an error, so
            # a transient auth lapse doesn't get mistaken for a job failure.
            if resp.status_code == 401:
                fresh = tapis_auth.get_token(force_refresh=True)
                if fresh:
                    resp = httpx.get(url, headers={"X-Tapis-Token": fresh}, timeout=60)
            resp.raise_for_status()
            return resp.json()["result"]["status"]

        return _mock_client.jobs.getJobStatus(jobUuid=job_uuid).status


def cancel_tapis_job(job_uuid: str) -> bool:
    """Best-effort cancel of a running Tapis job. Not a DBOS step — called from
    the HTTP layer (outside a workflow) to stop a run. Returns True if the cancel
    request was accepted, False otherwise (already terminal, gone, or no creds)."""
    if not job_uuid:
        return False
    if not _use_real():
        return True  # mock jobs have nothing to cancel remotely
    try:
        token = tapis_auth.get_token()
        url = f"{tapis_auth.TAPIS_BASE_URL}/v3/jobs/{job_uuid}/cancel"
        resp = httpx.post(url, headers={"X-Tapis-Token": token}, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        print(f"[tapis] cancel_tapis_job {job_uuid} failed: {type(e).__name__}")
        return False
