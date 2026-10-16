"""Shared source/version selection for SearXNG setup, settings and launchers."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent


def pinned_release() -> dict[str, str]:
    pin = json.loads((SCRIPT_DIR / "searxng_pin.json").read_text(encoding="utf-8"))
    requested = os.environ.get("SEARXNG_REF") or pin["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", requested):
        raise ValueError("SEARXNG_REF must be a full 40-character commit SHA; branches/tags are not supported")
    if requested != pin["commit"]:
        pin = {**pin, "commit": requested, "version": f"0.0.0+{requested}"}
    return pin


def configured_path(variable: str, default: Path) -> Path:
    path = Path(os.environ.get(variable) or default)
    return (path if path.is_absolute() else WORKSPACE_ROOT / path).resolve()


def source_directory() -> Path:
    return configured_path("SEARXNG_SRC_DIR", SCRIPT_DIR / ".searxng-src" / pinned_release()["commit"])


def venv_directory() -> Path:
    return configured_path("SEARXNG_VENV_DIR", SCRIPT_DIR / ".searxng-venv")


def venv_python() -> Path:
    return venv_directory() / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def settings_path() -> Path:
    return configured_path("SEARXNG_SETTINGS_PATH", SCRIPT_DIR / "searxng_settings.yml")
