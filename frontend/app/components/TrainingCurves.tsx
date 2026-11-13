import { useMemo, useState } from "react";
import { Box, Group, Paper, SimpleGrid, Stack, Text } from "@mantine/core";

// Per-epoch training curves from a YOLO (Ultralytics) results.csv: one row per
// epoch, with columns like train/box_loss, val/box_loss, metrics/mAP50(B),
// lr/pg0. Columns are grouped into charts by name, so detect, segment, pose
// and classify runs each get the charts their own columns support, and a
// column nothing claims still gets a chart of its own group. Plain SVG — no
// chart library — coloured from the Mantine palette so it follows the theme.

export type EpochRow = Record<string, number>;

export function parseResultsCsv(text: string): EpochRow[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return [];
  // Older Ultralytics versions pad the header names with spaces.
  const header = lines[0].split(",").map((h) => h.trim());
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    const row: EpochRow = {};
    header.forEach((h, i) => {
      const n = Number(cells[i]);
      if (h && Number.isFinite(n)) row[h] = n;
    });
    return row;
  }).filter((r) => Number.isFinite(r.epoch));
}

interface Series { key: string; label: string; color: string; dashed?: boolean }
interface Chart { title: string; series: Series[]; percent?: boolean; hint?: string }

const PALETTE = ["blue", "orange", "teal", "grape", "red", "cyan", "lime", "pink"];
const color = (name: string) => `var(--mantine-color-${name}-6)`;

const METRIC_NAMES: Record<string, string> = {
  precision: "Precision", recall: "Recall", mAP50: "mAP50", "mAP50-95": "mAP50-95",
  accuracy_top1: "Top-1 accuracy", accuracy_top5: "Top-5 accuracy",
};
const METRIC_GROUPS: Record<string, string> = { B: "Box", M: "Mask", P: "Pose" };
const LOSS_NAMES: Record<string, string> = {
  box: "Box loss", seg: "Segmentation loss", cls: "Classification loss", dfl: "DFL loss (box edges)",
  pose: "Pose loss", kobj: "Keypoint objectness loss", sem: "Semantic loss",
};

// A column that never moves off zero (e.g. sem_loss on a model without that
// head) is noise on a chart.
const varies = (rows: EpochRow[], key: string) => rows.some((r) => (r[key] ?? 0) !== 0);

