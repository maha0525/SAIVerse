#!/usr/bin/env bash
set -euo pipefail

# Launch a local SearXNG server.
# Setup is handled by setup_searxng.sh (called from setup.sh).
# If setup hasn't been done yet, this script runs it automatically.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT=${SEARXNG_PORT:-8080}
BIND_ADDRESS=${SEARXNG_BIND_ADDRESS:-0.0.0.0}
VENV_DIR=${SEARXNG_VENV_DIR:-${SCRIPT_DIR}/.searxng-venv}
SETTINGS_PATH=${SEARXNG_SETTINGS_PATH:-${SCRIPT_DIR}/searxng_settings.yml}

# The installer needs only this checkout and the standard library (certifi,
# when present, serves HTTPS trust via saiverse.tls_trust) and runs with
# SAIVerse's own .venv python, as setup.sh and the update run it: it then checks the SearXNG venv
# against the Python series SAIVerse runs, and makes it again when SAIVerse
# moved to another series. A SearXNG setup that stopped part-way
# (.setup_in_progress/searxng-venv.json, scripts/setup_in_progress.py) is
# finished the same way. Same as run_searxng_server.ps1; see
# docs/intent/dependency_management.md section 2-7.
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
saiverse_python="${WORKSPACE_ROOT}/.venv/bin/python"
bootstrap_python=python
if [[ -x "${saiverse_python}" ]]; then
    bootstrap_python="${saiverse_python}"
elif [[ -x "${VENV_DIR}/bin/python" ]]; then
    bootstrap_python="${VENV_DIR}/bin/python"
fi
SRC_DIR="$("${bootstrap_python}" "${SCRIPT_DIR}/searxng_install.py" source)"
export SEARXNG_SRC_DIR="${SRC_DIR}"

if ! "${bootstrap_python}" "${SCRIPT_DIR}/searxng_install.py" check; then
    echo "[INFO] Preparing the pinned SearXNG release..." >&2
    "${bootstrap_python}" "${SCRIPT_DIR}/searxng_install.py" setup
fi

python_bin="$("${bootstrap_python}" "${SCRIPT_DIR}/searxng_install.py" python)"
SETTINGS_PATH="$("${bootstrap_python}" "${SCRIPT_DIR}/searxng_install.py" settings)"

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
