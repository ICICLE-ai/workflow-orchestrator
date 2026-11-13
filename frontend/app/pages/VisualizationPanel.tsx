import { useEffect, useState } from "react";
import {
  Stack,
  Group,
  Text,
  Paper,
  Badge,
  Divider,
  Box,
  Loader,
  Alert,
  SimpleGrid,
  Progress,
  Anchor,
} from "@mantine/core";
import { IconChartBar, IconInfoCircle, IconExternalLink } from "@tabler/icons-react";
import { apiFetch } from "../lib/api";
import { resolveWiredLocation } from "../lib/tapis";
import type { StepPanelProps } from "./types";
import TrainingCurves, { parseResultsCsv, type EpochRow } from "../components/TrainingCurves";

// ─────────────────────────────────────────────────────────────
// VisualizationPanel — displays training performance metrics
// from a metrics.json, wherever it comes from:
//   - a Training node's `metrics` output (resolved once the run has
//     produced it — opened from the run page, see runs.$runId.tsx's
//     panelConnectedInputs), or
//   - a 📄 JSON File source pointing at any metrics.json, so a past
//     run's results can be viewed without re-running training.
// The wired value may name the file itself or a folder holding one.
// A YOLO run also leaves results.csv (one row per epoch) next to its
// metrics.json; when it's there, the per-epoch curves are charted too.
// Registered in registry.ts under "visualization".
// ─────────────────────────────────────────────────────────────

