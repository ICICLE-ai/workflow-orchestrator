import { useEffect, useState } from "react";
import type { ComponentProps, ComponentType } from "react";
import {
  Stack,
  Group,
  Text,
  Title,
  Divider,
  Switch,
  NumberInput,
  TextInput,
  SimpleGrid,
  Tooltip,
  Loader,
} from "@mantine/core";
import { IconInfoCircle } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { BACKEND_URL, fetchCurrentUser } from "../lib/api";

// Type-only import — erased at build, so this browser-only package (talks to
// Patra + the Tapis vault directly from the client) never loads during SSR.
// The runtime module is loaded lazily below, client-side only.
import type { ModelSelector as ModelSelectorComponent } from "@icicle-ai/patra-model-selector";

const studioFetch = (path: string, init?: RequestInit) =>
  fetch(`${BACKEND_URL}${path}`, { ...init, credentials: "include" });

type ModelSelectorProps = ComponentProps<typeof ModelSelectorComponent>;

interface Libs {
  ModelSelector: ComponentType<ModelSelectorProps>;
}

// A field label with an info icon whose hover tooltip is the config_schema's
// own `description` — one source of truth for the helper text, shown here as
// a real tooltip instead of GenericConfigForm's static under-label text.
function LabelWithTooltip({ text, tip }: { text: string; tip?: string }) {
  return (
    <Group gap={4} wrap="nowrap">
      <Text size="sm" fw={500}>{text}</Text>
      {tip && (
        <Tooltip label={tip} multiline w={280} withArrow position="right">
          <IconInfoCircle size={14} style={{ cursor: "help", opacity: 0.6, flexShrink: 0 }} />
        </Tooltip>
      )}
    </Group>
  );
}

