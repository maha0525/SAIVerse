import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd


class HistoryMixin:
    """Shared helpers for building histories and backup management."""

    building_memory_paths: Dict[str, Path]
    building_histories: Dict[str, List[Dict[str, str]]]
    backup_dir: Path
    saiverse_home: Path
    db_path: str

    def _save_building_histories(self, building_ids: Optional[Iterable[str]] = None) -> None:
        """Persist in-memory building histories to disk."""
        if building_ids is None:
            items = self.building_memory_paths.items()
        else:
            unique_ids = {bid for bid in building_ids if bid in self.building_memory_paths}
            items = ((bid, self.building_memory_paths[bid]) for bid in unique_ids)

        for b_id, path in items:
            hist = self.building_histories.get(b_id, [])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

    def get_building_history(self, building_id: str) -> List[Dict[str, str]]:
        """Return the raw conversation log for a given building."""
        return self.building_histories.get(building_id, [])

    # --- World Editor: Backup/Restore Methods ---

    def get_backups(self) -> pd.DataFrame:
        """Gets a list of available world backups (.zip)."""
        backups: List[Dict[str, str]] = []
        for archive in self.backup_dir.glob("*.zip"):
            try:
                stat = archive.stat()
            except FileNotFoundError:
                continue
            backups.append(
                {
                    "Backup Name": archive.stem,
                    "Created At": datetime.fromtimestamp(stat.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "Size (KB)": round(stat.st_size / 1024, 2),
                }
            )
        if not backups:
            return pd.DataFrame(columns=["Backup Name", "Created At", "Size (KB)"])
        return pd.DataFrame(backups).sort_values(by="Created At", ascending=False)

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
