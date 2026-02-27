"""System-level API endpoints: version check, update trigger, announcements."""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
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

# --- Announcements cache ---
_ANNOUNCEMENTS_GIST_URL: str = os.environ.get(
    "SAIVERSE_ANNOUNCEMENTS_URL",
    "https://gist.githubusercontent.com/maha0525/5e4c1aacf9d9550c5a46ca9d847ae559/raw/saiverse_announcements.json",
)
_ANNOUNCEMENTS_CACHE_TTL = 1800  # 30 minutes
_cached_announcements: Optional[dict] = None
_cached_announcements_at: float = 0.0


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


def _fetch_announcements() -> Optional[dict]:
    """Fetch announcements JSON from GitHub Gist.

    Returns the parsed JSON dict, or None on failure.
    Caches result for _ANNOUNCEMENTS_CACHE_TTL seconds.
    """
    global _cached_announcements, _cached_announcements_at

    now = time.time()
    if _cached_announcements is not None and (now - _cached_announcements_at) < _ANNOUNCEMENTS_CACHE_TTL:
        return _cached_announcements

    # Append timestamp to bypass GitHub CDN cache (max-age=300)
    bust = f"{'&' if '?' in _ANNOUNCEMENTS_GIST_URL else '?'}t={int(now)}"
    req = Request(_ANNOUNCEMENTS_GIST_URL + bust, headers={"User-Agent": "SAIVerse"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _cached_announcements = data
            _cached_announcements_at = now
            return data
    except (URLError, OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Failed to fetch announcements from Gist: %s", exc)
        return _cached_announcements  # return stale cache if available


@router.get("/announcements")
async def get_announcements():
    """Return announcements from the configured Gist."""
    data = _fetch_announcements()
    if data is None:
        return {"announcements": []}
    return data


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
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        try:
            subprocess.Popen(
                cmd,
                cwd=str(project_path),
                creationflags=flags,
                close_fds=True,
            )
        except OSError:
            # Job Object may not allow breakaway; fall back without it
            LOGGER.warning("CREATE_BREAKAWAY_FROM_JOB failed, retrying without it")
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

    # Use threading.Timer instead of asyncio to guarantee shutdown
    # even if the event loop is blocked by long-running synchronous tasks.
    def _force_exit():
        LOGGER.info("Shutting down for update...")
        # Run manager shutdown to save building histories, session metadata, etc.
        # os._exit() bypasses all Python cleanup, so we must do this explicitly.
        try:
            from saiverse.app_state import manager
            if manager is not None:
                LOGGER.info("Running manager shutdown before update exit...")
                manager.shutdown()
        except Exception as e:
            LOGGER.error("Failed to run shutdown before update exit: %s", e, exc_info=True)

        # Kill child processes (e.g., api_server) that os._exit won't clean up.
        for proc in app_state.child_processes:
            try:
                if proc.poll() is None:
                    LOGGER.info("Terminating child process PID %d", proc.pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except OSError as e:
                LOGGER.warning("Failed to terminate child process: %s", e)
        os._exit(0)

    timer = threading.Timer(3.0, _force_exit)
    timer.daemon = True
    timer.start()

    return {"status": "updating"}
