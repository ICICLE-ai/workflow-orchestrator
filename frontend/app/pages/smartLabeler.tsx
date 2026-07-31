import { useCallback, useEffect, useMemo, useState } from "react";
import type { ComponentProps, ComponentType, ReactElement } from "react";
import { Group, TextInput, Button, Loader, Select, Paper, SegmentedControl, Text, Stack, Drawer, ActionIcon, Tooltip } from "@mantine/core";
import { IconDeviceFloppy, IconLayoutSidebarLeftExpand, IconLayoutSidebarRightExpand } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import type { StepPanelProps } from "./types";
import { BACKEND_URL, SAM3_ENDPOINT } from "../lib/api";
import { TAPIS_SYSTEMS, DEFAULT_TAPIS_SYSTEM } from "../lib/tapis";
import { uploadTapisFile } from "../lib/tapisFiles";

// Type-only imports — erased at build, so these browser-only packages (an MUI
// file browser that talks to Tapis directly, a <canvas>-based annotator) never
// load during SSR. The runtime modules are loaded lazily below, client-side only.
import type { FileExplorerProps } from "@icicle-ai/tapis-file-explorer";
import type { AnnotationDetails as AnnotationDetailsComponent } from "@icicle-ai/annotation-details";
import type { ImageCanvasProps, CanvasEngine, BaseAnnotation } from "@icicle-ai/image-annotation-canvas";

const studioFetch = (path: string, init?: RequestInit) =>
  fetch(`${BACKEND_URL}${path}`, { ...init, credentials: "include" });

type Anno = BaseAnnotation & Record<string, any>;
type AnnotationDetailsProps = ComponentProps<typeof AnnotationDetailsComponent>;

interface Libs {
  FileExplorer: ComponentType<FileExplorerProps>;
  AnnotationDetails: ComponentType<AnnotationDetailsProps>;
  ImageCanvas: (props: ImageCanvasProps<any>) => ReactElement;
  detectionEngine: CanvasEngine<any>;
  segmentationEngine: CanvasEngine<any>;
}

