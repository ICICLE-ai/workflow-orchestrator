"""Execute a workflow bundle on a single self-hosted node.

Consumes the plan produced by backend/engine/local_bundle.py. Everything the
DAG needs is already resolved in that file, so this module only has to:

    substitute the three path tokens -> create each step's job directory ->
    stage its declared inputs -> run its container -> hand the outputs on

There is deliberately NO step-specific logic here. Each node in the bundle
carries its own image, binds, argv and staging list (derived from the step's
own step.json), which is what stops this from drifting out of sync with the
platform as steps are added or changed.

Pure standard library on purpose: the runner has to work both inside the
shipped Apptainer image and, when nested containers aren't available on a
given node, directly on the host with nothing but python3.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED_BUNDLE_VERSION = 1


class BundleError(RuntimeError):
    """A problem with the bundle or the node's setup — reported without a
    traceback, since neither is a bug in this program."""


# Secret values resolved from the environment for this run. Every one is
# masked out of anything this module prints or writes: a step's command line
# carries them as --env flags, so logging it verbatim would spill live
# credentials into terminal scrollback, CI output and the per-step log file.
# The platform redacts the same way when it logs a rendered job spec.
_REDACT: set[str] = set()


def redact(text: str) -> str:
    for value in _REDACT:
        if value:
            text = text.replace(value, "***")
    return text


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {redact(msg)}", flush=True)


def _normalized_image_name(name: str) -> str:
    """Fold the differences that don't actually identify a container image."""
    return re.sub(r"[-_\s]+", "", name.strip().lower())


def resolve_image(images_dir: Path, name: str) -> Path | None:
    """Find an image in the images directory, tolerating - / _ / case.

    The bundle names each image after the step's Tapis app id
    ('export-flight-mission.sif'), but this repo's container definitions are
    named with underscores ('export_flight_mission.def'), so building one the
    obvious way produces 'export_flight_mission.sif' — the same image under a
    spelling the exact match would miss. Rather than make everyone rename
    files, match on a normalized name and report which file was used.
    """
    exact = images_dir / name
    if exact.exists():
        return exact
    if not images_dir.is_dir():
        return None
    target = _normalized_image_name(name)
    for candidate in sorted(images_dir.iterdir()):
        if candidate.is_file() and _normalized_image_name(candidate.name) == target:
            return candidate
    return None


def inside_container() -> bool:
    """Whether this process is itself running inside Apptainer/Singularity.

    Only used to tailor the "no container runtime" message, since the fix is
    completely different depending on the answer. Both the env vars and the
    marker directory are checked because which of them is set varies by version
    and by how the container was started.
    """
    return bool(
        os.environ.get("APPTAINER_CONTAINER")
        or os.environ.get("SINGULARITY_CONTAINER")
        or os.path.isdir("/.singularity.d")
    )


# --- token substitution ---------------------------------------------------

def substitute(value, tokens: dict[str, str]):
    """Replace {{DATA_ROOT}} / {{WORKDIR}} / {{RUN_ID}} anywhere in the plan.

    Applied recursively over strings, lists and dicts so callers never have to
    know which fields happen to contain a path.
    """
    if isinstance(value, str):
        for token, replacement in tokens.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [substitute(v, tokens) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, tokens) for k, v in value.items()}
    return value


def _collapse_slashes(value):
    """Squash repeated slashes in every path string, leaving URLs alone."""
    if isinstance(value, str):
        return re.sub(r"(?<!:)//+", "/", value)
    if isinstance(value, list):
        return [_collapse_slashes(v) for v in value]
    if isinstance(value, dict):
        return {k: _collapse_slashes(v) for k, v in value.items()}
    return value


# --- input staging --------------------------------------------------------

def fetch_input(url: str, target: Path) -> None:
    """Download an input the bundle marked as remote.

    Only `training` and `inference` use this today: they stage their own .sif
    from a signed URL rather than naming an image. Kept to urllib so the runner
    stays dependency-free, and failures name the URL because an expired
    platform-signed link is the overwhelmingly likely cause.
    """
    import urllib.error
    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, OSError) as e:
        raise BundleError(
            f"could not download {url.split('?')[0]}: {e}\n"
            f"        If this is a platform-signed link it has probably expired — host the file\n"
            f"        yourself and re-export the bundle, or place it at {target} by hand."
        )


