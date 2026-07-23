import { useState } from "react";
import {
  Stack,
  Group,
  Select,
  NumberInput,
  TextInput,
  PasswordInput,
  Text,
  Badge,
  Divider,
  Paper,
  Box,
  Tabs,
  Alert,
  ActionIcon,
  Button,
} from "@mantine/core";
import { IconBrain, IconKey, IconSettings, IconInfoCircle, IconPlus, IconTrash } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";

// Model registry — each entry has the HuggingFace/YOLO model ID,
// a human-readable label, the task it supports, a short description,
// and the extra params the user can pass via the + button.
const MODELS = [
  // Classification
  {
    value: "google/vit-base-patch16-224",
    label: "ViT Base (Classification)",
    task: "classify",
    framework: "huggingface",
    description: "Vision Transformer from Google. Strong general-purpose image classifier.",
    params: [
      { key: "lr",           description: "Learning rate",              default: "0.00002" },
      { key: "weight_decay", description: "Weight decay",               default: "0.01"    },
      { key: "warmup_steps", description: "Warmup steps",               default: "0"       },
      { key: "seed",         description: "Random seed",                default: "42"      },
      { key: "patience",     description: "Early stopping patience",    default: "3"       },
    ],
  },
  {
    value: "microsoft/resnet-50",
    label: "ResNet-50 (Classification)",
    task: "classify",
    framework: "huggingface",
    description: "Classic CNN backbone from Microsoft. Fast and reliable for classification.",
    params: [
      { key: "lr",           description: "Learning rate",              default: "0.0001"  },
      { key: "weight_decay", description: "Weight decay",               default: "0.01"    },
      { key: "seed",         description: "Random seed",                default: "42"      },
      { key: "patience",     description: "Early stopping patience",    default: "3"       },
    ],
  },
  // Detection
  {
    value: "facebook/detr-resnet-50",
    label: "DETR ResNet-50 (Detection)",
    task: "detect",
    framework: "huggingface",
    description: "Detection Transformer from Meta. End-to-end object detection without anchors.",
    params: [
      { key: "lr",           description: "Learning rate",              default: "0.0001"  },
      { key: "weight_decay", description: "Weight decay",               default: "0.0001"  },
      { key: "seed",         description: "Random seed",                default: "42"      },
    ],
  },
  {
    value: "yolov8n.pt",
    label: "YOLOv8 Nano (Detection)",
    task: "detect",
    framework: "yolo",
    description: "Ultralytics YOLOv8 nano. Fastest YOLO model, great for quick experiments.",
    params: [
      { key: "imgsz",    description: "Image size",              default: "640"  },
      { key: "conf",     description: "Confidence threshold",    default: "0.25" },
      { key: "iou",      description: "IoU threshold",           default: "0.7"  },
      { key: "momentum", description: "SGD momentum",            default: "0.937"},
      { key: "lr0",      description: "Initial learning rate",   default: "0.01" },
    ],
  },
  {
    value: "yolov8s.pt",
    label: "YOLOv8 Small (Detection)",
    task: "detect",
    framework: "yolo",
    description: "Ultralytics YOLOv8 small. Better accuracy than nano, still fast.",
    params: [
      { key: "imgsz",    description: "Image size",              default: "640"  },
      { key: "conf",     description: "Confidence threshold",    default: "0.25" },
      { key: "iou",      description: "IoU threshold",           default: "0.7"  },
      { key: "momentum", description: "SGD momentum",            default: "0.937"},
      { key: "lr0",      description: "Initial learning rate",   default: "0.01" },
    ],
  },
  // Segmentation
  {
    value: "nvidia/mit-b0",
    label: "SegFormer B0 (Segmentation)",
    task: "segment",
    framework: "huggingface",
    description: "Lightweight SegFormer from NVIDIA. Efficient semantic segmentation.",
    params: [
      { key: "lr",           description: "Learning rate",              default: "0.00006" },
      { key: "weight_decay", description: "Weight decay",               default: "0.01"    },
      { key: "seed",         description: "Random seed",                default: "42"      },
      { key: "patience",     description: "Early stopping patience",    default: "3"       },
    ],
  },
  {
    value: "facebook/sam-vit-base",
    label: "SAM ViT Base (Segmentation)",
    task: "segment",
    framework: "huggingface",
    description: "Segment Anything Model from Meta. Promptable instance segmentation.",
    params: [
      { key: "lr",       description: "Learning rate",           default: "0.0001" },
      { key: "patience", description: "Early stopping patience", default: "3"      },
      { key: "seed",     description: "Random seed",             default: "42"     },
    ],
  },
  {
    value: "facebook/sam-vit-large",
    label: "SAM ViT Large (Segmentation)",
    task: "segment",
    framework: "huggingface",
    description: "Larger SAM model. Better accuracy, needs more GPU memory.",
    params: [
      { key: "lr",       description: "Learning rate",           default: "0.0001" },
      { key: "patience", description: "Early stopping patience", default: "3"      },
      { key: "seed",     description: "Random seed",             default: "42"     },
    ],
  },
  {
    value: "yolov8n-seg.pt",
    label: "YOLOv8 Nano Seg (Segmentation)",
    task: "segment",
    framework: "yolo",
    description: "YOLOv8 nano segmentation. Fast instance segmentation.",
    params: [
      { key: "imgsz",    description: "Image size",              default: "640"  },
      { key: "conf",     description: "Confidence threshold",    default: "0.25" },
      { key: "iou",      description: "IoU threshold",           default: "0.7"  },
      { key: "lr0",      description: "Initial learning rate",   default: "0.01" },
    ],
  },
];

