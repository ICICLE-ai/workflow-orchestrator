"""
merge_annotations.py
====================

Combines several annotation files (zero-shot flat output and/or Smart Labeler
annotations.json, in any mix) into one.

  * Annotations for the same image, from different files, are combined: e.g.
    two zero-shot runs on one folder with different text prompts.
  * --dedupe_iou drops near-duplicates of the same class on the same image
    (the higher-scored one is kept), for files that overlap.
  * --prefix_folders keys file N's images under folder_N/, which is where
    Merge Folders' --into_subfolders put that folder's images. Use it when the
    files describe DIFFERENT folders whose images may share names.

Usage:
    python merge_annotations.py --inputs a.json b.json --output merged.json
    python merge_annotations.py --inputs_root /job/input --output /job/output/annotations.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

from annotations_io import iou, read_file, to_smart_labeler, to_zero_shot


def parse_args():
    parser = argparse.ArgumentParser(
        description="Combine several annotation files into one",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--inputs", nargs="+", help="Annotation files, in order (file 1, file 2, ...)")
    src.add_argument("--inputs_root", help="Merge every annotations_<N>.json in this folder; N is the file's number")
    parser.add_argument("--output", required=True, help="The combined annotations file to write")
    parser.add_argument("--summary", default=None, help="Where to write merge_annotations_summary.json (default: next to --output)")
    parser.add_argument("--format", choices=["zero_shot", "smart_labeler"], default="zero_shot",
                        help="Format of the combined file. Both are read by Annotations to YOLO Dataset and the Smart Labeler")
    parser.add_argument("--dedupe_iou", type=float, default=0.0,
                        help="Drop an annotation overlapping a higher-scored one of the same class on the same image "
                             "by at least this IoU (0-1). 0 keeps everything")
    parser.add_argument("--prefix_folders", action="store_true",
                        help="Key file N's images under folder_N/, matching Merge Folders' --into_subfolders")
    return parser.parse_args()


def numbered_inputs(args) -> list:
    """[(N, Path)] in order. N is a file's position, or the number in its
    annotations_<N>.json name, so an unwired port in between doesn't shift the
    folder_N pairing."""
    if args.inputs:
        return [(i, Path(p)) for i, p in enumerate(args.inputs, start=1)]
    found = []
    for p in Path(args.inputs_root).iterdir():
        m = re.fullmatch(r"annotations_(\d+)\.json", p.name)
        if m and p.is_file():
            found.append((int(m.group(1)), p))
    return sorted(found)


def dedupe(dets: list, threshold: float) -> tuple:
    """Greedy per-class NMS: (kept, dropped count). Unscored annotations rank
    last but are otherwise treated the same."""
    order = sorted(dets, key=lambda a: a["score"] if isinstance(a["score"], (int, float)) else -1.0, reverse=True)
    kept = []
    for a in order:
        if any(k["label"] == a["label"] and iou(k, a) >= threshold for k in kept):
            continue
        kept.append(a)
    return kept, len(dets) - len(kept)


def main():
    args = parse_args()
    inputs = numbered_inputs(args)
    if not inputs:
        print("ERROR: no annotation files to merge")
        sys.exit(1)

    merged, seen_in, sources, total_in = {}, {}, [], 0
    for n, path in inputs:
        fmt, per_image, meta = read_file(path)
        count = sum(len(d) for d in per_image.values())
        total_in += count
        for key, dets in per_image.items():
            if args.prefix_folders:
                key = f"folder_{n}/{key.lstrip('/')}"
            merged.setdefault(key, []).extend(dets)
            seen_in.setdefault(key, set()).add(n)
        sources.append({"input": n, "file": str(path), "format": fmt, "images": len(per_image),
                        "annotations": count, **{k: meta[k] for k in ("model", "generated_at") if k in meta}})
        print(f"INFO: [{n}] {path.name}: {fmt}, {count} annotation(s) on {len(per_image)} image(s)")

    dropped = 0
    if args.dedupe_iou > 0:
        for key in merged:
            merged[key], d = dedupe(merged[key], args.dedupe_iou)
            dropped += d

    out = (to_zero_shot(merged, {"merged_from": sources}) if args.format == "zero_shot"
           else to_smart_labeler(merged))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(out, indent=2))

    total_out = sum(len(d) for d in merged.values())
    summary = {
        "format": args.format,
        "inputs": sources,
        "images": len(merged),
        "annotations_in": total_in,
        "annotations_out": total_out,
        "duplicates_dropped": dropped,
        "dedupe_iou": args.dedupe_iou,
        "prefix_folders": args.prefix_folders,
        "images_in_more_than_one_file": sum(1 for files in seen_in.values() if len(files) > 1),
    }
    summary_path = Path(args.summary) if args.summary else output.parent / "merge_annotations_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Merged {len(inputs)} file(s): {total_out} annotation(s) on {len(merged)} image(s)"
          + (f", {dropped} duplicate(s) dropped" if dropped else "") + f" -> {output}")
    print(f"Summary written to: {summary_path}")


if __name__ == "__main__":
    main()
