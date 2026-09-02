#!/usr/bin/env python3
"""
generate_geopackage.py

Converts drone detection results (JSON) + source imagery (EXIF GPS) into a
GeoPackage containing:
  - "detections"    : point layer, one point per detected object
  - "spray_zones"   : polygon grid layer, one cell per grid square, with a
                      spray decision (binary or custom banded) based on
                      detection counts
  - "farm_boundary" : polygon layer, convex hull of all image locations

Internal storage is GeoPackage. Shapefile/GeoJSON exports are handled by a
separate conversion step (ogr2ogr / geopandas.to_file) elsewhere in the
pipeline -- this script's only job is producing the source-of-truth .gpkg.

USAGE
-----
python generate_geopackage.py \
    --json detections.json \
    --outdir ./output \
    --imagedir ./images \
    --grid-width 30 --grid-height 30 \
    --fov-width-ft 29 --fov-height-ft 22 \
    --gps-offset-x-ft 0 --gps-offset-y-ft 0 \
    --spray-mode binary \
    --score-threshold 0.3 \
    --class-filter weed

INPUT JSON FORMAT
-----------------
{
  "annotations": [
    {
      "image_path": "100009240943.JPG",
      "class": "weed",
      "bounding_box": [x1, y1, x2, y2],
      "score": 0.795
    },
    ...
  ]
}

- "image_path" may be a bare filename or a relative path -- only the
  basename is used to locate the actual file, since --imagedir is searched
  recursively (multi-level directory support, see find_image_index()).
- "bounding_box" is [x1, y1, x2, y2] in raw pixel coordinates (not
  normalized). This script converts it to a normalized center point using
  the image's actual pixel dimensions.
- "score" is optional. Entries missing it are treated as score=1.0 (kept).
- "class" (or "label" -- either key is accepted) is matched against
  --class-filter, case- and whitespace-insensitively. Pass
  --class-filter all to keep every class.
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import geopandas as gpd
from shapely.geometry import Point, box


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FEET_PER_DEGREE_LAT = 364_000.0  # approx, constant everywhere
# Longitude feet-per-degree depends on latitude (shrinks toward the poles)


# ---------------------------------------------------------------------------
# Image directory indexing (multi-level directory support)
# ---------------------------------------------------------------------------

def build_image_index(imagedir):
    """
    Recursively walks imagedir and builds a {basename: full_path} index, so
    images can live in nested subfolders (e.g. per-flight or per-date
    folders) and still be found by the bare filename referenced in the
    detections JSON.

    If the same basename appears in more than one subfolder, the first one
    found wins and a warning is printed -- rename duplicates upstream if
    this matters for your dataset.
    """
    index = {}
    duplicates = []
    for root, _dirs, files in os.walk(imagedir):
        for fname in files:
            if fname.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff")):
                if fname in index:
                    duplicates.append(fname)
                    continue
                index[fname] = os.path.join(root, fname)

    if duplicates:
        print(
            f"WARNING: {len(duplicates)} duplicate image filename(s) found "
            f"across subfolders of {imagedir} -- first match used for each:",
            file=sys.stderr,
        )
        for name in duplicates:
            print(f"  - {name}", file=sys.stderr)

    return index


# ---------------------------------------------------------------------------
# EXIF / GPS helpers
# ---------------------------------------------------------------------------

def _dms_to_decimal(dms, ref):
    degrees, minutes, seconds = [float(v) for v in dms]
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def read_image_metadata(image_path):
    """
    Returns (lat, lon, heading_deg, width_px, height_px) for an image.
    heading_deg is the camera's pointing direction (GPSImgDirection),
    0 = true north, clockwise. Defaults to 0.0 if the tag is missing.
    """
    img = Image.open(image_path)
    width_px, height_px = img.size

    exif_raw = img._getexif()
    if not exif_raw:
        raise ValueError(f"No EXIF data found in {image_path}")

    exif = {TAGS.get(k, k): v for k, v in exif_raw.items()}
    gps_info = exif.get("GPSInfo")
    if not gps_info:
        raise ValueError(f"No GPS EXIF data found in {image_path}")

    gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}

    lat = _dms_to_decimal(gps["GPSLatitude"], gps["GPSLatitudeRef"])
    lon = _dms_to_decimal(gps["GPSLongitude"], gps["GPSLongitudeRef"])

    heading = 0.0
    if "GPSImgDirection" in gps:
        heading = float(gps["GPSImgDirection"])

    return lat, lon, heading, width_px, height_px


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def feet_to_degrees(dx_ft, dy_ft, at_lat_deg):
    """Convert a local east/north offset in feet to a (dlon, dlat) offset."""
    ft_per_deg_lon = FEET_PER_DEGREE_LAT * math.cos(math.radians(at_lat_deg))
    dlon = dx_ft / ft_per_deg_lon
    dlat = dy_ft / FEET_PER_DEGREE_LAT
    return dlon, dlat


def rotate(dx, dy, heading_deg):
    """
    Rotate a local (east, north) offset by the camera heading.
    Camera heading is degrees clockwise from true north -- this handles
    camera yaw/rotation so detections land in the correct place even when
    the drone wasn't flying due north.
    """
    theta = math.radians(heading_deg)
    rotated_x = dx * math.cos(theta) + dy * math.sin(theta)
    rotated_y = -dx * math.sin(theta) + dy * math.cos(theta)
    return rotated_x, rotated_y


def project_detection(
    image_lat, image_lon, heading_deg,
    norm_x, norm_y,
    fov_width_ft, fov_height_ft,
    gps_offset_x_ft, gps_offset_y_ft,
):
    """
    Projects a normalized in-image detection position (0-1, 0-1) to a
    (lat, lon) on the ground, accounting for:
      - the image's footprint size on the ground (fov width/height)
      - GPS antenna vs optical-center offset
      - camera heading (yaw) at capture time

    Assumes a nadir (straight-down) camera -- i.e. this handles *yaw* but not
    pitch/roll tilt. If your rig captures at a fixed oblique angle, that's a
    constant correction better applied upstream during footprint calibration
    (fov-width-ft / fov-height-ft should already reflect the *effective*
    ground footprint at that tilt).
    """
    local_dx = (norm_x - 0.5) * fov_width_ft
    local_dy = (0.5 - norm_y) * fov_height_ft  # image y grows downward

    local_dx += gps_offset_x_ft
    local_dy += gps_offset_y_ft

    east_ft, north_ft = rotate(local_dx, local_dy, heading_deg)

    dlon, dlat = feet_to_degrees(east_ft, north_ft, image_lat)
    return image_lat + dlat, image_lon + dlon


# ---------------------------------------------------------------------------
# Spray decision logic
# ---------------------------------------------------------------------------

def spray_decision_binary(count):
    return "Yes" if count > 0 else "No"


def spray_decision_custom(count, levels, thresholds):
    """
    thresholds are ascending minimum counts for each level, e.g.
    levels=["Low","Medium","High"], thresholds=[1,5,15]
    count=0         -> "No"
    1 <= count < 5   -> "Low"
    5 <= count < 15  -> "Medium"
    count >= 15      -> "High"
    """
    if count <= 0:
        return "No"
    decision = "No"
    for level, threshold in zip(levels, thresholds):
        if count >= threshold:
            decision = level
    return decision


# ---------------------------------------------------------------------------
# Detections JSON parsing
# ---------------------------------------------------------------------------

def load_and_group_annotations(json_path, class_filter, score_threshold):
    """
    Loads the {"annotations": [...]} JSON format and groups entries by
    image basename. Filters by class and score threshold along the way.

    Returns: dict {image_basename: [annotation, ...]}
    """
    with open(json_path, "r") as f:
        data = json.load(f)

    annotations = data.get("annotations", [])
    grouped = defaultdict(list)
    dropped_class = 0
    dropped_score = 0
    dropped_no_score = 0
    seen_classes = defaultdict(int)

    # "all" (or an empty --class-filter) disables class filtering entirely,
    # which is what you want for single-class runs and for upstream stages
    # that emit placeholder class names (see generate_proposals.py, which
    # writes the score string into "class").
    keep_all_classes = class_filter is None or class_filter.strip().lower() in ("", "all")
    wanted = None if keep_all_classes else class_filter.strip().lower()

    for ann in annotations:
        # Producers in this repo disagree on the key name: the smart-labeller
        # stages write "class", the shapefile/detection formats write "label".
        # Accept either, and compare case/whitespace-insensitively so that
        # "Weed" or "weed " from a text prompt doesn't silently drop the run.
        raw_class = ann.get("class", ann.get("label"))
        seen_classes[str(raw_class)] += 1

        if not keep_all_classes and str(raw_class).strip().lower() != wanted:
            dropped_class += 1
            continue

        score = ann.get("score")
        if score is None:
            dropped_no_score += 1
            score = 1.0  # keep, treat missing score as full confidence

        if score < score_threshold:
            dropped_score += 1
            continue

        basename = os.path.basename(ann["image_path"])
        grouped[basename].append(ann)

    kept = sum(len(v) for v in grouped.values())
    print(
        f"Loaded {len(annotations)} annotations -> kept "
        f"{kept} after filtering "
        f"(class-filtered: {dropped_class}, score-filtered: {dropped_score}, "
        f"missing-score-but-kept: {dropped_no_score})"
    )

    if annotations and not kept:
        # Nothing survived: say what the file actually contained, so the fix
        # is obvious instead of surfacing as an empty-grid crash downstream.
        summary = ", ".join(
            f"{name!r} x{count}"
            for name, count in sorted(seen_classes.items(), key=lambda kv: -kv[1])[:10]
        )
        print(
            f"WARNING: every annotation was filtered out. "
            f"--class-filter={class_filter!r}, --score-threshold={score_threshold}; "
            f"classes present in {json_path}: {summary}. "
            f"Use --class-filter all to keep every class.",
            file=sys.stderr,
        )

    return grouped


def bbox_to_normalized_center(bbox, width_px, height_px):
    """
    Converts [x1, y1, x2, y2] pixel bbox to a normalized (0-1, 0-1) center
    point, clamping to image bounds first (handles boxes that spill past
    the edge, e.g. [-5, 1152, 65, 1259]).
    """
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(x1, width_px))
    x2 = max(0, min(x2, width_px))
    y1 = max(0, min(y1, height_px))
    y2 = max(0, min(y2, height_px))

    center_x_px = (x1 + x2) / 2.0
    center_y_px = (y1 + y2) / 2.0

    return center_x_px / width_px, center_y_px / height_px


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build_detection_points(grouped_annotations, image_index,
                            fov_width_ft, fov_height_ft,
                            gps_offset_x_ft, gps_offset_y_ft):
    points = []
    missing_images = []

    for basename, anns in grouped_annotations.items():
        img_path = image_index.get(basename)

        if img_path is None:
            missing_images.append(basename)
            continue

        try:
            lat, lon, heading, width_px, height_px = read_image_metadata(img_path)
        except ValueError as e:
            print(f"WARNING: {e} -- skipping {basename}", file=sys.stderr)
            missing_images.append(basename)
            continue

        for ann in anns:
            norm_x, norm_y = bbox_to_normalized_center(
                ann["bounding_box"], width_px, height_px
            )
            det_lat, det_lon = project_detection(
                lat, lon, heading,
                norm_x, norm_y,
                fov_width_ft, fov_height_ft,
                gps_offset_x_ft, gps_offset_y_ft,
            )
            points.append({
                "geometry": Point(det_lon, det_lat),
                "source_img": basename,
                "score": ann.get("score", 1.0),
            })

    return points, missing_images


def build_grid(points, grid_width_ft, grid_height_ft):
    """Builds a rectangular grid covering the bounding box of all detections."""
    if not points:
        return []

    lats = [p["geometry"].y for p in points]
    lons = [p["geometry"].x for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    mid_lat = (min_lat + max_lat) / 2.0

    dlon_cell, dlat_cell = feet_to_degrees(grid_width_ft, grid_height_ft, mid_lat)

    cells = []
    cell_id = 0
    lat = min_lat
    while lat < max_lat:
        lon = min_lon
        while lon < max_lon:
            cells.append({
                "cell_id": cell_id,
                "geometry": box(lon, lat, lon + dlon_cell, lat + dlat_cell),
            })
            cell_id += 1
            lon += dlon_cell
        lat += dlat_cell

    return cells


def empty_gdf(columns):
    """
    An empty GeoDataFrame with a real geometry column and CRS.

    gpd.GeoDataFrame([]) has no columns at all, so asking it for
    geometry="geometry" raises ValueError: Unknown column geometry. Every
    empty layer therefore has to be constructed from an explicit column list.
    """
    gdf = gpd.GeoDataFrame(columns=columns, geometry="geometry", crs="EPSG:4326")
    return gdf


def assign_counts_to_cells(cells, points):
    if not cells:
        return empty_gdf(["cell_id", "geometry", "detection_count"])

    gdf_cells = gpd.GeoDataFrame(cells, geometry="geometry", crs="EPSG:4326")
    gdf_points = gpd.GeoDataFrame(points, geometry="geometry", crs="EPSG:4326") \
        if points else empty_gdf(["geometry", "source_img", "score"])

    if gdf_points.empty:
        gdf_cells["detection_count"] = 0
        return gdf_cells

    joined = gpd.sjoin(gdf_points, gdf_cells, how="left", predicate="within")
    counts = joined.groupby("cell_id").size()
    gdf_cells["detection_count"] = gdf_cells["cell_id"].map(counts).fillna(0).astype(int)
    return gdf_cells


def apply_spray_decisions(gdf_cells, spray_mode, spray_levels, spray_thresholds):
    if gdf_cells.empty:
        gdf_cells["spray_decision"] = []
        return gdf_cells

    if spray_mode == "binary":
        gdf_cells["spray_decision"] = gdf_cells["detection_count"].apply(spray_decision_binary)
    else:
        gdf_cells["spray_decision"] = gdf_cells["detection_count"].apply(
            lambda c: spray_decision_custom(c, spray_levels, spray_thresholds)
        )
    return gdf_cells


def build_farm_boundary(points):
    gdf_points = gpd.GeoDataFrame(points, geometry="geometry", crs="EPSG:4326")
    hull = gdf_points.union_all().convex_hull
    return gpd.GeoDataFrame([{"geometry": hull, "name": "farm_boundary"}], crs="EPSG:4326")


def main():
    parser = argparse.ArgumentParser(description="Generate GeoPackage from drone detections")
    parser.add_argument("--json", required=True, help="Input JSON path (detections)")
    parser.add_argument("--outdir", required=True, help="Output directory")
    parser.add_argument("--imagedir", required=True,
                         help="Directory of source images (searched recursively, "
                              "supports nested/multi-level subfolders)")
    parser.add_argument("--grid-width", type=float, default=30, help="Cell width in feet")
    parser.add_argument("--grid-height", type=float, default=30, help="Cell height in feet")
    parser.add_argument("--fov-width-ft", type=float, default=29, help="Ground footprint width (ft)")
    parser.add_argument("--fov-height-ft", type=float, default=22, help="Ground footprint height (ft)")
    parser.add_argument("--gps-offset-x-ft", type=float, default=0.0, help="East-west GPS/optical offset (ft)")
    parser.add_argument("--gps-offset-y-ft", type=float, default=0.0, help="North-south GPS/optical offset (ft)")
    parser.add_argument("--spray-mode", choices=["binary", "custom"], default="binary")
    parser.add_argument("--spray-levels", nargs="*", default=[], help="Custom mode band names")
    parser.add_argument("--spray-thresholds", nargs="*", type=int, default=[], help="Custom mode thresholds")
    parser.add_argument("--score-threshold", type=float, default=0.3,
                         help="Drop detections below this confidence score (default 0.3)")
    parser.add_argument("--class-filter", type=str, default="weed",
                         help="Only keep annotations with this class label, matched "
                              "case-insensitively against the 'class' or 'label' key "
                              "(default 'weed'). Use 'all' to keep every class.")

    args = parser.parse_args()

    if args.spray_mode == "custom":
        if len(args.spray_levels) != len(args.spray_thresholds):
            parser.error("--spray-levels and --spray-thresholds must have the same count")
        if not args.spray_levels:
            parser.error("--spray-mode custom requires --spray-levels and --spray-thresholds")

    os.makedirs(args.outdir, exist_ok=True)

    grouped_annotations = load_and_group_annotations(
        args.json, args.class_filter, args.score_threshold
    )

    image_index = build_image_index(args.imagedir)

    points, missing_images = build_detection_points(
        grouped_annotations, image_index,
        args.fov_width_ft, args.fov_height_ft,
        args.gps_offset_x_ft, args.gps_offset_y_ft,
    )

    if missing_images:
        print(f"WARNING: {len(missing_images)} image(s) missing or unreadable:", file=sys.stderr)
        for name in missing_images:
            print(f"  - {name}", file=sys.stderr)

    cells = build_grid(points, args.grid_width, args.grid_height)
    gdf_cells = assign_counts_to_cells(cells, points)
    gdf_cells = apply_spray_decisions(gdf_cells, args.spray_mode, args.spray_levels, args.spray_thresholds)

    gdf_points = gpd.GeoDataFrame(points, geometry="geometry", crs="EPSG:4326") if points else \
        empty_gdf(["geometry", "source_img", "score"])

    gdf_boundary = build_farm_boundary(points) if points else empty_gdf(["geometry", "name"])

    if not points:
        print(
            "WARNING: no detections survived filtering/geolocation -- writing "
            "empty spray_zones, detections and farm_boundary layers.",
            file=sys.stderr,
        )

    gpkg_path = os.path.join(args.outdir, "output.gpkg")

    gdf_cells.to_file(gpkg_path, layer="spray_zones", driver="GPKG")
    gdf_points.to_file(gpkg_path, layer="detections", driver="GPKG")
    gdf_boundary.to_file(gpkg_path, layer="farm_boundary", driver="GPKG")

    print(f"Wrote GeoPackage: {gpkg_path}")
    print(f"  spray_zones   : {len(gdf_cells)} cells")
    print(f"  detections    : {len(gdf_points)} points")
    print(f"  farm_boundary : {len(gdf_boundary)} polygon(s)")


if __name__ == "__main__":
    main()