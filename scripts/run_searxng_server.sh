#!/usr/bin/env bash
set -euo pipefail

# Launch a real SearXNG server locally without Docker.
# 初回実行時に SearXNG のソースをクローンし、専用 venv に依存を入れてから
# ローカルで `/search` エンドポイントを提供します。

PORT=${SEARXNG_PORT:-8080}
BIND_ADDRESS=${SEARXNG_BIND_ADDRESS:-0.0.0.0}
SRC_DIR=${SEARXNG_SRC_DIR:-scripts/.searxng-src}
VENV_DIR=${SEARXNG_VENV_DIR:-scripts/.searxng-venv}
SETTINGS_PATH=${SEARXNG_SETTINGS_PATH:-scripts/searxng_settings.yml}
BRANCH_OR_TAG=${SEARXNG_REF:-master}

python_bin() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    echo "${VENV_DIR}/bin/python"
  else
    echo "python"
  fi
}

setup_source() {
  if [[ -d "${SRC_DIR}" ]]; then
    return
  fi
  echo "[INFO] Cloning SearXNG source into ${SRC_DIR} (ref=${BRANCH_OR_TAG})" >&2
  git clone --depth 1 --branch "${BRANCH_OR_TAG}" https://github.com/searxng/searxng.git "${SRC_DIR}"
}

setup_venv() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    return
  fi
  echo "[INFO] Creating virtualenv at ${VENV_DIR}" >&2
  python -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
  echo "[INFO] Installing SearXNG runtime dependencies" >&2
  "${VENV_DIR}/bin/pip" install -r "${SRC_DIR}/requirements.txt"
  "${VENV_DIR}/bin/pip" install --no-build-isolation -e "${SRC_DIR}"
}

prepare_settings() {
  if [[ -f "${SETTINGS_PATH}" ]]; then
    return
  fi
  echo "[INFO] Preparing settings from ${SRC_DIR}/searx/settings.yml" >&2
  "${VENV_DIR}/bin/pip" show PyYAML >/dev/null 2>&1 || "${VENV_DIR}/bin/pip" install pyyaml
  cp "${SRC_DIR}/searx/settings.yml" "${SETTINGS_PATH}"
  "$(python_bin)" - "${SETTINGS_PATH}" <<'PY'
import sys
import yaml
from pathlib import Path

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text())

# allow JSON output by default
search = data.setdefault("search", {})
formats = search.get("formats") or []
if "json" not in formats:
    formats.append("json")
search["formats"] = formats
search.setdefault("safe_search", 1)

# bind on all interfaces for local network use
server = data.setdefault("server", {})
if server.get("bind_address") == "127.0.0.1":
    server["bind_address"] = "0.0.0.0"

path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
PY
}

run_server() {
  export SEARXNG_SETTINGS_PATH="${SETTINGS_PATH}"
  export SEARXNG_PORT="${PORT}"
  export SEARXNG_BIND_ADDRESS="${BIND_ADDRESS}"
  echo "[INFO] Starting SearXNG at http://${BIND_ADDRESS}:${PORT}" >&2
  exec "$(python_bin)" -m searx.webapp
}

setup_source
setup_venv
prepare_settings
run_server
