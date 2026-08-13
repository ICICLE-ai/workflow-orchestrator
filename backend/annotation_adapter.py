"""Backend endpoint for the 'annotation_format_adapter' step (submits_job:
false — see backend/steps/annotation_format_adapter/step.json).

Unlike the Tapis-job-submitting steps, format conversion is plain data
reshaping (no GPU, no HPC queue), so it runs inline here at request time —
same reasoning as geospatial.py's on-demand GeoPackage->Shapefile/GeoJSON
conversions. All the actual format logic lives in annotation_formats.py
(pure, no Tapis); this module is just Tapis I/O around it: fetch the source,
convert, upload the result.

_do_convert below has TWO callers, and they must stay in sync because they are
the same operation at different times:
  * the /convert endpoint here — the panel's "Convert now" button, for a source
    that already exists at design time;
  * engine/inline_steps.py — the same conversion performed during a RUN, when
    the node's turn comes in the DAG, for a source an upstream step just
    produced. That path passes the run owner's token and turns HTTPException
    into a step failure, since there's no response to return outside a request.

Auth/URL helpers are duplicated from main.py rather than imported — same
"avoid a circular import: main imports this module to mount its router" reason
documented in geospatial.py.
"""
import io
import os
import tempfile

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import AppUser
from engine import tapis_auth
import annotation_formats as fmt

router = APIRouter(prefix="/api/annotation-adapter", tags=["annotation-adapter"])

# 'sam3_exemplars' is a destination only: it's the zero_shot_annotation job's
# PROMPT file (SAM3 geometry prompts), not an annotation format, so it has no
# parser and nothing round-trips back out of it. Everything else converts both
# ways — hence two tuples rather than one FORMATS.
FROM_FORMATS = ("native", "coco", "yolo", "geopackage")
TO_FORMATS = FROM_FORMATS + ("sam3_exemplars",)


def _current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(AppUser).filter(AppUser.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session user no longer exists")
    return user


def _user_tapis_token(user: AppUser, db: Session) -> str:
    token = tapis_auth.get_token_for_user(user, db)
    if not token:
        raise HTTPException(status_code=401, detail="No valid Tapis token — log in with a real Tapis account.")
    return token


def _tapis_url(kind: str, system: str, path: str) -> str:
    return f"{tapis_auth.TAPIS_BASE_URL}/v3/files/{kind}/{system}/{path.lstrip('/')}"


def _tapis_get_bytes(system: str, path: str, token: str) -> bytes:
    try:
        resp = httpx.get(_tapis_url("content", system, path), headers={"X-Tapis-Token": token}, timeout=60)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code != 200:
        code = resp.status_code if resp.status_code in (401, 404) else 502
        raise HTTPException(status_code=code, detail=f"Could not read {system}{path} (HTTP {resp.status_code}).")
    return resp.content


def _tapis_list(system: str, path: str, token: str) -> list:
    try:
        resp = httpx.get(_tapis_url("ops", system, path), headers={"X-Tapis-Token": token}, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code != 200:
        code = resp.status_code if resp.status_code in (401, 404) else 502
        raise HTTPException(status_code=code, detail=f"Could not list {system}{path} (HTTP {resp.status_code}).")
    return resp.json().get("result", [])


def _path_type(system: str, path: str, token: str) -> str | None:
    """Whether `path` already exists on Tapis, and if so as a 'file' or 'dir' —
    found by listing its PARENT and matching by name (listing `path` itself
    would, for a directory, return its CONTENTS rather than a self-describing
    entry, which doesn't tell you what `path` itself is). None if `path`
    doesn't exist yet, or the parent can't be listed."""
    path = path.rstrip("/")
    parent, _, name = path.rpartition("/")
    parent = parent or "/"
    try:
        resp = httpx.get(_tapis_url("ops", system, parent), headers={"X-Tapis-Token": token}, timeout=30)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    for item in resp.json().get("result", []):
        if item.get("name") == name:
            return item.get("type")
    return None


def _reject_if_directory(system: str, path: str, token: str) -> None:
    """Guard for a FILE-mode destination (to_format native/coco/geopackage):
    fail clearly up front if `path` already exists as a directory on Tapis,
    instead of silently colliding — the most likely real cause being a
    zero_shot_annotation `--output_dir` (itself a directory, holding an
    annotations/annotations_<model>_<timestamp>.json inside it) reused
    verbatim as this adapter's destination. Not called for to_format=yolo,
    where `path` IS meant to be a directory."""
    if _path_type(system, path, token) == "dir":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Destination '{path}' already exists as a DIRECTORY on Tapis, not a file. "
                "This adapter writes the converted output directly AT the destination path for "
                "native/coco/geopackage — no filename is appended, unlike a job's own --output_dir "
                "convention. Point it at a distinct file path instead (e.g. "
                ".../converted_annotations.json), not a directory a job already writes into."
            ),
        )


