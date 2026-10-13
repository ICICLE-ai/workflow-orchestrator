"""How to run an exported bundle: the runner's images, each step's container,
and an offline zip that carries all of it.

engine/local_bundle.py answers WHAT runs (the resolved DAG). This module
answers the operator's next three questions, which the bundle alone leaves
open:

  * how do I start the runner — as a Docker container, or under Apptainer?
  * which container image does each step actually use, and how do I get it?
  * can I just have everything in one file, for a node with no network?

Step image names come from the LIVE Tapis app definition (containerImage +
runtime), not from a copy kept in this repo: the app is what the platform
really runs, so it is the only answer that cannot drift. image_sources.json
(local_bundle._load_image_sources) still supplies a download URL where the
app's own containerImage isn't something the backend can fetch.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import zipfile
from pathlib import Path
from typing import Iterator

import httpx

from engine import tapis_auth

RUNNER_VERSION = "0.1.0"

# Published runner images. Unset means "not published": the commands then
# build the image locally from runner/ instead of pulling a name that doesn't
# exist. See runner/Dockerfile and runner/wf_runner.def.
RUNNER_DOCKER_IMAGE = os.getenv("WF_RUNNER_DOCKER_IMAGE", "").strip()
# Any URI `apptainer pull` accepts (oras://, library://, docker://, https://).
# Defaults to the Docker image above, which Apptainer can convert itself.
RUNNER_APPTAINER_IMAGE = os.getenv("WF_RUNNER_APPTAINER_IMAGE", "").strip()

# The runner's source, copied into the offline zip so the node needs nothing
# but python3 and apptainer. Absent in the backend's own Docker image (which
# is built from backend/ alone) — the zip then says where to get it instead.
RUNNER_SOURCE_DIR = Path(
    os.getenv("WF_RUNNER_SOURCE_DIR")
    or Path(__file__).resolve().parents[2] / "runner"
)

_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*)://", re.IGNORECASE)
_CHUNK = 1024 * 1024


# --------------------------------------------------------------------------
# Runner image
# --------------------------------------------------------------------------

def _mounts(paths: list[str]) -> list[str]:
    """Host paths the runner container needs, each mounted at ITS OWN path.

    Identical host and container paths are what keep the bundle's paths valid
    on both sides: the runner hands absolute paths to apptainer, which runs
    inside the same container, and the report it writes names locations the
    user can open on the host afterwards. A relative path lives under the
    current directory, which is mounted (and used as the working dir) anyway.
    """
    seen: list[str] = []
    for p in paths:
        if p.startswith("/") and p not in seen:
            seen.append(p)
    # Drop any path already covered by a broader mount.
    return [p for p in seen if not any(p != q and p.startswith(q.rstrip("/") + "/") for q in seen)]


def runner_instructions(data_root: str, workdir: str, images_dir: str, bundle_file: str,
                        secrets: list[str] | None = None) -> dict:
    """Copy-paste commands for both ways of starting the runner."""
    flags = (
        f"--data-root {shlex.quote(data_root)} "
        f"--workdir {shlex.quote(workdir)} "
        f"--images-dir {shlex.quote(images_dir)}"
    )

    docker_image = RUNNER_DOCKER_IMAGE or f"wf-runner:{RUNNER_VERSION}"
    if RUNNER_DOCKER_IMAGE:
        docker_get = f"docker pull {docker_image}"
    else:
        docker_get = f"docker build -t {docker_image} runner/   # from a checkout of this repo"
    # `-e NAME` with no value forwards the host's variable, so credentials go
    # from the user's shell straight into the container and never onto the
    # command line (or into shell history).
    opts = ['-v "$PWD":"$PWD" -w "$PWD"']
    opts += [f"-v {shlex.quote(p)}:{shlex.quote(p)}" for p in _mounts([data_root, workdir, images_dir])]
    opts += [f"-e {k}" for k in secrets or []]
    docker_run = (
        "docker run --rm --privileged \\\n    " + " ".join(opts) + " \\\n"
        f"    {docker_image} {shlex.quote(bundle_file)} \\\n        {flags}"
    )

    apptainer_source = RUNNER_APPTAINER_IMAGE or (f"docker://{RUNNER_DOCKER_IMAGE}" if RUNNER_DOCKER_IMAGE else "")
    if apptainer_source:
        apptainer_get = f"apptainer pull wf-runner.sif {apptainer_source}"
    else:
        apptainer_get = "apptainer build wf-runner.sif runner/wf_runner.def   # from a checkout of this repo"
    binds = ",".join(_mounts([data_root, workdir, images_dir]))
    apptainer_run = (
        "apptainer run " + (f"--bind {shlex.quote(binds)} " if binds else "")
        + f"wf-runner.sif {shlex.quote(bundle_file)} \\\n    {flags}"
    )

    return {
        "version": RUNNER_VERSION,
        "docker": {
            "image": docker_image,
            "published": bool(RUNNER_DOCKER_IMAGE),
            "get": docker_get,
            "run": docker_run,
            "note": (
                "--privileged is required: each step is an Apptainer container started inside the "
                "runner's container. Images are amd64-only."
            ),
        },
        "apptainer": {
            "image": apptainer_source or "wf-runner.sif (built locally)",
            "published": bool(apptainer_source),
            "get": apptainer_get,
            "run": apptainer_run,
            "note": (
                "Running the runner inside Apptainer nests a container per step, which hardened HPC "
                "kernels often refuse. If it does, run the same code on the host: "
                f"python3 -m wf_runner {bundle_file} {flags}"
            ),
        },
        "python": {
            "run": f"python3 -m wf_runner {shlex.quote(bundle_file)} \\\n    {flags}",
        },
    }


# --------------------------------------------------------------------------
# Step images
# --------------------------------------------------------------------------

def _fetch_app(app_id: str, version: str, token: str | None, cache: dict) -> dict | None:
    """The Tapis app definition, or None when it can't be read (logged out,
    mock mode, app not shared with this user). Never raises: a missing app
    degrades one row of the listing, not the whole export."""
    key = (app_id, version)
    if key in cache:
        return cache[key]
    app = None
    if token:
        url = f"{tapis_auth.TAPIS_BASE_URL}/v3/apps/{app_id}" + (f"/{version}" if version else "")
        try:
            resp = httpx.get(url, headers={"X-Tapis-Token": token}, timeout=20)
            if resp.status_code == 200:
                app = resp.json().get("result") or None
        except httpx.HTTPError as e:
            print(f"[deployment] app lookup {app_id}/{version} failed: {type(e).__name__}")
    cache[key] = app
    return app


def _classify(container_image: str, runtime: str) -> dict:
    """What a containerImage value lets the operator do with it.

    Returns docker_image (a name `docker pull` accepts, if any), pull_uri (what
    `apptainer pull` accepts, if any) and download_url (bytes the backend can
    fetch into the offline zip, if any).
    """
    out = {"docker_image": None, "pull_uri": None, "download_url": None}
    if not container_image:
        return out
    m = _SCHEME.match(container_image)
    scheme = m.group(1).lower() if m else ""
    if scheme == "docker":
        out["docker_image"] = container_image[len("docker://"):]
        out["pull_uri"] = container_image
    elif scheme in ("oras", "library", "shub"):
        out["pull_uri"] = container_image
    elif scheme in ("http", "https", "tapis"):
        out["download_url"] = container_image
    elif not scheme and runtime == "DOCKER":
        # A DOCKER-runtime app names a registry image with no scheme.
        out["docker_image"] = container_image
        out["pull_uri"] = f"docker://{container_image}"
    # Anything else (a bare path on the exec system) is only meaningful there.
    return out


def step_images(bundle: dict, registries: dict, token: str | None, image_sources: dict) -> list[dict]:
    """One row per container image the bundle needs, with how to obtain it.

    `registries` maps step_type_key -> StepTypeRegistry. `image_sources` is
    local_bundle._load_image_sources() — the fallback download URL per app id.
    """
    app_cache: dict = {}
    rows: dict[str, dict] = {}
    for node in bundle.get("nodes") or []:
        if node.get("kind") != "job":
            continue
        reg = registries.get(node["step_type"])
        app_id = (reg.tapis_app_id if reg else None) or node["step_type"]
        version = str(((reg.tapis_job or {}) if reg else {}).get("appVersion") or "")

        if node.get("image_is_staged"):
            # training / inference / preprocessing: the real image is a staged
            # INPUT (already in the bundle as a URL the runner downloads), and
            # the Tapis app is only a wrapper around it.
            staged = next((s for s in node.get("stage_in") or [] if s.get("target") == node["image"]), {})
            key = f"staged:{node['node_id']}"
            rows[key] = {
                "sif": node["image"], "steps": [node["label"]], "app_id": app_id, "app_version": version,
                "runtime": "SINGULARITY", "container_image": staged.get("source") or "",
                "docker_image": None, "pull_uri": None,
                "download_url": staged.get("source") if staged.get("fetch") else None,
                "staged": True,
                "note": "Staged as a step input; the runner downloads it into the job directory itself.",
            }
            continue

        sif = node["image"]
        if sif in rows:
            rows[sif]["steps"].append(node["label"])
            continue

        app = _fetch_app(app_id, version, token, app_cache)
        runtime = str((app or {}).get("runtime") or "").upper()
        container_image = str((app or {}).get("containerImage") or "")
        info = _classify(container_image, runtime)
        if not info["download_url"] and app_id in image_sources:
            info["download_url"] = image_sources[app_id]

        if app is None:
            note = "Tapis app definition not readable (not logged in, or not shared with you)."
        elif runtime == "ZIP":
            note = "This app is a ZIP-runtime app, not a container."
        elif info["download_url"] or info["pull_uri"]:
            note = ""
        else:
            note = (f"The app's image ({container_image}) is a path on its exec system; copy that "
                    f"file to your images dir as {sif}.")
        rows[sif] = {
            "sif": sif, "steps": [node["label"]], "app_id": app_id, "app_version": version,
            "runtime": runtime or None, "container_image": container_image or None,
            **info, "staged": False, "note": note,
        }
    return list(rows.values())


def pull_command(row: dict, images_dir: str, shell_dir: bool = False) -> str | None:
    """The shell command that puts this image in images_dir, if there is one.

    `shell_dir` means images_dir is a shell expression ("$HERE/images") to be
    expanded rather than a literal path to quote.
    """
    if row.get("staged"):
        return None
    if shell_dir:
        dest = f'"{images_dir}"/{shlex.quote(row["sif"])}'
    else:
        dest = shlex.quote(f"{images_dir.rstrip('/')}/{row['sif']}")
    if row.get("pull_uri"):
        return f"apptainer pull {dest} {shlex.quote(row['pull_uri'])}"
    url = row.get("download_url") or ""
    if url.startswith("tapis://"):
        system, _, path = url[len("tapis://"):].partition("/")
        return (f"curl -fL -H \"X-Tapis-Token: $TAPIS_TOKEN\" -o {dest} "
                f"{shlex.quote(f'{tapis_auth.TAPIS_BASE_URL}/v3/files/content/{system}/{path}')}")
    if url:
        return f"curl -fL -o {dest} {shlex.quote(url)}"
    return None


# --------------------------------------------------------------------------
# Offline zip
# --------------------------------------------------------------------------

class _Sink(io.RawIOBase):
    """Write-only buffer the zip is written into and drained from as it grows.

    Unseekable on purpose: zipfile then writes data descriptors after each
    entry instead of seeking back to patch headers, which is what lets a
    multi-GB archive stream out with only one chunk ever held in memory.
    """

    def __init__(self):
        super().__init__()
        self._buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self._buf += b
        return len(b)

    def drain(self) -> bytes:
        data = bytes(self._buf)
        self._buf.clear()
        return data


def _open_download(url: str, token: str | None) -> httpx.Response:
    """Start streaming an image; raises with a readable reason on failure."""
    headers = {}
    if url.startswith("tapis://"):
        system, _, path = url[len("tapis://"):].partition("/")
        url = f"{tapis_auth.TAPIS_BASE_URL}/v3/files/content/{system}/{path}"
        headers["X-Tapis-Token"] = token or ""
    client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300))
    req = client.build_request("GET", url, headers=headers)
    resp = client.send(req, stream=True)
    if resp.status_code != 200:
        resp.close()
        client.close()
        raise RuntimeError(f"HTTP {resp.status_code}")
    resp._wf_client = client  # closed alongside the response
    return resp


def _runner_files() -> list[tuple[str, Path]]:
    """(arcname, path) for the runner source, or [] when it isn't on disk."""
    src = RUNNER_SOURCE_DIR / "wf_runner"
    if not src.is_dir():
        return []
    files = [(f"wf_runner/{p.relative_to(src).as_posix()}", p)
             for p in sorted(src.rglob("*.py"))]
    for extra in ("Dockerfile", "wf_runner.def"):
        if (RUNNER_SOURCE_DIR / extra).is_file():
            files.append((extra, RUNNER_SOURCE_DIR / extra))
    return files


