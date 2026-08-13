import { useCallback, useEffect, useMemo, useRef, useState, Suspense, lazy } from "react";
import { Group, TextInput, Button, Loader, Stack, Popover, Select, Paper } from "@mantine/core";
import { IconDeviceFloppy, IconSettings } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import type { StepPanelProps } from "./types";
import { BACKEND_URL } from "../lib/api";
import { TAPIS_SYSTEMS, DEFAULT_TAPIS_SYSTEM, resolveWiredLocation } from "../lib/tapis";
import TapisDirectoryBrowser, { parentOf } from "../components/TapisDirectoryBrowser";
// Type-only imports — erased at build, so the heavy opencv-js package is NOT
// pulled into the SSR bundle. The runtime component is loaded lazily below.
import type { FileSource, RemoteFile } from "@icicle-ai/opencv-image-playground";
import type { Pipeline } from "@icicle-ai/opencv-image-playground-core";

// Client-only load of the OpenCV playground (bundles opencv-js WASM + uses
// browser APIs; must never import/render during server render).
const ImagePlayground = lazy(async () => {
  const mod = await import("@icicle-ai/opencv-image-playground");
  return { default: mod.ImagePlayground };
});

const studioFetch = (path: string, init?: RequestInit) =>
  fetch(`${BACKEND_URL}${path}`, { ...init, credentials: "include" });

const qs = (system: string, path: string) =>
  `system=${encodeURIComponent(system)}&path=${encodeURIComponent(path)}`;

