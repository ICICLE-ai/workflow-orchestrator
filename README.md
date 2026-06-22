# DBOS FastAPI and Mock Tapis Pipeline Example

This repository contains a sample project showing how to integrate FastAPI with DBOS to orchestrate a mock AI pipeline. The workflow interacts with a simulated Tapis Jobs API, tracking progress and status durably through server crashes and restarts.

## File Structure

- main.py: Main entry point. Defines DBOS steps, transactions, and the AI pipeline workflow. It also starts the FastAPI application and runs the Uvicorn server.
- mock_tapis.py: Implements a mock client for the Tapis Jobs API, simulating training job submission and status transitions.
- models.py: Contains the SQLAlchemy database models and initializes the Postgres database schema used for pipeline tracking.
- test.py: An integration test suite verifying the normal execution of the pipeline and recovery behavior after a simulated server crash.
- docker-compose.yml: Defines a local PostgreSQL database container on port 5433 to serve as the backend database for DBOS and the application.
- pyproject.toml: Configuration file detailing project dependencies including dbos, fastapi, uvicorn, and psycopg2.

## How to Run

1. Start the database service:
   docker compose up -d

2. Run the integration tests:
   uv run test.py

3. Run the application server manually:
   uv run main.py
