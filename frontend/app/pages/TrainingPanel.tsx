import { useEffect, useState } from "react";
import {
  Stack,
  Group,
  Select,
  SegmentedControl,
  NumberInput,
  TextInput,
  Switch,
  Text,
  Badge,
  Paper,
  Tabs,
  Alert,
  ActionIcon,
  Button,
  Accordion,
  Code,
  SimpleGrid,
  List,
} from "@mantine/core";
import {
  IconBrain,
  IconKey,
  IconSettings,
  IconInfoCircle,
  IconPlus,
  IconTrash,
  IconDatabase,
  IconTerminal2,
  IconAlertTriangle,
} from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import ParamSection from "../components/ParamSection";

// Settings page for the training step. Every field here is a key in
// backend/steps/training/step.json's config_schema, which is the single source
// of defaults and help text; step.json's appArgs maps each key onto a
// train_main.py flag. This page only adds structure: which fields apply to the
// chosen framework, a model catalog, dataset expectations, and a preview of the
// command the job will run.

type Framework = "yolo" | "huggingface";
type Task = "classify" | "detect" | "segment";

interface CatalogModel {
  value: string;
  label: string;
  framework: Framework;
  task: Task;
  description: string;
}

const MODELS: CatalogModel[] = [
  // ── YOLO detection ──
  { value: "yolo11n.pt", label: "YOLO11 Nano", framework: "yolo", task: "detect", description: "Smallest and fastest YOLO11. Good for quick experiments and edge devices." },
  { value: "yolo11s.pt", label: "YOLO11 Small", framework: "yolo", task: "detect", description: "Noticeably more accurate than Nano, still fast." },
  { value: "yolo11m.pt", label: "YOLO11 Medium", framework: "yolo", task: "detect", description: "Balanced accuracy and speed; needs a mid-size GPU." },
  { value: "yolov8n.pt", label: "YOLOv8 Nano", framework: "yolo", task: "detect", description: "Widely used YOLOv8 baseline, fastest variant." },
  { value: "yolov8s.pt", label: "YOLOv8 Small", framework: "yolo", task: "detect", description: "YOLOv8 with better accuracy than Nano." },
  { value: "yolov8m.pt", label: "YOLOv8 Medium", framework: "yolo", task: "detect", description: "Larger YOLOv8 for higher accuracy." },
  // ── YOLO segmentation ──
  { value: "yolo11n-seg.pt", label: "YOLO11 Nano Seg", framework: "yolo", task: "segment", description: "Fast instance segmentation (boxes + masks)." },
  { value: "yolo11s-seg.pt", label: "YOLO11 Small Seg", framework: "yolo", task: "segment", description: "More accurate masks than Nano." },
  { value: "yolov8n-seg.pt", label: "YOLOv8 Nano Seg", framework: "yolo", task: "segment", description: "YOLOv8 instance segmentation, fastest variant." },
  { value: "yolov8s-seg.pt", label: "YOLOv8 Small Seg", framework: "yolo", task: "segment", description: "YOLOv8 instance segmentation with better accuracy." },
  // ── YOLO classification ──
  { value: "yolo11n-cls.pt", label: "YOLO11 Nano Cls", framework: "yolo", task: "classify", description: "Lightweight, fast image classifier." },
  { value: "yolov8n-cls.pt", label: "YOLOv8 Nano Cls", framework: "yolo", task: "classify", description: "YOLOv8 image classifier, fastest variant." },
  { value: "yolov8s-cls.pt", label: "YOLOv8 Small Cls", framework: "yolo", task: "classify", description: "YOLOv8 image classifier with better accuracy." },
  // ── HuggingFace classification ──
  { value: "google/vit-base-patch16-224", label: "ViT Base", framework: "huggingface", task: "classify", description: "Vision Transformer from Google. Strong general-purpose classifier." },
  { value: "microsoft/swin-tiny-patch4-window7-224", label: "Swin Tiny", framework: "huggingface", task: "classify", description: "Shifted-window transformer. Accurate and lighter than ViT Base." },
  { value: "facebook/deit-base-patch16-224", label: "DeiT Base", framework: "huggingface", task: "classify", description: "Data-efficient ViT from Meta. Trains well on smaller datasets." },
  { value: "microsoft/resnet-50", label: "ResNet-50", framework: "huggingface", task: "classify", description: "Classic CNN backbone. Fast and reliable." },
  // ── HuggingFace detection ──
  { value: "facebook/detr-resnet-50", label: "DETR ResNet-50", framework: "huggingface", task: "detect", description: "Detection Transformer from Meta. End-to-end detection without anchors." },
  { value: "facebook/detr-resnet-101", label: "DETR ResNet-101", framework: "huggingface", task: "detect", description: "Deeper DETR backbone; more accurate, slower." },
];

