"""
zero_shot_to_yolo.py
====================

Turns annotations into a ready-to-train Ultralytics YOLO dataset:

    <output>/
      data.yaml            # train/val paths, nc, names (no `path:`, so it resolves next to itself)
      classes.txt
      images/train/...     # the images, copied, keeping any subfolders
      images/val/...
      labels/train/...     # one .txt per image: "cls cx cy w h" (detect) or "cls x1 y1 x2 y2 ..." (segment)
      labels/val/...

Reads either annotation format the platform produces:
  * zero_shot_annotation's flat output:
      {"annotations": [{"image_path", "bounding_box": [x1, y1, x2, y2], "score", "class", "segmentation"?}]}
  * the Smart Labeler's own annotations.json (also hf_inference's):
      {"annotation_type", "annotations": {"<image path>": [{"label", "x", "y", "width", "height" | "points"}]}}

Usage:
    python zero_shot_to_yolo.py --annotations annotations.json --images /path/to/images --output /path/to/dataset
"""

import argparse
import json
import os
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

from annotations_io import load_annotations

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert zero-shot (or Smart Labeler) annotations to a YOLO dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--annotations", required=True, help="Annotations JSON file")
    parser.add_argument("--images", required=True, help="Folder holding the annotated images")
    parser.add_argument("--output", required=True, help="Folder to write the YOLO dataset into")
    parser.add_argument("--summary", default=None, help="Where to write conversion_summary.json (default: next to --output)")
    parser.add_argument("--task", choices=["detect", "segment"], default="detect",
                        help="detect writes boxes; segment writes polygons (a box-only annotation becomes a rectangle)")
    parser.add_argument("--val_split", type=float, default=0.2, help="Fraction of images held out for validation")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for the train/val split")
    parser.add_argument("--min_score", type=float, default=0.0, help="Drop annotations scored below this")
    parser.add_argument("--classes", default="",
                        help="Comma-separated class names in index order; labels not listed are dropped. "
                             "Empty uses every label found, sorted")
    parser.add_argument("--drop_empty", action="store_true",
                        help="Leave out images with no annotations (by default they're kept as background images)")
    return parser.parse_args()


