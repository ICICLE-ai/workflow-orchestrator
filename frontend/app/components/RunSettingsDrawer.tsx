import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Accordion,
  Alert,
  Badge,
  Box,
  Button,
  Drawer,
  Group,
  Loader,
  Paper,
  Radio,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  ThemeIcon,
  UnstyledButton,
  Tooltip,
} from "@mantine/core";
import {
  IconAlertTriangle,
  IconBox,
  IconCircleCheck,
  IconCircleX,
  IconCircleMinus,
  IconShieldCheck,
  IconChevronDown,
  IconCpu,
  IconDatabase,
  IconPlayerPlay,
  IconStack2,
  IconWebhook,
} from "@tabler/icons-react";
import { TAPIS_SYSTEMS } from "../lib/tapis";
import { apiFetch } from "../lib/api";
import SingleJobPanel, { type ExecutionMode, type SingleJobPlan } from "./SingleJobPanel";
import WebhookTriggerPanel from "./WebhookTriggerPanel";

export interface RunOptions {
  execution_mode: ExecutionMode;
  exec_system: string;
  exec_queue: string;
  gpu_exec_system: string;
  gpu_exec_queue: string;
  work_dir: string;
  archive_system: string;
  archive_dir: string;
  slurm_account: string;
}

interface QueueState {
  queues: any[];
  loading: boolean;
  // Human-readable limits of the selected queue ("max 4 node(s) · …").
  detail: string | null;
}

// The steps on the canvas that will actually submit a Tapis job, split by the
// target they follow (step.json "resources": {"gpu": true} -> GPU target).
export interface JobSteps {
  cpu: string[];
  gpu: string[];
}

const MODES: { value: ExecutionMode; title: string; icon: typeof IconStack2; body: string; bestFor: string }[] = [
  {
    value: "per_step",
    title: "One job per step",
    icon: IconStack2,
    body: "Each step is its own Tapis job with exactly the resources it asks for. Independent branches run in parallel.",
    bestFor: "Mixed CPU / GPU pipelines, long steps",
  },
  {
    value: "single_job",
    title: "Single job",
    icon: IconBox,
    body: "The whole pipeline runs back-to-back inside ONE Tapis job, sized to fit every step.",
    bestFor: "Short pipelines — one queue wait instead of many",
  },
];

function SectionHeader({ step, title, hint }: { step: number; title: string; hint?: ReactNode }) {
  return (
    <Group gap="xs" align="flex-start" wrap="nowrap">
      <ThemeIcon size={22} radius="xl" variant="light" color="green">
        <Text size="xs" fw={700}>{step}</Text>
      </ThemeIcon>
      <Box style={{ flex: 1 }}>
        <Text fw={600} size="sm" lh={1.6}>{title}</Text>
        {hint && <Text size="xs" c="dimmed">{hint}</Text>}
      </Box>
    </Group>
  );
}

// One exec target (system + queue), labelled with the steps that follow it so
// the routing is concrete rather than something to remember.
function TargetCard({
  kind,
  heading,
  steps,
  system,
  queue,
  queueState,
  onSystem,
  onQueue,
}: {
  kind: "cpu" | "gpu";
  heading: string;
  steps: string[];
  system: string;
  queue: string;
  queueState: QueueState;
  onSystem: (v: string) => void;
  onQueue: (v: string) => void;
}) {
  const color = kind === "gpu" ? "grape" : "blue";
  return (
    <Paper withBorder radius="md" p="sm" style={{ borderLeft: `3px solid var(--mantine-color-${color}-6)` }}>
      <Stack gap="xs">
        <Group gap="xs" justify="space-between" wrap="nowrap">
          <Group gap={6} wrap="nowrap">
            <IconCpu size={16} color={`var(--mantine-color-${color}-6)`} />
            <Text size="sm" fw={600}>{heading}</Text>
          </Group>
          <Badge size="sm" variant="light" color={color}>
            {steps.length} step{steps.length === 1 ? "" : "s"}
          </Badge>
        </Group>
        {steps.length > 0 && (
          <Text size="xs" c="dimmed" lineClamp={2} title={steps.join(", ")}>
            {steps.join(", ")}
          </Text>
        )}
        <SimpleGrid cols={2} spacing="xs">
          <Select
            size="xs"
            label="System"
            data={TAPIS_SYSTEMS}
            value={system}
            allowDeselect={false}
            onChange={(v) => v && onSystem(v)}
            comboboxProps={{ withinPortal: true }}
          />
          <Select
            size="xs"
            label="Queue"
            data={Array.from(new Set([...queueState.queues.map((q: any) => q.name).filter(Boolean), ...(queue ? [queue] : [])]))}
            value={queue || null}
            allowDeselect={false}
            disabled={queueState.loading}
            placeholder={queueState.loading ? "Loading…" : "Select a queue"}
            onChange={(v) => v && onQueue(v)}
            comboboxProps={{ withinPortal: true }}
          />
        </SimpleGrid>
        {(queueState.loading || queueState.detail) && (
          <Text size="xs" c="dimmed">
            {queueState.loading ? "Loading queues…" : `Queue limits: ${queueState.detail}`}
          </Text>
        )}
      </Stack>
    </Paper>
  );
}

