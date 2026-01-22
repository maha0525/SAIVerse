import base64
import json
from collections import defaultdict
from sqlalchemy import create_engine
import threading
import requests
import logging
from pathlib import Path
import mimetypes
from typing import Dict, List, Optional, Tuple, Iterator, Union, Any, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import importlib
import tools.core
from discord_gateway.mapping import ChannelMapping
import os

from google.genai import errors
from buildings import Building
from sea import SEARuntime
from sea.pulse_controller import PulseController
from persona_core import PersonaCore
from model_configs import get_model_provider, get_context_length
from occupancy_manager import OccupancyManager
from conversation_manager import ConversationManager
from schedule_manager import ScheduleManager
from phenomena.manager import PhenomenonManager
from phenomena.triggers import TriggerEvent, TriggerType
from sqlalchemy.orm import sessionmaker
from remote_persona_proxy import RemotePersonaProxy
from manager.sds import SDSMixin
from manager.background import DatabasePollingMixin
from manager.history import HistoryMixin
from manager.blueprints import BlueprintMixin
from manager.persona import PersonaMixin
from manager.visitors import VisitorMixin
from manager.gateway import GatewayMixin
from manager.user_state import UserStateMixin
from manager.initialization import InitializationMixin
from manager.persona_events import PersonaEventMixin
from manager.state import CoreState
from manager.runtime import RuntimeService
from manager.admin import AdminService
from manager.items import ItemService
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
    Playbook,
    PhenomenonRule,
)


#DEFAULT_MODEL = "gpt-4o"
DEFAULT_MODEL = "gemini-2.0-flash"


def _get_default_model() -> str:
    """Resolve the base default model with optional environment override."""
    return os.getenv("SAIVERSE_DEFAULT_MODEL", DEFAULT_MODEL)


