"""`python3 -m wf_runner <bundle.json>` entry point.

Kept separate from runner.py so the same code is reachable three ways with no
duplication: this module, the console entry inside the Apptainer image's
%runscript, and a direct `python3 runner/wf_runner/runner.py` on a node where
the image isn't usable.
"""
from wf_runner.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