def stage_input(source: str, target: Path, copy: bool) -> None:
    """Place one declared input at the target path inside the job directory.

    Directories are symlinked rather than copied by default: an image_dir input
    can be tens of gigabytes, and the container sees through the link because
    the data root is bind-mounted at its own absolute path (see container_binds).
    `--copy-inputs` forces real copies for the cases where that isn't wanted —
    a step that writes into its own input directory, or data on a filesystem
    that won't be visible inside the container.
    """
    src = Path(source)
    if not src.exists():
        raise BundleError(
            f"input not found: {src}\n"
            f"        Check --data-root, and that your local layout mirrors the platform's."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    if copy or src.is_file():
        if src.is_dir():
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)
    else:
        target.symlink_to(src.resolve())


def container_binds(data_root: Path, workdir: Path, sources: list[str]) -> list[str]:
    """Bind everything this step reads or writes, at its own absolute path.

    Identical paths inside and out, because inputs are staged as symlinks
    pointing at the real data: a link only resolves in the container if its
    target is mounted where the link says it is.

    Two rules keep this safe:

    - '/' is NEVER bound. `--data-root /` is a perfectly reasonable choice
      when the node's paths already match the platform's, but binding the host
      root over the container's root would mask the image's own filesystem —
      its interpreter and scripts included — and the step could not start. The
      individual source paths are bound instead, which is all that was needed.
    - A path already covered by an ancestor in the set is dropped, so a step
      reading several files from one tree gets one bind, not a dozen.
    """
    wanted: set[str] = set()
    for path in [data_root, workdir, *(Path(s) for s in sources)]:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if str(resolved) == "/" or not resolved.exists():
            continue
        # Bind the containing directory for a file, so sibling reads work too.
        wanted.add(str(resolved if resolved.is_dir() else resolved.parent))

    minimal = [
        p for p in sorted(wanted)
        if not any(p != other and p.startswith(other.rstrip("/") + "/") for other in wanted)
    ]

    binds: list[str] = []
    for path in minimal:
        binds.extend(["--bind", f"{path}:{path}"])
    return binds


# --- step execution -------------------------------------------------------

def build_command(node: dict, job_dir: Path, images_dir: Path, data_root: Path,
                  workdir: Path, runtime: str) -> list[str]:
    """The full container invocation for one job node.

    The step's own containerArgs are passed through verbatim apart from
    expanding `$PWD`, which every step.json writes its binds against and which
    Tapis resolves to the job directory — so the same "--bind $PWD:/job" that
    makes `/job/data/images` work on the platform makes it work here.
    """
    # A step that stages its own container (training/inference/preprocessing)
    # runs the staged file itself; everything else runs an image from the
    # images directory. See "image_is_staged" in the exporter.
    if node.get("image_is_staged"):
        image = job_dir / node["image"]
        if not image.exists():
            raise BundleError(f"container image not found: {image}")
    else:
        resolved = resolve_image(images_dir, node["image"])
        if resolved is None:
            raise BundleError(f"container image not found: {images_dir / node['image']}")
        if resolved.name != node["image"]:
            log(f"    using {resolved.name} for {node['image']}")
        image = resolved

    step_args = [arg.replace("$PWD", str(job_dir.resolve())) for arg in node.get("container_args", [])]

    cmd = [runtime, "run"]
    sources = [item["source"] for item in node.get("stage_in", []) if not item.get("fetch")]
    cmd.extend(container_binds(data_root, workdir, sources))
    cmd.extend(step_args)
    for key, value in (node.get("env") or {}).items():
        cmd.extend(["--env", f"{key}={value}"])
    cmd.append(str(image))
    cmd.extend(node.get("args", []))
    return cmd


