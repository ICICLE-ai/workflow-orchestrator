# workflow-orchestrator
An extensible workflow orchestration platform that manages the full ML lifecycle — ingesting data from HPC, cloud, and drone sources; harmonizing it into ZARR cubes; training and publishing models via PMC; and delivering inference results with uncertainty quantification.

# DBOS FastAPI and Mock Tapis Pipeline Example
This branch contains a sample project integrating FastAPI with DBOS to orchestrate a mock Tapis AI pipeline. The workflow orchestrator tracks progress and status through server crashes and restarts.

## Project Vision & Purpose

This workflow orchestrator provides a user interface (UI) to allow researchers who aren't AI researchers to implement machine learning and data processing pipelines for their projects (e.g., a researcher training image classification models on a custom dataset). 

The UI allows users to drag and drop "steps" and connect the inputs and outputs of nodes. The system supports Directed Acyclic Graphs (DAGs), enabling setups like two distinct datasets with their own preprocessing steps connecting to one or more training steps, which then feed into one or more inference steps.

All steps run on remote compute resources managed via Tapis v3. This project (`dbos-example`) acts as a proof-of-concept backend, utilizing DBOS Transact to facilitate reliable, resilient, and durable pipeline tracking and recovery.

## File Structure

- [app/main.py](app/main.py): Main entrypoint of the application. Contains the FastAPI app and lifespans.
- [app/models.py](app/models.py): Declares SQLAlchemy models for the business schema (`StepType`, `Workflow`, `WFNode`, `WFEdge`, `WFRun`, and `RunStep`) and handles database schema initialization.
- [app/transactions.py](app/transactions.py): Contains all the DBOS transactions (using SQLAlchemy session queries) for creating workflows, resolving inputs, and updating run step statuses.
- [app/workflows.py](app/workflows.py): Defines the DBOS orchestrator workflow (`dag_orchestrator_workflow`) and node execution workflow (`execute_node_workflow`).
- [app/integrations/TapisV3.py](app/integrations/TapisV3.py): Wraps job submission and status checks to Tapis in DBOS step interfaces.
- [app/mock/mock_tapis.py](app/mock/mock_tapis.py): Implements a simulated Tapis v3 job client, maintaining state inside `tapis_jobs.json`.
- [tests/test.py](tests/test.py): An integration test suite verifying standard DAG execution and crash recovery.
- [tests/test_graph.py](tests/test_graph.py): A unit test suite verifying graph topological sorting and ASCII progress graph rendering.
- [scripts/clear_dbos.py](scripts/clear_dbos.py): Script to clear and reset application database states.

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
   uv run python -m tests.test_graph
   ```

5. **Run the server api tests**:
   ```bash
   uv run python -m tests.test
   ```

6. **Run the application server**:
   ```bash
   uv run python -m app.main
   ```
   Or using Uvicorn directly:
   ```bash
   uv run uvicorn app.main:app --reload
   ```