def _readme(bundle: dict, rows: list[dict], included: list[str], failed: dict[str, str],
            has_runner: bool, runner: dict) -> str:
    t = bundle["template"]
    lines = [
        f"# {t['name']} v{t['version']} — offline deployment",
        "",
        "Everything needed to run this workflow on a node you own: the execution plan",
        "(`bundle.json`), the step container images (`images/`), and the runner.",
        "",
        "## Where things go",
        "",
        "Unzip anywhere and keep the layout: `run.sh` and `run-docker.sh` look for",
        "`bundle.json` and `images/` next to themselves. To keep images somewhere shared",
        "instead (one copy for many workflows), move the `.sif` files into that directory",
        "and pass `--images-dir <dir>`; the runner matches names ignoring case and `-`/`_`.",
        "",
        "Your input data must sit under one directory that mirrors the platform's paths",
        "(a source reading `tapis://<system>/users/you/farm` is read from `<data-root>/users/you/farm`).",
        "",
        "## Run it",
        "",
        "On the host (needs `python3` and `apptainer`):",
        "",
        "```bash",
        "./run.sh --data-root /path/to/data --workdir /path/to/workdir",
        "```",
        "",
        "Or inside the runner's Docker image (needs Docker; the container runs privileged",
        "because each step is an Apptainer container started inside it):",
        "",
        "```bash",
        (runner["docker"]["get"] if runner["docker"]["published"]
         else f"docker build -t {runner['docker']['image']} .   # Dockerfile and wf_runner/ are in this zip"),
        "./run-docker.sh --data-root /path/to/data --workdir /path/to/workdir",
        "```",
        "",
        "Add `--dry-run` first to print every step's command without running anything.",
        "",
    ]
    if bundle.get("secrets_required"):
        lines += ["## Credentials", "", "Export these before running (they are never stored in the bundle):", ""]
        lines += [f"    export {k}=..." for k in bundle["secrets_required"]] + [""]

    lines += ["## Container images", "", "| Image | Steps | Source | In this zip |", "|---|---|---|---|"]
    for r in rows:
        src = r.get("docker_image") or r.get("pull_uri") or r.get("container_image") or (
            "download link" if r.get("download_url") else "—")
        src = src if src in ("—", "download link") else f"`{src}`"
        if r.get("staged"):
            state = "no — downloaded by the runner at run time"
        elif r["sif"] in included:
            state = "yes"
        elif r["sif"] in failed:
            state = f"no — {failed[r['sif']]}"
        else:
            state = "no — see below"
        lines.append(f"| `{r['sif']}` | {', '.join(r['steps'])} | {src} | {state} |")
    lines.append("")

    missing = [r for r in rows if not r.get("staged") and r["sif"] not in included]
    if missing:
        lines += ["### Images you still need", "",
                  "`pull-images.sh` fetches the ones that have a source. Anything left over must",
                  "be copied into `images/` by hand:", ""]
        for r in missing:
            how = "fetched by pull-images.sh" if pull_command(r, "images") else (r.get("note") or "obtain it from the step's maintainer")
            lines.append(f"- `{r['sif']}` — {how}")
        lines.append("")

    if not has_runner:
        lines += ["## Runner", "",
                  "The runner source was not available to this server, so it isn't in the zip. Get",
                  "`runner/wf_runner/` from the Workflow Orchestrator repository and place it next to",
                  "`run.sh`, or use the Docker image above.", ""]

    if bundle.get("warnings"):
        lines += ["## Export warnings", ""] + [f"- {w}" for w in bundle["warnings"]] + [""]
    return "\n".join(lines)


