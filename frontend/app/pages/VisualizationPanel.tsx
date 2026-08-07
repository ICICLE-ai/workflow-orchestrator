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
import type { StepPanelProps } from "./types";

// ─────────────────────────────────────────────────────────────
// VisualizationPanel — displays training performance metrics
// from the metrics.json output of a training step.
// Registered in registry.ts under "visualization".
// ─────────────────────────────────────────────────────────────

interface TrainingMetrics {
  task?: string;
  framework?: string;
  metrics?: {
    eval_loss?: number;
    eval_accuracy?: number;
    eval_runtime?: number;
    eval_samples_per_second?: number;
    eval_steps_per_second?: number;
    epoch?: number;
    eval_dice_score?: number;
    eval_iou?: number;
    eval_map?: number;
  };
  metadata?: {
    model?: string;
    dataset?: string;
    epochs?: number;
    batch?: number;
    lr?: number;
    timestamp?: string;
    best_model?: string;
  };
}

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

export default function VisualizationPanel({ config, step }: StepPanelProps) {
  const [metrics, setMetrics] = useState<TrainingMetrics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Try to load metrics from the connected input (results port)
  const resultsPath = config?.results as string | undefined;
  const wandbProject = config?.wandb_project as string | undefined;
  const wandbUrl = config?.wandb_url as string | undefined;

  useEffect(() => {
    if (!resultsPath) return;
    setLoading(true);
    setError(null);

    // Fetch metrics.json via the backend files API
    apiFetch(`/api/files/read?path=${encodeURIComponent(resultsPath + "/metrics.json")}`)
      .then((res) => res.json())
      .then((data) => {
        setMetrics(data);
        setLoading(false);
      })
      .catch(() => {
        setError("Could not load metrics.json from the training output path.");
        setLoading(false);
      });
  }, [resultsPath]);

  // Format numbers nicely
  const fmt = (n?: number, decimals = 4) =>
    n !== undefined ? n.toFixed(decimals) : "—";

  const accuracy = metrics?.metrics?.eval_accuracy;
  const loss = metrics?.metrics?.eval_loss;
  const epoch = metrics?.metrics?.epoch;
  const diceScore = metrics?.metrics?.eval_dice_score;
  const iou = metrics?.metrics?.eval_iou;
  const mAP = metrics?.metrics?.eval_map;

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
            Connect this step to a training output or check the results path.
          </Text>
        </Alert>
      )}

      {/* No results path */}
      {!resultsPath && !loading && (
        <Alert icon={<IconInfoCircle size={14} />} color="gray" variant="light">
          <Text size="xs">
            Connect this step to the <strong>metrics</strong> output of a Training node to see results.
          </Text>
        </Alert>
      )}

      {/* Metrics display */}
      {metrics && !loading && (
        <>
          {/* Key metrics */}
          <SimpleGrid cols={2} spacing="sm">
            {accuracy !== undefined && (
              <MetricCard
                label="Accuracy"
                value={`${(accuracy * 100).toFixed(2)}%`}
                color="green"
              />
            )}
            {loss !== undefined && (
              <MetricCard
                label="Eval Loss"
                value={fmt(loss, 4)}
                color="red"
              />
            )}
            {diceScore !== undefined && (
              <MetricCard
                label="Dice Score"
                value={fmt(diceScore, 4)}
                color="violet"
              />
            )}
            {iou !== undefined && (
              <MetricCard
                label="IoU"
                value={fmt(iou, 4)}
                color="orange"
              />
            )}
            {mAP !== undefined && (
              <MetricCard
                label="mAP"
                value={fmt(mAP, 4)}
                color="blue"
              />
            )}
            {epoch !== undefined && (
              <MetricCard
                label="Epochs Trained"
                value={epoch}
                color="gray"
              />
            )}
          </SimpleGrid>

          {/* Accuracy bar */}
          {accuracy !== undefined && (
            <Box>
              <Group justify="space-between" mb={4}>
                <Text size="xs" fw={500}>Accuracy</Text>
                <Text size="xs" c="dimmed">{(accuracy * 100).toFixed(2)}%</Text>
              </Group>
              <Progress
                value={accuracy * 100}
                color={accuracy > 0.9 ? "green" : accuracy > 0.7 ? "yellow" : "red"}
                size="md"
                radius="sm"
              />
            </Box>
          )}

          <Divider />

          {/* Metadata */}
          {metrics.metadata && (
            <Stack gap={4}>
              <Text size="xs" fw={600} c="dimmed">Training Details</Text>
              {metrics.metadata.model && (
                <Group gap={6}>
                  <Text size="xs" c="dimmed" w={80}>Model</Text>
                  <Text size="xs" style={{ fontFamily: "monospace" }}>{metrics.metadata.model}</Text>
                </Group>
              )}
              {metrics.metadata.epochs && (
                <Group gap={6}>
                  <Text size="xs" c="dimmed" w={80}>Epochs</Text>
                  <Text size="xs">{metrics.metadata.epochs}</Text>
                </Group>
              )}
              {metrics.metadata.batch && (
                <Group gap={6}>
                  <Text size="xs" c="dimmed" w={80}>Batch size</Text>
                  <Text size="xs">{metrics.metadata.batch}</Text>
                </Group>
              )}
              {metrics.metadata.lr && (
                <Group gap={6}>
                  <Text size="xs" c="dimmed" w={80}>Learning rate</Text>
                  <Text size="xs">{metrics.metadata.lr}</Text>
                </Group>
              )}
              {metrics.metadata.timestamp && (
                <Group gap={6}>
                  <Text size="xs" c="dimmed" w={80}>Trained at</Text>
                  <Text size="xs">{new Date(metrics.metadata.timestamp).toLocaleString()}</Text>
                </Group>
              )}
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