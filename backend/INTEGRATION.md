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

---

## OSC (Pitzer/Ascend) execution — findings

Expanse is blocked by MFA on Tapis's automated SSH login. **OSC works**: the
`icicleai` tenant has `pitzer-tapis` / `ascend-tapis` systems (`canExec=true`,
`effectiveUserId` = the user's OSC login), and a Tapis→OSC credential is already
registered, so real jobs submit, SSH into OSC, and stage/schedule successfully.

Run an OSC job by passing these RunOptions to the execute endpoint (or via the
UI "Run Settings" drawer):
- `exec_system`: `pitzer-tapis` (or `ascend-tapis`)
- `exec_queue`: OSC queue — `cpu` / `gpu` / `nextgen` (NOT Expanse's tapisGPU*)
- `work_dir`: an ABSOLUTE project path, e.g. `/fs/ess/PAS2699/<user>`
- `archive_dir`: an ABSOLUTE path under your project (a root-relative value makes
  Tapis try to `mkdir` at `/` on the host → FILES_REMOTE_MKDIRS_ERROR)
- `slurm_account`: e.g. `PAS2699`

## Debugging a failed harvest-inference job on OSC

Verified working (do NOT re-investigate these):
- Source→step edge data flow, real submission, OSC SSH login, input staging.
- The inference `.sif` postit is ALIVE and a valid Singularity image
  (`#!/usr/bin/env run-singularity`). Downloads fine.
- Model files map correctly: `${model}/config.json` and `${model}/saved_model.pth`
  both exist in `/fs/ess/PAS2699/harvest_trained_models/2024-9-10-22-7-54`.
- Env vars (`WORKDIR=$PWD/workdir`, `DATADIR=$PWD/data`) and binds match the
  legacy working config.

Fixed in our template (was an Expanse-ism):
- Removed `--bind $CUDAHOME:/cuda` from inference containerArgs — `/cuda` doesn't
  exist on Pitzer, so Singularity aborted at container creation:
  `FATAL: container creation failed: mount /cuda->/cuda ... doesn't exist`.
  GPU access still works via the app's `--nv` flag. (Read from the failed job's
  `tapisjob.out` via GET /v3/jobs/{uuid}/output/download/tapisjob.out.)

Open issues (app-owner territory, NOT the orchestrator):
- The sample model's `config.json` has hardcoded absolute paths to ANOTHER user's
  dir (`/users/PAS2581/potlapally2/...`) for `data_dir` / `model_save_path`. If the
  inference code reads these, it will look in the wrong place. A portable model
  config (or a model trained/configured for the running user's paths) is needed.
- The model is a ViT regression model (`google/vit-base-patch16-224-in21k`,
  task=regression, num_classes=1). The container must match this architecture.
- Staging the 343MB `saved_model.pth` over SSH to OSC is slow (minutes) and is the
  main iteration bottleneck — use a small model/dataset for quick container tests.

To read any failed job's actual container output:
  GET /v3/jobs/{uuid}/output/list/
  GET /v3/jobs/{uuid}/output/download/tapisjob.out

## Inference container internals (harvest-inference v0.1.7)

The .sif is an NVIDIA Triton Inference Server image
(`nvcr.io/nvidia/tritonserver:24.03-py3`, built 2025-06-20). Its %runscript is
`/app/entrypoint.sh`; app files: entrypoint.sh, inf_util.py, pytriton_server.py,
pytriton_client.py, inf_server.py, inference_backend/, build_model/, dali_224/96/32.
(The `entrypoint.sh` in the harvest-webservers repo root is the OLD Flask webapp
container, NOT this Triton one — do not confuse them.)

Model config path handling:
- Our template mounts `${model}/config.json` -> workdir/models/saved_model.json,
  and `${model}/saved_model.pth` -> workdir/models_convert/saved_model.pth.
  Container binds `$PWD/workdir:/job_dir`, `$PWD/data:/dataset`, so the container
  reads config at /job_dir/models/saved_model.json.
- The sample models' config.json contain HARDCODED absolute paths to another
  user's dir (/users/PAS2581/potlapally2/...) for data_dir / model_save_path /
  dataset. The old InfWorker.parse_uploaded_model() reads conf["model_save_path"],
  so these paths likely DO matter to the container.

To make a run complete (recommended, user-doable on OSC):
1. Copy a model dir's saved_model.pth + config.json into your own space
   (e.g. /fs/ess/PAS2699/<user>/models/soy1/).
2. Edit config.json paths to the in-container mount targets (inferred):
     data_dir / dataset -> /dataset
     model_save_path     -> /job_dir/models_convert/saved_model.pth
3. Point the source_model node at your copy; run; read tapisjob.out and iterate.
The exact expected config is known only to the container's author (harvest team) —
ask them to avoid guesswork on the in-container paths.

## ROOT CAUSE of harvest-inference failures (definitive, 2026-07-05)

Not the data paths, not the input dataset. The inference container FAILS AT MODEL
LOAD with a state_dict key mismatch, then idles (count:0) until Slurm TIMEOUT.

From the container's tapisjob.out:
    File "/app/build_model/build_model.py", line 54, in build_model
        model.load_state_dict(torch.load(weight_path, map_location='cuda:0'))
    RuntimeError: Error(s) in loading state_dict for ViTForImageClassification:
        Missing key(s):    "vit.embeddings.cls_token", ...
        Unexpected key(s): "module.vit.embeddings.cls_token", ...

The sample models' saved_model.pth were trained with torch.nn.DataParallel/DDP,
which prefixes every weight key with "module.". The container loads the bare model
(no DataParallel), so every key mismatches -> build_model.py crashes -> Triton never
serves the model -> inf server returns {'count':0} forever -> job hits maxMinutes.

Confirmed NOT the cause (ruled out by testing):
- Input dataset: real soybean .jpg images gave the identical count:0 / TIMEOUT.
- Source-node path / DATADIR: model load crashes before any data is read.
- config.json hardcoded /users/PAS2581/... paths: printed but harmless — weights
  load from the mounted /job_dir/models/saved_model.json (log: "Found convertion
  pending model: /job_dir/models/saved_model.json").

To get a successful run, need ONE of:
  1. A model checkpoint saved WITHOUT DataParallel (no "module." prefix), OR
  2. The container's build_model.py fixed to strip "module." before load_state_dict
     (e.g. {k.replace('module.',''): v}) — container-owner change, OR
  3. A model known to be compatible with harvest-inference v0.1.7.

Also fixed this session: maxMinutes lowered 210->20 for fast iteration; the poller
now refreshes the token on 401 and tolerates transient errors so a long job isn't
falsely marked failed mid-run.

## Custom YOLO inference app (clean replacement for harvest-inference)

Because the legacy harvest-inference container fails at model load (DataParallel
"module." prefix, unfixable without the container owner), we built our own simple
YOLO inference app. Artifacts in repo: jobs/yolo_inference/{yolo_infer.py,
yolo_infer.def, requirements.txt}.

Design — RUN-WORKSPACE OUTPUT MODEL (how node outputs are handled):
  Every step writes outputs to a per-run/per-node dir; the output port's value IS
  that path (a tapis:// URI). Downstream consumption is uniform:
    - passed to a next node  -> edge resolves the upstream output dir into the
      next step's input sourceUrl (already works via _resolve_inputs)
    - written to a sink      -> sink_path overrides the step's archiveSystemDir to
      the user's path (already works via get_downstream_sink_path)
    - left hanging           -> output still exists in the run workspace, unconsumed
  This is type-agnostic (predictions json, trained model, image dir all handled
  the same) and reproducible. The YOLO app writes results to /out (bind-mounted),
  which maps to the node workspace -> sink relocates to the user path.

The app takes ALL paths as ARGS (--model ${model} --images ${images} --output /out),
so the COCO/YOLO paths live only in the source-node configs, never hardcoded.

Build/register steps (need OSC shell + fresh token):
  3. apptainer build yolo_infer.sif yolo_infer.def   (on OSC)
  4. POST /v3/files/postits/pitzer-tapis/<path>/yolo_infer.sif -> redeem URL
  5. register Tapis app 'yolo-inference' v1.0 (SINGULARITY_RUN, containerImage=postit URL,
     appArgs model/images/output, containerArgs --nv)
Then steps/yolo_inference/step.json wires ${images}/${model} -> the app; workflow:
  source_image_dir + source_model -> yolo_inference -> sink_path(wf_run_outputs).

Test inputs (in source-node configs, NOT hardcoded):
  images: tapis://pitzer-tapis/users/PAS2699/shyama02/coco8-multispectral/images/val
  model:  tapis://pitzer-tapis/users/PAS2699/shyama02/yolo26n/yolo26n.pt

## YOLO app — first end-to-end run result (2026-07-09)

FULL PIPELINE WORKED. Built yolo-inference app (sif+postit+app registered), ran
workflow source_image_dir+source_model -> yolo_inference -> sink on OSC GPU.
Container ran, script found the 4 images, loaded yolo26n.pt, ran YOLO forward.

Only failure (real ML data issue, fixed in OUR script):
  RuntimeError: expected input[1, 3, ...] to have 3 channels, but got 10 channels
The coco8-MULTISPECTRAL images are 10-channel; YOLO wants 3-channel RGB.
Fix: yolo_infer.py now converts any multiband/grayscale image to 3ch RGB via
cv2.IMREAD_UNCHANGED -> first 3 bands -> normalize to uint8 (load_rgb()).
Requires rebuilding yolo_infer.sif on OSC (script is baked in at build time);
postit + app unchanged (same sif path). Workflow template kept (id 28).

## ✅ FIRST FULLY SUCCESSFUL END-TO-END RUN (2026-07-09)

Workflow: source_image_dir + source_model -> yolo_inference -> sink_path
On OSC Pitzer GPU. Statuses: STAGING -> QUEUED -> RUNNING -> ARCHIVING -> FINISHED,
run COMPLETED, all nodes green.

Outputs written to the SINK dir /users/PAS2699/shyama02/wf_run_outputs:
  predictions.json, summary.json, annotated/*.tiff (4 annotated images), tapisjob.out
Real detections: 8 across 4 images (person, horse x2, elephant x2, umbrella, cell phone).

This proves the whole orchestrator: config-driven step (step.json), source nodes
provide paths (no hardcoding), edge data flow, real Tapis app submission on OSC,
sink relocates output to a user path. Model/data/output paths all come from nodes.

Note: after rebuilding the sif, REGENERATE the postit and PATCH the app's
containerImage to the new redeem URL (postits can cache old content):
  POST /v3/files/postits/<sys>/<sifpath>   -> new redeemUrl
  PATCH /v3/apps/yolo-inference/1.0  {"containerImage": <new redeemUrl>}

## ✅ MULTI-OUTPUT + NODE-TO-NODE + PER-PORT SINK — verified end-to-end (2026-07-11)

Workflow (template 31): source_image_dir + source_model -> yolo_inference
-> [predictions] -> class_histogram -> sink_image_dir. Ran COMPLETED on OSC.

Proved ALL of the requested design:
- Multi-output step: yolo_inference declares 3 output ports (predictions.json,
  summary.json, annotated/), each mapped to its own artifact via `output_path`.
- Node-to-node flow WITHOUT a sink between: 'predictions' fed class_histogram
  directly. Intermediate outputs live in the per-run workspace
  /fs/ess/PAS2699/shyama/wf_runs/<run_id>/<node_id>/; the downstream step's
  fileInput sourceUrl resolves to that exact path (e.g. .../<yolo_node>/predictions.json).
- Two outputs left hanging: 'summary' and 'annotations' stayed in the yolo
  workspace, unconsumed — no error.
- Per-port sink: class_histogram's 'chart' output (image_dir) -> sink copies it to
  the user path /users/PAS2699/shyama02/viz_output. Verified: class_histogram.png
  (2400x960) + class_counts.json landed there. Counts: person 2, horse 2,
  elephant 2, umbrella 1, cell phone 1.

New Tapis app: class-histogram v1.0 (matplotlib, CPU). jobs/class_histogram/.

Two bugs fixed to get here:
1. config_schema DEFAULTS weren't applied — a param the user omitted (imgsz)
   rendered as empty ${imgsz} and broke argparse. Now _resolve_inputs seeds from
   get_config_schema_defaults() (defaults < node config < edge inputs).
2. Per-step QUEUE: a CPU-only step inheriting the run's exec_queue=gpu hit Slurm
   'QOSMinGRES' (gpu partition requires a GPU). A step can now set exec_queue in
   its config_schema (default overrides the run-level queue); class_histogram
   defaults to 'cpu'.

Also: input paths on OSC were moved by the user (coco8-multispectral/yolo26n ->
my_new_images/my_new_model); source-node paths just point at the new locations.

## OUTPUT-PORT CONTRACT — enforced (2026-07-11)

RULE (so complex graphs stay consistent): each declared output PORT maps to
exactly one artifact of its declared type. A step with >1 output must give each
port a distinct, non-empty `output_path`; no two ports may share a path; a
`*_dir` port must be a directory of ONLY that type, a scalar type a single file.

Enforcement: main.py `validate_step_output_contract()` runs in the sync. A
step.json that violates the contract is SKIPPED (logged, not registered) so it
can't corrupt downstream edge routing.

Fixed to comply:
- class_histogram: was 1 output 'chart'(image_dir) but wrote BOTH a PNG and a
  JSON into it (mixed types). Now TWO ports: chart(image_dir -> chart/, images
  only) + counts(json_results -> class_counts.json). Script writes PNG into
  chart/ and the JSON separately. >> class_histogram.sif MUST BE REBUILT on OSC
  (script changed) before re-running.
- training: 2 outputs (model, metrics) both had empty output_path (would
  collide). Now model->'model', metrics->'metrics.json'.

Negative-tested: validator rejects (a) multi-output with empty paths and
(b) two ports sharing a path; passes valid multi/single-output configs.
