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
  if [[ ! -f "${SETTINGS_PATH}" ]]; then
    echo "[INFO] Preparing settings from ${SRC_DIR}/searx/settings.yml" >&2
    "${VENV_DIR}/bin/pip" show PyYAML >/dev/null 2>&1 || "${VENV_DIR}/bin/pip" install pyyaml
    cp "${SRC_DIR}/searx/settings.yml" "${SETTINGS_PATH}"
  fi
  "$(python_bin)" - "${SETTINGS_PATH}" <<'PY'
import sys
import yaml
from pathlib import Path
import os
import secrets
import re

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

default_secret = "ultrasecretkey"
env_secret = os.getenv("SEARXNG_SECRET_KEY")
if env_secret:
    server["secret_key"] = env_secret
elif not server.get("secret_key") or server.get("secret_key") == default_secret:
    server["secret_key"] = secrets.token_hex(32)

# disable engines that frequently fail without extra deps or network access
problematic_engines = {"ahmia", "torch", "wikidata", "radiobrowser"}

def normalize(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name.lower())

for engine in data.get("engines", []):
    name = engine.get("name")
    if not name:
        continue
    if normalize(name) in problematic_engines:
        engine["disabled"] = True

path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
PY
}

prepare_limiter() {
  local limiter_path=${SEARXNG_LIMITER_PATH:-scripts/limiter.toml}
  local template_path="${SRC_DIR}/searx/limiter.toml"

  echo "[INFO] Regenerating limiter configuration at ${limiter_path}" >&2

  if [[ -f "${template_path}" ]]; then
    cp "${template_path}" "${limiter_path}"
    return
  fi

  echo "[WARN] Limiter template not found at ${template_path}; writing minimal default" >&2
  cat >"${limiter_path}" <<'EOF'
[botdetection]

# The prefix defines the number of leading bits in an address that are compared
# to determine whether or not an address is part of a (client) network.

ipv4_prefix = 32
ipv6_prefix = 48

# If the request IP is in trusted_proxies list, the client IP address is
# extracted from the X-Forwarded-For and X-Real-IP headers. This should be
# used if SearXNG is behind a reverse proxy or load balancer.

trusted_proxies = [
  '127.0.0.0/8',
  '::1',
  # '192.168.0.0/16',
  # '172.16.0.0/12',
  # '10.0.0.0/8',
  # 'fd00::/8',
]

[botdetection.ip_limit]

# To get unlimited access in a local network, by default link-local addresses
# (networks) are not monitored by the ip_limit
filter_link_local = false

# activate link_token method in the ip_limit method
link_token = false

[botdetection.ip_lists]

# In the limiter, the ip_lists method has priority over all other methods -> if
# an IP is in the pass_ip list, it has unrestricted access and it is also not
# checked if e.g. the "user agent" suggests a bot (e.g. curl).

block_ip = [
  # '93.184.216.34',  # IPv4 of example.org
  # '257.1.1.1',      # invalid IP --> will be ignored, logged in ERROR class
]

pass_ip = [
  # '192.168.0.0/16',      # IPv4 private network
  # 'fe80::/10'            # IPv6 linklocal / wins over botdetection.ip_limit.filter_link_local
]

# Activate passlist of (hardcoded) IPs from the SearXNG organization,
# e.g. `check.searx.space`.
pass_searxng_org = true
EOF
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
prepare_limiter
run_server
