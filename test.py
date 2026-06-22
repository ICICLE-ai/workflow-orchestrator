import subprocess
import sys
import time
import json
import urllib.request

def start_server(log_file_path="server_test.log"):
    print(f"Starting server, writing logs to {log_file_path}...")
    server_log = open(log_file_path, "w")
    process = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=server_log,
        stderr=subprocess.STDOUT
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

def wait_for_server(timeout=15):
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

def trigger_pipeline(dataset_url):
    url = f"http://localhost:8000/pipeline/run?dataset_url={dataset_url}"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())

def get_pipeline_status(workflow_id):
    url = f"http://localhost:8000/pipeline/{workflow_id}"
    try:
        with urllib.request.urlopen(url) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"Error checking status: {e}")
        return None

def test_1_standard_flow():
    print("\n--- Starting Test 1: Standard Flow ---")
    proc, log = start_server("server_test1.log")
    try:
        if not wait_for_server():
            raise RuntimeError("Server failed to start")

        print("Triggering pipeline...")
        run_res = trigger_pipeline("test1_url")
        workflow_id = run_res["workflow_id"]
        print(f"Pipeline started with workflow_id: {workflow_id}")

        # Check immediately while running
        time.sleep(1)
        status = get_pipeline_status(workflow_id)
        print(f"Immediate status check: {status}")
        assert status is not None
        assert status["workflow_state"] == "PENDING"
        assert status["database_record"]["pipeline_status"] in ["PREPROCESSING", "SUBMITTING_TRAINING", "TRAINING"]

        # Poll until completed
        print("Polling status until completion...")
        finished = False
        for _ in range(30):
            status = get_pipeline_status(workflow_id)
            if status and status["workflow_state"] == "SUCCESS":
                print(f"Finished successfully: {status}")
                finished = True
                break
            elif status and status["workflow_state"] == "ERROR":
                raise RuntimeError(f"Workflow failed: {status}")
            time.sleep(1)

        assert finished, "Workflow did not finish in time"
        assert status["database_record"]["pipeline_status"] == "COMPLETED"
        assert status["database_record"]["model_accuracy"] is not None
        assert 0.85 <= status["database_record"]["model_accuracy"] <= 0.99
        print("Test 1 Passed!")
    finally:
        stop_server(proc, log)

def test_2_crash_and_recovery_flow():
    print("\n--- Starting Test 2: Crash and Recovery Flow ---")
    proc, log = start_server("server_test2.log")
    workflow_id = None
    try:
        if not wait_for_server():
            raise RuntimeError("Server failed to start")

        print("Triggering pipeline...")
        run_res = trigger_pipeline("test2_url")
        workflow_id = run_res["workflow_id"]
        print(f"Pipeline started with workflow_id: {workflow_id}")

        # Wait until it's running (e.g. 2 seconds, during preprocessing/submitting)
        time.sleep(2)
        status = get_pipeline_status(workflow_id)
        print(f"Status before crash: {status}")
        assert status is not None
        assert status["workflow_state"] == "PENDING"

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

        # Verify the server can take requests immediately after restart
        immediate_status = get_pipeline_status(workflow_id)
        print(f"Immediate status check after restart: {immediate_status}")
        assert immediate_status is not None
        assert immediate_status["workflow_state"] == "PENDING"
        assert immediate_status["database_record"]["pipeline_status"] in ["PREPROCESSING", "SUBMITTING_TRAINING", "TRAINING"]

        print("Checking status of workflow after restart...")
        # Wait until it is completed
        finished = False
        for _ in range(30):
            status = get_pipeline_status(workflow_id)
            if status and status["workflow_state"] == "SUCCESS":
                print(f"Finished successfully after recovery: {status}")
                finished = True
                break
            elif status and status["workflow_state"] == "ERROR":
                raise RuntimeError(f"Workflow failed after recovery: {status}")
            time.sleep(1)

        assert finished, "Workflow did not recover and finish in time"
        assert status["database_record"]["pipeline_status"] == "COMPLETED"
        assert status["database_record"]["model_accuracy"] is not None
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
