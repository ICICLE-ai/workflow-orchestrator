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
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object at the top level, got {type(data).__name__}")
    raw = data.get("annotations") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"'annotations' must be an object of {{file_path: [...]}}, got {type(raw).__name__}")
    out = {}
    for file_path, items in raw.items():
        if not isinstance(items, list):
            raise ValueError(f"annotations['{file_path}'] must be a list, got {type(items).__name__}")
        dets = []
        for item in items or []:
            if not isinstance(item, dict):
                raise ValueError(f"annotations['{file_path}'] entries must be objects, got {type(item).__name__}")
            points = item.get("points")
            if points:
                polygon = [[p["x"], p["y"]] for p in points]
                bbox = _bbox_from_polygon(polygon)
            else:
                polygon = None
                bbox = [item.get("x", 0), item.get("y", 0), item.get("width", 0), item.get("height", 0)]
            dets.append({
                "label": item.get("label", "object"),
                "score": item.get("score"),
                "bbox": bbox,
                "polygon": polygon,
                # Carried through the canonical shape (rather than dropped like
                # the rest of smart_labeler's per-annotation fields) because
                # build_sam3_exemplars reads it to decide box_labels: a box
                # flagged NEGATIVE_FLAG is a "not this" exemplar. No other
                # format reads or writes it, so it's simply absent from every
                # other parser's Detections.
                "flag": item.get("flag"),
            })
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
            if d.get("flag"):
                entry["flag"] = d["flag"]
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
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object at the top level, got {type(data).__name__}")
    images_raw = data.get("images", [])
    if not isinstance(images_raw, list):
        raise ValueError(
            f"'images' must be a list of COCO image objects, got {type(images_raw).__name__} — "
            "this doesn't look like a COCO annotations file."
        )
    annotations_raw = data.get("annotations", [])
    if not isinstance(annotations_raw, list):
        raise ValueError(f"'annotations' must be a list of COCO annotation objects, got {type(annotations_raw).__name__}")
    categories_raw = data.get("categories", [])
    if not isinstance(categories_raw, list):
        raise ValueError(f"'categories' must be a list of COCO category objects, got {type(categories_raw).__name__}")

    images = {img["id"]: img for img in images_raw}
    categories = {c["id"]: c.get("name", str(c["id"])) for c in categories_raw}
    out: dict = {}
    for ann in annotations_raw:
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


# --- sam3_exemplars (write-only) -----------------------------------------

# An annotation whose `flag` equals this (case-insensitive) becomes a NEGATIVE
# exemplar — box_labels 0, "the thing in this box is NOT what I want" — rather
# than a positive one. `flag` is already a first-class, filterable, bulk-
# editable field on smart_labeler's annotations (see @icicle-ai/annotation-
# details' BaseAnnotation), so marking negatives needs no new labeling UI.
NEGATIVE_FLAG = "negative"


def relpath_within(file_path: str, image_dir: str | None) -> str:
    """Turn a per_file key into the key SAM3 expects: a path relative to the
    image directory, or a bare filename.

    smart_labeler writes annotations_by_file keyed by whatever path its file
    browser reported — in practice an ABSOLUTE Tapis path
    ('/fs/ess/PAS2699/Demo_data/Weed_data/img.JPG'), while the job stages
    images at /job/input/images and looks them up relative to that. Stripping
    the image dir keeps any subdirectory structure ('batch_a/img_001.jpg');
    with no image dir known, or a key from somewhere else entirely, the
    basename is the documented fallback the app also accepts.
    """
    key = (file_path or "").lstrip("/")
    if image_dir:
        prefix = image_dir.strip("/")
        if prefix and (key == prefix or key.startswith(prefix + "/")):
            return key[len(prefix):].lstrip("/") or os.path.basename(key)
    return key if "/" not in key else os.path.basename(key)


def build_sam3_exemplars(
    per_file: dict,
    image_dir: str | None = None,
    text_prompts: list | None = None,
) -> dict:
    """Build the zero_shot_annotation prompt file (SAM3 geometry prompts).

    Write-only: this is a PROMPT format, not an annotation format, so it has
    no parser and can't round-trip (hence its absence from PARSERS below, and
    from the adapter's FROM_FORMATS). Shape:

        {"text_prompts": ["tree", "car"],
         "exemplars": {"batch_a/img_001.jpg": [
             {"label": "shrub", "text": "shrub",
              "boxes": [[x1, y1, x2, y2], ...], "box_labels": [1, 0, ...]}]}}

    Detections are grouped by LABEL within each image — SAM3 takes one box
    list per concept, not one entry per box, so N boxes sharing a label become
    one entry with N boxes rather than N single-box entries. `text` is set to
    the label: with exemplar_tile_mode='all' the job falls back to an
    exemplar's paired text on tiles where no box is visible (see the step's
    config_schema), and the label is the only text this pipeline actually
    knows for a hand-drawn box.

    Boxes are emitted as absolute-pixel [x1, y1, x2, y2] corners, rounded to
    int — the canonical Detection shape carries [x, y, w, h], and a polygon
    (segmentation-mode annotation) contributes its bounding box, since a
    geometry prompt is a box either way.

    Note there is deliberately no 'default' key: exemplar boxes are pixel
    coordinates inside ONE specific image, so promoting them to apply to every
    image is only meaningful if the job crops the exemplar from a reference
    image rather than reading the coordinates against each target. That's
    decided inside the zero-shot .sif, not here — so this keys strictly by the
    image the boxes were actually drawn on. Add an opt-in parameter once the
    app's 'default' semantics are confirmed.
    """
    exemplars = {}
    for file_path, dets in per_file.items():
        by_label = {}
        for d in dets:
            bbox = d.get("bbox")
            if not bbox and d.get("polygon"):
                bbox = _bbox_from_polygon(d["polygon"])
            if not bbox:
                continue
            label = d.get("label") or "object"
            group = by_label.setdefault(label, {"boxes": [], "box_labels": []})
            x1, y1, x2, y2 = _xyxy(bbox)
            group["boxes"].append([round(x1), round(y1), round(x2), round(y2)])
            is_negative = str(d.get("flag") or "").strip().lower() == NEGATIVE_FLAG
            group["box_labels"].append(0 if is_negative else 1)
        if not by_label:
            continue
        key = relpath_within(file_path, image_dir)
        # Two source keys can collapse onto one relative key (e.g. the same
        # basename in different directories, with no image_dir to disambiguate)
        # — merge rather than let the later one silently replace the earlier.
        entries = exemplars.setdefault(key, [])
        for label, group in by_label.items():
            existing = next((e for e in entries if e["label"] == label), None)
            if existing:
                existing["boxes"].extend(group["boxes"])
                existing["box_labels"].extend(group["box_labels"])
            else:
                entries.append({
                    "label": label,
                    "text": label,
                    "boxes": group["boxes"],
                    "box_labels": group["box_labels"],
                })

    out = {}
    if text_prompts:
        out["text_prompts"] = [t for t in text_prompts if t]
    out["exemplars"] = exemplars
    return out


PARSERS = {"native": parse_native, "coco": parse_coco}
BUILDERS = {"native": build_native, "coco": build_coco}
