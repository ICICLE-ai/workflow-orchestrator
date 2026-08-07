import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentType } from "react";
import {
  Stack, Group, Text, Title, NumberInput, ScrollArea, Badge,
} from "@mantine/core";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import ParamSection from "../components/ParamSection";

// Type-only imports — see geospatialMap.tsx for why Leaflet is loaded lazily,
// client-side only.
import type { MapContainerProps, TileLayerProps, GeoJSONProps, MarkerProps } from "react-leaflet";
import type * as LeafletNS from "leaflet";
import markerIcon2x from "leaflet/dist/images/marker-icon-2x.png?url";
import markerIcon from "leaflet/dist/images/marker-icon.png?url";
import markerShadow from "leaflet/dist/images/marker-shadow.png?url";
import "leaflet/dist/leaflet.css";

// Settings panel for the 'flight_plan' step (backend/steps/flight_plan/step.json)
// — turns a farm GeoPackage into an autonomous spray-drone flight plan
// (waypoints + timing), respecting nozzle swath, tank capacity, dispersion
// rate, and battery limits.
//
// Each parameter group below pairs a plain-language explainer with a small
// live diagram that reacts to the current values — not a computed flight
// plan (that's what the actual Tapis app produces), just an illustrative,
// proportionally-scaled visual so the numbers mean something before you run
// the job. Home position is the one field that's genuinely interactive:
// click (or drag the marker) on the map instead of typing raw coordinates.
//
// Registered in registry.ts under the key "flight_plan".
export default function FlightPlanPanel({ config, onChange, step, connectedInputs, runId }: StepPanelProps) {
  const val = (key: string) => {
    const v = config[key];
    return v !== undefined ? Number(v) : Number(step.config_schema[key]?.default ?? 0);
  };
  const set = (key: string, value: number | string) => onChange({ ...config, [key]: value });

  const nozzleSwathFt = val("nozzle_swath_ft");
  const flightHeightFt = val("flight_height_ft");
  const tankCapacityGal = val("tank_capacity_gal");
  const nozzleFlowRateGpm = val("nozzle_flow_rate_gpm");
  const dispersionRateGpa = val("dispersion_rate_gpa");
  const batteryTimeMin = val("battery_time_min");
  const batteryReservePct = val("battery_reserve_pct");
  const homeLat = val("home_lat");
  const homeLon = val("home_lon");

  const gpkgInputPort = step.inputs.find((p) => p.data_type === "geopackage")?.port_name;
  const isWired = !!(gpkgInputPort && connectedInputs[gpkgInputPort]);

  return (
    <ScrollArea style={{ height: "100%" }}>
      <Stack gap="lg" p="lg" maw={980} mx="auto">
        <div>
          <Title order={3}>🚁 Flight Plan Generator</Title>
          <Text size="sm" c="dimmed" mt={4}>
            Turns the wired GeoPackage into an autonomous spray-drone flight plan. The diagrams below are
            illustrative — proportioned from your current values, not the actual computed flight path.
          </Text>
        </div>

        <ParamSection
          title="Spray geometry"
          explainer="Nozzle swath is how wide a strip of ground each pass covers — wider means fewer passes but less precise coverage. Flight height is altitude above the crop canopy; higher flights widen the effective swath but increase drift risk."
          diagram={<SwathDiagram swathFt={nozzleSwathFt} heightFt={flightHeightFt} />}
        >
          <Group grow>
            <NumberInput label="Nozzle swath (ft)" min={1} decimalScale={1} value={nozzleSwathFt}
              onChange={(v) => set("nozzle_swath_ft", Number(v) || 0)} />
            <NumberInput label="Flight height (ft)" min={1} decimalScale={1} value={flightHeightFt}
              onChange={(v) => set("flight_height_ft", Number(v) || 0)} />
          </Group>
        </ParamSection>

        <ParamSection
          title="Tank & application rate"
          explainer="Tank capacity sets how much area the drone covers before returning to refill. Nozzle flow rate and dispersion rate together determine how fast the drone can fly while still applying the target amount of product per acre."
          diagram={<TankFlowDiagram tankGal={tankCapacityGal} flowGpm={nozzleFlowRateGpm} />}
        >
          <Group grow>
            <NumberInput label="Tank capacity (gal)" min={0.1} decimalScale={2} value={tankCapacityGal}
              onChange={(v) => set("tank_capacity_gal", Number(v) || 0)} />
            <NumberInput label="Nozzle flow rate (gpm)" min={0.001} decimalScale={3} value={nozzleFlowRateGpm}
              onChange={(v) => set("nozzle_flow_rate_gpm", Number(v) || 0)} />
            <NumberInput label="Dispersion rate (gal/acre)" min={0.01} decimalScale={2} value={dispersionRateGpa}
              onChange={(v) => set("dispersion_rate_gpa", Number(v) || 0)} />
          </Group>
        </ParamSection>

        <ParamSection
          title="Battery"
          explainer="Total flight time on a single charge, with a reserve percentage held back for a safe return-to-home rather than spent spraying."
          diagram={<BatteryDiagram minutes={batteryTimeMin} reservePct={batteryReservePct} />}
        >
          <Group grow>
            <NumberInput label="Battery time (min)" min={1} decimalScale={1} value={batteryTimeMin}
              onChange={(v) => set("battery_time_min", Number(v) || 0)} />
            <NumberInput label="Battery reserve (%)" min={0} max={90} decimalScale={0} value={batteryReservePct}
              onChange={(v) => set("battery_reserve_pct", Number(v) || 0)} />
          </Group>
        </ParamSection>

        <ParamSection
          title="Home position"
          explainer="The launch point the drone departs from and returns to. Click the map (or drag the marker) instead of typing coordinates — if a farm boundary is available it's shown for reference."
          diagram={null}
        >
          <HomePositionMap
            lat={homeLat}
            lon={homeLon}
            onPick={(lat, lon) => onChange({ ...config, home_lat: lat, home_lon: lon })}
            gpkgWired={isWired}
            previewUri={String(connectedInputs[gpkgInputPort || ""]?.config?.path || "")}
          />
        </ParamSection>
      </Stack>
    </ScrollArea>
  );
}

