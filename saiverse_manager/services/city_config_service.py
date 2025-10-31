"""Services for loading and managing city configuration."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Tuple

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from zoneinfo import ZoneInfo

from database.models import City as CityModel


@dataclass
class CityConfig:
    """Lightweight container describing a city's configuration."""

    city_id: int
    city_name: str
    ui_port: int
    api_port: int
    start_in_online_mode: bool
    timezone_name: str
    timezone_info: ZoneInfo


class CityConfigService:
    """Encapsulates all logic required to load the city configuration."""

    def __init__(self, city_name: str, db_path: str) -> None:
        self.city_name = city_name
        self.db_path = db_path
        database_url = f"sqlite:///{db_path}"
        self.engine = create_engine(database_url, connect_args={"check_same_thread": False})
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self._ensure_city_timezone_column()

    def create_session(self) -> Session:
        """Return a new SQLAlchemy session."""
        return self.session_factory()

    def load_city_configuration(self) -> Tuple[CityConfig, Dict[str, Dict[str, object]]]:
        """Load the city configuration and the other cities from the database."""
        session = self.create_session()
        try:
            my_city_config = (
                session.query(CityModel)
                .filter(CityModel.CITYNAME == self.city_name)
                .first()
            )
            if not my_city_config:
                raise ValueError(
                    f"City '{self.city_name}' not found in the database. Please run 'python database/seed.py' first."
                )

            timezone_name = getattr(my_city_config, "TIMEZONE", "UTC") or "UTC"
            timezone_name = timezone_name.strip() or "UTC"
            timezone_info = self._resolve_timezone(timezone_name)

            config = CityConfig(
                city_id=my_city_config.CITYID,
                city_name=my_city_config.CITYNAME,
                ui_port=my_city_config.UI_PORT,
                api_port=my_city_config.API_PORT,
                start_in_online_mode=my_city_config.START_IN_ONLINE_MODE,
                timezone_name=timezone_name,
                timezone_info=timezone_info,
            )

            other_cities = self._load_other_cities(session, my_city_config.CITYID)
            return config, other_cities
        finally:
            session.close()

    def load_other_cities(
        self, current_city_id: int | None = None
    ) -> Dict[str, Dict[str, object]]:
        """Public helper to fetch other city configurations."""
        session = self.create_session()
        try:
            return self._load_other_cities(session, current_city_id)
        finally:
            session.close()

    def _ensure_city_timezone_column(self) -> None:
        """Ensure that the CITY table has a TIMEZONE column."""
        try:
            inspector = inspect(self.engine)
            columns = {col["name"] for col in inspector.get_columns("city")}
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.warning(
                "Failed to inspect city table for timezone column: %s", exc
            )
            return

        if "TIMEZONE" in columns:
            return

        logging.info("Adding TIMEZONE column to city table.")
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE city ADD COLUMN TIMEZONE TEXT DEFAULT 'UTC' NOT NULL"
                    )
                )
                conn.execute(
                    text("UPDATE city SET TIMEZONE = 'UTC' WHERE TIMEZONE IS NULL")
                )
        except Exception as exc:  # pragma: no cover - depends on DB state
            logging.error("Failed to add TIMEZONE column to city table: %s", exc)

    def _load_other_cities(
        self, session: Session, current_city_id: int | None
    ) -> Dict[str, Dict[str, object]]:
        query = session.query(CityModel)
        if current_city_id is not None:
            query = query.filter(CityModel.CITYID != current_city_id)

        other_cities = query.all()
        return {
            city.CITYNAME: {
                "city_id": city.CITYID,
                "api_base_url": f"http://127.0.0.1:{city.API_PORT}",
                "timezone": getattr(city, "TIMEZONE", "UTC") or "UTC",
            }
            for city in other_cities
        }

    @staticmethod
    def _resolve_timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:  # pragma: no cover - depends on system tz database
            logging.warning("Invalid timezone '%s'. Falling back to UTC.", name)
            return ZoneInfo("UTC")
