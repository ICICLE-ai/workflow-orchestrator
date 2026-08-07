import { useEffect, useState } from "react";
import {
  Stack, Group, Text, Title, NumberInput, TextInput, TagsInput, Select, Switch,
  SegmentedControl, ScrollArea, Divider, Badge,
} from "@mantine/core";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import ParamSection from "../components/ParamSection";
import TapisPathField from "../components/TapisPathField";

// Settings panel for the 'zero_shot_annotation' step (backend/steps/zero_shot_annotation/step.json)
// — annotates every image in a directory with a zero-shot open-vocabulary
// model (SAM3 by default), driven by either free-text concepts or visual
// exemplar boxes (or both).
//
// text_prompts is stored in config as a single CLI-ready string (each prompt
// individually double-quoted and space-joined, e.g. `"tree" "parked car"`) —
// see backend/engine/job_spec.py, which does plain ${...} substitution with
// no argv-list awareness, so the quoting has to already be baked in. The tag
// list below is just an editing convenience over that string.
//
// prompt_mode is UI-only (see step.json): it decides which prompt section is
// shown, but doesn't gate what's sent to the job — text_prompts and
// prompt_file are each included whenever set, so switching modes never
// silently discards the other one's value.
//
// Registered in registry.ts under the key "zero_shot_annotation".
export default function ZeroShotAnnotationPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const schema = step.config_schema || {};
  const tip = (key: string) => schema[key]?.description;
  const field = (key: string) => (config[key] !== undefined ? config[key] : schema[key]?.default);
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const imagesPort = step.inputs.find((p) => p.data_type === "image_dir")?.port_name;
  const wiredImages = String(connectedInputs[imagesPort || ""]?.config?.path ?? "");

  const promptMode = String(field("prompt_mode") ?? "text");
  const textPrompts = String(field("text_prompts") ?? "");
  const promptFile = String(field("prompt_file") ?? "");
  const isSahi = Boolean(field("is_sahi"));
  const hasExemplars = promptFile.trim().length > 0;

  const setTextPromptTags = (tags: string[]) => {
    const cleaned = tags.map((t) => t.trim()).filter(Boolean);
    set("text_prompts", cleaned.map((t) => (t.includes(" ") ? `"${t}"` : t)).join(" "));
  };

  // SAM3 (facebook/sam3) is a gated HuggingFace checkpoint — the job needs an
  // HF_TOKEN with access to it (see step.json's hf_token field). "secret"
  // fields hold a KEY into the team's stored secrets (added via the
  // dashboard), never the value itself; the real value is resolved
  // server-side at job-submission time (see backend/engine/secrets.py). This
  // mirrors GenericConfigForm's own "type": "secret" handling, reimplemented
  // here since this step uses a custom panel instead of the generic form.
  const [secretOptions, setSecretOptions] = useState<{ value: string; label: string }[]>([]);
  useEffect(() => {
    apiFetch("/api/secrets")
      .then((res) => res.json())
      .then((data) => {
        const secrets = (data.secrets || []) as { key: string }[];
        // Only the key is ever shown — never a secret's value (which the list
        // endpoint doesn't even return) and, per feedback, not even its
        // free-text description alongside it.
        setSecretOptions(secrets.map((s) => ({ value: s.key, label: s.key })));
      })
      .catch(() => {});
  }, []);
  const hfToken = String(field("hf_token") ?? "");

  return (
    <ScrollArea style={{ height: "100%" }}>
      <Stack gap="lg" p="lg" maw={900} mx="auto">
        <div>
          <Title order={3}>✨ Zero-Shot Annotation</Title>
          <Text size="sm" c="dimmed" mt={4}>
            Annotates every image in the wired directory with a zero-shot open-vocabulary model — no training,
            no class supports. Drive it with free-text concepts, visual exemplar boxes, or both.
          </Text>
        </div>

        <TextInput
          label="Images"
          value={wiredImages}
          placeholder="Connect an images (image directory) input"
          readOnly
          variant="filled"
        />

        <Divider />

        <ParamSection
          title="Prompts"
          explainer="Text prompts are free-text concepts applied to every image. Exemplar prompts are boxes drawn around one instance in a specific image — SAM3 finds the rest. Both can be supplied; at least one is required."
        >
          <Stack gap="sm">
            <SegmentedControl
              value={promptMode}
              onChange={(v) => set("prompt_mode", v)}
              data={[
                { label: "Text prompts", value: "text" },
                { label: "Exemplar prompts", value: "exemplar" },
              ]}
            />

            {promptMode === "text" ? (
              <TagsInput
                label="Text prompts"
                description={tip("text_prompts")}
                placeholder="Type a concept and press Enter (e.g. tree, parked car)"
                value={parsePromptTags(textPrompts)}
                onChange={setTextPromptTags}
              />
            ) : (
              <Stack gap="xs">
                <TapisPathField
                  label="Prompt file"
                  description={tip("prompt_file")}
                  system={String(config.system ?? "")}
                  path={promptFile}
                  selectType="file"
                  onSystemChange={(v) => set("system", v)}
                  onPathChange={(v) => set("prompt_file", v)}
                />
                <TagsInput
                  label="Additional text prompts (optional)"
                  description="Text concepts to run alongside the exemplars — not a substitute for text paired with an exemplar in the prompt file itself."
                  placeholder="Type a concept and press Enter"
                  value={parsePromptTags(textPrompts)}
                  onChange={setTextPromptTags}
                />
                {!hasExemplars && (
                  <Badge size="xs" variant="light" color="yellow">
                    No prompt file selected yet — exemplar detection needs one.
                  </Badge>
                )}
              </Stack>
            )}
          </Stack>
        </ParamSection>

        <ParamSection
          title="Model"
          explainer="Backend model from the zero-shot registry. SAM3 is the default and only registered backend today; model_id overrides its HuggingFace checkpoint."
        >
          <Group grow align="flex-start">
            <Select
              label="Model"
              description={tip("model")}
              data={schema.model?.options || ["sam3"]}
              value={String(field("model") ?? "sam3")}
              onChange={(v) => set("model", v ?? "sam3")}
              allowDeselect={false}
            />
            <TextInput
              label="Model ID override"
              description={tip("model_id")}
              placeholder="facebook/sam3"
              value={String(field("model_id") ?? "")}
              onChange={(e) => set("model_id", e.currentTarget.value)}
            />
          </Group>
          <Select
            mt="sm"
            label="HuggingFace token"
            description={tip("hf_token") || "facebook/sam3 is a gated checkpoint — pick the team secret holding an HF token with access to it."}
            placeholder="Select a secret"
            data={secretOptions}
            value={hfToken || null}
            onChange={(v) => set("hf_token", v ?? "")}
            searchable
            clearable
          />
          {!hfToken && (
            <Badge mt={4} size="xs" variant="light" color="yellow">
              No HF token selected — facebook/sam3 is gated and the job will fail to load it unless the
              checkpoint is already cached with HF_HUB_OFFLINE=1 on the exec system.
            </Badge>
          )}
        </ParamSection>

        <ParamSection
          title="SAHI tiling"
          explainer="Slices each image into overlapping tiles before inference — improves detection of small objects in large imagery. Tile count grows quickly: images x prompts x tiles inferences per run."
        >
          <Stack gap="sm">
            <Switch
              label="Enable SAHI"
              description={tip("is_sahi")}
              checked={isSahi}
              onChange={(e) => set("is_sahi", e.currentTarget.checked)}
            />
            {isSahi && (
              <Group grow>
                <NumberInput
                  label="Tile size (px)"
                  description={tip("tile_size")}
                  value={Number(field("tile_size") ?? 960)}
                  onChange={(v) => set("tile_size", v)}
                  min={32}
                  step={32}
                />
                <NumberInput
                  label="Overlap ratio"
                  description={tip("overlap_ratio")}
                  value={Number(field("overlap_ratio") ?? 0.2)}
                  onChange={(v) => set("overlap_ratio", v)}
                  min={0}
                  max={1}
                  step={0.05}
                  decimalScale={2}
                />
              </Group>
            )}
            {isSahi && hasExemplars && (
              <Group grow>
                <Select
                  label="Exemplar tile mode"
                  description={tip("exemplar_tile_mode")}
                  data={schema.exemplar_tile_mode?.options || ["intersecting", "all"]}
                  value={String(field("exemplar_tile_mode") ?? "intersecting")}
                  onChange={(v) => set("exemplar_tile_mode", v ?? "intersecting")}
                  allowDeselect={false}
                />
                <NumberInput
                  label="Exemplar min visibility"
                  description={tip("exemplar_min_visibility")}
                  value={Number(field("exemplar_min_visibility") ?? 0.5)}
                  onChange={(v) => set("exemplar_min_visibility", v)}
                  min={0}
                  max={1}
                  step={0.05}
                  decimalScale={2}
                />
              </Group>
            )}
          </Stack>
        </ParamSection>

        <ParamSection
          title="Batching & thresholds"
          explainer="Batch size counts tiles, not images. Confidence and mask threshold filter individual detections; NMS IoU merges detections across tiles and prompts."
        >
          <Stack gap="sm">
            <NumberInput
              label="Batch size"
              description={tip("batch_size")}
              value={Number(field("batch_size") ?? 8)}
              onChange={(v) => set("batch_size", v)}
              min={1}
              step={1}
            />
            <Group grow>
              <NumberInput
                label="Confidence"
                description={tip("confidence")}
                value={Number(field("confidence") ?? 0.3)}
                onChange={(v) => set("confidence", v)}
                min={0}
                max={1}
                step={0.05}
                decimalScale={2}
              />
              <NumberInput
                label="Mask threshold"
                description={tip("mask_threshold")}
                value={Number(field("mask_threshold") ?? 0.5)}
                onChange={(v) => set("mask_threshold", v)}
                min={0}
                max={1}
                step={0.05}
                decimalScale={2}
              />
              <NumberInput
                label="NMS IoU"
                description={tip("nms_iou")}
                value={Number(field("nms_iou") ?? 0.5)}
                onChange={(v) => set("nms_iou", v)}
                min={0}
                max={1}
                step={0.05}
                decimalScale={2}
              />
            </Group>
            <Group>
              <Switch
                label="Class-agnostic NMS"
                description={tip("class_agnostic_nms")}
                checked={Boolean(field("class_agnostic_nms"))}
                onChange={(e) => set("class_agnostic_nms", e.currentTarget.checked)}
              />
              <Switch
                label="Skip segmentation"
                description={tip("no_segmentation")}
                checked={Boolean(field("no_segmentation"))}
                onChange={(e) => set("no_segmentation", e.currentTarget.checked)}
              />
            </Group>
          </Stack>
        </ParamSection>

        <ParamSection
          title="Output"
          explainer="Flat matches the rest of this repo's annotation format; COCO writes standard images/annotations/categories JSON. max_images caps the run to a smoke test — leave at 0 for every image."
        >
          <Group grow>
            <Select
              label="Output format"
              description={tip("output_format")}
              data={schema.output_format?.options || ["flat", "coco", "both"]}
              value={String(field("output_format") ?? "flat")}
              onChange={(v) => set("output_format", v ?? "flat")}
              allowDeselect={false}
            />
            <NumberInput
              label="Max images"
              description={tip("max_images")}
              value={Number(field("max_images") ?? 0)}
              onChange={(v) => set("max_images", v)}
              min={0}
              step={1}
            />
          </Group>
        </ParamSection>
      </Stack>
    </ScrollArea>
  );
}

// Full-height scrollable layout — matches flightPlan/missionExport, which the
// StepSettingsModal host honors via this static flag.
(ZeroShotAnnotationPanel as any).fullScreen = true;

// Reverse of setTextPromptTags: split a CLI-ready string (individually
// double-quoted multi-word prompts, space-joined) back into an editable tag
// list for TagsInput's initial value.
function parsePromptTags(value: string): string[] {
  const tags: string[] = [];
  const re = /"([^"]*)"|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(value)) !== null) {
    const tag = (m[1] ?? m[2] ?? "").trim();
    if (tag) tags.push(tag);
  }
  return tags;
}
