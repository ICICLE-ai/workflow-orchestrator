"""
merge_folders.py
================

Copies the contents of several folders into one target folder, keeping each
folder's own subfolder layout:

    input/folder_1/a.jpg, input/folder_1/sub/b.jpg     ->  output/a.jpg, output/sub/b.jpg
    input/folder_2/a.jpg                               ->  output/a_1.jpg   (--on_conflict rename)

Usage:
    python merge_folders.py --inputs /dir/one /dir/two --output /merged
    python merge_folders.py --inputs_root /job/input --output /job/output/merged
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Merge several folders into one",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--inputs", nargs="+", help="Folders to merge, in order")
    src.add_argument("--inputs_root", help="Merge every folder directly inside this one, in natural name order "
                                           "(folder_2 before folder_10)")
    parser.add_argument("--output", required=True, help="Target folder; created, and emptied first if it exists")
    parser.add_argument("--summary", default=None, help="Where to write merge_summary.json (default: next to --output)")
    parser.add_argument("--on_conflict", choices=["rename", "overwrite", "skip"], default="rename",
                        help="When two folders hold the same relative path: keep both (a_1.jpg), "
                             "let the later folder win, or keep the first")
    parser.add_argument("--into_subfolders", action="store_true",
                        help="Put each folder's contents under its own subfolder (its name) instead of merging flat")
    return parser.parse_args()


def _natural(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def free_name(path: Path) -> Path:
    """path, or the first of path_1, path_2, ... that doesn't exist."""
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def main():
    args = parse_args()
    if args.inputs_root:
        root = Path(args.inputs_root)
        inputs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: _natural(p.name))
    else:
        inputs = [Path(p) for p in args.inputs]
    missing = [str(p) for p in inputs if not p.is_dir()]
    if missing:
        print(f"ERROR: not a folder: {', '.join(missing)}")
        sys.exit(1)
    if not inputs:
        print("ERROR: no folders to merge")
        sys.exit(1)

    output = Path(args.output)
    if output.exists():
        shutil.rmtree(output)  # idempotent: the result reflects only this run's inputs
    output.mkdir(parents=True)

    per_input, renamed, overwritten, skipped = [], 0, 0, 0
    for src_root in inputs:
        copied = 0
        base = output / src_root.name if args.into_subfolders else output
        # followlinks: staged inputs are often symlinks to the real folders.
        for dirpath, dirnames, filenames in os.walk(src_root, followlinks=True):
            dirnames.sort()
            for name in sorted(filenames):
                src = Path(dirpath) / name
                dst = base / src.relative_to(src_root)
                if dst.exists():
                    if args.on_conflict == "skip":
                        skipped += 1
                        continue
                    if args.on_conflict == "rename":
                        dst = free_name(dst)
                        renamed += 1
                    else:
                        overwritten += 1
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
        per_input.append({"folder": str(src_root), "files_copied": copied})
        print(f"INFO: {src_root}: {copied} file(s)")

    total = sum(p["files_copied"] for p in per_input)
    summary = {
        "output": str(output),
        "inputs": per_input,
        "files": total,
        "on_conflict": args.on_conflict,
        "renamed": renamed,
        "overwritten": overwritten,
        "skipped": skipped,
    }
    summary_path = Path(args.summary) if args.summary else output.parent / "merge_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Merged {len(inputs)} folder(s), {total} file(s) into {output} "
          f"(renamed {renamed}, overwritten {overwritten}, skipped {skipped})")
    print(f"Summary written to: {summary_path}")


if __name__ == "__main__":
    main()
