"""Export the backend's OpenAPI spec to docs/openapi.{json,yaml}.

The spec is generated from the live FastAPI routes, so this never needs hand
editing — re-run it after adding or changing an endpoint and commit the result.

    cd backend
    ./.venv/bin/python -m scripts.export_openapi          # write both files
    ./.venv/bin/python -m scripts.export_openapi --check  # CI: fail if stale

Importing main initializes DBOS, which connects to Postgres and runs its
migrations, so the database must be reachable (docker compose up -d).
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main  # noqa: E402  (path set up above)

DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs"
JSON_PATH = DOCS / "openapi.json"
YAML_PATH = DOCS / "openapi.yaml"


def render() -> tuple[str, str | None]:
    """Return (json_text, yaml_text). yaml_text is None when PyYAML is absent."""
    schema = main.app.openapi()
    json_text = json.dumps(schema, indent=2, sort_keys=False) + "\n"
    try:
        import yaml
    except ImportError:
        return json_text, None
    return json_text, yaml.safe_dump(schema, sort_keys=False, width=100) + "\n"


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="Exit non-zero if the committed files are out of date.")
    args = ap.parse_args()

    json_text, yaml_text = render()
    targets = [(JSON_PATH, json_text)]
    if yaml_text is not None:
        targets.append((YAML_PATH, yaml_text))

    if args.check:
        stale = [p.name for p, text in targets
                 if not p.exists() or p.read_text() != text]
        if stale:
            print(f"OpenAPI spec is out of date: {', '.join(stale)}\n"
                  f"Regenerate with: python -m scripts.export_openapi", file=sys.stderr)
            return 1
        print("OpenAPI spec is up to date.")
        return 0

    DOCS.mkdir(parents=True, exist_ok=True)
    for path, text in targets:
        path.write_text(text)
        print(f"wrote {path.relative_to(DOCS.parent)} ({len(text):,} bytes)")
    if yaml_text is None:
        print("note: PyYAML not installed — skipped openapi.yaml", file=sys.stderr)

    schema = main.app.openapi()
    ops = sum(1 for ops in schema["paths"].values() for m in ops
              if m in ("get", "post", "put", "patch", "delete"))
    print(f"     {len(schema['paths'])} paths, {ops} operations, "
          f"{len(schema.get('components', {}).get('schemas', {}))} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
