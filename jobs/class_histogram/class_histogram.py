#!/usr/bin/env python3
"""Class-histogram visualization for the Harvest workflow orchestrator.

Reads a YOLO predictions.json (the output of the yolo_inference step), counts how
many detections occur per class across the ENTIRE input dataset, and writes a
single bar-chart PNG with one bar per COCO class (80 classes). All paths are
command-line args — nothing hardcoded.

Usage:
    class_histogram.py --predictions <predictions.json OR dir containing it>
                       --output <out_dir> [--title "..."]

Output written to <out_dir>:
    class_histogram.png   — 80-bar chart (count of detections per class)
    class_counts.json     — the underlying per-class counts
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# The 80 COCO class names, in class-id order (0..79).
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def find_predictions(p: Path) -> Path:
    if p.is_file():
        return p
    # A directory was handed in (e.g. an upstream step's output dir).
    cand = p / "predictions.json"
    if cand.exists():
        return cand
    hits = sorted(p.rglob("predictions.json"))
    if hits:
        return hits[0]
    raise SystemExit(f"[class_histogram] ERROR: no predictions.json under {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True, help="predictions.json or a dir containing it")
    ap.add_argument("--output", required=True, help="Directory to write the chart into")
    ap.add_argument("--title", default="Detections per class (COCO 80)", help="Chart title")
    args = ap.parse_args()

    pred_path = find_predictions(Path(args.predictions))
    out_dir = Path(args.output)
    # Keep each declared OUTPUT PORT's artifact in its own type-clean location so
    # downstream nodes get exactly what the port's data type promises:
    #   chart/   -> image_dir  (contains ONLY images)
    #   class_counts.json -> json_results (a single JSON file)
    chart_dir = out_dir / "chart"
    chart_dir.mkdir(parents=True, exist_ok=True)
    print(f"[class_histogram] predictions: {pred_path}")
    print(f"[class_histogram] output:      {out_dir}")

    with open(pred_path) as f:
        data = json.load(f)

    # data is a list of {image, detections:[{class_id, class_name, ...}]}
    counts = Counter()
    for img in data:
        for det in img.get("detections", []):
            cid = det.get("class_id")
            if isinstance(cid, int) and 0 <= cid < len(COCO_CLASSES):
                counts[cid] += 1
            else:
                # fall back to name if id is out of range
                name = det.get("class_name")
                if name in COCO_CLASSES:
                    counts[COCO_CLASSES.index(name)] += 1

    per_class = [counts.get(i, 0) for i in range(len(COCO_CLASSES))]
    total = sum(per_class)
    print(f"[class_histogram] {total} total detections across {len(data)} image(s)")

    # 'counts' output port (json_results): a single JSON file.
    with open(out_dir / "class_counts.json", "w") as f:
        json.dump({COCO_CLASSES[i]: per_class[i] for i in range(len(COCO_CLASSES))}, f, indent=2)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(20, 8))
    ax.bar(range(len(COCO_CLASSES)), per_class, color="#3b82f6")
    ax.set_xticks(range(len(COCO_CLASSES)))
    ax.set_xticklabels(COCO_CLASSES, rotation=90, fontsize=7)
    ax.set_ylabel("Detection count")
    ax.set_title(f"{args.title}  (total={total})")
    fig.tight_layout()
    # 'chart' output port (image_dir): the PNG lives in chart/ (images only).
    out_png = chart_dir / "class_histogram.png"
    fig.savefig(out_png, dpi=120)
    print(f"[class_histogram] DONE — wrote {out_png} and {out_dir/'class_counts.json'}")


if __name__ == "__main__":
    main()
