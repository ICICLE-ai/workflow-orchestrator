"""
zero_shot_annotation/zero_shot_annotation.py  –  Batch zero-shot annotation.

Annotates every image under an input directory with a zero-shot open-vocabulary
model (SAM3 by default) and writes detections as JSON.  This is the batch
counterpart of the single-image `sam3_service/main.py` endpoint.

Prompt types
------------
text      – one or more free-text concepts applied to every image
            (`--text_prompts "tree" "car"`).
exemplar  – per-image visual exemplar boxes (SAM3 "geometry prompts").  Positive
            exemplars say "find more things like this", negative exemplars say
            "not like this".  Supplied through `--prompt_file`, because the boxes
            are image-specific pixel coordinates.

Both prompt types may be used in the same run; every detection records which
prompt produced it.

Prompt file schema (`--prompt_file`)
------------------------------------
    {
      "text_prompts": ["tree", "car"],

      "exemplars": {
        "default": [
          {"label": "tree", "boxes": [[10, 10, 90, 90]], "box_labels": [1]}
        ],
        "batch_a/img_001.jpg": [
          {"label": "tree",
           "text":  "visual",
           "boxes":      [[10, 10, 90, 90], [220, 40, 300, 130], [400, 400, 460, 460]],
           "box_labels": [1, 1, 0]}
        ]
      }
    }

  - Keys under "exemplars" are the image's path relative to `--image_dir`
    (a bare filename also matches).
  - "default" applies to every image without its own entry.  Only meaningful when
    the imagery is uniform, since the boxes are absolute pixel coordinates.
  - "box_labels" is 1 for a positive exemplar and 0 for a negative one; it
    defaults to all-positive.
  - "text" is the concept name SAM3 pairs with the exemplar (default "visual").
  - "label" is the class name written to the output annotations.

SAHI (tiled inference)
----------------------
`--is_sahi` slices each image into overlapping `--tile_size` tiles with
`--overlap_ratio` overlap, runs the model per tile, maps boxes back to full-image
coordinates and merges them with NMS.  Use it for large imagery holding small
objects.  Without it, each image is run whole.

Exemplar boxes are mapped into tile coordinates and only kept for a tile when at
least `--exemplar_min_visibility` of the box falls inside it.  `--exemplar_tile_mode`
decides what happens to the remaining tiles:
    intersecting – (default) skip tiles that hold no positive exemplar.
    all          – run every tile, falling back to the exemplar's paired text
                   prompt where no exemplar is visible (recorded as prompt_type
                   "text").

Swapping the model
------------------
Backends are looked up in a registry, so adding one means subclassing
`ZeroShotBackend`, implementing `load()` and `predict()`, and decorating it with
`@register_backend("name")`.  `--model` then accepts that name, and `--model_id`
overrides the checkpoint.

Usage
-----
    # text prompts, whole images
    python zero_shot_annotation.py --image_dir ./images --output_dir ./out \
        --text_prompts "tree" "car"

    # text prompts, tiled inference over large imagery
    python zero_shot_annotation.py --image_dir ./images --output_dir ./out \
        --text_prompts "tree" --is_sahi --tile_size 960 --overlap_ratio 0.2 \
        --batch_size 8

    # exemplar prompts (and any text prompts) from a prompt file
    python zero_shot_annotation.py --image_dir ./images --output_dir ./out \
        --prompt_file prompts.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import torch
from PIL import Image
from torchvision.ops import batched_nms
from tqdm import tqdm

logger = logging.getLogger(__name__)

try:
    import cv2
except ImportError:  # segmentation polygons are optional
    cv2 = None

# Drone / orthomosaic imagery routinely exceeds Pillow's decompression-bomb guard.
Image.MAX_IMAGE_PIXELS = None

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"})

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Concept name paired with an exemplar when the prompt file does not give one.
DEFAULT_EXEMPLAR_TEXT = "visual"

# Abort a run once this many batches fail back to back. A persistent failure
# (bad CUDA build, missing kernels) hits every batch identically, and grinding
# through thousands of tiles to write an empty annotation file helps nobody.
MAX_CONSECUTIVE_BATCH_FAILURES = 3


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Prompt:
    """One prompt applied to one image (or one tile of it)."""
    label: str                                              # class written to output
    text: str                                               # concept given to the model
    kind: str = "text"                                      # "text" | "exemplar"
    boxes: list[list[float]] = field(default_factory=list)  # xyxy, image coords
    box_labels: list[int] = field(default_factory=list)     # 1 positive, 0 negative

    def has_exemplars(self) -> bool:
        return self.kind == "exemplar" and len(self.boxes) > 0


@dataclass
class Detection:
    """One predicted instance."""
    x_min: int
    y_min: int
    x_max: int
    y_max: int
    score: float
    label: str
    prompt_type: str
    segmentation: list[list[int]] = field(default_factory=list)


@dataclass
class TileJob:
    """A single (image, tile, prompt) unit of work."""
    image_idx: int
    tile: tuple[int, int, int, int]      # x1, y1, x2, y2 in image coords
    prompt: Prompt                       # exemplar boxes already in tile coords


# ──────────────────────────────────────────────────────────────────────────────
# Image discovery  (mirrors smart-labeller/image_discovery.py)
# ──────────────────────────────────────────────────────────────────────────────

def discover_images(root: Path) -> list[tuple[Path, str]]:
    """Recursively find images under ``root`` as ``(absolute_path, relative_key)``."""
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    found: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            found.append((path, path.relative_to(root).as_posix()))
    return found


# ──────────────────────────────────────────────────────────────────────────────
# Tiling / geometry helpers
# ──────────────────────────────────────────────────────────────────────────────

def build_overlapping_tiles(
    img_w: int,
    img_h: int,
    tile_size: int,
    overlap_ratio: float,
) -> list[tuple[int, int, int, int]]:
    """Generate deduplicated, edge-snapped (x1, y1, x2, y2) tiles."""
    tile_size = max(1, int(tile_size))
    stride = max(1, int(tile_size * (1 - overlap_ratio)))

    cols = max(1, math.ceil((img_w - tile_size) / stride) + 1)
    rows = max(1, math.ceil((img_h - tile_size) / stride) + 1)

    tiles: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for r in range(rows):
        for c in range(cols):
            x1, y1 = c * stride, r * stride
            x2, y2 = min(x1 + tile_size, img_w), min(y1 + tile_size, img_h)

            # Snap the last row/column back so tiles keep a consistent size.
            if x2 == img_w:
                x1 = max(0, img_w - tile_size)
            if y2 == img_h:
                y1 = max(0, img_h - tile_size)

            box = (int(x1), int(y1), int(x2), int(y2))
            if box not in seen:
                seen.add(box)
                tiles.append(box)
    return tiles


def clip_boxes_to_tile(
    boxes: list[list[float]],
    box_labels: list[int],
    tile: tuple[int, int, int, int],
    min_visibility: float,
) -> tuple[list[list[float]], list[int]]:
    """
    Map image-space exemplar boxes into tile-local coordinates.

    A box is kept when at least ``min_visibility`` of its area falls inside the
    tile; the kept box is clipped to the tile bounds.
    """
    tx1, ty1, tx2, ty2 = tile
    kept_boxes: list[list[float]] = []
    kept_labels: list[int] = []

    for box, label in zip(boxes, box_labels):
        bx1, by1, bx2, by2 = box
        area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        if area <= 0:
            continue

        ix1, iy1 = max(bx1, tx1), max(by1, ty1)
        ix2, iy2 = min(bx2, tx2), min(by2, ty2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        if inter / area < min_visibility:
            continue

        kept_boxes.append([ix1 - tx1, iy1 - ty1, ix2 - tx1, iy2 - ty1])
        kept_labels.append(int(label))

    return kept_boxes, kept_labels


def resolve_device(choice: str) -> str:
    """Turn ``--device`` into a concrete device string."""
    if choice == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if choice == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("--device cuda requested but no CUDA device is visible")
    return choice


def preflight_device_check(device: str) -> None:
    """
    Verify the device can actually execute kernels, before spending minutes
    loading weights.

    A torch build without kernels for the card's compute capability loads and
    reports ``cuda.is_available() == True``, then fails on the first real op with
    "unable to find an engine to execute this computation".  A tiny matmul
    surfaces that immediately, with an error that names the fix.
    """
    if device != "cuda":
        logger.info(f"Running on {device} | torch {torch.__version__}")
        return

    name = torch.cuda.get_device_name(0)
    major, minor = torch.cuda.get_device_capability(0)
    arch_list = torch.cuda.get_arch_list()
    logger.info(
        f"GPU: {name} (compute capability {major}.{minor}) | "
        f"torch {torch.__version__} built for [{', '.join(arch_list)}]"
    )

    if f"sm_{major}{minor}" not in arch_list:
        logger.warning(
            f"This torch build ships no sm_{major}{minor} kernels for {name} — "
            f"expect failures unless it falls back cleanly."
        )

    try:
        probe = torch.randn(8, 8, device=device)
        (probe @ probe).sum().item()
        del probe
    except Exception as exc:
        raise RuntimeError(
            f"{name} (compute capability {major}.{minor}) cannot execute kernels with "
            f"torch {torch.__version__}, which was built for [{', '.join(arch_list)}]. "
            f"Install a CUDA build targeting sm_{major}{minor} — for this card: "
            f"pip install torch==2.13.0 torchvision==0.28.0 "
            f"--index-url https://download.pytorch.org/whl/cu126 — "
            f"or rerun with --device cpu. Underlying error: {exc}"
        ) from exc


def mask_to_boundary_points(mask: np.ndarray, max_points: int = 256) -> list[list[int]]:
    """Extract the largest contour of a binary mask as ordered [x, y] points."""
    if cv2 is None or mask is None or not np.any(mask):
        return []

    contours, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return []

    contour = max(contours, key=cv2.contourArea)
    points = contour.squeeze(axis=1)                     # (N,1,2) → (N,2)
    if points.ndim != 2:
        return []
    if len(points) > max_points:
        idx = np.round(np.linspace(0, len(points) - 1, max_points)).astype(int)
        points = points[idx]
    return [[int(p[0]), int(p[1])] for p in points]


# ──────────────────────────────────────────────────────────────────────────────
# Backend registry
# ──────────────────────────────────────────────────────────────────────────────

BACKEND_REGISTRY: dict[str, type["ZeroShotBackend"]] = {}


def register_backend(name: str):
    """Class decorator registering a backend under ``name`` for ``--model``."""
    def decorator(cls: type["ZeroShotBackend"]) -> type["ZeroShotBackend"]:
        cls.name = name
        BACKEND_REGISTRY[name] = cls
        return cls
    return decorator


def get_backend(name: str, **kwargs) -> "ZeroShotBackend":
    if name not in BACKEND_REGISTRY:
        raise ValueError(
            f"Unknown model '{name}'. Available: {', '.join(sorted(BACKEND_REGISTRY))}"
        )
    return BACKEND_REGISTRY[name](**kwargs)


class ZeroShotBackend(ABC):
    """
    Interface every zero-shot annotation model must implement.

    Subclasses declare which prompt types they handle through ``supports_text`` /
    ``supports_exemplars`` and return, per input image, a list of tile-local
    detections.
    """

    name: str = "base"
    default_model_id: str = ""
    supports_text: bool = True
    supports_exemplars: bool = False

    def __init__(self, model_id: Optional[str] = None, device: str = DEVICE):
        self.model_id = model_id or self.default_model_id
        self.device = device

    @abstractmethod
    def load(self) -> None:
        """Load weights onto ``self.device``. Called once before inference."""

    @abstractmethod
    def predict(
        self,
        images: list[Image.Image],
        prompts: list[Prompt],
        threshold: float,
        mask_threshold: float,
        want_segmentation: bool,
    ) -> list[list[Detection]]:
        """
        Run one batch.  ``images[i]`` is scored against ``prompts[i]``; exemplar
        boxes arrive already in that image's (tile-local) coordinates.

        Returns one detection list per input image, in tile-local coordinates.
        """


@register_backend("sam3")
class Sam3Backend(ZeroShotBackend):
    """SAM3 concept segmentation — text prompts and visual exemplar boxes."""

    default_model_id = "facebook/sam3"
    supports_text = True
    supports_exemplars = True

    def __init__(self, model_id: Optional[str] = None, device: str = DEVICE):
        super().__init__(model_id=model_id, device=device)
        self.model = None
        self.processor = None

    def load(self) -> None:
        from transformers import Sam3Model, Sam3Processor

        logger.info(f"Loading {self.model_id} on {self.device}...")
        self.processor = Sam3Processor.from_pretrained(self.model_id)
        self.model = Sam3Model.from_pretrained(self.model_id).to(self.device).eval()
        logger.info("Model loaded")

    @torch.no_grad()
    def predict(
        self,
        images: list[Image.Image],
        prompts: list[Prompt],
        threshold: float,
        mask_threshold: float,
        want_segmentation: bool,
    ) -> list[list[Detection]]:
        texts = [p.text for p in prompts]

        processor_kwargs = {}
        if any(p.has_exemplars() for p in prompts):
            # Pixel-space xyxy boxes; the processor normalises them and converts
            # to the cxcywh form the geometry encoder expects.
            processor_kwargs["input_boxes"] = [p.boxes for p in prompts]
            processor_kwargs["input_boxes_labels"] = [p.box_labels for p in prompts]

        inputs = self.processor(
            images=images,
            text=texts,
            return_tensors="pt",
            **processor_kwargs,
        ).to(self.device)

        outputs = self.model(**inputs)

        target_sizes = inputs.get("original_sizes").tolist()
        batch_results = self.processor.post_process_instance_segmentation(
            outputs,
            threshold=threshold,
            mask_threshold=mask_threshold,
            target_sizes=target_sizes,
        )

        batch_detections: list[list[Detection]] = []
        for result, prompt in zip(batch_results, prompts):
            detections: list[Detection] = []
            boxes = result.get("boxes")
            scores = result.get("scores")
            masks = result.get("masks")

            for i in range(0 if boxes is None else len(boxes)):
                x_min, y_min, x_max, y_max = boxes[i].tolist()

                seg_points: list[list[int]] = []
                if want_segmentation and masks is not None and len(masks) > i:
                    mask = masks[i]
                    mask_np = mask.cpu().numpy() if hasattr(mask, "cpu") else np.asarray(mask)
                    seg_points = mask_to_boundary_points(mask_np > 0)

                detections.append(Detection(
                    x_min=int(x_min), y_min=int(y_min),
                    x_max=int(x_max), y_max=int(y_max),
                    score=float(scores[i].item()),
                    label=prompt.label,
                    prompt_type=prompt.kind,
                    segmentation=seg_points,
                ))
            batch_detections.append(detections)

        del inputs, outputs, batch_results
        if self.device == "cuda":
            torch.cuda.empty_cache()

        return batch_detections


# ──────────────────────────────────────────────────────────────────────────────
# Prompt loading
# ──────────────────────────────────────────────────────────────────────────────

def load_prompt_file(path: Path) -> tuple[list[str], dict[str, list[Prompt]]]:
    """
    Parse a prompt file into ``(text_prompts, exemplars_by_image_key)``.

    Exemplar boxes stay in full-image pixel coordinates here; they are mapped to
    tiles later.
    """
    with open(path) as f:
        data = json.load(f)

    text_prompts = [str(t).strip() for t in data.get("text_prompts", []) if str(t).strip()]

    exemplars: dict[str, list[Prompt]] = {}
    for image_key, entries in (data.get("exemplars") or {}).items():
        prompts: list[Prompt] = []
        for entry in entries:
            boxes = [[float(v) for v in box] for box in entry.get("boxes", [])]
            if not boxes:
                continue
            if any(len(box) != 4 for box in boxes):
                raise ValueError(f"Exemplar boxes for '{image_key}' must be [x1, y1, x2, y2]")

            box_labels = [int(v) for v in entry.get("box_labels", [])] or [1] * len(boxes)
            if len(box_labels) != len(boxes):
                raise ValueError(
                    f"Exemplar '{image_key}': {len(boxes)} boxes but {len(box_labels)} box_labels"
                )

            label = str(entry.get("label") or entry.get("text") or DEFAULT_EXEMPLAR_TEXT)
            prompts.append(Prompt(
                label=label,
                text=str(entry.get("text") or DEFAULT_EXEMPLAR_TEXT),
                kind="exemplar",
                boxes=boxes,
                box_labels=box_labels,
            ))
        if prompts:
            exemplars[image_key] = prompts

    return text_prompts, exemplars


def resolve_exemplars(rel_key: str, exemplars: dict[str, list[Prompt]]) -> list[Prompt]:
    """Look up exemplars by relative key, then bare filename, then 'default'."""
    if rel_key in exemplars:
        return exemplars[rel_key]

    basename = Path(rel_key).name
    if basename in exemplars:
        return exemplars[basename]

    return exemplars.get("default", [])


# ──────────────────────────────────────────────────────────────────────────────
# Job planning
# ──────────────────────────────────────────────────────────────────────────────

def build_jobs(
    image_paths: list[Path],
    rel_keys: list[str],
    text_prompts: list[str],
    exemplars: dict[str, list[Prompt]],
    is_sahi: bool,
    tile_size: int,
    overlap_ratio: float,
    exemplar_tile_mode: str,
    exemplar_min_visibility: float,
) -> list[TileJob]:
    """Expand (image × prompt × tile) into a flat, image-ordered job list."""
    jobs: list[TileJob] = []
    skipped_tiles = 0

    for image_idx, (path, rel_key) in enumerate(zip(image_paths, rel_keys)):
        with Image.open(path) as img:
            width, height = img.size

        if is_sahi:
            tiles = build_overlapping_tiles(width, height, tile_size, overlap_ratio)
        else:
            tiles = [(0, 0, width, height)]

        for text in text_prompts:
            for tile in tiles:
                jobs.append(TileJob(
                    image_idx=image_idx,
                    tile=tile,
                    prompt=Prompt(label=text, text=text, kind="text"),
                ))

        for exemplar in resolve_exemplars(rel_key, exemplars):
            for tile in tiles:
                boxes, box_labels = clip_boxes_to_tile(
                    exemplar.boxes, exemplar.box_labels, tile, exemplar_min_visibility
                )
                has_positive = any(label == 1 for label in box_labels)

                if has_positive:
                    jobs.append(TileJob(
                        image_idx=image_idx,
                        tile=tile,
                        prompt=Prompt(
                            label=exemplar.label,
                            text=exemplar.text,
                            kind="exemplar",
                            boxes=boxes,
                            box_labels=box_labels,
                        ),
                    ))
                elif exemplar_tile_mode == "all":
                    # No exemplar visible in this tile — fall back to its text.
                    jobs.append(TileJob(
                        image_idx=image_idx,
                        tile=tile,
                        prompt=Prompt(
                            label=exemplar.label,
                            text=exemplar.text,
                            kind="text",
                        ),
                    ))
                else:
                    skipped_tiles += 1

    if skipped_tiles:
        logger.info(
            f"Skipped {skipped_tiles} tiles holding no positive exemplar "
            f"(--exemplar_tile_mode intersecting). Use 'all' to run them with the "
            f"exemplar's paired text prompt."
        )
    return jobs


def batched(jobs: list[TileJob], batch_size: int) -> Iterator[list[TileJob]]:
    for i in range(0, len(jobs), batch_size):
        yield jobs[i: i + batch_size]


def split_by_prompt_kind(jobs: list[TileJob]) -> tuple[list[TileJob], list[TileJob]]:
    """
    Separate text jobs from exemplar jobs so batches stay homogeneous — exemplar
    padding never leaks into text-only inference, and text jobs are never charged
    for a geometry-encoder pass.
    """
    exemplar_jobs = [j for j in jobs if j.prompt.has_exemplars()]
    text_jobs = [j for j in jobs if not j.prompt.has_exemplars()]
    return text_jobs, exemplar_jobs


# ──────────────────────────────────────────────────────────────────────────────
# Inference driver
# ──────────────────────────────────────────────────────────────────────────────

class _ImageCache:
    """Keeps the most recently decoded image, since jobs are image-ordered."""

    def __init__(self, image_paths: list[Path]):
        self.image_paths = image_paths
        self._idx: Optional[int] = None
        self._image: Optional[Image.Image] = None

    def crop(self, image_idx: int, tile: tuple[int, int, int, int]) -> Image.Image:
        if self._idx != image_idx:
            self.close()
            self._image = Image.open(self.image_paths[image_idx]).convert("RGB")
            self._idx = image_idx
        return self._image.crop(tile)

    def close(self) -> None:
        if self._image is not None:
            self._image.close()
        self._image, self._idx = None, None


def run_jobs(
    backend: ZeroShotBackend,
    jobs: list[TileJob],
    image_paths: list[Path],
    batch_size: int,
    threshold: float,
    mask_threshold: float,
    want_segmentation: bool,
    desc: str,
) -> tuple[dict[int, list[Detection]], int]:
    """
    Run every job and accumulate detections per image in full-image coords.

    Returns ``(detections_by_image_index, failed_batch_count)``.
    """
    results: dict[int, list[Detection]] = {}
    cache = _ImageCache(image_paths)
    failed_batches = 0
    consecutive_failures = 0

    try:
        for batch in tqdm(list(batched(jobs, batch_size)), desc=desc):
            crops = [cache.crop(job.image_idx, job.tile) for job in batch]
            prompts = [job.prompt for job in batch]

            try:
                batch_detections = backend.predict(
                    crops, prompts, threshold, mask_threshold, want_segmentation
                )
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                logger.warning(
                    f"CUDA OOM on a batch of {len(batch)} — retrying one tile at a time"
                )
                batch_detections = []
                for crop, prompt in zip(crops, prompts):
                    try:
                        batch_detections.extend(backend.predict(
                            [crop], [prompt], threshold, mask_threshold, want_segmentation
                        ))
                    except Exception as exc:
                        logger.error(f"Tile failed after OOM retry: {exc}")
                        batch_detections.append([])
                        torch.cuda.empty_cache()
            except Exception as exc:
                failed_batches += 1
                consecutive_failures += 1
                logger.error(f"Batch failed ({exc}); skipping {len(batch)} tiles")
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

                if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                    raise RuntimeError(
                        f"Aborting after {consecutive_failures} consecutive batch "
                        f"failures — this looks like an environment problem, not bad "
                        f"input. Last error: {exc}"
                    ) from exc
                continue

            consecutive_failures = 0

            for job, detections in zip(batch, batch_detections):
                x_off, y_off = job.tile[0], job.tile[1]
                bucket = results.setdefault(job.image_idx, [])
                for det in detections:
                    bucket.append(Detection(
                        x_min=det.x_min + x_off,
                        y_min=det.y_min + y_off,
                        x_max=det.x_max + x_off,
                        y_max=det.y_max + y_off,
                        score=det.score,
                        label=det.label,
                        prompt_type=det.prompt_type,
                        segmentation=[[px + x_off, py + y_off] for px, py in det.segmentation],
                    ))
    finally:
        cache.close()

    if failed_batches:
        logger.warning(f"{failed_batches} batches failed and were skipped")
    return results, failed_batches


def merge_detections(
    detections: list[Detection],
    iou_threshold: float,
    class_agnostic: bool,
) -> list[Detection]:
    """NMS across tiles and prompts. Class-wise unless ``class_agnostic``."""
    if not detections:
        return []

    boxes = torch.tensor(
        [[d.x_min, d.y_min, d.x_max, d.y_max] for d in detections], dtype=torch.float32
    )
    scores = torch.tensor([d.score for d in detections], dtype=torch.float32)

    if class_agnostic:
        idxs = torch.zeros(len(detections), dtype=torch.int64)
    else:
        label_ids = {label: i for i, label in enumerate(sorted({d.label for d in detections}))}
        idxs = torch.tensor([label_ids[d.label] for d in detections], dtype=torch.int64)

    keep = batched_nms(boxes, scores, idxs, iou_threshold)
    return [detections[i] for i in keep.tolist()]


# ──────────────────────────────────────────────────────────────────────────────
# Output writers
# ──────────────────────────────────────────────────────────────────────────────

def write_flat_json(
    path: Path,
    detections_by_key: dict[str, list[Detection]],
    metadata: dict,
    want_segmentation: bool,
) -> int:
    """Write the repo's flat format (readable by evaluate_annotations.py)."""
    annotations = []
    for rel_key, detections in detections_by_key.items():
        for det in detections:
            record = {
                "image_path": rel_key,
                "bounding_box": [det.x_min, det.y_min, det.x_max, det.y_max],
                "score": round(det.score, 4),
                "class": det.label,
                "prompt_type": det.prompt_type,
            }
            if want_segmentation:
                record["segmentation"] = det.segmentation
            annotations.append(record)

    with open(path, "w") as f:
        json.dump({**metadata, "annotations": annotations}, f, indent=2)
    return len(annotations)


