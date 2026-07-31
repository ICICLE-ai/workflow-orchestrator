// Shared Tapis constants used across step panels.

// Selectable Tapis execution/storage systems.
export const TAPIS_SYSTEMS = [
  "pitzer-tapis",
  "expanse-tapis",
  "cardinal-tapis",
  "ascend-tapis",
  "expanse-tapis-static",
];

// Default system for all steps. Override per-deployment with VITE_DEFAULT_TAPIS_SYSTEM
// in frontend/.env; otherwise falls back to expanse-tapis-static. (A DB/config-sourced
// default could replace this later.)
export const DEFAULT_TAPIS_SYSTEM: string =
  import.meta.env.VITE_DEFAULT_TAPIS_SYSTEM ?? "expanse-tapis-static";

// OSC systems share one scratch layout, keyed by the run's Slurm allocation.
const OSC_SCRATCH_SYSTEMS = ["pitzer-tapis", "cardinal-tapis", "ascend-tapis"];

// The base directory a run's Tapis jobs execute/archive under is fixed per
// exec system (not a free-form path) — each system has its own scratch/project
// layout. `slurmAccount` is used for the OSC systems; `username` (the Tapis
// username) is used for expanse-tapis's per-user scratch path.
export function defaultWorkDir(system: string, ctx: { slurmAccount?: string; username?: string }): string {
  if (OSC_SCRATCH_SYSTEMS.includes(system)) {
    return `/fs/scratch/${ctx.slurmAccount || ""}/jobs/`;
  }
  if (system === "expanse-tapis-static") {
    return "/jobs/";
  }
  if (system === "expanse-tapis") {
    return `/expanse/lustre/scratch/${ctx.username || ""}/temp_project/jobs/`;
  }
  return "";
}