def _run_sh(runner_image: str, secrets: list[str]) -> tuple[str, str]:
    # Neither script cd's: relative --data-root / --workdir must keep meaning
    # "relative to where the user ran it", exactly as with the runner itself.
    host = """#!/usr/bin/env bash
# Run this workflow on the host. Extra flags (--data-root, --workdir, --dry-run, ...)
# are passed straight to the runner; see README.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" \\
  exec python3 -m wf_runner "$HERE/bundle.json" --images-dir "$HERE/images" "$@"
"""
    # Credentials are forwarded by NAME (-e NAME), so values stay in the
    # user's environment and never appear in this file or on a command line.
    env_flags = "".join(f" -e {shlex.quote(k)}" for k in secrets)
    docker = f"""#!/usr/bin/env bash
# Run this workflow inside the runner's Docker image. This directory and the
# current one are mounted at their own paths; mount your data too, e.g.
#   EXTRA_MOUNTS="-v /data:/data" ./run-docker.sh --data-root /data
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${{WF_RUNNER_IMAGE:-{runner_image}}}"
exec docker run --rm --privileged \\
  -v "$HERE":"$HERE" -v "$PWD":"$PWD" -w "$PWD" ${{EXTRA_MOUNTS:-}}{env_flags} \\
  "$IMAGE" "$HERE/bundle.json" --images-dir "$HERE/images" "$@"
"""
    return host, docker


