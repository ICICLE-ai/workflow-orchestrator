# wf-runner as a Tapis app — run a whole pipeline as ONE job

This registers the same runner that powers *Deploy to my node* as a Tapis app, so
the platform can submit an entire workflow as a **single Tapis job** instead of
one job per step. One queue wait instead of N, which on a busy allocation is
usually the difference that matters.

The platform side is [`backend/engine/single_job.py`](../../backend/engine/single_job.py);
the user-facing behaviour is in [HOW_TO_USE.md](../../HOW_TO_USE.md#run-a-whole-pipeline-as-one-tapis-job).
**Until this app is registered, the mode cannot submit anything** — the Run
Settings panel will still show the merged job it *would* submit.

| | |
|---|---|
| `tapisjob_app.sh` | What Tapis runs on the compute node. Execs `python3 -m wf_runner`. |
| `app.json` | The app definition. Two fields need filling in; see below. |

## Why a ZIP app, not a container app

The runner's whole job is to invoke `apptainer` once per step. If the runner
itself ran inside a container, each step would be a **nested** container launch —
which needs unprivileged user namespaces and is disabled on many hardened HPC
kernels. The runner is pure Python standard library precisely so it can run
directly on the node, where `apptainer` is an ordinary command. A ZIP-runtime app
is how Tapis runs a plain script on the node.

## Register it

```bash
# 1. Build the app archive. tapisjob_app.sh must sit at the ROOT of the zip:
#    Tapis unpacks the archive into the job directory and runs ./tapisjob_app.sh
#    from there, so a nested copy would not be found.
cd runner
rm -rf build && mkdir -p build/wf-runner
cp -r wf_runner build/wf-runner/
cp tapis-app/tapisjob_app.sh build/wf-runner/
(cd build/wf-runner && zip -rq ../wf-runner-1.0.zip .)
unzip -l build/wf-runner-1.0.zip | head    # expect tapisjob_app.sh and wf_runner/

# 2. Upload it somewhere the exec system can read.
tapis files upload tapis://<system>/<path>/ build/wf-runner-1.0.zip

# 3. Point app.json's containerImage at that location, then create the app.
#    (Set `owner` to your username if you are not using ${apiUserId}.)
tapis apps create -f tapis-app/app.json
```

Then tell the backend the app id and version, if they differ from the defaults:

```bash
WF_RUNNER_APP_ID=wf-runner        # default
WF_RUNNER_APP_VERSION=1.0         # default
```

## Cache the step images on the cluster

A pipeline job needs each step's `.sif` on the exec system. Put them all in one
directory and point the backend at it:

```bash
WF_RUNNER_IMAGES_DIR=/fs/ess/PAS2699/wf-images
```

With that set, nothing is transferred per run. **Without** it, the platform stages
any image it has a download URL for (`backend/image_sources.json`) into the job
directory on every run, and *refuses to launch* when an image has no known source
— better than a job that starts, runs for a while, and dies on a missing
container. The refusal names the image and both ways to fix it.

Build the images defined in this repo from `jobs/*/*.def`; filenames are matched
ignoring case and `-`/`_`, so `zero_shot_annotation.sif` satisfies a plan asking
for `zero-shot-annotation.sif`.

## What the job looks like

The platform submits, per run:

```
appId       wf-runner
appArgs     pipeline.json --data-root data --workdir output --images-dir <dir> --run-id <n>
fileInputs  pipeline.json          <- the frozen plan, uploaded beside the run's archive dir
            data/<platform path>   <- every external input the pipeline reads
resources   max(nodeCount), max(coresPerNode), max(memoryMB) across steps
            sum(maxMinutes) across steps   (they run sequentially in this one job)
            -G max(gpus)  ONLY when some step actually wants a GPU
```

`--workdir output` is the job's own output directory, which Tapis archives — so
every step's job directory and the runner's `run-<id>-report.json` land in the
run's archive with nothing to copy. The platform reads that report back to fill in
each step's status and outputs on the run page, so a single job still shows
per-step detail rather than one opaque bar.

## Node requirements

`python3` (3.9+) and `apptainer` (or `singularity`) on `PATH` on the compute node.
Where a site exposes them through modules, set these on the app or the job:

| Variable | Effect |
|---|---|
| `WF_RUNNER_MODULES` | Space-separated modules to `module load` first. |
| `WF_RUNNER_PYTHON` | Python interpreter to use (default `python3`). |
| `WF_RUNNER_RUNTIME` | Container runtime (default `apptainer`; `singularity` works). |
