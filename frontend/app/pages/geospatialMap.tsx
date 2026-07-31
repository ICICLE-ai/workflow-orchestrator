import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentType } from "react";
import {
  Group, Select, Text, Loader, Stack, Paper, ScrollArea, Badge, UnstyledButton,
  Checkbox, Anchor, Divider,
} from "@mantine/core";
import { IconDownload } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch, BACKEND_URL } from "../lib/api";

// Type-only imports — erased at build, so Leaflet (manipulates window/document
// directly) never loads during SSR. The runtime module is loaded lazily below.
import type { MapContainerProps, TileLayerProps, GeoJSONProps } from "react-leaflet";
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

// This panel renders a completed 'geospatial' step's GeoPackage — built
// server-side (backend/geospatial.py) from drone detection results (JSON) +
// source imagery (EXIF GPS) into three fixed layers:
//   - detections    : point layer, one point per detected object
//   - spray_zones   : polygon grid layer, one cell per grid square, with a
//                     spray decision (binary or custom banded) based on
//                     detection counts
//   - farm_boundary : polygon layer, convex hull of all image locations
//
// Unlike the shapefile-viewer this replaces, this panel never touches raw
// geodata bytes: it calls a backend resource (backend/geospatial.py) that
// converts GeoPackage -> GeoJSON server-side via GDAL/OGR. That resource comes
// in two forms, and this panel picks whichever applies:
//   - run-scoped (/api/pipeline-runs/{run_id}/geospatial/...): opened from the
//     run page (runs.$runId.tsx passes `runId` down) — reads the actual
//     GeoPackage a completed run produced.
//   - design-time preview (/api/geospatial-preview/...): opened from the
//     canvas before any run exists. Only works wired to a static
//     source_geopackage node, whose path is already known without running
//     anything — a 'geospatial' job step has nothing to preview until it
//     actually runs, so this shows a placeholder in that case instead.
//
// Registered in registry.ts under the key "geospatial_map".
export default function GeospatialMapPanel({ step, connectedInputs, runId }: StepPanelProps) {
  const gpkgInputPort = step.inputs.find((p) => p.data_type === "geopackage")?.port_name;
  const wiredInput = gpkgInputPort ? connectedInputs[gpkgInputPort] : undefined;
  const isWired = !!wiredInput;

  // Design-time-resolved location (a tapis://system/path URI — see
  // CustomNode's resolveOutputPath, which builds this the same way the
  // backend does for a completed run's output). Only meaningful without a
  // run; a job step's connectedInputs value is empty until it actually runs.
  const previewUri = !runId ? String(wiredInput?.config?.path || "") : "";
  const mode: "run" | "preview" | null = runId ? "run" : (previewUri ? "preview" : null);
  const apiBase = mode === "run" ? `/api/pipeline-runs/${runId}/geospatial` : "/api/geospatial-preview";
  const previewQs = mode === "preview" ? `?uri=${encodeURIComponent(previewUri)}` : "";

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
        setLibs({ MapContainer: rl.MapContainer as any, TileLayer: rl.TileLayer, GeoJSON: rl.GeoJSON as any, L });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [layerData, setLayerData] = useState<Record<string, any>>({});
  const [runConfig, setRunConfig] = useState<RunGeoConfig | null>(null);
  const [missingCount, setMissingCount] = useState(0);
  const [visible, setVisible] = useState<Record<string, boolean>>({
    farm_boundary: true, spray_zones: true, detections: true,
  });
  const [sidebarLayer, setSidebarLayer] = useState<string>("detections");

  useEffect(() => {
    if (!isWired || !mode) return;
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
        const present: string[] = KNOWN_LAYERS.filter((l) => (labels || []).includes(l));

        // /config only exists on the run-scoped resource — a design-time
        // preview has no run-level step config to read spray mode from.
        const [cfgRes, missingRes, ...geojsonResults] = await Promise.all([
          mode === "run" ? apiFetch(`${apiBase}/config`) : Promise.resolve(null),
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
        for (let i = 0; i < present.length; i++) {
          if (geojsonResults[i].ok) data[present[i]] = await geojsonResults[i].json();
        }
        if (!cancelled) {
          setLayerData(data);
          setSidebarLayer(present.find((l) => l === "detections") || present[0] || "detections");
        }
      } catch (err: any) {
        if (!cancelled) setLoadError(err?.message || "Could not load the geospatial layers");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [isWired, mode, apiBase, previewQs]);

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

  // Fit the map to the union of every loaded (visible-by-default) layer once loaded.
  useEffect(() => {
    if (!libs || !mapRef.current) return;
    const collections = KNOWN_LAYERS.map((l) => layerData[l]).filter(Boolean);
    if (collections.length === 0) return;
    const merged = { type: "FeatureCollection" as const, features: collections.flatMap((fc) => fc.features || []) };
    const bounds = libs.L.geoJSON(merged as any).getBounds();
    if (bounds.isValid()) mapRef.current.fitBounds(bounds, { padding: [24, 24] });
  }, [libs, layerData]);

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
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
    } else if (typeof layer.getLatLng === "function") {
      map.setView(layer.getLatLng(), 17);
    }
  };

  if (!gpkgInputPort) {
    return (
      <Stack align="center" justify="center" style={{ height: "100%" }} p="xl">
        <Text c="dimmed">This step type has no GeoPackage input configured.</Text>
      </Stack>
    );
  }

  if (!isWired) {
    return (
      <Stack align="center" justify="center" style={{ height: "100%" }} p="xl">
        <Text c="dimmed">Connect a Geospatial step's GeoPackage output to this step's "gpkg" input to visualize it.</Text>
      </Stack>
    );
  }

  if (!mode) {
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

  const { MapContainer, TileLayer, GeoJSON } = libs;
  const presentLayers = KNOWN_LAYERS.filter((l) => layerData[l]);
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
            {!loading && !loadError && KNOWN_LAYERS.map((l) => (
              layerData[l] && (
                <Checkbox
                  key={l}
                  size="xs"
                  label={LAYER_LABELS[l]}
                  checked={visible[l]}
                  onChange={(e) => setVisible((v) => ({ ...v, [l]: e.currentTarget.checked }))}
                />
              )
            ))}
          </Group>
          <Group gap="sm" wrap="nowrap">
            {missingCount > 0 && (
              <Badge size="xs" color="orange" variant="light">{missingCount} missing source image(s)</Badge>
            )}
            {presentLayers.map((l) => (
              <Anchor
                key={l}
                href={downloadUrl(apiBase, previewQs, l === "farm_boundary" ? "download-farm" : "download", l)}
                target="_blank"
                rel="noreferrer"
                size="xs"
              >
                <Group gap={4} wrap="nowrap"><IconDownload size={12} />{LAYER_LABELS[l]} (.zip)</Group>
              </Anchor>
            ))}
            {presentLayers.length > 0 && (
              <Anchor
                href={downloadUrl(apiBase, previewQs, "download-gpkg", mode === "run" ? `run_${runId}` : "geospatial")}
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

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <MapContainer ref={mapRef} center={[0, 0]} zoom={2} style={{ height: "100%", width: "100%" }}>
            <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} />
            {visible.farm_boundary && layerData.farm_boundary && (
              <GeoJSON
                key="farm_boundary"
                data={layerData.farm_boundary}
                style={() => FARM_BOUNDARY_STYLE}
                onEachFeature={(f, l) => onEachFeature(f, l, "farm_boundary", layerData.farm_boundary.features.indexOf(f))}
              />
            )}
            {visible.spray_zones && layerData.spray_zones && (
              <GeoJSON
                key="spray_zones"
                data={layerData.spray_zones}
                style={(f: any) => ({ color: "#475569", weight: 1, fillOpacity: 0.55, fillColor: sprayZoneColor(f?.properties || {}, levels, sprayMode) })}
                onEachFeature={(f, l) => onEachFeature(f, l, "spray_zones", layerData.spray_zones.features.indexOf(f))}
              />
            )}
            {visible.detections && layerData.detections && (
              <GeoJSON
                key="detections"
                data={layerData.detections}
                pointToLayer={(_f, latlng) => libs.L.circleMarker(latlng, DETECTION_MARKER_STYLE)}
                onEachFeature={(f, l) => onEachFeature(f, l, "detections", layerData.detections.features.indexOf(f))}
              />
            )}
          </MapContainer>
        </div>

        <div style={{ width: 320, borderLeft: "1px solid #e2e8f0", display: "flex", flexDirection: "column" }}>
          <Group justify="space-between" p="xs">
            <Text size="sm" fw={600}>Features</Text>
            <Select
              size="xs"
              w={150}
              data={presentLayers.map((l) => ({ value: l, label: LAYER_LABELS[l] }))}
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

// Fixed layer names the geospatial step's GeoPackage is documented to contain
// (backend/geospatial.py / backend/steps/geospatial/step.json). Draw order:
// boundary first (bottom), zones over it, detections on top.
const KNOWN_LAYERS = ["farm_boundary", "spray_zones", "detections"];
const LAYER_LABELS: Record<string, string> = {
  farm_boundary: "Farm boundary",
  spray_zones: "Spray zones",
  detections: "Detections",
};

const FARM_BOUNDARY_STYLE = { color: "#334155", weight: 2, fill: false, dashArray: "6 4" };
const DETECTION_MARKER_STYLE = { radius: 5, color: "#dc2626", fillColor: "#f87171", fillOpacity: 0.85, weight: 1 };

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