export default function FewShotAnnotationPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const schema = step.config_schema || {};
  const tip = (key: string) => schema[key]?.description;

  const field = (key: string) => config[key] !== undefined ? config[key] : schema[key]?.default;
  const setField = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  // annotation_file / ground_truth / dataset are wired input ports (see
  // step.json) — shown read-only here from the connected upstream node's
  // config, not editable directly. Every source-like step in this app (source
  // image dir/json file, smart_labeler) exposes its single value under "path".
  const wiredPath = (portName: string) => String(connectedInputs[portName]?.config?.path ?? "");

  const useSahi = Boolean(field("use_sahi"));

  // Client-only load of the Patra model selector (talks to Patra/Tapis
  // directly from the browser; see docs/adding-a-step-custom-ui.md §5).
  const [libs, setLibs] = useState<Libs | null>(null);
  useEffect(() => {
    let cancelled = false;
    import("@icicle-ai/patra-model-selector").then((mod) => {
      if (!cancelled) setLibs({ ModelSelector: mod.ModelSelector });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // ModelSelector needs a raw Tapis token + username (gated-model / HF-token
  // vault flow) — same "logged in but no Tapis session shouldn't bounce the
  // whole app to login" reasoning as smartLabeler.tsx's tapis-file-explorer use.
  const [tapisToken, setTapisToken] = useState<string | undefined>(undefined);
  const [tapisUsername, setTapisUsername] = useState<string | undefined>(undefined);
  useEffect(() => {
    studioFetch("/api/tapis/token")
      .then((res) => (res.ok ? res.json() : null))
      .then((d) => d && setTapisToken(d.token))
      .catch(() => {});
    fetchCurrentUser().then((u) => setTapisUsername(u?.username));
  }, []);

  return (
    <Stack gap="lg">
      <div>
        <Title order={6} mb="xs">Inputs</Title>
        <Stack gap="xs">
          <TextInput
            label={<LabelWithTooltip text="Annotation file" tip={tip("annotation_file")} />}
            value={wiredPath("annotation_file")}
            placeholder="Connect an annotation_file (JSON) input"
            readOnly
            variant="filled"
          />
          <TextInput
            label={<LabelWithTooltip text="Ground truth" tip={tip("ground_truth")} />}
            value={wiredPath("ground_truth")}
            placeholder="Connect a ground_truth (image directory) input"
            readOnly
            variant="filled"
          />
          <TextInput
            label={<LabelWithTooltip text="Dataset" tip={tip("dataset")} />}
            value={wiredPath("dataset")}
            placeholder="Connect a dataset (image directory) input"
            readOnly
            variant="filled"
          />
        </Stack>
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">Model cards</Title>
        {!libs || tapisToken === undefined ? (
          <Group justify="center" p="md"><Loader size="sm" /></Group>
        ) : (
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
            <div>
              <LabelWithTooltip text="Proposer" tip={tip("proposer_model")} />
              <libs.ModelSelector
                title="Select a proposer model"
                maxHeight={380}
                multiSelect={false}
                filterList={["04ac0992-d0ab-4a50-8a4f-92a5da89d848", "1eef29f7-cb6e-430a-a47f-b0075df2cd48"]}
                selectedModelIds={config.proposer_model ? [String(config.proposer_model)] : []}
                onModelSelect={(id) => setField("proposer_model", id)}
                onModelDeselect={(id) => {
                  if (id === config.proposer_model) setField("proposer_model", "");
                }}
                tapisToken={tapisToken}
                tapisUsername={tapisUsername}
              />
            </div>
            <div>
              <LabelWithTooltip text="Embedder" tip={tip("embedder_model")} />
              <libs.ModelSelector
                title="Select an embedder model"
                maxHeight={380}
                multiSelect={false}
                filterList={["8c517ed0-c9c0-4f57-bb9d-f066ab4ec34e", "04ac0992-d0ab-4a50-8a4f-92a5da89d848", "affa8339-13a8-41d0-95ed-475147e7900a"]}
                selectedModelIds={config.embedder_model ? [String(config.embedder_model)] : []}
                onModelSelect={(id) => setField("embedder_model", id)}
                onModelDeselect={(id) => {
                  if (id === config.embedder_model) setField("embedder_model", "");
                }}
                tapisToken={tapisToken}
                tapisUsername={tapisUsername}
              />
            </div>
          </SimpleGrid>
        )}
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">SAHI (Slicing Aided Hyper Inference)</Title>
        <Stack gap="xs">
          <Switch
            label={<LabelWithTooltip text="Enable SAHI" tip={tip("use_sahi")} />}
            checked={useSahi}
            onChange={(e) => setField("use_sahi", e.currentTarget.checked)}
          />
          {useSahi && (
            <Group grow>
              <NumberInput
                label={<LabelWithTooltip text="Tile size" tip={tip("tile_size")} />}
                value={Number(field("tile_size") ?? 640)}
                onChange={(v) => setField("tile_size", v)}
                min={32}
                step={32}
              />
              <NumberInput
                label={<LabelWithTooltip text="Overlap ratio" tip={tip("overlap_ratio")} />}
                value={Number(field("overlap_ratio") ?? 0.2)}
                onChange={(v) => setField("overlap_ratio", v)}
                min={0}
                max={1}
                step={0.05}
                decimalScale={2}
              />
            </Group>
          )}
        </Stack>
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">Batch size</Title>
        <NumberInput
          label={<LabelWithTooltip text="Batch size" tip={tip("batch_size")} />}
          value={Number(field("batch_size") ?? 8)}
          onChange={(v) => setField("batch_size", v)}
          min={1}
          step={1}
        />
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">Thresholds</Title>
        <Group grow>
          <NumberInput
            label={<LabelWithTooltip text="Confidence" tip={tip("confidence_threshold")} />}
            value={Number(field("confidence_threshold") ?? 0.3)}
            onChange={(v) => setField("confidence_threshold", v)}
            min={0}
            max={1}
            step={0.05}
            decimalScale={2}
          />
          <NumberInput
            label={<LabelWithTooltip text="Similarity" tip={tip("similarity_threshold")} />}
            value={Number(field("similarity_threshold") ?? 0.7)}
            onChange={(v) => setField("similarity_threshold", v)}
            min={0}
            max={1}
            step={0.05}
            decimalScale={2}
          />
          <NumberInput
            label={<LabelWithTooltip text="NMS IoU" tip={tip("nms_iou_threshold")} />}
            value={Number(field("nms_iou_threshold") ?? 0.5)}
            onChange={(v) => setField("nms_iou_threshold", v)}
            min={0}
            max={1}
            step={0.05}
            decimalScale={2}
          />
        </Group>
      </div>
    </Stack>
  );
}

// A wider centered modal — two side-by-side model card grids need more room
// than the default "lg" (see StepSettingsModal, which honors this).
(FewShotAnnotationPanel as any).modalSize = "1100px";
