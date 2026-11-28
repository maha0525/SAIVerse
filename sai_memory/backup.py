from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None

import argparse
import contextlib
import gc
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

def get_backup_root() -> Path:
    """rdiff-backup repository root under SAIVERSE_HOME (created on demand)."""
    from saiverse.data_paths import get_saiverse_home

    root = get_saiverse_home() / "backups" / "saimemory_rdiff"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_simple_backup_root() -> Path:
    """Simple-copy backup root under SAIVERSE_HOME (created on demand)."""
    from saiverse.data_paths import get_saiverse_home

    root = get_saiverse_home() / "backups" / "saimemory_simple"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_global_lock_path() -> Path:
    env_path = os.getenv("SAIMEMORY_BACKUP_LOCK_PATH")
    if env_path:
        return Path(env_path)
    return get_backup_root() / "saimemory_backup.lock"
BACKUP_TIMEOUT_SEC = int(os.getenv("SAIMEMORY_BACKUP_TIMEOUT_SEC", "300"))
LOCK_WAIT_SEC = int(os.getenv("SAIMEMORY_BACKUP_LOCK_WAIT_SEC", "10"))
RETRY_ON_CORRUPT = os.getenv("SAIMEMORY_BACKUP_RETRY_ON_CORRUPT", "true").strip().lower() in {"1", "true", "yes", "on"}
ARCHIVE_KEEP = int(os.getenv("SAIMEMORY_BACKUP_ARCHIVE_KEEP", "3"))
SIMPLE_BACKUP_KEEP = int(os.getenv("SAIMEMORY_SIMPLE_BACKUP_KEEP", "10"))


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
        # Use closing() to ensure close() is called, not just commit().
        # sqlite3's context manager only commits/rollbacks but does NOT close,
        # causing file lock issues on Windows.
        with closing(sqlite3.connect(db_path)) as src:
            with closing(sqlite3.connect(snapshot_path)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.execute("PRAGMA journal_mode=DELETE")
        # Force garbage collection to release file handles on Windows
        gc.collect()
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return tmpdir


def _persona_backup_dir(persona_id: str, root: Path | None = None) -> Path:
    base = Path(root) if root else get_backup_root()
    persona_root = base / persona_id
    persona_root.mkdir(parents=True, exist_ok=True)
    return persona_root


def _rotate_repo(repo_dir: Path) -> Path:
    """Archive existing repo to persona.archived.TIMESTAMP and recreate empty dir."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archived = repo_dir.parent / f"{repo_dir.name}.archived.{timestamp}"
    LOGGER.info("Rotating existing backup repo for %s to %s", repo_dir.name, archived)
    if archived.exists():
        shutil.rmtree(archived)
    if repo_dir.exists():
        repo_dir.rename(archived)
    repo_dir.mkdir(parents=True, exist_ok=True)
    return archived


def _prune_archives(repo_dir: Path, keep: int = ARCHIVE_KEEP) -> None:
    """Keep only the newest `keep` archives for this persona."""
    parent = repo_dir.parent
    stem = repo_dir.name + ".archived."
    archives = sorted([p for p in parent.glob(stem + "*") if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
    for extra in archives[keep:]:
        try:
            shutil.rmtree(extra)
            LOGGER.info("Pruned old archive %s", extra)
        except Exception as exc:
            LOGGER.warning("Failed to prune archive %s: %s", extra, exc)


def _looks_corrupt(output: str) -> bool:
    signals = [
        "current mirror",
        "current_mirror",
        "Previous backup seems to have failed",
        "not in the past",
    ]
    low = output.lower()
    return any(s.lower() in low for s in signals)


def _run_rdiff(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=BACKUP_TIMEOUT_SEC)
    return result.returncode, result.stdout or "", result.stderr or ""


def _backup_once(snapshot_dir: Path, repo_dir: Path, rdiff_exec: str) -> tuple[int, str]:
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
    code, out, err = _run_rdiff(cmd)
    return code, (err or out)



def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running without terminating it."""
    from saiverse.runtime_marker import probe_process_alive

    return probe_process_alive(pid) != "dead"


def _check_stale_lock() -> bool:
    """Check if the lock file is stale (holder process is dead). Returns True if stale and cleaned up."""
    lock_path = get_global_lock_path()
    if not lock_path.exists():
        return False
    try:
        content = lock_path.read_text().strip()
        # Parse pid=XXXXX from lock file
        for part in content.split():
            if part.startswith("pid="):
                pid = int(part.split("=")[1])
                if not _is_process_alive(pid):
                    LOGGER.warning(
                        "Removing stale backup lock (holder pid=%d is dead): %s",
                        pid,
                        lock_path,
                    )
                    lock_path.unlink(missing_ok=True)
                    return True
                break
    except Exception as exc:
        LOGGER.debug("Failed to check stale lock: %s", exc)
    return False


@contextlib.contextmanager
def _global_backup_lock(timeout: int = LOCK_WAIT_SEC):
    """Best-effort global lock to avoid concurrent rdiff-backup runs."""

    # Clean up stale lock from crashed processes
    _check_stale_lock()
    
    if fcntl is None:
        # Windows or other non-Unix systems: skip file locking
        LOGGER.warning("File locking (fcntl) not available, skipping backup lock.")
        yield
        return

    lock_path = get_global_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    start = time.monotonic()
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                os.ftruncate(fd, 0)
                os.write(fd, f"pid={os.getpid()} ts={datetime.now(timezone.utc).isoformat()}".encode())
                break
            except BlockingIOError:
                # Check for stale lock on each retry
                if _check_stale_lock():
                    continue
                if time.monotonic() - start > timeout:
                    raise BackupError("Another SAIMemory backup appears to be running; timed out waiting for lock.")
                time.sleep(0.25)
        yield
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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

    with _global_backup_lock():
        repo_dir = _persona_backup_dir(persona_id, output_root)
        rdiff_exec = _ensure_rdiff_backup(rdiff_path)

        if force_full and repo_dir.exists():
            _rotate_repo(repo_dir)
            _prune_archives(repo_dir)

        snapshot_dir = _sqlite_snapshot(db_path)
        try:
            code, msg = _backup_once(snapshot_dir, repo_dir, rdiff_exec)
            if code != 0 and RETRY_ON_CORRUPT and _looks_corrupt(msg):
                LOGGER.warning(
                    "Backup for %s failed with probable corruption (exit %s); rotating repo and retrying once",
                    persona_id,
                    code,
                )
                _rotate_repo(repo_dir)
                code, msg = _backup_once(snapshot_dir, repo_dir, rdiff_exec)

            if code != 0:
                raise BackupError(f"rdiff-backup failed (exit {code}): {msg.strip()[:400]}")
        except subprocess.TimeoutExpired as exc:
            LOGGER.error("rdiff-backup timed out after %ss for persona=%s", BACKUP_TIMEOUT_SEC, persona_id)
            lock_file = repo_dir / "rdiff-backup-data" / "lock.yml"
            lock_file.unlink(missing_ok=True)
            raise BackupError(f"rdiff-backup timed out after {BACKUP_TIMEOUT_SEC}s") from exc
        finally:
            shutil.rmtree(snapshot_dir, ignore_errors=True)

        _prune_archives(repo_dir)
        return repo_dir


def latest_backup_path(persona_id: str, output_root: Path | None = None) -> Path:
    return _persona_backup_dir(persona_id, output_root)


# ---------------------------------------------------------------------------
# Simple backup (SQLite direct backup, no rdiff-backup dependency)
# ---------------------------------------------------------------------------


def _simple_backup_dir(persona_id: str, root: Path | None = None) -> Path:
    """Get the simple backup directory for a persona."""
    base = Path(root) if root else get_simple_backup_root()
    persona_dir = base / persona_id
    persona_dir.mkdir(parents=True, exist_ok=True)
    return persona_dir


def _prune_simple_backups(backup_dir: Path, keep_count: int = SIMPLE_BACKUP_KEEP) -> None:
    """Remove old simple backups, keeping only the most recent N backups.

    ``memory.db_backup_pre_restore_*.bak`` safety backups created right before
    a restore are preserved separately so routine startup backups cannot wipe
    the safety net of a recent restore.
    """
    pattern = "memory.db_backup_*.bak"
    backups = sorted(
        [
            p
            for p in backup_dir.glob(pattern)
            if p.is_file() and not p.name.startswith("memory.db_backup_pre_restore_")
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if len(backups) <= keep_count:
        return
    for old_backup in backups[keep_count:]:
        try:
            old_backup.unlink()
            LOGGER.info("Removed old simple backup: %s", old_backup.name)
        except OSError as exc:
            LOGGER.warning("Failed to remove old backup %s: %s", old_backup.name, exc)


def _get_latest_simple_backup(backup_dir: Path) -> Path | None:
    """Get the most recent routine backup file in the directory."""
    pattern = "memory.db_backup_*.bak"
    backups = sorted(
        [
            p
            for p in backup_dir.glob(pattern)
            if p.is_file() and not p.name.startswith("memory.db_backup_pre_restore_")
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return backups[0] if backups else None


def _compute_file_hash(file_path: Path, algorithm: str = "sha256") -> str:
    """Compute hash of a file."""
    h = hashlib.new(algorithm)
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_simple_backup(
    *,
    persona_id: str,
    db_path: Path,
    output_root: Path | None = None,
    keep_count: int | None = None,
    skip_if_unchanged: bool = True,
) -> Path | None:
    """Create a simple timestamped backup of the persona memory.db.

    This is a fallback when rdiff-backup is not available. Uses SQLite's
    built-in backup API for safe copying.

    Args:
        persona_id: Persona identifier
        db_path: Path to memory.db
        output_root: Custom backup directory (default: ~/.saiverse/backups/saimemory_simple)
        keep_count: Number of backups to keep (default: from env or 10)
        skip_if_unchanged: Skip backup if DB hasn't changed since last backup (default: True)

    Returns:
        Path to created backup file, or None if skipped (no changes)

    Raises:
        BackupError: If backup fails
    """
    if not db_path.exists():
        raise BackupError(f"memory.db not found: {db_path}")

    if keep_count is None:
        keep_count = SIMPLE_BACKUP_KEEP

    backup_dir = _simple_backup_dir(persona_id, output_root)

    # Create a temporary snapshot first to handle WAL mode correctly
    tmpdir = None
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="saimemory_simple_"))
        snapshot_path = tmpdir / "snapshot.db"

        # Create snapshot using SQLite backup API
        # Use closing() to ensure close() is called, not just commit().
        # sqlite3's context manager only commits/rollbacks but does NOT close,
        # causing file lock issues on Windows.
        with closing(sqlite3.connect(db_path)) as src:
            with closing(sqlite3.connect(snapshot_path)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.execute("PRAGMA journal_mode=DELETE")
        # Force garbage collection to release file handles on Windows
        gc.collect()

        # Check if unchanged from latest backup
        if skip_if_unchanged:
            latest_backup = _get_latest_simple_backup(backup_dir)
            if latest_backup:
                current_hash = _compute_file_hash(snapshot_path)
                latest_hash = _compute_file_hash(latest_backup)
                if current_hash == latest_hash:
                    LOGGER.info(
                        "Simple backup skipped for %s: no changes since %s",
                        persona_id,
                        latest_backup.name,
                    )
                    return None

        # Save backup
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:19]
        backup_path = backup_dir / f"memory.db_backup_{timestamp}.bak"

        LOGGER.info("Creating simple backup for %s: %s", persona_id, backup_path)
        shutil.move(str(snapshot_path), str(backup_path))

        size_kb = backup_path.stat().st_size / 1024
        LOGGER.info("Simple backup created: %s (size: %.1f KB)", backup_path.name, size_kb)

        _prune_simple_backups(backup_dir, keep_count)

        return backup_path

    except Exception as exc:
        LOGGER.error("Failed to create simple backup for %s: %s", persona_id, exc)
        raise BackupError(f"Simple backup failed: {exc}") from exc
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


def is_rdiff_backup_available(rdiff_path: str | None = None) -> bool:
    """Check if rdiff-backup is available."""
    candidate = rdiff_path or shutil.which("rdiff-backup")
    return candidate is not None


def run_backup_auto(
    *,
    persona_id: str,
    db_path: Path,
    output_root: Path | None = None,
    rdiff_path: str | None = None,
    force_full: bool = False,
    prefer_simple: bool = False,
    skip_if_unchanged: bool = True,
) -> Path | None:
    """Run backup with automatic fallback to simple backup if rdiff-backup unavailable.

    Args:
        persona_id: Persona identifier
        db_path: Path to memory.db
        output_root: Custom backup directory
        rdiff_path: Path to rdiff-backup executable
        force_full: Force full backup (rdiff-backup only)
        prefer_simple: Always use simple backup even if rdiff-backup is available
        skip_if_unchanged: Skip backup if DB hasn't changed (simple backup only)

    Returns:
        Path to backup (directory for rdiff, file for simple), or None if skipped

    Raises:
        BackupError: If backup fails
    """
    if prefer_simple or not is_rdiff_backup_available(rdiff_path):
        if not prefer_simple:
            LOGGER.info(
                "rdiff-backup not available for %s, using simple backup",
                persona_id,
            )
        return run_simple_backup(
            persona_id=persona_id,
            db_path=db_path,
            output_root=output_root,
            skip_if_unchanged=skip_if_unchanged,
        )

    return run_backup(
        persona_id=persona_id,
        db_path=db_path,
        output_root=output_root,
        rdiff_path=rdiff_path,
        force_full=force_full,
    )


# ---------------------------------------------------------------------------
# Simple backup inspection, deletion, and safe offline restore
# ---------------------------------------------------------------------------

_SIMPLE_BACKUP_NAME_RE = re.compile(
    r"^memory\.db_backup_(?:(?P<kind>pre_restore)_)?(?P<ts>\d{8}_\d{6}(?:_\d+)?)\.bak$"
)


def _validate_persona_id(persona_id: str) -> str:
    if (
        not persona_id
        or persona_id in (".", "..")
        or "/" in persona_id
        or "\\" in persona_id
        or ":" in persona_id
        or ".." in persona_id
    ):
        raise ValueError(f"Invalid persona_id: {persona_id!r}")
    return persona_id


def _validate_simple_backup_filename(filename: str) -> str:
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or ".." in filename
        or not _SIMPLE_BACKUP_NAME_RE.match(filename)
    ):
        raise ValueError(f"Invalid persona backup filename: {filename!r}")
    return filename


def _parse_simple_backup_timestamp(filename: str, path: Path) -> str:
    match = _SIMPLE_BACKUP_NAME_RE.match(filename)
    if match:
        raw_ts = match.group("ts")
        base_ts = raw_ts[:15]  # YYYYMMDD_HHMMSS
        try:
            dt = datetime.strptime(base_ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            return dt.astimezone().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone().isoformat()
    except OSError:
        return ""


def _resolve_simple_backup_path(
    persona_id: str,
    filename: str,
    *,
    output_root: Path | None = None,
) -> Path:
    safe_persona = _validate_persona_id(persona_id)
    safe_name = _validate_simple_backup_filename(filename)
    base = (Path(output_root) if output_root else get_simple_backup_root()).resolve()
    persona_dir = (base / safe_persona).resolve()
    if not persona_dir.is_relative_to(base):
        raise ValueError(f"Invalid persona_id path escape: {persona_id!r}")
    target = (persona_dir / safe_name).resolve()
    if not target.is_relative_to(persona_dir):
        raise ValueError(f"Invalid backup filename path escape: {filename!r}")
    return target


def list_simple_backups(
    persona_id: str,
    *,
    output_root: Path | None = None,
) -> list[dict[str, Any]]:
    """List simple SQLite backups for one persona, newest first."""
    safe_persona = _validate_persona_id(persona_id)
    base = Path(output_root) if output_root else get_simple_backup_root()
    persona_dir = base / safe_persona
    if not persona_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for path in persona_dir.glob("memory.db_backup_*.bak"):
        if not path.is_file():
            continue
        match = _SIMPLE_BACKUP_NAME_RE.match(path.name)
        if not match:
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        items.append({
            "filename": path.name,
            "kind": match.group("kind") or "backup",
            "size_bytes": stat_result.st_size,
            "created_at": _parse_simple_backup_timestamp(path.name, path),
            "mtime": stat_result.st_mtime,
            "path": str(path),
        })

    items.sort(key=lambda item: (item["created_at"], item["mtime"], item["filename"]), reverse=True)
    for item in items:
        item.pop("mtime", None)
    return items


def delete_simple_backup(
    persona_id: str,
    filename: str,
    *,
    output_root: Path | None = None,
) -> int:
    """Delete one simple backup file for a persona and return reclaimed bytes."""
    target = _resolve_simple_backup_path(persona_id, filename, output_root=output_root)
    if not target.is_file():
        raise FileNotFoundError(f"Backup not found: {persona_id}/{filename}")
    size_bytes = target.stat().st_size
    target.unlink()
    for ext in ("-wal", "-shm"):
        sidecar = target.with_name(target.name + ext)
        with contextlib.suppress(OSError):
            sidecar.unlink(missing_ok=True)
    return size_bytes


def _check_sqlite_integrity(path: Path) -> bool:
    wal = path.with_name(path.name + "-wal")
    uri = f"file:{path}?mode=ro" if (wal.is_file() and wal.stat().st_size > 0) else f"file:{path}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")
    except sqlite3.Error:
        return False


def _create_pre_restore_backup(
    persona_id: str,
    db_path: Path,
    *,
    output_root: Path | None = None,
) -> Path | None:
    if not db_path.is_file():
        return None
    backup_dir = _simple_backup_dir(persona_id, output_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:19]
    backup_path = backup_dir / f"memory.db_backup_pre_restore_{timestamp}.bak"
    tmp_path = backup_path.with_name(f".{backup_path.name}.{uuid4().hex}.tmp")
    try:
        with closing(sqlite3.connect(db_path)) as src:
            with closing(sqlite3.connect(tmp_path)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.execute("PRAGMA journal_mode=DELETE")
        gc.collect()
        if not _check_sqlite_integrity(tmp_path):
            raise BackupError("Pre-restore safety backup failed PRAGMA integrity_check")
        os.replace(tmp_path, backup_path)
        return backup_path
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _clear_persona_head_snapshots(saiverse_db_path: Path, persona_id: str) -> int:
    """Remove cached head snapshots for ``persona_id`` so the next turn rebuilds
    Memopedia / Core Memory sections from the restored ``memory.db``."""
    if not saiverse_db_path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(saiverse_db_path)) as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_head_snapshot'"
            ).fetchone()
            if not table_exists:
                return 0
            cur = conn.execute(
                "DELETE FROM session_head_snapshot WHERE PERSONA_ID = ?",
                (persona_id,),
            )
            conn.commit()
            return cur.rowcount or 0
    except sqlite3.Error as exc:
        LOGGER.warning(
            "Could not clear session_head_snapshot for %s in %s: %s",
            persona_id,
            saiverse_db_path,
            exc,
        )
        return 0


def restore_simple_backup(
    *,
    persona_id: str,
    filename: str,
    db_path: Path,
    saiverse_db_path: Path | None = None,
    output_root: Path | None = None,
    require_stopped: bool = True,
) -> dict[str, Any]:
    """Restore one persona's ``memory.db`` from a validated simple backup while stopped.

    1. Verifies SAIVerse is stopped (when ``require_stopped=True``).
    2. Runs ``PRAGMA integrity_check`` on the backup before touching live files.
    3. Takes an automatic ``memory.db_backup_pre_restore_*.bak`` safety backup.
    4. Stages the restored DB via SQLite Backup API, checks integrity, and swaps
       atomically with rollback on failure (clearing stale ``-wal`` / ``-shm``).
    5. Clears ``session_head_snapshot`` rows for ``persona_id`` in ``saiverse.db``.
    """
    safe_persona = _validate_persona_id(persona_id)
    if require_stopped:
        from saiverse.runtime_marker import marker_status

        state, reason = marker_status()
        if state != "stopped":
            raise BackupError(f"Persona memory restore requires stopped SAIVerse: {reason}")

    backup_path = _resolve_simple_backup_path(safe_persona, filename, output_root=output_root)
    if not backup_path.is_file():
        raise FileNotFoundError(f"Backup not found: {safe_persona}/{filename}")
    if not _check_sqlite_integrity(backup_path):
        raise BackupError(f"Backup failed PRAGMA integrity_check: {backup_path.name}")

    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    safety_backup = _create_pre_restore_backup(safe_persona, db_path, output_root=output_root)

    restore_id = uuid4().hex
    staged = db_path.with_name(f".{db_path.name}.restore-{restore_id}.tmp")
    rollback = db_path.with_name(f".{db_path.name}.restore-{restore_id}.rollback")
    sidecars = [
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]
    moved_sidecars: list[tuple[Path, Path]] = []
    moved_main = False

    try:
        wal = backup_path.with_name(backup_path.name + "-wal")
        src_uri = (
            f"file:{backup_path}?mode=ro"
            if (wal.is_file() and wal.stat().st_size > 0)
            else f"file:{backup_path}?mode=ro&immutable=1"
        )
        with closing(sqlite3.connect(src_uri, uri=True)) as src:
            with closing(sqlite3.connect(staged)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.execute("PRAGMA journal_mode=DELETE")
        gc.collect()

        if not _check_sqlite_integrity(staged):
            raise BackupError("Staged restored memory.db failed PRAGMA integrity_check")

        if db_path.exists():
            os.replace(db_path, rollback)
            moved_main = True
        for sidecar in sidecars:
            if sidecar.exists():
                destination = sidecar.with_name(f".{sidecar.name}.{restore_id}.rollback")
                os.replace(sidecar, destination)
                moved_sidecars.append((sidecar, destination))

        os.replace(staged, db_path)

        if rollback.exists():
            with contextlib.suppress(OSError):
                rollback.unlink(missing_ok=True)
        for _orig, moved in moved_sidecars:
            with contextlib.suppress(OSError):
                moved.unlink(missing_ok=True)
    except Exception:
        with contextlib.suppress(OSError):
            staged.unlink(missing_ok=True)
        if moved_main and rollback.exists():
            with contextlib.suppress(OSError):
                db_path.unlink(missing_ok=True)
            os.replace(rollback, db_path)
        for original, moved in reversed(moved_sidecars):
            if moved.exists():
                os.replace(moved, original)
        raise

    cleared_heads = 0
    if saiverse_db_path is not None:
        cleared_heads = _clear_persona_head_snapshots(saiverse_db_path, safe_persona)

    LOGGER.info(
        "Restored persona %s memory.db from %s (pre_restore=%s, cleared_head_snapshots=%d)",
        safe_persona,
        backup_path.name,
        safety_backup.name if safety_backup else None,
        cleared_heads,
    )
    return {
        "persona_id": safe_persona,
        "restored_from": backup_path.name,
        "pre_restore_backup": safety_backup.name if safety_backup else None,
        "cleared_head_snapshots": cleared_heads,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage SAIMemory simple backups")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_restore = sub.add_parser("restore", help="Restore one persona's memory.db while SAIVerse is stopped")
    p_restore.add_argument("--persona", required=True, help="Persona ID")
    p_restore.add_argument("--file", required=True, help="Backup filename under saimemory_simple/<persona>")
    p_restore.add_argument("--db", type=Path, help="Explicit path to target memory.db")
    p_restore.add_argument("--saiverse-db", type=Path, help="Path to saiverse.db for head snapshot cleanup")

    args = parser.parse_args(argv)
    if args.cmd == "restore":
        from saiverse.data_paths import get_saiverse_home

        home = get_saiverse_home()
        db_path = args.db or (home / "personas" / args.persona / "memory.db")
        saiverse_db = args.saiverse_db or (home / "user_data" / "database" / "saiverse.db")
        try:
            result = restore_simple_backup(
                persona_id=args.persona,
                filename=args.file,
                db_path=db_path,
                saiverse_db_path=saiverse_db,
                require_stopped=True,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(
            f"OK: Restored {result['persona_id']} memory.db from {result['restored_from']} "
            f"(pre-restore backup: {result['pre_restore_backup'] or 'none'})",
            flush=True,
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
