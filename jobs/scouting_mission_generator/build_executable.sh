#!/usr/bin/env bash
# build_executable.sh
#
# Bundles fly_mission.py (plus mission_generator.py and tapis_upload.py)
# into a single standalone Linux executable using PyInstaller, so the
# end user can download one file and double-click it -- no Python
# environment, no pip install, no venv activation.
#
# IMPORTANT: this must be run on a machine that already has Olympe
# installed if you want the built executable to be able to actually fly
# a drone. If Olympe isn't installed at build time, PyInstaller can't
# bundle it, and the built executable will only be able to run in
# --dry-run mode (mission generation, no flight). Mission generation
# and Tapis upload work either way.
#
# Usage:
#   chmod +x build_executable.sh
#   ./build_executable.sh
#
# Output: dist/fly_mission  (a single executable file)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "== Installing build-time Python dependencies =="
pip install --break-system-packages -r requirements.txt pyinstaller

echo "== Building standalone executable with PyInstaller =="
pyinstaller \
    --onefile \
    --name fly_mission \
    --add-data "mission_generator.py:." \
    --add-data "tapis_upload.py:." \
    --hidden-import shapely \
    --hidden-import requests \
    fly_mission.py

echo ""
echo "== Done =="
echo "Executable is at: $SCRIPT_DIR/dist/fly_mission"
echo ""
echo "Give this single file to the end user. They should:"
echo "  1. chmod +x fly_mission"
echo "  2. double-click it (or run ./fly_mission --help from a terminal)"
