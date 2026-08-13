"""Render a step's tapis_job template into a concrete Tapis job spec.

The template (from step.json / step_type_registry.tapis_job) contains ${name}
placeholders. We substitute them from a context dict built per node at run time:
  - resolved input-port values (Tapis URIs coming from upstream outputs or
    source nodes), keyed by input port name,
  - the node's own config values (e.g. epochs, learning_rate),
  - run-level values: slurm_account, archive_system, archive_dir, archive_uri.

Substitution is plain ${key} replacement over all string leaves of the template
(recursing into dicts/lists), so it works inside fileInputs sourceUrls, env
variable values, scheduler args, etc.

A dict inside a list (appArgs, envVariables, schedulerOptions, containerArgs,
fileInputs, ...) may carry an "if": "config_key" entry — the whole item is
dropped unless context[config_key] is truthy, letting a step conditionally
include an arg/env var based on a boolean config field (e.g. only pass
--is_sahi, --tile-size, --overlap-ratio when a "use_sahi" toggle is on). The
"if" key itself is stripped from the rendered output either way.

Separately, ${secrets.KEY} is a step-AUTHOR-hardcoded reference to a team
secret (see backend/engine/secrets.py) — resolved by resolve_secret_refs()
after render(), independent of config_schema/context entirely. Unlike a
config_schema field with "type": "secret" (where the user PICKS which secret
via a UI dropdown, and the step.json only knows a field name), this lets a
step.json wire a SPECIFIC, always-the-same secret (e.g. a HuggingFace token
every run of this step needs) with no per-node UI involved at all.
"""
import re

_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")
_SECRET_PLACEHOLDER = re.compile(r"\$\{secrets\.([A-Za-z0-9_]+)\}")


def _sub_string(s: str, context: dict) -> str:
    def repl(m):
        key = m.group(1)
        val = context.get(key)
        # Leave unknown placeholders untouched (so $PWD, $SLURM_* shell vars and
        # any unresolved optional inputs survive rather than becoming "None").
        return str(val) if val is not None else m.group(0)
    return _PLACEHOLDER.sub(repl, s)


def _render_value(template, context: dict):
    """Recursively substitute ${...} placeholders throughout a template value."""
    if isinstance(template, str):
        return _sub_string(template, context)
    if isinstance(template, dict):
        return {k: _render_value(v, context) for k, v in template.items() if k != "if"}
    if isinstance(template, list):
        result = []
        for item in template:
            if isinstance(item, dict) and "if" in item and not context.get(item["if"]):
                continue  # condition key is falsy/missing — drop this item entirely
            result.append(_render_value(item, context))
        return result
    return template


# execSystem{Exec,Input,Output}Dir are set centrally from context here (from
# engine.transactions.get_run_archive_context's exec_system_*_dir values,
# computed per exec_system — see _exec_system_dirs there) rather than left to
# each step.json to declare — every Tapis-job-submitting step gets the same
# exec-system-appropriate paths without repeating them.
_EXEC_DIR_FIELDS = {
    "execSystemExecDir": "exec_system_exec_dir",
    "execSystemInputDir": "exec_system_input_dir",
    "execSystemOutputDir": "exec_system_output_dir",
}


_URI_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _collapse_nested_uri(url: str) -> str:
    """Reduce a sourceUrl that ended up with a scheme inside a scheme to just
    the inner URI:

      tapis://expanse-tapis-static/tapis://expanse-tapis-static/users/x/images
      -> tapis://expanse-tapis-static/users/x/images

    This happens whenever a step.json writes "tapis://${system}/${port}" for a
    value that is ALREADY a full URI. Most config fields that name a location
    are also wired input PORTS, and _resolve_inputs overwrites the config value
    with the upstream output's full tapis://system/path URI at run time — so a
    template that looks right against its own bare-path default silently
    doubles the scheme the moment someone connects an edge to it.

    The INNER URI wins, not the outer prefix, and that's the substantive part
    rather than just cosmetic tidying: the inner one carries the system the data
    actually lives on, which needn't be the system the job executes on (archive
    stays run-level while exec target varies per node — see
    get_run_archive_context). Truncating to the last scheme keeps the data's own
    system; stripping the inner one instead would silently retarget the transfer
    at the exec system and 404 there.

    Left alone: a string with a single scheme (the normal case), and a bare path.
    """
    matches = list(_URI_SCHEME.finditer(url))
    if len(matches) < 2:
        return url
    return url[matches[-1].start():]


def _tidy_source_url(url: str) -> str:
    """_collapse_nested_uri, plus squashing repeated slashes in the path of a
    tapis:// URI.

    The same "tapis://${system}/${path}" templates that cause the nested-URI
    case also produce 'tapis://sys//abs/path' whenever the configured path is
    absolute (which it usually is — the panels' path fields and Tapis browsers
    all hand back a leading slash), since the template supplies a separator of
    its own. Restricted to tapis:// so an https:// postit redeem URL, where a
    path segment is opaque to us, is never rewritten.
    """
    url = _collapse_nested_uri(url)
    if not url.startswith("tapis://"):
        return url
    scheme, rest = url[: len("tapis://")], url[len("tapis://") :]
    return scheme + re.sub(r"/{2,}", "/", rest)


