"""Run-time handlers for steps that do real work in THIS backend instead of
submitting a Tapis job.

Why this exists
---------------
A step with no `tapis_job` template used to be treated by execute_node_workflow
as a pure data-provider: it re-published its configured `path` on its output
ports and went straight to "completed" (see _source_node_outputs). That's
correct for a real source node (source_image_dir), and for a design-time panel
whose work the user does by hand BEFORE a run (smart_labeler's labeling UI).

It is wrong for a step whose work is a deterministic transformation of its
INPUTS, because those inputs usually only exist once upstream steps have run.
The node went green having written nothing, and the first downstream job to
stage that phantom "output" died in the Tapis transfer stage with
FILES_TXFR_SVC_SRCPATH_NOTFOUND — a failure that points at the consumer, not at
the step that never produced the file.

A step registered here still submits NO Tapis job (submits_job stays false, and
there are no compute resources to configure). The difference is that the engine
calls its handler in-process at the point in the DAG where the node runs, so the
work sees this run's actual upstream outputs and downstream steps stage a file
that exists.

Registering a handler
---------------------
Handlers are keyed by step_type_key, mirroring the `sink*` special case already
in execute_node_workflow, so adding one needs no step-registry column or DB
migration. A handler:

  * receives (run_id, resolved, token), where `resolved` is the node's config
    merged with its edge-resolved input ports (each a tapis://system/path URI,
    per _resolve_inputs), and `token` is the run owner's Tapis access token;
  * returns the outputs dict to persist. It only needs to name the ports it
    actually computes plus "path" — run_inline_step fills any remaining declared
    output port from "path", exactly like a source node;
  * raises on failure. The caller marks the step failed with that message and
    the DAG blocks downstream nodes, instead of completing a lie.

Handlers run inside the node's DBOS workflow but are NOT @DBOS.step()s, matching
_run_sink_node's Tapis copy: a workflow recovered mid-step re-runs them, so a
handler must be idempotent (write to a fixed destination, don't append).
"""
from engine import tapis_auth
from engine.tapis import TapisAuthError, split_tapis_uri, use_real_tapis


def _loc(value, fallback_system: str = ""):
    """A resolved input-port value -> {"system", "path"}, or None if unwired.

    Input ports resolve to full tapis://system/path URIs, so the system comes
    off the value itself rather than being paired with some unrelated field —
    the same rule the frontend panels follow via resolveWiredLocation. A bare
    path (an older template, or a hand-typed config value) falls back to
    `fallback_system` so it degrades to the pre-URI behaviour rather than
    failing outright.
    """
    if not value or not isinstance(value, str):
        return None
    system, path = split_tapis_uri(value)
    system = system or fallback_system
    if not system or not path:
        return None
    return {"system": system, "path": path}


