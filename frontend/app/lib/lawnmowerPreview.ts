// Client-side port of jobs/scouting_mission_generator/mission_generator.py's
// generate_lawnmower_path() — lets scoutingMission.tsx preview the ACTUAL
// boustrophedon survey pattern live on the map, clipped to the real traced
// boundary and starting point, rather than only the fixed-size illustrative
// SVG diagram. Same flat-earth approximation and same scanline polygon-clip
// algorithm as the Python original; keep the two in sync if that file's
// approach changes.
export interface LawnmowerPreview {
  points: [number, number][]; // ordered [lat, lon] — the flown path
  capturePoints: [number, number][]; // image mode only: every point above is a capture stop, so this equals `points`; empty in video mode
  pathLengthM: number; // home -> path -> home, for a quick sanity check against the mission's actual length
}

const EARTH_M_PER_DEG_LAT = 111320;
function mPerDegLon(refLatDeg: number): number {
  return 111320 * Math.cos((refLatDeg * Math.PI) / 180);
}
function toXY(lat: number, lon: number, refLat: number, refLon: number): [number, number] {
  return [(lon - refLon) * mPerDegLon(refLat), (lat - refLat) * EARTH_M_PER_DEG_LAT];
}
function toLatLon(x: number, y: number, refLat: number, refLon: number): [number, number] {
  return [refLat + y / EARTH_M_PER_DEG_LAT, refLon + x / mPerDegLon(refLat)];
}

// Even-odd scanline intersection of a vertical line x=const against a closed
// polygon ring — sorted [yStart, yEnd] "inside" segments, the same result
// shape as mission_generator.py's _segments_for_column (a shapely
// intersection), for a simple (non-self-intersecting) polygon.
function segmentsForColumn(ring: [number, number][], x: number): [number, number][] {
  const ys: number[] = [];
  const n = ring.length;
  for (let i = 0; i < n; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % n];
    if ((x1 <= x && x2 > x) || (x2 <= x && x1 > x)) {
      const t = (x - x1) / (x2 - x1);
      ys.push(y1 + t * (y2 - y1));
    }
  }
  ys.sort((a, b) => a - b);
  const segments: [number, number][] = [];
  for (let i = 0; i + 1 < ys.length; i += 2) segments.push([ys[i], ys[i + 1]]);
  return segments;
}

function densify(y0: number, y1: number, step: number | null): number[] {
  if (!step || step <= 0) return [y0, y1];
  const length = Math.abs(y1 - y0);
  if (length < 1e-6) return [y0];
  const n = Math.max(1, Math.ceil(length / step));
  const dir = y1 >= y0 ? 1 : -1;
  const ys = Array.from({ length: n }, (_, i) => y0 + dir * step * i);
  ys.push(y1);
  if (Math.abs(ys[ys.length - 1] - ys[ys.length - 2]) < 1e-6) ys.splice(-2, 1);
  return ys;
}

// boundaryLatLon: the traced polygon, [lat,lon][], 3+ points, any order.
// home: the starting point, [lat,lon] — also the projection's local origin.
// captureSpacingM: pass null/0 for video mode (sparse path, no capture dots);
// a positive value for image mode (every resulting point is a capture stop,
// matching mission_generator.py's is_stop = capture_spacing_m is not None).
export function computeLawnmowerPreview(
  boundaryLatLon: [number, number][],
  home: [number, number],
  lineSpacingM: number,
  captureSpacingM: number | null,
): LawnmowerPreview | null {
  if (boundaryLatLon.length < 3 || !(lineSpacingM > 0)) return null;
  const [refLat, refLon] = home;
  const ring = boundaryLatLon.map(([lat, lon]) => toXY(lat, lon, refLat, refLon));

  const xs = ring.map((p) => p[0]);
  const minx = Math.min(...xs);
  const maxx = Math.max(...xs);
  if (maxx - minx < 1e-6) return null;

  const nLines = Math.max(2, Math.ceil((maxx - minx) / lineSpacingM) + 1);
  const columnXs = Array.from({ length: nLines }, (_, i) => minx + (i * (maxx - minx)) / (nLines - 1));

  const columns: { x: number; segs: [number, number][] }[] = [];
  for (const x of columnXs) {
    const segs = segmentsForColumn(ring, x);
    if (segs.length > 0) columns.push({ x, segs });
  }
  if (columns.length === 0) return null;

  const first = columns[0];
  const bottomY = first.segs[0][0];
  const topY = first.segs[first.segs.length - 1][1];
  let ascending = Math.abs(0 - bottomY) <= Math.abs(0 - topY); // home is the xy origin

  const pathXY: [number, number][] = [];
  for (const { x, segs } of columns) {
    const orderedSegs = ascending ? segs : [...segs].reverse();
    for (const seg of orderedSegs) {
      const [yStart, yEnd] = ascending ? seg : ([seg[1], seg[0]] as [number, number]);
      for (const y of densify(yStart, yEnd, captureSpacingM)) pathXY.push([x, y]);
    }
    ascending = !ascending; // boustrophedon: flip direction each column
  }

  const points = pathXY.map(([x, y]) => toLatLon(x, y, refLat, refLon));
  const capturePoints = captureSpacingM ? points : [];

  let pathLengthM = 0;
  const full: [number, number][] = [home, ...points, home];
  for (let i = 0; i + 1 < full.length; i++) {
    const [lat1, lon1] = full[i];
    const [lat2, lon2] = full[i + 1];
    const dy = (lat2 - lat1) * EARTH_M_PER_DEG_LAT;
    const dx = (lon2 - lon1) * mPerDegLon((lat1 + lat2) / 2);
    pathLengthM += Math.hypot(dx, dy);
  }

  return { points, capturePoints, pathLengthM };
}
