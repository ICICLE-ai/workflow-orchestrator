# workflow-orchestrator
An extensible workflow orchestration platform that manages the full ML lifecycle — ingesting data from HPC, cloud, and drone sources; harmonizing it into ZARR cubes; training and publishing models via PMC; and delivering inference results with uncertainty quantification.

## Overview
A DBOS-FastAPI proof-of-concept backend for orchestrating ML pipelines as DAGs on Tapis v3 remote compute resources with reliable, durable tracking and automatic recovery.

## Structure
* [app/main.py](app/main.py): FastAPI app & endpoints.
* [app/models.py](app/models.py): SQLAlchemy database schema.
* [app/transactions.py](app/transactions.py): DBOS database transactions.
* [app/workflows.py](app/workflows.py): Orchestrator & execution workflows.
* [app/integrations/TapisV3.py](app/integrations/TapisV3.py): Tapis API wrapper.
* [app/mock/mock_tapis.py](app/mock/mock_tapis.py): Mock Tapis job client.
* [tests/](tests/): DAG, API, and recovery tests.

## How to Run

1. **Install dependencies**:
   ```bash
   uv sync
   ```

2. **Start the database service**:
   ```bash
   docker compose up -d
   ```

3. **Reset DBOS and Database**:
   ```bash
   uv run python -m scripts.clear_dbos
   ```

4. **Run the unit tests**:
   ```bash
   uv run python -m tests.graph_test
   ```

5. **Run the server api tests**:
   ```bash
   uv run python -m tests.api_test
   ```

6. **Run the application server**:
   ```bash
   uv run python -m app.main
   ```
   Or using Uvicorn directly:
   ```bash
   uv run uvicorn app.main:app --reload
   ```

## Code Quality & Pre-commit

This project uses `pre-commit` to maintain code style and quality. The hook configurations are defined in `.pre-commit-config.yaml` and include:
- Formatting and linting checks via `ruff` (`ruff-check`, `ruff-format`).
- UV lockfile consistency checks (`uv-lock`).
- File sanity checks (trailing whitespace, mixed line endings, end of file formatting, UTF-8 BOM).
- Verification that tests follow the `*_test.py` naming convention.

### Setting Up Pre-commit

Pre-commit is installed automatically as part of the development dependencies when you run `uv sync`.

1. **Install the git hook**:
   ```bash
   uv run pre-commit install
   ```

2. **Run manually against all files**:
   ```bash
   uv run pre-commit run --all-files
```
