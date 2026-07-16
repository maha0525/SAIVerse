#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x ".venv/bin/python" ]]; then
  echo "[ERROR] .venv not found. Run setup.sh first." >&2
  exit 1
fi
exec .venv/bin/python scripts/update_engine.py --manual
