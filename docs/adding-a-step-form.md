# Adding a step (form-rendered config)

This is the simplest kind of step: you define it **entirely in the backend** with a
`step.json`, and the UI is generated for you. When a user clicks the gear/Settings
icon on the node, the app auto-renders a form from your `config_schema` — no
frontend code required.

Use this when the step's configuration is a set of simple fields (numbers,
toggles, text). If you need an interactive UI (a map, an image editor, a live
preview), see [adding-a-step-custom-ui.md](./adding-a-step-custom-ui.md) instead.

This doc is also the reference for **ports** (§3) and for **how a step is actually
executed** (§5–6), which apply to custom-UI steps identically.

---

## 1. Create `backend/steps/<key>/step.json`

Each step lives in its own folder under `backend/steps/`. The folder name is
conventional; the real identity is the `step_type_key` field.

```jsonc
{
  "step_type_key": "resize_images",          // unique id (see naming note below)
  "display_name": "Resize Images",            // shown on the node + palette
  "description": "Resize every image to a fixed size.",
  "category": "Data Pre-processing",          // groups the step in the palette
  "icon": "default",                          // optional

  // Input/output ports. `type` is a port data type (see §3).
  "inputs":  [{ "name": "images", "type": "image_dir" }],
  "outputs": [{ "name": "resized", "type": "image_dir" }],

  // The fields the user edits. Each becomes one form control (see §2).
  "config_schema": {
    "width":  { "type": "int",     "description": "Target width in px.",  "default": 224 },
    "height": { "type": "int",     "description": "Target height in px.", "default": 224 },
    "keep_aspect": { "type": "boolean", "description": "Preserve aspect ratio.", "default": true }
  },

  // Optional: how the step actually runs on Tapis (omit / null for a
  // design-time-only step). See §4.
  "tapis_app_id": null,
  "tapis_job": null
}
```

### Every field the registry reads

Synced on backend startup by `sync_step_registry` in
[`backend/main.py`](../backend/main.py); anything else in the file is ignored.

| Field | Required | Purpose |
| --- | --- | --- |
| `step_type_key` | ✅ | Identity everywhere. Stable forever — see the naming note. |
| `display_name` | | Node + palette label. Defaults to the key. |
| `description` | | Palette tooltip / step docs. |
| `category` | | Palette grouping. Defaults to `"general"`. |
| `icon` | | Node icon. Defaults to `"default"`. |
| `inputs` / `outputs` | | Port declarations (§3). |
| `config_schema` | | Form fields (§2). `{}` is valid. |
| `tapis_app_id` | | The registered Tapis app this step submits to. |
| `tapis_job` | | Full Tapis job template with `${...}` placeholders (§4). |
| `submits_job` | | Overrides the inferred "does this step run compute" flag (§5). |
| `resources` | | e.g. `{"gpu": true}` — routes the step to the run's GPU target (§5). |
| `hidden` | | `true` keeps the step out of the palette (deprecated/internal steps). |

## 2. `config_schema` field types → form controls

The auto-form ([`frontend/app/pages/GenericConfigForm.tsx`](../frontend/app/pages/GenericConfigForm.tsx))
renders one control per key, driven by `type`:

| `type` in schema      | Rendered control      | Value stored        |
| --------------------- | --------------------- | ------------------- |
| `int` / `float`       | number input          | number              |
| `boolean`             | switch (toggle)       | true/false          |
| `select`              | dropdown of `options` | the chosen string   |
| `tapis_path`          | system + path + **Browse** (Tapis file picker) | the path, **plus the system under a separate `system` key** |
| `secret`              | dropdown of team secrets | secret's **key** (e.g. `"WANDB_API_KEY"`), never the value |
| `string` (or other)   | text input            | string              |

Two of these carry extra schema keys:

```jsonc
"output_format": { "type": "select", "options": ["coco", "yolo", "both"], "default": "coco" },
"path":          { "type": "tapis_path", "selectType": "dir", "description": "Where to write results." }
```

- `select` — `options` is the fixed choice list; a dropdown instead of free text,
  so a CLI enum can't be typo'd.
- `tapis_path` — `selectType` is `"file"` (default) or `"dir"`. The chosen Tapis
  system is stored under the fixed key **`system`**, *not* under this field's own
  key and not itself a declared `config_schema` field. That's the same
  `path` + `system` convention `_source_node_outputs` reads to build a
  `tapis://<system>/<path>` URI (§3.3), so a source step should use this type
  rather than asking the user to hand-type a full URI.

