import { NumberInput, Switch, TextInput, Stack, Text } from "@mantine/core";
import type { StepPanelProps } from "./types";

// The default step settings page: an auto-generated form driven by the step's
// config_schema. Used for every step type that has no custom page registered in
// registry.ts. This preserves the original inline behavior from CustomNode.
export default function GenericConfigForm({ config, onChange, step }: StepPanelProps) {
  const schema = step.config_schema || {};
  const entries = Object.entries(schema);

  if (entries.length === 0) {
    return <Text c="dimmed">No configuration available for this step.</Text>;
  }

  return (
    <Stack>
      {entries.map(([key, field]) => {
        const value = config[key] !== undefined ? config[key] : field.default;

        if (field.type === "int" || field.type === "float") {
          return (
            <NumberInput
              key={key}
              label={key}
              description={field.description}
              value={value as number}
              onChange={(val) => onChange({ ...config, [key]: val })}
            />
          );
        }
        if (field.type === "boolean") {
          return (
            <Switch
              key={key}
              label={key}
              description={field.description}
              checked={Boolean(value)}
              onChange={(e) => onChange({ ...config, [key]: e.currentTarget.checked })}
            />
          );
        }
        return (
          <TextInput
            key={key}
            label={key}
            description={field.description}
            value={(value as string) ?? ""}
            onChange={(e) => onChange({ ...config, [key]: e.currentTarget.value })}
          />
        );
      })}
    </Stack>
  );
}
