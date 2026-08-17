# workflow-orchestrator

**No-Code Workflow Studio** — an extensible workflow orchestration platform that manages the full ML lifecycle: ingesting data from HPC, cloud, and drone sources; harmonizing it into ZARR cubes; training and publishing models via PMC; and delivering inference results with uncertainty quantification.

Workflows are composed on a visual canvas from reusable **steps**, then executed as a durable DAG against Tapis-managed HPC resources.

## Documentation

| Document | Purpose |
| --- | --- |
| [HOW_TO_USE.md](HOW_TO_USE.md) | End-user walkthrough of the Studio — building, configuring, and running a workflow. |
| [docs/adding-a-step-form.md](docs/adding-a-step-form.md) | Add a step defined entirely in the backend with a `step.json`. **Also the reference for ports, run configuration, and the runtime execution model.** |
| [docs/adding-a-step-custom-ui.md](docs/adding-a-step-custom-ui.md) | Replace a step's generated form with a custom interactive React panel. |

## Project Structure

This is a monorepo containing both the React-based frontend and FastAPI-based backend.

- `frontend/`: React Router v7 & Mantine UI frontend app.
- `backend/`: FastAPI & PostgreSQL backend service.
  - `backend/steps/`: One folder per workflow step, each with a `step.json` synced into the step registry on startup.
  - `backend/engine/`: The DBOS-based durable execution engine — DAG orchestration, Tapis job rendering and submission, secrets.
- `docs/`: Developer guides and project governance documents.
- `jobs/`: Container/job definitions backing executable steps.
- `component.yaml`: ICICLE component descriptor (release metadata and documentation links).

## Setup & Running Locally

### Backend Setup

The backend uses Python 3 and FastAPI, and connects to a PostgreSQL database.

1. Navigate to the backend directory:
   ```bash
   cd backend
   ```
2. Start the PostgreSQL database using Docker:
   ```bash
   docker compose up -d
   ```
   *Requires the Docker daemon to be running. This publishes Postgres on host port `5433`.*
3. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```
4. Start the backend development server:
   ```bash
   python main.py
   ```
   *The backend will run on `http://localhost:8002`.*

### Frontend Setup

The frontend uses Node.js and React Router v7.

1. Navigate to the frontend directory:
   ```bash
   cd frontend
   ```
2. **Authenticate to GitHub Packages** (one-time). The UI depends on five
   `@icicle-ai/*` packages published there, and that registry returns `401` without
   a token *even for public packages*. Create a
   [personal access token](https://github.com/settings/tokens) with the
   **`read:packages`** scope and add it to your `~/.npmrc`:
   ```bash
   echo "//npm.pkg.github.com/:_authToken=YOUR_TOKEN" >> ~/.npmrc
   ```
   *The registry mapping itself is already in `frontend/.npmrc` — only the token is
   personal. Never commit it.*
3. Install the Node modules:
   ```bash
   npm install
   ```
4. Start the Vite development server:
   ```bash
   npm run dev
   ```
   *The frontend will be accessible at `http://localhost:5173`.*

## Contributing

Contributions are welcome — bug reports, documentation, tests, examples, and code. New workflow steps are the most common contribution; the two step-authoring guides above cover the whole path.

Start with [CONTRIBUTING.md](CONTRIBUTING.md), and open an issue using one of the templates before beginning substantial work.

## Project and governance files

| File | Purpose |
| --- | --- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute, the contribution pathway, and pull-request expectations. |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Expected and unacceptable behavior, reporting, and enforcement. |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability privately. **Do not open a public issue for one.** |
| [CITATION.cff](CITATION.cff) | Citation metadata — use this if you reference the project in published work. |
| [docs/MAINTAINER_ROLES.md](docs/MAINTAINER_ROLES.md) | Who is responsible for roadmap, review, releases, and security response. |
| [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) | Checklist completed before creating a public release. |

### Repository automation

GitHub configuration lives in [`.github/`](.github/):

| Path | Purpose |
| --- | --- |
| `ISSUE_TEMPLATE/bug_report.yml` | Structured bug report form. |
| `ISSUE_TEMPLATE/feature_request.yml` | Contribution / feature proposal form. |
| `ISSUE_TEMPLATE/config.yml` | Routes security reports and usage questions away from public issues. |
| `PULL_REQUEST_TEMPLATE.md` | Pull-request checklist covering validation, dependencies, and data/security implications. |
| `workflows/repository-health.yml` | CI check that required project files are present. |
| `workflows/secret-scan.yml` | CI secret scanning (gitleaks + TruffleHog), plus a weekly full-history rescan. |

## Citation

If you use this software, please cite it using the metadata in [CITATION.cff](CITATION.cff).