A `secret` field lets a step reference an API token (Weights & Biases,
Hugging Face, ...) added via the dashboard's settings icon (`/api/secrets`),
without ever putting the real value in the node's config, the saved template,
or a run's frozen config — only the key name is stored there. The engine
resolves the key to its decrypted value (scoped to the run owner's team) right
before rendering the Tapis job template, so `${your_field_name}` in the
step's `tapis_job` (an env var, a CLI arg, ...) receives the real value at
submission time only. See `backend/engine/secrets.py` and
`workflows.py`'s `_resolve_secrets`.

### Hardcoding a specific secret: `${secrets.KEY}`

A `secret` config field is for when the *user* should pick which secret a
node uses. If a step always needs one specific, known secret — e.g. every run
of a particular model needs the same Hugging Face token — reference it
directly in the `tapis_job` template instead, with no config_schema field and
no per-node UI at all:

```jsonc
"envVariables": [
  { "key": "HF_TOKEN", "value": "${secrets.HF_TOKEN}" }
]
```

`${secrets.HF_TOKEN}` is resolved against the run owner's team vault (the
same one `/api/secrets` manages) after the normal `${...}` substitution runs,
by `job_spec.resolve_secret_refs` — so it works anywhere a string can appear
in the template (env vars, `appArgs`, scheduler options, ...). A secret that
doesn't exist for that team is left as the literal `${secrets.KEY}` text
rather than failing the substitution outright; the job will typically fail
downstream in a way that makes the missing secret obvious. Like the `secret`
field type, the resolved value is only ever substituted into the rendered
job spec sent to Tapis — never persisted to `run_step`/`frozen_config`, and
redacted from the debug logging in `engine.tapis.submit_job` if the job is
rejected.

Each field supports:
- `description` — helper text under the label.
- `default` — pre-filled value when the user hasn't set one.

The user's edits are saved onto the node as `config_values`, which become
`wf_node.default_config` when the template is saved, and are frozen into the run
at execution time.

---

## 3. Ports — the whole data-flow story

A port is `{ "name": "...", "type": "<port_data_type>" }`. Ports are what edges
connect, and at run time they are what carries **Tapis URIs** between steps.

### 3.0 Declaring ports

| Key | Applies to | Meaning |
| --- | --- | --- |
| `name` | both | The port label, **and** the `${name}` placeholder you use in `tapis_job`. |
| `type` | both | A port data type key — drives connection validation. |
| `required` | inputs | `false` makes the input optional (default `true`). |
| `description` | both | Shown in the UI; use it to document non-obvious wiring. |
| `output_path` | outputs | Subpath of the artifact within the step's archive dir (§3.2). |
| `file_glob` | outputs | Resolve a dynamically-named file inside `output_path` (§3.2). |

About types:

- `type` must be a **port data type** key. Existing ones include `image_dir`,
  `video_file`, `json_results`, `pytorch_model`, `csv_data`, `shapefile`,
  `heatmap_image` (the canonical list with descriptions is in
  [`backend/seed_db.py`](../backend/seed_db.py); the live set is the
  `port_data_type` table).
- **You can invent a new type** — just use it as a port `type`. On startup the
  backend auto-creates any missing port data type it finds referenced by a step
  (`sync_port_data_types`), so foreign keys resolve. To give it a description /
  hierarchy, add it to `backend/seed_db.py` and run `python seed_db.py`.

Ports drive connection validation: an edge is allowed only when the source port's
type is compatible with the target port's type (exact match, subtype, or
declared coercion).

> ⚠️ **Name ports with `[a-zA-Z0-9_]` only.** The placeholder regex in
> [`job_spec.py`](../backend/engine/job_spec.py) is `\$\{([a-zA-Z0-9_]+)\}`, so a
> port named `image-dir` can never be substituted — `${image-dir}` is left in the
> spec as literal text.

### 3.1 Accessing an INPUT port inside your step

Input ports are not fetched by your code — they arrive as **substitution
values**, and Tapis stages the data into the container for you.

**The chain:**

