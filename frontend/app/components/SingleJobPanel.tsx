import { useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Group,
  List,
  Loader,
  Paper,
  SegmentedControl,
  Spoiler,
  Stack,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import { IconAlertTriangle, IconCpu, IconInfoCircle } from "@tabler/icons-react";
import { apiFetch } from "../lib/api";

export type ExecutionMode = "per_step" | "single_job";

export interface SingleJobPlan {
  app: { id: string; version: string };
  exec: {
    exec_system: string;
    exec_queue: string;
    target: "cpu" | "gpu";
    archive_system: string;
    archive_dir: string;
    images_dir: string;
    images_staged: boolean;
  };
  resources: {
    nodeCount: number;
    coresPerNode: number;
    memoryMB: number;
    maxMinutes: number;
    gpus: number;
    needs_gpu: boolean;
    gpu_steps?: string[];
    step_count: number;
    derivation: Record<
      string,
      { how: string; value: number; driven_by: string[]; per_step: Record<string, number> }
    >;
  };
  steps: { node_key: string; label: string; step_type: string }[];
  job?: Record<string, any>;
  warnings: string[];
  blockers: string[];
}

// Order and labels for the envelope table — the fields a scheduler actually
// reserves, in the order someone reading a job script would expect them.
const FIELDS: { key: string; label: string; format: (n: number) => string }[] = [
  { key: "nodeCount", label: "Nodes", format: (n) => String(n) },
  { key: "coresPerNode", label: "Cores / node", format: (n) => String(n) },
  { key: "memoryMB", label: "Memory", format: (n) => `${n.toLocaleString()} MB` },
  { key: "gpus", label: "GPUs", format: (n) => String(n) },
  {
    key: "maxMinutes",
    label: "Max runtime",
    format: (n) => (n >= 60 ? `${n} min (${(n / 60).toFixed(1)} h)` : `${n} min`),
  },
];

/**
 * Choose between one Tapis job per step and one job for the whole pipeline, and
 * show exactly what the merged job would request before it is launched.
 *
 * The numbers are the point. A single job has ONE resource request, so the
 * per-step requests are merged into an envelope — the max of what any step needs,
 * except runtime, which is the sum because the steps run sequentially inside that
 * one allocation. Those are decisions a user has to be able to audit: a pipeline
 * that suddenly asks for 8 hours and a GPU should say which step did that. So
 * every row shows how it was derived and which steps drove it, and the whole
 * thing comes from the SAME planner the launch freezes onto the run
 * (backend/engine/single_job.py) rather than a re-implementation here.
 */
export default function SingleJobPanel({
  opened,
  templateVersionId,
  runOptions,
  mode,
  onModeChange,
  onPlanChange,
}: {
  opened: boolean;
  templateVersionId: number | null | undefined;
  // The Run Settings on screen. The preview depends on them (the exec target
  // follows whether any step needs a GPU), so it is re-fetched when they change.
  runOptions: Record<string, any>;
  mode: ExecutionMode;
  onModeChange: (mode: ExecutionMode) => void;
  // Lets the canvas refuse to launch a pipeline this mode can't express.
  onPlanChange?: (plan: SingleJobPlan | null) => void;
}) {
  const [plan, setPlan] = useState<SingleJobPlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const optionsKey = JSON.stringify(runOptions);

  useEffect(() => {
    if (!opened || mode !== "single_job" || !templateVersionId) {
      if (mode !== "single_job") {
        setPlan(null);
        onPlanChange?.(null);
      }
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch(`/api/workflow-templates/${templateVersionId}/single-job-preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runOptions),
    })
      .then(async (res) => {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
        return body as SingleJobPlan;
      })
      .then((p) => {
        if (cancelled) return;
        setPlan(p);
        onPlanChange?.(p);
      })
      .catch((e: any) => {
        if (cancelled) return;
        setPlan(null);
        onPlanChange?.(null);
        setError(e?.message || "Could not work out the merged job");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // onPlanChange is deliberately not a dependency: the canvas passes an inline
    // arrow, so including it would re-fetch on every render of the parent.
  }, [opened, mode, templateVersionId, optionsKey]);

  const r = plan?.resources;

  return (
    <Stack gap="xs">
      <SegmentedControl
        fullWidth
        size="xs"
        value={mode}
        onChange={(v) => onModeChange(v as ExecutionMode)}
        data={[
          { label: "One job per step", value: "per_step" },
          { label: "Single job", value: "single_job" },
        ]}
      />
      <Text size="xs" c="dimmed">
        {mode === "per_step"
          ? "Each step is its own Tapis job: it gets exactly the resources it asks for, and independent branches run at the same time. Each one queues separately."
          : "The whole pipeline runs sequentially inside ONE Tapis job — one queue wait instead of one per step, sized to fit every step."}
      </Text>

      {mode === "single_job" && (
        <>
          {loading && (
            <Group gap="xs">
              <Loader size={14} />
              <Text size="xs" c="dimmed">
                Working out the merged job…
              </Text>
            </Group>
          )}

          {error && (
            <Alert color="red" variant="light" title="Could not preview the single job">
              <Text size="xs">{error}</Text>
            </Alert>
          )}

          {plan && plan.blockers.length > 0 && (
            <Alert
              color="red"
              variant="light"
              icon={<IconAlertTriangle size={16} />}
              title="This workflow can't run as a single job"
            >
              <List size="xs" spacing={4}>
                {plan.blockers.map((b, i) => (
                  <List.Item key={i}>{b}</List.Item>
                ))}
              </List>
            </Alert>
          )}

          {plan && r && plan.blockers.length === 0 && (
            <Paper withBorder p="xs" radius="sm">
              <Stack gap={6}>
                <Group gap="xs" align="center">
                  <IconCpu size={14} />
                  <Text size="xs" fw={600} style={{ flex: 1 }}>
                    One job, {r.step_count} step{r.step_count === 1 ? "" : "s"}
                  </Text>
                  <Badge
                    size="xs"
                    variant="light"
                    color={r.needs_gpu ? "grape" : "gray"}
                    title={
                      r.needs_gpu
                        ? `GPU target, because ${(r.gpu_steps || []).join(", ")} need${
                            (r.gpu_steps || []).length === 1 ? "s" : ""
                          } one`
                        : "No step needs a GPU, so no GPU resources are requested"
                    }
                  >
                    {r.needs_gpu ? "GPU target" : "CPU target"}
                  </Badge>
                </Group>

                <Table withRowBorders={false} verticalSpacing={2} fz="xs">
                  <Table.Tbody>
                    {FIELDS.map(({ key, label, format }) => {
                      const d = r.derivation?.[key];
                      const value = (r as any)[key] as number;
                      return (
                        <Table.Tr key={key}>
                          <Table.Td c="dimmed" style={{ width: "38%" }}>
                            {label}
                          </Table.Td>
                          <Table.Td fw={600}>{format(value)}</Table.Td>
                          <Table.Td c="dimmed" ta="right">
                            {d && (
                              // The arithmetic, not just the answer: which steps
                              // set this number is the thing you need in order to
                              // change it.
                              <Tooltip
                                withArrow
                                multiline
                                w={260}
                                label={
                                  Object.entries(d.per_step)
                                    .map(([step, v]) => `${step}: ${v}`)
                                    .join("\n") || "no steps"
                                }
                              >
                                <span style={{ cursor: "help" }}>
                                  {d.how.startsWith("sum") ? "sum" : "max"}
                                  {d.driven_by.length > 0 && d.how.startsWith("max")
                                    ? ` · ${d.driven_by[0]}${
                                        d.driven_by.length > 1 ? ` +${d.driven_by.length - 1}` : ""
                                      }`
                                    : ""}
                                </span>
                              </Tooltip>
                            )}
                          </Table.Td>
                        </Table.Tr>
                      );
                    })}
                    <Table.Tr>
                      <Table.Td c="dimmed">Runs on</Table.Td>
                      <Table.Td colSpan={2} fw={600}>
                        {plan.exec.exec_system}
                        {plan.exec.exec_queue ? ` / ${plan.exec.exec_queue}` : ""}
                      </Table.Td>
                    </Table.Tr>
                    <Table.Tr>
                      <Table.Td c="dimmed">Tapis app</Table.Td>
                      <Table.Td colSpan={2}>
                        <Text size="xs" ff="monospace">
                          {plan.app.id}:{plan.app.version}
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  </Table.Tbody>
                </Table>

                <Text size="xs" c="dimmed">
                  Runtime is the <b>sum</b> of the steps because they run one after another in this
                  job; everything else is the <b>largest</b> any step asks for.
                </Text>

                {plan.exec.images_staged && (
                  <Text size="xs" c="dimmed">
                    Container images are staged into the job on each run — cache them on the exec
                    system to skip that transfer.
                  </Text>
                )}

                {plan.warnings.length > 0 && (
                  <Spoiler
                    maxHeight={0}
                    showLabel={`Show ${plan.warnings.length} note${
                      plan.warnings.length === 1 ? "" : "s"
                    }`}
                    hideLabel="Hide notes"
                    fz="xs"
                  >
                    <List size="xs" spacing={4} icon={<IconInfoCircle size={12} />}>
                      {plan.warnings.map((w, i) => (
                        <List.Item key={i}>{w}</List.Item>
                      ))}
                    </List>
                  </Spoiler>
                )}
              </Stack>
            </Paper>
          )}
        </>
      )}
    </Stack>
  );
}
