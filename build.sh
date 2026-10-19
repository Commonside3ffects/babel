#!/usr/bin/env bash
# Build the single-file Babel binary into dist/babel.
# Needs PyInstaller: python3 -m venv --system-site-packages .build-venv && .build-venv/bin/pip install pyinstaller
set -euo pipefail
cd "$(dirname "$0")"
A=$(pwd)
args=()
for f in babel.svg chevron-down.svg chevron-down-accent.svg window-close.svg open-menu.svg dice.svg refresh.svg; do
    args+=(--add-data "$A/$f:.")
done
.build-venv/bin/python -m PyInstaller --noconfirm --clean --onefile --name babel "${args[@]}" \
    --hidden-import PyQt6.QtSvg --exclude-module tkinter --exclude-module PyQt6.QtWebEngineCore \
    --exclude-module PyQt6.QtMultimedia --exclude-module PyQt6.Qt3DCore \
    --distpath "$A/dist" --workpath "$A/build/work" --specpath "$A/build" "$A/babel.py"
echo "Built: $A/dist/babel"
