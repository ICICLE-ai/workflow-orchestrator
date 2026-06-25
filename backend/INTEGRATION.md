# Tapis Execution — Integration & Prerequisites

This backend can run workflow templates as **real Tapis V3 jobs**. The orchestration,
job-spec rendering, and submission are all implemented and verified. What remains
before a job can actually run to completion is **Tapis account/system provisioning**,
which requires permissions the application code cannot grant itself.

This document is for whoever administers the `icicleai` Tapis tenant / the team's
Tapis accounts.

---

## How execution works (one paragraph)

A saved workflow template (nodes + edges) is executed by a DBOS durable workflow.
For each node, the engine resolves its input ports from the **incoming edges**
(binding upstream output URIs to downstream inputs), renders that step's
`tapis_job` template (from its `step.json`) by substituting `${...}` placeholders,
and submits the resulting job spec to `POST {TAPIS_BASE_URL}/v3/jobs/submit`,
then polls `GET /v3/jobs/{uuid}/status` until `FINISHED`/`FAILED`. Per-step state
is tracked in `pipeline_run` / `run_step`. If no Tapis credential is configured,
the engine falls back to a mock client so the system still runs locally.

Relevant code: `engine/tapis_auth.py` (credentials), `engine/tapis.py` (submit/poll),
`engine/job_spec.py` (placeholder rendering), `engine/workflows.py` (DAG orchestration),
`engine/transactions.py` (DB + edge resolution). Per-step job payloads live in
`steps/<step>/step.json` under a `tapis_job` block.

---

## Credentials (already supported by the code)

Set in `backend/.env` (gitignored — never commit it). Copy `backend/.env.example`.

```
TAPIS_BASE_URL=https://icicleai.tapis.io
TAPIS_TENANT=icicleai

# Option A — an existing JWT (X-Tapis-Token):
TAPIS_TOKEN=<jwt>

# Option B — username/password, minted to a JWT at runtime:
# TAPIS_USERNAME=<user>
# TAPIS_PASSWORD=<pass>

# Force the mock even if creds are present:
TAPIS_USE_MOCK=false
```

Resolution order: `TAPIS_TOKEN` → mint from `TAPIS_USERNAME`/`TAPIS_PASSWORD` →
mock fallback. To verify a token is live without printing it:

```bash
cd backend
.venv/bin/python -c "from engine import tapis_auth, json; print(json.dumps(tapis_auth.describe_credentials(), indent=2))"
```

A valid result reports `"mode": "real"`, `"token_valid": true`, and the resolved username.

---

## Verified status (what works today)

Tested against `icicleai.tapis.io` with a real user token (`arunachalam.31@osu.edu`):

| Check | Result |
| --- | --- |
| Token validates against Tapis `userinfo` | ✅ valid |
| App `harvest-preprocess-yuan1374` v0.3.6 visible/enabled | ✅ |
| App `harvest-inference` v0.1.7 visible/enabled | ✅ |
| Exec system `expanse-tapis` reachable, `canExec=true` | ✅ |
| Job spec renders edge outputs → real Tapis URIs | ✅ |
| Real `POST /v3/jobs/submit` reaches & is validated by Tapis | ✅ (request accepted, then rejected on provisioning — see below) |

**The integration is proven end-to-end up to Tapis's own credential check.**

---

## Blockers — require Tapis admin / account provisioning

These are **not** code issues. A real submission currently fails at Tapis with:

> `SYSTEMS_MISSING_CREDENTIALS — There are no credentials associated with system
> expanse-tapis (login.expanse.sdsc.edu) for tenant icicleai. Set a default
> credential to access data on this system.`

To unblock real runs, the following must be set up on the Tapis side for the
account whose token the backend uses:

1. **Register an SSH credential for the user on `expanse-tapis`.**
   `GET /v3/systems/expanse-tapis/credentials/<user>` currently returns 404 (no
   credential). An admin must register one, e.g.:
   ```
   POST /v3/systems/expanse-tapis/credentials/<user>
   { "privateKey": "...", "publicKey": "...", "loginUser": "<expanse-login>" }
   ```
   (Or whatever auth method the system requires.) This is what threw the 400 above.

2. **A valid Slurm allocation** on Expanse for that user. It is passed at execute
   time and substituted into the job's `-A ${slurm_account}` scheduler arg. Without
   a real account string, scheduling will be rejected even after the credential is set.

3. **Real input data** at valid Tapis paths. The job's `fileInputs.sourceUrl`
   values come from the workflow's source nodes / upstream step outputs
   (`${images}`, `${model}`, `${csv_input}`, etc.). Test/placeholder paths will
   fail at input staging.

4. **Access to the `harvest-train` app.** It currently returns **403** for the
   test account (preprocess and inference return 200). The app owner must share
   `harvest-train` with the executing account, or training cannot be submitted.

---

## Caveats about the ported job specs

The `tapis_job` templates in `steps/*/step.json` were ported from the legacy
`harvest-webservers/server/steps.py`. They may have drifted from current reality:

- **App versions** (`harvest-inference` 0.1.7, `harvest-train` 0.2.6,
  `harvest-preprocess-yuan1374` 0.3.6) — confirm these are still the intended versions.
- **`.sif` container postit URLs** are hardcoded in the templates and **expire**;
  the legacy token postit is already dead. Regenerate and update if stale.
- **Resource requests** (queues `tapisGPU`/`tapisGPUshared`, cores, memory,
  `maxMinutes`) are copied verbatim; adjust to current allocation policy.
- **Output mapping** is generic: each step exposes `archive_uri` / `output_dir`
  as its outputs. Mapping a specific output *port* to a specific produced file
  (e.g. inference's exact `result.json`) needs per-output path conventions that
  depend on what each container writes.

---

## How to run once provisioned

```bash
# backend (Python 3.14 venv via uv)
cd backend && .venv/bin/python -m uvicorn main:app --port 8002
# frontend
cd frontend && npm run dev
```

Open a saved template in the canvas → **Run Workflow**. Or via API:

```
POST /api/pipeline-runs/{template_version_id}/execute      # returns dbos_workflow_id
GET  /api/pipeline-runs/status/{dbos_workflow_id}          # poll status + progress graph
```

To pass run-level values (allocation, archive location), include them in the
execute request's config so they flow into `frozen_config` and get substituted:
`slurm_account`, `archive_system`, `archive_dir`.