export default function SmartLabelerPanel({ config, onChange, step, nodeId, connectedInputs }: StepPanelProps) {
  // Render only after mount so the canvas/MUI/Tapis-direct packages never run
  // during server render (see docs/adding-a-step-custom-ui.md §5).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const field = (key: string) => String(config[key] ?? step.config_schema[key]?.default ?? "");
  const setField = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const system = field("system") || DEFAULT_TAPIS_SYSTEM;
  const outputPath = field("path");
  const annotationType = (config.annotation_type === "segmentation" ? "segmentation" : "detection") as
    | "detection"
    | "segmentation";

  // The image directory this step labels comes from its wired 'images' input
  // (an upstream source_image_dir node), not its own config — see step.json.
  const imageInputPort = step.inputs.find((p) => p.data_type === "image_dir")?.port_name;
  const sourceDir = imageInputPort ? String(connectedInputs[imageInputPort]?.config?.path ?? "") : "";

  // Load the three smart_labeler packages once, client-side only, and point the
  // file explorer at this deployment's Tapis proxy / systems.
  const [libs, setLibs] = useState<Libs | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [tfe, ad, iac] = await Promise.all([
        import("@icicle-ai/tapis-file-explorer"),
        import("@icicle-ai/annotation-details"),
        import("@icicle-ai/image-annotation-canvas"),
      ]);
      tfe.configureTapisFileExplorer({
        apiBaseUrl: BACKEND_URL,
        allowedSystems: TAPIS_SYSTEMS.map((s) => ({ value: s, label: s })),
        defaultSystem: DEFAULT_TAPIS_SYSTEM,
      });
      if (!cancelled) {
        setLibs({
          FileExplorer: tfe.FileExplorer,
          AnnotationDetails: ad.AnnotationDetails,
          ImageCanvas: iac.ImageCanvas,
          detectionEngine: iac.detectionEngine,
          segmentationEngine: iac.segmentationEngine,
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Unlike the rest of the app (which proxies every Tapis call through the
  // backend so a raw token never reaches the browser), tapis-file-explorer
  // fetches images directly from Tapis client-side, so it needs the real token.
  // A missing token means "logged into the app but no Tapis session" — surfaced
  // inline rather than bouncing the whole app to login.
  const [tapisToken, setTapisToken] = useState<string | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);
  useEffect(() => {
    studioFetch("/api/tapis/token")
      .then(async (res) => {
        if (!res.ok) {
          const e = await res.json().catch(() => ({}));
          throw new Error(e.detail || `HTTP ${res.status}`);
        }
        return res.json();
      })
      .then((d) => setTapisToken(d.token))
      .catch((err) => setTokenError(err?.message || "Could not fetch Tapis token"));
  }, []);

  const [imageFiles, setImageFiles] = useState<string[]>([]);
  const [current, setCurrent] = useState<{ url: string | null; path: string } | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  // The file browser and annotation list are collapsible drawers over the
  // canvas (not fixed side columns), so either can be tucked away for more
  // canvas room. Open by default to match the previous always-visible layout.
  const [fileDrawerOpen, setFileDrawerOpen] = useState(true);
  const [detailsDrawerOpen, setDetailsDrawerOpen] = useState(true);
  const [filter, setFilter] = useState<{ minScore: number; activeLabels: string[]; activeFlags: string[] }>({
    minScore: 0,
    activeLabels: [],
    activeFlags: [],
  });

  // Per-image annotation lists, keyed by the file's path within sourceDir. This
  // whole map is what gets serialized into annotations.json on Save.
  const annotationsByFile: Record<string, Anno[]> = config.annotations_by_file ?? {};
  const currentPath = current?.path ?? "";
  const annotations = annotationsByFile[currentPath] ?? [];

  // AnnotationDetails filters its own displayed list internally by score; the
  // confidence slider only reaches us via handleFilterAnnotations, so the
  // canvas has to be filtered here too — it has no built-in score threshold
  // (only activeLabels/activeFlags), unlike AnnotationDetails which mirrors
  // this exact "missing score always passes" rule internally.
  const visibleAnnotations = useMemo(
    () => annotations.filter((a) => (typeof a.score === "number" ? a.score >= filter.minScore : true)),
    [annotations, filter.minScore]
  );

  const setAnnotationsForCurrent = useCallback(
    (next: Anno[]) => {
      onChange({ ...config, annotations_by_file: { ...annotationsByFile, [currentPath]: next } });
    },
    [config, annotationsByFile, currentPath, onChange]
  );

  const [saving, setSaving] = useState(false);
  const saveAnnotations = async () => {
    if (!system || !outputPath) {
      notifications.show({ color: "yellow", message: "Select a Tapis system and set an output path first." });
      return;
    }
    if (!tapisToken) {
      notifications.show({ color: "red", message: "No Tapis session — log in with a real Tapis account to save." });
      return;
    }
    setSaving(true);
    try {
      await uploadTapisFile({
        system,
        path: outputPath,
        content: JSON.stringify({ annotation_type: annotationType, annotations: annotationsByFile }, null, 2),
        token: tapisToken,
      });
      notifications.show({ color: "green", message: `Saved annotations.json to ${outputPath}` });
    } catch (err: any) {
      notifications.show({ color: "red", title: "Save failed", message: err?.message || "Could not save" });
    } finally {
      setSaving(false);
    }
  };

  if (!imageInputPort || !sourceDir) {
    return (
      <Stack align="center" justify="center" style={{ height: "100%" }} p="xl">
        <Text c="dimmed">Connect an image directory source to this step's "images" input to begin labeling.</Text>
      </Stack>
    );
  }

  if (tokenError) {
    return (
      <Stack align="center" justify="center" style={{ height: "100%" }} p="xl">
        <Text c="red">{tokenError}</Text>
        <Text c="dimmed" size="sm">Log in with a real Tapis account to browse and label images.</Text>
      </Stack>
    );
  }

  if (!mounted || !libs || !tapisToken) {
    return (
      <Group justify="center" p="xl" style={{ height: "100%" }}>
        <Loader />
      </Group>
    );
  }

  const { FileExplorer, AnnotationDetails, ImageCanvas, detectionEngine, segmentationEngine } = libs;
  const engine = annotationType === "segmentation" ? segmentationEngine : detectionEngine;
  const labeledCount = Object.values(annotationsByFile).filter((a) => a.length > 0).length;

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <Paper shadow="xs" p={8} radius={0} withBorder style={{ borderLeft: 0, borderRight: 0, borderTop: 0 }}>
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <Tooltip label={fileDrawerOpen ? "Hide files" : "Show files"}>
              <ActionIcon
                variant={fileDrawerOpen ? "filled" : "light"}
                size="lg"
                onClick={() => setFileDrawerOpen((o) => !o)}
              >
                <IconLayoutSidebarLeftExpand size={18} />
              </ActionIcon>
            </Tooltip>
            <Select
              size="xs"
              placeholder="Tapis system"
              data={TAPIS_SYSTEMS}
              value={system}
              onChange={(v) => setField("system", v ?? "")}
              allowDeselect={false}
              w={170}
              comboboxProps={{ withinPortal: true, zIndex: 10002 }}
            />
            <SegmentedControl
              size="xs"
              value={annotationType}
              onChange={(v) => setField("annotation_type", v)}
              data={[
                { label: "Boxes", value: "detection" },
                { label: "Masks", value: "segmentation" },
              ]}
            />
            <TextInput
              size="xs"
              placeholder="/path/to/annotations.json"
              value={outputPath}
              onChange={(e) => setField("path", e.currentTarget.value)}
              w={240}
            />
          </Group>
          <Group gap="xs" wrap="nowrap">
            <Text size="xs" c="dimmed">
              {labeledCount} / {imageFiles.length || "?"} images labeled
            </Text>
            <Tooltip label={detailsDrawerOpen ? "Hide annotation details" : "Show annotation details"}>
              <ActionIcon
                variant={detailsDrawerOpen ? "filled" : "light"}
                size="lg"
                onClick={() => setDetailsDrawerOpen((o) => !o)}
              >
                <IconLayoutSidebarRightExpand size={18} />
              </ActionIcon>
            </Tooltip>
            <Button
              size="xs"
              leftSection={<IconDeviceFloppy size={14} />}
              loading={saving}
              onClick={saveAnnotations}
            >
              Save annotations.json
            </Button>
          </Group>
        </Group>
      </Paper>

      <div style={{ flex: 1, position: "relative", minHeight: 0 }}>
        <Drawer
          opened={fileDrawerOpen}
          onClose={() => setFileDrawerOpen(false)}
          position="left"
          size={300}
          title="Files"
          withOverlay={false}
          closeOnClickOutside={false}
          closeOnEscape={false}
          trapFocus={false}
          lockScroll={false}
          zIndex={1000}
          className="nokey"
        >
          <FileExplorer
            token={tapisToken}
            pipeid={`smart-labeler-${nodeId}`}
            fileDir={sourceDir}
            parentSystem={system}
            onFileSelect={(imageUrl, filePath) => {
              setCurrent({ url: imageUrl, path: filePath });
              setSelectedId(null);
              setSelectedIds([]);
            }}
            filesInDirectory={(files) => setImageFiles(files)}
          />
        </Drawer>

        {/* The drawers are position:fixed overlays (Mantine Drawer doesn't
            participate in layout flow), so this pane's own left/right insets
            are adjusted manually to match — otherwise it stays full-width
            underneath and the canvas ends up hidden behind an open drawer
            instead of just losing that much space. Transition matches
            Mantine's own default drawer slide duration (200ms) so both
            animate together. */}
        <div
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            left: fileDrawerOpen ? 300 : 0,
            right: detailsDrawerOpen ? 340 : 0,
            transition: "left 200ms ease, right 200ms ease",
          }}
        >
          {current?.url ? (
            <ImageCanvas
              engine={engine}
              file={current.url}
              fileName={current.path}
              systemId={system}
              pipeId={`smart-labeler-${nodeId}`}
              annotations={visibleAnnotations}
              isEditable
              selectedAnnotationId={selectedId}
              onSelection={setSelectedId}
              selectedAnnotationIds={selectedIds}
              onMultiSelection={setSelectedIds}
              onAddition={(added: Anno[]) => setAnnotationsForCurrent([...annotations, ...added])}
              onUpdate={(id: string, updates: Partial<Anno>) =>
                setAnnotationsForCurrent(annotations.map((a) => (a.id === id ? { ...a, ...updates } : a)))
              }
              deleteAnnotations={(ids: string[]) =>
                setAnnotationsForCurrent(annotations.filter((a) => !ids.includes(a.id)))
              }
              setFileSize={() => {}}
              isGraphEnabled={false}
              score={0}
              activeLabels={filter.activeLabels.length ? filter.activeLabels : undefined}
              activeFlags={filter.activeFlags.length ? filter.activeFlags : undefined}
              sam3Endpoint={SAM3_ENDPOINT || undefined}
              tapisToken={tapisToken}
            />
          ) : (
            <Group justify="center" align="center" style={{ height: "100%" }}>
              <Text c="dimmed">Select an image from the browser on the left.</Text>
            </Group>
          )}
        </div>

        <Drawer
          opened={detailsDrawerOpen}
          onClose={() => setDetailsDrawerOpen(false)}
          position="right"
          size={340}
          title="Annotation Details"
          withOverlay={false}
          closeOnClickOutside={false}
          closeOnEscape={false}
          trapFocus={false}
          lockScroll={false}
          zIndex={1000}
          className="nokey"
        >
          <AnnotationDetails
            variant={annotationType}
            annotations={annotations}
            selectedBoxId={selectedId ?? undefined}
            onSelectedBoxChange={(id) => setSelectedId(id ?? null)}
            selectedBoxIds={selectedIds}
            onSelectedBoxIdsChange={setSelectedIds}
            onAnnotationUpdate={(id, updates) =>
              setAnnotationsForCurrent(annotations.map((a) => (a.id === id ? { ...a, ...updates } : a)))
            }
            deleteAnnotations={(ids) => setAnnotationsForCurrent(annotations.filter((a) => !ids.includes(a.id)))}
            handleFilterAnnotations={(minScore, activeLabels, activeFlags) => setFilter({ minScore, activeLabels, activeFlags })}
          />
        </Drawer>
      </div>
    </div>
  );
}

// This panel renders its own full-height 3-pane layout (browser / canvas /
// details), so give it the whole screen (see StepSettingsModal, which honors
// this static flag).
(SmartLabelerPanel as any).fullScreen = true;
