# Generate GeoPackage

Turns drone detection results (JSON) plus the source imagery (EXIF GPS) into a georeferenced GeoPackage: where each detected object actually sits on the ground, which grid cells should be sprayed, and the outline of the flown area.

The `.gpkg` is the source of truth. Shapefile/GeoJSON exports are a separate conversion step elsewhere in the pipeline — this job does not produce them.

## How It Works

1. Loads `{"annotations": [...]}` and groups entries by image basename, dropping anything below `--score-threshold` or outside `--class-filter`
2. Recursively indexes `--imagedir` by basename, so images may live in nested per-flight or per-date folders
3. Reads each image's EXIF GPS position, camera heading (`GPSImgDirection`) and pixel dimensions
4. Converts each pixel bounding box to a normalized in-image center point
5. Projects that point to a ground `(lat, lon)` using the image's ground footprint, the GPS/optical offset, and the camera heading
6. Lays a rectangular grid over the detections' bounding box and counts the points falling in each cell
7. Turns each cell's count into a spray decision — binary, or custom banded
8. Takes the convex hull of the detections as the farm boundary
9. Writes all three layers into one `output.gpkg`

## Files

| File | Description |
|---|---|
| `generate_geopackage.py` | Everything — CLI, EXIF parsing, projection, gridding, spray logic, GPKG writer |
| `generate_geopackage.def` | Apptainer/Singularity definition |
| `requirements.txt` | Pinned Python dependencies (resolved against Python 3.13) |
| `run.sh` | The reference invocation used against the OSC demo dataset |

## Usage

```bash
cd generate_geopackage

# binary spray decision — spray any cell holding at least one weed
python generate_geopackage.py \
    --json /fs/ess/PAS2699/Demo_data/outputs/out1/annotations.json \
    --outdir /fs/ess/PAS2699/Demo_data/outputs/out1 \
    --imagedir /fs/ess/PAS2699/Demo_data/Weed_data \
    --grid-width 30 --grid-height 30 \
    --fov-width-ft 29 --fov-height-ft 22 \
    --spray-mode binary \
    --score-threshold 0.3 \
    --class-filter weed

# banded spray rates by infestation density
python generate_geopackage.py \
    --json detections.json --outdir ./output --imagedir ./images \
    --spray-mode custom \
    --spray-levels Low Medium High \
    --spray-thresholds 1 5 15 \
    --class-filter all
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--json` | *required* | Detections JSON to read |
| `--outdir` | *required* | Directory the `output.gpkg` is written into (created if absent) |
| `--imagedir` | *required* | Root of the source imagery, searched **recursively** |
| `--grid-width` | `30` | Grid cell width in feet |
| `--grid-height` | `30` | Grid cell height in feet |
| `--fov-width-ft` | `29` | Ground footprint width of one image, in feet |
| `--fov-height-ft` | `22` | Ground footprint height of one image, in feet |
| `--gps-offset-x-ft` | `0.0` | East–west offset between the GPS antenna and the optical center |
| `--gps-offset-y-ft` | `0.0` | North–south offset between the GPS antenna and the optical center |
| `--spray-mode` | `binary` | `binary` (Yes/No) or `custom` (banded levels) |
| `--spray-levels` | `[]` | Band names for custom mode, ascending — e.g. `Low Medium High` |
| `--spray-thresholds` | `[]` | Minimum detection count per band, ascending — e.g. `1 5 15` |
| `--score-threshold` | `0.3` | Drop detections scoring below this |
| `--class-filter` | `weed` | Keep only this class; `all` keeps every class |

`--spray-levels` and `--spray-thresholds` must be the same length, and custom mode requires both.

## Input Format

