import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps, ComponentType, ReactElement } from "react";
import { Group, TextInput, Button, Loader, Select, Paper, SegmentedControl, Text, Stack, Drawer, ActionIcon, Tooltip, Badge, Modal } from "@mantine/core";
import { IconDeviceFloppy, IconLayoutSidebarLeftExpand, IconLayoutSidebarRightExpand, IconFileDownload } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import type { StepPanelProps } from "./types";
import { BACKEND_URL, SAM3_ENDPOINT } from "../lib/api";
import { TAPIS_SYSTEMS, DEFAULT_TAPIS_SYSTEM, resolveWiredLocation } from "../lib/tapis";
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

// Stable reference for "no annotations for this image" — a fresh [] literal
// on every render would otherwise make the `annotations` useMemo below (see
// toEngineShape) recompute every render even when nothing changed.
const EMPTY_ANNOTATIONS: Anno[] = [];

// Reshape one annotation to whatever `mode` ("detection" | "segmentation")
// needs, synthesizing the missing half from whichever half IS present rather
// than leaving it undefined (see the crash this works around, at its call
// site below). A round-trip through the "wrong" mode's synthesized shape is
// harmless — editing there and switching back re-derives the other half from
// whatever was actually edited.
function toEngineShape(a: Anno, mode: "detection" | "segmentation"): Anno {
  if (mode === "segmentation") {
    if (Array.isArray(a.points)) return a;
    const x = a.x ?? 0, y = a.y ?? 0, w = a.width ?? 0, h = a.height ?? 0;
    return { ...a, points: [{ x, y }, { x: x + w, y }, { x: x + w, y: y + h }, { x, y: y + h }] };
  }
  if (typeof a.x === "number" && typeof a.width === "number") return a;
  const pts: { x: number; y: number }[] = Array.isArray(a.points) ? a.points : [];
  if (pts.length === 0) return { ...a, x: 0, y: 0, width: 0, height: 0 };
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const minX = Math.min(...xs), minY = Math.min(...ys), maxX = Math.max(...xs), maxY = Math.max(...ys);
  return { ...a, x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

interface Libs {
  FileExplorer: ComponentType<FileExplorerProps>;
  AnnotationDetails: ComponentType<AnnotationDetailsProps>;
  ImageCanvas: (props: ImageCanvasProps<any>) => ReactElement;
  detectionEngine: CanvasEngine<any>;
  segmentationEngine: CanvasEngine<any>;
}

export default function SmartLabelerPanel({ config, onChange, step, nodeId, connectedInputs, onSave }: StepPanelProps) {
  // Render only after mount so the canvas/MUI/Tapis-direct packages never run
  // during server render (see docs/adding-a-step-custom-ui.md §5).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const field = (key: string) => String(config[key] ?? step.config_schema[key]?.default ?? "");
  const setField = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  // The image directory this step labels comes from its wired 'images' input
  // (an upstream source_image_dir node), not its own config — see step.json.
  // resolveWiredLocation pulls BOTH the path and ITS system off the wire
  // (CustomNode's resolveOutputPath returns a full tapis://system/path URI
  // for a wired source-like node) rather than assuming it shares this node's
  // own `system` field below.
  const imageInputPort = step.inputs.find((p) => p.data_type === "image_dir")?.port_name;
  const wiredImages = imageInputPort ? resolveWiredLocation(connectedInputs[imageInputPort]) : null;
  const sourceDir = wiredImages?.path ?? "";
  const sourceSystem = wiredImages?.system ?? "";

  // Optional 'resume_annotations' input — lets this node resume labeling from
  // an existing annotations.json (e.g. wired to another smart_labeler's
  // 'annotations' output, an annotation_format_adapter's 'converted' output,
  // or a source_json_file node) instead of starting blank. Like
  // connectedInputs generally, this only resolves for a directly wired
  // DESIGN-TIME node (one with its own system/path config) — an upstream JOB
  // step's output is a runtime artifact and isn't available here (see
  // StepPanelProps.ConnectedInput).
  //
  // Deliberately NOT named 'annotations' — see step.json: an output port is
  // treated as a pure passthrough of an input port of the SAME NAME
  // (resolveOutputPath / _source_node_outputs), which the 'images' output
  // relies on intentionally. Sharing that name with this input previously
  // made the 'annotations' OUTPUT stop exposing this node's own saved
  // system/path and try to resolve through this (usually unwired) input
  // instead — resolving to nothing, i.e. "not passing on anything."
  const annotationsInputPort = step.inputs.find((p) => p.port_name === "resume_annotations")?.port_name;
  const wiredAnnotations = annotationsInputPort ? resolveWiredLocation(connectedInputs[annotationsInputPort]) : null;
  const wiredAnnotationsSystem = wiredAnnotations?.system ?? "";
  const wiredAnnotationsPath = wiredAnnotations?.path ?? "";

  // The system this node's OWN output (annotations.json) is saved to. Once
  // the user has explicitly picked one (via the toolbar Select below) that
  // choice always wins; until then it defaults to wherever this node's data
  // actually already lives — the wired annotations input (continuing a prior
  // labeling session, e.g. from annotation_format_adapter) if there is one,
  // else the wired images input — rather than silently falling back to the
  // unrelated global DEFAULT_TAPIS_SYSTEM, which risked saving to a system
  // that had nothing to do with this node's actual data.
  const system = field("system") || wiredAnnotationsSystem || sourceSystem || DEFAULT_TAPIS_SYSTEM;
  const outputPath = field("path");
  const annotationType = (config.annotation_type === "segmentation" ? "segmentation" : "detection") as
    | "detection"
    | "segmentation";

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
  // A file's KEY here isn't necessarily one THIS FileExplorer instance ever
  // produced — e.g. loaded from the wired 'annotations' input (see
  // loadAnnotationsFromInput below), where the key is whatever "image_path"/
  // "file_name" convention the ORIGINAL producer used (annotation_format_adapter
  // just carries it forward verbatim). A leading-slash or absolute-vs-
  // relative-to-image-dir difference from THIS browser's own path convention
  // means an exact lookup finds nothing even though the annotations are
  // genuinely for this image — matching the "API pulled the file, but
  // nothing shows" symptom. findAnnotationKey falls back to a normalized /
  // basename match before giving up.
  const matchedAnnotationKey = findAnnotationKey(annotationsByFile, currentPath);
  const rawAnnotations = matchedAnnotationKey ? annotationsByFile[matchedAnnotationKey] : EMPTY_ANNOTATIONS;
  // Reshape to whatever the CURRENT engine (detectionEngine/segmentationEngine,
  // picked below from annotationType) actually needs. Toggling the Boxes/Masks
  // SegmentedControl only swaps which engine renders — it doesn't touch the
  // stored data — so box-only annotations (x/y/width/height, no `points`,
  // e.g. loaded from zero_shot_annotation's flat format with no segmentation
  // data) reaching the segmentation engine crash its draw() deep inside
  // @icicle-ai/image-annotation-canvas (`Cannot read properties of undefined
  // (reading 'length')` — it assumes every annotation has `.points`). A
  // synthesized rectangle keeps the annotation visible/editable instead of
  // crashing or silently vanishing; the reverse direction derives a bounding
  // box from points for the same reason on the way back to Boxes mode.
  const annotations = useMemo(
    () => rawAnnotations.map((a) => toEngineShape(a, annotationType)),
    [rawAnnotations, annotationType]
  );
  // True once a fallback (non-exact) key match has actually been used for
  // some image this session — surfaced as a heads-up badge below, since a
  // silently "self-healed" key (see setAnnotationsForCurrent) is still worth
  // knowing about.
  const [usedFallbackMatch, setUsedFallbackMatch] = useState(false);
  useEffect(() => {
    if (matchedAnnotationKey && matchedAnnotationKey !== currentPath) setUsedFallbackMatch(true);
  }, [matchedAnnotationKey, currentPath]);

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
      const nextByFile = { ...annotationsByFile };
      // First edit after a fallback-matched load: fold the old (mismatched)
      // key's entry under the canonical currentPath going forward, instead
      // of leaving a stale duplicate sitting under the original key.
      if (matchedAnnotationKey && matchedAnnotationKey !== currentPath) delete nextByFile[matchedAnnotationKey];
      nextByFile[currentPath] = next;
      onChange({ ...config, annotations_by_file: nextByFile });
    },
    [config, annotationsByFile, currentPath, matchedAnnotationKey, onChange]
  );

  // Load annotations_by_file from the wired 'annotations' input, if any.
  // Shared by the auto-load-on-connect effect below and the manual "Load from
  // input" button — `notify` only shows a toast for the explicit button click,
  // since the auto-load runs on mount/reconnect and a silent success (or
  // silent no-op absence) matches this panel's other best-effort fetches.
  const [loadingAnnotationsInput, setLoadingAnnotationsInput] = useState(false);
  const loadAnnotationsFromInput = useCallback(
    async (notify: boolean) => {
      if (!wiredAnnotationsSystem || !wiredAnnotationsPath) {
        if (notify) notifications.show({ color: "yellow", message: "Connect an annotations input first." });
        return;
      }
      setLoadingAnnotationsInput(true);
      try {
        const res = await studioFetch(
          `/api/tapis-files/content?system=${encodeURIComponent(wiredAnnotationsSystem)}&path=${encodeURIComponent(wiredAnnotationsPath)}`
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const parsed = parseLoadedAnnotations(data);
        if (!parsed) {
          throw new Error(
            "File doesn't look like a smart_labeler annotations.json or a zero_shot_annotation flat annotations file"
          );
        }
        onChange({
          ...config,
          annotations_by_file: parsed.byFile,
          annotation_type: parsed.annotationType === "segmentation" ? "segmentation" : config.annotation_type,
        });
        if (notify) notifications.show({ color: "green", message: "Loaded annotations from the wired input." });
      } catch (err: any) {
        if (notify) {
          notifications.show({ color: "red", title: "Load failed", message: err?.message || "Could not load annotations" });
        }
      } finally {
        setLoadingAnnotationsInput(false);
      }
    },
    [wiredAnnotationsSystem, wiredAnnotationsPath, config, onChange]
  );

  // Auto-load once, only when this node has no annotations yet — resumes a
  // labeling session wired to a prior run's output without a manual step, but
  // never silently clobbers work already done in this session.
  const autoLoadedRef = useRef(false);
  useEffect(() => {
    if (autoLoadedRef.current) return;
    if (!wiredAnnotationsSystem || !wiredAnnotationsPath) return;
    if (Object.keys(annotationsByFile).length > 0) return;
    autoLoadedRef.current = true;
    loadAnnotationsFromInput(false);
  }, [wiredAnnotationsSystem, wiredAnnotationsPath, annotationsByFile, loadAnnotationsFromInput]);

  // Persisting a config change to the NODE itself (so a connected downstream
  // step can actually see it — e.g. zeroShotAnnotation.tsx's exemplar-prompt
  // auto-fill) is a SEPARATE action from onChange: onChange only updates
  // this settings modal's own WORKING COPY (StepSettingsModal's local
  // `config` state) — nothing reaches the node/DB until the modal's own
  // "Save & Close" button fires onSave(), which this panel previously never
  // called at all. That gap meant "Save annotations.json" (uploads to Tapis,
  // updates the working copy) looked complete but went nowhere for a
  // connected step unless the user ALSO remembered to click the separate
  // outer Save button before closing.
  //
  // schedulePersist defers the actual onSave() call to an effect keyed off a
  // version bump, so it always runs AFTER any onChange() in the same handler
  // has actually landed (StepSettingsModal re-rendered with the new config
  // and handed this panel a FRESH onSave closure reading it) — calling
  // onSave() synchronously right after onChange() would still close over the
  // modal's PREVIOUS config, one render behind.
  const [persistVersion, setPersistVersion] = useState(0);
  const pendingPersistRef = useRef(false);
  const schedulePersist = () => {
    pendingPersistRef.current = true;
    setPersistVersion((v) => v + 1);
  };
  useEffect(() => {
    if (!pendingPersistRef.current) return;
    pendingPersistRef.current = false;
    onSave();
  }, [persistVersion, onSave]);

  // Save-destination prompt ("Save As"): shown when Save is clicked with no
  // system/path configured yet, instead of just a toast telling the user to
  // go fill in the (easy-to-miss) toolbar fields themselves. Takes explicit
  // system/path params rather than reading `system`/`outputPath` off state —
  // when called from the modal's own confirm handler, those are the
  // just-typed values, which haven't round-tripped through onChange/config
  // yet and so wouldn't be visible on `system`/`outputPath` within the same
  // tick.
  const [saving, setSaving] = useState(false);
  const [saveAsOpen, setSaveAsOpen] = useState(false);
  const [saveAsSystem, setSaveAsSystem] = useState("");
  const [saveAsPath, setSaveAsPath] = useState("");
  const [saveAsError, setSaveAsError] = useState<string | null>(null);

  const doSave = async (saveSystem: string, savePath: string): Promise<boolean> => {
    if (!tapisToken) {
      notifications.show({ color: "red", message: "No Tapis session — log in with a real Tapis account to save." });
      return false;
    }
    setSaving(true);
    try {
      await uploadTapisFile({
        system: saveSystem,
        path: savePath,
        content: JSON.stringify({ annotation_type: annotationType, annotations: annotationsByFile }, null, 2),
        token: tapisToken,
      });
      notifications.show({ color: "green", message: `Saved annotations.json to ${savePath}` });
      return true;
    } catch (err: any) {
      const message = err?.message || "Could not save";
      notifications.show({ color: "red", title: "Save failed", message });
      setSaveAsError(message);
      return false;
    } finally {
      setSaving(false);
    }
  };

  // Main Save button: save straight away if a destination is already
  // configured (the common case — same path every time); otherwise prompt
  // for one first, per the same "system along with path" pairing the output
  // port itself relies on (see step.json's resume_annotations note). Either
  // way, a successful upload immediately persists the node config too — see
  // schedulePersist — so a connected downstream step sees the destination
  // right away, with no separate "now also click Save Configuration" step.
  const saveAnnotations = async () => {
    if (system && outputPath) {
      const ok = await doSave(system, outputPath);
      // Write the system back into config, not just the file to Tapis. `system`
      // above is DERIVED (field("system") || wiredAnnotationsSystem ||
      // sourceSystem || DEFAULT_TAPIS_SYSTEM) — so unless the user actually
      // touched the toolbar Select, config.system stayed unset even though the
      // toolbar displayed a system and the upload used it. That left a saved
      // node holding a `path` and no `system`, which silently breaks this
      // node's 'annotations' OUTPUT for every consumer: both CustomNode's
      // resolveOutputPath (design time) and _source_node_outputs (run time)
      // only build a tapis://system/path URI when BOTH are present, so the
      // port resolved to a bare path — and resolveWiredLocation returns null
      // for a bare path with no system, so a downstream panel (e.g.
      // zeroShotAnnotation's Prompt file) saw nothing wired at all.
      // Persisting the effective system makes what was saved match what the
      // port advertises.
      if (ok) {
        onChange({ ...config, system, path: outputPath });
        schedulePersist();
      }
      return;
    }
    setSaveAsSystem(system || DEFAULT_TAPIS_SYSTEM);
    setSaveAsPath(outputPath);
    setSaveAsError(null);
    setSaveAsOpen(true);
  };

  const confirmSaveAs = async () => {
    if (!saveAsSystem || !saveAsPath) {
      setSaveAsError("Both a Tapis system and a path are required.");
      return;
    }
    setSaveAsError(null);
    const ok = await doSave(saveAsSystem, saveAsPath);
    if (!ok) return; // error already shown inline + toast; leave the modal open to retry
    // Update the working config (toolbar + the 'annotations' output port
    // other steps wire to) AND persist it to the node right away.
    onChange({ ...config, system: saveAsSystem, path: saveAsPath });
    schedulePersist();
    setSaveAsOpen(false);
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
            {usedFallbackMatch && (
              <Tooltip label="Loaded annotations' file paths didn't exactly match this browser's paths (e.g. loaded via the format adapter, which carries forward whatever path convention the original file used) — matched by filename instead. Editing an image folds its entry under this browser's own path going forward.">
                <Badge size="xs" variant="light" color="yellow">path-matched by filename</Badge>
              </Tooltip>
            )}
            <Tooltip label={detailsDrawerOpen ? "Hide annotation details" : "Show annotation details"}>
              <ActionIcon
                variant={detailsDrawerOpen ? "filled" : "light"}
                size="lg"
                onClick={() => setDetailsDrawerOpen((o) => !o)}
              >
                <IconLayoutSidebarRightExpand size={18} />
              </ActionIcon>
            </Tooltip>
            {annotationsInputPort && (
              <Tooltip
                label={
                  wiredAnnotationsPath
                    ? "Reload annotations_by_file from the wired 'annotations' input, overwriting anything unsaved here"
                    : "Connect an 'annotations' input to resume labeling from an existing annotations.json"
                }
              >
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconFileDownload size={14} />}
                  loading={loadingAnnotationsInput}
                  disabled={!wiredAnnotationsPath}
                  onClick={() => loadAnnotationsFromInput(true)}
                >
                  Load from input
                </Button>
              </Tooltip>
            )}
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
            parentSystem={sourceSystem}
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
              // Force a remount on Boxes/Masks toggle. @icicle-ai/image-annotation-canvas
              // reads `engine` straight from props (updates immediately) but
              // mirrors `annotations` into its OWN useState, re-synced only via
              // a passive useEffect one render later — so for exactly one draw
              // after switching modes, the NEW engine (e.g. segmentationEngine)
              // runs against the STILL-STALE internal annotations state from
              // the previous mode (box-shaped, no `.points`), crashing inside
              // its draw() (`Cannot read properties of undefined (reading
              // 'length')` at `ann.points.length`). A key keyed on mode forces
              // a fresh mount instead: its initial state reads directly from
              // our already-correctly-shaped `annotations` prop (see
              // toEngineShape above), with no lag to race against.
              key={annotationType}
              engine={engine}
              file={current.url}
              fileName={current.path}
              systemId={sourceSystem}
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

      <Modal
        opened={saveAsOpen}
        onClose={() => setSaveAsOpen(false)}
        title="Save annotations.json"
        centered
      >
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            No destination is set yet. Pick a Tapis system and path to save (and reuse for every save going
            forward) — the same location this node's "annotations" output exposes to any connected step.
          </Text>
          <Select
            label="Tapis system"
            data={TAPIS_SYSTEMS}
            value={saveAsSystem}
            onChange={(v) => setSaveAsSystem(v ?? "")}
            allowDeselect={false}
          />
          <TextInput
            label="Path"
            placeholder="/path/to/annotations.json"
            value={saveAsPath}
            onChange={(e) => setSaveAsPath(e.currentTarget.value)}
          />
          {saveAsError && (
            <Text size="sm" c="red">{saveAsError}</Text>
          )}
          <Group justify="flex-end" mt="sm">
            <Button variant="default" onClick={() => setSaveAsOpen(false)}>Cancel</Button>
            <Button leftSection={<IconDeviceFloppy size={14} />} loading={saving} onClick={confirmSaveAs}>
              Save
            </Button>
          </Group>
        </Stack>
      </Modal>
    </div>
  );
}

