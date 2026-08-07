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
  runId,
  onSave,
  viewOnly = false,
}: {
  opened: boolean;
  onClose: () => void;
  nodeId: string;
  step: StepMeta;
  initialConfig: Record<string, any>;
  templateVersionId?: number;
  connectedInputs?: Record<string, ConnectedInput>;
  // The pipeline run this modal is being viewed against (run page only — see
  // StepPanelProps.runId). Undefined at design time.
  runId?: number;
  onSave: (config: Record<string, any>) => void;
  // Read-only host, used by the run page to show a design-time step's custom
  // panel (e.g. smart_labeler, geospatial_map) against a run's resolved config.
  // There's no node to persist edits back to, so the footer offers Close only
  // — no Save button implying changes stick. Panels that persist themselves
  // (e.g. smart_labeler's own "Save annotations.json", which writes straight
  // to Tapis) are unaffected.
  viewOnly?: boolean;
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
      runId={runId}
      onSave={handleSave}
      onClose={onClose}
    />
  );

  // Full-screen panels own the whole surface; render them edge-to-edge with a
  // floating action bar (a centered modal's Stack/footer would be covered by the
  // panel's own 100vh layout).
  //
  // React Flow's own keyboard shortcuts (Delete/Backspace to remove a selected
  // node, etc.) listen globally and only skip an event if its target is inside
  // an element carrying the "nokey" class (see @xyflow/system's
  // isInputDOMNode). Without this, a custom panel's own shortcuts — e.g.
  // smart_labeler's Delete-to-remove-annotation / Escape-to-deselect, from the
  // embedded image-annotation-canvas — fight with the canvas underneath
  // instead of just applying inside the modal.
  //
  // closeOnEscape is also off here: the embedded canvas uses Escape itself
  // (cancel a draw, deselect), and Mantine's default Escape handler would
  // otherwise close the whole modal on top of that — discarding unsaved work.
  // Cancel/Save & Close below are the explicit way out instead.
  if (fullScreen) {
    return (
      <Modal
        opened={opened}
        onClose={onClose}
        fullScreen
        padding={0}
        withCloseButton={false}
        closeOnEscape={false}
        className="nokey"
        styles={{ body: { height: "100dvh" } }}
      >
        {panel}
        <Group
          gap="xs"
          style={{ position: "fixed", bottom: 16, right: 16, zIndex: 10001 }}
        >
          {viewOnly ? (
            <Button onClick={onClose}>Close</Button>
          ) : (
            <>
              <Button variant="default" onClick={onClose}>Cancel</Button>
              <Button onClick={handleSave}>Save &amp; Close</Button>
            </>
          )}
        </Group>
      </Modal>
    );
  }

  return (
    <Modal opened={opened} onClose={onClose} title={viewOnly ? step.display_name : `${step.display_name} Configuration`} size={size} className="nokey">
      <Stack>
        {panel}
        <Group justify="flex-end" mt="md">
          {viewOnly ? (
            <Button onClick={onClose}>Close</Button>
          ) : (
            <>
              <Button variant="default" onClick={onClose}>Cancel</Button>
              <Button onClick={handleSave}>Save Configuration</Button>
            </>
          )}
        </Group>
      </Stack>
    </Modal>
  );
}
