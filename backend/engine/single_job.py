"""Submit a whole pipeline as ONE Tapis job.

WHY THIS IS A SEPARATE EXECUTION MODE
-------------------------------------
The normal path (engine/workflows.py) submits one Tapis job per node. That is
the right default — each step gets exactly the resources it asks for, steps with
no dependency on each other run concurrently, and a failure is isolated to one
job. It also means a five-step pipeline sits in the scheduler's queue five
times. On a busy allocation the queue wait dominates the actual compute, so a
pipeline that takes twenty minutes of GPU time can take a day to get through.

Single-job mode trades the per-step fit for ONE queue wait: the entire DAG runs
sequentially inside one allocation.

HOW ONE TAPIS JOB CAN RUN MANY STEPS
------------------------------------
A Tapis job runs exactly one app, and every step here has its own
(`yolo-inference`, `workflow-orchestrator-trainer-v2`, ...), so the pipeline
cannot be expressed as one job of those apps. What it CAN be is one job of an
app that executes step containers itself — which is precisely what
`runner/wf_runner` already does for the bundles engine/local_bundle.py exports
for self-hosted nodes.

So this module does not invent a second execution plan. It builds the SAME
bundle, and submits one Tapis job whose app is that runner:

    build_bundle()  ->  pipeline.json  ->  one Tapis job (app: wf-runner)
                                              |
                                              +-- step 1 container
                                              +-- step 2 container   (in the one
                                              +-- ...                 allocation)

The runner needs no Tapis knowledge: the three path tokens it substitutes
({{DATA_ROOT}}, {{WORKDIR}}, {{RUN_ID}}) are pointed at directories inside the
Tapis job directory, and Tapis' own fileInput staging puts the pipeline's
external inputs where those tokens resolve. See `runner/tapis-app/` for the app
definition to register, and HOW_TO_USE.md for the operator's side.

SIZING THE ONE JOB
------------------
One job means one resource request, so the per-step requests have to be merged
into an envelope big enough for every step (derive_resources):

  * nodeCount / coresPerNode / memoryMB / gpus -- the MAX across steps. The job
    must be able to host the hungriest step.
  * maxMinutes -- the SUM across steps. They run one after another inside this
    single allocation, so a wall clock of only the longest step would have the
    scheduler kill the job partway through the pipeline.
  * GPU resources at all -- only when some step actually wants one. A pipeline
    of CPU-only steps goes to the run's CPU target with no `-G` request, so it
    is never queued behind GPU availability it has no use for.

WHAT THIS MODE CANNOT DO
------------------------
Reported as `blockers` (refuse to launch) or `warnings` (launch, but say so):

  * an inline step (engine/inline_steps.py) runs Python inside THIS backend, so
    there is no container for the runner to execute -- a blocker, exactly as it
    is for a local bundle;
  * per-node exec-system pins are ignored, because there is one job and one
    system -- a warning naming each node that pinned one;
  * concurrency is gone. Independent branches that would have run side by side
    run in sequence, so wall-clock time is the sum, which is why maxMinutes is.
"""
from __future__ import annotations

import os

from sqlalchemy.orm import Session

from models import StepTypeRegistry, WfNode, WorkflowTemplate
from engine import local_bundle
from engine.transactions import _default_work_dir, _exec_system_dirs

# The Tapis app that executes a bundle. Not a container app: it must be able to
# launch apptainer itself for each step, so it is registered as a ZIP-runtime app
# whose tapisjob_app.sh runs wf_runner on the compute node (see
# runner/tapis-app/). Overridable because the app id is a per-tenant deployment
# detail, not a property of this code.
RUNNER_APP_ID = os.getenv("WF_RUNNER_APP_ID", "wf-runner")
RUNNER_APP_VERSION = os.getenv("WF_RUNNER_APP_VERSION", "1.0")