def run_job_node(node: dict, images_dir: Path, data_root: Path, workdir: Path,
                 runtime: str, copy_inputs: bool, dry_run: bool) -> dict:
    job_dir = Path(node["job_dir"])
    output_dir = job_dir / "output"

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        # Files the platform would have materialized from node config at submit
        # time (e.g. the preprocess studio's operations.json) — written before
        # staging, since a step's fileInput may name the same target.
        for item in node.get("write_files", []):
            target = job_dir / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            content = item["json"] if isinstance(item.get("json"), str) else json.dumps(item.get("json"), indent=2)
            target.write_text(content)
            log(f"    write {target}")
        for item in node.get("stage_in", []):
            target = job_dir / item["target"]
            log(f"    stage {item['name']}: {item['source']} -> {target}")
            if item.get("fetch"):
                fetch_input(item["source"], target)
            else:
                stage_input(item["source"], target, copy_inputs)

    cmd = build_command(node, job_dir, images_dir, data_root, workdir, runtime)
    # shlex.join, not ' '.join: a correctly-quoted single argument like
    # --text_prompts "purple flower" is otherwise indistinguishable from two
    # separate arguments, which is exactly the confusion this line caused.
    # It also makes the printed command paste-able into a shell as-is.
    log(f"    exec: {shlex.join(cmd)}")
    if dry_run:
        return {"status": "skipped (dry run)", "command": shlex.join(cmd)}

    started = time.time()
    log_path = job_dir / "runner.log"
    with open(log_path, "w") as log_file:
        log_file.write(redact(f"# {shlex.join(cmd)}") + "\n\n")
        log_file.flush()
        # Output is streamed to the step's own log file rather than this
        # process's stdout: a long-running step (training, inference) otherwise
        # buries the DAG-level progress this runner prints.
        proc = subprocess.run(cmd, cwd=str(job_dir), stdout=log_file, stderr=subprocess.STDOUT)
    elapsed = time.time() - started

    if proc.returncode != 0:
        tail = ""
        try:
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-25:])
        except OSError:
            pass
        raise BundleError(
            f"step exited {proc.returncode} after {elapsed:.0f}s.\n"
            f"        Full log: {log_path}\n"
            f"        Last lines:\n{tail}"
        )

    outputs = resolve_outputs(node)
    return {"status": "completed", "seconds": round(elapsed, 1), "log": str(log_path), "outputs": outputs}


def resolve_outputs(node: dict) -> dict:
    """Each output port's final location.

    A port with a filename pattern (file_glob on the platform) names a
    DIRECTORY holding one dynamically-named file, so it can only be resolved
    now that the step has actually written it — matching what
    engine/workflows.py's _derive_outputs does with a Tapis listing.
    """
    outputs = dict(node.get("outputs") or {})
    for port, pattern in (node.get("output_globs") or {}).items():
        directory = Path(outputs.get(port, ""))
        if not directory.is_dir():
            continue
        matches = sorted(
            (p for p in directory.iterdir() if fnmatch.fnmatch(p.name, pattern)),
            key=lambda p: p.stat().st_mtime,
        )
        if matches:
            outputs[port] = str(matches[-1])
    return outputs


