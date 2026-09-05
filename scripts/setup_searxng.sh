#!/usr/bin/env bash
set -euo pipefail

# SearXNG setup script (idempotent).
# Clones the SearXNG source, creates a dedicated venv, installs dependencies,
# and prepares settings / limiter configuration.
#
# This script is called from setup.sh during initial setup
# and also from run_searxng_server.sh as a fallback.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT=${SEARXNG_PORT:-8080}
BIND_ADDRESS=${SEARXNG_BIND_ADDRESS:-0.0.0.0}
SRC_DIR=${SEARXNG_SRC_DIR:-${SCRIPT_DIR}/.searxng-src}
VENV_DIR=${SEARXNG_VENV_DIR:-${SCRIPT_DIR}/.searxng-venv}
SETTINGS_PATH=${SEARXNG_SETTINGS_PATH:-${SCRIPT_DIR}/searxng_settings.yml}
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

  if command -v git &>/dev/null; then
    echo "[INFO] Cloning SearXNG source into ${SRC_DIR} (ref=${BRANCH_OR_TAG})" >&2
    git clone --depth 1 --branch "${BRANCH_OR_TAG}" https://github.com/searxng/searxng.git "${SRC_DIR}"
  else
    echo "[INFO] git not found. Downloading SearXNG source archive..." >&2
    python "${SCRIPT_DIR}/download_searxng_source.py" "${SRC_DIR}" --ref "${BRANCH_OR_TAG}"
  fi
}

setup_venv() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    if "${VENV_DIR}/bin/python" - <<'PY'
import importlib.util
import sys

sys.exit(0 if importlib.util.find_spec("searx") else 1)
PY
    then
      return
    fi
    echo "[INFO] Existing virtualenv found but SearXNG is not installed. Reinstalling..." >&2
  else
    echo "[INFO] Creating virtualenv at ${VENV_DIR}" >&2
    python -m venv "${VENV_DIR}"
  fi

  "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
  echo "[INFO] Installing SearXNG runtime dependencies" >&2
  "${VENV_DIR}/bin/pip" install -r "${SRC_DIR}/requirements.txt"
  "${VENV_DIR}/bin/pip" install --no-build-isolation -e "${SRC_DIR}"
}

prepare_settings() {
  # 3-layer merge: SearXNG base + SAIVerse defaults + user overrides
  echo "[INFO] Generating SearXNG settings..." >&2
  "$(python_bin)" "${SCRIPT_DIR}/merge_searxng_settings.py"
}

prepare_limiter() {
  local limiter_path=${SEARXNG_LIMITER_PATH:-${SCRIPT_DIR}/limiter.toml}
  echo "[INFO] Regenerating limiter configuration at ${limiter_path}" >&2

  "$(python_bin)" - "${limiter_path}" "${SRC_DIR}" <<'PY'
import sys
from pathlib import Path
import shutil
import importlib.resources as resources

dest = Path(sys.argv[1])
src_dir = Path(sys.argv[2])
dest.parent.mkdir(parents=True, exist_ok=True)

def copy_candidate(path: Path) -> bool:
    if path.is_file():
        shutil.copyfile(path, dest)
        return True
    return False

# Prefer the installed package template to match the runtime schema exactly.
try:
    package_template = resources.files("searx").joinpath("limiter.toml")
except Exception:
    package_template = None

if package_template and copy_candidate(Path(package_template)):
    sys.exit(0)

# Fallback to the cloned source tree if available.
if copy_candidate(src_dir / "searx" / "limiter.toml"):
    sys.exit(0)

# Last resort: minimal valid config matching the current schema.
dest.write_text("""[botdetection]\n\nipv4_prefix = 32\nipv6_prefix = 48\n\ntrusted_proxies = [\n  '127.0.0.0/8',\n  '::1',\n]\n\n[botdetection.ip_limit]\nfilter_link_local = false\nlink_token = false\n\n[botdetection.ip_lists]\nblock_ip = [\n]\npass_ip = [\n]\npass_searxng_org = true\n""", encoding="utf-8")
PY
}

echo "[INFO] Setting up SearXNG..." >&2
setup_source
setup_venv
prepare_settings
prepare_limiter
echo "[OK] SearXNG setup complete" >&2