# Where the step .sif images live ON THE EXEC SYSTEM. When set, the job uses that
# shared directory and stages no images at all — the sane production setup, since
# a GB-scale .sif should be cached on the cluster rather than transferred per run.
# When unset, images with a known source URL (backend/image_sources.json) are
# staged into the job directory and anything else is reported as a blocker: the
# job would otherwise start and then fail on a missing container.
RUNNER_IMAGES_DIR = os.getenv("WF_RUNNER_IMAGES_DIR", "").rstrip("/")

# Layout inside the Tapis job directory. Relative paths, resolved by the runner
# against the job directory it is launched in (see runner/tapis-app/tapisjob_app.sh).
#
# WORK_SUBDIR *is* the job's output directory, not a directory beneath it: Tapis
# archives that directory's CONTENTS into archiveSystemDir, so pointing the runner
# straight at it means `{{WORKDIR}}/x/y` archives to `<archive_dir>/x/y` — every
# artifact's final location is then a pure substitution over its path in the plan
# (see archived_uri), with no listing of the archive and no extra nesting level in
# the paths the user sees.
DATA_SUBDIR = "data"
IMAGES_SUBDIR = "images"
WORK_SUBDIR = "output"
BUNDLE_FILENAME = "pipeline.json"

# Fields merged by taking the largest request across steps.
_MAX_FIELDS = ("nodeCount", "coresPerNode", "memoryMB")

# Fallbacks for a step whose step.json names no resources at all and whose node
# has no Run Configuration saved. Deliberately small: the envelope is a max, so
# an over-generous default here would silently inflate every pipeline.
_FALLBACK = {"nodeCount": 1, "coresPerNode": 1, "memoryMB": 1024, "maxMinutes": 30}


