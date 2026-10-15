import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentType } from "react";
import {
  Stack, Group, Text, Title, NumberInput, Select, ScrollArea, Badge, SegmentedControl, Button,
} from "@mantine/core";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import ParamSection from "../components/ParamSection";
import TapisPathField from "../components/TapisPathField";
import { computeLawnmowerPreview } from "../lib/lawnmowerPreview";
import type { LawnmowerPreview } from "../lib/lawnmowerPreview";

// Type-only imports — see geospatialMap.tsx for why Leaflet is loaded lazily,
// client-side only.
import type { MapContainerProps, TileLayerProps, GeoJSONProps, MarkerProps, PolygonProps, PolylineProps, CircleMarkerProps } from "react-leaflet";
import type * as LeafletNS from "leaflet";
import markerIcon2x from "leaflet/dist/images/marker-icon-2x.png?url";
import markerIcon from "leaflet/dist/images/marker-icon.png?url";
import markerShadow from "leaflet/dist/images/marker-shadow.png?url";
import "leaflet/dist/leaflet.css";

// Settings panel for the 'scouting_mission' step
// (backend/steps/scouting_mission/step.json) — turns a hand-traced farm
// boundary + starting point into a lawnmower survey mission (waypoints +
// timing) for image or video capture, via the generate-scouting-mission
// Tapis app (jobs/scouting_mission_generator). That job only GENERATES the
// mission — actually flying it happens locally, on a machine connected to
// the drone's own Wi-Fi network, via the standalone executable built from
// fly_mission.py (see that job folder's README). This panel also captures
// where captured media should eventually be uploaded, so the generated
// next_steps.txt can pre-fill the local flight command with it.
//
// Sibling of flightPlan.tsx (the spray-mission step) — same diagram +
// interactive-map pattern, different geometry: a user-traced boundary
// polygon instead of zones read from a wired GeoPackage.
//
// Registered in registry.ts under the key "scouting_mission".
export default function ScoutingMissionPanel({ config, onChange, step, connectedInputs, runId }: StepPanelProps) {
  const val = (key: string) => {
    const v = config[key];
    return v !== undefined ? Number(v) : Number(step.config_schema[key]?.default ?? 0);
  };
  const valStr = (key: string) => {
    const v = config[key];
    if (v !== undefined && v !== null) return String(v);
    const d = step.config_schema[key]?.default;
    return d !== undefined && d !== null ? String(d) : "";
  };
  const set = (key: string, value: number | string) => onChange({ ...config, [key]: value });

  const homeLat = val("home_lat");
  const homeLon = val("home_lon");
  const altitudeM = val("altitude_m");
  const lineSpacingM = val("line_spacing_m");
  const captureMode = valStr("capture_mode") || "image";
  const captureSpacingM = val("capture_spacing_m");
  const holdTimeS = val("hold_time_s");
  const boundaryPoints = valStr("boundary_points");
  const imageUploadSystem = valStr("system");
  const imageUploadPath = valStr("image_upload_path");

  const gpkgInputPort = step.inputs.find((p) => p.data_type === "geopackage")?.port_name;
  const isWired = !!(gpkgInputPort && connectedInputs[gpkgInputPort]);

  // The ACTUAL survey pattern (not the illustrative fixed-size diagram above),
  // clipped to the real traced boundary and starting point, recomputed live
  // as any of these change — see lib/lawnmowerPreview.ts (a client-side port
  // of the real generator's algorithm).
  const homeIsSet = homeLat !== 0 || homeLon !== 0;
  const preview = useMemo(
    () => (homeIsSet ? computeLawnmowerPreview(
      parseBoundaryPoints(boundaryPoints),
      [homeLat, homeLon],
      lineSpacingM,
      captureMode === "image" ? captureSpacingM : null,
    ) : null),
    [homeIsSet, boundaryPoints, homeLat, homeLon, lineSpacingM, captureSpacingM, captureMode]
  );

  return (
    <ScrollArea style={{ height: "100%" }}>
      <Stack gap="lg" p="lg" maw={980} mx="auto">
        <div>
          <Title order={3}>🔎 Scouting Mission Generator</Title>
          <Text size="sm" c="dimmed" mt={4}>
            Flies a lawnmower survey pattern over a traced farm boundary, capturing images or video at regular
            intervals. This only generates the mission — flying it happens afterward, locally, on a machine
            connected to the drone (see the step description). The diagram below is illustrative — proportioned
            from your current values, not the actual computed flight path.
          </Text>
        </div>

        <ParamSection
          title="Survey geometry"
          explainer="Line spacing is the distance between adjacent survey passes — tighter spacing gives denser coverage but a longer flight. Altitude is flown relative to the takeoff point."
          diagram={<ScoutingDiagram spacingFt={lineSpacingM} captureDistFt={captureSpacingM} />}
        >
          <Group grow>
            <NumberInput label="Line spacing (m)" min={1} decimalScale={1} value={lineSpacingM}
              onChange={(v) => set("line_spacing_m", Number(v) || 0)} />
            <NumberInput label="Altitude (m)" min={1} decimalScale={1} value={altitudeM}
              onChange={(v) => set("altitude_m", Number(v) || 0)} />
          </Group>
        </ParamSection>

        <ParamSection
          title="Capture mode"
          explainer="Image mode stops and holds at a fixed distance interval to take a still photo. Video mode records continuously through the whole mission with no stops."
          diagram={null}
        >
          <Select
            label="Mode"
            data={[
              { value: "image", label: "Image — stop & capture stills" },
              { value: "video", label: "Video — continuous recording" },
            ]}
            value={captureMode}
            onChange={(v) => set("capture_mode", v ?? "image")}
            allowDeselect={false}
          />
          {captureMode === "image" && (
            <Group grow mt="sm">
              <NumberInput label="Image capture distance (m)" min={1} decimalScale={1} value={captureSpacingM}
                onChange={(v) => set("capture_spacing_m", Number(v) || 0)} />
              <NumberInput label="Hold time at each capture (s)" min={0} decimalScale={1} value={holdTimeS}
                onChange={(v) => set("hold_time_s", Number(v) || 0)} />
            </Group>
          )}
        </ParamSection>

        <ParamSection
          title="Image upload destination"
          explainer="Where captured images/video should be uploaded after the flight — a Tapis system and directory. Not used to generate the mission itself; carried into the generated next_steps.txt so the local flight command is pre-filled with it."
          diagram={null}
        >
          <TapisPathField
            label="Upload directory"
            system={imageUploadSystem}
            path={imageUploadPath}
            selectType="dir"
            onSystemChange={(v) => set("system", v)}
            onPathChange={(v) => set("image_upload_path", v)}
          />
        </ParamSection>

        <ParamSection
          title="Farm boundary & starting point"
          explainer="Trace the farm boundary by clicking the map to drop vertices, then switch to “Set start point” and click the approximate point the drone should launch from. Once both are set, the actual survey pattern for your current line spacing / capture distance appears live on the map — this is the real generator's algorithm, not the illustrative diagram above. If a farm boundary is available from a wired GeoPackage it's shown for reference."
          diagram={null}
        >
          <BoundaryMap
            homeLat={homeLat}
            homeLon={homeLon}
            onPickHome={(lat, lon) => onChange({ ...config, home_lat: lat, home_lon: lon })}
            boundaryPoints={boundaryPoints}
            onBoundaryPointsChange={(pts) => set("boundary_points", pts)}
            gpkgWired={isWired}
            previewUri={String(connectedInputs[gpkgInputPort || ""]?.config?.path || "")}
            preview={preview}
          />
        </ParamSection>
      </Stack>
    </ScrollArea>
  );
}

