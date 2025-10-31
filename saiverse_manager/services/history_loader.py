"""Utilities for loading building data, avatars, and histories."""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from sqlalchemy.orm import Session

from buildings import Building
from database.models import Building as BuildingModel


@dataclass
class AvatarAssets:
    """Container for avatar assets used by the manager."""

    default_avatar: str
    host_avatar: str


class HistoryLoader:
    """Loads building definitions, avatar assets, and conversation histories."""

    def __init__(self, city_name: str, saiverse_home: Path | None = None) -> None:
        self.city_name = city_name
        self.saiverse_home = saiverse_home or Path.home() / ".saiverse"
        self.backup_dir = self.saiverse_home / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def load_buildings(self, session: Session, city_id: int) -> List[Building]:
        """Read buildings for the given city from the database."""
        db_buildings = (
            session.query(BuildingModel)
            .filter(BuildingModel.CITYID == city_id)
            .all()
        )
        buildings: List[Building] = []
        for db_b in db_buildings:
            building = Building(
                building_id=db_b.BUILDINGID,
                name=db_b.BUILDINGNAME,
                capacity=db_b.CAPACITY or 1,
                system_instruction=db_b.SYSTEM_INSTRUCTION or "",
                entry_prompt=db_b.ENTRY_PROMPT or "",
                auto_prompt=db_b.AUTO_PROMPT or "",
                description=db_b.DESCRIPTION or "",
                auto_interval_sec=
                db_b.AUTO_INTERVAL_SEC if hasattr(db_b, "AUTO_INTERVAL_SEC") else 10,
            )
            buildings.append(building)

        logging.info("Loaded and created %s buildings from database.", len(buildings))
        return buildings

    def build_memory_paths(self, buildings: Iterable[Building]) -> Dict[str, Path]:
        """Return mapping of building IDs to their history file paths."""
        return {
            b.building_id: self.saiverse_home
            / "cities"
            / self.city_name
            / "buildings"
            / b.building_id
            / "log.json"
            for b in buildings
        }

    def load_histories(self, memory_paths: Dict[str, Path]) -> Dict[str, List[Dict[str, str]]]:
        """Load history JSON files if they exist."""
        histories: Dict[str, List[Dict[str, str]]] = {}
        for building_id, path in memory_paths.items():
            if path.exists():
                try:
                    histories[building_id] = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    logging.warning("Failed to load building history %s", building_id)
                    histories[building_id] = []
            else:
                histories[building_id] = []
        return histories

    def load_avatar_assets(
        self,
        default_avatar_path: Path = Path("assets/icons/blank.png"),
        host_avatar_path: Path = Path("assets/icons/host.png"),
    ) -> AvatarAssets:
        """Prepare default and host avatar assets in base64 format."""
        default_avatar = self._load_avatar(default_avatar_path)
        host_avatar = self._load_avatar(host_avatar_path) or default_avatar
        return AvatarAssets(default_avatar=default_avatar, host_avatar=host_avatar)

    @staticmethod
    def _load_avatar(path: Path) -> str:
        if not path.exists():
            return ""
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        data_b = path.read_bytes()
        b64 = base64.b64encode(data_b).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def ensure_history_directories(self, memory_paths: Iterable[Path]) -> None:
        """Make sure the directories for history files exist."""
        for path in memory_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
