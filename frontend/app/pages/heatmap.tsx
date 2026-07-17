import { Stack, Group, NumberInput, Select, Text, Paper, Divider, Box } from "@mantine/core";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";

// Example custom settings page for the `heatmap` step. Demonstrates the pattern
// for an interactive panel (vs. the generic auto-form): a purpose-built layout,
// a live preview computed from the values, and access to the backend via
// apiFetch. It reads/writes the same config_values as any other step.
//
// Registered in registry.ts under the key "heatmap".
export default function HeatmapPanel({ config, onChange, step }: StepPanelProps) {
  // Pull each value from working config, falling back to the schema default.
  const val = (key: string) => {
    const v = config[key];
    return v !== undefined ? v : step.config_schema[key]?.default;
  };
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const gridW = Number(val("grid_width")) || 1;
  const gridH = Number(val("grid_height")) || 1;
  const fovW = Number(val("fov_width_ft")) || 0;
  const fovH = Number(val("fov_height_ft")) || 0;

  // Interactive preview: cell size in feet derived from FOV / grid.
  const cellW = gridW ? (fovW / gridW).toFixed(2) : "–";
  const cellH = gridH ? (fovH / gridH).toFixed(2) : "–";

  // Render a scaled-down grid preview (capped so a huge grid stays readable).
  const previewCols = Math.min(gridW, 40);
  const previewRows = Math.min(gridH, 40);

  // Example of backend access from a panel (apiFetch is authenticated + points
  // at the configured backend). A real panel might, for instance, preview live
  // detection density. Left here as a reference:
  //
  //   const res = await apiFetch(`/api/pipeline-runs/${runId}/detail`);
  void apiFetch; // referenced so the import documents the capability

  return (
    <Stack gap="md">
      <Group grow>
        <NumberInput
          label="Grid width (cells)"
          description={step.config_schema.grid_width?.description}
          min={1}
          value={gridW}
          onChange={(v) => set("grid_width", v)}
        />
        <NumberInput
          label="Grid height (cells)"
          description={step.config_schema.grid_height?.description}
          min={1}
          value={gridH}
          onChange={(v) => set("grid_height", v)}
        />
      </Group>

      <Group grow>
        <NumberInput
          label="FOV width (ft)"
          min={0}
          decimalScale={2}
          value={fovW}
          onChange={(v) => set("fov_width_ft", v)}
        />
        <NumberInput
          label="FOV height (ft)"
          min={0}
          decimalScale={2}
          value={fovH}
          onChange={(v) => set("fov_height_ft", v)}
        />
      </Group>

      <Group grow>
        <Select
          label="Spray mode"
          data={[
            { value: "binary", label: "Binary" },
            { value: "graduated", label: "Graduated" },
          ]}
          value={String(val("spray_mode") ?? "binary")}
          onChange={(v) => set("spray_mode", v)}
          allowDeselect={false}
        />
        <Select
          label="Generator mode"
          data={[
            { value: "detection", label: "Detection (from images)" },
            { value: "results", label: "Results (from JSON)" },
          ]}
          value={String(val("generator_mode") ?? "detection")}
          onChange={(v) => set("generator_mode", v)}
          allowDeselect={false}
        />
      </Group>

      <Divider label="Preview" labelPosition="center" />

      <Paper withBorder p="sm" radius="md">
        <Text size="sm" mb={6}>
          {gridW} × {gridH} grid — {gridW * gridH} cells
        </Text>
        <Text size="xs" c="dimmed" mb="sm">
          Each cell ≈ {cellW} ft × {cellH} ft
        </Text>
        <Box
          style={{
            display: "grid",
            gridTemplateColumns: `repeat(${previewCols}, 1fr)`,
            gap: 1,
            width: "100%",
            maxWidth: 260,
            aspectRatio: `${previewCols} / ${previewRows}`,
            background: "#e2e8f0",
            border: "1px solid #cbd5e1",
          }}
        >
          {Array.from({ length: previewCols * previewRows }).map((_, i) => (
            <div key={i} style={{ background: "#f8fafc" }} />
          ))}
        </Box>
        {(gridW > previewCols || gridH > previewRows) && (
          <Text size="xs" c="dimmed" mt={6}>
            (preview capped at {previewCols} × {previewRows})
          </Text>
        )}
      </Paper>
    </Stack>
  );
}
