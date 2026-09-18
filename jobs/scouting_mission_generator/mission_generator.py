"""
mission_generator.py

Generates a lawnmower (boustrophedon) survey mission for a Parrot ANAFI
drone, clipped to an arbitrary farm boundary polygon (not just a rectangle),
and exports it in several formats:

    .waypoints  - QGC WPL 120 plain-text mission (ArduPilot/MAVProxy/QGC)
    .plan       - QGroundControl JSON mission format
    .kmz        - Google Earth-compatible zipped KML, for a quick visual check
    .json       - generic internal format (used by fly_mission.py at runtime,
                  and handy if you want to feed the mission into your own
                  orchestrator UI without parsing MAVLink text)

------------------------------------------------------------------------
CAPTURE MODES
------------------------------------------------------------------------
    "image": the drone flies each scan line but pauses at fixed intervals
             (`capture_spacing_m`) to hold and capture a still photo. This
             mirrors the stop-and-hold pattern that was confirmed to work
             reliably on real ANAFI hardware in earlier testing on this
             project -- ANAFI's onboard flight-plan player does NOT reliably
             honor MAVLink camera-trigger commands like
             DO_SET_CAM_TRIGG_DIST, so the authoritative camera trigger is
             issued by fly_mission.py via the Olympe SDK companion script
             while it watches the drone's progress through the mission, not
             by the mission file alone. We still embed DO_SET_CAM_TRIGG_DIST
             in the file for GCS software that understands it and as a
             fallback, but do not rely on it for ANAFI.

    "video":  the drone flies the lines without stopping; fly_mission.py
              starts recording right after takeoff and stops it right
              before RTL.

------------------------------------------------------------------------
POLYGON HANDLING (important, please read)
------------------------------------------------------------------------
Scan lines are generated axis-aligned (running north-south) across the
polygon's bounding box, spaced `line_spacing_m` apart in the east-west
direction, then each candidate line is intersected with the actual
boundary polygon. For a convex or mildly-concave field this produces the
expected back-and-forth coverage. For a strongly concave or multi-lobed
polygon, a single scan column can intersect the boundary in more than one
disconnected segment -- we handle that by visiting every segment in that
column in order and adding a short direct connector between them, which
is a reasonable simplification but is NOT a globally-optimal coverage
path. For unusual field shapes, look at the generated .kmz in Google
Earth before flying.

The "approximate starting point" you provide is used to (a) set the
TAKEOFF / RTL home position, and (b) pick which corner of the pattern the
lawnmower starts from, so the first leg begins near where you'll actually
be standing.
"""

from __future__ import annotations

import json
import math
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from shapely.geometry import LineString, MultiLineString, Point, Polygon

# ---------------------------------------------------------------------------
# MAVLink command / frame constants used when writing .waypoints and .plan
# ---------------------------------------------------------------------------
MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_DO_SET_CAM_TRIGG_DIST = 206      # fallback only, see module docstring
MAV_CMD_VIDEO_START_CAPTURE = 2500
MAV_CMD_VIDEO_STOP_CAPTURE = 2501
MAV_CMD_IMAGE_START_CAPTURE = 2000
FRAME_GLOBAL = 0
FRAME_GLOBAL_RELATIVE_ALT = 3

EARTH_M_PER_DEG_LAT = 111320.0


def _m_per_deg_lon(ref_lat_deg: float) -> float:
    return 111320.0 * math.cos(math.radians(ref_lat_deg))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MissionWaypoint:
    lat: float
    lon: float
    alt_m: float
    is_capture_stop: bool = False       # true if drone should pause + shoot here
    hold_time_s: float = 0.0


@dataclass
class Mission:
    boundary: List[Tuple[float, float]]      # farm boundary, (lat, lon) list
    home: Tuple[float, float]                # approximate starting point
    altitude_m: float
    line_spacing_m: float
    capture_mode: str                        # "image" or "video"
    capture_spacing_m: Optional[float]        # required if capture_mode == "image"
    hold_time_s: float = 2.0
    waypoints: List[MissionWaypoint] = field(default_factory=list)

    def __post_init__(self):
        if self.capture_mode not in ("image", "video"):
            raise ValueError("capture_mode must be 'image' or 'video'")
        if self.capture_mode == "image" and not self.capture_spacing_m:
            raise ValueError("capture_spacing_m is required when capture_mode='image'")


# ---------------------------------------------------------------------------
# Coordinate projection helpers (local flat-earth approximation, fine for
# field-sized areas up to a few km across)
# ---------------------------------------------------------------------------

