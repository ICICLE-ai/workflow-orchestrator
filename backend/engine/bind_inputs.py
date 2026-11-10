"""Replace Tapis input staging with container bind mounts where possible.

A rendered job spec's fileInputs make Tapis COPY every input into the job's
exec dir before the job can start — for an image directory that's often the
bulk of the job's wall time, even when the data already sits on the very
filesystem the job runs on. So after render(), each fileInput whose source is
on the exec system's own filesystem is dropped from fileInputs and handed to
the container as a read-only `--bind <host path>:<container path>:ro` instead.
Only inputs on a DIFFERENT filesystem (another site, an https:// postit) are
still staged — that's the one case a copy is genuinely needed.

Where the container expects the input
-------------------------------------
A staged input lands at <execSystemInputDir>/<targetPath>. Every exec system we
configure uses the same dir for input and exec (see transactions._exec_system_dirs),
and Tapis runs the container from the exec dir, so that's $PWD/<targetPath> on
the host. The container usually sees it through a bind of its own — the app's
or the step's `--bind $PWD:/job`, `--bind $PWD/input:/input`, ... — so the
destination is found by mapping $PWD/<targetPath> through the longest matching
$PWD bind; with none, Apptainer mounts the cwd at its own path and $PWD/<targetPath>
is used as-is.

Mount points
------------
Apptainer can't create a bind destination inside a directory that is itself
bound from the host (/job/images under `--bind $PWD:/job`), and the host
directory a step binds ($PWD/input) is no longer created by staging. So the
mount points are created up front through the Files API: an empty dir for a
directory input, an empty file for a file input. That needs the exec dir's path
BEFORE Tapis assigns a job UUID, so ${JobUUID} in execSystem{Exec,Input,Output}Dir
is replaced with an id of our own when anything is bound.

Anything this can't verify (mock mode, a non-Singularity app, an app that marks
the input REQUIRED/FIXED, a rootDir with macros, a missing source, a Files API
error) leaves that input staged exactly as before: binding is an optimization,
never a reason for a job to fail. A fileInput can opt out explicitly with
"bind": false in its step.json — e.g. a tool that writes into its input dir,
which a read-only bind would break.
"""
import uuid

import httpx
from dbos import DBOS

from engine import tapis_auth
from engine.tapis import _require_run_token, _tapis_mkdir, _tapis_path_type, split_tapis_uri, use_real_tapis

# Systems that mount the same filesystems (/fs/ess, /fs/scratch), so a path on
# one is reachable as-is from a job on another. Mirrors transactions._OSC_EXEC_SYSTEMS.
_SHARED_FS_GROUPS = [{"pitzer-tapis", "cardinal-tapis", "ascend-tapis"}]

_BIND_FLAGS = ("--bind", "-B")
_JOB_DIR_FIELDS = ("execSystemExecDir", "execSystemInputDir", "execSystemOutputDir")

# System/app definitions are shared and rarely change — cache per process.
_system_cache: dict = {}
_app_cache: dict = {}


def same_filesystem(a: str, b: str) -> bool:
    return a == b or any(a in g and b in g for g in _SHARED_FS_GROUPS)


def parse_binds(container_args: list) -> list[tuple[str, str]]:
    """(source, destination) pairs from every --bind/-B in a containerArgs list,
    in either `--bind a:b,c:d` or `--bind=a:b` form. ${PWD} is normalized to $PWD."""
    pairs = []
    for item in container_args or []:
        arg = item.get("arg", "") if isinstance(item, dict) else str(item)
        tokens = arg.split()
        specs = []
        for i, tok in enumerate(tokens):
            if tok in _BIND_FLAGS and i + 1 < len(tokens):
                specs.append(tokens[i + 1])
            elif tok.startswith("--bind="):
                specs.append(tok[len("--bind="):])
        for spec in specs:
            for pair in spec.split(","):
                parts = pair.replace("${PWD}", "$PWD").split(":")
                src = parts[0].rstrip("/") or "/"
                dst = (parts[1] if len(parts) > 1 else parts[0]).rstrip("/") or "/"
                pairs.append((src, dst))
    return pairs


def container_path(target_path: str, binds: list[tuple[str, str]]) -> str:
    """Where the container sees <exec dir>/<target_path>: mapped through the
    longest $PWD bind covering it, else $PWD/<target_path> (the cwd mount)."""
    staged = "$PWD/" + target_path.strip("/")
    best = None
    for src, dst in binds:
        if not src.startswith("$PWD"):
            continue
        if staged == src or staged.startswith(src + "/"):
            if best is None or len(src) > len(best[0]):
                best = (src, dst)
    if best is None:
        return staged
    src, dst = best
    return dst.rstrip("/") + staged[len(src):]


def _get_json(path: str, headers: dict) -> dict | None:
    r = httpx.get(f"{tapis_auth.TAPIS_BASE_URL}{path}", headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"[bind_inputs] GET {path} -> HTTP {r.status_code}")
        return None
    return r.json().get("result") or {}


def _root_dir(sys_id: str, headers: dict) -> str | None:
    """The system's rootDir — Tapis paths are relative to it — or None when it
    isn't a plain absolute path (e.g. still holds a ${HOST_EVAL(...)} macro)."""
    if sys_id not in _system_cache:
        result = _get_json(f"/v3/systems/{sys_id}", headers)
        _system_cache[sys_id] = (result or {}).get("rootDir") if result is not None else None
    root = _system_cache[sys_id] or "/"
    if not root.startswith("/") or "$" in root:
        return None
    return root.rstrip("/")


