import { useEffect, useState } from "react";
import { Modal, Stack, Group, Button } from "@mantine/core";
import type { StepMeta, ConnectedInput } from "../pages/types";
import { getStepPanel } from "../pages/registry";
import GenericConfigForm from "../pages/GenericConfigForm";

// Host for a step's settings UI. Opened by the gear icon on a node. Resolves the
// step type to its registered custom page (registry.ts) or falls back to the
// generic auto-form, owns the working-config state and the modal shell, and
// persists on Save via onSave(config).
export default function StepSettingsModal({
  opened,
  onClose,
  nodeId,
  step,
  initialConfig,
  templateVersionId,
  connectedInputs = {},
  onSave,
}: {
  opened: boolean;
  onClose: () => void;
  nodeId: string;
  step: StepMeta;
  initialConfig: Record<string, any>;
  templateVersionId?: number;
  connectedInputs?: Record<string, ConnectedInput>;
  onSave: (config: Record<string, any>) => void;
}) {
  const [config, setConfig] = useState<Record<string, any>>(initialConfig || {});

  // Reseed the working copy whenever the modal is (re)opened for this node, so
  // reopening after a cancel doesn't show stale edits.
  useEffect(() => {
    if (opened) setConfig(initialConfig || {});
  }, [opened, initialConfig]);

  const Panel = getStepPanel(step.step_type_key) ?? GenericConfigForm;

  // A panel may declare a preferred modal size via static `modalSize`, or request
  // a full-screen surface via static `fullScreen` (e.g. an embedded editor that
  // renders its own full-height AppShell). Defaults to a "lg" centered modal.
  const size = (Panel as any).modalSize ?? "lg";
  const fullScreen = Boolean((Panel as any).fullScreen);

  const handleSave = () => onSave(config);

  const panel = (
    <Panel
      config={config}
      onChange={setConfig}
      step={step}
      nodeId={nodeId}
      templateVersionId={templateVersionId}
      connectedInputs={connectedInputs}
      onSave={handleSave}
      onClose={onClose}
    />
  );

  // Full-screen panels own the whole surface; render them edge-to-edge with a
  // floating action bar (a centered modal's Stack/footer would be covered by the
  // panel's own 100vh layout).
  if (fullScreen) {
    return (
      <Modal
        opened={opened}
        onClose={onClose}
        fullScreen
        padding={0}
        withCloseButton={false}
        styles={{ body: { height: "100dvh" } }}
      >
        {panel}
        <Group
          gap="xs"
          style={{ position: "fixed", bottom: 16, right: 16, zIndex: 10001 }}
        >
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button onClick={handleSave}>Save &amp; Close</Button>
        </Group>
      </Modal>
    );
  }

  return (
    <Modal opened={opened} onClose={onClose} title={`${step.display_name} Configuration`} size={size}>
      <Stack>
        {panel}
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button onClick={handleSave}>Save Configuration</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
