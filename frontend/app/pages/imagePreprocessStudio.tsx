import { useEffect, useMemo, useRef, useState, Suspense, lazy } from "react";
import {
  Stack, Group, TextInput, Button, Modal, Loader, Text, ScrollArea,
  UnstyledButton, Alert, Popover,
} from "@mantine/core";
import { IconFolder, IconPhoto, IconArrowUp, IconDeviceFloppy, IconSettings } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import type { StepPanelProps } from "./types";
import { BACKEND_URL } from "../lib/api";
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

// fetch that sends the session cookie and targets the configured backend, but
// WITHOUT apiFetch's global 401->login redirect: a 401/403 from the tapis-files
// endpoints means "no Tapis token", which we surface inline rather than bouncing
// the whole app to the login page.
const studioFetch = (path: string, init?: RequestInit) =>
  fetch(`${BACKEND_URL}${path}`, { ...init, credentials: "include" });

const IMAGE_EXT = /\.(jpe?g|png|bmp|tiff?|webp|gif)$/i;
const qs = (system: string, path: string) =>
  `system=${encodeURIComponent(system)}&path=${encodeURIComponent(path)}`;

// Modal that lists a Tapis directory, lets the user navigate folders and pick an
// image. Resolves the selection back to the caller (the FileSource.pickFile).
function DirectoryBrowser({ opened, system, rootPath, onPick, onCancel }: {
  opened: boolean;
  system: string;
  rootPath: string;
  onPick: (f: RemoteFile) => void;
  onCancel: () => void;
}) {
  const [path, setPath] = useState(rootPath || "/");
  const [entries, setEntries] = useState<RemoteFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (opened) setPath(rootPath || "/");
  }, [opened, rootPath]);

  useEffect(() => {
    if (!opened) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await studioFetch(`/api/tapis-files/list?${qs(system, path)}`);
        if (!res.ok) {
          const e = await res.json().catch(() => ({}));
          if (!cancelled) {
            setEntries([]);
            setError(
              res.status === 401 || res.status === 403
                ? "Log in with a real Tapis account to browse files."
                : e.detail || `Could not list directory (HTTP ${res.status}).`
            );
          }
        } else {
          const data = await res.json();
          if (!cancelled) setEntries(data.result || []);
        }
      } catch {
        if (!cancelled) setError("Could not reach the backend.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [opened, system, path]);

  const parent = path.replace(/\/+$/, "").split("/").slice(0, -1).join("/") || "/";
  const dirs = entries.filter((e) => e.type === "dir");
  const images = entries.filter(
    (e) => e.type === "file" && (IMAGE_EXT.test(e.name) || (e.mimeType || "").startsWith("image/"))
  );

  return (
    <Modal opened={opened} onClose={onCancel} title="Browse input directory" size="lg" zIndex={2000}>
      <Stack gap="xs">
        <Group gap="xs" wrap="nowrap">
          <Button
            size="xs"
            variant="light"
            leftSection={<IconArrowUp size={14} />}
            disabled={!path || path === "/"}
            onClick={() => setPath(parent)}
          >
            Up
          </Button>
          <Text size="sm" c="dimmed" style={{ wordBreak: "break-all" }}>
            {system || "(no system)"}:{path}
          </Text>
        </Group>

        {error && <Alert color="red" variant="light">{error}</Alert>}

        {loading ? (
          <Group justify="center" p="md"><Loader size="sm" /></Group>
        ) : (
          <ScrollArea.Autosize mah={360}>
            <Stack gap={2}>
              {dirs.map((d) => (
                <UnstyledButton
                  key={d.path}
                  onClick={() => setPath(d.path)}
                  style={{ padding: 6, borderRadius: 6 }}
                >
                  <Group gap={8}><IconFolder size={16} /><Text size="sm">{d.name}</Text></Group>
                </UnstyledButton>
              ))}
              {images.map((f) => (
                <UnstyledButton
                  key={f.path}
                  onClick={() => onPick(f)}
                  style={{ padding: 6, borderRadius: 6 }}
                >
                  <Group gap={8}><IconPhoto size={16} color="#7c3aed" /><Text size="sm">{f.name}</Text></Group>
                </UnstyledButton>
              ))}
              {!error && dirs.length === 0 && images.length === 0 && (
                <Text size="sm" c="dimmed">No folders or images here.</Text>
              )}
            </Stack>
          </ScrollArea.Autosize>
        )}
      </Stack>
    </Modal>
  );
}

export default function ImagePreprocessStudioPanel({ config, onChange, step }: StepPanelProps) {
  // Render the playground only after mount so its opencv-js WASM never runs on
  // the server.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const field = (key: string) => String(config[key] ?? step.config_schema[key]?.default ?? "");
  const setField = (key: string, value: string) => onChange({ ...config, [key]: value });

  const system = String(config.source_system ?? "");
  const sourceDir = field("source_dir");
  const pipelinePath = field("pipeline_path");

  // Custom FileSource: pickFile() opens the directory browser and resolves the
  // chosen image as a File (fetched from Tapis via the backend proxy). The source
  // object is stable; the browser modal reads the current system/path at open.
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

  const [saving, setSaving] = useState(false);
  const savePipeline = async () => {
    if (!system || !pipelinePath) {
      notifications.show({ color: "yellow", message: "Set the Tapis system and pipeline path first." });
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

  // The Tapis path config lives in a popover in the playground's header, so the
  // playground (a full-height Mantine AppShell) can own the whole surface.
  const pathControls = (
    <Popover width={340} position="bottom-end" withinPortal zIndex={10002} shadow="md">
      <Popover.Target>
        <Button size="xs" variant="light" leftSection={<IconSettings size={14} />}>
          Paths
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <TextInput
            label="Tapis system"
            placeholder="storage system id"
            value={system}
            onChange={(e) => setField("source_system", e.currentTarget.value)}
          />
          <TextInput
            label="Source directory"
            value={sourceDir}
            onChange={(e) => setField("source_dir", e.currentTarget.value)}
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
  );

  return (
    <div style={{ height: "100%" }}>
      {mounted ? (
        <Suspense fallback={<Group justify="center" p="xl"><Loader /></Group>}>
          <ImagePlayground
            title="Image Preprocessing Studio"
            fileSources={[dirSource]}
            initialPipeline={config.operations as Pipeline | undefined}
            onPipelineChange={(p: Pipeline) => onChange({ ...config, operations: p })}
            headerActions={
              <Group gap="xs" wrap="nowrap">
                {pathControls}
                <Button
                  size="xs"
                  leftSection={<IconDeviceFloppy size={14} />}
                  loading={saving}
                  onClick={savePipeline}
                >
                  Save operations.json
                </Button>
              </Group>
            }
          />
        </Suspense>
      ) : (
        <Group justify="center" p="xl"><Loader /></Group>
      )}

      <DirectoryBrowser
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