def _int_or_none(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _template_gpus(tapis_job: dict | None) -> int | None:
    """The GPU count a step.json's own job template requests, from its `-G n`
    schedulerOption — the only place Tapis expresses GPUs (there is no
    top-level field; see job_spec.apply_resource_overrides).
    """
    options = ((tapis_job or {}).get("parameterSet") or {}).get("schedulerOptions") or []
    for option in options:
        if not isinstance(option, dict):
            continue
        arg = str(option.get("arg", "")).strip()
        if arg.startswith("-G"):
            return _int_or_none(arg[2:].strip())
    return None


def step_request(registry: StepTypeRegistry, node_config: dict) -> dict:
    """What one step asks for, resolved exactly as its own per-step job would.

    Same precedence engine/workflows.py applies when it submits this step alone:
    the node's Run Configuration overrides (camelCase reserved keys in its
    config) win over the step.json job template, which wins over the fallbacks.
    So the envelope is built from the numbers the steps would really have used,
    not from a separate guess about them.

    `gpus` follows the same rule, with one addition: a step whose step.json
    declares `"resources": {"gpu": true}` but names no count is taken as
    wanting one GPU, because that declaration is what routes it to the run's GPU
    target in per-step mode (resolve_node_exec_target).
    """
    node_config = node_config or {}
    tapis_job = (registry.tapis_job if registry else None) or {}
    # `and` binds tighter than `or`, so the parentheses here are load bearing:
    # `resources or {} and resources.get("gpu")` evaluates to `resources or {}`,
    # which is truthy for ANY non-empty resources dict — including the
    # {"gpu": false} that most steps carry. That read every CPU step as a GPU
    # step and sent every pipeline to the GPU target.
    declared_gpu = bool(((registry.resources if registry else None) or {}).get("gpu"))

    request = {}
    for field in _MAX_FIELDS + ("maxMinutes",):
        value = _int_or_none(node_config.get(field))
        if value is None:
            value = _int_or_none(tapis_job.get(field))
        if value is None:
            value = _FALLBACK[field]
        request[field] = value

    # An explicit 0 from the node's Run Configuration is meaningful ("drop the
    # GPU request from this step"), so only fall through when the key is absent.
    gpus = _int_or_none(node_config.get("gpus"))
    if gpus is None and "gpus" not in node_config:
        gpus = _template_gpus(tapis_job)
    if gpus is None:
        gpus = 1 if declared_gpu else 0
    request["gpus"] = max(0, gpus)
    request["gpu_declared"] = declared_gpu
    return request


def derive_resources(steps: list[dict]) -> dict:
    """Merge the steps' requests into the one job's resource envelope.

    `steps` is [{"label", "request": <step_request()>}, ...] for the steps that
    actually submit a job — a source, sink or inline node asks for no compute and
    must not drag the envelope up (or, worse, its fallback floor).

    Returns the envelope plus a per-field `derivation` recording how each number
    was reached and which steps drove it, so the UI can show the arithmetic
    rather than a set of numbers the user has to take on faith.
    """
    envelope = {"nodeCount": 1, "coresPerNode": 1, "memoryMB": 1024, "maxMinutes": 0, "gpus": 0}
    derivation: dict[str, dict] = {}

    if not steps:
        return {**envelope, "needs_gpu": False, "derivation": {}, "step_count": 0}

    for field in _MAX_FIELDS + ("gpus",):
        values = {s["label"]: s["request"][field] for s in steps}
        top = max(values.values())
        envelope[field] = top
        derivation[field] = {
            "how": "max",
            "value": top,
            # Which steps are AT the maximum — the ones that would have to shrink
            # for the envelope to shrink.
            "driven_by": sorted(label for label, v in values.items() if v == top),
            "per_step": values,
        }

    minutes = {s["label"]: s["request"]["maxMinutes"] for s in steps}
    total = sum(minutes.values())
    envelope["maxMinutes"] = total
    derivation["maxMinutes"] = {
        # Summed, not maxed: the steps run one after another inside this single
        # allocation, so the job needs wall clock for all of them.
        "how": "sum",
        "value": total,
        "driven_by": sorted(minutes, key=lambda k: -minutes[k]),
        "per_step": minutes,
    }

    # Requirement: GPU resources only when some step actually wants one. Both
    # halves of "wanting one" count — a step.json declaring resources.gpu (which
    # is what routes it to the GPU target in per-step mode) and a non-zero GPU
    # count from either the template or the node's Run Configuration.
    gpu_steps = sorted(
        s["label"] for s in steps if s["request"]["gpus"] > 0 or s["request"]["gpu_declared"]
    )
    needs_gpu = bool(gpu_steps)
    if needs_gpu and envelope["gpus"] < 1:
        # A step declared a GPU requirement but every explicit count was 0 — ask
        # for one rather than sending a GPU-target job with no `-G` at all.
        envelope["gpus"] = 1
        derivation["gpus"]["value"] = 1
        derivation["gpus"]["how"] = "max (floored to 1: a step declares a GPU requirement)"

    return {
        **envelope,
        "needs_gpu": needs_gpu,
        "gpu_steps": gpu_steps,
        "derivation": derivation,
        "step_count": len(steps),
    }


def resolve_exec_target(cfg: dict, needs_gpu: bool) -> dict:
    """Pick the ONE exec system/queue the merged job runs on.

    The pipeline-wide counterpart of transactions.resolve_node_exec_target: the
    run's GPU pair when any step wants a GPU, else its CPU pair. There is no
    per-node layer here — one job has one system — which is why a node that
    pinned its own is reported as a warning by plan().
    """
    cpu_system = cfg.get("exec_system") or "expanse-tapis"
    cpu_queue = cfg.get("exec_queue") or ""
    if needs_gpu:
        return {
            "exec_system": cfg.get("gpu_exec_system") or cpu_system,
            "exec_queue": cfg.get("gpu_exec_queue") or cpu_queue,
            "target": "gpu",
        }
    return {"exec_system": cpu_system, "exec_queue": cpu_queue, "target": "cpu"}


def _workspace(cfg: dict, run_id, archive_system: str) -> str:
    """The run's archive workspace — the same formula get_run_archive_context
    uses, minus the per-node suffix (there is one job, so one archive dir).

    Duplicated as a read of `cfg` rather than called through
    get_run_archive_context because that helper is a DBOS transaction keyed on an
    existing run, and this also has to answer for a PREVIEW, where no run exists
    yet. `run_id` is then None and the caller gets a display placeholder.
    """
    slurm_account = cfg.get("slurm_account", "")
    username = cfg.get("tapis_username", "")
    override = cfg.get("archive_dir", "")
    work_dir = cfg.get("work_dir", "") or _default_work_dir(archive_system, slurm_account, username)
    if override:
        base = override.rstrip("/")
    elif work_dir:
        base = work_dir.rstrip("/") + "/wf_runs"
    else:
        base = "wf_runs"
    return f"{base}/{run_id if run_id is not None else '<run_id>'}"


def _strip_scheme(uri: str) -> str:
    """'tapis://sys/a/b' -> 'a/b'; a bare path -> 'a/b'. Mirrors
    local_bundle._strip_uri, whose output is what the {{DATA_ROOT}} paths in the
    bundle were built from — so re-deriving the same relative path here is what
    makes a staged input land exactly where the plan expects to read it."""
    return local_bundle._strip_uri(uri)


def _stage_inputs(bundle: dict) -> tuple[list[dict], list[str]]:
    """Every external location the pipeline reads, as Tapis fileInputs.

    The bundle expresses external data as `{{DATA_ROOT}}/<platform path>` and the
    job points the runner's --data-root at the job's own `data/` directory. So
    staging `tapis://sys/<platform path>` to `data/<platform path>` makes the
    token resolve to the staged copy with no path rewriting at all — the
    substitution the runner already performs does the work.

    Returns (fileInputs, warnings). Deduplicated: two steps reading the same
    source directory stage it once.
    """
    inputs: dict[str, dict] = {}
    warnings: list[str] = []

    def add(platform_uri: str, label: str, name: str) -> None:
        uri = (platform_uri or "").strip()
        if not uri:
            return
        if uri.startswith(("http://", "https://")):
            # A real remote resource the plan fetches itself (a step shipping its
            # own .sif from a postit URL). The runner downloads it on the node;
            # Tapis need not stage it.
            return
        relative = _strip_scheme(uri)
        if not relative or relative.startswith("{{"):
            return
        target = f"{DATA_SUBDIR}/{relative}"
        if target in inputs:
            return
        if "://" not in uri:
            # A bare path with no system anywhere — we cannot name a sourceUrl
            # for Tapis to fetch. Warn instead of emitting a broken fileInput.
            warnings.append(
                f"{label}: input {name!r} is the bare path {uri!r} with no Tapis system, "
                f"so it cannot be staged into the single job. Set the step's system, "
                f"or use a full tapis:// URI."
            )
            return
        inputs[target] = {
            "name": f"in-{len(inputs)}",
            "sourceUrl": uri,
            "targetPath": target,
            "autoMountLocal": True,
        }

    for node in bundle.get("nodes", []):
        label = node.get("label") or node.get("node_id")
        if node.get("kind") == "source":
            add(node.get("platform_path", ""), label, "path")
        for item in node.get("stage_in") or []:
            # Only inputs that point OUTSIDE the run need staging; anything
            # produced by an upstream step already lives in the work tree.
            if str(item.get("source", "")).startswith(f"{local_bundle.WORKDIR}"):
                continue
            add(item.get("platform_source", ""), label, item.get("name") or "input")

    return list(inputs.values()), warnings


def _image_inputs(bundle: dict) -> tuple[list[dict], list[str], list[str]]:
    """The step container images the job needs.

    Two arrangements, because a .sif is large and a cluster is not a laptop:

    * WF_RUNNER_IMAGES_DIR set — the images are already on the exec system in a
      shared directory. Nothing is staged; the runner is pointed at it. This is
      the arrangement a real deployment should use.
    * unset — each image with a known source URL (backend/image_sources.json) is
      staged into the job's images/ directory by Tapis. An image with no known
      source is a BLOCKER: the job would start, run for a while, and then fail on
      a container that was never there.

    Returns (fileInputs, blockers, warnings).
    """
    images = bundle.get("images") or []
    sources = bundle.get("image_sources") or {}
    if RUNNER_IMAGES_DIR:
        return [], [], []

    inputs, blockers = [], []
    for name in images:
        url = sources.get(name)
        if not url:
            blockers.append(
                f"Container image {name!r} has no known download source, and this backend has no "
                f"shared image directory configured. Place the .sif on the exec system and set "
                f"WF_RUNNER_IMAGES_DIR, or add a source for it to backend/image_sources.json."
            )
            continue
        inputs.append({
            "name": f"image-{name}",
            "sourceUrl": url,
            "targetPath": f"{IMAGES_SUBDIR}/{name}",
        })
    warnings = []
    if inputs:
        warnings.append(
            f"{len(inputs)} container image(s) are staged into the job on every run. Cache them on "
            f"the exec system and set WF_RUNNER_IMAGES_DIR to skip the transfer."
        )
    return inputs, blockers, warnings


def archived_uri(archive_system: str, archive_dir: str, plan_path: str, run_id) -> str:
    """Where a path in the PLAN ends up once the job has archived.

    The runner writes everything beneath its --workdir, which this mode points at
    the job's own output directory; Tapis archives that directory's contents into
    archiveSystemDir. So `{{WORKDIR}}/wf_runs/{{RUN_ID}}/x/y` lands at
    `<archive_dir>/wf_runs/<run_id>/x/y`.

    Both of the runner's path tokens have to be substituted, not just the workdir
    one: the plan is written in tokens precisely so the runner can place it, and a
    `{{RUN_ID}}` left standing in a recorded output URI is a path that exists
    nowhere — the downstream consumer of that URI (a sink copy, the run page's
    artifact links) would look for a directory literally named '{{RUN_ID}}'.

    Accepts BOTH shapes a path can arrive in, because two different callers need
    this translation:

      * `{{WORKDIR}}/wf_runs/{{RUN_ID}}/...` — straight from the plan;
      * `/<node's job dir>/output/wf_runs/<run_id>/...` — from the runner's own
        report, where the tokens are already resolved to absolute paths on the
        compute node. Those paths mean nothing on the archive system, so the
        run-scope marker is what the tail is recovered from. Without this a
        reported output would be recorded as
        `<archive_dir>/fs/scratch/.../harvest_jobs/<uuid>/output/wf_runs/...` —
        a URI pointing nowhere, for the ports whose real filename only the
        report knows (see StepTypePort.file_glob).
    """
    path = str(plan_path or "")
    if not path:
        return ""
    tail = path.replace(local_bundle.WORKDIR + "/", "").replace(local_bundle.WORKDIR, "")
    tail = tail.replace(local_bundle.RUN_ID, str(run_id)).lstrip("/")

    marker = f"wf_runs/{run_id}/"
    if marker in tail:
        # rindex, not index: a job directory could itself sit beneath a wf_runs
        # path, and the LAST occurrence is the one that starts the artifact's
        # own tail. A plan path has exactly one and is unaffected.
        tail = tail[tail.rindex(marker):]

    # Both sides scope by run — the archive dir ends with wf_runs/<run_id> (the
    # run's workspace, shared with per-step mode) and the bundle's own workspace
    # is {{WORKDIR}}/wf_runs/{{RUN_ID}} (so an exported bundle keeps its runs
    # apart on the user's node). Concatenating them yields
    # '.../wf_runs/7/wf_runs/7/...', which is merely deterministic rather than
    # wrong, but it is a path people read in the UI and copy into Tapis. One
    # level of it is redundant, so drop it.
    run_scope = f"wf_runs/{run_id}"
    if archive_dir.rstrip("/").endswith(run_scope) and tail.startswith(run_scope + "/"):
        tail = tail[len(run_scope) + 1:]

    base = f"{archive_dir.rstrip('/')}/{tail}" if tail else archive_dir
    return f"tapis://{archive_system}/{base.lstrip('/')}"


def plan(db: Session, template: WorkflowTemplate, cfg: dict, *, run_id=None) -> dict:
    """Everything needed to run one template version as a single Tapis job.

    Pure computation against the database and the run's config — no Tapis calls,
    no writes — so the SAME function answers the pre-launch preview
    (run_id=None) and builds the spec a launch freezes onto the run. A preview
    that came from different code than the launch would be worth very little.

    Returns a dict carrying `job` (the Tapis job spec), `bundle` (the plan the
    runner executes), `resources` (the envelope + its derivation), `exec`,
    `sinks`, `warnings` and `blockers`. A non-empty `blockers` means this
    pipeline must not be launched in single-job mode; the caller reports them.
    """
    bundle = local_bundle.build_bundle(
        db, template,
        # The runner's three tokens are pointed inside the Tapis job directory.
        # Passing them here only sets the bundle's recorded DEFAULTS; the job's
        # appArgs pass them explicitly anyway, so the two cannot drift.
        data_root=DATA_SUBDIR, workdir=WORK_SUBDIR, images_dir=IMAGES_SUBDIR,
    )

    nodes = db.query(WfNode).filter(WfNode.template_version_id == template.template_version_id).all()
    nodes_by_key = {str(n.node_id): n for n in nodes}
    registries: dict[str, StepTypeRegistry] = {}
    for node in nodes:
        if node.step_type_key not in registries:
            registries[node.step_type_key] = db.query(StepTypeRegistry).filter(
                StepTypeRegistry.step_type_key == node.step_type_key
            ).first()

    blockers: list[str] = []
    warnings: list[str] = []
    steps: list[dict] = []
    pinned: list[str] = []

    for entry in bundle.get("nodes", []):
        node_key = entry["node_id"]
        node = nodes_by_key.get(node_key)
        label = entry.get("label") or node_key
        if entry.get("kind") == "unsupported":
            blockers.append(
                f"'{label}' runs inside the platform rather than in a container "
                f"({entry.get('reason')}), so it cannot run inside a single Tapis job. "
                f"Run this workflow per step, or replace that step."
            )
            continue
        if entry.get("kind") != "job":
            continue
        registry = registries.get(entry.get("step_type"))
        node_config = (node.default_config if node is not None else {}) or {}
        steps.append({
            "node_key": node_key,
            "label": label,
            "step_type": entry.get("step_type"),
            "request": step_request(registry, node_config),
        })
        if node_config.get("execSystem"):
            pinned.append(f"{label} -> {node_config['execSystem']}")

    if not steps and not blockers:
        blockers.append(
            "This workflow has no steps that submit a job, so there is nothing for a single "
            "Tapis job to run. Run it per step — its sources, sinks and panels execute in the backend."
        )

    resources = derive_resources(steps)
    exec_target = resolve_exec_target(cfg, resources["needs_gpu"])
    archive_system = cfg.get("archive_system") or cfg.get("exec_system") or exec_target["exec_system"]
    archive_dir = _workspace(cfg, run_id, archive_system)
    slurm_account = cfg.get("slurm_account", "")
    username = cfg.get("tapis_username", "")

    if pinned:
        warnings.append(
            "A single job has one exec system, so these per-step system pins are ignored: "
            + "; ".join(pinned)
            + f". Everything runs on {exec_target['exec_system']}."
        )
    if resources["step_count"] > 1:
        warnings.append(
            f"{resources['step_count']} steps run one after another in this job, so independent "
            f"branches no longer overlap. Wall clock is the sum of the steps, which is why the "
            f"runtime request is {resources['maxMinutes']} minutes."
        )
    # Bundle warnings are about the PLAN and apply just as much here (an unwired
    # optional input, a site path bound by a step.json, ...).
    warnings.extend(bundle.get("warnings") or [])

    stage_in, stage_warnings = _stage_inputs(bundle)
    warnings.extend(stage_warnings)
    image_inputs, image_blockers, image_warnings = _image_inputs(bundle)
    blockers.extend(image_blockers)
    warnings.extend(image_warnings)

    images_dir = RUNNER_IMAGES_DIR or IMAGES_SUBDIR

    scheduler_options = []
    if slurm_account:
        scheduler_options.append({"name": "slurm account", "arg": f"-A {slurm_account}"})
    if resources["needs_gpu"] and resources["gpus"] > 0:
        # Requirement: the `-G` request exists ONLY when a step wants a GPU, so a
        # CPU-only pipeline is never queued behind GPU availability.
        scheduler_options.append({"name": "gpu_per_node", "arg": f"-G {resources['gpus']}"})

    job = {
        "name": f"wf-pipeline-{run_id if run_id is not None else 'preview'}",
        "appId": RUNNER_APP_ID,
        "appVersion": RUNNER_APP_VERSION,
        "description": (
            f"{template.name} v{template.version} — {resources['step_count']} step(s) "
            f"as a single job"
        ),
        "execSystemId": exec_target["exec_system"],
        "archiveSystemId": archive_system,
        "archiveSystemDir": archive_dir,
        "nodeCount": resources["nodeCount"],
        "coresPerNode": resources["coresPerNode"],
        "memoryMB": resources["memoryMB"],
        "maxMinutes": resources["maxMinutes"],
        "fileInputs": [
            # The plan itself. Uploaded next to the run's archive dir and staged
            # here, so the job carries the exact graph that was frozen onto it.
            {"name": "pipeline", "sourceUrl": "${pipeline_uri}", "targetPath": BUNDLE_FILENAME},
            *stage_in,
            *image_inputs,
        ],
        "parameterSet": {
            "appArgs": [
                {"name": "bundle", "arg": BUNDLE_FILENAME},
                {"name": "data-root", "arg": f"--data-root {DATA_SUBDIR}"},
                {"name": "workdir", "arg": f"--workdir {WORK_SUBDIR}"},
                {"name": "images-dir", "arg": f"--images-dir {images_dir}"},
                {"name": "run-id", "arg": f"--run-id {run_id if run_id is not None else 'preview'}"},
            ],
            "schedulerOptions": scheduler_options,
            # Secrets the plan needs by NAME (the bundle never carries values).
            # Filled in at submit time from the run owner's team secrets — see
            # workflows._single_job_env.
            "envVariables": [
                {"key": key, "value": "${secrets." + key + "}"}
                for key in bundle.get("secrets_required") or []
            ],
            "archiveFilter": {"includeLaunchFiles": False},
        },
    }
    if exec_target["exec_queue"]:
        job["execSystemLogicalQueue"] = exec_target["exec_queue"]
    exec_dir, input_dir, output_dir = _exec_system_dirs(exec_target["exec_system"], slurm_account, username)
    for field, value in (("execSystemExecDir", exec_dir),
                         ("execSystemInputDir", input_dir),
                         ("execSystemOutputDir", output_dir)):
        if value:
            job[field] = value

    # Sinks: off platform the data root IS where the user wanted the artifact, so
    # the runner's local copy finishes the job. Here it only reaches the job
    # directory, so the real copy happens after the job archives — see
    # workflows._complete_single_job.
    sinks = [
        {
            "node_key": n["node_id"],
            "label": n.get("label"),
            "source": n.get("source"),
            "dest": n.get("platform_dest") or "",
        }
        for n in bundle.get("nodes", [])
        if n.get("kind") == "sink" and n.get("platform_dest")
    ]

    return {
        "mode": "single_job",
        "app": {"id": RUNNER_APP_ID, "version": RUNNER_APP_VERSION},
        "exec": {
            **exec_target,
            "archive_system": archive_system,
            "archive_dir": archive_dir,
            "archive_uri": f"tapis://{archive_system}/{archive_dir.lstrip('/')}",
            "images_dir": images_dir,
            "images_staged": bool(image_inputs),
        },
        "resources": resources,
        "steps": steps,
        "sinks": sinks,
        "bundle": bundle,
        "job": job,
        # Token the engine replaces once it has uploaded the plan (it needs the
        # run owner's Tapis credential, which planning deliberately does not).
        "pipeline_path": f"{archive_dir.rstrip('/')}/{BUNDLE_FILENAME}",
        "warnings": warnings,
        "blockers": blockers,
    }
