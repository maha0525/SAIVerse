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
from pydantic import BaseModel

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


@router.get("/alerts")
async def get_system_alerts():
    """Return system-level alerts populated during startup.

    Includes critical events like corrupted log.json files that were rescued.
    The frontend displays these as a prominent banner so the user is never
    surprised by silent data loss.
    """
    manager = app_state.manager
    if manager is None or not hasattr(manager, "startup_alerts"):
        return {"alerts": []}
    return {"alerts": list(manager.startup_alerts)}


def _persist_persona_state(manager) -> None:
    """Trigger save of conscious_log.json for each persona.

    Called after restore/reset clamps in-memory pulse_cursors so the changes
    survive a crash. If skipped, on crash the next startup would re-clamp
    from disk anyway, but we want the saved state to immediately reflect
    the user's explicit recovery action.
    """
    personas = getattr(manager, "personas", None) or {}
    for persona in personas.values():
        save = getattr(persona, "_save_conscious_log", None)
        if callable(save):
            try:
                save()
            except Exception:
                LOGGER.warning(
                    "Failed to persist conscious_log for persona=%s",
                    getattr(persona, "persona_id", "?"),
                    exc_info=True,
                )


@router.get("/quarantine")
async def list_quarantined_buildings():
    """Return all buildings currently quarantined due to log corruption.

    Each entry includes restoration options (available backups, corrupted file
    location). The UI uses this to populate the quarantine management modal.
    """
    manager = app_state.manager
    if manager is None or not hasattr(manager, "quarantined_buildings"):
        return {"quarantined": []}
    items = []
    for b_id, info in manager.quarantined_buildings.items():
        building_name = b_id
        if hasattr(manager, "building_map") and b_id in manager.building_map:
            building_name = getattr(manager.building_map[b_id], "name", b_id)
        items.append({**info, "building_name": building_name})
    return {"quarantined": items}


class _QuarantineRestoreBody(BaseModel):
    backup_filename: str  # full path or just filename within building dir


class _QuarantineActionResponse(BaseModel):
    success: bool
    message: str


@router.post("/quarantine/{building_id}/restore", response_model=_QuarantineActionResponse)
async def restore_quarantined_building(building_id: str, body: _QuarantineRestoreBody):
    """Restore a quarantined building from a chosen backup file.

    The selected backup is copied to ``log.json`` (replacing whatever is
    there), the building is removed from quarantine, and its history is
    re-loaded into memory.
    """
    manager = app_state.manager
    if manager is None or not hasattr(manager, "quarantined_buildings"):
        raise HTTPException(status_code=503, detail="Manager not ready")
    if building_id not in manager.quarantined_buildings:
        raise HTTPException(status_code=404, detail=f"Building {building_id} is not quarantined")

    info = manager.quarantined_buildings[building_id]
    available = set(info.get("available_backups", []))
    backup_path = Path(body.backup_filename)
    # Allow either full path (from list_log_backups) or just filename
    if not backup_path.is_absolute():
        original_path = Path(info["original_path"])
        backup_path = original_path.parent / backup_path.name
    if str(backup_path) not in available and backup_path.name not in {Path(p).name for p in available}:
        raise HTTPException(
            status_code=400,
            detail=f"Backup '{body.backup_filename}' is not in the available backup list",
        )
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail=f"Backup file does not exist: {backup_path}")

    target_path = Path(info["original_path"])
    try:
        # Validate backup is parseable JSON before restoring
        data = json.loads(backup_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise HTTPException(
                status_code=400,
                detail=f"Backup is not a valid history (expected list, got {type(data).__name__})",
            )
        # Atomic restore: copy backup to log.json
        shutil.copy2(backup_path, target_path)
        # Re-load into memory
        manager.building_histories[building_id] = data
        # Remove from quarantine
        del manager.quarantined_buildings[building_id]
        # Remove related startup alerts
        manager.startup_alerts = [
            a for a in manager.startup_alerts
            if a.get("details", {}).get("building_id") != building_id
        ]
        # Clamp persona pulse_cursors so they don't skip new messages.
        # The restored data may have a different (smaller) seq range than
        # what personas remember from before.
        max_seq = max((int(m.get("seq", 0)) for m in data), default=0)
        manager.clamp_persona_cursors_for_building(building_id, max_seq)
        # Reset seq counter so new messages get seq > max_seq (preventing
        # collision with restored seqs and the "low seq < cursor → skip" bug).
        manager.reset_persona_seq_counters_for_building(building_id, max_seq + 1)
        _persist_persona_state(manager)
        LOGGER.info(
            "Restored quarantined building %s from backup %s (%d messages, max_seq=%d)",
            building_id, backup_path, len(data), max_seq,
        )
        return {"success": True, "message": f"復元完了: {len(data)}件のメッセージを読み込みました"}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Backup file is not valid JSON: {exc}")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to restore backup: {exc}")


@router.post("/quarantine/{building_id}/reset", response_model=_QuarantineActionResponse)
async def reset_quarantined_building(building_id: str):
    """Reset a quarantined building to empty history (fresh start).

    The original corrupted file remains preserved at its ``.corrupted_*``
    location for manual recovery later. A new empty ``log.json`` is created.
    """
    manager = app_state.manager
    if manager is None or not hasattr(manager, "quarantined_buildings"):
        raise HTTPException(status_code=503, detail="Manager not ready")
    if building_id not in manager.quarantined_buildings:
        raise HTTPException(status_code=404, detail=f"Building {building_id} is not quarantined")

    info = manager.quarantined_buildings[building_id]
    target_path = Path(info["original_path"])
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write of empty list
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write("[]")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)

        manager.building_histories[building_id] = []
        del manager.quarantined_buildings[building_id]
        manager.startup_alerts = [
            a for a in manager.startup_alerts
            if a.get("details", {}).get("building_id") != building_id
        ]
        # Reset persona cursors to 0 since the building log is now empty.
        # Without this, personas with cursor > 0 would skip the next messages.
        manager.clamp_persona_cursors_for_building(building_id, 0)
        # Counter back to 1 so new messages start fresh from seq=1.
        manager.reset_persona_seq_counters_for_building(building_id, 1)
        _persist_persona_state(manager)
        LOGGER.info(
            "Reset quarantined building %s to empty history (corrupted file preserved at %s)",
            building_id, info.get("corrupted_path"),
        )
        return {
            "success": True,
            "message": (
                f"リセット完了: 空履歴で再開。破損ファイルは {info.get('corrupted_path')} に保持。"
            ),
        }
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to reset: {exc}")


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
