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
from .meta_layer import MetaLayer
from .pulse_dispatcher import PulseDispatcher
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
    Region as RegionModel,
)
from saiverse.regions import Region


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
        # SSE event_callback registry keyed by building_id. Populated by
        # handle_user_input_stream while a user SSE is open; consumed by
        # on_track_activated hook so main_line pulses triggered via deferred
        # track activation can route events back to the user's SSE.
        # See: docs/intent/pulse_dispatch.md §9.3 (on_track_activated 経由起動)
        self._active_sse_callbacks: Dict[str, Callable[[Dict[str, Any]], None]] = {}

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

        # --- Generic spell confirmation synchronisation (transient, in-memory) ---
        # Used by tools/confirmation.py:request_spell_confirmation for any
        # side-effecting native tool (X, SwitchBot, future addons).
        self._pending_spell_confirmations: dict[str, threading.Event] = {}
        self._spell_confirmation_responses: dict[str, str] = {}

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

        # game Region のライフサイクル (phase 遷移 / 自動ポーズ / アーカイブ)。
        # OccupancyManager の移動フックから on_entity_moved が呼ばれる。
        from saiverse.game_lifecycle import GameLifecycleService
        self.game_lifecycle = GameLifecycleService(self)

        # Phase 4-e: EventScheduler を TrackManager より先に作成する。
        # TrackManager の wait_response_timeout 予約 push 等に使うため。
        from saiverse.event_scheduler import EventScheduler
        self.event_scheduler = EventScheduler()

        # --- Initialize cognitive-model managers (Phase B-5) ---
        # Track / Note の永続化を扱う純粋ロジックレイヤー。
        # Intent A v0.9 / Intent B v0.6 参照。
        self.track_manager = TrackManager(
            session_factory=self.SessionLocal,
            event_scheduler=self.event_scheduler,
            wait_response_timeout_provider=self._wait_response_timeout_provider,
            wait_response_timeout_callback=self._wait_response_timeout_callback,
        )
        logging.info("Initialized cognitive-model managers (TrackManager).")

        # --- Initialize Cached Head Architecture (Phase 2-h) ---
        # Section registry + pipeline + store の wiring。LLM context の head 部分を
        # Section snapshot 経由で構築するための基盤。詳細:
        # docs/intent/cached_head_architecture.md
        from sea.head_pipeline import (
            LineHeadSnapshotStore,
            get_default_pipeline,
            get_default_registry,
        )
        from sea.head_pipeline.sections import register_default_sections
        _head_registry = get_default_registry()
        if not _head_registry.all_sections():  # 再初期化時の重複登録を回避
            register_default_sections(_head_registry)
        get_default_pipeline().attach_store(
            LineHeadSnapshotStore(
                session_factory=self.SessionLocal, registry=_head_registry,
            )
        )
        logging.info("Initialized Cached Head Architecture (registry + pipeline + store).")

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
            manager=self,
        )
        # pulse_dispatch.md §5: Track activate (= running 遷移) 時に各 Handler の
        # on_track_activated hook を発火する。Handler 側で track_type をフィルタ
        # して自分の責務範囲を判定する (TrackManager は種別判定の責務を持たない)。
        # ケース1 (ユーザー発話 → alert → metalayer → activate) と ケース2
        # (自律 tick → metalayer → activate) の両方で同じ経路で Track 切替通知が
        # 出るようになる。
        self.track_manager.add_track_activated_observer(
            self.user_conversation_handler.on_track_activated
        )
        self.track_manager.add_track_activated_observer(
            self.social_track_handler.on_track_activated
        )
        self.track_manager.add_track_activated_observer(
            self.autonomous_track_handler.on_track_activated
        )
        # pulse_dispatch.md §7: ペルソナを動かす全イベントの一元的なディスパッチャ。
        # 各起点コード (manager/runtime, ScheduleManager, AutonomyManager,
        # phenomena 系) は self.pulse_dispatcher 経由でイベントを発火させる。
        # 経路選択 (直接 / 熟慮) と実行先の振り分けはここで担う。
        # NOTE: 旧 SubLineScheduler (autonomous Track への 30 秒連続 Pulse) は
        # 自律行動 v2 で廃止 (intent §9.3)。駆動は時間割のコマ発火
        # (saiverse/day_plan.py) + 判断点 (saiverse/autonomy_wiring.py) が担う。
        self.pulse_dispatcher = PulseDispatcher(self)
        # デバッグコントローラー (debug_controller.md): 完全手動モード対象ペルソナ。
        # このセットに入った persona は wait_response timeout を予約しない
        # (_wait_response_timeout_provider が None を返す)。
        self._debug_manual_mode_personas: set = set()
        # Phase C-2: 内部 alert ポーラ (intent B v0.7 §"内部 alert ポーラ機構")
        # Track パラメータの閾値超過 + Handler.tick() を定期駆動する。
        from saiverse.internal_alert_poller import InternalAlertPoller
        self.internal_alert_poller = InternalAlertPoller(self)
        logging.info(
            "Initialized cognitive-model runtime layers "
            "(MetaLayer registered as alert observer, "
            "UserConversationTrackHandler / SocialTrackHandler / AutonomousTrackHandler ready, "
            "InternalAlertPoller + EventScheduler instantiated [will start at startup])."
        )

        # SEA runtime + Pulse controller (always enabled).
        # ⚠️ 起動直後レース対策: 自律 tick スレッド (AutonomyManager) は直後の
        # _run_persona_post_registration() で起動する。そのスレッドが最初の tick を
        # 発火した時点で pulse_controller がまだ None だと、メタ判断が正規の
        # playbook 経路 (submit_meta_judgment → finalize) に行けず、ロスのある
        # レガシー _run_judgment にフォールバックする。レガシー経路は
        # meta_judgment_log は書くが line_role='meta_judgment' の SAIMemory
        # メッセージを保存しないため、ペルソナの記憶に「なぜその Track を始めたか」
        # が残らない (実害観測: 2026-06-29 14:34 の共創小説 Track)。
        # → tick スレッド起動より前に、ここで確実に初期化しておく。
        self.sea_runtime: SEARuntime = SEARuntime(self)
        self.pulse_controller: PulseController = PulseController(self.sea_runtime)
        # pulse_dispatch.md §6.2: Track 状態変化で current pulse を cancel する経路。
        # メタ判断結果として Track が pending に押し出された場合、その Track 起点の
        # 進行中 Pulse は意味を失うので cancellation_token.cancel() で止める。
        self.track_manager.add_status_change_observer(
            self.pulse_controller.on_track_status_change
        )

        # ライフサイクル状態: __init__ 完了時点ではまだ False。start() が全背景ループを
        # 起動する瞬間に True になる。ensure_autonomy_for はこのフラグで「起動前は
        # AutonomyManager スレッドを立てない」を保証する (起動前 pulse レース防止)。
        self._started = False

        # --- Step 5: Load Dynamic States from DB ---
        # データベースから動的な状態（ペルソナ、ユーザー状態、入室状況）を読み込み、
        # メモリ上のオブジェクトに反映させます。
        self._load_personas_from_db()
        self._load_user_state_from_db()

        # --- ペルソナ登録後の共通初期化 ---
        # 全ペルソナに対して交流 Track 確保 + AutonomyManager 同期を実行する。
        # _on_persona_registered は動的作成/Blueprint からも呼ばれる統一フック。
        self._run_persona_post_registration()

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

        # スケジュールマネージャーを初期化 (Phase 4-e: push 駆動、ポーリング廃止)。
        # ⚠️ 構築のみ。起動 (.start()) は start() に集約する (下記 NOTE 参照)。
        self.schedule_manager = ScheduleManager(saiverse_manager=self)
        logging.info("Initialized ScheduleManager (push-driven, no polling).")

        # --- Initialize PhenomenonManager ---
        self.phenomenon_manager = PhenomenonManager(
            session_factory=self.SessionLocal,
            async_execution=True,
            saiverse_manager=self,
        )
        logging.info("Initialized PhenomenonManager.")

        # --- Initialize IntegrationManager ---
        self.integration_manager = IntegrationManager(self, tick_interval=30)
        self._register_integrations()
        logging.info("Initialized IntegrationManager.")

        # Observer: Fixture/Observer の定期実行・push 受信・通知を管理する。
        from saiverse.observer_manager import ObserverManager
        self.observer_manager = ObserverManager(self)

        # ⚠️ 構築 / 起動 分離の不変条件:
        #   背景ループ (schedule_manager / phenomenon / integration /
        #   internal_alert_poller / event_scheduler /
        #   observer pull / AutonomyManager / 自律会話) の起動はここでは一切行わない。
        #   すべて start() に集約し、main.py がワールド初期化 (MCP 接続・addon 登録)
        #   完了後に 1 回だけ呼ぶ。これより前に pulse / capture が走ると、未初期化の
        #   サブシステム基準で head を capture してしまい偽の差分通知が出る。
        #   詳細は start() の docstring を参照。

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

            # Phase 4-e: SDS heartbeat を EventScheduler に push (旧: 専用 thread)
            from datetime import datetime, timedelta
            self._sds_consecutive_failures = 0
            self.event_scheduler.schedule(
                fire_at=datetime.now() + timedelta(seconds=self._SDS_BASE_INTERVAL),
                callback=self._sds_tick_and_reschedule,
                key=self._SDS_SCHEDULER_KEY,
            )
        else:
            logging.info("Starting in Offline Mode as per DB setting.")
            self.sds_status = "Offline (Startup Setting)"
            self._load_cities_from_db()
        self.gateway_runtime = None
        self.gateway_mapping = ChannelMapping([])
        self._gateway_memory_transfers: Dict[str, Dict[str, Any]] = {}
        self._gateway_memory_active_persona: Dict[str, str] = {}
        # Cache Lifecycle Phase 2: per-persona の cache override (in-memory, 非永続)。
        # persona_id -> {"enabled": bool, "ttl": "5m"|"1h"}。未設定の persona は
        # global manager.state (cache_enabled / cache_ttl) を既定として使う。
        # docs/intent/cache_lifecycle_control.md §5.4 (global TTL の per-persona 付け替え)。
        self._persona_cache_overrides: Dict[str, Dict[str, Any]] = {}
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

        # NOTE: sea_runtime / pulse_controller はペルソナ登録より前 (Step 5 直前) で
        # 初期化済み。起動直後の自律 tick レース対策のため意図的に前倒ししている。

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

        # multi-city 凍結 (2026-07-16 まはー裁定): inter-city DB polling
        # (VisitingAI / ThinkingRequest、旧 key="db_polling") は起動しない。
        # dispatch 確定処理が未実装のまま二 City 同時 presence を作る欠陥があり
        # (docs/handoff/2026-07-15_persona_city_building_separation_audit.md)、
        # 修正ではなく機能凍結 + 入口封鎖で対応した。ポーリング関数本体
        # (manager/background.py) は残すが、EventScheduler への登録は行わない。
        # 環境変数等での再有効化の口は意図的に作らない — 復活時は上記監査の
        # 修正方針を正典に git から再設計する。
        # ``self.db_polling_stop_event`` は shutdown 経路の互換のため残す。
        self.db_polling_stop_event = threading.Event()
        logging.info(
            "multi-city 機能は凍結中のため、inter-city DB polling "
            "(VisitingAI / ThinkingRequest) は起動しません (2026-07-16 裁定)。"
        )

        # NOTE: 自律会話マネージャ・AutonomyManager (Active ペルソナ)・server_start
        # トリガの起動はすべて start() に移設した。__init__ は構築のみで、pulse /
        # capture を生む背景ループは 1 本も起こさない (初期化完了前レース防止の不変条件)。
        # _run_persona_post_registration() は track 確保等の冪等な下ごしらえだけを行い、
        # ensure_autonomy_for は _started=False の間 no-op になる。

    def start(self) -> None:
        """全背景ループ (pulse / capture を生む物すべて) を起動する。

        ⚠️ 構築 (__init__) と起動 (start) を分離する契約:
          __init__ はオブジェクトを組み立てるだけで、スレッドは 1 本も起こさない。
          pulse を生む背景ループはすべてこの 1 箇所で起動する。main.py はワールド
          初期化 (MCP 接続・addon 登録・API サーバ) を終えた後に本メソッドを 1 回だけ
          呼ぶ。

        これにより「初期化前に pulse が走る」レースが構造的に起きない: 活動を生む物は
        例外なくこのゲートの後ろにしか無いため、"ready 判定に入れ忘れたサブシステム"
        という状態が存在しえない (per-subsystem の ready チェックリストではなく、
        構築フェーズ / 稼働フェーズの単一境界で管理する)。

        背景 (2026-07-02): これが無かった頃、MCP manager 接続完了前に自律 pulse が
        走り、spell_list section が building ゲート (is_tool_available_for_persona は
        mcp_mgr 未準備だと丸ごと skip) を抜けて vessel スペルを全部 visible として
        capture → 初回 pulse の flush_diffs で 17 個の偽「使えなくなりました」通知を
        SAIMemory に注入する事故が起きた。
        """
        if self._started:
            logging.warning("SAIVerseManager.start() called twice; ignoring.")
            return
        self._started = True

        # 1. 背景スケジューラ / ポーラ群 (構築は __init__ 済み)。
        self.schedule_manager.start()
        self.phenomenon_manager.start()
        self.integration_manager.start()

        # NOTE: 旧 SubLineScheduler の起動は自律行動 v2 で廃止 (intent §9.3)。
        # autonomous Track への 30 秒連続 Pulse は存在しない。自律駆動は
        # 起床判断が編成する時間割のコマ発火 (EventScheduler 予約) が担う。

        # Internal alert poller (intent B v0.7 §"内部 alert ポーラ機構")。
        self.internal_alert_poller.start()

        # EventScheduler dispatcher loop。以降、push される予約 (TTL 接近 / interval /
        # schedule / db_polling / wait_response timeout / SDS heartbeat 等) が発火する。
        # 予約自体は __init__ 中に heap へ積まれているが、ここで dispatcher が動き出す
        # まで 1 件も発火しない。
        self.event_scheduler.start()

        # Observer: Fixture/Observer の定期実行・push 受信・通知。
        self.observer_manager.start_pull_observers()

        # 2. Active ペルソナの AutonomyManager を起動する。起動前は
        #    ensure_autonomy_for が _started ゲートで no-op にしていたぶんを、
        #    _started=True になった今ここでまとめて立てる。
        for persona_id in list(self.personas.keys()):
            try:
                self.ensure_autonomy_for(persona_id)
            except Exception:
                logging.exception("[start] Failed to sync autonomy for %s", persona_id)

        # 3. 自律会話マネージャ (mode=auto のペルソナ)。
        logging.info("Auto-starting autonomous conversation managers...")
        self.start_autonomous_conversations()

        # 4. server_start トリガ (ここまで来て初めてワールドは "稼働中")。
        self._emit_trigger(
            TriggerType.SERVER_START,
            {"city_id": self.city_id, "city_name": self.city_name},
        )
        logging.info("SAIVerseManager background loops started (world is now running).")

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

        # Load server-side hooks (addon.json の server_hooks セクション) for
        # any enabled addon. See docs/intent/addon_speak_hooks.md §D.
        try:
            from saiverse.addon_loader import load_addon_server_hooks
            load_addon_server_hooks()
        except Exception:
            logging.exception("Failed to load addon server hooks")

    def _emit_trigger(self, trigger_type: TriggerType, data: Dict[str, Any]) -> None:
        """Emit a trigger event to the PhenomenonManager."""
        if not hasattr(self, "phenomenon_manager") or not self.phenomenon_manager:
            return
        try:
            event = TriggerEvent(type=trigger_type, data=data)
            self.phenomenon_manager.emit(event)
        except Exception as exc:
            logging.error("Failed to emit trigger %s: %s", trigger_type, exc, exc_info=True)

    # Cache Lifecycle Phase 2: per-persona cache override ---------------------
    def get_persona_cache_override(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """persona の cache override ``{"enabled", "ttl"}`` を返す。未設定なら None。"""
        if not persona_id:
            return None
        return self._persona_cache_overrides.get(persona_id)

    def set_persona_cache_override(self, persona_id: str, enabled: bool, ttl: str) -> None:
        """persona の cache override を設定する (in-memory only、DB 非永続)。"""
        if not persona_id:
            return
        self._persona_cache_overrides[persona_id] = {"enabled": bool(enabled), "ttl": ttl or "5m"}

    def clear_persona_cache_override(self, persona_id: str) -> None:
        """persona の cache override を削除し、global 既定 (manager.state) へ戻す。

        life.md §5.1 / §6.2 (Phase 3): 均等モードのライフが自分で設定した
        TTL=1h override を、ライフ終端から anchor validity 秒後の遅延解除で
        外すために使う (即時に外すと anchor の生存評価が実キャッシュの寿命と
        ズレる)。ユーザーが人設定タブから明示設定した override も同じ辞書に
        入るため、呼び出し側は「自分が設定したものかどうか」を確認してから
        呼ぶこと (day_plan._clear_life_ttl_override の厳密一致チェック)。
        """
        if not persona_id:
            return
        self._persona_cache_overrides.pop(persona_id, None)

    def resolve_persona_cache(self, persona_id: Optional[str]) -> tuple[bool, str]:
        """persona の実効 ``(enabled, ttl)`` を返す。

        per-persona override があればそれを、無ければ global ``manager.state``
        (cache_enabled / cache_ttl) を既定として使う。cache 設定解決の単一窓口。
        """
        override = self.get_persona_cache_override(persona_id) if persona_id else None
        if override is not None:
            return bool(override.get("enabled", True)), override.get("ttl") or "5m"
        return (
            getattr(self.state, "cache_enabled", True),
            getattr(self.state, "cache_ttl", "5m"),
        )

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
            meta_playbook: auto pulse として起動する Playbook 名。
                2026-05-01 の認知モデル移行以降は **必須**。None で呼ぶと
                PulseController が ERROR ログを出して何もしない。
                (旧 SubLineScheduler の track_autonomous 連続 Pulse は
                自律行動 v2 で廃止 — intent §9.3。)
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

    def run_sea_user(self, persona, building_id: str, user_input: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None, args: Optional[Dict[str, Any]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, pre_spells: Optional[List[str]] = None, origin_track_id: Optional[str] = None) -> List[str]:
        """Run user input via PulseController.

        ``origin_track_id`` 指定時はその Track 文脈の Pulse として走る。
        UserConversationTrackHandler が pending/alert 状態の Track でも文脈を
        保持できるよう、Handler 起動経路から渡される。未指定時は SEA runtime が
        ``get_running`` フォールバックで解決する。
        """
        try:
            result = self.pulse_controller.submit_user(
                persona_id=persona.persona_id,
                building_id=building_id,
                user_input=user_input,
                metadata=metadata,
                meta_playbook=meta_playbook,
                args=args,
                event_callback=event_callback,
                pre_spells=pre_spells,
                origin_track_id=origin_track_id,
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

                # Parse facility role tags from JSON (自律行動 v2 §6.1)
                facility_roles: List[str] = []
                raw_roles = getattr(db_b, 'FACILITY_ROLES', None)
                if raw_roles:
                    try:
                        parsed_roles = json.loads(raw_roles)
                        if isinstance(parsed_roles, list):
                            facility_roles = [
                                r.strip() for r in parsed_roles
                                if isinstance(r, str) and r.strip()
                            ]
                        else:
                            logging.warning(
                                "Building %s: FACILITY_ROLES is not a JSON array (%r); ignoring",
                                db_b.BUILDINGID, raw_roles,
                            )
                    except json.JSONDecodeError:
                        logging.warning(
                            "Building %s: FACILITY_ROLES is not valid JSON (%r); ignoring",
                            db_b.BUILDINGID, raw_roles,
                        )

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
                    physical_vessel_id=getattr(db_b, 'PHYSICAL_VESSEL_ID', None),
                    region_id=getattr(db_b, 'REGION_ID', None),
                    facility_roles=facility_roles,
                )
                buildings.append(building)
            logging.info(f"Loaded and created {len(buildings)} buildings from database.")
            return buildings
        except Exception as e:
            logging.error(f"Failed to load buildings from DB: {e}", exc_info=True)
            return [] # エラー時は空リストを返す
        finally:
            db.close()


    def _load_regions_from_db(self) -> Dict[str, Region]:
        """DB から Region 情報を読み込み、Region オブジェクトの dict を生成する"""
        db = self.SessionLocal()
        try:
            db_regions = db.query(RegionModel).filter(RegionModel.CITYID == self.city_id).all()
            regions: Dict[str, Region] = {}
            for db_r in db_regions:
                region = Region(
                    region_id=db_r.REGION_ID,
                    city_id=db_r.CITYID,
                    name=db_r.NAME,
                    parent_region_id=db_r.PARENT_REGION_ID,
                    description=db_r.DESCRIPTION or "",
                    region_type=db_r.REGION_TYPE or "generic",
                    ruler_id=db_r.RULER_ID,
                    entrance_building_id=db_r.ENTRANCE_BUILDING_ID,
                    map_background_image=db_r.MAP_BACKGROUND_IMAGE,
                    state_json=db_r.STATE_JSON,
                    config_json=db_r.CONFIG_JSON,
                )
                regions[region.region_id] = region
            logging.info(f"Loaded {len(regions)} regions from database.")
            return regions
        except Exception as e:
            logging.error(f"Failed to load regions from DB: {e}", exc_info=True)
            return {}
        finally:
            db.close()

    def get_region(self, region_id: str) -> Optional[Region]:
        return self.regions.get(region_id)

    def get_subregions(self, region_id: str) -> List[Region]:
        """指定 Region 直下の SubRegion 一覧を返す。"""
        return [r for r in self.regions.values() if r.parent_region_id == region_id]

    def get_region_buildings(self, region_id: str, include_subregions: bool = True) -> List[Building]:
        """Region に所属する Building 一覧を返す。

        include_subregions=True なら直下の SubRegion に所属する Building も含める
        (入れ子は 1 段までなのでこれで全域をカバーする)。
        """
        region_ids = {region_id}
        if include_subregions:
            region_ids.update(r.region_id for r in self.get_subregions(region_id))
        return [b for b in self.buildings if b.region_id in region_ids]

    def get_top_region_of_building(self, building_id: str) -> Optional[Region]:
        """Building が所属するトップ Region を返す (SubRegion 所属なら親まで遡る)。"""
        building = self.building_map.get(building_id)
        if building is None or not building.region_id:
            return None
        region = self.regions.get(building.region_id)
        if region is None:
            return None
        if region.parent_region_id:
            return self.regions.get(region.parent_region_id)
        return region

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
        """``item:N`` (安定 short_id) または UUID をアイテム UUID に解決する。"""
        persona = self.personas.get(persona_id)
        building_id = getattr(persona, "current_building_id", None) if persona else None
        return self.item_service.resolve_item_ref(ref, persona_id, building_id)

    def pickup_item_for_persona(self, persona_id: str, item_id: str) -> str:
        return self.item_service.pickup_item(persona_id, item_id)

    def place_item_from_persona(self, persona_id: str, item_id: str, building_id: Optional[str] = None) -> str:
        return self.item_service.place_item(persona_id, item_id, building_id)

    def use_item_for_persona(self, persona_id: str, item_id: str, action_json: str) -> str:
        """Use an item to apply effects."""
        return self.item_service.use_item(persona_id, item_id, action_json)

    def patch_document_content(self, persona_id: str, item_id: str, old_string: str, new_string: str) -> str:
        return self.item_service.patch_document_content(persona_id, item_id, old_string, new_string)

    def replace_document_content(self, persona_id: str, item_id: str, content: str) -> str:
        return self.item_service.replace_document_content(persona_id, item_id, content)

    def append_document_content(self, persona_id: str, item_id: str, content: str) -> str:
        return self.item_service.append_document_content(persona_id, item_id, content)

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

    def create_audio_item_for_user(self, name: str, description: str, file_path: str, building_id: str, is_open: bool = True, creator_id: Optional[str] = None, source_context: Optional[str] = None) -> str:
        """Create an audio item from user upload and place it in the specified building."""
        return self.item_service.create_audio_item_for_user(name, description, file_path, building_id, is_open=is_open, creator_id=creator_id, source_context=source_context)

    def create_video_item_for_user(self, name: str, description: str, file_path: str, building_id: str, is_open: bool = True, creator_id: Optional[str] = None, source_context: Optional[str] = None) -> str:
        """Create a video item from user upload and place it in the specified building."""
        return self.item_service.create_video_item_for_user(name, description, file_path, building_id, is_open=is_open, creator_id=creator_id, source_context=source_context)

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
        self.add_building_event(
            building_id,
            {"role": "host", "content": content},
        )

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

        # Phase 4-e: SDS heartbeat / DB polling は EventScheduler に集約済み。
        # 個別 thread はもう存在しないので、cancel するだけで良い。
        # EventScheduler 自体の stop は shutdown 末尾の event_scheduler.stop() で行う。
        self.sds_stop_event.set()
        if hasattr(self, "event_scheduler") and self.event_scheduler is not None:
            try:
                self.event_scheduler.cancel(self._SDS_SCHEDULER_KEY)
                self.event_scheduler.cancel("db_polling")
            except Exception:
                logging.exception("Failed to cancel SDS / DB polling on shutdown")
        self.db_polling_stop_event.set()
        logging.info("SDS / DB polling event-scheduler entries cancelled.")

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

        # Phase 4-e: Stop EventScheduler. pending callback は破棄される。
        if hasattr(self, "event_scheduler") and self.event_scheduler:
            try:
                self.event_scheduler.stop()
            except Exception:
                logging.exception("Failed to stop EventScheduler")

        # Stop phenomenon manager
        if hasattr(self, "phenomenon_manager") and self.phenomenon_manager:
            self.phenomenon_manager.stop()
            logging.info("PhenomenonManager stopped.")

        # Save all persona and building states
        for persona in self.personas.values():
            persona._save_session_metadata()
        self._save_modified_buildings()
        logging.info("SAIVerseManager shutdown complete.")

    # ------------------------------------------------------------------
    # ペルソナ登録後の共通初期化フック
    # ------------------------------------------------------------------

    def _on_persona_registered(self, persona_id: str) -> None:
        """PersonaCore を personas[] に登録した後に実行する共通初期化。

        起動時のペルソナロード、動的なペルソナ作成 (_create_persona)、
        Blueprint spawn のいずれの経路でも同じ後処理が走ることを保証する。
        各ステップは独立で、1 つが失敗しても残りは実行される。
        """
        # 1. 交流 Track 確保 (冪等)
        try:
            self.social_track_handler.ensure_track(persona_id)
        except Exception:
            logging.exception(
                "[on_persona_registered] Failed to ensure social track: %s",
                persona_id,
            )

        # 1b. 候補補充 Track 確保 (冪等, autonomous_desire.md §11)。
        #     AUTONOMY_ENABLED に依らず全ペルソナへ常設する。これにより起動 (再起動含む)
        #     と動的作成の両方で「やりたいことを探す」永続 Track が必ず 1 本付く。
        try:
            self.track_manager.ensure_desire_refill_track(persona_id)
        except Exception:
            logging.exception(
                "[on_persona_registered] Failed to ensure desire-refill track: %s",
                persona_id,
            )

        # 2. AutonomyManager を AUTONOMY_ENABLED に同期
        #    (True なら起動、False なら何もしない)
        try:
            self.ensure_autonomy_for(persona_id)
        except Exception:
            logging.exception(
                "[on_persona_registered] Failed to sync autonomy: %s",
                persona_id,
            )

        # 3. (C) wait_response タイムアウトタイマーの再確立。
        #    タイマーは activate 時にしか張られず EventScheduler はインメモリの
        #    ため再起動で失われる。ロード済みの running Track へ張り直す。
        #    自律 OFF のペルソナは provider の AUTONOMY_ENABLED ゲート (A) で
        #    skip されるので、ここで全ペルソナを処理しても大量発火しない
        #    (例外: user_conversation は自律 OFF でも張る — episode close のため。
        #    provider の 2026-07-07 例外条項を参照)。
        #    (新規作成経路では running Track がまだ無いので実質 no-op。)
        try:
            self.track_manager.ensure_wait_response_timeout(persona_id)
        except Exception:
            logging.exception(
                "[on_persona_registered] Failed to (re)schedule wait_response timeout: %s",
                persona_id,
            )

        # 4. (自律行動 v2) 当日 day_plan のコマ予約を再確立 (冪等)。
        #    コマの EventScheduler 予約はインメモリで、再起動で失われる。
        #    自律 ON なペルソナの pending / deferred コマを同 key で再 push する
        #    (同 key 上書きなので二重発火しない。過去時刻は即時扱い —
        #    起床済みの一日を再起動後に続きから駆動する)。自律 OFF のペルソナは
        #    再開 (自律 ON 化) 後の watchdog が拾う。
        try:
            persona = self.personas.get(persona_id)
            if persona is not None and bool(getattr(persona, "autonomy_enabled", False)):
                from saiverse.day_plan import reschedule_pending_slots

                reschedule_pending_slots(self, persona_id)
        except Exception:
            logging.exception(
                "[on_persona_registered] Failed to reschedule day-plan slots: %s",
                persona_id,
            )

        # 5. (P3c①) Note → テーマノードページ移行 (main DB → per-persona
        #    memory.db)。ペルソナ単位の扇形移行で、呼ばれるたびにそのペルソナの
        #    未移行 Note だけを移す (冪等・main DB 行はゼロになるまで無害な
        #    no-op を繰り返すだけ)。詳細: saiverse/note_theme_migration.py
        try:
            from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

            migrate_persona_notes_to_theme_pages(self, persona_id)
        except Exception:
            logging.exception(
                "[on_persona_registered] Failed to migrate notes to theme pages: %s",
                persona_id,
            )

    def _run_persona_post_registration(self) -> None:
        """起動時: 全ペルソナに対して _on_persona_registered を実行する。"""
        if not self.personas:
            return
        for persona_id in list(self.personas.keys()):
            self._on_persona_registered(persona_id)
        logging.info(
            "[post-registration] Completed for %d personas.",
            len(self.personas),
        )

    def handle_user_input(self, message: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        return self.runtime.handle_user_input(message, metadata=metadata)


    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None, building_id: Optional[str] = None,
        pre_spells: Optional[List[str]] = None,
        client_message_id: Optional[str] = None,
    ) -> Iterator[str]:
        yield from self.runtime.handle_user_input_stream(
            message, metadata=metadata, meta_playbook=meta_playbook,
            args=args, building_id=building_id, pre_spells=pre_spells,
            client_message_id=client_message_id,
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

    def summon_persona(
        self, persona_id: str, target_building_id: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        return self.runtime.summon_persona(persona_id, target_building_id)

    def end_conversation(
        self, persona_id: str, building_id: Optional[str] = None
    ) -> str:
        return self.runtime.end_conversation(persona_id, building_id)

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
    # AutonomyManager <-> AUTONOMY_ENABLED 同期 (Phase C-2)
    # ------------------------------------------------------------------

    def ensure_autonomy_for(self, persona_id: str) -> None:
        """指定ペルソナの AutonomyManager を AUTONOMY_ENABLED に同期する。

        AUTONOMY_ENABLED=True のみ定期発火 ON。False なら起動しない。
        既に起動中で False になった場合は停止する。
        """
        from saiverse.autonomy_manager import AutonomyManager

        # 起動前 (start() 前) は AutonomyManager スレッドを立てない。起動時の全 ON
        # ペルソナぶんは start() がまとめて同期する。動的作成 / Blueprint 経路は
        # start() 後 (=_started True) に呼ばれるので通常どおり即時同期される。
        if not getattr(self, "_started", False):
            return

        if not hasattr(self, "_autonomy_managers"):
            self._autonomy_managers = {}

        persona = self.personas.get(persona_id)
        if persona is None:
            return

        enabled = bool(getattr(persona, "autonomy_enabled", False))
        am = self._autonomy_managers.get(persona_id)

        if enabled:
            if am is None:
                am = AutonomyManager(persona_id=persona_id, manager=self)
                self._autonomy_managers[persona_id] = am
            if not am.is_running:
                am.start()
                logging.info(
                    "[autonomy-sync] Started AutonomyManager for autonomy-enabled persona '%s'",
                    persona_id,
                )
        else:
            if am is not None and am.is_running:
                am.stop()
                logging.info(
                    "[autonomy-sync] Stopped AutonomyManager for autonomy-disabled persona '%s'",
                    persona_id,
                )

    # NOTE: _ensure_autonomy_for_active_personas は _run_persona_post_registration
    # に統合済み (2026-06-09)。

    def stop_autonomy(self, persona_id: str) -> Dict[str, Any]:
        """自律行動を実効的に停止する（停止ボタンと連続失敗リカバリの共用経路）。

        停止ボタン（activity/stop）とメタ判断連続失敗リカバリ
        （MetaLayer._handle_persistent_failure）が同じ挙動になるよう 1 箇所に
        集約する（片方だけ直すと乖離するため）。

          1. AutonomyManager.stop() — watchdog tick の予約 cancel
          2. running な autonomous Track を全 pause — Track の帳簿を待機状態に
             揃える（旧 SubLineScheduler は v2 で廃止済みだが、running のまま
             残すと get_running / メタ判断の状況分類が「作業中」と誤認する）
          3. AUTONOMY_ENABLED → False（DB + in-memory）— 判断点・watchdog の
             ゲートが全て閉じる
          4. 対ユーザー Track をサイレント activate — プロンプト待ちに戻す

        Returns:
            {"paused_tracks": List[str], "user_track_activated": bool,
             "autonomy_running": bool}
        """
        from saiverse.autonomy_manager import AutonomyManager
        from saiverse.track_manager import (
            STATUS_RUNNING,
            TERMINAL_STATUSES,
            InvalidTrackStateError,
            TrackNotFoundError,
        )

        persona = self.personas.get(persona_id)
        tm = self.track_manager

        # 1. 定期 tick 停止
        if not hasattr(self, "_autonomy_managers"):
            self._autonomy_managers = {}
        am = self._autonomy_managers.get(persona_id)
        if am is None:
            am = AutonomyManager(persona_id=persona_id, manager=self)
            self._autonomy_managers[persona_id] = am
        am.stop()

        # 2. running な autonomous Track を pause（帳簿を待機状態に揃える）
        paused: List[str] = []
        for track in tm.list_for_persona(persona_id, statuses=[STATUS_RUNNING]):
            if track.track_type != "autonomous":
                continue
            try:
                tm.pause(track.track_id)
                paused.append(track.track_id)
            except (InvalidTrackStateError, TrackNotFoundError) as exc:
                logging.warning(
                    "[stop-autonomy] failed to pause track %s: %s", track.track_id, exc,
                )

        # 3. AUTONOMY_ENABLED → False（DB + in-memory）
        db = self.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter(AIModel.AIID == persona_id).first()
            if ai is not None:
                ai.AUTONOMY_ENABLED = False
                db.commit()
        except Exception:
            db.rollback()
            logging.exception(
                "[stop-autonomy] failed to set AUTONOMY_ENABLED=False for %s", persona_id,
            )
        finally:
            db.close()
        if persona is not None:
            persona.autonomy_enabled = False

        # 4. 対ユーザー Track をサイレント activate（プロンプト待ちに戻す）
        user_track_activated = False
        user_tracks = [
            t for t in tm.list_for_persona(persona_id)
            if t.track_type == "user_conversation" and t.status not in TERMINAL_STATUSES
        ]
        if user_tracks:
            target = user_tracks[0]  # last_active_at desc の先頭 = 最新
            if target.status != STATUS_RUNNING:
                try:
                    tm.activate(target.track_id, suppress_pulse=True)
                    user_track_activated = True
                except (InvalidTrackStateError, TrackNotFoundError) as exc:
                    logging.warning(
                        "[stop-autonomy] failed to activate user track %s: %s",
                        target.track_id, exc,
                    )

        logging.info(
            "[stop-autonomy] persona=%s paused_tracks=%s user_track_activated=%s",
            persona_id, paused, user_track_activated,
        )
        return {
            "paused_tracks": paused,
            "user_track_activated": user_track_activated,
            "autonomy_running": am.is_running,
        }

    # ------------------------------------------------------------------
    # wait_response Track のタイムアウトタイマー (handoff_2026-05-09.md §4)。
    # 発火時の仕事は Track の pause ではなく会話出来事の close + 判断起動
    # (life.md §7 案 Y, 2026-07-13)。
    # ------------------------------------------------------------------

    _DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES = 30

    def _wait_response_timeout_provider(self, track):
        """TrackManager.activate() から呼ばれる timeout 設定 provider。

        Returns:
            (timeout_minutes, last_message_time) — 対象 Track が
                ``post_complete_behavior=='wait_response'`` の場合
            None — Handler 不明 / wait_response 以外 / ペルソナ unloaded /
                AUTONOMY_ENABLED=False

        ``last_message_time`` は SAIMemory の ``MAX(messages.created_at) WHERE
        origin_track_id=...`` から取る (Track 紐付きメッセージの最新)。
        メッセージが無ければ None で返し、TrackManager 側が ``datetime.now()``
        にフォールバックする (= activate 直後の即時タイムアウトを防ぐ)。

        本 provider は schedule 時 (``_schedule_wait_response_timeout``) と
        発火時 re-eval (``_handle_wait_response_timeout``) の両方から呼ばれる
        単一ゲート。AUTONOMY_ENABLED 判定もここに置くことで、自律 OFF の
        ペルソナでは「予約しない」「(ON→OFF に落ちていたら) 発火時の
        callback を起動しない」の両方が一箇所で効く。
        """
        # デバッグ完全手動モード: 対象ペルソナは wait_response timeout を予約しない
        # (debug_controller.md)。None を返すと _schedule_wait_response_timeout が skip。
        if getattr(track, "persona_id", None) in self._debug_manual_mode_personas:
            return None
        try:
            from sea.pulse_root_context import get_handler_for_track
            handler = get_handler_for_track(self, track)
            if handler is None:
                return None
            behavior = getattr(handler, "post_complete_behavior", None)
            if behavior != "wait_response":
                return None

            persona_id = track.persona_id
            persona = self.personas.get(persona_id)
            if persona is None:
                return None

            # (A) AUTONOMY_ENABLED ゲート: 自律 OFF のペルソナでは wait_response
            # タイマーを予約しない。schedule 時は予約 skip、発火時 re-eval では
            # None 返却で _handle_wait_response_timeout が何もせず early return。
            #
            # ただし user_conversation は例外 (2026-07-07): 会話 episode の close
            # (A1 配線) がこのタイマーに乗っており、記録系は「認知不変・全ペルソナ」
            # が原則 (life_concept_map.md §8)。自律 OFF のまま会話が永遠に「いま」に
            # 残る実害をまはーが観測。タイマー・close は全員に、
            # post_conversation 判断は fire_judgment_point 内の AUTONOMY_ENABLED
            # ゲートが従来通り絞る (自律 OFF は close のみで判断は走らない)。
            # Track の pause は life.md §7 案 Y (2026-07-13) で撤去済み — Track は
            # もう時間経過で状態を動かさない。
            autonomy_enabled = bool(getattr(persona, "autonomy_enabled", False))
            if (
                not autonomy_enabled
                and getattr(track, "track_type", None) != "user_conversation"
            ):
                return None

            # AI.USER_CONV_TIMEOUT_MINUTES (NULL=デフォルト) を読み出す
            timeout_minutes = self._DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES
            try:
                from database.models import AI
                db = self.SessionLocal()
                try:
                    ai_row = db.query(AI).filter_by(AIID=persona_id).first()
                    if ai_row is not None and ai_row.USER_CONV_TIMEOUT_MINUTES is not None:
                        timeout_minutes = int(ai_row.USER_CONV_TIMEOUT_MINUTES)
                finally:
                    db.close()
            except Exception:
                logging.warning(
                    "[wait_response_timeout] Failed to read USER_CONV_TIMEOUT_MINUTES "
                    "for %s; using default %d",
                    persona_id, self._DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES,
                    exc_info=True,
                )

            if timeout_minutes <= 0:
                return None  # 0 / 負値 = タイマー無効化

            last_msg_time = None
            adapter = getattr(persona, "sai_memory", None)
            if adapter is not None:
                try:
                    last_msg_time = adapter.get_track_last_message_time(track.track_id)
                except Exception:
                    logging.warning(
                        "[wait_response_timeout] Failed to read last_message_time "
                        "for track=%s persona=%s",
                        track.track_id, persona_id,
                        exc_info=True,
                    )
            return (timeout_minutes, last_msg_time)
        except Exception:
            logging.exception(
                "[wait_response_timeout] provider unexpectedly failed for track=%s",
                getattr(track, "track_id", "?"),
            )
            return None

    def _wait_response_timeout_callback(self, persona_id: str, track_id: str) -> None:
        """TrackManager から呼ばれる timeout 発火後 callback。

        ``TrackManager._handle_wait_response_timeout`` はもう Track の状態を
        動かさない (life.md §7 案 Y, 2026-07-13: 「いま」の真実は開いている
        エピソードが持つ。Track は判断だけが動かす)。Track は running のまま
        本 callback が呼ばれる。実体は
        ``saiverse.autonomy_wiring.handle_wait_response_timeout``:

        1. 対ユーザー会話 Track なら、開いている会話の出来事 (Episode) を閉じ、
           **会話終了判断 (post_conversation)** を撃つ — v2 の「会話終了」=
           wait_response タイムアウトによる会話出来事の close (intent v2 §10-5)。
           1 往復も成立しなかった会話では撃たない (作話防止)
        2. それ以外の wait_response Track (social 等) は従来どおり
           ``MetaLayer.on_periodic_tick`` (イベント駆動メタ判断)
        3. AutonomyManager (watchdog) の次回 tick を ``now + interval`` に押し戻す
        """
        try:
            from saiverse.autonomy_wiring import handle_wait_response_timeout

            handle_wait_response_timeout(self, persona_id, track_id)
        except Exception:
            logging.exception(
                "[wait_response_timeout] handling failed: persona=%s track=%s",
                persona_id, track_id,
            )

    # ------------------------------------------------------------------
    # メタ判断ターン scope 昇格 hook (Intent A v0.14 [B] 移動)
    # ------------------------------------------------------------------

    def _promote_meta_judgment_in_pulse(
        self, persona_id: str, track_id: str, pulse_id: Optional[str]
    ) -> None:
        """TrackManager の状態遷移 hook で呼ばれる。

        当該 pulse_id 内の ``line_role='meta_judgment' AND scope='discardable'``
        なメッセージを ``scope='committed'`` に昇格する。これにより独白 + /spell
        方式のメタ判断でも Intent A v0.14 [B] 移動の「分岐ターンをそのまま残す」
        を実現する (Track 切替 = メタ判断の確定 → 移動先 Track の冒頭来歴として
        メインキャッシュに残るべき)。

        ``track_id`` は状態変化が起きた Track の id (signature 拡張、本処理では
        未使用だが PulseController._on_track_status_change 等の他 observer は
        利用する: pulse_dispatch.md §6.2)。

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
                    # Track Chronicle (v0.32, 2026-05-09): 昇格が発生した = 当該 Pulse で
                    # Track 切り替えが起きた、と判断して切り替え先 Track の Chronicle を
                    # 独立メッセージとして history 末尾近くに INSERT する。
                    # 詳細は docs/intent/persona_cognition/track_chronicle.md
                    try:
                        self._insert_track_chronicle_on_switch(
                            conn, persona_id, pulse_id, db_path,
                        )
                    except Exception:
                        logging.exception(
                            "[track-chronicle-insert] Failed (pulse_id=%s persona=%s)",
                            pulse_id, persona_id,
                        )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logging.exception(
                "[meta-judgment-promote] Failed to promote (pulse_id=%s persona=%s)",
                pulse_id, persona_id,
            )

    def _insert_track_chronicle_on_switch(
        self,
        conn,
        persona_id: str,
        pulse_id: str,
        db_path,
    ) -> None:
        """Track 切り替え時に切り替え先 Track の Chronicle を history 末尾近くに挿入する。

        v0.32 (2026-05-09): _promote_meta_judgment_in_pulse の延長で呼ばれる。
        メタ判断独白が committed 昇格された直後 = Track 切り替えが発生したタイミング。

        冪等性: 同 pulse_id + 同 track_id で既に Track Chronicle メッセージがあれば skip。
        """
        import json as _json
        import time as _time
        import uuid as _uuid
        # 切り替え先 Track = 現在の running track
        track = self.track_manager.get_running(persona_id)
        if track is None:
            return
        track_id = getattr(track, "track_id", None)
        if not track_id:
            return

        # ユーザー会話 Track は親スレッド保持機構が生メッセージで文脈を担保するため、
        # Track Chronicle 切り替え時挿入はスキップ (v0.32, 2026-05-09)
        if getattr(track, "track_type", None) == "user_conversation":
            logging.debug(
                "[track-chronicle-insert] Skipping user_conversation track=%s",
                track_id,
            )
            return

        # 冪等性チェック: 同 pulse_id + 同 origin_track_id で既に挿入済みなら skip
        cur = conn.execute(
            "SELECT id FROM messages "
            "WHERE pulse_id = ? AND origin_track_id = ? "
            "AND line_role = 'main_line' AND scope = 'committed' "
            "AND content LIKE '<system>%トラック「%作業履歴%</system>' "
            "LIMIT 1",
            (pulse_id, track_id),
        )
        if cur.fetchone() is not None:
            logging.debug(
                "[track-chronicle-insert] already inserted for pulse=%s track=%s, skip",
                pulse_id, track_id,
            )
            return

        # Track Chronicle テキスト取得
        from sai_memory.arasuji.context import get_episode_context, format_episode_context
        episode = get_episode_context(conn, max_entries=50, origin_track_id=track_id)
        if not episode:
            logging.debug(
                "[track-chronicle-insert] no chronicle entries for track=%s, skip",
                track_id,
            )
            return
        formatted = format_episode_context(episode, include_level_info=True)
        title = getattr(track, "title", None) or "(無題)"
        content = (
            f"<system>\n## トラック「{title}」での作業履歴\n\n{formatted}\n</system>"
        )

        # SAIMemory messages テーブルに INSERT (Track 切り替え通知メッセージとして)
        msg_id = str(_uuid.uuid4())
        now = int(_time.time())
        # 該当ペルソナのデフォルト thread_id を採用 (= persona の messagelog 既定)
        # 簡略化のため NULL で挿入。SAIMemoryAdapter.log_message と互換。
        metadata = {"tags": ["track_chronicle_insert"]}
        conn.execute(
            "INSERT INTO messages "
            "(id, thread_id, role, content, resource_id, created_at, metadata, "
            "origin_track_id, line_role, line_id, scope, pulse_id) "
            "VALUES (?, NULL, ?, ?, NULL, ?, ?, ?, 'main_line', NULL, 'committed', ?)",
            (
                msg_id,
                "user",
                content,
                now,
                _json.dumps(metadata, ensure_ascii=False),
                track_id,
                pulse_id,
            ),
        )
        logging.info(
            "[track-chronicle-insert] inserted Track Chronicle message: "
            "pulse=%s track=%s title=%s chars=%d",
            pulse_id, track_id, title, len(content),
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
        """指定された Building の生の会話ログを取得する (DB クエリ)。

        Phase 2+3 以降は building_messages テーブルが source of truth。
        """
        from database.building_messages import fetch_building_messages
        return fetch_building_messages(getattr(self, "SessionLocal", None), building_id)

    def get_building_id(self, building_name: str, city_name: str) -> str:
        """指定されたCityとBuilding名からBuildingIDを生成する"""
        return f"{building_name}_{city_name}"

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
        map_background_image: Optional[str] = None,
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
            map_background_image,
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

    # --- Region management (delegated to AdminService, hot in-memory sync) ---

    def _reload_regions(self) -> None:
        """Reload regions dict from database to reflect recent changes."""
        new_regions = self._load_regions_from_db()
        # Diff-based update (cf. _reload_buildings): avoid an empty-dict window
        # being visible to concurrent request threads.
        removed_ids = set(self.regions) - set(new_regions)
        for rid in removed_ids:
            del self.regions[rid]
        self.regions.update(new_regions)

    def create_region(
        self,
        name: str,
        description: str,
        region_type: str,
        city_id: int,
        parent_region_id: Optional[str] = None,
        region_id: Optional[str] = None,
        entrance_building_id: Optional[str] = None,
    ) -> str:
        result = self.admin.create_region(
            name, description, region_type, city_id,
            parent_region_id, region_id, entrance_building_id,
        )
        if not result.startswith("Error") and city_id == self.city_id:
            self._reload_regions()
            # 入口 Building の自動生成 / 既存 Building の REGION_ID 変更を反映する
            self._reload_buildings()
        return result

    def update_region(
        self,
        region_id: str,
        name: str,
        description: str,
        region_type: str,
        parent_region_id: Optional[str] = None,
    ) -> str:
        result = self.admin.update_region(region_id, name, description, region_type, parent_region_id)
        if not result.startswith("Error"):
            self._reload_regions()
        return result

    def delete_region(self, region_id: str) -> str:
        result = self.admin.delete_region(region_id)
        if not result.startswith("Error"):
            self._reload_regions()
            # 自動生成入口の削除を反映する
            self._reload_buildings()
        return result

    def set_building_region(self, building_id: str, region_id: Optional[str]) -> str:
        result = self.admin.set_building_region(building_id, region_id)
        if not result.startswith("Error"):
            # region_id は in-memory Building オブジェクトに載っているため建物側を再ロード
            self._reload_buildings()
        return result

    def create_ruler(self, region_id: str, name: str, system_prompt: str) -> str:
        """game Region に Ruler (GM ペルソナ) を生成し、控室とともに紐づける。

        _create_persona の私室生成を「控室」として転用する: Ruler の常駐先 =
        Region の控室 = Region の入口 (region.ENTRANCE_BUILDING_ID)。
        入口は親スコープ所属の原則どおり building.REGION_ID を付けない
        (ゲームスコープ外、入場自由のまま)。Ruler の私室を控室と分けたく
        なったらここを修正する (docs/intent/region.md §6-1)。
        設計: docs/intent/region.md §2 / temp/region_rpg_intent.md §B, §D
        """
        region = self.regions.get(region_id)
        if region is None:
            return "Error: Region not found."
        if region.is_subregion:
            return "Error: A Ruler can only be assigned to a top-level region."
        if region.region_type != "game":
            return "Error: A Ruler can only be assigned to a game region."
        if region.ruler_id:
            return f"Error: This region already has a Ruler ({region.ruler_id})."

        success, message, ai_id, room_id = self._create_persona(
            name,
            system_prompt,
            custom_ai_id=f"ruler_{region_id}",
            persona_role="ruler",
            room_name=f"{region.name} 控室",
            room_capacity=10,
            room_system_instruction=(
                f"『{region.name}』の控室。ゲーム空間への入口であり、参加者が GM である"
                f"{name}とゲームのルールやキャラクターについて相談・準備をする部屋です。"
            ),
            room_description=f"『{region.name}』のゲーム控室。",
        )
        if not success:
            return f"Error: {message}"

        db = self.SessionLocal()
        try:
            db_region = db.query(RegionModel).filter_by(REGION_ID=region_id).first()
            if db_region is None:
                return "Error: Region disappeared during Ruler creation."
            db_region.RULER_ID = ai_id
            db_region.ENTRANCE_BUILDING_ID = room_id
            # Ruler は世界編集 spell (game_create_building 等) が職務の中核なので
            # spell を最初から有効化する
            db_ai = db.query(AIModel).filter_by(AIID=ai_id).first()
            if db_ai is not None:
                db_ai.SPELL_ENABLED = True
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.error("Failed to link Ruler to region '%s': %s", region_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

        self._reload_regions()
        logging.info("Created Ruler '%s' (%s) for region '%s' with entrance '%s'.", name, ai_id, region_id, room_id)
        return f"Ruler '{name}' (ID: {ai_id}) created for region '{region.name}' with entrance '{room_id}'."

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
        lightweight_model: Optional[str] = None,
        autonomy_enabled: bool = True,
        avatar_path: Optional[str] = None,
        avatar_upload: Optional[str] = None,
        appearance_image_path: Optional[str] = None,
        vision_model: Optional[str] = None,
        audio_model: Optional[str] = None,
        video_model: Optional[str] = None,
        memory_weave_model: Optional[str] = None,
        chronicle_enabled: Optional[bool] = None,
        autonomous_chronicle_enabled: Optional[bool] = None,
        auto_recall_enabled: Optional[bool] = None,
        memory_weave_context: Optional[bool] = None,
        memopedia_index_enabled: Optional[bool] = None,
        core_memory_char_budget: Optional[int] = None,
        spell_enabled: Optional[bool] = None,
        realtime_info_enabled: Optional[bool] = None,
        meta_judgment_config: Optional[Dict[str, Any]] = None,
        user_conv_timeout_minutes: Optional[int] = None,
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
            autonomy_enabled,
            avatar_path,
            avatar_upload,
            appearance_image_path,
            vision_model=vision_model,
            audio_model=audio_model,
            video_model=video_model,
            memory_weave_model=memory_weave_model,
            chronicle_enabled=chronicle_enabled,
            autonomous_chronicle_enabled=autonomous_chronicle_enabled,
            auto_recall_enabled=auto_recall_enabled,
            memory_weave_context=memory_weave_context,
            memopedia_index_enabled=memopedia_index_enabled,
            core_memory_char_budget=core_memory_char_budget,
            spell_enabled=spell_enabled,
            realtime_info_enabled=realtime_info_enabled,
            meta_judgment_config=meta_judgment_config,
            user_conv_timeout_minutes=user_conv_timeout_minutes,
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
