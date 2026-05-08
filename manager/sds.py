import json
import logging
import threading

import requests
from sqlalchemy import inspect, text


class SDSMixin:
    """Provide SDS networking support for SAIVerseManager."""

    @staticmethod
    def _ensure_city_timezone_column(engine) -> None:
        try:
            inspector = inspect(engine)
            columns = {col["name"] for col in inspector.get_columns("city")}
        except Exception as exc:
            logging.warning("Failed to inspect city table for timezone column: %s", exc)
            return
        if "TIMEZONE" in columns:
            return
        logging.info("Adding TIMEZONE column to city table.")
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE city ADD COLUMN TIMEZONE TEXT DEFAULT 'UTC' NOT NULL"))
                conn.execute(text("UPDATE city SET TIMEZONE = 'UTC' WHERE TIMEZONE IS NULL"))
        except Exception as exc:
            logging.error("Failed to add TIMEZONE column to city table: %s", exc)

    @staticmethod
    def _ensure_user_avatar_column(engine) -> None:
        try:
            inspector = inspect(engine)
            columns = {col["name"] for col in inspector.get_columns("user")}
        except Exception as exc:
            logging.warning("Failed to inspect user table for avatar column: %s", exc)
            return
        if "AVATAR_IMAGE" in columns:
            return
        logging.info("Adding AVATAR_IMAGE column to user table.")
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE user ADD COLUMN AVATAR_IMAGE TEXT"))
        except Exception as exc:
            logging.error("Failed to add AVATAR_IMAGE column to user table: %s", exc)

    @staticmethod
    def _ensure_city_host_avatar_column(engine) -> None:
        try:
            inspector = inspect(engine)
            columns = {col["name"] for col in inspector.get_columns("city")}
        except Exception as exc:
            logging.warning("Failed to inspect city table for host avatar column: %s", exc)
            return
        if "HOST_AVATAR_IMAGE" in columns:
            return
        logging.info("Adding HOST_AVATAR_IMAGE column to city table.")
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE city ADD COLUMN HOST_AVATAR_IMAGE TEXT"))
        except Exception as exc:
            logging.error("Failed to add HOST_AVATAR_IMAGE column to city table: %s", exc)

    # SDS backoff constants
    _SDS_BASE_INTERVAL = 30      # Normal polling interval (seconds)
    _SDS_MAX_INTERVAL = 300      # Max backoff interval (5 minutes)
    _SDS_BACKOFF_FACTOR = 2      # Exponential backoff multiplier
    _SDS_SCHEDULER_KEY = "sds_heartbeat"

    def _sds_tick_and_reschedule(self):
        """1 回分の SDS heartbeat + 動的 backoff で次回再 schedule。

        Phase 4-e で旧 ``_sds_background_loop`` (固定 sleep ループ) を廃止。
        失敗時の指数 backoff は callback 内で次回 fire_at を変えて表現する。
        ``schedule_periodic`` だと固定 interval しか持てないので、ここは
        ``schedule()`` を毎回呼ぶ手動再 register 方式を採用。
        """
        from datetime import datetime, timedelta
        try:
            success = self._send_heartbeat()
            self._update_cities_from_sds()
            if success:
                self._sds_consecutive_failures = 0
                interval = self._SDS_BASE_INTERVAL
            else:
                self._sds_consecutive_failures = getattr(self, "_sds_consecutive_failures", 0) + 1
                interval = min(
                    self._SDS_BASE_INTERVAL * (self._SDS_BACKOFF_FACTOR ** self._sds_consecutive_failures),
                    self._SDS_MAX_INTERVAL,
                )
        except Exception:
            logging.exception("Error in SDS tick")
            self._sds_consecutive_failures = getattr(self, "_sds_consecutive_failures", 0) + 1
            interval = min(
                self._SDS_BASE_INTERVAL * (self._SDS_BACKOFF_FACTOR ** self._sds_consecutive_failures),
                self._SDS_MAX_INTERVAL,
            )

        # 次回を schedule (停止時は呼ばれない: stop で cancel される)
        scheduler = getattr(self, "event_scheduler", None)
        if scheduler is None or getattr(self, "sds_stop_event", None) is None or self.sds_stop_event.is_set():
            return
        scheduler.schedule(
            fire_at=datetime.now() + timedelta(seconds=interval),
            callback=self._sds_tick_and_reschedule,
            key=self._SDS_SCHEDULER_KEY,
        )

    def _register_with_sds(self):
        register_url = f"{self.sds_url}/register"
        payload = {
            "city_name": self.city_name,
            "city_id_pk": self.city_id,
            "api_port": self.api_port,
        }
        try:
            response = self.sds_session.post(register_url, json=payload, timeout=5)
            response.raise_for_status()
            logging.info("Successfully registered with SDS at %s", self.sds_url)
        except requests.exceptions.RequestException as exc:
            logging.error("Could not register with SDS: %s. Will retry in the background.", exc)

    def _send_heartbeat(self) -> bool:
        """Send heartbeat to SDS. Returns True on success, False on failure."""
        heartbeat_url = f"{self.sds_url}/heartbeat"
        payload = {"city_name": self.city_name}
        try:
            response = self.sds_session.post(heartbeat_url, json=payload, timeout=2)
            response.raise_for_status()
            if self.sds_status != "Online":
                logging.info("SDS heartbeat restored for %s", self.city_name)
            logging.debug("Heartbeat sent to SDS for %s", self.city_name)
            return True
        except requests.exceptions.RequestException as exc:
            # First failure: WARNING, subsequent: DEBUG (avoid log flooding)
            if self.sds_status == "Online":
                logging.warning("Could not send heartbeat to SDS: %s", exc)
            else:
                logging.debug("Could not send heartbeat to SDS: %s", exc)
            return False

    def _update_cities_from_sds(self):
        cities_url = f"{self.sds_url}/cities"
        try:
            response = self.sds_session.get(cities_url, timeout=5)
            response.raise_for_status()
            cities_data = response.json()
            if self.city_name in cities_data:
                del cities_data[self.city_name]

            if self.cities_config != cities_data:
                logging.info("Updated city directory from SDS: %s", list(cities_data.keys()))
                self.cities_config = cities_data

            if self.sds_status != "Online":
                logging.info("Connection to SDS established.")
            self.sds_status = "Online"

        except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
            if self.sds_status == "Online":
                logging.warning("Lost connection to SDS: %s. Falling back to local DB config.", exc)
                self._load_cities_from_db()
            else:
                logging.debug("Could not update city list from SDS: %s", exc)
            self.sds_status = "Offline (SDS Unreachable)"

    def _load_cities_from_db(self):
        db = self.SessionLocal()
        try:
            other_cities = db.query(self.city_model).filter(self.city_model.CITYID != self.city_id).all()
            self.cities_config = {
                city.CITYNAME: {
                    "city_id": city.CITYID,
                    "api_base_url": f"http://127.0.0.1:{city.API_PORT}",
                    "timezone": getattr(city, "TIMEZONE", "UTC") or "UTC",
                }
                for city in other_cities
            }
            logging.info("Loaded/reloaded city config from local DB. Found %d other cities.", len(self.cities_config))
        finally:
            db.close()

    def switch_to_offline_mode(self):
        if self.sds_status == "Offline (Forced by User)":
            logging.info("Already in forced offline mode.")
            return self.sds_status

        logging.info("User requested to switch to offline mode.")
        if self.sds_thread and self.sds_thread.is_alive():
            self.sds_stop_event.set()
            self.sds_thread.join(timeout=2)

        self._load_cities_from_db()
        self.sds_status = "Offline (Forced by User)"
        logging.info("Switched to offline mode. SDS communication is stopped.")
        return self.sds_status

    def switch_to_online_mode(self):
        logging.info("User requested to switch to online mode.")

        if self.sds_thread and self.sds_thread.is_alive():
            logging.info("SDS thread is already running. Forcing an update.")
            self._update_cities_from_sds()
            return self.sds_status

        logging.info("SDS thread is not running. Attempting to start it.")
        self.sds_status = "Online (Connecting...)"
        self.sds_stop_event.clear()

        self._register_with_sds()
        self._update_cities_from_sds()

        self.sds_thread = threading.Thread(target=self._sds_background_loop, daemon=True)
        self.sds_thread.start()
        logging.info("SDS background thread re-started.")
        return self.sds_status
