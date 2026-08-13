import { useEffect, useRef, useState } from "react";
import {
  Stack, Group, Text, Title, NumberInput, TextInput, TagsInput, Select, Switch,
  SegmentedControl, ScrollArea, Divider, Badge, Button, Tooltip,
} from "@mantine/core";
import { IconLink } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import { resolveWiredLocation, splitTapisUri } from "../lib/tapis";
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
// annotation_file are each included whenever set, so switching modes never
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
  const isSahi = Boolean(field("is_sahi"));

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
  const promptFileSystem = promptFileParts?.system ?? String(config.system ?? "");
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
      prompt_mode: "exemplar",
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
              <Stack gap="xs">
                <TagsInput
                  label="Text prompts"
                  description={tip("text_prompts")}
                  placeholder="Type a concept and press Enter (e.g. tree, parked car)"
                  value={parsePromptTags(textPrompts)}
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
                          ? "An annotation_file input is wired but has no resolvable location — switch to Exemplar prompts for details"
                          : "An annotation_file input is wired — switch to Exemplar prompts to use it"}
                    </Badge>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      leftSection={<IconLink size={12} />}
                      onClick={() => set("prompt_mode", "exemplar")}
                    >
                      Switch
                    </Button>
                  </Group>
                )}
              </Stack>
            ) : (
              <Stack gap="xs">
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
                    <Tooltip label={`Use the wired annotation_file input (${wiredAnnotationFile.system}:${wiredAnnotationFile.path}) as the prompt file`}>
                      <Button
                        size="sm"
                        variant="default"
                        leftSection={<IconLink size={14} />}
                        onClick={applyWiredAnnotationFile}
                      >
                        Use wired
                      </Button>
                    </Tooltip>
                  )}
                </Group>
                {wiredButUnresolved && (
                  <Badge size="xs" variant="light" color="yellow" style={{ height: "auto", whiteSpace: "normal", textTransform: "none", lineHeight: 1.4, padding: "4px 8px" }}>
                    An annotation_file input is connected
                    {annotationFileWire?.sourceType ? ` (from ${annotationFileWire.sourceType})` : ""}, but it
                    resolves to no location — the upstream node has a path with no Tapis system saved
                    alongside it, so its output port can't produce a tapis://system/path URI. Open that node,
                    pick its system explicitly and re-save it, then reopen this panel. Meanwhile you can
                    browse for the file by hand below.
                  </Badge>
                )}
                {wiredAnnotationFile &&
                  promptFile === wiredAnnotationFile.path &&
                  promptFileSystem === wiredAnnotationFile.system && (
                  <Badge size="xs" variant="light" color="blue">
                    prompt file from wired annotation_file input
                  </Badge>
                )}
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
