import type { ComponentType } from "react";
import type { StepPanelProps } from "./types";
import HeatmapPanel from "./heatmap";
import ImagePlayground from "./imagePreprocessStudio";
import SmartLabelerPanel from "./smartLabeler";
import FewShotAnnotationPanel from "./fewShotAnnotation";
import GeospatialMapPanel from "./geospatialMap";

// Map of step_type_key -> custom settings page component.
//
// To add a custom interactive panel for a step: create a component under
// app/pages/ that implements StepPanelProps (see ./types and ./heatmap for an
// example), then add one line here mapping its step_type_key to the component.
// Step types not listed here fall back to GenericConfigForm automatically.
export const stepPanels: Record<string, ComponentType<StepPanelProps>> = {
  heatmap: HeatmapPanel,
  "image-preprocess-studio": ImagePlayground,
  smart_labeler: SmartLabelerPanel,
  few_shot_annotation: FewShotAnnotationPanel,
  geospatial_map: GeospatialMapPanel,
};

// Resolve the registered panel for a step type, or undefined if none.
export function getStepPanel(stepTypeKey: string): ComponentType<StepPanelProps> | undefined {
  return stepPanels[stepTypeKey];
}