def _latlon_to_xy(lat, lon, ref_lat, ref_lon):
    m_lat = EARTH_M_PER_DEG_LAT
    m_lon = _m_per_deg_lon(ref_lat)
    return ((lon - ref_lon) * m_lon, (lat - ref_lat) * m_lat)


def _xy_to_latlon(x, y, ref_lat, ref_lon):
    m_lat = EARTH_M_PER_DEG_LAT
    m_lon = _m_per_deg_lon(ref_lat)
    return (ref_lat + y / m_lat, ref_lon + x / m_lon)


# ---------------------------------------------------------------------------
# Core pattern generation
# ---------------------------------------------------------------------------

def _polygon_to_xy(boundary_latlon, ref_lat, ref_lon) -> Polygon:
    pts_xy = [_latlon_to_xy(lat, lon, ref_lat, ref_lon) for lat, lon in boundary_latlon]
    poly = Polygon(pts_xy)
    if not poly.is_valid:
        poly = poly.buffer(0)  # common fix for self-touching/invalid rings
    return poly


def _segments_for_column(poly: Polygon, x: float, miny: float, maxy: float):
    """Intersect a vertical scan line at x=const with poly, return sorted
    list of (y_start, y_end) segments (ascending y)."""
    probe = LineString([(x, miny - 1.0), (x, maxy + 1.0)])
    inter = poly.intersection(probe)
    segments = []
    if inter.is_empty:
        return segments
    if isinstance(inter, LineString):
        (x0, y0), (x1, y1) = inter.coords[0], inter.coords[-1]
        segments.append(tuple(sorted((y0, y1))))
    elif isinstance(inter, MultiLineString):
        for geom in inter.geoms:
            (x0, y0), (x1, y1) = geom.coords[0], geom.coords[-1]
            segments.append(tuple(sorted((y0, y1))))
    segments.sort(key=lambda s: s[0])
    return segments


def _densify_leg(y0: float, y1: float, step: float) -> List[float]:
    """Return list of y-values from y0 to y1 (in that direction) spaced
    `step` apart, always including both endpoints."""
    if step is None or step <= 0:
        return [y0, y1]
    length = abs(y1 - y0)
    if length < 1e-6:
        return [y0]
    n = max(1, math.ceil(length / step))
    direction = 1.0 if y1 >= y0 else -1.0
    ys = [y0 + direction * step * i for i in range(n)]
    ys.append(y1)
    # dedupe near-duplicate final point
    if abs(ys[-1] - ys[-2]) < 1e-6:
        ys.pop(-2)
    return ys


def generate_lawnmower_path(
    boundary_latlon: List[Tuple[float, float]],
    home_latlon: Tuple[float, float],
    line_spacing_m: float,
    capture_spacing_m: Optional[float] = None,
) -> List[Tuple[float, float, bool]]:
    """
    Returns an ordered list of (lat, lon, is_capture_stop) covering the
    polygon in a boustrophedon pattern, clipped to the boundary.

    If capture_spacing_m is given, extra points are inserted along each
    flown leg at that spacing and flagged is_capture_stop=True (used for
    "image" mode). If None, only the leg endpoints are emitted and none
    are flagged as capture stops (used for "video" mode).
    """
    ref_lat, ref_lon = home_latlon
    poly = _polygon_to_xy(boundary_latlon, ref_lat, ref_lon)
    minx, miny, maxx, maxy = poly.bounds

    n_lines = max(2, math.ceil((maxx - minx) / line_spacing_m) + 1)
    xs = [minx + i * (maxx - minx) / (n_lines - 1) for i in range(n_lines)]

    # Decide starting side: whichever end of the first column is nearer home
    home_xy = _latlon_to_xy(ref_lat, ref_lon, ref_lat, ref_lon)  # = (0, 0)

    columns = []  # list of list-of-segments, one per x
    for x in xs:
        segs = _segments_for_column(poly, x, miny, maxy)
        if segs:
            columns.append((x, segs))

    if not columns:
        raise ValueError(
            "No scan lines intersected the boundary polygon -- check that "
            "the polygon coordinates are valid and line_spacing_m isn't "
            "larger than the field width."
        )

    # Determine whether to start ascending (south->north) or descending,
    # based on proximity of home to the bottom vs top of the first column.
    first_x, first_segs = columns[0]
    bottom_y = first_segs[0][0]
    top_y = first_segs[-1][1]
    dist_to_bottom = abs(home_xy[1] - bottom_y)
    dist_to_top = abs(home_xy[1] - top_y)
    ascending = dist_to_bottom <= dist_to_top

    path_xy: List[Tuple[float, float, bool]] = []  # (x, y, is_capture_stop)

    for col_idx, (x, segs) in enumerate(columns):
        ordered_segs = segs if ascending else list(reversed(segs))
        for seg in ordered_segs:
            y_start, y_end = seg if ascending else (seg[1], seg[0])
            ys = _densify_leg(y_start, y_end, capture_spacing_m)
            for i, y in enumerate(ys):
                is_stop = capture_spacing_m is not None
                path_xy.append((x, y, is_stop))
        ascending = not ascending  # boustrophedon: flip direction each column

    path_latlon = [
        (*_xy_to_latlon(x, y, ref_lat, ref_lon), is_stop) for x, y, is_stop in path_xy
    ]
    return path_latlon


