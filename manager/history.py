import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LOGGER = logging.getLogger(__name__)

# How many startup backups (log.json.backup_<ts>.bak) to keep per building.
# Override via SAIVERSE_BUILDING_LOG_BACKUP_KEEP env var.
_BACKUP_KEEP_DEFAULT = 5
_BACKUP_SUFFIX_PATTERN = ".backup_"  # log.json.backup_20260426_120000.bak


def _backup_keep_count() -> int:
    raw = os.getenv("SAIVERSE_BUILDING_LOG_BACKUP_KEEP")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _BACKUP_KEEP_DEFAULT


def list_log_backups(log_path: Path) -> List[Path]:
    """Return existing backup snapshots for a log path, newest first.

    Backups follow the naming ``<log_filename>.backup_<YYYYMMDD_HHMMSS>.bak``.
    Sibling ``.corrupted_*`` files are NOT included (they are quarantine
    rescues, not backups).
    """
    parent = log_path.parent
    if not parent.exists():
        return []
    prefix = f"{log_path.name}{_BACKUP_SUFFIX_PATTERN}"
    matches = [p for p in parent.glob(f"{log_path.name}.backup_*.bak") if p.name.startswith(prefix)]
    matches.sort(key=lambda p: p.name, reverse=True)  # newest first (timestamp in name)
    return matches


def create_log_backup_snapshot(log_path: Path, timestamp: str) -> Optional[Path]:
    """Copy ``log_path`` to a timestamped ``.backup_<ts>.bak`` snapshot.

    Should only be called when ``log_path`` has been **successfully loaded**
    (so we know it's known-good content). Rotates older backups beyond
    the keep limit.

    Returns the path of the created backup, or None if log_path doesn't exist.
    """
    if not log_path.exists():
        return None
    backup_path = log_path.parent / f"{log_path.name}.backup_{timestamp}.bak"
    if backup_path.exists():
        # 同一秒の二重起動などレアケースは黙ってskip
        return backup_path
    shutil.copy2(log_path, backup_path)
    LOGGER.debug("Created backup snapshot: %s", backup_path)

    # ローテーション
    keep = _backup_keep_count()
    backups = list_log_backups(log_path)  # newest first
    for old in backups[keep:]:
        try:
            old.unlink()
            LOGGER.debug("Pruned old backup: %s", old)
        except OSError:
            LOGGER.warning("Failed to prune old backup %s", old, exc_info=True)
    return backup_path


