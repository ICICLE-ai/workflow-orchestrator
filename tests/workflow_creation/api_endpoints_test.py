import json


async def test_api_workflow_run_valid(client):
    """Test that submitting a valid DAG configuration starts a workflow and returns a workflow_id."""
    with open("tests/fixtures/valid_dag.json") as f:
        valid_dag = json.load(f)

    response = await client.post("/workflow/run", json=valid_dag)
    assert response.status_code == 200
    data = response.json()
    assert "workflow_id" in data
    assert data["workflow_id"] is not None


async def test_api_workflow_run_invalid_schema(client):
    """Test that submitting an invalid DAG payload results in a 422 Unprocessable Entity error."""
    invalid_payload = {"some_unknown_field": "value"}
    response = await client.post("/workflow/run", json=invalid_payload)
    # FastAPI should return 422 Unprocessable Entity due to missing required fields
    assert response.status_code == 422


async def test_api_workflow_status_valid(client):
    """Test retrieving the status of a running workflow using its workflow_id."""
    with open("tests/fixtures/valid_dag.json") as f:
        valid_dag = json.load(f)

    run_response = await client.post("/workflow/run", json=valid_dag)
    workflow_id = run_response.json()["workflow_id"]

    status_response = await client.get(f"/workflow/{workflow_id}")
    assert status_response.status_code == 200
    data = status_response.json()
    assert data["workflow_id"] == workflow_id
    assert "workflow_state" in data
    assert "database_record" in data


async def test_api_workflow_status_not_found(client):
    """Test retrieving the status of a non-existent workflow returns a 404 Not Found error."""
    status_response = await client.get("/workflow/non-existent-wf-id")
    assert status_response.status_code == 404
