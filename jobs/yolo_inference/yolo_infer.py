#!/usr/bin/env python3
"""YOLO inference job for the Harvest workflow orchestrator.

Runs an Ultralytics YOLO model on a directory of images and writes results to an
output directory. ALL paths are passed as command-line arguments — nothing is
hardcoded — so the workflow's data-source / model-source / sink nodes fully
control where inputs come from and where outputs go.

Usage:
    yolo_infer.py --model <weights.pt> --images <image_dir> --output <out_dir>
                  [--conf 0.25] [--imgsz 640]

Outputs written to <out_dir>:
    predictions.json   — per-image detections (boxes, classes, confidences)
    annotated/         — input images with detections drawn on them
    summary.json       — run metadata (counts, model, params)
"""
import argparse
import json
import os
import sys
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def find_images(images_dir: Path):
    if images_dir.is_file():
        return [images_dir]
    return sorted(
        p for p in images_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Path to YOLO weights (.pt) OR a dir containing them")
    ap.add_argument("--images", required=True, help="Directory of images (or a single image)")
    ap.add_argument("--output", required=True, help="Directory to write results into")
    ap.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    ap.add_argument("--imgsz", type=int, default=640, help="Inference image size")
    args = ap.parse_args()

    model_path = Path(args.model)
    images_dir = Path(args.images)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "annotated").mkdir(parents=True, exist_ok=True)

    # A model "source" node may hand us a directory; find the .pt inside it.
    if model_path.is_dir():
        pts = sorted(model_path.rglob("*.pt"))
        if not pts:
            print(f"[yolo_infer] ERROR: no .pt weights found under {model_path}", file=sys.stderr)
            sys.exit(2)
        model_path = pts[0]
    print(f"[yolo_infer] model:  {model_path}")
    print(f"[yolo_infer] images: {images_dir}")
    print(f"[yolo_infer] output: {out_dir}")

    images = find_images(images_dir)
    print(f"[yolo_infer] found {len(images)} image(s)")
    if not images:
        print(f"[yolo_infer] ERROR: no images found under {images_dir}", file=sys.stderr)
        sys.exit(3)

    # Import here so --help works without the heavy dep installed.
    from ultralytics import YOLO
    import cv2
    import numpy as np

    def load_rgb(path: Path):
        """Load an image as a 3-channel BGR uint8 array for YOLO.

        Handles multispectral/multiband inputs (e.g. 10-channel COCO-multispectral):
        take the first 3 bands. Grayscale is expanded to 3 channels. This lets a
        standard RGB-trained YOLO model run on non-RGB imagery."""
        # IMREAD_UNCHANGED preserves all bands for multiband TIFFs.
        arr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if arr is None:
            raise RuntimeError(f"could not read image: {path}")
        if arr.ndim == 2:                      # grayscale -> 3ch
            arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        elif arr.shape[2] == 1:                # single channel -> 3ch
            arr = cv2.cvtColor(arr[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif arr.shape[2] >= 3:                # RGB or multiband -> first 3 bands
            arr = arr[:, :, :3]
        # Normalize non-8-bit imagery (e.g. 16-bit multispectral) to uint8.
        if arr.dtype != np.uint8:
            arr = cv2.normalize(arr, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        return arr

    model = YOLO(str(model_path))
    names = model.names  # class id -> name

    predictions = []
    for img_path in images:
        img = load_rgb(img_path)
        results = model.predict(source=img, conf=args.conf, imgsz=args.imgsz, verbose=False)
        r = results[0]
        dets = []
        boxes = getattr(r, "boxes", None)
        if boxes is not None:
            for b in boxes:
                cls_id = int(b.cls[0])
                dets.append({
                    "class_id": cls_id,
                    "class_name": names.get(cls_id, str(cls_id)) if isinstance(names, dict) else names[cls_id],
                    "confidence": round(float(b.conf[0]), 4),
                    "bbox_xyxy": [round(float(x), 2) for x in b.xyxy[0].tolist()],
                })
        predictions.append({"image": img_path.name, "detections": dets})

        # Save an annotated copy.
        annotated = r.plot()  # numpy BGR array with boxes drawn
        cv2.imwrite(str(out_dir / "annotated" / img_path.name), annotated)
        print(f"[yolo_infer]   {img_path.name}: {len(dets)} detection(s)")

    with open(out_dir / "predictions.json", "w") as f:
        json.dump(predictions, f, indent=2)

    summary = {
        "model": str(model_path),
        "num_images": len(images),
        "total_detections": sum(len(p["detections"]) for p in predictions),
        "conf": args.conf,
        "imgsz": args.imgsz,
        "classes": names if isinstance(names, dict) else {i: n for i, n in enumerate(names)},
    }
    with open(out_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"[yolo_infer] DONE — wrote predictions.json, summary.json, and "
          f"{len(images)} annotated image(s) to {out_dir}")


if __name__ == "__main__":
    main()
