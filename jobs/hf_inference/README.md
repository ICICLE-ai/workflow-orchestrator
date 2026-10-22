# Model Inference

Runs a trained model over a folder of images and writes the predictions as JSON. The model can be a **YOLO** (Ultralytics) `.pt` file or a **HuggingFace** model folder, so it takes the `model` output of the Model Training step directly.

This is the job behind the **Model Inference** step (`backend/steps/hf_inference/step.json`).

## Files

| File | Description |
|---|---|
| `infer_main.py` | CLI entry point: argument parsing, YOLO and HuggingFace backends, output writers |
| `infer_main.def` | Apptainer/Singularity definition |
| `requirements.txt` | Python dependencies (torch comes from the base image) |

## How It Works

1. Parses arguments and creates `<output_path>/<name>/`
2. **YOLO:** loads the `.pt` with Ultralytics and runs `model.predict` over `--data`, also saving annotated images under `<name>/predictions/`
3. **HuggingFace:** finds every image under `--data` (recursively) and loads the model with the Auto class for the task:
   - `classify`: `AutoModelForImageClassification`, keeps up to the top 5 classes above `--conf`
   - `detect`: `AutoModelForObjectDetection`, boxes post-processed at `--conf`
   - `segment`: `AutoModelForSemanticSegmentation`; models with `sam` in the name use `SamModel`, prompted with the image centre
4. Writes `predictions.json` and `summary.json`

## Supported Models

| Framework | `--model` | Tasks |
|---|---|---|
| `yolo` | A `.pt` file, e.g. `weights/best.pt` from training | `detect`, `segment`, `classify` |
| `huggingface` | A saved model folder (config, weights, image processor), e.g. `best_model/` from training, or a Hub ID | `classify`, `detect`, `segment` |

The framework and task must match how the model was trained.

## Build

```bash
cd jobs/hf_inference
apptainer build hf_inference.sif infer_main.def
```

The base image is `pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime`. Its CUDA 12.1 build covers V100 (sm_70) through H100 GPUs. torch is not reinstalled from `requirements.txt`, because a default PyPI wheel can drop older GPU architectures.

## Usage

The `%runscript` calls `infer_main.py`, so arguments pass straight through:

```bash
# YOLO detection
apptainer run --nv \
  --bind /path/to/run:/job \
  hf_inference.sif \
  --framework yolo --task detect \
  --model /job/best.pt --data /job/images \
  --output_path /job --name output

# HuggingFace classification
apptainer run --nv \
  --bind /path/to/run:/job \
  hf_inference.sif \
  --framework huggingface --task classify \
  --model /job/best_model --data /job/images \
  --output_path /job --name output --batch 32
```

Without a container (`pip install -r requirements.txt` plus a CUDA build of torch):

```bash
python infer_main.py --framework yolo --task detect \
  --model best.pt --data images/ --output_path outputs
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--framework` | *(required)* | `yolo` or `huggingface` |
| `--model` | *(required)* | `.pt` file (YOLO) or model folder / Hub ID (HuggingFace) |
| `--task` | *(required)* | `detect`, `segment`, or `classify` |
| `--data` | *(required)* | Folder of images, searched recursively (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.webp`) |
| `--output_path` | `outputs` | Root folder for results |
| `--name` | `<framework>_<task>_<timestamp>` | Subfolder of `--output_path` the results go in |
| `--conf` | `0.25` | Minimum confidence for a prediction to be kept |
| `--device` | `cuda` if available, else `cpu` | Compute device |
| `--imgsz` | `640` | Inference image size in pixels (YOLO only) |
| `--batch` | `16` | Images per forward pass (HuggingFace only) |

## Outputs

```
<output_path>/<name>/
  predictions.json
  summary.json
  predictions/        # YOLO only: annotated images + Ultralytics JSON
```

`predictions.json` has one entry per image:

```json
[
  {
    "image": "img_001.jpg",
    "predictions": [
      { "class_id": 0, "class_name": "weed", "confidence": 0.81, "bbox": [12.0, 40.5, 210.3, 188.9] }
    ]
  }
]
```

- `bbox` is `[x1, y1, x2, y2]` in pixels, for detection only.
- Classification entries have no `bbox`.
- Segmentation lists class and confidence per instance (YOLO) or the classes present (HuggingFace semantic). Mask pixels are not written.

`summary.json` records the framework, task, model, data, device, confidence threshold, image count, total predictions and a timestamp.

## On the Platform

The step's `step.json` handles staging. These paths come from that file:
- the model is copied to `model.pt` (YOLO) or `model/` (HuggingFace)
- the images are copied to `images/`
- the job dir is bound to `/job`
- the step passes `--output_path /job --name output`, so results land in the job's output dir and are archived to the run folder

The Tapis app it submits to is **`hf_inference`**, version **`1.0.0`**. To register it, follow the `extract_frames` app:

- `runtime: SINGULARITY` with `runtimeOptions: ["SINGULARITY_RUN"]`
- `containerImage: "$PWD/hf_inference.sif"`
- a `container SIF` file input whose `sourceUrl` is the postit link to the built `.sif`, with `targetPath: "hf_inference.sif"`

Then add the same link to `backend/image_sources.json` under `"hf_inference"`, and its ID to the allowlist in `.gitleaks.toml`, so "Deploy to my node" bundles the image too.
