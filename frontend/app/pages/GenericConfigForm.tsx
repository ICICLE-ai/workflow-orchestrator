import { useEffect, useState } from "react";
import { NumberInput, Switch, TextInput, Select, Stack, Text } from "@mantine/core";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import { TAPIS_SYSTEMS } from "../lib/tapis";

// The default step settings page: an auto-generated form driven by the step's
// config_schema. Used for every step type that has no custom page registered in
// registry.ts. This preserves the original inline behavior from CustomNode.
export default function GenericConfigForm({ config, onChange, step }: StepPanelProps) {
  const schema = step.config_schema || {};
  const entries = Object.entries(schema);

  // A "secret" field stores a secret's KEY (e.g. "WANDB_API_KEY"), never its
  // value — the actual value is resolved server-side at job-submission time
  // (see backend/engine/secrets.py). Fetch the team's available keys to
  // populate the dropdown, only when this step actually has such a field.
  const hasSecretField = entries.some(([, field]) => field.type === "secret");
  const [secretOptions, setSecretOptions] = useState<{ value: string; label: string }[]>([]);
  useEffect(() => {
    if (!hasSecretField) return;
    apiFetch("/api/secrets")
      .then((res) => res.json())
      .then((data) => {
        const secrets = (data.secrets || []) as { key: string; description?: string }[];
        setSecretOptions(secrets.map((s) => ({
          value: s.key,
          label: s.description ? `${s.key} — ${s.description}` : s.key,
        })));
      })
      .catch(() => {});
  }, [hasSecretField]);

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
        if (field.type === "tapis_system") {
          return (
            <Select
              key={key}
              label={key}
              description={field.description}
              placeholder="Select a Tapis system"
              data={TAPIS_SYSTEMS}
              value={(value as string) || null}
              onChange={(val) => onChange({ ...config, [key]: val ?? "" })}
              allowDeselect={false}
            />
          );
        }
        if (field.type === "secret") {
          return (
            <Select
              key={key}
              label={key}
              description={field.description || "Select a secret configured from the dashboard's settings icon."}
              placeholder="Select a secret"
              data={secretOptions}
              value={(value as string) || null}
              onChange={(val) => onChange({ ...config, [key]: val ?? "" })}
              searchable
              clearable
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
