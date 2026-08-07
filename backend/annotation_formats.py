"""Pure format conversion for the 'annotation_format_adapter' step — no Tapis,
no network, no FastAPI. See annotation_adapter.py for the router that fetches
inputs from Tapis, calls into here, and uploads the result.

Every format is converted through one canonical in-memory shape:

    per_file: dict[image_relpath, list[Detection]]
    Detection: {"label": str, "score": float|None,
                "bbox": [x, y, w, h] | None, "polygon": [[x, y], ...] | None}

bbox/polygon are absolute pixel coordinates (never normalized) — normalizing
for YOLO happens only at the very edge of build_yolo/parse_yolo, where an
image's actual width/height is available. A Detection always has bbox set
(computed from the polygon if needed); polygon is set only when the source
data actually had shape detail beyond a box.

Supported formats:
  - native:     smart_labeler's own {"annotation_type", "annotations": {file: [...]}}
                schema (see frontend/app/pages/smartLabeler.tsx) — box items
                shaped like {x, y, width, height}, polygon items shaped like
                {points: [{x, y}, ...]}.
  - coco:       standard images/annotations/categories COCO detection JSON.
  - yolo:       one class-index-normalized-box (or -polygon, YOLO-seg style)
                .txt per image, plus a classes.txt naming each index. ASSUMES
                a flat label directory (see annotation_adapter.py) whose file
                stems match the image directory's file stems.
  - geopackage: NOT geo-referenced — this adapter has no camera GPS/FOV model
                (see the 'geospatial' step for that), so geometries are
                written in plain image pixel space, one feature per
                detection, in a single 'annotations' layer with columns
                image_path/label/score. Useful as a GIS-tool-readable vector
                dump of a labeled dataset, not as map-projected data.
"""
import os


def _bbox_from_polygon(points: list) -> list:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
    return [minx, miny, maxx - minx, maxy - miny]


def _is_axis_aligned_rect(coords: list) -> bool:
    """True if a 4-point ring is exactly the corners of an axis-aligned
    rectangle — i.e. it round-trips as a plain bbox rather than a polygon.
    Used by parse_geopackage to avoid inflating a box build_geopackage itself
    wrote back into a spurious polygon."""
    xs = sorted({round(x, 6) for x, _ in coords})
    ys = sorted({round(y, 6) for _, y in coords})
    return len(xs) == 2 and len(ys) == 2


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-") or "file"


# --- native ------------------------------------------------------------

def parse_native(data: dict) -> dict:
    raw = (data or {}).get("annotations") or {}
    out = {}
    for file_path, items in raw.items():
        dets = []
        for item in items or []:
            points = item.get("points")
            if points:
                polygon = [[p["x"], p["y"]] for p in points]
                bbox = _bbox_from_polygon(polygon)
            else:
                polygon = None
                bbox = [item.get("x", 0), item.get("y", 0), item.get("width", 0), item.get("height", 0)]
            dets.append({"label": item.get("label", "object"), "score": item.get("score"), "bbox": bbox, "polygon": polygon})
        out[file_path] = dets
    return out


def build_native(per_file: dict, annotation_type: str | None = None) -> dict:
    has_polygon = any(d.get("polygon") for dets in per_file.values() for d in dets)
    inferred_type = annotation_type if annotation_type in ("detection", "segmentation") else (
        "segmentation" if has_polygon else "detection"
    )
    annotations = {}
    for file_path, dets in per_file.items():
        items = []
        for i, d in enumerate(dets):
            entry = {"id": f"{_slug(file_path)}-{i}", "label": d.get("label", "object")}
            if d.get("score") is not None:
                entry["score"] = d["score"]
            if inferred_type == "segmentation" and d.get("polygon"):
                entry["points"] = [{"x": p[0], "y": p[1]} for p in d["polygon"]]
            else:
                x, y, w, h = d.get("bbox") or _bbox_from_polygon(d.get("polygon") or [[0, 0]])
                entry.update({"x": x, "y": y, "width": w, "height": h})
            items.append(entry)
        annotations[file_path] = items
    return {"annotation_type": inferred_type, "annotations": annotations}


# --- coco ----------------------------------------------------------------

def parse_coco(data: dict) -> dict:
    images = {img["id"]: img for img in (data or {}).get("images", [])}
    categories = {c["id"]: c.get("name", str(c["id"])) for c in (data or {}).get("categories", [])}
    out: dict = {}
    for ann in (data or {}).get("annotations", []):
        img = images.get(ann.get("image_id"))
        if not img:
            continue
        file_path = img.get("file_name") or str(ann["image_id"])
        x, y, w, h = ann.get("bbox", [0, 0, 0, 0])
        polygon = None
        seg = ann.get("segmentation")
        if isinstance(seg, list) and seg and isinstance(seg[0], list) and len(seg[0]) >= 6:
            flat = seg[0]
            polygon = [[flat[i], flat[i + 1]] for i in range(0, len(flat) - 1, 2)]
        out.setdefault(file_path, []).append({
            "label": categories.get(ann.get("category_id"), str(ann.get("category_id"))),
            "score": ann.get("score"),
            "bbox": [x, y, w, h],
            "polygon": polygon,
        })
    return out


