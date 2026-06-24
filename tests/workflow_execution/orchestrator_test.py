import asyncio
import json
import time

import pytest
from httpx import AsyncClient


async def test_orchestrator_valid_dag(client: AsyncClient):
    """Test the complete orchestration flow of a valid DAG from initialization to success."""
    with open("tests/fixtures/valid_dag.json") as f:
        dag_payload = json.load(f)

    # Submit valid DAG
    response = await client.post("/workflow/run", json=dag_payload)
    assert response.status_code == 200
    data = response.json()
    assert "workflow_id" in data
    workflow_id = data["workflow_id"]

    # Poll until SUCCESS
    start_time = time.time()
    success = False
    while time.time() - start_time < 20:
        res = await client.get(f"/workflow/{workflow_id}")
        assert res.status_code == 200
        status_data = res.json()
        if status_data.get("workflow_state") == "SUCCESS":
            success = True
            break
        elif status_data.get("workflow_state") == "ERROR":
            pytest.fail(f"Workflow failed: {status_data}")
        await asyncio.sleep(1)

    assert success, "Workflow did not reach SUCCESS within 20 seconds"
