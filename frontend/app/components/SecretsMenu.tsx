import { useEffect, useState } from "react";
import { ActionIcon, Popover, Stack, Group, TextInput, PasswordInput, Button, Text, Divider, Loader, Tooltip } from "@mantine/core";
import { IconSettings, IconTrash, IconPlus } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { apiFetch } from "../lib/api";

interface SecretItem {
  key: string;
  description: string;
  created_at?: string | null;
}

// Dashboard settings dropdown: manage team-scoped secrets (W&B / Hugging Face
// tokens, etc.) referenced by key from a step's config_schema ("type":
// "secret"). Values are write-only here — the list endpoint never returns them.
export default function SecretsMenu() {
  const [opened, setOpened] = useState(false);
  const [secrets, setSecrets] = useState<SecretItem[]>([]);
  const [loading, setLoading] = useState(false);

  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [saving, setSaving] = useState(false);
  // Keys with a delete in flight — disables that row's button so a double
  // click can't fire two DELETE requests for the same key (the second would
  // 404 since the first already removed it, surfacing a false "failed" toast
  // even though the delete actually succeeded).
  const [deletingKeys, setDeletingKeys] = useState<Set<string>>(new Set());

  const load = async () => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/secrets");
      const data = await res.json();
      setSecrets(data.secrets || []);
    } catch {
      notifications.show({ color: "red", message: "Could not load secrets" });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (opened) load();
  }, [opened]);

  const addSecret = async () => {
    const key = newKey.trim();
    if (!key || !newValue) {
      notifications.show({ color: "yellow", message: "Key and value are required." });
      return;
    }
    setSaving(true);
    try {
      const res = await apiFetch("/api/secrets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, value: newValue, description: newDescription }),
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `HTTP ${res.status}`);
      }
      setNewKey("");
      setNewValue("");
      setNewDescription("");
      await load();
      notifications.show({ color: "green", message: `Added secret '${key}'.` });
    } catch (err: any) {
      notifications.show({ color: "red", title: "Could not add secret", message: err?.message || "Unknown error" });
    } finally {
      setSaving(false);
    }
  };

  const deleteSecret = async (key: string) => {
    if (deletingKeys.has(key)) return; // already deleting this one
    setDeletingKeys((prev) => new Set(prev).add(key));
    try {
      const res = await apiFetch(`/api/secrets/${encodeURIComponent(key)}`, { method: "DELETE" });
      // A 404 here means it's already gone (e.g. a second, redundant request) —
      // treat that as success too rather than surfacing a misleading error.
      if (!res.ok && res.status !== 404) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `HTTP ${res.status}`);
      }
      setSecrets((prev) => prev.filter((s) => s.key !== key));
    } catch (err: any) {
      notifications.show({
        color: "red",
        title: `Could not delete '${key}'`,
        message: err?.message || "Unknown error",
      });
    } finally {
      setDeletingKeys((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  };

  return (
    <Popover opened={opened} onChange={setOpened} position="bottom-end" width={340} shadow="md" withinPortal>
      <Popover.Target>
        <Tooltip label="Secrets (API tokens for workflows)">
          <ActionIcon variant="light" size="lg" radius="md" onClick={() => setOpened((o) => !o)}>
            <IconSettings size={20} />
          </ActionIcon>
        </Tooltip>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Text fw={600} size="sm">Secrets</Text>
          <Text size="xs" c="dimmed">
            Shared across your team and referenced by key from a step's config (e.g. a Weights &amp; Biases
            or Hugging Face token). Values are never shown again after saving.
          </Text>

          {loading ? (
            <Group justify="center" py="sm">
              <Loader size="sm" />
            </Group>
          ) : secrets.length === 0 ? (
            <Text size="xs" c="dimmed" fs="italic">No secrets yet.</Text>
          ) : (
            <Stack gap={4}>
              {secrets.map((s) => (
                <Group key={s.key} justify="space-between" wrap="nowrap">
                  <div style={{ minWidth: 0 }}>
                    <Text size="sm" fw={500} truncate>{s.key}</Text>
                    {s.description && (
                      <Text size="xs" c="dimmed" truncate>{s.description}</Text>
                    )}
                  </div>
                  <ActionIcon
                    color="red"
                    variant="subtle"
                    size="sm"
                    loading={deletingKeys.has(s.key)}
                    onClick={() => deleteSecret(s.key)}
                  >
                    <IconTrash size={14} />
                  </ActionIcon>
                </Group>
              ))}
            </Stack>
          )}

          <Divider my={4} />

          <Text size="xs" fw={600}>Add a secret</Text>
          <TextInput
            size="xs"
            placeholder="Key (e.g. WANDB_API_KEY)"
            value={newKey}
            onChange={(e) => setNewKey(e.currentTarget.value)}
          />
          <PasswordInput
            size="xs"
            placeholder="Value"
            value={newValue}
            onChange={(e) => setNewValue(e.currentTarget.value)}
          />
          <TextInput
            size="xs"
            placeholder="Description (optional)"
            value={newDescription}
            onChange={(e) => setNewDescription(e.currentTarget.value)}
          />
          <Button size="xs" leftSection={<IconPlus size={14} />} loading={saving} onClick={addSecret} fullWidth>
            Add secret
          </Button>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
