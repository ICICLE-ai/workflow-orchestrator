"""
zero_shot_annotation/zero_shot_annotation.py  –  Batch zero-shot annotation.

Annotates every image under an input directory with a zero-shot open-vocabulary
model (SAM3 by default) and writes detections as JSON.  This is the batch
counterpart of the single-image `sam3_service/main.py` endpoint.

Prompt types
------------
text      – one or more free-text concepts applied to every image
            (`--text_prompts "tree" "car"`).  A prompt written `prompt=label`
            is sent to the model as `prompt` and its detections are labelled
            `label` (`--text_prompts dog="german shepherd"`); without `=`, the
            prompt is its own label.  Several prompts may share one label.
exemplar  – per-image visual exemplar boxes (SAM3 "geometry prompts").  Positive
            exemplars say "find more things like this", negative exemplars say
            "not like this".  Supplied through `--prompt_file`, because the boxes
            are image-specific pixel coordinates.
reference – in-context segmentation (`--model insid3`).  The same prompt-file
            exemplars are read as REFERENCES instead: each annotated object
            (polygon, or box turned into a mask by SAM3) teaches the model what
            a class looks like, and every image in the directory is segmented
            for it — not only the image the exemplar was drawn on.

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
  - "polygons" (optional) gives each box's object outline as [[x, y], ...],
    aligned with "boxes" — `null` where a box has no outline.  SAM3 ignores it;
    INSID3 uses it as the reference mask.  An entry may give "polygons" alone,
    in which case its boxes are the polygons' bounding boxes.

In-context segmentation (`--model insid3`)
------------------------------------------
INSID3 (training-free in-context segmentation on a frozen DINOv3) takes a
reference image + object mask and segments the same concept in a target image.
Exemplar entries become references, grouped by label:
  - Outlines come from "polygons"; a box with no outline is turned into a mask
    by the SAM3 tracker (`--reference_box_masks sam3`, the default) or used as
    a filled rectangle (`box`).
  - Negative boxes and the "default" key are ignored — a reference must be a
    real object in a real image.
  - Reference images must live under `--image_dir`; they are segmented too, so
    instances left unlabelled there are still found.
INSID3 returns one mask per class, which is split into connected components —
one annotation each.  Touching objects therefore come out as one annotation.
Each component's score is its mean similarity to the reference prototype.
`--confidence` and `--mask_threshold` do not apply; INSID3 makes its own
foreground decision.
The DINOv3 backbone is downloaded from the Hugging Face Hub
(facebook/dinov3-<backbone>-pretrain-lvd1689m, gated: HF_TOKEN must belong to
an account that accepted the license there), unless `--insid3_weights` points
at an original .pth checkpoint.

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

    # in-context segmentation: exemplars on a few images, segments on the rest
    # (backbone from the Hugging Face Hub; or add --insid3_weights <.pth>)
    HF_TOKEN=hf_... python zero_shot_annotation.py --image_dir ./images \
        --output_dir ./out --prompt_file prompts.json --model insid3
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import torch
import torch.nn.functional as F
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

# Checkpoint whose tracker head turns box-only references into masks for INSID3.
SAM3_BOX_MODEL_ID = "facebook/sam3"

# Reference shots per class INSID3 is given.  Every shot adds a full
# (patches x patches) similarity map per target, so memory grows linearly.
MAX_REFERENCE_SHOTS = 16


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Prompt:
    """One prompt applied to one image (or one tile of it)."""
    label: str                                              # class written to output
    text: str                                               # concept given to the model
    kind: str = "text"                                      # "text" | "exemplar" | "reference"
    boxes: list[list[float]] = field(default_factory=list)  # xyxy, image coords
    box_labels: list[int] = field(default_factory=list)     # 1 positive, 0 negative
    # Outline per box ([[x, y], ...]) or None; only read for INSID3 references.
    polygons: list[Optional[list[list[float]]]] = field(default_factory=list)

    def has_exemplars(self) -> bool:
        return self.kind == "exemplar" and len(self.boxes) > 0


@dataclass
class ReferenceShot:
    """One reference image (or crop of it) and the object mask for one class."""
    label: str
    image: Image.Image
    mask: np.ndarray                     # (H, W) bool, same size as image


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
    # Reference backends read prompt-file exemplars as cross-image references,
    # are handed them once through prepare_references(), then receive
    # kind="reference" prompts naming only the label to segment.
    supports_references: bool = False

    def __init__(self, model_id: Optional[str] = None, device: str = DEVICE):
        self.model_id = model_id or self.default_model_id
        self.device = device

    @abstractmethod
    def load(self) -> None:
        """Load weights onto ``self.device``. Called once before inference."""

    def prepare_references(self, shots_by_label: dict[str, list[ReferenceShot]]) -> None:
        """Encode reference shots once, before any reference prompt is run."""
        raise NotImplementedError(f"Backend '{self.name}' does not support references")

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


class HFDinov3Encoder(torch.nn.Module):
    """
    Wraps a ``transformers`` DINOv3ViTModel in the one call INSID3 makes on its
    encoder: ``get_intermediate_layers(x, n=1, reshape=True)[0]``, the final
    layer-normed patch tokens as a (B, C, h, w) map.
    """

    def __init__(self, model: torch.nn.Module):
        super().__init__()
        self.model = model
        self.patch_size = model.config.patch_size
        self.n_prefix_tokens = 1 + model.config.num_register_tokens

    def get_intermediate_layers(self, x: torch.Tensor, n: int = 1, reshape: bool = False, **_):
        if n != 1:
            raise NotImplementedError("Only the last layer is exposed")
        # last_hidden_state is already passed through the final norm.
        tokens = self.model(pixel_values=x).last_hidden_state[:, self.n_prefix_tokens:]
        if reshape:
            B, _, H, W = x.shape
            tokens = tokens.reshape(B, H // self.patch_size, W // self.patch_size, -1)
            tokens = tokens.permute(0, 3, 1, 2).contiguous()
        return (tokens,)


@register_backend("insid3")
class Insid3Backend(ZeroShotBackend):
    """
    INSID3 in-context segmentation — reference image + mask in, the same
    concept segmented in each target out.  github.com/visinf/INSID3

    Drives INSID3's own pipeline step by step rather than through ``segment()``,
    which re-encodes every reference for every target and returns a bare
    boolean mask: here references are encoded once per class, targets are
    encoded a batch at a time, and the cross-image similarity map is kept to
    score each output instance.  Those steps are private INSID3 methods, which
    is why the container pins the INSID3 commit.

    ``model_id`` names the DINOv3 backbone.  ``weights`` is that backbone's
    original .pth checkpoint; without one, the backbone is downloaded from the
    Hugging Face Hub (gated: needs HF_TOKEN for an account that has accepted
    the DINOv3 license on the model page).
    """

    default_model_id = "dinov3_vitl16"
    supports_text = False
    supports_exemplars = False
    supports_references = True

    BACKBONES = ("dinov3_vits16", "dinov3_vitb16", "dinov3_vitl16")

    def __init__(
        self,
        model_id: Optional[str] = None,
        device: str = DEVICE,
        weights: Optional[str] = None,
        image_size: int = 1024,
        min_area: float = 0.0,
    ):
        super().__init__(model_id=model_id, device=device)
        if self.model_id not in self.BACKBONES:
            raise ValueError(
                f"INSID3 backbone must be one of {', '.join(self.BACKBONES)}, got '{self.model_id}'"
            )
        self.weights = weights or os.environ.get("DINOV3_WEIGHTS", "")
        # dinov3_vitl16 -> facebook/dinov3-vitl16-pretrain-lvd1689m
        self.hf_repo = f"facebook/{self.model_id.replace('_', '-')}-pretrain-lvd1689m"
        self.image_size = image_size
        self.min_area = min_area
        self.model = None
        self._references: dict[str, dict] = {}

    def load(self) -> None:
        if cv2 is None:
            raise RuntimeError("INSID3 needs opencv-python to split masks into instances")
        if self.weights and not Path(self.weights).is_file():
            raise RuntimeError(
                f"DINOv3 weights not found at '{self.weights}'. Pass --insid3_weights with the "
                f"{self.model_id} .pth checkpoint from github.com/facebookresearch/dinov3, "
                f"or omit it to download {self.hf_repo} from the Hugging Face Hub."
            )

        # Neither repo is pip-installable; the container clones both (see .def).
        for env_var, default in (("INSID3_HOME", "/opt/insid3"), ("DINOV3_HOME", "/opt/dinov3")):
            home = os.environ.get(env_var, default)
            if home not in sys.path:
                sys.path.insert(0, home)

        from models.insid3 import INSID3

        if self.weights:
            from dinov3.hub import backbones

            logger.info(f"Loading INSID3 on {self.model_id} ({self.weights}) on {self.device}...")
            # Load the checkpoint ourselves: handed a path, the hub builder copies
            # it into $TORCH_HOME on every run.
            encoder = getattr(backbones, self.model_id)(pretrained=False)
            encoder.load_state_dict(
                torch.load(self.weights, map_location="cpu", weights_only=True), strict=True
            )
        else:
            from transformers import DINOv3ViTModel

            logger.info(f"Loading INSID3 on {self.hf_repo} (Hugging Face Hub) on {self.device}...")
            try:
                encoder = HFDinov3Encoder(DINOv3ViTModel.from_pretrained(self.hf_repo))
            except OSError as e:
                raise RuntimeError(
                    f"Could not download {self.hf_repo}: {e}. The repo is gated — set HF_TOKEN "
                    f"for an account that has accepted the DINOv3 license on "
                    f"huggingface.co/{self.hf_repo}, or pass --insid3_weights."
                ) from e
        self.model = INSID3(
            encoder=encoder,
            image_size=self.image_size,
            mask_refiner="bilinear",
            # Masks come back at image_size x image_size; instances are split
            # there and scaled to each tile, rather than upsampling every mask
            # to full resolution first.
            resize_to_orig_size=False,
            device=self.device,
        ).to(self.device).eval()
        for param in self.model.parameters():
            param.requires_grad = False
        logger.info("Model loaded")

    @torch.no_grad()
    def _encode(self, pixels: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """(N, C, H, W) normalised images -> L2-normalised and debiased (N, C, h, w) features."""
        fmaps = F.normalize(self.model._extract_features(pixels.unsqueeze(0)), p=2, dim=2)
        return fmaps[0], self.model._debias_features(fmaps)[0]

    def _to_pixels(self, images: list[Image.Image]) -> torch.Tensor:
        return torch.stack([self.model._transform(img) for img in images]).to(self.device)

    @torch.no_grad()
    def prepare_references(self, shots_by_label: dict[str, list[ReferenceShot]]) -> None:
        from utils.data import downsample_mask, load_mask

        for label, shots in shots_by_label.items():
            pixels = self._to_pixels([shot.image for shot in shots])
            masks = torch.cat([
                load_mask(torch.from_numpy(shot.mask), self.image_size, self.device)
                for shot in shots
            ]).unsqueeze(1)                                       # (S, 1, H, W)

            feats_deb = torch.cat([
                self._encode(pixels[i: i + 1])[1] for i in range(len(shots))
            ]).unsqueeze(0)                                       # (1, S, C, h, w)
            h, w = feats_deb.shape[-2:]

            # Same prototype INSID3 builds: mean foreground feature per shot,
            # averaged across shots.
            prototypes = []
            for s in range(len(shots)):
                fg = feats_deb[0, s, :, downsample_mask(masks[s: s + 1], h, w)]
                if fg.shape[1] > 0:
                    prototypes.append(fg.mean(dim=1))
            prototype = F.normalize(torch.stack(prototypes).mean(dim=0), p=2, dim=0).unsqueeze(1)

            self._references[label] = {
                "feats_deb": feats_deb,
                "masks": masks,
                "prototype": prototype,
            }
            logger.info(f"Encoded {len(shots)} reference shot(s) for '{label}'")

    @torch.no_grad()
    def _segment(
        self,
        feat_tgt: torch.Tensor,
        feat_tgt_deb: torch.Tensor,
        tgt_pixels: torch.Tensor,
        ref: dict,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """INSID3.predict_mask from feature extraction on, with cached references.

        Returns ``(mask, similarity)``: an (image_size, image_size) bool mask and
        the (h, w) similarity of each target patch to the reference prototype.
        """
        from utils.clustering import agglomerative_clustering, compute_cluster_prototypes

        _, C, h, w = feat_tgt.shape
        feats_ref_deb, prototype = ref["feats_deb"], ref["prototype"]

        sim_maps = [
            torch.einsum("bchw,bcxy->bhwxy", feats_ref_deb[:, m], feat_tgt_deb)
            for m in range(feats_ref_deb.shape[1])
        ]
        candidate = self.model._locate_candidates(
            sim_maps, ref["masks"], feat_tgt_deb, prototype, h, w
        )
        similarity = torch.einsum("bchw,cd->bhw", feat_tgt_deb, prototype).squeeze(0)

        if candidate.sum() == 0:
            mask = candidate
        else:
            cluster_labels = agglomerative_clustering(
                feat_tgt[0].reshape(C, -1).permute(1, 0), self.model.tau
            ).reshape(h, w)
            K = int(cluster_labels.max().item()) + 1
            cluster_protos = compute_cluster_prototypes(
                feat_tgt_deb[0].reshape(C, -1).permute(1, 0), cluster_labels.view(-1), K
            )
            mask = self.model._seed_and_aggregate(
                candidate, cluster_labels, cluster_protos, K,
                prototype, feat_tgt, feat_tgt_deb, h, w,
            )

        return self.model._finalize_mask(mask, tgt_pixels), similarity

    def _mask_to_detections(
        self,
        mask: torch.Tensor,
        similarity: torch.Tensor,
        tile_size: tuple[int, int],
        prompt: Prompt,
        want_segmentation: bool,
    ) -> list[Detection]:
        """Split a class mask into one detection per connected component."""
        mask_np = mask.cpu().numpy().astype(np.uint8)
        if not mask_np.any():
            return []

        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask_np, connectivity=8)
        sim_np = F.interpolate(
            similarity[None, None].float(), size=mask_np.shape, mode="bilinear", align_corners=False
        )[0, 0].cpu().numpy()
        counts = np.maximum(stats[:, cv2.CC_STAT_AREA], 1)
        mean_sim = np.bincount(labels.ravel(), weights=sim_np.ravel(), minlength=n) / counts

        width, height = tile_size
        sx, sy = width / mask_np.shape[1], height / mask_np.shape[0]

        detections: list[Detection] = []
        for k in range(1, n):                        # 0 is background
            x, y, w, h, area = stats[k]
            if area * sx * sy < self.min_area:
                continue

            seg_points: list[list[int]] = []
            if want_segmentation:
                seg_points = [
                    [int(round(px * sx)), int(round(py * sy))]
                    for px, py in mask_to_boundary_points(labels == k)
                ]

            detections.append(Detection(
                x_min=int(x * sx), y_min=int(y * sy),
                x_max=int(math.ceil((x + w) * sx)), y_max=int(math.ceil((y + h) * sy)),
                score=float(np.clip(mean_sim[k], 0.0, 1.0)),
                label=prompt.label,
                prompt_type=prompt.kind,
                segmentation=seg_points,
            ))
        return detections

    @torch.no_grad()
    def predict(
        self,
        images: list[Image.Image],
        prompts: list[Prompt],
        threshold: float,
        mask_threshold: float,
        want_segmentation: bool,
    ) -> list[list[Detection]]:
        # threshold / mask_threshold don't apply: INSID3 makes its own
        # foreground decision, and scores only rank instances for NMS.
        pixels = self._to_pixels(images)
        feats, feats_deb = self._encode(pixels)

        batch_detections: list[list[Detection]] = []
        for i, (image, prompt) in enumerate(zip(images, prompts)):
            mask, similarity = self._segment(
                feats[i: i + 1], feats_deb[i: i + 1], pixels[i: i + 1],
                self._references[prompt.label],
            )
            batch_detections.append(
                self._mask_to_detections(mask, similarity, image.size, prompt, want_segmentation)
            )

        del pixels, feats, feats_deb
        if self.device == "cuda":
            torch.cuda.empty_cache()

        return batch_detections


# ──────────────────────────────────────────────────────────────────────────────
# Reference preparation  (INSID3)
# ──────────────────────────────────────────────────────────────────────────────

def polygon_to_mask(polygon: list[list[float]], width: int, height: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    points = np.round(np.asarray(polygon, dtype=np.float64)).astype(np.int32)
    if len(points) >= 3:
        cv2.fillPoly(mask, [points], 1)
    return mask.astype(bool)


def box_to_mask(box: list[float], width: int, height: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=bool)
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    mask[max(0, y1): min(height, y2), max(0, x1): min(width, x2)] = True
    return mask


class Sam3BoxSegmenter:
    """Box -> object mask with the SAM3 tracker head (SAM-style box prompt).

    Unlike Sam3Model's exemplar prompts, which find every instance LIKE the
    box, this segments the one object INSIDE it — what a reference mask needs.
    """

    def __init__(self, device: str):
        from transformers import Sam3TrackerModel, Sam3TrackerProcessor

        logger.info(f"Loading {SAM3_BOX_MODEL_ID} tracker to turn reference boxes into masks...")
        self.device = device
        self.processor = Sam3TrackerProcessor.from_pretrained(SAM3_BOX_MODEL_ID)
        self.model = Sam3TrackerModel.from_pretrained(SAM3_BOX_MODEL_ID).to(device).eval()

    @torch.no_grad()
    def masks(self, image: Image.Image, boxes: list[list[float]]) -> list[np.ndarray]:
        inputs = self.processor(images=image, input_boxes=[boxes], return_tensors="pt").to(self.device)
        outputs = self.model(**inputs, multimask_output=False)
        masks = self.processor.post_process_masks(
            outputs.pred_masks.cpu(), inputs["original_sizes"].cpu()
        )[0]                                         # (num_boxes, 1, H, W)

        width, height = image.size
        result = []
        for box, mask in zip(boxes, masks):
            # Keep the mask inside its box so a leaky prediction can't pull
            # background into the reference prototype.
            result.append(mask[0].numpy().astype(bool) & box_to_mask(box, width, height))
        return result

    def close(self) -> None:
        del self.model, self.processor
        if self.device == "cuda":
            torch.cuda.empty_cache()


def find_image(key: str, discovered: list[tuple[Path, str]]) -> Optional[Path]:
    """Resolve a prompt-file image key the way resolve_exemplars does."""
    for path, rel_key in discovered:
        if rel_key == key:
            return path
    basename = Path(key).name
    for path, rel_key in discovered:
        if Path(rel_key).name == basename:
            return path
    return None


def crop_window(
    box: list[float], width: int, height: int, size: int
) -> tuple[int, int, int, int]:
    """A ``size`` x ``size`` window centred on ``box``, shifted to stay inside the image."""
    size_w, size_h = min(size, width), min(size, height)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    x1 = int(min(max(0, cx - size_w / 2), width - size_w))
    y1 = int(min(max(0, cy - size_h / 2), height - size_h))
    return x1, y1, x1 + size_w, y1 + size_h


def build_reference_shots(
    exemplars: dict[str, list[Prompt]],
    discovered: list[tuple[Path, str]],
    box_masks: str,
    device: str,
    is_sahi: bool,
    tile_size: int,
) -> dict[str, list[ReferenceShot]]:
    """
    Turn prompt-file exemplars into INSID3 reference shots, grouped by label.

    Without SAHI there is one shot per (image, label): the whole image and the
    union of that label's object masks.  With SAHI, targets are tiles, so each
    object becomes its own tile-sized crop — a reference at the scale the
    model will see objects at — with every same-label object in the crop
    marked foreground.
    """
    if "default" in exemplars:
        logger.warning("Ignoring the 'default' exemplar entry — references must be drawn on a real image")

    segmenter: Optional[Sam3BoxSegmenter] = None
    shots: dict[str, list[ReferenceShot]] = {}
    ignored_negatives = 0

    try:
        for key, prompts in exemplars.items():
            if key == "default":
                continue
            path = find_image(key, discovered)
            if path is None:
                logger.warning(f"Reference image '{key}' is not under --image_dir — skipping it")
                continue

            image = Image.open(path).convert("RGB")
            width, height = image.size

            # Per label: (box, outline-or-None) for each positive object.
            objects: dict[str, list[tuple[list[float], Optional[list[list[float]]]]]] = {}
            for prompt in prompts:
                polygons = prompt.polygons or [None] * len(prompt.boxes)
                for box, box_label, polygon in zip(prompt.boxes, prompt.box_labels, polygons):
                    if box_label != 1:
                        ignored_negatives += 1
                        continue
                    objects.setdefault(prompt.label, []).append((box, polygon))

            box_only = [box for objs in objects.values() for box, polygon in objs if polygon is None]
            refined: list[np.ndarray] = []
            if box_only and box_masks == "sam3":
                if segmenter is None:
                    try:
                        segmenter = Sam3BoxSegmenter(device)
                    except Exception as exc:
                        logger.warning(
                            f"Could not load the SAM3 tracker ({exc}) — using box-shaped "
                            f"reference masks instead"
                        )
                        box_masks = "box"
                if segmenter is not None:
                    refined = segmenter.masks(image, box_only)

            for label, objs in objects.items():
                masks = []
                for box, polygon in objs:
                    if polygon is not None:
                        masks.append(polygon_to_mask(polygon, width, height))
                    elif refined:
                        masks.append(refined.pop(0))
                    else:
                        masks.append(box_to_mask(box, width, height))
                masks = [m for m in masks if m.any()]
                if not masks:
                    logger.warning(f"Reference '{key}' / '{label}': every mask was empty — skipping")
                    continue

                union = np.logical_or.reduce(masks)
                if not is_sahi:
                    shots.setdefault(label, []).append(ReferenceShot(label, image, union))
                    continue

                for box, _ in objs:
                    window = crop_window(box, width, height, tile_size)
                    x1, y1, x2, y2 = window
                    crop_mask = union[y1:y2, x1:x2]
                    if crop_mask.any():
                        shots.setdefault(label, []).append(
                            ReferenceShot(label, image.crop(window), crop_mask.copy())
                        )
    finally:
        if segmenter is not None:
            segmenter.close()

    if ignored_negatives:
        logger.info(f"Ignored {ignored_negatives} negative exemplar box(es) — INSID3 uses positives only")

    for label, label_shots in shots.items():
        if len(label_shots) > MAX_REFERENCE_SHOTS:
            idx = np.round(np.linspace(0, len(label_shots) - 1, MAX_REFERENCE_SHOTS)).astype(int)
            shots[label] = [label_shots[i] for i in idx]
            logger.info(
                f"'{label}': using {MAX_REFERENCE_SHOTS} of {len(label_shots)} reference shots"
            )
    return shots


# ──────────────────────────────────────────────────────────────────────────────
# Prompt loading
# ──────────────────────────────────────────────────────────────────────────────

def split_text_prompt(prompt: str) -> tuple[str, str]:
    """``'dog=german shepherd'`` -> ``('dog', 'german shepherd')``: the model is
    prompted with the left side and detections are labelled with the right.

    A prompt with no ``=``, or nothing after it, is its own label; one with
    nothing before it is kept whole rather than sending an empty prompt.
    """
    text, sep, label = prompt.partition("=")
    text, label = text.strip(), label.strip()
    if not sep or not text:
        return prompt.strip(), prompt.strip()
    return text, label or text


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
            polygons = [
                [[float(x), float(y)] for x, y in polygon] if polygon else None
                for polygon in entry.get("polygons") or []
            ]
            if not boxes and polygons:
                # Outlines alone: each polygon is an object, boxed by its extent.
                polygons = [p for p in polygons if p]
                boxes = [
                    [min(x for x, _ in p), min(y for _, y in p), max(x for x, _ in p), max(y for _, y in p)]
                    for p in polygons
                ]
            if not boxes:
                continue
            if any(len(box) != 4 for box in boxes):
                raise ValueError(f"Exemplar boxes for '{image_key}' must be [x1, y1, x2, y2]")
            if polygons and len(polygons) != len(boxes):
                raise ValueError(
                    f"Exemplar '{image_key}': {len(boxes)} boxes but {len(polygons)} polygons "
                    f"(use null for a box with no outline)"
                )

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
                polygons=polygons,
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
    reference_labels: Optional[list[str]] = None,
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

        for raw_prompt in text_prompts:
            text, label = split_text_prompt(raw_prompt)
            for tile in tiles:
                jobs.append(TileJob(
                    image_idx=image_idx,
                    tile=tile,
                    prompt=Prompt(label=label, text=text, kind="text"),
                ))

        # References were encoded up front; every tile of every image is
        # segmented for each referenced class, reference images included.
        for label in reference_labels or []:
            for tile in tiles:
                jobs.append(TileJob(
                    image_idx=image_idx,
                    tile=tile,
                    prompt=Prompt(label=label, text=label, kind="reference"),
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


def split_by_prompt_kind(jobs: list[TileJob]) -> tuple[list[TileJob], list[TileJob], list[TileJob]]:
    """
    Separate text, exemplar and reference jobs so batches stay homogeneous —
    exemplar padding never leaks into text-only inference, and text jobs are
    never charged for a geometry-encoder pass.
    """
    exemplar_jobs = [j for j in jobs if j.prompt.has_exemplars()]
    reference_jobs = [j for j in jobs if j.prompt.kind == "reference"]
    text_jobs = [
        j for j in jobs if not j.prompt.has_exemplars() and j.prompt.kind != "reference"
    ]
    return text_jobs, exemplar_jobs, reference_jobs


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
                        help="HuggingFace checkpoint override for the backend; for insid3, "
                             "the DINOv3 backbone (dinov3_vits16 / dinov3_vitb16 / dinov3_vitl16)")
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

    # INSID3 (in-context segmentation)
    parser.add_argument("--insid3_weights", type=str, default=None,
                        help="DINOv3 .pth checkpoint matching --model_id (default: "
                             "$DINOV3_WEIGHTS; if unset, download from the Hugging Face Hub)")
    parser.add_argument("--insid3_image_size", type=int, default=1024,
                        help="Square resolution INSID3 runs at; 768 is faster, slightly less accurate")
    parser.add_argument("--reference_box_masks", type=str, default="sam3",
                        choices=["sam3", "box"],
                        help="How a reference box with no polygon becomes a mask: segmented by "
                             "the SAM3 tracker, or the filled box")
    parser.add_argument("--min_area", type=float, default=0.0,
                        help="Drop INSID3 instances smaller than this many pixels")

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

    for raw_prompt in text_prompts:
        text, label = split_text_prompt(raw_prompt)
        if text != label:
            logger.info(f"Text prompt '{text}' -> label '{label}'")

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

    # References may sit past the --max_images cut-off; look them up in the lot.
    all_discovered = discovered
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

    backend_kwargs = {}
    if args.model == "insid3":
        backend_kwargs = {
            "weights": args.insid3_weights,
            "image_size": args.insid3_image_size,
            "min_area": args.min_area,
        }
    try:
        backend = get_backend(args.model, model_id=args.model_id, device=device, **backend_kwargs)
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    # A reference backend reads the prompt file's exemplars as references.
    references: dict[str, list[Prompt]] = {}
    if backend.supports_references:
        references, exemplars = exemplars, {}
        if not references:
            logger.error(
                f"Backend '{args.model}' needs reference exemplars — pass a --prompt_file "
                f"with boxes and/or polygons drawn on some of the images."
            )
            return 1
        if text_prompts and not backend.supports_text:
            # Typically left over in the step's config from an earlier text run.
            logger.warning(
                f"Backend '{args.model}' has no text prompts — ignoring {text_prompts}"
            )
            text_prompts = []

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

    # ── References ───────────────────────────────────────────────────────────
    reference_shots: dict[str, list[ReferenceShot]] = {}
    if references:
        if cv2 is None:
            logger.error("Reference prompts need opencv-python to rasterise masks")
            return 1
        reference_shots = build_reference_shots(
            exemplars=references,
            discovered=all_discovered,
            box_masks=args.reference_box_masks,
            device=device,
            is_sahi=args.is_sahi,
            tile_size=args.tile_size,
        )
        if not reference_shots:
            logger.error("No usable reference masks — check the prompt file's image keys")
            return 1

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
        reference_labels=sorted(reference_shots),
    )
    if not jobs:
        logger.error("Nothing to do — no (image, prompt, tile) combinations were produced")
        return 1

    text_jobs, exemplar_jobs, reference_jobs = split_by_prompt_kind(jobs)
    logger.info(
        f"Planned {len(jobs)} inferences "
        f"({len(text_jobs)} text, {len(exemplar_jobs)} exemplar, "
        f"{len(reference_jobs)} reference) | "
        f"SAHI={args.is_sahi} "
        f"tile_size={args.tile_size if args.is_sahi else '-'} "
        f"overlap={args.overlap_ratio if args.is_sahi else '-'} "
        f"batch_size={args.batch_size} device={device}"
    )

    # ── Run ──────────────────────────────────────────────────────────────────
    start = time.time()
    try:
        backend.load()
        if reference_shots:
            backend.prepare_references(reference_shots)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    detections_by_idx: dict[int, list[Detection]] = {}
    total_failed_batches = 0
    for job_group, desc in (
        (text_jobs, "Text prompts"),
        (exemplar_jobs, "Exemplar prompts"),
        (reference_jobs, "Reference prompts"),
    ):
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
        # What the model was asked for -> the class written to "class".
        "text_prompt_labels": {
            text: label for text, label in (split_text_prompt(t) for t in text_prompts)
        },
        "exemplar_images": len(exemplars),
        # Class -> reference shots INSID3 was given.
        "reference_shots": {label: len(shots) for label, shots in reference_shots.items()},
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
            **({
                "insid3_image_size": args.insid3_image_size,
                "insid3_weights": backend.weights or backend.hf_repo,
                "reference_box_masks": args.reference_box_masks,
                "min_area": args.min_area,
            } if reference_shots else {}),
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