def _app_def(app_id: str, version: str, headers: dict) -> dict | None:
    key = (app_id, version)
    if key not in _app_cache:
        path = f"/v3/apps/{app_id}/{version}" if version else f"/v3/apps/{app_id}"
        _app_cache[key] = _get_json(path, headers)
    return _app_cache[key]


def _touch(sys_id: str, path: str, headers: dict) -> bool:
    """Create an empty file (a mount point for a single-file bind)."""
    r = httpx.post(f"{tapis_auth.TAPIS_BASE_URL}/v3/files/ops/{sys_id}{path}", headers=headers,
                   files={"file": (path.rsplit("/", 1)[-1], b"", "application/octet-stream")}, timeout=60)
    return r.status_code in (200, 201)


def _bindable_host_path(path: str) -> bool:
    # --bind splits on ',' and ':'; whitespace would split the containerArgs string.
    return not any(c in path for c in ",: \t\n")


@DBOS.step()
def localize_file_inputs(rendered: dict, run_id: int) -> dict:
    """Return `rendered` with same-filesystem fileInputs turned into read-only
    container binds (see the module docstring). A DBOS step so the generated
    exec dir id and the Files API calls are recorded, not redone on recovery."""
    file_inputs = rendered.get("fileInputs") or []
    opted_out = {id(fi) for fi in file_inputs if isinstance(fi, dict) and fi.pop("bind", True) is False}
    if not file_inputs or not use_real_tapis():
        return rendered
    try:
        return _localize(rendered, run_id, opted_out)
    except Exception as e:
        print(f"[bind_inputs] {type(e).__name__}: {e} — staging every input as before")
        return rendered


def _localize(rendered: dict, run_id: int, opted_out: set) -> dict:
    exec_system = rendered.get("execSystemId") or ""
    exec_dir = rendered.get("execSystemExecDir") or ""
    if not exec_dir or rendered.get("execSystemInputDir") != exec_dir:
        # Without a known input dir == exec dir, $PWD/<targetPath> isn't where
        # staging would have put the input, so there's nothing safe to map.
        print("[bind_inputs] exec/input dir unknown or different — staging inputs")
        return rendered

    candidates = []
    for fi in rendered["fileInputs"]:
        if not isinstance(fi, dict) or id(fi) in opted_out or not fi.get("targetPath"):
            continue
        sys_id, path = split_tapis_uri(fi.get("sourceUrl") or "")
        if sys_id and same_filesystem(sys_id, exec_system):
            candidates.append((fi, sys_id, path))
    if not candidates:
        return rendered

    headers = {"X-Tapis-Token": _require_run_token(run_id)}
    app = _app_def(rendered.get("appId", ""), rendered.get("appVersion", ""), headers)
    if not app or str(app.get("runtime", "")).upper() != "SINGULARITY":
        print(f"[bind_inputs] app runtime is {(app or {}).get('runtime')!r}, not SINGULARITY — staging inputs")
        return rendered
    app_params = (app.get("jobAttributes") or {}).get("parameterSet") or {}
    app_inputs = {i.get("name"): i for i in (app.get("jobAttributes") or {}).get("fileInputs") or []}
    parameter_set = rendered.setdefault("parameterSet", {})
    binds = parse_binds((app_params.get("containerArgs") or []) + (parameter_set.get("containerArgs") or []))

    # Pin the exec dir now so the mount points can be created inside it.
    job_dir_id = f"wf{run_id}-{uuid.uuid4().hex[:12]}"
    pinned = {f: rendered[f].replace("${JobUUID}", job_dir_id) for f in _JOB_DIR_FIELDS if isinstance(rendered.get(f), str)}
    exec_dir = pinned.get("execSystemExecDir", exec_dir)

    bound = []
    for fi, sys_id, path in candidates:
        name = fi.get("name")
        mode = (app_inputs.get(name) or {}).get("inputMode")
        if mode in ("REQUIRED", "FIXED"):
            print(f"[bind_inputs] '{name}': app declares it {mode} — staging (mark it OPTIONAL in the app to bind)")
            continue
        root = _root_dir(sys_id, headers)
        if root is None:
            continue
        host_src = f"{root}{path}".rstrip("/")
        if not _bindable_host_path(host_src):
            print(f"[bind_inputs] '{name}': {host_src!r} can't be expressed in --bind — staging")
            continue
        kind = _tapis_path_type(sys_id, path, headers)
        if kind is None:
            print(f"[bind_inputs] '{name}': {fi['sourceUrl']} not found — staging (Tapis will report it)")
            continue

        target = fi["targetPath"].strip("/")
        mount_point = f"{exec_dir.rstrip('/')}/{target}"
        if kind == "dir":
            _tapis_mkdir(exec_system, mount_point, headers)
            ok = _tapis_path_type(exec_system, mount_point, headers) == "dir"
        else:
            parent = mount_point.rsplit("/", 1)[0]
            _tapis_mkdir(exec_system, parent, headers)
            ok = _touch(exec_system, mount_point, headers)
        if not ok:
            print(f"[bind_inputs] '{name}': couldn't create mount point {mount_point} — staging")
            continue
        bound.append((fi, f"--bind {host_src}:{container_path(target, binds)}:ro"))

    if not bound:
        return rendered

    rendered.update(pinned)
    bound_ids = {id(fi) for fi, _ in bound}
    rendered["fileInputs"] = [fi for fi in rendered["fileInputs"] if id(fi) not in bound_ids]
    container_args = parameter_set.setdefault("containerArgs", [])
    for fi, arg in bound:
        container_args.append({"name": f"bind-{fi.get('name')}", "arg": arg})
        print(f"[bind_inputs] '{fi.get('name')}': {fi['sourceUrl']} -> {arg}")
    return rendered
