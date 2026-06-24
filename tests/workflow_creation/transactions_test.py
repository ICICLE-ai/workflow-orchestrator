import json

import pytest
from sqlalchemy import select

from app.models import RunStep, WFEdge, WFNode, WFRun, Workflow
from app.transactions import ads, create_workflow_from_config


@pytest.mark.asyncio
async def test_create_workflow_from_config_valid(reset_dbos):
    """Test that a valid DAG configuration is correctly parsed and stored in the database."""
    with open("tests/fixtures/valid_dag.json") as f:
        valid_dag = json.load(f)

    workflow_id = "test-dbos-wf-1"
    run_id = await create_workflow_from_config(workflow_id, valid_dag)

    assert run_id > 0

    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(ads.engine) as session:
        # Assert workflow is created
        result = await session.execute(select(Workflow))
        workflows = result.scalars().all()
        assert len(workflows) == 1

        # Assert nodes are created
        result = await session.execute(select(WFNode))
        nodes = result.scalars().all()
        assert len(nodes) == len(valid_dag["nodes"])

        # Assert edges are created
        result = await session.execute(select(WFEdge))
        edges = result.scalars().all()
        assert len(edges) == len(valid_dag["edges"])

        # Assert wf_run is created
        result = await session.execute(select(WFRun))
        wf_runs = result.scalars().all()
        assert len(wf_runs) == 1
        assert wf_runs[0].dbos_workflow_id == workflow_id

        # Assert run steps are created
        result = await session.execute(select(RunStep))
        run_steps = result.scalars().all()
        assert len(run_steps) == len(valid_dag["nodes"])


@pytest.mark.asyncio
async def test_create_workflow_from_config_invalid(reset_dbos):
    """Test that an invalid DAG configuration with missing dependencies raises an error during creation."""
    with open("tests/fixtures/invalid_dag.json") as f:
        invalid_dag = json.load(f)

    workflow_id = "test-dbos-wf-2"
    with pytest.raises(KeyError):
        await create_workflow_from_config(workflow_id, invalid_dag)
