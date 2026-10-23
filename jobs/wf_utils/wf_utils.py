"""
wf_utils.py
===========

Entry point of the shared wf-utils container: small, CPU-only data tools that
don't each deserve their own image and Tapis app. The first argument names the
tool; everything after it is that tool's own arguments.

    python wf_utils.py merge_folders --inputs_root /job/input --output /job/output/merged
    python wf_utils.py zero_shot_to_yolo --annotations a.json --images imgs --output dataset
    python wf_utils.py split_dataset --input dataset --output split --val_split 0.2 --test_split 0.1
    python wf_utils.py --list

Adding a tool: drop <name>.py next to this file with a main() that parses
sys.argv, add it to TOOLS, rebuild the image, and point the step's first
appArg at <name>. The Tapis app stays the same.
"""

import importlib
import sys

# tool name -> one-line description. Modules are imported only when chosen,
# so one tool's dependencies never affect another.
TOOLS = {
    "merge_folders": "Copy the contents of several folders into one target folder",
    "merge_annotations": "Combine several annotation files (zero-shot / Smart Labeler) into one",
    "zero_shot_to_yolo": "Convert zero-shot (or Smart Labeler) annotations into a YOLO dataset",
    "split_dataset": "Split a dataset into train/valid/test in the layout YOLO training expects",
}


def usage() -> str:
    width = max(len(n) for n in TOOLS)
    lines = ["usage: wf_utils.py <tool> [tool arguments...]   (<tool> --help for its options)", "", "tools:"]
    lines += [f"  {name.ljust(width)}  {desc}" for name, desc in TOOLS.items()]
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "--list"):
        print(usage())
        sys.exit(0 if len(sys.argv) >= 2 else 2)
    tool = sys.argv[1]
    if tool not in TOOLS:
        print(f"error: unknown tool '{tool}'\n\n{usage()}", file=sys.stderr)
        sys.exit(2)
    print(f"wf-utils: {tool}", flush=True)
    # The tool parses sys.argv itself, so hand it its own argument list with
    # its name in argv[0] (what argparse shows in usage and errors).
    sys.argv = [tool] + sys.argv[2:]
    importlib.import_module(tool).main()


if __name__ == "__main__":
    main()
