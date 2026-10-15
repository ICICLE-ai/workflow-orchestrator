"""
tapis_upload.py

Minimal Tapis v3 Files API client for pushing a folder of captured
media (photos or video) up to a Tapis storage system after a flight
completes.

This uses plain `requests` calls against the Tapis Files API rather than
the official `tapipy` SDK, specifically so the packaged executable stays
lightweight and doesn't need tapipy's heavier dependency tree bundled by
PyInstaller. If your orchestrator already standardizes on tapipy
elsewhere, swap this out -- the upload_directory() function signature is
the only thing fly_mission.py depends on.

Auth: Tapis expects a bearer JWT (an access token from a Tapis OAuth2
flow), not a raw "API key" in the traditional sense. In this workflow,
the orchestrator's job-submission step is expected to mint or pass
through a short-lived Tapis JWT alongside the job parameters, which
fly_mission.py receives as --tapis-token. If your orchestrator instead
issues long-lived service tokens, that still works here -- Tapis doesn't
care how the JWT was obtained, only that it's valid and not expired at
upload time.

Docs: https://tapis.readthedocs.io/en/latest/technical/files.html
"""

from __future__ import annotations

import os
import sys
import time
from typing import Iterable, List, Optional

import requests


class TapisUploadError(RuntimeError):
    pass


def _iter_upload_files(local_dir: str, extensions: Optional[Iterable[str]] = None) -> List[str]:
    exts = tuple(e.lower() for e in extensions) if extensions else None
    out = []
    for root, _dirs, files in os.walk(local_dir):
        for fname in files:
            if exts and not fname.lower().endswith(exts):
                continue
            out.append(os.path.join(root, fname))
    return sorted(out)


def upload_directory(
    base_url: str,
    token: str,
    system_id: str,
    remote_path: str,
    local_dir: str,
    extensions: Optional[Iterable[str]] = ("jpg", "jpeg", "png", "mp4"),
    max_retries: int = 3,
    retry_backoff_s: float = 3.0,
    log=print,
) -> List[str]:
    """
    Uploads every matching file in local_dir (recursively) to
    {base_url}/v3/files/content/{system_id}/{remote_path}/<filename>.

    Returns the list of local file paths that were successfully uploaded.
    Raises TapisUploadError if any file fails after retries.

    base_url:     e.g. "https://tacc.tapis.io"  (no trailing slash)
    token:        Tapis bearer JWT, passed through from job submission
    system_id:    Tapis storage system id, e.g. "farm-drone-storage"
    remote_path:  destination directory on that system, e.g.
                  "missions/2026-09-14_run6/media"
    """
    files = _iter_upload_files(local_dir, extensions)
    if not files:
        log(f"[tapis] no files matching {extensions} found under {local_dir}; nothing to upload")
        return []

    headers = {"X-Tapis-Token": token}
    uploaded = []
    remote_path = remote_path.strip("/")

    for fpath in files:
        fname = os.path.basename(fpath)
        url = f"{base_url.rstrip('/')}/v3/files/content/{system_id}/{remote_path}/{fname}"

        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                with open(fpath, "rb") as fh:
                    resp = requests.post(url, headers=headers, data=fh, timeout=120)
                if resp.status_code in (200, 201):
                    log(f"[tapis] uploaded {fname} -> {system_id}:{remote_path}/")
                    uploaded.append(fpath)
                    break
                else:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            except requests.RequestException as e:
                last_err = str(e)

            if attempt < max_retries:
                log(f"[tapis] upload of {fname} failed (attempt {attempt}/{max_retries}): "
                    f"{last_err} -- retrying in {retry_backoff_s:.0f}s")
                time.sleep(retry_backoff_s)
        else:
            raise TapisUploadError(f"Failed to upload {fpath} after {max_retries} attempts: {last_err}")

    log(f"[tapis] done: {len(uploaded)}/{len(files)} files uploaded")
    return uploaded


def verify_token(base_url: str, token: str, log=print) -> bool:
    """Quick sanity check that the token is accepted before we bother
    flying anything -- calling this at the top of fly_mission.py lets us
    fail fast on a bad/expired Tapis token instead of discovering it only
    after landing."""
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/v3/systems",
            headers={"X-Tapis-Token": token},
            timeout=15,
        )
        ok = resp.status_code == 200
        if not ok:
            log(f"[tapis] token verification failed: HTTP {resp.status_code}: {resp.text[:300]}")
        return ok
    except requests.RequestException as e:
        log(f"[tapis] token verification request failed: {e}")
        return False


if __name__ == "__main__":
    # Small standalone CLI for testing the uploader in isolation, e.g.:
    #   python3 tapis_upload.py --base-url https://tacc.tapis.io \
    #       --token $TAPIS_TOKEN --system-id farm-drone-storage \
    #       --remote-path missions/test --local-dir ./captured_media
    import argparse

    p = argparse.ArgumentParser(description="Upload a media folder to Tapis")
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--system-id", required=True)
    p.add_argument("--remote-path", required=True)
    p.add_argument("--local-dir", required=True)
    args = p.parse_args()

    if not verify_token(args.base_url, args.token):
        sys.exit("Tapis token failed verification -- aborting.")

    upload_directory(
        base_url=args.base_url,
        token=args.token,
        system_id=args.system_id,
        remote_path=args.remote_path,
        local_dir=args.local_dir,
    )
