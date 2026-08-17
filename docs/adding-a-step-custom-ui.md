# Adding a step with a custom UI panel

Some steps need more than a form — an *interactive panel* (a map picker, an image
editor, a live preview). The app resolves the gear/Settings icon to a **custom
panel** you register for the step type, falling back to the auto-generated form
for everything else. Both are "pages" implementing one shared contract.

**Prerequisite:** the step must exist in the backend first. Create its
`step.json` exactly as in [adding-a-step-form.md](./adding-a-step-form.md) (ports,
category, `config_schema`, optional `tapis_job`). Everything below is the *extra*
frontend work to replace the auto-form with your own UI.

**A custom panel changes only the editing UI.** Ports, run configuration and
runtime execution behave identically to a form step — those are backend concerns,
documented once in the form doc:

- **Ports** (declaring, reading inputs, producing outputs, passthrough) →
  [form doc §3](./adding-a-step-form.md#3-ports--the-whole-data-flow-story)
- **Run configuration** (run-level + per-node) →
  [form doc §5](./adding-a-step-form.md#5-run-configuration--what-the-platform-gives-every-step)
- **Runtime lifecycle** →
  [form doc §6](./adding-a-step-form.md#6-what-happens-to-your-step-at-run-time)

§7 below covers only what's *different* when a panel is involved.

---

## 1. Write the panel component

Create a component under `frontend/app/pages/` that implements
[`StepPanelProps`](../frontend/app/pages/types.ts). It reads the current config
and reports changes via `onChange`; the host modal owns Save/Cancel.

```tsx
// frontend/app/pages/my_step.tsx
import { TextInput } from "@mantine/core";
import type { StepPanelProps } from "./types";

export default function MyStepPanel({ config, onChange, step }: StepPanelProps) {
  // Read each value from config, falling back to the schema default.
  const val = (k: string) => config[k] ?? step.config_schema[k]?.default ?? "";
  const set = (k: string, v: unknown) => onChange({ ...config, [k]: v });

  return (
    <TextInput
      label="Prompt"
      value={String(val("prompt"))}
      onChange={(e) => set("prompt", e.currentTarget.value)}
    />
  );
}
```

### The `StepPanelProps` contract

| Prop | Purpose |
| --- | --- |
| `config` | Current working config for this node. Read values from here. |
| `onChange(next)` | Report an updated config object. Call on every edit. |
| `step` | `StepMeta`: `step_type_key`, `display_name`, `category`, `config_schema`, `inputs`, `outputs`, `submits_job`. |
| `nodeId` | The canvas node id being configured. |
| `connectedInputs` | Upstream connections feeding this node's inputs, keyed by **this node's** input `port_name` (see §2). |
| `runId` | The run this panel is being viewed against, when opened from the run page. `undefined` at design time. |
| `templateVersionId` | The template version (undefined for an unsaved template). |
| `onSave` / `onClose` | Trigger the modal's Save / Cancel from your own control if you want. |

Only values you put into `config` are persisted — they save to the node's
`config_values` (→ `wf_node.default_config` on save, frozen at run time). That
includes the reserved Run Configuration keys, which live in the same dict — so
**always spread**: `onChange({ ...config, [k]: v })`, never `onChange({ [k]: v })`,
or you'll wipe a user's compute settings along with everything else.

Ports are read-only here: `step.inputs` / `step.outputs` are `{ port_name,
data_type }` for rendering, but a panel cannot add ports — those are declared in
`step.json`.

## 2. Reading upstream inputs at design time (`connectedInputs`)

A panel often needs to know what it's wired to — e.g. a labeling tool that must
browse the image directory a source node points at, *before* any run exists.

`connectedInputs` is keyed by **this node's input port name**:

```tsx
const images = connectedInputs["images"];
if (images) {
  // images.sourceNodeId, images.sourceType (upstream step_type_key),
  // images.sourcePort (upstream OUTPUT port name),
  // images.config      (upstream node's config_values — e.g. its `path`/`system`)
  const upstreamPath = images.config?.path ?? "";
}
```

> ⚠️ **Design-time only, and only *directly* wired nodes.** `config` is the
> upstream node's saved configuration — so a source node's `path` is there, but a
> value **produced by an upstream job** is a runtime artifact that does not exist
> in the editor. If your panel needs a real produced file, it must be opened
> against a run (`runId`, §7) or read the path from the run page instead.

## 3. Register it

Add one line to [`frontend/app/pages/registry.ts`](../frontend/app/pages/registry.ts),
mapping the **exact** `step_type_key` to the component:

```ts
import MyStepPanel from "./my_step";

export const stepPanels: Record<string, ComponentType<StepPanelProps>> = {
  heatmap: HeatmapPanel,
  my_step: MyStepPanel,   // <- key MUST equal step_type_key exactly
};
```

> ⚠️ **The #1 mistake:** the registry key must be byte-for-byte the step's
> `step_type_key`, including **hyphens vs. underscores**. `"image-preprocess-studio"`
> ≠ `image_playground`. A mismatch silently falls back to the generic form. Keys
> with hyphens must be quoted.

That's the whole wiring — nothing in `CustomNode.tsx` or `StepSettingsModal.tsx`
changes when you add a panel.

## 4. Sizing the modal (optional)

By default the panel opens in a centered `lg` modal. A panel can request more room
via a **static property** the host honors:

```tsx
// a wider centered modal
(MyStepPanel as any).modalSize = "80%";

// OR a full-screen surface (for a panel that renders its own full-height layout,
// e.g. a Mantine AppShell-based editor)
(MyStepPanel as any).fullScreen = true;
```

Full-screen panels are rendered edge-to-edge with a floating **Cancel / Save &
Close** bar (see [`StepSettingsModal.tsx`](../frontend/app/components/StepSettingsModal.tsx)).

## 5. Talking to the backend

Panels are full React components — they can hold state, render previews, and call
the backend.

- For normal app calls, use [`apiFetch`](../frontend/app/lib/api.ts) — it targets
  the configured backend URL and sends the auth session cookie, and redirects to
  login on a 401.
- If a 401 shouldn't bounce the whole app to login (e.g. "you're logged in but
  lack a Tapis token to browse files"), call `fetch` with
  `credentials: "include"` against `BACKEND_URL` directly and handle the status
  inline — see the `studioFetch` helper in
  [`imagePreprocessStudio.tsx`](../frontend/app/pages/imagePreprocessStudio.tsx).

Need a new backend capability? Add an endpoint in `backend/main.py` guarded by
`Depends(get_current_user)`. The **Tapis Files proxy** endpoints
(`/api/tapis-files/list|content|upload`) are a good template: they use the
logged-in user's Tapis token via `tapis_auth.get_token_for_user`.

## 6. SSR / heavy or browser-only libraries

The frontend server-renders by default. If your panel imports a library that only
works in the browser (WASM, canvas, `window`), load it **client-only** so it never
runs during SSR:

```tsx
import { useEffect, useState, Suspense, lazy } from "react";
const Heavy = lazy(async () => ({ default: (await import("some-browser-lib")).Thing }));

function Panel() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted ? <Suspense fallback={<Loader/>}><Heavy/></Suspense> : <Loader/>;
}
```

Use `import type { ... }` for types from such packages (type imports are erased,
so they don't pull the package into the SSR bundle). If a dependency uses Vite
`?url`/asset imports and breaks dep optimization, add it to
`optimizeDeps.exclude` in `vite.config.ts`.

---

## 7. Ports, run config and runtime — what's panel-specific

Everything here builds on the form doc sections linked at the top. Only the
differences are repeated.

### Your panel usually owns an output port's location

Most panel-driven steps are **design-time only** (`"tapis_job": null`,
`"submits_job": false`). They never submit a job, so their outputs don't come from
an archive dir — they come from **whatever `path` the panel wrote into `config`**
(`_source_node_outputs`, form doc §3.3).

Practically: if your panel writes a file to Tapis (via the Files proxy), store the
destination in `config.path` and declare an output port for it. A `tapis_path`
config field is the easiest way to let the user choose that location — it stores
the system under the separate `system` key that the backend joins into
`tapis://<system>/<path>`.

> ⚠️ **The passthrough naming rule bites panels hardest.** An output port whose
> name matches one of this node's **input** port names publishes that input
> unchanged instead of your `path`. `smart_labeler` relies on this deliberately for
> its `images` output — and had a real bug from it: its optional input had to be
> renamed `resume_annotations`, because calling it `annotations` collided with the
> `annotations` *output* and silently made that output resolve through an unwired
> input rather than the panel's saved path. Both the backend and `CustomNode`'s
> `resolveOutputPath` apply this rule. See form doc §3.3.

### Work that must happen during the run

A panel edits config at design time; it isn't running when the DAG executes. If
your step needs real work to happen *during* the run, register a backend handler
in [`engine/inline_steps.py`](../backend/engine/inline_steps.py):

- **`HANDLERS`** — for a step with no `tapis_job` whose output must actually exist
  by the time a downstream job stages it (e.g. `annotation_format_adapter`
  converting a file an upstream step only just produced).
- **`PRE_SUBMIT_HANDLERS`** — for a step that *does* submit a job but needs a file
  materialized on Tapis first, typically turning something the panel edited
  in-browser into the file the job expects to stage (e.g.
  `image-preprocess-studio`). It runs with the full render context and its return
  value overrides context keys before `render()`, so the template's placeholder
  resolves to what was actually written.

### Run Configuration is not yours to render

The CPU icon opens [`RunConfigModal`](../frontend/app/components/RunConfigModal.tsx)
separately from your panel. Don't build compute controls — just don't clobber the
reserved keys (§1). The icon is hidden for steps with no `tapis_job`; override
with `"submits_job": true/false` in step.json.

### Panels are shown on the run page too

If a design-time-only step has a registered panel, that panel is **also** shown
when its node is clicked on `/runs/:runId`, against the run's resolved config —
with `runId` set. Steps that *do* submit a job keep the run page's usual logs view
instead.

Handle `runId === undefined` gracefully: it means "opened from the canvas, no run
exists yet". A panel whose data is run-scoped (e.g. `geospatialMap`, which reads a
completed run's generated GeoPackage) should render an empty/waiting state rather
than erroring.

## 8. Reference implementations

- **Simple custom panel** — [`heatmap.tsx`](../frontend/app/pages/heatmap.tsx):
  grouped fields + a live preview computed from the values.
- **Design-time step that writes its own output** —
  [`smartLabeler.tsx`](../frontend/app/pages/smartLabeler.tsx): the passthrough
  output + own-`path` output pattern described above.
- **Full editor** — [`imagePreprocessStudio.tsx`](../frontend/app/pages/imagePreprocessStudio.tsx):
  full-screen, a lazy client-only third-party component, a directory browser backed
  by the Tapis Files proxy, a save-to-Tapis action, and a `PRE_SUBMIT_HANDLERS`
  counterpart on the backend.

## 9. Verify

```bash
cd frontend
npm run typecheck   # no new errors in your panel / pages / StepSettingsModal
npm run build       # confirms it bundles (and SSR doesn't import browser-only libs)
npm run dev
```

Then drag your step onto the canvas and click the **gear** — your panel should
render instead of the auto-form. If it shows the generic form instead, the
registry key doesn't match the `step_type_key` (see §3).

---

## Checklist

- [ ] `backend/steps/<key>/step.json` exists (see the form doc) and syncs on
      startup.
- [ ] Panel component in `frontend/app/pages/` implements `StepPanelProps`.
- [ ] Registered in `registry.ts` under the **exact** `step_type_key`.
- [ ] `onChange` always spreads `...config` (doesn't drop Run Configuration keys).
- [ ] No output port accidentally shares a name with an input port (§7).
- [ ] Panel handles `runId === undefined` (design time) and missing
      `connectedInputs`.
- [ ] (Optional) `modalSize` / `fullScreen` set for large panels.
- [ ] (Optional) backend endpoint added for any data the panel fetches.
- [ ] (Optional) inline / pre-submit handler registered if work must happen during
      the run.
- [ ] `npm run typecheck` and `npm run build` pass.