// The trainers' metrics.json: { task, framework, metrics: {...}, metadata: {...} }.
// `metrics` keys differ by framework (YOLO: "metrics/mAP50(M)", ...;
// HuggingFace: "eval_accuracy", ...), so they're rendered generically.
interface TrainingMetrics {
  task?: string;
  framework?: string;
  metrics?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

// Friendly names for the keys the trainers are known to write. Anything else
// is still shown, under its raw key.
const METRIC_LABELS: Record<string, string> = {
  "metrics/mAP50(B)": "Box mAP50",
  "metrics/mAP50-95(B)": "Box mAP50-95",
  "metrics/mAP50(M)": "Mask mAP50",
  "metrics/mAP50-95(M)": "Mask mAP50-95",
  "metrics/accuracy_top1": "Top-1 Accuracy",
  "metrics/accuracy_top5": "Top-5 Accuracy",
  eval_accuracy: "Accuracy",
  eval_loss: "Eval Loss",
  eval_dice_score: "Dice Score",
  eval_iou: "IoU",
  eval_map: "mAP",
  eval_runtime: "Eval Runtime",
  eval_samples_per_second: "Samples / s",
  eval_steps_per_second: "Steps / s",
  epoch: "Epochs Trained",
};

const METADATA_LABELS: Record<string, string> = {
  model: "Model",
  dataset: "Dataset",
  epochs: "Epochs",
  imgsz: "Image size",
  batch: "Batch size",
  lr: "Learning rate",
  lr0: "Learning rate",
  device: "Device",
  optimizer: "Optimizer",
  output_path: "Output",
  best_weights: "Best weights",
  last_weights: "Last weights",
  best_model: "Best model",
  timestamp: "Trained at",
};

// Scores in [0, 1] that read better as percentages.
const isRatio = (key: string) => /map|accuracy|iou|dice/i.test(key);

// The first of these present is the headline score shown as a bar.
const HEADLINE_KEYS = [
  "metrics/mAP50-95(M)", "metrics/mAP50-95(B)", "metrics/accuracy_top1", "eval_accuracy", "eval_map",
];

function MetricCard({ label, value, color = "blue", unit = "" }: {
  label: string;
  value: string | number;
  color?: string;
  unit?: string;
}) {
  return (
    <Paper withBorder p="md" radius="md">
      <Text size="xs" c="dimmed" mb={4}>{label}</Text>
      <Group gap={4} align="baseline">
        <Text size="xl" fw={700} c={`${color}.6`}>
          {value}
        </Text>
        {unit && <Text size="xs" c="dimmed">{unit}</Text>}
      </Group>
    </Paper>
  );
}

export default function VisualizationPanel({ config, step, connectedInputs }: StepPanelProps) {
  const [metrics, setMetrics] = useState<TrainingMetrics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [epochs, setEpochs] = useState<EpochRow[]>([]);

  const resultsPort = step.inputs.find((p) => p.data_type === "json_results")?.port_name ?? "results";
  const wired = connectedInputs?.[resultsPort];
  const loc = wired ? resolveWiredLocation(wired) : null;
  // A folder (e.g. a training run's <task>/<name>/) holds metrics.json.
  const metricsPath = loc ? (/\.json$/i.test(loc.path) ? loc.path : `${loc.path.replace(/\/+$/, "")}/metrics.json`) : "";
  const wandbProject = config?.wandb_project as string | undefined;
  const wandbUrl = config?.wandb_url as string | undefined;

  // Next to metrics.json, wherever that is. Absent for HuggingFace runs, which
  // simply get no curves.
  const resultsCsvPath = metricsPath ? `${metricsPath.replace(/\/[^/]*$/, "")}/results.csv` : "";
  useEffect(() => {
    setEpochs([]);
    if (!loc || !resultsCsvPath) return;
    let cancelled = false;
    apiFetch(`/api/tapis-files/content?system=${encodeURIComponent(loc.system)}&path=${encodeURIComponent(resultsCsvPath)}`)
      .then((res) => (res.ok ? res.text() : ""))
      .then((text) => { if (!cancelled) setEpochs(parseResultsCsv(text)); })
      .catch(() => { /* no curves, the scores still show */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc?.system, resultsCsvPath]);

  useEffect(() => {
    setMetrics(null);
    setError(null);
    if (!loc) return;
    let cancelled = false;
    setLoading(true);
    apiFetch(`/api/tapis-files/content?system=${encodeURIComponent(loc.system)}&path=${encodeURIComponent(metricsPath)}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(res.status === 404 ? "not found" : `HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => { if (!cancelled) setMetrics(data); })
      .catch((err) => {
        if (!cancelled) setError(`Could not load ${loc.system}:${metricsPath} (${err?.message || "unreadable"}).`);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc?.system, metricsPath]);

  const fmt = (key: string, n: number) =>
    isRatio(key) && n >= 0 && n <= 1 ? `${(n * 100).toFixed(2)}%` : Number.isInteger(n) ? String(n) : n.toFixed(4);

  const scores = Object.entries(metrics?.metrics || {})
    .filter((entry): entry is [string, number] => typeof entry[1] === "number");
  const headlineKey = HEADLINE_KEYS.find((k) => typeof metrics?.metrics?.[k] === "number");
  const headline = headlineKey ? (metrics!.metrics![headlineKey] as number) : undefined;
  const details = Object.entries(metrics?.metadata || {})
    .filter(([, v]) => v !== null && v !== undefined && v !== "" && typeof v !== "object");

  return (
    <Stack gap="md">
      {/* Header */}
      <Group gap="xs">
        <IconChartBar size={18} color="var(--mantine-color-blue-6)" />
        <Text fw={600} size="sm">Training Performance</Text>
        {metrics?.task && (
          <Badge size="xs" variant="light" color="teal">{metrics.task}</Badge>
        )}
        {metrics?.framework && (
          <Badge size="xs" variant="light" color="blue">{metrics.framework}</Badge>
        )}
      </Group>

      {/* Loading state */}
      {loading && (
        <Group justify="center" py="xl">
          <Loader size="sm" />
          <Text size="sm" c="dimmed">Loading metrics...</Text>
        </Group>
      )}

      {/* Error state */}
      {error && (
        <Alert icon={<IconInfoCircle size={14} />} color="orange" variant="light">
          <Text size="xs">{error}</Text>
          <Text size="xs" c="dimmed" mt={4}>
            A Training node's metrics exist only once its run has finished — open this step from that run's page.
            Otherwise check the JSON File source's path.
          </Text>
        </Alert>
      )}

      {/* Nothing wired, or wired to a step whose output isn't known yet */}
      {!loc && !loading && (
        <Alert icon={<IconInfoCircle size={14} />} color="gray" variant="light">
          <Text size="xs">
            {wired
              ? "The connected step's metrics will be available once it has run. Open this step from the run's page to see them."
              : <>Connect a <strong>📄 JSON File</strong> source (any metrics.json) or a Training node's <strong>metrics</strong> output to see results.</>}
          </Text>
        </Alert>
      )}

      {/* Metrics display */}
      {metrics && !loading && (
        <>
          {/* Key metrics */}
          {scores.length > 0 ? (
            <SimpleGrid cols={2} spacing="sm">
              {scores.map(([key, value]) => (
                <MetricCard
                  key={key}
                  label={METRIC_LABELS[key] ?? key}
                  value={fmt(key, value)}
                  color={/loss/i.test(key) ? "red" : isRatio(key) ? "green" : "gray"}
                />
              ))}
            </SimpleGrid>
          ) : (
            <Text size="xs" c="dimmed">This metrics.json has no numeric scores.</Text>
          )}

          {/* Headline score bar */}
          {headlineKey && headline !== undefined && headline >= 0 && headline <= 1 && (
            <Box>
              <Group justify="space-between" mb={4}>
                <Text size="xs" fw={500}>{METRIC_LABELS[headlineKey] ?? headlineKey}</Text>
                <Text size="xs" c="dimmed">{(headline * 100).toFixed(2)}%</Text>
              </Group>
              <Progress
                value={headline * 100}
                color={headline > 0.9 ? "green" : headline > 0.7 ? "yellow" : "red"}
                size="md"
                radius="sm"
              />
            </Box>
          )}

          {/* Per-epoch curves */}
          {epochs.length > 0 && (
            <>
              <Divider label="Training curves" labelPosition="left" />
              <TrainingCurves
                rows={epochs}
                plannedEpochs={typeof metrics.metadata?.epochs === "number" ? metrics.metadata.epochs : undefined}
              />
            </>
          )}

          <Divider />

          {/* Metadata */}
          {details.length > 0 && (
            <Stack gap={4}>
              <Text size="xs" fw={600} c="dimmed">Training Details</Text>
              {details.map(([key, value]) => (
                <Group key={key} gap={6} wrap="nowrap" align="flex-start">
                  <Text size="xs" c="dimmed" w={90} style={{ flexShrink: 0 }}>{METADATA_LABELS[key] ?? key}</Text>
                  <Text size="xs" style={{ fontFamily: "monospace", wordBreak: "break-all" }}>
                    {key === "timestamp" ? new Date(String(value)).toLocaleString() : String(value)}
                  </Text>
                </Group>
              ))}
            </Stack>
          )}

          {/* W&B link */}
          {(wandbUrl || wandbProject) && (
            <>
              <Divider />
              <Group gap={6}>
                <IconExternalLink size={14} color="var(--mantine-color-blue-6)" />
                <Text size="xs" fw={500}>Live Charts</Text>
                {wandbUrl && (
                  <Anchor href={wandbUrl} target="_blank" size="xs">
                    Open W&B Dashboard
                  </Anchor>
                )}
                {!wandbUrl && wandbProject && (
                  <Anchor
                    href={`https://wandb.ai/${wandbProject}`}
                    target="_blank"
                    size="xs"
                  >
                    Open W&B Project
                  </Anchor>
                )}
              </Group>
            </>
          )}
        </>
      )}
    </Stack>
  );
}
