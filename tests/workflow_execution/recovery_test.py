import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest
from dbos import DBOS


def start_server(log_file_path="server_test.log"):
    print(f"Starting server, writing logs to {log_file_path}...")
    # ruff: noqa: SIM115
    server_log = open(log_file_path, "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8000"],
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    return process, server_log


def stop_server(process, server_log):
    print("Stopping server...")
    if process:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("Server didn't exit cleanly. Killing process...")
            process.kill()
            process.wait()
    if server_log:
        server_log.close()


def wait_for_server(port=8000, timeout=15):
    print("Waiting for server to start...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/docs", timeout=1) as r:
                if r.status == 200:
                    print("Server is ready!")
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def trigger_workflow(dag_config, port=8000):
    url = f"http://localhost:{port}/workflow/run"
    data = json.dumps(dag_config).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def get_workflow_status(workflow_id, port=8000):
    url = f"http://localhost:{port}/workflow/{workflow_id}"
    try:
        with urllib.request.urlopen(url) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"Error checking status: {e}")
        return None


def test_crash_and_recovery_flow():
    """Test the resilience of DBOS workflows by simulating a server crash and verifying seamless recovery."""
    # Because conftest.py's reset_dbos starts DBOS in the pytest process,
    # we should shut it down so it doesn't accidentally execute the workflow in the background.
    DBOS.destroy()

    with open("tests/fixtures/valid_dag.json") as f:
        TEST_DAG = json.load(f)

    if os.path.exists("tapis_jobs.json"):
        os.remove("tapis_jobs.json")

    proc, log = start_server("server_test2.log")
    try:
        if not wait_for_server():
            pytest.fail("Server failed to start")

        print("Triggering workflow...")
        run_res = trigger_workflow(TEST_DAG)
        workflow_id = run_res["workflow_id"]
        print(f"Workflow started with workflow_id: {workflow_id}")

        time.sleep(1.5)
        status = get_workflow_status(workflow_id)
        if status and "progress_graph" in status:
            print("\nIn-Progress Graph before crash:")
            print(status["progress_graph"])

        print("Simulating server crash (killing process)...")
        proc.kill()
        proc.wait()
        log.close()
        proc, log = None, None
        print("Server crashed.")

        time.sleep(2)

        print("Restarting server...")
        proc, log = start_server("server_test2_recovered.log")
        if not wait_for_server():
            pytest.fail("Server failed to restart")

        print("Checking status of workflow after restart...")
        finished = False
        for _ in range(40):
            status = get_workflow_status(workflow_id)
            if status and status["workflow_state"] == "SUCCESS":
                print("Finished successfully after recovery.")
                finished = True
                break
            elif status and status["workflow_state"] == "ERROR":
                pytest.fail(f"Workflow failed after recovery: {status}")
            time.sleep(1)

        assert finished, "Workflow did not recover and finish in time"
        assert status["database_record"]["run_status"] == "COMPLETED"
    finally:
        if proc:
            stop_server(proc, log)
        # Restart DBOS for subsequent tests running in the same pytest session
        from app.main import app, config

        try:
            DBOS(fastapi=app, config=config)
        except RuntimeError:
            DBOS(config=config)
        DBOS.launch()