def _warn_on_comma_env_values(rendered: dict) -> None:
    """Flag env-var values containing a comma, which a container runtime that
    takes its environment as ONE delimited string cannot express.

    Tapis joins every entry of parameterSet.envVariables into a single
    comma-separated `--env k=v,k=v,...` argument. Apptainer/Singularity then
    splits that back apart on commas, so a value with a comma of its own breaks
    the whole flag — every fragment after the first has no '=' and the job dies
    before it starts, with a bare "must be formatted as key=value" followed by
    the runtime's entire usage text. Nothing in the message names the variable
    at fault, which is why this warns by name at submit time instead.

    Concretely: image_preprocess_studio used to pass
    IMAGE_EXTENSIONS='.jpg,.jpeg,.png,...' and every one of its jobs failed this
    way. The established workaround in this repo is a space-separated list —
    see custom_shapefile's SPRAY_LEVELS/SPRAY_THRESHOLDS, documented as
    space-separated for exactly this reason.

    A warning rather than a hard failure: the limitation belongs to the
    apptainer/singularity runtimes, and the runtime lives on the Tapis APP
    definition, which isn't visible from here — a DOCKER-runtime app takes one
    --env flag per variable and is unaffected.
    """
    for item in (rendered.get("parameterSet") or {}).get("envVariables") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, str) and "," in value:
            print(
                f"[job_spec] WARNING: env var {item.get('key')!r} value {value!r} contains a comma. "
                f"On an apptainer/singularity app this breaks the job's --env flag entirely "
                f"(the runtime splits it on commas). Use a space-separated list instead."
            )


def _split_uri(url: str) -> tuple:
    """'tapis://sys/abs/path' -> ('sys', '/abs/path'); a bare path -> ('', path).

    Local rather than reusing engine.tapis.split_tapis_uri so this module stays
    a pure renderer with no engine imports (it's exercised directly by tooling
    that has no Tapis/DBOS setup).
    """
    m = _URI_SCHEME.match(url)
    if not m:
        return "", url
    rest = url[m.end():]
    system, _, path = rest.partition("/")
    return system, "/" + path


def _normalize_archive_dir(rendered: dict, context: dict) -> None:
    """Force archiveSystemDir to a PLAIN directory path, in place.

    Tapis wants a path here, not a URI — the system is named separately by
    archiveSystemId. Feed it 'tapis://expanse-tapis-static/users/x/out' and it
    treats the whole thing as a path, mangling it into
    '/tapis:/expanse-tapis-static/users/x/out'.

    A template hits this whenever it points archiveSystemDir at a config field
    or port rather than at ${archive_dir} (image_preprocess_studio's
    ${output_path}, say): those values are full tapis://system/path URIs at run
    time, because that's what every other consumer of them — fileInput
    sourceUrls, downstream edges — requires.

    The URI's system wins over whatever archiveSystemId rendered to: a user who
    pointed this step's output at a specific location meant that location, and
    archiving the path to a DIFFERENT system would silently scatter the run's
    outputs across two sites.

    Also covers the value rendering empty or still holding an unsubstituted
    ${placeholder} (a config field with no default never enters the context at
    all) by falling back to the run's own archive_dir, so a half-configured node
    archives somewhere real instead of into a literal '${output_path}'.
    """
    value = rendered.get("archiveSystemDir")
    if not isinstance(value, str):
        return
    original = value

    if "://" in value:
        system, value = _split_uri(_collapse_nested_uri(value))
        if system:
            rendered["archiveSystemId"] = system

    if not value.strip() or _PLACEHOLDER.search(value):
        fallback = context.get("archive_dir")
        if not fallback:
            print(f"[job_spec] archiveSystemDir is unusable ({original!r}) and the run has no "
                  f"archive_dir to fall back to — leaving it for Tapis to reject.")
            return
        print(f"[job_spec] archiveSystemDir {original!r} was empty/unresolved — falling back to {fallback!r}")
        value = str(fallback)

    value = re.sub(r"/{2,}", "/", value)
    if value != original:
        print(f"[job_spec] archiveSystemDir: {original!r} -> {value!r}")
    rendered["archiveSystemDir"] = value


