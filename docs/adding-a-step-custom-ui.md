# Adding a step with a custom UI panel

Some steps need more than a form — an *interactive panel* (a map picker, an image
editor, a live preview). The app resolves the gear/Settings icon to a **custom
panel** you register for the step type, falling back to the auto-generated form
for everything else. Both are "pages" implementing one shared contract.

**Prerequisite:** the step must exist in the backend first. Create its
`step.json` exactly as in [adding-a-step-form.md](./adding-a-step-form.md) (ports,
category, `config_schema`, optional `tapis_job`). Everything below is the *extra*
frontend work to replace the auto-form with your own UI.

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
| `step` | Step metadata: `step_type_key`, `display_name`, `config_schema`, `inputs`, `outputs`, `category`. |
| `nodeId` | The canvas node id being configured. |
| `templateVersionId` | The template version (undefined for an unsaved template). |
| `onSave` / `onClose` | Trigger the modal's Save / Cancel from your own control if you want. |

Only values you put into `config` are persisted — they save to the node's
`config_values` (→ `wf_node.default_config` on save, frozen at run time).

## 2. Register it

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

## 3. Sizing the modal (optional)

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

## 4. Talking to the backend

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

## 5. SSR / heavy or browser-only libraries

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

## 6. Reference implementations

- **Simple custom panel** — [`heatmap.tsx`](../frontend/app/pages/heatmap.tsx):
  grouped fields + a live preview computed from the values.
- **Full editor** — [`imagePreprocessStudio.tsx`](../frontend/app/pages/imagePreprocessStudio.tsx):
  full-screen, a lazy client-only third-party component, a directory browser backed
  by the Tapis Files proxy, and a save-to-Tapis action.

## 7. Verify

```bash
cd frontend
npm run typecheck   # no new errors in your panel / pages / StepSettingsModal
npm run build       # confirms it bundles (and SSR doesn't import browser-only libs)
npm run dev
```

Then drag your step onto the canvas and click the **gear** — your panel should
render instead of the auto-form. If it shows the generic form instead, the
registry key doesn't match the `step_type_key` (see §2).

---

## Checklist

- [ ] `backend/steps/<key>/step.json` exists (see the form doc) and syncs on
      startup.
- [ ] Panel component in `frontend/app/pages/` implements `StepPanelProps`.
- [ ] Registered in `registry.ts` under the **exact** `step_type_key`.
- [ ] (Optional) `modalSize` / `fullScreen` set for large panels.
- [ ] (Optional) backend endpoint added for any data the panel fetches.
- [ ] `npm run typecheck` and `npm run build` pass.
