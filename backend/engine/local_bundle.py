"""Export a saved template as a self-contained bundle that runs OFF-platform.

The normal execution path (engine/workflows.py) submits each node as a Tapis
job and threads `tapis://system/path` URIs between ports. This module produces
the same DAG as a *fully-resolved local execution plan* instead, so a user can
run the identical workflow on hardware they own — no Tapis API, no scheduler,
no network call back to this platform. The plan is consumed by the runner in
`runner/` (shipped as an Apptainer image); see docs/local-deployment.md.

WHY THIS CAN BE A STATIC PLAN
-----------------------------
Every executable step.json already self-describes how its container is invoked:

    fileInputs[].targetPath   where an input is staged, relative to the job dir
    containerArgs             its own bind mounts, written against $PWD
                              (e.g. "--bind $PWD:/job", "--bind $PWD/input:/input")
    appArgs                   the command line, written against the CONTAINER-side
                              paths those binds create (e.g. "--images /job/data/images")
    output ports' output_path the artifact's subpath within the job's output dir

That is precisely Tapis's job-directory contract, and nothing in it is
Tapis-specific once a job directory exists: give the step a directory, stage
its inputs to the declared targetPaths, run the container with the declared
binds and `$PWD` pointing at that directory, and collect outputs from
`<jobdir>/output`. So the runner needs no per-step knowledge at all — which
is what keeps this from becoming a second, drifting copy of the step registry.

Because the workspace layout is deterministic, every port's location is known
at EXPORT time, so the bundle ships with the data flow already resolved and
the runner stays a dumb executor. Only two things aren't knowable here, and
they are left as tokens the runner substitutes:

    {{DATA_ROOT}}   where the user's own input data lives on their node
    {{WORKDIR}}     where this run's working/output tree should be written
    {{RUN_ID}}      picked per invocation, so repeat runs don't collide

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
- It does not build or fetch container images. A node names the `.sif` it
  needs; obtaining those is the operator's job (see `image` per node and the
  bundle's `images` summary). Many step types in this repo are backed by
  external Tapis apps whose container definitions do not live here at all.
- It does not run the platform's inline steps (engine/inline_steps.py). Those
  execute Python inside the backend during a run, not in a container, so a
  node that needs one is exported with kind "unsupported" and an explanatory
  warning rather than silently producing a plan that would skip real work.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import StepTypeRegistry, StepTypePort, WfEdge, WfNode, WorkflowTemplate
from engine import inline_steps, job_spec

# Tokens the runner substitutes. Kept as literal braces (not ${...}) so they
# can never collide with job_spec's own placeholder syntax during render().
DATA_ROOT = "{{DATA_ROOT}}"
WORKDIR = "{{WORKDIR}}"
RUN_ID = "{{RUN_ID}}"

BUNDLE_VERSION = 1

_UNRESOLVED = re.compile(r"\$\{[a-zA-Z0-9_]+\}")
# A step.json can hardcode a team secret as ${secrets.KEY} (job_spec.resolve_secret_refs
# swaps in the real value at submit time). A bundle is a FILE the user downloads
# and copies onto their own hardware, so the value must never be resolved into
# it — the reference is rewritten to a token the runner fills from the node's
# own environment instead. See _rewrite_secrets.
_SECRET_REF = re.compile(r"\$\{secrets\.([A-Za-z0-9_]+)\}")
_PLACEHOLDER_KEY = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")
# A plain http(s) source is NOT a platform path and must survive untouched —
# `training` and `inference` stage their own .sif from a signed URL, which
# re-rooting under the data root would silently turn into a nonexistent file.
_REMOTE_URL = re.compile(r"^https?://", re.IGNORECASE)


def _strip_uri(value: str) -> str:
    """'tapis://system/abs/path' -> 'abs/path'; a bare path -> 'path'.

    A source node's configured location is a Tapis URI (or a bare path plus a
    separately-chosen system — see _source_node_outputs in workflows.py). Off
    platform neither the scheme nor the system means anything, so the PATH is
    kept and re-rooted under {{DATA_ROOT}}: the user's local layout mirrors the
    platform's beneath one directory they choose, which is the whole premise of
    the single-data-root model.
    """
    if "://" in value:
        value = value.split("://", 1)[1]
        # Drop the system id, keeping the path that followed it.
        _, _, value = value.partition("/")
    return value.lstrip("/")


def _data_root_path(value: str) -> str:
    """Re-root a platform path under {{DATA_ROOT}}.

    Passed through untouched: a token-rooted path (already resolved, e.g. an
    upstream node's output) and an http(s) URL (a real remote resource the
    runner fetches, not a location on the user's disk).
    """
    if not value:
        return ""
    if _REMOTE_URL.match(value):
        return value
    # Strip the scheme BEFORE the token check: a step.json that writes
    # "tapis://${system}/${port}" around an already-resolved value hands us
    # 'tapis://sys/{{DATA_ROOT}}/...', which would otherwise get a second data
    # root prefixed onto it.
    stripped = _strip_uri(value)
    if stripped.startswith("{{"):
        return stripped
    return f"{DATA_ROOT}/{stripped}"


def _placeholder_keys(template) -> set[str]:
    """Every ${key} referenced anywhere in a job template's string leaves."""
    found: set[str] = set()
    if isinstance(template, str):
        found.update(_PLACEHOLDER_KEY.findall(template))
    elif isinstance(template, dict):
        for value in template.values():
            found |= _placeholder_keys(value)
    elif isinstance(template, list):
        for item in template:
            found |= _placeholder_keys(item)
    return found


def _warn_on_empty_placeholders(template: dict, ctx: dict, label: str, warnings: list[str]) -> None:
    """Flag placeholders that resolve to an EMPTY string.

    These are worse than an unresolved one, because they vanish silently: an
    unwired `${dataset}` in "--data ${dataset}" renders to "--data " and then
    shell-splits to a bare ["--data"], so the flag swallows whatever argument
    happens to follow it and the step fails somewhere far from the cause. The
    platform has the identical behaviour, so this is reported rather than
    "fixed" here — but it is always worth knowing before a long run starts.

    Scoped to the parts of the template the local run actually uses. Scheduler
    options are full of ${slurm_account} / ${exec_queue}, which are MEANT to be
    empty off-platform (there is no scheduler) — warning about those would bury
    the real ones in noise.
    """
    param_set = template.get("parameterSet") or {}

    def kept(items):
        """Only the items render() will actually emit.

        An item carrying "if": "<key>" is dropped wholesale when that key is
        falsy — which is exactly how a step declares an optional argument
        (zero_shot_annotation's --model_id, geospatial's spray levels). Scanning
        those would warn that an argument "collapses to a bare flag" when in
        truth it never appears at all, sending people looking for a problem
        that isn't there.
        """
        return [
            item for item in (items or [])
            if not (isinstance(item, dict) and "if" in item and not ctx.get(item["if"]))
        ]

    # fileInputs are deliberately excluded: an empty sourceUrl there is an
    # unwired optional input, which is dropped from staging with its own
    # explicit warning (or supplied by _CONFIG_FILES), so flagging it again
    # here just contradicts that message.
    relevant = [
        kept(param_set.get("appArgs")),
        kept(param_set.get("containerArgs")),
        kept(param_set.get("envVariables")),
    ]
    for key in sorted(_placeholder_keys(relevant)):
        if key in ctx and isinstance(ctx[key], str) and not ctx[key].strip():
            warnings.append(
                f"{label}: '{key}' is empty, so the argument using it collapses to a bare flag. "
                f"Wire that input (or set the value) before running."
            )


# Local counterpart to engine.inline_steps.PRE_SUBMIT_HANDLERS.
#
# Those handlers exist because some steps stage a file the USER never uploads:
# the panel edits it live and keeps it on the node's config, and the backend
# materializes it to Tapis just before submitting. Off platform there is no
# Tapis to write it to, but the need is identical, so the bundle carries the
# config value and the runner drops it into the job directory as a file.
#
# Keyed by step type -> (config key holding the content, filename the step's
# fileInput declares as its targetPath).
_CONFIG_FILES: dict[str, tuple[str, str]] = {
    "image-preprocess-studio": ("operations", "operations.json"),
}


def _rewrite_secrets(value, required: set[str]):
    """Turn ${secrets.KEY} into {{SECRET:KEY}} throughout a rendered spec.

    Deliberately NOT resolved here. The platform resolves a secret only into
    the job spec it hands Tapis, and never persists the value; a bundle is a
    file the user downloads, mails around and copies onto their own nodes, so
    writing real credentials into it would be a far worse leak than anything
    the platform does today. The runner reads each KEY from its own
    environment, which also means one bundle is safely shareable between
    people who each hold their own credentials.
    """
    if isinstance(value, str):
        def swap(match):
            required.add(match.group(1))
            return "{{SECRET:" + match.group(1) + "}}"
        return _SECRET_REF.sub(swap, value)
    if isinstance(value, list):
        return [_rewrite_secrets(v, required) for v in value]
    if isinstance(value, dict):
        return {k: _rewrite_secrets(v, required) for k, v in value.items()}
    return value


def _topological_order(node_ids: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm. Raises on a cycle rather than exporting a plan that
    would deadlock on the user's node with no explanation."""
    remaining = {n: set(d) for n, d in deps.items()}
    order: list[str] = []
    ready = sorted([n for n in node_ids if not remaining[n]])
    while ready:
        node = ready.pop(0)
        order.append(node)
        for other, ds in remaining.items():
            if node in ds:
                ds.discard(node)
                if not ds and other not in order and other not in ready:
                    ready.append(other)
        ready.sort()
    if len(order) != len(node_ids):
        stuck = sorted(set(node_ids) - set(order))
        raise ValueError(f"Workflow has a cycle; these nodes can never run: {', '.join(stuck)}")
    return order


def _ports(db: Session, step_type_key: str, direction: str) -> list[dict]:
    rows = (
        db.query(StepTypePort)
        .filter(StepTypePort.step_type_key == step_type_key, StepTypePort.direction == direction)
        .all()
    )
    return [
        {"name": p.port_name, "data_type": p.data_type, "output_path": p.output_path, "file_glob": p.file_glob}
        for p in rows
    ]


def _schema_defaults(schema: dict | None) -> dict:
    out = {}
    for key, field in (schema or {}).items():
        if isinstance(field, dict) and field.get("default") is not None:
            out[key] = field["default"]
    return out


def _load_image_sources() -> dict[str, str]:
    """Download URL per Tapis app id, from backend/image_sources.json.

    Steps whose container isn't built from this repo's jobs/*.def have to get
    it from somewhere; this is that somewhere. Missing or unreadable is not an
    error — the bundle then simply names the images and the operator supplies
    the files themselves, which is the pre-existing behaviour.

    WF_IMAGE_SOURCES points at an alternate file, for deployments that would
    rather not keep capability URLs in the repo.
    """
    path = os.environ.get("WF_IMAGE_SOURCES") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "image_sources.json"
    )
    try:
        with open(path) as f:
            return {str(k): str(v) for k, v in (json.load(f).get("images") or {}).items()}
    except (OSError, ValueError, AttributeError) as e:
        print(f"[local_bundle] no image sources loaded from {path}: {type(e).__name__}")
        return {}


def _sif_name(app_id: str | None, step_type_key: str) -> str:
    """Container filename a node expects to find in the images directory.

    Named after the Tapis app id (the identity that actually determines which
    container runs — two step types can share one app, as heatmap and
    custom_shapefile do) and falling back to the step type when a step has no
    app id at all.
    """
    base = app_id or step_type_key
    return f"{base}.sif"


def _split_args(rendered_args: list, warnings: list[str], node_label: str) -> list[str]:
    """Flatten Tapis appArgs/containerArgs into a real argv.

    Each entry's `arg` is a command-line FRAGMENT, not one argument: steps
    variously write "--output /job/output" as a single entry, or "--outdir"
    and "/output" as two, and some quote values ("--title '${title}'").
    Tapis concatenates them into one command line, so the faithful local
    equivalent is to shell-split each fragment and concatenate.
    """
    argv: list[str] = []
    for item in rendered_args or []:
        if not isinstance(item, dict):
            continue
        arg = item.get("arg")
        if not isinstance(arg, str) or not arg.strip():
            continue
        if _UNRESOLVED.search(arg):
            # Mirrors Tapis: job_spec leaves unknown placeholders untouched so
            # shell vars survive, which means an unwired optional input reaches
            # the command line as literal "${x}". Kept (so local behaviour
            # matches the platform's) but surfaced, since it's almost always a
            # misconfiguration the user would rather fix before running.
            warnings.append(
                f"{node_label}: argument {arg!r} still contains an unresolved placeholder — "
                f"the matching input is probably unwired or unset."
            )
        try:
            argv.extend(shlex.split(arg))
        except ValueError as e:
            raise ValueError(f"{node_label}: could not parse argument {arg!r}: {e}") from e
    return argv


def build_bundle(
    db: Session,
    template: WorkflowTemplate,
    *,
    data_root: str = "./data",
    workdir: str = "./wf-local",
    images_dir: str = "./images",
) -> dict:
    """Build the portable execution plan for one saved template version.

    `data_root`, `workdir` and `images_dir` are only DEFAULTS recorded in the
    bundle — the runner's own flags override them, so the same bundle file
    works on any node without re-exporting.
    """
    template_version_id = template.template_version_id
    nodes = db.query(WfNode).filter(WfNode.template_version_id == template_version_id).all()
    edges = db.query(WfEdge).filter(WfEdge.template_version_id == template_version_id).all()
    if not nodes:
        raise ValueError("Template has no nodes to export")

    nodes_by_id = {str(n.node_id): n for n in nodes}
    registries: dict[str, StepTypeRegistry] = {}
    for n in nodes:
        if n.step_type_key not in registries:
            registries[n.step_type_key] = (
                db.query(StepTypeRegistry).filter(StepTypeRegistry.step_type_key == n.step_type_key).first()
            )

    # Resolve each edge's port NAMES once (the rows store port ids), giving the
    # same source_port -> target_port binding _resolve_inputs does at run time.
    port_rows = {p.port_id: p for p in db.query(StepTypePort).all()}
    wired: list[dict] = []
    deps: dict[str, set[str]] = {str(n.node_id): set() for n in nodes}
    for e in edges:
        src, tgt = str(e.source_node_id), str(e.target_node_id)
        if src not in nodes_by_id or tgt not in nodes_by_id:
            continue
        deps[tgt].add(src)
        wired.append({
            "from": src,
            "to": tgt,
            "from_port": (port_rows.get(e.source_port_id).port_name if e.source_port_id in port_rows else None),
            "to_port": (port_rows.get(e.target_port_id).port_name if e.target_port_id in port_rows else None),
        })

    order = _topological_order(list(nodes_by_id), deps)
    image_sources = _load_image_sources()

    warnings: list[str] = []
    secrets_required: set[str] = set()
    image_app_ids: dict[str, str] = {}
    outputs_by_node: dict[str, dict[str, str]] = {}
    plan: list[dict] = []
    images: set[str] = set()

    workspace = f"{WORKDIR}/wf_runs/{RUN_ID}"

    for node_key in order:
        node = nodes_by_id[node_key]
        step_type = node.step_type_key
        registry = registries.get(step_type)
        label = node.node_label or step_type
        node_config = node.default_config or {}

        # Same layering as engine.workflows._resolve_inputs: schema defaults,
        # then the node's saved config, then edge-bound upstream outputs.
        resolved = _schema_defaults(registry.config_schema if registry else {})
        resolved.update(node_config)
        for edge in wired:
            if edge["to"] != node_key or not edge["to_port"]:
                continue
            src_outputs = outputs_by_node.get(edge["from"], {})
            src_port = edge["from_port"]
            if src_port and src_port in src_outputs:
                resolved[edge["to_port"]] = src_outputs[src_port]
            elif len(src_outputs) == 1:
                resolved[edge["to_port"]] = next(iter(src_outputs.values()))

        out_ports = _ports(db, step_type, "output") if registry else []
        tapis_job = registry.tapis_job if registry else None

        entry: dict = {
            "node_id": node_key,
            "label": label,
            "step_type": step_type,
            "config": node_config,
        }

        if tapis_job:
            # --- executable step: render its Tapis job spec against a LOCAL
            # context, then translate the result into a container invocation.
            job_dir = f"{workspace}/{step_type}/{node_key}"
            output_dir = f"{job_dir}/output"

            # get_run_archive_context's keys, with local equivalents. archive_dir
            # / archive_uri are what a template's ${archive_dir} resolves to and
            # what output ports hang off; locally the job's own output directory
            # IS the artifact location, since nothing is archived anywhere else.
            ctx = {
                **resolved,
                "archive_dir": output_dir,
                "archive_uri": output_dir,
                "archive_system": "local",
                "exec_system": "local",
                "exec_queue": "",
                "slurm_account": "",
                "work_dir": WORKDIR,
                "workspace": workspace,
                "run_id": RUN_ID,
            }
            # A "secret"-typed config field holds a KEY NAME, not a value (the
            # engine swaps in the real value at submit time — see
            # workflows._resolve_secrets). Locally the runner does that from its
            # own environment, so the key name becomes the same SECRET token the
            # hardcoded ${secrets.KEY} form gets below.
            for field, spec in (registry.config_schema or {}).items():
                if isinstance(spec, dict) and spec.get("type") == "secret" and ctx.get(field):
                    key_name = str(ctx[field])
                    secrets_required.add(key_name)
                    ctx[field] = "{{SECRET:" + key_name + "}}"

            _warn_on_empty_placeholders(tapis_job, ctx, label, warnings)
            rendered = _rewrite_secrets(job_spec.render(tapis_job, ctx), secrets_required)
            param_set = rendered.get("parameterSet") or {}

            stage_in = []
            for fi in rendered.get("fileInputs") or []:
                if not isinstance(fi, dict):
                    continue
                source = fi.get("sourceUrl") or ""
                target = fi.get("targetPath") or ""
                if not source or not target:
                    continue
                if _UNRESOLVED.search(source):
                    # An optional input nobody wired. Tapis would try to
                    # transfer a file literally named '${video}'; dropping it
                    # is the local equivalent of the step.json's own "if"
                    # guard, and the step sees a missing file exactly as it
                    # would on platform with the input absent.
                    warnings.append(
                        f"{label}: input {fi.get('name')!r} is unwired — not staged. "
                        f"The step must tolerate its absence."
                    )
                    continue
                entry_in = {
                    "name": fi.get("name"),
                    # A source that is neither a token-rooted path nor another
                    # node's output is a location the platform knew about but
                    # the node never declared as a port (a panel-saved path,
                    # say) — re-root it under the data root like a source node.
                    "source": _data_root_path(source),
                    "target": target,
                }
                if _REMOTE_URL.match(source):
                    # Kept verbatim for the runner to download. `training` and
                    # `inference` ship their own container this way, via a
                    # signed URL the platform minted — which may well have
                    # expired by the time this bundle is run somewhere else.
                    entry_in["fetch"] = True
                    warnings.append(
                        f"{label}: input {fi.get('name')!r} comes from a URL ({source.split('?')[0]}). "
                        f"The runner downloads it, but a platform-signed link can expire — host that "
                        f"file yourself and re-export if the download fails."
                    )
                stage_in.append(entry_in)

            # A file the platform would have materialized at submit time from
            # this node's own config (see _CONFIG_FILES).
            write_files = []
            if step_type in _CONFIG_FILES:
                config_key, filename = _CONFIG_FILES[step_type]
                content = resolved.get(config_key)
                if content:
                    write_files.append({"target": filename, "json": content})
                else:
                    warnings.append(
                        f"{label}: nothing configured under '{config_key}', so {filename} cannot be written "
                        f"and the step will fail. Build it in the step's panel on the platform first."
                    )

            # Some steps ship the REAL container as a staged input rather than
            # running the Tapis app's own image: `training` stages trainer.sif,
            # `inference` inf.sif, `preprocessing` preprocess.sif, and the app
            # is just a wrapper that runs the staged file. For those, the image
            # to run is that staged path — demanding an <app_id>.sif in the
            # images directory as well would be asking for a file that does not
            # exist anywhere.
            staged_image = next(
                (item["target"] for item in stage_in if str(item.get("target", "")).endswith(".sif")),
                None,
            )

            entry.update({
                "kind": "job",
                "image": staged_image or _sif_name(registry.tapis_app_id, step_type),
                # Relative to the job dir (and staged at run time) rather than
                # looked up in --images-dir.
                "image_is_staged": bool(staged_image),
                "write_files": write_files,
                "gpu": bool((registry.resources or {}).get("gpu")),
                "job_dir": job_dir,
                "stage_in": stage_in,
                "container_args": _split_args(param_set.get("containerArgs"), warnings, label),
                "args": _split_args(param_set.get("appArgs"), warnings, label),
                "env": {
                    str(v.get("key")): str(v.get("value"))
                    for v in (param_set.get("envVariables") or [])
                    if isinstance(v, dict) and v.get("key")
                },
                "outputs": {
                    p["name"]: f"{output_dir}/{p['output_path']}" if p["output_path"] else output_dir
                    for p in out_ports
                },
            })
            if not staged_image:
                images.add(entry["image"])
                image_app_ids[entry["image"]] = registry.tapis_app_id or step_type
            outputs_by_node[node_key] = entry["outputs"]

            # A bind whose host side is an absolute path baked into the
            # step.json is a SITE path (an HPC scratch cache, say) that almost
            # certainly does not exist on the user's own node, and apptainer
            # fails outright on a missing bind source rather than skipping it.
            for carg in entry["container_args"]:
                host_side = carg.split(":", 1)[0]
                if host_side.startswith("/") and not host_side.startswith("{{"):
                    warnings.append(
                        f"{label}: binds the host path '{host_side}', which is a path on the platform's "
                        f"cluster. Create it on your node (or edit the bundle's container_args) before running."
                    )
            if any(p["file_glob"] for p in out_ports):
                warnings.append(
                    f"{label}: an output port uses a filename pattern (file_glob); the runner resolves it "
                    f"by listing that directory after the step finishes."
                )
                entry["output_globs"] = {p["name"]: p["file_glob"] for p in out_ports if p["file_glob"]}

        elif step_type.startswith("sink"):
            # A sink copies its single wired artifact to a user-chosen location.
            source = resolved.get("data") or resolved.get("path") or ""
            entry.update({
                "kind": "sink",
                "source": source if str(source).startswith("{{") else _data_root_path(str(source)),
                "dest": _data_root_path(str(node_config.get("path", ""))),
            })
            outputs_by_node[node_key] = {}

        elif inline_steps.get_handler(step_type):
            # Runs Python inside the platform backend during a run (see
            # engine/inline_steps.py) — there is no container to hand the
            # runner. Exported explicitly so the runner refuses rather than
            # quietly skipping work the rest of the DAG depends on.
            entry.update({"kind": "unsupported", "reason": "runs as a platform inline step, not a container"})
            warnings.append(
                f"{label} ({step_type}) runs inside the platform, not in a container, so it cannot execute "
                f"locally. Run it on the platform, or replace it in the workflow before deploying."
            )
            outputs_by_node[node_key] = {p["name"]: "" for p in out_ports}

        else:
            # Source / design-time passthrough node. Mirrors
            # _source_node_outputs: a port whose name is already bound (an
            # upstream value passed straight through, e.g. smart_labeler's
            # 'images') keeps that value; every other port exposes this node's
            # own configured path.
            own_path = _data_root_path(str(node_config.get("path", "")))
            node_outputs = {}
            for p in out_ports:
                bound = resolved.get(p["name"])
                node_outputs[p["name"]] = (
                    str(bound) if isinstance(bound, str) and bound.startswith("{{") else own_path
                )
            entry.update({"kind": "source", "path": own_path})
            outputs_by_node[node_key] = node_outputs
            if not own_path and not any(node_outputs.values()):
                warnings.append(f"{label}: no path configured — downstream steps will have nothing to read.")

        plan.append(entry)

    return {
        "bundle_version": BUNDLE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "template": {
            "template_version_id": template_version_id,
            "template_id": template.template_id,
            "name": template.name,
            "version": template.version,
        },
        # Defaults only; every one is overridable by the runner's flags.
        "defaults": {"data_root": data_root, "workdir": workdir, "images_dir": images_dir},
        "tokens": {"data_root": DATA_ROOT, "workdir": WORKDIR, "run_id": RUN_ID},
        "images": sorted(images),
        # Where the runner can fetch each image it does not already have.
        # Only images with a known source appear; anything absent here the
        # operator must place in --images-dir themselves.
        "image_sources": {
            name: image_sources[image_app_ids[name]]
            for name in sorted(images)
            if image_app_ids.get(name) in image_sources
        },
        # Env vars the RUNNER must supply on the node. Never the values —
        # see _rewrite_secrets for why a bundle carries only the names.
        "secrets_required": sorted(secrets_required),
        "execution_order": order,
        "edges": wired,
        "nodes": plan,
        "warnings": warnings,
    }
