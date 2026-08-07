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


def render(template, context: dict):
    """Render a Tapis job template: substitute placeholders, then set/drop fields
    that depend on the chosen exec system rather than the step's own template.

    - execSystem{Exec,Input,Output}Dir are overwritten from context's
      exec_system_*_dir values (present only for exec systems that need them;
      see _exec_system_dirs) — dropped entirely when absent, so Tapis applies
      the app's own default layout instead of a broken/empty path.
    - If execSystemLogicalQueue rendered empty, drop it (let the app default apply).
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
