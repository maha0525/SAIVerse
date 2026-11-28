"""Backup management API for Global Settings -> Backup tab.

Provides:
- World snapshot listing, deletion (+ auto fail-closed GC), orphan GC, and
  offline snapshot save / restore via the detached updater worker.
- Per-persona ``memory.db`` backup listing, immediate live backup (via SQLite
  Backup API), deletion, and offline individual restore via the detached worker.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.routes.system import (
    _claim_updater_launch,
    _launch_detached_updater,
    _refuse_without_updater_psutil,
    _release_updater_launch,
    _resolve_updater,
)
from saiverse import app_state
from saiverse.data_paths import get_saiverse_home

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Serializes in-process snapshot delete / GC operations so two concurrent clicks
# cannot race on manifest removal and object collection.
_SNAPSHOT_OP_LOCK = threading.Lock()


class _CreateSnapshotBody(BaseModel):
    name: Optional[str] = None
    note: Optional[str] = None


class _RestoreSnapshotBody(BaseModel):
    confirm: bool = False


class _RestorePersonaMemoryBody(BaseModel):
    filename: str
    confirm: bool = False


def _launch_offline_backup_op(extra_config: dict[str, Any]) -> None:
    project_path, updater_script, venv_python = _resolve_updater()
    _refuse_without_updater_psutil(venv_python)
    _claim_updater_launch()
    try:
        _launch_detached_updater(
            project_path,
            updater_script,
            venv_python,
            extra_config=extra_config,
        )
    except Exception:
        _release_updater_launch()
        raise


# ---- World snapshots -------------------------------------------------------


@router.get("/snapshots")
def get_world_snapshots():
    """Return all world snapshots (CAS v3 + legacy ZIP v2) and disk stats."""
    from scripts import snapshot

    items = snapshot.list_snapshots()
    stats = snapshot.get_store_stats()
    return {
        "snapshots": items,
        "stats": stats,
        "saiverse_version": snapshot.get_saiverse_version(),
    }


@router.post("/snapshots")
def create_world_snapshot(body: _CreateSnapshotBody):
    """Create a manual whole-world snapshot via the detached offline worker."""
    from scripts import snapshot
    from scripts.update_engine import OPERATION_SNAPSHOT_SAVE

    note = (body.note or "").strip() or "Manual snapshot from Backup settings"
    if body.name is not None and body.name.strip():
        name = body.name.strip()
        if not snapshot._is_valid_snapshot_name(name):
            raise HTTPException(
                status_code=400,
                detail="Invalid snapshot name (use only letters, digits, '_', '-', '.').",
            )
        if (snapshot.manifests_dir() / f"{name}.json").exists() or (
            snapshot.snapshots_dir() / f"{name}.zip"
        ).exists():
            raise HTTPException(status_code=409, detail=f"Snapshot '{name}' already exists.")
    else:
        name = datetime.now(timezone.utc).strftime("manual_%Y%m%d_%H%M%S_%f")

    _launch_offline_backup_op(
        {
            "operation": OPERATION_SNAPSHOT_SAVE,
            "snapshot_name": name,
            "snapshot_note": note,
        }
    )
    return {
        "status": "running",
        "operation": OPERATION_SNAPSHOT_SAVE,
        "snapshot_name": name,
    }


@router.post("/snapshots/gc")
def gc_world_snapshots():
    """Reclaim unreferenced CAS objects and stale temporary files (fail-closed)."""
    from scripts import snapshot

    with _SNAPSHOT_OP_LOCK:
        try:
            result = snapshot.gc_store()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Snapshot GC failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        stats = snapshot.get_store_stats()
    return {"gc": result, "stats": stats}


@router.delete("/snapshots/{name}")
def remove_world_snapshot(name: str):
    """Delete one world snapshot and automatically reclaim unreferenced CAS objects."""
    from scripts import snapshot

    with _SNAPSHOT_OP_LOCK:
        try:
            result = snapshot.delete_snapshot(name, run_gc=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except snapshot.SnapshotStoreBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Failed to delete snapshot %s", name)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        stats = snapshot.get_store_stats()
    return {"deleted": result, "stats": stats}


@router.post("/snapshots/{name}/restore")
def restore_world_snapshot(name: str, body: _RestoreSnapshotBody):
    """Restore the whole world from ``name`` via the detached offline worker."""
    from scripts import snapshot
    from scripts.update_engine import OPERATION_SNAPSHOT_RESTORE

    if not body.confirm:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required to restore.")

    try:
        fmt, snap_path = snapshot.resolve_snapshot(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    meta = snapshot.read_snapshot_metadata(snap_path)
    if meta is None:
        raise HTTPException(
            status_code=400,
            detail=f"Snapshot '{name}' metadata is unreadable; refusing to restore.",
        )

    _launch_offline_backup_op(
        {
            "operation": OPERATION_SNAPSHOT_RESTORE,
            "snapshot_name": name,
        }
    )
    return {
        "status": "running",
        "operation": OPERATION_SNAPSHOT_RESTORE,
        "snapshot_name": name,
        "format": fmt,
    }


# ---- Persona memory.db backups ---------------------------------------------


def _load_registered_personas() -> dict[str, dict[str, Any]]:
    """Return ``{persona_id: {display_name, avatar_image}}`` from the main DB."""
    registered: dict[str, dict[str, Any]] = {}
    manager = app_state.manager
    if manager is None or not hasattr(manager, "SessionLocal"):
        return registered
    try:
        from database.models import AI

        db = manager.SessionLocal()
        try:
            for row in db.query(AI.AIID, AI.AINAME, AI.AVATAR_IMAGE).all():
                registered[str(row.AIID)] = {
                    "display_name": str(row.AINAME or row.AIID),
                    "avatar_image": row.AVATAR_IMAGE,
                }
        finally:
            db.close()
    except Exception:
        LOGGER.warning("Could not query AI table for backup listing", exc_info=True)
    return registered


@router.get("/personas")
def list_backup_personas():
    """List personas with their live ``memory.db`` size and backup counts."""
    from sai_memory.backup import (
        get_backup_root,
        get_simple_backup_root,
        list_simple_backups,
    )

    home = get_saiverse_home()
    personas_root = home / "personas"
    simple_root = get_simple_backup_root()
    rdiff_root = get_backup_root()

    registered = _load_registered_personas()
    persona_ids: set[str] = set(registered.keys())
    if personas_root.is_dir():
        for child in personas_root.iterdir():
            if child.is_dir() and (child / "memory.db").is_file():
                persona_ids.add(child.name)
    if simple_root.is_dir():
        for child in simple_root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                persona_ids.add(child.name)

    items: list[dict[str, Any]] = []
    for pid in sorted(persona_ids):
        try:
            backups = list_simple_backups(pid)
        except ValueError:
            continue
        db_path = personas_root / pid / "memory.db"
        has_db = db_path.is_file()
        db_size = db_path.stat().st_size if has_db else 0
        has_rdiff = (rdiff_root / pid / "rdiff-backup-data").is_dir()
        reg = registered.get(pid)
        if not reg and not has_db and not backups:
            continue
        items.append({
            "persona_id": pid,
            "display_name": reg["display_name"] if reg else pid,
            "registered": reg is not None,
            "has_memory_db": has_db,
            "memory_db_size_bytes": db_size,
            "backup_count": len(backups),
            "latest_backup_at": backups[0]["created_at"] if backups else None,
            "has_rdiff_backup": has_rdiff,
        })

    items.sort(key=lambda x: (not x["registered"], x["display_name"].lower(), x["persona_id"]))
    return {"personas": items}


@router.get("/personas/{persona_id}")
def get_persona_memory_backups(persona_id: str):
    """List ``memory.db`` simple backups for one persona."""
    from sai_memory.backup import (
        _validate_persona_id,
        get_backup_root,
        list_simple_backups,
    )

    try:
        safe_id = _validate_persona_id(persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    home = get_saiverse_home()
    db_path = home / "personas" / safe_id / "memory.db"
    has_db = db_path.is_file()
    backups = list_simple_backups(safe_id)
    has_rdiff = (get_backup_root() / safe_id / "rdiff-backup-data").is_dir()
    return {
        "persona_id": safe_id,
        "has_memory_db": has_db,
        "memory_db_size_bytes": db_path.stat().st_size if has_db else 0,
        "backups": backups,
        "has_rdiff_backup": has_rdiff,
    }


@router.post("/personas/{persona_id}")
def create_persona_memory_backup(persona_id: str):
    """Create an immediate simple backup of one persona's ``memory.db`` (live-safe)."""
    from sai_memory.backup import (
        BackupError,
        _validate_persona_id,
        list_simple_backups,
        run_simple_backup,
    )

    try:
        safe_id = _validate_persona_id(persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db_path = get_saiverse_home() / "personas" / safe_id / "memory.db"
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail=f"memory.db not found for persona '{safe_id}'.")

    try:
        created = run_simple_backup(
            persona_id=safe_id,
            db_path=db_path,
            skip_if_unchanged=False,
        )
    except BackupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "persona_id": safe_id,
        "created": created.name if created else None,
        "backups": list_simple_backups(safe_id),
    }


