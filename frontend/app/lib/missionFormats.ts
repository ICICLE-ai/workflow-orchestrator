// Parsers for the drone mission file formats produced by the flight_plan,
// scouting_mission, and mission_export steps (see jobs/flight_plan_generator,
// jobs/scouting_mission_generator, jobs/flight_mission_adaptar) — turns each
// into a flat, ordered list of waypoints (MissionPoint[]) that
// geospatialMap.tsx renders on the map (path + direction arrows + distinct
// start/end icons), regardless of which format it came from.
//
// Everything here runs client-side against bytes fetched through the
// existing generic /api/tapis-files/content (and /list, for browsing a
// mission_files_dir) proxy — no new backend endpoints needed, since every
// format below is either plain JSON or small enough to parse in the browser.
import { apiFetch } from "./api";

export interface MissionPoint {
  lat: number;
  lon: number;
  seq: number;
  action?: string; // e.g. "TAKEOFF", "SPRAY", "CAPTURE", "RTL", "TRANSIT" — format-dependent, informational only
}

export interface MissionFileOption {
  label: string; // relative path shown in the picker, e.g. "ardupilot/mission_cycle1.waypoints"
  system: string;
  path: string;
  ext: "json" | "waypoints" | "plan" | "kmz";
}

const MISSION_EXTENSIONS: MissionFileOption["ext"][] = ["json", "waypoints", "plan", "kmz"];

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i === -1 ? "" : name.slice(i + 1).toLowerCase();
}

// --- generic mission JSON (flight_plan_json / mission_json outputs) ------

// Handles both shapes this platform produces:
//   - spray (jobs/flight_plan_generator): { cycles: [{ cycle_id, waypoints: [{seq,lat,lon,alt,action}] }] }
//   - scouting (jobs/scouting_mission_generator): { home: {lat,lon}, waypoints: [{lat,lon,is_capture_stop}] }
export function missionJsonToPoints(data: any): MissionPoint[] {
  if (Array.isArray(data?.cycles)) {
    const pts: MissionPoint[] = [];
    let seq = 0;
    for (const cycle of data.cycles) {
      for (const wp of cycle?.waypoints || []) {
        if (!Number.isFinite(wp?.lat) || !Number.isFinite(wp?.lon)) continue;
        pts.push({ lat: wp.lat, lon: wp.lon, seq: seq++, action: wp.action });
      }
    }
    return pts;
  }
  if (Array.isArray(data?.waypoints)) {
    const pts: MissionPoint[] = [];
    let seq = 0;
    const home = data.home;
    if (Number.isFinite(home?.lat) && Number.isFinite(home?.lon)) {
      pts.push({ lat: home.lat, lon: home.lon, seq: seq++, action: "HOME" });
    }
    for (const wp of data.waypoints) {
      if (!Number.isFinite(wp?.lat) || !Number.isFinite(wp?.lon)) continue;
      pts.push({ lat: wp.lat, lon: wp.lon, seq: seq++, action: wp.is_capture_stop ? "CAPTURE" : "TRANSIT" });
    }
    if (Number.isFinite(home?.lat) && Number.isFinite(home?.lon)) {
      pts.push({ lat: home.lat, lon: home.lon, seq: seq++, action: "RTL" });
    }
    return pts;
  }
  return [];
}

// --- QGC WPL 120 (.waypoints) — ArduPilot/MAVProxy/QGC plain-text mission --

const MAV_CMD_NAV_WAYPOINT = 16;
const MAV_CMD_NAV_TAKEOFF = 22;
const MAV_CMD_NAV_RETURN_TO_LAUNCH = 20;