// Full-height scrollable layout, so give it the whole screen like the other
// rich panels (see StepSettingsModal, which honors this static flag).
(ScoutingMissionPanel as any).fullScreen = true;

// --- diagram (illustrative, proportioned from live values) ---------------

// Overhead "lawnmower" survey pattern. Pass count is derived from line
// spacing against a fixed illustrative field size, mirroring
// flightPlan.tsx's SwathDiagram — narrower spacing -> more, tighter
// passes. Capture points (derived from capture distance against the same
// illustrative reference length) are marked as dots along each line. A
// drone marker animates along the path via SVG's native animateMotion.
function ScoutingDiagram({ spacingFt, captureDistFt }: { spacingFt: number; captureDistFt: number }) {
  const W = 200;
  const H = 140;
  const referenceFieldFt = 100; // purely illustrative scale, not a real field size
  const passes = Math.min(10, Math.max(2, Math.round(referenceFieldFt / Math.max(spacingFt, 1))));
  const spacing = H / (passes - 1);
  const capturesPerLine = Math.min(20, Math.max(2, Math.round(referenceFieldFt / Math.max(captureDistFt, 1))));

  const { path, captures } = useMemo(() => {
    const points: string[] = [];
    const captureDots: { x: number; y: number }[] = [];
    for (let i = 0; i < passes; i++) {
      const y = i * spacing;
      const startX = i % 2 === 0 ? 0 : W;
      const endX = i % 2 === 0 ? W : 0;
      points.push(`${startX},${y.toFixed(1)}`, `${endX},${y.toFixed(1)}`);
      for (let c = 0; c <= capturesPerLine; c++) {
        const t = c / capturesPerLine;
        captureDots.push({ x: startX + (endX - startX) * t, y });
      }
    }
    return { path: `M ${points.join(" L ")}`, captures: captureDots };
  }, [passes, spacing, capturesPerLine]);

  return (
    <Stack gap={4} align="center">
      <svg width={W + 40} height={H} viewBox={`-20 0 ${W + 40} ${H}`}>
        {Array.from({ length: passes }).map((_, i) => (
          <line key={i} x1={0} y1={i * spacing} x2={W} y2={i * spacing} stroke="#94a3b8" strokeWidth={1} strokeDasharray="3 3" />
        ))}
        <path d={path} fill="none" stroke="#0891b2" strokeWidth={1.5} opacity={0.5} />
        {captures.map((c, i) => (
          <circle key={i} cx={c.x} cy={c.y} r={2} fill="#d97706" />
        ))}
        <circle r={4} fill="#0891b2">
          <animateMotion dur="4s" repeatCount="indefinite" path={path} rotate="auto" />
        </circle>
      </svg>
      <Text size="xs" c="dimmed">{passes} illustrative lines · {captures.length} illustrative captures</Text>
    </Stack>
  );
}

