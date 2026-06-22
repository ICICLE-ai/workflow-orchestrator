import random

class MockTapisJobResponse:
    def __init__(self, uuid: str):
        self.uuid = uuid

class MockTapisStatusResponse:
    def __init__(self, status: str):
        self.status = status

class MockTapisJobs:
    def __init__(self):
        self.job_states = {}

    def submitJob(self, name: str, appId: str, appVersion: str, parameterSet: dict) -> MockTapisJobResponse:
        job_uuid = f"tapis-job-{random.randint(1000, 9999)}"
        self.job_states[job_uuid] = {"status": "PENDING", "ticks": 0}
        print(f"[Mock Tapis] Job {name} submitted. UUID: {job_uuid}")
        return MockTapisJobResponse(job_uuid)

    def getJobStatus(self, jobUuid: str) -> MockTapisStatusResponse:
        if jobUuid not in self.job_states:
            return MockTapisStatusResponse("FAILED")
        
        job = self.job_states[jobUuid]
        job["ticks"] += 1
        
        # Transition: PENDING -> RUNNING -> FINISHED
        if job["ticks"] >= 3:
            job["status"] = "FINISHED"
        elif job["ticks"] >= 1:
            job["status"] = "RUNNING"
            
        print(f"[Mock Tapis] Polling Job {jobUuid}: Status is {job['status']}")
        return MockTapisStatusResponse(job["status"])

class MockTapis:
    def __init__(self, base_url, username, password):
        self.jobs = MockTapisJobs()
    def get_tokens(self):
        pass

tapis_client = MockTapis(
    base_url="https://tacc.tapis.io",
    username="mock_user",
    password="mock_password"
)
