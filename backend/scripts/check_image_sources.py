"""Check backend/image_sources.json against the steps that need a container image.

    cd backend && uv run python scripts/check_image_sources.py

Reports every key that doesn't match a step (a typo would otherwise be
silently ignored), every executable step and the link it will get, and the
steps still without one. Reads files only: links are NOT fetched, because
redeeming a Tapis postit uses up one of its allowed downloads.

Exits 1 when a key matches no step, so it can run in CI.
"""
from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.local_bundle import _load_image_sources, _norm_key, image_source_for, image_sources_path  # noqa: E402

STEPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "steps")


def executable_steps() -> list[dict]:
    steps = []
    for path in sorted(glob.glob(os.path.join(STEPS_DIR, "*", "step.json"))):
        with open(path) as f:
            d = json.load(f)
        job = d.get("tapis_job")
        if not job:
            continue  # sources, sinks and inline steps run no container
        staged = [
            fi for fi in job.get("fileInputs") or []
            if str(fi.get("targetPath", "")).endswith(".sif")
        ]
        steps.append({
            "step": d["step_type_key"],
            "app": d.get("tapis_app_id") or job.get("appId") or d["step_type_key"],
            "staged": staged[0]["targetPath"] if staged else None,
            "staged_url": staged[0].get("sourceUrl") if staged else None,
        })
    return steps


def main() -> int:
    sources = _load_image_sources()
    steps = executable_steps()
    print(f"Image links: {image_sources_path()} ({len(sources)} entries)\n")

    known = {_norm_key(s["app"]) for s in steps} | {_norm_key(s["step"]) for s in steps}
    unmatched = [k for k in sources if _norm_key(k) not in known]
    for key in unmatched:
        print(f"  ✗ key '{key}' matches no step's app id or name — typo?")
    if unmatched:
        print()

    missing = []
    width = max(len(s["step"]) for s in steps)
    for s in steps:
        link = image_source_for(sources, s["app"], s["step"])
        if s["staged"]:
            where = "image_sources.json" if link else "step.json"
            print(f"  ✓ {s['step']:<{width}}  {s['staged']} (step input), link from {where}")
        elif link:
            print(f"  ✓ {s['step']:<{width}}  {s['app']}.sif")
        else:
            print(f"  · {s['step']:<{width}}  {s['app']}.sif — no link")
            missing.append(s)

    if missing:
        print(f"\n{len(missing)} step(s) without a link. Users must build or copy these images themselves.")
        print("Add each under its app id (shown above) in image_sources.json.")
    return 1 if unmatched else 0


if __name__ == "__main__":
    sys.exit(main())