// This panel renders its own full-height 3-pane layout (browser / canvas /
// details), so give it the whole screen (see StepSettingsModal, which honors
// this static flag).
(SmartLabelerPanel as any).fullScreen = true;

// Two shapes accepted for the wired 'annotations' input:
//
// 1. smart_labeler's own native format (what Save writes, and what this
//    accepts as the round-trip case):
//      { annotation_type, annotations: { file_path: [Anno, ...] } }
//    — `annotations` is an OBJECT keyed by file path.
//
// 2. zero_shot_annotation's own flat format (wired directly, no adapter in
//    between — its own output already matches this repo's general
//    annotations.json convention, just not smart_labeler's per-file-keyed one):
//      { ..., annotations: [{ image_path, bounding_box: [x1,y1,x2,y2],
//                              score, class, prompt_type, segmentation? }, ...] }
//    — `annotations` is a flat ARRAY, one record per detection, grouped here
//    by image_path. bounding_box is [x1, y1, x2, y2] (corners), converted to
//    smart_labeler's own x/y/width/height. `class` -> label, `prompt_type`
//    stashed on `flag` (filterable in the details panel same as any other
//    flag). A present `segmentation` (absolute-pixel [[x,y],...] outline)
//    is used instead of the box when non-empty.
//
// `typeof x === "object"` is true for arrays too, so shape (2) must be
// distinguished with Array.isArray BEFORE falling through to shape (1) —
// that mixup (silently accepting the array as if it were the file-keyed
// object) was the original "fetch succeeds, nothing displays" bug: every
// per-image lookup against an array indexed by file-path strings just never
// matches ANY key at all.
function parseLoadedAnnotations(data: any): { byFile: Record<string, Anno[]>; annotationType?: string } | null {
  if (!data) return null;

  if (Array.isArray(data.annotations)) {
    const byFile: Record<string, Anno[]> = {};
    let hasSegmentation = false;
    data.annotations.forEach((item: any, i: number) => {
      const path = item?.image_path;
      if (!path || typeof path !== "string") return;
      const anno: Anno = { id: `zs-${i}`, label: item.class ?? item.label ?? "object" };
      if (typeof item.score === "number") anno.score = item.score;
      if (item.prompt_type) anno.flag = String(item.prompt_type);
      const seg = item.segmentation;
      if (Array.isArray(seg) && seg.length > 0) {
        anno.points = seg.map((p: number[]) => ({ x: p[0], y: p[1] }));
        hasSegmentation = true;
      } else {
        const box = item.bounding_box || item.bbox;
        const [x1, y1, x2, y2] = Array.isArray(box) && box.length === 4 ? box : [0, 0, 0, 0];
        anno.x = x1;
        anno.y = y1;
        anno.width = x2 - x1;
        anno.height = y2 - y1;
      }
      (byFile[path] ||= []).push(anno);
    });
    if (Object.keys(byFile).length === 0) return null;
    return { byFile, annotationType: hasSegmentation ? "segmentation" : "detection" };
  }

  if (data.annotations && typeof data.annotations === "object") {
    return { byFile: data.annotations, annotationType: data.annotation_type };
  }

  return null;
}

// Find the annotations_by_file key that actually corresponds to `path`, the
// current image's path as THIS FileExplorer instance reports it. Tries, in
// order: an exact match; the same string with/without a leading slash
// (absolute-vs-relative mismatches are the most common real case); and
// finally a basename-only match, but ONLY when exactly one key in
// annotationsByFile shares that basename — with two candidates there's no
// way to tell which directory they actually meant, so it's safer to report
// no match than to silently guess wrong and show someone else's boxes.
// Returns null when nothing matches at all.
function findAnnotationKey(annotationsByFile: Record<string, unknown>, path: string): string | null {
  if (!path) return null;
  if (path in annotationsByFile) return path;
  const stripped = path.replace(/^\/+/, "");
  if (stripped !== path && stripped in annotationsByFile) return stripped;
  const withSlash = `/${stripped}`;
  if (withSlash in annotationsByFile) return withSlash;
  const basename = stripped.split("/").pop();
  if (basename) {
    const candidates = Object.keys(annotationsByFile).filter(
      (k) => k.replace(/^\/+/, "").split("/").pop() === basename
    );
    if (candidates.length === 1) return candidates[0];
  }
  return null;
}
