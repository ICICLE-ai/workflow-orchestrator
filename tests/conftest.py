# ruff: noqa: E402
import os

from dbos import DBOS
from dotenv import load_dotenv

load_dotenv()

import pytest_asyncio

from app.main import app


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_dbos_session():
    """Initialize the DBOS system database for the entire test session."""
    # Ensure system DB is reset before launching
    DBOS.reset_system_database()
    DBOS.launch()
    yield
    DBOS.destroy()


@pytest_asyncio.fixture(autouse=True)
async def reset_dbos():
    """Clear out database tables and mock files before each test to ensure a clean state."""
    # Clean up mock state
    if os.path.exists("tapis_jobs.json"):
        os.remove("tapis_jobs.json")

    # Clear the application database workflows
    from sqlalchemy import text

    from app.models import init_db
    from app.transactions import ads, mock_step_types

    await init_db()
    await ads.run_migrations()
    async with ads.engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE step_types, workflows, wf_nodes, wf_edges, wf_runs, run_steps CASCADE")
        )

    await mock_step_types()
    yield


import httpx
from httpx import ASGITransport


@pytest_asyncio.fixture
async def client(reset_dbos):
    """Provide an asynchronous HTTP client for testing the FastAPI app endpoints."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
