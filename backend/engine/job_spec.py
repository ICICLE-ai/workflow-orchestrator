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


def render(template, context: dict):
    """Recursively substitute ${...} placeholders throughout a template value."""
    if isinstance(template, str):
        return _sub_string(template, context)
    if isinstance(template, dict):
        return {k: render(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, context) for v in template]
    return template