// Full-height scrollable layout, so give it the whole screen like the other
// rich panels (see StepSettingsModal, which honors this static flag).
(FlightPlanPanel as any).fullScreen = true;

// --- diagrams (illustrative, proportioned from live values) ------------

// Overhead "lawnmower" spray-pass pattern. Pass count is derived from swath
// width against a fixed illustrative field size — narrower swath -> more,
// tighter passes; wider swath -> fewer, wider-spaced passes. A drone marker
// animates along the resulting path via SVG's native animateMotion, so the
// path recomputing on every keystroke doesn't need any custom JS animation loop.
function SwathDiagram({ swathFt, heightFt }: { swathFt: number; heightFt: number }) {
  const W = 200;
  const H = 140;
  const referenceFieldFt = 100; // purely illustrative scale, not a real field size
  const passes = Math.min(10, Math.max(2, Math.round(referenceFieldFt / Math.max(swathFt, 1))));
  const spacing = H / (passes - 1);

  const path = useMemo(() => {
    const points: string[] = [];
    for (let i = 0; i < passes; i++) {
      const y = i * spacing;
      const startX = i % 2 === 0 ? 0 : W;
      const endX = i % 2 === 0 ? W : 0;
      points.push(`${startX},${y.toFixed(1)}`, `${endX},${y.toFixed(1)}`);
    }
    return `M ${points.join(" L ")}`;
  }, [passes, spacing]);

  // Altitude is shown as a simple side indicator: a dashed line whose height
  // (capped) scales with flight_height_ft, next to a fixed ground line.
  const altitudePx = Math.min(50, Math.max(6, heightFt * 1.2));

  return (
    <Stack gap={4} align="center">
      <svg width={W + 40} height={H} viewBox={`-20 0 ${W + 40} ${H}`}>
        {Array.from({ length: passes }).map((_, i) => (
          <line key={i} x1={0} y1={i * spacing} x2={W} y2={i * spacing} stroke="#94a3b8" strokeWidth={1} strokeDasharray="3 3" />
        ))}
        <path d={path} fill="none" stroke="#0891b2" strokeWidth={1.5} opacity={0.5} />
        <circle r={4} fill="#0891b2">
          <animateMotion dur="4s" repeatCount="indefinite" path={path} rotate="auto" />
        </circle>
        {/* altitude indicator */}
        <line x1={-14} y1={H} x2={-14} y2={H - altitudePx} stroke="#334155" strokeWidth={2} />
        <line x1={-18} y1={H} x2={-10} y2={H} stroke="#334155" strokeWidth={2} />
      </svg>
      <Text size="xs" c="dimmed">{passes} illustrative passes · {Math.round(heightFt)} ft altitude</Text>
    </Stack>
  );
}

