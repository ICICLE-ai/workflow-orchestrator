# Step definitions

Each folder here defines one workflow step, via a `step.json` that the backend
syncs into the step registry on startup. The folder name is conventional — the
step's real identity is the `step_type_key` field inside the file.

**Read these before adding or changing a step:**

- [../../docs/adding-a-step-form.md](../../docs/adding-a-step-form.md) — define a
  step entirely here with a `step.json`; its settings form is generated
  automatically. **Also the reference for ports (declaring them, reading inputs,
  producing outputs, passthrough), run configuration, and how a step is executed
  at run time.**
- [../../docs/adding-a-step-custom-ui.md](../../docs/adding-a-step-custom-ui.md) —
  replace the generated form with a custom interactive React panel.

## Quick orientation

```
backend/steps/<key>/step.json
```

| Kind of step | Shape |
| --- | --- |
| **Job step** | `tapis_app_id` + `tapis_job` template — submits a Tapis job (`yolo_inference`, `training`, `preprocessing`). |
| **Design-time step** | `"tapis_job": null` — configured and wired but submits nothing (`smart_labeler`, `geospatial_map`). |
| **Source / sink** | Publishes a user-entered `path` on its outputs, or copies its input to one (`source_image_dir`, `sink_csv`). |

After editing a `step.json`, **restart the backend** — the registry only syncs at
startup. Watch the log for:

```
Synced registry: <key> (config_schema keys: [...])
```

A `SKIPPING step` line instead means the step failed the output-port contract and
was not registered; see the form guide's §3.2.