```json
{
  "annotations": [
    {
      "image_path": "100009240943.JPG",
      "class": "weed",
      "bounding_box": [1204, 880, 1310, 985],
      "score": 0.795
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `image_path` | yes | Bare filename or relative path — only the basename is used for lookup |
| `bounding_box` | yes | `[x1, y1, x2, y2]` in **raw pixels**, not normalized. Clamped to the image, so boxes spilling past an edge are fine |
| `score` | no | Missing scores are treated as `1.0` and kept |
| `class` | no | `label` is accepted as an alias; matched case- and whitespace-insensitively |

This is the same flat format `zero_shot_annotation` writes, so its output can be fed in directly.

## Outputs

One file, `<outdir>/output.gpkg`, with three layers, all in **EPSG:4326**:

| Layer | Geometry | Columns |
|---|---|---|
| `detections` | Point | `source_img`, `score` |
| `spray_zones` | Polygon | `cell_id`, `detection_count`, `spray_decision` |
| `farm_boundary` | Polygon | `name` — convex hull of all detections |

`spray_decision` is `Yes`/`No` in binary mode. In custom mode a count of `0` is `No`, and anything above falls into the highest band whose threshold it meets: with `--spray-levels Low Medium High --spray-thresholds 1 5 15`, counts `1–4` are `Low`, `5–14` `Medium`, `15+` `High`.

Inspect the result with:

```bash
ogrinfo -so output.gpkg spray_zones
```

## Geometry Model

Detections are projected assuming a **nadir (straight-down) camera**. Camera *yaw* is handled — `GPSImgDirection` rotates the local offset so detections land correctly even when the drone wasn't flying due north — but pitch and roll are not. For a rig capturing at a fixed oblique angle, fold that correction into `--fov-width-ft` / `--fov-height-ft` upstream so they describe the *effective* ground footprint at that tilt.

Feet are converted to degrees with a fixed 364,000 ft per degree of latitude; longitude scales by `cos(latitude)`. This is a local flat-earth approximation, accurate at field scale and not intended for regional extents.

Accuracy is bounded by the inputs, not the math: the footprint numbers and the GPS/optical offset need to be calibrated for your rig and flight altitude, or every detection is offset by the same constant error.

## Behavior at the Edges

The job is built to fail loudly at the point of the mistake rather than produce quietly wrong output downstream:

| Situation | Behavior |
|---|---|
| Duplicate basenames across subfolders | First match wins; every duplicate is listed on stderr |
| Image missing, or has no EXIF/GPS | That image's detections are skipped, and the names are listed on stderr |
| Every annotation filtered out | Warns with the class names actually present in the file and their counts, so the wrong `--class-filter` is obvious |
| No detections survive at all | Writes empty `detections`, `spray_zones` and `farm_boundary` layers with correct schema and CRS, plus a warning |

## Container

Build:

```bash
apptainer build generate_geopackage.sif generate_geopackage.def
```

Run — **CPU only, do not pass `--nv`** — binding any host path you reference, since `/fs/ess` is not visible inside the container otherwise:

```bash
apptainer run \
    --bind /fs/ess/PAS2699:/fs/ess/PAS2699 \
    generate_geopackage.sif \
    --json /fs/ess/PAS2699/Demo_data/outputs/out1/annotations.json \
    --outdir /fs/ess/PAS2699/Demo_data/outputs/out1 \
    --imagedir /fs/ess/PAS2699/Demo_data/Weed_data \
    --spray-mode binary --class-filter weed
```

### Why the base image is pinned

`requirements.txt` was resolved against Python 3.13, which is why the definition uses `python:3.13-slim-bookworm`. Change one and you must re-resolve the other.

No system GDAL/PROJ/GEOS is installed, deliberately: the `pyogrio`, `pyproj` and `shapely` manylinux wheels each vendor their own copy. Adding a distro GDAL alongside them is the classic route to two PROJ data directories and silently wrong reprojection.

The build itself verifies this — it writes and re-reads a real GeoPackage and checks the CRS survived, and asserts Pillow exposes the GPS tag tables. A broken vendored library fails the *build*, not a job three hours into a flight campaign.

## Pipeline Position

The last stage of the weed-detection pipeline — it consumes annotations and imagery, and emits the geospatial product a sprayer or GIS consumes:

```
images  →  zero_shot_annotation.py  →  annotations .json  ┐
                                                          ├→  generate_geopackage.py  →  output.gpkg
images (EXIF GPS)  ────────────────────────────────────── ┘
```
