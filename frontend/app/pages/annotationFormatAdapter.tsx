import { useState } from "react";
import { Stack, Group, Text, Title, Select, Button, ScrollArea, Divider, Alert, TextInput, Badge, TagsInput } from "@mantine/core";
import { IconArrowRight, IconAlertTriangle, IconCircleCheck } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import ParamSection from "../components/ParamSection";
import TapisPathField from "../components/TapisPathField";
import { resolveWiredLocation } from "../lib/tapis";

// Settings panel for the 'annotation_format_adapter' step
// (backend/steps/annotation_format_adapter/step.json) — converts annotations
// between native (smart_labeler JSON), COCO, YOLO, and GeoPackage.
//
// There's no Tapis job to configure a run against: the conversion runs inline
// in the backend, in two places that share the same code path —
//   * during a RUN, in-process when the node's turn comes in the DAG
//     (backend/engine/inline_steps.py). This is the one that matters for a
//     source an upstream step produces, since that file doesn't exist yet at
//     design time.
//   * from the "Convert now" button here, via POST
//     /api/annotation-adapter/convert (see annotation_adapter.py) — an
//     immediate, design-time convenience for a source that already exists,
//     the same way smart_labeler's Save button writes straight to Tapis.
// So the button is optional, not a prerequisite for running the workflow.
// Each wired input port only resolves HERE when it comes from a directly-wired
// DESIGN-TIME node (its own system/path config) — see
// StepPanelProps.ConnectedInput — which is why "Convert now" can be disabled
// for a wiring the run itself handles fine.
//
// Registered in registry.ts under the key "annotation_format_adapter".
export default function AnnotationFormatAdapterPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const schema = step.config_schema || {};
  const field = (key: string) => (config[key] !== undefined ? config[key] : schema[key]?.default);
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const fromFormat = String(field("from_format") ?? "native");
  const toFormat = String(field("to_format") ?? "coco");
  const destSystem = String(config.system ?? "");
  const destPath = String(config.path ?? "");

  // 'sam3_exemplars' writes zero_shot_annotation's PROMPT file, not an
  // annotation file — it's a destination only (no parser exists), so it's
  // filtered out of the "From" list even though step.json lists it under
  // to_format. See annotation_adapter.py's FROM_FORMATS/TO_FORMATS.
  const isExemplars = toFormat === "sam3_exemplars";
  const textPrompts = String(field("text_prompts") ?? "");
  const textPromptList = textPrompts.split(",").map((t) => t.trim()).filter(Boolean);

  // resolveWiredLocation pulls the system OFF the wired value itself
  // (CustomNode's resolveOutputPath returns a full tapis://system/path URI
  // for a wired source-like node), rather than assuming a bare `.config.path`
  // paired with some unrelated field — the exact "directory won't open,
  // defaulted to the wrong system" bug class this step, and every other
  // panel reading connectedInputs for a browsable/fetchable location, needs
  // to avoid.
  const wired = (portName: string) => {
    const c = connectedInputs[portName];
    const loc = resolveWiredLocation(c);
    return { system: loc?.system ?? "", path: loc?.path ?? "", wired: !!c };
  };
  const annotationsIn = wired("annotations");
  const annotationsDirIn = wired("annotations_dir");
  const annotationsGpkgIn = wired("annotations_gpkg");
  const imagesIn = wired("images");

  const needsImages = toFormat === "coco" || toFormat === "yolo" || fromFormat === "yolo";
  const sourceReady =
    (fromFormat === "native" || fromFormat === "coco") ? !!annotationsIn.path :
    fromFormat === "yolo" ? !!annotationsDirIn.path && !!imagesIn.path :
    !!annotationsGpkgIn.path; // geopackage
  const imagesReady = !needsImages || !!imagesIn.path;

  const [converting, setConverting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  const convert = async () => {
    if (!destSystem || !destPath) {
      setResult({ ok: false, message: "Set a destination Tapis system and path first." });
      return;
    }
    if (!sourceReady) {
      setResult({ ok: false, message: `from_format '${fromFormat}' needs its matching input wired.` });
      return;
    }
    if (!imagesReady) {
      setResult({ ok: false, message: `to_format '${toFormat}' (or from_format '${fromFormat}') needs the 'images' input wired.` });
      return;
    }
    setConverting(true);
    setResult(null);
    try {
      const body: Record<string, unknown> = {
        from_format: fromFormat,
        to_format: toFormat,
        dest: { system: destSystem, path: destPath },
      };
      if (fromFormat === "native" || fromFormat === "coco") body.annotations = { system: annotationsIn.system, path: annotationsIn.path };
      if (fromFormat === "yolo") body.annotations_dir = { system: annotationsDirIn.system, path: annotationsDirIn.path };
      if (fromFormat === "geopackage") body.annotations_gpkg = { system: annotationsGpkgIn.system, path: annotationsGpkgIn.path };
      if (imagesIn.path) body.images = { system: imagesIn.system, path: imagesIn.path };
      if (isExemplars) body.text_prompts = textPromptList;

      const res = await apiFetch("/api/annotation-adapter/convert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setResult({
        ok: true,
        message: `Converted ${data.annotation_count} annotation(s) across ${data.image_count} image(s) → ${destPath}`,
      });
    } catch (err: any) {
      setResult({ ok: false, message: err?.message || "Conversion failed" });
    } finally {
      setConverting(false);
    }
  };

  return (
    <ScrollArea style={{ height: "100%" }}>
      <Stack gap="lg" p="lg" maw={900} mx="auto">
        <div>
          <Title order={3}>🔄 Annotation Format Adapter</Title>
          <Text size="sm" c="dimmed" mt={4}>
            Converts annotations between native (smart_labeler JSON), COCO, YOLO, and GeoPackage. Runs inline —
            no Tapis job, just a direct conversion + upload. The workflow run performs the conversion itself when
            this step's turn comes, so a source produced by an upstream step is converted with that run's real
            data; "Convert now" below is an optional design-time shortcut for a source that already exists.
            GeoPackage output here is pixel-space, not geo-referenced (see the 'geospatial' step for real
            map-projected output).
          </Text>
        </div>

        <ParamSection
          title="Formats"
          explainer="Pick the source format (what's wired below) and the destination format to write."
        >
          <Group grow align="center">
            <Select
              label="From"
              data={(schema.from_format?.options || ["native", "coco", "yolo", "geopackage"]).filter(
                (f: string) => f !== "sam3_exemplars"
              )}
              value={fromFormat}
              onChange={(v) => set("from_format", v ?? "native")}
              allowDeselect={false}
            />
            <IconArrowRight size={18} style={{ marginTop: 22, flexShrink: 0 }} />
            <Select
              label="To"
              data={schema.to_format?.options || ["native", "coco", "yolo", "geopackage", "sam3_exemplars"]}
              value={toFormat}
              onChange={(v) => set("to_format", v ?? "coco")}
              allowDeselect={false}
            />
          </Group>
          {isExemplars && (
            <Text size="xs" c="dimmed" mt="xs">
              Writes the zero-shot step's exemplar prompt file: boxes grouped by label per image, as absolute
              pixel <code>[x1, y1, x2, y2]</code> corners. Wire this step's <b>converted</b> output into
              zero_shot_annotation's <b>annotation_file</b> input. A box whose <b>flag</b> is set
              to <code>negative</code> in the labeler becomes a negative exemplar (<code>box_labels</code> 0).
            </Text>
          )}
        </ParamSection>

        {isExemplars && (
          <ParamSection
            title="Text prompts"
            explainer="Exemplar boxes only apply to the images they were actually drawn on. These free-text concepts are written to the prompt file's top-level 'text_prompts' and apply to every image — without them, images you never labeled have nothing to detect with."
          >
            <TagsInput
              label="Text prompts"
              description={schema.text_prompts?.description}
              placeholder="Type a concept and press Enter (e.g. plant)"
              value={textPromptList}
              onChange={(tags) => set("text_prompts", tags.map((t) => t.trim()).filter(Boolean).join(","))}
            />
            {textPromptList.length === 0 && (
              <Badge mt="xs" size="xs" variant="light" color="yellow">
                No text prompts — only the labeled images will have anything to detect.
              </Badge>
            )}
          </ParamSection>
        )}

        <ParamSection
          title="Inputs"
          explainer="Only the input matching the chosen 'From' format is required. 'images' is additionally needed whenever COCO/YOLO width-height normalization is involved."
        >
          <Stack gap="xs">
            {(fromFormat === "native" || fromFormat === "coco") && (
              <InputStatus label={`annotations (${fromFormat} JSON)`} wiredPath={annotationsIn.path} required />
            )}
            {fromFormat === "yolo" && (
              <InputStatus label="annotations_dir (YOLO labels + classes.txt)" wiredPath={annotationsDirIn.path} required />
            )}
            {fromFormat === "geopackage" && (
              <InputStatus label="annotations_gpkg (GeoPackage)" wiredPath={annotationsGpkgIn.path} required />
            )}
            <InputStatus
              label="images"
              wiredPath={imagesIn.path}
              required={needsImages}
              // Optional for sam3_exemplars, but it decides whether an
              // exemplar key keeps its subdirectory ("batch_a/img.jpg") or
              // falls back to a bare filename — both of which the job accepts.
              optionalNote={isExemplars ? "improves keys" : undefined}
            />
          </Stack>
        </ParamSection>

        <ParamSection
          title="Destination"
          explainer={toFormat === "yolo"
            ? "A directory — one .txt per image plus classes.txt are written under it."
            : "A file path the converted output is written to."}
        >
          <TapisPathField
            label="Destination"
            system={destSystem}
            path={destPath}
            selectType={toFormat === "yolo" ? "dir" : "file"}
            onSystemChange={(v) => set("system", v)}
            onPathChange={(v) => set("path", v)}
          />
        </ParamSection>

        <Divider />

        <Group justify="space-between" align="center">
          <Button onClick={convert} loading={converting} disabled={!sourceReady || !imagesReady || !destSystem || !destPath}>
            Convert now
          </Button>
          {/* A source coming from a JOB step has no design-time path to fetch,
              so this button stays disabled — that's expected, not a misconfigured
              node. Say so, otherwise a disabled button reads as "this step can't
              run" when the run will convert it just fine. */}
          {(!sourceReady || !imagesReady) && !!destSystem && !!destPath && !result && (
            <Text size="xs" c="dimmed" flex={1}>
              The inputs aren't available at design time (they're produced by an upstream step at run time), so
              there's nothing to convert right now. The workflow run will do the conversion itself.
            </Text>
          )}
          {result && (
            <Alert
              flex={1}
              variant="light"
              color={result.ok ? "green" : "red"}
              icon={result.ok ? <IconCircleCheck size={16} /> : <IconAlertTriangle size={16} />}
              py={6}
            >
              {result.message}
            </Alert>
          )}
        </Group>
      </Stack>
    </ScrollArea>
  );
}

// Full-height scrollable layout, matching the other custom ParamSection-based
// panels (see StepSettingsModal, which honors this static flag).
(AnnotationFormatAdapterPanel as any).fullScreen = true;

function InputStatus({
  label,
  wiredPath,
  required,
  optionalNote,
}: {
  label: string;
  wiredPath: string;
  required: boolean;
  // Shown instead of "not needed" when the port is genuinely optional but
  // still does something useful for the current format pair.
  optionalNote?: string;
}) {
  return (
    <Group gap="xs" wrap="nowrap">
      <TextInput
        label={label}
        value={wiredPath}
        placeholder={`Connect a ${label} input`}
        readOnly
        variant="filled"
        style={{ flex: 1 }}
      />
      <Badge mt={22} size="xs" variant="light" color={wiredPath ? "green" : required ? "red" : optionalNote ? "blue" : "gray"}>
        {wiredPath ? "connected" : required ? "required" : optionalNote || "not needed"}
      </Badge>
    </Group>
  );
}
