# Deploying a workflow to your own node

The platform normally executes a workflow by submitting each step as a Tapis
job on an HPC system. This is the other path: export the workflow as a file,
carry it to hardware you own, and run the whole DAG there with Apptainer — no
Tapis API involved at run time, nothing calling back to the platform.

Use it when the data can't leave your site, when you have your own GPU box, or
when you want a workflow to keep running somewhere you control.

```
   canvas                     bundle.json                  your node
┌────────────┐   export    ┌──────────────┐   carry     ┌──────────────────┐
│  workflow  │ ──────────► │ resolved DAG │ ──────────► │ apptainer run    │
│  template  │             │ + arguments  │             │   wf-runner.sif  │
└────────────┘             └──────────────┘             └──────────────────┘
```

## 1. Export the bundle

On the canvas, open a saved template and press **Deploy to my node**.

Set the three paths as they will be *on your node* (they're only defaults — the
runner's flags override them), then:

- **Check workflow** validates the export and reports what the node will need:
  which container images, which credentials, and any step that can't run
  locally. Read this before downloading.
- **Download bundle** saves `<name>-v<version>-bundle.json`.

The bundle is built from the **last saved version**, not the current canvas. If
you have unsaved changes, save a new version first.

### What's in it

A fully-resolved execution plan: every step's container image, command line,
bind mounts, staged inputs and output locations, with the data flow between
steps already worked out. Two things are left as tokens the runner fills in —
`{{DATA_ROOT}}` and `{{WORKDIR}}` — which is what makes one bundle reusable
across nodes.

**Credentials are never included.** A step needing an API token contributes
only the variable's *name* to `secrets_required`; you export the value on your
node. This is deliberate: a bundle is a file that gets downloaded, copied and
shared, so resolving secrets into it would leak them far more widely than the
platform ever does.

## 2. Prepare the node

**Apptainer** on `PATH`, plus:

### Container images

One directory holding the `.sif` for every image the bundle names. There are
three ways one gets there, and the runner tells you which applies:

1. **Downloaded automatically.** If `backend/image_sources.json` knows a URL
   for the image, the bundle carries it and the runner fetches it into
   `--images-dir` on the first run, then reuses that file forever after. The
   caching is the point: these are typically use-limited Tapis postit links,
   so redeeming once and keeping the `.sif` is the only thing that works
   long-term. `--no-download` opts out.
2. **Built from this repo**, for steps whose container is defined in `jobs/`:
   ```bash
   cd jobs/flight_plan_generator
   apptainer build generate-flight-plan.sif generate_flight_plan.def
   ```
3. **Supplied by you**, for steps backed by containers maintained elsewhere
   with no URL registered. Get the image from whoever owns the Tapis app and
   drop it in `--images-dir`.

Either way the filename must match the bundle's `image` field, which is named
after the step's Tapis app id. **Check workflow** in the export dialog lists
exactly which images a given workflow needs.

A few steps (`training`, `inference`, `preprocessing`) need nothing here at
all: they stage their real container as an input, and the Tapis app is only a
wrapper that runs it. The runner executes the staged `.sif` directly.

#### Registering a new image source

`backend/image_sources.json` maps Tapis app id → download URL:

```json
{ "images": { "few_shot_detection": "https://…/postits/redeem/…" } }
```

Set `WF_IMAGE_SOURCES` to a path outside the repo if you'd rather not keep
capability URLs in version control — anyone holding such a link can download
that file, and it is copied into every exported bundle.

### Data layout

Your input data must mirror the platform's paths beneath one root. A source
step reading `tapis://pitzer-tapis/users/you/farm/images` resolves, with
`--data-root /data`, to `/data/users/you/farm/images`.

The runner checks every input a step will actually consume *before* running
anything and lists all the missing ones at once, so a wrong `--data-root`
surfaces immediately rather than after the first hour-long step.

### Credentials

```bash
export HF_TOKEN=...      # whatever `secrets_required` lists
```

## 3. Run it

```bash
python3 -m wf_runner bundle.json \
    --data-root  /data \
    --workdir    /scratch/wf-local \
    --images-dir /data/images
```

or through the runner image:

```bash
apptainer build wf-runner.sif runner/wf_runner.def
apptainer run --bind /data:/data --bind /scratch:/scratch \
    wf-runner.sif bundle.json --data-root /data --workdir /scratch/wf-local --images-dir /data/images
```

Start with `--dry-run` to print the exact `apptainer` command for every step
without executing any of them.

> **Nested containers.** The runner starts a container per step, so running the
> runner *itself* inside Apptainer means nesting, which many hardened HPC
> kernels disallow. The runner is pure standard library precisely so it doesn't
> need its own image — if the nested call fails, run it directly with `python3`.
> See [runner/README.md](../runner/README.md).

## 4. Results

```
<workdir>/
  run-<run-id>-report.json            what ran, how long, where each output is
  wf_runs/<run-id>/<step>/<node>/
      input/ data/ ...                staged inputs
      output/                         the step's artifacts
      runner.log                      that step's stdout/stderr
```

The report is the local equivalent of the platform's run detail page — without
it, finding a step's outputs means guessing at the workspace layout.

## How it stays correct

The runner contains **no per-step knowledge**. Everything it does comes from
the step's own `step.json`, carried through the bundle:

| From step.json | Used for |
|---|---|
| `fileInputs[].targetPath` | where each input is staged in the job directory |
| `parameterSet.containerArgs` | the step's own bind mounts (`$PWD` → job directory) |
| `parameterSet.appArgs` | the command line |
| output ports' `output_path` | where each artifact lands, and so what the next step reads |

That is exactly Tapis's job-directory contract, and none of it is
Tapis-specific once a job directory exists. So adding or changing a step on the
platform changes local execution the same way, with no second implementation to
keep in sync — which is the main thing this design is protecting.

## Known limitations

- **Single node.** The DAG runs sequentially on one machine. Independent
  branches aren't parallelized or spread across a node group yet; the bundle
  format doesn't need to change to add that later.
- **Platform-only steps.** Steps that run inside the backend rather than in a
  container (`engine/inline_steps.py` — currently the annotation format
  adapter) can't execute locally. They're exported as `unsupported` with an
  explanation, and the runner refuses to start unless you pass
  `--skip-unsupported`.
- **Signed-URL inputs expire.** A couple of steps (`training`, `inference`)
  stage their own `.sif` from a platform-signed URL. The runner downloads it,
  but the link can expire — host that file yourself and re-export if so.
- **No resumption.** A failed run stops at the failing step; re-running starts
  over under a new run id, leaving the earlier run's outputs in place.
