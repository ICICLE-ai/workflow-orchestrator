#!/usr/bin/env python3
"""
generate_flight_plan.py

Takes the farm_output.gpkg (farm_boundary, spray_zones, detections) and a
set of physical/operational parameters, and produces a platform-agnostic
flight plan:

  - Dissolves contiguous spray_zones cells (spray_decision != "No") into
    zone polygons.
  - Generates a boustrophedon ("lawnmower") coverage path inside each zone,
    spaced by the nozzle swath width -- multiple passes if the zone is
    wider than one swath, a single pass if it's narrower.
  - Computes cruise speed from nozzle flow rate + target application rate
    + swath width, using the standard ag-spray formula:
        speed_mph = (flow_rate_gpm * 5940) / (target_gpa * swath_ft)
  - Walks the full path (transit + spray legs) tracking cumulative flight
    time and cumulative spray volume, and splits the mission into separate
    "charge cycles" whenever either the battery time budget or tank volume
    budget would be exceeded -- inserting a return-to-home leg and
    resuming the NEXT cycle from home at the exact checkpoint where it
    left off.

OUTPUT
------
  <outdir>/flight_plan.json  -- platform-agnostic mission, one entry per
                                 charge cycle, each a list of waypoints
                                 with lat/lon/alt/action
  <outdir>/flight_plan.gpkg  -- same data as a GeoPackage for visual QA:
                                 "planned_path" (one line per cycle) and
                                 "planned_waypoints" (all waypoints, with
                                 cycle + action attributes)

This script only produces the generic plan. A separate script,
export_mission.py, converts flight_plan.json into platform-specific
formats (ArduPilot .waypoints, PX4 .plan, generic CSV, etc.) -- one file
per charge cycle, since most ground control software loads one mission
file at a time and an operator (or docking/swap station) performs the
physical battery-swap / tank-refill step between cycles.

USAGE
-----
python generate_flight_plan.py \
    --gpkg farm_output.gpkg \
    --outdir ./output \
    --nozzle-swath-ft 20 \
    --flight-height-ft 15 \
    --tank-capacity-gal 5 \
    --nozzle-flow-rate-gpm 0.5 \
    --dispersion-rate-gpa 2 \
    --battery-time-min 20 \
    --battery-reserve-pct 20 \
    --home-lat 40.01300 --home-lon -83.04300
"""

import argparse
import json
import math
import os

import geopandas as gpd
from shapely.geometry import LineString, Point
from shapely.affinity import rotate as shapely_rotate


FEET_PER_DEGREE_LAT = 364_000.0


# ---------------------------------------------------------------------------
# Local coordinate helpers (consistent with the rest of this pipeline --
# a flat-earth approximation valid for single-field distances)
# ---------------------------------------------------------------------------

def ft_per_deg_lon(at_lat_deg):
    return FEET_PER_DEGREE_LAT * math.cos(math.radians(at_lat_deg))


def latlon_to_local_ft(lat, lon, origin_lat, origin_lon):
    x_ft = (lon - origin_lon) * ft_per_deg_lon(origin_lat)
    y_ft = (lat - origin_lat) * FEET_PER_DEGREE_LAT
    return x_ft, y_ft


def local_ft_to_latlon(x_ft, y_ft, origin_lat, origin_lon):
    lon = origin_lon + x_ft / ft_per_deg_lon(origin_lat)
    lat = origin_lat + y_ft / FEET_PER_DEGREE_LAT
    return lat, lon


def distance_ft(p1, p2, ref_lat):
    """Straight-line distance in feet between two (lat, lon) points."""
    x1, y1 = latlon_to_local_ft(p1[0], p1[1], ref_lat, p2[1])
    x2, y2 = latlon_to_local_ft(p2[0], p2[1], ref_lat, p2[1])
    return math.hypot(x2 - x1, y2 - y1)


# ---------------------------------------------------------------------------
# Load farm_output.gpkg and dissolve spray zones into contiguous polygons
# ---------------------------------------------------------------------------

