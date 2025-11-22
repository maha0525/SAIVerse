from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

BACKUP_ROOT = Path.home() / ".saiverse" / "backups" / "saimemory_rdiff"
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
GLOBAL_LOCK_PATH = Path(os.getenv("SAIMEMORY_BACKUP_LOCK_PATH", BACKUP_ROOT / "saimemory_backup.lock"))
BACKUP_TIMEOUT_SEC = int(os.getenv("SAIMEMORY_BACKUP_TIMEOUT_SEC", "300"))
LOCK_WAIT_SEC = int(os.getenv("SAIMEMORY_BACKUP_LOCK_WAIT_SEC", "10"))
RETRY_ON_CORRUPT = os.getenv("SAIMEMORY_BACKUP_RETRY_ON_CORRUPT", "true").strip().lower() in {"1", "true", "yes", "on"}
ARCHIVE_KEEP = int(os.getenv("SAIMEMORY_BACKUP_ARCHIVE_KEEP", "3"))


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



@contextlib.contextmanager
def _global_backup_lock(timeout: int = LOCK_WAIT_SEC):
    """Best-effort global lock to avoid concurrent rdiff-backup runs."""

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
