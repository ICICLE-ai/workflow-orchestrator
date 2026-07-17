# Adding a step (form-rendered config)

This is the simplest kind of step: you define it **entirely in the backend** with a
`step.json`, and the UI is generated for you. When a user clicks the gear/Settings
icon on the node, the app auto-renders a form from your `config_schema` — no
frontend code required.

Use this when the step's configuration is a set of simple fields (numbers,
toggles, text). If you need an interactive UI (a map, an image editor, a live
preview), see [adding-a-step-custom-ui.md](./adding-a-step-custom-ui.md) instead.

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

## 2. `config_schema` field types → form controls

The auto-form ([`frontend/app/pages/GenericConfigForm.tsx`](../frontend/app/pages/GenericConfigForm.tsx))
renders one control per key, driven by `type`:

| `type` in schema      | Rendered control      | Value stored        |
| --------------------- | --------------------- | ------------------- |
| `int` / `float`       | number input          | number              |
| `boolean`             | switch (toggle)       | true/false          |
| `string` (or other)   | text input            | string              |

Each field supports:
- `description` — helper text under the label.
- `default` — pre-filled value when the user hasn't set one.

The user's edits are saved onto the node as `config_values`, which become
`wf_node.default_config` when the template is saved, and are frozen into the run
at execution time.

## 3. Ports and data types

- A port is `{ "name": "...", "type": "<port_data_type>" }`. Inputs are required by
  default; add `"required": false` to make one optional.
- `type` must be a **port data type** key. Existing ones include `image_dir`,
  `video_file`, `json_results`, `pytorch_model`, `csv_data`, `shapefile`,
  `heatmap_image` (the canonical list with descriptions is in
  [`backend/seed_db.py`](../backend/seed_db.py); the live set is the
  `port_data_type` table).
- **You can invent a new type** — just use it as a port `type`. On startup the
  backend auto-creates any missing port data type it finds referenced by a step
  (`sync_port_data_types`), so foreign keys resolve. To give it a description /
  hierarchy, add it to `backend/seed_db.py` and run `python seed_db.py`.
- Output ports may set `"output_path"` (a subpath within the step's job output
  dir) so a step can expose several distinct outputs.

Ports drive connection validation: an edge is allowed only when the source port's
type is compatible with the target port's type (exact match, subtype, or
declared coercion).

## 4. Execution: `tapis_job` (optional)

- **Design-time only** (no run): set `"tapis_job": null`. The step appears and can
  be configured/wired, but submits nothing. Good for sources/sinks or a first pass.
- **Executable**: provide `tapis_app_id` and a `tapis_job` template. The template
  is a full Tapis job spec with `${...}` placeholders that the engine substitutes
  at run time from the step's `config_values` and run-level options
  (`${slurm_account}`, `${exec_system}`, `${work_dir}`, …). See
  [`backend/steps/preprocessing/step.json`](../backend/steps/preprocessing/step.json)
  for a complete, working example.

## 5. Load it

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

## 6. Verify

- The step appears in the canvas palette under its `category`.
- Drag it in, connect its ports, click the **gear** → the auto-generated form
  shows your fields → Save persists them onto the node.

---

## Naming note (important)

`step_type_key` is the step's identity everywhere. Keep it stable — renaming it
orphans saved templates/runs that reference the old key. If you later add a custom
UI panel, the frontend registry key must match this string **exactly** (hyphens
vs. underscores included).

## Gotchas

- **Nothing shows in the palette** → check the startup log for a `SKIPPING step`
  message (invalid output contract) or a JSON parse error.
- **Foreign-key error on a port** → you referenced a port data type that couldn't
  be created; check the type spelling.
- **`config_schema: {}`** is fine — the form just shows "No configuration
  available for this step."
