import {
  Stack,
  Group,
  Select,
  SegmentedControl,
  NumberInput,
  Slider,
  Text,
  Alert,
  Button,
  Code,
  SimpleGrid,
} from "@mantine/core";
import { IconInfoCircle, IconLink, IconAlertTriangle, IconFileCode } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import ParamSection from "../components/ParamSection";

// Settings page for the hf_inference step (jobs/hf_inference/infer_main.py).
// Field keys, defaults and help text come from
// backend/steps/hf_inference/step.json; this page shows only the fields that
// apply to the chosen framework and checks them against a wired Model
// Training node, since a framework/task mismatch only fails once the job runs.

type Framework = "yolo" | "huggingface";
type Task = "classify" | "detect" | "segment";

const TASKS: { value: Task; label: string }[] = [
  { value: "classify", label: "Image classification" },
  { value: "detect", label: "Object detection" },
  { value: "segment", label: "Segmentation" },
];

const MODEL_INPUT: Record<Framework, string> = {
  yolo: "a YOLO .pt file, e.g. Model Training's model output (weights/best.pt)",
  huggingface: "a HuggingFace model folder (config + weights + image processor), e.g. Model Training's best_model/",
};

// One entry of predictions.json, per task, so users know what downstream
// steps receive.
const SAMPLE: Record<Task, string> = {
  classify: `{ "image": "img_001.jpg",
  "predictions": [
    { "class_id": 2, "class_name": "healthy", "confidence": 0.94 } ] }`,
  detect: `{ "image": "img_001.jpg",
  "predictions": [
    { "class_id": 0, "class_name": "weed", "confidence": 0.81,
      "bbox": [x1, y1, x2, y2] } ] }`,
  segment: `{ "image": "img_001.jpg",
  "predictions": [
    { "class_id": 0, "class_name": "weed", "confidence": 0.77 } ] }`,
};

const SEGMENT_NOTE: Record<Framework, string> = {
  yolo: "Masks themselves aren't written to predictions.json; each entry lists a detected instance's class and confidence.",
  huggingface: "Semantic segmentation (e.g. SegFormer) lists the classes present in each image. A SAM model ('sam' in its name) is prompted with the image centre and reports how many masks it found.",
};

