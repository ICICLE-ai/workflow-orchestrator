import asyncio
import json
import os

import pytest
from httpx import AsyncClient


@pytest.mark.smoke
async def test_1_standard_flow(client: AsyncClient):
    """Test the standard successful execution of the DBOS DAG workflow using Pytest."""
    # Load valid DAG config
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "valid_dag.json")
    with open(fixture_path) as f:
        dag_config = json.load(f)

    # Trigger workflow
    response = await client.post("/workflow/run", json=dag_config)
    assert response.status_code == 200, f"Failed to start workflow: {response.text}"
    run_res = response.json()
    workflow_id = run_res["workflow_id"]

    # Poll status until completion
    finished = False
    status = None
    for _ in range(40):
        status_res = await client.get(f"/workflow/{workflow_id}")
        assert status_res.status_code == 200
        status = status_res.json()

        if status and status.get("workflow_state") == "SUCCESS":
            finished = True
            break
        elif status and status.get("workflow_state") == "ERROR":
            raise RuntimeError(f"Workflow failed: {status}")

        await asyncio.sleep(1)

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

    # Test format=text format response
    url_text = f"/workflow/{workflow_id}?format=text"
    res_text = await client.get(url_text)
    assert res_text.status_code == 200
    assert res_text.text, "text graph is empty"