// Result of POST .../preflight (backend/engine/preflight.py).
interface PreflightCheck {
  key: string;
  title: string;
  status: "pass" | "warn" | "fail" | "skip";
  detail: string;
  group: string;
}
interface PreflightResult {
  ok: boolean;
  checks: PreflightCheck[];
}

const STATUS_STYLE = {
  pass: { color: "green", Icon: IconCircleCheck },
  warn: { color: "yellow", Icon: IconAlertTriangle },
  fail: { color: "red", Icon: IconCircleX },
  skip: { color: "gray", Icon: IconCircleMinus },
} as const;

function PreflightResults({ result, stale }: { result: PreflightResult; stale: boolean }) {
  const groups = Array.from(new Set(result.checks.map((c) => c.group)));
  const fails = result.checks.filter((c) => c.status === "fail").length;
  const warns = result.checks.filter((c) => c.status === "warn").length;
  return (
    <Paper
      withBorder
      radius="md"
      p="sm"
      style={{
        borderColor: `var(--mantine-color-${fails ? "red" : warns ? "yellow" : "green"}-6)`,
        opacity: stale ? 0.6 : 1,
      }}
    >
      <Stack gap="xs">
        <Group gap="xs" justify="space-between">
          <Text size="sm" fw={600}>
            {fails
              ? `${fails} problem${fails === 1 ? "" : "s"} would stop this run`
              : warns
                ? `Ready, with ${warns} warning${warns === 1 ? "" : "s"}`
                : `All ${result.checks.length} checks passed`}
          </Text>
          {stale && <Badge size="xs" color="gray" variant="light">settings changed — re-verify</Badge>}
        </Group>
        {groups.map((g) => (
          <Stack key={g} gap={4}>
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>{g}</Text>
            {result.checks
              .filter((c) => c.group === g)
              .map((c) => {
                const { color, Icon } = STATUS_STYLE[c.status];
                return (
                  <Group key={c.key + c.title} gap={8} align="flex-start" wrap="nowrap">
                    <Icon size={16} color={`var(--mantine-color-${color}-6)`} style={{ flexShrink: 0, marginTop: 2 }} />
                    <Box style={{ minWidth: 0 }}>
                      <Text size="xs" fw={500}>{c.title}</Text>
                      {c.detail && (
                        <Text size="xs" c={c.status === "fail" ? "red.7" : "dimmed"} style={{ wordBreak: "break-word" }}>
                          {c.detail}
                        </Text>
                      )}
                    </Box>
                  </Group>
                );
              })}
          </Stack>
        ))}
      </Stack>
    </Paper>
  );
}

/**
 * Run Settings, organised around the one decision that changes everything
 * else: HOW the workflow is submitted. Each mode only shows the targets it
 * will actually use —
 *   * one job per step: a CPU target, plus a GPU target when GPU steps exist;
 *   * single job: the ONE target the merged job lands on (GPU if any step
 *     needs a GPU, else CPU — engine/single_job.py resolve_exec_target), and
 *     the merged resource envelope.
 * Billing comes next; storage and the webhook are optional, so collapsed.
 */
