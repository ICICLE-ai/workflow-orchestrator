# wf-runner — run a workflow on your own node

Executes a workflow bundle exported from the platform's canvas ("Deploy to my
node") on hardware you own. No Tapis API, no scheduler, no call back to the
platform at run time.

| | |
|---|---|
| `wf_runner/` | The orchestrator. Pure Python standard library — no pip install. |
| `wf_runner.def` | Apptainer definition for the runner image. |

The bundle format, and how it's derived from the same step definitions the
platform executes, is documented in
[`backend/engine/local_bundle.py`](../backend/engine/local_bundle.py). The
end-to-end workflow is in [`docs/local-deployment.md`](../docs/local-deployment.md).

## What you need on the node

1. **Apptainer** (or Singularity) on `PATH`.
2. **The step container images** (`.sif`) the bundle names, in one directory —
   though the runner fills that in for you where it can. On the first run it
   downloads any image the bundle has a URL for into `--images-dir` and reuses
   that file on every later run (these are use-limited links, so redeeming once
   and keeping the `.sif` is what makes repeat runs work); `--no-download` opts
   out. Build the ones defined in this repo from `jobs/*/*.def`:
   ```bash
   cd jobs/flight_plan_generator && apptainer build generate-flight-plan.sif generate_flight_plan.def
   ```
   The `.sif` filename should match the bundle's `image` field, which is named
   after the step's Tapis app id — though matching ignores case and `-`/`_`, so
   the `generate_flight_plan.sif` you get from building `generate_flight_plan.def`
   satisfies a bundle asking for `generate-flight-plan.sif`. Steps backed by container definitions that
   live outside this repo, with no URL registered in
   `backend/image_sources.json`, have to be obtained from whoever maintains
   them. `training`, `inference` and `preprocessing` need nothing here: they
   stage their real container as an input and the runner runs that directly.
3. **Your input data**, laid out beneath one directory so it mirrors the paths
   the workflow used on the platform. If a source step read
   `tapis://pitzer-tapis/users/you/farm/images`, then with `--data-root /data`
   the runner looks in `/data/users/you/farm/images`.
4. **Any credentials the workflow needs**, exported as environment variables.
   The bundle names them but never contains their values.

## Running it

Directly with Python (recommended — see the nesting note below):

```bash
python3 -m wf_runner bundle.json \
    --data-root  /data \
    --workdir    /scratch/wf-local \
    --images-dir /data/images
```

Or via the runner image:

```bash
apptainer build wf-runner.sif wf_runner.def

apptainer run --bind /data:/data --bind /scratch:/scratch \
    wf-runner.sif bundle.json \
        --data-root /data --workdir /scratch/wf-local --images-dir /data/images
```

Useful flags:

| Flag | |
|---|---|
| `--dry-run` | Print the plan and the exact `apptainer` command for every step, run nothing. |
| `--run-id LABEL` | Name this run's workspace (default: a timestamp). |
| `--copy-inputs` | Copy staged inputs instead of symlinking them. |
| `--skip-unsupported` | Continue past steps that can only run on the platform. |
| `--runtime PATH` | Use a different container runtime binary (e.g. `singularity`). |

`--data-root`, `--workdir` and `--images-dir` also read from `WF_DATA_ROOT`,
`WF_WORKDIR` and `WF_IMAGES_DIR`, and fall back to the defaults recorded in the
bundle — so a bundle exported with paths that already match your node runs with
no flags at all.

## A note on nested containers

The runner invokes `apptainer` once per step. Running the runner *itself* with
`apptainer run` therefore asks your node to start a container from inside a
container, which works only where unprivileged user namespaces are enabled and
is disabled on many hardened HPC kernels.

The runner is deliberately pure standard library so it never needs its own
image: if the nested call fails, run `python3 -m wf_runner ...` on the host
instead. Both paths execute identical code.

## What it does per step

For each node, in dependency order:

1. Create the job directory `<workdir>/wf_runs/<run-id>/<step_type>/<node_id>/`.
2. Write any file the platform would have materialized from node config (e.g.
   the preprocess studio's `operations.json`).
3. Stage each declared input to its `targetPath` inside that directory —
   symlinked by default, so a large image directory isn't copied.
4. Run the step's container with its own declared bind mounts, `$PWD` resolved
   to the job directory, and the data root and workspace bound at their own
   absolute paths (which is what makes those symlinks resolve inside the
   container).
5. Collect outputs from `<jobdir>/output`; the next step's inputs point there.

Nothing in that is step-specific — every detail comes from the step's own
definition, carried in the bundle. Per-step output goes to `<jobdir>/runner.log`,
and a summary of the whole run to `<workdir>/run-<run-id>-report.json`.

## Limitations

- **Single node.** The DAG runs sequentially on the machine you start it on.
  Independent branches are not yet parallelized or distributed.
- **Platform-only steps.** Steps that do their work inside the platform backend
  rather than in a container (see `engine/inline_steps.py`) cannot run here.
  They're exported as `unsupported` and the runner refuses to start unless you
  pass `--skip-unsupported`.
- **No resumption.** A failed run stops at the failing step; re-running starts
  from the beginning, into a new run id. The completed steps' outputs are still
  on disk under the previous run id.
