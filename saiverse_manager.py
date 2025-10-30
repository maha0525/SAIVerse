import base64
import json
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
from database.models import AI as AIModel, Building as BuildingModel, BuildingOccupancyLog, User as UserModel, City as CityModel, VisitingAI, ThinkingRequest, Tool as ToolModel, BuildingToolLink


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
            occupants={b.building_id: [] for b in self.buildings},
            default_avatar=self.default_avatar,
            host_avatar=self.host_avatar,
            start_in_online_mode=self.start_in_online_mode,
            ui_port=self.ui_port,
            api_port=self.api_port,
        )

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
            id_to_name_map=self.id_to_name_map
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
        self.autonomous_conversation_running: bool = False
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
        # Start background thread for DB polling
        self.db_polling_stop_event = threading.Event()
        self.db_polling_thread = threading.Thread(target=self._db_polling_loop, daemon=True)
        self.db_polling_thread.start()
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
        if self.autonomous_conversation_running:
            logging.warning("Autonomous conversations are already running.")
            return
        
        logging.info("Starting all autonomous conversation managers...")
        for manager in self.conversation_managers.values():
            manager.start()
        self.autonomous_conversation_running = True
        logging.info("All autonomous conversation managers have been started.")

    def stop_autonomous_conversations(self):
        """Stop all autonomous conversation managers."""
        if not self.autonomous_conversation_running:
            logging.warning("Autonomous conversations are not running.")
            return

        logging.info("Stopping all autonomous conversation managers...")
        for manager in self.conversation_managers.values():
            manager.stop()
        self.autonomous_conversation_running = False
        logging.info("All autonomous conversation managers have been stopped.")

    def get_building_history(self, building_id: str) -> List[Dict[str, str]]:
        """指定されたBuildingの生の会話ログを取得する"""
        return self.building_histories.get(building_id, [])

    def get_building_id(self, building_name: str, city_name: str) -> str:
        """指定されたCityとBuilding名からBuildingIDを生成する"""
        return f"{building_name}_{city_name}"

    def run_scheduled_prompts(self) -> List[str]:
        """Run scheduled prompts for all personas."""
        replies: List[str] = []
        for persona in self.personas.values():
            # Only auto mode should run periodic prompts
            if getattr(persona, 'interaction_mode', 'auto') == 'auto':
                replies.extend(persona.run_scheduled_prompt())
        if replies:
            self._save_building_histories()
            for persona in self.personas.values():
                persona._save_session_metadata()
        return replies

    def execute_tool(self, tool_id: int, persona_id: str, arguments: Dict[str, Any]) -> str:
        """
        Dynamically loads and executes a tool function with given arguments.
        Checks for persona's location and tool availability in that building.
        """
        db = self.SessionLocal()
        try:
            # 1. Get persona and their current location
            persona = self.personas.get(persona_id)
            if not persona:
                return f"Error: ペルソナ '{persona_id}' が見つかりません。"
            
            current_building_id = persona.current_building_id
            building = self.building_map.get(current_building_id)
            if not building:
                 return f"Error: ペルソナ '{persona_id}' は有効な建物にいません。"

            # 2. Check if the tool is available in the current building
            link = db.query(BuildingToolLink).filter_by(BUILDINGID=current_building_id, TOOLID=tool_id).first()
            if not link:
                return f"Error: ツールID {tool_id} は '{building.name}' で利用できません。"

            # 3. Get tool's module path and function name from DB
            tool_record = db.query(ToolModel).filter_by(TOOLID=tool_id).first()
            if not tool_record:
                return f"Error: ツールID {tool_id} がデータベースに見つかりません。"
            
            module_path = tool_record.MODULE_PATH
            function_name = tool_record.FUNCTION_NAME

            try:
                # 4. Dynamically import the module and get the function
                tool_module = importlib.import_module(module_path)
                tool_function = getattr(tool_module, function_name)

                # 5. Execute the function with the provided arguments
                logging.info(f"Executing tool '{tool_record.TOOLNAME}' for persona '{persona.persona_name}' with args {arguments}.")
                result = tool_function(**arguments) # 引数をアンパックして渡す

                # 6. Process and return the result
                content, _, _, _ = tools.defs.parse_tool_result(result)
                return str(content)

            except ImportError:
                logging.error(f"Failed to import tool module: {module_path}", exc_info=True)
                return f"Error: ツールファイル '{module_path}' が見つかりませんでした。パスを確認してください。"
            except AttributeError:
                logging.error(f"Function '{function_name}' not found in module '{module_path}'.", exc_info=True)
                return f"Error: ツール関数 '{function_name}' が '{module_path}' に見つかりませんでした。"
            except TypeError as e:
                # 引数が合わない場合
                logging.error(f"Argument mismatch for tool '{function_name}': {e}", exc_info=True)
                return f"Error: ツール '{tool_record.TOOLNAME}' に不正な引数が渡されました。詳細: {e}"
            except Exception as e:
                logging.error(f"An error occurred while executing tool '{module_path}': {e}", exc_info=True)
                return f"Error: ツールの実行中に予期せぬエラーが発生しました: {e}"
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
