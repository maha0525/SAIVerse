"""Simple backup utility for saiverse.db with rotation and cleanup."""

from __future__ import annotations

import argparse
import logging
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

DEFAULT_BACKUP_KEEP = 10


def _auto_backup_enabled() -> bool:
    """Check if auto-backup on startup is enabled via environment variable."""
    value = os.getenv("SAIVERSE_DB_BACKUP_ON_START", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _get_backup_keep_count(kind: str = "startup") -> int:
    """Get number of backups to keep from environment variable."""
    env_name = (
        "SAIVERSE_DB_PREUPGRADE_BACKUP_KEEP"
        if kind == "pre_upgrade"
        else "SAIVERSE_DB_BACKUP_KEEP"
    )
    try:
        return max(1, int(os.getenv(env_name, str(DEFAULT_BACKUP_KEEP))))
    except ValueError:
        return DEFAULT_BACKUP_KEEP


def backup_saiverse_db(
    db_path: Path,
    keep_count: int | None = None,
    *,
    kind: str = "startup",
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
        keep_count = _get_backup_keep_count(kind)

    backup_dir = db_path.parent
    # Keep the full microsecond timestamp and add a process-safe random suffix.
    # The existence probe is retained as a final guard for a mocked UUID source.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    unique_suffix = uuid4().hex
    safe_kind = "".join(character for character in kind if character.isalnum() or character in "_-")
    if not safe_kind:
        raise ValueError("Backup kind must contain a safe character")
    backup_path = backup_dir / f"{db_path.name}_backup_{safe_kind}_{timestamp}_{unique_suffix}.bak"
    collision_index = 1
    while backup_path.exists():
        backup_path = backup_dir / (
            f"{db_path.name}_backup_{safe_kind}_{timestamp}_{unique_suffix}_{collision_index}.bak"
        )
        collision_index += 1

    try:
        # Create SQLite backup (safe even if DB is in use)
        LOGGER.info("Creating backup: %s", backup_path)
        with closing(sqlite3.connect(db_path)) as src:
            with closing(sqlite3.connect(backup_path)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        with closing(sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("Backup integrity_check failed")

        from saiverse import __version__

        manifest_path = backup_path.with_suffix(backup_path.suffix + ".json")
        manifest_tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        manifest_tmp.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "backup_id": unique_suffix,
                    "kind": safe_kind,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "source_db": str(db_path.resolve()),
                    "source_version": __version__,
                    "size": backup_path.stat().st_size,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(manifest_tmp, manifest_path)

        LOGGER.info("✓ Backup created: %s (size: %.1f KB)", backup_path, backup_path.stat().st_size / 1024)

        # Prune old backups
        _prune_old_backups(db_path, keep_count, kind=safe_kind)

        return backup_path

    except Exception as exc:
        LOGGER.error("Failed to create backup: %s", exc)
        # Clean up partial backup if it exists
        if backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
        backup_path.with_suffix(backup_path.suffix + ".json").unlink(missing_ok=True)
        raise RuntimeError(f"Backup failed: {exc}") from exc


def _validated_backup_metadata(path: Path) -> dict | None:
    manifest_path = path.with_suffix(path.suffix + ".json")
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        if metadata.get("size") != path.stat().st_size:
            return None
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            return None
        datetime.fromisoformat(metadata["created_at"])
        return metadata
    except (OSError, KeyError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return None


def _prune_old_backups(db_path: Path, keep_count: int, *, kind: str | None = None) -> None:
    """Remove old backups, keeping only the most recent N backups.

    Args:
        db_path: Path to the main database
        keep_count: Number of recent backups to keep
    """
    backup_dir = db_path.parent
    pattern = (
        f"{db_path.name}_backup_{kind}_*.bak"
        if kind
        else f"{db_path.name}_backup_*.bak"
    )

    # Find all backups matching the pattern
    validated = [
        (path, metadata)
        for path in backup_dir.glob(pattern)
        if path.is_file() and (metadata := _validated_backup_metadata(path)) is not None
    ]
    backups = [
        item[0]
        for item in sorted(validated, key=lambda item: item[1]["created_at"], reverse=True)
    ]

    if len(backups) <= keep_count:
        return

    # Remove old backups beyond keep_count
    for old_backup in backups[keep_count:]:
        try:
            old_backup.unlink()
            old_backup.with_suffix(old_backup.suffix + ".json").unlink(missing_ok=True)
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

    validated = [
        (path, metadata)
        for path in backup_dir.glob(pattern)
        if path.is_file() and (metadata := _validated_backup_metadata(path)) is not None
    ]
    backups = sorted(validated, key=lambda item: item[1]["created_at"], reverse=True)[:limit]

    result = []
    for backup, metadata in backups:
        size_mb = backup.stat().st_size / (1024 * 1024)
        timestamp_str = metadata["created_at"]
        result.append((backup, timestamp_str, size_mb))

    return result


def restore_saiverse_db_backup(backup_path: Path, db_path: Path) -> Path | None:
    """Restore one validated main-DB generation while SAIVerse is stopped.

    Returns the automatic pre-restore backup path. The target and its SQLite
    sidecars are swapped as a rollback-capable unit.
    """
    from saiverse.runtime_marker import marker_status

    state, reason = marker_status()
    if state != "stopped":
        raise RuntimeError(f"Database restore requires stopped SAIVerse: {reason}")
    backup_path = backup_path.resolve()
    db_path = db_path.resolve()
    metadata = _validated_backup_metadata(backup_path)
    if metadata is None:
        raise ValueError(f"Backup is incomplete or corrupt: {backup_path}")
    try:
        source_db = Path(metadata["source_db"]).resolve()
    except (KeyError, TypeError) as exc:
        raise ValueError("Backup manifest has no valid source_db") from exc
    if source_db != db_path:
        raise ValueError(f"Backup belongs to {source_db}, not {db_path}")
    if not db_path.is_file():
        raise FileNotFoundError(f"Target database does not exist: {db_path}")

    safety_backup = backup_saiverse_db(db_path, kind="pre_restore")
    restore_id = uuid4().hex
    staged = db_path.with_name(f".{db_path.name}.restore-{restore_id}.tmp")
    rollback = db_path.with_name(f".{db_path.name}.restore-{restore_id}.rollback")
    sidecars = [
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]
    moved_sidecars: list[tuple[Path, Path]] = []
    try:
        with closing(sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(staged)) as destination:
                source.backup(destination)
        if _validated_sqlite_integrity(staged) is False:
            raise RuntimeError("Staged restore database failed integrity_check")

        os.replace(db_path, rollback)
        for sidecar in sidecars:
            if sidecar.exists():
                destination = sidecar.with_name(f".{sidecar.name}.{restore_id}.rollback")
                os.replace(sidecar, destination)
                moved_sidecars.append((sidecar, destination))
        os.replace(staged, db_path)
        try:
            rollback.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Could not remove old DB rollback file: %s", rollback, exc_info=True)
        for _original, moved in moved_sidecars:
            try:
                moved.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove old SQLite sidecar: %s", moved, exc_info=True)
        return safety_backup
    except Exception:
        staged.unlink(missing_ok=True)
        if rollback.exists():
            db_path.unlink(missing_ok=True)
            os.replace(rollback, db_path)
        for original, moved in reversed(moved_sidecars):
            if moved.exists():
                os.replace(moved, original)
        raise


def _validated_sqlite_integrity(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(result and result[0] == "ok")
    except sqlite3.Error:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or restore saiverse.db backups")
    parser.add_argument("--db", type=Path, required=True, help="Target saiverse.db path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="List validated backup generations")
    list_parser.add_argument("--limit", type=int, default=10)
    restore_parser = subparsers.add_parser("restore", help="Restore one validated generation")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "list":
        for path, created_at, size_mb in get_recent_backups(args.db, args.limit):
            print(f"{created_at}\t{size_mb:.2f} MB\t{path}")
        return 0

    if not args.yes:
        print("Type RESTORE to replace the stopped database: ", end="", flush=True)
        if input().strip() != "RESTORE":
            print("Cancelled.")
            return 0
    try:
        safety = restore_saiverse_db_backup(args.backup, args.db)
    except Exception as exc:
        LOGGER.exception("Database restore failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {args.db} from {args.backup}")
    print(f"Pre-restore backup: {safety}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
