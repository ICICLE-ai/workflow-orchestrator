import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentType } from "react";
import {
  Group, Select, Text, Loader, Stack, Paper, ScrollArea, Badge, UnstyledButton,
  Checkbox, Anchor, Divider,
} from "@mantine/core";
import { IconDownload } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch, BACKEND_URL } from "../lib/api";
import { resolveWiredLocation } from "../lib/tapis";
import {
  missionFileToPoints, missionJsonToPoints, listMissionFiles,
  missionPointsToFeatureCollection, sampleDirectionArrows,
} from "../lib/missionFormats";
import type { MissionFileOption } from "../lib/missionFormats";

// Type-only imports — erased at build, so Leaflet (manipulates window/document
// directly) never loads during SSR. The runtime module is loaded lazily below.
import type { MapContainerProps, TileLayerProps, GeoJSONProps, MarkerProps } from "react-leaflet";
import type * as LeafletNS from "leaflet";

// Marker icon assets — plain asset URLs (no JS execution), safe to import
// eagerly. Kept even though detections render as circle markers (not the
// default pin icon) — react-leaflet's Icon.Default is still touched by
// L.geoJSON's bounds/marker machinery and breaks under bundlers unless these
// are supplied explicitly (see the original shapefileMap.tsx for the same note).
import markerIcon2x from "leaflet/dist/images/marker-icon-2x.png?url";
import markerIcon from "leaflet/dist/images/marker-icon.png?url";
import markerShadow from "leaflet/dist/images/marker-shadow.png?url";
import "leaflet/dist/leaflet.css";

