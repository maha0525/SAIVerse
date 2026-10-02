#!/usr/bin/env bash
set -euo pipefail

# Launch a local SearXNG server.
# Setup is handled by setup_searxng.sh (called from setup.sh).
# If setup hasn't been done yet, this script runs it automatically.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT=${SEARXNG_PORT:-8080}
BIND_ADDRESS=${SEARXNG_BIND_ADDRESS:-0.0.0.0}
SRC_DIR=${SEARXNG_SRC_DIR:-${SCRIPT_DIR}/.searxng-src}
VENV_DIR=${SEARXNG_VENV_DIR:-${SCRIPT_DIR}/.searxng-venv}
SETTINGS_PATH=${SEARXNG_SETTINGS_PATH:-${SCRIPT_DIR}/searxng_settings.yml}

# Run setup if not done yet
if [[ ! -d "${SRC_DIR}" ]] || [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "[INFO] SearXNG is not set up. Running setup..." >&2
    bash "${SCRIPT_DIR}/setup_searxng.sh"
fi

python_bin="${VENV_DIR}/bin/python"

# Re-merge settings (picks up SAIVerse default changes + user overrides)
echo "[INFO] Merging SearXNG settings..." >&2
"${python_bin}" "${SCRIPT_DIR}/merge_searxng_settings.py"

# SearXNG asks git for its version at start-up and dies when git cannot be
# found. Let it start without git. (run_searxng_server.ps1 does the same.)
"${python_bin}" "${SCRIPT_DIR}/ensure_searxng_version.py" "${SRC_DIR}"

export SEARXNG_SETTINGS_PATH="${SETTINGS_PATH}"
export SEARXNG_PORT="${PORT}"
export SEARXNG_BIND_ADDRESS="${BIND_ADDRESS}"
export FLASK_SKIP_DOTENV=1

echo "[INFO] Starting SearXNG at http://${BIND_ADDRESS}:${PORT}" >&2
exec "${python_bin}" -m searx.webapp