def build_coco(per_file: dict, image_sizes: dict) -> dict:
    file_to_id = {}
    images = []
    for i, file_path in enumerate(sorted(per_file.keys()), start=1):
        w, h = image_sizes.get(file_path, (0, 0))
        file_to_id[file_path] = i
        images.append({"id": i, "file_name": file_path, "width": w, "height": h})

    labels = sorted({d.get("label", "object") for dets in per_file.values() for d in dets})
    label_to_id = {label: i for i, label in enumerate(labels, start=1)}
    categories = [{"id": cid, "name": label, "supercategory": ""} for label, cid in label_to_id.items()]

    annotations = []
    ann_id = 1
    for file_path, dets in per_file.items():
        img_id = file_to_id[file_path]
        for d in dets:
            x, y, w, h = d.get("bbox") or _bbox_from_polygon(d.get("polygon") or [[0, 0]])
            entry = {
                "id": ann_id, "image_id": img_id, "category_id": label_to_id.get(d.get("label", "object"), 0),
                "bbox": [x, y, w, h], "area": w * h, "iscrowd": 0,
            }
            if d.get("score") is not None:
                entry["score"] = d["score"]
            if d.get("polygon"):
                entry["segmentation"] = [[coord for pt in d["polygon"] for coord in pt]]
            annotations.append(entry)
            ann_id += 1
    return {"images": images, "annotations": annotations, "categories": categories}


# --- yolo ------------------------------------------------------------------

def parse_yolo(label_texts: dict, classes: list, stem_to_file: dict, image_sizes: dict) -> dict:
    """label_texts: {stem (no extension) -> raw .txt content}. stem_to_file maps
    that same stem to the matching image's relpath (see annotation_adapter.py's
    flat-directory stem match). Detections for a stem with no image match are
    dropped (there's no file_path to key them under)."""
    out: dict = {}
    for stem, text in label_texts.items():
        file_path = stem_to_file.get(stem)
        if not file_path:
            continue
        w, h = image_sizes.get(file_path, (0, 0))
        dets = []
        for line in (text or "").strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                cls_idx = int(float(parts[0]))
                nums = [float(p) for p in parts[1:]]
            except ValueError:
                continue
            label = classes[cls_idx] if 0 <= cls_idx < len(classes) else str(cls_idx)
            if len(nums) == 4 and w and h:
                cx, cy, nw, nh = nums
                bw, bh = nw * w, nh * h
                dets.append({"label": label, "score": None, "bbox": [cx * w - bw / 2, cy * h - bh / 2, bw, bh], "polygon": None})
            elif len(nums) >= 6 and w and h:
                polygon = [[nums[i] * w, nums[i + 1] * h] for i in range(0, len(nums) - 1, 2)]
                dets.append({"label": label, "score": None, "bbox": _bbox_from_polygon(polygon), "polygon": polygon})
        out[file_path] = dets
    return out


def build_yolo(per_file: dict, image_sizes: dict) -> tuple:
    """-> ({relpath (with .txt) -> text content}, classes_txt_content)."""
    labels = sorted({d.get("label", "object") for dets in per_file.values() for d in dets})
    label_to_idx = {label: i for i, label in enumerate(labels)}

    label_texts = {}
    for file_path, dets in per_file.items():
        w, h = image_sizes.get(file_path, (0, 0))
        lines = []
        if w and h:
            for d in dets:
                idx = label_to_idx.get(d.get("label", "object"), 0)
                if d.get("polygon"):
                    coords = " ".join(f"{px / w:.6f} {py / h:.6f}" for px, py in d["polygon"])
                    lines.append(f"{idx} {coords}")
                elif d.get("bbox"):
                    x, y, bw, bh = d["bbox"]
                    cx, cy = (x + bw / 2) / w, (y + bh / 2) / h
                    lines.append(f"{idx} {cx:.6f} {cy:.6f} {bw / w:.6f} {bh / h:.6f}")
        stem = os.path.splitext(file_path)[0]
        label_texts[f"{stem}.txt"] = ("\n".join(lines) + "\n") if lines else ""
    classes_txt = ("\n".join(labels) + "\n") if labels else ""
    return label_texts, classes_txt


# --- geopackage --------------------------------------------------------

def parse_geopackage(gpkg_path: str, layer: str = "annotations") -> dict:
    import geopandas as gpd

    gdf = gpd.read_file(gpkg_path, layer=layer)
    out: dict = {}
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.geom_type not in ("Polygon",):
            continue  # points / other geometry types carry no box — documented limitation
        coords = list(geom.exterior.coords)[:-1]
        minx, miny, maxx, maxy = geom.bounds
        bbox = [minx, miny, maxx - minx, maxy - miny]
        polygon = None if (len(coords) == 4 and _is_axis_aligned_rect(coords)) else [[x, y] for x, y in coords]
        file_path = row.get("image_path") or ""
        out.setdefault(file_path, []).append({
            "label": row.get("label", "object"),
            "score": row.get("score"),
            "bbox": bbox,
            "polygon": polygon,
        })
    return out


def build_geopackage(per_file: dict, dest_path: str, layer: str = "annotations") -> None:
    import geopandas as gpd
    from shapely.geometry import box as shapely_box, Polygon

    rows = []
    for file_path, dets in per_file.items():
        for d in dets:
            geom = Polygon(d["polygon"]) if d.get("polygon") else shapely_box(*_xyxy(d.get("bbox") or [0, 0, 0, 0]))
            rows.append({"image_path": file_path, "label": d.get("label", "object"), "score": d.get("score"), "geometry": geom})
    # No CRS is set — see module docstring: pixel space, not geo-referenced.
    gdf = gpd.GeoDataFrame(rows or [{"image_path": None, "label": None, "score": None, "geometry": None}], geometry="geometry")
    gdf.to_file(dest_path, driver="GPKG", layer=layer)


def _xyxy(bbox: list) -> list:
    x, y, w, h = bbox
    return [x, y, x + w, y + h]


PARSERS = {"native": parse_native, "coco": parse_coco}
BUILDERS = {"native": build_native, "coco": build_coco}