def _annotation_format_adapter(run_id: int, resolved: dict, token: str) -> dict:
    """Convert annotations between formats and upload the result to the node's
    configured destination — the same work POST /api/annotation-adapter/convert
    does for the panel's "Convert now" button, driven by run-time inputs.

    Shares annotation_adapter._do_convert rather than reimplementing it, so the
    design-time button and the run-time step can never drift apart. The import
    is function-local because main.py imports annotation_adapter (to mount its
    router) and annotation_adapter reaches into the engine package for Tapis
    auth — a module-level import here would close that loop.
    """
    # HTTPException is what _do_convert raises for every "the user's setup is
    # wrong" case (missing input, destination is a directory, Tapis rejected the
    # read/write). Outside a request there's no response to turn it into, so its
    # .detail becomes the step's error_message, which the run page shows.
    from fastapi import HTTPException

    from annotation_adapter import FROM_FORMATS, TO_FORMATS, ConvertRequest, TapisLoc, _do_convert

    from_format = str(resolved.get("from_format") or "native")
    to_format = str(resolved.get("to_format") or "coco")
    if from_format not in FROM_FORMATS:
        raise RuntimeError(f"from_format must be one of {FROM_FORMATS}, got '{from_format}'")
    if to_format not in TO_FORMATS:
        raise RuntimeError(f"to_format must be one of {TO_FORMATS}, got '{to_format}'")

    # Destination: the node's own config, either a bare path + a `system` field
    # (what TapisPathField writes) or a full URI if a template hand-set one.
    dest_system = str(resolved.get("system") or "")
    dest = _loc(str(resolved.get("path") or ""), dest_system)
    if not dest:
        raise RuntimeError(
            "Annotation Format Adapter has no destination — open the step's settings and set a "
            "Tapis system and path for the converted output before running."
        )

    # Only the input port matching from_format is required; 'images' is passed
    # whenever it's wired (required for coco/yolo width-height normalization,
    # optional-but-useful for sam3_exemplars keys — _do_convert enforces which).
    port = {"native": "annotations", "coco": "annotations", "yolo": "annotations_dir"}.get(
        from_format, "annotations_gpkg"
    )
    source = _loc(resolved.get(port), dest_system)
    if not source:
        raise RuntimeError(
            f"from_format '{from_format}' needs the '{port}' input wired to an upstream step "
            f"that produces it — nothing resolved for that port in this run."
        )
    images = _loc(resolved.get("images"), dest_system)

    # The panel stores text_prompts as one comma-separated string (TagsInput
    # joins on save); accept a real list too, in case a template or API caller
    # set the config field directly.
    raw_prompts = resolved.get("text_prompts") or ""
    if not isinstance(raw_prompts, (list, tuple)):
        raw_prompts = str(raw_prompts).split(",")
    text_prompts = [str(t).strip() for t in raw_prompts if str(t).strip()]
    body = ConvertRequest(
        from_format=from_format,
        to_format=to_format,
        dest=TapisLoc(**dest),
        images=TapisLoc(**images) if images else None,
        text_prompts=text_prompts or None,
        **{port: TapisLoc(**source)},
    )

    try:
        result = _do_convert(body, token)
    except HTTPException as e:
        raise RuntimeError(f"Annotation conversion failed: {e.detail}") from e
    except (ValueError, KeyError, TypeError) as e:
        # Same reinterpretation the HTTP endpoint does: annotation_formats'
        # parsers assume well-formed input for their declared format and raise
        # these on anything else, which is a user-facing "wrong from_format /
        # wrong file" rather than a bug.
        raise RuntimeError(
            f"'{from_format}' source doesn't look like a valid {from_format} file: {e}"
        ) from e

    dest_uri = f"tapis://{dest['system']}/{dest['path'].lstrip('/')}"
    print(
        f"[inline] annotation_format_adapter (run {run_id}): {from_format} -> {to_format}, "
        f"{result.get('annotation_count')} annotation(s) across {result.get('image_count')} "
        f"image(s) -> {dest_uri}"
    )
    return {
        "path": dest_uri,
        "converted": dest_uri,
        "image_count": result.get("image_count"),
        "annotation_count": result.get("annotation_count"),
        "written": result.get("written"),
    }


def _image_preprocess_studio_presubmit(run_id: int, ctx: dict, token: str) -> dict:
    """Write the node's OpenCV pipeline out as operations.json and point
    `pipeline_path` at it, so the job's 'pipeline' fileInput stages a file that
    exists.

    The studio panel edits the pipeline live and stores it on the node config
    under `operations` — but the job stages operations.json as a FILE. The only
    bridge between the two was a manual "Save operations.json" button plus a
    hand-typed pipeline_path, so three things could each silently break the run:
    never clicking the button, never typing the path (pipeline_path has no
    config_schema default, so the placeholder rendered LITERALLY as
    "${pipeline_path}"), or editing the pipeline afterwards and not re-saving,
    leaving a stale file on Tapis. Writing it here from the config the run
    actually resolved makes all three impossible — the staged file always
    matches the pipeline the node is carrying.

    Returns ctx overrides for job_spec.render.
    """
    from annotation_adapter import _tapis_upload

    operations = ctx.get("operations")
    configured = str(ctx.get("pipeline_path") or "").strip()

    # An explicitly configured path stays authoritative (it may be a curated
    # operations.json the user maintains elsewhere); it just gets resolved to a
    # full URI. Otherwise the file lands in this step's own per-node archive
    # dir, which is already unique per run+node.
    if configured:
        system, path = split_tapis_uri(configured)
        system = system or str(ctx.get("archive_system") or ctx.get("exec_system") or "")
    else:
        system = str(ctx.get("archive_system") or "")
        path = f"{str(ctx.get('archive_dir') or '').rstrip('/')}/operations.json"

    if not system or not path or path == "/operations.json":
        raise RuntimeError(
            "Image Preprocess Studio: couldn't determine where to write operations.json "
            "(no pipeline path configured and no archive location for this run)."
        )

    if operations:
        _tapis_upload(system, path, _json_bytes(operations), token, "application/json")
        print(f"[presubmit] image-preprocess-studio (run {run_id}): wrote operations.json -> tapis://{system}{path}")
    elif not configured:
        # Nothing to write and nowhere pre-existing to point at — the job would
        # stage a file that has never existed. Fail here, with the reason, rather
        # than in Tapis' transfer stage minutes later.
        raise RuntimeError(
            "Image Preprocess Studio has no pipeline to run: build one in the step's panel "
            "(or set a pipeline path pointing at an existing operations.json)."
        )
    else:
        print(f"[presubmit] image-preprocess-studio (run {run_id}): no pipeline in config, "
              f"staging the configured tapis://{system}{path} as-is")

    # A full URI, not a bare path: the file lives on the ARCHIVE system, which
    # needn't be the exec system this node runs on.
    return {"pipeline_path": f"tapis://{system}/{path.lstrip('/')}"}


