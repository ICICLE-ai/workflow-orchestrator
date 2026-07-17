// The standard contract every step settings "page" implements.
//
// A step page is the UI shown when a user clicks the gear/Settings icon on a
// node in the workflow canvas. It reads the step's current config, lets the user
// edit it, and reports changes back up via onChange. The host (StepSettingsModal)
// owns the modal shell + Save/Cancel; a page may also drive Save/Close itself via
// onSave/onClose, and may call the backend (apiFetch) for interactive behavior.
//
// To add a custom page for a step type, implement this interface and register the
// component under its step_type_key in registry.ts. Steps without a registered
// page fall back to GenericConfigForm (an auto-generated form from config_schema).

// A single config field as declared in a step's step.json config_schema.
export interface StepConfigField {
  type: "int" | "float" | "boolean" | "string" | string;
  description?: string;
  default?: unknown;
}

// A port as normalized for the UI (see CustomNode's normalization).
export interface StepPort {
  port_name: string;
  data_type: string;
}

// Everything a page knows about the step type it's configuring.
export interface StepMeta {
  step_type_key: string;
  display_name: string;
  category?: string;
  config_schema: Record<string, StepConfigField>;
  inputs: StepPort[];
  outputs: StepPort[];
}

export interface StepPanelProps {
  // Current working config for this node (starts from the node's config_values).
  config: Record<string, any>;
  // Report an updated config. Pass the full next config object.
  onChange: (next: Record<string, any>) => void;
  // Metadata about the step type being configured.
  step: StepMeta;
  // The canvas node id this config belongs to.
  nodeId: string;
  // The template version this node lives in (undefined for an unsaved template).
  templateVersionId?: number;
  // Persist the working config to the node and close (the modal's Save action).
  onSave: () => void;
  // Close without persisting (the modal's Cancel action).
  onClose: () => void;
}