function waypointsTextToPoints(text: string): MissionPoint[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
  let home: [number, number] | null = null;
  let hasRtl = false;
  const pts: MissionPoint[] = [];
  let seq = 0;
  // First line is the "QGC WPL 120" header — skip it.
  for (const line of lines.slice(1)) {
    const cols = line.split("\t");
    if (cols.length < 11) continue;
    const command = Number(cols[3]);
    const holdTime = Number(cols[4]);
    const lon = Number(cols[8]);
    const lat = Number(cols[9]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    if (command === MAV_CMD_NAV_TAKEOFF) {
      home = [lat, lon];
      pts.push({ lat, lon, seq: seq++, action: "TAKEOFF" });
    } else if (command === MAV_CMD_NAV_WAYPOINT) {
      pts.push({ lat, lon, seq: seq++, action: holdTime > 0 ? "CAPTURE" : "WAYPOINT" });
    } else if (command === MAV_CMD_NAV_RETURN_TO_LAUNCH) {
      hasRtl = true;
    }
  }
  if (hasRtl && home) pts.push({ lat: home[0], lon: home[1], seq: seq++, action: "RTL" });
  return pts;
}

// --- QGroundControl .plan (JSON) ------------------------------------------

function planJsonToPoints(data: any): MissionPoint[] {
  const items: any[] = data?.mission?.items || [];
  const home: any[] | undefined = data?.mission?.plannedHomePosition;
  const pts: MissionPoint[] = [];
  let seq = 0;
  for (const item of items) {
    if (item.command === MAV_CMD_NAV_TAKEOFF || item.command === MAV_CMD_NAV_WAYPOINT) {
      const p = item.params || [];
      const lat = p[4];
      const lon = p[5];
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const action = item.command === MAV_CMD_NAV_TAKEOFF ? "TAKEOFF" : (Number(p[0]) > 0 ? "CAPTURE" : "WAYPOINT");
      pts.push({ lat, lon, seq: seq++, action });
    } else if (item.command === MAV_CMD_NAV_RETURN_TO_LAUNCH && Array.isArray(home)) {
      pts.push({ lat: home[0], lon: home[1], seq: seq++, action: "RTL" });
    }
  }
  return pts;
}

// --- .kmz (zipped KML) -----------------------------------------------------

const ZIP_EOCD_SIG = 0x06054b50;
const ZIP_CDH_SIG = 0x02014b50;
const ZIP_LFH_SIG = 0x04034b50;

// Minimal ZIP reader — enough to pull one named entry's bytes out of the
// small, single/few-entry archives write_kmz_file() produces (mission_generator.py
// / generate_flight_plan.py), not a general-purpose ZIP library.
async function extractZipEntry(buf: ArrayBuffer, matchName: (name: string) => boolean): Promise<Uint8Array | null> {
  const bytes = new Uint8Array(buf);
  const view = new DataView(buf);

  let eocdOffset = -1;
  const scanFrom = Math.max(0, bytes.length - 65557);
  for (let i = bytes.length - 22; i >= scanFrom; i--) {
    if (view.getUint32(i, true) === ZIP_EOCD_SIG) { eocdOffset = i; break; }
  }
  if (eocdOffset === -1) return null;

  const entryCount = view.getUint16(eocdOffset + 10, true);
  let offset = view.getUint32(eocdOffset + 16, true);

  for (let i = 0; i < entryCount; i++) {
    if (view.getUint32(offset, true) !== ZIP_CDH_SIG) break;
    const compressionMethod = view.getUint16(offset + 10, true);
    const compressedSize = view.getUint32(offset + 20, true);
    const fileNameLen = view.getUint16(offset + 28, true);
    const extraLen = view.getUint16(offset + 30, true);
    const commentLen = view.getUint16(offset + 32, true);
    const localHeaderOffset = view.getUint32(offset + 42, true);
    const fileName = new TextDecoder().decode(bytes.subarray(offset + 46, offset + 46 + fileNameLen));

    if (matchName(fileName)) {
      if (view.getUint32(localHeaderOffset, true) !== ZIP_LFH_SIG) return null;
      const lfNameLen = view.getUint16(localHeaderOffset + 26, true);
      const lfExtraLen = view.getUint16(localHeaderOffset + 28, true);
      const dataStart = localHeaderOffset + 30 + lfNameLen + lfExtraLen;
      const compressed = bytes.subarray(dataStart, dataStart + compressedSize);

      if (compressionMethod === 0) return compressed; // stored
      if (compressionMethod === 8) {
        // Deflate, raw (no zlib/gzip wrapper) — supported natively by modern
        // browsers, so no zip/inflate library dependency is needed.
        const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
        return new Uint8Array(await new Response(stream).arrayBuffer());
      }
      throw new Error(`Unsupported .kmz compression method ${compressionMethod}`);
    }

    offset += 46 + fileNameLen + extraLen + commentLen;
  }
  return null;
}

async function kmzToPoints(buf: ArrayBuffer): Promise<MissionPoint[]> {
  const kmlBytes = await extractZipEntry(buf, (name) => name.toLowerCase().endsWith(".kml"));
  if (!kmlBytes) return [];
  const kmlText = new TextDecoder("utf-8").decode(kmlBytes);
  const doc = new DOMParser().parseFromString(kmlText, "application/xml");

  // write_kmz_file() (mission_generator.py / generate_flight_plan.py) writes
  // exactly one Placemark with a LineString covering every waypoint in
  // order, plus separate Point Placemarks marking capture stops — so the
  // LineString alone gives the full ordered path, and we only need the
  // capture Placemarks to flag which of those points are capture stops.
  const placemarks = Array.from(doc.getElementsByTagName("Placemark"));
  let lineCoordsRaw: string | null = null;
  const captureCoordsRaw = new Set<string>();
  for (const pm of placemarks) {
    const name = pm.getElementsByTagName("name")[0]?.textContent || "";
    const lineEl = pm.getElementsByTagName("LineString")[0];
    if (lineEl) {
      lineCoordsRaw = lineEl.getElementsByTagName("coordinates")[0]?.textContent || lineCoordsRaw;
      continue;
    }
    const pointEl = pm.getElementsByTagName("Point")[0];
    if (pointEl && /capture/i.test(name)) {
      const raw = pointEl.getElementsByTagName("coordinates")[0]?.textContent?.trim();
      if (raw) captureCoordsRaw.add(raw);
    }
  }
  if (!lineCoordsRaw) return [];

  const tuples = lineCoordsRaw.trim().split(/\s+/).filter(Boolean);
  return tuples.map((t, i) => {
    const [lon, lat] = t.split(",").map(Number);
    return { lat, lon, seq: i, action: captureCoordsRaw.has(t) ? "CAPTURE" : undefined };
  }).filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon));
}