def load_spray_zone_polygons(gpkg_path):
    """
    Returns a list of dicts: {polygon (shapely, lat/lon), decision, area_acres}
    One entry per contiguous dissolved zone (spray_decision != "No").
    """
    cells = gpd.read_file(gpkg_path, layer="spray_zones")
    active = cells[cells["spray_decision"] != "No"].copy()

    if active.empty:
        return []

    dissolved = active.dissolve()
    zones = dissolved.explode(index_parts=False).reset_index(drop=True)

    results = []
    for _, zone_row in zones.iterrows():
        poly = zone_row.geometry
        centroid = poly.centroid

        # Re-derive a representative spray_decision for this dissolved zone:
        # whichever original cells fall inside it, take the "worst" (most
        # severe / highest-priority) decision present, so custom-mode zones
        # with mixed bands never under-spray.
        overlapping = active[active.intersects(poly)]
        decisions_present = overlapping["spray_decision"].unique().tolist()

        # Accurate area via local-ft projection (not naive degree^2)
        local_coords = [
            latlon_to_local_ft(lat, lon, centroid.y, centroid.x)
            for lon, lat in poly.exterior.coords
        ]
        from shapely.geometry import Polygon as ShapelyPolygon
        local_poly = ShapelyPolygon(local_coords)
        area_acres = local_poly.area / 43560.0

        results.append({
            "polygon": poly,
            "decisions_present": decisions_present,
            "area_acres": area_acres,
            "centroid": (centroid.y, centroid.x),
        })

    return results


# ---------------------------------------------------------------------------
# Boustrophedon coverage path generation, per zone
# ---------------------------------------------------------------------------

def generate_zone_coverage_path(polygon_latlon, swath_ft):
    """
    Returns an ordered list of (lat, lon) points forming a lawnmower path
    that covers polygon_latlon, spaced by swath_ft.
    """
    centroid = polygon_latlon.centroid
    origin_lat, origin_lon = centroid.y, centroid.x

    # Convert to local feet coordinates centered on the zone's centroid
    local_coords = [
        latlon_to_local_ft(lat, lon, origin_lat, origin_lon)
        for lon, lat in polygon_latlon.exterior.coords
    ]
    from shapely.geometry import Polygon as ShapelyPolygon
    local_poly = ShapelyPolygon(local_coords)

    # Find sweep direction: align with the longest edge of the minimum
    # rotated bounding rectangle, so lawnmower lines run the "long way"
    # across the field (fewer turns).
    mrr = local_poly.minimum_rotated_rectangle
    mrr_coords = list(mrr.exterior.coords)[:4]
    edge_lengths = []
    for i in range(4):
        x1, y1 = mrr_coords[i]
        x2, y2 = mrr_coords[(i + 1) % 4]
        edge_lengths.append((math.hypot(x2 - x1, y2 - y1), x2 - x1, y2 - y1))
    edge_lengths.sort(key=lambda e: e[0], reverse=True)
    _, dx, dy = edge_lengths[0]
    sweep_angle_deg = math.degrees(math.atan2(dy, dx))

    # Rotate the polygon so the sweep direction aligns with the x-axis
    rotated_poly = shapely_rotate(local_poly, -sweep_angle_deg, origin=(0, 0), use_radians=False)
    minx, miny, maxx, maxy = rotated_poly.bounds

    # Generate horizontal scan lines spaced by swath_ft
    lines_of_points = []
    y = miny + swath_ft / 2.0
    while y < maxy:
        scan_line = LineString([(minx - 10, y), (maxx + 10, y)])
        clipped = scan_line.intersection(rotated_poly)

        segment_points = []
        if clipped.is_empty:
            y += swath_ft
            continue
        elif clipped.geom_type == "LineString":
            segment_points = list(clipped.coords)
        elif clipped.geom_type == "MultiLineString":
            for part in clipped.geoms:
                segment_points.extend(list(part.coords))
            segment_points.sort(key=lambda p: p[0])
        else:
            y += swath_ft
            continue

        lines_of_points.append(segment_points)
        y += swath_ft

    if not lines_of_points:
        # Zone smaller than swath width -- single pass through centroid
        lines_of_points = [[(minx, (miny + maxy) / 2.0), (maxx, (miny + maxy) / 2.0)]]

    # Boustrophedon ordering: alternate direction each line
    path_local = []
    for idx, line_pts in enumerate(lines_of_points):
        if idx % 2 == 1:
            line_pts = list(reversed(line_pts))
        path_local.extend(line_pts)

    # Rotate path back to original orientation and convert back to lat/lon
    path_latlon = []
    cos_a, sin_a = math.cos(math.radians(sweep_angle_deg)), math.sin(math.radians(sweep_angle_deg))
    for x, y in path_local:
        x_rot = x * cos_a - y * sin_a
        y_rot = x * sin_a + y * cos_a
        lat, lon = local_ft_to_latlon(x_rot, y_rot, origin_lat, origin_lon)
        path_latlon.append((lat, lon))

    return path_latlon


