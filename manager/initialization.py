"""Initialization helpers extracted from SAIVerseManager.__init__."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from saiverse.buildings import Building
from database.models import City as CityModel

if TYPE_CHECKING:
    pass

LOGGER = logging.getLogger(__name__)


class InitializationMixin:
    """Initialization helper methods for SAIVerseManager."""

    @staticmethod
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """Enable WAL mode and busy_timeout for concurrent read/write safety."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def _init_database(self, db_path: str) -> None:
        """Step 0: Database and Configuration Setup."""
        self.db_path = db_path
        self.city_model = CityModel
        self.city_host_avatar_path: Optional[str] = None
        DATABASE_URL = f"sqlite:///{db_path}"
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
        event.listen(engine, "connect", self._set_sqlite_pragmas)
        self._ensure_city_timezone_column(engine)
        self._ensure_user_avatar_column(engine)
        self._ensure_city_host_avatar_column(engine)
        self._ensure_item_tables(engine)
        self._ensure_phenomenon_tables(engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        # Configure UsageTracker to use the same database
        from saiverse.usage_tracker import get_usage_tracker
        get_usage_tracker().configure(self.SessionLocal)

    def _init_city_config(self, city_name: str) -> None:
        """Step 1: Load City Configuration from DB."""
        db = self.SessionLocal()
        try:
            my_city_config = db.query(CityModel).filter(CityModel.CITYNAME == city_name).first()
            if not my_city_config:
                # Fallback: find by CITYID=1 and auto-repair CITYNAME.
                # This handles cases where the tutorial overwrote the internal
                # city identifier (e.g. with a non-ASCII display name).
                my_city_config = db.query(CityModel).filter(CityModel.CITYID == 1).first()
                if my_city_config:
                    old_name = my_city_config.CITYNAME
                    LOGGER.warning(
                        "City '%s' not found but CITYID=1 exists with CITYNAME='%s'. "
                        "Auto-repairing CITYNAME to '%s'.",
                        city_name, old_name, city_name,
                    )
                    my_city_config.CITYNAME = city_name
                    db.commit()
                else:
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
        from saiverse.data_paths import get_saiverse_home
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
        """Step 3: Load Conversation Histories.

        Treats 5 file states distinctly (NEVER conflate them):
          1. **不在** (no file): new building. building_histories[b_id] = [].
          2. **0バイト** (zero-byte): abnormal — write_text was interrupted.
             Rescue + quarantine. Key NOT inserted.
          3. **空配列** (``[]``): valid — building_histories[b_id] = [].
          4. **正常配列**: valid — building_histories[b_id] = data.
          5. **破損** (invalid JSON): abnormal. Rescue + quarantine. Key NOT inserted.

        **Quarantine semantics**: a building in ``self.quarantined_buildings``
        is treated as "no truth available". Save refuses to touch the file,
        movement refuses entry. The UI shows the user options to restore from
        backup / reset / handle manually. This guarantees that a corrupted file
        is NEVER overwritten by the system — only by explicit user action.

        For successfully loaded files, also performs a **startup backup snapshot**
        (``log.json.backup_<timestamp>.bak``, rotated to keep last N) so users
        always have a recent known-good state to restore from.
        """
        from datetime import datetime
        from manager.history import (
            create_log_backup_snapshot,
            list_log_backups,
        )

        self.building_histories: Dict[str, List[Dict[str, str]]] = {}
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for b_id, path in self.building_memory_paths.items():
            # State 1: 不在
            if not path.exists():
                self.building_histories[b_id] = []
                continue

            # State 2: 0バイト
            if path.stat().st_size == 0:
                self._quarantine_building(
                    b_id, path, timestamp,
                    reason="zero_byte",
                    title_suffix="0バイト",
                    message_extra="ファイルが空（0バイト）になっており、書き込みが途中で中断された痕跡です。",
                )
                continue

            # States 3, 4, 5: try to parse
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                # State 5: 破損
                self._quarantine_building(
                    b_id, path, timestamp,
                    reason="corrupted",
                    title_suffix="破損",
                    message_extra=f"JSONパース失敗: {exc}",
                )
                continue

            if not isinstance(data, list):
                # 構造異常（dictやnullなど）
                self._quarantine_building(
                    b_id, path, timestamp,
                    reason="invalid_structure",
                    title_suffix="構造異常",
                    message_extra=f"配列でなく {type(data).__name__} が格納されていました。",
                )
                continue

            # States 3, 4: valid load
            self.building_histories[b_id] = data

            # 起動成功時バックアップスナップショット
            try:
                create_log_backup_snapshot(path, timestamp)
            except Exception:
                LOGGER.warning(
                    "Failed to create startup backup for %s", b_id, exc_info=True,
                )
            # 既存のバックアップ一覧をログ（隔離時の選択肢になるため）
            backups = list_log_backups(path)
            LOGGER.debug(
                "Building %s loaded (%d msgs); %d backup(s) available",
                b_id, len(data), len(backups),
            )

    def _quarantine_building(
        self,
        b_id: str,
        path: "Path",
        timestamp: str,
        *,
        reason: str,
        title_suffix: str,
        message_extra: str,
    ) -> None:
        """Move corrupted/invalid file to a backup name and mark building as quarantined.

        Quarantined buildings:
          - have NO key in self.building_histories (so save skips them)
          - are listed in self.quarantined_buildings (with restore options)
          - block movement (handled in OccupancyManager)
          - are surfaced via self.startup_alerts (banner)
        """
        from manager.history import list_log_backups

        backup_path = path.parent / f"{path.name}.corrupted_{timestamp}"
        rescue_error: Optional[str] = None
        rescued = False
        try:
            path.rename(backup_path)
            rescued = True
            LOGGER.error(
                "Building history for %s is %s; rescued to %s",
                b_id, reason, backup_path,
            )
        except OSError as rename_exc:
            rescue_error = str(rename_exc)
            LOGGER.error(
                "Failed to rescue %s log for %s: %s",
                reason, b_id, rename_exc,
            )

        available_backups = [str(p) for p in list_log_backups(path)]

        # 隔離レコード — UI から復旧操作する時のソースオブトゥルース
        self.quarantined_buildings[b_id] = {
            "building_id": b_id,
            "reason": reason,
            "original_path": str(path),
            "corrupted_path": str(backup_path) if rescued else None,
            "rescue_error": rescue_error,
            "available_backups": available_backups,
            "detected_at": timestamp,
        }

        # アラート
        if rescued:
            alert = {
                "id": f"quarantine_{b_id}_{timestamp}",
                "level": "critical",
                "title": f"会話履歴ファイルが{title_suffix}: {b_id}",
                "message": (
                    f"ビルディング「{b_id}」のチャット履歴ファイルが異常状態でした。"
                    f"{message_extra} 破損ファイルを安全な場所に退避し、このビルディングは"
                    "**隔離状態**にしました。新規会話・入室は制限されています。"
                    f"利用可能なバックアップが{len(available_backups)}個あります。"
                    "アラート横の「対応する」ボタンから復元・リセット等を選択してください。"
                ),
                "details": {
                    "building_id": b_id,
                    "reason": reason,
                    "original_path": str(path),
                    "corrupted_path": str(backup_path),
                    "available_backups": available_backups,
                    "recovery_instructions": (
                        "1) アラート横の「対応する」ボタンから復元方法を選ぶ、"
                        "2) または手動で退避ファイルを log.json にリネームして再起動する"
                    ),
                },
            }
        else:
            alert = {
                "id": f"quarantine_rescue_failed_{b_id}_{timestamp}",
                "level": "critical",
                "title": f"会話履歴ファイル{title_suffix} + 退避失敗: {b_id}",
                "message": (
                    "ビルディングのチャット履歴ファイルが異常で、さらに退避にも失敗しました。"
                    "**システムは自動上書きを停止しました**ので、安全な場所にファイルをコピーして"
                    "から手動で対応してください。"
                ),
                "details": {
                    "building_id": b_id,
                    "reason": reason,
                    "original_path": str(path),
                    "rescue_error": rescue_error,
                    "available_backups": available_backups,
                },
            }
        self.startup_alerts.append(alert)

    def _init_model_config(self, model: Optional[str]) -> None:
        """Step 4a: Initialize model configuration."""
        from saiverse.model_configs import get_context_length, get_model_provider
        import os

        def _get_default_model() -> str:
            from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
            return os.getenv("SAIVERSE_DEFAULT_MODEL", BUILTIN_DEFAULT_LITE_MODEL)

        base_model = model or _get_default_model()
        self.model = None  # No global override by default
        self.startup_warnings: List[Dict[str, str]] = []
        try:
            self.context_length = get_context_length(base_model)
            self.provider = get_model_provider(base_model)
        except ValueError:
            from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
            fallback = BUILTIN_DEFAULT_LITE_MODEL
            city = getattr(self, "city_name", "unknown")
            msg = (
                f"City '{city}' のデフォルトモデル '{base_model}' の設定ファイルが見つかりません。"
                f"デフォルトモデル '{fallback}' にフォールバックしました。"
            )
            LOGGER.warning(
                "Model config '%s' not found. Falling back to '%s'. "
                "Check that the model JSON file exists in builtin_data/models/ or user_data/models/.",
                base_model, fallback,
            )
            self.startup_warnings.append({
                "source": "model_config",
                "message": msg,
            })
            base_model = fallback
            self.context_length = get_context_length(base_model)
            self.provider = get_model_provider(base_model)
        self._base_model = base_model
        self.model_parameter_overrides: Dict[str, Any] = {}
        self.max_history_messages_override: Optional[int] = None
        self.metabolism_enabled: bool = True
        self.metabolism_keep_messages_override: Optional[int] = None
        self.max_image_embeds_override: Optional[int] = None

    def _update_timezone_cache(self, tz_name: Optional[str]) -> None:
        """Update cached timezone information for this manager.

        Updates the manager's own attributes AND the CoreState object
        (if it exists) so that AdminService / PersonaMixin always see
        the latest timezone when creating or loading personas.
        """
        name = (tz_name or "UTC").strip() or "UTC"
        try:
            tz = ZoneInfo(name)
        except Exception:
            LOGGER.warning("Invalid timezone '%s'. Falling back to UTC.", name)
            name = "UTC"
            tz = ZoneInfo("UTC")
        self.timezone_name = name
        self.timezone_info = tz
        # Propagate to CoreState so AdminService (which reads from state)
        # also picks up the change.
        state = getattr(self, "state", None)
        if state is not None:
            state.timezone_name = name
            state.timezone_info = tz


__all__ = ["InitializationMixin"]
