// Shared Tapis constants used across step panels.

// Selectable Tapis execution/storage systems.
export const TAPIS_SYSTEMS = [
  "pitzer-tapis",
  "expanse-tapis",
  "cardinal-tapis",
  "ascend-tapis",
  "expanse-tapis-static",
];

// The `system` a path field stores for data on the user's own node rather
// than on a Tapis system. Never joined into a tapis:// URI; only runnable via
// "Deploy to my node" (the backend refuses a platform launch). Mirrors
// NODE_LOCAL_SYSTEM in backend/engine/local_bundle.py.
export const NODE_LOCAL_SYSTEM = "node-local";

// The system dropdown for a path field: the Tapis systems, plus "My node".
export const PATH_SYSTEM_OPTIONS = [
  { group: "Tapis systems", items: TAPIS_SYSTEMS },
  { group: "Your own machine", items: [{ value: NODE_LOCAL_SYSTEM, label: "My node (not on Tapis)" }] },
];

// Default system for all steps. Override per-deployment with VITE_DEFAULT_TAPIS_SYSTEM
// in frontend/.env; otherwise falls back to expanse-tapis-static. (A DB/config-sourced
// default could replace this later.)
export const DEFAULT_TAPIS_SYSTEM: string =
  import.meta.env.VITE_DEFAULT_TAPIS_SYSTEM ?? "expanse-tapis-static";

// Split a "tapis://system/abs/path" URI into its parts, or null if `uri`
// isn't one (bare path, empty, etc).
export function splitTapisUri(uri: string): { system: string; path: string } | null {
  if (!uri || !uri.startsWith("tapis://")) return null;
  const rest = uri.slice("tapis://".length);
  const slash = rest.indexOf("/");
  if (slash === -1) return null;
  return { system: rest.slice(0, slash), path: rest.slice(slash) };
}

// Resolve {system, path} for a wired input port — i.e. a StepPanelProps
// `connectedInputs[portName]` entry.
//
// `.config.path` there comes from CustomNode's resolveOutputPath, which
// returns a full "tapis://system/path" URI whenever the upstream node has its
// own system+path config (source_image_dir, smart_labeler, source_json_file,
// ...) — the common case. Split that apart rather than treating `.path` as a
// bare path: pairing a bare `.config.path` with THIS panel's own unrelated
// "system" field (e.g. a save-destination system) rather than the wire's
// actual system is exactly the "directory won't open, defaulted to the wrong
// system" bug this helper exists to prevent. Falls back to the upstream
// node's raw `.config.system` + `.config.path` when the value isn't a URI
// (e.g. the upstream never picked a system). Returns null when nothing
// usable is wired.
export function resolveWiredLocation(
  input: { config?: Record<string, any> } | undefined
): { system: string; path: string } | null {
  const raw = String(input?.config?.path ?? "");
  const fromUri = splitTapisUri(raw);
  if (fromUri) return fromUri;
  const system = String(input?.config?.system ?? "");
  // A "My node" path isn't on any Tapis system, so there's nothing to browse.
  if (system === NODE_LOCAL_SYSTEM) return null;
  return system && raw ? { system, path: raw } : null;
}

// OSC systems share one scratch layout, keyed by the run's Slurm allocation.
const OSC_SCRATCH_SYSTEMS = ["pitzer-tapis", "cardinal-tapis", "ascend-tapis"];

// The base directory a run's Tapis jobs execute/archive under is fixed per
// exec system (not a free-form path) — each system has its own scratch/project
// layout. `slurmAccount` is used for the OSC systems; `username` (the Tapis
// username) is used for expanse-tapis's per-user scratch path.
export function defaultWorkDir(system: string, ctx: { slurmAccount?: string; username?: string }): string {
  if (OSC_SCRATCH_SYSTEMS.includes(system)) {
    // harvest_jobs, not jobs: /fs/scratch/<acct>/jobs is a pre-existing folder
    // owned by one project member (0755), so nobody else could create wf_runs/
    // under it (FILES_CLIENT_SSH_PERM_DENIED at mkdir). harvest_jobs is
    // world-writable and already holds the jobs' exec dirs (see the backend's
    // _exec_system_dirs). Keep in sync with transactions._default_work_dir.
    return `/fs/scratch/${ctx.slurmAccount || ""}/harvest_jobs/`;
  }
  if (system === "expanse-tapis-static") {
    return "/jobs/";
  }
  if (system === "expanse-tapis") {
    return `/expanse/lustre/scratch/${ctx.username || ""}/temp_project/jobs/`;
  }
  return "";
}