export default function RunSettingsDrawer({
  opened,
  onClose,
  title,
  runOptions,
  setRunOptions,
  cpuQueues,
  gpuQueues,
  jobSteps,
  templateVersionId,
  onPlanChange,
  singleJobBlocked,
  running,
  unsaved,
  onLaunch,
  onSaveRequest,
}: {
  opened: boolean;
  onClose: () => void;
  title?: string;
  runOptions: RunOptions;
  setRunOptions: (fn: (prev: RunOptions) => RunOptions) => void;
  cpuQueues: QueueState;
  gpuQueues: QueueState;
  jobSteps: JobSteps;
  templateVersionId: number | undefined;
  onPlanChange: (plan: SingleJobPlan | null) => void;
  singleJobBlocked: boolean;
  running: boolean;
  unsaved: boolean;
  onLaunch: () => void;
  onSaveRequest?: () => void;
}) {
  const mode = runOptions.execution_mode;
  const set = (patch: Partial<RunOptions>) => setRunOptions((prev) => ({ ...prev, ...patch }));
  const hasGpuSteps = jobSteps.gpu.length > 0;
  const totalSteps = jobSteps.cpu.length + jobSteps.gpu.length;

  // --- Pre-launch verification (live counterpart of the local "Check
  // workflow"). A result belongs to the exact settings + version it checked;
  // any change makes it stale, and Launch re-verifies.
  const checkKey = `${templateVersionId}|${JSON.stringify(runOptions)}`;
  const [preflight, setPreflight] = useState<{ key: string; result: PreflightResult } | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const resultsRef = useRef<HTMLDivElement>(null);
  const fresh = preflight?.key === checkKey ? preflight.result : null;
  const blockedByCheck = !!fresh && !fresh.ok;

  // A new drawer opening starts clean.
  useEffect(() => {
    if (!opened) {
      setPreflight(null);
      setVerifyError(null);
    }
  }, [opened]);

  const verify = async (): Promise<PreflightResult | null> => {
    if (!templateVersionId) return null;
    setVerifying(true);
    setVerifyError(null);
    const key = checkKey;
    try {
      const res = await apiFetch(`/api/workflow-templates/${templateVersionId}/preflight`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(runOptions),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setPreflight({ key, result: body as PreflightResult });
      requestAnimationFrame(() => resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
      return body as PreflightResult;
    } catch (e: any) {
      setVerifyError(e?.message || "Could not run the checks");
      return null;
    } finally {
      setVerifying(false);
    }
  };

  // Launch verifies first (unless these exact settings were just verified),
  // and only goes ahead on a clean result. A failed check turns the button
  // into "Launch anyway" — the user can still override, knowingly.
  const launch = async () => {
    if (blockedByCheck) return onLaunch(); // "Launch anyway"
    const result = fresh ?? (await verify());
    if (result && !result.ok) return;
    onLaunch();
  };

  const [openSections, setOpenSections] = useState<string[]>([]);
  // A webhook is only CREATED once its section is opened.
  const [webhookOpen, setWebhookOpen] = useState(false);

  const cpuTarget = (heading: string, steps: string[]) => (
    <TargetCard
      kind="cpu"
      heading={heading}
      steps={steps}
      system={runOptions.exec_system}
      queue={runOptions.exec_queue}
      queueState={cpuQueues}
      // Archive system follows the CPU system by default; Storage below can
      // still point it elsewhere afterwards.
      onSystem={(v) => set({ exec_system: v, archive_system: v })}
      onQueue={(v) => set({ exec_queue: v })}
    />
  );
  const gpuTarget = (heading: string, steps: string[]) => (
    <TargetCard
      kind="gpu"
      heading={heading}
      steps={steps}
      system={runOptions.gpu_exec_system}
      queue={runOptions.gpu_exec_queue}
      queueState={gpuQueues}
      onSystem={(v) => set({ gpu_exec_system: v })}
      onQueue={(v) => set({ gpu_exec_queue: v })}
    />
  );

  // One line saying what Launch will submit, kept next to the button.
  const summary =
    mode === "single_job"
      ? `1 job · ${totalSteps} step${totalSteps === 1 ? "" : "s"} on ${
          hasGpuSteps ? `${runOptions.gpu_exec_system} / ${runOptions.gpu_exec_queue}` : `${runOptions.exec_system} / ${runOptions.exec_queue}`
        }`
      : `${totalSteps} job${totalSteps === 1 ? "" : "s"}` +
        (hasGpuSteps ? ` · ${jobSteps.cpu.length} CPU, ${jobSteps.gpu.length} GPU` : "") +
        ` · charged to ${runOptions.slurm_account || "—"}`;

  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position="right"
      size={520}
      title={
        <Group gap="xs">
          <ThemeIcon variant="light" color="green" radius="md">
            <IconPlayerPlay size={16} />
          </ThemeIcon>
          <Box>
            <Text fw={700} lh={1.2}>Run workflow</Text>
            {title && <Text size="xs" c="dimmed">{title}</Text>}
          </Box>
        </Group>
      }
      styles={{ body: { paddingBottom: 0 } }}
    >
      <Stack gap="lg">
        {/* 1 — how it's submitted */}
        <Stack gap="xs">
          <SectionHeader step={1} title="Run type" hint="How the pipeline is submitted to Tapis." />
          <Radio.Group value={mode} onChange={(v) => set({ execution_mode: v as ExecutionMode })}>
            <SimpleGrid cols={2} spacing="xs">
              {MODES.map((m) => {
                const selected = mode === m.value;
                return (
                  <Radio.Card
                    key={m.value}
                    value={m.value}
                    radius="md"
                    p="sm"
                    style={{
                      borderColor: selected ? "var(--mantine-color-green-6)" : undefined,
                      background: selected ? "var(--mantine-color-green-light)" : undefined,
                      transition: "border-color 120ms, background 120ms",
                    }}
                  >
                    <Stack gap={6}>
                      <Group justify="space-between" wrap="nowrap">
                        <Group gap={6} wrap="nowrap">
                          <m.icon size={18} color={selected ? "var(--mantine-color-green-7)" : undefined} />
                          <Text size="sm" fw={600}>{m.title}</Text>
                        </Group>
                        <Radio.Indicator size="xs" color="green" />
                      </Group>
                      <Text size="xs" c="dimmed">{m.body}</Text>
                      <Text size="xs" fw={500}>Best for: <Text span size="xs" c="dimmed">{m.bestFor}</Text></Text>
                    </Stack>
                  </Radio.Card>
                );
              })}
            </SimpleGrid>
          </Radio.Group>
        </Stack>

        {/* 2 — where, depending on the mode */}
        <Stack gap="xs">
          {mode === "per_step" ? (
            <>
              <SectionHeader
                step={2}
                title="Where each step runs"
                hint={
                  hasGpuSteps
                    ? "Steps are routed by what they need. A step's own Run Configuration overrides this."
                    : "A step's own Run Configuration overrides this."
                }
              />
              {cpuTarget("CPU steps", jobSteps.cpu)}
              {hasGpuSteps ? (
                gpuTarget("GPU steps", jobSteps.gpu)
              ) : (
                <Text size="xs" c="dimmed" pl={30}>
                  No step on this canvas needs a GPU, so there is no GPU target to set.
                </Text>
              )}
            </>
          ) : (
            <>
              <SectionHeader
                step={2}
                title="Where the job runs"
                hint={
                  hasGpuSteps
                    ? `On the GPU target, because ${jobSteps.gpu.join(", ")} need${jobSteps.gpu.length === 1 ? "s" : ""} a GPU.`
                    : "No step needs a GPU, so the job uses a CPU target."
                }
              />
              {hasGpuSteps
                ? gpuTarget("Job target (GPU)", [...jobSteps.cpu, ...jobSteps.gpu])
                : cpuTarget("Job target (CPU)", jobSteps.cpu)}
              <SingleJobPanel
                opened={opened}
                templateVersionId={templateVersionId}
                runOptions={runOptions}
                mode={mode}
                onModeChange={(m) => set({ execution_mode: m })}
                onPlanChange={onPlanChange}
                showModeSwitch={false}
              />
            </>
          )}
        </Stack>

        {/* 3 — who pays */}
        <Stack gap="xs">
          <SectionHeader step={3} title="Charge account" hint="Follows the template's allocation account unless changed here." />
          <TextInput
            pl={30}
            placeholder="e.g. PAS2699"
            value={runOptions.slurm_account}
            onChange={(e) => set({ slurm_account: e.currentTarget.value })}
          />
        </Stack>

        {/* Optional extras */}
        <Accordion
          multiple
          variant="separated"
          radius="md"
          value={openSections}
          onChange={setOpenSections}
          chevronPosition="right"
        >
          <Accordion.Item value="storage">
            <Accordion.Control icon={<IconDatabase size={16} />}>
              <Text size="sm" fw={500}>Storage & archive</Text>
              <Text size="xs" c="dimmed">
                Outputs → {runOptions.archive_system}
                {runOptions.archive_dir ? `:${runOptions.archive_dir}` : " (default location)"}
              </Text>
            </Accordion.Control>
            <Accordion.Panel>
              <Stack gap="sm">
                <TextInput
                  size="xs"
                  label="Work dir"
                  description="Base dir on the archive system: steps running there execute under it, and outputs archive to …/wf_runs. Reset to the default when the archive system, charge account or user changes."
                  value={runOptions.work_dir}
                  onChange={(e) => set({ work_dir: e.currentTarget.value })}
                />
                <Select
                  size="xs"
                  label="Archive system"
                  description="Where step outputs are archived (unless a sink node says otherwise)."
                  data={TAPIS_SYSTEMS}
                  value={runOptions.archive_system}
                  allowDeselect={false}
                  onChange={(v) => v && set({ archive_system: v })}
                  comboboxProps={{ withinPortal: true }}
                />
                <TextInput
                  size="xs"
                  label="Archive dir (optional)"
                  description="Each step still archives under …/{run_id}/{step_type_key}/{node_id}."
                  placeholder={runOptions.work_dir ? `${runOptions.work_dir.replace(/\/$/, "")}/wf_runs` : "wf_runs"}
                  value={runOptions.archive_dir}
                  onChange={(e) => set({ archive_dir: e.currentTarget.value })}
                />
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>

        </Accordion>

        {/* Not an Accordion item: a collapsed Accordion panel is wrapped in a
            hidden React <Activity>, which pauses effects — and the webhook
            panel's effect is what keeps an existing trigger's stored run
            settings in sync with this drawer. Hiding with CSS keeps it live. */}
        <Paper withBorder radius="md">
          <UnstyledButton
            w="100%"
            px="md"
            py="sm"
            onClick={() => setWebhookOpen((o) => !o)}
            aria-expanded={webhookOpen}
          >
            <Group gap="sm" wrap="nowrap">
              <IconWebhook size={16} />
              <Box style={{ flex: 1 }}>
                <Text size="sm" fw={500}>Trigger with a webhook</Text>
                <Text size="xs" c="dimmed">Start this workflow from outside the Studio, with these settings.</Text>
              </Box>
              <IconChevronDown
                size={16}
                style={{ transform: webhookOpen ? "rotate(180deg)" : undefined, transition: "transform 150ms" }}
              />
            </Group>
          </UnstyledButton>
          <Box px="md" pb="md" style={{ display: webhookOpen ? undefined : "none" }}>
            <WebhookTriggerPanel
              // Loaded whenever Run is open, so an EXISTING webhook's stored run
              // settings stay in sync with this drawer; only created once this
              // section is expanded.
              opened={opened}
              createIfMissing={opened && webhookOpen}
              templateVersionId={templateVersionId}
              runOptions={runOptions}
              unsaved={unsaved}
              onSaveRequest={onSaveRequest}
            />
          </Box>
        </Paper>

        {(preflight || verifyError) && (
          <Box ref={resultsRef}>
            {verifyError ? (
              <Alert color="red" variant="light" title="Couldn't run the checks">
                <Text size="xs">{verifyError}</Text>
              </Alert>
            ) : (
              preflight && <PreflightResults result={preflight.result} stale={!fresh} />
            )}
          </Box>
        )}
      </Stack>

      {/* Launch stays in view however far the drawer is scrolled. */}
      <Box
        mt="lg"
        mx="calc(var(--mantine-spacing-md) * -1)"
        px="md"
        py="sm"
        style={{
          position: "sticky",
          bottom: 0,
          background: "var(--mantine-color-body)",
          borderTop: "1px solid var(--mantine-color-default-border)",
          zIndex: 1,
        }}
      >
        <Stack gap={8}>
          {unsaved && (
            <Alert color="yellow" variant="light" p={8} icon={<IconAlertTriangle size={16} />}>
              <Text size="xs">Unsaved changes — you'll be asked to save before the run starts.</Text>
            </Alert>
          )}
          <Text size="xs" c="dimmed" ta="center">
            {summary}
            {unsaved ? " · checks run against the last saved version" : ""}
          </Text>
          <Group gap="xs" wrap="nowrap">
            <Tooltip label="Check systems, queues, apps, inputs and the output folder on Tapis — nothing is launched" multiline w={240} withArrow>
              <Button
                variant="default"
                size="md"
                leftSection={<IconShieldCheck size={18} />}
                onClick={verify}
                loading={verifying && !running}
                disabled={!templateVersionId}
              >
                Verify
              </Button>
            </Tooltip>
            <Tooltip
              label="This workflow can't run as a single job — see the reasons above."
              disabled={!singleJobBlocked}
              multiline
              w={240}
              withArrow
            >
              <Button
                color={blockedByCheck ? "orange" : "green"}
                size="md"
                style={{ flex: 1 }}
                leftSection={verifying ? <Loader size={16} color="white" /> : blockedByCheck ? <IconAlertTriangle size={18} /> : <IconPlayerPlay size={18} />}
                onClick={launch}
                loading={running}
                disabled={singleJobBlocked || verifying}
              >
                {verifying ? "Verifying…" : blockedByCheck ? "Launch anyway" : "Verify & launch"}
              </Button>
            </Tooltip>
          </Group>
        </Stack>
      </Box>
    </Drawer>
  );
}
