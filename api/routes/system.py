"""System-level API endpoints: version check, update trigger."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from fastapi import APIRouter, HTTPException

from saiverse import app_state

router = APIRouter()
LOGGER = logging.getLogger(__name__)

# --- GitHub release cache ---
_GITHUB_REPO = "maha0525/SAIVerse"
_CACHE_TTL = 3600  # 1 hour
_cached_latest: Optional[dict] = None
_cached_at: float = 0.0


def _compare_versions(current: str, latest: str) -> bool:
    """Return True if latest > current using tuple comparison.

    Handles versions like '0.1.6' and '0.1.10' correctly.
    """
    def _parse(v: str) -> tuple:
        v = v.lstrip("v")
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    return _parse(latest) > _parse(current)


def _fetch_latest_release() -> Optional[dict]:
    """Fetch latest release info from GitHub API.

    Returns dict with tag_name, html_url, or None on failure.
    Caches result for _CACHE_TTL seconds.
    """
    global _cached_latest, _cached_at

    now = time.time()
    if _cached_latest is not None and (now - _cached_at) < _CACHE_TTL:
        return _cached_latest

    url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
    req = Request(url, headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "SAIVerse"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            result = {
                "tag_name": data.get("tag_name", ""),
                "html_url": data.get("html_url", ""),
                "name": data.get("name", ""),
                "published_at": data.get("published_at", ""),
            }
            _cached_latest = result
            _cached_at = now
            return result
    except (URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        LOGGER.warning("Failed to fetch latest release from GitHub: %s", exc)
        return None


@router.get("/version")
async def get_version():
    """Return current version and check for updates."""
    current = app_state.version

    release = _fetch_latest_release()
    if release:
        latest_tag = release["tag_name"].lstrip("v")
        update_available = _compare_versions(current, latest_tag)
        return {
            "version": current,
            "latest_version": latest_tag,
            "update_available": update_available,
            "latest_release_url": release["html_url"],
            "release_name": release["name"],
            "checked_at": _cached_at,
        }

    return {
        "version": current,
        "latest_version": None,
        "update_available": None,
        "latest_release_url": None,
        "release_name": None,
        "checked_at": None,
    }


@router.post("/update")
async def trigger_update():
    """Trigger a self-update: spawn detached updater, then shutdown."""
    project_dir = app_state.project_dir
    if not project_dir:
        raise HTTPException(status_code=500, detail="Project directory not set")

    project_path = Path(project_dir)
    config_path = project_path / ".update_config.json"
    updater_script = project_path / "scripts" / "self_update.py"

    if not updater_script.exists():
        raise HTTPException(status_code=500, detail="Update script not found")

    # Detect environment
    has_git = shutil.which("git") is not None and (project_path / ".git").is_dir()
    manager = app_state.manager
    backend_port = manager.ui_port if manager else 8000

    # Determine venv python path
    if sys.platform == "win32":
        venv_python = str(project_path / ".venv" / "Scripts" / "python.exe")
    else:
        venv_python = str(project_path / ".venv" / "bin" / "python")

    # Write update config
    config = {
        "project_dir": str(project_path),
        "city_name": app_state.city_name,
        "backend_port": backend_port,
        "frontend_port": 3000,
        "main_pid": os.getpid(),
        "venv_python": venv_python,
        "has_git": has_git,
        "platform": sys.platform,
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    LOGGER.info("Written update config to %s", config_path)

    # Spawn detached updater
    cmd = [venv_python, str(updater_script)]
    LOGGER.info("Spawning detached updater: %s", cmd)
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            cmd,
            cwd=str(project_path),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            cmd,
            cwd=str(project_path),
            start_new_session=True,
            close_fds=True,
        )

    LOGGER.info("Update process spawned. Scheduling shutdown in 3 seconds...")

    # Schedule shutdown after response is sent
    async def _delayed_shutdown():
        await asyncio.sleep(3)
        LOGGER.info("Shutting down for update...")
        os._exit(0)

    asyncio.ensure_future(_delayed_shutdown())

    return {"status": "updating"}
