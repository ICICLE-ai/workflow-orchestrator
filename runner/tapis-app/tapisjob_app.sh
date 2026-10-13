#!/bin/bash
#
# Tapis ZIP-runtime entrypoint for wf-runner: runs a whole workflow pipeline as
# ONE Tapis job (see backend/engine/single_job.py).
#
# Tapis unpacks this app's zip into the job directory, makes this script
# executable and runs it with the job's appArgs appended. The platform sends:
#
#   pipeline.json --data-root data --workdir output --images-dir <dir> --run-id <n>
#
# so every path is relative to the job directory unless the images directory is
# an absolute path on the exec system (WF_RUNNER_IMAGES_DIR).
#
# WHY ZIP AND NOT A CONTAINER APP: the runner's job is to invoke apptainer once
# per step. If the runner itself ran inside a container, that would be a nested
# container launch, which is disabled on many hardened HPC kernels. The runner is
# pure Python standard library precisely so it can run directly on the compute
# node, where `apptainer` is a normal command.
set -euo pipefail

cd "$(dirname "$0")"

echo "[wf-runner] host: $(hostname)"
echo "[wf-runner] job dir: $PWD"
echo "[wf-runner] args: $*"

# The runner needs python3 and a container runtime. Both are normally present on
# a compute node; a site that exposes them through modules can set
# WF_RUNNER_MODULES (space-separated) on the app or the job.
if [ -n "${WF_RUNNER_MODULES:-}" ]; then
  # shellcheck disable=SC1091
  if command -v module >/dev/null 2>&1 || [ -f /etc/profile.d/modules.sh ]; then
    [ -f /etc/profile.d/modules.sh ] && source /etc/profile.d/modules.sh
    for m in ${WF_RUNNER_MODULES}; do
      echo "[wf-runner] module load ${m}"
      module load "${m}" || echo "[wf-runner] WARNING: could not load module ${m}"
    done
  fi
fi

PYTHON="${WF_RUNNER_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || {
  echo "[wf-runner] ERROR: $PYTHON not found on this node. Set WF_RUNNER_PYTHON or WF_RUNNER_MODULES." >&2
  exit 1
}
RUNTIME="${WF_RUNNER_RUNTIME:-apptainer}"
command -v "$RUNTIME" >/dev/null 2>&1 || {
  echo "[wf-runner] ERROR: container runtime '$RUNTIME' not found on this node." >&2
  echo "[wf-runner]        Set WF_RUNNER_RUNTIME (e.g. singularity) or WF_RUNNER_MODULES." >&2
  exit 1
}

# The workdir is the job's own output directory, which Tapis archives — so the
# per-step job dirs and the run report end up in the run's archive without this
# script copying anything. Created here because the runner is handed a relative
# path and Tapis does not guarantee the directory exists yet.
mkdir -p output

exec "$PYTHON" -m wf_runner "$@" --runtime "$RUNTIME"
