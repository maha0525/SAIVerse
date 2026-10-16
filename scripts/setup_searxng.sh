#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Same installer and pin as setup_searxng.ps1.
exec python "${SCRIPT_DIR}/searxng_install.py" setup