export function buildCharts(rows: EpochRow[]): Chart[] {
  if (!rows.length) return [];
  const columns = Object.keys(rows[0]).filter((k) => k !== "epoch" && k !== "time" && varies(rows, k));
  const used = new Set<string>();
  const charts: Chart[] = [];

  // Losses: train and validation of the same loss on one chart, so
  // over-fitting (val rising while train falls) is visible at a glance.
  const losses = Array.from(new Set(columns
    .filter((c) => /^(train|val)\/.+loss$/.test(c))
    .map((c) => c.split("/")[1])));
  for (const loss of losses) {
    const series: Series[] = [];
    for (const [split, c, dashed] of [["train", "blue", false], ["val", "orange", true]] as const) {
      const key = `${split}/${loss}`;
      if (columns.includes(key)) {
        series.push({ key, label: split === "train" ? "Train" : "Validation", color: color(c), dashed });
        used.add(key);
      }
    }
    const name = loss.replace(/_loss$/, "");
    charts.push({ title: LOSS_NAMES[name] ?? `${name.replace(/_/g, " ")} loss`, series, hint: "lower is better" });
  }

  // Scores, one chart per head: metrics/<name>(B) is box, (M) mask, (P) pose;
  // a classify run's metrics/accuracy_* have no suffix.
  const scores = columns.filter((c) => c.startsWith("metrics/"));
  const byHead = new Map<string, string[]>();
  for (const c of scores) {
    const head = c.match(/\(([A-Z])\)$/)?.[1] ?? "";
    byHead.set(head, [...(byHead.get(head) || []), c]);
  }
  for (const [head, keys] of byHead) {
    charts.push({
      title: head ? `${METRIC_GROUPS[head] ?? head} metrics` : "Metrics",
      percent: true,
      hint: "higher is better",
      series: keys.map((key, i) => {
        used.add(key);
        const name = key.replace(/^metrics\//, "").replace(/\([A-Z]\)$/, "");
        return { key, label: METRIC_NAMES[name] ?? name, color: color(PALETTE[(i + 2) % PALETTE.length]) };
      }),
    });
  }

  // Learning rate per parameter group — usually identical, so only distinct ones.
  const lrs = columns.filter((c) => c.startsWith("lr/"));
  const distinct = lrs.filter((k, i) => !lrs.slice(0, i).some((p) => rows.every((r) => r[p] === r[k])));
  lrs.forEach((k) => used.add(k));
  if (distinct.length) {
    charts.push({
      title: "Learning rate",
      series: distinct.map((key, i) => ({
        key, label: distinct.length > 1 ? key.replace(/^lr\//, "") : "Learning rate", color: color(PALETTE[i % PALETTE.length]),
      })),
    });
  }

  // Anything else, grouped by its prefix.
  const rest = new Map<string, string[]>();
  for (const c of columns.filter((c) => !used.has(c))) {
    const group = c.includes("/") ? c.split("/")[0] : "Other";
    rest.set(group, [...(rest.get(group) || []), c]);
  }
  for (const [group, keys] of rest) {
    charts.push({
      title: group,
      series: keys.map((key, i) => ({ key, label: key.replace(`${group}/`, ""), color: color(PALETTE[i % PALETTE.length]) })),
    });
  }
  return charts;
}

const W = 360, H = 180, PAD = { l: 44, r: 10, t: 10, b: 26 };

function fmtValue(v: number, percent?: boolean) {
  if (percent) return `${(v * 100).toFixed(1)}%`;
  const a = Math.abs(v);
  return a !== 0 && (a < 0.001 || a >= 10000) ? v.toExponential(2) : Number(v.toPrecision(4)).toString();
}

function LineChart({ chart, rows, bestEpoch }: { chart: Chart; rows: EpochRow[]; bestEpoch?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const epochs = rows.map((r) => r.epoch);
  const values = chart.series.flatMap((s) => rows.map((r) => r[s.key]).filter((v) => v !== undefined));
  let lo = Math.min(...values), hi = Math.max(...values);
  if (chart.percent) { lo = Math.min(lo, 0); hi = Math.max(hi, Math.min(1, hi * 1.05)); }
  if (lo === hi) { lo -= Math.abs(lo) * 0.1 || 1; hi += Math.abs(hi) * 0.1 || 1; }
  const e0 = epochs[0], e1 = epochs[epochs.length - 1];
  const x = (e: number) => PAD.l + (e1 === e0 ? (W - PAD.l - PAD.r) / 2 : ((e - e0) / (e1 - e0)) * (W - PAD.l - PAD.r));
  const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + f * (hi - lo));
  const step = Math.max(1, Math.ceil(epochs.length / 6));
  const xTicks = epochs.filter((_, i) => i % step === 0 || i === epochs.length - 1);
  const row = hover !== null ? rows[hover] : null;

  return (
    <Paper withBorder p="sm" radius="md">
      <Group justify="space-between" mb={4} wrap="nowrap">
        <Text size="xs" fw={600}>{chart.title}</Text>
        {chart.hint && <Text size="xs" c="dimmed">{chart.hint}</Text>}
      </Group>
      <Box pos="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          role="img"
          aria-label={`${chart.title} by epoch`}
          style={{ display: "block", overflow: "visible" }}
          onMouseLeave={() => setHover(null)}
          onMouseMove={(ev) => {
            const box = ev.currentTarget.getBoundingClientRect();
            const px = ((ev.clientX - box.left) / box.width) * W;
            let best = 0;
            epochs.forEach((e, i) => { if (Math.abs(x(e) - px) < Math.abs(x(epochs[best]) - px)) best = i; });
            setHover(best);
          }}
        >
          {yTicks.map((t, i) => (
            <g key={i}>
              <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke="var(--mantine-color-default-border)" strokeWidth={0.5} />
              <text x={PAD.l - 4} y={y(t) + 3} fontSize={9} textAnchor="end" fill="var(--mantine-color-dimmed)">{fmtValue(t, chart.percent)}</text>
            </g>
          ))}
          {xTicks.map((e) => (
            <text key={e} x={x(e)} y={H - 8} fontSize={9} textAnchor="middle" fill="var(--mantine-color-dimmed)">{e}</text>
          ))}
          <text x={(PAD.l + W - PAD.r) / 2} y={H + 4} fontSize={9} textAnchor="middle" fill="var(--mantine-color-dimmed)">epoch</text>
          {bestEpoch !== undefined && epochs.includes(bestEpoch) && (
            <line x1={x(bestEpoch)} x2={x(bestEpoch)} y1={PAD.t} y2={H - PAD.b}
              stroke="var(--mantine-color-green-6)" strokeWidth={1} strokeDasharray="3 3" />
          )}
          {chart.series.map((s) => {
            const pts = rows.filter((r) => r[s.key] !== undefined);
            return (
              <g key={s.key}>
                <polyline
                  fill="none" stroke={s.color} strokeWidth={1.75} strokeLinejoin="round"
                  strokeDasharray={s.dashed ? "5 3" : undefined}
                  points={pts.map((r) => `${x(r.epoch)},${y(r[s.key])}`).join(" ")}
                />
                {/* Dots only where there are few epochs; with many they just clutter. */}
                {pts.length <= 30 && pts.map((r) => <circle key={r.epoch} cx={x(r.epoch)} cy={y(r[s.key])} r={2} fill={s.color} />)}
              </g>
            );
          })}
          {row && (
            <line x1={x(row.epoch)} x2={x(row.epoch)} y1={PAD.t} y2={H - PAD.b} stroke="var(--mantine-color-dimmed)" strokeWidth={0.75} />
          )}
        </svg>
        {row && (
          <Paper
            withBorder shadow="sm" p={6} radius="sm"
            style={{
              position: "absolute", top: 4, pointerEvents: "none", zIndex: 1,
              ...(x(row.epoch) > W / 2 ? { left: 8 } : { right: 8 }),
            }}
          >
            <Text size="xs" fw={600}>Epoch {row.epoch}{row.epoch === bestEpoch ? " · best" : ""}</Text>
            {chart.series.map((s) => row[s.key] !== undefined && (
              <Group key={s.key} gap={6} wrap="nowrap">
                <Box w={8} h={8} style={{ background: s.color, borderRadius: 2, flexShrink: 0 }} />
                <Text size="xs">{s.label}: {fmtValue(row[s.key], chart.percent)}</Text>
              </Group>
            ))}
          </Paper>
        )}
      </Box>
      <Group gap="sm" mt={4}>
        {chart.series.map((s) => (
          <Group key={s.key} gap={4} wrap="nowrap">
            <Box w={12} h={2} style={{ background: s.color }} />
            <Text size="xs" c="dimmed">{s.label}</Text>
          </Group>
        ))}
      </Group>
    </Paper>
  );
}

// The epoch Ultralytics keeps as best.pt: highest fitness, which it defines as
// 0.1·mAP50 + 0.9·mAP50-95 (summed over box and mask for segmentation).
function bestEpochOf(rows: EpochRow[]): number | undefined {
  const keys = Object.keys(rows[0] || {});
  const m50 = keys.filter((k) => /^metrics\/mAP50\([A-Z]\)$/.test(k));
  const m5095 = keys.filter((k) => /^metrics\/mAP50-95\([A-Z]\)$/.test(k));
  const top1 = keys.find((k) => k === "metrics/accuracy_top1");
  const fitness = (r: EpochRow) =>
    m5095.length ? m50.reduce((s, k) => s + 0.1 * r[k], 0) + m5095.reduce((s, k) => s + 0.9 * r[k], 0)
      : top1 ? r[top1] : undefined;
  if (fitness(rows[0] || {}) === undefined) return undefined;
  return rows.reduce((b, r) => (fitness(r)! > fitness(b)! ? r : b)).epoch;
}

export default function TrainingCurves({ rows, plannedEpochs }: { rows: EpochRow[]; plannedEpochs?: number }) {
  const charts = useMemo(() => buildCharts(rows), [rows]);
  const best = useMemo(() => bestEpochOf(rows), [rows]);
  if (!charts.length) return null;
  const last = rows[rows.length - 1]?.epoch;
  const stoppedEarly = plannedEpochs !== undefined && last !== undefined && last < plannedEpochs;

  return (
    <Stack gap="sm">
      <Group gap="md">
        <Text size="xs" c="dimmed">
          {rows.length} epoch{rows.length === 1 ? "" : "s"} trained
          {stoppedEarly ? ` of ${plannedEpochs} planned — stopped early (no improvement within the patience window)` : ""}
        </Text>
        {best !== undefined && (
          <Group gap={4} wrap="nowrap">
            <Box w={12} style={{ borderTop: "1px dashed var(--mantine-color-green-6)" }} />
            <Text size="xs" c="dimmed">best epoch ({best}), kept as best.pt</Text>
          </Group>
        )}
      </Group>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
        {charts.map((c) => <LineChart key={c.title} chart={c} rows={rows} bestEpoch={best} />)}
      </SimpleGrid>
    </Stack>
  );
}