def stream_zip(bundle: dict, rows: list[dict], token: str | None, runner: dict) -> Iterator[bytes]:
    """Yield the offline deployment zip chunk by chunk.

    Images are downloaded and written through as they arrive (STORED: a .sif
    is already compressed). One that fails to download is left out and named
    in the README rather than aborting the whole archive, since by then the
    response has started and there is no way to report an error status.
    """
    sink = _Sink()
    included: list[str] = []
    failed: dict[str, str] = {}
    runner_files = _runner_files()
    root = re.sub(r"[^A-Za-z0-9._-]+", "-", bundle["template"]["name"] or "workflow").strip("-") or "workflow"
    root = f"{root}-v{bundle['template']['version']}"

    def entry(zf, name: str, data: bytes, mode: int = 0o644):
        info = zipfile.ZipInfo(f"{root}/{name}")
        info.external_attr = (0o100000 | mode) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, data)

    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        entry(zf, "bundle.json", json.dumps(bundle, indent=2).encode())
        yield sink.drain()

        for arcname, path in runner_files:
            entry(zf, arcname, path.read_bytes())
        host_sh, docker_sh = _run_sh(runner["docker"]["image"], bundle.get("secrets_required") or [])
        entry(zf, "run.sh", host_sh.encode(), 0o755)
        entry(zf, "run-docker.sh", docker_sh.encode(), 0o755)
        yield sink.drain()

        for row in rows:
            url = row.get("download_url")
            if row.get("staged") or not url:
                continue
            try:
                resp = _open_download(url, token)
            except Exception as e:
                failed[row["sif"]] = f"download failed ({e})"
                continue
            try:
                info = zipfile.ZipInfo(f"{root}/images/{row['sif']}")
                info.external_attr = 0o100644 << 16
                with zf.open(info, "w", force_zip64=True) as dst:
                    for chunk in resp.iter_bytes(_CHUNK):
                        dst.write(chunk)
                        yield sink.drain()
                included.append(row["sif"])
            except Exception as e:
                # The partial entry is already in the archive; say so plainly.
                failed[row["sif"]] = f"download interrupted ({type(e).__name__}) — the file in images/ is incomplete"
            finally:
                resp.close()
                resp._wf_client.close()

        pulls = [c for r in rows if r["sif"] not in included and (c := pull_command(r, "$HERE/images", shell_dir=True))]
        pull_sh = (
            "#!/usr/bin/env bash\n# Fetch the images that are not in this zip. Needs network access.\n"
            "set -euo pipefail\nHERE=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\nmkdir -p \"$HERE/images\"\n"
            + ("\n".join(pulls) if pulls else "echo 'Nothing to fetch: every image is already in images/.'") + "\n"
        )
        entry(zf, "pull-images.sh", pull_sh.encode(), 0o755)
        entry(zf, "README.md", _readme(bundle, rows, included, failed, bool(runner_files), runner).encode())
    yield sink.drain()