// --- dispatch by extension + fetch helpers --------------------------------

export async function missionFileToPoints(ext: MissionFileOption["ext"], buf: ArrayBuffer): Promise<MissionPoint[]> {
  switch (ext) {
    case "json":
      return missionJsonToPoints(JSON.parse(new TextDecoder("utf-8").decode(buf)));
    case "waypoints":
      return waypointsTextToPoints(new TextDecoder("utf-8").decode(buf));
    case "plan":
      return planJsonToPoints(JSON.parse(new TextDecoder("utf-8").decode(buf)));
    case "kmz":
      return kmzToPoints(buf);
    default:
      return [];
  }
}

// Lists a wired mission_files_dir's contents one level deep — flat for
// scouting_mission's output, or one subfolder per format for
// mission_export's (ardupilot/, px4/, generic_csv/, dji/) — and returns
// every recognized mission file found, for a picker.
export async function listMissionFiles(system: string, basePath: string): Promise<MissionFileOption[]> {
  const out: MissionFileOption[] = [];
  const list = async (path: string) => {
    const res = await apiFetch(`/api/tapis-files/list?system=${encodeURIComponent(system)}&path=${encodeURIComponent(path)}`);
    if (!res.ok) return [];
    const data = await res.json().catch(() => ({}));
    return Array.isArray(data.result) ? data.result : [];
  };

  const top = await list(basePath);
  for (const entry of top) {
    const entryPath = `${basePath.replace(/\/$/, "")}/${entry.name}`;
    if (entry.type === "dir") {
      const sub = await list(entryPath);
      for (const subEntry of sub) {
        const ext = extOf(subEntry.name);
        if ((MISSION_EXTENSIONS as string[]).includes(ext)) {
          out.push({ label: `${entry.name}/${subEntry.name}`, system, path: `${entryPath}/${subEntry.name}`, ext: ext as MissionFileOption["ext"] });
        }
      }
    } else {
      const ext = extOf(entry.name);
      if ((MISSION_EXTENSIONS as string[]).includes(ext)) {
        out.push({ label: entry.name, system, path: entryPath, ext: ext as MissionFileOption["ext"] });
      }
    }
  }
  return out;
}

// --- MissionPoint[] -> GeoJSON, for map rendering -------------------------

// A path LineString (properties.kind = "path") plus one Point feature per
// waypoint, with properties.role = "start" | "end" | "waypoint" so the map
// can give the first/last point a distinct icon from the rest.
export function missionPointsToFeatureCollection(points: MissionPoint[]): any | null {
  if (points.length === 0) return null;
  const lineFeature = {
    type: "Feature",
    properties: { kind: "path" },
    geometry: { type: "LineString", coordinates: points.map((p) => [p.lon, p.lat]) },
  };
  const pointFeatures = points.map((p, i) => ({
    type: "Feature",
    properties: {
      kind: "waypoint",
      seq: p.seq,
      action: p.action,
      role: i === 0 ? "start" : i === points.length - 1 ? "end" : "waypoint",
    },
    geometry: { type: "Point", coordinates: [p.lon, p.lat] },
  }));
  return { type: "FeatureCollection", features: [lineFeature, ...pointFeatures] };
}

// Evenly-sampled bearings along a LineString's coordinates, for drawing
// direction-of-travel arrowheads without cluttering the map with one per
// segment on a long mission.
export function sampleDirectionArrows(coordsLonLat: [number, number][], maxArrows = 24): { lat: number; lon: number; bearingDeg: number }[] {
  if (coordsLonLat.length < 2) return [];
  const segments = coordsLonLat.length - 1;
  const step = Math.max(1, Math.round(segments / maxArrows));
  const arrows: { lat: number; lon: number; bearingDeg: number }[] = [];
  for (let i = 0; i < segments; i += step) {
    const [lon1, lat1] = coordsLonLat[i];
    const [lon2, lat2] = coordsLonLat[i + 1];
    if (lat1 === lat2 && lon1 === lon2) continue;
    const midLat = (lat1 + lat2) / 2;
    const midLon = (lon1 + lon2) / 2;
    // Local flat-earth approximation (fine at field/mission scale, same as
    // the generators' own coordinate math) — atan2(dx, dy) with dx scaled by
    // cos(latitude) gives bearing clockwise from north.
    const dy = lat2 - lat1;
    const dx = (lon2 - lon1) * Math.cos((midLat * Math.PI) / 180);
    const bearingDeg = ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
    arrows.push({ lat: midLat, lon: midLon, bearingDeg });
  }
  return arrows;
}