1. At run time `_resolve_inputs` ([`workflows.py`](../backend/engine/workflows.py))
   builds one flat `resolved` dict per node, layered in this order — each layer
   overriding the one before:

   | Layer | Source |
   | --- | --- |
   | 1. schema defaults | every `config_schema` key's `default` |
   | 2. node config | what the user saved in the gear/settings form |
   | 3. **edge inputs** | for each incoming edge, the upstream output's Tapis URI, keyed by **your input port's name** |

   So an input port and a config field sharing a name is fine and often
   deliberate: the config value is the default, and a connected edge overrides
   it.

2. That dict becomes the `${...}` context. **Your input port's name is the
   placeholder.** An input named `images` gives you `${images}`, which renders
   to something like `tapis://expanse-tapis-static/…/wf_runs/42/training/17/model`.

3. You consume it in `fileInputs`, and Tapis transfers it to `targetPath`
   *inside the job*:

```jsonc
"inputs": [
  { "name": "images", "type": "image_dir" },
  { "name": "model",  "type": "pytorch_model" }
],
"tapis_job": {
  "fileInputs": [
    { "name": "images", "autoMountLocal": true,
      "sourceUrl": "${images}", "targetPath": "data/images" },
    { "name": "model",  "autoMountLocal": true,
      "sourceUrl": "${model}",  "targetPath": "data/model.pt" }
  ],
  "parameterSet": {
    "appArgs": [
      { "name": "images", "arg": "--images /job/data/images" },
      { "name": "model",  "arg": "--model /job/data/model.pt" }
    ]
  }
}
```

4. **Inside the container your code reads `/job/<targetPath>`** — never the
   `tapis://` URI. `targetPath` is relative to the job dir; the conventional
   prefix in this repo is `/job/`. That is the actual answer to "how do I access
   an input port in my step": *declare the port, wire it into a `fileInput`, and
   read the local path you chose.*

A value can equally go somewhere other than a `fileInput` — an env var or a CLI
arg — when the step wants the URI/string itself rather than staged bytes:

```jsonc
"envVariables": [{ "key": "IMAGE_DIR", "value": "${images}" }]
```

**Edge cases worth knowing:**

- **Unwired optional input** → the placeholder is left as literal `${images}`
  text (unknown keys are deliberately not replaced, so `$PWD` and `$SLURM_*`
  survive). Guard it with a conditional item rather than shipping a broken arg:
  ```jsonc
  { "name": "sahi", "arg": "--is_sahi", "if": "use_sahi" }
  ```
  Any dict in a list may carry `"if": "<config_key>"` — the whole item is dropped
  when that key is falsy, and the `if` key is stripped from the output either way.
- **Upstream has several outputs** → the value bound is the output whose name
  matches the connected *source port*. If the upstream produced exactly one
  output, that one is used regardless of name. Otherwise the whole outputs dict
  is passed through (rarely what you want — connect a specific port).
- **Doubled URI scheme** → writing `"sourceUrl": "tapis://${system}/${images}"`
  breaks the moment an edge is connected, because `${images}` is *already* a full
  URI. `_normalize_file_inputs` repairs this (and repeated slashes) at submit
  time and logs it, but write `"sourceUrl": "${images}"` and avoid the problem.

### 3.2 Producing OUTPUT ports

Your job writes files; the platform maps them to ports by **path convention**.

1. Tapis archives the job's whole output directory to this step's own
   per-node archive dir, computed by `get_run_archive_context`:

   ```
   tapis://<archive_system>/<work_dir>/wf_runs/<run_id>/<step_type_key>/<node_id>
   ```

   That URI is available in the template as `${archive_uri}`, and its path form
   as `${archive_dir}` — which is what `archiveSystemDir` should point at.

2. After the job reaches `FINISHED`, `_derive_outputs` maps each declared output
   port to `archive_uri + "/" + output_path`. A port with **no** `output_path`
   resolves to the whole archive dir.

```jsonc
"outputs": [
  { "name": "predictions", "type": "json_results", "output_path": "predictions.json" },
  { "name": "summary",     "type": "json_results", "output_path": "summary.json" },
  { "name": "annotations", "type": "image_dir",    "output_path": "annotated" }
]
```

**So your container's contract is: write `predictions.json`, `summary.json`, and
`annotated/` into the job output dir** (`/job/output` in the examples here, wired
via `--output /job/output`). The names must match `output_path` exactly. Nothing
inspects the files — the mapping is purely positional, so a typo yields a port
pointing at a path that doesn't exist and the downstream step fails at transfer.