def build_mission(
    boundary_latlon: List[Tuple[float, float]],
    home_latlon: Tuple[float, float],
    altitude_m: float,
    line_spacing_m: float,
    capture_mode: str,
    capture_spacing_m: Optional[float] = None,
    hold_time_s: float = 2.0,
) -> Mission:
    mission = Mission(
        boundary=boundary_latlon,
        home=home_latlon,
        altitude_m=altitude_m,
        line_spacing_m=line_spacing_m,
        capture_mode=capture_mode,
        capture_spacing_m=capture_spacing_m if capture_mode == "image" else None,
        hold_time_s=hold_time_s,
    )
    raw_path = generate_lawnmower_path(
        boundary_latlon,
        home_latlon,
        line_spacing_m,
        capture_spacing_m=capture_spacing_m if capture_mode == "image" else None,
    )
    mission.waypoints = [
        MissionWaypoint(
            lat=lat,
            lon=lon,
            alt_m=altitude_m,
            is_capture_stop=is_stop,
            hold_time_s=hold_time_s if is_stop else 0.0,
        )
        for lat, lon, is_stop in raw_path
    ]
    return mission


def path_length_m(mission: Mission) -> float:
    total = 0.0
    pts = [mission.home] + [(w.lat, w.lon) for w in mission.waypoints] + [mission.home]
    ref_lat = mission.home[0]
    m_lat, m_lon = EARTH_M_PER_DEG_LAT, _m_per_deg_lon(ref_lat)
    for (lat1, lon1), (lat2, lon2) in zip(pts[:-1], pts[1:]):
        dy = (lat2 - lat1) * m_lat
        dx = (lon2 - lon1) * m_lon
        total += math.hypot(dx, dy)
    return total


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------