export default function InferencePanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const schema = step.config_schema || {};
  const val = (key: string) => (config[key] !== undefined && config[key] !== "" ? config[key] : schema[key]?.default);
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });
  const help = (key: string) => (schema[key]?.description ?? "").replace(/^(YOLO|HuggingFace) only: /, "");

  const framework = String(val("framework")) as Framework;
  const task = String(val("task")) as Task;
  const isYolo = framework === "yolo";

  // A wired Model Training node tells us what the model actually is.
  const modelSource = connectedInputs?.model;
  const trained = modelSource?.sourceType === "training" ? modelSource.config : undefined;
  const trainedFramework = trained?.framework as Framework | undefined;
  const trainedTask = trained?.task as Task | undefined;
  const trainedImgsz = trained?.imgsz as number | undefined;
  const mismatch =
    trained && ((trainedFramework && trainedFramework !== framework) || (trainedTask && trainedTask !== task));

  const matchTraining = () =>
    onChange({
      ...config,
      ...(trainedFramework ? { framework: trainedFramework } : {}),
      ...(trainedTask ? { task: trainedTask } : {}),
      ...(trainedFramework === "yolo" && trainedImgsz ? { imgsz: trainedImgsz } : {}),
    });

  const num = (key: string, label: string, opts: { min?: number; max?: number; step?: number }) => (
    <NumberInput
      label={label}
      description={help(key)}
      value={Number(val(key))}
      min={opts.min}
      max={opts.max}
      step={opts.step}
      allowDecimal={false}
      onChange={(v) => {
        if (v === "" || v === undefined) {
          const { [key]: _drop, ...rest } = config;
          onChange(rest);
        } else {
          set(key, v);
        }
      }}
    />
  );

  return (
    <Stack gap="md">
      {mismatch && (
        <Alert icon={<IconAlertTriangle size={16} />} color="orange" variant="light" title="Doesn't match the training step">
          <Stack gap="xs">
            <Text size="xs">
              The connected Model Training step trains a <b>{trainedFramework}</b> <b>{trainedTask}</b> model, but this
              step is set to <b>{framework}</b> <b>{task}</b>. The job would fail to load the model.
            </Text>
            <Button size="xs" variant="light" color="orange" onClick={matchTraining} style={{ alignSelf: "flex-start" }}>
              Match training settings
            </Button>
          </Stack>
        </Alert>
      )}
      {trained && !mismatch && (
        <Alert icon={<IconLink size={16} />} color="teal" variant="light">
          <Text size="xs">
            Matches the connected Model Training step ({trainedFramework} {trainedTask}).
          </Text>
        </Alert>
      )}

      <ParamSection
        title="Model"
        explainer="Tell the job what kind of model is wired into the model input, so it loads it the right way."
      >
        <Stack gap={4}>
          <Text size="sm" fw={500}>Framework</Text>
          <Text size="xs" c="dimmed">{help("framework")}</Text>
          <SegmentedControl
            value={framework}
            onChange={(v) => set("framework", v)}
            data={[
              { value: "yolo", label: "YOLO (Ultralytics)" },
              { value: "huggingface", label: "HuggingFace" },
            ]}
          />
        </Stack>
        <Select
          label="Task"
          description={help("task")}
          data={TASKS}
          value={task}
          onChange={(v) => v && set("task", v)}
          allowDeselect={false}
        />
        <Text size="xs" c="dimmed">
          The <b>model</b> input should be {MODEL_INPUT[framework]}.
        </Text>
      </ParamSection>

      <ParamSection
        title="Predictions"
        explainer="Which predictions are kept, and how the images are processed."
      >
        <Stack gap={4}>
          <Group justify="space-between">
            <Text size="sm" fw={500}>Confidence threshold</Text>
            <Code>{Number(val("conf")).toFixed(2)}</Code>
          </Group>
          <Text size="xs" c="dimmed">{help("conf")}</Text>
          <Slider
            min={0}
            max={1}
            step={0.05}
            value={Number(val("conf"))}
            onChange={(v) => set("conf", Math.round(v * 100) / 100)}
            marks={[{ value: 0.25, label: "0.25" }, { value: 0.5, label: "0.5" }, { value: 0.75, label: "0.75" }]}
            mb="md"
          />
        </Stack>
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          {isYolo
            ? num("imgsz", "Image size (px)", { min: 32, max: 4096, step: 32 })
            : num("batch", "Batch size", { min: 1, max: 512 })}
          <Select
            label="Device"
            description={help("device")}
            data={(schema.device?.options ?? ["cuda", "cpu"]).map(String)}
            value={String(val("device"))}
            onChange={(v) => v && set("device", v)}
            allowDeselect={false}
          />
        </SimpleGrid>
        {isYolo && trainedImgsz && Number(val("imgsz")) !== Number(trainedImgsz) && (
          <Text size="xs" c="orange">
            The model was trained at {trainedImgsz}px; using the same size usually gives the best results.
          </Text>
        )}
      </ParamSection>

      <Alert icon={<IconFileCode size={16} />} color="gray" variant="light" title="What the run produces">
        <Stack gap={6}>
          <Text size="xs">
            <Code>predictions.json</Code> (the <b>predictions</b> output): one entry per image, for example:
          </Text>
          <Code block>{SAMPLE[task]}</Code>
          {task === "segment" && <Text size="xs">{SEGMENT_NOTE[framework]}</Text>}
          {task === "classify" && !isYolo && (
            <Text size="xs">HuggingFace classification lists up to the top 5 classes above the threshold.</Text>
          )}
          <Text size="xs">
            <Code>annotations.json</Code> (the <b>annotations</b> output): the same predictions in the Smart
            Labeler's format. Wire it to a Smart Labeler's <b>resume_annotations</b> input, with this images folder
            on its <b>images</b> input, to see and correct them.
            {task === "classify" && " Each image's class shows as a box around the whole image."}
            {task === "segment" &&
              (isYolo ? " Masks show as outlines." : " HuggingFace segmentation has no shapes to show there.")}
          </Text>
          <Text size="xs">
            <Code>summary.json</Code> (the <b>summary</b> output): framework, task, threshold, image count and
            total predictions.
          </Text>
        </Stack>
      </Alert>

      <Alert icon={<IconInfoCircle size={16} />} color="gray" variant="light">
        <Text size="xs">
          The model and the images folder are copied into the job before it starts, so large image folders add
          some transfer time.
        </Text>
      </Alert>
    </Stack>
  );
}
