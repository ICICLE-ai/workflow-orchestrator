#!/usr/bin/env python3
"""
run_job.py

The Tapis-app entrypoint for the platform's 'scouting_mission' step
(backend/steps/scouting_mission/step.json, tapis_app_id
"generate-scouting-mission"). A thin wrapper around fly_mission.py's
mission-generation path -- the exact same code as running
`fly_mission.py --dry-run` -- reused as-is rather than reimplemented.

This job ONLY generates mission files (.waypoints/.plan/.kmz/.json). It
never imports olympe and never attempts to fly anything: flying requires
the drone's own local Wi-Fi network (typically 192.168.42.1), which no
HPC batch/exec system is ever on. Actually flying + capturing +
uploading media happens afterward, locally, on a machine connected to
the ANAFI, via fly_mission.py directly or the standalone executable
build_executable.sh produces from it -- see this folder's README.md.

On top of generation, this also writes next_steps.txt into the output
directory: the exact fly_mission.py command line to run locally to fly
this same mission, pre-filled with everything the platform already
knows -- the mission parameters, and the Tapis upload destination
entered in the step's "Image upload destination" field -- so the only
things left to fill in by hand are --drone-ip (if not the default) and
--tapis-token.
"""

from __future__ import annotations

import argparse
import os
import shutil

import fly_mission as fm


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate a scouting mission (lawnmower survey over a "
                    "farm boundary). Generation only -- does not fly."
    )
    p.add_argument("--boundary", required=True, type=fm.parse_latlon_list,
                    help="Farm boundary polygon as 'lat1,lon1;lat2,lon2;...'")
    p.add_argument("--home", required=True, type=fm.parse_latlon_pair,
                    help="Approximate starting point as 'lat,lon'")
    p.add_argument("--altitude", type=float, default=10.0)
    p.add_argument("--line-spacing", type=float, required=True)
    p.add_argument("--mode", choices=["image", "video"], required=True)
    p.add_argument("--capture-spacing", type=float, default=None)
    p.add_argument("--hold-time", type=float, default=2.0)
    p.add_argument("--out-dir", default="/output/mission_files")
    p.add_argument("--mission-name", default="mission")
    # Carried through only to prefill next_steps.txt below -- not used
    # for mission generation itself.
    p.add_argument("--tapis-system-id", default="")
    p.add_argument("--tapis-remote-path", default="")
    p.add_argument("--json-copy-to", default=None,
                    help="If given, also copy the generated <mission-name>.json "
                         "here (lets the platform expose it as its own output "
                         "port alongside the mission_files directory).")
    return p


def write_next_steps(args: argparse.Namespace, out_dir: str) -> None:
    boundary_str = ";".join(f"{lat},{lon}" for lat, lon in args.boundary)
    home_str = f"{args.home[0]},{args.home[1]}"

    lines = [
        "This job only GENERATES the mission -- it does not fly anything.",
        "HPC batch systems aren't on the drone's own Wi-Fi network, so",
        "flying + capturing + uploading media happens locally, on a",
        "machine connected to the ANAFI, using fly_mission.py (or the",
        "standalone executable built from it via build_executable.sh --",
        "see jobs/scouting_mission_generator/README.md).",
        "",
        "Run locally:",
        "",
        "  ./fly_mission \\",
        f"      --boundary \"{boundary_str}\" \\",
        f"      --home \"{home_str}\" \\",
        f"      --altitude {args.altitude} \\",
        f"      --line-spacing {args.line_spacing} \\",
        f"      --mode {args.mode} \\",
    ]
    if args.mode == "image" and args.capture_spacing:
        lines.append(f"      --capture-spacing {args.capture_spacing} \\")
        lines.append(f"      --hold-time {args.hold_time} \\")
    lines += [
        "      --drone-ip 192.168.42.1 \\",
        "      --tapis-base-url <your Tapis tenant base URL> \\",
        "      --tapis-token \"$TAPIS_TOKEN\" \\",
        f"      --tapis-system-id {args.tapis_system_id or '<system id>'} \\",
        f"      --tapis-remote-path {args.tapis_remote_path or '<remote path>'}",
        "",
        "(--tapis-system-id / --tapis-remote-path above are pre-filled from",
        " the \"Image upload destination\" field you set on the step, if any.)",
    ]
    with open(os.path.join(out_dir, "next_steps.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    args = build_arg_parser().parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # Same code path as `fly_mission.py --dry-run` -- generates the
    # mission and writes .waypoints/.plan/.kmz/.json into --out-dir.
    fm.generate(args)

    write_next_steps(args, args.out_dir)

    if args.json_copy_to:
        generated_json = os.path.join(args.out_dir, f"{args.mission_name}.json")
        os.makedirs(os.path.dirname(args.json_copy_to) or ".", exist_ok=True)
        shutil.copyfile(generated_json, args.json_copy_to)


if __name__ == "__main__":
    main()
