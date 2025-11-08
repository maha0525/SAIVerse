import base64
import json
from collections import defaultdict
from sqlalchemy import create_engine
import threading
import requests
import logging
from pathlib import Path
import mimetypes
from typing import Dict, List, Optional, Tuple, Iterator, Union, Any
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import importlib
import tools.defs
from discord_gateway.mapping import ChannelMapping
import os

from google.genai import errors
from buildings import Building
from persona_core import PersonaCore
from model_configs import get_model_provider, get_context_length
from occupancy_manager import OccupancyManager
from conversation_manager import ConversationManager
from sqlalchemy.orm import sessionmaker
from remote_persona_proxy import RemotePersonaProxy
from manager.sds import SDSMixin
from manager.background import DatabasePollingMixin
from manager.history import HistoryMixin
from manager.blueprints import BlueprintMixin
from manager.persona import PersonaMixin
from manager.visitors import VisitorMixin
from manager.state import CoreState
from manager.runtime import RuntimeService
from manager.admin import AdminService
from database.models import (
    AI as AIModel,
    Building as BuildingModel,
    BuildingOccupancyLog,
    User as UserModel,
    City as CityModel,
    VisitingAI,
    ThinkingRequest,
    Tool as ToolModel,
    BuildingToolLink,
    Item as ItemModel,
    ItemLocation as ItemLocationModel,
    PersonaEventLog,
)


#DEFAULT_MODEL = "gpt-4o"
DEFAULT_MODEL = "gemini-2.0-flash"


