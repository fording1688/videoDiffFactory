#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --onefile --name VideoVariantStudio \
  --add-data "static:static" \
  --add-data "runtime:runtime" \
  app/launcher.py
printf "\nBuild done: dist/VideoVariantStudio\n"
