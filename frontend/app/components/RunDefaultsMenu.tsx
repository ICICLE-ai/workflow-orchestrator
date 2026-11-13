import { useEffect, useState } from "react";
import { Button, Modal, Stack, Select, TextInput, Group, Text, SimpleGrid, Switch } from "@mantine/core";
import { IconServerCog } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { apiFetch } from "../lib/api";
import { TAPIS_SYSTEMS, preferredQueue } from "../lib/tapis";
import { type RunDefaults, setRunDefaultsCache } from "../lib/runDefaults";

function useQueueNames(system: string) {
  const [names, setNames] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!system) return setNames([]);
    let cancelled = false;
    setLoading(true);
    apiFetch(`/api/tapis-systems/${system}/queues`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled) setNames((d?.queues || []).map((q: any) => q.name).filter(Boolean)); })
      .catch(() => { if (!cancelled) setNames([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [system]);
  return { names, loading };
}

// Header button + modal for the user's run-settings defaults (stored on the
// server, GET/PUT /api/me/run-defaults). Every workflow's Run settings start
// from these until the user launches it with something else — that workflow
// then keeps its own (see the backend's RunPreference).
export default function RunDefaultsMenu() {
  const [opened, setOpened] = useState(false);
  const [values, setValues] = useState<RunDefaults>({});
  const [saving, setSaving] = useState(false);
  const { names, loading } = useQueueNames(values.exec_system || "");

  useEffect(() => {
    if (!opened) return;
    apiFetch("/api/me/run-defaults")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setValues(d?.defaults || {}))
      .catch(() => notifications.show({ color: "red", message: "Could not load your run defaults" }));
  }, [opened]);

  const set = (patch: RunDefaults) => setValues((p) => ({ ...p, ...patch }));
  const queueData = (current?: string) => Array.from(new Set([...names, ...(current ? [current] : [])]));

  const save = async () => {
    setSaving(true);
    try {
      const res = await apiFetch("/api/me/run-defaults", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      if (!res.ok) throw new Error();
      setRunDefaultsCache((await res.json()).defaults || {});
      notifications.show({ color: "green", message: "Run defaults saved" });
      setOpened(false);
    } catch {
      notifications.show({ color: "red", message: "Could not save your run defaults" });
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Button size="xs" variant="subtle" color="gray" leftSection={<IconServerCog size={14} />} onClick={() => setOpened(true)}>
        Run defaults
      </Button>
      <Modal opened={opened} onClose={() => setOpened(false)} title="Run defaults" size="md">
        <Stack gap="sm">
          <Text size="xs" c="dimmed">
            Every workflow's Run settings start from these. Launching a workflow with different settings keeps
            those for that workflow only; leave a field blank to use the built-in default.
          </Text>
          <Select
            label="System"
            description="Runs CPU and GPU steps, and holds their outputs."
            data={TAPIS_SYSTEMS}
            value={values.exec_system || null}
            clearable
            onChange={(v) =>
              set({
                exec_system: v || "",
                exec_queue: (v && preferredQueue(v, "cpu")) || "",
                gpu_exec_queue: (v && preferredQueue(v, "gpu")) || "",
              })
            }
            comboboxProps={{ withinPortal: true }}
          />
          <SimpleGrid cols={2} spacing="sm">
            <Select
              label="CPU queue"
              data={queueData(values.exec_queue)}
              value={values.exec_queue || null}
              disabled={!values.exec_system || loading}
              placeholder={loading ? "Loading…" : "System default"}
              clearable
              onChange={(v) => set({ exec_queue: v || "" })}
              comboboxProps={{ withinPortal: true }}
            />
            <Select
              label="GPU queue"
              data={queueData(values.gpu_exec_queue)}
              value={values.gpu_exec_queue || null}
              disabled={!values.exec_system || loading}
              placeholder={loading ? "Loading…" : "System default"}
              clearable
              onChange={(v) => set({ gpu_exec_queue: v || "" })}
              comboboxProps={{ withinPortal: true }}
            />
          </SimpleGrid>
          <TextInput
            label="Work dir"
            description="Base dir on the system: jobs run under it and outputs archive to …/wf_runs."
            placeholder="System default"
            value={values.work_dir || ""}
            onChange={(e) => set({ work_dir: e.currentTarget.value })}
          />
          <TextInput
            label="Containers dir"
            description="Folder on the system holding step container images (trainer.sif, inf.sif, …), mounted instead of copied."
            placeholder="e.g. /fs/ess/PAS2699/containers"
            value={values.containers_dir || ""}
            onChange={(e) => set({ containers_dir: e.currentTarget.value })}
          />
          <Switch
            label="File staging"
            description="On: Tapis copies every input into the job. Off: inputs already on the system are mounted into the container instead of copied — faster, but needs Apptainer/Singularity on the compute nodes."
            checked={values.stage_files === true}
            onChange={(e) => set({ stage_files: e.currentTarget.checked })}
          />
          <Group justify="flex-end" mt="xs">
            <Button variant="default" onClick={() => setOpened(false)}>Cancel</Button>
            <Button onClick={save} loading={saving}>Save defaults</Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
