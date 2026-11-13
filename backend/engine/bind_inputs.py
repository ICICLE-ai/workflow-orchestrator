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

Container images
----------------
A few steps (training, inference, preprocessing) ship their real container as a
.sif fileInput from a postit link, so every run copies gigabytes in. When the run
sets a containers dir (Run settings -> containers_dir, a path on the exec
system), a .sif input whose file is in that dir is pointed there instead, and so
binds like any other same-filesystem input. Matched on the targetPath's file
name ignoring case, '-' and '_' — the same rule the off-platform runner uses for
its --images-dir (runner/wf_runner/runner.py resolve_image). A .sif not found
there is still staged from its link.

Anything this can't verify (mock mode, a non-Singularity app, an app that marks
the input REQUIRED/FIXED, a rootDir with macros, a missing source, a Files API
error) leaves that input staged exactly as before: binding is an optimization,
never a reason for a job to fail. An input the APP definition also declares can't be dropped from the job (Tapis
merges it back in and stages it anyway), so it's rewritten to a tapislocal://
URL instead: Tapis's own form for "already on the exec system — mount, don't
copy". A fileInput can opt out explicitly with
"bind": false in its step.json — e.g. a tool that writes into its input dir,
which a read-only bind would break.
"""
import re
import uuid

import httpx
from dbos import DBOS

from engine import tapis_auth
from engine.tapis import _require_run_token, _tapis_children, _tapis_mkdir, _tapis_path_type, split_tapis_uri, use_real_tapis

# Systems that mount the same filesystems (/fs/ess, /fs/scratch), so a path on
# one is reachable as-is from a job on another. Mirrors transactions._OSC_EXEC_SYSTEMS.
_SHARED_FS_GROUPS = [{"pitzer-tapis", "pitzer-tapis-static", "cardinal-tapis", "ascend-tapis"}]

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


def _normalized_image_name(name: str) -> str:
    return re.sub(r"[-_\s]+", "", name.strip().lower())


def _use_containers_dir(file_inputs: list, exec_system: str, containers_dir: str, headers: dict) -> None:
    """Point each .sif fileInput at its copy in `containers_dir` on the exec
    system, when there is one (see "Container images" in the module docstring)."""
    sifs = [fi for fi in file_inputs if isinstance(fi, dict) and str(fi.get("targetPath", "")).endswith(".sif")]
    if not sifs or not containers_dir:
        return
    containers_dir = "/" + containers_dir.strip("/")
    try:
        available = _tapis_children(exec_system, containers_dir, headers)
    except Exception as e:
        print(f"[bind_inputs] can't list containers dir {exec_system}:{containers_dir} ({type(e).__name__}) — staging images")
        return
    by_name = {_normalized_image_name(n): n for n in available}
    for fi in sifs:
        wanted = fi["targetPath"].rsplit("/", 1)[-1]
        found = by_name.get(_normalized_image_name(wanted))
        if not found:
            print(f"[bind_inputs] '{fi.get('name')}': no {wanted} in {containers_dir} — staging it from its link")
            continue
        fi["sourceUrl"] = f"tapis://{exec_system}{containers_dir}/{found}"


def _add_app_images(rendered: dict, containers_dir: str, headers: dict) -> None:
    """Override the app's own .sif input with the containers dir copy, for a
    step whose job doesn't name that input (wf_inference): Tapis would otherwise
    merge it in from the app with its download link. A job input of the same
    name replaces the app's; one only added when the copy exists, so a missing
    image still falls back to the app's link."""
    if not containers_dir:
        return
    app = _app_def(rendered.get("appId", ""), rendered.get("appVersion", ""), headers) or {}
    named = {fi.get("name") for fi in rendered.get("fileInputs") or [] if isinstance(fi, dict)}
    extra = [
        {"name": i["name"], "sourceUrl": i.get("sourceUrl"), "targetPath": i["targetPath"], "autoMountLocal": True}
        for i in (app.get("jobAttributes") or {}).get("fileInputs") or []
        if str(i.get("targetPath", "")).endswith(".sif") and i.get("name") not in named
        and str(i.get("inputMode", "")).upper() != "FIXED"
    ]
    _use_containers_dir(extra, rendered.get("execSystemId") or "", containers_dir, headers)
    found = [fi for fi in extra if str(fi.get("sourceUrl", "")).startswith("tapis://")]
    if found:
        rendered.setdefault("fileInputs", []).extend(found)


def _bindable_host_path(path: str) -> bool:
    # --bind splits on ',' and ':'; whitespace would split the containerArgs string.
    return not any(c in path for c in ",: \t\n")


