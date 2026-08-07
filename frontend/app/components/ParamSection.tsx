import type { ReactNode } from "react";
import { Paper, Group, Text, Stack, Badge } from "@mantine/core";

// Shared layout for a custom step panel's parameter groups: a titled card
// pairing a plain-language explainer with the actual input controls, plus an
// optional small diagram alongside them. Used by flightPlan.tsx and
// missionExport.tsx.
//
// `disabled` greys the whole section out and blocks interaction with every
// control inside it via a native <fieldset disabled> — that's what actually
// disables Mantine's underlying <input>/<select>/<button> elements without
// threading a `disabled` prop into each one individually. `disabledHint`
// (e.g. "Only used by: ArduPilot, PX4") explains why, rather than a field
// just going inert with no explanation.
export default function ParamSection({
  title, explainer, diagram, children, disabled, disabledHint,
}: {
  title: string;
  explainer: string;
  diagram?: ReactNode;
  children: ReactNode;
  disabled?: boolean;
  disabledHint?: string;
}) {
  return (
    <Paper withBorder radius="md" p="md" style={{ opacity: disabled ? 0.55 : 1, transition: "opacity 150ms ease" }}>
      <Group justify="space-between" mb={4} wrap="nowrap" align="flex-start">
        <Text fw={600} size="sm">{title}</Text>
        {disabled && disabledHint && (
          <Badge size="xs" variant="light" color="gray" style={{ flexShrink: 0 }}>{disabledHint}</Badge>
        )}
      </Group>
      <Text size="xs" c="dimmed" mb="sm">{explainer}</Text>
      <fieldset disabled={disabled} style={{ border: "none", padding: 0, margin: 0 }}>
        <Group align="flex-start" gap="lg" wrap="wrap">
          <Stack style={{ flex: "1 1 320px", minWidth: 280 }}>{children}</Stack>
          {diagram && <div style={{ flex: "0 0 auto" }}>{diagram}</div>}
        </Group>
      </fieldset>
    </Paper>
  );
}