class SAIVerseManager(
    InitializationMixin,
    UserStateMixin,
    PersonaEventMixin,
    VisitorMixin,
    PersonaMixin,
    HistoryMixin,
    BlueprintMixin,
    SDSMixin,
    DatabasePollingMixin,
    GatewayMixin,
):
    """Manage multiple personas and building occupancy."""

    def __init__(
        self,
        city_name: str,
        db_path: str,
        model: Optional[str] = None,
        sds_url: str = os.getenv("SDS_URL", "http://127.0.0.1:8080"),
    ):
        # --- Phase 1: Data Loading ---
        self._init_database(db_path)
        self._init_city_config(city_name)
        self._init_buildings()
        self._init_file_paths()
        self._init_avatars()
        self._init_building_histories()
        self._init_model_config(model)

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
            user_avatar_data=self.user_avatar_data,
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

        # スケジュールマネージャーを初期化して起動
        self.schedule_manager = ScheduleManager(saiverse_manager=self, check_interval=60)
        self.schedule_manager.start()
        logging.info("Initialized and started ScheduleManager with 60 second check interval.")

        # --- Initialize PhenomenonManager ---
        self.phenomenon_manager = PhenomenonManager(
            session_factory=self.SessionLocal,
            async_execution=True,
        )
        self.phenomenon_manager.start()
        logging.info("Initialized and started PhenomenonManager.")

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

        # SEA runtime (always enabled)
        self.sea_runtime: SEARuntime = SEARuntime(self)
        
        # Pulse controller for managing concurrent playbook executions
        self.pulse_controller: PulseController = PulseController(self.sea_runtime)

        self.runtime = RuntimeService(self, self.state)
        self.admin = AdminService(self, self.runtime, self.state)
        self.item_service = ItemService(self, self.state)
        
        # Load items through ItemService and sync data structures
        self.item_service.load_items_from_db()
        self.items = self.item_service.items
        self.item_locations = self.item_service.item_locations
        self.items_by_building = self.item_service.items_by_building
        self.items_by_persona = self.item_service.items_by_persona
        self.world_items = self.item_service.world_items
        self.item_registry = self.items  # Alias for UI compatibility

        # Start background thread for DB polling (after runtime is ready)
        self.db_polling_stop_event = threading.Event()
        self.db_polling_thread = threading.Thread(
            target=self._db_polling_loop, daemon=True
        )
        self.db_polling_thread.start()

        # Auto-start autonomous conversation managers for personas with mode=auto
        logging.info("Auto-starting autonomous conversation managers...")
        self.start_autonomous_conversations()

        # Emit server_start trigger
        self._emit_trigger(
            TriggerType.SERVER_START,
            {"city_id": self.city_id, "city_name": self.city_name},
        )

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

    # Phenomenon trigger helpers -----------------------------------------------
    def _emit_trigger(self, trigger_type: TriggerType, data: Dict[str, Any]) -> None:
        """Emit a trigger event to the PhenomenonManager."""
        if not hasattr(self, "phenomenon_manager") or not self.phenomenon_manager:
            return
        try:
            event = TriggerEvent(type=trigger_type, data=data)
            self.phenomenon_manager.emit(event)
        except Exception as exc:
            logging.error("Failed to emit trigger %s: %s", trigger_type, exc, exc_info=True)

    # SEA integration helpers -------------------------------------------------
    def run_sea_auto(self, persona, building_id: str, occupants: List[str]) -> None:
        """Run autonomous pulse via PulseController.

        Discord visitors (DiscordVisitorStub) are handled by DiscordConnector,
        not by the local PulseController.
        """
        # Discord visitor guard: skip local processing
        # DiscordConnector will handle turn requests via Turn Request/Response flow
        if getattr(persona, "is_discord_visitor", False):
            logging.debug(
                "Skipping local run_sea_auto for Discord visitor: %s",
                getattr(persona, "persona_id", "unknown"),
            )
            return

        try:
            self.pulse_controller.submit_auto(
                persona_id=persona.persona_id,
                building_id=building_id,
            )
        except Exception as exc:
            logging.exception("SEA auto run failed: %s", exc)

    def run_sea_user(self, persona, building_id: str, user_input: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[str]:
        """Run user input via PulseController."""
        try:
            result = self.pulse_controller.submit_user(
                persona_id=persona.persona_id,
                building_id=building_id,
                user_input=user_input,
                metadata=metadata,
                meta_playbook=meta_playbook,
                event_callback=event_callback,
            )
            return result if result else []
        except Exception as exc:
            logging.exception("SEA user run failed: %s", exc)
            return []

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
                # Parse extra prompt files from JSON
                extra_prompts: List[str] = []
                raw_extra = getattr(db_b, 'EXTRA_PROMPT_FILES', None)
                if raw_extra:
                    try:
                        extra_prompts = json.loads(raw_extra)
                        if not isinstance(extra_prompts, list):
                            extra_prompts = []
                    except json.JSONDecodeError:
                        extra_prompts = []

                building = Building(
                    building_id=db_b.BUILDINGID,
                    name=db_b.BUILDINGNAME,
                    capacity=db_b.CAPACITY or 1,
                    system_instruction=db_b.SYSTEM_INSTRUCTION or "",
                    entry_prompt=db_b.ENTRY_PROMPT or "",
                    auto_prompt=db_b.AUTO_PROMPT or "",
                    description=db_b.DESCRIPTION or "", # 探索結果で説明を表示するために追加
                    auto_interval_sec=db_b.AUTO_INTERVAL_SEC if hasattr(db_b, 'AUTO_INTERVAL_SEC') else 10,
                    extra_prompt_files=extra_prompts,
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

    def _ensure_phenomenon_tables(self, engine) -> None:
        """Ensure phenomenon-related tables exist."""
        try:
            PhenomenonRule.__table__.create(bind=engine, checkfirst=True)
        except Exception as exc:
            logging.error("Failed to ensure phenomenon tables exist: %s", exc, exc_info=True)

    # --- Item operations (delegated to ItemService) ---

    def _load_items_from_db(self) -> None:
        """Load items and their locations from the database into memory."""
        self.item_service.load_items_from_db()
        # Sync references after loading
        self.items = self.item_service.items
        self.item_locations = self.item_service.item_locations
        self.items_by_building = self.item_service.items_by_building
        self.items_by_persona = self.item_service.items_by_persona
        self.world_items = self.item_service.world_items
        self.item_registry = self.items

    def _refresh_building_system_instruction(self, building_id: str) -> None:
        """Refresh building.system_instruction so that it includes the current item list."""
        self.item_service.refresh_building_system_instruction(building_id)

    def _update_item_cache(self, item_id: str, owner_kind: str, owner_id: Optional[str], updated_at: datetime) -> None:
        self.item_service.update_item_cache(item_id, owner_kind, owner_id, updated_at)

    def _broadcast_item_event(self, persona_ids: List[str], message: str) -> None:
        self.item_service.broadcast_item_event(persona_ids, message)

    def pickup_item_for_persona(self, persona_id: str, item_id: str) -> str:
        return self.item_service.pickup_item(persona_id, item_id)

    def place_item_from_persona(self, persona_id: str, item_id: str, building_id: Optional[str] = None) -> str:
        return self.item_service.place_item(persona_id, item_id, building_id)

    def use_item_for_persona(self, persona_id: str, item_id: str, action_json: str) -> str:
        """Use an item to apply effects."""
        return self.item_service.use_item(persona_id, item_id, action_json)

    def view_item_for_persona(self, persona_id: str, item_id: str) -> str:
        """View the full content of a picture or document item."""
        return self.item_service.view_item(persona_id, item_id)

    def toggle_item_open_state(self, item_id: str) -> bool:
        """Toggle the open/close state of an item."""
        return self.item_service.toggle_item_open_state(item_id)

    def get_open_items_in_building(self, building_id: str) -> list:
        """Get all items in a building that have is_open = True."""
        return self.item_service.get_open_items_in_building(building_id)

    def create_document_item(self, persona_id: str, name: str, description: str, content: str) -> str:
        """Create a new document item and place it in the current building."""
        return self.item_service.create_document_item(persona_id, name, description, content)

    def create_picture_item(self, persona_id: str, name: str, description: str, file_path: str, building_id: Optional[str] = None) -> str:
        """Create a new picture item and place it in the specified building."""
        return self.item_service.create_picture_item(persona_id, name, description, file_path, building_id)

    # Note: Persona event methods (_load_persona_event_logs, record_persona_event,
    # get_persona_pending_events, archive_persona_events) are in PersonaEventMixin

    def _append_building_history_note(self, building_id: str, content: str) -> None:
        if not building_id:
            return
        history = self.building_histories.setdefault(building_id, [])
        history.append({
            "role": "host", 
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        try:
            self._save_building_histories([building_id])
        except Exception:
            logging.debug("Failed to save building history for %s", building_id, exc_info=True)

    def _explore_city(self, persona_id: str, target_city_id: str):
        self.runtime.explore_city(persona_id, target_city_id)

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
                self.state.user_presence_status = "online" if status else "offline"
                self.state.user_display_name = (user.USERNAME or "ユーザー").strip() or "ユーザー"
                self.user_is_online = status  # Backward compat
                self.user_presence_status = self.state.user_presence_status
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
        if self.state.user_presence_status != "offline":
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

        # Stop schedule manager
        if hasattr(self, "schedule_manager"):
            self.schedule_manager.stop()
            logging.info("ScheduleManager stopped.")

        # Emit server_stop trigger before stopping phenomenon manager
        self._emit_trigger(
            TriggerType.SERVER_STOP,
            {"city_id": self.city_id, "city_name": self.city_name},
        )

        # Stop phenomenon manager
        if hasattr(self, "phenomenon_manager") and self.phenomenon_manager:
            self.phenomenon_manager.stop()
            logging.info("PhenomenonManager stopped.")

        # Save all persona and building states
        for persona in self.personas.values():
            persona._save_session_metadata()
        self._save_building_histories()
        logging.info("SAIVerseManager shutdown complete.")

    def handle_user_input(self, message: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        return self.runtime.handle_user_input(message, metadata=metadata)


    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None
    ) -> Iterator[str]:
        yield from self.runtime.handle_user_input_stream(message, metadata=metadata, meta_playbook=meta_playbook)

    def get_summonable_personas(self) -> List[str]:
        """Returns a list of persona names that can be summoned to the user's current location."""
        return self.runtime.get_summonable_personas()

    def get_conversing_personas(self) -> List[Tuple[str, str]]:
        return self.runtime.get_conversing_personas()

    def get_selectable_meta_playbooks(self) -> List[Tuple[str, str]]:
        """Returns a list of (name, description) for user-selectable meta playbooks."""
        db = self.SessionLocal()
        try:
            playbooks = (
                db.query(Playbook)
                .filter(Playbook.user_selectable == True)
                .order_by(Playbook.name)
                .all()
            )
            return [(pb.name, pb.description) for pb in playbooks]
        finally:
            db.close()

    def summon_persona(self, persona_id: str) -> Tuple[bool, Optional[str]]:
        return self.runtime.summon_persona(persona_id)

    def end_conversation(self, persona_id: str) -> str:
        return self.runtime.end_conversation(persona_id)

    def set_model(self, model: str, parameters: Optional[Dict[str, Any]] = None) -> None:
        """
        Update LLM model override for all active personas in memory.
        - If model is "None" or empty: clear the override and reset each persona to its DB-defined default model.
        - Otherwise: set the given model for all personas (temporary, not persisted).
        """
        if model == "None" or not model or not model.strip():
            logging.info("Clearing global model override; restoring each persona's DB default model.")
            self.model_parameter_overrides = {}
            db = self.SessionLocal()
            try:
                for pid, persona in self.personas.items():
                    ai = db.query(AIModel).filter_by(AIID=pid).first()
                    if not ai:
                        continue
                    m = ai.DEFAULT_MODEL or getattr(self, '_base_model', None) or _get_default_model()
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
        self.model_parameter_overrides = dict(parameters or {})
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
            persona.set_model(model, self.context_length, self.provider, self.model_parameter_overrides)

    def set_model_parameters(self, parameters: Optional[Dict[str, Any]] = None) -> None:
        """Update model parameters for the current override model."""
        self.model_parameter_overrides = dict(parameters or {})
        if self.model == "None":
            logging.info("Parameter overrides ignored because no global model override is active.")
            return
        for persona in self.personas.values():
            persona.apply_parameter_overrides(self.model_parameter_overrides)

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
                content, _, _, _ = tools.core.parse_tool_result(result)
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
        host_avatar_path: Optional[str] = None,
        host_avatar_upload: Optional[str] = None,
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
            host_avatar_path,
            host_avatar_upload,
        )

    def get_user_profile(self) -> Tuple[str, str]:
        return self.admin.get_user_profile()

    def update_user_profile(
        self,
        name: str,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
    ) -> str:
        return self.admin.update_user_profile(name, avatar_path, avatar_upload)



    # --- World Editor: Create/Delete Methods ---

    def create_city(self, name: str, description: str, ui_port: int, api_port: int, timezone_name: str) -> str:
        """Creates a new city."""
        return self.admin.create_city(name, description, ui_port, api_port, timezone_name)

    def delete_city(self, city_id: int) -> str:
        """Deletes a city after checking dependencies."""
        return self.admin.delete_city(city_id)

    def create_building(
        self, name: str, description: str, capacity: int, system_instruction: str, city_id: int, building_id: str = None
    ) -> str:
        """Creates a new building in a specified city."""
        return self.admin.create_building(name, description, capacity, system_instruction, city_id, building_id)

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
        lightweight_model: Optional[str],
        interaction_mode: str,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
        appearance_image_path: Optional[str] = None,
    ) -> str:
        """ワールドエディタからAIの設定を更新する"""
        return self.admin.update_ai(
            ai_id,
            name,
            description,
            system_prompt,
            home_city_id,
            default_model,
            lightweight_model,
            interaction_mode,
            avatar_path,
            avatar_upload,
            appearance_image_path,
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
        image_path: Optional[str] = None,
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
            image_path,
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
        file_path: Optional[str] = None,
    ) -> str:
        return self.admin.create_item(name, item_type, description, owner_kind, owner_id, state_json, file_path)

    def update_item(
        self,
        item_id: str,
        name: str,
        item_type: str,
        description: str,
        owner_kind: str,
        owner_id: Optional[str],
        state_json: Optional[str],
        file_path: Optional[str] = None,
    ) -> str:
        return self.admin.update_item(item_id, name, item_type, description, owner_kind, owner_id, state_json, file_path)

    def delete_item(self, item_id: str) -> str:
        return self.admin.delete_item(item_id)

    # --- Playbook Management ---

    def get_playbooks_df(self) -> pd.DataFrame:
        """Get all playbooks as a DataFrame for the world editor."""
        return self.admin.get_playbooks_df()

    def get_playbook_details(self, playbook_id: int) -> Optional[Dict[str, Any]]:
        """Get detailed information for a specific playbook."""
        return self.admin.get_playbook_details(playbook_id)

    def update_playbook(
        self,
        playbook_id: int,
        name: str,
        description: str,
        scope: str,
        created_by_persona_id: Optional[str],
        building_id: Optional[str],
        schema_json: str,
        nodes_json: str,
        router_callable: bool,
    ) -> str:
        """Update a playbook from the world editor."""
        return self.admin.update_playbook(
            playbook_id, name, description, scope,
            created_by_persona_id, building_id,
            schema_json, nodes_json, router_callable
        )

    def delete_playbook(self, playbook_id: int) -> str:
        """Delete a playbook from the world editor."""
        return self.admin.delete_playbook(playbook_id)

    def import_playbook_from_file(self, file_path: str) -> str:
        """Import a playbook JSON file from the world editor."""
        return self.admin.import_playbook_from_file(file_path)

    def reimport_all_playbooks(self, base_dir: Optional[str] = None) -> str:
        """Re-import all playbooks under sea/playbooks or a custom directory."""
        return self.admin.reimport_all_playbooks(base_dir)
