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
"""
import re

_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")


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
        return {k: _render_value(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [_render_value(v, context) for v in template]
    return template


# Fields that only apply to OSC-style exec systems. When work_dir is not provided
# (e.g. an Expanse run), we drop them so they don't render as broken paths.
_OSC_DIR_FIELDS = ("execSystemExecDir", "execSystemInputDir", "execSystemOutputDir")


def render(template, context: dict):
    """Render a Tapis job template: substitute placeholders, then drop fields that
    don't apply to the chosen exec system.

    - If work_dir is empty/missing, remove the execSystem*Dir fields (OSC-only).
    - If execSystemLogicalQueue rendered empty, drop it (let the app default apply).
    """
    rendered = _render_value(template, context)

    if isinstance(rendered, dict):
        if not context.get("work_dir"):
            for f in _OSC_DIR_FIELDS:
                rendered.pop(f, None)
        if rendered.get("execSystemLogicalQueue", None) in ("", None):
            rendered.pop("execSystemLogicalQueue", None)

    return rendered