**The output contract is validated at sync time** (`validate_step_output_contract`
in [`main.py`](../backend/main.py)), and a step that fails it is **skipped
entirely** with a `SKIPPING step` line in the startup log:

- More than one output port → every one needs a distinct, non-empty `output_path`.
- No two output ports may share an `output_path`.
- A scalar type should point at a file and a `*_dir` type at a directory — a
  warning only, since containers legitimately vary.

**Dynamically-named outputs — `file_glob`.** When your tool stamps its own
filename (a timestamp, a model name), point `output_path` at the *directory* and
give an fnmatch pattern; the real file is resolved by a live Tapis listing once
the job finishes:

```jsonc
{ "name": "annotations", "type": "json_results",
  "output_path": "annotations", "file_glob": "annotations_*.json" }
```

If nothing matches (or in mock mode) the port falls back to the directory URI
rather than failing the step.

### 3.3 Passing inputs through *and* emitting your own outputs

A step often needs to forward something it received while also publishing
something it made — e.g. a labeling tool that hands the images onward untouched
and writes an annotations file of its own.

**Rule:** for a step with **no** `tapis_job` (design-time / source / panel-driven
steps, handled by `_source_node_outputs`), an output port whose **name matches a
key already in `resolved`** — i.e. one of this node's own input port names, or a
config field — publishes *that* value unchanged. Every other output port
publishes the node's configured `path`.

`smart_labeler` is the reference implementation:

```jsonc
"inputs": [
  { "name": "images", "type": "image_dir" },
  { "name": "resume_annotations", "type": "json_results", "required": false }
],
"outputs": [
  { "name": "annotations", "type": "json_results", "output_path": "annotations.json" },
  { "name": "images",      "type": "image_dir",    "output_path": "images" }
]
```

- `images` output ≡ `images` input → **passthrough**: downstream nodes receive the
  original upstream directory.
- `annotations` output has no matching input → publishes the node's own `path`
  config (where the panel wrote `annotations.json`).

> ⚠️ **This is why the optional input is called `resume_annotations`, not
> `annotations`.** Naming it `annotations` would collide with the `annotations`
> *output*, silently converting that output into a passthrough of an input that
> is usually unwired — so it would resolve to nothing instead of the node's own
> saved path. Both `_source_node_outputs` (backend) and `resolveOutputPath` in
> [`CustomNode.tsx`](../frontend/app/components/CustomNode.tsx) (frontend) apply
> this rule, so the canvas and the engine agree. **If you do not want an output
> to be a passthrough, do not give it the same name as one of your input ports.**

For a step that **does** submit a Tapis job, there is no passthrough shortcut:
every output port resolves under the archive dir (§3.2). To forward an input
unchanged from such a step, have the job copy it into the output dir at the
`output_path` you declared.

Source steps get one more convenience: a `config_schema` declaring both `path`
and `system` lets the user type a bare path and pick the system separately —
`_source_node_outputs` joins them into `tapis://<system>/<path>` so downstream
consumers always see the same URI shape a job step's outputs use.

---

## 4. Execution: `tapis_job` (optional)

- **Design-time only** (no run): set `"tapis_job": null`. The step appears and can
  be configured/wired, but submits nothing. Good for sources/sinks, a visualization
  step (a map viewer, a labeling tool), or a first pass.
- **Executable**: provide `tapis_app_id` and a `tapis_job` template. The template
  is a full Tapis job spec with `${...}` placeholders that the engine substitutes
  at run time from the step's `config_values`, its resolved input ports, and
  run-level values. See
  [`backend/steps/preprocessing/step.json`](../backend/steps/preprocessing/step.json)
  for a complete, working example.

### Placeholders available in `tapis_job`

| Placeholder | From |
| --- | --- |
| `${<input_port_name>}` | Resolved upstream output URI (§3.1) |
| `${<config_key>}` | The node's config value, or the schema default |
| `${exec_system}` / `${exec_queue}` | This node's resolved exec target (§5) |
| `${archive_system}` / `${archive_dir}` / `${archive_uri}` | This node's archive location (§3.2) |
| `${work_dir}` / `${workspace}` | Run scratch base, and `<base>/wf_runs/<run_id>` |
| `${slurm_account}` / `${run_id}` | Run-level values from `frozen_config` |
| `${secrets.KEY}` | Team secret, resolved after render (§2) |

