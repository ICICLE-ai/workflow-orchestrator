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

RUNNER_VERSION = "0.1.3"

# The published, public runner image (built from runner/Dockerfile), so users
# only ever pull it. Override to point a deployment at another tag or mirror.
RUNNER_DOCKER_IMAGE = (
    os.getenv("WF_RUNNER_DOCKER_IMAGE", "").strip() or "ghcr.io/icicle-ai/workflow-runner:v0.1.3"
)
# Any URI `apptainer pull` accepts (oras://, library://, docker://, https://).
# Defaults to the Docker image above, which Apptainer converts itself.
RUNNER_APPTAINER_IMAGE = (
    os.getenv("WF_RUNNER_APPTAINER_IMAGE", "").strip() or f"docker://{RUNNER_DOCKER_IMAGE}"
)

# The runner's source, copied into the offline zip so the node needs nothing
# but python3 and apptainer. Absent in the backend's own Docker image (which
# is built from backend/ alone) — the zip then says where to get it instead.
RUNNER_SOURCE_DIR = Path(
    os.getenv("WF_RUNNER_SOURCE_DIR")
    or Path(__file__).resolve().parents[2] / "runner"
)

_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*)://", re.IGNORECASE)
_CHUNK = 1024 * 1024

# Folder inside the deployment zip holding the step .sif files.
CONTAINERS_DIR = "containers"


def node_images_dir(images_dir: str | None) -> str | None:
    """The folder on the node the user chose for step images, or None for the
    default: containers/ inside the unzipped zip.

    A chosen folder is absolute, so every command and script means the same
    place wherever it is run from; one folder can then serve every workflow,
    and an image downloaded once is reused by all of them.
    """
    d = (images_dir or "").strip().rstrip("/")
    if d in ("", ".", CONTAINERS_DIR, f"./{CONTAINERS_DIR}", "./images"):
        return None
    if not d.startswith("/"):
        raise ValueError(f"Images folder must be an absolute path on the node (got {images_dir!r}).")
    return d

# How long a signed zip download link stays valid. It is a bearer link (whoever
# holds it can download the zip), so short; generating another is one click.
DOWNLOAD_LINK_TTL = int(os.getenv("WF_DEPLOY_LINK_TTL_SECONDS", "3600"))
_LINK_SALT = "wf-deployment-zip"


# --------------------------------------------------------------------------
# Signed download links
# --------------------------------------------------------------------------
# A cluster node fetching the zip with curl has no browser session, so the
# Deploy dialog hands it a link that carries its own short-lived, signed
# authorization instead. Signed with SESSION_SECRET — the key that already
# vouches for every session cookie — so there's nothing new to configure and
# nothing stored server-side.

def _link_serializer():
    from itsdangerous import URLSafeTimedSerializer
    secret = os.getenv("SESSION_SECRET", "dev-insecure-session-secret-change-me")
    return URLSafeTimedSerializer(secret, salt=_LINK_SALT)


def make_download_token(user_id: int, template_version_id: int,
                        data_root: str, workdir: str, images_dir: str, kind: str = "zip",
                        include_images: bool = True) -> str:
    """`kind` is "zip" (the whole deployment) or "bundle" (bundle.json alone,
    for a node that already has the images and only needs new paths).
    `include_images` false leaves the step images out of the zip."""
    return _link_serializer().dumps(
        {"k": kind, "u": user_id, "t": template_version_id, "d": data_root, "w": workdir, "i": images_dir,
         "x": include_images}
    )


def read_download_token(token: str) -> dict | None:
    """The token's payload, or None when it is forged, altered or expired."""
    from itsdangerous import BadSignature
    try:
        return _link_serializer().loads(token, max_age=DOWNLOAD_LINK_TTL)
    except BadSignature:  # SignatureExpired is a subclass
        return None


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
        p = p.rstrip("/")
        # Never "/": mounting the host root over the container's own would
        # replace the runner's whole filesystem. A "/" data root is covered by
        # mounting each source's folder instead (see _source_mounts).
        if p.startswith("/") and p not in seen:
            seen.append(p)
    # Drop any path already covered by a broader mount.
    return [p for p in seen if not any(p != q and p.startswith(q.rstrip("/") + "/") for q in seen)]


