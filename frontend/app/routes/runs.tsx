import type { Route } from "./+types/runs";
import { AppShell, Container, Title, Text, Button, Group, Card, ThemeIcon, ActionIcon, Badge, Stack, Collapse, Loader, Modal, Code, ScrollArea } from "@mantine/core";
import { IconActivity, IconArrowLeft, IconRefresh, IconChevronDown, IconChevronRight, IconPlayerStop, IconFileText } from "@tabler/icons-react";
import { useNavigate } from "react-router";
import { useState, useEffect, useCallback } from "react";
import { apiFetch } from "../lib/api";
import TopNav from "../components/TopNav";

export async function clientLoader() {
  const res = await apiFetch("/api/pipeline-runs");
  if (!res.ok) throw new Error("Failed to load runs");
  return res.json();
}

const runColor = (s: string) => {
  const v = (s || "").toUpperCase();
  if (v === "COMPLETED") return "teal";
  if (v === "FAILED") return "red";
  if (v === "RUNNING") return "blue";
  if (v === "CANCELLED") return "orange";
  return "gray";
};
const stepColor = (s: string) => {
  if (s === "completed") return "teal";
  if (s === "running") return "blue";
  if (s === "failed") return "red";
  if (s === "blocked") return "yellow";
  if (s === "cancelled") return "orange";
  return "gray";
};

// Modal that fetches and shows a step's failure detail: our recorded error,
// Tapis' outcome summary, and the container's stdout/stderr (tapisjob.out).
// `config` (the step's resolved inputs, passed down from the run detail
// already in hand) renders immediately — it doesn't wait on the logs fetch.
export function StepLogsModal({ runId, nodeId, stepType, config, opened, onClose }:
  { runId: number; nodeId: number | null; stepType?: string; config?: any; opened: boolean; onClose: () => void }) {
  const [logs, setLogs] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!opened || nodeId == null) return;
    setLoading(true); setLogs(null);
    apiFetch(`/api/pipeline-runs/${runId}/step/${nodeId}/logs`)
      .then((r) => r.ok ? r.json() : null)
      .then((d) => setLogs(d))
      .catch(() => setLogs(null))
      .finally(() => setLoading(false));
  }, [opened, runId, nodeId]);

  return (
    <Modal opened={opened} onClose={onClose} size="xl" title={`Logs — ${stepType || 'step'} #${nodeId}`}>
      {config && Object.keys(config).length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Text size="sm" fw={600} mb={4}>Resolved configuration</Text>
          <Code block style={{ whiteSpace: 'pre-wrap', fontSize: 11 }}>{JSON.stringify(config, null, 2)}</Code>
        </div>
      )}
      {loading && <Loader />}
      {!loading && logs && (
        <Stack gap="sm">
          <Group gap="xs">
            <Badge color="gray" variant="light">step: {logs.status}</Badge>
            {logs.tapis_job_status && <Badge color="gray" variant="dot">tapis: {logs.tapis_job_status}</Badge>}
            {logs.tapis_job_uuid && <Text size="xs" c="dimmed">job {logs.tapis_job_uuid}</Text>}
          </Group>

          {logs.tapis_last_message && (
            <div>
              <Text size="sm" fw={600}>Tapis outcome</Text>
              <Code block style={{ whiteSpace: 'pre-wrap' }}>{logs.tapis_last_message}</Code>
            </div>
          )}

          {logs.error_message && (
            <div>
              <Text size="sm" fw={600}>Orchestrator error</Text>
              <Code block color="red" style={{ whiteSpace: 'pre-wrap' }}>{logs.error_message}</Code>
            </div>
          )}

          {logs.job_output && (
            <div>
              <Text size="sm" fw={600}>Container output (tapisjob.out, tail)</Text>
              <ScrollArea.Autosize mah={360}>
                <Code block style={{ fontSize: 11, whiteSpace: 'pre-wrap' }}>{logs.job_output}</Code>
              </ScrollArea.Autosize>
            </div>
          )}

          {logs.logs_note && <Text size="sm" c="dimmed">{logs.logs_note}</Text>}
          {!logs.tapis_last_message && !logs.error_message && !logs.job_output && !logs.logs_note && (
            <Text size="sm" c="dimmed">No logs available for this step.</Text>
          )}
        </Stack>
      )}
      {!loading && !logs && <Text c="dimmed">Could not load logs.</Text>}
    </Modal>
  );
}