def run_sink_node(node: dict, dry_run: bool) -> dict:
    source, dest = node.get("source", ""), node.get("dest", "")
    if not source or not dest:
        return {"status": "skipped", "note": "sink has no source or destination configured"}
    log(f"    copy {source} -> {dest}")
    if dry_run:
        return {"status": "skipped (dry run)"}
    src, dst = Path(source), Path(dest)
    if not src.exists():
        raise BundleError(f"sink source does not exist: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # replace=True semantics, matching _run_sink_node on the platform: the
    # destination reflects only this run, with no residue from an earlier one.
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
    return {"status": "completed", "dest": str(dst)}


# --- preflight ------------------------------------------------------------

def preflight(bundle: dict, images_dir: Path, data_root: Path, workdir: Path,
              runtime: str, skip_unsupported: bool, no_download: bool = False,
              dry_run: bool = False) -> None:
    """Fail before running anything, listing every problem at once.

    A workflow's first step can take hours; discovering a missing image or an
    unrunnable node only when the DAG reaches it wastes that entirely.
    """
    problems: list[str] = []

    if bundle.get("bundle_version") != SUPPORTED_BUNDLE_VERSION:
        problems.append(
            f"bundle_version {bundle.get('bundle_version')} is not supported by this runner "
            f"(expected {SUPPORTED_BUNDLE_VERSION}) — re-export from the platform, or update the runner."
        )

    if shutil.which(runtime) is None:
        if inside_container():
            # By far the most likely way to hit this: the runner image ships the
            # orchestrator only, so `apptainer run wf-runner.sif` leaves it with
            # no runtime to launch the steps with. Spell out both fixes rather
            # than leaving "not on PATH" to be puzzled over.
            problems.append(
                f"'{runtime}' is not on PATH, and this runner is itself running inside a container.\n"
                f"        The runner image contains the orchestrator only — it has to reach a\n"
                f"        container runtime to start each step. Either:\n"
                f"\n"
                f"        (a) run it on the host instead (it needs nothing but python3):\n"
                f"              python3 -m wf_runner <bundle.json> --data-root ... --images-dir ...\n"
                f"\n"
                f"        (b) expose the host's apptainer to this container, e.g.\n"
                f"              apptainer run --bind /usr/bin/apptainer,/usr/libexec/apptainer,/var/lib/apptainer \\\n"
                f"                  wf-runner.sif <bundle.json> ...\n"
                f"            (paths vary by install; `which apptainer` and `apptainer --version` on the\n"
                f"             host show what to bind. Nested execution also needs unprivileged user\n"
                f"             namespaces, which some hardened kernels disable — if so, use (a).)"
            )
        else:
            problems.append(
                f"'{runtime}' is not on PATH.\n"
                f"        Install Apptainer on this node, or pass --runtime with the binary's path\n"
                f"        (--runtime singularity also works)."
            )

    missing_images = sorted(
        {n["image"] for n in bundle["nodes"] if n.get("kind") == "job"
         and not n.get("image_is_staged")  # staged containers arrive with the inputs
         and resolve_image(images_dir, n["image"]) is None}
    )
    # An image the bundle knows a download URL for is fetched once and kept, so
    # later runs need no network and a use-limited link is redeemed only once.
    sources = bundle.get("image_sources") or {}
    fetchable = [name for name in missing_images if name in sources]
    if fetchable and not no_download and not dry_run:
        images_dir.mkdir(parents=True, exist_ok=True)
        for name in fetchable:
            log(f"downloading image {name} (once; cached in {images_dir})")
            try:
                fetch_input(sources[name], images_dir / name)
            except BundleError as e:
                problems.append(f"could not download {name}: {e}")
                continue
            missing_images.remove(name)

    if missing_images:
        # List what IS there. The overwhelmingly common cause is a file that's
        # present under a different spelling, and showing both lists side by
        # side makes that obvious instead of leaving "but they ARE there".
        try:
            present = sorted(p.name for p in images_dir.iterdir() if p.is_file() and p.suffix == ".sif")
        except OSError:
            present = []
        detail = (
            "container images not found in " + str(images_dir) + ":\n        "
            + "\n        ".join(missing_images)
        )
        detail += (
            "\n\n        .sif files actually in that directory:\n        "
            + ("\n        ".join(present) if present else "(none)")
        )
        detail += (
            "\n\n        Names are matched ignoring case and -/_ , so a mismatch beyond that means\n"
            "        the file really is absent or unreadable. Build from this repo's jobs/*.def,\n"
            "        or obtain the image for steps defined outside it."
        )
        if no_download:
            detail += "\n        (--no-download is set, so known download URLs were not used.)"
        # A dry run exists precisely to inspect the plan BEFORE everything is in
        # place, so a missing image must not block it.
        if dry_run:
            log("WARNING: " + detail.splitlines()[0] + " (continuing: --dry-run)")
        else:
            problems.append(detail)

    unsupported = [n for n in bundle["nodes"] if n.get("kind") == "unsupported"]
    if unsupported and not skip_unsupported:
        problems.append(
            "these steps cannot run locally:\n        "
            + "\n        ".join(f"{n['label']} ({n['step_type']}): {n.get('reason', '')}" for n in unsupported)
            + "\n        Re-run them on the platform, or pass --skip-unsupported to continue anyway\n"
              "          (downstream steps will then read whatever is already at their inputs)."
        )

    if not data_root.exists():
        problems.append(f"--data-root does not exist: {data_root}")

    # Every input a step will actually stage, checked up front rather than when
    # the DAG reaches it — the first step can run for hours, and discovering a
    # typo in --data-root only afterwards wastes all of it. Sources under the
    # workspace are upstream outputs that legitimately don't exist yet, and a
    # `fetch` source is a URL, so both are skipped here.
    missing_inputs: list[str] = []
    for node in bundle["nodes"]:
        if node.get("kind") != "job":
            continue
        for item in node.get("stage_in", []):
            source = item.get("source", "")
            if item.get("fetch") or not source or source.startswith(str(workdir)):
                continue
            if not Path(source).exists():
                missing_inputs.append(f"{node['label']} needs {item['name']}: {source}")
    if missing_inputs:
        detail = (
            "these inputs do not exist on this node:\n        "
            + "\n        ".join(missing_inputs)
            + "\n        Check --data-root: your local layout must mirror the platform's beneath it."
        )
        # Same reasoning as the image check above: a dry run is for inspecting
        # the plan before the node is fully set up, so it reports and continues.
        if dry_run:
            log(f"WARNING: {len(missing_inputs)} input(s) not present yet (continuing: --dry-run)")
        else:
            problems.append(detail)

    if problems:
        raise BundleError("preflight failed:\n\n  - " + "\n\n  - ".join(problems))


# --- main -----------------------------------------------------------------

def execute(bundle: dict, args) -> dict:
    data_root = Path(args.data_root).resolve()
    workdir = Path(args.workdir).resolve()
    images_dir = Path(args.images_dir).resolve()
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")

    tokens = {
        bundle["tokens"]["data_root"]: str(data_root),
        bundle["tokens"]["workdir"]: str(workdir),
        bundle["tokens"]["run_id"]: run_id,
    }
    # Secrets are named but never carried by the bundle (see _rewrite_secrets
    # in the exporter) — they come from THIS node's environment, so the same
    # bundle is safe to share between people holding different credentials.
    missing_secrets = [k for k in bundle.get("secrets_required", []) if not os.environ.get(k)]
    if missing_secrets:
        raise BundleError(
            "this workflow needs credentials that are not set in your environment:\n        "
            + "\n        ".join(missing_secrets)
            + "\n        Export each one before running, e.g.  export "
            + f"{missing_secrets[0]}=...\n"
            + "        They are intentionally not stored in the bundle file."
        )
    for key in bundle.get("secrets_required", []):
        tokens["{{SECRET:" + key + "}}"] = os.environ[key]
        _REDACT.add(os.environ[key])

    bundle = substitute(bundle, tokens)
    # A '/' data root joins to '//fs/...'. Harmless on Linux but confusing
    # in logs, and POSIX leaves a leading '//' implementation-defined.
    bundle = _collapse_slashes(bundle)

    workdir.mkdir(parents=True, exist_ok=True)
    preflight(bundle, images_dir, data_root, workdir, args.runtime,
              args.skip_unsupported, args.no_download, args.dry_run)

    for warning in bundle.get("warnings", []):
        log(f"WARNING: {warning}")

    nodes_by_id = {n["node_id"]: n for n in bundle["nodes"]}
    order = [nid for nid in bundle["execution_order"] if nid in nodes_by_id]
    results: dict[str, dict] = {}
    started = time.time()

    # An output port with a filename pattern names a DIRECTORY holding one
    # dynamically-named file, so its real location is only knowable once the
    # step has written it. The bundle necessarily bakes in the directory, and
    # downstream steps were wired to that — so as each step finishes, any port
    # that resolved to something more specific is rewritten into the steps that
    # have not run yet. Without this a consumer stages the directory where the
    # producer's file was meant to go. (The platform avoids the problem by
    # resolving every input at the moment the consuming step runs; an exported
    # plan has to carry the correction forward itself.)
    port_rewrites: dict[str, str] = {}

    def apply_rewrites(node: dict) -> None:
        for item in node.get("stage_in", []):
            target = port_rewrites.get(item.get("source", ""))
            if target:
                log(f"    resolved {item['name']}: {Path(target).name}")
                item["source"] = target

    log(f"run {run_id}: {len(order)} steps, workspace {workdir}")
    for position, node_id in enumerate(order, start=1):
        node = nodes_by_id[node_id]
        kind = node.get("kind")
        log(f"[{position}/{len(order)}] {node['label']} ({node['step_type']}, {kind})")
        apply_rewrites(node)
        try:
            if kind == "job":
                result = run_job_node(node, images_dir, data_root, workdir,
                                      args.runtime, args.copy_inputs, args.dry_run)
            elif kind == "sink":
                result = run_sink_node(node, args.dry_run)
            elif kind == "unsupported":
                result = {"status": "skipped", "note": node.get("reason", "not runnable locally")}
                log(f"    SKIPPED: {node.get('reason', '')}")
            else:
                # Source / passthrough: nothing to execute. A missing path is
                # only a warning here — a source node is also how the canvas
                # names an output DESTINATION that nothing has created yet, and
                # failing on those would block runs that are perfectly fine.
                # Inputs that a step genuinely consumes are already checked in
                # preflight, which is the strict gate.
                path = node.get("path", "")
                missing = bool(path) and not args.dry_run and not Path(path).exists()
                result = {"status": "completed", "path": path, "exists": not missing}
                log(f"    path: {path or '(none)'}" + ("  (does not exist yet)" if missing else ""))
        except BundleError as e:
            log(f"    FAILED: {e}")
            results[node_id] = {"status": "failed", "error": str(e)}
            write_report(workdir, run_id, bundle, results, started, "failed")
            raise

        for port, actual in (result.get("outputs") or {}).items():
            baked = (node.get("outputs") or {}).get(port)
            if baked and actual and baked != actual:
                port_rewrites[baked] = actual

        results[node_id] = result
        log(f"    {result['status']}" + (f" in {result['seconds']}s" if "seconds" in result else ""))

    status = "dry-run" if args.dry_run else "completed"
    report = write_report(workdir, run_id, bundle, results, started, status)
    log(f"{status} in {time.time() - started:.0f}s — report: {report}")
    return results


def write_report(workdir: Path, run_id: str, bundle: dict, results: dict,
                 started: float, status: str) -> Path:
    """Persist what ran and where every artifact landed.

    This is the local stand-in for the platform's run detail page: without it,
    finding a completed step's outputs means guessing at the workspace layout.
    """
    path = workdir / f"run-{run_id}-report.json"
    path.write_text(json.dumps({
        "run_id": run_id,
        "status": status,
        "template": bundle.get("template"),
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.time() - started, 1),
        "steps": [
            {"node_id": nid, "label": next((n["label"] for n in bundle["nodes"] if n["node_id"] == nid), nid),
             **res}
            for nid, res in results.items()
        ],
    }, indent=2))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wf-runner",
        description="Run a workflow bundle exported from the Workflow Orchestrator on this node.",
    )
    parser.add_argument("bundle", help="Path to the exported bundle JSON")
    parser.add_argument("--data-root", default=None,
                        help="Root of your input data; every source path in the bundle resolves beneath it")
    parser.add_argument("--workdir", default=None,
                        help="Where this run's job directories and outputs are written")
    parser.add_argument("--images-dir", default=None,
                        help="Directory holding the .sif container images the steps need")
    parser.add_argument("--run-id", default=None,
                        help="Label for this run's workspace (default: a timestamp)")
    parser.add_argument("--runtime", default="apptainer",
                        help="Container runtime binary (default: apptainer; 'singularity' also works)")
    parser.add_argument("--copy-inputs", action="store_true",
                        help="Copy staged inputs instead of symlinking them")
    parser.add_argument("--skip-unsupported", action="store_true",
                        help="Continue past steps that can only run on the platform")
    parser.add_argument("--no-download", action="store_true",
                        help="Never fetch container images, even when the bundle knows a URL for them")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan and the exact container commands without running anything")
    args = parser.parse_args(argv)

    try:
        bundle = json.loads(Path(args.bundle).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read bundle {args.bundle}: {e}", file=sys.stderr)
        return 2

    # The bundle's own defaults apply to anything the user didn't override, so
    # a bundle exported with sensible paths runs with no flags at all.
    defaults = bundle.get("defaults", {})
    args.data_root = args.data_root or os.environ.get("WF_DATA_ROOT") or defaults.get("data_root", "./data")
    args.workdir = args.workdir or os.environ.get("WF_WORKDIR") or defaults.get("workdir", "./wf-local")
    args.images_dir = args.images_dir or os.environ.get("WF_IMAGES_DIR") or defaults.get("images_dir", "./images")

    try:
        execute(bundle, args)
    except BundleError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