def analyze_sources(bundle: dict) -> list[dict]:
    """Each source node, and whether anything on the node actually reads it.

    A source wired into a port the local plan never uses (image-preprocess-
    studio's `output_path`, say, which on the platform is only where results
    are ARCHIVED; locally every step writes under its own job dir) must not be
    mounted or presented as input data. Mounting it is actively harmful:
    Apptainer refuses to start when a bind source doesn't exist, and an unused
    output folder often doesn't yet.

    "Read" means the source's resolved path appears in something a step or
    sink consumes: its staged inputs, arguments, binds or environment.
    """
    from engine import local_bundle

    consumed: list[str] = []
    for n in bundle.get("nodes") or []:
        if n.get("kind") == "job":
            consumed += [str(si.get("source") or "") for si in n.get("stage_in") or []]
            consumed += [str(a) for a in (n.get("args") or []) + (n.get("container_args") or [])]
            consumed += [str(v) for v in (n.get("env") or {}).values()]
        elif n.get("kind") == "sink":
            consumed.append(str(n.get("source") or ""))
    blob = "\n".join(consumed)

    labels = {n["node_id"]: n["label"] for n in bundle.get("nodes") or []}
    out = []
    for n in bundle.get("nodes") or []:
        if n.get("kind") != "source" or not n.get("platform_path"):
            continue
        path = n.get("path") or ""
        out.append({
            "label": n["label"],
            "platform_path": "/" + local_bundle._strip_uri(n["platform_path"]),
            "used": bool(path) and path in blob,
            # A "My node" path: already where the data is on the node.
            "node_local": bool(n.get("node_local")),
            # Which steps and ports it's wired to, so the dialog can say what
            # an input is FOR rather than only where it lives.
            "used_by": [
                {"step": labels.get(e["to"], e["to"]), "port": e.get("to_port")}
                for e in bundle.get("edges") or []
                if e["from"] == n["node_id"]
            ],
        })
    return out


def resolve_source(data_root: str, platform_path: str) -> str:
    """Where the runner reads a source on the node, for this data root.

    Mirrors runner.resolve_already_rooted (and resolveSource in the Deploy
    dialog): a platform path already under the data root, or any path when the
    data root is "/", is read as-is; anything else is re-rooted beneath it.
    """
    root = data_root.rstrip("/") or "/"
    if root == "/" or platform_path == root or platform_path.startswith(root + "/"):
        return platform_path
    return f"{root}{platform_path}"


def _source_mounts(data_root: str, sources: list[str]) -> list[str]:
    """Folders to mount so every source is visible inside the container.

    Needed on top of the data root itself: with a "/" data root nothing else
    would expose the data, and a source outside a narrower data root lives at
    a path the data-root mount doesn't cover. A relative data root sits under
    $PWD, which is always mounted.
    """
    if not data_root.startswith("/"):
        return []
    return [resolve_source(data_root, p) for p in sources]


def sink_mounts(bundle: dict, data_root: str) -> list[str]:
    """Host folders the bundle's sinks write into, to mount into the runner's
    container. Without them a containerised runner writes a sink's output
    inside the container, where it is lost when the container exits.

    The folder mounted is the destination's parent (or the destination itself
    when it ends in "/"), which covers both "a file at this path" and "into
    this folder" (see the runner's run_sink_node). Paths under a relative data
    root live below $PWD, which is mounted anyway.
    """
    from engine import local_bundle

    root = data_root.rstrip("/")
    out: list[str] = []
    for n in bundle.get("nodes") or []:
        if n.get("kind") != "sink" or not n.get("dest"):
            continue
        dest = str(n["dest"]).replace(local_bundle.DATA_ROOT, root)
        if not dest.startswith("/"):
            continue
        folder = dest.rstrip("/") if dest.endswith("/") else dest.rsplit("/", 1)[0]
        if folder and folder not in out:
            out.append(folder)
    return out


