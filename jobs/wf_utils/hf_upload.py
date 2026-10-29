"""
hf_upload.py
============

Pushes a trained model to a Hugging Face Hub model repo and records the links:

    model file (YOLO weights/best.pt)    ->  <repo>/best.pt
    model folder (HuggingFace best_model) ->  <repo>/<its files>
    metrics.json (optional)               ->  <repo>/metrics.json, and a model card (README.md)

The repo is created when it doesn't exist. The token comes from the HF_TOKEN
environment variable (never an argument, so it stays out of job listings and
logs) and needs write access. The links land in --result:

    { "repo_id", "repo_url", "private", "revision", "commit_url",
      "download_url", "files": [{ "path", "url", "download_url" }] }

`download_url` is pinned to this commit, so it keeps pointing at exactly what
this run uploaded even after later pushes to the same repo.

Usage:
    HF_TOKEN=hf_... python hf_upload.py --model weights/best.pt --repo_id apple-leaf-seg \\
        --metrics metrics.json --result hf_upload.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Push a trained model to the Hugging Face Hub",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model", required=True, help="The model: a weights file (best.pt) or a model folder")
    parser.add_argument("--source_name", default="",
                        help="The model's original path or URI, for its file name: a staged file arrives "
                             "renamed (input/model), and this restores best.pt")
    parser.add_argument("--repo_id", required=True,
                        help="'user-or-org/name', or just 'name' to create it under the token's own account")
    parser.add_argument("--metrics", default=None, help="Training metrics.json, uploaded and used for the model card")
    parser.add_argument("--private", action="store_true", help="Create the repo as private (only applies when creating it)")
    parser.add_argument("--no_card", action="store_true", help="Don't write a README.md model card")
    parser.add_argument("--commit_message", default="Upload model from Workflow Orchestrator")
    parser.add_argument("--result", required=True, help="Where to write the links (hf_upload.json)")
    return parser.parse_args()


def repo_name(raw: str) -> str:
    """A Hub-valid repo name: letters, digits, '-', '_' and '.' only."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip("-.")
    if not name:
        print(f"ERROR: '{raw}' is not usable as a Hugging Face repo name")
        sys.exit(1)
    return name[:96]


def model_card(repo_id: str, metrics: dict | None, weights: list[str]) -> str:
    task = (metrics or {}).get("task") or ""
    framework = (metrics or {}).get("framework") or ""
    meta = (metrics or {}).get("metadata") or {}
    scores = (metrics or {}).get("metrics") or {}

    tags = ["workflow-orchestrator"] + [t for t in (framework, task) if t]
    front = ["---", "tags:"] + [f"- {t}" for t in tags]
    if framework == "yolo":
        front.append("library_name: ultralytics")
    front.append("---")

    lines = front + ["", f"# {repo_id.split('/')[-1]}", "",
                     "Trained and published with the Workflow Orchestrator."]
    if task or meta.get("model"):
        lines += ["", f"- **Task:** {task or 'n/a'}", f"- **Base model:** {meta.get('model', 'n/a')}"]
        for key, label in (("epochs", "Epochs"), ("imgsz", "Image size"), ("batch", "Batch size")):
            if meta.get(key) is not None:
                lines.append(f"- **{label}:** {meta[key]}")
    numeric = {k: v for k, v in scores.items() if isinstance(v, (int, float))}
    if numeric:
        lines += ["", "## Metrics", "", "| Metric | Value |", "|---|---|"]
        lines += [f"| {k} | {v:.4f} |" for k, v in numeric.items()]
    if framework == "yolo" and weights:
        lines += ["", "## Usage", "", "```python",
                  "from huggingface_hub import hf_hub_download",
                  "from ultralytics import YOLO", "",
                  f'model = YOLO(hf_hub_download("{repo_id}", "{weights[0]}"))',
                  'results = model("image.jpg")', "```"]
    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print("ERROR: HF_TOKEN is not set. Pick a team secret holding a Hugging Face token with write access.")
        sys.exit(1)

    model = Path(args.model)
    if not model.exists():
        print(f"ERROR: model not found: {model}")
        sys.exit(1)
    metrics = None
    if args.metrics:
        try:
            metrics = json.loads(Path(args.metrics).read_text())
        except (OSError, ValueError) as e:
            print(f"WARNING: could not read metrics {args.metrics} ({e}); uploading without it")

    from huggingface_hub import CommitOperationAdd, HfApi
    from huggingface_hub.utils import HfHubHTTPError

    api = HfApi(token=token)
    try:
        user = api.whoami()["name"]
    except HfHubHTTPError as e:
        print(f"ERROR: Hugging Face rejected the token ({e}). Check that the secret holds a valid token.")
        sys.exit(1)

    owner, _, name = args.repo_id.strip().rpartition("/")
    repo_id = f"{repo_name(owner) if owner else user}/{repo_name(name)}"

    # Repo layout: a weights file keeps its own name (best.pt, not the staged
    # "model"); a model folder's files go in at the repo root.
    operations, weights = [], []
    if model.is_file():
        filename = Path(args.source_name.rstrip("/")).name if args.source_name else model.name
        if not Path(filename).suffix:
            filename = model.name if Path(model.name).suffix else "best.pt"
        operations.append(CommitOperationAdd(path_in_repo=filename, path_or_fileobj=str(model)))
        weights.append(filename)
    else:
        for path in sorted(p for p in model.rglob("*") if p.is_file()):
            rel = path.relative_to(model).as_posix()
            operations.append(CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(path)))
        if not operations:
            print(f"ERROR: model folder is empty: {model}")
            sys.exit(1)
    if metrics is not None:
        operations.append(CommitOperationAdd(path_in_repo="metrics.json", path_or_fileobj=args.metrics))
    if not args.no_card:
        operations.append(CommitOperationAdd(path_in_repo="README.md",
                                             path_or_fileobj=model_card(repo_id, metrics, weights).encode()))

    try:
        api.create_repo(repo_id, repo_type="model", private=args.private, exist_ok=True)
        print(f"INFO: pushing {len(operations)} file(s) to {repo_id}")
        commit = api.create_commit(repo_id, operations=operations, commit_message=args.commit_message)
        private = bool(getattr(api.model_info(repo_id), "private", args.private))
    except HfHubHTTPError as e:
        print(f"ERROR: upload to {repo_id} failed: {e}\n"
              f"       The token needs WRITE access, and to '{owner}' too if that's an organization.")
        sys.exit(1)

    repo_url = f"https://huggingface.co/{repo_id}"
    revision = commit.oid
    files = [{
        "path": op.path_in_repo,
        "url": f"{repo_url}/blob/{revision}/{op.path_in_repo}",
        "download_url": f"{repo_url}/resolve/{revision}/{op.path_in_repo}",
    } for op in operations]
    primary = next((f for f in files if f["path"] in weights), None)
    result = {
        "repo_id": repo_id,
        "repo_url": repo_url,
        "private": private,
        "revision": revision,
        "commit_url": commit.commit_url,
        "download_url": primary["download_url"] if primary else repo_url,
        "files": files,
    }
    out = Path(args.result)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))

    print(f"INFO: model repo : {repo_url}")
    if primary:
        print(f"INFO: download   : {primary['download_url']}")
    if private:
        print("INFO: the repo is private: these links need a Hugging Face token with read access")


if __name__ == "__main__":
    main()