export default function ImagePreprocessStudioPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  // Render the playground only after mount so its opencv-js WASM never runs on
  // the server.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const field = (key: string) => String(config[key] ?? step.config_schema[key]?.default ?? "");
  const setField = (key: string, value: string) => onChange({ ...config, [key]: value });

  // Directory wired into the step's image_dir input (from an upstream source
  // node's `path`), used as the browse root when the user hasn't picked one.
  // resolveWiredLocation pulls the system OFF the wired value itself
  // (CustomNode's resolveOutputPath returns a full tapis://system/path URI
  // for a wired source-like node) rather than assuming it shares whatever
  // `system` below defaults to.
  const imageInputPort = step.inputs.find((p) => p.data_type === "image_dir")?.port_name;
  const wiredLocation = imageInputPort ? resolveWiredLocation(connectedInputs[imageInputPort]) : null;
  const wiredDir = wiredLocation?.path;

  // System: explicit user pick > the wired directory's own system > shared
  // default. Falling straight to DEFAULT_TAPIS_SYSTEM regardless of a wired
  // input was the "browsing a wired directory silently uses the wrong
  // system" bug — this `system` value is shared for browsing sourceDir below
  // AND for where operations.json gets saved.
  const system = String(config.source_system ?? "") || wiredLocation?.system || DEFAULT_TAPIS_SYSTEM;

  // Source dir precedence: explicitly browsed value > wired input > schema default.
  const sourceDir =
    String(config.source_dir ?? "") ||
    String(wiredDir ?? "") ||
    String(step.config_schema.source_dir?.default ?? "");
  const pipelinePath = field("pipeline_path");

  // Custom FileSource: pickFile() opens the directory browser and resolves the
  // chosen image as a File (fetched from Tapis via the backend proxy).
  const [browserOpen, setBrowserOpen] = useState(false);
  const resolverRef = useRef<((f: File | null) => void) | null>(null);

  const dirSource: FileSource = useMemo(
    () => ({
      id: "input-dir",
      label: "Input directory",
      pickFile: () =>
        new Promise<File | null>((resolve) => {
          resolverRef.current = resolve;
          setBrowserOpen(true);
        }),
    }),
    []
  );

  const finishPick = async (remote: RemoteFile) => {
    setBrowserOpen(false);
    // The folder we browsed becomes the (read-only) source directory.
    setField("source_dir", parentOf(remote.path));
    try {
      const res = await studioFetch(`/api/tapis-files/content?${qs(system, remote.path)}`);
      if (!res.ok) throw new Error(String(res.status));
      const blob = await res.blob();
      resolverRef.current?.(
        new File([blob], remote.name, { type: blob.type || remote.mimeType || "application/octet-stream" })
      );
    } catch {
      notifications.show({ color: "red", message: `Could not load ${remote.name}` });
      resolverRef.current?.(null);
    } finally {
      resolverRef.current = null;
    }
  };

  const cancelPick = () => {
    setBrowserOpen(false);
    resolverRef.current?.(null);
    resolverRef.current = null;
  };

  // Keep the latest config reachable WITHOUT re-rendering the (expensive)
  // playground on every keystroke. Typing in the path/system fields updates
  // `config`, but the memoized <ImagePlayground> below stays stable.
  const configRef = useRef(config);
  configRef.current = config;

  const handlePipelineChange = useCallback(
    (p: Pipeline) => onChange({ ...configRef.current, operations: p }),
    [onChange]
  );

  // The playground is uncontrolled after mount, so capture its initial pipeline
  // once; then memoize the element so unrelated field edits don't re-render it.
  const initialPipelineRef = useRef(config.operations as Pipeline | undefined);
  const playground = useMemo(
    () => (
      <ImagePlayground
        title="Image Preprocessing Studio"
        fileSources={[dirSource]}
        initialPipeline={initialPipelineRef.current}
        onPipelineChange={handlePipelineChange}
      />
    ),
    [dirSource, handlePipelineChange]
  );

  const [saving, setSaving] = useState(false);
  const savePipeline = async () => {
    if (!system || !pipelinePath) {
      notifications.show({ color: "yellow", message: "Select a Tapis system and set a pipeline path first." });
      return;
    }
    setSaving(true);
    try {
      const res = await studioFetch(`/api/tapis-files/upload`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system,
          path: pipelinePath,
          content: JSON.stringify(config.operations ?? {}, null, 2),
        }),
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `HTTP ${res.status}`);
      }
      notifications.show({ color: "green", message: `Saved operations.json to ${pipelinePath}` });
    } catch (err: any) {
      notifications.show({ color: "red", title: "Save failed", message: err?.message || "Could not save" });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ height: "100%" }}>
      {mounted ? (
        <Suspense fallback={<Group justify="center" p="xl"><Loader /></Group>}>
          {playground}
        </Suspense>
      ) : (
        <Group justify="center" p="xl"><Loader /></Group>
      )}

      {/* Our own floating toolbar. Rendered by us on every state change, so the
          system Select stays interactive — the library's headerActions did not
          re-render reactively, which made the dropdown go stale after one pick. */}
      <Paper
        shadow="sm"
        p={6}
        radius="md"
        withBorder
        style={{ position: "fixed", top: 12, right: 16, zIndex: 10001 }}
      >
        <Group gap="xs" wrap="nowrap">
          <Select
            size="xs"
            placeholder="Tapis system"
            data={TAPIS_SYSTEMS}
            value={system}
            onChange={(v) => setField("source_system", v ?? "")}
            allowDeselect={false}
            w={180}
            comboboxProps={{ withinPortal: true, zIndex: 10002 }}
          />
          <Popover width={360} position="bottom-end" withinPortal zIndex={10002} shadow="md">
            <Popover.Target>
              <Button size="xs" variant="light" leftSection={<IconSettings size={14} />}>
                Paths
              </Button>
            </Popover.Target>
            <Popover.Dropdown>
              <Stack gap="xs">
                <TextInput
                  label="Source directory"
                  description="Set by browsing images — not edited directly."
                  value={sourceDir}
                  readOnly
                  variant="filled"
                />
                <TextInput
                  label="Pipeline path (operations.json target)"
                  placeholder="/path/on/tapis/operations.json"
                  value={pipelinePath}
                  onChange={(e) => setField("pipeline_path", e.currentTarget.value)}
                />
              </Stack>
            </Popover.Dropdown>
          </Popover>
          <Button
            size="xs"
            leftSection={<IconDeviceFloppy size={14} />}
            loading={saving}
            onClick={savePipeline}
          >
            Save operations.json
          </Button>
        </Group>
      </Paper>

      <TapisDirectoryBrowser
        opened={browserOpen}
        system={system}
        rootPath={sourceDir}
        onPick={finishPick}
        onCancel={cancelPick}
      />
    </div>
  );
}

// The playground renders its own full-height AppShell, so give it the whole
// screen (see StepSettingsModal, which honors this).
(ImagePreprocessStudioPanel as any).fullScreen = true;