const TASKS = [
  { value: "classify", label: "Image Classification" },
  { value: "detect",   label: "Object Detection"     },
  { value: "segment",  label: "Segmentation"         },
];

// ─────────────────────────────────────────────────────────────
// TrainingPanel — custom settings UI for the training step.
// Registered in registry.ts under "training".
// ─────────────────────────────────────────────────────────────
export default function TrainingPanel({ config, onChange, onSave, step }: StepPanelProps) {
  const val = (key: string, fallback?: unknown) => {
    const v = config[key];
    return v !== undefined ? v : (step.config_schema?.[key]?.default ?? fallback);
  };
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  // Build extra_args string from key-value pairs list
  // Called every time params change so extra_args is always up to date
  const buildExtraArgs = (params: {key: string; value: string}[]) =>
    params
      .filter((p) => p.key.trim())
      .map((p) => `--${p.key.trim()} ${p.value.trim()}`.trim())
      .join(" ");

  // Derive filtered model list from selected task
  const selectedTask = String(val("task", "classify"));
  const filteredModels = MODELS.filter((m) => m.task === selectedTask);
  const selectedModel = String(val("model", filteredModels[0]?.value ?? ""));
  const modelInfo = MODELS.find((m) => m.value === selectedModel);

  // When task changes, auto-select the first compatible model
  const handleTaskChange = (task: string | null) => {
    if (!task) return;
    const compatible = MODELS.filter((m) => m.task === task);
    const firstModel = compatible[0];
    onChange({
      ...config,
      task,
      model: firstModel?.value ?? "",
      framework: firstModel?.framework ?? "huggingface",
    });
  };

  const handleModelChange = (model: string | null) => {
    if (!model) return;
    const info = MODELS.find((m) => m.value === model);
    onChange({
      ...config,
      model,
      framework: info?.framework ?? "huggingface",
    });
  };

  return (
    <Tabs defaultValue="model">
      <Tabs.List mb="md">
        <Tabs.Tab value="model"   leftSection={<IconBrain   size={14} />}>Model</Tabs.Tab>
        <Tabs.Tab value="train"   leftSection={<IconSettings size={14} />}>Training</Tabs.Tab>
        <Tabs.Tab value="secrets" leftSection={<IconKey     size={14} />}>API Keys</Tabs.Tab>
      </Tabs.List>

      {/* ── Tab 1: Model selection ──────────────────────────── */}
      <Tabs.Panel value="model">
        <Stack gap="md">
          <Select
            label="Task"
            description="What kind of prediction do you want the model to make?"
            data={TASKS}
            value={selectedTask}
            onChange={handleTaskChange}
            allowDeselect={false}
          />

          <Select
            label="Model"
            description="Choose a model compatible with the selected task."
            data={filteredModels.map((m) => ({ value: m.value, label: m.label }))}
            value={selectedModel}
            onChange={handleModelChange}
            allowDeselect={false}
          />

          {/* Model info card */}
          {modelInfo && (
            <Paper withBorder p="sm" radius="md" bg="blue.0">
              <Group gap="xs" mb={4}>
                <Text size="sm" fw={600}>{modelInfo.label}</Text>
                <Badge size="xs" variant="light" color="blue">
                  {modelInfo.framework === "yolo" ? "YOLO" : "HuggingFace"}
                </Badge>
                <Badge size="xs" variant="light" color="teal">
                  {modelInfo.task}
                </Badge>
              </Group>
              <Text size="xs" c="dimmed">{modelInfo.description}</Text>
              <Text size="xs" c="blue.6" mt={4} style={{ fontFamily: "monospace" }}>
                {modelInfo.value}
              </Text>

            </Paper>
          )}

          <Alert
            icon={<IconInfoCircle size={14} />}
            color="gray"
            variant="light"
            title="Not seeing your model?"
          >
            <Text size="xs">
              All HuggingFace models compatible with AutoModelForImageClassification,
              AutoModelForObjectDetection, and AutoModelForSemanticSegmentation are supported.
              Type the model ID directly in the training config if it's not listed here.
            </Text>
          </Alert>
        </Stack>
      </Tabs.Panel>

      {/* ── Tab 2: Training config ──────────────────────────── */}
      <Tabs.Panel value="train">
        <Stack gap="md">
          <Group grow>
            <NumberInput
              label="Epochs"
              description="Number of full passes through the training dataset."
              min={1}
              max={1000}
              value={Number(val("epochs", 10))}
              onChange={(v) => set("epochs", v)}
            />
            <NumberInput
              label="Batch size"
              description="Number of images processed per step."
              min={1}
              max={256}
              value={Number(val("batch_size", 16))}
              onChange={(v) => set("batch_size", v)}
            />
          </Group>

          <Select
            label="Device"
            description="Run training on GPU (faster) or CPU."
            data={[
              { value: "cuda", label: "GPU (CUDA) — recommended" },
              { value: "cpu",  label: "CPU — slower, no GPU required" },
            ]}
            value={String(val("device", "cuda"))}
            onChange={(v) => set("device", v ?? "cuda")}
            allowDeselect={false}
          />

          <Divider label="Output" labelPosition="left" />

          <TextInput
            label="Output path"
            description="Where to save the trained model and metrics on OSC."
            placeholder="/fs/scratch/PAS2699/harvest_jobs/outputs"
            value={String(val("output_path", ""))}
            onChange={(e) => set("output_path", e.currentTarget.value)}
          />

          <Divider label="Additional Parameters" labelPosition="left" />

          {/* Supported params hint box */}
          {modelInfo && modelInfo.params && modelInfo.params.length > 0 && (
            <Paper withBorder p="sm" radius="md" bg="blue.0">
              <Text size="xs" fw={600} mb={6}>Supported parameters for {modelInfo.label}:</Text>
              {modelInfo.params.map((p: any) => (
                <Group key={p.key} gap={4} mb={2}>
                  <Text size="xs" c="blue.7" style={{ fontFamily: "monospace", minWidth: 120 }}>
                    {p.key}
                  </Text>
                  <Text size="xs" c="dimmed">— {p.description} (default: {p.default})</Text>
                </Group>
              ))}
            </Paper>
          )}

          {/* Dynamic key-value pairs */}
          {(val("extra_params", []) as {key: string; value: string}[]).map((pair, i) => (
            <Group key={i} gap="xs" align="flex-end">
              <TextInput
                label={i === 0 ? "Key" : undefined}
                placeholder="e.g. --lr"
                value={pair.key}
                style={{ flex: 1 }}
                onChange={(e) => {
                  const updated = [...(val("extra_params", []) as any[])];
                  updated[i] = { ...pair, key: e.currentTarget.value };
                  onChange({ ...config, extra_params: updated, extra_args: buildExtraArgs(updated) });
                }}
              />
              <TextInput
                label={i === 0 ? "Value" : undefined}
                placeholder="e.g. 0.001"
                value={pair.value}
                style={{ flex: 1 }}
                onChange={(e) => {
                  const updated = [...(val("extra_params", []) as any[])];
                  updated[i] = { ...pair, value: e.currentTarget.value };
                  onChange({ ...config, extra_params: updated, extra_args: buildExtraArgs(updated) });
                }}
              />
              <ActionIcon
                color="red"
                variant="light"
                mb={2}
                onClick={() => {
                  const updated = (val("extra_params", []) as any[]).filter((_, idx) => idx !== i);
                  onChange({ ...config, extra_params: updated, extra_args: buildExtraArgs(updated) });
                }}
              >
                <IconTrash size={14} />
              </ActionIcon>
            </Group>
          ))}

          <Button
            variant="light"
            size="xs"
            leftSection={<IconPlus size={14} />}
            onClick={() => {
              const updated = [...(val("extra_params", []) as any[]), { key: "", value: "" }];
              onChange({ ...config, extra_params: updated, extra_args: buildExtraArgs(updated) });
            }}
          >
            Add Parameter
          </Button>

        </Stack>
      </Tabs.Panel>

      {/* ── Tab 3: API keys ─────────────────────────────────── */}
      <Tabs.Panel value="secrets">
        <Stack gap="md">
          <Alert icon={<IconKey size={14} />} color="yellow" variant="light">
            <Text size="xs">
              API keys are passed securely as job parameters. For production use,
              store them in Tapis Vault and reference them by name.
            </Text>
          </Alert>

          <Box>
            <Text size="sm" fw={500} mb={4}>Weights & Biases</Text>
            <Text size="xs" c="dimmed" mb="xs">
              Enable live experiment tracking. Get your key at wandb.ai/settings.
            </Text>
            <PasswordInput
              label="W&B API Key"
              placeholder="wandb_v1_..."
              value={String(val("wandb_key", ""))}
              onChange={(e) => set("wandb_key", e.currentTarget.value)}
            />
            <TextInput
              label="W&B Project"
              placeholder="workflow-orchestrator"
              mt="xs"
              value={String(val("wandb_project", "workflow-orchestrator"))}
              onChange={(e) => set("wandb_project", e.currentTarget.value)}
            />
          </Box>

          <Divider />

          <Box>
            <Text size="sm" fw={500} mb={4}>HuggingFace</Text>
            <Text size="xs" c="dimmed" mb="xs">
              Required for gated models (e.g. Llama, Gemma). Get your token at huggingface.co/settings/tokens.
            </Text>
            <PasswordInput
              label="HuggingFace Token"
              placeholder="hf_..."
              value={String(val("hf_token", ""))}
              onChange={(e) => set("hf_token", e.currentTarget.value)}
            />
          </Box>
        </Stack>
      </Tabs.Panel>
    </Tabs>
  );
}