#!/usr/bin/env bash
# Thin wrapper around scripts/snapshot.py.
# Usage: ./snapshot.sh {save|list|restore|inspect|delete} [args...]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
elif [ -f ".venv/Scripts/activate" ]; then
    # Windows-style venv path (Git Bash on Windows)
    # shellcheck disable=SC1091
    source ".venv/Scripts/activate"
fi

python "scripts/snapshot.py" "$@"