// --- boundary + start-point map -------------------------------------------

interface MapLibs {
  MapContainer: ComponentType<MapContainerProps & { ref?: any }>;
  TileLayer: ComponentType<TileLayerProps>;
  GeoJSON: ComponentType<GeoJSONProps & { ref?: any }>;
  Marker: ComponentType<MarkerProps & { ref?: any }>;
  Polygon: ComponentType<PolygonProps & { ref?: any }>;
  Polyline: ComponentType<PolylineProps & { ref?: any }>;
  CircleMarker: ComponentType<CircleMarkerProps & { ref?: any }>;
  useMapEvents: (handlers: Record<string, (e: any) => void>) => any;
  L: typeof LeafletNS;
}

const OSM_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// The boundary is stored in config as the exact "lat,lon;lat,lon;..."
// string fly_mission.py's --boundary expects (see run_job.py /
// mission_generator.py) — no intermediate GeoJSON, so what's saved on the
// node is already what gets substituted straight into the Tapis job's
// appArgs.
function parseBoundaryPoints(s: string): [number, number][] {
  if (!s) return [];
  return s
    .split(";")
    .map((pair) => pair.split(",").map(Number))
    .filter((p): p is [number, number] => p.length === 2 && p.every(Number.isFinite)) as [number, number][];
}
function formatBoundaryPoints(points: [number, number][]): string {
  return points.map(([lat, lon]) => `${lat.toFixed(6)},${lon.toFixed(6)}`).join(";");
}