const CUSTOM = "__custom__";

// What the `model` output port points at inside <task>/<name>/ (step.json's
// model_artifact): YOLO's best weights file, or HuggingFace's saved model dir.
const MODEL_ARTIFACT: Record<Framework, string> = {
  yolo: "weights/best.pt",
  huggingface: "best_model",
};

const TASKS: { value: Task; label: string }[] = [
  { value: "classify", label: "Image classification" },
  { value: "detect", label: "Object detection" },
  { value: "segment", label: "Instance segmentation" },
];

// What train_main.py expects at --data for each framework/task (from the
// trainer README).
const DATASET_HELP: Record<Framework, Partial<Record<Task, { summary: string; layout?: string; notes?: string[] }>>> = {
  yolo: {
    detect: {
      summary: "An Ultralytics dataset .yaml (e.g. data.yaml) listing the train/val image folders and class names.",
      layout: "data.yaml\nimages/train/*.jpg   labels/train/*.txt\nimages/val/*.jpg     labels/val/*.txt",
    },
    segment: {
      summary: "An Ultralytics dataset .yaml, with polygon labels (one .txt per image).",
      layout: "data.yaml\nimages/train/*.jpg   labels/train/*.txt\nimages/val/*.jpg     labels/val/*.txt",
    },
    classify: {
      summary: "A folder with one subfolder per class (ImageFolder style).",
      layout: "train/<class_a>/*.jpg\ntrain/<class_b>/*.jpg\nval/<class_a>/*.jpg\nval/<class_b>/*.jpg",
    },
  },
  huggingface: {
    classify: {
      summary: "train/ and val/ folders, each with one subfolder per class.",
      layout: "train/<class_a>/*.jpg\ntrain/<class_b>/*.jpg\nval/<class_a>/*.jpg\nval/<class_b>/*.jpg",
    },
    detect: {
      summary: "COCO-format train/ and val/ folders, or a HuggingFace Hub dataset ID.",
      layout: "train/images/*.jpg   train/annotations.json\nval/images/*.jpg     val/annotations.json",
      notes: [
        "If the path doesn't exist, it's treated as a Hub dataset ID and a slice is downloaded (see Train / Val samples).",
        "Trains against the full 91-class COCO label space, whatever categories your annotations declare.",
      ],
    },
  },
};

const METRICS_HELP: Record<Framework, Partial<Record<Task, string>>> = {
  yolo: {
    detect: "mAP50 and mAP50-95 (box)",
    segment: "mAP50 and mAP50-95 for boxes (B) and masks (M)",
    classify: "top-1 and top-5 accuracy",
  },
  huggingface: {
    classify: "eval_accuracy, eval_loss; the best checkpoint is picked by accuracy",
    detect: "eval_loss; the best checkpoint is picked by eval_loss",
  },
};

const WEIGHTS_HELP: Record<Framework, string> = {
  yolo: "weights/best.pt and weights/last.pt, plus results.csv and training plots",
  huggingface: "best_model/ (model + image processor) and the most recent checkpoint-* folders",
};

