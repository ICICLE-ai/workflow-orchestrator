import { useEffect, useState } from "react";
import { apiFetch } from "./api";
import { DEFAULT_TAPIS_SYSTEM } from "./tapis";

// The user's own run defaults (header -> Run defaults; GET/PUT
// /api/me/run-defaults). Shared app-wide so every place that pre-fills a
// system, queue or work dir — Run settings, a step's Run Configuration, path
// fields — starts from them. Anything already saved on a node still wins; these
// only replace the built-in fallbacks.
export interface RunDefaults {
  exec_system?: string;
  exec_queue?: string;
  gpu_exec_queue?: string;
  work_dir?: string;
  containers_dir?: string;
}

let cache: RunDefaults | null = null;
let inflight: Promise<RunDefaults> | null = null;
const listeners = new Set<(d: RunDefaults) => void>();

function load(): Promise<RunDefaults> {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = apiFetch("/api/me/run-defaults")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => (cache = d?.defaults || {}))
      .catch(() => ({}))
      .finally(() => { inflight = null; });
  }
  return inflight;
}

// Called after the defaults are saved, so open panels pick them up at once.
export function setRunDefaultsCache(d: RunDefaults) {
  cache = d;
  listeners.forEach((fn) => fn(d));
}

export function useRunDefaults(): RunDefaults {
  const [defaults, setDefaults] = useState<RunDefaults>(cache || {});
  useEffect(() => {
    let alive = true;
    load().then((d) => alive && setDefaults(d));
    listeners.add(setDefaults);
    return () => { alive = false; listeners.delete(setDefaults); };
  }, []);
  return defaults;
}

// The system a path field or panel falls back to when nothing chose one.
export function useDefaultSystem(): string {
  return useRunDefaults().exec_system || DEFAULT_TAPIS_SYSTEM;
}

// Map the defaults onto Run settings fields. One system serves the CPU target,
// the GPU target and the archive; only fields the user filled in are returned,
// so blanks keep whatever the caller already has.
export function runDefaultsToOptions(d: RunDefaults): Record<string, string> {
  const out: Record<string, string> = {};
  if (d.exec_system) {
    out.exec_system = d.exec_system;
    out.gpu_exec_system = d.exec_system;
    out.archive_system = d.exec_system;
  }
  if (d.exec_queue) out.exec_queue = d.exec_queue;
  if (d.gpu_exec_queue) out.gpu_exec_queue = d.gpu_exec_queue;
  if (d.work_dir) out.work_dir = d.work_dir;
  if (d.containers_dir) out.containers_dir = d.containers_dir;
  return out;
}
