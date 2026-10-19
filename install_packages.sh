#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing standalone .venv for CLI / fallback execution..."
python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
"$SCRIPT_DIR/.venv/bin/pip" install --only-binary=:all: -r "$SCRIPT_DIR/requirements.txt"

echo "Installing Python 3.14 (cp314) wheels into packages/ for Fusion 360 in-process execution..."
"$SCRIPT_DIR/.venv/bin/pip" install \
  --target "$SCRIPT_DIR/packages" \
  --platform macosx_11_0_arm64 \
  --python-version 3.14 \
  --implementation cp \
  --only-binary=:all: \
  --no-compile \
  --upgrade \
  -r "$SCRIPT_DIR/requirements.txt"

echo "Done! Dependencies installed in $SCRIPT_DIR/packages and $SCRIPT_DIR/.venv"
