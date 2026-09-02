# Zero-Shot Annotation

Annotates every image in a directory with a zero-shot open-vocabulary model — no training, no class supports. SAM3 is the default backend, and the model is swappable through a registry.

This is the batch counterpart of `smart-labeller/sam3_service/main.py`, which serves one image per request.

## How It Works

1. Recursively discovers images under `--image_dir`, keyed by path relative to that root
2. Expands the work into `(image × prompt × tile)` jobs — one tile per image unless `--is_sahi` is set
3. Maps exemplar boxes into tile coordinates and drops the ones that fall outside
4. Runs the backend in batches of `--batch_size` tiles
5. Shifts tile-local boxes and mask polygons back to full-image coordinates
6. Merges detections across tiles and prompts with class-wise NMS
7. Writes annotations as flat JSON and/or COCO JSON

## Files

| File | Description |
|---|---|
| `zero_shot_annotation.py` | Everything — CLI, backend registry, tiling, NMS, writers |
| `zero_shot_annotation.def` | Apptainer/Singularity definition |
| `requirements.txt` | Python dependencies |

The script is self-contained; it does not import from `smart-labeller/`.

## Usage

```bash
cd zero_shot_annotation

# text prompts, whole images
python zero_shot_annotation.py \
  --image_dir /fs/ess/PAS2699/Demo_data/Weed_data \
  --output_dir /fs/ess/PAS2699/brijesh/outputs/out1 \
  --text_prompts "plant"

# text prompts, tiled inference for large imagery with small objects
python zero_shot_annotation.py \
  --image_dir /path/to/images \
  --output_dir /path/to/output \
  --text_prompts "tree" \
  --is_sahi --tile_size 960 --overlap_ratio 0.2 --batch_size 8

# exemplar prompts (and any text prompts) from a prompt file
python zero_shot_annotation.py \
  --image_dir /path/to/images \
  --output_dir /path/to/output \
  --prompt_file prompts.json
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--image_dir` | *(required)* | Directory of input images, searched recursively (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.tif`) |
| `--output_dir` | `output` | Root directory for annotation files |
| `--max_images` | *(all)* | Annotate at most this many images — useful for smoke tests |
| `--model` | `sam3` | Backend name from the registry |
| `--model_id` | backend default | HuggingFace checkpoint override (`facebook/sam3`) |
| `--device` | `auto` | Compute device: `auto`, `cuda`, or `cpu` |
| `--text_prompts` | *(none)* | Text concepts applied to every image, space-separated |
| `--prompt_file` | *(none)* | JSON file with text prompts and/or per-image exemplars |
| `--is_sahi` | `False` | Slice each image into overlapping tiles before inference |
| `--tile_size` | `960` | Tile size in pixels (with `--is_sahi`) |
| `--overlap_ratio` | `0.2` | Fractional overlap between adjacent tiles (with `--is_sahi`) |
| `--exemplar_tile_mode` | `intersecting` | Which tiles to run for exemplar prompts: `intersecting` or `all` |
| `--exemplar_min_visibility` | `0.5` | Fraction of an exemplar box that must fall inside a tile to count |
| `--batch_size` | `8` | Tiles per inference batch |
| `--confidence` | `0.3` | Score threshold for keeping an instance |
| `--mask_threshold` | `0.5` | Threshold for binarising predicted masks |
| `--nms_iou` | `0.5` | IoU threshold for merging detections across tiles |
| `--class_agnostic_nms` | `False` | Let detections from different prompts suppress each other |
| `--no_segmentation` | `False` | Skip mask-to-polygon extraction (boxes only, faster) |
| `--output_format` | `flat` | `flat`, `coco`, or `both` |
| `--log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

At least one of `--text_prompts` or `--prompt_file` is required.

## Prompts

### Text prompts

Free-text concepts applied to every image. Each detection is labelled with the prompt that found it.

```bash
--text_prompts "tree" "parked car" "solar panel"
```

### Exemplar prompts

Visual exemplars — SAM3 geometry prompts. You draw a box around one instance and the model finds the rest. Positive exemplars mean "more like this"; negative exemplars mean "not like this".

Exemplar boxes are **absolute pixel coordinates in a specific image**, so they are supplied per image through `--prompt_file` rather than on the command line.

```json
{
  "text_prompts": ["tree", "car"],

  "exemplars": {
    "default": [
      {"label": "tree", "boxes": [[10, 10, 90, 90]], "box_labels": [1]}
    ],
    "batch_a/img_001.jpg": [
      {
        "label": "shrub",
        "text":  "visual",
        "boxes":      [[100, 100, 200, 200], [820, 340, 900, 430], [1500, 1200, 1600, 1300]],
        "box_labels": [1, 1, 0]
      }
    ]
  }
}
```

| Key | Required | Description |
|---|---|---|
| `label` | yes | Class name written to the output annotations |
| `boxes` | yes | List of `[x1, y1, x2, y2]` pixel boxes in that image |
| `box_labels` | no | `1` positive, `0` negative — defaults to all positive |
| `text` | no | Concept name SAM3 pairs with the exemplar (default `"visual"`) |

Image keys are matched in this order: path relative to `--image_dir`, then bare filename, then the `default` entry. `default` applies to every image without its own entry — only meaningful when the imagery is uniform, since the boxes are absolute coordinates.

Text prompts given on the command line override those in the prompt file; exemplars only ever come from the file.

### Exemplars combined with SAHI

These two features are in tension: a tile far from the exemplar has no exemplar to condition on. `--exemplar_tile_mode` makes the choice explicit rather than silent.

| Mode | Behaviour |
|---|---|
| `intersecting` *(default)* | Run only tiles holding a positive exemplar. The number of skipped tiles is logged. |
| `all` | Run every tile. Tiles with no visible exemplar fall back to the exemplar's paired `text`, and those detections are recorded as `prompt_type: "text"` so you can tell them apart. |

A box counts as visible in a tile when at least `--exemplar_min_visibility` of its area falls inside; the box is then clipped to the tile.

## Outputs

Saved under `<output_dir>/annotations/`:

```
annotations_<model>_<timestamp>.json          # --output_format flat (default)
annotations_coco_<model>_<timestamp>.json     # --output_format coco
```

The flat format matches the rest of the repo and is read directly by `smart-labeller/evaluate_annotations.py`:

```json
{
  "model": "sam3",
  "model_id": "facebook/sam3",
  "params": { "is_sahi": true, "tile_size": 960, "...": "..." },
  "annotations": [
    {
      "image_path": "batch_a/img_001.jpg",
      "bounding_box": [69, 69, 130, 130],
      "score": 0.986,
      "class": "tree",
      "prompt_type": "text",
      "segmentation": [[70, 80], [71, 82]]
    }
  ]
}
```

`prompt_type` is `"text"` or `"exemplar"`. `segmentation` is the object's outline as `[x, y]` points (up to 256), omitted under `--no_segmentation`. Every run embeds its full parameter set for provenance.

The COCO file uses standard `images` / `annotations` / `categories` blocks with `bbox` in `[x, y, width, height]` and flattened polygons.

> Note: NMS is class-wise by default, so a text prompt and an exemplar prompt that both find the same object each keep their own annotation. Use `--class_agnostic_nms` to collapse them into one box per object.

## Container

Build:

```bash
apptainer build zero_shot_annotation.sif zero_shot_annotation.def
```

Run (`--nv` for GPU, plus binds for data and the HuggingFace cache so weights are not re-downloaded per job):

```bash
apptainer run --nv \
  --bind /fs/ess/PAS2699:/fs/ess/PAS2699 \
  --bind $HOME/.cache/huggingface:/root/.cache/huggingface \
  zero_shot_annotation.sif \
    --image_dir /fs/ess/PAS2699/path/to/images \
    --output_dir /fs/ess/PAS2699/path/to/output \
    --text_prompts "tree" \
    --is_sahi --tile_size 960 --batch_size 8
```

The model weights (~3.3 GB for `facebook/sam3`) download on first use. Set `HF_HUB_OFFLINE=1` once they are cached to skip the network check on compute nodes.

### GPU compatibility

`requirements.txt` pins the **cu126** build of PyTorch on purpose. Do not relax it to a bare `torch` without checking your hardware:

| GPU | Compute capability | cu126 wheel | default PyPI wheel (cu130) |
|---|---|---|---|
| V100 | 7.0 | ✅ | ❌ no kernels |
| A100 | 8.0 | ✅ | ✅ |
| H100 | 9.0 | ✅ | ✅ |

The default PyPI wheel ships kernels for CC ≥ 7.5 only. On a V100 it installs and imports fine, and `torch.cuda.is_available()` returns `True` — then every operation fails at runtime with:

```
GET was unable to find an engine to execute this computation
```

The script now catches this before loading any weights, with a preflight matmul that names the fix. If you hit it anyway, either rebuild the container against cu126 or rerun with `--device cpu` (much slower — SAM3 takes minutes per image on CPU).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All batches succeeded |
| `1` | Bad arguments, unusable device, or **one or more batches failed** |

A run that fails every batch aborts after 3 consecutive failures rather than grinding through the whole dataset, and writes no annotation file. A run with partial failures writes what succeeded, logs the count, and still exits `1` — so a job that quietly produced incomplete annotations cannot look like a success in your scheduler.

## Adding a Model

Backends are looked up in a registry, so nothing outside your new class needs to change — `--model` picks up the name automatically.

```python
@register_backend("my_model")
class MyBackend(ZeroShotBackend):
    default_model_id = "org/my-model"
    supports_text = True
    supports_exemplars = False        # CLI rejects exemplar prompts for this backend

    def load(self) -> None:
        ...                            # load weights onto self.device

    def predict(self, images, prompts, threshold, mask_threshold, want_segmentation):
        ...                            # return list[list[Detection]], one list per image
```

`predict` receives a batch of tile crops with `prompts[i]` applied to `images[i]`, and returns detections in **tile-local** coordinates — the driver handles offsets, NMS and writing. Exemplar boxes arrive already mapped into tile coordinates. Batches are homogeneous: text jobs and exemplar jobs are never mixed in the same call.

## Performance Notes

- `--batch_size` counts tiles, not images, so batches fill across images. A CUDA OOM falls back automatically to one tile at a time for that batch.
- `--no_segmentation` skips contour extraction when you only need boxes.
- With `--is_sahi`, tile count grows quickly: a 4000×3000 image at `--tile_size 960 --overlap_ratio 0.2` is 5 × 4 = 20 tiles **per prompt**. Total inferences = images × prompts × tiles.
- Each prompt is a separate forward pass, so runtime scales linearly with the number of prompts.

## Pipeline Position

Standalone. It produces annotations directly from images and prompts, with no class-support or proposal stage:

```
images + prompts  →  zero_shot_annotation.py  →  annotations .json  →  evaluate_annotations.py
```

Compare with the `smart-labeller/` pipeline, which routes through proposals and embedding-based classification:

```
class_supports  →  proposals  →  object_classification  →  annotations .json
```
