import { useEffect, useRef, useState } from "react";
import {
  Stack, Group, Text, Title, NumberInput, TextInput, Select, Switch,
  SegmentedControl, ScrollArea, Divider, Badge, Button, Tooltip, ActionIcon, Collapse,
} from "@mantine/core";
import { IconArrowRight, IconLink, IconPlus, IconTrash } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import { resolveWiredLocation, splitTapisUri } from "../lib/tapis";
import ParamSection from "../components/ParamSection";
import TapisPathField from "../components/TapisPathField";
import { useDefaultSystem } from "../lib/runDefaults";

// Settings panel for the 'zero_shot_annotation' step (backend/steps/zero_shot_annotation/step.json)
// — annotates every image in a directory with a zero-shot open-vocabulary
// model (SAM3 by default), driven by either free-text concepts or visual
// exemplar boxes (or both). With model 'insid3', the same prompt file's
// exemplars are references instead: INSID3 segments their classes in every
// image, not just the one they were drawn on.
//
// prompt_mode 'reference' and model 'insid3' go together — picking either one
// sets the other (see setPromptMode / setModel), since INSID3 takes no text
// and SAM3 has no cross-image references.
//
// text_prompts is stored in config as a single CLI-ready string (each prompt
// individually double-quoted and space-joined, e.g. `tree "parked car"`, and
// `"dog=german shepherd"` for a prompt with its own label) —
// see backend/engine/job_spec.py, which does plain ${...} substitution with
// no argv-list awareness, so the quoting has to already be baked in. The tag
// list below is just an editing convenience over that string.
//
// prompt_mode is UI-only (see step.json): it decides which prompt section is
// shown, but doesn't gate what's sent to the job — text_prompts and
// annotation_file are each included whenever set, so switching modes never
// silently discards the other one's value.
//
// Registered in registry.ts under the key "zero_shot_annotation".
export default function ZeroShotAnnotationPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const defaultSystem = useDefaultSystem();
  const schema = step.config_schema || {};
  const tip = (key: string) => schema[key]?.description;
  const field = (key: string) => (config[key] !== undefined ? config[key] : schema[key]?.default);
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const imagesPort = step.inputs.find((p) => p.data_type === "image_dir")?.port_name;
  const wiredImages = String(connectedInputs[imagesPort || ""]?.config?.path ?? "");

  const promptMode = String(field("prompt_mode") ?? "text");
  const textPrompts = String(field("text_prompts") ?? "");
  const isSahi = Boolean(field("is_sahi"));
  const model = String(field("model") ?? "sam3");
  const isInsid3 = model === "insid3";

  const setPromptMode = (mode: string) =>
    onChange({
      ...config,
      prompt_mode: mode,
      model: mode === "reference" ? "insid3" : isInsid3 ? "sam3" : model,
    });
  const setModel = (next: string) =>
    onChange({
      ...config,
      model: next,
      prompt_mode: next === "insid3" ? "reference" : promptMode === "reference" ? "exemplar" : promptMode,
    });

  // Same full-URI storage as annotation_file below, so both file fields
  // round-trip through TapisPathField the same way.
  const weightsUri = String(field("insid3_weights") ?? "");
  const weightsParts = splitTapisUri(weightsUri);
  const weightsSystem = weightsParts?.system || defaultSystem;
  const weightsPath = weightsParts?.path ?? weightsUri;
  const setWeights = (nextSystem: string, nextPath: string) =>
    set(
      "insid3_weights",
      nextSystem && nextPath ? `tapis://${nextSystem}/${nextPath.replace(/^\/+/, "")}` : nextPath,
    );
  const boxMasks = String(field("reference_box_masks") ?? "sam3");
  const refiner = String(field("insid3_mask_refiner") ?? "bilinear");
  const [showAdvanced, setShowAdvanced] = useState(false);
  // Recommended INSID3 setup for small, low-contrast targets (leaf lesions):
  // tau / merge threshold are left alone — they need tuning on labelled data.
  const applySmallTargetPreset = () =>
    onChange({
      ...config,
      model_id: "",
      insid3_image_size: 1024,
      is_sahi: true,
      tile_size: 512,
      overlap_ratio: 0.25,
      insid3_mask_refiner: "crf",
      insid3_crf_size: 1024,
    });

  // annotation_file is stored as a FULL tapis://system/path URI, not a bare
  // path alongside a separate `system` field. It has to be: the key doubles as
  // an input port name, so once that port is wired the backend's
  // _resolve_inputs overwrites this config value with the upstream output's
  // URI at run time (see step.json). A bare path here would then have meant
  // step.json's fileInput needed a "tapis://${system}" prefix that the wired
  // URI doesn't want — doubling the scheme. Split it back apart only for
  // TapisPathField's two-widget display, falling back to the legacy
  // bare-path + config.system shape so nodes saved before this still open.
  const promptFileUri = String(field("annotation_file") ?? "");
  const promptFileParts = splitTapisUri(promptFileUri);
  const promptFileSystem = promptFileParts?.system ?? (String(config.system ?? "") || defaultSystem);
  const promptFile = promptFileParts?.path ?? promptFileUri;
  const hasExemplars = promptFileUri.trim().length > 0;

  const setPromptFile = (nextSystem: string, nextPath: string) =>
    onChange({
      ...config,
      // `system` is kept in sync purely so a node saved by this panel still
      // reads correctly if anything falls back to the legacy shape above.
      system: nextSystem,
      annotation_file:
        nextSystem && nextPath ? `tapis://${nextSystem}/${nextPath.replace(/^\/+/, "")}` : nextPath,
    });

  const setTextPromptTags = (tags: string[]) => {
    const cleaned = tags.map((t) => t.trim()).filter(Boolean);
    set("text_prompts", cleaned.map((t) => (t.includes(" ") ? `"${t}"` : t)).join(" "));
  };

  // Optional 'annotation_file' input — wiring an existing annotations/
  // exemplar-prompt JSON in populates annotation_file (+ its system) instead of
  // browsing/typing it by hand. resolveWiredLocation pulls both off the wire
  // (CustomNode's resolveOutputPath returns a full tapis://system/path URI
  // for a wired source-like node); only resolves for a directly-wired
  // DESIGN-TIME node, same caveat as every other connectedInputs read (see
  // StepPanelProps.ConnectedInput) — an upstream JOB step's output isn't
  // available here until it's actually run.
  const annotationFileInputPort = step.inputs.find((p) => p.port_name === "annotation_file")?.port_name;
  const annotationFileWire = annotationFileInputPort ? connectedInputs[annotationFileInputPort] : undefined;
  const wiredAnnotationFile = resolveWiredLocation(annotationFileWire);
  // The edge exists but carries no usable location. The common cause is an
  // upstream node holding a `path` with no `system` — resolveWiredLocation
  // returns null rather than guess a system, and resolveOutputPath only emits
  // a tapis://system/path URI when both are set. Most often that's a
  // smart_labeler whose annotations.json was saved while its system Select
  // showed a DERIVED default the user never explicitly picked. Worth saying
  // out loud: otherwise a correctly-drawn edge looks identical to no edge.
  const wiredButUnresolved = !!annotationFileWire && !wiredAnnotationFile;

  // Also flips prompt_mode to "exemplar": the Prompt file field only EXISTS in
  // that mode (see the SegmentedControl below), and prompt_mode defaults to
  // "text" — so filling annotation_file while still in text mode wrote the
  // value somewhere with no on-screen representation at all, which reads
  // exactly like "I wired the input and nothing turned up." Wiring an
  // exemplar file is an unambiguous statement of intent to use exemplars, so
  // switch to the view that shows it. Nothing is discarded either way:
  // prompt_mode is UI-only and text_prompts is still sent when set.
  const applyWiredAnnotationFile = () => {
    if (!wiredAnnotationFile) return;
    const { system, path } = wiredAnnotationFile;
    onChange({
      ...config,
      system,
      annotation_file: system && path ? `tapis://${system}/${path.replace(/^\/+/, "")}` : path,
      // Reference mode shows the same Prompt file field — keep it.
      prompt_mode: isInsid3 ? "reference" : "exemplar",
    });
  };

  // Auto-populate once per connection, and only when the user hasn't already
  // set a annotation_file — an explicit edit (including clearing it back out)
  // always takes precedence, matching every other wired-vs-manual field in
  // this app (e.g. smartLabeler.tsx's Run Configuration system default).
  const autoPopulatedRef = useRef(false);
  useEffect(() => {
    if (autoPopulatedRef.current) return;
    if (!wiredAnnotationFile) return;
    if (promptFileUri) return;
    autoPopulatedRef.current = true;
    applyWiredAnnotationFile();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wiredAnnotationFile?.system, wiredAnnotationFile?.path, promptFileUri]);

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
            Labels every image in the connected folder automatically, with no training. Describe what to find
            in words, or label a few examples and let the model find the rest.
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
          explainer="Tell the model what to find."
        >
          <Stack gap="sm">
            <SegmentedControl
              value={promptMode}
              onChange={setPromptMode}
              data={[
                { label: "Text prompts", value: "text" },
                { label: "Exemplar prompts", value: "exemplar" },
                { label: "In-context (INSID3)", value: "reference" },
              ]}
            />

            {promptMode === "text" ? (
              <Stack gap="xs">
                <PromptLabelEditor
                  label="Text prompts"
                  description={tip("text_prompts")}
                  value={textPrompts}
                  onChange={setTextPromptTags}
                />
                {/* The Prompt file field lives only in the other mode, so a
                    wired annotation_file (or one already set and then switched
                    away from) is invisible here — say so rather than leaving
                    the wire looking like it did nothing. */}
                {(wiredAnnotationFile || hasExemplars || wiredButUnresolved) && (
                  <Group gap="xs" align="center">
                    <Badge size="xs" variant="light" color={wiredButUnresolved && !hasExemplars ? "yellow" : "blue"}>
                      {hasExemplars
                        ? "A prompt file is set — switch to Exemplar prompts to see it"
                        : wiredButUnresolved
                          ? "A connected file couldn't be found — switch to Exemplar prompts for details"
                          : "A file is connected — switch to Exemplar prompts to use it"}
                    </Badge>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      leftSection={<IconLink size={12} />}
                      onClick={() => setPromptMode("exemplar")}
                    >
                      Switch
                    </Button>
                  </Group>
                )}
              </Stack>
            ) : (
              <Stack gap="xs">
                <Text size="xs" c="dimmed">
                  {promptMode === "reference"
                    ? "Label a few images in this folder (e.g. in Smart Labeler). INSID3 then finds the same things in every image."
                    : "Draw boxes around a few objects. SAM3 finds similar objects in the same image."}
                </Text>
                <Group align="flex-end" gap="xs" wrap="nowrap">
                  <div style={{ flex: 1 }}>
                    <TapisPathField
                      label="Prompt file"
                      description={tip("annotation_file")}
                      system={promptFileSystem}
                      path={promptFile}
                      selectType="file"
                      onSystemChange={(v) => setPromptFile(v, promptFile)}
                      onPathChange={(v) => setPromptFile(promptFileSystem, v)}
                    />
                  </div>
                  {wiredAnnotationFile && (
                    <Tooltip label={`Use the connected file (${wiredAnnotationFile.system}:${wiredAnnotationFile.path})`}>
                      <Button
                        size="sm"
                        variant="default"
                        leftSection={<IconLink size={14} />}
                        onClick={applyWiredAnnotationFile}
                      >
                        Use connected
                      </Button>
                    </Tooltip>
                  )}
                </Group>
                {wiredButUnresolved && (
                  <Badge size="xs" variant="light" color="yellow" style={{ height: "auto", whiteSpace: "normal", textTransform: "none", lineHeight: 1.4, padding: "4px 8px" }}>
                    A file is connected
                    {annotationFileWire?.sourceType ? ` (from ${annotationFileWire.sourceType})` : ""}, but its
                    location is missing. Open that node, choose its system, save it, and reopen this panel — or
                    browse for the file below.
                  </Badge>
                )}
                {wiredAnnotationFile &&
                  promptFile === wiredAnnotationFile.path &&
                  promptFileSystem === wiredAnnotationFile.system && (
                  <Badge size="xs" variant="light" color="blue">
                    Using the connected file
                  </Badge>
                )}
                {promptMode === "exemplar" && (
                  <PromptLabelEditor
                    label="Additional text prompts (optional)"
                    description="Extra words to search for alongside your examples."
                    value={textPrompts}
                    onChange={setTextPromptTags}
                  />
                )}
                {!hasExemplars && (
                  <Badge size="xs" variant="light" color="yellow">
                    {promptMode === "reference"
                      ? "Choose a file with your labelled examples."
                      : "Choose a file with your example boxes."}
                  </Badge>
                )}
              </Stack>
            )}
          </Stack>
        </ParamSection>

        <ParamSection
          title="Model"
          explainer="The defaults work well. Change these only if the results need improving."
        >
          <Group grow align="flex-start">
            <Select
              label="Model"
              description={tip("model")}
              data={schema.model?.options || ["sam3", "insid3"]}
              value={model}
              onChange={(v) => setModel(v ?? "sam3")}
              allowDeselect={false}
            />
            {isInsid3 ? (
              <Select
                label="Model size"
                description={tip("model_id")}
                data={[
                  { value: "dinov3_vitl16", label: "Large (most accurate)" },
                  { value: "dinov3_vitb16", label: "Base" },
                  { value: "dinov3_vits16", label: "Small (fastest)" },
                ]}
                value={String(field("model_id") || "dinov3_vitl16")}
                onChange={(v) => set("model_id", v === "dinov3_vitl16" ? "" : v ?? "")}
                allowDeselect={false}
              />
            ) : (
              <TextInput
                label="Model version"
                description={tip("model_id")}
                placeholder="facebook/sam3"
                value={String(field("model_id") ?? "")}
                onChange={(e) => set("model_id", e.currentTarget.value)}
              />
            )}
          </Group>
          {isInsid3 && (
            <Stack gap="sm" mt="sm">
              <TapisPathField
                label="Model file (optional)"
                description={tip("insid3_weights")}
                system={weightsSystem}
                path={weightsPath}
                selectType="file"
                onSystemChange={(v) => setWeights(v, weightsPath)}
                onPathChange={(v) => setWeights(weightsSystem, v)}
              />
              <Group grow align="flex-start">
                <Select
                  label="Box-only examples"
                  description={tip("reference_box_masks")}
                  data={schema.reference_box_masks?.options || ["sam3", "box"]}
                  value={boxMasks}
                  onChange={(v) => set("reference_box_masks", v ?? "sam3")}
                  allowDeselect={false}
                />
                <Select
                  label="Image size"
                  description={tip("insid3_image_size")}
                  data={["1024", "768"]}
                  value={String(field("insid3_image_size") ?? 1024)}
                  onChange={(v) => set("insid3_image_size", Number(v ?? 1024))}
                  allowDeselect={false}
                />
                <NumberInput
                  label="Min size (px)"
                  description={tip("min_area")}
                  value={Number(field("min_area") ?? 0)}
                  onChange={(v) => set("min_area", v)}
                  min={0}
                  step={50}
                />
              </Group>
              <Group grow align="flex-start">
                <NumberInput
                  label="Examples per class"
                  description={tip("max_shots")}
                  value={Number(field("max_shots") ?? 16)}
                  onChange={(v) => set("max_shots", v)}
                  min={1}
                  step={1}
                />
                <Select
                  label="Outline quality"
                  description={tip("insid3_mask_refiner")}
                  data={[
                    { value: "bilinear", label: "Fast (bilinear)" },
                    { value: "crf", label: "Sharp (CRF, GPU)" },
                  ]}
                  value={refiner}
                  onChange={(v) => set("insid3_mask_refiner", v ?? "bilinear")}
                  allowDeselect={false}
                />
                {refiner === "crf" && (
                  <NumberInput
                    label="Sharpness (px)"
                    description={tip("insid3_crf_size")}
                    value={Number(field("insid3_crf_size") ?? 640)}
                    onChange={(v) => set("insid3_crf_size", v)}
                    min={128}
                    step={64}
                  />
                )}
              </Group>
              <Button
                size="compact-xs"
                variant="subtle"
                onClick={() => setShowAdvanced((v) => !v)}
                style={{ alignSelf: "flex-start" }}
              >
                {showAdvanced ? "Hide advanced settings" : "Show advanced settings"}
              </Button>
              <Collapse expanded={showAdvanced}>
              <Group grow align="flex-start">
                <NumberInput
                  label="Detail level (tau)"
                  description={tip("insid3_tau")}
                  value={Number(field("insid3_tau") ?? 0.6)}
                  onChange={(v) => set("insid3_tau", v)}
                  min={0.05}
                  max={2}
                  step={0.05}
                  decimalScale={2}
                />
                <NumberInput
                  label="Match strictness"
                  description={tip("insid3_merge_threshold")}
                  value={Number(field("insid3_merge_threshold") ?? 0.2)}
                  onChange={(v) => set("insid3_merge_threshold", v)}
                  min={0}
                  max={1}
                  step={0.05}
                  decimalScale={2}
                />
              </Group>
              </Collapse>
              <Group gap="xs" align="center">
                <Button size="xs" variant="light" onClick={applySmallTargetPreset}>
                  Small-lesion preset
                </Button>
                <Text size="xs" c="dimmed" style={{ flex: 1 }}>
                  One click to set up for tiny targets like leaf lesions: small tiles and sharp outlines. Works
                  best with 5+ varied examples.
                </Text>
              </Group>
            </Stack>
          )}
          {/* INSID3 needs the Hub for its DINOv3 backbone (unless a checkpoint is
              given) and for SAM3, which turns box-only references into masks. */}
          {(!isInsid3 || !weightsUri.trim() || boxMasks === "sam3") && (
            <>
              <Select
                mt="sm"
                label="Hugging Face token"
                description={tip("hf_token") || "The saved secret holding your Hugging Face token, used to download the models."}
                placeholder="Select a secret"
                data={secretOptions}
                value={hfToken || null}
                onChange={(v) => set("hf_token", v ?? "")}
                searchable
                clearable
              />
              {!hfToken && (
                <Badge mt={4} size="xs" variant="light" color="yellow">
                  {isInsid3 && !weightsUri.trim()
                    ? "No token selected — the model can't be downloaded."
                    : isInsid3
                    ? "No token selected — box-only examples will use the whole box as the outline."
                    : "No token selected — SAM3 can't be downloaded."}
                </Badge>
              )}
            </>
          )}
        </ParamSection>

        <ParamSection
          title="Tiling (SAHI)"
          explainer="For large images with small objects."
        >
          <Stack gap="sm">
            <Switch
              label="Split images into tiles"
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
            {isSahi && isInsid3 && (
              <Text size="xs" c="dimmed">
                Your examples are cropped to tile size too, so they're compared at the same scale.
              </Text>
            )}
            {isSahi && hasExemplars && !isInsid3 && (
              <Group grow>
                <Select
                  label="Tiles to search"
                  description={tip("exemplar_tile_mode")}
                  data={schema.exemplar_tile_mode?.options || ["intersecting", "all"]}
                  value={String(field("exemplar_tile_mode") ?? "intersecting")}
                  onChange={(v) => set("exemplar_tile_mode", v ?? "intersecting")}
                  allowDeselect={false}
                />
                <NumberInput
                  label="Min example visibility"
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
          title="Filtering"
          explainer="Control which detections are kept. The defaults suit most runs."
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
                disabled={isInsid3}
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
                disabled={isInsid3}
                min={0}
                max={1}
                step={0.05}
                decimalScale={2}
              />
              <NumberInput
                label="Merge overlap"
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
                label="Merge across prompts"
                description={tip("class_agnostic_nms")}
                checked={Boolean(field("class_agnostic_nms"))}
                onChange={(e) => set("class_agnostic_nms", e.currentTarget.checked)}
              />
              <Switch
                label="Boxes only"
                description={tip("no_segmentation")}
                checked={Boolean(field("no_segmentation"))}
                onChange={(e) => set("no_segmentation", e.currentTarget.checked)}
              />
            </Group>
          </Stack>
        </ParamSection>

        <ParamSection
          title="Output"
          explainer="File format, and an optional limit for quick test runs."
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

// One text prompt: what the model is asked to find, and the class written to
// the annotations ("" = the prompt itself). Stored inside text_prompts as a
// `prompt=label` token, which the job splits on the first "=".
interface PromptRow {
  prompt: string;
  label: string;
}

function toRows(value: string): PromptRow[] {
  const rows = parsePromptTags(value).map((tag) => {
    const i = tag.indexOf("=");
    return i > 0 ? { prompt: tag.slice(0, i).trim(), label: tag.slice(i + 1).trim() } : { prompt: tag, label: "" };
  });
  return rows.length ? rows : [{ prompt: "", label: "" }];
}

function toTags(rows: PromptRow[]): string[] {
  return rows
    .filter((r) => r.prompt.trim())
    .map((r) => {
      const prompt = r.prompt.trim();
      const label = r.label.trim();
      return label && label !== prompt ? `${prompt}=${label}` : prompt;
    });
}

// Rows of prompt -> label. Rows live in local state so a half-typed or blank
// row survives (the stored string drops empty prompts), and are re-read from
// `value` only when it changes from outside this editor.
function PromptLabelEditor({ value, onChange, label, description }: {
  value: string;
  onChange: (tags: string[]) => void;
  label: string;
  description?: string;
}) {
  const [rows, setRows] = useState<PromptRow[]>(() => toRows(value));
  const lastSent = useRef(value);
  useEffect(() => {
    if (value !== lastSent.current) {
      lastSent.current = value;
      setRows(toRows(value));
    }
  }, [value]);

  const update = (next: PromptRow[]) => {
    setRows(next);
    const tags = toTags(next);
    lastSent.current = tags.map((t) => (t.includes(" ") ? `"${t}"` : t)).join(" ");
    onChange(tags);
  };
  // A double quote would break the CLI quoting; "=" in a prompt would be read
  // as the start of its label.
  const edit = (i: number, patch: Partial<PromptRow>) =>
    update(rows.map((r, j) => (j === i ? {
      prompt: (patch.prompt ?? r.prompt).replace(/["=]/g, ""),
      label: (patch.label ?? r.label).replace(/"/g, ""),
    } : r)));

  return (
    <Stack gap={6}>
      <div>
        <Text size="sm" fw={500}>{label}</Text>
        {description && <Text size="xs" c="dimmed">{description}</Text>}
      </div>
      <Group gap="xs" wrap="nowrap">
        <Text size="xs" c="dimmed" style={{ flex: 1 }}>Prompt (sent to the model)</Text>
        <span style={{ width: 14 }} />
        <Text size="xs" c="dimmed" style={{ flex: 1 }}>Label (written to the annotations)</Text>
        <span style={{ width: 28 }} />
      </Group>
      {rows.map((row, i) => (
        <Group key={i} gap="xs" wrap="nowrap">
          <TextInput
            style={{ flex: 1 }}
            size="sm"
            placeholder="e.g. dog"
            value={row.prompt}
            onChange={(e) => edit(i, { prompt: e.currentTarget.value })}
          />
          <IconArrowRight size={14} style={{ opacity: 0.5, flexShrink: 0 }} />
          <TextInput
            style={{ flex: 1 }}
            size="sm"
            placeholder={row.prompt.trim() ? `${row.prompt.trim()} (same as prompt)` : "e.g. german shepherd"}
            value={row.label}
            onChange={(e) => edit(i, { label: e.currentTarget.value })}
          />
          <Tooltip label="Remove this prompt">
            <ActionIcon
              variant="subtle"
              color="gray"
              onClick={() => update(rows.length > 1 ? rows.filter((_, j) => j !== i) : [{ prompt: "", label: "" }])}
            >
              <IconTrash size={14} />
            </ActionIcon>
          </Tooltip>
        </Group>
      ))}
      <Group>
        <Button
          size="compact-xs"
          variant="subtle"
          leftSection={<IconPlus size={12} />}
          onClick={() => setRows([...rows, { prompt: "", label: "" }])}
        >
          Add prompt
        </Button>
      </Group>
    </Stack>
  );
}

// Reverse of setTextPromptTags: split a CLI-ready string (individually
// double-quoted multi-word prompts, space-joined) back into an editable tag
// list (and from there, the prompt -> label rows).
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