// Tank fill level (scaled against a fixed illustrative max) + a dripping
// droplet whose animation speed reflects nozzle flow rate.
function TankFlowDiagram({ tankGal, flowGpm }: { tankGal: number; flowGpm: number }) {
  const illustrativeMaxGal = 20;
  const fillPct = Math.min(100, Math.max(4, (tankGal / illustrativeMaxGal) * 100));
  const dripDuration = Math.max(0.3, Math.min(3, 0.06 / Math.max(flowGpm, 0.001)));

  return (
    <Stack gap={4} align="center">
      <Group gap="md" align="flex-end">
        <div style={{ width: 36, height: 70, border: "2px solid #334155", borderRadius: 4, position: "relative", overflow: "hidden", background: "#f1f5f9" }}>
          <div style={{
            position: "absolute", bottom: 0, left: 0, right: 0, height: `${fillPct}%`,
            background: "#0ea5e9", transition: "height 300ms ease",
          }} />
        </div>
        <div style={{ position: "relative", width: 16, height: 70 }}>
          <div style={{ width: 6, height: 6, borderRadius: "50%", background: "#0ea5e9", position: "absolute", left: 5, animation: `flightplan-drip ${dripDuration}s linear infinite` }} />
        </div>
      </Group>
      <Text size="xs" c="dimmed">tank fill (relative) · droplet ≈ flow rate</Text>
      <style>{`
        @keyframes flightplan-drip {
          0% { top: 0; opacity: 0; }
          10% { opacity: 1; }
          90% { opacity: 1; }
          100% { top: 60px; opacity: 0; }
        }
      `}</style>
    </Stack>
  );
}

// Battery bar with a shaded reserve zone, plus the (genuinely computed, not
// fabricated) usable spray time left after reserving that percentage.
function BatteryDiagram({ minutes, reservePct }: { minutes: number; reservePct: number }) {
  const W = 200;
  const H = 36;
  const reserveFrac = Math.min(0.9, Math.max(0, reservePct / 100));
  const usableW = W * (1 - reserveFrac);
  const usableMinutes = minutes * (1 - reserveFrac);

  return (
    <Stack gap={4} align="center">
      <svg width={W + 8} height={H}>
        <rect x={0} y={0} width={W} height={H} rx={6} fill="#f1f5f9" stroke="#334155" strokeWidth={2} />
        <rect x={2} y={2} width={Math.max(0, usableW - 4)} height={H - 4} rx={4} fill="#16a34a">
          <animate attributeName="width" to={Math.max(0, usableW - 4)} dur="300ms" fill="freeze" />
        </rect>
        <rect x={usableW} y={2} width={Math.max(0, W - usableW - 2)} height={H - 4} fill="#fca5a5" opacity={0.8} />
        <rect x={W + 1} y={H / 2 - 6} width={5} height={12} rx={1} fill="#334155" />
      </svg>
      <Text size="xs" c="dimmed">
        ≈ {usableMinutes.toFixed(1)} min usable spray time, {(minutes - usableMinutes).toFixed(1)} min reserved
      </Text>
    </Stack>
  );
}

// --- home position map ---------------------------------------------------