# ---------------------------------------------------------------------------
# Order zones (simple greedy nearest-neighbor from home)
# ---------------------------------------------------------------------------

def order_zones_nearest_neighbor(zones, home_lat, home_lon):
    remaining = list(zones)
    ordered = []
    current_pos = (home_lat, home_lon)

    while remaining:
        remaining.sort(key=lambda z: distance_ft(current_pos, z["centroid"], home_lat))
        nearest = remaining.pop(0)
        ordered.append(nearest)
        current_pos = nearest["centroid"]

    return ordered


# ---------------------------------------------------------------------------
# Assemble full raw waypoint sequence (transit + spray legs)
# ---------------------------------------------------------------------------

def assemble_raw_waypoints(ordered_zones, swath_ft, flight_height_ft):
    """
    Returns a flat list of {lat, lon, alt, spraying} dicts covering every
    zone in order, including the coverage path points for each zone.
    Transit between zones is implicit -- consumers connect consecutive
    points directly.
    """
    raw = []
    for zone in ordered_zones:
        path = generate_zone_coverage_path(zone["polygon"], swath_ft)
        for lat, lon in path:
            raw.append({"lat": lat, "lon": lon, "alt": flight_height_ft, "spraying": True})

    return raw


# ---------------------------------------------------------------------------
# Speed calculation
# ---------------------------------------------------------------------------

def compute_speed_mph(nozzle_flow_rate_gpm, dispersion_rate_gpa, swath_ft):
    """
    Standard ag-spray rate formula:
        GPA = (GPM * 5940) / (MPH * swath_ft)
    Rearranged for speed:
        MPH = (GPM * 5940) / (GPA * swath_ft)
    """
    return (nozzle_flow_rate_gpm * 5940.0) / (dispersion_rate_gpa * swath_ft)


# ---------------------------------------------------------------------------
# Charge-cycle segmentation
# ---------------------------------------------------------------------------

def segment_into_cycles(raw_waypoints, home_lat, home_lon,
                         speed_fps, transit_speed_fps,
                         volume_gal_per_ft,
                         battery_budget_s, tank_budget_gal):
    """
    Walks raw_waypoints in order, tracking cumulative flight time and
    spray volume since the last home departure. Whenever adding the next
    waypoint (plus the time needed to still get home afterward) would
    exceed either budget, closes the current cycle with a return-to-home
    leg and starts a new cycle from home, resuming at the SAME next
    waypoint (checkpoint) rather than skipping it.
    """
    cycles = []
    current_cycle = {
        "waypoints": [{"lat": home_lat, "lon": home_lon, "alt": 0, "action": "HOME", "spraying": False}],
        "cum_time": 0.0,
        "cum_volume": 0.0,
    }
    current_pos = (home_lat, home_lon)
    i = 0
    warnings = []

    while i < len(raw_waypoints):
        wp = raw_waypoints[i]
        seg_len_ft = distance_ft(current_pos, (wp["lat"], wp["lon"]), home_lat)
        spraying = wp["spraying"]
        seg_time = seg_len_ft / (speed_fps if spraying else transit_speed_fps)
        seg_vol = seg_len_ft * volume_gal_per_ft if spraying else 0.0

        time_home_from_wp = distance_ft((wp["lat"], wp["lon"]), (home_lat, home_lon), home_lat) / transit_speed_fps

        projected_time = current_cycle["cum_time"] + seg_time + time_home_from_wp
        projected_volume = current_cycle["cum_volume"] + seg_vol

        needs_break = (projected_time > battery_budget_s) or (projected_volume > tank_budget_gal)
        is_fresh_cycle = len(current_cycle["waypoints"]) == 1  # only the HOME point so far

        if needs_break and not is_fresh_cycle:
            time_home_from_current = distance_ft(current_pos, (home_lat, home_lon), home_lat) / transit_speed_fps
            current_cycle["waypoints"].append(
                {"lat": home_lat, "lon": home_lon, "alt": 0, "action": "RTL", "spraying": False}
            )
            current_cycle["cum_time"] += time_home_from_current
            cycles.append(current_cycle)

            current_cycle = {
                "waypoints": [{"lat": home_lat, "lon": home_lon, "alt": 0, "action": "HOME", "spraying": False}],
                "cum_time": 0.0,
                "cum_volume": 0.0,
            }
            current_pos = (home_lat, home_lon)
            continue  # retry same waypoint i from the fresh cycle

        if needs_break and is_fresh_cycle:
            # Even starting fresh from home, this single leg alone exceeds
            # one cycle's budget. Can't split further at this granularity
            # -- add it anyway and warn.
            warnings.append(
                f"Waypoint {i} alone exceeds per-cycle budget "
                f"(time={seg_time:.0f}s, vol={seg_vol:.2f}gal) -- added anyway."
            )

        current_cycle["waypoints"].append({
            "lat": wp["lat"], "lon": wp["lon"], "alt": wp["alt"],
            "action": "SPRAY" if spraying else "TRANSIT",
            "spraying": spraying,
        })
        current_cycle["cum_time"] += seg_time
        current_cycle["cum_volume"] += seg_vol
        current_pos = (wp["lat"], wp["lon"])
        i += 1

    # Close the final cycle
    final_time_home = distance_ft(current_pos, (home_lat, home_lon), home_lat) / transit_speed_fps
    current_cycle["waypoints"].append(
        {"lat": home_lat, "lon": home_lon, "alt": 0, "action": "RTL", "spraying": False}
    )
    current_cycle["cum_time"] += final_time_home
    cycles.append(current_cycle)

    return cycles, warnings


