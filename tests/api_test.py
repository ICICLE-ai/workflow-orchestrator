import json
import subprocess
import sys
import time
import urllib.request


def start_server(log_file_path="server_test.log"):
    """
    Start the main FastAPI/DBOS application server in a background subprocess.

    Inputs:
    - log_file_path (str, optional): Server log file path. Defaults to "server_test.log".

    Outputs:
    - process (subprocess.Popen): Spawned server process.
    - server_log (file): Opened log file.
    """
    print(f"Starting server, writing logs to {log_file_path}...")
    server_log = open(log_file_path, "w")  # noqa: SIM115
    print(sys.executable)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app"], stdout=server_log, stderr=subprocess.STDOUT
    )
    return process, server_log


def stop_server(process, server_log):
    """
    Stop the running server subprocess and close its log file.

    Inputs:
    - process (subprocess.Popen): Running server to be terminated.
    - server_log (file): Opened log file.
    """
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


def wait_for_server(timeout=15):
    """
    Poll the server until it is ready and responding to requests.

    Inputs:
    - timeout (int, optional): Max wait duration in seconds. Defaults to 15.

    Outputs:
    - success (bool): True if HTTP 200 within timeout; False otherwise.
    """
    print("Waiting for server to start...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen("http://localhost:8000/docs", timeout=1) as r:
                if r.status == 200:
                    print("Server is ready!")
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def trigger_workflow(dag_config):
    """
    Submit a DAG workflow run request to the FastAPI application.

    Inputs:
    - dag_config (dict): DAG configuration to run.

    Outputs:
    - response (dict): JSON response containing `workflow_id`.
    """
    url = "http://localhost:8000/workflow/run"
    data = json.dumps(dag_config).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def get_workflow_status(workflow_id):
    """
    Retrieve the execution status of a workflow run.

    Inputs:
    - workflow_id (str): DBOS workflow ID.

    Outputs:
    - status (dict | None): Status info and database records if successful, None otherwise.
    """
    url = f"http://localhost:8000/workflow/{workflow_id}"
    try:
        with urllib.request.urlopen(url) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"Error checking status: {e}")
        return None


TEST_DAG = {
    "dataset_url": "test_dataset_url",
    "nodes": [
        {"id": "preprocess_1", "type": "preprocess", "dataset_url": "url_1"},
        {"id": "preprocess_2", "type": "preprocess", "dataset_url": "url_2"},
        {"id": "train_1", "type": "train", "inputs": {"dataset_path": "preprocess_1.dataset_path"}},
        {
            "id": "train_2",
            "type": "train",
            "inputs": {
                "dataset_path": "preprocess_1.dataset_path",
                "dataset_path_2": "preprocess_2.dataset_path",
            },
        },
        {
            "id": "inference",
            "type": "inference",
            "inputs": {"model_path": "train_1.model_path", "model_path_2": "train_2.model_path"},
        },
    ],
    "edges": [
        {"from": "preprocess_1", "to": "train_1"},
        {"from": "preprocess_1", "to": "train_2"},
        {"from": "preprocess_2", "to": "train_2"},
        {"from": "train_1", "to": "inference"},
        {"from": "train_2", "to": "inference"},
    ],
}


def test_1_standard_flow():
    """Test the standard successful execution of the DBOS DAG workflow workflow."""
    print("\n--- Starting Test 1: Standard Flow ---")
    import os

    if os.path.exists("tapis_jobs.json"):
        os.remove("tapis_jobs.json")
    proc, log = start_server("server_test1.log")
    try:
        if not wait_for_server():
            raise RuntimeError("Server failed to start")

        print("Triggering workflow...")
        run_res = trigger_workflow(TEST_DAG)
        workflow_id = run_res["workflow_id"]
        print(f"Workflow started with workflow_id: {workflow_id}")

        # Poll until completed
        print("Polling status until completion...")
        finished = False
        for _ in range(40):
            status = get_workflow_status(workflow_id)
            if status and status["workflow_state"] == "SUCCESS":
                print(f"Finished successfully: {status}")
                finished = True
                break
            elif status and status["workflow_state"] == "ERROR":
                raise RuntimeError(f"Workflow failed: {status}")
            time.sleep(1)

        assert finished, "Workflow did not finish in time"
        db_rec = status["database_record"]
        assert db_rec["run_status"] == "COMPLETED"

        # Verify node outputs and connections
        steps_by_id = {s["node_id"]: s for s in db_rec["steps"]}
        assert steps_by_id["preprocess_1"]["status"] == "completed"
        assert steps_by_id["preprocess_2"]["status"] == "completed"
        assert steps_by_id["train_1"]["status"] == "completed"
        assert steps_by_id["train_2"]["status"] == "completed"
        assert steps_by_id["inference"]["status"] == "completed"

        # Verify progress_graph is present and non-empty
        assert "progress_graph" in status, "progress_graph missing from status response"
        assert status["progress_graph"], "progress_graph is empty"
        print("\nProgress Graph (JSON response):")
        print(status["progress_graph"])

        # Test format=text format response
        url_text = f"http://localhost:8000/workflow/{workflow_id}?format=text"
        with urllib.request.urlopen(url_text) as r:
            text_graph = r.read().decode("utf-8")
            assert text_graph, "text graph is empty"
            print("\nProgress Graph (text format response):")
            print(text_graph)

        print("Test 1 Passed!")
    finally:
        stop_server(proc, log)


def test_2_crash_and_recovery_flow():
    """Test the server crash, recovery, and workflow resumption features of the DBOS workflow."""
    print("\n--- Starting Test 2: Crash and Recovery Flow ---")
    import os

    if os.path.exists("tapis_jobs.json"):
        os.remove("tapis_jobs.json")
    proc, log = start_server("server_test2.log")
    try:
        if not wait_for_server():
            raise RuntimeError("Server failed to start")

        print("Triggering workflow...")
        run_res = trigger_workflow(TEST_DAG)
        workflow_id = run_res["workflow_id"]
        print(f"Workflow started with workflow_id: {workflow_id}")

        # Wait until preprocessing runs and train starts (about 1.5 seconds)
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

        # Start server up again
        print("Restarting server...")
        proc, log = start_server("server_test2_recovered.log")
        if not wait_for_server():
            raise RuntimeError("Server failed to restart")

        print("Checking status of workflow after restart...")
        finished = False
        for _ in range(40):
            status = get_workflow_status(workflow_id)
            if status and status["workflow_state"] == "SUCCESS":
                print("Finished successfully after recovery.")
                finished = True
                break
            elif status and status["workflow_state"] == "ERROR":
                raise RuntimeError(f"Workflow failed after recovery: {status}")
            time.sleep(1)

        assert finished, "Workflow did not recover and finish in time"
        assert status["database_record"]["run_status"] == "COMPLETED"
        print("Test 2 Passed!")
    finally:
        if proc:
            stop_server(proc, log)


if __name__ == "__main__":
    try:
        test_1_standard_flow()
        test_2_crash_and_recovery_flow()
        print("\nAll integration tests passed successfully!")
        sys.exit(0)
    except Exception as e:
        print(f"\nTest suite failed: {e}")
        sys.exit(1)
