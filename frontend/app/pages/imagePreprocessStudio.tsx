import { useCallback, useEffect, useMemo, useRef, useState, Suspense, lazy } from "react";
import { Group, TextInput, Button, Loader, Stack, Popover, Select, Paper, Text, Alert, CloseButton } from "@mantine/core";
import { IconDeviceFloppy, IconSettings, IconAlertTriangle, IconCheck } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
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

// apiFetch, not a bare fetch: it carries whichever credential this deployment
// uses — the session cookie standalone, the host's X-Tapis-Token when embedded.
const studioFetch = (path: string, init?: RequestInit) => apiFetch(path, init);

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

  // A browsed source_dir (and the image remembered with it) only holds while
  // the wiring it was picked under is unchanged. Without this, ANY earlier
  // browse pinned source_dir forever: re-wiring the input to another directory
  // moved `system` (which follows the wire) but left the path on the old
  // folder. A browse records the wired location it was made against
  // (source_dir_wired_from); when that no longer matches, the wire wins again.
  // Configs saved before that key existed count as still valid only if the
  // browsed folder sits inside the currently wired one.
  const trimSlash = (p: string) => p.replace(/\/+$/, "");
  const wiredKey = wiredLocation ? `${wiredLocation.system}:${trimSlash(wiredLocation.path)}` : "";
  const browsedDir = String(config.source_dir ?? "");
  const browseStillValid = (() => {
    if (!browsedDir) return false;
    if (!wiredLocation) return true;
    if (config.source_dir_wired_from !== undefined) return config.source_dir_wired_from === wiredKey;
    const wired = trimSlash(wiredLocation.path);
    const browsed = trimSlash(browsedDir);
    return browsed === wired || browsed.startsWith(`${wired}/`);
  })();

  // Source dir precedence: browsed value (still valid for this wiring) > wired
  // input > schema default.
  const sourceDir =
    (browseStillValid ? browsedDir : "") ||
    String(wiredDir ?? "") ||
    String(step.config_schema.source_dir?.default ?? "");
  const pipelinePath = field("pipeline_path");

  // Where "Save operations.json" actually writes.
  //
  // An explicit pipeline path wins. Blank falls back to operations.json beside
  // the images in the source directory — previously a blank path just refused
  // to save at all, which read as the button doing nothing. Blank still means
  // "auto" for the RUN (the pre-submit handler writes to the step's archive
  // dir); this fallback only decides where a MANUAL save lands.
  const saveTarget = useMemo(() => {
    const explicit = pipelinePath.trim();
    if (explicit) return explicit;
    const dir = sourceDir.trim().replace(/\/+$/, "");
    return dir ? `${dir}/operations.json` : "";
  }, [pipelinePath, sourceDir]);

  // Keep the latest config reachable WITHOUT re-rendering the (expensive)
  // playground on every keystroke. Typing in the path/system fields updates
  // `config`, but the memoized <ImagePlayground> below stays stable.
  const configRef = useRef(config);
  configRef.current = config;

  const fetchImage = async (sys: string, path: string, mimeType?: string): Promise<File> => {
    const res = await studioFetch(`/api/tapis-files/content?${qs(sys, path)}`);
    if (!res.ok) throw new Error(String(res.status));
    const blob = await res.blob();
    const name = path.split("/").pop() || path;
    return new File([blob], name, { type: blob.type || mimeType || "application/octet-stream" });
  };

  // The image last worked on, remembered on the node config so reopening the
  // studio brings it back alongside the saved `operations`. Captured once at
  // mount: the restore below consumes it a single time.
  // Skipped when the wiring has moved on: the remembered image belongs to the
  // old directory.
  const restoreRef = useRef<{ system: string; path: string } | null>(
    config.last_image_path && (browseStillValid || !wiredLocation)
      ? { system: String(config.last_image_system || system), path: String(config.last_image_path) }
      : null
  );

  // Custom FileSource: pickFile() opens the directory browser and resolves the
  // chosen image as a File (fetched from Tapis via the backend proxy). While a
  // restore is pending it resolves the remembered image instead, without
  // opening the browser.
  const [browserOpen, setBrowserOpen] = useState(false);
  const resolverRef = useRef<((f: File | null) => void) | null>(null);

  const dirSource: FileSource = useMemo(
    () => ({
      id: "input-dir",
      label: "Input directory",
      pickFile: () => {
        const restore = restoreRef.current;
        if (restore) {
          restoreRef.current = null;
          return fetchImage(restore.system, restore.path).catch(() => {
            notifications.show({ color: "red", message: `Could not reopen last image ${restore.path}` });
            return null;
          });
        }
        return new Promise<File | null>((resolve) => {
          resolverRef.current = resolve;
          setBrowserOpen(true);
        });
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );

  // The playground holds the loaded image in internal state and only exposes
  // its own "Open image" button as the way in (no initial-image prop), so a
  // restore clicks that button once it renders; pickFile above answers it.
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const root = containerRef.current;
    if (!root || !restoreRef.current) return;
    const tryOpen = () => {
      const btn = Array.from(root.querySelectorAll("button")).find(
        (b) => b.textContent?.trim() === "Open image"
      );
      if (!btn) return false;
      btn.click();
      return true;
    };
    if (tryOpen()) return;
    const observer = new MutationObserver(() => {
      if (tryOpen()) observer.disconnect();
    });
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);

  const finishPick = async (remote: RemoteFile) => {
    setBrowserOpen(false);
    // The folder we browsed becomes the (read-only) source directory, and the
    // image itself is remembered for the next time the studio opens.
    onChange({
      ...configRef.current,
      source_dir: parentOf(remote.path),
      last_image_path: remote.path,
      last_image_system: system,
      source_dir_wired_from: wiredKey,
    });
    try {
      resolverRef.current?.(await fetchImage(system, remote.path, remote.mimeType));
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

  // Manual save to a chosen location. NOT what feeds the job any more — the run
  // writes operations.json itself from `config.operations`, so a pipeline built
  // here reaches the job whether or not this button is ever pressed. Kept for
  // exporting the pipeline somewhere of your own choosing.
  const [saving, setSaving] = useState(false);
  // Save result shown INLINE in the toolbar below, not only as a notification.
  // This panel is a full-screen modal that paints its own toolbar at z-index
  // 10001; a toast is easy to miss behind it, and "did that save or not?" is
  // exactly the question this button has to answer unambiguously.
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedTo, setSavedTo] = useState<string | null>(null);

  const savePipeline = async () => {
    setSaveError(null);
    setSavedTo(null);

    if (!system) {
      setSaveError("Pick a Tapis system first (top-left of this toolbar).");
      return;
    }
    if (!saveTarget) {
      setSaveError(
        "Nowhere to save yet. Browse to an image to set the source directory, " +
          "or open Paths and type a full path including the filename."
      );
      return;
    }
    // A directory-looking target would upload under a filename Tapis derives
    // from the trailing segment, i.e. not operations.json. Catch it here rather
    // than let the file land somewhere surprising.
    if (saveTarget.endsWith("/")) {
      setSaveError(`"${saveTarget}" is a directory. Include the filename, e.g. ${saveTarget}operations.json`);
      return;
    }

    setSaving(true);
    try {
      const res = await studioFetch(`/api/tapis-files/upload`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system,
          path: saveTarget,
          content: JSON.stringify(config.operations ?? {}, null, 2),
        }),
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `HTTP ${res.status} ${res.statusText}`.trim());
      }
      setSavedTo(saveTarget);
      notifications.show({ color: "green", message: `Saved to ${saveTarget}` });
    } catch (err: any) {
      const message = err?.message || "Could not save (network error).";
      setSaveError(message);
      notifications.show({ color: "red", title: "Save failed", message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div ref={containerRef} style={{ height: "100%" }}>
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
                {/* Optional since the RUN writes operations.json itself, from
                    the `operations` pipeline stored on this node's config (see
                    _image_preprocess_studio_presubmit). Before that, leaving
                    this blank meant the job staged a file nobody had ever
                    written — and the placeholder went out unsubstituted, since
                    a schema field with no default never reaches the render
                    context. Kept as an override for putting the file somewhere
                    specific, or pointing at one maintained outside this app. */}
                <TextInput
                  label="Pipeline path (operations.json target)"
                  description={
                    "Enter the FULL path including the filename — e.g. " +
                    `${(sourceDir || "/home/you/data").replace(/\/+$/, "")}/operations.json. ` +
                    "Leave blank to save beside your images; the run always writes its " +
                    "own copy to this step's archive dir either way."
                  }
                  placeholder={saveTarget || "/full/path/to/operations.json"}
                  value={pipelinePath}
                  onChange={(e) => setField("pipeline_path", e.currentTarget.value)}
                />
                {/* The single most useful line here: where the button will
                    actually write, resolved the same way savePipeline resolves
                    it, so "blank" is never a mystery. */}
                <Text size="xs" c={saveTarget ? "dimmed" : "orange"}>
                  {saveTarget
                    ? `Save writes to: ${saveTarget}`
                    : "No target yet — browse to an image, or type a full path above."}
                </Text>
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

        {/* Inline result, in the toolbar itself. A notification is also fired,
            but this panel is a full-screen modal painting at z-index 10001, so
            a toast can land behind it — this is the copy the user is certain
            to see. Errors stay until the next attempt; the Tapis message is
            shown verbatim because it names the actual cause (bad path, no
            permission, expired token). */}
        {(saveError || savedTo) && (
          <Alert
            mt={6}
            py={6}
            px={8}
            variant="light"
            color={saveError ? "red" : "green"}
            icon={saveError ? <IconAlertTriangle size={16} /> : <IconCheck size={16} />}
            title={saveError ? "Could not save operations.json" : "Saved"}
            styles={{ title: { fontSize: 12, marginBottom: 2 }, body: { maxWidth: 420 } }}
          >
            <Group justify="space-between" align="flex-start" wrap="nowrap" gap="xs">
              <Text size="xs" style={{ wordBreak: "break-word" }}>
                {saveError ?? savedTo}
              </Text>
              <CloseButton
                size="xs"
                aria-label="Dismiss"
                onClick={() => { setSaveError(null); setSavedTo(null); }}
              />
            </Group>
          </Alert>
        )}
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
