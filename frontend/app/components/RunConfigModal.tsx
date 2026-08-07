import { useEffect, useState } from "react";
import { Modal, Stack, Group, Button, NumberInput, Text } from "@mantine/core";

// Defaults for a step's compute resources — applied to any node that hasn't
// customized them yet. These become the Tapis job's nodeCount/coresPerNode/
// memoryMB/maxMinutes fields (and a `-G <n>` schedulerOption for GPUs),
// overriding whatever the step type's own job template specifies.
export const RUN_CONFIG_DEFAULTS = {
  nodeCount: 1,
  coresPerNode: 8,
  memoryMB: 64800,
  maxMinutes: 210,
  gpus: 0,
};

export type RunConfigValues = typeof RUN_CONFIG_DEFAULTS;

export function readRunConfig(config: Record<string, any>): RunConfigValues {
  return {
    nodeCount: config?.nodeCount ?? RUN_CONFIG_DEFAULTS.nodeCount,
    coresPerNode: config?.coresPerNode ?? RUN_CONFIG_DEFAULTS.coresPerNode,
    memoryMB: config?.memoryMB ?? RUN_CONFIG_DEFAULTS.memoryMB,
    maxMinutes: config?.maxMinutes ?? RUN_CONFIG_DEFAULTS.maxMinutes,
    gpus: config?.gpus ?? RUN_CONFIG_DEFAULTS.gpus,
  };
}

// Per-step compute-resource form — separate from the step's business
// StepSettingsModal (which edits config_schema params like conf/imgsz). The
// values are stored in the same node config_values dict, under reserved keys
// the orchestrator applies on top of the step's Tapis job template.
export default function RunConfigModal({
  opened,
  onClose,
  initialConfig,
  onSave,
}: {
  opened: boolean;
  onClose: () => void;
  initialConfig: Record<string, any>;
  onSave: (values: RunConfigValues) => void;
}) {
  const [values, setValues] = useState<RunConfigValues>(RUN_CONFIG_DEFAULTS);

  // Reseed the working copy whenever the modal is (re)opened for this node.
  useEffect(() => {
    if (opened) setValues(readRunConfig(initialConfig));
  }, [opened, initialConfig]);

  return (
    <Modal opened={opened} onClose={onClose} title="Run Configuration" size="sm">
      <Stack>
        <Text size="sm" c="dimmed">
          Compute resources requested for this step's Tapis job.
        </Text>
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
        <NumberInput
          label="GPUs"
          min={0}
          value={values.gpus}
          onChange={(v) => setValues((p) => ({ ...p, gpus: Number(v) || 0 }))}
        />
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button onClick={() => onSave(values)}>Save</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
