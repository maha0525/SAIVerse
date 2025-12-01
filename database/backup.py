"""Simple backup utility for saiverse.db with rotation and cleanup."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DEFAULT_BACKUP_KEEP = 10


def _auto_backup_enabled() -> bool:
    """Check if auto-backup on startup is enabled via environment variable."""
    value = os.getenv("SAIVERSE_DB_BACKUP_ON_START", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _get_backup_keep_count() -> int:
    """Get number of backups to keep from environment variable."""
    try:
        return max(1, int(os.getenv("SAIVERSE_DB_BACKUP_KEEP", str(DEFAULT_BACKUP_KEEP))))
    except ValueError:
        return DEFAULT_BACKUP_KEEP


def backup_saiverse_db(
    db_path: Path,
    keep_count: int | None = None,
) -> Path | None:
    """Create a timestamped backup of saiverse.db and prune old backups.

    Args:
        db_path: Path to saiverse.db
        keep_count: Number of recent backups to keep (default: from env or 10)

    Returns:
        Path to created backup, or None if backup was skipped

    Raises:
        RuntimeError: If backup fails
    """
    if not db_path.exists():
        LOGGER.warning("Database not found, skipping backup: %s", db_path)
        return None

    if keep_count is None:
        keep_count = _get_backup_keep_count()

    backup_dir = db_path.parent
    # Include microseconds to avoid collisions when creating multiple backups quickly
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]  # YYYYmmdd_HHMMSS_mmm (milliseconds)
    backup_path = backup_dir / f"{db_path.name}_backup_{timestamp}.bak"

    try:
        # Create SQLite backup (safe even if DB is in use)
        LOGGER.info("Creating backup: %s", backup_path)
        with sqlite3.connect(db_path) as src:
            with sqlite3.connect(backup_path) as dst:
                src.backup(dst)
                # Ensure backup is cleanly closed
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        LOGGER.info("✓ Backup created: %s (size: %.1f KB)", backup_path, backup_path.stat().st_size / 1024)

        # Prune old backups
        _prune_old_backups(db_path, keep_count)

        return backup_path

    except Exception as exc:
        LOGGER.error("Failed to create backup: %s", exc)
        # Clean up partial backup if it exists
        if backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Backup failed: {exc}") from exc


def _prune_old_backups(db_path: Path, keep_count: int) -> None:
    """Remove old backups, keeping only the most recent N backups.

    Args:
        db_path: Path to the main database
        keep_count: Number of recent backups to keep
    """
    backup_dir = db_path.parent
    pattern = f"{db_path.name}_backup_*.bak"

    # Find all backups matching the pattern
    backups = sorted(
        [p for p in backup_dir.glob(pattern) if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # Newest first
    )

    if len(backups) <= keep_count:
        return

    # Remove old backups beyond keep_count
    for old_backup in backups[keep_count:]:
        try:
            old_backup.unlink()
            LOGGER.info("Removed old backup: %s", old_backup.name)
        except OSError as exc:
            LOGGER.warning("Failed to remove old backup %s: %s", old_backup.name, exc)


def run_startup_backup(db_path: Path) -> None:
    """Run backup on startup if enabled via environment variable.

    This is designed to be called in a background thread during application startup.

    Args:
        db_path: Path to saiverse.db
    """
    if not _auto_backup_enabled():
        LOGGER.debug("Auto-backup is disabled (SAIVERSE_DB_BACKUP_ON_START=false)")
        return

    try:
        backup_path = backup_saiverse_db(db_path)
        if backup_path:
            LOGGER.info("✓ Startup backup completed: %s", backup_path.name)
    except Exception:
        LOGGER.exception("Startup backup failed (non-fatal)")


def get_recent_backups(db_path: Path, limit: int = 5) -> list[tuple[Path, str, float]]:
    """Get list of recent backups with metadata.

    Args:
        db_path: Path to the main database
        limit: Maximum number of backups to return

    Returns:
        List of tuples: (backup_path, timestamp_str, size_mb)
    """
    backup_dir = db_path.parent
    pattern = f"{db_path.name}_backup_*.bak"

    backups = sorted(
        [p for p in backup_dir.glob(pattern) if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    result = []
    for backup in backups:
        size_mb = backup.stat().st_size / (1024 * 1024)
        # Extract timestamp from filename
        timestamp_str = backup.stem.split("_backup_")[-1]
        result.append((backup, timestamp_str, size_mb))

    return result