def write_waypoints_file(mission: Mission, out_path: str) -> None:
    """QGC WPL 120 plain-text format. Column order X=lon, Y=lat (verified
    against real coordinates earlier on this project)."""
    home_lat, home_lon = mission.home
    lines = ["QGC WPL 120"]
    seq = 0

    def add(current, frame, command, p1, p2, p3, p4, lon, lat, alt, autocontinue=1):
        nonlocal seq
        lines.append("\t".join(str(v) for v in
                                [seq, current, frame, command, p1, p2, p3, p4, lon, lat, alt, autocontinue]))
        seq += 1

    add(1, FRAME_GLOBAL_RELATIVE_ALT, MAV_CMD_NAV_TAKEOFF,
        15.0, 0.0, 0.0, 0.0, home_lon, home_lat, mission.altitude_m)

    if mission.capture_mode == "video":
        add(0, FRAME_GLOBAL_RELATIVE_ALT, MAV_CMD_VIDEO_START_CAPTURE,
            0.0, 0.0, 0.0, 0.0, home_lon, home_lat, mission.altitude_m)
    else:
        add(0, FRAME_GLOBAL_RELATIVE_ALT, MAV_CMD_DO_SET_CAM_TRIGG_DIST,
            mission.capture_spacing_m, 0.0, 0.0, 0.0, home_lon, home_lat, mission.altitude_m)

    for wp in mission.waypoints:
        hold = wp.hold_time_s if wp.is_capture_stop else 0.0
        add(0, FRAME_GLOBAL_RELATIVE_ALT, MAV_CMD_NAV_WAYPOINT,
            hold, 0.0, 0.0, 0.0, wp.lon, wp.lat, wp.alt_m)

    if mission.capture_mode == "video":
        add(0, FRAME_GLOBAL_RELATIVE_ALT, MAV_CMD_VIDEO_STOP_CAPTURE,
            0.0, 0.0, 0.0, 0.0, home_lon, home_lat, mission.altitude_m)

    add(0, FRAME_GLOBAL, MAV_CMD_NAV_RETURN_TO_LAUNCH,
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def write_plan_file(mission: Mission, out_path: str) -> None:
    """QGroundControl .plan JSON mission format (minimal, nav-only items;
    QGC's schema for camera items varies by version, so capture behavior
    for this format is informational only -- the authoritative capture
    logic lives in fly_mission.py)."""
    home_lat, home_lon = mission.home
    items = []
    seq = 1

    def nav_item(command, lat, lon, alt, param1=0.0):
        nonlocal seq
        item = {
            "AMSLAltAboveTerrain": None,
            "Altitude": alt,
            "AltitudeMode": 1,
            "autoContinue": True,
            "command": command,
            "doJumpId": seq,
            "frame": 3,
            "params": [param1, 0, 0, 0, lat, lon, alt],
            "type": "SimpleItem",
        }
        seq += 1
        return item

    items.append(nav_item(MAV_CMD_NAV_TAKEOFF, home_lat, home_lon, mission.altitude_m))
    for wp in mission.waypoints:
        hold = wp.hold_time_s if wp.is_capture_stop else 0.0
        items.append(nav_item(MAV_CMD_NAV_WAYPOINT, wp.lat, wp.lon, wp.alt_m, param1=hold))
    items.append({
        "autoContinue": True,
        "command": MAV_CMD_NAV_RETURN_TO_LAUNCH,
        "doJumpId": seq,
        "frame": 2,
        "params": [0, 0, 0, 0, 0, 0, 0],
        "type": "SimpleItem",
    })

    plan = {
        "fileType": "Plan",
        "geoFence": {"circles": [], "polygons": [], "version": 2},
        "groundStation": "ParrotAnafiOrchestrator",
        "mission": {
            "cruiseSpeed": 5,
            "hoverSpeed": 2,
            "firmwareType": 12,
            "vehicleType": 2,
            "items": items,
            "plannedHomePosition": [home_lat, home_lon, mission.altitude_m],
            "version": 2,
        },
        "rallyPoints": {"points": [], "version": 2},
        "version": 1,
    }
    with open(out_path, "w") as f:
        json.dump(plan, f, indent=2)


def write_kmz_file(mission: Mission, out_path: str) -> None:
    """Zipped KML for a quick visual sanity check in Google Earth / most
    map viewers that accept KMZ."""
    home_lat, home_lon = mission.home
    coords_str = " ".join(f"{home_lon},{home_lat},0")
    path_coords = " ".join(f"{wp.lon},{wp.lat},{wp.alt_m}" for wp in mission.waypoints)
    stop_marks = "\n".join(
        f"""<Placemark><name>Capture {i}</name>
        <Point><coordinates>{wp.lon},{wp.lat},{wp.alt_m}</coordinates></Point></Placemark>"""
        for i, wp in enumerate(mission.waypoints) if wp.is_capture_stop
    )
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>ANAFI Farm Mission</name>
  <Placemark>
    <name>Home / Takeoff</name>
    <Point><coordinates>{coords_str}</coordinates></Point>
  </Placemark>
  <Placemark>
    <name>Flight Path ({mission.capture_mode})</name>
    <LineString>
      <altitudeMode>relativeToGround</altitudeMode>
      <coordinates>{path_coords}</coordinates>
    </LineString>
  </Placemark>
  {stop_marks}
</Document>
</kml>
"""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)


def write_generic_json(mission: Mission, out_path: str) -> None:
    """Internal generic format -- this is what fly_mission.py actually
    reads at runtime, since it needs the is_capture_stop / hold_time_s
    metadata that .waypoints/.plan can only encode indirectly."""
    data = {
        "home": {"lat": mission.home[0], "lon": mission.home[1]},
        "altitude_m": mission.altitude_m,
        "line_spacing_m": mission.line_spacing_m,
        "capture_mode": mission.capture_mode,
        "capture_spacing_m": mission.capture_spacing_m,
        "hold_time_s": mission.hold_time_s,
        "boundary": [{"lat": lat, "lon": lon} for lat, lon in mission.boundary],
        "waypoints": [
            {
                "lat": wp.lat,
                "lon": wp.lon,
                "alt_m": wp.alt_m,
                "is_capture_stop": wp.is_capture_stop,
                "hold_time_s": wp.hold_time_s,
            }
            for wp in mission.waypoints
        ],
    }
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)


def export_all(mission: Mission, out_dir: str, base_name: str = "mission") -> dict:
    import os
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "waypoints": os.path.join(out_dir, f"{base_name}.waypoints"),
        "plan": os.path.join(out_dir, f"{base_name}.plan"),
        "kmz": os.path.join(out_dir, f"{base_name}.kmz"),
        "json": os.path.join(out_dir, f"{base_name}.json"),
    }
    write_waypoints_file(mission, paths["waypoints"])
    write_plan_file(mission, paths["plan"])
    write_kmz_file(mission, paths["kmz"])
    write_generic_json(mission, paths["json"])
    return paths
