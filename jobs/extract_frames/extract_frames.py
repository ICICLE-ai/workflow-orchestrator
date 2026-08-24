#!/usr/bin/env python3
"""Frame-extraction job for the Harvest workflow orchestrator.

ONE operation: decode a video and save sampled frames as images. Nothing else —
no detection, no resizing, no annotation. Downstream steps (yolo_inference,
image_preprocess_studio, ...) consume the frames directory this writes.

The source may be any of the three things the step exposes:
  * a public video link          --source https://host/clip.mp4
  * a Tapis file-system location --source-file /job/data/video   (staged by Tapis)
  * a live stream                --source rtsp://host/stream  (also rtsps/rtp/udp)
All three end up in the same cv2.VideoCapture call; only the stop condition and
the fps source differ (a live stream reports no frame count, and often no fps).

Usage:
    extract_frames.py --output <out_dir>
                      (--source <url> | --source-file <path>)
                      [--sampling-rate 1.0] [--sampling-mode fps|every_n|all]
                      [--max-frames 0] [--image-format jpg|png]

Outputs written to <out_dir>:
    frames/            — the extracted images, zero-padded so lexical order is
                         temporal order (image_dir output port)
    manifest.json      — per-frame metadata + source/video properties
                         (json_results output port)
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import cv2

# Schemes that never end on their own: they have no frame count, so the run is
# bounded by --max-frames / the job's walltime rather than by EOF.
LIVE_SCHEMES = ("rtsp://", "rtsps://", "rtp://", "udp://")

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".mpg", ".mpeg", ".webm", ".wmv", ".ts"}

# A live stream drops frames for all sorts of transient reasons; treat a read
# miss as end-of-stream only after this many in a row. A FILE ends at the first
# miss (that is what EOF looks like), so this applies to live sources only.
STREAM_READ_RETRIES = 30
STREAM_RETRY_SLEEP = 0.2

_CREDENTIALS = re.compile(r"(?<=://)[^/@\s]+:[^/@\s]+@")


def redact(source: str) -> str:
    """Strip user:pass@ out of a stream URL before it is logged or written to
    the manifest — RTSP cameras are routinely addressed with inline credentials,
    and the manifest is archived where anyone with the run can read it."""
    return _CREDENTIALS.sub("<redacted>@", source)


def is_live(source: str) -> bool:
    return source.lower().startswith(LIVE_SCHEMES)


def resolve_source(args) -> str:
    """Pick the source to open. A Tapis-staged file wins over --source, since a
    wired input port is a more specific statement of intent than a config field
    the user may have left over from an earlier edit."""
    staged = Path(args.source_file) if args.source_file else None
    if staged and staged.exists():
        if staged.is_dir():
            # A source node may hand over a directory rather than the file.
            vids = sorted(p for p in staged.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
            if not vids:
                raise SystemExit(f"[extract_frames] ERROR: no video file under {staged}")
            return str(vids[0])
        return str(staged)
    if args.source and args.source.strip():
        return args.source.strip()
    raise SystemExit(
        "[extract_frames] ERROR: no source. Wire the 'video' input port to a Tapis "
        "video file, or set the step's 'source' config to a public URL or an RTSP URL.")


def native_fps(cap) -> float:
    """Frames per second the container reports, or 0.0 when it doesn't know.

    Live streams frequently report 0 or NaN here, which is why every fps
    calculation below has a wall-clock fallback.
    """
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps is None or fps != fps or fps <= 0:  # fps != fps catches NaN
        return 0.0
    return float(fps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", help="Public video URL, or an rtsp://|rtsps:// stream URL")
    ap.add_argument("--source-file", default="", help="Local path to a video staged from the Tapis file system")
    ap.add_argument("--output", required=True, help="Directory to write frames/ and manifest.json into")
    ap.add_argument("--sampling-rate", type=float, default=1.0,
                    help="fps mode: frames to keep per second of video. every_n mode: keep 1 of every N frames.")
    ap.add_argument("--sampling-mode", choices=["fps", "every_n", "all"], default="fps",
                    help="How --sampling-rate is interpreted ('all' keeps every frame and ignores it)")
    ap.add_argument("--max-frames", type=int, default=0, help="Stop after this many SAVED frames (0 = unlimited)")
    ap.add_argument("--image-format", choices=["jpg", "png"], default="jpg", help="Frame image format")
    args = ap.parse_args()

    source = resolve_source(args)
    live = is_live(source)

    if args.sampling_mode == "fps" and args.sampling_rate <= 0:
        raise SystemExit("[extract_frames] ERROR: --sampling-rate must be > 0 in fps mode "
                         "(use --sampling-mode all to keep every frame)")
    if live and args.max_frames <= 0:
        print("[extract_frames] WARNING: live stream with no --max-frames — extraction only "
              "stops when the stream drops or the job hits its walltime.")

    out_dir = Path(args.output)
    # Keep the image_dir output port's directory free of anything that isn't an
    # image: manifest.json is its own json_results port, one level up.
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    print(f"[extract_frames] source:   {redact(source)}{' (live stream)' if live else ''}")
    print(f"[extract_frames] output:   {out_dir}")
    print(f"[extract_frames] sampling: {args.sampling_mode} @ {args.sampling_rate}")

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"[extract_frames] ERROR: could not open source {redact(source)}")

    fps = native_fps(cap)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total < 0:
        total = 0
    print(f"[extract_frames] video:    {width}x{height} @ {fps or 'unknown'} fps, "
          f"{total or 'unknown'} frame(s)")

    # Sampling state. Two strategies, picked per mode:
    #   every_n / fps-with-known-fps -> count decoded frames (exact, deterministic)
    #   fps-with-unknown-fps         -> gate on the wall clock (the only option
    #                                   for a live stream that reports no fps)
    every_n = max(1, int(round(args.sampling_rate))) if args.sampling_mode == "every_n" else 1
    frame_step = (fps / args.sampling_rate) if (args.sampling_mode == "fps" and fps) else 0.0
    next_index = 0.0
    wall_interval = (1.0 / args.sampling_rate) if args.sampling_mode == "fps" else 0.0
    last_saved_at = None

    started = time.monotonic()
    read_index = 0
    saved = 0
    misses = 0
    frames = []

    while True:
        ok, frame = cap.read()
        if not ok:
            if live and misses < STREAM_READ_RETRIES:
                misses += 1
                time.sleep(STREAM_RETRY_SLEEP)
                continue
            break
        misses = 0
        now = time.monotonic()

        if args.sampling_mode == "all":
            keep = True
        elif args.sampling_mode == "every_n":
            keep = read_index % every_n == 0
        elif frame_step:
            keep = read_index >= next_index
        else:
            keep = last_saved_at is None or (now - last_saved_at) >= wall_interval

        read_index += 1
        if not keep:
            continue
        if frame_step:
            next_index += frame_step
        last_saved_at = now

        # POS_MSEC is meaningful for a file; a live stream usually reports 0, so
        # fall back to elapsed wall time since the capture opened.
        pos_msec = cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0
        timestamp = round(pos_msec / 1000.0, 3) if pos_msec > 0 else round(now - started, 3)

        name = f"frame_{saved:06d}.{args.image_format}"
        if not cv2.imwrite(str(frames_dir / name), frame):
            cap.release()
            raise SystemExit(f"[extract_frames] ERROR: failed to write {frames_dir / name}")
        frames.append({
            "file": f"frames/{name}",
            "index": saved,
            "source_frame_index": read_index - 1,
            "timestamp_sec": timestamp,
        })
        saved += 1

        if saved % 100 == 0:
            print(f"[extract_frames] saved {saved} frame(s) ({read_index} read)")
        if args.max_frames and saved >= args.max_frames:
            print(f"[extract_frames] reached --max-frames {args.max_frames}")
            break

    cap.release()

    manifest = {
        "source": redact(source),
        "live_stream": live,
        "sampling_mode": args.sampling_mode,
        "sampling_rate": args.sampling_rate,
        "max_frames": args.max_frames,
        "image_format": args.image_format,
        "video": {
            "fps": fps or None,
            "width": width or None,
            "height": height or None,
            "frame_count": total or None,
            "duration_sec": round(total / fps, 3) if (total and fps) else None,
        },
        "frames_read": read_index,
        "frames_saved": saved,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "frames": frames,
    }
    with open(out_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    if saved == 0:
        # An empty frames/ dir would fail the next step at transfer time with a
        # message about a missing path rather than about the video.
        raise SystemExit(f"[extract_frames] ERROR: read {read_index} frame(s) from "
                         f"{redact(source)} but saved none — check the source and sampling rate")

    print(f"[extract_frames] DONE — {saved} frame(s) in {frames_dir}, manifest at {out_dir/'manifest.json'}")


if __name__ == "__main__":
    main()
