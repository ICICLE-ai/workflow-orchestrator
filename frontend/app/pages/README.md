# Step settings pages

Each step node in the workflow canvas has a **gear/Settings** icon. Clicking it
opens a modal that renders a **page** for configuring that step. A page is either:

- the **generic form** ([`GenericConfigForm.tsx`](./GenericConfigForm.tsx)) —
  auto-generated from the step's `config_schema`. This is the default and needs
  no code; every step gets it for free.
- a **custom interactive panel** — a component you write for a specific step type
  (e.g. a grid configurator with a live preview). Registered in
  [`registry.ts`](./registry.ts).

Both implement the same contract, [`StepPanelProps`](./types.ts), and both read
and write the node's `config_values` (which becomes `wf_node.default_config` when
the template is saved, and is frozen into the run at execution time).

## Add a custom panel

1. **Create the component** under `app/pages/`, implementing `StepPanelProps`:

   ```tsx
   // app/pages/my_step.tsx
   import type { StepPanelProps } from "./types";

   export default function MyStepPanel({ config, onChange, step }: StepPanelProps) {
     const set = (k: string, v: unknown) => onChange({ ...config, [k]: v });
     return (
       <input
         value={config.some_field ?? step.config_schema.some_field?.default ?? ""}
         onChange={(e) => set("some_field", e.currentTarget.value)}
       />
     );
   }
   ```

2. **Register it** in [`registry.ts`](./registry.ts) under the step's
   `step_type_key` (the folder name in `backend/steps/<key>/step.json`):

   ```ts
   import MyStepPanel from "./my_step";
   export const stepPanels = {
     heatmap: HeatmapPanel,
     my_step: MyStepPanel,   // <- one line
   };
   ```

That's it — the gear icon for `my_step` now opens your panel; every other step
keeps the generic form. Nothing in `CustomNode.tsx` or `StepSettingsModal.tsx`
needs to change.

## What a panel receives (`StepPanelProps`)

| Prop | Purpose |
| --- | --- |
| `config` | Current working config for this node. Read values from here. |
| `onChange(next)` | Report an updated config object. Call on every edit. |
| `step` | Step metadata: `step_type_key`, `display_name`, `config_schema`, `inputs`, `outputs`, `category`. |
| `nodeId` | The canvas node id being configured. |
| `templateVersionId` | The template version (undefined for an unsaved template). |
| `onSave` / `onClose` | Trigger the modal's Save / Cancel yourself if you want a custom control. The modal already shows a Save/Cancel footer. |

## Interactive panels & backend access

Panels are full React components, so they can hold local state, render previews,
and call the backend. Use [`apiFetch`](../lib/api.ts) — it targets the configured
backend URL and sends the auth session cookie:

```tsx
import { apiFetch } from "../lib/api";
const res = await apiFetch(`/api/pipeline-runs/${runId}/detail`);
```

See [`heatmap.tsx`](./heatmap.tsx) for a worked example: custom layout, `Select`
inputs, a live grid preview computed from the values, and the `apiFetch` pattern.

## Conventions

- Read each value as `config[key] ?? step.config_schema[key]?.default` so unset
  fields show their schema default.
- Only put values you want persisted into `config` — it's saved verbatim as the
  step's config.
- The modal owns the Save/Cancel footer and the working-config lifecycle; a panel
  just reflects `config` and reports changes via `onChange`.
