"""
annotations_io.py
=================

Reading and writing the two annotation formats the platform produces, shared
by the wf-utils tools:

  * "zero_shot": zero_shot_annotation's flat output, one record per detection:
      {..., "annotations": [{"image_path", "bounding_box": [x1, y1, x2, y2],
                             "score", "class", "prompt_type"?, "segmentation"?: [[x, y], ...]}]}
  * "smart_labeler": the Smart Labeler's annotations.json (also hf_inference's),
    keyed by image:
      {"annotation_type", "annotations": {"<image path>": [{"id", "label", "score"?,
                                          "x", "y", "width", "height" | "points": [{x, y}]}]}}

In between, every annotation is a dict:
    {"label": str, "score": float | None, "box": [x1, y1, x2, y2] | None,
     "polygon": [[x, y], ...] | None, "extra": {...}}
where "extra" carries format-specific fields (prompt_type, flag) through a
round trip in the same format.
"""

import json


def detect_format(data) -> str:
    raw = data.get("annotations") if isinstance(data, dict) else None
    if isinstance(raw, list):
        return "zero_shot"
    if isinstance(raw, dict):
        return "smart_labeler"
    raise ValueError("no 'annotations' list (zero-shot) or object (Smart Labeler) in the annotations file")


def load_annotations(data) -> dict:
    """{image key: [annotation, ...]} from either format (see the module docstring)."""
    fmt = detect_format(data)
    out = {}
    if fmt == "zero_shot":
        for item in data["annotations"]:
            key = item.get("image_path")
            if not key:
                continue
            seg = item.get("segmentation") or None
            box = item.get("bounding_box") or item.get("bbox")
            out.setdefault(key, []).append({
                "label": str(item.get("class") or item.get("label") or "object"),
                "score": item.get("score"),
                "box": [float(v) for v in box] if box and len(box) == 4 else None,
                "polygon": [[float(p[0]), float(p[1])] for p in seg] if seg else None,
                "extra": {k: item[k] for k in ("prompt_type",) if k in item},
            })
    else:
        for key, items in data["annotations"].items():
            dets = out.setdefault(key, [])
            for item in items or []:
                points = item.get("points")
                polygon = [[float(p["x"]), float(p["y"])] for p in points] if points else None
                box = None
                if all(isinstance(item.get(k), (int, float)) for k in ("x", "y", "width", "height")):
                    box = [item["x"], item["y"], item["x"] + item["width"], item["y"] + item["height"]]
                elif polygon:
                    box = bbox_of(polygon)
                dets.append({
                    "label": str(item.get("label") or "object"),
                    "score": item.get("score"),
                    "box": box,
                    "polygon": polygon,
                    "extra": {k: item[k] for k in ("flag",) if item.get(k)},
                })
    return out


def read_file(path) -> tuple:
    """(format, {image key: [annotation, ...]}, top-level metadata) for one file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    fmt = detect_format(data)
    meta = {k: v for k, v in data.items() if k != "annotations"} if isinstance(data, dict) else {}
    return fmt, load_annotations(data), meta


def bbox_of(polygon) -> list:
    xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _box(a):
    return a["box"] or (bbox_of(a["polygon"]) if a["polygon"] else None)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-") or "file"


def to_zero_shot(per_image: dict, meta: dict) -> dict:
    """The flat zero-shot format, with `meta` as its other top-level fields."""
    records = []
    for key, dets in per_image.items():
        for a in dets:
            box = _box(a)
            rec = {
                "image_path": key,
                "bounding_box": [round(v, 2) for v in box] if box else [0, 0, 0, 0],
                "score": a["score"],
                "class": a["label"],
                "prompt_type": a["extra"].get("prompt_type") or a["extra"].get("flag") or "merged",
            }
            if a["polygon"]:
                rec["segmentation"] = [[round(x, 2), round(y, 2)] for x, y in a["polygon"]]
            records.append(rec)
    return {**meta, "annotations": records}


def to_smart_labeler(per_image: dict) -> dict:
    """The Smart Labeler's annotations.json, as its own Save writes it."""
    has_polygon = any(a["polygon"] for dets in per_image.values() for a in dets)
    out = {}
    for key, dets in per_image.items():
        items = []
        for i, a in enumerate(dets):
            entry = {"id": f"{_slug(key)}-{i}", "label": a["label"]}
            if isinstance(a["score"], (int, float)):
                entry["score"] = a["score"]
            flag = a["extra"].get("flag") or a["extra"].get("prompt_type")
            if flag:
                entry["flag"] = str(flag)
            if has_polygon and a["polygon"]:
                entry["points"] = [{"x": round(x, 2), "y": round(y, 2)} for x, y in a["polygon"]]
            else:
                x1, y1, x2, y2 = _box(a) or [0, 0, 0, 0]
                entry.update({"x": round(x1, 2), "y": round(y1, 2),
                              "width": round(x2 - x1, 2), "height": round(y2 - y1, 2)})
            items.append(entry)
        out[key] = items
    return {"annotation_type": "segmentation" if has_polygon else "detection", "annotations": out}


def iou(a, b) -> float:
    ba, bb = _box(a), _box(b)
    if not ba or not bb:
        return 0.0
    ix = max(0.0, min(ba[2], bb[2]) - max(ba[0], bb[0]))
    iy = max(0.0, min(ba[3], bb[3]) - max(ba[1], bb[1]))
    inter = ix * iy
    area = (ba[2] - ba[0]) * (ba[3] - ba[1]) + (bb[2] - bb[0]) * (bb[3] - bb[1]) - inter
    return inter / area if area > 0 else 0.0