def _tapis_upload(system: str, path: str, content: bytes, token: str, content_type: str) -> None:
    filename = path.rstrip("/").split("/")[-1] or "file"
    try:
        resp = httpx.post(
            _tapis_url("ops", system, path),
            headers={"X-Tapis-Token": token},
            files={"file": (filename, content, content_type)},
            timeout=120,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Tapis: {type(e).__name__}")
    if resp.status_code not in (200, 201):
        code = 401 if resp.status_code == 401 else 502
        raise HTTPException(status_code=code, detail=f"Upload to {system}{path} failed (HTTP {resp.status_code}): {resp.text[:200]}")


# --- request/response models ------------------------------------------------

class TapisLoc(BaseModel):
    system: str
    path: str


class ConvertRequest(BaseModel):
    from_format: str
    to_format: str
    annotations: TapisLoc | None = None    # native/coco JSON file
    annotations_dir: TapisLoc | None = None  # yolo: flat directory of *.txt + classes.txt
    annotations_gpkg: TapisLoc | None = None  # geopackage source file
    images: TapisLoc | None = None          # image directory (dims for coco/yolo; key relativization for sam3_exemplars)
    dest: TapisLoc                          # destination: file (native/coco/geopackage/sam3_exemplars) or directory (yolo)
    text_prompts: list[str] | None = None   # sam3_exemplars only: top-level free-text concepts


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}


def _image_stems(system: str, path: str, token: str) -> dict:
    """Flat listing of an image directory -> {stem_without_ext: relpath}. See
    annotation_formats module docstring: YOLO support assumes a flat directory
    whose file stems match the label directory's file stems 1:1."""
    out = {}
    for entry in _tapis_list(system, path, token):
        name = entry.get("name") or ""
        stem, ext = os.path.splitext(name)
        if ext.lower() in IMAGE_EXTS:
            out[stem] = name
    return out


def _image_sizes(system: str, path: str, files: set, token: str) -> dict:
    """Download each of `files` (relpaths within the image dir) just far enough
    to read its pixel dimensions. One request per referenced image — fine for
    the dataset sizes this panel targets, but a real cost on very large sets;
    see the step's UI for that caveat."""
    from PIL import Image

    out = {}
    for file_path in files:
        try:
            data = _tapis_get_bytes(system, f"{path.rstrip('/')}/{file_path.lstrip('/')}", token)
            with Image.open(io.BytesIO(data)) as im:
                out[file_path] = im.size  # (width, height)
        except HTTPException:
            continue  # missing/unreadable image — that file's detections just get no size
    return out


@router.post("/convert")
def convert(body: ConvertRequest, db: Session = Depends(get_db), user: AppUser = Depends(_current_user)):
    if body.from_format not in FROM_FORMATS:
        raise HTTPException(status_code=422, detail=f"from_format must be one of {FROM_FORMATS}")
    if body.to_format not in TO_FORMATS:
        raise HTTPException(status_code=422, detail=f"to_format must be one of {TO_FORMATS}")
    token = _user_tapis_token(user, db)

    # annotation_formats' parsers/builders assume well-formed input for their
    # declared format and raise ValueError/KeyError/TypeError on anything
    # else (e.g. from_format='coco' pointed at a JSON file that isn't
    # actually COCO-shaped) — deliberately not caught inside them, since a
    # step.json field like ${x} substitution has no way to pre-validate file
    # *content*. Caught here instead, once, so a bad-shape source surfaces as
    # a clear 422 naming the actual problem rather than an unhandled 500.
    # HTTPExceptions raised deliberately below (missing inputs, auth, Tapis
    # errors) pass through unchanged — only these three "the data wasn't
    # shaped like we expected" exception types get reinterpreted.
    try:
        return _do_convert(body, token)
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(
            status_code=422,
            detail=f"'{body.from_format}' source doesn't look like a valid {body.from_format} file: {e}",
        )


