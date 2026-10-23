"""
split_dataset.py
================

Splits a dataset folder into train / validation / test, in the layout
Ultralytics YOLO training expects. Any existing split is ignored and redone.

Detection / segmentation (images with YOLO .txt labels):

    <output>/
      data.yaml                      # train: train/images, val: valid/images, test: test/images, nc, names
      train/images/...  train/labels/...
      valid/images/...  valid/labels/...
      test/images/...   test/labels/...

  A label is found next to its image (a.jpg + a.txt), or in the matching
  labels/ folder (images/x/a.jpg -> labels/x/a.txt), so flat folders, the
  Annotations to YOLO Dataset output and Roboflow-style exports all work.
  Class names come from the input's data.yaml or classes.txt.

Classification (one folder per class):

    <output>/train/<class>/...   val/<class>/...   test/<class>/...

  Split class by class, so every split keeps the class balance. Named "val",
  not "valid": that's the folder Ultralytics looks for when classifying.

Usage:
    python split_dataset.py --input ./dataset --output ./split --val_split 0.2 --test_split 0.1
"""

import argparse
import json
import os
import random
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
# Folder names that mark an existing split or the images half of a YOLO
# dataset. Dropped from an image's path in the output, so a re-split dataset
# doesn't end up with train/images/valid/images/a.jpg.
LAYOUT_DIRS = {"train", "val", "valid", "validation", "test", "images"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Split a dataset into train/valid/test for YOLO",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="Dataset folder to split")
    parser.add_argument("--output", required=True, help="Folder to write the split dataset into")
    parser.add_argument("--summary", default=None, help="Where to write split_summary.json (default: next to --output)")
    parser.add_argument("--val_split", type=float, default=0.2, help="Fraction of images for validation")
    parser.add_argument("--test_split", type=float, default=0.1, help="Fraction of images for test; 0 for no test split")
    parser.add_argument("--seed", type=int, default=42, help="Random seed, so the same input always splits the same way")
    parser.add_argument("--type", choices=["auto", "detect", "classify"], default="auto",
                        help="'detect' for images with YOLO .txt labels (detection or segmentation), 'classify' for one "
                             "folder per class. 'auto' picks 'detect' when any label file exists")
    parser.add_argument("--drop_unlabeled", action="store_true",
                        help="Detection only: leave out images with no label file (by default they're kept as "
                             "background images with an empty label)")
    return parser.parse_args()


# ─────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────
def walk_files(root: Path):
    """Every file under root, following symlinked folders (staged inputs often are)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


def label_for(image: Path, root: Path):
    """The YOLO label file for an image, or None."""
    beside = image.with_suffix(".txt")
    if beside.is_file():
        return beside
    parts = list(image.relative_to(root).parts)
    for i in range(len(parts) - 2, -1, -1):  # the innermost images/ folder
        if parts[i] == "images":
            candidate = root.joinpath(*parts[:i], "labels", *parts[i + 1:]).with_suffix(".txt")
            if candidate.is_file():
                return candidate
            break
    return None


def clean_rel(image: Path, root: Path) -> Path:
    """The image's path inside its split, without old split/images folders."""
    parts = [p for p in image.relative_to(root).parts[:-1] if p.lower() not in LAYOUT_DIRS]
    return Path(*parts, image.name)


def read_names(root: Path):
    """Class names from data.yaml (names: list or {id: name}) or classes.txt, or None."""
    for yaml_path in sorted(root.rglob("data.yaml")) + sorted(root.rglob("*.yaml")):
        try:
            import yaml
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except Exception:
            continue
        names = data.get("names") if isinstance(data, dict) else None
        if isinstance(names, dict):
            return [str(names[k]) for k in sorted(names, key=int)]
        if isinstance(names, list):
            return [str(n) for n in names]
    for txt in sorted(root.rglob("classes.txt")):
        names = [l.strip() for l in txt.read_text().splitlines() if l.strip()]
        if names:
            return names
    return None


# ─────────────────────────────────────────────
# Splitting
# ─────────────────────────────────────────────
def split_counts(n: int, val: float, test: float) -> tuple:
    """(n_train, n_val, n_test), keeping at least one training image and one
    in each requested split whenever there are enough images."""
    n_test = int(round(n * test)) if test > 0 else 0
    n_val = int(round(n * val)) if val > 0 else 0
    if test > 0 and n >= 3:
        n_test = max(n_test, 1)
    if val > 0 and n >= 2:
        n_val = max(n_val, 1)
    while n_val + n_test >= n and (n_val or n_test):  # always leave something to train on
        if n_test >= n_val and n_test:
            n_test -= 1
        else:
            n_val -= 1
    return n - n_val - n_test, n_val, n_test


def assign(items: list, val: float, test: float, rng: random.Random) -> dict:
    order = sorted(items)
    rng.shuffle(order)
    _, n_val, n_test = split_counts(len(order), val, test)
    return {"test": order[:n_test], "val": order[n_test:n_test + n_val], "train": order[n_test + n_val:]}


RENAMED = Counter()


def unique(path: Path, taken: set) -> Path:
    """path, or path_1, path_2, ... when two inputs land on the same output path
    (e.g. train/a.jpg and valid/a.jpg in the input)."""
    candidate, n = path, 1
    while candidate in taken:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        n += 1
    if n > 1:
        RENAMED["files"] += 1
    taken.add(candidate)
    return candidate


def main():
    args = parse_args()
    root = Path(args.input)
    output = Path(args.output)
    if not root.is_dir():
        print(f"ERROR: not a folder: {root}")
        sys.exit(1)
    if not 0 <= args.val_split < 1 or not 0 <= args.test_split < 1 or args.val_split + args.test_split >= 1:
        print("ERROR: --val_split and --test_split must each be in [0, 1) and add up to less than 1")
        sys.exit(1)

    files = list(walk_files(root))
    images = [p for p in files if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not images:
        print(f"ERROR: no images found under {root}")
        sys.exit(1)
    labels = {p: label_for(p, root) for p in images}
    kind = args.type
    if kind == "auto":
        kind = "detect" if any(labels.values()) else "classify"
    print(f"INFO: {len(images)} images, treating as a {kind} dataset")

    rng = random.Random(args.seed)
    if output.exists():
        shutil.rmtree(output)  # idempotent: a re-run never mixes in an older split
    taken, counts = set(), Counter()
    summary = {"type": kind, "seed": args.seed, "val_split": args.val_split, "test_split": args.test_split}

    if kind == "detect":
        unlabeled = [p for p in images if labels[p] is None]
        use = [p for p in images if labels[p] is not None or not args.drop_unlabeled]
        folders = {"train": "train", "val": "valid", "test": "test"}
        ids = Counter()
        for split, members in assign(use, args.val_split, args.test_split, rng).items():
            for img in members:
                rel = unique(Path(folders[split]) / clean_rel(img, root), taken)
                img_dst = output / rel.parts[0] / "images" / Path(*rel.parts[1:])
                lbl_dst = (output / rel.parts[0] / "labels" / Path(*rel.parts[1:])).with_suffix(".txt")
                img_dst.parent.mkdir(parents=True, exist_ok=True)
                lbl_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(img, img_dst)
                text = labels[img].read_text() if labels[img] else ""
                lbl_dst.write_text(text)
                for line in text.splitlines():
                    if line.split():
                        ids[line.split()[0]] += 1
                counts[folders[split]] += 1

        names = read_names(root)
        max_id = max((int(float(i)) for i in ids), default=-1)
        if not names:
            names = [f"class_{i}" for i in range(max_id + 1)]
            print("WARNING: no data.yaml or classes.txt with class names in the input; "
                  "naming classes class_0, class_1, ... in data.yaml")
        elif max_id >= len(names):
            print(f"WARNING: labels use class {max_id}, but only {len(names)} names were found")
        lines = ["# Written by split_dataset.py", "train: train/images",
                 f"val: {'valid/images' if counts['valid'] else 'train/images'}"]
        if counts["test"]:
            lines.append("test: test/images")
        lines += [f"nc: {len(names)}", "names:"] + [f"  {i}: {json.dumps(n)}" for i, n in enumerate(names)]
        output.mkdir(parents=True, exist_ok=True)
        (output / "data.yaml").write_text("\n".join(lines) + "\n")
        summary.update({
            "images": {k: counts[k] for k in ("train", "valid", "test")},
            "classes": names,
            "labels_per_class_id": dict(sorted(ids.items(), key=lambda kv: int(float(kv[0])))),
            "images_without_label_file": len(unlabeled),
            "left_out_unlabeled": len(unlabeled) if args.drop_unlabeled else 0,
        })
    else:
        by_class = defaultdict(list)
        for img in images:
            cls = img.parent.name
            if img.parent.resolve() == root.resolve() or cls.lower() in LAYOUT_DIRS:
                print(f"ERROR: {img.relative_to(root)} isn't inside a class folder. Classification needs one "
                      f"folder per class (e.g. healthy/img.jpg); for detection, include the YOLO .txt labels.")
                sys.exit(1)
            by_class[cls].append(img)
        per_class = {}
        for cls in sorted(by_class):
            per_class[cls] = {}
            for split, members in assign(by_class[cls], args.val_split, args.test_split, rng).items():
                for img in members:
                    dst = output / unique(Path(split) / cls / img.name, taken)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(img, dst)
                    counts[split] += 1
                per_class[cls][split] = len(members)
        summary.update({"images": {k: counts[k] for k in ("train", "val", "test")},
                        "classes": sorted(by_class), "images_per_class": per_class})

    summary["renamed_to_avoid_clashes"] = RENAMED["files"]
    summary_path = Path(args.summary) if args.summary else output.parent / "split_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print("INFO: " + ", ".join(f"{k} {v}" for k, v in summary["images"].items()) + " images")
    print(f"Dataset written to: {output}")
    print(f"Summary written to: {summary_path}")


if __name__ == "__main__":
    main()