def runner_instructions(data_root: str, workdir: str, images_dir: str, bundle_file: str,
                        secrets: list[str] | None = None, sources: list[str] | None = None,
                        outputs: list[str] | None = None) -> dict:
    """Copy-paste commands for running the deployment zip, three ways.

    All three run from inside the unzipped zip, so the bundle is ./bundle.json
    and the step images are ./containers unless the user chose their own
    folder (see node_images_dir), which is then passed and mounted.
    `bundle_file` no longer appears in any command (kept in the signature for
    callers). `sources` are the platform paths a step actually READS (see
    analyze_sources), mounted so the runner can see them; anything else, an
    unused output folder especially, is left out because a missing bind
    source stops Apptainer from starting at all. `outputs` are the folders
    sinks write into (see sink_mounts): created up front, then mounted.
    """
    images = node_images_dir(images_dir)
    data_mounts = _source_mounts(data_root, sources or [])
    outputs = [o for o in (outputs or []) if o.startswith("/")]
    mounts = _mounts([data_root, workdir, *data_mounts, *([images] if images else []), *outputs])
    images_flag = f"--images-dir {shlex.quote(images)}" if images else f'--images-dir "$PWD/{CONTAINERS_DIR}"'
    flags = f"--data-root {shlex.quote(data_root)} --workdir {shlex.quote(workdir)} {images_flag}"
    # Created up front: Docker would otherwise create a missing bind source as
    # root, and Apptainer refuses to start without it.
    mkdir = "mkdir -p " + " ".join(shlex.quote(p) for p in [workdir, *([images] if images else []), *outputs]) + "\n"

    # `-e NAME` with no value forwards the host's variable, so credentials go
    # from the user's shell straight into the container and never onto the
    # command line (or into shell history).
    docker_opts = ['-v "$PWD":"$PWD" -w "$PWD"']
    docker_opts += [f"-v {shlex.quote(p)}:{shlex.quote(p)}" for p in mounts]
    docker_opts += [f"-e {k}" for k in secrets or []]
    docker_run = (
        mkdir
        + "docker run --rm --privileged \\\n    " + " \\\n    ".join(docker_opts) + " \\\n"
        + f"    {RUNNER_DOCKER_IMAGE} bundle.json \\\n        {flags}"
    )

    # One --bind per path: a comma-joined list can't hold "$PWD" and still let
    # the shell expand it, and a single missing entry is easier to spot.
    apptainer_binds = ['--bind "$PWD"'] + [f"--bind {shlex.quote(p)}" for p in mounts]
    apptainer_run = (
        mkdir
        + "apptainer run " + " ".join(apptainer_binds) + " \\\n"
        + f"    wf-runner.sif bundle.json \\\n        {flags}"
    )

    # No container around the runner at all: the zip's run.sh starts it with
    # the node's own python3 and each step with the node's own apptainer. On
    # an HPC node that already has both, this is the most reliable route —
    # nothing is nested, so no user-namespace support is needed.
    host_run = (
        mkdir
        + f"./run.sh --data-root {shlex.quote(data_root)} --workdir {shlex.quote(workdir)}"
        + (f" \\\n    --images-dir {shlex.quote(images)}" if images else "")
    )

    return {
        "version": RUNNER_VERSION,
        # Host folders the containerised runner needs (sources, work dir,
        # images, sink destinations), also baked into the zip's run-docker.sh,
        # and the ones to create first: the work dir and sink folders only, as
        # sources must already exist (and may be files).
        "mounts": mounts,
        "create": [workdir, *outputs],
        "docker": {
            "image": RUNNER_DOCKER_IMAGE,
            "get": f"docker pull {RUNNER_DOCKER_IMAGE}",
            "run": docker_run,
            "note": (
                "--privileged is required: each step is an Apptainer container started inside the "
                "runner's container. Images are amd64-only."
            ),
        },
        "apptainer": {
            "image": RUNNER_APPTAINER_IMAGE,
            "get": f"apptainer pull wf-runner.sif {RUNNER_APPTAINER_IMAGE}",
            "run": apptainer_run,
            "note": (
                "This starts each step's container from inside the runner's, which many HPC nodes "
                "don't allow. If it fails, use the run.sh command instead."
            ),
        },
        "host": {
            "run": host_run,
            "note": "Needs python3 (3.9+) and apptainer on the node; nothing to pull.",
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
        # image_sources.json wins over the app's own containerImage: it's the
        # maintained link, and the one the runner downloads from, so the
        # dialog, the zip and the runner all agree on a single source.
        from engine.local_bundle import image_source_for
        central = image_source_for(image_sources, app_id, node["step_type"])
        if central:
            info["download_url"] = central

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

    `shell_dir` means images_dir is a shell expression ("$HERE/containers") to be
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
            has_runner: bool, runner: dict, images: str | None = None, images_in_zip: bool = True) -> str:
    t = bundle["template"]
    where = f"`{images}`" if images else "`containers/`"
    lines = [
        f"# {t['name']} v{t['version']} — {'offline ' if images_in_zip else ''}deployment",
        "",
        "Everything needed to run this workflow on a node you own: the execution plan",
        ("(`bundle.json`), the step container images (`containers/`), and the runner."
         if images_in_zip else
         "(`bundle.json`) and the runner. The step container images are NOT in this zip."),
        "",
        "## Where things go",
        "",
        "Unzip anywhere and keep the layout: `run.sh` and `run-docker.sh` look for",
        "`bundle.json` next to themselves.",
        "",
        "## Where the container images go",
        "",
    ]
    if images:
        lines += [
            f"Step images are kept in {where} on this node, the folder chosen when this zip",
            "was made. `run.sh`, `run-docker.sh` and `pull-images.sh` all use it. Any image",
            "missing from it is downloaded there the first time a run needs it, and kept, so",
            "later runs (and other workflows pointed at the same folder) reuse it.",
            "",
            "To use a different folder, set `WF_IMAGES_DIR=/path/to/images` or pass",
            "`--images-dir /path/to/images` to `run.sh`.",
            "",
        ]
    else:
        lines += [
            "Step images go in `containers/` next to `run.sh`."
            + ("" if images_in_zip else " It starts empty: any image missing from it is downloaded"
               " there the first time a run needs it, and kept for later runs. On a node"
               " without network access, run `pull-images.sh` somewhere that has it first."),
            "",
            "To keep images somewhere shared instead (one copy for many workflows), put the",
            "`.sif` files in one folder and set `WF_IMAGES_DIR=/that/folder` or pass",
            "`--images-dir /that/folder`; the runner matches names ignoring case and `-`/`_`.",
            "",
        ]
    lines += [
        "## Your input data",
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
        runner["docker"]["get"],
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
        # The download link itself when there is one, so a user who needs to
        # fetch an image again (or on another node) has it right here.
        if r.get("download_url"):
            src = f"[download]({r['download_url']})"
        else:
            src = r.get("docker_image") or r.get("pull_uri") or r.get("container_image")
            src = f"`{src}`" if src else "—"
        if r.get("staged"):
            state = "no — downloaded by the runner at run time"
        elif r["sif"] in included:
            state = "yes"
        elif r["sif"] in failed:
            state = f"no — {failed[r['sif']]}"
        elif not images_in_zip and r.get("download_url"):
            state = f"no — downloaded into {where} on first run"
        else:
            state = "no — see below"
        lines.append(f"| `{r['sif']}` | {', '.join(r['steps'])} | {src} | {state} |")
    lines.append("")

    links = [r for r in rows if r.get("download_url")]
    if links:
        lines += ["### Download links", "",
                  "To fetch an image again, or onto another node. Each download uses up one of the",
                  "link's allowed uses, so keep the files rather than re-downloading.", "", "```"]
        lines += [f"{r['sif']}  {r['download_url']}" for r in links]
        lines += ["```", ""]

    missing = [r for r in rows if not r.get("staged") and r["sif"] not in included]
    if missing:
        lines += ["### Images you still need", "",
                  "`pull-images.sh` fetches the ones that have a source. Anything left over must",
                  f"be copied into {where} by hand:", ""]
        for r in missing:
            how = "fetched by pull-images.sh" if pull_command(r, "containers") else (r.get("note") or "obtain it from the step's maintainer")
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


def _images_dir_sh(images: str | None) -> str:
    """Shell lines setting IMAGES_DIR: WF_IMAGES_DIR, else the folder chosen at
    export, else containers/ next to the script. Assigned in two steps so a
    quoted path never sits inside a "${...:-...}" expansion."""
    default = shlex.quote(images) if images else f'"$HERE/{CONTAINERS_DIR}"'
    return f'IMAGES_DIR="${{WF_IMAGES_DIR:-}}"\n[ -n "$IMAGES_DIR" ] || IMAGES_DIR={default}\n'


def _run_sh(runner_image: str, secrets: list[str], images: str | None = None,
            mounts: list[str] | None = None, create: list[str] | None = None) -> tuple[str, str]:
    # Neither script cd's: relative --data-root / --workdir must keep meaning
    # "relative to where the user ran it", exactly as with the runner itself.
    # A later --images-dir in "$@" still wins: the runner takes the last one.
    images_sh = _images_dir_sh(images)
    host = f"""#!/usr/bin/env bash
# Run this workflow on the host. Extra flags (--data-root, --workdir, --dry-run, ...)
# are passed straight to the runner; see README.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
{images_sh}PYTHONPATH="$HERE${{PYTHONPATH:+:$PYTHONPATH}}" \\
  exec python3 -m wf_runner "$HERE/bundle.json" --images-dir "$IMAGES_DIR" "$@"
"""
    # Credentials are forwarded by NAME (-e NAME), so values stay in the
    # user's environment and never appear in this file or on a command line.
    env_flags = "".join(f" -e {shlex.quote(k)}" for k in secrets)
    # The folders the workflow reads and its sinks write, as chosen when the zip
    # was made: mounted at their own paths. The work dir and sink folders are
    # created first (Docker would otherwise create a missing bind source as
    # root); sources must already exist, and a file source can't be mkdir'd.
    mounts = mounts or []
    create = [c for c in (create or []) if c.startswith("/")]
    mount_mkdir = ("mkdir -p " + " ".join(shlex.quote(c) for c in create) + "\n") if create else ""
    mount_flags = "".join(f" -v {shlex.quote(m)}:{shlex.quote(m)}" for m in mounts)
    docker = f"""#!/usr/bin/env bash
# Run this workflow inside the runner's Docker image. This directory, the
# current one and the images folder are mounted at their own paths; mount your
# data too, e.g.
#   EXTRA_MOUNTS="-v /data:/data" ./run-docker.sh --data-root /data
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
{images_sh}# Created here: Docker would otherwise create a missing bind source as root.
mkdir -p "$IMAGES_DIR"
{mount_mkdir}
IMAGE="${{WF_RUNNER_IMAGE:-{runner_image}}}"
exec docker run --rm --privileged \\
  -v "$HERE":"$HERE" -v "$PWD":"$PWD" -v "$IMAGES_DIR":"$IMAGES_DIR"{mount_flags} -w "$PWD" ${{EXTRA_MOUNTS:-}}{env_flags} \\
  "$IMAGE" "$HERE/bundle.json" --images-dir "$IMAGES_DIR" "$@"
"""
    return host, docker


def stream_zip(bundle: dict, rows: list[dict], token: str | None, runner: dict,
               include_images: bool = True, images_dir: str | None = None) -> Iterator[bytes]:
    """Yield the deployment zip chunk by chunk.

    Images are downloaded and written through as they arrive (STORED: a .sif
    is already compressed). One that fails to download is left out and named
    in the README rather than aborting the whole archive, since by then the
    response has started and there is no way to report an error status.

    With `include_images` false, or a folder of the user's own as
    `images_dir`, the zip holds no images: the runner downloads each into that
    folder (or containers/) on first use. A chosen folder never gets images
    from the zip, since they would land in containers/ where nothing looks.
    """
    images = node_images_dir(images_dir)
    include_images = include_images and images is None
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
        host_sh, docker_sh = _run_sh(runner["docker"]["image"], bundle.get("secrets_required") or [], images,
                                     runner.get("mounts"), runner.get("create"))
        entry(zf, "run.sh", host_sh.encode(), 0o755)
        entry(zf, "run-docker.sh", docker_sh.encode(), 0o755)
        yield sink.drain()

        for row in rows:
            url = row.get("download_url")
            if not include_images or row.get("staged") or not url:
                continue
            try:
                resp = _open_download(url, token)
            except Exception as e:
                failed[row["sif"]] = f"download failed ({e})"
                continue
            try:
                info = zipfile.ZipInfo(f"{root}/containers/{row['sif']}")
                info.external_attr = 0o100644 << 16
                with zf.open(info, "w", force_zip64=True) as dst:
                    for chunk in resp.iter_bytes(_CHUNK):
                        dst.write(chunk)
                        yield sink.drain()
                included.append(row["sif"])
            except Exception as e:
                # The partial entry is already in the archive; say so plainly.
                failed[row["sif"]] = f"download interrupted ({type(e).__name__}) — the file in containers/ is incomplete"
            finally:
                resp.close()
                resp._wf_client.close()

        pulls = [c for r in rows if r["sif"] not in included and (c := pull_command(r, "$IMAGES_DIR", shell_dir=True))]
        pull_sh = (
            "#!/usr/bin/env bash\n# Fetch the images that are not in this zip. Needs network access.\n"
            "# Into WF_IMAGES_DIR when set, else the folder run.sh uses (see README.md).\n"
            "set -euo pipefail\nHERE=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n" + _images_dir_sh(images)
            + "mkdir -p \"$IMAGES_DIR\"\n"
            + ("\n".join(pulls) if pulls else "echo \"Nothing to fetch: every image is already in $IMAGES_DIR.\"") + "\n"
        )
        entry(zf, "pull-images.sh", pull_sh.encode(), 0o755)
        entry(zf, "README.md", _readme(bundle, rows, included, failed, bool(runner_files), runner,
                                       images, include_images).encode())
    yield sink.drain()