def _normalize_file_inputs(rendered: dict) -> None:
    """Repair malformed sourceUrls (nested scheme, repeated slashes) in every
    fileInput, in place.

    Applied centrally here rather than fixed template-by-template because this
    has now bitten two different steps (zero_shot_annotation, whose step.json
    documents its own occurrence, and image_preprocess_studio), and a template
    can only be verified against the wiring someone happened to try. Tapis
    rejects the doubled form at the transfer stage with a path that reads like
    the user typed it wrong, so failing quietly-but-late is the alternative.
    """
    file_inputs = rendered.get("fileInputs")
    if not isinstance(file_inputs, list):
        return
    for item in file_inputs:
        if not isinstance(item, dict):
            continue
        url = item.get("sourceUrl")
        if not isinstance(url, str):
            continue
        fixed = _tidy_source_url(url)
        if fixed != url:
            # Worth logging: the rendered spec is now correct, but the step.json
            # that produced it still has the redundant prefix/separator.
            print(f"[job_spec] fileInput '{item.get('name')}': normalized sourceUrl {url!r} -> {fixed!r}")
            item["sourceUrl"] = fixed


def render(template, context: dict):
    """Render a Tapis job template: substitute placeholders, then set/drop fields
    that depend on the chosen exec system rather than the step's own template.

    - execSystem{Exec,Input,Output}Dir are overwritten from context's
      exec_system_*_dir values (present only for exec systems that need them;
      see _exec_system_dirs) — dropped entirely when absent, so Tapis applies
      the app's own default layout instead of a broken/empty path.
    - If execSystemLogicalQueue rendered empty, drop it (let the app default apply).
    - archiveSystemDir is forced to a plain path, since Tapis names the system
      separately in archiveSystemId (see _normalize_archive_dir).
    - fileInput sourceUrls that ended up with a scheme inside a scheme are
      collapsed to the inner URI (see _normalize_file_inputs).
    """
    rendered = _render_value(template, context)

    if isinstance(rendered, dict):
        for field, ctx_key in _EXEC_DIR_FIELDS.items():
            value = context.get(ctx_key)
            if value:
                rendered[field] = value
            else:
                rendered.pop(field, None)
        if rendered.get("execSystemLogicalQueue", None) in ("", None):
            rendered.pop("execSystemLogicalQueue", None)
        _normalize_archive_dir(rendered, context)
        _normalize_file_inputs(rendered)
        _warn_on_comma_env_values(rendered)

    return rendered


def resolve_secret_refs(rendered, team_id: int | None) -> tuple:
    """Walk an already-rendered Tapis job spec and substitute any
    ${secrets.KEY} reference with the real value of the run owner's team
    secret named KEY.

    Runs AFTER render() — a ${secrets.KEY} ref never depends on the node's
    resolved config, so it's untouched by the normal ${...} substitution
    (that placeholder regex doesn't match the dot) and can be resolved in a
    separate pass, wherever it ends up in the tree (hardcoded directly in the
    template, or arrived via a config default that itself was "${secrets.KEY}").

    Returns (rendered_with_secrets, secret_values) — secret_values are the
    real values substituted, so the caller can redact them from any logging
    of the rendered spec (see engine.tapis.submit_job's `redact` param).
    Unresolved refs (unknown key, no team) are left untouched, same
    "don't blow up on a miss" convention as job_spec.render's placeholders.
    """
    from engine import secrets as secrets_store

    values: list[str] = []

    def sub_string(s: str) -> str:
        def repl(m):
            value = secrets_store.resolve_secret(team_id, m.group(1))
            if value is None:
                return m.group(0)
            values.append(value)
            return value
        return _SECRET_PLACEHOLDER.sub(repl, s)

    def walk(value):
        if isinstance(value, str):
            return sub_string(value)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(rendered), values


# Per-step compute-resource keys a node's config may override (set via the
# canvas's "Run Configuration" panel, alongside the step's business config
# values). These are plain ints in the step.json template rather than
# ${...} placeholders, so `render()` never touches them — apply overrides
# as a separate pass after rendering.
_RESOURCE_KEYS = ("nodeCount", "coresPerNode", "memoryMB", "maxMinutes")


def apply_resource_overrides(rendered: dict, resolved: dict) -> None:
    """Override a rendered job spec's compute resources from a node's resolved
    config, in place. A key the node didn't set is left as whatever the
    step.json template already specified (e.g. legacy nodes created before
    per-step resource config existed).

    GPUs isn't a top-level Tapis job field — Tapis expresses it as a `-G <n>`
    schedulerOption, so we replace any GPU request the template baked in with
    the node's own (or drop it entirely when gpus is 0).
    """
    for key in _RESOURCE_KEYS:
        value = resolved.get(key)
        if value in (None, ""):
            continue
        try:
            rendered[key] = int(value)
        except (TypeError, ValueError):
            pass

    gpus = resolved.get("gpus")
    if gpus in (None, ""):
        return
    try:
        gpus_n = int(gpus)
    except (TypeError, ValueError):
        return
    scheduler_options = rendered.setdefault("parameterSet", {}).setdefault("schedulerOptions", [])
    scheduler_options[:] = [
        o for o in scheduler_options
        if not (isinstance(o, dict) and (o.get("name") == "gpu_per_node" or str(o.get("arg", "")).strip().startswith("-G ")))
    ]
    if gpus_n > 0:
        scheduler_options.append({"name": "gpu_per_node", "arg": f"-G {gpus_n}"})