# ─────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────
def discover_images(root: Path) -> dict:
    """{relative posix path: absolute Path} for every image under root.

    os.walk with followlinks: a staged input is often a symlink to the real
    folder, and Path.rglob doesn't descend into symlinked subfolders.
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                found[path.relative_to(root).as_posix()] = path
    return found


def match_key(key: str, images: dict, by_name: dict):
    """The discovered image an annotation key refers to, or None.

    Zero-shot keys are relative to its image folder, which is this folder; a
    Smart Labeler key is often a full Tapis path, so it's matched by trailing
    path, then by file name when exactly one image has that name.
    """
    key = key.replace("\\", "/").lstrip("/")
    if key in images:
        return key
    for rel in images:
        if key.endswith("/" + rel):
            return rel
    names = by_name.get(key.rsplit("/", 1)[-1], [])
    return names[0] if len(names) == 1 else None


# ─────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────
def _clip(v: float) -> float:
    return min(1.0, max(0.0, v))


def label_line(det: dict, cls: int, w: int, h: int, task: str):
    """One YOLO label line, or None when the annotation has no usable shape."""
    box, polygon = det["box"], det["polygon"]
    if task == "segment":
        if not polygon and box:
            x1, y1, x2, y2 = box
            polygon = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        if not polygon or len(polygon) < 3:
            return None
        coords = " ".join(f"{_clip(x / w):.6f} {_clip(y / h):.6f}" for x, y in polygon)
        return f"{cls} {coords}"
    if not box and polygon:
        xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
        box = [min(xs), min(ys), max(xs), max(ys)]
    if not box:
        return None
    x1, y1, x2, y2 = (_clip(box[0] / w), _clip(box[1] / h), _clip(box[2] / w), _clip(box[3] / h))
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return None
    return f"{cls} {x1 + bw / 2:.6f} {y1 + bh / 2:.6f} {bw:.6f} {bh:.6f}"


def write_data_yaml(path: Path, names: list, has_val: bool):
    # No `path:` key: Ultralytics then resolves train/val next to this file,
    # wherever the dataset is copied (the training job stages it into its own
    # dir). Names are JSON strings, which YAML reads as double-quoted scalars.
    lines = [
        "# Written by zero_shot_to_yolo.py",
        "train: images/train",
        f"val: {'images/val' if has_val else 'images/train'}",
        f"nc: {len(names)}",
        "names:",
    ] + [f"  {i}: {json.dumps(n)}" for i, n in enumerate(names)]
    path.write_text("\n".join(lines) + "\n")


def main():
    args = parse_args()
    images_root = Path(args.images)
    output = Path(args.output)

    with open(args.annotations, encoding="utf-8") as f:
        annotations = load_annotations(json.load(f))
    images = discover_images(images_root)
    if not images:
        print(f"ERROR: no images found under {images_root}")
        sys.exit(1)
    by_name = {}
    for rel in images:
        by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)

    print(f"INFO: {len(images)} images, annotations for {len(annotations)} keys")

    # Annotations per discovered image, filtered by score.
    per_image = {rel: [] for rel in images}
    unmatched_keys, low_score = [], 0
    for key, dets in annotations.items():
        rel = match_key(key, images, by_name)
        if rel is None:
            unmatched_keys.append(key)
            continue
        for d in dets:
            if isinstance(d["score"], (int, float)) and d["score"] < args.min_score:
                low_score += 1
                continue
            per_image[rel].append(d)

    fixed = [c.strip() for c in args.classes.split(",") if c.strip()]
    names = fixed or sorted({d["label"] for dets in per_image.values() for d in dets})
    index = {n: i for i, n in enumerate(names)}

    selected = [rel for rel in images if per_image[rel] or not args.drop_empty]
    rng = random.Random(args.seed)
    order = sorted(selected)
    rng.shuffle(order)
    n_val = int(round(len(order) * args.val_split)) if args.val_split > 0 else 0
    if args.val_split > 0 and len(order) > 1:
        n_val = min(max(n_val, 1), len(order) - 1)
    val = set(order[:n_val])

    if output.exists():
        shutil.rmtree(output)  # idempotent: a re-run never mixes in an older split
    counts, per_split = Counter(), Counter()
    unknown_label, no_shape = Counter(), 0
    for rel in sorted(selected):
        split = "val" if rel in val else "train"
        src = images[rel]
        with Image.open(src) as im:
            w, h = im.size
        lines = []
        for d in per_image[rel]:
            if d["label"] not in index:
                unknown_label[d["label"]] += 1
                continue
            line = label_line(d, index[d["label"]], w, h, args.task)
            if line is None:
                no_shape += 1
                continue
            lines.append(line)
            counts[d["label"]] += 1

        img_dst = output / "images" / split / rel
        lbl_dst = (output / "labels" / split / rel).with_suffix(".txt")
        img_dst.parent.mkdir(parents=True, exist_ok=True)
        lbl_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, img_dst)
        lbl_dst.write_text("\n".join(lines) + ("\n" if lines else ""))
        per_split[split] += 1

    write_data_yaml(output / "data.yaml", names, has_val=bool(val))
    (output / "classes.txt").write_text("\n".join(names) + ("\n" if names else ""))

    summary = {
        "task": args.task,
        "classes": names,
        "images": {"train": per_split["train"], "val": per_split["val"]},
        "images_without_annotations": sum(1 for rel in selected if not per_image[rel]),
        "annotations_per_class": dict(counts),
        "dropped": {
            "below_min_score": low_score,
            "label_not_in_classes": dict(unknown_label),
            "no_usable_shape": no_shape,
            "images_left_out_empty": len(images) - len(selected),
        },
        "unmatched_annotation_keys": unmatched_keys[:50],
        "unmatched_annotation_key_count": len(unmatched_keys),
    }
    summary_path = Path(args.summary) if args.summary else output.parent / "conversion_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    print(f"INFO: classes ({len(names)}): {', '.join(names) or '-'}")
    print(f"INFO: train {per_split['train']} / val {per_split['val']} images, "
          f"{sum(counts.values())} labels written")
    if unmatched_keys:
        print(f"WARNING: {len(unmatched_keys)} annotation key(s) matched no image under {images_root}, "
              f"e.g. {unmatched_keys[0]!r}. Is --images the folder the annotations were made on?")
    if unknown_label:
        print(f"WARNING: dropped labels not in --classes: {dict(unknown_label)}")
    print(f"Dataset written to: {output}")
    print(f"Summary written to: {summary_path}")
    if not sum(counts.values()):
        print("ERROR: no labels were written, so this dataset can't train a model")
        sys.exit(1)


if __name__ == "__main__":
    main()
