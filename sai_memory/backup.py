from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

BACKUP_ROOT = Path.home() / ".saiverse" / "backups" / "saimemory_rdiff"
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)


class BackupError(RuntimeError):
    pass


def _ensure_rdiff_backup(rdiff_path: str | None) -> str:
    candidate = rdiff_path or shutil.which("rdiff-backup")
    if not candidate:
        raise BackupError(
            "rdiff-backup not found. Install it (e.g. `pip install rdiff-backup` or system package) before running backups."
        )
    return candidate


def _sqlite_snapshot(db_path: Path) -> Path:
    if not db_path.exists():
        raise BackupError(f"memory.db not found: {db_path}")
    tmpdir = Path(tempfile.mkdtemp(prefix="saimemory_snapshot_"))
    snapshot_path = tmpdir / "memory.db"
    try:
        with sqlite3.connect(db_path) as src:
            with sqlite3.connect(snapshot_path) as dst:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return tmpdir


def _persona_backup_dir(persona_id: str, root: Path | None = None) -> Path:
    base = Path(root) if root else BACKUP_ROOT
    persona_root = base / persona_id
    persona_root.mkdir(parents=True, exist_ok=True)
    return persona_root


def run_backup(
    *,
    persona_id: str,
    db_path: Path,
    output_root: Path | None = None,
    rdiff_path: str | None = None,
    force_full: bool = False,
) -> Path:
    """Create a snapshot of the persona DB and push it via rdiff-backup.

    Returns the backup repository path.
    """

    repo_dir = _persona_backup_dir(persona_id, output_root)
    rdiff_exec = _ensure_rdiff_backup(rdiff_path)

    if force_full and repo_dir.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archived = repo_dir.parent / f"{repo_dir.name}.archived.{timestamp}"
        LOGGER.info("Rotating existing backup repo for persona=%s to %s", persona_id, archived)
        if archived.exists():
            shutil.rmtree(archived)
        repo_dir.rename(archived)
        repo_dir.mkdir(parents=True, exist_ok=True)

    snapshot_dir = _sqlite_snapshot(db_path)
    try:
        cmd = [
            rdiff_exec,
            "--api-version",
            os.getenv("SAIMEMORY_RDIFF_API_VERSION", "201"),
            "backup",
            "--preserve-numerical-ids",
            str(snapshot_dir),
            str(repo_dir),
        ]
        LOGGER.debug("Running rdiff-backup: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise BackupError(f"rdiff-backup failed (exit {exc.returncode})") from exc
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
    return repo_dir


def latest_backup_path(persona_id: str, output_root: Path | None = None) -> Path:
    return _persona_backup_dir(persona_id, output_root)