// Flags per framework, in step.json appArgs order. Used only for the command
// preview; step.json's appArgs is what the job actually runs.
const COMMON_FLAGS: [string, string][] = [
  ["epochs", "epochs"], ["batch", "batch_size"], ["device", "device"], ["seed", "seed"], ["patience", "patience"],
];
const YOLO_FLAGS: [string, string][] = [
  ["workers", "workers"], ["imgsz", "imgsz"], ["lr0", "lr0"], ["lrf", "lrf"], ["momentum", "momentum"],
  ["weight_decay", "weight_decay"], ["optimizer", "optimizer"], ["warmup_epochs", "warmup_epochs"],
  ["warmup_momentum", "warmup_momentum"], ["warmup_bias_lr", "warmup_bias_lr"], ["dropout", "dropout"],
  ["fraction", "fraction"], ["close_mosaic", "close_mosaic"], ["save_period", "save_period"],
];
const YOLO_SWITCHES = ["cos_lr", "resume", "profile"];
const HF_FLAGS: [string, string][] = [
  ["lr", "learning_rate"], ["hf_weight_decay", "hf_weight_decay"], ["warmup_steps", "warmup_steps"],
  ["lr_scheduler", "lr_scheduler"], ["hf_optimizer", "hf_optimizer"],
  ["gradient_accumulation_steps", "gradient_accumulation_steps"], ["eval_strategy", "eval_strategy"],
  ["save_strategy", "save_strategy"], ["save_total_limit", "save_total_limit"], ["logging_steps", "logging_steps"],
  ["train_samples", "train_samples"], ["val_samples", "val_samples"],
];
const HF_SWITCHES = ["fp16"];

// "--lr" typed into the key box used to become "----lr" (run 91's extra_args).
const cleanFlag = (k: string) => k.trim().replace(/^-+/, "");

const buildExtraArgs = (params: { key: string; value: string }[]) =>
  params
    .filter((p) => cleanFlag(p.key))
    .map((p) => `--${cleanFlag(p.key)} ${p.value.trim()}`.trim())
    .join(" ");

const LABELS: Record<string, string> = {
  epochs: "Epochs",
  batch_size: "Batch size",
  seed: "Seed",
  patience: "Early-stopping patience",
  workers: "Dataloader workers",
  imgsz: "Image size (px)",
  lr0: "Initial learning rate (lr0)",
  lrf: "Final LR fraction (lrf)",
  momentum: "Momentum",
  weight_decay: "Weight decay",
  warmup_epochs: "Warmup epochs",
  warmup_momentum: "Warmup momentum",
  warmup_bias_lr: "Warmup bias LR",
  dropout: "Dropout",
  fraction: "Dataset fraction",
  freeze: "Freeze first N layers",
  close_mosaic: "Close mosaic (last N epochs)",
  save_period: "Save every N epochs",
  learning_rate: "Learning rate",
  hf_weight_decay: "Weight decay",
  warmup_steps: "Warmup steps",
  gradient_accumulation_steps: "Gradient accumulation steps",
  save_total_limit: "Checkpoints to keep",
  logging_steps: "Log every N steps",
  train_samples: "Train samples (Hub)",
  val_samples: "Val samples (Hub)",
};