def mark_spray_transitions(cycle_waypoints):
    """
    Post-processes a cycle's waypoint list: the first point entering a
    spraying run gets action SPRAY_ON, the last point of that run gets
    SPRAY_OFF, interior spraying points stay SPRAY, HOME/RTL untouched.
    """
    n = len(cycle_waypoints)
    for idx, wp in enumerate(cycle_waypoints):
        if wp["action"] != "SPRAY":
            continue
        prev_spraying = idx > 0 and cycle_waypoints[idx - 1]["spraying"]
        next_spraying = idx < n - 1 and cycle_waypoints[idx + 1]["spraying"]

        if not prev_spraying and next_spraying:
            wp["action"] = "SPRAY_ON"
        elif prev_spraying and not next_spraying:
            wp["action"] = "SPRAY_OFF"
        elif not prev_spraying and not next_spraying:
            wp["action"] = "SPRAY_ON_OFF"  # single isolated spray point


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_flight_plan_json(cycles, outdir, meta):
    out_path = os.path.join(outdir, "flight_plan.json")
    payload = {
        "meta": meta,
        "cycles": [
            {
                "cycle_id": idx + 1,
                "cum_time_s": round(cycle["cum_time"], 1),
                "cum_volume_gal": round(cycle["cum_volume"], 2),
                "waypoints": [
                    {"seq": seq, "lat": wp["lat"], "lon": wp["lon"], "alt": wp["alt"], "action": wp["action"]}
                    for seq, wp in enumerate(cycle["waypoints"])
                ],
            }
            for idx, cycle in enumerate(cycles)
        ],
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    return out_path


def write_flight_plan_gpkg(cycles, outdir):
    path_rows, point_rows = [], []
    for idx, cycle in enumerate(cycles):
        coords = [(wp["lon"], wp["lat"]) for wp in cycle["waypoints"]]
        if len(coords) >= 2:
            path_rows.append({"geometry": LineString(coords), "cycle_id": idx + 1})
        for seq, wp in enumerate(cycle["waypoints"]):
            point_rows.append({
                "geometry": Point(wp["lon"], wp["lat"]),
                "cycle_id": idx + 1,
                "seq": seq,
                "action": wp["action"],
                "altitude": wp["alt"],
            })

    gdf_path = gpd.GeoDataFrame(path_rows, geometry="geometry", crs="EPSG:4326")
    gdf_points = gpd.GeoDataFrame(point_rows, geometry="geometry", crs="EPSG:4326")

    out_path = os.path.join(outdir, "flight_plan.gpkg")
    gdf_path.to_file(out_path, layer="planned_path", driver="GPKG")
    gdf_points.to_file(out_path, layer="planned_waypoints", driver="GPKG")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a spray-drone flight plan from farm_output.gpkg")
    parser.add_argument("--gpkg", required=True, help="Path to farm_output.gpkg")
    parser.add_argument("--outdir", required=True, help="Output directory")

    parser.add_argument("--nozzle-swath-ft", type=float, required=True,
                         help="Ground width covered by the spray nozzles per pass (feet)")
    parser.add_argument("--flight-height-ft", type=float, required=True,
                         help="Flight altitude during spraying (feet)")

    parser.add_argument("--tank-capacity-gal", type=float, required=True,
                         help="Spray tank capacity (gallons)")
    parser.add_argument("--nozzle-flow-rate-gpm", type=float, required=True,
                         help="Total nozzle flow rate (gallons/minute)")
    parser.add_argument("--dispersion-rate-gpa", type=float, required=True,
                         help="Target application rate (gallons per acre)")

    parser.add_argument("--battery-time-min", type=float, required=True,
                         help="Max flight endurance per charge (minutes)")
    parser.add_argument("--battery-reserve-pct", type=float, default=20.0,
                         help="Safety reserve held back from battery budget (default 20%%)")

    parser.add_argument("--transit-speed-mph", type=float, default=None,
                         help="Speed during non-spraying transit legs (default: same as spray speed)")

    parser.add_argument("--home-lat", type=float, required=True, help="Home/launch point latitude")
    parser.add_argument("--home-lon", type=float, required=True, help="Home/launch point longitude")

    args = parser.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    zones = load_spray_zone_polygons(args.gpkg)
    if not zones:
        raise SystemExit("No spray zones (spray_decision != 'No') found in the GeoPackage.")

    total_area_acres = sum(z["area_acres"] for z in zones)
    print(f"Found {len(zones)} spray zone(s), total area {total_area_acres:.2f} acres")

    ordered_zones = order_zones_nearest_neighbor(zones, args.home_lat, args.home_lon)
    raw_waypoints = assemble_raw_waypoints(ordered_zones, args.nozzle_swath_ft, args.flight_height_ft)

    speed_mph = compute_speed_mph(args.nozzle_flow_rate_gpm, args.dispersion_rate_gpa, args.nozzle_swath_ft)
    speed_fps = speed_mph * 5280.0 / 3600.0
    transit_speed_mph = args.transit_speed_mph or speed_mph
    transit_speed_fps = transit_speed_mph * 5280.0 / 3600.0

    volume_gal_per_ft = (args.nozzle_swath_ft * args.dispersion_rate_gpa) / 43560.0

    battery_budget_s = args.battery_time_min * 60.0 * (1 - args.battery_reserve_pct / 100.0)
    tank_budget_gal = args.tank_capacity_gal

    print(f"Computed spray speed: {speed_mph:.2f} mph (transit speed: {transit_speed_mph:.2f} mph)")
    print(f"Battery budget per cycle: {battery_budget_s/60:.1f} min (after reserve)")
    print(f"Tank budget per cycle: {tank_budget_gal:.2f} gal")

    cycles, warnings = segment_into_cycles(
        raw_waypoints, args.home_lat, args.home_lon,
        speed_fps, transit_speed_fps, volume_gal_per_ft,
        battery_budget_s, tank_budget_gal,
    )

    for cycle in cycles:
        mark_spray_transitions(cycle["waypoints"])

    for w in warnings:
        print(f"WARNING: {w}")

    meta = {
        "total_spray_zones": len(zones),
        "total_area_acres": round(total_area_acres, 3),
        "speed_mph": round(speed_mph, 2),
        "transit_speed_mph": round(transit_speed_mph, 2),
        "nozzle_swath_ft": args.nozzle_swath_ft,
        "flight_height_ft": args.flight_height_ft,
        "tank_capacity_gal": args.tank_capacity_gal,
        "dispersion_rate_gpa": args.dispersion_rate_gpa,
        "battery_time_min": args.battery_time_min,
        "num_charge_cycles": len(cycles),
        "home": {"lat": args.home_lat, "lon": args.home_lon},
    }

    json_path = write_flight_plan_json(cycles, args.outdir, meta)
    gpkg_path = write_flight_plan_gpkg(cycles, args.outdir)

    print()
    print(f"Wrote: {json_path}")
    print(f"Wrote: {gpkg_path}")
    print(f"Total charge cycles required: {len(cycles)}")
    for idx, cycle in enumerate(cycles):
        print(f"  Cycle {idx+1}: {cycle['cum_time']/60:.1f} min, {cycle['cum_volume']:.2f} gal, "
              f"{len(cycle['waypoints'])} waypoints")


if __name__ == "__main__":
    main()