// This panel renders every layer in a GeoPackage — server-side conversion via
// backend/geospatial.py (GeoPackage -> GeoJSON, GDAL/OGR). It applies special
// styling/legend treatment to three layer names the 'geospatial' step's
// GeoPackage is documented to produce (from drone detection results + source
// imagery EXIF GPS):
//   - detections    : point layer, one point per detected object
//   - spray_zones   : polygon grid layer, one cell per grid square, with a
//                     spray decision (binary or custom banded) based on
//                     detection counts
//   - farm_boundary : polygon layer, convex hull of all image locations
// Any OTHER layer present (e.g. a hand-supplied source_geopackage file, or a
// generic GeoPackage used for testing — the panel doesn't assume the file it's
// pointed at came from the 'geospatial' step) still renders, with a generic
// per-layer color instead of the special styling above.
//
// Data always comes from /api/geospatial-preview/... (a `uri` query param —
// see backend/geospatial.py's preview_router), never from the run-scoped
// /api/pipeline-runs/{run_id}/geospatial/... resource. That resource finds
// "any node in this run producing a geopackage", which breaks the moment a
// run has more than one (e.g. geospatial -> flight_plan, both producing a
// .gpkg) — it has no way to know which one THIS panel is wired to. Using the
// wired edge's own resolved URI instead (from CustomNode's resolveOutputPath
// at design time, or this run's resolved RunStep.config at run time — see
// runs.$runId.tsx's panelConnectedInputs) is unambiguous in both cases, so
// the run-scoped resource is only still used for the optional /config call
// (the spray-legend's colors/labels — cosmetic, never which data renders).
//
// Opened from the canvas before any run exists, this only has something to
// show when wired to a static source_geopackage node (whose path is already
// known); a 'geospatial' or 'flight_plan' job step has nothing to preview
// until it actually runs, so a placeholder shows in that case instead.
//
// Registered in registry.ts under the key "geospatial_map".
export default function GeospatialMapPanel({ step, connectedInputs, runId }: StepPanelProps) {
  const gpkgInputPort = step.inputs.find((p) => p.data_type === "geopackage")?.port_name;
  const wiredInput = gpkgInputPort ? connectedInputs[gpkgInputPort] : undefined;
  const isWired = !!wiredInput;

  // A mission overlay (path + waypoints) is entirely independent of the
  // GeoPackage layers above — either or both may be wired. "mission_json" is
  // the generic waypoint JSON a flight_plan/scouting_mission step produces
  // (rendered directly, no picker); "mission_files" is a directory of
  // exported per-format files (mission_export/scouting_mission) — .waypoints/
  // .plan/.kmz/.json, one file picked to preview. See lib/missionFormats.ts.
  const missionJsonPort = step.inputs.find((p) => p.port_name === "mission_json")?.port_name;
  const missionFilesPort = step.inputs.find((p) => p.port_name === "mission_files")?.port_name;
  const missionJsonWired = missionJsonPort ? connectedInputs[missionJsonPort] : undefined;
  const missionFilesWired = missionFilesPort ? connectedInputs[missionFilesPort] : undefined;
  const hasMissionSource = !!missionJsonWired || !!missionFilesWired;

  // The wired GeoPackage's own resolved location (a tapis://system/path URI)
  // — at design time via CustomNode's resolveOutputPath, or at run time via
  // this run's resolved RunStep.config (see runs.$runId.tsx's
  // panelConnectedInputs, built from _resolve_inputs's per-edge resolution).
  // Either way this is the ACTUAL wired edge's value, not a guess.
  //
  // Deliberately NOT using the run-scoped /api/pipeline-runs/{run_id}/geospatial/...
  // resource's own lookup here: that endpoint finds "any node in this run
  // that produces a geopackage", which breaks the moment a run has more than
  // one such node (e.g. geospatial -> flight_plan, both producing a .gpkg) —
  // it can't know which one THIS viewer is actually wired to. Going through
  // the wired URI directly (via /api/geospatial-preview/..., which just takes
  // a uri param) sidesteps that ambiguity entirely, in both modes.
  const previewUri = String(wiredInput?.config?.path || "");
  const hasData = !!previewUri;
  const apiBase = "/api/geospatial-preview";
  const previewQs = hasData ? `?uri=${encodeURIComponent(previewUri)}` : "";

  // Client-only load of Leaflet + react-leaflet (see the type-only imports above).
  const [libs, setLibs] = useState<Libs | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [rl, leafletMod] = await Promise.all([import("react-leaflet"), import("leaflet")]);
      const L = ((leafletMod as any).default ?? leafletMod) as typeof LeafletNS;
      delete (L.Icon.Default.prototype as any)._getIconUrl;
      L.Icon.Default.mergeOptions({ iconRetinaUrl: markerIcon2x, iconUrl: markerIcon, shadowUrl: markerShadow });
      if (!cancelled) {
        setLibs({ MapContainer: rl.MapContainer as any, TileLayer: rl.TileLayer, GeoJSON: rl.GeoJSON as any, Marker: rl.Marker as any, L });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [layerData, setLayerData] = useState<Record<string, any>>({});
  const [skippedLayers, setSkippedLayers] = useState<string[]>([]);
  const [runConfig, setRunConfig] = useState<RunGeoConfig | null>(null);
  const [missingCount, setMissingCount] = useState(0);
  const [visible, setVisible] = useState<Record<string, boolean>>({});
  const [sidebarLayer, setSidebarLayer] = useState<string>("");

  // --- mission overlay: load + parse, independent of the gpkg layers -----
  const [missionFileOptions, setMissionFileOptions] = useState<MissionFileOption[]>([]);
  const [selectedMissionPath, setSelectedMissionPath] = useState<string>("");
  const [missionFeatures, setMissionFeatures] = useState<any | null>(null);
  const [missionLoading, setMissionLoading] = useState(false);
  const [missionError, setMissionError] = useState<string | null>(null);
  const [missionVisible, setMissionVisible] = useState(true);

  const missionFilesLoc = missionFilesWired ? resolveWiredLocation(missionFilesWired) : null;
  const missionFilesLocKey = missionFilesLoc ? `${missionFilesLoc.system}:${missionFilesLoc.path}` : "";

  // List the wired mission_files directory whenever it (re)resolves, and
  // default the picker to the first file found.
  useEffect(() => {
    setMissionFileOptions([]);
    setSelectedMissionPath("");
    if (!missionFilesLoc) return;
    let cancelled = false;
    listMissionFiles(missionFilesLoc.system, missionFilesLoc.path)
      .then((files) => {
        if (cancelled) return;
        setMissionFileOptions(files);
        if (files.length > 0) setSelectedMissionPath(files[0].path);
      })
      .catch((err) => { if (!cancelled) setMissionError(err?.message || "Could not list mission files"); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missionFilesLocKey]);

  const missionJsonLoc = missionJsonWired ? resolveWiredLocation(missionJsonWired) : null;
  const missionJsonLocKey = missionJsonLoc ? `${missionJsonLoc.system}:${missionJsonLoc.path}` : "";
  const selectedMissionFile = missionFileOptions.find((f) => f.path === selectedMissionPath) || null;

  // Load + parse whichever mission source is actually active: the directly
  // wired mission_json (no picker involved), or the currently-selected file
  // out of a wired mission_files directory.
  useEffect(() => {
    setMissionError(null);
    if (!missionJsonLoc && !selectedMissionFile) {
      setMissionFeatures(null);
      return;
    }
    let cancelled = false;
    setMissionLoading(true);
    (async () => {
      try {
        const loc = missionJsonLoc || { system: selectedMissionFile!.system, path: selectedMissionFile!.path };
        const res = await apiFetch(`/api/tapis-files/content?system=${encodeURIComponent(loc.system)}&path=${encodeURIComponent(loc.path)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const buf = await res.arrayBuffer();
        const points = missionJsonLoc
          ? missionJsonToPoints(JSON.parse(new TextDecoder("utf-8").decode(buf)))
          : await missionFileToPoints(selectedMissionFile!.ext, buf);
        if (!cancelled) setMissionFeatures(missionPointsToFeatureCollection(points));
      } catch (err: any) {
        if (!cancelled) { setMissionError(err?.message || "Could not load mission file"); setMissionFeatures(null); }
      } finally {
        if (!cancelled) setMissionLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missionJsonLocKey, selectedMissionPath]);

  useEffect(() => {
    if (!isWired || !hasData) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    setRunConfig(null);
    (async () => {
      try {
        const labelsRes = await apiFetch(`${apiBase}/labels${previewQs}`);
        if (!labelsRes.ok) {
          const e = await labelsRes.json().catch(() => ({}));
          throw new Error(e.detail || `HTTP ${labelsRes.status}`);
        }
        const { labels } = await labelsRes.json();
        // Render every layer the GeoPackage actually has — not just the three
        // the 'geospatial' step is documented to produce. A hand-supplied
        // source_geopackage file (or any GeoPackage used to test this panel)
        // can have any layer names at all; those still render, just without
        // the special styling farm_boundary/spray_zones/detections get below.
        const allLabels: string[] = labels || [];
        const present = [...allLabels].sort(layerDrawOrder);

        // Spray-mode config for the legend is a best-effort nicety only,
        // fetched from the run-scoped resource (which has no way to target
        // this specific wired node — see the comment above previewUri). In a
        // run with more than one geopackage-producing step this may reflect
        // the wrong one's config; that only affects the legend's colors/labels,
        // never which data actually renders, so it's left as a soft best-effort.
        const [cfgRes, missingRes, ...geojsonResults] = await Promise.all([
          runId ? apiFetch(`/api/pipeline-runs/${runId}/geospatial/config`) : Promise.resolve(null),
          apiFetch(`${apiBase}/missing${previewQs}`),
          ...present.map((label) => apiFetch(`${apiBase}/geojson/${encodeURIComponent(label)}${previewQs}`)),
        ]);

        if (cancelled) return;

        if (cfgRes && cfgRes.ok) setRunConfig(await cfgRes.json());
        if (missingRes.ok) {
          const m = await missingRes.json();
          setMissingCount(Array.isArray(m.missing) ? m.missing.length : 0);
        }

        const data: Record<string, any> = {};
        const skipped: string[] = [];
        for (let i = 0; i < present.length; i++) {
          if (geojsonResults[i].ok) data[present[i]] = await geojsonResults[i].json();
          else skipped.push(present[i]);
        }
        if (!cancelled) {
          const loaded = Object.keys(data);
          setLayerData(data);
          setSkippedLayers(skipped);
          setVisible(Object.fromEntries(loaded.map((l) => [l, true])));
          setSidebarLayer(loaded.find((l) => l === "detections") || loaded[0] || "");
        }
      } catch (err: any) {
        if (!cancelled) setLoadError(err?.message || "Could not load the geospatial layers");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [isWired, hasData, runId, apiBase, previewQs]);

  const levels = useMemo(
    () => (runConfig?.levels || "").split(/\s+/).filter(Boolean),
    [runConfig]
  );
  const thresholds = useMemo(
    () => (runConfig?.thresholds || "").split(/\s+/).filter(Boolean),
    [runConfig]
  );
  const sprayMode = runConfig?.sprayMode || "binary";

  const mapRef = useRef<any>(null);
  const layerRefs = useRef<Record<string, any>>({});

  // Fit the map to the union of every loaded gpkg layer PLUS the mission
  // overlay (if any), once loaded.
  useEffect(() => {
    if (!libs || !mapRef.current) return;
    const collections = [...Object.values(layerData), missionFeatures].filter(Boolean);
    if (collections.length === 0) return;
    const merged = { type: "FeatureCollection" as const, features: collections.flatMap((fc: any) => fc.features || []) };
    const bounds = libs.L.geoJSON(merged as any).getBounds();
    if (bounds.isValid()) mapRef.current.fitBounds(bounds, { padding: [20, 20], maxZoom: 19 });
  }, [libs, layerData, missionFeatures]);

  const onEachFeature = useCallback((feature: any, layer: any, layerKey: string, idx: number) => {
    const id = `${layerKey}-${idx}`;
    layerRefs.current[id] = layer;
    layer.bindPopup(buildPopup(feature.properties || {}));
  }, []);

  const focusFeature = (layerKey: string, idx: number) => {
    const id = `${layerKey}-${idx}`;
    const layer = layerRefs.current[id];
    const map = mapRef.current;
    if (!layer || !map) return;
    layer.openPopup?.();
    if (typeof layer.getBounds === "function") {
      const bounds = layer.getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 19 });
    } else if (typeof layer.getLatLng === "function") {
      map.setView(layer.getLatLng(), 19);
    }
  };

  if (!gpkgInputPort && !missionJsonPort && !missionFilesPort) {
    return (
      <Stack align="center" justify="center" style={{ height: "100%" }} p="xl">
        <Text c="dimmed">This step type has no geospatial inputs configured.</Text>
      </Stack>
    );
  }

  if (!isWired && !hasMissionSource) {
    return (
      <Stack align="center" justify="center" style={{ height: "100%" }} p="xl">
        <Text c="dimmed">
          Connect a GeoPackage, a mission JSON (flight_plan/scouting_mission), or a mission files
          output (mission_export/scouting_mission) to this step to visualize it.
        </Text>
      </Stack>
    );
  }

  if (!hasData && !hasMissionSource) {
    return (
      <Stack align="center" justify="center" style={{ height: "100%" }} p="xl">
        <Text c="dimmed">
          This step's GeoPackage isn't available yet — run the workflow to generate it
          (or wire a "GeoPackage" source step here to preview an existing file directly).
        </Text>
      </Stack>
    );
  }

  if (!libs) {
    return (
      <Group justify="center" p="xl" style={{ height: "100%" }}>
        <Loader />
      </Group>
    );
  }

  const { MapContainer, TileLayer, GeoJSON, Marker } = libs;
  const presentLayers = Object.keys(layerData).sort(layerDrawOrder);
  const sidebarFeatures = (layerData[sidebarLayer]?.features || []).map((f: any, i: number) => ({
    idx: i,
    geomType: f.geometry?.type || "Unknown",
    coords: representativeCoords(f.geometry),
    properties: f.properties || {},
  }));

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <Paper shadow="xs" p={8} radius={0} withBorder style={{ borderLeft: 0, borderRight: 0, borderTop: 0 }}>
        <Group justify="space-between" wrap="nowrap">
          <Group gap="md" wrap="nowrap">
            {loading && <Loader size="xs" />}
            {loadError && <Text size="xs" c="red">{loadError}</Text>}
            {!loading && !loadError && presentLayers.length === 0 && (
              <Text size="xs" c="dimmed">No layers in this GeoPackage.</Text>
            )}
            {!loading && !loadError && presentLayers.map((l) => (
              <Checkbox
                key={l}
                size="xs"
                label={layerLabel(l)}
                checked={!!visible[l]}
                onChange={(e) => {
                  // Read .checked synchronously — the native ChangeEvent's
                  // currentTarget is reset to null once dispatch finishes, and
                  // the functional setState updater below can run after that
                  // point, not during the event itself.
                  const checked = e.currentTarget.checked;
                  setVisible((v) => ({ ...v, [l]: checked }));
                }}
              />
            ))}
            {missionLoading && <Loader size="xs" />}
            {missionError && <Text size="xs" c="red">{missionError}</Text>}
            {missionFeatures && (
              <Checkbox
                size="xs"
                label="Mission path"
                checked={missionVisible}
                onChange={(e) => setMissionVisible(e.currentTarget.checked)}
              />
            )}
          </Group>
          <Group gap="sm" wrap="nowrap">
            {missingCount > 0 && (
              <Badge size="xs" color="orange" variant="light">{missingCount} missing source image(s)</Badge>
            )}
            {skippedLayers.length > 0 && (
              <Badge size="xs" color="gray" variant="light" title={skippedLayers.join(", ")}>
                {skippedLayers.length} layer(s) couldn't be converted
              </Badge>
            )}
            {presentLayers.map((l) => (
              <Anchor
                key={l}
                href={downloadUrl(apiBase, previewQs, l === "farm_boundary" ? "download-farm" : "download", l)}
                target="_blank"
                rel="noreferrer"
                size="xs"
              >
                <Group gap={4} wrap="nowrap"><IconDownload size={12} />{layerLabel(l)} (.zip)</Group>
              </Anchor>
            ))}
            {presentLayers.length > 0 && (
              <Anchor
                href={downloadUrl(apiBase, previewQs, "download-gpkg", runId ? `run_${runId}` : "geospatial")}
                target="_blank"
                rel="noreferrer"
                size="xs"
              >
                <Group gap={4} wrap="nowrap"><IconDownload size={12} />GeoPackage (.gpkg)</Group>
              </Anchor>
            )}
          </Group>
        </Group>
      </Paper>

      {sprayMode && layerData.spray_zones && (
        <SprayLegend sprayMode={sprayMode} levels={levels} thresholds={thresholds} />
      )}

      {missionFileOptions.length > 0 && (
        <Group gap="sm" px="sm" py={4} style={{ borderBottom: "1px solid #e2e8f0" }}>
          <Text size="xs" c="dimmed">Mission file:</Text>
          <Select
            size="xs"
            w={280}
            data={missionFileOptions.map((f) => ({ value: f.path, label: f.label }))}
            value={selectedMissionPath}
            onChange={(v) => v && setSelectedMissionPath(v)}
            allowDeselect={false}
            comboboxProps={{ withinPortal: true, zIndex: 10002 }}
          />
        </Group>
      )}

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <MapContainer ref={mapRef} center={[0, 0]} zoom={2} style={{ height: "100%", width: "100%" }}>
            <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} maxZoom={19} />
            {presentLayers.map((l) => {
              if (!visible[l]) return null;
              const fc = layerData[l];
              const onEach = (f: any, ly: any) => onEachFeature(f, ly, l, fc.features.indexOf(f));

              if (l === "farm_boundary") {
                return <GeoJSON key={l} data={fc} style={() => FARM_BOUNDARY_STYLE} onEachFeature={onEach} />;
              }
              if (l === "spray_zones") {
                return (
                  <GeoJSON
                    key={l}
                    data={fc}
                    style={(f: any) => ({ color: "#475569", weight: 1, fillOpacity: 0.55, fillColor: sprayZoneColor(f?.properties || {}, levels, sprayMode) })}
                    onEachFeature={onEach}
                  />
                );
              }
              if (l === "detections") {
                return (
                  <GeoJSON
                    key={l}
                    data={fc}
                    pointToLayer={(_f, latlng) => libs.L.circleMarker(latlng, DETECTION_MARKER_STYLE)}
                    onEachFeature={onEach}
                  />
                );
              }
              // Any other layer (a hand-supplied source_geopackage file, or a
              // generic GeoPackage used to test this panel) — a stable color
              // per layer name, generic enough for any geometry type.
              const color = genericLayerColor(l);
              return (
                <GeoJSON
                  key={l}
                  data={fc}
                  style={() => ({ color, weight: 2, fillColor: color, fillOpacity: 0.3 })}
                  pointToLayer={(_f, latlng) => libs.L.circleMarker(latlng, { radius: 5, color, fillColor: color, fillOpacity: 0.85, weight: 1 })}
                  onEachFeature={onEach}
                />
              );
            })}
            {missionVisible && missionFeatures && (
              <MissionLayer fc={missionFeatures} GeoJSON={GeoJSON} Marker={Marker} L={libs.L} />
            )}
          </MapContainer>
        </div>

        <div style={{ width: 320, borderLeft: "1px solid #e2e8f0", display: "flex", flexDirection: "column" }}>
          <Group justify="space-between" p="xs">
            <Text size="sm" fw={600}>Features</Text>
            <Select
              size="xs"
              w={150}
              data={presentLayers.map((l) => ({ value: l, label: layerLabel(l) }))}
              value={sidebarLayer}
              onChange={(v) => v && setSidebarLayer(v)}
              allowDeselect={false}
              comboboxProps={{ withinPortal: true, zIndex: 10002 }}
            />
          </Group>
          <Divider />
          <ScrollArea style={{ flex: 1 }} px="xs" pb="xs">
            {sidebarFeatures.length === 0 ? (
              <Text size="xs" c="dimmed" fs="italic" px="xs" mt="xs">
                {loading ? "Loading…" : "No features in this layer."}
              </Text>
            ) : (
              <Stack gap={4} mt="xs">
                {sidebarFeatures.map((row: any) => (
                  <UnstyledButton
                    key={row.idx}
                    onClick={() => focusFeature(sidebarLayer, row.idx)}
                    style={{ padding: 8, borderRadius: 6, border: "1px solid #e2e8f0" }}
                  >
                    <Group gap={6} wrap="nowrap" justify="space-between">
                      <Text size="xs" fw={500}>#{row.idx + 1}</Text>
                      <Badge size="xs" variant="light" color="blue">{row.geomType}</Badge>
                    </Group>
                    <Text size="xs" c="dimmed" mt={2} style={{ fontFamily: "monospace" }}>
                      {row.coords}
                    </Text>
                  </UnstyledButton>
                ))}
              </Stack>
            )}
          </ScrollArea>
        </div>
      </div>
    </div>
  );
}

// This panel renders its own full-height map + sidebar layout, so give it the
// whole screen (see StepSettingsModal, which honors this static flag).
(GeospatialMapPanel as any).fullScreen = true;

// --- helpers -----------------------------------------------------------

interface Libs {
  MapContainer: ComponentType<MapContainerProps & { ref?: any }>;
  TileLayer: ComponentType<TileLayerProps>;
  GeoJSON: ComponentType<GeoJSONProps & { ref?: any }>;
  Marker: ComponentType<MarkerProps & { ref?: any }>;
  L: typeof LeafletNS;
}

interface RunGeoConfig {
  generatorMode?: string;
  sprayMode?: string;
  levels?: string;
  thresholds?: string;
}

const OSM_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// The three layer names the 'geospatial' step's GeoPackage is documented to
// contain (backend/geospatial.py / backend/steps/geospatial/step.json) — get
// special styling below. Any OTHER layer in the GeoPackage still renders (see
// the panel's generic-layer branch), just without this treatment: this panel
// doesn't assume every GeoPackage it's pointed at came from that step.
const KNOWN_LAYER_LABELS: Record<string, string> = {
  farm_boundary: "Farm boundary",
  spray_zones: "Spray zones",
  detections: "Detections",
};
// Draw order: any layer outside the known three first (bottom, alphabetical
// for determinism), then boundary, zones, detections on top — so detections
// (points) stay the most visible/clickable layer regardless of what else the
// GeoPackage contains.
const KNOWN_LAYER_ORDER = ["farm_boundary", "spray_zones", "detections"];
function layerDrawOrder(a: string, b: string): number {
  const ai = KNOWN_LAYER_ORDER.indexOf(a);
  const bi = KNOWN_LAYER_ORDER.indexOf(b);
  if (ai === -1 && bi === -1) return a.localeCompare(b);
  if (ai === -1) return -1;
  if (bi === -1) return 1;
  return ai - bi;
}
function layerLabel(l: string): string {
  return KNOWN_LAYER_LABELS[l] ?? l.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Stable per-label color for a layer with no special styling — hashed rather
// than index-based, so a layer keeps its color across toggles/reloads
// regardless of what else happens to be present in the same GeoPackage.
const GENERIC_LAYER_PALETTE = ["#0891b2", "#7c3aed", "#059669", "#ca8a04", "#db2777", "#4f46e5", "#0d9488", "#b45309"];
function genericLayerColor(label: string): string {
  let hash = 0;
  for (let i = 0; i < label.length; i++) hash = (hash * 31 + label.charCodeAt(i)) >>> 0;
  return GENERIC_LAYER_PALETTE[hash % GENERIC_LAYER_PALETTE.length];
}

const FARM_BOUNDARY_STYLE = { color: "#334155", weight: 2, fill: false, dashArray: "6 4" };
const DETECTION_MARKER_STYLE = { radius: 5, color: "#dc2626", fillColor: "#f87171", fillOpacity: 0.85, weight: 1 };

// --- mission overlay (path + direction arrows + start/end icons) ---------
//
// Renders a MissionPoint[]-derived FeatureCollection (see lib/missionFormats.ts)
// on top of whatever gpkg layers are showing: the flown path as a line, small
// chevrons along it indicating direction of travel, a distinct icon for the
// first ("start") and last ("end") waypoint, and plain dots for the rest —
// amber for a capture stop, teal otherwise.
const MISSION_PATH_STYLE = { color: "#0891b2", weight: 3, opacity: 0.85 };

function arrowDivIcon(bearingDeg: number, L: typeof LeafletNS) {
  return L.divIcon({
    html: `<svg width="16" height="16" viewBox="0 0 16 16" style="transform: rotate(${bearingDeg}deg)">
             <polygon points="8,1 14,14 8,10 2,14" fill="#0891b2" stroke="#0e7490" stroke-width="0.5" />
           </svg>`,
    className: "",
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}
function endpointDivIcon(emoji: string, L: typeof LeafletNS) {
  return L.divIcon({
    html: `<div style="font-size:20px; line-height:24px; text-align:center;">${emoji}</div>`,
    className: "",
    iconSize: [24, 24],
    iconAnchor: [12, 22],
  });
}
function waypointDivIcon(action: string | undefined, L: typeof LeafletNS) {
  const color = action === "CAPTURE" ? "#d97706" : "#0891b2";
  return L.divIcon({
    html: `<div style="width:9px;height:9px;border-radius:50%;background:${color};border:1.5px solid white;box-shadow:0 0 1px rgba(0,0,0,0.5);"></div>`,
    className: "",
    iconSize: [9, 9],
    iconAnchor: [4.5, 4.5],
  });
}

function MissionLayer({
  fc, GeoJSON, Marker, L,
}: {
  fc: any;
  GeoJSON: ComponentType<GeoJSONProps & { ref?: any }>;
  Marker: ComponentType<MarkerProps & { ref?: any }>;
  L: typeof LeafletNS;
}) {
  const lineFeature = fc.features.find((f: any) => f.geometry?.type === "LineString");
  const pointFeatures = fc.features.filter((f: any) => f.geometry?.type === "Point");
  const arrows = lineFeature ? sampleDirectionArrows(lineFeature.geometry.coordinates) : [];

  return (
    <>
      {lineFeature && <GeoJSON data={lineFeature} style={() => MISSION_PATH_STYLE} />}
      {arrows.map((a, i) => (
        <Marker key={`arrow-${i}`} position={[a.lat, a.lon]} icon={arrowDivIcon(a.bearingDeg, L)} interactive={false} />
      ))}
      {pointFeatures.map((f: any, i: number) => {
        const [lon, lat] = f.geometry.coordinates;
        const role = f.properties?.role;
        const icon = role === "start" ? endpointDivIcon("🟢", L)
          : role === "end" ? endpointDivIcon("🏁", L)
          : waypointDivIcon(f.properties?.action, L);
        return (
          <Marker
            key={`wp-${i}`}
            position={[lat, lon]}
            icon={icon}
            eventHandlers={{
              add: (e: any) => e.target.bindPopup(buildPopup({
                seq: f.properties?.seq,
                action: f.properties?.action || (role === "start" ? "START" : role === "end" ? "END" : ""),
              })),
            }}
          />
        );
      })}
    </>
  );
}

function downloadUrl(apiBase: string, previewQs: string, endpoint: string, label: string) {
  return `${BACKEND_URL}${apiBase}/${endpoint}/${encodeURIComponent(label)}${previewQs}`;
}

// ASSUMPTION (unconfirmed against the real Tapis app, which is outside this
// repo): the spray decision is recorded on each spray_zones feature under one
// of these property names. If none match, the zone renders in a neutral color
// but its raw attributes are still visible in the popup.
const DECISION_KEYS = ["spray_decision", "decision", "spray", "band", "level", "zone_level", "class"];
const NO_DECISION_COLOR = "#94a3b8"; // slate — unknown/no data
const SPRAY_RAMP = ["#16a34a", "#eab308", "#f97316", "#dc2626"]; // green -> red

function findDecisionValue(props: Record<string, any>): string | null {
  for (const key of DECISION_KEYS) {
    const hit = Object.keys(props || {}).find((k) => k.toLowerCase() === key);
    if (hit && props[hit] !== undefined && props[hit] !== null && String(props[hit]) !== "") {
      return String(props[hit]);
    }
  }
  return null;
}

function sprayZoneColor(props: Record<string, any>, levels: string[], sprayMode: string): string {
  const raw = findDecisionValue(props);
  if (raw == null) return NO_DECISION_COLOR;
  if (sprayMode !== "custom" || levels.length === 0) {
    const truthy = ["1", "true", "spray", "yes"].includes(raw.toLowerCase());
    return truthy ? SPRAY_RAMP[SPRAY_RAMP.length - 1] : SPRAY_RAMP[0];
  }
  const idx = levels.findIndex((l) => l.toLowerCase() === raw.toLowerCase());
  if (idx === -1) return NO_DECISION_COLOR;
  const pos = levels.length > 1 ? idx / (levels.length - 1) : 0;
  return SPRAY_RAMP[Math.round(pos * (SPRAY_RAMP.length - 1))];
}

function SprayLegend({ sprayMode, levels, thresholds }: { sprayMode: string; levels: string[]; thresholds: string[] }) {
  const entries: { label: string; color: string }[] =
    sprayMode === "custom" && levels.length > 0
      ? levels.map((label, i) => ({
          label: thresholds[i] ? `${label} (>${thresholds[i]})` : label,
          color: SPRAY_RAMP[Math.round((levels.length > 1 ? i / (levels.length - 1) : 0) * (SPRAY_RAMP.length - 1))],
        }))
      : [
          { label: "No spray", color: SPRAY_RAMP[0] },
          { label: "Spray", color: SPRAY_RAMP[SPRAY_RAMP.length - 1] },
        ];

  return (
    <Group gap="md" px="sm" py={4} style={{ borderBottom: "1px solid #e2e8f0" }}>
      <Text size="xs" c="dimmed">Spray zones:</Text>
      {entries.map((e) => (
        <Group key={e.label} gap={4} wrap="nowrap">
          <div style={{ width: 10, height: 10, borderRadius: 2, background: e.color }} />
          <Text size="xs">{e.label}</Text>
        </Group>
      ))}
    </Group>
  );
}

// Human-readable coordinate summary for the sidebar list. GeoJSON coordinates
// are [lng, lat]; shown here as (lat, lng) to match how people usually read them.
function representativeCoords(geometry: any): string {
  if (!geometry) return "";
  const fmt = (c: number[]) => `${c[1]?.toFixed(5)}, ${c[0]?.toFixed(5)}`;
  switch (geometry.type) {
    case "Point":
      return fmt(geometry.coordinates);
    case "MultiPoint":
    case "LineString": {
      const pts = geometry.coordinates;
      return pts.length > 1 ? `${fmt(pts[0])} (+${pts.length - 1} more)` : fmt(pts[0]);
    }
    case "Polygon":
      return `${fmt(geometry.coordinates[0][0])} (${geometry.coordinates[0].length} pts)`;
    case "MultiLineString":
    case "MultiPolygon":
      return `${geometry.coordinates.length} part(s)`;
    default:
      return "";
  }
}

// Popup content is built as real DOM nodes (never an HTML string) so a
// GeoPackage's attribute values — arbitrary external data — can't inject
// markup/scripts into the page.
function buildPopup(properties: Record<string, any>): HTMLElement {
  const container = document.createElement("div");
  container.style.fontSize = "12px";
  container.style.maxWidth = "220px";
  const entries = Object.entries(properties);
  if (entries.length === 0) {
    const em = document.createElement("i");
    em.textContent = "No attributes";
    container.appendChild(em);
  } else {
    entries.forEach(([k, v]) => {
      const row = document.createElement("div");
      const b = document.createElement("b");
      b.textContent = `${k}: `;
      row.appendChild(b);
      row.appendChild(document.createTextNode(String(v)));
      container.appendChild(row);
    });
  }
  return container;
}
