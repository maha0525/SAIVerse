import base64
import json
from collections import defaultdict
from sqlalchemy import create_engine
import threading
import requests
import logging
from pathlib import Path
import mimetypes
from typing import Dict, List, Optional, Set, Tuple, Iterator, Union, Any, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import importlib
import tools.core
from discord_gateway.mapping import ChannelMapping
import os

from google.genai import errors
from llm_clients.exceptions import LLMError
from .buildings import Building
from sea import SEARuntime
from sea.pulse_controller import PulseController
from persona.core import PersonaCore
from .model_configs import get_model_provider, get_context_length
from .occupancy_manager import OccupancyManager
from .conversation_manager import ConversationManager
from .schedule_manager import ScheduleManager
from .integration_manager import IntegrationManager
from .track_manager import TrackManager
from .note_manager import NoteManager
from .meta_layer import MetaLayer
from .pulse_scheduler import SubLineScheduler, is_subline_scheduler_enabled
from .track_handlers import (
    AutonomousTrackHandler,
    SocialTrackHandler,
    UserConversationTrackHandler,
)
from phenomena.manager import PhenomenonManager
from phenomena.triggers import TriggerEvent, TriggerType
from sqlalchemy.orm import sessionmaker
from .remote_persona_proxy import RemotePersonaProxy
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


from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL

DEFAULT_MODEL = BUILTIN_DEFAULT_LITE_MODEL


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
        # --- Critical: startup_alerts and quarantine state must exist before
        # _init_building_histories so corruption events can be recorded.
        self.startup_alerts: List[Dict[str, Any]] = []
        # Buildings whose log.json is corrupted/zero-byte. While quarantined:
        #   - building_histories does NOT contain the key (treated as "no truth")
        #   - save_building_histories refuses to write
        #   - move_entity refuses entry
        # Quarantine info: {building_id: {"reason", "corrupted_path", "available_backups"}}
        self.quarantined_buildings: Dict[str, Dict[str, Any]] = {}
        # Buildings whose in-memory history was modified since last save.
        # Used to scope explicit save calls so we never iterate the full path map.
        self.modified_buildings: Set[str] = set()

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
            timezone_name=self.timezone_name,
            timezone_info=self.timezone_info,
        )
        self.state.items = self.items
        self.state.item_locations = self.item_locations
        self.state.items_by_building = self.items_by_building
        self.state.items_by_persona = self.items_by_persona
        self.state.world_items = self.world_items
        self.state.persona_pending_events = self.persona_pending_events

        # --- Playbook permission request synchronisation (transient, in-memory) ---
        self._pending_permission_requests: dict[str, threading.Event] = {}
        self._permission_responses: dict[str, str] = {}

        # --- Tweet confirmation synchronisation (transient, in-memory) ---
        self._pending_tweet_confirmations: dict[str, threading.Event] = {}
        self._tweet_confirmation_responses: dict[str, str] = {}

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
            manager_ref=self,
        )
        logging.info("Initialized OccupancyManager.")

        # --- Initialize cognitive-model managers (Phase B-5) ---
        # Track / Note の永続化を扱う純粋ロジックレイヤー。
        # Intent A v0.9 / Intent B v0.6 参照。
        self.track_manager = TrackManager(session_factory=self.SessionLocal)
        self.note_manager = NoteManager(session_factory=self.SessionLocal)
        logging.info("Initialized cognitive-model managers (TrackManager, NoteManager).")

        # --- Initialize cognitive-model runtime layers (Phase C-1) ---
        # MetaLayer は alert observer として TrackManager に登録される。
        # UserConversationTrackHandler はユーザー発話イベントの受け口として
        # handle_user_input から呼ばれる (Track 状態判定 → 必要なら alert 遷移)。
        self.meta_layer = MetaLayer(self)
        self.track_manager.add_alert_observer(self.meta_layer.on_track_alert)
        # Intent A v0.14 [B] 移動: Track 状態遷移の起点でメタ判断ターン
        # (line_role='meta_judgment', scope='discardable') を 'committed' に昇格する。
        # メタ判断 Playbook が独白 + /spell 方式で /spell track_activate 等を発動 →
        # Pulse 完了時に TrackManager.activate(...) が呼ばれる → このルートで pulse_id
        # ベースに当該 pulse 内のメタ判断ターンを committed 化する。
        self.track_manager.add_status_change_observer(self._promote_meta_judgment_in_pulse)
        self.user_conversation_handler = UserConversationTrackHandler(
            track_manager=self.track_manager,
            manager=self,
        )
        self.social_track_handler = SocialTrackHandler(
            track_manager=self.track_manager,
        )
        self.autonomous_track_handler = AutonomousTrackHandler(
            track_manager=self.track_manager,
        )
        # Phase C-3b: SubLineScheduler を生成 (start は startup 内で行う)。
        # これにより running な連続実行型 Track のサブライン Pulse が定期的に
        # 起動される (Intent A v0.13 / Intent B v0.10)。
        self.subline_scheduler = SubLineScheduler(self)
        # Phase C-2: 内部 alert ポーラ (intent B v0.7 §"内部 alert ポーラ機構")
        # Track パラメータの閾値超過 + Handler.tick() を定期駆動する。
        from saiverse.internal_alert_poller import InternalAlertPoller
        self.internal_alert_poller = InternalAlertPoller(self)
        logging.info(
            "Initialized cognitive-model runtime layers "
            "(MetaLayer registered as alert observer, "
            "UserConversationTrackHandler / SocialTrackHandler / AutonomousTrackHandler ready, "
            "SubLineScheduler + InternalAlertPoller instantiated [will start at startup])."
        )

        # --- Step 5: Load Dynamic States from DB ---
        # データベースから動的な状態（ペルソナ、ユーザー状態、入室状況）を読み込み、
        # メモリ上のオブジェクトに反映させます。
        self._load_personas_from_db()
        self._load_user_state_from_db()

        # --- Phase B-X: 既存ペルソナへの social Track migration sweep ---
        # 起動時、ロード済みペルソナ全員に交流 Track が存在することを保証する。
        # 既存 Track がある場合は何もしない (冪等性は SocialTrackHandler 側で担保)。
        self._ensure_social_tracks_for_all_personas()

        # Load saved meta playbook preference from DB
        try:
            db = self.SessionLocal()
            try:
                from database.models import UserSettings
                settings = db.query(UserSettings).filter(
                    UserSettings.USERID == self.state.user_id
                ).first()
                if settings and settings.SELECTED_META_PLAYBOOK:
                    self.state.current_playbook = settings.SELECTED_META_PLAYBOOK
                    logging.info("Loaded saved meta playbook: %s", settings.SELECTED_META_PLAYBOOK)
            finally:
                db.close()
        except Exception:
            logging.warning("Failed to load playbook preference from DB", exc_info=True)

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
            saiverse_manager=self,
        )
        self.phenomenon_manager.start()
        logging.info("Initialized and started PhenomenonManager.")

        # --- Initialize IntegrationManager ---
        self.integration_manager = IntegrationManager(self, tick_interval=30)
        self._register_integrations()
        self.integration_manager.start()
        logging.info("Initialized and started IntegrationManager.")

        # --- Phase C-3b: Start SubLineScheduler ---
        # 全ペルソナのインメモリ状態が揃った後に起動する (personas dict が利用可能)。
        # SAIVERSE_SUBLINE_SCHEDULER_ENABLED=false で起動を抑止できる
        # (動き出した自律行動を一時的に止めたい時の安全弁)。
        if is_subline_scheduler_enabled():
            self.subline_scheduler.start()
            logging.info("Started SubLineScheduler.")
        else:
            logging.warning(
                "SubLineScheduler is disabled by SAIVERSE_SUBLINE_SCHEDULER_ENABLED=false. "
                "Autonomous tracks will not run pulses until re-enabled and restarted."
            )

        # Phase C-2: Internal alert poller (intent B v0.7 §"内部 alert ポーラ機構")
        self.internal_alert_poller.start()

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

        # Stop event registry for user-initiated generation cancellation
        self._active_stop_events: Dict[str, threading.Event] = {}

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

        # Phase C-2 (intent A v0.9 §"ペルソナのアクティビティ状態"):
        # ACTIVITY_STATE='Active' のペルソナで AutonomyManager を自動起動する。
        # Active 以外 (Stop/Sleep/Idle) は起動しない (定期 tick OFF と整合)。
        logging.info("Auto-starting autonomy managers for Active personas...")
        self._ensure_autonomy_for_active_personas()

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
    def _register_integrations(self) -> None:
        """Register external integrations with IntegrationManager.

        Loads addon-provided integrations from
        ``expansion_data/<addon>/integrations/*.py`` for any enabled addon.
        """
        try:
            from saiverse.addon_loader import load_addon_integrations
            load_addon_integrations(self.integration_manager)
        except Exception:
            logging.exception("Failed to load addon integrations")

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
    def run_sea_auto(
        self,
        persona,
        building_id: str,
        occupants: List[str],
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Run autonomous pulse via PulseController.

        Args:
            meta_playbook: auto pulse として起動する Playbook 名 (例:
                SubLineScheduler から track_autonomous を回す用途)。
                2026-05-01 の認知モデル移行以降は **必須**。None で呼ぶと
                PulseController が ERROR ログを出して何もしない。
            args: Playbook 起動時に渡す引数。

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
                meta_playbook=meta_playbook,
                args=args,
            )
        except Exception as exc:
            logging.exception("SEA auto run failed: %s", exc)

    def run_sea_user(self, persona, building_id: str, user_input: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None, args: Optional[Dict[str, Any]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[str]:
        """Run user input via PulseController."""
        try:
            result = self.pulse_controller.submit_user(
                persona_id=persona.persona_id,
                building_id=building_id,
                user_input=user_input,
                metadata=metadata,
                meta_playbook=meta_playbook,
                args=args,
                event_callback=event_callback,
            )
            return result if result else []
        except LLMError:
            # Propagate LLM errors to the caller for frontend display
            raise
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

    def resolve_item_ref_for_persona(self, persona_id: str, ref: str) -> str:
        """スロット参照（b:3, i:2, b:5>1 等）またはUUIDをアイテムUUIDに解決する。"""
        persona = self.personas.get(persona_id)
        building_id = getattr(persona, "current_building_id", None) if persona else None
        return self.item_service.resolve_slot_ref(ref, persona_id, building_id)

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

    def get_open_items_for_persona(self, persona_id: str) -> list:
        """Get all items in a persona's inventory that have is_open = True."""
        return self.item_service.get_open_items_for_persona(persona_id)

    def get_all_items_in_building(self, building_id: str) -> list:
        """Get all items in a building (regardless of open state)."""
        return self.item_service.get_all_items_in_building(building_id)

    def get_all_items_for_persona(self, persona_id: str) -> list:
        """Get all items in a persona's inventory (regardless of open state)."""
        return self.item_service.get_all_items_for_persona(persona_id)

    def create_document_item(self, persona_id: str, name: str, description: str, content: str, source_context: Optional[str] = None) -> str:
        """Create a new document item and place it in the current building."""
        return self.item_service.create_document_item(persona_id, name, description, content, source_context=source_context)

    def create_picture_item(self, persona_id: str, name: str, description: str, file_path: str, building_id: Optional[str] = None, source_context: Optional[str] = None) -> tuple:
        """Create a new picture item and place it in the specified building. Returns (item_id, slot_num)."""
        return self.item_service.create_picture_item(persona_id, name, description, file_path, building_id, source_context=source_context)

    def create_picture_item_for_user(self, name: str, description: str, file_path: str, building_id: str, creator_id: Optional[str] = None, source_context: Optional[str] = None) -> str:
        """Create a picture item from user upload and place it in the specified building."""
        return self.item_service.create_picture_item_for_user(name, description, file_path, building_id, creator_id=creator_id, source_context=source_context)

    def create_document_item_for_user(self, name: str, description: str, file_path: str, building_id: str, is_open: bool = True, creator_id: Optional[str] = None, source_context: Optional[str] = None) -> str:
        """Create a document item from user upload and place it in the specified building."""
        return self.item_service.create_document_item_for_user(name, description, file_path, building_id, is_open, creator_id=creator_id, source_context=source_context)

    def move_item_for_persona(self, persona_id: str, item_ids: list, destination_kind: str, destination_id: str) -> str:
        """Move items to a destination (building, persona, or bag)."""
        return self.item_service.move_item(persona_id, item_ids, destination_kind, destination_id)

    def view_items_for_persona(self, persona_id: str, item_ids: list) -> str:
        """View multiple items (up to 5). For bags, shows contents list."""
        return self.item_service.view_items(persona_id, item_ids)

    def get_bag_items_in_building(self, building_id: str) -> list:
        """Get all bag-type items in a building."""
        return self.item_service.get_bag_items_in_building(building_id)

    def get_items_in_bag(self, bag_item_id: str) -> list:
        """Get all items directly contained in a bag."""
        return self.item_service.get_items_in_bag(bag_item_id)

    def get_bag_contents_recursive(self, bag_item_id: str) -> list:
        """Get bag contents recursively, including nested bags."""
        return self.item_service.get_bag_contents_recursive(bag_item_id)

    def update_item_description(self, item_id: str, description: str) -> None:
        """Update an item's description in DB and cache."""
        self.item_service.update_item_description(item_id, description)

    def update_item_name(self, item_id: str, name: str) -> None:
        """Update an item's name in DB and cache."""
        self.item_service.update_item_name(item_id, name)

    def backfill_item_descriptions(
        self,
        building_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Batch-generate descriptions for picture items with placeholder text."""
        return self.item_service.backfill_item_descriptions(
            building_id=building_id, persona_id=persona_id, dry_run=dry_run
        )

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
        """ユーザーのログイン状態を更新する。

        occupants 連動: logout 時に user_id を現在の建物の occupants から外す。
        login 時に DB の CURRENT_BUILDINGID へ戻す。これにより dynamic_state の
        occupant_entered / occupant_left 検出が自動的に状態変化を各ペルソナへ
        通知する（オフラインメッセージを建物ログに直接書く必要がなくなる）。
        """
        last_building_id = self.state.user_current_building_id if not status else None
        user_id_str = str(user_id)

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
                self.id_to_name_map[user_id_str] = self.user_display_name
                status_text = "オンライン" if status else "オフライン"
                logging.info(f"User {user_id} login status set to: {status_text}")

                if status:
                    # Login: ユーザーを CURRENT_BUILDINGID の occupants に追加
                    target_bid = user.CURRENT_BUILDINGID
                    if target_bid and target_bid in self.building_map:
                        occ = self.occupants.setdefault(target_bid, [])
                        if user_id_str not in occ:
                            occ.append(user_id_str)
                            logging.info(
                                "Added user %s to occupants of %s on login",
                                user_id_str, target_bid,
                            )
                else:
                    # Logout: ユーザーを現在の建物の occupants から外す
                    if last_building_id:
                        occ = self.occupants.get(last_building_id, [])
                        if user_id_str in occ:
                            occ.remove(user_id_str)
                            logging.info(
                                "Removed user %s from occupants of %s on logout",
                                user_id_str, last_building_id,
                            )

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

        # Stop integration manager
        if hasattr(self, "integration_manager"):
            self.integration_manager.stop()
            logging.info("IntegrationManager stopped.")

        # Stop schedule manager
        if hasattr(self, "schedule_manager"):
            self.schedule_manager.stop()
            logging.info("ScheduleManager stopped.")

        # Emit server_stop trigger before stopping phenomenon manager
        self._emit_trigger(
            TriggerType.SERVER_STOP,
            {"city_id": self.city_id, "city_name": self.city_name},
        )

        # Phase C-3b: Stop SubLineScheduler before saving persona state
        if hasattr(self, "subline_scheduler") and self.subline_scheduler:
            try:
                self.subline_scheduler.stop()
            except Exception:
                logging.exception("Failed to stop SubLineScheduler")

        # Stop phenomenon manager
        if hasattr(self, "phenomenon_manager") and self.phenomenon_manager:
            self.phenomenon_manager.stop()
            logging.info("PhenomenonManager stopped.")

        # Save all persona and building states
        for persona in self.personas.values():
            persona._save_session_metadata()
        self._save_modified_buildings()
        logging.info("SAIVerseManager shutdown complete.")

    def _ensure_social_tracks_for_all_personas(self) -> None:
        """起動時 migration: ロード済み全ペルソナに交流 Track を確保する。

        Phase B-X 導入以前に作られたペルソナは交流 Track を持たないため、
        起動時に一度なめて未作成のものを作る。冪等なので何度走っても安全。

        個別ペルソナの hook 失敗で他ペルソナの初期化を巻き込まないよう
        try/except で囲む。
        """
        if not self.personas:
            return
        created_count = 0
        existed_count = 0
        for persona_id in list(self.personas.keys()):
            try:
                # ensure_track は既存があれば無作成で返すため、ログだけ追跡する
                # 簡便のため戻り値の status で「今作ったか / もとからあったか」を判別。
                # 厳密な区別が必要なら handler に作成フックを追加することも可能だが、
                # migration 用途では集計ログで十分。
                track_before = self.social_track_handler._find_existing(persona_id)
                self.social_track_handler.ensure_track(persona_id)
                if track_before is None:
                    created_count += 1
                else:
                    existed_count += 1
            except Exception:
                logging.exception(
                    "[social-handler] migration sweep failed for persona=%s",
                    persona_id,
                )
        logging.info(
            "[social-handler] migration sweep done: created=%d existed=%d total=%d",
            created_count, existed_count, len(self.personas),
        )

    def handle_user_input(self, message: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        return self.runtime.handle_user_input(message, metadata=metadata)


    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None, building_id: Optional[str] = None,
    ) -> Iterator[str]:
        yield from self.runtime.handle_user_input_stream(
            message, metadata=metadata, meta_playbook=meta_playbook,
            args=args, building_id=building_id,
        )

    def cancel_active_generation(self) -> bool:
        """Cancel the active LLM generation for personas in the user's current building.

        Sends cancellation signal via CancellationToken (stops SEA playbook execution
        and closes LLM streaming connections) and sets the stop_event (breaks the
        per-persona loop in backend_worker).
        """
        building_id = self.state.user_current_building_id
        if not building_id:
            logging.warning("[cancel] No user_current_building_id; cannot cancel.")
            return False

        persona_ids = self.occupants.get(building_id, [])
        cancelled = False

        for pid in persona_ids:
            req = self.pulse_controller._current.get(pid)
            if req:
                logging.info("[cancel] Cancelling active request for persona %s (pulse_id=%s)", pid, req.pulse_id)
                req.cancellation_token.cancel(interrupted_by="user_stop")
                cancelled = True

        # Also set the stop_event so backend_worker breaks its persona loop
        stop_event = self._active_stop_events.get(building_id)
        if stop_event:
            logging.info("[cancel] Setting stop_event for building %s", building_id)
            stop_event.set()
            cancelled = True

        if not cancelled:
            logging.info("[cancel] No active generation found for building %s", building_id)

        return cancelled

    def preview_context(
        self, message: str, building_id: Optional[str] = None,
        meta_playbook: Optional[str] = None,
        image_count: int = 0, document_count: int = 0,
    ) -> List[Dict[str, Any]]:
        """Preview context for responding personas without executing LLM calls."""
        return self.runtime.preview_context(
            message, building_id=building_id, meta_playbook=meta_playbook,
            image_count=image_count, document_count=document_count,
        )

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
        if not model or not model.strip():
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
                self.model = None
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

    def update_default_model(self, model: str) -> None:
        """Update the base default model without setting a global override.

        Unlike ``set_model()``, this does NOT create a session-level global
        override.  It updates ``_base_model`` and refreshes each persona that
        has no explicit ``DEFAULT_MODEL`` in the database.
        """
        from saiverse.model_configs import get_context_length, get_model_provider

        logging.info(
            "Updating base default model from '%s' to '%s' (no global override).",
            getattr(self, "_base_model", None),
            model,
        )
        self._base_model = model

        db = self.SessionLocal()
        try:
            for pid, persona in self.personas.items():
                ai = db.query(AIModel).filter_by(AIID=pid).first()
                if not ai:
                    continue
                if ai.DEFAULT_MODEL:
                    # Persona has an explicit model in DB; leave it alone
                    logging.debug(
                        "Persona '%s' has explicit DEFAULT_MODEL='%s'; skipping.",
                        pid,
                        ai.DEFAULT_MODEL,
                    )
                    continue
                new_ctx = get_context_length(model)
                new_provider = get_model_provider(model)
                persona.set_model(model, new_ctx, new_provider)
                logging.info(
                    "Updated persona '%s' to base default model '%s'.",
                    pid,
                    model,
                )
        except Exception as e:
            logging.error(
                "Failed to update personas to new default model '%s': %s",
                model,
                e,
                exc_info=True,
            )
        finally:
            db.close()

    def set_model_parameters(self, parameters: Optional[Dict[str, Any]] = None) -> None:
        """Update model parameters for the current override model."""
        self.model_parameter_overrides = dict(parameters or {})
        if not self.model:
            logging.info("Parameter overrides ignored because no global model override is active.")
            return
        for persona in self.personas.values():
            persona.apply_parameter_overrides(self.model_parameter_overrides)

    # ------------------------------------------------------------------
    # AutonomyManager <-> ACTIVITY_STATE 同期 (Phase C-2)
    # ------------------------------------------------------------------

    def ensure_autonomy_for(self, persona_id: str) -> None:
        """指定ペルソナの AutonomyManager を ACTIVITY_STATE に同期する。

        intent A v0.9: Active のみ定期発火 ON。それ以外 (Stop/Sleep/Idle) は
        起動しない。既に起動中で Active 以外になった場合は停止する。
        """
        from saiverse.autonomy_manager import AutonomyManager

        if not hasattr(self, "_autonomy_managers"):
            self._autonomy_managers = {}

        persona = self.personas.get(persona_id)
        if persona is None:
            return

        state = getattr(persona, "activity_state", "Idle")
        am = self._autonomy_managers.get(persona_id)

        if state == "Active":
            if am is None:
                am = AutonomyManager(persona_id=persona_id, manager=self)
                self._autonomy_managers[persona_id] = am
            if not am.is_running:
                am.start()
                logging.info(
                    "[autonomy-sync] Started AutonomyManager for active persona '%s'",
                    persona_id,
                )
        else:
            if am is not None and am.is_running:
                am.stop()
                logging.info(
                    "[autonomy-sync] Stopped AutonomyManager for non-active persona '%s' (state=%s)",
                    persona_id, state,
                )

    def _ensure_autonomy_for_active_personas(self) -> None:
        """全ペルソナの AutonomyManager 状態を ACTIVITY_STATE に同期する (起動時用)。"""
        for persona_id in list(self.personas.keys()):
            try:
                self.ensure_autonomy_for(persona_id)
            except Exception:
                logging.exception(
                    "Failed to ensure autonomy for persona '%s'", persona_id,
                )

    # ------------------------------------------------------------------
    # メタ判断ターン scope 昇格 hook (Intent A v0.14 [B] 移動)
    # ------------------------------------------------------------------

    def _promote_meta_judgment_in_pulse(
        self, persona_id: str, pulse_id: Optional[str]
    ) -> None:
        """TrackManager の状態遷移 hook で呼ばれる。

        当該 pulse_id 内の ``line_role='meta_judgment' AND scope='discardable'``
        なメッセージを ``scope='committed'`` に昇格する。これにより独白 + /spell
        方式のメタ判断でも Intent A v0.14 [B] 移動の「分岐ターンをそのまま残す」
        を実現する (Track 切替 = メタ判断の確定 → 移動先 Track の冒頭来歴として
        メインキャッシュに残るべき)。

        - ``pulse_id`` が None (CLI / テスト) の場合は何もしない (該当 Pulse 不在)。
        - ペルソナがメモリにロードされていない場合も skip。
        - メッセージが見つからなくても (= 通常会話の中で /spell track_pause を
          発動した等、メタ判断 Playbook を経由していない場合) 静かに 0 件 UPDATE
          で終わる。これは正しい挙動 (continue 相当のため昇格不要)。
        """
        if not pulse_id:
            return
        persona = self.personas.get(persona_id)
        if persona is None:
            return
        persona_log_path = getattr(persona, "persona_log_path", None)
        if persona_log_path is None:
            return
        db_path = persona_log_path.parent / "memory.db"
        if not db_path.exists():
            logging.warning(
                "[meta-judgment-promote] memory.db not found at %s for persona=%s",
                db_path, persona_id,
            )
            return

        # Phase 2.5 (2026-05-01): messages.pulse_id 専用カラムに対する INDEX 付き
        # 直接 WHERE で昇格を行う。旧実装は metadata.tags の "pulse:{uuid}" を
        # json_each で参照していたが INDEX が効かず線形スキャンになっていた。
        import sqlite3
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.execute(
                    """
                    UPDATE messages SET scope = 'committed'
                    WHERE pulse_id = ?
                      AND line_role = 'meta_judgment'
                      AND scope = 'discardable'
                    """,
                    (pulse_id,),
                )
                if cur.rowcount > 0:
                    logging.info(
                        "[meta-judgment-promote] Promoted %d meta_judgment row(s) "
                        "to 'committed' (pulse_id=%s persona=%s)",
                        cur.rowcount, pulse_id, persona_id,
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logging.exception(
                "[meta-judgment-promote] Failed to promote (pulse_id=%s persona=%s)",
                pulse_id, persona_id,
            )

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
            if getattr(persona, "activity_state", "Idle") == "Active":
                replies.extend(persona.run_scheduled_prompt())
        if replies:
            self._save_modified_buildings()
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
        result = self.admin.create_building(name, description, capacity, system_instruction, city_id, building_id)
        # If creation succeeded and it's in our city, reload buildings list
        if not result.startswith("Error") and city_id == self.city_id:
            self._reload_buildings()
        return result

    def _reload_buildings(self) -> None:
        """Reload buildings list from database to reflect recent changes."""
        new_buildings = self._load_and_create_buildings_from_db()
        if not new_buildings and self.buildings:
            # DB load failed — keep existing state to avoid wiping all buildings
            logging.warning(
                "_reload_buildings: DB returned empty list but %d buildings "
                "exist in memory; keeping current state.",
                len(self.buildings),
            )
            return

        self.buildings = new_buildings
        new_building_map = {b.building_id: b for b in self.buildings}

        # Diff-based update: remove deleted, add/update existing — avoids
        # the race condition where clear()+update() leaves an empty map
        # visible to concurrent request threads.
        removed_ids = set(self.building_map) - set(new_building_map)
        for bid in removed_ids:
            del self.building_map[bid]
            self.capacities.pop(bid, None)
            # Clean up in-memory occupants and histories for deleted buildings
            self.occupants.pop(bid, None)
            self.building_histories.pop(bid, None)

        self.building_map.update(new_building_map)

        new_capacities = {b.building_id: b.capacity for b in self.buildings}
        self.capacities.update(new_capacities)

        # Update building memory paths
        self.building_memory_paths = {
            b.building_id: self.saiverse_home / "cities" / self.city_name / "buildings" / b.building_id / "log.json"
            for b in self.buildings
        }

        # Initialize occupants and building_histories for new buildings
        for building_id in self.building_map:
            if building_id not in self.occupants:
                self.occupants[building_id] = []
            if building_id not in self.building_histories:
                self.building_histories[building_id] = []

    def delete_building(self, building_id: str) -> str:
        """Deletes a building after checking for occupants."""
        # Check if building is in our city before deletion
        was_in_city = building_id in self.building_map
        result = self.admin.delete_building(building_id)
        # If deletion succeeded and it was in our city, reload buildings list
        if not result.startswith("Error") and was_in_city:
            self._reload_buildings()
        return result

    def move_ai_from_editor(self, ai_id: str, target_building_id: str) -> str:
        """
        Moves an AI to a specified building, triggered from the World Editor.
        """
        return self.admin.move_ai_from_editor(ai_id, target_building_id)

    def get_ai_details(self, ai_id: str) -> Optional[Dict]:
        """Get full details for a single AI for the edit form."""
        return self.admin.get_ai_details(ai_id)

    def create_ai(
        self, name: str, system_prompt: str, home_city_id: int, custom_ai_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[str], Optional[str]]:
        """Creates a new AI and their private room."""
        result = self.admin.create_ai(name, system_prompt, home_city_id, custom_ai_id)
        success = result[0]
        if success:
            # Reload buildings from DB to ensure in-memory list is consistent.
            # _create_persona() appends to self.buildings manually, but this
            # defensive reload guarantees the list matches the DB state.
            self._reload_buildings()
        return result

    def update_ai(
        self,
        ai_id: str,
        name: str,
        description: str,
        system_prompt: str,
        home_city_id: int,
        default_model: Optional[str],
        lightweight_model: Optional[str],
        activity_state: str,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
        appearance_image_path: Optional[str] = None,
        chronicle_enabled: Optional[bool] = None,
        memory_weave_context: Optional[bool] = None,
        spell_enabled: Optional[bool] = None,
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
            activity_state,
            avatar_path,
            avatar_upload,
            appearance_image_path,
            chronicle_enabled=chronicle_enabled,
            memory_weave_context=memory_weave_context,
            spell_enabled=spell_enabled,
        )

    def delete_ai(self, ai_id: str) -> str:
        """Deletes an AI after checking its state."""
        return self.admin.delete_ai(ai_id)

    def get_linked_tool_ids(self, building_id: str) -> List[int]:
        """Gets a list of tool IDs linked to a specific building."""
        return self.admin.get_linked_tool_ids(building_id)

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
        extra_prompt_files: Optional[List[str]] = None,
    ) -> str:
        """ワールドエディタからBuildingの設定を更新する"""
        result = self.admin.update_building(
            building_id,
            name,
            capacity,
            description,
            system_instruction,
            city_id,
            tool_ids,
            interval,
            image_path,
            extra_prompt_files,
        )

        # Update in-memory Building object if DB update succeeded
        if not result.startswith("Error") and building_id in self.building_map:
            building = self.building_map[building_id]
            building.name = name
            building.capacity = capacity
            building.description = description
            building.base_system_instruction = system_instruction
            building.system_instruction = system_instruction
            building.auto_interval_sec = interval
            building.extra_prompt_files = extra_prompt_files or []
            # Update capacities dict used by OccupancyManager
            if hasattr(self, 'capacities') and building_id in self.capacities:
                self.capacities[building_id] = capacity
            logging.info(f"Updated in-memory Building object: {building_id}")

        return result

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
        creator_id: Optional[str] = None,
        source_context: Optional[str] = None,
    ) -> str:
        return self.admin.create_item(name, item_type, description, owner_kind, owner_id, state_json, file_path, creator_id=creator_id, source_context=source_context)

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
