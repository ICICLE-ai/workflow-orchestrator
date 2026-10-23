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

### Where it runs

The dialog starts with **Where will it run?**. Pick the Tapis system your node
belongs to and **Browse** lists its folders for the data root and work dir. For a
machine that isn't on Tapis, pick **Other (my own machine)** and give it a name.
Browsing is then off and you type the paths as they appear on that machine.
**My data is at the same paths as on the platform** is on by default, so each
input is read from its platform path. Turn it off to set a data root for a copy
of the data laid out the same way. A live preview shows where each input will be
read from: *same path*, or *mirrored* when it's placed beneath your data root. The dialog remembers your node and paths
in this browser.

### Data that's only on your node

A path field's system dropdown also offers **My node (not on Tapis)**, for data
that exists only on your own machine. The path is then stored as a plain path
on the node (no `tapis://` system), Browse is off, and the exported bundle reads
it exactly as typed, even with a data root set. The platform can't reach your
machine, so launching such a workflow on Tapis is refused and names the steps involved;
run it with Deploy to my node instead.

### Run instructions and the offline zip

Below the bundle buttons, **Run it on your node** offers three tabs. The
commands in them already use the paths you entered above:

- **Docker:** step by step, built around the deployment zip:
  1. **Generate curl command** mints a signed download link, so you can paste
     `curl -fL -o <name>-deploy.zip '<link>'` straight onto a cluster node. Or
     use **Download in browser** and copy the file over yourself. The link needs
     no login, expires after `WF_DEPLOY_LINK_TTL_SECONDS` (default 1 h), and
     anyone holding it can download the zip until then.
  2. `unzip <name>-deploy.zip && cd <name>`. The folder has `bundle.json` and
     every step image in `containers/`.
  3. `docker pull ghcr.io/icicle-ai/workflow-runner:v0.1.3`. The runner image is
     public, so there's nothing to build or log in to.
  4. The full `docker run` command. It mounts the current folder, your data root
     and your work dir, each at the same path inside the container, and points
     `--images-dir` at `containers/`. It runs `--privileged` because each step is
     an Apptainer container started inside the runner's container.
- **Apptainer:** the same zip, then `./run.sh --data-root … --workdir …` from
  inside it. This is the recommended route on HPC: the runner uses the node's own
  `python3` (3.9+) and starts each step with the node's own `apptainer`, so
  nothing is nested. Running the runner itself in Apptainer (`apptainer pull
  wf-runner.sif docker://…`) is offered as a fallback, but many HPC nodes
  refuse nested containers.

Both tabs mount (or bind) only the folders a step actually reads on the node.
A source wired to a port the node never uses, such as a step's platform
`output_path` archive folder, is left out: on the node every step writes under
the work dir. This matters for Apptainer, which won't start if a bind source
doesn't exist. Your node, data-root choice and work dir are saved per workflow,
for you, so they're filled in next time and carry over to new versions.
- **Offline zip:** one file with `bundle.json`, every step image the server
  can fetch (in `containers/`), the runner source, `run.sh` / `run-docker.sh` /
  `pull-images.sh`, and a README saying where each piece goes. It is streamed,
  because images make it gigabytes. Downloading it redeems each image's link
  once, so keep the zip rather than downloading it again.

The **Step container images** table lists each `.sif` the workflow needs. Its
image comes from the step's live Tapis app definition (`containerImage` +
`runtime`), so it is always what the platform actually runs. Below the table is
the command that fetches each image into your images dir. A Docker-runtime or
`docker://` app shows its Docker image name. Reading app definitions needs a
real Tapis login. Without one, the table falls back to
`backend/image_sources.json`.

The same data is available from `GET /api/workflow-templates/{id}/deployment` and
`GET /api/workflow-templates/{id}/deployment.zip`. Configure the published runner
images with `WF_RUNNER_DOCKER_IMAGE` / `WF_RUNNER_APPTAINER_IMAGE` (see
`backend/.env.example`).

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

Either way the filename should match the bundle's `image` field, which is named
after the step's Tapis app id. Matching ignores case and `-` vs `_`, so
`generate_flight_plan.sif` (what building `generate_flight_plan.def` produces)
satisfies a bundle asking for `generate-flight-plan.sif`; the runner logs which
file it used. **Check workflow** in the export dialog lists exactly which images
a given workflow needs.

A few steps (`training`, `inference`, `preprocessing`) need nothing here at
all: they stage their real container as an input, and the Tapis app is only a
wrapper that runs it. The runner executes the staged `.sif` directly.

#### Adding or updating an image link (developers)

All image download links live in one file, **`backend/image_sources.json`**.
To add a link, or replace one that has expired, edit its `images` map:

```json
{ "images": { "generate-flight-plan": "https://icicleai.tapis.io/v3/files/postits/redeem/…" } }
```

- **Key:** the step's Tapis app id (`tapis_app_id` in its `step.json`) or its
  step name. Case and `-`/`_` don't matter. Prefer the app id: steps that
  share an app (custom_shapefile and heatmap) are then covered by one entry.
- **No restart needed:** every export reads the file fresh.
- **Steps that ship their container as an input** (training, inference,
  preprocessing): a link here replaces the one in that step's `step.json` for
  deployments. Platform runs still use `step.json`'s link, so update both when
  it changes.
- **Check your edit:** `cd backend && uv run python scripts/check_image_sources.py`.
  It lists every step with the link it gets, flags keys that match no step (a
  typo would otherwise be ignored), and exits non-zero on one, so it can run in
  CI. It never downloads the links, since each download uses up one of a
  postit's allowed uses.
- **Allow it in the secret scan:** add the link's ID (the part after
  `/redeem/`) to the allowlist in `.gitleaks.toml`. The secret scan treats every
  Tapis postit link as a secret until it's listed there, and fails the PR
  otherwise.

Wherever a workflow includes one of these steps, its link appears in the Deploy
dialog's image table (with a copy button), in the zip's `README.md`, and in
`pull-images.sh`. The image itself goes into the zip's `containers/`.

Postit links are capability URLs: anyone holding one can download the file, and
they're copied into every exported bundle. Create them with high (or unlimited)
allowed uses and a long expiry. To keep them out of version control, set
`WF_IMAGE_SOURCES` to the path of a file in the same format outside the repo.

### Data layout

By default every input is read from exactly the path the platform uses
(`--data-root /`). A source step reading
`tapis://pitzer-tapis/fs/ess/PAS123/farm/images` is read from
`/fs/ess/PAS123/farm/images` on the node. That's right whenever the node sees the
same folders, e.g. a node on the same cluster.

If you've copied the data somewhere else, keep the platform's layout beneath one
folder and pass it as `--data-root`. With `--data-root /data`, the same source
is read from `/data/fs/ess/PAS123/farm/images`. A source whose platform path is
already under the data root is read as-is rather than nested a second time.

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
