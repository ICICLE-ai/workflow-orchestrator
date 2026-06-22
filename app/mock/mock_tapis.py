import random
import threading

class MockTapisJobResponse:
    """
    Represents a simulated response returned after submitting a Tapis job.

    Attributes:
        uuid (str): The unique identifier assigned to the simulated job.
    """
    def __init__(self, uuid: str):
        """
        Initializes a new instance of MockTapisJobResponse.

        Inputs:
            uuid (str): The unique identifier of the job.

        Outputs:
            None.

        What it does and how:
            Stores the provided job UUID into the instance attribute `self.uuid`.
        """
        self.uuid = uuid

class MockTapisStatusResponse:
    """
    Represents a simulated response containing the status of a Tapis job.

    Attributes:
        status (str): The current status of the job (e.g., 'PENDING', 'RUNNING', 'FINISHED', 'FAILED').
    """
    def __init__(self, status: str):
        """
        Initializes a new instance of MockTapisStatusResponse.

        Inputs:
            status (str): The status string of the job.

        Outputs:
            None.

        What it does and how:
            Stores the provided job status into the instance attribute `self.status`.
        """
        self.status = status

import os
import json

class MockTapisJobs:
    """
    Simulates a Tapis Jobs service by managing job states locally in a JSON file.

    Attributes:
        file_path (str): The path to the local JSON file storing job states.
        job_states (dict): A dictionary mapping job UUIDs to their state/progress.
        lock (threading.Lock): A reentrant or standard lock to ensure thread-safe operations on job states.
    """
    def __init__(self):
        """
        Initializes a new instance of MockTapisJobs.

        Inputs:
            None.

        Outputs:
            None.

        What it does and how:
            Sets up the local state file path, initializes the thread lock,
            and calls the private load method `_load` to retrieve existing jobs.
        """
        self.file_path = "tapis_jobs.json"
        self.job_states = {}
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        """
        Loads the job states dictionary from the local JSON file.

        Inputs:
            None.

        Outputs:
            None.

        What it does and how:
            Checks if the status file exists on the local disk. If it does,
            reads its contents and deserializes the JSON object into `self.job_states`.
            Catches any exception and defaults to an empty dictionary on failure.
        """
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r") as f:
                    self.job_states = json.load(f)
            except Exception:
                self.job_states = {}

    def _save(self):
        """
        Saves the current job states dictionary to the local JSON file.

        Inputs:
            None.

        Outputs:
            None.

        What it does and how:
            Opens the status file for writing and serializes the `self.job_states`
            dictionary into it as JSON formatted text. Catches and suppresses any exceptions.
        """
        try:
            with open(self.file_path, "w") as f:
                json.dump(self.job_states, f)
        except Exception:
            pass

    def submitJob(self, name: str, appId: str, appVersion: str, parameterSet: dict) -> MockTapisJobResponse:
        """
        Submits a new simulated job to the Tapis Jobs service.

        Inputs:
            name (str): The name of the job to submit.
            appId (str): The Tapis application ID to run.
            appVersion (str): The version of the application.
            parameterSet (dict): Configuration options and parameters for the job.

        Outputs:
            MockTapisJobResponse: A response object containing the generated job UUID.

        What it does and how:
            Acquires a thread lock, loads the current job states from disk, generates a new
            random job UUID (e.g. tapis-job-<random_int>), initializes its state as
            PENDING with 0 ticks, saves the updated states to disk, prints a confirmation
            message to stdout, and returns a response containing the new job UUID.
        """
        with self.lock:
            self._load()
            job_uuid = f"tapis-job-{random.randint(1000, 9999)}"
            self.job_states[job_uuid] = {"status": "PENDING", "ticks": 0}
            self._save()
            print(f"[Mock Tapis] Job {name} submitted. UUID: {job_uuid}")
            return MockTapisJobResponse(job_uuid)

    def getJobStatus(self, jobUuid: str) -> MockTapisStatusResponse:
        """
        Retrieves and updates the current status of a simulated job.

        Inputs:
            jobUuid (str): The UUID of the job whose status is being queried.

        Outputs:
            MockTapisStatusResponse: A response object containing the current job status.

        What it does and how:
            Acquires a thread lock, loads the current job states, and checks if the UUID exists.
            If the UUID is not found, returns a status response of FAILED.
            Otherwise, increments the job's progress tick count. Transitions status based on ticks:
            if ticks >= 3, status becomes FINISHED; if ticks >= 1, status becomes RUNNING;
            otherwise status remains PENDING. Saves updated states to disk, prints the status
            to stdout, and returns a MockTapisStatusResponse containing the updated status.
        """
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
    """
    A simulated top-level Tapis v3 client that manages authentication tokens and jobs.

    Attributes:
        jobs (MockTapisJobs): The simulated jobs service instance.
    """
    def __init__(self, base_url, username, password):
        """
        Initializes a new instance of MockTapis.

        Inputs:
            base_url (str): The base URL of the simulated Tapis server.
            username (str): The username for simulated authentication.
            password (str): The password for simulated authentication.

        Outputs:
            None.

        What it does and how:
            Initializes the jobs attribute with an instance of `MockTapisJobs`.
            The base URL, username, and password parameters are accepted for API compatibility but not stored.
        """
        self.jobs = MockTapisJobs()

    def get_tokens(self):
        """
        Simulates retrieving authentication tokens from the Tapis service.

        Inputs:
            None.

        Outputs:
            None.

        What it does and how:
            Placeholder method for retrieving authentication tokens. Does nothing.
        """
        pass

tapis_client = MockTapis(
    base_url="https://tacc.tapis.io",
    username="mock_user",
    password="mock_password"
)