`execSystemExecDir`, `execSystemInputDir` and `execSystemOutputDir` are **set
centrally** from the resolved exec system — don't declare them yourself. When the
engine has no site-appropriate value it drops the field so Tapis falls back to
the app definition's own layout.

`archiveSystemDir` must be a **plain path**, not a URI — Tapis names the system
separately in `archiveSystemId`. `_normalize_archive_dir` repairs a URI (adopting
its system, which wins over `archiveSystemId`) and falls back to the run's
`archive_dir` if the value is empty or unresolved.

> ⚠️ **No commas in env-var values.** Tapis joins all `envVariables` into a single
> comma-separated `--env k=v,k=v` argument, which apptainer/singularity splits
> back apart on commas — one comma in a value breaks the whole flag and the job
> dies before starting, with an error naming nothing. Use a space-separated list
> (see `custom_shapefile`'s `SPRAY_LEVELS`). Submitting warns by name via
> `_warn_on_comma_env_values`.

---

## 5. Run configuration — what the platform gives every step

You do not implement compute configuration; you inherit it. There are **two
layers**, and they compose.

### Run-level (the whole DAG) — set at launch

`RunOptions` in [`main.py`](../backend/main.py), frozen onto the run as
`frozen_config` when execution starts:

| Option | Purpose |
| --- | --- |
| `slurm_account` | `${slurm_account}` — the allocation to charge |
| `exec_system` / `exec_queue` | **CPU** target for the run |
| `gpu_exec_system` / `gpu_exec_queue` | **GPU** target; falls back to the CPU pair when unset |
| `work_dir` | Scratch base; defaults per site from `_default_work_dir` |
| `archive_system` | Where artifacts land — **run-level on purpose** |
| `archive_dir` | Override the archive base (`run_id/node_id` is still appended) |

Archive location stays run-level even though exec target varies per node, so every
artifact lands on one system and **no DAG edge becomes a cross-site transfer**.
Only compute moves.

### Per-node — the CPU icon on the canvas

[`RunConfigModal.tsx`](../frontend/app/components/RunConfigModal.tsx) writes these
**reserved camelCase keys** into the same `config_values` dict as your business
config:

| Key | Default | Effect |
| --- | --- | --- |
| `nodeCount` | 1 | Overrides the rendered job spec |
| `coresPerNode` | 8 | ” |
| `memoryMB` | 64800 | ” |
| `maxMinutes` | 210 | ” |
| `gpus` | 0 | Becomes a `-G <n>` schedulerOption; `0` drops it |
| `execSystem` | `""` | Empty = inherit the run's target |
| `execQueue` | `""` | ” |

Applied by `apply_resource_overrides` **after** rendering, so they override
whatever your template hardcoded. A key the user never set leaves your
template's value in place.

> The keys are camelCase deliberately. Node config spreads into the `${...}`
> context, so a key named `exec_system` would shadow the run's resolved value
> while the derived archive/exec directories stayed computed from the *other*
> system — a job running on system A with directories for system B. The engine
> re-asserts `_AUTHORITATIVE_CTX_KEYS` after the spread as belt-and-braces.

### How a node's exec target is picked

`resolve_node_exec_target`, most specific first:

1. the node's own `execSystem`/`execQueue` override, else
2. the run's **GPU** pair — if the step's step.json declares `"resources": {"gpu": true}`, else
3. the run's **CPU** pair.

So declaring `"resources": {"gpu": true}` is all a step needs to land on GPU
hardware; no step.json ever names a site. One run can put `zero_shot_annotation`
on a GPU queue and `flight_plan` on a CPU queue.

### Hiding the control

A step with `"tapis_job": null` has no compute to configure, so the canvas hides
the CPU icon. This is inferred — set `"submits_job": false` to be explicit (e.g.
`smart_labeler`, `geospatial_map`), or `true` to force the control back on for a
design-time step that wants resources anyway. It reaches the frontend as
`submits_job` on `/api/step-types` and on `StepMeta`.

---

## 6. What happens to your step at run time

Two DBOS workflows in [`workflows.py`](../backend/engine/workflows.py) — durable,
so a backend restart resumes rather than restarts.

### The orchestrator (`dag_orchestrator_workflow`)

1. Freezes the template into a run (`create_run_for_template`) — config values are
   copied, so editing the template later never changes a run in flight.
2. Builds the dependency graph from the edges.
3. Loops: spawn every `pending` node whose dependencies are all `completed` as a
   **concurrent child workflow**, then block on `DBOS.recv(topic="step_complete")`
   with a 30s re-check timeout.
4. On any failure, marks every still-`pending` node reachable from it **`blocked`**
   (cascading transitively to a fixed point) and fails the run. `blocked` is
   distinct from `failed` so the UI can tell "this step broke" from "skipped
   because an upstream step broke".
5. All `completed` → run `COMPLETED`.

### One node (`execute_node_workflow`)

1. Status → `running`.
2. Resolve inputs (§3.1) and **persist** the resolved dict onto the run step, so
   the run page shows exactly what the node ran with.
3. **No `tapis_job`?** Branch and finish here:
   - `step_type_key` starts with `sink` → copy the incoming artifact to the
     configured `path` (`replace=True`, so the destination reflects only this run).
   - A registered **inline handler** (`inline_steps.HANDLERS`, e.g.
     `annotation_format_adapter`) → do real work in-process. Use this when work
     must happen *during* the run because it depends on a file an upstream step
     only just produced.
   - Otherwise → **source node**: publish config `path` / passthroughs (§3.3).
4. **Has a `tapis_job`?**
   - Build the context: `{**archive_ctx, **resolved, **authoritative_keys}`.
   - Run any **pre-submit handler** (`inline_steps.PRE_SUBMIT_HANDLERS`) that must
     materialize a file on Tapis before the job can stage it; its overrides land in
     the context before rendering.
   - Swap `secret` fields for real values (render-only), `render()`, resolve
     `${secrets.KEY}`, then `apply_resource_overrides`.
   - Submit, then poll every 3s. Only a **terminal** Tapis status fails the step —
     transient poll errors are retried up to 100 consecutive times (~5 min) so a
     network blip never marks a long-running job failed.
   - On `FINISHED`, derive each output port's URI (§3.2) and complete the step.
5. Any exception → mark `failed`, **signal the orchestrator anyway** (otherwise the
   DAG waits forever on a node that already died), re-raise.

Step statuses: `pending` → `running` → `completed` | `failed` | `blocked`.

---

## 7. Load it

The registry syncs from the JSON files on backend startup — restart the backend:

```bash
cd backend
# stop the server, then start it again (however you run it)
./.venv/bin/uvicorn main:app --reload --port 8002
```

In the startup log you should see:

```
Synced registry: resize_images (config_schema keys: ['width', 'height', 'keep_aspect'])
```

(If your step references a brand-new port data type, you'll also see
`Seeded N missing port data type(s): ...`.)

## 8. Verify

- The step appears in the canvas palette under its `category`.
- Drag it in, connect its ports, click the **gear** → the auto-generated form
  shows your fields → Save persists them onto the node.
- Click the **CPU icon** → Run Configuration, unless the step is design-time only.
- Run it, then open `/runs/:runId` and check the node's resolved config and the
  derived output URIs.

---

## Naming note (important)

`step_type_key` is the step's identity everywhere. Keep it stable — renaming it
orphans saved templates/runs that reference the old key. If you later add a custom
UI panel, the frontend registry key must match this string **exactly** (hyphens
vs. underscores included).

## Gotchas

- **Nothing shows in the palette** → check the startup log for a `SKIPPING step`
  message (invalid output contract, §3.2) or a JSON parse error. Also check
  `"hidden": true`.
- **Foreign-key error on a port** → you referenced a port data type that couldn't
  be created; check the type spelling.
- **`config_schema: {}`** is fine — the form just shows "No configuration
  available for this step."
- **`${my_port}` appears literally in the submitted job** → the port is unwired
  and has no config default, or its name contains a character outside
  `[a-zA-Z0-9_]`.
- **Downstream step fails staging a file that "should" exist** → your container
  didn't write the exact `output_path` you declared, or wrote it outside the
  archived output dir.
- **An output port silently resolves to nothing** → it shares a name with one of
  your input ports and became a passthrough (§3.3).
- **Job dies instantly with a `key=value` usage dump** → a comma in an env-var
  value (§4).
