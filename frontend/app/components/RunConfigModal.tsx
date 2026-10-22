import { useEffect, useState } from "react";
import { Modal, Stack, Group, Button, NumberInput, Text, TextInput, Select, Badge, Divider, Loader } from "@mantine/core";
import { TAPIS_SYSTEMS } from "../lib/tapis";
import { apiFetch } from "../lib/api";

// Defaults for a step's compute resources — applied to any node that hasn't
// customized them yet. These become the Tapis job's nodeCount/coresPerNode/
// memoryMB/maxMinutes fields (and a `-G <n>` schedulerOption for GPUs),
// overriding whatever the step type's own job template specifies.
//
// execSystem/execQueue default to the shared Expanse static system. Clearing
// the system in the modal still means "inherit the run's target" (its GPU pair
// when the step's step.json declares resources.gpu, else its CPU pair — see
// engine/transactions.py's resolve_node_exec_target). execWorkDir empty means
// the system's own derived job directory (transactions._exec_system_dirs).
export const RUN_CONFIG_DEFAULTS = {
  nodeCount: 1,
  coresPerNode: 8,
  memoryMB: 800,
  maxMinutes: 30,
  gpus: 0,
  execSystem: "expanse-tapis-static",
  execQueue: "tapisShared",
  execWorkDir: "",
};

// A step declaring resources.gpu in its step.json starts on the shared GPU
// queue with one GPU requested.
export const GPU_RUN_CONFIG_DEFAULTS = {
  ...RUN_CONFIG_DEFAULTS,
  gpus: 1,
  execQueue: "tapisGPUShared",
};

export type RunConfigValues = typeof RUN_CONFIG_DEFAULTS;

export function runConfigDefaults(gpu?: boolean): RunConfigValues {
  return gpu ? GPU_RUN_CONFIG_DEFAULTS : RUN_CONFIG_DEFAULTS;
}

export function readRunConfig(config: Record<string, any>, gpu?: boolean): RunConfigValues {
  const defaults = runConfigDefaults(gpu);
  return {
    nodeCount: config?.nodeCount ?? defaults.nodeCount,
    coresPerNode: config?.coresPerNode ?? defaults.coresPerNode,
    memoryMB: config?.memoryMB ?? defaults.memoryMB,
    maxMinutes: config?.maxMinutes ?? defaults.maxMinutes,
    gpus: config?.gpus ?? defaults.gpus,
    execSystem: config?.execSystem ?? defaults.execSystem,
    execQueue: config?.execQueue ?? defaults.execQueue,
    execWorkDir: config?.execWorkDir ?? defaults.execWorkDir,
  };
}