@DBOS.step()
def localize_file_inputs(rendered: dict, run_id: int, containers_dir: str = "", stage_files: bool = False) -> dict:
    """Return `rendered` with same-filesystem fileInputs turned into read-only
    container binds (see the module docstring). A DBOS step so the generated
    exec dir id and the Files API calls are recorded, not redone on recovery.

    With `stage_files` (the run setting, off by default) nothing is bound: every
    input is staged by Tapis as before, and the containers dir only changes
    where a .sif is copied from — the exec system itself, not its link."""
    if not use_real_tapis():
        return rendered
    try:
        _add_app_images(rendered, containers_dir or "", {"X-Tapis-Token": _require_run_token(run_id)})
    except Exception as e:
        print(f"[bind_inputs] can't check the app's image ({type(e).__name__}: {e}) — using its link")
    file_inputs = rendered.get("fileInputs") or []
    opted_out = {id(fi) for fi in file_inputs if isinstance(fi, dict) and fi.pop("bind", True) is False}
    if not file_inputs:
        return rendered
    try:
        if stage_files:
            headers = {"X-Tapis-Token": _require_run_token(run_id)}
            _use_containers_dir(file_inputs, rendered.get("execSystemId") or "", containers_dir or "", headers)
            return rendered
        return _localize(rendered, run_id, opted_out, containers_dir or "")
    except Exception as e:
        print(f"[bind_inputs] {type(e).__name__}: {e} — staging every input as before")
        return rendered


def _localize(rendered: dict, run_id: int, opted_out: set, containers_dir: str) -> dict:
    exec_system = rendered.get("execSystemId") or ""
    exec_dir = rendered.get("execSystemExecDir") or ""
    if not exec_dir or rendered.get("execSystemInputDir") != exec_dir:
        # Without a known input dir == exec dir, $PWD/<targetPath> isn't where
        # staging would have put the input, so there's nothing safe to map.
        print("[bind_inputs] exec/input dir unknown or different — staging inputs")
        return rendered

    headers = {"X-Tapis-Token": _require_run_token(run_id)}
    _use_containers_dir([fi for fi in rendered["fileInputs"] if id(fi) not in opted_out],
                        exec_system, containers_dir, headers)

    candidates = []
    for fi in rendered["fileInputs"]:
        if not isinstance(fi, dict) or id(fi) in opted_out or not fi.get("targetPath"):
            continue
        sys_id, path = split_tapis_uri(fi.get("sourceUrl") or "")
        if sys_id and same_filesystem(sys_id, exec_system):
            candidates.append((fi, sys_id, path))
    if not candidates:
        return rendered

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

    # The app's own image, when the app runs a staged file ($PWD/trainer.sif):
    # Singularity reads that from the host before any bind exists, so it must
    # land in the exec dir as a file — a bind or a tapislocal:// mount puts it
    # only inside a container that then can't start.
    image = str(app.get("containerImage") or "").replace("${PWD}", "$PWD")
    image_target = image[len("$PWD/"):].strip("/") if image.startswith("$PWD/") else None

    bound = []
    for fi, sys_id, path in candidates:
        name = fi.get("name")
        if image_target and fi["targetPath"].strip("/") == image_target:
            print(f"[bind_inputs] '{name}': the app's container image — staging it from {fi['sourceUrl']}")
            continue
        root = _root_dir(sys_id, headers)
        if root is None:
            continue
        host_src = f"{root}{path}".rstrip("/")
        if name in app_inputs:
            # Tapis merges the app's own declaration back in whatever the job
            # sends, so dropping it from the job doesn't stop it being staged —
            # and its envKey then collides with a --bind's (JOBS_DUPLICATE_ENV_VAR).
            # Keep the entry and let Tapis mount it instead: a tapislocal:// URL
            # names a path already on the exec system, which Tapis doesn't copy
            # and, with autoMountLocal, mounts into the container at targetPath —
            # setting the envKey itself.
            fi["sourceUrl"] = f"tapislocal://exec.tapis{host_src}"
            fi["autoMountLocal"] = True
            print(f"[bind_inputs] '{name}': declared by the app too — {fi['sourceUrl']} (mounted by Tapis, not copied)")
            continue
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
        bound.append((fi, host_src, container_path(target, binds)))

    if not bound:
        return rendered

    rendered.update(pinned)
    bound_ids = {id(fi) for fi, _, _ in bound}
    rendered["fileInputs"] = [fi for fi in rendered["fileInputs"] if id(fi) not in bound_ids]
    container_args = parameter_set.setdefault("containerArgs", [])
    # A destination under $PWD itself (no `--bind $PWD:/job` of the step's or
    # app's maps it elsewhere) lands inside the working dir Apptainer mounts on
    # its own — which then covers the bind, leaving the container the empty
    # mount point. Binding $PWD explicitly first makes it a parent like /job,
    # so the binds inside it apply on top.
    if any(dest.startswith("$PWD") for _, _, dest in bound):
        container_args.append({"name": "bind-cwd", "arg": "--bind $PWD"})
    env_vars = parameter_set.setdefault("envVariables", [])
    # Tapis rejects a key set twice — by the step, the app, or another input.
    env_keys = {e.get("key") for e in env_vars + (app_params.get("envVariables") or []) if isinstance(e, dict)}
    env_keys |= {fi.get("envKey") for fi in rendered["fileInputs"] if isinstance(fi, dict)}
    env_keys |= {i.get("envKey") for i in app_inputs.values()}
    for fi, host_src, dest in bound:
        arg = f"--bind {host_src}:{dest}:ro"
        container_args.append({"name": f"bind-{fi.get('name')}", "arg": arg})
        # A staged input's envKey hands the program its path; a bound one has
        # no staging to set it, so set it to where the bind puts the input.
        if fi.get("envKey") and fi["envKey"] not in env_keys:
            env_vars.append({"key": fi["envKey"], "value": dest})
        print(f"[bind_inputs] '{fi.get('name')}': {fi['sourceUrl']} -> {arg}")
    return rendered