def write_coco_json(
    path: Path,
    detections_by_key: dict[str, list[Detection]],
    image_sizes: dict[str, tuple[int, int]],
    metadata: dict,
    want_segmentation: bool,
) -> int:
    """Write COCO-style detections for annotation tools that expect them."""
    categories: dict[str, int] = {}
    for detections in detections_by_key.values():
        for det in detections:
            categories.setdefault(det.label, len(categories) + 1)

    images, annotations = [], []
    for image_id, (rel_key, detections) in enumerate(detections_by_key.items(), start=1):
        width, height = image_sizes.get(rel_key, (0, 0))
        images.append({"id": image_id, "file_name": rel_key, "width": width, "height": height})

        for det in detections:
            w = det.x_max - det.x_min
            h = det.y_max - det.y_min
            record = {
                "id": len(annotations) + 1,
                "image_id": image_id,
                "category_id": categories[det.label],
                "bbox": [det.x_min, det.y_min, w, h],
                "area": int(w * h),
                "score": round(det.score, 4),
                "iscrowd": 0,
                "prompt_type": det.prompt_type,
            }
            if want_segmentation and det.segmentation:
                record["segmentation"] = [
                    [coord for point in det.segmentation for coord in point]
                ]
            annotations.append(record)

    with open(path, "w") as f:
        json.dump({
            "info": metadata,
            "images": images,
            "annotations": annotations,
            "categories": [{"id": i, "name": n} for n, i in categories.items()],
        }, f, indent=2)
    return len(annotations)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch zero-shot annotation with text and exemplar prompts",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # I/O
    parser.add_argument("--image_dir", type=str, required=True,
                        help="Directory of images to annotate (searched recursively)")
    parser.add_argument("--output_dir", type=str, default="output",
                        help="Directory for annotation JSON files")
    parser.add_argument("--max_images", type=int, default=None,
                        help="Annotate at most this many images (smoke tests)")

    # Model
    parser.add_argument("--model", type=str, default="sam3",
                        choices=sorted(BACKEND_REGISTRY),
                        help="Zero-shot backend")
    parser.add_argument("--model_id", type=str, default=None,
                        help="HuggingFace checkpoint override for the backend")
    parser.add_argument("--device", type=str, default="auto",
                        choices=["auto", "cuda", "cpu"],
                        help="Compute device; 'auto' uses CUDA when available")

    # Prompts
    parser.add_argument("--text_prompts", type=str, nargs="+", default=None,
                        help="Text concepts applied to every image")
    parser.add_argument("--prompt_file", type=str, default=None,
                        help="JSON file with text_prompts and/or per-image exemplars")

    # SAHI
    parser.add_argument("--is_sahi", action="store_true", default=False,
                        help="Slice each image into overlapping tiles before inference")
    parser.add_argument("--tile_size", type=int, default=960,
                        help="Tile size in pixels (with --is_sahi)")
    parser.add_argument("--overlap_ratio", type=float, default=0.2,
                        help="Fractional overlap between tiles (with --is_sahi)")
    parser.add_argument("--exemplar_tile_mode", type=str, default="intersecting",
                        choices=["intersecting", "all"],
                        help="Which tiles to run for exemplar prompts under --is_sahi")
    parser.add_argument("--exemplar_min_visibility", type=float, default=0.5,
                        help="Fraction of an exemplar box that must fall inside a tile")

    # Inference
    parser.add_argument("--batch_size", type=int, default=8,
                        help="Tiles per inference batch")
    parser.add_argument("--confidence", type=float, default=0.3,
                        help="Score threshold for keeping an instance")
    parser.add_argument("--mask_threshold", type=float, default=0.5,
                        help="Threshold for binarising predicted masks")
    parser.add_argument("--nms_iou", type=float, default=0.5,
                        help="IoU threshold for merging detections across tiles")
    parser.add_argument("--class_agnostic_nms", action="store_true", default=False,
                        help="Let detections from different prompts suppress each other")
    parser.add_argument("--no_segmentation", action="store_true", default=False,
                        help="Skip mask-to-polygon extraction (boxes only, faster)")

    # Output
    parser.add_argument("--output_format", type=str, default="flat",
                        choices=["flat", "coco", "both"],
                        help="Annotation file format(s) to write")
    parser.add_argument("--log_level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity")

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not 0.0 <= args.overlap_ratio < 1.0:
        logger.error("--overlap_ratio must be in [0, 1)")
        return 1
    if args.is_sahi and args.tile_size <= 0:
        logger.error("--tile_size must be > 0")
        return 1

    # ── Prompts ──────────────────────────────────────────────────────────────
    text_prompts = [t.strip() for t in (args.text_prompts or []) if t.strip()]
    exemplars: dict[str, list[Prompt]] = {}

    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_file():
            logger.error(f"Prompt file not found: {prompt_path}")
            return 1
        file_texts, exemplars = load_prompt_file(prompt_path)
        # CLI text prompts win; otherwise fall back to the file's.
        text_prompts = text_prompts or file_texts

    if not text_prompts and not exemplars:
        logger.error(
            "No prompts given. Pass --text_prompts and/or a --prompt_file with exemplars."
        )
        return 1

    # ── Images ───────────────────────────────────────────────────────────────
    image_dir = Path(args.image_dir)
    try:
        discovered = discover_images(image_dir)
    except NotADirectoryError as exc:
        logger.error(str(exc))
        return 1

    if args.max_images is not None:
        discovered = discovered[: args.max_images]

    if not discovered:
        logger.error(f"No images found under {image_dir.resolve()}")
        return 1

    image_paths = [p for p, _ in discovered]
    rel_keys = [k for _, k in discovered]
    logger.info(f"Found {len(image_paths)} images under {image_dir.resolve()} (recursive)")

    # ── Backend ──────────────────────────────────────────────────────────────
    try:
        device = resolve_device(args.device)
        preflight_device_check(device)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    backend = get_backend(args.model, model_id=args.model_id, device=device)

    if exemplars and not backend.supports_exemplars:
        logger.error(f"Backend '{args.model}' does not support exemplar prompts")
        return 1
    if text_prompts and not backend.supports_text:
        logger.error(f"Backend '{args.model}' does not support text prompts")
        return 1

    want_segmentation = not args.no_segmentation
    if want_segmentation and cv2 is None:
        logger.warning("opencv-python not installed — writing boxes without polygons")
        want_segmentation = False

    # ── Plan ─────────────────────────────────────────────────────────────────
    jobs = build_jobs(
        image_paths=image_paths,
        rel_keys=rel_keys,
        text_prompts=text_prompts,
        exemplars=exemplars,
        is_sahi=args.is_sahi,
        tile_size=args.tile_size,
        overlap_ratio=args.overlap_ratio,
        exemplar_tile_mode=args.exemplar_tile_mode,
        exemplar_min_visibility=args.exemplar_min_visibility,
    )
    if not jobs:
        logger.error("Nothing to do — no (image, prompt, tile) combinations were produced")
        return 1

    text_jobs, exemplar_jobs = split_by_prompt_kind(jobs)
    logger.info(
        f"Planned {len(jobs)} inferences "
        f"({len(text_jobs)} text, {len(exemplar_jobs)} exemplar) | "
        f"SAHI={args.is_sahi} "
        f"tile_size={args.tile_size if args.is_sahi else '-'} "
        f"overlap={args.overlap_ratio if args.is_sahi else '-'} "
        f"batch_size={args.batch_size} device={device}"
    )

    # ── Run ──────────────────────────────────────────────────────────────────
    start = time.time()
    backend.load()

    detections_by_idx: dict[int, list[Detection]] = {}
    total_failed_batches = 0
    for job_group, desc in ((text_jobs, "Text prompts"), (exemplar_jobs, "Exemplar prompts")):
        if not job_group:
            continue
        try:
            group_results, failed = run_jobs(
                backend=backend,
                jobs=job_group,
                image_paths=image_paths,
                batch_size=args.batch_size,
                threshold=args.confidence,
                mask_threshold=args.mask_threshold,
                want_segmentation=want_segmentation,
                desc=desc,
            )
        except RuntimeError as exc:
            logger.error(str(exc))
            return 1

        total_failed_batches += failed
        for image_idx, detections in group_results.items():
            detections_by_idx.setdefault(image_idx, []).extend(detections)

    # ── Merge + write ────────────────────────────────────────────────────────
    detections_by_key: dict[str, list[Detection]] = {}
    raw_total = 0
    for image_idx, detections in sorted(detections_by_idx.items()):
        raw_total += len(detections)
        merged = merge_detections(detections, args.nms_iou, args.class_agnostic_nms)
        if merged:
            detections_by_key[rel_keys[image_idx]] = merged
            logger.debug(
                f"{rel_keys[image_idx]}: {len(detections)} raw → {len(merged)} after NMS"
            )

    elapsed = time.time() - start
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    output_dir = Path(args.output_dir) / "annotations"
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "model": args.model,
        "model_id": backend.model_id,
        "device": device,
        "generated_at": timestamp,
        "image_dir": str(image_dir.resolve()),
        "images": len(image_paths),
        "text_prompts": text_prompts,
        "exemplar_images": len(exemplars),
        "params": {
            "is_sahi": args.is_sahi,
            "tile_size": args.tile_size if args.is_sahi else None,
            "overlap_ratio": args.overlap_ratio if args.is_sahi else None,
            "batch_size": args.batch_size,
            "confidence": args.confidence,
            "mask_threshold": args.mask_threshold,
            "nms_iou": args.nms_iou,
            "class_agnostic_nms": args.class_agnostic_nms,
            "exemplar_tile_mode": args.exemplar_tile_mode,
            "exemplar_min_visibility": args.exemplar_min_visibility,
        },
        "runtime_seconds": round(elapsed, 1),
    }

    if args.output_format in ("flat", "both"):
        flat_path = output_dir / f"annotations_{args.model}_{timestamp}.json"
        count = write_flat_json(flat_path, detections_by_key, metadata, want_segmentation)
        logger.info(f"Saved {count} annotations → {flat_path}")

    if args.output_format in ("coco", "both"):
        image_sizes: dict[str, tuple[int, int]] = {}
        for image_idx, rel_key in enumerate(rel_keys):
            if rel_key in detections_by_key:
                with Image.open(image_paths[image_idx]) as img:
                    image_sizes[rel_key] = img.size
        coco_path = output_dir / f"annotations_coco_{args.model}_{timestamp}.json"
        count = write_coco_json(
            coco_path, detections_by_key, image_sizes, metadata, want_segmentation
        )
        logger.info(f"Saved {count} COCO annotations → {coco_path}")

    kept = sum(len(d) for d in detections_by_key.values())
    logger.info(
        f"Done in {elapsed:.1f}s | {raw_total} raw → {kept} after NMS across "
        f"{len(detections_by_key)}/{len(image_paths)} images with detections"
    )

    if total_failed_batches:
        logger.error(
            f"{total_failed_batches} batches failed during this run — the annotations "
            f"above are incomplete."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