// Per-step compute-resource form — separate from the step's business
// StepSettingsModal (which edits config_schema params like conf/imgsz). The
// values are stored in the same node config_values dict, under reserved keys
// the orchestrator applies on top of the step's Tapis job template.
//
// The reserved keys are camelCase on purpose: the engine substitutes
// snake_case ${...} placeholders from a context dict that node config spreads
// into, so a key named `exec_system` would shadow the run's resolved value
// while leaving the derived archive/exec directories computed from the OTHER
// system. See _AUTHORITATIVE_CTX_KEYS in engine/workflows.py.
export default function RunConfigModal({
  opened,
  onClose,
  initialConfig,
  onSave,
  stepResources,
}: {
  opened: boolean;
  onClose: () => void;
  initialConfig: Record<string, any>;
  onSave: (values: RunConfigValues) => void;
  // The step type's declared requirement from step.json ({"gpu": true} for
  // zero_shot_annotation, training, …) — shown so it's clear WHY a node
  // inherits the GPU target rather than the CPU one.
  stepResources?: { gpu?: boolean };
}) {
  const [values, setValues] = useState<RunConfigValues>(() => runConfigDefaults(stepResources?.gpu));

  // Reseed the working copy whenever the modal is (re)opened for this node.
  useEffect(() => {
    if (opened) setValues(readRunConfig(initialConfig, stepResources?.gpu));
  }, [opened, initialConfig, stepResources?.gpu]);

  // Queues belong to a specific system, so the list is fetched for whichever
  // system this NODE overrode to — not the run's. Skipped entirely while
  // execSystem is empty (inheriting), since there's no node-specific system to
  // query and the queue field is disabled anyway.
  const [queues, setQueues] = useState<any[]>([]);
  const [queuesLoading, setQueuesLoading] = useState(false);
  useEffect(() => {
    if (!opened || !values.execSystem) {
      setQueues([]);
      return;
    }
    let cancelled = false;
    setQueuesLoading(true);
    apiFetch(`/api/tapis-systems/${values.execSystem}/queues`)
      .then((res) => (res.ok ? res.json() : { queues: [] }))
      .then((d) => {
        if (!cancelled) setQueues(d.queues || []);
      })
      .catch(() => {
        if (!cancelled) setQueues([]);
      })
      .finally(() => {
        if (!cancelled) setQueuesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [opened, values.execSystem]);

  // Queue limits for the selected queue, so an over-request is caught here
  // rather than by a Tapis rejection minutes into a run.
  const selectedQueue = queues.find((q: any) => q.name === values.execQueue);
  const limitWarnings: string[] = [];
  if (selectedQueue) {
    if (selectedQueue.maxNodeCount != null && values.nodeCount > selectedQueue.maxNodeCount) {
      limitWarnings.push(`node count exceeds the queue's max of ${selectedQueue.maxNodeCount}`);
    }
    if (selectedQueue.maxCoresPerNode != null && values.coresPerNode > selectedQueue.maxCoresPerNode) {
      limitWarnings.push(`cores per node exceeds the queue's max of ${selectedQueue.maxCoresPerNode}`);
    }
    if (selectedQueue.maxMinutes != null && values.maxMinutes > selectedQueue.maxMinutes) {
      limitWarnings.push(`max runtime exceeds the queue's limit of ${selectedQueue.maxMinutes} min`);
    }
  }

  const inheriting = !values.execSystem;

  return (
    <Modal opened={opened} onClose={onClose} title="Run Configuration" size="md">
      <Stack>
        <Group gap="xs" align="center">
          <Text size="sm" c="dimmed" style={{ flex: 1 }}>
            Where this step runs, and the compute it requests.
          </Text>
          <Badge size="sm" variant="light" color={stepResources?.gpu ? "grape" : "gray"}>
            {stepResources?.gpu ? "GPU step" : "CPU step"}
          </Badge>
        </Group>

        <Select
          label="Execution system"
          description={
            inheriting
              ? `Inheriting the run's ${stepResources?.gpu ? "GPU" : "CPU"} target. Pick one to pin this step to its own system.`
              : "This step is pinned to its own system, ignoring the run's target."
          }
          data={TAPIS_SYSTEMS}
          value={values.execSystem || null}
          placeholder={`Inherit the run's ${stepResources?.gpu ? "GPU" : "CPU"} target`}
          clearable
          onChange={(v) =>
            // Clearing the system must clear the queue too: a queue name only
            // exists on the system it belongs to, so keeping e.g. OSC's "gpu"
            // while inheriting an Expanse target submits a queue that isn't
            // there. The same goes for the job directory: a path only means
            // something on the system it was written for.
            setValues((p) => ({ ...p, execSystem: v ?? "", execQueue: "", execWorkDir: "" }))
          }
        />
        <Select
          label="Queue"
          description={
            inheriting
              ? "Available once this step is pinned to a system."
              : queuesLoading
                ? "Loading queues from the system…"
                : "Leave empty to use the system's own default queue."
          }
          data={Array.from(new Set([
            ...queues.map((q: any) => q.name).filter(Boolean),
            // A default queue (e.g. tapisShared) must still show while the
            // list loads or if the system doesn't publish it.
            ...(values.execQueue ? [values.execQueue] : []),
          ]))}
          value={values.execQueue || null}
          placeholder={inheriting ? "—" : queuesLoading ? "Loading…" : "System default"}
          clearable
          disabled={inheriting || queuesLoading}
          rightSection={queuesLoading ? <Loader size={14} /> : undefined}
          onChange={(v) => setValues((p) => ({ ...p, execQueue: v ?? "" }))}
        />
        <TextInput
          label="Job working directory"
          description={
            inheriting
              ? "Available once this step is pinned to a system."
              : "Where the job runs, on the execution system (relative to its root dir). Each job gets its own subfolder. Leave empty for the system's default."
          }
          placeholder={inheriting ? "—" : "System default"}
          disabled={inheriting}
          value={values.execWorkDir}
          onChange={(e) => {
            const v = e.currentTarget.value;
            setValues((p) => ({ ...p, execWorkDir: v }));
          }}
        />

        <Divider label="Compute" labelPosition="left" />

        <Group grow>
          <NumberInput
            label="Node count"
            min={1}
            value={values.nodeCount}
            onChange={(v) => setValues((p) => ({ ...p, nodeCount: Number(v) || 1 }))}
          />
          <NumberInput
            label="Cores per node"
            min={1}
            value={values.coresPerNode}
            onChange={(v) => setValues((p) => ({ ...p, coresPerNode: Number(v) || 1 }))}
          />
        </Group>
        <Group grow>
          <NumberInput
            label="Memory (MB)"
            min={1}
            value={values.memoryMB}
            onChange={(v) => setValues((p) => ({ ...p, memoryMB: Number(v) || 1 }))}
          />
          <NumberInput
            label="Max runtime (minutes)"
            min={1}
            value={values.maxMinutes}
            onChange={(v) => setValues((p) => ({ ...p, maxMinutes: Number(v) || 1 }))}
          />
        </Group>
        <NumberInput
          label="GPUs"
          description={
            stepResources?.gpu && values.gpus === 0
              ? "This step declares a GPU requirement — 0 removes the -G request from its job."
              : undefined
          }
          min={0}
          value={values.gpus}
          onChange={(v) => setValues((p) => ({ ...p, gpus: Number(v) || 0 }))}
        />

        {limitWarnings.length > 0 && (
          <Badge
            size="sm"
            variant="light"
            color="yellow"
            style={{ height: "auto", whiteSpace: "normal", textTransform: "none", lineHeight: 1.4, padding: "6px 10px" }}
          >
            {`Queue "${values.execQueue}": ${limitWarnings.join("; ")}.`}
          </Badge>
        )}

        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button onClick={() => onSave(values)}>Save</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