def _json_bytes(obj) -> bytes:
    import json

    return json.dumps(obj, indent=2).encode("utf-8")


HANDLERS = {
    "annotation_format_adapter": _annotation_format_adapter,
}

# Handlers for steps that DO submit a Tapis job but need something materialized
# on Tapis first — typically turning a config value the panel edits in-browser
# into the file the job expects to stage. Run after the render context is built
# and before job_spec.render, and return ctx overrides.
PRE_SUBMIT_HANDLERS = {
    "image-preprocess-studio": _image_preprocess_studio_presubmit,
}


def get_handler(step_type: str | None):
    """The inline handler for a step type, or None if it has none (in which case
    the engine keeps treating a template-less node as a source/sink)."""
    return HANDLERS.get(step_type or "")


def run_pre_submit(run_id: int, step_type: str | None, ctx: dict) -> dict:
    """Run a step type's pre-submit handler, returning ctx overrides ({} when it
    has none). Raises on failure — the caller's normal step-failure path applies,
    which is the point: a job whose input couldn't be materialized must not be
    submitted."""
    handler = PRE_SUBMIT_HANDLERS.get(step_type or "")
    if handler is None:
        return {}
    if not use_real_tapis():
        print(f"[presubmit] {step_type} (run {run_id}): MOCK mode — skipping materialization")
        return {}
    token = tapis_auth.get_token_for_run(run_id)
    if not token:
        raise TapisAuthError(
            "Tapis authentication required — the run owner's session has expired. "
            "Please log in again and re-run."
        )
    return handler(run_id, ctx, token)


def run_inline_step(run_id: int, node_key: str, step_type: str, resolved: dict, output_ports: list) -> dict:
    """Run a registered inline handler and shape its result into step outputs."""
    if not use_real_tapis():
        # Mock mode has no Files API to read from or write to. Mirror the mock
        # job path (engine.tapis) by staying runnable rather than failing: fall
        # back to the old pass-through, publishing the configured destination so
        # a local credential-free run still exercises the DAG wiring.
        path = str(resolved.get("path") or "")
        system = str(resolved.get("system") or "")
        if system and path and "://" not in path:
            path = f"tapis://{system}/{path.lstrip('/')}"
        print(f"[inline] {step_type} (run {run_id}): MOCK mode — no conversion, publishing {path}")
        outputs = {"path": path, "mock": True}
    else:
        token = tapis_auth.get_token_for_run(run_id)
        if not token:
            raise TapisAuthError(
                "Tapis authentication required — the run owner's session has expired. "
                "Please log in again and re-run."
            )
        outputs = get_handler(step_type)(run_id, resolved, token)

    # Every declared output port that the handler didn't name itself falls back
    # to "path", so a step can add a port without touching its handler.
    for port in output_ports or ():
        outputs.setdefault(port["name"], outputs.get("path", ""))
    return outputs