interface MapLibs {
  MapContainer: ComponentType<MapContainerProps & { ref?: any }>;
  TileLayer: ComponentType<TileLayerProps>;
  GeoJSON: ComponentType<GeoJSONProps & { ref?: any }>;
  Marker: ComponentType<MarkerProps & { ref?: any }>;
  useMapEvents: (handlers: Record<string, (e: any) => void>) => any;
  L: typeof LeafletNS;
}

const OSM_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

function HomePositionMap({
  lat, lon, onPick, gpkgWired, previewUri,
}: {
  lat: number;
  lon: number;
  onPick: (lat: number, lon: number) => void;
  gpkgWired: boolean;
  previewUri: string;
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
          useMapEvents: rl.useMapEvents,
          L,
        });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Best-effort farm boundary overlay, via the wired GeoPackage's own
  // resolved URI (design-time or run-resolved — see previewUri's origin in
  // FlightPlanPanel) and /api/geospatial-preview/... — NOT the run-scoped
  // /api/pipeline-runs/{run_id}/geospatial/... resource, which finds "any
  // node in this run producing a geopackage": in a run with more than one
  // (this step's own "flight_gpkg" output counts too, on top of whatever
  // feeds this step's "gpkg" input), that lookup can't know which one this
  // map is even trying to preview. Going through the wired URI directly
  // sidesteps the ambiguity — same fix as geospatialMap.tsx's data fetch.
  // Silently absent if nothing's wired yet, there's no boundary layer, or
  // the fetch fails — this is reference context, not a blocker.
  const [boundary, setBoundary] = useState<any | null>(null);
  useEffect(() => {
    if (!gpkgWired || !previewUri) return;
    let cancelled = false;
    apiFetch(`/api/geospatial-preview/geojson/farm_boundary?uri=${encodeURIComponent(previewUri)}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => { if (!cancelled && data) setBoundary(data); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [gpkgWired, previewUri]);

  const mapRef = useRef<any>(null);
  useEffect(() => {
    if (!libs || !mapRef.current || !boundary) return;
    const bounds = libs.L.geoJSON(boundary).getBounds();
    if (bounds.isValid()) mapRef.current.fitBounds(bounds, { padding: [24, 24] });
  }, [libs, boundary]);

  if (!libs) {
    return <Text size="xs" c="dimmed">Loading map…</Text>;
  }

  const { MapContainer, TileLayer, GeoJSON, Marker, useMapEvents } = libs;
  const center: [number, number] = lat && lon ? [lat, lon] : [40.0, -83.0];

  function ClickHandler() {
    useMapEvents({ click: (e: any) => onPick(Number(e.latlng.lat.toFixed(6)), Number(e.latlng.lng.toFixed(6))) });
    return null;
  }

  return (
    <Stack gap={4}>
      <div style={{ height: 260, borderRadius: 8, overflow: "hidden", border: "1px solid #e2e8f0" }}>
        <MapContainer ref={mapRef} center={center} zoom={boundary ? 2 : 13} style={{ height: "100%", width: "100%" }}>
          <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} />
          {boundary && <GeoJSON data={boundary} style={() => ({ color: "#334155", weight: 2, fill: false, dashArray: "6 4" })} />}
          {lat !== 0 || lon !== 0 ? (
            <Marker
              position={[lat, lon]}
              draggable
              eventHandlers={{
                dragend: (e: any) => {
                  const p = e.target.getLatLng();
                  onPick(Number(p.lat.toFixed(6)), Number(p.lng.toFixed(6)));
                },
              }}
            />
          ) : null}
          <ClickHandler />
        </MapContainer>
      </div>
      <Group gap="lg">
        <Text size="xs" c="dimmed">Lat: <b>{lat.toFixed(6)}</b></Text>
        <Text size="xs" c="dimmed">Lon: <b>{lon.toFixed(6)}</b></Text>
        {!boundary && gpkgWired && <Badge size="xs" variant="light" color="gray">No farm boundary preview available</Badge>}
      </Group>
    </Stack>
  );
}