def _do_convert(body: ConvertRequest, token: str) -> dict:
    # 1. Parse the source into the canonical per_file shape.
    if body.from_format in ("native", "coco"):
        if not body.annotations:
            raise HTTPException(status_code=422, detail=f"from_format='{body.from_format}' needs 'annotations' (a JSON file).")
        data = _json_bytes(_tapis_get_bytes(body.annotations.system, body.annotations.path, token))
        per_file = fmt.PARSERS[body.from_format](data)
        annotation_type = data.get("annotation_type") if body.from_format == "native" else None
    elif body.from_format == "geopackage":
        if not body.annotations_gpkg:
            raise HTTPException(status_code=422, detail="from_format='geopackage' needs 'annotations_gpkg'.")
        with tempfile.TemporaryDirectory() as tmp:
            gpkg_path = os.path.join(tmp, "src.gpkg")
            with open(gpkg_path, "wb") as f:
                f.write(_tapis_get_bytes(body.annotations_gpkg.system, body.annotations_gpkg.path, token))
            per_file = fmt.parse_geopackage(gpkg_path)
        annotation_type = None
    else:  # yolo
        if not body.annotations_dir or not body.images:
            raise HTTPException(status_code=422, detail="from_format='yolo' needs both 'annotations_dir' and 'images'.")
        stem_to_file = _image_stems(body.images.system, body.images.path, token)
        entries = _tapis_list(body.annotations_dir.system, body.annotations_dir.path, token)
        classes_entry = next((e for e in entries if e.get("name") == "classes.txt"), None)
        if not classes_entry:
            raise HTTPException(status_code=422, detail="annotations_dir has no classes.txt.")
        classes = _json_text(_tapis_get_bytes(body.annotations_dir.system, f"{body.annotations_dir.path.rstrip('/')}/classes.txt", token)).splitlines()
        label_texts = {}
        for e in entries:
            name = e.get("name") or ""
            if not name.endswith(".txt") or name == "classes.txt":
                continue
            stem = name[:-4]
            content = _tapis_get_bytes(body.annotations_dir.system, f"{body.annotations_dir.path.rstrip('/')}/{name}", token)
            label_texts[stem] = _json_text(content)
        image_sizes = _image_sizes(body.images.system, body.images.path, set(stem_to_file.values()), token)
        per_file = fmt.parse_yolo(label_texts, classes, stem_to_file, image_sizes)
        annotation_type = None

    detection_count = sum(len(v) for v in per_file.values())

    # 2. Serialize to to_format and upload. native/coco/geopackage write ONE
    # file directly at dest.path, so catch a directory already sitting there
    # (e.g. a reused --output_dir) before uploading rather than after.
    if body.to_format != "yolo":
        _reject_if_directory(body.dest.system, body.dest.path, token)

    if body.to_format in ("native", "coco"):
        image_sizes = {}
        if body.to_format == "coco":
            if not body.images:
                raise HTTPException(status_code=422, detail="to_format='coco' needs 'images' (for width/height).")
            image_sizes = _image_sizes(body.images.system, body.images.path, set(per_file.keys()), token)
        out = fmt.build_native(per_file, annotation_type) if body.to_format == "native" else fmt.build_coco(per_file, image_sizes)
        _tapis_upload(body.dest.system, body.dest.path, _dumps(out), token, "application/json")
        written = [body.dest.path]
    elif body.to_format == "sam3_exemplars":
        # 'images' is OPTIONAL here, unlike coco/yolo: it only sharpens the
        # exemplar KEY (a path relative to the image dir, preserving any
        # subdirectory) — without it the basename is used, which the job also
        # accepts. No image is downloaded, so this stays a cheap conversion
        # even on large datasets.
        out = fmt.build_sam3_exemplars(
            per_file,
            image_dir=body.images.path if body.images else None,
            text_prompts=body.text_prompts,
        )
        if not out["exemplars"] and not out.get("text_prompts"):
            raise HTTPException(
                status_code=422,
                detail="Nothing to write: the source has no boxes and no text prompts were given. "
                       "The zero-shot job needs at least one of the two.",
            )
        _tapis_upload(body.dest.system, body.dest.path, _dumps(out), token, "application/json")
        written = [body.dest.path]
    elif body.to_format == "geopackage":
        with tempfile.TemporaryDirectory() as tmp:
            gpkg_path = os.path.join(tmp, "out.gpkg")
            fmt.build_geopackage(per_file, gpkg_path)
            with open(gpkg_path, "rb") as f:
                _tapis_upload(body.dest.system, body.dest.path, f.read(), token, "application/geopackage+sqlite3")
        written = [body.dest.path]
    else:  # yolo
        if not body.images:
            raise HTTPException(status_code=422, detail="to_format='yolo' needs 'images' (for normalization).")
        image_sizes = _image_sizes(body.images.system, body.images.path, set(per_file.keys()), token)
        label_texts, classes_txt = fmt.build_yolo(per_file, image_sizes)
        dest_dir = body.dest.path.rstrip("/")
        written = []
        for relpath, text in label_texts.items():
            dest_path = f"{dest_dir}/{relpath}"
            _tapis_upload(body.dest.system, dest_path, text.encode("utf-8"), token, "text/plain")
            written.append(dest_path)
        classes_path = f"{dest_dir}/classes.txt"
        _tapis_upload(body.dest.system, classes_path, classes_txt.encode("utf-8"), token, "text/plain")
        written.append(classes_path)

    return {"ok": True, "image_count": len(per_file), "annotation_count": detection_count, "written": written}


def _dumps(obj) -> bytes:
    import json
    return json.dumps(obj, indent=2).encode("utf-8")


def _json_bytes(raw: bytes) -> dict:
    import json
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Source file isn't valid JSON: {e}")


def _json_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")
