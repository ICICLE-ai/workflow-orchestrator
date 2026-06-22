from dbos import DBOS

from app.mock.mock_tapis import tapis_client


class TapisV3:
    """
    A static utility class that provides DBOS step interfaces for interacting with the Tapis v3 service.
    Wraps static operations to submit and check the status of jobs using the mock Tapis client.
    """

    @staticmethod
    @DBOS.step()
    async def submit_job(app_id: str, app_version: str, name: str, args: list) -> str:
        """
        Submits a new job to the Tapis service.

        Inputs:
            app_id (str): The Tapis application ID to run.
            app_version (str): The version of the application.
            name (str): The name to assign to the submitted job.
            args (list): The list of argument values to pass to the job.

        Outputs:
            str: The UUID of the submitted Tapis job.
        """
        print(f"[TapisV3] Submitting job {name} for App {app_id}...")
        response = await tapis_client.jobs.submitJob(
            name=name, appId=app_id, appVersion=app_version, parameterSet={"args": args}
        )
        return response.uuid

    @staticmethod
    @DBOS.step()
    async def check_job_status(job_uuid: str) -> str:
        """
        Retrieves the current status of a submitted Tapis job.

        Inputs:
            job_uuid (str): The UUID of the job whose status is being queried.

        Outputs:
            str: The status string of the job (e.g., 'PENDING', 'RUNNING', 'FINISHED', 'FAILED').
        """
        status_resp = await tapis_client.jobs.getJobStatus(jobUuid=job_uuid)
        return status_resp.status