export default function TrainingPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const schema = step.config_schema || {};
  const val = (key: string, fallback?: unknown) => {
    const v = config[key];
    return v !== undefined && v !== "" ? v : (schema[key]?.default ?? fallback);
  };
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });
  const unset = (key: string) => {
    const { [key]: _drop, ...rest } = config;
    onChange(rest);
  };
  // Help text comes from step.json; the framework prefix is dropped because
  // each field already sits in its framework's section.
  const help = (key: string) => (schema[key]?.description ?? "").replace(/^(YOLO|HuggingFace): /, "");

  const framework = (String(val("framework", "huggingface")) as Framework);
  const task = (String(val("task", "classify")) as Task);
  const model = String(val("model", ""));
  const catalog = MODELS.filter((m) => m.framework === framework && m.task === task);
  const modelInfo = catalog.find((m) => m.value === model);
  const [customModel, setCustomModel] = useState(!modelInfo && model !== "");
  const isYolo = framework === "yolo";
  const name = String(val("name", "train"));
  const dataFile = String(val("data_file", ""));
  const hubDataset = String(val("hub_dataset", ""));
  const datasetWired = Boolean(connectedInputs?.dataset);

  // A secret field stores the secret's KEY; the value is resolved server-side
  // at submission (see GenericConfigForm / backend/engine/secrets.py).
  const [secretOptions, setSecretOptions] = useState<{ value: string; label: string }[]>([]);
  useEffect(() => {
    apiFetch("/api/secrets")
      .then((res) => res.json())
      .then((data) => {
        const secrets = (data.secrets || []) as { key: string }[];
        setSecretOptions(secrets.map((s) => ({ value: s.key, label: s.key })));
      })
      .catch(() => {});
  }, []);

  // Nodes saved before model_artifact existed (or edited elsewhere) may not
  // match their framework; fix it on open so the model output is right.
  useEffect(() => {
    if (val("model_artifact") !== MODEL_ARTIFACT[framework]) set("model_artifact", MODEL_ARTIFACT[framework]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [framework]);

  const pickFramework = (fw: Framework, nextTask: Task = task) => {
    // HuggingFace has no segmentation backend; fall back to detection.
    const t: Task = fw === "huggingface" && nextTask === "segment" ? "detect" : nextTask;
    const first = MODELS.find((m) => m.framework === fw && m.task === t);
    setCustomModel(false);
    onChange({
      ...config,
      framework: fw,
      task: t,
      model: first?.value ?? "",
      model_artifact: MODEL_ARTIFACT[fw],
      // YOLO detect/segment read a dataset .yaml; everything else reads the folder.
      data_file: fw === "yolo" && t !== "classify" ? "data.yaml" : "",
      // A Hub dataset only makes sense for HuggingFace detection.
      hub_dataset: fw === "huggingface" && t === "detect" ? String(val("hub_dataset", "")) : "",
    });
  };

  const pickTask = (t: string | null) => {
    if (!t) return;
    // Segmentation is YOLO-only, so choosing it switches the framework.
    pickFramework(t === "segment" ? "yolo" : framework, t as Task);
  };

  const pickModel = (v: string | null) => {
    if (!v) return;
    if (v === CUSTOM) {
      setCustomModel(true);
      set("model", "");
      return;
    }
    setCustomModel(false);
    set("model", v);
  };

  // ── Field helpers ────────────────────────────────────────────────
  const num = (key: string, opts: { min?: number; max?: number; step?: number; decimalScale?: number } = {}) => (
    <NumberInput
      key={key}
      label={LABELS[key] ?? key}
      description={help(key)}
      value={Number(val(key))}
      // A cleared box falls back to the step.json default rather than sending
      // an empty flag value (`--epochs ` would fail argparse).
      onChange={(v) => {
        if (v === "" || v === undefined) {
          unset(key);
        } else {
          set(key, v);
        }
      }}
      min={opts.min}
      max={opts.max}
      step={opts.step}
      decimalScale={opts.decimalScale}
      allowDecimal={opts.decimalScale !== 0}
    />
  );

  const choice = (key: string, label: string) => (
    <Select
      key={key}
      label={label}
      description={help(key)}
      data={(schema[key]?.options ?? []).map(String)}
      value={String(val(key))}
      onChange={(v) => v && set(key, v)}
      allowDeselect={false}
    />
  );

  const toggle = (key: string, label: string) => (
    <Switch
      key={key}
      label={label}
      description={help(key)}
      checked={Boolean(val(key, false))}
      onChange={(e) => set(key, e.currentTarget.checked)}
    />
  );

  const extraParams = (val("extra_params", []) as { key: string; value: string }[]);
  const setExtraParams = (updated: { key: string; value: string }[]) =>
    onChange({ ...config, extra_params: updated, extra_args: buildExtraArgs(updated) });

  // ── Command preview ──────────────────────────────────────────────
  const command = (() => {
    const parts: string[] = [
      `--framework ${framework}`,
      `--model ${model || "<model>"}`,
      `--task ${task}`,
      hubDataset && !datasetWired ? `--data ${hubDataset}` : `--data /job/dataset/${dataFile}`,
      "--output_path /job/output",
      `--name ${name}`,
    ];
    const flags = [...COMMON_FLAGS, ...(isYolo ? YOLO_FLAGS : HF_FLAGS)];
    for (const [flag, key] of flags) parts.push(`--${flag} ${val(key)}`);
    if (isYolo && Number(val("freeze", 0)) > 0) parts.push(`--freeze ${val("freeze")}`);
    for (const key of isYolo ? YOLO_SWITCHES : HF_SWITCHES) if (val(key, false)) parts.push(`--${key}`);
    const extra = String(val("extra_args", ""));
    if (extra) parts.push(extra);
    return "python train_main.py \\\n  " + parts.join(" \\\n  ");
  })();

  const dataset = DATASET_HELP[framework][task];

  return (
    <Tabs defaultValue="model">
      <Tabs.List mb="md">
        <Tabs.Tab value="model" leftSection={<IconBrain size={14} />}>Model &amp; data</Tabs.Tab>
        <Tabs.Tab value="train" leftSection={<IconSettings size={14} />}>Hyperparameters</Tabs.Tab>
        <Tabs.Tab value="secrets" leftSection={<IconKey size={14} />}>Tracking &amp; keys</Tabs.Tab>
        <Tabs.Tab value="command" leftSection={<IconTerminal2 size={14} />}>Command</Tabs.Tab>
      </Tabs.List>

      {/* ── Tab 1: Model & data ─────────────────────────────────── */}
      <Tabs.Panel value="model">
        <Stack gap="md">
          <Select
            label="Task"
            description="What the model should predict. Segmentation is only available with YOLO."
            data={TASKS}
            value={task}
            onChange={pickTask}
            allowDeselect={false}
          />

          <Stack gap={4}>
            <Text size="sm" fw={500}>Framework</Text>
            <Text size="xs" c="dimmed">
              YOLO: fast single-stage models with strong augmentation built in; best for detection and
              segmentation. HuggingFace: Transformer and CNN models from the Hub; best for classification.
            </Text>
            <SegmentedControl
              value={framework}
              onChange={(v) => pickFramework(v as Framework)}
              data={[
                { value: "yolo", label: "YOLO (Ultralytics)" },
                { value: "huggingface", label: "HuggingFace", disabled: task === "segment" },
              ]}
            />
          </Stack>

          <Select
            label="Model"
            description={
              isYolo
                ? "Pretrained checkpoints are downloaded automatically. Choose 'Other' for a different YOLO v8–v11 checkpoint or your own best.pt."
                : "Any Hub model compatible with the task's Auto class works. Choose 'Other' to type a model ID or a local directory."
            }
            data={[
              ...catalog.map((m) => ({ value: m.value, label: `${m.label} — ${m.value}` })),
              { value: CUSTOM, label: isYolo ? "Other checkpoint / my own weights…" : "Other Hub model / local directory…" },
            ]}
            value={customModel || !modelInfo ? CUSTOM : model}
            onChange={pickModel}
            allowDeselect={false}
          />

          {(customModel || !modelInfo) && (
            <TextInput
              label={isYolo ? "Checkpoint name or path" : "Model ID or path"}
              description={
                isYolo
                  ? `e.g. ${task === "segment" ? "yolo11m-seg.pt" : task === "classify" ? "yolo11s-cls.pt" : "yolov9c.pt"}, or a path to a previous run's weights/best.pt to fine-tune it further.`
                  : task === "detect"
                    ? "Must load with AutoModelForObjectDetection, e.g. facebook/detr-resnet-50."
                    : "Must load with AutoModelForImageClassification, e.g. google/efficientnet-b0."
              }
              placeholder={isYolo ? "yolo11m.pt or /path/to/weights/best.pt" : "org/model-name"}
              value={model}
              onChange={(e) => set("model", e.currentTarget.value.trim())}
              error={model === "" ? "Enter a model" : undefined}
            />
          )}

          {modelInfo && (
            <Paper withBorder p="sm" radius="md">
              <Group gap="xs" mb={4}>
                <Text size="sm" fw={600}>{modelInfo.label}</Text>
                <Badge size="xs" variant="light">{isYolo ? "YOLO" : "HuggingFace"}</Badge>
                <Badge size="xs" variant="light" color="teal">{task}</Badge>
              </Group>
              <Text size="xs" c="dimmed">{modelInfo.description}</Text>
              <Code mt={6}>{modelInfo.value}</Code>
            </Paper>
          )}

          {dataset && (
            <Alert icon={<IconDatabase size={16} />} variant="light" title="Dataset the trainer expects">
              <Stack gap={6}>
                <Text size="xs">
                  Wire a dataset folder into the <b>dataset</b> input. The whole folder is copied into the job
                  and passed as <Code>--data</Code>. {dataset.summary}
                </Text>
                {dataset.layout && <Code block>{dataset.layout}</Code>}
                {dataset.notes?.map((n) => <Text key={n} size="xs">{n}</Text>)}
              </Stack>
            </Alert>
          )}

          {!datasetWired && !(framework === "huggingface" && task === "detect" && hubDataset) && (
            <Alert icon={<IconAlertTriangle size={16} />} color="orange" variant="light">
              <Text size="xs">
                No dataset is wired into this step yet. Connect a dataset folder to its <b>dataset</b> input
                {framework === "huggingface" && task === "detect" ? ", or enter a Hub dataset ID below" : ""}.
              </Text>
            </Alert>
          )}

          {isYolo && task !== "classify" && (
            <TextInput
              label="Dataset .yaml"
              description={help("data_file")}
              placeholder="data.yaml"
              value={dataFile}
              onChange={(e) => set("data_file", e.currentTarget.value.trim())}
              error={dataFile === "" ? "YOLO detection and segmentation need the dataset's .yaml file" : undefined}
            />
          )}

          {framework === "huggingface" && task === "detect" && (
            <TextInput
              label="Hub dataset ID (optional)"
              description={help("hub_dataset")}
              placeholder="detection-datasets/coco"
              value={hubDataset}
              onChange={(e) => set("hub_dataset", e.currentTarget.value.trim())}
            />
          )}

          <Alert icon={<IconInfoCircle size={16} />} color="gray" variant="light" title="What the run produces">
            <List size="xs" spacing={2}>
              <List.Item>
                Saved in the run's archive folder under <Code>{task}/{name}/</Code>; the <b>metrics</b> output
                points at its <Code>metrics.json</Code>.
              </List.Item>
              <List.Item>
                Weights: {WEIGHTS_HELP[framework]}. The <b>model</b> output points at{" "}
                <Code>{MODEL_ARTIFACT[framework]}</Code>.
              </List.Item>
              <List.Item><Code>metrics.json</Code> with {METRICS_HELP[framework][task]}</List.Item>
            </List>
          </Alert>

          <TextInput
            label="Experiment name"
            description={help("name")}
            placeholder="train"
            value={String(config.name ?? "")}
            // Empty falls back to the default ("train"): the output ports
            // point at <task>/<name>/, so the name can't be left blank.
            onChange={(e) => {
              const v = e.currentTarget.value.trim().replace(/[\/\s]+/g, "_");
              if (v) set("name", v);
              else unset("name");
            }}
          />
        </Stack>
      </Tabs.Panel>

      {/* ── Tab 2: Hyperparameters ──────────────────────────────── */}
      <Tabs.Panel value="train">
        <Stack gap="md">
          <ParamSection
            title="Training run"
            explainer="Length of training and how much data goes through the GPU at once. These apply to both frameworks."
          >
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              {num("epochs", { min: 1, max: 1000, decimalScale: 0 })}
              {num("batch_size", { min: 1, max: 1024, decimalScale: 0 })}
              {num("patience", { min: 0, max: 1000, decimalScale: 0 })}
              {num("seed", { min: 0, decimalScale: 0 })}
            </SimpleGrid>
            {choice("device", "Device")}
          </ParamSection>

          {isYolo ? (
            <>
              <ParamSection
                title="YOLO: learning rate & optimizer"
                explainer="The defaults are Ultralytics' tuned values and work well for most datasets. Lower lr0 when fine-tuning from your own weights."
              >
                <SimpleGrid cols={{ base: 1, sm: 2 }}>
                  {num("imgsz", { min: 32, max: 4096, step: 32, decimalScale: 0 })}
                  {choice("optimizer", "Optimizer")}
                  {num("lr0", { min: 0, step: 0.001, decimalScale: 6 })}
                  {num("lrf", { min: 0, max: 1, step: 0.01, decimalScale: 4 })}
                  {num("momentum", { min: 0, max: 1, step: 0.001, decimalScale: 4 })}
                  {num("weight_decay", { min: 0, step: 0.0001, decimalScale: 6 })}
                </SimpleGrid>
                {toggle("cos_lr", "Cosine LR schedule")}
              </ParamSection>

              <Accordion variant="separated">
                <Accordion.Item value="yolo-advanced">
                  <Accordion.Control>Advanced YOLO options: warmup, augmentation, data, checkpoints</Accordion.Control>
                  <Accordion.Panel>
                    <Stack gap="md">
                      <SimpleGrid cols={{ base: 1, sm: 2 }}>
                        {num("warmup_epochs", { min: 0, step: 0.5, decimalScale: 2 })}
                        {num("warmup_momentum", { min: 0, max: 1, step: 0.05, decimalScale: 3 })}
                        {num("warmup_bias_lr", { min: 0, step: 0.01, decimalScale: 4 })}
                        {num("close_mosaic", { min: 0, decimalScale: 0 })}
                        {num("fraction", { min: 0.01, max: 1, step: 0.05, decimalScale: 2 })}
                        {num("freeze", { min: 0, decimalScale: 0 })}
                        {task === "classify" && num("dropout", { min: 0, max: 1, step: 0.05, decimalScale: 2 })}
                        {num("save_period", { min: -1, decimalScale: 0 })}
                        {num("workers", { min: 0, max: 64, decimalScale: 0 })}
                      </SimpleGrid>
                      {toggle("resume", "Resume an interrupted run")}
                      {toggle("profile", "Profile export speeds")}
                      <Text size="xs" c="dimmed">
                        Mixed precision (AMP) is always on for YOLO; the trainer has no switch to turn it off.
                      </Text>
                    </Stack>
                  </Accordion.Panel>
                </Accordion.Item>
              </Accordion>
            </>
          ) : (
            <>
              <ParamSection
                title="HuggingFace: learning rate & optimizer"
                explainer="Fine-tuning a pretrained Transformer usually needs a small learning rate (1e-5 to 5e-5). Raise it for CNNs such as ResNet."
              >
                <SimpleGrid cols={{ base: 1, sm: 2 }}>
                  {num("learning_rate", { min: 0, step: 0.00001, decimalScale: 8 })}
                  {num("hf_weight_decay", { min: 0, step: 0.001, decimalScale: 6 })}
                  {num("warmup_steps", { min: 0, decimalScale: 0 })}
                  {choice("lr_scheduler", "LR scheduler")}
                  {num("gradient_accumulation_steps", { min: 1, decimalScale: 0 })}
                  <TextInput
                    label="Optimizer"
                    description={help("hf_optimizer")}
                    value={String(val("hf_optimizer"))}
                    onChange={(e) => set("hf_optimizer", e.currentTarget.value.trim())}
                  />
                </SimpleGrid>
                {toggle("fp16", "Mixed precision (fp16)")}
              </ParamSection>

              <Accordion variant="separated">
                <Accordion.Item value="hf-advanced">
                  <Accordion.Control>Advanced HuggingFace options: evaluation, checkpoints, Hub datasets</Accordion.Control>
                  <Accordion.Panel>
                    <Stack gap="md">
                      <SimpleGrid cols={{ base: 1, sm: 2 }}>
                        {choice("eval_strategy", "Evaluate")}
                        {choice("save_strategy", "Save checkpoints")}
                        {num("save_total_limit", { min: 1, decimalScale: 0 })}
                        {num("logging_steps", { min: 1, decimalScale: 0 })}
                        {task === "detect" && num("train_samples", { min: 1, decimalScale: 0 })}
                        {task === "detect" && num("val_samples", { min: 1, decimalScale: 0 })}
                      </SimpleGrid>
                      <Text size="xs" c="dimmed">
                        The best checkpoint is always reloaded at the end
                        ({task === "classify" ? "highest accuracy" : "lowest eval_loss"}), so keep Evaluate and
                        Save checkpoints on the same schedule.
                      </Text>
                    </Stack>
                  </Accordion.Panel>
                </Accordion.Item>
              </Accordion>
            </>
          )}

          <ParamSection
            title="Additional flags"
            explainer="Anything else train_main.py accepts, appended to the command as-is. Enter the flag name without dashes."
          >
            {extraParams.map((pair, i) => (
              <Group key={i} gap="xs" align="flex-end" wrap="nowrap">
                <TextInput
                  label={i === 0 ? "Flag" : undefined}
                  placeholder="e.g. nproc_per_node"
                  value={pair.key}
                  style={{ flex: 1 }}
                  onChange={(e) => {
                    const updated = [...extraParams];
                    updated[i] = { ...pair, key: e.currentTarget.value };
                    setExtraParams(updated);
                  }}
                />
                <TextInput
                  label={i === 0 ? "Value" : undefined}
                  placeholder="e.g. 2 (empty for a switch)"
                  value={pair.value}
                  style={{ flex: 1 }}
                  onChange={(e) => {
                    const updated = [...extraParams];
                    updated[i] = { ...pair, value: e.currentTarget.value };
                    setExtraParams(updated);
                  }}
                />
                <ActionIcon
                  color="red"
                  variant="light"
                  mb={4}
                  aria-label="Remove flag"
                  onClick={() => setExtraParams(extraParams.filter((_, idx) => idx !== i))}
                >
                  <IconTrash size={14} />
                </ActionIcon>
              </Group>
            ))}
            <Button
              variant="light"
              size="xs"
              leftSection={<IconPlus size={14} />}
              onClick={() => setExtraParams([...extraParams, { key: "", value: "" }])}
              style={{ alignSelf: "flex-start" }}
            >
              Add flag
            </Button>
            <Text size="xs" c="dimmed">
              An unknown flag stops the job before training starts. See the Command tab for what's
              already passed.
            </Text>
          </ParamSection>
        </Stack>
      </Tabs.Panel>

      {/* ── Tab 3: Tracking & keys ──────────────────────────────── */}
      <Tabs.Panel value="secrets">
        <Stack gap="md">
          <Alert icon={<IconKey size={16} />} color="yellow" variant="light">
            <Text size="xs">
              Keys are stored as team secrets (add them from the dashboard's settings icon) and are only
              resolved when the job is submitted. This page stores the secret's name, never its value.
            </Text>
          </Alert>

          <ParamSection
            title="Weights & Biases"
            explainer="Live loss and metric charts while the job runs. Sets WANDB_API_KEY and WANDB_PROJECT in the job; leave the key empty to skip W&B."
          >
            <Select
              label="W&B API key"
              description="Team secret holding your key from wandb.ai/authorize."
              placeholder={secretOptions.length ? "Select a secret" : "No team secrets yet"}
              data={secretOptions}
              value={String(val("wandb_key", "")) || null}
              onChange={(v) => set("wandb_key", v ?? "")}
              searchable
              clearable
            />
            <TextInput
              label="W&B project"
              description={help("wandb_project")}
              value={String(val("wandb_project", ""))}
              onChange={(e) => set("wandb_project", e.currentTarget.value)}
              disabled={!val("wandb_key", "")}
            />
          </ParamSection>

          <ParamSection
            title="HuggingFace"
            explainer="Only needed for gated or private Hub models and datasets. Public models download without a token."
          >
            <Select
              label="HuggingFace token"
              description="Team secret holding a token from huggingface.co/settings/tokens. Passed to the job as HF_TOKEN."
              placeholder={secretOptions.length ? "Select a secret" : "No team secrets yet"}
              data={secretOptions}
              value={String(val("hf_token", "")) || null}
              onChange={(v) => set("hf_token", v ?? "")}
              searchable
              clearable
            />
            <TextInput
              label="Model cache folder"
              description={help("hf_cache_dir")}
              placeholder="Inside the job folder"
              value={String(val("hf_cache_dir", ""))}
              onChange={(e) => set("hf_cache_dir", e.currentTarget.value.trim())}
            />
          </ParamSection>
        </Stack>
      </Tabs.Panel>

      {/* ── Tab 4: Command preview ──────────────────────────────── */}
      <Tabs.Panel value="command">
        <Stack gap="sm">
          <Text size="xs" c="dimmed">
            The trainer command this configuration produces. Inside the job, <Code>/job/dataset</Code> is
            the copied dataset folder and <Code>/job/output</Code> is archived to the run's folder when the
            job ends.
          </Text>
          <Code block>{command}</Code>
          <Alert icon={<IconAlertTriangle size={16} />} color="gray" variant="light">
            <Text size="xs">
              Settings for the {isYolo ? "HuggingFace" : "YOLO"} framework are also sent with their current
              values; the trainer ignores them for a {isYolo ? "YOLO" : "HuggingFace"} run.
            </Text>
          </Alert>
        </Stack>
      </Tabs.Panel>
    </Tabs>
  );
}
