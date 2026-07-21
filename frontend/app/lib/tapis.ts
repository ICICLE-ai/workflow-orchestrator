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
