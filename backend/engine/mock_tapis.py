"""Mock Tapis V3 client — simulates job submit/poll for local development.

Copied verbatim from the dbos-example branch. A real deployment would replace
`tapis_client` with an authenticated tapipy client; the engine only depends on
the submitJob / getJobStatus surface, so swapping is a one-line change in
tapis.py. Job state is persisted to a small JSON file so polls advance a job
from PENDING -> RUNNING -> FINISHED over a few ticks.
"""
import os
import json
import random
import threading


class MockTapisJobResponse:
    """Simulated response returned after submitting a Tapis job."""
    def __init__(self, uuid: str):
        self.uuid = uuid


class MockTapisStatusResponse:
    """Simulated response containing the status of a Tapis job."""
    def __init__(self, status: str):
        self.status = status


class MockTapisJobs:
    """Simulates a Tapis Jobs service by managing job states in a local JSON file."""
    def __init__(self):
        self.file_path = "tapis_jobs.json"
        self.job_states = {}
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r") as f:
                    self.job_states = json.load(f)
            except Exception:
                self.job_states = {}

    def _save(self):
        try:
            with open(self.file_path, "w") as f:
                json.dump(self.job_states, f)
        except Exception:
            pass

    def submitJob(self, name: str, appId: str, appVersion: str, parameterSet: dict) -> MockTapisJobResponse:
        with self.lock:
            self._load()
            job_uuid = f"tapis-job-{random.randint(1000, 9999)}"
            self.job_states[job_uuid] = {"status": "PENDING", "ticks": 0}
            self._save()
            print(f"[Mock Tapis] Job {name} submitted. UUID: {job_uuid}")
            return MockTapisJobResponse(job_uuid)

    def getJobStatus(self, jobUuid: str) -> MockTapisStatusResponse:
        with self.lock:
            self._load()
            if jobUuid not in self.job_states:
                return MockTapisStatusResponse("FAILED")

            job = self.job_states[jobUuid]
            job["ticks"] += 1

            # Transition: PENDING -> RUNNING -> FINISHED
            if job["ticks"] >= 3:
                job["status"] = "FINISHED"
            elif job["ticks"] >= 1:
                job["status"] = "RUNNING"

            self._save()
            print(f"[Mock Tapis] Polling Job {jobUuid}: Status is {job['status']}")
            return MockTapisStatusResponse(job["status"])


class MockTapis:
    """A simulated top-level Tapis v3 client."""
    def __init__(self, base_url, username, password):
        self.jobs = MockTapisJobs()

    def get_tokens(self):
        pass


tapis_client = MockTapis(
    base_url="https://tacc.tapis.io",
    username="mock_user",
    password="mock_password",
)
