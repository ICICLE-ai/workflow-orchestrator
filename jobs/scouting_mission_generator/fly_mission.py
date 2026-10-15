#!/usr/bin/env python3
"""
fly_mission.py

The single entry point the end user runs (or double-clicks, once built
into a standalone executable -- see build_executable.sh). It:

  1. Generates the survey mission from the farm boundary / parameters
     you give it, and writes .waypoints / .plan / .kmz / .json next to
     wherever you point --out-dir.
  2. Connects to a Parrot ANAFI over Olympe, uploads the flight plan,
     and flies it.
  3. Because ANAFI's onboard flight-plan player does not reliably fire
     MAVLink camera-trigger commands, this script itself watches mission
     progress and calls the Olympe camera API directly at each
     capture-stop waypoint (image mode) or immediately after takeoff /
     before RTL (video mode).
  4. After the drone lands, downloads the newly-captured media from the
     drone to a local folder.
  5. Uploads that folder to a Tapis storage system using the token
     passed in at job submission time.

------------------------------------------------------------------------
IMPORTANT -- READ BEFORE FLYING
------------------------------------------------------------------------
* This targets Parrot ANAFI specifically, via Parrot's Olympe SDK.
  Olympe is NOT distributed on PyPI -- it must be installed on the
  Linux machine that will actually run this (or baked into the build
  environment before running build_executable.sh) following Parrot's
  own installation instructions:
      https://developer.parrot.com/docs/olympe/installation.html
  This script imports olympe lazily and will print a clear error
  instead of a stack trace if it's missing, but it cannot fly anything
  without it.

* The mission-generation step (--dry-run) has no such dependency and
  will always work, so you can generate and sanity-check the
  .waypoints/.plan/.kmz files on any machine, then move only the actual
  flight to a machine with Olympe + drone connectivity.

  This is also why only the generation path (fly_mission.py --dry-run)
  runs as the platform's own "scouting_mission" step (see
  backend/steps/scouting_mission/step.json and run_job.py in this
  folder) -- an HPC batch node has no route to the drone's own local
  Wi-Fi network (192.168.42.1), so the actual flight + capture + upload
  below always runs locally, on a machine connected to the drone, via
  this script directly or the standalone executable built from it.

* Camera-trigger timing (image mode) is driven by this script watching
  the drone's position/mission-item progress in real time, not by the
  uploaded flight-plan file's camera commands -- those are included in
  the exported files for compatibility with other GCS tools, but are a
  fallback only for ANAFI.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List, Optional, Tuple

import mission_generator as mg
import tapis_upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fly_mission")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_latlon_list(raw: str) -> List[Tuple[float, float]]:
    """Parses a boundary given as 'lat1,lon1;lat2,lon2;...' """
    pts = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        lat_str, lon_str = pair.split(",")
        pts.append((float(lat_str), float(lon_str)))
    if len(pts) < 3:
        raise argparse.ArgumentTypeError(
            "boundary must have at least 3 points, e.g. "
            "'40.013,-83.046;40.013,-83.045;40.014,-83.045'"
        )
    return pts


def parse_latlon_pair(raw: str) -> Tuple[float, float]:
    lat_str, lon_str = raw.split(",")
    return (float(lat_str), float(lon_str))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate and fly a Parrot ANAFI survey mission, then "
                    "upload captured media to Tapis."
    )
    p.add_argument("--boundary", required=True, type=parse_latlon_list,
                    help="Farm boundary polygon as 'lat1,lon1;lat2,lon2;...' "
                         "(3+ points, any order, will be traced as given)")
    p.add_argument("--home", required=True, type=parse_latlon_pair,
                    help="Approximate starting point as 'lat,lon' -- used "
                         "for takeoff/RTL and to orient the scan pattern")
    p.add_argument("--altitude", type=float, default=10.0,
                    help="Flight altitude in meters, relative to takeoff (default: 10)")
    p.add_argument("--line-spacing", type=float, required=True,
                    help="Distance in meters between parallel lawnmower lines")
    p.add_argument("--mode", choices=["image", "video"], required=True,
                    help="'image' = stop and capture stills at --capture-spacing; "
                         "'video' = record continuously through the whole mission")
    p.add_argument("--capture-spacing", type=float, default=None,
                    help="Distance in meters between still-image captures "
                         "(required when --mode image)")
    p.add_argument("--hold-time", type=float, default=2.0,
                    help="Seconds to hover at each capture stop, image mode only "
                         "(default: 2.0)")
    p.add_argument("--out-dir", default="./mission_output",
                    help="Where to write the generated mission files "
                         "(default: ./mission_output)")
    p.add_argument("--mission-name", default="farm_mission",
                    help="Base filename for the generated mission files "
                         "(default: farm_mission)")

    p.add_argument("--dry-run", action="store_true",
                    help="Only generate mission files; do not attempt to "
                         "connect to a drone or upload anything")

    p.add_argument("--drone-ip", default="192.168.42.1",
                    help="ANAFI IP address (default: 192.168.42.1, the "
                         "standard direct Wi-Fi address)")

    p.add_argument("--media-dir", default=None,
                    help="Where to save media downloaded from the drone "
                         "(default: <out-dir>/media)")

    p.add_argument("--tapis-base-url", default=None,
                    help="Tapis tenant base URL, e.g. https://tacc.tapis.io")
    p.add_argument("--tapis-token", default=None,
                    help="Tapis bearer JWT, normally passed through from "
                         "job submission -- can also be set via the "
                         "TAPIS_TOKEN environment variable instead")
    p.add_argument("--tapis-system-id", default=None,
                    help="Destination Tapis storage system id")
    p.add_argument("--tapis-remote-path", default=None,
                    help="Destination directory on the Tapis system, e.g. "
                         "missions/2026-09-14_run6/media")
    p.add_argument("--skip-upload", action="store_true",
                    help="Fly and download media, but skip the Tapis upload step")

    return p


# ---------------------------------------------------------------------------
# Mission generation step (no drone / network dependency)
# ---------------------------------------------------------------------------

def generate(args) -> "mg.Mission":
    if args.mode == "image" and not args.capture_spacing:
        sys.exit("--capture-spacing is required when --mode image")

    mission = mg.build_mission(
        boundary_latlon=args.boundary,
        home_latlon=args.home,
        altitude_m=args.altitude,
        line_spacing_m=args.line_spacing,
        capture_mode=args.mode,
        capture_spacing_m=args.capture_spacing,
        hold_time_s=args.hold_time,
    )
    paths = mg.export_all(mission, args.out_dir, base_name=args.mission_name)
    total_len = mg.path_length_m(mission)

    log.info(f"Mission generated: {len(mission.waypoints)} waypoints, "
              f"{total_len:.1f} m total path length")
    for fmt, path in paths.items():
        log.info(f"  {fmt}: {path}")

    return mission, paths


# ---------------------------------------------------------------------------
# Flight execution step (requires Olympe + a real/simulated ANAFI)
# ---------------------------------------------------------------------------

def fly_and_capture(mission: "mg.Mission", args) -> str:
    """
    Connects to the ANAFI, flies the mission, triggers camera capture at
    the right moments, lands, and downloads media.

    Returns the local directory the downloaded media was saved to.
    """
    try:
        import olympe
        from olympe.messages.ardrone3.Piloting import (
            TakeOff, Landing, moveTo,
        )
        from olympe.messages.ardrone3.PilotingState import (
            FlyingStateChanged, moveToChanged,
        )
        from olympe.messages.camera import take_photo, stop_recording, start_recording
        from olympe.messages.rth import return_to_home
    except ImportError as e:
        sys.exit(
            "Could not import olympe. Parrot's Olympe SDK is not available "
            "on PyPI and must be installed separately on this machine "
            "following Parrot's instructions: "
            "https://developer.parrot.com/docs/olympe/installation.html\n"
            f"(underlying import error: {e})"
        )

    media_dir = args.media_dir or os.path.join(args.out_dir, "media")
    os.makedirs(media_dir, exist_ok=True)

    log.info(f"Connecting to ANAFI at {args.drone_ip} ...")
    drone = olympe.Drone(args.drone_ip)
    drone.connect()

    try:
        log.info("Taking off ...")
        drone(TakeOff() >> FlyingStateChanged(state="hovering", _timeout=15)).wait()

        if mission.capture_mode == "video":
            log.info("Starting video recording ...")
            drone(start_recording()).wait()

        for i, wp in enumerate(mission.waypoints):
            log.info(f"Waypoint {i+1}/{len(mission.waypoints)}: "
                     f"({wp.lat:.6f}, {wp.lon:.6f}) alt={wp.alt_m}m")
            drone(
                moveTo(wp.lat, wp.lon, wp.alt_m, orientation_mode=0, heading=0)
                >> moveToChanged(status="DONE", _timeout=30)
            ).wait()

            if wp.is_capture_stop:
                if wp.hold_time_s > 0:
                    time.sleep(wp.hold_time_s)
                log.info(f"  capturing photo at waypoint {i+1}")
                drone(take_photo(cam_id=0)).wait()

        if mission.capture_mode == "video":
            log.info("Stopping video recording ...")
            drone(stop_recording()).wait()

        log.info("Mission complete, returning to home ...")
        drone(return_to_home()).wait()
        drone(FlyingStateChanged(state="landed", _timeout=120)).wait()

    finally:
        log.info(f"Downloading captured media to {media_dir} ...")
        try:
            # Olympe's media API varies a bit by SDK version; this covers
            # the common resource-download pattern. Adjust if your
            # installed Olympe version's media API differs.
            media_list = drone.media.list()
            for media in media_list:
                for resource in media.resources:
                    dest = os.path.join(media_dir, resource.resource_id)
                    drone.media.download(resource, dest)
                    log.info(f"  downloaded {resource.resource_id}")
        except Exception as e:
            log.error(f"Media download step failed or is unsupported by "
                      f"this Olympe version -- check media manually on the "
                      f"drone/companion storage. Error: {e}")

        drone.disconnect()

    return media_dir


# ---------------------------------------------------------------------------
# Upload step
# ---------------------------------------------------------------------------

def upload(media_dir: str, args) -> None:
    token = args.tapis_token or os.environ.get("TAPIS_TOKEN")
    if not token:
        sys.exit("No Tapis token provided (--tapis-token or TAPIS_TOKEN env var)")
    if not (args.tapis_base_url and args.tapis_system_id and args.tapis_remote_path):
        sys.exit("--tapis-base-url, --tapis-system-id, and --tapis-remote-path "
                 "are all required to upload")

    log.info("Verifying Tapis token ...")
    if not tapis_upload.verify_token(args.tapis_base_url, token, log=log.info):
        sys.exit("Tapis token failed verification -- aborting before upload.")

    log.info(f"Uploading media from {media_dir} to Tapis "
             f"({args.tapis_system_id}:{args.tapis_remote_path}) ...")
    tapis_upload.upload_directory(
        base_url=args.tapis_base_url,
        token=token,
        system_id=args.tapis_system_id,
        remote_path=args.tapis_remote_path,
        local_dir=media_dir,
        log=log.info,
    )
    log.info("Upload complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = build_arg_parser().parse_args()

    mission, _paths = generate(args)

    if args.dry_run:
        log.info("--dry-run set: mission files generated, skipping flight and upload.")
        return

    media_dir = fly_and_capture(mission, args)

    if args.skip_upload:
        log.info("--skip-upload set: leaving media locally, skipping Tapis upload.")
        return

    upload(media_dir, args)


if __name__ == "__main__":
    main()