class SAIVerseManager(VisitorMixin, PersonaMixin, HistoryMixin, BlueprintMixin, SDSMixin, DatabasePollingMixin):
    """Manage multiple personas and building occupancy."""

    def __init__(
        self,
        city_name: str,
        db_path: str,
        model: str = DEFAULT_MODEL,
        sds_url: str = os.getenv("SDS_URL", "http://127.0.0.1:8080"),
    ):
        # --- Step 0: Database and Configuration Setup ---
        self.db_path = db_path
        self.city_model = CityModel
        DATABASE_URL = f"sqlite:///{db_path}"
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
        self._ensure_city_timezone_column(engine)
        self._ensure_item_tables(engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        
        # --- Step 1: Load City Configuration from DB ---
        db = self.SessionLocal()
        try:
            my_city_config = db.query(CityModel).filter(CityModel.CITYNAME == city_name).first()
            if not my_city_config:
                raise ValueError(f"City '{city_name}' not found in the database. Please run 'python database/seed.py' first.")
            
            self.city_id = my_city_config.CITYID # This is the integer PK
            self.city_name = my_city_config.CITYNAME # This is the string identifier
            self.user_room_id = f"user_room_{self.city_name}"
            self.ui_port = my_city_config.UI_PORT
            self.api_port = my_city_config.API_PORT
            self.start_in_online_mode = my_city_config.START_IN_ONLINE_MODE
            self._update_timezone_cache(getattr(my_city_config, "TIMEZONE", "UTC"))
            
            # Load other cities' configs for inter-city communication
            other_cities = db.query(CityModel).filter(CityModel.CITYID != self.city_id).all()
            self.cities_config = {
                city.CITYNAME: {
                    "city_id": city.CITYID,
                    "api_base_url": f"http://127.0.0.1:{city.API_PORT}",
                    "timezone": getattr(city, "TIMEZONE", "UTC") or "UTC",
                } for city in other_cities
            }
            logging.info(f"Loaded config for '{self.city_name}' (ID: {self.city_id}). Found {len(self.cities_config)} other cities.")

        finally:
            db.close()

        # --- Step 1: Load Static Assets from DB ---
        # データベースから建物の静的な情報を読み込み、メモリ上にBuildingオブジェクトとして展開します。
        self.buildings: List[Building] = self._load_and_create_buildings_from_db()
        self.building_map: Dict[str, Building] = {b.building_id: b for b in self.buildings}
        self.capacities: Dict[str, int] = {b.building_id: b.capacity for b in self.buildings}
        self.items: Dict[str, Dict[str, Any]] = {}
        self.item_locations: Dict[str, Dict[str, str]] = {}
        self.items_by_building: Dict[str, List[str]] = defaultdict(list)
        self.items_by_persona: Dict[str, List[str]] = defaultdict(list)
        self.world_items: List[str] = []
        self._load_items_from_db()
        self.persona_pending_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._load_persona_event_logs()

        # --- Step 2: Setup File Paths and Default Avatars ---
        # 各種ログファイルのパスや、デフォルトのアバター画像を設定します。
        self.saiverse_home = Path.home() / ".saiverse"
        self.backup_dir = self.saiverse_home / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.building_memory_paths: Dict[str, Path] = {
            b.building_id: self.saiverse_home / "cities" / self.city_name / "buildings" / b.building_id / "log.json"
            for b in self.buildings
        }
        # Load default avatars with graceful fallback
        avatar_fallback_paths = [
            Path("assets/icons/blank.png"),
            Path("assets/icons/user.png"),
            Path("assets/icons/host.png"),
            Path("assets/icons/air.png"),
        ]
        default_avatar_data = ""
        for avatar_path in avatar_fallback_paths:
            data_url = self._load_avatar_data(avatar_path)
            if data_url:
                default_avatar_data = data_url
                break
        self.default_avatar = default_avatar_data

        host_avatar_data = self._load_avatar_data(Path("assets/icons/host.png"))
        self.host_avatar = host_avatar_data or self.default_avatar

        # --- Step 3: Load Conversation Histories ---
        # 各建物の会話履歴をファイルから読み込みます。
        self.building_histories: Dict[str, List[Dict[str, str]]] = {}
        for b_id, path in self.building_memory_paths.items():
            if path.exists():
                try:
                    self.building_histories[b_id] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logging.warning("Failed to load building history %s", b_id)
                    self.building_histories[b_id] = []
            else:
                self.building_histories[b_id] = []

        # --- Step 4: Initialize Core Components and State Containers ---
        self.model = model
        self.context_length = get_context_length(model)
        self.provider = get_model_provider(model)

        self.state = CoreState(
            session_factory=self.SessionLocal,
            city_id=self.city_id,
            city_name=self.city_name,
            model=self.model,
            provider=self.provider,
            context_length=self.context_length,
            saiverse_home=self.saiverse_home,
            user_room_id=self.user_room_id,
            buildings=self.buildings,
            building_map=self.building_map,
            building_memory_paths=self.building_memory_paths,
            building_histories=self.building_histories,
            capacities=self.capacities,
            items=self.items,
            item_locations=self.item_locations,
            items_by_building={k: list(v) for k, v in self.items_by_building.items()},
            items_by_persona={k: list(v) for k, v in self.items_by_persona.items()},
            world_items=list(self.world_items),
            persona_pending_events={
                k: [dict(ev) for ev in events] for k, events in self.persona_pending_events.items()
            },
            occupants={b.building_id: [] for b in self.buildings},
            default_avatar=self.default_avatar,
            host_avatar=self.host_avatar,
            start_in_online_mode=self.start_in_online_mode,
            ui_port=self.ui_port,
            api_port=self.api_port,
        )
        self.state.items = self.items
        self.state.item_locations = self.item_locations
        self.state.items_by_building = self.items_by_building
        self.state.items_by_persona = self.items_by_persona
        self.state.world_items = self.world_items
        self.state.persona_pending_events = self.persona_pending_events

        self.personas = self.state.personas
        self.visiting_personas = self.state.visiting_personas
        self.avatar_map = self.state.avatar_map
        self.persona_map = self.state.persona_map
        self.occupants = self.state.occupants
        self.id_to_name_map = self.state.id_to_name_map
        self.user_id = self.state.user_id
        self.default_avatar = self.state.default_avatar
        self.host_avatar = self.state.host_avatar
        self._refresh_user_state_cache()

        # --- Step 5: Initialize OccupancyManager ---
        self.occupancy_manager = OccupancyManager(
            session_factory=self.SessionLocal,
            city_id=self.city_id,
            occupants=self.occupants,
            capacities=self.capacities,
            building_map=self.building_map,
            building_histories=self.building_histories,
            id_to_name_map=self.id_to_name_map,
            user_id=self.state.user_id,
        )
        logging.info("Initialized OccupancyManager.")

        # --- Step 5: Load Dynamic States from DB ---
        # データベースから動的な状態（ペルソナ、ユーザー状態、入室状況）を読み込み、
        # メモリ上のオブジェクトに反映させます。
        self._load_personas_from_db()
        self._load_user_state_from_db()
        self.state.persona_map.clear()
        self.state.persona_map.update({p.persona_name: p.persona_id for p in self.personas.values()})
        self.persona_map = self.state.persona_map
        self.id_to_name_map.update({pid: p.persona_name for pid, p in self.personas.items()})
        self._load_occupancy_from_db()

        # --- Step 6: Prepare Background Task Managers ---
        # 自律会話を管理するConversationManagerを準備します（この時点ではまだ起動しません）。
        self.conversation_managers: Dict[str, ConversationManager] = {}
        for b_id in self.building_map.keys(): # building_map is already filtered by city
            # user_roomはユーザー操作起点なので自律会話は不要
            if not b_id.startswith("user_room"):
                building = self.building_map[b_id]
                manager = ConversationManager(
                    building_id=b_id,
                    saiverse_manager=self,
                    interval=building.auto_interval_sec
                )
                self.conversation_managers[b_id] = manager
        logging.info(f"Initialized {len(self.conversation_managers)} conversation managers.")

        # --- Step 7: Register with SDS and start background tasks ---
        self.sds_url = sds_url
        self.sds_session = requests.Session()
        self.sds_status = "Offline (Connecting...)"
        self.sds_stop_event = threading.Event()
        self.sds_thread = None
        
        if self.start_in_online_mode:
            logging.info("Starting in Online Mode as per DB setting.")
            self._load_cities_from_db() # Load local config as a fallback first
            self._register_with_sds()
            self._update_cities_from_sds()
            
            # Start background thread for SDS communication
            self.sds_thread = threading.Thread(target=self._sds_background_loop, daemon=True)
            self.sds_thread.start()
        else:
            logging.info("Starting in Offline Mode as per DB setting.")
            self.sds_status = "Offline (Startup Setting)"
            self._load_cities_from_db()
        self.gateway_runtime = None
        self.gateway_mapping = ChannelMapping([])
        self._gateway_memory_transfers: Dict[str, Dict[str, Any]] = {}
        self._gateway_memory_active_persona: Dict[str, str] = {}
        gateway_enabled = os.getenv("SAIVERSE_GATEWAY_ENABLED", "0").lower() in {
            "1",
            "true",
            "yes",
        }
        if gateway_enabled:
            try:
                self._initialize_gateway_integration()
            except Exception as exc:
                logging.exception(
                    "Failed to initialize Discord gateway integration: %s", exc
                )

        self.runtime = RuntimeService(self, self.state)
        self.admin = AdminService(self, self.runtime, self.state)

        # Start background thread for DB polling (after runtime is ready)
        self.db_polling_stop_event = threading.Event()
        self.db_polling_thread = threading.Thread(
            target=self._db_polling_loop, daemon=True
        )
        self.db_polling_thread.start()

    def _update_timezone_cache(self, tz_name: Optional[str]) -> None:
        """Update cached timezone information for this manager."""
        name = (tz_name or "UTC").strip() or "UTC"
        try:
            tz = ZoneInfo(name)
        except Exception:
            logging.warning("Invalid timezone '%s'. Falling back to UTC.", name)
            name = "UTC"
            tz = ZoneInfo("UTC")
        self.timezone_name = name
        self.timezone_info = tz

    @staticmethod
    def _load_avatar_data(path: Path) -> Optional[str]:
        """Return a data URL for the given avatar path if it exists."""
        try:
            if not path.exists():
                return None
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            data_b = path.read_bytes()
            b64 = base64.b64encode(data_b).decode("ascii")
            return f"data:{mime};base64,{b64}"
        except Exception:
            logging.warning("Failed to load avatar asset %s", path, exc_info=True)
            return None

    def _refresh_user_state_cache(self) -> None:
        """Mirror CoreState's user info onto the manager-level attributes."""
        self.user_is_online = self.state.user_is_online
        self.user_display_name = self.state.user_display_name
        self.user_current_building_id = self.state.user_current_building_id
        self.user_current_city_id = self.state.user_current_city_id

    @property
    def all_personas(self) -> Dict[str, Union[PersonaCore, RemotePersonaProxy]]:
        """Returns a combined dictionary of resident and visiting personas."""
        return {**self.personas, **self.visiting_personas}

    def _process_thinking_requests(self):
        self.runtime.process_thinking_requests()

    def _check_for_visitors(self):
        self.runtime.check_for_visitors()

    def _check_dispatch_status(self):
        self.runtime.check_dispatch_status()

    def _load_and_create_buildings_from_db(self) -> List[Building]:
        """DBからBuilding情報を読み込み、Buildingオブジェクトのリストを生成する"""
        db = self.SessionLocal()
        try:
            db_buildings = db.query(BuildingModel).filter(BuildingModel.CITYID == self.city_id).all()
            buildings = []
            for db_b in db_buildings:
                building = Building(
                    building_id=db_b.BUILDINGID,
                    name=db_b.BUILDINGNAME,
                    capacity=db_b.CAPACITY or 1,
                    system_instruction=db_b.SYSTEM_INSTRUCTION or "",
                    entry_prompt=db_b.ENTRY_PROMPT or "",
                    auto_prompt=db_b.AUTO_PROMPT or "",
                    description=db_b.DESCRIPTION or "", # 探索結果で説明を表示するために追加
                    auto_interval_sec=db_b.AUTO_INTERVAL_SEC if hasattr(db_b, 'AUTO_INTERVAL_SEC') else 10
                )
                buildings.append(building)
            logging.info(f"Loaded and created {len(buildings)} buildings from database.")
            return buildings
        except Exception as e:
            logging.error(f"Failed to load buildings from DB: {e}", exc_info=True)
            return [] # エラー時は空リストを返す
        finally:
            db.close()

    def _ensure_item_tables(self, engine) -> None:
        """Ensure newly introduced item-related tables exist."""
        try:
            ItemModel.__table__.create(bind=engine, checkfirst=True)
            ItemLocationModel.__table__.create(bind=engine, checkfirst=True)
            PersonaEventLog.__table__.create(bind=engine, checkfirst=True)
        except Exception as exc:
            logging.error("Failed to ensure item tables exist: %s", exc, exc_info=True)

    def _load_items_from_db(self) -> None:
        """Load items and their locations from the database into memory."""
        db = self.SessionLocal()
        try:
            item_rows = db.query(ItemModel).all()
            location_rows = db.query(ItemLocationModel).all()
        except Exception as exc:
            logging.error("Failed to load items from DB: %s", exc, exc_info=True)
            item_rows = []
            location_rows = []
        finally:
            db.close()

        self.items = {}
        self.item_locations = {}
        self.items_by_building = defaultdict(list)
        self.items_by_persona = defaultdict(list)
        self.world_items = []

        for row in item_rows:
            if row.STATE_JSON:
                try:
                    state_payload = json.loads(row.STATE_JSON)
                except json.JSONDecodeError:
                    logging.warning("Invalid STATE_JSON for item %s", row.ITEM_ID)
                    state_payload = {}
            else:
                state_payload = {}
            self.items[row.ITEM_ID] = {
                "item_id": row.ITEM_ID,
                "name": row.NAME,
                "type": row.TYPE,
                "description": row.DESCRIPTION or "",
                "state": state_payload,
                "created_at": row.CREATED_AT,
                "updated_at": row.UPDATED_AT,
            }

        for loc in location_rows:
            payload = {
                "owner_kind": (loc.OWNER_KIND or "").strip(),
                "owner_id": (loc.OWNER_ID or "").strip(),
                "updated_at": loc.UPDATED_AT,
                "location_id": loc.LOCATION_ID,
            }
            self.item_locations[loc.ITEM_ID] = payload
            owner_kind = payload["owner_kind"]
            owner_id = payload["owner_id"]
            if owner_kind == "building":
                self.items_by_building[owner_id].append(loc.ITEM_ID)
            elif owner_kind == "persona":
                self.items_by_persona[owner_id].append(loc.ITEM_ID)
            else:
                self.world_items.append(loc.ITEM_ID)

        for item_id in self.items.keys():
            if item_id not in self.item_locations:
                self.world_items.append(item_id)

        for building in self.buildings:
            building.item_ids = list(self.items_by_building.get(building.building_id, []))
            self._refresh_building_system_instruction(building.building_id)
        if hasattr(self, "personas") and isinstance(self.personas, dict):
            for persona_id, persona in self.personas.items():
                if hasattr(persona, "set_item_registry"):
                    try:
                        persona.set_item_registry(self.items)
                    except Exception as exc:
                        logging.debug("Failed to update item registry for %s: %s", persona_id, exc)
                inventory_ids = self.items_by_persona.get(persona_id, [])
                persona.set_inventory(list(inventory_ids))
        if hasattr(self, "state") and isinstance(self.state, CoreState):
            self.state.items = self.items
            self.state.item_locations = self.item_locations
            self.state.items_by_building = {k: list(v) for k, v in self.items_by_building.items()}
            self.state.items_by_persona = {k: list(v) for k, v in self.items_by_persona.items()}
            self.state.world_items = list(self.world_items)

    def _refresh_building_system_instruction(self, building_id: str) -> None:
        """Refresh building.system_instruction so that it includes the current item list."""
        building = self.building_map.get(building_id)
        if not building:
            return
        base_text = building.base_system_instruction or ""
        item_ids = self.items_by_building.get(building_id, [])
        if not item_ids:
            building.system_instruction = base_text
            return
        lines: List[str] = []
        for item_id in item_ids:
            data = self.items.get(item_id)
            if not data:
                continue
            description = (data.get("description") or "").strip() or "(説明なし)"
            if len(description) > 160:
                description = description[:157] + "..."
            display_name = data.get("name", item_id)
            lines.append(f"- {display_name}: {description} [アイテムID:\"{item_id}\"]")
        if not lines:
            building.system_instruction = base_text
            return
        items_block = "\n".join(lines)
        marker = "## 現在地にあるアイテム"
        if marker in base_text:
            before, after = base_text.split(marker, 1)
            after = after.lstrip("\n")
            building.system_instruction = f"{before}{marker}\n{items_block}\n{after}".rstrip()
        else:
            building.system_instruction = f"{base_text.rstrip()}\n\n{marker}\n{items_block}"

    def _load_persona_event_logs(self) -> None:
        """Load pending persona events from the database."""
        db = self.SessionLocal()
        try:
            rows = (
                db.query(PersonaEventLog)
                .join(AIModel, PersonaEventLog.PERSONA_ID == AIModel.AIID)
                .filter(
                    AIModel.HOME_CITYID == self.city_id,
                    PersonaEventLog.STATUS == "pending",
                )
                .all()
            )
        except Exception as exc:
            logging.error("Failed to load persona events: %s", exc, exc_info=True)
            rows = []
        finally:
            db.close()

        self.persona_pending_events = defaultdict(list)
        for row in rows:
            self.persona_pending_events[row.PERSONA_ID].append(
                {
                    "event_id": row.EVENT_ID,
                    "content": row.CONTENT,
                    "created_at": row.CREATED_AT,
                }
            )

    def record_persona_event(self, persona_id: str, content: str) -> None:
        """Add a new pending event for the specified persona."""
        db = self.SessionLocal()
        try:
            entry = PersonaEventLog(PERSONA_ID=persona_id, CONTENT=content, STATUS="pending")
            db.add(entry)
            db.commit()
            db.refresh(entry)
            created_at = entry.CREATED_AT
            event_id = entry.EVENT_ID
        except Exception as exc:
            logging.error("Failed to record persona event for %s: %s", persona_id, exc, exc_info=True)
            db.rollback()
            return
        finally:
            db.close()
        self.persona_pending_events[persona_id].append(
            {
                "event_id": event_id,
                "content": content,
                "created_at": created_at,
            }
        )

    def get_persona_pending_events(self, persona_id: str) -> List[Dict[str, Any]]:
        events = list(self.persona_pending_events.get(persona_id, []))
        events.sort(key=lambda e: e.get("created_at") or datetime.utcnow())
        return events

    def archive_persona_events(self, persona_id: str, event_ids: List[int]) -> None:
        if not event_ids:
            return
        db = self.SessionLocal()
        try:
            (
                db.query(PersonaEventLog)
                .filter(PersonaEventLog.EVENT_ID.in_(event_ids))
                .update({PersonaEventLog.STATUS: "archived"}, synchronize_session=False)
            )
            db.commit()
        except Exception as exc:
            logging.error("Failed to archive persona events %s: %s", event_ids, exc, exc_info=True)
            db.rollback()
            return
        finally:
            db.close()

        pending = self.persona_pending_events.get(persona_id, [])
        if pending:
            remaining = [ev for ev in pending if ev.get("event_id") not in event_ids]
            if remaining:
                self.persona_pending_events[persona_id] = remaining
            else:
                self.persona_pending_events.pop(persona_id, None)

    def _append_building_history_note(self, building_id: str, content: str) -> None:
        if not building_id:
            return
        history = self.building_histories.setdefault(building_id, [])
        history.append({"role": "host", "content": content})
        try:
            self._save_building_histories([building_id])
        except Exception:
            logging.debug("Failed to save building history for %s", building_id, exc_info=True)

    def _update_item_cache(self, item_id: str, owner_kind: str, owner_id: Optional[str], updated_at: datetime) -> None:
        prev = self.item_locations.get(item_id)
        prev_kind = prev.get("owner_kind") if prev else None
        prev_owner = prev.get("owner_id") if prev else None

        if prev_kind == "building" and prev_owner:
            listing = self.items_by_building.get(prev_owner, [])
            if listing and item_id in listing:
                listing[:] = [itm for itm in listing if itm != item_id]
            if not listing:
                self.items_by_building.pop(prev_owner, None)
            self._refresh_building_system_instruction(prev_owner)
        elif prev_kind == "persona" and prev_owner:
            inventory = self.items_by_persona.get(prev_owner, [])
            if inventory and item_id in inventory:
                inventory[:] = [itm for itm in inventory if itm != item_id]
            if not inventory:
                self.items_by_persona.pop(prev_owner, None)
            persona_obj = self.personas.get(prev_owner)
            if persona_obj:
                persona_obj.set_inventory(self.items_by_persona.get(prev_owner, []))
        else:
            if item_id in self.world_items:
                self.world_items[:] = [itm for itm in self.world_items if itm != item_id]

        if owner_kind == "building" and owner_id:
            listing = self.items_by_building[owner_id]
            if item_id not in listing:
                listing.append(item_id)
            self._refresh_building_system_instruction(owner_id)
        elif owner_kind == "persona" and owner_id:
            inventory = self.items_by_persona[owner_id]
            if item_id not in inventory:
                inventory.append(item_id)
            persona_obj = self.personas.get(owner_id)
            if persona_obj:
                persona_obj.set_inventory(list(inventory))
        else:
            if item_id not in self.world_items:
                self.world_items.append(item_id)

            self.item_locations[item_id] = {
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "updated_at": updated_at,
        }

    def _broadcast_item_event(self, persona_ids: List[str], message: str) -> None:
        deduped = {pid for pid in persona_ids if pid}
        for pid in deduped:
            self.record_persona_event(pid, message)

    # --- Item operations ---

    def pickup_item_for_persona(self, persona_id: str, item_id: str) -> str:
        persona = self.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")
        building_id = persona.current_building_id
        if not building_id:
            raise RuntimeError("現在地が不明なため、アイテムを拾えません。")
        resolved_id = self._resolve_item_identifier(item_id) or item_id
        item = self.items.get(resolved_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")
        location = self.item_locations.get(resolved_id)
        if not location or location.get("owner_kind") != "building" or location.get("owner_id") != building_id:
            raise RuntimeError("このアイテムは現在の建物にはありません。")

        timestamp = datetime.utcnow()
        db = self.SessionLocal()
        try:
            row = (
                db.query(ItemLocationModel)
                .filter(ItemLocationModel.ITEM_ID == resolved_id)
                .one_or_none()
            )
            if row is None:
                raise RuntimeError("アイテムの配置情報が見つかりませんでした。")
            row.OWNER_KIND = "persona"
            row.OWNER_ID = persona_id
            row.UPDATED_AT = timestamp
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        self._update_item_cache(resolved_id, "persona", persona_id, timestamp)
        item_name = item.get("name", resolved_id)
        actor_msg = f"「{item_name}」を拾った。"
        self.record_persona_event(persona_id, actor_msg)
        other_ids = [
            oid for oid in self.occupants.get(building_id, [])
            if oid and oid != persona_id
        ]
        if other_ids:
            notice = f"{persona.persona_name}が「{item_name}」を拾った。"
            self._broadcast_item_event(other_ids, notice)
        building_name = self.building_map.get(building_id).name if building_id in self.building_map else building_id
        note = (
            "<div class=\"note-box\">📦 Item Pickup:<br>"
            f"<b>{persona.persona_name}が「{item_name}」を拾いました（{building_name}）。</b></div>"
        )
        self._append_building_history_note(building_id, note)
        return actor_msg

    def place_item_from_persona(self, persona_id: str, item_id: str, building_id: Optional[str] = None) -> str:
        persona = self.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")
        building_id = building_id or persona.current_building_id
        if not building_id:
            raise RuntimeError("現在地が不明なため、アイテムを置けません。")
        resolved_id = self._resolve_item_identifier(item_id) or item_id
        item = self.items.get(resolved_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")
        location = self.item_locations.get(resolved_id)
        if not location or location.get("owner_kind") != "persona" or location.get("owner_id") != persona_id:
            raise RuntimeError("このアイテムを所持していないため、置けません。")

        timestamp = datetime.utcnow()
        db = self.SessionLocal()
        try:
            row = (
                db.query(ItemLocationModel)
                .filter(ItemLocationModel.ITEM_ID == resolved_id)
                .one_or_none()
            )
            if row is None:
                row = ItemLocationModel(
                    ITEM_ID=resolved_id,
                    OWNER_KIND="building",
                    OWNER_ID=building_id,
                    UPDATED_AT=timestamp,
                )
                db.add(row)
            else:
                row.OWNER_KIND = "building"
                row.OWNER_ID = building_id
                row.UPDATED_AT = timestamp
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        self._update_item_cache(resolved_id, "building", building_id, timestamp)
        building_name = self.building_map.get(building_id).name if building_id in self.building_map else building_id
        item_name = item.get("name", resolved_id)
        actor_msg = f"「{item_name}」を{building_name}に置いた。"
        self.record_persona_event(persona_id, actor_msg)
        other_ids = [
            oid for oid in self.occupants.get(building_id, [])
            if oid and oid != persona_id
        ]
        if other_ids:
            notice = f"{persona.persona_name}が{building_name}に「{item_name}」を置いた。"
            self._broadcast_item_event(other_ids, notice)
        note = (
            "<div class=\"note-box\">📦 Item Placement:<br>"
            f"<b>{persona.persona_name}が「{item_name}」を{building_name}に置きました。</b></div>"
        )
        self._append_building_history_note(building_id, note)
        return actor_msg

    def use_item_for_persona(self, persona_id: str, item_id: str, new_description: str) -> str:
        persona = self.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")
        resolved_id = self._resolve_item_identifier(item_id) or item_id
        item = self.items.get(resolved_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")
        location = self.item_locations.get(resolved_id)
        if not location or location.get("owner_kind") != "persona" or location.get("owner_id") != persona_id:
            raise RuntimeError("このアイテムは現在あなたのインベントリにありません。")
        if (item.get("type") or "").lower() != "object":
            raise RuntimeError("このアイテムは use 操作に対応していません。")
        cleaned = (new_description or "").strip()
        timestamp = datetime.utcnow()

        db = self.SessionLocal()
        try:
            row = (
                db.query(ItemModel)
                .filter(ItemModel.ITEM_ID == resolved_id)
                .one_or_none()
            )
            if row is None:
                raise RuntimeError("アイテム本体が見つかりません。")
            row.DESCRIPTION = cleaned
            row.UPDATED_AT = timestamp
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        item["description"] = cleaned
        item["updated_at"] = timestamp
        location_owner_kind = self.item_locations.get(resolved_id, {}).get("owner_kind")
        location_owner_id = self.item_locations.get(resolved_id, {}).get("owner_id")
        if location_owner_kind == "building" and location_owner_id:
            self._refresh_building_system_instruction(location_owner_id)
        inventory = self.items_by_persona.get(persona_id, [])
        persona.set_inventory(list(inventory))

        preview = cleaned if cleaned else "(内容未設定)"
        if len(preview) > 80:
            preview = preview[:77] + "..."
        item_name = item.get("name", resolved_id)
        actor_msg = f"「{item_name}」を使った。内容: {preview}"
        self.record_persona_event(persona_id, actor_msg)
        building_id = persona.current_building_id
        other_ids = [
            oid for oid in self.occupants.get(building_id or "", [])
            if oid and oid != persona_id
        ]
        if other_ids:
            notice = f"{persona.persona_name}が「{item_name}」を使った。"
            self._broadcast_item_event(other_ids, notice)
        if building_id:
            building_name = self.building_map.get(building_id).name if building_id in self.building_map else building_id
            note = (
                "<div class=\"note-box\">🛠 Item Use:<br>"
                f"<b>{persona.persona_name}が「{item_name}」を使いました（{building_name}）。</b></div>"
            )
            self._append_building_history_note(building_id, note)
        return actor_msg


    def _explore_city(self, persona_id: str, target_city_id: str):
        self.runtime.explore_city(persona_id, target_city_id)

    def _load_user_state_from_db(self):
        if getattr(self, "runtime", None) is not None:
            self.runtime.load_user_state_from_db()
        else:
            db = self.SessionLocal()
            try:
                user = (
                    db.query(UserModel)
                    .filter(UserModel.USERID == self.state.user_id)
                    .first()
                )
                if user:
                    self.state.user_is_online = user.LOGGED_IN
                    self.state.user_current_city_id = user.CURRENT_CITYID
                    self.state.user_current_building_id = user.CURRENT_BUILDINGID
                    self.state.user_display_name = (
                        (user.USERNAME or "ユーザー").strip() or "ユーザー"
                    )
                    self.id_to_name_map[str(self.state.user_id)] = (
                        self.state.user_display_name
                    )
                else:
                    self.state.user_is_online = False
                    self.state.user_current_building_id = None
                    self.state.user_current_city_id = None
                    self.state.user_display_name = "ユーザー"
                    self.id_to_name_map[str(self.state.user_id)] = (
                        self.state.user_display_name
                    )
            except Exception as exc:
                logging.error(
                    "Failed to load user status from DB: %s", exc, exc_info=True
                )
                self.state.user_is_online = False
                self.state.user_current_building_id = None
                self.state.user_current_city_id = None
                self.state.user_display_name = "ユーザー"
                self.id_to_name_map[str(self.state.user_id)] = (
                    self.state.user_display_name
                )
            finally:
                db.close()
        self._refresh_user_state_cache()

    def set_user_login_status(self, user_id: int, status: bool) -> str:
        """ユーザーのログイン状態を更新し、ログアウト時にメッセージを記録する"""
        # ログアウト処理の場合、先に現在地を記録しておく
        last_building_id = self.state.user_current_building_id if not status else None

        db = self.SessionLocal()
        try:
            user = db.query(UserModel).filter(UserModel.USERID == user_id).first()
            if user:
                user.LOGGED_IN = status
                db.commit()
                self.state.user_is_online = status
                self.state.user_display_name = (user.USERNAME or "ユーザー").strip() or "ユーザー"
                self.user_is_online = self.state.user_is_online
                self.user_display_name = self.state.user_display_name
                self.id_to_name_map[str(self.user_id)] = self.user_display_name
                status_text = "オンライン" if status else "オフライン"
                logging.info(f"User {user_id} login status set to: {status_text}")

                # ログアウト時にメッセージを記録
                if last_building_id and last_building_id in self.building_map:
                    username = user.USERNAME or "ユーザー"
                    logout_message = f'<div class="note-box">🚶 User Action:<br><b>{username}がオフラインになりました</b></div>'
                    self.building_histories.setdefault(last_building_id, []).append({"role": "host", "content": logout_message})
                    self._save_building_histories()
                    logging.info(f"Logged user logout in building {last_building_id}")

                self._refresh_user_state_cache()
                return status_text
            else:
                logging.error(f"User with USERID={user_id} not found.")
                return "エラー: ユーザーが見つかりません"
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update user login status for USERID={user_id}: {e}", exc_info=True)
            return "エラー: DB更新に失敗"
        finally:
            db.close()

    def move_user(self, target_building_id: str) -> Tuple[bool, str]:
        """Moves the user to a new building and logs the movement."""
        result = self.runtime.move_user(target_building_id)
        self._refresh_user_state_cache()
        return result


    def _move_persona(self, persona_id: str, from_id: str, to_id: str, db_session=None) -> Tuple[bool, Optional[str]]:
        """Moves a persona between buildings, utilizing OccupancyManager."""
        return self.runtime._move_persona(persona_id, from_id, to_id, db_session=db_session)


    def shutdown(self):
        """Safely shutdown all managers and save data."""
        logging.info("Shutting down SAIVerseManager...")

        if getattr(self, "gateway_runtime", None):
            try:
                self.gateway_runtime.stop()
            except Exception:
                logging.debug("Failed to stop gateway runtime cleanly.", exc_info=True)
            self.gateway_runtime = None

        # --- ★ アプリケーション終了時にユーザーをログアウトさせる ---
        if self.state.user_is_online:
            logging.info("Setting user to offline as part of shutdown.")
            self.set_user_login_status(self.user_id, False)

        # Stop the SDS background thread
        if self.sds_thread and self.sds_thread.is_alive():
            self.sds_stop_event.set()
            self.sds_thread.join(timeout=5)
        logging.info("SDS communication thread stopped.")

        # Stop the DB polling thread
        self.db_polling_stop_event.set()
        if hasattr(self, 'db_polling_thread') and self.db_polling_thread.is_alive():
            self.db_polling_thread.join(timeout=5)
        logging.info("DB polling thread stopped.")

        # Stop all conversation managers
        for manager in self.conversation_managers.values():
            manager.stop()
        
        # Save all persona and building states
        for persona in self.personas.values():
            persona._save_session_metadata()
        self._save_building_histories()
        logging.info("SAIVerseManager shutdown complete.")

    def handle_user_input(self, message: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        return self.runtime.handle_user_input(message, metadata=metadata)


    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Iterator[str]:
        yield from self.runtime.handle_user_input_stream(message, metadata=metadata)

    def get_summonable_personas(self) -> List[str]:
        """Returns a list of persona names that can be summoned to the user's current location."""
        return self.runtime.get_summonable_personas()

    def get_conversing_personas(self) -> List[Tuple[str, str]]:
        return self.runtime.get_conversing_personas()

    def summon_persona(self, persona_id: str) -> Tuple[bool, Optional[str]]:
        return self.runtime.summon_persona(persona_id)

    def end_conversation(self, persona_id: str) -> str:
        return self.runtime.end_conversation(persona_id)

    def set_model(self, model: str) -> None:
        """
        Update LLM model override for all active personas in memory.
        - If model == "None": clear the override and reset each persona to its DB-defined default model.
        - Otherwise: set the given model for all personas (temporary, not persisted).
        """
        if model == "None":
            logging.info("Clearing global model override; restoring each persona's DB default model.")
            db = self.SessionLocal()
            try:
                for pid, persona in self.personas.items():
                    ai = db.query(AIModel).filter_by(AIID=pid).first()
                    if not ai:
                        continue
                    m = ai.DEFAULT_MODEL or DEFAULT_MODEL
                    persona.set_model(m, get_context_length(m), get_model_provider(m))
                # Reflect no-override state in manager
                self.model = "None"
                self.state.model = self.model
                if hasattr(self.runtime, "model"):
                    self.runtime.model = self.model
            except Exception as e:
                logging.error(f"Failed to restore DB default models: {e}", exc_info=True)
            finally:
                db.close()
            return

        logging.info(f"Temporarily setting model to '{model}' for all active personas.")
        self.model = model
        self.context_length = get_context_length(model)
        self.provider = get_model_provider(model)
        self.state.model = self.model
        self.state.context_length = self.context_length
        self.state.provider = self.provider
        if hasattr(self.runtime, "model"):
            self.runtime.model = self.model
            self.runtime.context_length = self.context_length
            self.runtime.provider = self.provider
        for persona in self.personas.values():
            persona.set_model(model, self.context_length, self.provider)

    def start_autonomous_conversations(self):
        """Start all autonomous conversation managers."""
        if getattr(self, "runtime", None):
            self.runtime.start_autonomous_conversations()
            return

        if self.state.autonomous_conversation_running:
            logging.warning("Autonomous conversations are already running.")
            return

        logging.info("Starting all autonomous conversation managers...")
        for manager in self.conversation_managers.values():
            manager.start()
        self.state.autonomous_conversation_running = True
        logging.info("All autonomous conversation managers have been started.")

    def stop_autonomous_conversations(self):
        """Stop all autonomous conversation managers."""
        if getattr(self, "runtime", None):
            self.runtime.stop_autonomous_conversations()
            return

        if not self.state.autonomous_conversation_running:
            logging.warning("Autonomous conversations are not running.")
            return

        logging.info("Stopping all autonomous conversation managers...")
        for manager in self.conversation_managers.values():
            manager.stop()
        self.state.autonomous_conversation_running = False
        logging.info("All autonomous conversation managers have been stopped.")

    def get_building_history(self, building_id: str) -> List[Dict[str, str]]:
        """指定されたBuildingの生の会話ログを取得する"""
        return self.building_histories.get(building_id, [])

    def get_building_id(self, building_name: str, city_name: str) -> str:
        """指定されたCityとBuilding名からBuildingIDを生成する"""
        return f"{building_name}_{city_name}"

    def run_scheduled_prompts(self) -> List[str]:
        """Run scheduled prompts via runtime service (fallback to local logic if needed)."""
        if getattr(self, "runtime", None):
            return self.runtime.run_scheduled_prompts()

        replies: List[str] = []
        for persona in self.personas.values():
            if getattr(persona, "interaction_mode", "auto") == "auto":
                replies.extend(persona.run_scheduled_prompt())
        if replies:
            self._save_building_histories()
            for persona in self.personas.values():
                persona._save_session_metadata()
        return replies

    def execute_tool(self, tool_id: int, persona_id: str, arguments: Dict[str, Any]) -> str:
        if getattr(self, "runtime", None):
            return self.runtime.execute_tool(tool_id, persona_id, arguments)

        db = self.SessionLocal()
        try:
            persona = self.personas.get(persona_id)
            if not persona:
                return f"Error: ペルソナ '{persona_id}' が見つかりません。"

            current_building_id = persona.current_building_id
            building = self.building_map.get(current_building_id)
            if not building:
                return f"Error: ペルソナ '{persona_id}' は有効な建物にいません。"

            link = (
                db.query(BuildingToolLink)
                .filter_by(BUILDINGID=current_building_id, TOOLID=tool_id)
                .first()
            )
            if not link:
                return f"Error: ツールID {tool_id} は '{building.name}' で利用できません。"

            tool_record = db.query(ToolModel).filter_by(TOOLID=tool_id).first()
            if not tool_record:
                return f"Error: ツールID {tool_id} がデータベースに見つかりません。"

            module_path = tool_record.MODULE_PATH
            function_name = tool_record.FUNCTION_NAME

            try:
                tool_module = importlib.import_module(module_path)
                tool_function = getattr(tool_module, function_name)
                logging.info(
                    "Executing tool '%s' for persona '%s' with args %s.",
                    tool_record.TOOLNAME,
                    persona.persona_name,
                    arguments,
                )
                result = tool_function(**arguments)
                content, _, _, _ = tools.defs.parse_tool_result(result)
                return str(content)
            except ImportError:
                logging.error(
                    "Failed to import tool module: %s", module_path, exc_info=True
                )
                return (
                    f"Error: ツールファイル '{module_path}' が見つかりませんでした。"
                    "パスを確認してください。"
                )
            except AttributeError:
                logging.error(
                    "Function '%s' not found in module '%s'.",
                    function_name,
                    module_path,
                    exc_info=True,
                )
                return (
                    f"Error: ツール関数 '{function_name}' が '{module_path}' に"
                    "見つかりませんでした。"
                )
            except TypeError as exc:
                logging.error(
                    "Argument mismatch for tool '%s': %s",
                    function_name,
                    exc,
                    exc_info=True,
                )
                return (
                    f"Error: ツール '{tool_record.TOOLNAME}' に不正な引数が渡されました。"
                    f"詳細: {exc}"
                )
            except Exception as exc:
                logging.error(
                    "An error occurred while executing tool '%s': %s",
                    module_path,
                    exc,
                    exc_info=True,
                )
                return (
                    "Error: ツールの実行中に予期せぬエラーが発生しました: "
                    f"{exc}"
                )
        finally:
            db.close()

    def trigger_world_event(self, event_message: str) -> str:
        """
        Broadcasts a world event message to all buildings in the current city.
        """
        return self.admin.trigger_world_event(event_message)

    # --- World Editor Backend Methods ---

    def get_cities_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのCity一覧をDataFrameとして取得する"""
        return self.admin.get_cities_df()

    def update_city(
        self,
        city_id: int,
        name: str,
        description: str,
        online_mode: bool,
        ui_port: int,
        api_port: int,
        timezone_name: str,
    ) -> str:
        """ワールドエディタからCityの設定を更新する"""
        return self.admin.update_city(
            city_id,
            name,
            description,
            online_mode,
            ui_port,
            api_port,
            timezone_name,
        )



    # --- World Editor: Create/Delete Methods ---

    def create_city(self, name: str, description: str, ui_port: int, api_port: int, timezone_name: str) -> str:
        """Creates a new city."""
        return self.admin.create_city(name, description, ui_port, api_port, timezone_name)

    def delete_city(self, city_id: int) -> str:
        """Deletes a city after checking dependencies."""
        return self.admin.delete_city(city_id)

    def create_building(
        self, name: str, description: str, capacity: int, system_instruction: str, city_id: int
    ) -> str:
        """Creates a new building in a specified city."""
        return self.admin.create_building(name, description, capacity, system_instruction, city_id)

    def delete_building(self, building_id: str) -> str:
        """Deletes a building after checking for occupants."""
        return self.admin.delete_building(building_id)

    def move_ai_from_editor(self, ai_id: str, target_building_id: str) -> str:
        """
        Moves an AI to a specified building, triggered from the World Editor.
        """
        return self.admin.move_ai_from_editor(ai_id, target_building_id)

    def get_ais_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのAI一覧をDataFrameとして取得する"""
        return self.admin.get_ais_df()

    def get_ai_details(self, ai_id: str) -> Optional[Dict]:
        """Get full details for a single AI for the edit form."""
        return self.admin.get_ai_details(ai_id)

    def create_ai(self, name: str, system_prompt: str, home_city_id: int) -> str:
        """Creates a new AI and their private room."""
        return self.admin.create_ai(name, system_prompt, home_city_id)

    def update_ai(
        self,
        ai_id: str,
        name: str,
        description: str,
        system_prompt: str,
        home_city_id: int,
        default_model: Optional[str],
        interaction_mode: str,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
    ) -> str:
        """ワールドエディタからAIの設定を更新する"""
        return self.admin.update_ai(
            ai_id,
            name,
            description,
            system_prompt,
            home_city_id,
            default_model,
            interaction_mode,
            avatar_path,
            avatar_upload,
        )

    def delete_ai(self, ai_id: str) -> str:
        """Deletes an AI after checking its state."""
        return self.admin.delete_ai(ai_id)

    def get_linked_tool_ids(self, building_id: str) -> List[int]:
        """Gets a list of tool IDs linked to a specific building."""
        return self.admin.get_linked_tool_ids(building_id)

    def get_buildings_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのBuilding一覧をDataFrameとして取得する"""
        return self.admin.get_buildings_df()

    def update_building(
        self,
        building_id: str,
        name: str,
        capacity: int,
        description: str,
        system_instruction: str,
        city_id: int,
        tool_ids: List[int],
        interval: int,
    ) -> str:
        """ワールドエディタからBuildingの設定を更新する"""
        return self.admin.update_building(
            building_id,
            name,
            capacity,
            description,
            system_instruction,
            city_id,
            tool_ids,
            interval,
        )

    def get_items_df(self) -> pd.DataFrame:
        return self.admin.get_items_df()

    def get_item_details(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self.admin.get_item_details(item_id)

    def create_item(
        self,
        name: str,
        item_type: str,
        description: str,
        owner_kind: str,
        owner_id: Optional[str],
        state_json: Optional[str],
    ) -> str:
        return self.admin.create_item(name, item_type, description, owner_kind, owner_id, state_json)

    def update_item(
        self,
        item_id: str,
        name: str,
        item_type: str,
        description: str,
        owner_kind: str,
        owner_id: Optional[str],
        state_json: Optional[str],
    ) -> str:
        return self.admin.update_item(item_id, name, item_type, description, owner_kind, owner_id, state_json)

    def delete_item(self, item_id: str) -> str:
        return self.admin.delete_item(item_id)
