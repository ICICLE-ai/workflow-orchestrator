"""
infer_main.py
=============

Single entry point for the Workflow Orchestrator inference module.
Supports YOLO and HuggingFace inference backends via factory pattern.

Usage:
    # YOLO detection inference
    python infer_main.py --framework yolo --model /path/to/best.pt --task detect --data /path/to/images --output_path /path/to/outputs

    # HuggingFace classification inference
    python infer_main.py --framework huggingface --model /path/to/best_model --task classify --data /path/to/images --output_path /path/to/outputs

    # HuggingFace detection inference
    python infer_main.py --framework huggingface --model /path/to/best_model --task detect --data /path/to/images --output_path /path/to/outputs
"""

import argparse
import sys
import os
import json
import torch
from pathlib import Path
from datetime import datetime


# ─────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────
DEFAULT_OUTPUT          = "outputs"
DEFAULT_DEVICE          = "cuda" if torch.cuda.is_available() else "cpu"
DEFAULT_CONF            = 0.25
DEFAULT_IMGSZ           = 640
DEFAULT_BATCH           = 16


def parse_args():
    parser = argparse.ArgumentParser(
        description="Workflow Orchestrator — Inference Module",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Required ──────────────────────────────────────────────────────────
    parser.add_argument("--framework",   type=str, required=True,
                        choices=["yolo", "huggingface"],
                        help="Inference framework: yolo or huggingface")
    parser.add_argument("--model",       type=str, required=True,
                        help="Path to trained model (best.pt for YOLO, best_model/ dir for HuggingFace)")
    parser.add_argument("--task",        type=str, required=True,
                        choices=["detect", "segment", "classify"],
                        help="Task type: detect, segment, or classify")
    parser.add_argument("--data",        type=str, required=True,
                        help="Path to input images directory")

    # ── Optional ──────────────────────────────────────────────────────────
    parser.add_argument("--output_path", type=str, default=DEFAULT_OUTPUT,
                        help="Directory to save predictions and results")
    parser.add_argument("--device",      type=str, default=DEFAULT_DEVICE,
                        help="Compute device: cpu or cuda")
    parser.add_argument("--conf",        type=float, default=DEFAULT_CONF,
                        help="Minimum confidence threshold for detections/classifications")
    parser.add_argument("--imgsz",       type=int, default=DEFAULT_IMGSZ,
                        help="Inference image size (pixels) — YOLO only")
    parser.add_argument("--batch",       type=int, default=DEFAULT_BATCH,
                        help="Batch size for HuggingFace inference")
    parser.add_argument("--name",        type=str, default=None,
                        help="Run name for output folder (auto-generated if not set)")

    return parser.parse_args()


# ─────────────────────────────────────────────
# YOLO Inference
# ─────────────────────────────────────────────
def run_yolo_inference(args, output_dir: Path):
    """Run YOLO inference on an image directory."""
    from ultralytics import YOLO

    print(f"INFO: Loading YOLO model from {args.model}...")
    model = YOLO(args.model)

    print(f"INFO: Running YOLO {args.task} inference on {args.data}...")
    results = model.predict(
        source=args.data,
        conf=args.conf,
        imgsz=args.imgsz,
        device=args.device,
        save=True,
        save_json=True,
        project=str(output_dir),
        name="predictions",
    )

    # Build predictions JSON
    predictions = []
    for r in results:
        pred = {
            "image": Path(r.path).name,
            "predictions": [],
        }
        if args.task == "detect" and r.boxes is not None:
            for box in r.boxes:
                pred["predictions"].append({
                    "class_id": int(box.cls),
                    "class_name": model.names[int(box.cls)],
                    "confidence": float(box.conf),
                    "bbox": box.xyxy[0].tolist(),
                })
        elif args.task == "classify" and r.probs is not None:
            top1_idx = int(r.probs.top1)
            pred["predictions"].append({
                "class_id": top1_idx,
                "class_name": model.names[top1_idx],
                "confidence": float(r.probs.top1conf),
            })
        elif args.task == "segment" and r.masks is not None:
            for i, mask in enumerate(r.masks):
                pred["predictions"].append({
                    "class_id": int(r.boxes.cls[i]),
                    "class_name": model.names[int(r.boxes.cls[i])],
                    "confidence": float(r.boxes.conf[i]),
                })
        predictions.append(pred)

    return predictions


# ─────────────────────────────────────────────
# HuggingFace Inference
# ─────────────────────────────────────────────
def run_hf_inference(args, output_dir: Path):
    """Run HuggingFace inference on an image directory."""
    from transformers import AutoImageProcessor
    from PIL import Image
    import torch

    data_path = Path(args.data)
    image_extensions = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
    image_files = [
        f for f in data_path.rglob("*")
        if f.suffix.lower() in image_extensions
    ]

    if not image_files:
        print(f"ERROR: No images found in {args.data}")
        sys.exit(1)

    print(f"INFO: Found {len(image_files)} images")
    print(f"INFO: Loading model and processor from {args.model}...")

    processor = AutoImageProcessor.from_pretrained(args.model)
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")

    if args.task == "classify":
        from transformers import AutoModelForImageClassification
        model = AutoModelForImageClassification.from_pretrained(args.model)
        model.to(device)
        model.eval()
        return _hf_classify(model, processor, image_files, device, args)

    elif args.task == "detect":
        from transformers import AutoModelForObjectDetection
        model = AutoModelForObjectDetection.from_pretrained(args.model)
        model.to(device)
        model.eval()
        return _hf_detect(model, processor, image_files, device, args)

    elif args.task == "segment":
        if "sam" in args.model.lower():
            from transformers import SamModel, SamProcessor
            model = SamModel.from_pretrained(args.model)
            processor = SamProcessor.from_pretrained(args.model)
            model.to(device)
            model.eval()
            return _hf_sam_segment(model, processor, image_files, device, args)
        else:
            from transformers import AutoModelForSemanticSegmentation
            model = AutoModelForSemanticSegmentation.from_pretrained(args.model)
            model.to(device)
            model.eval()
            return _hf_segment(model, processor, image_files, device, args)
    else:
        print(f"ERROR: Unsupported task '{args.task}' for HuggingFace inference")
        sys.exit(1)


def _hf_classify(model, processor, image_files, device, args):
    """HuggingFace classification inference."""
    import torch

    predictions = []
    print(f"INFO: Running classification inference (batch_size={args.batch})...")

    for i in range(0, len(image_files), args.batch):
        batch_files = image_files[i:i + args.batch]
        images = [Image.open(f).convert("RGB") for f in batch_files]
        inputs = processor(images=images, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        logits = outputs.logits
        probs = torch.softmax(logits, dim=-1)
        top_probs, top_ids = probs.topk(5, dim=-1)

        for j, f in enumerate(batch_files):
            top_preds = []
            for prob, idx in zip(top_probs[j], top_ids[j]):
                label = model.config.id2label.get(idx.item(), str(idx.item()))
                confidence = prob.item()
                if confidence >= args.conf:
                    top_preds.append({
                        "class_id": idx.item(),
                        "class_name": label,
                        "confidence": round(confidence, 4),
                    })
            predictions.append({
                "image": f.name,
                "predictions": top_preds,
            })
        print(f"  Processed {min(i + args.batch, len(image_files))}/{len(image_files)} images")

    return predictions


def _hf_detect(model, processor, image_files, device, args):
    """HuggingFace object detection inference."""
    import torch

    predictions = []
    print(f"INFO: Running detection inference (batch_size={args.batch})...")

    for i in range(0, len(image_files), args.batch):
        batch_files = image_files[i:i + args.batch]
        images = [Image.open(f).convert("RGB") for f in batch_files]
        inputs = processor(images=images, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        # Post-process detections
        target_sizes = [img.size[::-1] for img in images]
        results = processor.post_process_object_detection(
            outputs, threshold=args.conf, target_sizes=target_sizes
        )

        for j, (f, result) in enumerate(zip(batch_files, results)):
            preds = []
            for score, label, box in zip(result["scores"], result["labels"], result["boxes"]):
                preds.append({
                    "class_id": label.item(),
                    "class_name": model.config.id2label.get(label.item(), str(label.item())),
                    "confidence": round(score.item(), 4),
                    "bbox": [round(v, 2) for v in box.tolist()],
                })
            predictions.append({
                "image": f.name,
                "predictions": preds,
            })
        print(f"  Processed {min(i + args.batch, len(image_files))}/{len(image_files)} images")

    return predictions


def _hf_segment(model, processor, image_files, device, args):
    """HuggingFace semantic segmentation inference."""
    import torch
    import numpy as np

    predictions = []
    print(f"INFO: Running segmentation inference (batch_size={args.batch})...")

    for i in range(0, len(image_files), args.batch):
        batch_files = image_files[i:i + args.batch]
        images = [Image.open(f).convert("RGB") for f in batch_files]
        inputs = processor(images=images, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        logits = outputs.logits  # [batch, num_classes, H, W]
        pred_masks = logits.argmax(dim=1).cpu().numpy()

        for j, f in enumerate(batch_files):
            unique_classes = np.unique(pred_masks[j]).tolist()
            class_names = [
                model.config.id2label.get(c, str(c)) for c in unique_classes
            ]
            predictions.append({
                "image": f.name,
                "predictions": [
                    {"class_id": c, "class_name": n}
                    for c, n in zip(unique_classes, class_names)
                ],
            })
        print(f"  Processed {min(i + args.batch, len(image_files))}/{len(image_files)} images")

    return predictions


def _hf_sam_segment(model, processor, image_files, device, args):
    """SAM segmentation inference using automatic point prompts."""
    import torch
    import numpy as np

    predictions = []
    print(f"INFO: Running SAM segmentation inference...")

    for f in image_files:
        image = Image.open(f).convert("RGB")
        w, h = image.size
        # Use center point as automatic prompt
        center_point = [[w // 2, h // 2]]
        inputs = processor(
            images=image,
            input_points=[center_point],
            return_tensors="pt",
        )
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        pred_masks = outputs.pred_masks.squeeze().cpu().numpy()
        predictions.append({
            "image": f.name,
            "predictions": [{
                "type": "segmentation_mask",
                "num_masks": int(pred_masks.shape[0]) if pred_masks.ndim > 2 else 1,
            }],
        })

    return predictions


# ─────────────────────────────────────────────
# Save Results
# ─────────────────────────────────────────────
def save_results(predictions, args, output_dir: Path):
    """Save predictions and summary to output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save full predictions
    predictions_path = output_dir / "predictions.json"
    with open(predictions_path, "w", encoding="utf-8") as f:
        json.dump(predictions, f, indent=2)
    print(f"\nPredictions saved to: {predictions_path}")

    # Save summary
    total_images = len(predictions)
    total_predictions = sum(len(p["predictions"]) for p in predictions)
    summary = {
        "framework": args.framework,
        "task": args.task,
        "model": args.model,
        "data": args.data,
        "device": args.device,
        "conf_threshold": args.conf,
        "total_images": total_images,
        "total_predictions": total_predictions,
        "avg_predictions_per_image": round(total_predictions / total_images, 2) if total_images > 0 else 0,
        "timestamp": datetime.now().isoformat(),
    }

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary saved to: {summary_path}")

    return summary


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    args = parse_args()

    # Setup output directory
    run_name = args.name or f"{args.framework}_{args.task}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir = Path(args.output_path) / run_name
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Workflow Orchestrator — Inference Module")
    print("=" * 60)
    print(f"Framework : {args.framework}")
    print(f"Task      : {args.task}")
    print(f"Model     : {args.model}")
    print(f"Data      : {args.data}")
    print(f"Device    : {args.device}")
    print(f"Output    : {output_dir}")
    print("=" * 60)

    # Run inference
    if args.framework == "yolo":
        predictions = run_yolo_inference(args, output_dir)
    elif args.framework == "huggingface":
        predictions = run_hf_inference(args, output_dir)
    else:
        print(f"ERROR: Unknown framework '{args.framework}'")
        sys.exit(1)

    # Save results
    summary = save_results(predictions, args, output_dir)

    print("\n" + "=" * 60)
    print("Inference Complete!")
    print(f"Total images     : {summary['total_images']}")
    print(f"Total predictions: {summary['total_predictions']}")
    print(f"Output directory : {output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()