function RunDetail({ runId }: { runId: number }) {
  const [detail, setDetail] = useState<any>(null);
  const [logStep, setLogStep] = useState<{ nodeId: number; stepType: string; config?: any } | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await apiFetch(`/api/pipeline-runs/${runId}/detail`);
      if (r.ok) setDetail(await r.json());
    } catch { /* ignore */ }
  }, [runId]);

  useEffect(() => {
    load();
    // Auto-refresh while the run is active so nodes update live.
    const id = setInterval(load, 2500);
    return () => clearInterval(id);
  }, [load]);

  if (!detail) return <Loader size="sm" my="sm" />;

  return (
    <Stack gap={6} mt="sm">
      {(detail.steps || []).map((s: any) => (
        <Group key={s.node_id} justify="space-between" wrap="nowrap"
          style={{ borderBottom: '1px solid #f1f5f9', paddingBottom: 4 }}>
          <div style={{ minWidth: 0 }}>
            <Text size="sm" fw={500}>{s.step_type} <Text span size="xs" c="dimmed">#{s.node_id}</Text></Text>
            {s.error_message && <Text size="xs" c="red" lineClamp={2}>{s.error_message}</Text>}
          </div>
          <Group gap={6} wrap="nowrap">
            {(s.status === 'failed' || s.tapis_job_uuid) && (
              <Button size="compact-xs" variant="subtle" color="gray"
                leftSection={<IconFileText size={12} />}
                onClick={() => setLogStep({ nodeId: s.node_id, stepType: s.step_type, config: s.config })}>
                Logs
              </Button>
            )}
            {s.tapis_job_status && <Badge size="xs" variant="dot" color="gray">{s.tapis_job_status}</Badge>}
            <Badge size="sm" color={stepColor(s.status)} variant="light">{s.status}</Badge>
          </Group>
        </Group>
      ))}
      {(!detail.steps || detail.steps.length === 0) && <Text size="xs" c="dimmed">No steps recorded.</Text>}
      <StepLogsModal
        runId={runId}
        nodeId={logStep?.nodeId ?? null}
        stepType={logStep?.stepType}
        config={logStep?.config}
        opened={logStep != null}
        onClose={() => setLogStep(null)}
      />
    </Stack>
  );
}

export default function Runs({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<any[]>(loaderData || []);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [stopping, setStopping] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const r = await apiFetch("/api/pipeline-runs");
      if (r.ok) setRuns(await r.json());
    } catch { /* ignore */ }
    setRefreshing(false);
  }, []);

  const handleStop = useCallback(async (runId: number) => {
    if (!window.confirm(`Stop run #${runId}? This cancels the workflow and any running Tapis job. This cannot be undone.`)) return;
    setStopping(runId);
    try {
      await apiFetch(`/api/pipeline-runs/${runId}/stop`, { method: "POST" });
    } catch { /* ignore */ }
    setStopping(null);
    refresh();
  }, [refresh]);

  // Auto-refresh the run list while any run is active.
  useEffect(() => {
    const anyActive = runs.some((r) => (r.status || "").toUpperCase() === "RUNNING");
    if (!anyActive) return;
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [runs, refresh]);

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          {/* nowrap: the product name is long enough to wrap onto a second
              line on a narrow viewport, which a fixed 60px header would clip. */}
          <Group wrap="nowrap">
            <ActionIcon variant="subtle" color="gray" onClick={() => navigate('/')}>
              <IconArrowLeft size={20} />
            </ActionIcon>
            <ThemeIcon variant="gradient" gradient={{ from: 'indigo', to: 'cyan' }} size="md" radius="md">
              <IconActivity size={16} />
            </ThemeIcon>
            <Text fw={700} style={{ whiteSpace: 'nowrap' }}>No-Code Workflow Studio</Text>
            <TopNav />
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Container size="lg" py="xl">
          <Group justify="space-between" mb="xl">
            <Title order={2}>Past Runs</Title>
            <Button variant="light" leftSection={refreshing ? <Loader size={14} /> : <IconRefresh size={16} />} onClick={refresh}>
              Refresh
            </Button>
          </Group>

          {runs.length === 0 ? (
            <Text c="dimmed" ta="center" py="xl">No runs yet. Execute a workflow to see it here.</Text>
          ) : (
            <Stack gap="sm">
              {runs.map((r: any) => {
                const isRunning = (r.status || '').toUpperCase() === 'RUNNING';
                return (
                <Card key={r.run_id} shadow="sm" padding="md" radius="md" withBorder
                  style={isRunning ? { borderColor: '#3b82f6' } : undefined}>
                  <Group justify="space-between" wrap="nowrap">
                    <Group gap="sm" wrap="nowrap" style={{ cursor: 'pointer', flex: 1 }}
                      onClick={() => isRunning
                        ? navigate(`/runs/${r.run_id}`)
                        : setExpanded(expanded === r.run_id ? null : r.run_id)}>
                      {!isRunning && (expanded === r.run_id ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />)}
                      <div>
                        <Text fw={600}>{r.template_name || r.name || `Run ${r.run_id}`}</Text>
                        <Text size="xs" c="dimmed">
                          run #{r.run_id}
                          {r.created_at ? ` · ${new Date(r.created_at).toLocaleString()}` : ''}
                        </Text>
                      </div>
                    </Group>
                    <Group gap="sm" wrap="nowrap">
                      {isRunning && (
                        <Button size="xs" color="red" variant="light"
                          leftSection={stopping === r.run_id ? <Loader size={12} color="red" /> : <IconPlayerStop size={14} />}
                          disabled={stopping === r.run_id}
                          onClick={() => handleStop(r.run_id)}>
                          {stopping === r.run_id ? 'Stopping…' : 'Stop'}
                        </Button>
                      )}
                      <Button size="xs" variant={isRunning ? 'filled' : 'light'}
                        color={isRunning ? 'blue' : 'gray'}
                        onClick={() => navigate(`/runs/${r.run_id}`)}>
                        {isRunning ? 'View Live Graph' : 'View Graph'}
                      </Button>
                      <Badge color={runColor(r.status)} variant="light" size="lg">{r.status}</Badge>
                    </Group>
                  </Group>
                  {!isRunning && (
                    <Collapse in={expanded === r.run_id}>
                      {expanded === r.run_id && <RunDetail runId={r.run_id} />}
                    </Collapse>
                  )}
                </Card>
                );
              })}
            </Stack>
          )}
        </Container>
      </AppShell.Main>
    </AppShell>
  );
}
