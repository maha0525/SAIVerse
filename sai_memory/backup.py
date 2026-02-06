from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None

import contextlib
import gc
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

BACKUP_ROOT = Path.home() / ".saiverse" / "backups" / "saimemory_rdiff"
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
SIMPLE_BACKUP_ROOT = Path.home() / ".saiverse" / "backups" / "saimemory_simple"
SIMPLE_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
GLOBAL_LOCK_PATH = Path(os.getenv("SAIMEMORY_BACKUP_LOCK_PATH", BACKUP_ROOT / "saimemory_backup.lock"))
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
    base = Path(root) if root else BACKUP_ROOT
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
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _check_stale_lock() -> bool:
    """Check if the lock file is stale (holder process is dead). Returns True if stale and cleaned up."""
    if not GLOBAL_LOCK_PATH.exists():
        return False
    try:
        content = GLOBAL_LOCK_PATH.read_text().strip()
        # Parse pid=XXXXX from lock file
        for part in content.split():
            if part.startswith("pid="):
                pid = int(part.split("=")[1])
                if not _is_process_alive(pid):
                    LOGGER.warning(
                        "Removing stale backup lock (holder pid=%d is dead): %s",
                        pid,
                        GLOBAL_LOCK_PATH,
                    )
                    GLOBAL_LOCK_PATH.unlink(missing_ok=True)
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

    GLOBAL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(GLOBAL_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
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
    base = Path(root) if root else SIMPLE_BACKUP_ROOT
    persona_dir = base / persona_id
    persona_dir.mkdir(parents=True, exist_ok=True)
    return persona_dir


def _prune_simple_backups(backup_dir: Path, keep_count: int = SIMPLE_BACKUP_KEEP) -> None:
    """Remove old simple backups, keeping only the most recent N backups."""
    pattern = "memory.db_backup_*.bak"
    backups = sorted(
        [p for p in backup_dir.glob(pattern) if p.is_file()],
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
    """Get the most recent backup file in the directory."""
    pattern = "memory.db_backup_*.bak"
    backups = sorted(
        [p for p in backup_dir.glob(pattern) if p.is_file()],
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