class HistoryMixin:
    """Shared helpers for building histories and backup management."""

    building_memory_paths: Dict[str, Path]
    building_histories: Dict[str, List[Dict[str, str]]]
    backup_dir: Path
    saiverse_home: Path
    db_path: str
    quarantined_buildings: Dict[str, Dict[str, Any]]
    modified_buildings: set
    SessionLocal: Any  # SQLAlchemy sessionmaker bound by SAIVerseManager init

    def _save_modified_buildings(self) -> None:
        """[Deprecated] DB が source of truth のため no-op。 互換のため残存。"""
        return

    def add_building_event(
        self,
        building_id: str,
        msg: Dict[str, Any],
        heard_by: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Add a building-level event (movement, system, etc.) to building DB.

        Used by OccupancyManager and other code paths that need to inject
        events outside of a specific persona's HistoryManager. DB が seq /
        message_id を独立採番する (= 旧 building_histories 経由の seq 衝突問題は消えた)。
        Skips quarantined buildings entirely.
        """
        if building_id in self.quarantined_buildings:
            LOGGER.warning(
                "add_building_event: building %s is quarantined — refusing event",
                building_id,
            )
            return None

        enriched: Dict[str, Any] = dict(msg)
        if "timestamp" not in enriched:
            enriched["timestamp"] = datetime.now(timezone.utc).isoformat()
        heard_set = sorted({str(eid) for eid in (heard_by or []) if eid})
        enriched["heard_by"] = heard_set
        if "ingested_by" not in enriched:
            enriched["ingested_by"] = []

        try:
            from database.building_messages import insert_building_message
            session_factory = getattr(self, "SessionLocal", None)
            saved = insert_building_message(session_factory, building_id, enriched)
            return saved or enriched
        except Exception:
            LOGGER.warning(
                "add_building_event: failed to insert (bid=%s)", building_id,
                exc_info=True,
            )
            return None

    def reset_persona_seq_counters_for_building(
        self, building_id: str, value: int
    ) -> None:
        """[Deprecated] DB が seq を管理するため no-op。 互換のため残存。"""
        return

    def clamp_persona_cursors_for_building(
        self, building_id: str, max_seq: int
    ) -> None:
        """Clamp every persona's pulse_cursors / entry_markers for a building.

        Call this after restoring or resetting a building's log.json so that
        personas don't skip new messages. Without clamping, a persona whose
        cursor was 1857 would skip all messages with seq <= 1857 even if the
        log.json now only contains seq 1..100 (cursor in seq space points
        beyond the file's range, so new messages with seq 101.. would match,
        but if log was reset to []), the new seq starts from 1, which is <
        cursor → persona ignores them).

        Also updates entry_markers (used to mark "what the persona had seen
        when entering the building"). After restore, the entry marker should
        be no greater than the current max_seq.

        Note: in-memory only. The next ``_save_session_metadata`` call (or
        shutdown) writes the updated cursor to conscious_log.json. If the
        process crashes before that write, ``initialise_pulse_state`` will
        re-clamp on the next startup using the loaded log.json's max_seq.
        """
        personas = getattr(self, "personas", None)
        if not personas:
            return
        for persona in personas.values():
            cursors = getattr(persona, "pulse_cursors", None)
            if isinstance(cursors, dict) and building_id in cursors:
                cur = cursors[building_id]
                new_cur = min(cur, max_seq)
                if cur != new_cur:
                    cursors[building_id] = new_cur
                    LOGGER.info(
                        "Clamped pulse_cursor for %s/%s: %d -> %d (after restore/reset)",
                        getattr(persona, "persona_id", "?"),
                        building_id,
                        cur,
                        new_cur,
                    )
            markers = getattr(persona, "entry_markers", None)
            if isinstance(markers, dict) and building_id in markers:
                em = markers[building_id]
                new_em = min(em, max_seq)
                if em != new_em:
                    markers[building_id] = new_em
                    LOGGER.info(
                        "Clamped entry_marker for %s/%s: %d -> %d (after restore/reset)",
                        getattr(persona, "persona_id", "?"),
                        building_id,
                        em,
                        new_em,
                    )

    def _save_building_histories(self, building_ids: Iterable[str]) -> None:
        """[Deprecated] DB が source of truth のため no-op。 互換のため残存。"""
        return

    def get_building_history(self, building_id: str) -> List[Dict[str, str]]:
        """Return the conversation log for a given building (DB クエリ)。"""
        from database.building_messages import fetch_building_messages
        session_factory = getattr(self, "SessionLocal", None)
        return fetch_building_messages(session_factory, building_id)

    # --- World Editor: Backup/Restore Methods ---

    def backup_world(self, backup_name: str) -> str:
        """
        Creates a backup of the entire world state, including the database and all log files,
        into a single .zip archive.
        """
        if not backup_name or not backup_name.isalnum():
            return "Error: Backup name must be alphanumeric and not empty."

        backup_zip_path = self.backup_dir / f"{backup_name}.zip"
        if backup_zip_path.exists():
            return f"Error: A backup named '{backup_name}' already exists."

        db_file_path = Path(self.db_path)
        cities_log_path = self.saiverse_home / "cities"
        personas_log_path = self.saiverse_home / "personas"
        buildings_log_path = self.saiverse_home / "buildings"

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)

                if db_file_path.exists():
                    shutil.copy(db_file_path, tmp_path / db_file_path.name)
                    logging.info("Added database to backup staging: %s", db_file_path)

                if cities_log_path.exists() and cities_log_path.is_dir():
                    shutil.copytree(cities_log_path, tmp_path / "cities")
                    logging.info("Added cities logs to backup staging: %s", cities_log_path)

                if personas_log_path.exists() and personas_log_path.is_dir():
                    shutil.copytree(personas_log_path, tmp_path / "personas")
                    logging.info(
                        "Added personas logs to backup staging: %s", personas_log_path
                    )

                if buildings_log_path.exists() and buildings_log_path.is_dir():
                    shutil.copytree(buildings_log_path, tmp_path / "buildings")
                    logging.info(
                        "Added buildings logs to backup staging: %s", buildings_log_path
                    )

                shutil.make_archive(
                    base_name=self.backup_dir / backup_name,
                    format="zip",
                    root_dir=tmp_path,
                )

            logging.info("World state successfully backed up to %s", backup_zip_path)
            return f"Backup '{backup_name}' created successfully."
        except Exception as exc:
            logging.error("Failed to create backup: %s", exc, exc_info=True)
            return f"Error: {exc}"

    def restore_world(self, backup_name: str) -> str:
        """
        Restores the entire world state from a .zip archive.
        This operation is destructive and requires an application restart.
        """
        backup_zip_path = self.backup_dir / f"{backup_name}.zip"
        if not backup_zip_path.exists():
            return f"Error: Backup '{backup_name}' not found."

        db_file_path = Path(self.db_path)
        cities_log_path = self.saiverse_home / "cities"
        personas_log_path = self.saiverse_home / "personas"
        buildings_log_path = self.saiverse_home / "buildings"

        try:
            logging.warning("Starting world restore. Removing existing data...")
            if db_file_path.exists():
                db_file_path.unlink()
                logging.info("Removed existing database file: %s", db_file_path)
            if cities_log_path.exists():
                shutil.rmtree(cities_log_path)
                logging.info("Removed existing cities log directory: %s", cities_log_path)
            if personas_log_path.exists():
                shutil.rmtree(personas_log_path)
                logging.info(
                    "Removed existing personas log directory: %s", personas_log_path
                )
            if buildings_log_path.exists():
                shutil.rmtree(buildings_log_path)
                logging.info(
                    "Removed existing buildings log directory: %s", buildings_log_path
                )

            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                logging.info(
                    "Unpacking backup '%s' to temporary directory '%s'",
                    backup_zip_path,
                    tmp_path,
                )
                shutil.unpack_archive(backup_zip_path, tmp_path)

                unpacked_db = tmp_path / db_file_path.name
                if unpacked_db.exists():
                    shutil.move(str(unpacked_db), str(db_file_path))
                    logging.info("Restored database file to %s", db_file_path)

                for log_dir_name in ("cities", "personas", "buildings"):
                    unpacked_dir = tmp_path / log_dir_name
                    if unpacked_dir.exists() and unpacked_dir.is_dir():
                        shutil.move(
                            str(unpacked_dir), str(self.saiverse_home / log_dir_name)
                        )
                        logging.info("Restored %s log directory.", log_dir_name)

            logging.warning(
                "World state has been restored from %s. A RESTART IS REQUIRED.",
                backup_zip_path,
            )
            return "Restore successful. Please RESTART the application to load the restored world."
        except Exception as exc:
            logging.error("Failed to restore world: %s", exc, exc_info=True)
            return (
                f"Error during restore: {exc}. The world state may be inconsistent. "
                "It is recommended to restore another backup or re-seed the database."
            )

    def delete_backup(self, backup_name: str) -> str:
        """Deletes a specific backup file (.zip)."""
        backup_path = self.backup_dir / f"{backup_name}.zip"
        if not backup_path.exists():
            return f"Error: Backup '{backup_name}' not found."
        try:
            os.remove(backup_path)
            logging.info("Deleted backup: %s", backup_path)
            return f"Backup '{backup_name}' deleted successfully."
        except Exception as exc:
            logging.error("Failed to delete backup: %s", exc, exc_info=True)
            return f"Error: {exc}"
