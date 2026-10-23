# wf-utils

One container and one Tapis app (`wf-utils`) for small, CPU-only data tools, so a simple step doesn't need its own image and app. The first argument picks the tool:

```bash
apptainer run wf-utils.sif --list
apptainer run wf-utils.sif merge_folders --inputs /data/a /data/b --output ./merged
apptainer run wf-utils.sif zero_shot_to_yolo --annotations annotations.json --images ./images --output ./dataset
apptainer run wf-utils.sif split_dataset --input ./dataset --output ./split --val_split 0.2 --test_split 0.1
```

| Tool | Step | What it does |
|---|---|---|
| `merge_folders` | Merge Folders (`backend/steps/merge_folders`) | Copies the contents of several folders into one target folder |
| `merge_annotations` | Merge Annotations (`backend/steps/merge_annotations`) | Combines several annotation files into one |
| `zero_shot_to_yolo` | Annotations to YOLO Dataset (`backend/steps/zero_shot_to_yolo`) | Turns zero-shot (or Smart Labeler) annotations into a ready-to-train YOLO dataset |
| `split_dataset` | Split Dataset (`backend/steps/split_dataset`) | Splits a dataset into train / valid / test in the layout YOLO training expects |

`annotations_io.py` is shared code, not a tool: it reads and writes both annotation formats (zero-shot's flat output and the Smart Labeler's `annotations.json`).

A typical chain: Zero-Shot Annotation (one run per prompt set or folder) → Merge Annotations → Annotations to YOLO Dataset → Split Dataset → Model Training.

Each tool also runs on its own with plain Python, e.g. `python merge_folders.py --help`.

## Build and register

```bash
cd jobs/wf_utils
apptainer build wf-utils.sif wf_utils.def
```

Upload `wf-utils.sif` and register the Tapis app **`wf-utils`**, version **`1.0.0`**, like the other job apps, with `containerImage` set to the uploaded `.sif`. Every step listed above submits to that one app.

Then add the image's link to `backend/image_sources.json` under `"wf-utils"`, and its ID to the allowlist in `.gitleaks.toml`, so "Deploy to my node" can fetch it. Every step above expects the same `wf-utils.sif`, so one link covers them all.

## Adding a tool

1. Add `<name>.py` here with a `main()` that reads its arguments from `sys.argv` (argparse does this by default).
2. Add it to `TOOLS` in `wf_utils.py`, to `%files` in `wf_utils.def`, and any new packages to `requirements.txt`.
3. Write its step with `"tapis_app_id": "wf-utils"` and `"appId": "wf-utils"`, and make its first appArg the tool's name: `{ "name": "tool", "arg": "<name>" }`.
4. Rebuild the image, upload it, and update the app's image if its link changed. The app itself doesn't change.

## merge_folders

Copies 2–6 folders into one, in order, each keeping its subfolder layout. That target folder is the step's `merged` output.

```
folder_1/a.jpg, folder_1/sub/b.jpg   ->  merged/a.jpg, merged/sub/b.jpg
folder_2/a.jpg                       ->  merged/a_1.jpg          (--on_conflict rename)
```

- `--on_conflict` decides what happens when two folders have a file at the same path: `rename` keeps both (the default), `overwrite` lets the later folder win, `skip` keeps the first.
- `--into_subfolders` puts each folder under its own subfolder (`folder_1/`, `folder_2/`, …) instead.
- The target folder is emptied first, so a rerun never mixes in an earlier result. `merge_summary.json` (the step's `summary` output) records files copied per folder and how clashes were handled.

## merge_annotations

Combines 2–6 annotation files into one, in zero-shot or Smart Labeler format (`--format`); the inputs can be either, in any mix.

- Annotations for the same image are combined, e.g. two zero-shot runs on one folder with different text prompts.
- `--dedupe_iou 0.7` drops an annotation overlapping a higher-scored one of the same class on the same image by at least that IoU. The default 0 keeps everything.
- `--prefix_folders` is for files describing different folders that Merge Folders combined with `--into_subfolders`: file N's images are keyed under `folder_N/`, where that step put them. Without it, `a.jpg` from two folders would count as one image.
- `merge_annotations_summary.json` records each file's counts, the totals, duplicates dropped and how many images appeared in more than one file.

## zero_shot_to_yolo

```
<output>/                 # the step's `dataset` output
  data.yaml               # train/val, nc, names; no `path:`, so it resolves next to itself
  classes.txt
  images/train/  images/val/
  labels/train/  labels/val/
conversion_summary.json   # the step's `summary` output, next to the dataset
```

- `--images` must be the folder the annotations were made on; zero-shot keys each annotation by the image's path inside it. Keys that match no image are listed in the summary.
- Images are copied, keeping any subfolders, and split into train/val at random with a fixed `--seed`.
- `--task detect` writes `cls cx cy w h`. `--task segment` writes polygons from the zero-shot masks; a box-only annotation becomes a rectangle.
- Boxes are clipped to the image. Images with no annotations are kept as background images unless `--drop_empty` is set.
- Class indices follow `--classes` when given, otherwise every label found, sorted.

To train on it, wire the step's **dataset** output to Model Training's **dataset** input and set Model Training's **data file** to `data.yaml`, with the same task.

## split_dataset

Splits a dataset into train / validation / test (`--val_split`, `--test_split`, the rest is train) with a fixed `--seed`. Any split already in the input is ignored and redone.

Detection or segmentation (images with YOLO `.txt` labels):

```
<output>/
  data.yaml                       # train: train/images, val: valid/images, test: test/images, nc, names
  train/images/  train/labels/
  valid/images/  valid/labels/
  test/images/   test/labels/
```

- A label is found beside its image (`a.jpg` + `a.txt`) or in the matching `labels/` folder, so flat folders, `zero_shot_to_yolo` output and Roboflow-style exports all work.
- Class names come from the input's `data.yaml` or `classes.txt`; without either they're named `class_0`, `class_1`, ….
- Images with no label file are kept as background images, unless `--drop_unlabeled` is set.

Classification (one folder per class):

```
<output>/train/<class>/   val/<class>/   test/<class>/
```

- Split class by class, so each split keeps the class balance. The folder is `val`, not `valid`: that's the name Ultralytics looks for when classifying. No `data.yaml` is needed; Model Training takes the folder itself.

`--type auto` (the default) picks detection when any label file exists. Files that would land on the same path (e.g. `a.jpg` from both the old train and valid folders) are renamed `a_1.jpg`, with their labels. `split_summary.json` records images per split, classes and labels per class.
