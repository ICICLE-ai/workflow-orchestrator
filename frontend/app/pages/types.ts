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
  // "tapis_path" fields only: whether the Tapis file browser (see
  // components/TapisPathField) picks a single file or a directory. Defaults
  // to "file" when omitted.
  selectType?: "file" | "dir";
  // "select" fields only: the fixed set of choices, rendered as a dropdown
  // instead of free text (e.g. a CLI flag with an enum of valid values).
  options?: string[];
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
  // False for design-time-only steps (e.g. smart_labeler, geospatial_map) that
  // never submit a Tapis job — the canvas hides Run Configuration for these.
  // Defaults to true when the step type doesn't say otherwise.
  submits_job?: boolean;
}

// An upstream connection feeding one of this node's input ports (resolved from
// the canvas at design time). Only *directly wired* nodes are resolved — a value
// produced by an upstream job is a runtime artifact and isn't available in the
// editor. `config` is the upstream node's config_values (e.g. a source node's
// `path`), so the panel can read whatever it needs from it.
export interface ConnectedInput {
  sourceNodeId: string;
  sourceType: string;   // upstream step_type_key
  sourcePort: string;   // upstream output port_name
  config: Record<string, any>;
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
  // Upstream connections feeding this node's inputs, keyed by THIS node's input
  // port_name. Empty when nothing is wired (or the source has no config). See
  // ConnectedInput for the design-time-only caveat.
  connectedInputs: Record<string, ConnectedInput>;
  // The template version this node lives in (undefined for an unsaved template).
  templateVersionId?: number;
  // The pipeline run this panel is being viewed against, when opened from the
  // run page (runs.$runId.tsx) for a design-time-only step. Undefined when the
  // panel is opened from the canvas at design time (no run exists yet) — a
  // panel whose data is run-scoped (e.g. geospatialMap, which reads a
  // completed run's generated GeoPackage) should treat that as "no run yet"
  // rather than erroring.
  runId?: number;
  // Persist the working config to the node and close (the modal's Save action).
  onSave: () => void;
  // Close without persisting (the modal's Cancel action).
  onClose: () => void;
}
