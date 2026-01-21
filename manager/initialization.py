"""Initialization helpers extracted from SAIVerseManager.__init__."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from buildings import Building
from database.models import City as CityModel

if TYPE_CHECKING:
    pass

LOGGER = logging.getLogger(__name__)


class InitializationMixin:
    """Initialization helper methods for SAIVerseManager."""

    def _init_database(self, db_path: str) -> None:
        """Step 0: Database and Configuration Setup."""
        self.db_path = db_path
        self.city_model = CityModel
        self.city_host_avatar_path: Optional[str] = None
        DATABASE_URL = f"sqlite:///{db_path}"
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
        self._ensure_city_timezone_column(engine)
        self._ensure_user_avatar_column(engine)
        self._ensure_city_host_avatar_column(engine)
        self._ensure_item_tables(engine)
        self._ensure_phenomenon_tables(engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _init_city_config(self, city_name: str) -> None:
        """Step 1: Load City Configuration from DB."""
        db = self.SessionLocal()
        try:
            my_city_config = db.query(CityModel).filter(CityModel.CITYNAME == city_name).first()
            if not my_city_config:
                raise ValueError(
                    f"City '{city_name}' not found in the database. "
                    "Please run 'python database/seed.py' first."
                )
            
            self.city_id = my_city_config.CITYID
            self.city_name = my_city_config.CITYNAME
            self.user_room_id = f"user_room_{self.city_name}"
            self.ui_port = my_city_config.UI_PORT
            self.api_port = my_city_config.API_PORT
            self.start_in_online_mode = my_city_config.START_IN_ONLINE_MODE
            self._update_timezone_cache(getattr(my_city_config, "TIMEZONE", "UTC"))
            self.city_host_avatar_path = getattr(my_city_config, "HOST_AVATAR_IMAGE", None)
            
            # Load other cities' configs for inter-city communication
            other_cities = db.query(CityModel).filter(CityModel.CITYID != self.city_id).all()
            self.cities_config = {
                city.CITYNAME: {
                    "city_id": city.CITYID,
                    "api_base_url": f"http://127.0.0.1:{city.API_PORT}",
                    "timezone": getattr(city, "TIMEZONE", "UTC") or "UTC",
                } for city in other_cities
            }
            LOGGER.info(
                "Loaded config for '%s' (ID: %s). Found %d other cities.",
                self.city_name, self.city_id, len(self.cities_config)
            )
        finally:
            db.close()

    def _init_buildings(self) -> None:
        """Step 1b: Load Static Assets from DB."""
        self.buildings: List[Building] = self._load_and_create_buildings_from_db()
        self.building_map: Dict[str, Building] = {b.building_id: b for b in self.buildings}
        self.capacities: Dict[str, int] = {b.building_id: b.capacity for b in self.buildings}
        
        # Item containers (populated later by ItemService)
        self.items: Dict[str, Dict[str, Any]] = {}
        self.item_locations: Dict[str, Dict[str, str]] = {}
        self.items_by_building: Dict[str, List[str]] = defaultdict(list)
        self.items_by_persona: Dict[str, List[str]] = defaultdict(list)
        self.world_items: List[str] = []
        
        # Persona events
        self.persona_pending_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._load_persona_event_logs()

    def _init_file_paths(self) -> None:
        """Step 2: Setup File Paths and Default Avatars."""
        from data_paths import get_saiverse_home
        self.saiverse_home = get_saiverse_home()
        self.backup_dir = self.saiverse_home / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.building_memory_paths: Dict[str, Path] = {
            b.building_id: self.saiverse_home / "cities" / self.city_name / "buildings" / b.building_id / "log.json"
            for b in self.buildings
        }

    def _init_avatars(self) -> None:
        """Step 2b: Load default avatars with graceful fallback."""
        avatar_fallback_paths = [
            Path("builtin_data/icons/blank.png"),
            Path("builtin_data/icons/user.png"),
            Path("builtin_data/icons/host.png"),
            Path("assets/icons/host.png"),  # Legacy fallback
        ]
        default_avatar_data = ""
        for avatar_path in avatar_fallback_paths:
            data_url = self._load_avatar_data(avatar_path)
            if data_url:
                default_avatar_data = data_url
                break
        self.default_avatar = default_avatar_data

        host_avatar_data = self._load_avatar_data(Path("builtin_data/icons/host.png"))
        self.host_avatar = host_avatar_data or self.default_avatar
        if getattr(self, "city_host_avatar_path", None):
            host_override = self._load_avatar_data(Path(self.city_host_avatar_path))
            if host_override:
                self.host_avatar = host_override
        self.user_avatar_data = self.default_avatar

    def _init_building_histories(self) -> None:
        """Step 3: Load Conversation Histories."""
        self.building_histories: Dict[str, List[Dict[str, str]]] = {}
        for b_id, path in self.building_memory_paths.items():
            if path.exists():
                try:
                    self.building_histories[b_id] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    LOGGER.warning("Failed to load building history %s", b_id)
                    self.building_histories[b_id] = []
            else:
                self.building_histories[b_id] = []

    def _init_model_config(self, model: Optional[str]) -> None:
        """Step 4a: Initialize model configuration."""
        from model_configs import get_context_length, get_model_provider
        import os

        def _get_default_model() -> str:
            return os.getenv("SAIVERSE_DEFAULT_MODEL", "gemini-2.0-flash")

        base_model = model or _get_default_model()
        self.model = "None"  # No global override by default
        self.context_length = get_context_length(base_model)
        self.provider = get_model_provider(base_model)
        self._base_model = base_model
        self.model_parameter_overrides: Dict[str, Any] = {}

    def _update_timezone_cache(self, tz_name: Optional[str]) -> None:
        """Update cached timezone information for this manager."""
        name = (tz_name or "UTC").strip() or "UTC"
        try:
            tz = ZoneInfo(name)
        except Exception:
            LOGGER.warning("Invalid timezone '%s'. Falling back to UTC.", name)
            name = "UTC"
            tz = ZoneInfo("UTC")
        self.timezone_name = name
        self.timezone_info = tz


__all__ = ["InitializationMixin"]