function BoundaryMap({
  homeLat, homeLon, onPickHome, boundaryPoints, onBoundaryPointsChange, gpkgWired, previewUri, preview,
}: {
  homeLat: number;
  homeLon: number;
  onPickHome: (lat: number, lon: number) => void;
  boundaryPoints: string;
  onBoundaryPointsChange: (points: string) => void;
  gpkgWired: boolean;
  previewUri: string;
  preview: LawnmowerPreview | null;
}) {
  const [libs, setLibs] = useState<MapLibs | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [rl, leafletMod] = await Promise.all([import("react-leaflet"), import("leaflet")]);
      const L = ((leafletMod as any).default ?? leafletMod) as typeof LeafletNS;
      delete (L.Icon.Default.prototype as any)._getIconUrl;
      L.Icon.Default.mergeOptions({ iconRetinaUrl: markerIcon2x, iconUrl: markerIcon, shadowUrl: markerShadow });
      if (!cancelled) {
        setLibs({
          MapContainer: rl.MapContainer as any,
          TileLayer: rl.TileLayer,
          GeoJSON: rl.GeoJSON as any,
          Marker: rl.Marker as any,
          Polygon: rl.Polygon as any,
          Polyline: rl.Polyline as any,
          CircleMarker: rl.CircleMarker as any,
          useMapEvents: rl.useMapEvents,
          L,
        });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Best-effort farm boundary overlay, via the wired GeoPackage's own
  // resolved URI and /api/geospatial-preview/... — same approach as
  // flightPlan.tsx's HomePositionMap (see its comments for why the
  // run-scoped resource is deliberately avoided). Reference only — this
  // is not what gets saved as boundary_points; the user traces that
  // themselves below.
  const [wiredBoundary, setWiredBoundary] = useState<any | null>(null);
  useEffect(() => {
    if (!gpkgWired || !previewUri) return;
    let cancelled = false;
    apiFetch(`/api/geospatial-preview/geojson/farm_boundary?uri=${encodeURIComponent(previewUri)}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => { if (!cancelled && data) setWiredBoundary(data); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [gpkgWired, previewUri]);

  const mapRef = useRef<any>(null);
  useEffect(() => {
    if (!libs || !mapRef.current || !wiredBoundary) return;
    const bounds = libs.L.geoJSON(wiredBoundary).getBounds();
    if (bounds.isValid()) mapRef.current.fitBounds(bounds, { padding: [24, 24] });
  }, [libs, wiredBoundary]);

  // Click adds a boundary vertex, toggled against "set start point"
  // clicks via `mode` so one map surface can do both without the two
  // interactions stepping on each other.
  const [mode, setMode] = useState<"boundary" | "point">("boundary");
  const tracedPoints = useMemo(() => parseBoundaryPoints(boundaryPoints), [boundaryPoints]);

  // Keep the traced boundary in view as it's built — refits whenever a vertex
  // is added/removed/undone (keyed on the raw string, not `preview`, so
  // tweaking line spacing/capture distance afterward doesn't yank the view
  // around on every keystroke).
  useEffect(() => {
    if (!libs || !mapRef.current || tracedPoints.length < 3) return;
    const bounds = libs.L.polygon(tracedPoints).getBounds();
    if (bounds.isValid()) mapRef.current.fitBounds(bounds, { padding: [24, 24] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [libs, boundaryPoints]);

  const addBoundaryPoint = (p: [number, number]) => onBoundaryPointsChange(formatBoundaryPoints([...tracedPoints, p]));
  const undoBoundaryPoint = () => onBoundaryPointsChange(formatBoundaryPoints(tracedPoints.slice(0, -1)));
  const clearBoundary = () => onBoundaryPointsChange("");

  if (!libs) {
    return <Text size="xs" c="dimmed">Loading map…</Text>;
  }

  const { MapContainer, TileLayer, GeoJSON, Marker, Polygon, Polyline, CircleMarker, useMapEvents } = libs;
  const center: [number, number] = homeLat && homeLon ? [homeLat, homeLon] : [40.0, -83.0];

  function ClickHandler() {
    useMapEvents({
      click: (e: any) => {
        const p: [number, number] = [Number(e.latlng.lat.toFixed(6)), Number(e.latlng.lng.toFixed(6))];
        if (mode === "boundary") addBoundaryPoint(p);
        else onPickHome(p[0], p[1]);
      },
    });
    return null;
  }

  return (
    <Stack gap={4}>
      <Group gap="xs">
        <SegmentedControl
          size="xs"
          value={mode}
          onChange={(v) => setMode(v as "boundary" | "point")}
          data={[
            { label: "Draw boundary", value: "boundary" },
            { label: "Set start point", value: "point" },
          ]}
        />
        <Button size="xs" variant="light" color="gray" onClick={undoBoundaryPoint} disabled={tracedPoints.length === 0}>
          Undo point
        </Button>
        <Button size="xs" variant="light" color="red" onClick={clearBoundary} disabled={tracedPoints.length === 0}>
          Clear boundary
        </Button>
      </Group>
      <div style={{ height: 520, borderRadius: 8, overflow: "hidden", border: "1px solid #e2e8f0" }}>
        <MapContainer ref={mapRef} center={center} zoom={wiredBoundary ? 2 : 13} style={{ height: "100%", width: "100%" }}>
          <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} />
          {wiredBoundary && <GeoJSON data={wiredBoundary} style={() => ({ color: "#334155", weight: 2, fill: false, dashArray: "6 4" })} />}
          {tracedPoints.length >= 3 && (
            <Polygon positions={tracedPoints} pathOptions={{ color: "#d97706", weight: 2, fillColor: "#f59e0b", fillOpacity: 0.15 }} />
          )}
          {tracedPoints.length > 0 && tracedPoints.length < 3 && (
            <Polygon positions={tracedPoints} pathOptions={{ color: "#d97706", weight: 2, fill: false }} />
          )}
          {preview && (
            <Polyline positions={preview.points} pathOptions={{ color: "#0891b2", weight: 2, opacity: 0.85 }} />
          )}
          {preview && preview.capturePoints.map((p, i) => (
            <CircleMarker key={i} center={p} radius={2.5} pathOptions={{ color: "#0e7490", fillColor: "#0891b2", fillOpacity: 0.9, weight: 1 }} />
          ))}
          {(homeLat !== 0 || homeLon !== 0) && (
            <Marker
              position={[homeLat, homeLon]}
              draggable
              eventHandlers={{
                dragend: (e: any) => {
                  const p = e.target.getLatLng();
                  onPickHome(Number(p.lat.toFixed(6)), Number(p.lng.toFixed(6)));
                },
              }}
            />
          )}
          <ClickHandler />
        </MapContainer>
      </div>
      <Group gap="lg">
        <Text size="xs" c="dimmed">Start: <b>{homeLat.toFixed(6)}, {homeLon.toFixed(6)}</b></Text>
        <Text size="xs" c="dimmed">Boundary points: <b>{tracedPoints.length}</b></Text>
        {preview && (
          <Text size="xs" c="dimmed">
            Preview path: <b>{(preview.pathLengthM / 1000).toFixed(2)} km</b>
            {preview.capturePoints.length > 0 && <> · <b>{preview.capturePoints.length}</b> captures</>}
          </Text>
        )}
        {!wiredBoundary && gpkgWired && <Badge size="xs" variant="light" color="gray">No farm boundary preview available</Badge>}
        {tracedPoints.length >= 3 && !preview && (
          <Badge size="xs" variant="light" color="gray">Set a start point to preview the survey pattern</Badge>
        )}
      </Group>
    </Stack>
  );
}
