import { useState } from "react";
import { Stack, Group, Text, Title, Select, Button, ScrollArea, Divider, Alert, TextInput, Badge } from "@mantine/core";
import { IconArrowRight, IconAlertTriangle, IconCircleCheck } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import ParamSection from "../components/ParamSection";
import TapisPathField from "../components/TapisPathField";

// Settings panel for the 'annotation_format_adapter' step
// (backend/steps/annotation_format_adapter/step.json) — converts annotations
// between native (smart_labeler JSON), COCO, YOLO, and GeoPackage.
//
// Design-time only: there's no Tapis job to configure a run against, just a
// "Convert now" action that calls POST /api/annotation-adapter/convert (see
// annotation_adapter.py) directly, the same way smart_labeler's Save button
// writes straight to Tapis instead of going through a job. Each wired input
// port only resolves here when it comes from a directly-wired DESIGN-TIME
// node (its own system/path config) — see StepPanelProps.ConnectedInput.
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

  const wired = (portName: string) => {
    const c = connectedInputs[portName];
    return { system: String(c?.config?.system ?? ""), path: String(c?.config?.path ?? ""), wired: !!c };
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
            no Tapis job, just a direct conversion + upload. GeoPackage output here is pixel-space, not
            geo-referenced (see the 'geospatial' step for real map-projected output).
          </Text>
        </div>

        <ParamSection
          title="Formats"
          explainer="Pick the source format (what's wired below) and the destination format to write."
        >
          <Group grow align="center">
            <Select
              label="From"
              data={schema.from_format?.options || ["native", "coco", "yolo", "geopackage"]}
              value={fromFormat}
              onChange={(v) => set("from_format", v ?? "native")}
              allowDeselect={false}
            />
            <IconArrowRight size={18} style={{ marginTop: 22, flexShrink: 0 }} />
            <Select
              label="To"
              data={schema.to_format?.options || ["native", "coco", "yolo", "geopackage"]}
              value={toFormat}
              onChange={(v) => set("to_format", v ?? "coco")}
              allowDeselect={false}
            />
          </Group>
        </ParamSection>

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
            <InputStatus label="images" wiredPath={imagesIn.path} required={needsImages} />
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

function InputStatus({ label, wiredPath, required }: { label: string; wiredPath: string; required: boolean }) {
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
      <Badge mt={22} size="xs" variant="light" color={wiredPath ? "green" : required ? "red" : "gray"}>
        {wiredPath ? "connected" : required ? "required" : "not needed"}
      </Badge>
    </Group>
  );
}