@router.delete("/personas/{persona_id}/{filename}")
def remove_persona_memory_backup(persona_id: str, filename: str):
    """Delete one simple backup file for a persona."""
    from sai_memory.backup import delete_simple_backup, list_simple_backups

    try:
        reclaimed = delete_simple_backup(persona_id, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Failed to delete persona backup %s/%s", persona_id, filename)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "persona_id": persona_id,
        "deleted": filename,
        "reclaimed_bytes": reclaimed,
        "backups": list_simple_backups(persona_id),
    }


@router.post("/personas/{persona_id}/restore")
def restore_persona_memory(persona_id: str, body: _RestorePersonaMemoryBody):
    """Restore one persona's ``memory.db`` via the detached offline worker."""
    from sai_memory.backup import (
        _check_sqlite_integrity,
        _resolve_simple_backup_path,
    )
    from scripts.update_engine import OPERATION_PERSONA_MEMORY_RESTORE

    if not body.confirm:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required to restore.")

    try:
        backup_path = _resolve_simple_backup_path(persona_id, body.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not backup_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Backup '{body.filename}' not found for persona '{persona_id}'.",
        )
    if not _check_sqlite_integrity(backup_path):
        raise HTTPException(
            status_code=400,
            detail=f"Backup '{body.filename}' failed SQLite integrity check; refusing to restore.",
        )

    _launch_offline_backup_op(
        {
            "operation": OPERATION_PERSONA_MEMORY_RESTORE,
            "persona_id": persona_id,
            "backup_file": body.filename,
        }
    )
    return {
        "status": "running",
        "operation": OPERATION_PERSONA_MEMORY_RESTORE,
        "persona_id": persona_id,
        "backup_file": body.filename,
    }
