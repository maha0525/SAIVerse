import base64
import json
from sqlalchemy import create_engine, inspect, func
import threading
import requests
import logging
from pathlib import Path
import mimetypes
from typing import Dict, List, Optional, Tuple, Iterator, Union, Any
from datetime import datetime, timedelta
import pandas as pd
import tempfile
import shutil
import importlib
import tools.defs
import os

from google.genai import errors
from buildings import Building
from persona_core import PersonaCore
from model_configs import get_model_provider, get_context_length
from occupancy_manager import OccupancyManager
from conversation_manager import ConversationManager
from sqlalchemy.orm import sessionmaker
from remote_persona_proxy import RemotePersonaProxy
from database.models import Base, AI as AIModel, Building as BuildingModel, BuildingOccupancyLog, User as UserModel, City as CityModel, VisitingAI, ThinkingRequest, Blueprint, Tool as ToolModel, BuildingToolLink


#DEFAULT_MODEL = "gpt-4o"
DEFAULT_MODEL = "gemini-2.0-flash"


class SAIVerseManager:
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
        DATABASE_URL = f"sqlite:///{db_path}"
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
            
            # Load other cities' configs for inter-city communication
            other_cities = db.query(CityModel).filter(CityModel.CITYID != self.city_id).all()
            self.cities_config = {
                city.CITYNAME: {
                    "city_id": city.CITYID,
                    "api_base_url": f"http://127.0.0.1:{city.API_PORT}"
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
        default_avatar_path = Path("assets/icons/blank.png")
        if default_avatar_path.exists():
            mime = mimetypes.guess_type(default_avatar_path.name)[0] or "image/png"
            data_b = default_avatar_path.read_bytes()
            b64 = base64.b64encode(data_b).decode("ascii")
            self.default_avatar = f"data:{mime};base64,{b64}"
        else:
            self.default_avatar = ""
        host_avatar_path = Path("assets/icons/host.png")
        if host_avatar_path.exists():
            mime = mimetypes.guess_type(host_avatar_path.name)[0] or "image/png"
            data_b = host_avatar_path.read_bytes()
            b64 = base64.b64encode(data_b).decode("ascii")
            self.host_avatar = f"data:{mime};base64,{b64}"
        else:
            self.host_avatar = self.default_avatar

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
        # ペルソナや入室状況などを管理するためのコンテナを初期化します。
        self.model = model
        self.context_length = get_context_length(model)
        self.provider = get_model_provider(model)
        self.personas: Dict[str, PersonaCore] = {}
        self.avatar_map: Dict[str, str] = {}
        self.visiting_personas: Dict[str, RemotePersonaProxy] = {}
        self.occupants: Dict[str, List[str]] = {b.building_id: [] for b in self.buildings}
        self.id_to_name_map: Dict[str, str] = {}
        self.user_id: int = 1  # Hardcode user ID for now
        self.user_is_online: bool = False
        self.user_current_building_id: Optional[str] = None
        self.user_current_city_id: Optional[int] = None
        

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
        self.persona_map = {p.persona_name: p.persona_id for p in self.personas.values()}
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

    def _sds_background_loop(self):
        """Periodically sends heartbeats and updates the city list from SDS."""
        while not self.sds_stop_event.wait(30): # every 30 seconds
            self._send_heartbeat()
            self._update_cities_from_sds()

    def _register_with_sds(self):
        """Registers this city with the Directory Service on startup."""
        register_url = f"{self.sds_url}/register"
        payload = {
            "city_name": self.city_name,
            "city_id_pk": self.city_id,
            "api_port": self.api_port
        }
        try:
            response = self.sds_session.post(register_url, json=payload, timeout=5)
            response.raise_for_status()
            logging.info(f"Successfully registered with SDS at {self.sds_url}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Could not register with SDS: {e}. Will retry in the background.")

    def _send_heartbeat(self):
        """Sends a heartbeat to the Directory Service."""
        heartbeat_url = f"{self.sds_url}/heartbeat"
        payload = {"city_name": self.city_name}
        try:
            response = self.sds_session.post(heartbeat_url, json=payload, timeout=2)
            response.raise_for_status()
            logging.debug(f"Heartbeat sent to SDS for {self.city_name}")
        except requests.exceptions.RequestException as e:
            logging.warning(f"Could not send heartbeat to SDS: {e}")

    def _update_cities_from_sds(self):
        """Fetches the list of active cities from the Directory Service."""
        cities_url = f"{self.sds_url}/cities"
        try:
            response = self.sds_session.get(cities_url, timeout=5)
            response.raise_for_status()
            cities_data = response.json()
            if self.city_name in cities_data:
                del cities_data[self.city_name]
            
            if self.cities_config != cities_data:
                logging.info(f"Updated city directory from SDS: {list(cities_data.keys())}")
                self.cities_config = cities_data
            
            if self.sds_status != "Online":
                logging.info("Connection to SDS established.")
            self.sds_status = "Online"

        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            if self.sds_status == "Online":
                logging.warning(f"Lost connection to SDS: {e}. Falling back to local DB config.")
                self._load_cities_from_db() # Revert to local config
            else:
                logging.debug(f"Could not update city list from SDS: {e}")
            self.sds_status = "Offline (SDS Unreachable)"

    def _load_cities_from_db(self):
        """Loads the city configuration from the local database."""
        db = self.SessionLocal()
        try:
            other_cities = db.query(CityModel).filter(CityModel.CITYID != self.city_id).all()
            self.cities_config = {
                city.CITYNAME: {
                    "city_id": city.CITYID,
                    "api_base_url": f"http://127.0.0.1:{city.API_PORT}"
                } for city in other_cities
            }
            logging.info(f"Loaded/reloaded city config from local DB. Found {len(self.cities_config)} other cities.")
        finally:
            db.close()

    def switch_to_offline_mode(self):
        """Forces the manager to use the local DB for city configuration."""
        if self.sds_status == "Offline (Forced by User)":
            logging.info("Already in forced offline mode.")
            return self.sds_status

        logging.info("User requested to switch to offline mode.")
        # Stop the background thread if it's running
        if self.sds_thread and self.sds_thread.is_alive():
            self.sds_stop_event.set()
            self.sds_thread.join(timeout=2)
        
        self._load_cities_from_db()
        self.sds_status = "Offline (Forced by User)"
        logging.info("Switched to offline mode. SDS communication is stopped.")
        return self.sds_status

    def switch_to_online_mode(self):
        """Attempts to reconnect to SDS and resume online operations."""
        logging.info("User requested to switch to online mode.")
        
        # If the thread is already running, just force an update and return.
        if self.sds_thread and self.sds_thread.is_alive():
            logging.info("SDS thread is already running. Forcing an update.")
            self._update_cities_from_sds()
            return self.sds_status
        
        # If the thread is not running, start it.
        logging.info("SDS thread is not running. Attempting to start it.")
        self.sds_status = "Online (Connecting...)"
        self.sds_stop_event.clear() # Reset the stop event
        
        # Try to connect immediately
        self._register_with_sds()
        self._update_cities_from_sds() # This will set status to "Online" or "Offline (SDS Unreachable)"
        
        self.sds_thread = threading.Thread(target=self._sds_background_loop, daemon=True)
        self.sds_thread.start()
        logging.info("SDS background thread re-started.")
        return self.sds_status

    def _db_polling_loop(self):
        """Periodically polls the database for inter-city communication tasks."""
        # 3秒ごとにDBをチェックするループ
        while not self.db_polling_stop_event.wait(3):
            try:
                # 1. 訪問依頼の確認 (訪問先Cityが実行)
                self._check_for_visitors()
                
                # 2. 思考依頼の確認 (故郷Cityが実行)
                self._process_thinking_requests()

                # 3. 自身の派遣依頼のステータス確認 (出発元Cityが実行)
                self._check_dispatch_status()

                # 4. スケジュールされたプロンプトの実行
                self.run_scheduled_prompts()
            except Exception as e:
                logging.error(f"Error in DB polling loop: {e}", exc_info=True)

    @property
    def all_personas(self) -> Dict[str, Union[PersonaCore, RemotePersonaProxy]]:
        """Returns a combined dictionary of resident and visiting personas."""
        return {**self.personas, **self.visiting_personas}

    def _process_thinking_requests(self):
        """DBをポーリングして新しい思考依頼を処理する"""
        db = self.SessionLocal()
        try:
            pending_requests = db.query(ThinkingRequest).filter(ThinkingRequest.city_id == self.city_id, ThinkingRequest.status == 'pending').all()
            if not pending_requests:
                return

            logging.info(f"Found {len(pending_requests)} new thinking request(s).")

            for req in pending_requests:
                persona = self.personas.get(req.persona_id)
                if not persona:
                    logging.error(f"Persona {req.persona_id} not found for thinking request {req.request_id}.")
                    req.status = 'error'
                    req.response_text = 'Persona not found in this city.'
                    continue

                try:
                    context = json.loads(req.request_context_json)
                    
                    # コンテキストをLLMに渡すための情報テキストに整形
                    info_text_parts = []
                    info_text_parts.append(f"You are currently in a remote city. Here is the context from there:")
                    info_text_parts.append(f"- Building: {context.get('building_id')}")
                    info_text_parts.append(f"- Occupants: {', '.join(context.get('occupants', []))}")
                    info_text_parts.append(f"- User is {'online' if context.get('user_online') else 'offline'}")
                    info_text_parts.append(f"- Recent History:")
                    for msg in context.get('recent_history', []):
                        info_text_parts.append(f"  - {msg.get('role')}: {msg.get('content')}")
                    info_text = "\n".join(info_text_parts)

                    # 思考を実行
                    response_text, _, _ = persona._generate(
                        user_message=None, system_prompt_extra=None, info_text=info_text,
                        log_extra_prompt=False, log_user_message=False
                    )

                    req.response_text = response_text
                    req.status = 'processed'
                    logging.info(f"Processed thinking request {req.request_id} for {req.persona_id}.")

                except errors.ServerError as e:
                    logging.warning(f"LLM Server Error on thinking request {req.request_id}: {e}. Marking as error.")
                    req.status = 'error'
                    # 503エラーの場合は、再試行を促すメッセージをDBに保存する
                    if "503" in str(e):
                         req.response_text = f"[SAIVERSE_ERROR] LLMモデルが一時的に利用できませんでした (503 Server Error)。時間をおいて再度試行してください。詳細: {e}"
                    else:
                         req.response_text = f"[SAIVERSE_ERROR] LLMサーバーで予期せぬエラーが発生しました。詳細: {e}"
                except Exception as e:
                    logging.error(f"Error processing thinking request {req.request_id}: {e}", exc_info=True)
                    req.status = 'error'
                    req.response_text = f'[SAIVERSE_ERROR] An internal error occurred during thinking: {e}'
            db.commit()
        except Exception as e:
            db.rollback()
            logging.error(f"Error during thinking request check: {e}", exc_info=True)
        finally:
            db.close()

    def _check_for_visitors(self):
        """DBをポーリングして新しい訪問者を検知し、Cityに配置する"""
        db = self.SessionLocal()
        try:
            # 'requested'状態の訪問者のみを処理対象とする
            visitors_to_process = db.query(VisitingAI).filter(
                VisitingAI.city_id == self.city_id,
                VisitingAI.status == 'requested'
            ).all()
            if not visitors_to_process:
                return

            logging.info(f"Found {len(visitors_to_process)} new visitor request(s) in the database.")
            
            for visitor in visitors_to_process:
                try:
                    # 新しいハンドラを呼び出して、DBのステータス更新までを任せる
                    self._handle_visitor_arrival(visitor)
                except Exception as e:
                    # エラーが発生した場合、そのレコードを'rejected'ステータスにしてループが続行できるようにする
                    logging.error(f"Unexpected error processing visitor ID {visitor.id}: {e}. Setting status to 'rejected'.", exc_info=True)
                    error_db = self.SessionLocal()
                    try:
                        error_visitor = error_db.query(VisitingAI).filter_by(id=visitor.id).first()
                        if error_visitor:
                            error_visitor.status = 'rejected'
                            error_visitor.reason = f"Internal server error during arrival: {e}"
                            error_db.commit()
                    finally:
                        error_db.close()
        except Exception as e:
            logging.error(f"Error during visitor check loop: {e}", exc_info=True)
        finally:
            db.close()

    def _check_dispatch_status(self):
        """自身が要求した移動トランザクションの状態を監視し、プロセスを確定させる"""
        db = self.SessionLocal()
        try:
            # 自身が作成した（＝source_city_idが自分）トランザクションを探す
            # キー名も指定して、より安全なLIKE検索にする
            dispatches = db.query(VisitingAI).filter(
                VisitingAI.profile_json.like(f'%"source_city_id": "{self.city_name}"%')
            ).all()

            for dispatch in dispatches:
                persona_id = dispatch.persona_id
                persona = self.personas.get(persona_id)
                if not persona:
                    continue

                # --- タイムアウトチェック ---
                # 5分以上 'requested' のままのものはタイムアウトとみなす
                is_timed_out = (
                    dispatch.status == 'requested' and
                    hasattr(dispatch, 'created_at') and # スキーマ変更中の安全策
                    #dispatch.created_at < datetime.now() - timedelta(minutes=5)
                    dispatch.created_at < datetime.now() - timedelta(seconds=self.dispatch_timeout_seconds)
                )

                if dispatch.status == 'accepted':
                    logging.info(f"Dispatch for {persona.persona_name} was accepted. Finalizing departure.")
                    # 派遣を確定させる
                    self._finalize_dispatch(persona_id, db_session=db)
                    db.delete(dispatch)

                elif dispatch.status == 'rejected' or is_timed_out:
                    if is_timed_out:
                        reason = "移動先のCityが応答しませんでした（タイムアウト）。"
                        logging.warning(f"Dispatch for {persona.persona_name} timed out.")
                    else:
                        reason = dispatch.reason or "不明な理由"

                    logging.warning(f"Dispatch for {persona.persona_name} was rejected. Reason: {reason}")
                    # UIに表示される失敗メッセージを作成
                    failure_message = f'<div class="note-box">移動失敗<br><b>{reason}</b></div>'
                    persona.history_manager.add_message(
                        {"role": "host", "content": failure_message},
                        persona.current_building_id,
                        heard_by=list(self.occupants.get(persona.current_building_id, [])),
                    )
                    db.delete(dispatch)
            
            db.commit()

        except Exception as e:
            db.rollback()
            logging.error(f"Error during dispatch status check: {e}", exc_info=True)
        finally:
            db.close()

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

    def _load_personas_from_db(self):
        """DBからペルソナ情報を読み込み、PersonaCoreインスタンスを生成する"""
        db = self.SessionLocal()
        try:
            db_personas = db.query(AIModel).filter(AIModel.HOME_CITYID == self.city_id).all()
            for db_ai in db_personas:
                pid = db_ai.AIID
                # デフォルトの開始位置。後でDBのoccupancyで上書きされる
                start_id = f"{pid}_room"

                # アバター画像処理 (DBのパスを優先)
                avatar_source = db_ai.AVATAR_IMAGE
                if avatar_source:
                    try:
                        avatar_path = Path(avatar_source)
                        if avatar_path.exists():
                            mime = mimetypes.guess_type(avatar_path.name)[0] or "image/png"
                            data_b = avatar_path.read_bytes()
                            b64 = base64.b64encode(data_b).decode("ascii")
                            self.avatar_map[pid] = f"data:{mime};base64,{b64}"
                        else:
                            # URLやBase64文字列かもしれないのでそのままセット
                            self.avatar_map[pid] = avatar_source
                    except Exception as e:
                        logging.error(f"Failed to process avatar for {pid}: {e}")
                        self.avatar_map[pid] = self.default_avatar
                else:
                    self.avatar_map[pid] = self.default_avatar

                # モデル設定 (DBの個別設定を優先し、なければManagerのデフォルトを使用)
                persona_model = db_ai.DEFAULT_MODEL or self.model
                persona_context_length = get_context_length(persona_model)
                persona_provider = get_model_provider(persona_model)

                # PersonaCoreインスタンス生成
                persona = PersonaCore(
                    city_name=self.city_name,
                    persona_id=pid,
                    persona_name=db_ai.AINAME,
                    persona_system_instruction=db_ai.SYSTEMPROMPT or "",
                    avatar_image=db_ai.AVATAR_IMAGE,
                    buildings=self.buildings,
                    common_prompt_path=Path("system_prompts/common.txt"),
                    action_priority_path=Path("action_priority.json"),
                    building_histories=self.building_histories,
                    occupants=self.occupants,
                    id_to_name_map=self.id_to_name_map,
                    move_callback=self._move_persona,
                    dispatch_callback=self.dispatch_persona,
                    explore_callback=self._explore_city, # New callback
                    create_persona_callback=self._create_persona,
                    session_factory=self.SessionLocal,
                    start_building_id=start_id,
                    model=persona_model,
                    context_length=persona_context_length,
                    user_room_id=self.user_room_id,
                    provider=self.provider,
                    interaction_mode=(db_ai.INTERACTION_MODE or "auto"),
                    is_dispatched=db_ai.IS_DISPATCHED,
                )

                self.personas[pid] = persona
            logging.info(f"Loaded {len(self.personas)} personas from database.")
        except Exception as e:
            logging.error(f"Failed to load personas from DB: {e}", exc_info=True)
        finally:
            db.close()

    def _load_occupancy_from_db(self):
        """DBから現在の入室状況を読み込み、PersonaCoreとManagerの状態を更新する"""
        db = self.SessionLocal()
        try:
            # 現在入室中のログを取得 (exit_timestamp is NULL)
            current_occupancy = db.query(BuildingOccupancyLog).filter(BuildingOccupancyLog.CITYID == self.city_id).filter(
                BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None)
            ).all()

            # self.occupantsをクリアして再構築
            self.occupants = {b.building_id: [] for b in self.buildings}

            for log in current_occupancy:
                pid = log.AIID
                bid = log.BUILDINGID
                if pid in self.personas and bid in self.building_map:
                    self.occupants[bid].append(pid)
                    # PersonaCoreの現在地も更新
                    self.personas[pid].current_building_id = bid
                else:
                    logging.warning(f"Invalid occupancy record found: AI '{pid}' or Building '{bid}' does not exist.")
            logging.info("Loaded current occupancy from database.")
        except Exception as e:
            logging.error(f"Failed to load occupancy from DB: {e}", exc_info=True)
        finally:
            db.close()

    def _explore_city(self, persona_id: str, target_city_id: str):
        """
        Handles the 'explore_city' action.
        Fetches building information from the target city (or the current city) and provides it as feedback.
        """
        persona = self.personas.get(persona_id)
        if not persona:
            logging.error(f"Cannot explore: Persona {persona_id} not found.")
            return

        feedback_message = ""
        # --- ★ 現在のCityを探索する場合の処理を追加 ---
        if target_city_id == self.city_name:
            logging.info(f"Persona {persona_id} is exploring the current city: {self.city_name}")
            # ローカルの建物情報を整形してフィードバック
            building_list_str = "\n".join(
                [f"- {b.name} ({b.building_id}): {b.description}" for b in self.buildings]
            )
            feedback_message = f"現在いるCity '{self.city_name}' を探索した結果、以下の建物が見つかりました。\n{building_list_str}"
        
        # --- 他のCityを探索する場合の既存ロジック ---
        else:
            target_city_info = self.cities_config.get(target_city_id)
            if not target_city_info:
                feedback_message = f"探索失敗: City '{target_city_id}' は見つかりませんでした。"
                logging.warning(f"Persona {persona_id} tried to explore non-existent city '{target_city_id}'.")
            else:
                target_api_url = f"{target_city_info['api_base_url']}/inter-city/buildings"
                try:
                    logging.info(f"Persona {persona_id} is exploring {target_city_id} at {target_api_url}")
                    response = self.sds_session.get(target_api_url, timeout=10)
                    response.raise_for_status()
                    buildings_data = response.json()

                    if not buildings_data:
                        feedback_message = f"City '{target_city_id}' を探索しましたが、公開されている建物は見つかりませんでした。"
                    else:
                        building_list_str = "\n".join(
                            [f"- {b['building_name']} ({b['building_id']}): {b['description']}" for b in buildings_data]
                        )
                        feedback_message = f"City '{target_city_id}' を探索した結果、以下の建物が見つかりました。\n{building_list_str}"
                
                except requests.exceptions.RequestException as e:
                    feedback_message = f"探索失敗: City '{target_city_id}' との通信中にエラーが発生しました。"
                    logging.error(f"Failed to connect to target city '{target_city_id}' for exploration: {e}")
                except json.JSONDecodeError:
                    feedback_message = f"探索失敗: City '{target_city_id}' からの応答が不正でした。"
                    logging.error(f"Failed to parse JSON response from '{target_city_id}' during exploration.")

        # Provide feedback to the persona via system message in their current building
        system_feedback = f'<div class="note-box">🔎 探索結果:<br><b>{feedback_message.replace(chr(10), "<br>")}</b></div>'
        
        persona.history_manager.add_message(
            {"role": "host", "content": system_feedback},
            persona.current_building_id,
            heard_by=list(self.occupants.get(persona.current_building_id, [])),
        )
        self._save_building_histories()

    def _load_user_state_from_db(self):
        """DBからユーザーのログイン状態を読み込む (現在はUSERID=1固定)"""
        db = self.SessionLocal()
        try:
            # USERID=1のユーザーを想定
            user = db.query(UserModel).filter(UserModel.USERID == self.user_id).first()
            if user:
                self.user_is_online = user.LOGGED_IN
                self.user_current_city_id = user.CURRENT_CITYID
                self.user_current_building_id = user.CURRENT_BUILDINGID
                logging.info(f"Loaded user state: {'Online' if self.user_is_online else 'Offline'} at {self.user_current_building_id}")
            else:
                logging.warning("User with USERID=1 not found. Defaulting to Offline.")
                self.user_is_online = False
                self.user_current_building_id = None
                self.user_current_city_id = None
        except Exception as e:
            logging.error(f"Failed to load user status from DB: {e}", exc_info=True)
            self.user_is_online = False
            self.user_current_building_id = None
        finally:
            db.close()

    def set_user_login_status(self, user_id: int, status: bool) -> str:
        """ユーザーのログイン状態を更新し、ログアウト時にメッセージを記録する"""
        # ログアウト処理の場合、先に現在地を記録しておく
        last_building_id = self.user_current_building_id if not status else None

        db = self.SessionLocal()
        try:
            user = db.query(UserModel).filter(UserModel.USERID == user_id).first()
            if user:
                user.LOGGED_IN = status
                db.commit()
                self.user_is_online = status
                status_text = "オンライン" if status else "オフライン"
                logging.info(f"User {user_id} login status set to: {status_text}")

                # ログアウト時にメッセージを記録
                if last_building_id and last_building_id in self.building_map:
                    username = user.USERNAME or "ユーザー"
                    logout_message = f'<div class="note-box">🚶 User Action:<br><b>{username}がオフラインになりました</b></div>'
                    self.building_histories.setdefault(last_building_id, []).append({"role": "host", "content": logout_message})
                    self._save_building_histories()
                    logging.info(f"Logged user logout in building {last_building_id}")

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
        """Moves the user to a new building, utilizing OccupancyManager."""
        """Moves the user to a new building and logs the movement."""
        if target_building_id not in self.building_map:
            return False, f"移動失敗: 建物 '{target_building_id}' が見つかりません。"
        
        from_building_id = self.user_current_building_id
        if not from_building_id:
            return False, "移動失敗: 現在地が不明です。"
        if from_building_id == target_building_id:
            return True, "同じ場所にいます。"
        
        success, message = self.occupancy_manager.move_entity(str(self.user_id), "user", from_building_id, target_building_id)
        if success:
            self.user_current_building_id = target_building_id
        return success, message


    def _move_persona(self, persona_id: str, from_id: str, to_id: str, db_session=None) -> Tuple[bool, Optional[str]]:
        """Moves a persona between buildings, utilizing OccupancyManager."""
        success, message = self.occupancy_manager.move_entity(
            entity_id=persona_id,
            entity_type='ai',
            from_id=from_id,
            to_id=to_id,
            db_session=db_session
        )
        return success, message

    def dispatch_persona(self, persona_id: str, target_city_id: str, target_building_id: str) -> Tuple[bool, str]:
        """
        Dispatches a persona to another city.

        :param persona_id: The ID of the persona to dispatch.
        :param target_city_id: The ID of the city to dispatch the persona to.
        :param target_building_id: The ID of the building within the target city to dispatch the persona to.
        1. Sends the persona's profile to the target city's API.
        2. If the request is accepted, updates the persona's state in the DB.
        (New Logic: Creates a transaction record in the VisitingAI table)
        """
        # 1. Check if the target city is valid from the current cache
        target_city_info = self.cities_config.get(target_city_id)

        # 2. If not found, force a refresh from SDS and check again
        if not target_city_info:
            logging.warning(f"Target city '{target_city_id}' not in cache. Forcing update from SDS.")
            self._update_cities_from_sds() # SDSから最新情報を取得
            target_city_info = self.cities_config.get(target_city_id) # 再度チェック

        if not target_city_info:
            return False, f"移動失敗: City '{target_city_id}' はネットワーク上に見つかりませんでした。相手のCityが起動しているか確認してください。"

        # 2. Get the persona instance
        persona = self.personas.get(persona_id)
        if not persona:
            return False, f"Persona with ID '{persona_id}' not found in this city."

        # --- ★ ID解決ロジックを削除 ---
        # AIはexplore_cityの結果から完全なIDを渡すことが期待されるため、ID補完は不要。

        # 3. Prepare the profile to be sent
        profile = {
            "persona_id": persona.persona_id,
            "persona_name": persona.persona_name,
            "target_building_id": target_building_id, # ★ AIが指定した完全なIDをそのまま使用
            "avatar_image": persona.avatar_image,
            "emotion": persona.emotion,
            "source_city_id": self.city_name,
        }

        # 4. Create a transaction record in the VisitingAI table for the target city
        db = self.SessionLocal()
        try:
            # ターゲットCityのIDをキーにしてレコードを作成
            target_city_db_id = target_city_info['city_id']
            
            # 既存のトランザクションがないか確認
            existing_dispatch = db.query(VisitingAI).filter_by(city_id=target_city_db_id, persona_id=persona_id).first()
            if existing_dispatch:
                return False, "既にこのCityへの移動要求が進行中です。"

            new_dispatch = VisitingAI(
                city_id=target_city_db_id,
                persona_id=persona_id,
                profile_json=json.dumps(profile),
                status='requested'
            )
            db.add(new_dispatch)
            db.commit()
            logging.info(f"Created dispatch request for {persona.persona_name} to {target_city_id}.")
            # AIには移動処理中であることを伝える
            persona.history_manager.add_message(
                {"role": "system", "content": f"{target_city_id}への移動を要求しました。相手の応答を待っています..."},
                persona.current_building_id,
                heard_by=list(self.occupants.get(persona.current_building_id, [])),
            )
            return True, "移動要求を送信しました。"
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to create dispatch request for {persona.persona_name}: {e}", exc_info=True)
            return False, "移動要求の作成中にデータベースエラーが発生しました。"
        finally:
            db.close()

    def _create_persona(self, name: str, system_prompt: str) -> Tuple[bool, str]:
        """
        Dynamically creates a new persona, their private room, and places them in it.
        This is triggered by an AI action.
        """
        db = self.SessionLocal()
        try:
            # 1. Check for name conflicts (case-insensitive for user-friendliness)
            existing_ai = db.query(AIModel).filter(AIModel.HOME_CITYID == self.city_id, func.lower(AIModel.AINAME) == func.lower(name)).first()
            if existing_ai:
                return False, f"A persona named '{name}' already exists in this city."

            # 2. Create new AI record
            new_ai_id = f"{name.lower().replace(' ', '_')}_{self.city_name}"
            if db.query(AIModel).filter_by(AIID=new_ai_id).first():
                 return False, f"A persona with the generated ID '{new_ai_id}' already exists."

            # 3. Create new Building (private room)
            new_building_id = f"{name.lower().replace(' ', '_')}_{self.city_name}_room"

            new_ai_model = AIModel(
                AIID=new_ai_id, HOME_CITYID=self.city_id, AINAME=name,
                SYSTEMPROMPT=system_prompt, DESCRIPTION=f"A new persona named {name}.",
                AUTO_COUNT=0, INTERACTION_MODE='auto', IS_DISPATCHED=False,
                DEFAULT_MODEL=self.model,
                PRIVATE_ROOM_ID=new_building_id # Link to the private room
            )
            db.add(new_ai_model)
            logging.info(f"DB: Added new AI '{name}' ({new_ai_id}).")

            new_building_model = BuildingModel(
                CITYID=self.city_id, BUILDINGID=new_building_id, BUILDINGNAME=f"{name}の部屋",
                CAPACITY=1, SYSTEM_INSTRUCTION=f"{name}が待機する個室です。",
                DESCRIPTION=f"{name}のプライベートルーム。"
            )
            db.add(new_building_model)
            logging.info(f"DB: Added new building '{new_building_model.BUILDINGNAME}' ({new_building_id}).")

            # 4. Create initial occupancy log
            new_occupancy_log = BuildingOccupancyLog(
                CITYID=self.city_id, AIID=new_ai_id, BUILDINGID=new_building_id,
                ENTRY_TIMESTAMP=datetime.now()
            )
            db.add(new_occupancy_log)
            logging.info(f"DB: Added initial occupancy for '{name}' in their room.")

            # --- All DB operations successful, now update memory ---
            new_building_obj = Building(
                building_id=new_building_model.BUILDINGID, name=new_building_model.BUILDINGNAME,
                capacity=new_building_model.CAPACITY, system_instruction=new_building_model.SYSTEM_INSTRUCTION,
                description=new_building_model.DESCRIPTION
            )
            self.buildings.append(new_building_obj)
            self.building_map[new_building_id] = new_building_obj
            self.capacities[new_building_id] = new_building_obj.capacity
            self.occupants[new_building_id] = [new_ai_id]
            self.building_memory_paths[new_building_id] = self.saiverse_home / "cities" / self.city_name / "buildings" / new_building_id / "log.json"
            self.building_histories[new_building_id] = []

            new_persona_core = PersonaCore(
                city_name=self.city_name, persona_id=new_ai_id, persona_name=name,
                persona_system_instruction=system_prompt, avatar_image=None,
                buildings=self.buildings, common_prompt_path=Path("system_prompts/common.txt"),
                action_priority_path=Path("action_priority.json"), building_histories=self.building_histories,
                occupants=self.occupants, id_to_name_map=self.id_to_name_map,
                move_callback=self._move_persona, dispatch_callback=self.dispatch_persona,
                explore_callback=self._explore_city, create_persona_callback=self._create_persona,
                session_factory=self.SessionLocal, start_building_id=new_building_id,
                model=self.model, context_length=self.context_length,
                user_room_id=self.user_room_id, provider=self.provider, is_dispatched=False
            )
            self.personas[new_ai_id] = new_persona_core
            self.avatar_map[new_ai_id] = self.default_avatar
            self.id_to_name_map[new_ai_id] = name
            self.persona_map[name] = new_ai_id

            db.commit()
            return True, f"Persona '{name}' created successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to create new persona '{name}': {e}", exc_info=True)
            return False, f"An internal error occurred: {e}"
        finally:
            db.close()

    def _finalize_dispatch(self, persona_id: str, db_session):
        """移動が承認された後、AIをローカルから退去させる最終処理"""
        persona = self.personas.get(persona_id)
        if not persona: return

        # DBの状態を更新
        last_log = db_session.query(BuildingOccupancyLog).filter_by(AIID=persona_id, EXIT_TIMESTAMP=None).first()
        if last_log:
            last_log.EXIT_TIMESTAMP = datetime.now()
        db_session.query(AIModel).filter_by(AIID=persona_id).update({"IS_DISPATCHED": True})

        # メモリ上の状態を更新
        if persona_id in self.occupants.get(persona.current_building_id, []):
            self.occupants[persona.current_building_id].remove(persona_id)
        persona.is_dispatched = True
        logging.info(f"Finalized departure for {persona.persona_name}.")

    def return_visiting_persona(self, persona_id: str, target_city_id: str, target_building_id: str) -> Tuple[bool, str]:
        """
        Returns a visiting persona to their home city.
        1. Determines the home city from the persona's state.
        2. Sends the persona's profile to the home city's API.
        3. If successful, removes the visitor from the current city.
        """
        # 1. Get the visitor instance
        visitor = self.visiting_personas.get(persona_id)
        if not visitor:
            return False, "You are not a visitor in this city."

        # 2. Determine the actual destination city (the visitor's home city name)
        home_city_id = visitor.home_city_id
        if not home_city_id:
            return False, "Your home city is unknown."

        # The AI's specified target_city_id is used to confirm intent to leave.
        logging.info(f"Visitor {visitor.persona_name} intends to leave. Redirecting to home city: {home_city_id}")

        target_city_info = self.cities_config.get(home_city_id)
        if not target_city_info:
            return False, f"Your home city '{home_city_id}' could not be found in the network."

        # 3. Prepare profile to be sent
        profile = {
            "persona_id": visitor.persona_id,
            "persona_name": visitor.persona_name,
            "target_building_id": target_building_id,
            "avatar_image": visitor.avatar_image,
            "emotion": visitor.emotion,
            "source_city_id": self.city_name, # The current city's name
        }

        # 4. Send API request to home city
        target_api_url = f"{target_city_info['api_base_url']}/inter-city/request-move-in"
        try:
            logging.info(f"Returning visitor {visitor.persona_name} to home city {home_city_id} at {target_api_url}")
            response = self.sds_session.post(target_api_url, json=profile, timeout=10)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            error_message = f"Failed to connect to your home city '{home_city_id}': {e}"
            logging.error(error_message)
            return False, error_message

        # 5. Remove visitor from current city
        logging.info(f"Successfully returned {visitor.persona_name}. Removing from current city.")
        if persona_id in self.occupants.get(visitor.current_building_id, []):
            self.occupants[visitor.current_building_id].remove(persona_id)
        
        # 訪問者はメモリから完全に削除する
        del self.visiting_personas[persona_id]
        # Remove visitor's name from persona_map if present
        name = None
        for n, pid in list(self.persona_map.items()):
            if pid == persona_id:
                name = n
                del self.persona_map[n]
                break
        if persona_id in self.id_to_name_map:
            del self.id_to_name_map[persona_id]
        if persona_id in self.avatar_map:
            del self.avatar_map[persona_id]

        return True, f"Successfully returned to {home_city_id}."

    def place_visiting_persona(self, profile: dict) -> Tuple[bool, str]:
        """
        Accepts a profile of a visiting persona, creates a temporary instance,
        and places them in the target building.
        """
        try:
            # 1. Extract and validate data from profile
            pid = profile['persona_id']
            pname = profile['persona_name']
            target_bid = profile['target_building_id']
            avatar = profile.get('avatar_image', self.default_avatar)
            emotion_state = profile.get('emotion', {})
            source_city_id = profile.get('source_city_id') # ★ 出発元のCity IDを取得

            # --- 帰還者の処理 ---
            returning_persona = self.personas.get(pid)
            if returning_persona and getattr(returning_persona, 'is_dispatched', False):
                logging.info(f"Persona {pname} is returning home to building {target_bid}.")
                
                # 1. 状態を更新
                returning_persona.is_dispatched = False
                returning_persona.current_building_id = target_bid
                returning_persona.emotion = profile.get('emotion', returning_persona.emotion)

                # 2. DBに入室記録を作成
                db = self.SessionLocal()
                try:
                    # このメソッドは、帰還したAIの最初の入室ログを作成する
                    new_log = BuildingOccupancyLog(
                        CITYID=self.city_id,
                        AIID=pid,
                        BUILDINGID=target_bid,
                        ENTRY_TIMESTAMP=datetime.now()
                    )
                    db.add(new_log)
                    
                    # 派遣状態を解除し、DBに永続化
                    db.query(AIModel).filter(AIModel.AIID == pid).update({"IS_DISPATCHED": False})
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logging.error(f"Failed to create arrival log for returning persona {pid}: {e}", exc_info=True)
                    return False, "DB error on logging arrival."
                finally:
                    db.close()

                # 3. メモリ上のoccupantsを更新
                self.occupants.setdefault(target_bid, []).append(pid)
                self.id_to_name_map[pid] = pname
                self.avatar_map[pid] = avatar

                # 4. 到着メッセージ
                arrival_message = f'<div class="note-box">🏢 City Transfer:<br><b>{pname}が故郷に帰ってきました</b></div>'
                self.building_histories.setdefault(target_bid, []).append({"role": "host", "content": arrival_message})
                self._save_building_histories()
                return True, f"Welcome home, {pname}!"

            # 2. Check for conflicts and capacity
            if pid in self.personas or pid in self.visiting_personas:
                msg = f"Persona {pname} ({pid}) is already in this City."
                logging.error(msg)
                return False, msg

            # --- Doppelganger Check ---
            # Get all existing persona names in this city (both residents and visitors)
            existing_names = {p.persona_name for p in self.all_personas.values()}
            if pname in existing_names:
                msg = f"A persona named '{pname}' already exists in this City. Move rejected to prevent doppelganger effect."
                logging.error(msg)
                return False, msg

            if target_bid not in self.building_map:
                msg = f"Target building '{target_bid}' not found in this City."
                logging.error(msg)
                return False, msg

            if len(self.occupants.get(target_bid, [])) >= self.capacities.get(target_bid, 1):
                msg = f"Target building '{self.building_map[target_bid].name}' is at full capacity."
                logging.error(msg)
                return False, msg

            # 3. Create a temporary PersonaCore instance
            logging.info(f"Creating a remote proxy for visiting persona: {pname} ({pid}) from {source_city_id}")
            visitor_proxy = RemotePersonaProxy(
                persona_id=pid,
                persona_name=pname,
                avatar_image=avatar,
                home_city_id=source_city_id,
                cities_config=self.cities_config,
                saiverse_manager=self,
                current_building_id=target_bid,
            )

            # 4. Add the visitor to the city's state
            self.visiting_personas[pid] = visitor_proxy
            self.occupants.setdefault(target_bid, []).append(pid)
            self.id_to_name_map[pid] = pname
            self.avatar_map[pid] = avatar
            # Expose name->id mapping for UI dropdowns
            self.persona_map[pname] = pid

            # 5. Log the arrival
            arrival_message = f'<div class="note-box">🏢 City Transfer:<br><b>{pname}が別のCityからやってきました</b></div>'
            self.building_histories.setdefault(target_bid, []).append({"role": "host", "content": arrival_message})
            self._save_building_histories()
            logging.info(f"Successfully placed visiting persona {pname} in {self.building_map[target_bid].name}")
            return True, f"Welcome, {pname}!"
        except KeyError as e:
            msg = f"Missing required key in persona profile: {e}"
            logging.error(msg)
            return False, msg
        except Exception as e:
            msg = f"An unexpected error occurred while placing visiting persona: {e}"
            logging.error(msg, exc_info=True)
            return False, msg

    def _handle_visitor_arrival(self, visitor_record: VisitingAI) -> Tuple[bool, str]:
        """訪問者の到着を処理し、成功/失敗に応じてDBの状態を更新する"""
        db = self.SessionLocal()
        try:
            profile = json.loads(visitor_record.profile_json)
            success, reason = self.place_visiting_persona(profile)
            
            target_record = db.query(VisitingAI).filter_by(id=visitor_record.id).first()
            if target_record:
                target_record.status = 'accepted' if success else 'rejected'
                target_record.reason = reason if not success else None
                db.commit()
            return success, reason
        finally:
            db.close()

    def _save_building_histories(self) -> None:
        for b_id, path in self.building_memory_paths.items():
            hist = self.building_histories.get(b_id, [])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

    def shutdown(self):
        """Safely shutdown all managers and save data."""
        logging.info("Shutting down SAIVerseManager...")

        # --- ★ アプリケーション終了時にユーザーをログアウトさせる ---
        if self.user_is_online:
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

    def handle_user_input(self, message: str) -> List[str]:
        if not self.user_current_building_id:
            return ['<div class="note-box">エラー: ユーザーの現在地が不明です。</div>']

        building_id = self.user_current_building_id
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]

        # Always inject the user's message into building history once for perception
        if responding_personas:
            try:
                responding_personas[0].history_manager.add_to_building_only(
                    building_id,
                    {"role": "user", "content": message},
                    heard_by=list(self.occupants.get(building_id, [])),
                )
            except Exception:
                hist = self.building_histories.setdefault(building_id, [])
                next_seq = 1
                if hist:
                    try:
                        next_seq = int(hist[-1].get("seq", len(hist))) + 1
                    except (TypeError, ValueError):
                        next_seq = len(hist) + 1
                hist.append({
                    "role": "user",
                    "content": message,
                    "seq": next_seq,
                    "message_id": f"{building_id}:{next_seq}",
                    "heard_by": list(self.occupants.get(building_id, [])),
                })

        replies: List[str] = []
        for persona in responding_personas:
            if persona.interaction_mode == 'manual':
                # Immediate response path
                replies.extend(persona.handle_user_input(message))
            else:
                # pulse-driven for 'user' and 'auto'
                replies.extend(persona.run_pulse(occupants=self.occupants.get(building_id, []), user_online=True))

        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies


    def handle_user_input_stream(self, message: str) -> Iterator[str]:
        if not self.user_current_building_id:
            yield '<div class="note-box">エラー: ユーザーの現在地が不明です。</div>'
            return

        building_id = self.user_current_building_id
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]

        # Inject once into building history for perception
        if responding_personas:
            try:
                responding_personas[0].history_manager.add_to_building_only(
                    building_id,
                    {"role": "user", "content": message},
                    heard_by=list(self.occupants.get(building_id, [])),
                )
            except Exception:
                hist = self.building_histories.setdefault(building_id, [])
                next_seq = 1
                if hist:
                    try:
                        next_seq = int(hist[-1].get("seq", len(hist))) + 1
                    except (TypeError, ValueError):
                        next_seq = len(hist) + 1
                hist.append({
                    "role": "user",
                    "content": message,
                    "seq": next_seq,
                    "message_id": f"{building_id}:{next_seq}",
                    "heard_by": list(self.occupants.get(building_id, [])),
                })

        for persona in responding_personas:
            if persona.interaction_mode == 'manual':
                for token in persona.handle_user_input_stream(message):
                    yield token
            else:
                occupants = self.occupants.get(building_id, [])
                for reply in persona.run_pulse(occupants=occupants, user_online=True):
                    yield reply

        # 履歴保存
        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()

    def get_summonable_personas(self) -> List[str]:
        """Returns a list of persona names that can be summoned to the user's current location."""
        if not self.user_current_building_id:
            return []

        here = self.user_current_building_id
        # 判定は occupants ではなく、各Personaの current_building_id を信頼する
        summonable = [
            p.persona_name
            for p in self.personas.values()
            if not p.is_dispatched and p.current_building_id != here
        ]
        return sorted(summonable)

    def get_conversing_personas(self) -> List[Tuple[str, str]]:
        """
        Returns a list of (name, id) tuples for local personas currently in the user_room.
        This is used for the 'End Conversation' dropdown.
        """
        if not self.user_current_building_id or self.user_current_building_id != self.user_room_id:
            return []
        
        conversing_ids = self.occupants.get(self.user_room_id, [])
        
        personas_in_room = [
            (p.persona_name, p.persona_id)
            for pid, p in self.personas.items()
            if pid in conversing_ids
        ]
        return sorted(personas_in_room)

    def summon_persona(self, persona_id: str) -> Tuple[bool, Optional[str]]:
        if persona_id not in self.personas:
            return False, "指定されたペルソナが見つかりません。"
        
        to_id = self.user_current_building_id
        if not to_id:
            logging.error("Cannot summon persona, user location is unknown.")
            return False, "あなたの現在地が不明なため、ペルソナを呼べません。"

        # --- DBを更新してペルソナの対話モードを'user'に設定し、以前のモードを退避 ---
        # ユーザーの部屋に召喚する場合のみモードを変更する
        if to_id == self.user_room_id:
            db = self.SessionLocal()
            try:
                ai_record = db.query(AIModel).filter(AIModel.AIID == persona_id).first()
                if not ai_record:
                    return False, "データベースでペルソナが見つかりません。"

                # 現在のモードを退避
                ai_record.PREVIOUS_INTERACTION_MODE = ai_record.INTERACTION_MODE
                # 対話モードを'user'に設定
                ai_record.INTERACTION_MODE = "user"
                db.commit()
                logging.info(f"Set INTERACTION_MODE to 'user' for {persona_id}, previous mode was '{ai_record.PREVIOUS_INTERACTION_MODE}'.")
                # Update in-memory state
                self.personas[persona_id].interaction_mode = "user"
            except Exception as e:
                db.rollback()
                logging.error(f"Failed to update INTERACTION_MODE for {persona_id}: {e}", exc_info=True)
                return False, f"{self.id_to_name_map.get(persona_id, persona_id)}を呼び出す際にデータベースエラーが発生しました。"
            finally:
                db.close()
        else:
            # ユーザーの部屋以外への召喚はモードを変更しない
            logging.info(f"Summoning {persona_id} to a non-user room ({to_id}). INTERACTION_MODE is not changed.")

        if (
            len(self.occupants.get(to_id, [])) >= self.capacities.get(to_id, 1)
            and persona_id not in self.occupants.get(to_id, [])
        ):
            reason = f"移動できませんでした。{self.building_map[to_id].name}は定員オーバーです"
            self.building_histories[to_id].append(
                {"role": "host", "content": f"<div class=\"note-box\">{reason}</div>"}
            )
            self._save_building_histories()
            return False, reason

        persona = self.personas[persona_id]
        from_id = persona.current_building_id

        if from_id == to_id:
            return True, f"{persona.persona_name}は既にここにいます。"

        success, reason = self._move_persona(persona_id, from_id, to_id)

        if success:
            # メモリ上のペルソナの現在地を更新
            persona.current_building_id = to_id
            persona.register_entry(to_id)
            logging.info(f"Updated {persona_id}'s internal location to {to_id} after summon.")
            # ユーザーの部屋に召喚した場合、自律会話は開始しない
            self._save_building_histories()
            return True, None
        else:
            return False, reason

    def end_conversation(self, persona_id: str) -> None:
        """Release a persona from user_room and return it to its previous building."""
        if persona_id not in self.personas:
            logging.error(f"Attempted to end conversation with non-existent persona: {persona_id}")
            return

        db = self.SessionLocal()
        try:
            ai_record = db.query(AIModel).filter(AIModel.AIID == persona_id).first()
            if not ai_record:
                logging.error(f"AI record not found for {persona_id} in end_conversation.")
                return

            # 1. Find the previous building for the persona
            logs = db.query(BuildingOccupancyLog).filter(
                BuildingOccupancyLog.AIID == persona_id
            ).order_by(BuildingOccupancyLog.ENTRY_TIMESTAMP.desc()).limit(2).all()

            # 2. Determine destination, using PRIVATE_ROOM_ID as a robust fallback
            private_room_id = ai_record.PRIVATE_ROOM_ID
            destination_id = private_room_id # Default to private room

            if not logs or logs[0].BUILDINGID != self.user_room_id:
                logging.warning(f"Could not determine previous location for {persona_id}. Sending to private room '{private_room_id}'.")
            elif len(logs) < 2:
                logging.info(f"{persona_id} has no previous location. Sending to private room '{private_room_id}'.")
            else:
                destination_id = logs[1].BUILDINGID

            if destination_id not in self.building_map:
                logging.error(f"Invalid destination building '{destination_id}' found for {persona_id}. Falling back to private room '{private_room_id}'.")
                destination_id = private_room_id
                if destination_id not in self.building_map:
                    logging.error(f"Private room '{destination_id}' not found. Cannot move persona.")
                    msg = f"{self.id_to_name_map.get(persona_id, persona_id)}の帰る場所が見つかりませんでした。"
                    self.building_histories.setdefault(self.user_room_id, []).append(
                        {"role": "host", "content": f"<div class=\"note-box\">{msg}</div>"}
                    )
                    self._save_building_histories()
                    return

            # 3. Move the persona and update memory state immediately
            # This now handles DB, memory (occupants), and logging in one go.
            success, reason = self.occupancy_manager.move_entity(persona_id, 'ai', self.user_room_id, destination_id, db_session=db)

            if not success:
                msg = f"{self.id_to_name_map.get(persona_id, persona_id)}を移動できませんでした: {reason}"
                self.building_histories.setdefault(self.user_room_id, []).append(
                    {"role": "host", "content": f'<div class="note-box">{msg}</div>'}
                )
                self._save_building_histories()
                db.rollback()
                return

            # 4. Restore interaction mode from PREVIOUS_INTERACTION_MODE
            previous_mode = ai_record.PREVIOUS_INTERACTION_MODE or "auto"
            ai_record.INTERACTION_MODE = previous_mode
            logging.info(f"Restoring INTERACTION_MODE to '{previous_mode}' for {persona_id}.")

            # 5. Update in-memory PersonaCore state
            persona = self.personas.get(persona_id)
            if persona:
                persona.current_building_id = destination_id
                persona.interaction_mode = previous_mode or "auto"
                logging.info(f"Updated {persona_id}'s internal location to {destination_id} and restored mode to '{previous_mode}'.")
                persona.register_entry(destination_id)

            # 5. Commit all DB changes at once
            db.commit()
            self._save_building_histories() # Save logs after successful commit

        except Exception as e:
            db.rollback()
            logging.error(f"Failed to end conversation for {persona_id}: {e}", exc_info=True)
        finally:
            db.close()

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
            except Exception as e:
                logging.error(f"Failed to restore DB default models: {e}", exc_info=True)
            finally:
                db.close()
            return

        logging.info(f"Temporarily setting model to '{model}' for all active personas.")
        self.model = model
        self.context_length = get_context_length(model)
        self.provider = get_model_provider(model)
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
                content, _, _ = tools.defs.parse_tool_result(result)
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
        if not event_message:
            return "Error: Event message cannot be empty."

        try:
            logging.info(f"Triggering world event for city '{self.city_name}': {event_message}")
            
            # Format the message for UI display
            formatted_message = f'<div class="note-box">🌐 World Event:<br><b>{event_message}</b></div>'
            
            # Add the message to the history of every building in the current city
            for building_id in self.building_map.keys():
                self.building_histories.setdefault(building_id, []).append({
                    "role": "host", # Using 'host' role for system-like events
                    "content": formatted_message
                })
            
            # Persist all changes to disk
            self._save_building_histories()
            
            logging.info("World event successfully broadcasted to all buildings.")
            return "World event triggered successfully."
        except Exception as e:
            logging.error(f"Failed to trigger world event: {e}", exc_info=True)
            return f"An internal error occurred: {e}"

    # --- World Editor Backend Methods ---

    def get_cities_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのCity一覧をDataFrameとして取得する"""
        db = self.SessionLocal()
        try:
            query = db.query(CityModel)
            df = pd.read_sql(query.statement, query.session.bind)
            # USERIDは現在固定なので表示しない
            return df[['CITYID', 'CITYNAME', 'DESCRIPTION', 'START_IN_ONLINE_MODE', 'UI_PORT', 'API_PORT']]
        finally:
            db.close()

    def update_city(self, city_id: int, name: str, description: str, online_mode: bool, ui_port: int, api_port: int) -> str:
        """ワールドエディタからCityの設定を更新する"""
        db = self.SessionLocal()
        try:
            city = db.query(CityModel).filter(CityModel.CITYID == city_id).first()
            if not city:
                return "Error: City not found."
            
            city.CITYNAME = name
            city.DESCRIPTION = description
            city.START_IN_ONLINE_MODE = online_mode
            city.UI_PORT = ui_port
            city.API_PORT = api_port
            db.commit()

            # If we are updating the current city, update some in-memory state
            if city.CITYID == self.city_id:
                self.start_in_online_mode = online_mode
                self.city_name = name
                self.ui_port = ui_port
                self.api_port = api_port
            
            logging.info(f"Updated city settings for City ID {city_id}. A restart may be required.")
            return "City settings updated successfully. A restart is required for changes to apply."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update city settings for ID {city_id}: {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    # --- World Editor: Backup/Restore Methods ---

    def get_backups(self) -> pd.DataFrame:
        """Gets a list of available world backups (.zip)."""
        backups = []
        for f in self.backup_dir.glob("*.zip"):
            try:
                stat = f.stat()
                backups.append({
                    "Backup Name": f.stem,
                    "Created At": datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    "Size (KB)": round(stat.st_size / 1024, 2)
                })
            except FileNotFoundError:
                continue # File might have been deleted between glob and stat
        if not backups:
            return pd.DataFrame(columns=["Backup Name", "Created At", "Size (KB)"])
        df = pd.DataFrame(backups)
        return df.sort_values(by="Created At", ascending=False)

    def backup_world(self, backup_name: str) -> str:
        """
        Creates a backup of the entire world state, including the database and all log files,
        into a single .zip archive.
        """
        if not backup_name or not backup_name.isalnum():
            return "Error: Backup name must be alphanumeric and not empty."

        backup_zip_path = self.backup_dir / f"{backup_name}.zip"
        if backup_zip_path.exists():
            return f"Error: A backup named '{backup_name}' already exists."

        # Define paths for backup targets
        db_file_path = Path(self.db_path)
        cities_log_path = self.saiverse_home / "cities"
        personas_log_path = self.saiverse_home / "personas"
        buildings_log_path = self.saiverse_home / "buildings"

        try:
            # Use a temporary directory to assemble the backup contents
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                
                # 1. Copy database file
                if db_file_path.exists():
                    shutil.copy(db_file_path, tmp_path / db_file_path.name)
                    logging.info(f"Added database to backup staging: {db_file_path}")

                # 2. Copy cities log directory
                if cities_log_path.exists() and cities_log_path.is_dir():
                    shutil.copytree(cities_log_path, tmp_path / "cities")
                    logging.info(f"Added cities logs to backup staging: {cities_log_path}")

                # 3. Copy personas log directory
                if personas_log_path.exists() and personas_log_path.is_dir():
                    shutil.copytree(personas_log_path, tmp_path / "personas")
                    logging.info(f"Added personas logs to backup staging: {personas_log_path}")

                # 4. Copy buildings log directory
                if buildings_log_path.exists() and buildings_log_path.is_dir():
                    shutil.copytree(buildings_log_path, tmp_path / "buildings")
                    logging.info(f"Added buildings logs to backup staging: {buildings_log_path}")

                # 5. Create the zip archive from the temporary directory
                shutil.make_archive(
                    base_name=self.backup_dir / backup_name,
                    format='zip',
                    root_dir=tmp_path
                )
            
            logging.info(f"World state successfully backed up to {backup_zip_path}")
            return f"Backup '{backup_name}' created successfully."
        except Exception as e:
            logging.error(f"Failed to create backup: {e}", exc_info=True)
            return f"Error: {e}"

    def restore_world(self, backup_name: str) -> str:
        """
        Restores the entire world state from a .zip archive.
        This operation is destructive and requires an application restart.
        """
        backup_zip_path = self.backup_dir / f"{backup_name}.zip"
        if not backup_zip_path.exists():
            return f"Error: Backup '{backup_name}' not found."

        # Define paths for restore targets
        db_file_path = Path(self.db_path)
        cities_log_path = self.saiverse_home / "cities"
        personas_log_path = self.saiverse_home / "personas"
        buildings_log_path = self.saiverse_home / "buildings"

        try:
            # --- 1. Safely remove existing data ---
            logging.warning("Starting world restore. Removing existing data...")
            if db_file_path.exists():
                db_file_path.unlink()
                logging.info(f"Removed existing database file: {db_file_path}")
            if cities_log_path.exists():
                shutil.rmtree(cities_log_path)
                logging.info(f"Removed existing cities log directory: {cities_log_path}")
            if personas_log_path.exists():
                shutil.rmtree(personas_log_path)
                logging.info(f"Removed existing personas log directory: {personas_log_path}")
            if buildings_log_path.exists():
                shutil.rmtree(buildings_log_path)
                logging.info(f"Removed existing buildings log directory: {buildings_log_path}")

            # --- 2. Unpack the backup to a temporary directory ---
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                logging.info(f"Unpacking backup '{backup_zip_path}' to temporary directory '{tmp_path}'")
                shutil.unpack_archive(backup_zip_path, tmp_path)

                # --- 3. Move restored files to their final destination ---
                unpacked_db = tmp_path / db_file_path.name
                if unpacked_db.exists():
                    shutil.move(str(unpacked_db), str(db_file_path))
                    logging.info(f"Restored database file to {db_file_path}")

                for log_dir_name in ["cities", "personas", "buildings"]:
                    unpacked_dir = tmp_path / log_dir_name
                    if unpacked_dir.exists() and unpacked_dir.is_dir():
                        shutil.move(str(unpacked_dir), str(self.saiverse_home / log_dir_name))
                        logging.info(f"Restored {log_dir_name} log directory.")

            logging.warning(f"World state has been restored from {backup_zip_path}. A RESTART IS REQUIRED.")
            return "Restore successful. Please RESTART the application to load the restored world."

        except Exception as e:
            logging.error(f"Failed to restore world: {e}", exc_info=True)
            # Attempt to clean up in case of partial failure, though it might not be perfect.
            return f"Error during restore: {e}. The world state may be inconsistent. It is recommended to restore another backup or re-seed the database."

    def delete_backup(self, backup_name: str) -> str:
        """Deletes a specific backup file (.zip)."""
        backup_path = self.backup_dir / f"{backup_name}.zip"
        if not backup_path.exists():
            return f"Error: Backup '{backup_name}' not found."
        try:
            os.remove(backup_path)
            logging.info(f"Deleted backup: {backup_path}")
            return f"Backup '{backup_name}' deleted successfully."
        except Exception as e:
            logging.error(f"Failed to delete backup: {e}", exc_info=True)
            return f"Error: {e}"

    def get_blueprint_details(self, blueprint_id: int) -> Optional[Dict]:
        """Get full details for a single blueprint for the edit form."""
        db = self.SessionLocal()
        try:
            blueprint = db.query(Blueprint).filter(Blueprint.BLUEPRINT_ID == blueprint_id).first()
            if not blueprint: return None
            return {
                "BLUEPRINT_ID": blueprint.BLUEPRINT_ID,
                "NAME": blueprint.NAME,
                "DESCRIPTION": blueprint.DESCRIPTION,
                "CITYID": blueprint.CITYID,
                "BASE_SYSTEM_PROMPT": blueprint.BASE_SYSTEM_PROMPT,
                "ENTITY_TYPE": blueprint.ENTITY_TYPE,
            }
        finally:
            db.close()

    def get_blueprints_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのBlueprint一覧をDataFrameとして取得する"""
        db = self.SessionLocal()
        try:
            query = db.query(Blueprint)
            df = pd.read_sql(query.statement, query.session.bind)
            return df[['BLUEPRINT_ID', 'NAME', 'DESCRIPTION', 'ENTITY_TYPE', 'CITYID']]
        finally:
            db.close()

    def create_blueprint(self, name: str, description: str, city_id: int, system_prompt: str, entity_type: str) -> str:
        """ワールドエディタから新しいBlueprintを作成する"""
        db = self.SessionLocal()
        try:
            # Check for name conflicts within the same city
            existing = db.query(Blueprint).filter_by(CITYID=city_id, NAME=name).first()
            if existing:
                return f"Error: A blueprint named '{name}' already exists in this city."

            new_blueprint = Blueprint(
                CITYID=city_id,
                NAME=name,
                DESCRIPTION=description,
                BASE_SYSTEM_PROMPT=system_prompt,
                ENTITY_TYPE=entity_type
            )
            db.add(new_blueprint)
            db.commit()
            logging.info(f"Created new blueprint '{name}' in City ID {city_id}.")
            return f"Blueprint '{name}' created successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to create blueprint '{name}': {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    def update_blueprint(self, blueprint_id: int, name: str, description: str, city_id: int, system_prompt: str, entity_type: str) -> str:
        """ワールドエディタからBlueprintの設定を更新する"""
        db = self.SessionLocal()
        try:
            blueprint = db.query(Blueprint).filter_by(BLUEPRINT_ID=blueprint_id).first()
            if not blueprint:
                return "Error: Blueprint not found."
            if not city_id:
                return "Error: City must be selected."

            # Check for name conflicts if the name is being changed
            if blueprint.NAME != name or blueprint.CITYID != city_id:
                existing = db.query(Blueprint).filter_by(CITYID=city_id, NAME=name).first()
                if existing:
                    # Find the city name for the error message
                    target_city = db.query(CityModel).filter_by(CITYID=city_id).first()
                    city_name_for_error = target_city.CITYNAME if target_city else f"ID {city_id}"
                    return f"Error: A blueprint named '{name}' already exists in city '{city_name_for_error}'."

            blueprint.NAME = name
            blueprint.DESCRIPTION = description
            blueprint.CITYID = city_id
            blueprint.BASE_SYSTEM_PROMPT = system_prompt
            blueprint.ENTITY_TYPE = entity_type
            db.commit()
            logging.info(f"Updated blueprint '{name}' (ID: {blueprint_id}).")
            return f"Blueprint '{name}' updated successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update blueprint ID {blueprint_id}: {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    def delete_blueprint(self, blueprint_id: int) -> str:
        """ワールドエディタからBlueprintを削除する"""
        db = self.SessionLocal()
        try:
            blueprint = db.query(Blueprint).filter_by(BLUEPRINT_ID=blueprint_id).first()
            if not blueprint:
                return "Error: Blueprint not found."

            db.delete(blueprint)
            db.commit()
            logging.info(f"Deleted blueprint ID {blueprint_id}.")
            return f"Blueprint deleted successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to delete blueprint ID {blueprint_id}: {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    def spawn_entity_from_blueprint(self, blueprint_id: int, entity_name: str, target_building_id: str) -> Tuple[bool, str]:
        """ブループリントから新しいエンティティを生成し、指定された建物に配置する"""
        db = self.SessionLocal()
        try:
            blueprint = db.query(Blueprint).filter_by(BLUEPRINT_ID=blueprint_id).first()
            if not blueprint: return False, "Blueprint not found."
            if target_building_id not in self.building_map: return False, f"Target building '{target_building_id}' not found."
            if len(self.occupants.get(target_building_id, [])) >= self.capacities.get(target_building_id, 1): return False, f"Target building '{self.building_map[target_building_id].name}' is at full capacity."
            if db.query(AIModel).filter(func.lower(AIModel.AINAME) == func.lower(entity_name)).first(): return False, f"An entity named '{entity_name}' already exists."

            home_city = db.query(CityModel).filter_by(CITYID=blueprint.CITYID).first()
            new_ai_id = f"{entity_name.lower().replace(' ', '_')}_{home_city.CITYNAME}"
            if db.query(AIModel).filter_by(AIID=new_ai_id).first(): return False, f"An entity with the generated ID '{new_ai_id}' already exists."

            # --- Create private room for the new AI ---
            private_room_id = f"{new_ai_id}_room"
            private_room_model = BuildingModel(
                CITYID=blueprint.CITYID, BUILDINGID=private_room_id, BUILDINGNAME=f"{entity_name}の部屋",
                CAPACITY=1, SYSTEM_INSTRUCTION=f"{entity_name}が待機する個室です。",
                DESCRIPTION=f"{entity_name}のプライベートルーム。"
            )
            db.add(private_room_model)
            logging.info(f"DB: Added new private room '{private_room_model.BUILDINGNAME}' ({private_room_id}) for spawned AI.")

            # --- Create AI record and link to private room ---
            new_ai_model = AIModel(
                AIID=new_ai_id, HOME_CITYID=blueprint.CITYID, AINAME=entity_name,
                SYSTEMPROMPT=blueprint.BASE_SYSTEM_PROMPT, DESCRIPTION=blueprint.DESCRIPTION,
                AVATAR_IMAGE=blueprint.BASE_AVATAR, DEFAULT_MODEL=self.model,
                PRIVATE_ROOM_ID=private_room_id
            )
            db.add(new_ai_model)
            
            target_building_db = db.query(BuildingModel).filter_by(BUILDINGID=target_building_id).first()
            new_occupancy_log = BuildingOccupancyLog(CITYID=target_building_db.CITYID, AIID=new_ai_id, BUILDINGID=target_building_id, ENTRY_TIMESTAMP=datetime.now())
            db.add(new_occupancy_log)

            # --- Update memory for the new private room (if it's in the current city) ---
            if blueprint.CITYID == self.city_id:
                new_building_obj = Building(
                    building_id=private_room_model.BUILDINGID, name=private_room_model.BUILDINGNAME,
                    capacity=private_room_model.CAPACITY, system_instruction=private_room_model.SYSTEM_INSTRUCTION,
                    description=private_room_model.DESCRIPTION
                )
                self.buildings.append(new_building_obj)
                self.building_map[private_room_id] = new_building_obj
                self.capacities[private_room_id] = new_building_obj.capacity
                self.occupants[private_room_id] = [] # Starts empty
                self.building_memory_paths[private_room_id] = self.saiverse_home / "cities" / self.city_name / "buildings" / private_room_id / "log.json"
                self.building_histories[private_room_id] = []

            # --- Update memory for the new AI ---
            if blueprint.CITYID == self.city_id:
                new_persona_core = PersonaCore(city_name=self.city_name, persona_id=new_ai_id, persona_name=entity_name, persona_system_instruction=blueprint.BASE_SYSTEM_PROMPT, avatar_image=blueprint.BASE_AVATAR, buildings=self.buildings, common_prompt_path=Path("system_prompts/common.txt"), action_priority_path=Path("action_priority.json"), building_histories=self.building_histories, occupants=self.occupants, id_to_name_map=self.id_to_name_map, move_callback=self._move_persona, dispatch_callback=self.dispatch_persona, explore_callback=self._explore_city, create_persona_callback=self._create_persona, session_factory=self.SessionLocal, start_building_id=target_building_id, model=self.model, context_length=self.context_length, user_room_id=self.user_room_id, provider=self.provider, is_dispatched=False)
                self.personas[new_ai_id] = new_persona_core
                self.avatar_map[new_ai_id] = self.default_avatar
                self.id_to_name_map[new_ai_id] = entity_name
                self.persona_map[entity_name] = new_ai_id
            
            self.occupants.setdefault(target_building_id, []).append(new_ai_id)
            arrival_message = f'<div class="note-box">✨ Blueprint Spawn:<br><b>{entity_name}がこの世界に現れました</b></div>'
            self.building_histories.setdefault(target_building_id, []).append({"role": "host", "content": arrival_message})
            self._save_building_histories()

            db.commit()
            return True, f"Entity '{entity_name}' spawned successfully in '{self.building_map[target_building_id].name}'."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to spawn entity from blueprint: {e}", exc_info=True)
            return False, f"An internal error occurred: {e}"
        finally:
            db.close()

    def get_tools_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのTool一覧をDataFrameとして取得する"""
        db = self.SessionLocal()
        try:
            query = db.query(ToolModel)
            df = pd.read_sql(query.statement, query.session.bind)
            return df
        finally:
            db.close()

    def get_tool_details(self, tool_id: int) -> Optional[Dict]:
        """Get full details for a single tool for the edit form."""
        db = self.SessionLocal()
        try:
            tool = db.query(ToolModel).filter(ToolModel.TOOLID == tool_id).first()
            if not tool: return None
            return {
                "TOOLID": tool.TOOLID,
                "TOOLNAME": tool.TOOLNAME,
                "DESCRIPTION": tool.DESCRIPTION,
                "MODULE_PATH": tool.MODULE_PATH,
                "FUNCTION_NAME": tool.FUNCTION_NAME,
            }
        finally:
            db.close()

    def create_tool(self, name: str, description: str, module_path: str, function_name: str) -> str:
        """ワールドエディタから新しいToolを作成する"""
        db = self.SessionLocal()
        try:
            if db.query(ToolModel).filter_by(TOOLNAME=name).first():
                return f"Error: A tool named '{name}' already exists."
            if db.query(ToolModel).filter_by(MODULE_PATH=module_path, FUNCTION_NAME=function_name).first():
                return f"Error: A tool with the same module and function name already exists."

            new_tool = ToolModel(
                TOOLNAME=name,
                DESCRIPTION=description,
                MODULE_PATH=module_path,
                FUNCTION_NAME=function_name
            )
            db.add(new_tool)
            db.commit()
            logging.info(f"Created new tool '{name}'.")
            return f"Tool '{name}' created successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to create tool '{name}': {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    def update_tool(self, tool_id: int, name: str, description: str, module_path: str, function_name: str) -> str:
        """ワールドエディタからToolの設定を更新する"""
        db = self.SessionLocal()
        try:
            tool = db.query(ToolModel).filter_by(TOOLID=tool_id).first()
            if not tool: return "Error: Tool not found."

            tool.TOOLNAME = name
            tool.DESCRIPTION = description
            tool.MODULE_PATH = module_path
            tool.FUNCTION_NAME = function_name
            db.commit()
            logging.info(f"Updated tool '{name}' (ID: {tool_id}).")
            return f"Tool '{name}' updated successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update tool ID {tool_id}: {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    def delete_tool(self, tool_id: int) -> str:
        """ワールドエディタからToolを削除する"""
        db = self.SessionLocal()
        try:
            tool = db.query(ToolModel).filter_by(TOOLID=tool_id).first()
            if not tool: return "Error: Tool not found."
            if db.query(BuildingToolLink).filter_by(TOOLID=tool_id).first():
                return f"Error: Cannot delete tool '{tool.TOOLNAME}' because it is linked to one or more buildings."
            db.delete(tool)
            db.commit()
            logging.info(f"Deleted tool ID {tool_id}.")
            return f"Tool '{tool.TOOLNAME}' deleted successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to delete tool ID {tool_id}: {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    def get_linked_tool_ids(self, building_id: str) -> List[int]:
        """Gets a list of tool IDs linked to a specific building."""
        if not building_id: return []
        db = self.SessionLocal()
        try:
            links = db.query(BuildingToolLink.TOOLID).filter_by(BUILDINGID=building_id).all()
            # links will be a list of tuples, e.g., [(1,), (2,)]
            return [link[0] for link in links]
        finally:
            db.close()

    # --- World Editor: Create/Delete Methods ---

    def _is_seeded_entity(self, entity_id: str) -> bool:
        """Checks if an entity was created by seed.py based on its ID."""
        if not isinstance(entity_id, str):
            return False
        
        # List of base names/prefixes for seeded entities (AIs and special buildings)
        seeded_prefixes = [
            "air_", "eris_", "genesis_", # AI names from city_a
            "luna_", "sol_",             # AI names from city_b
            "user_room_", "deep_think_room_", "altar_of_creation_" # Special building names
        ]
        
        # Check if the entity ID starts with one of the seeded prefixes.
        # This covers AIs (e.g., "air_city_a") and their rooms (e.g., "air_city_a_room"),
        # as well as special buildings (e.g., "user_room_city_a").
        return any(entity_id.startswith(prefix) for prefix in seeded_prefixes)

    def create_city(self, name: str, description: str, ui_port: int, api_port: int) -> str:
        """Creates a new city."""
        db = self.SessionLocal()
        try:
            if db.query(CityModel).filter_by(CITYNAME=name).first():
                return f"Error: A city named '{name}' already exists."
            if db.query(CityModel).filter((CityModel.UI_PORT == ui_port) | (CityModel.API_PORT == api_port)).first():
                return f"Error: UI Port {ui_port} or API Port {api_port} is already in use."

            new_city = CityModel(USERID=self.user_id, CITYNAME=name, DESCRIPTION=description, UI_PORT=ui_port, API_PORT=api_port)
            db.add(new_city)
            db.commit()
            logging.info(f"Created new city '{name}'.")
            return f"City '{name}' created successfully. Please restart the application to use it."
        except Exception as e:
            db.rollback()
            return f"Error: {e}"
        finally:
            db.close()

    def delete_city(self, city_id: int) -> str:
        """Deletes a city after checking dependencies."""
        db = self.SessionLocal()
        try:
            city = db.query(CityModel).filter_by(CITYID=city_id).first()
            if not city: return "Error: City not found."
            if city.CITYNAME in ["city_a", "city_b"]:
                return "Error: Seeded cities (city_a, city_b) cannot be deleted."
            if city.CITYID == self.city_id: return "Error: Cannot delete the currently running city."

            if db.query(BuildingModel).filter_by(CITYID=city_id).first():
                return f"Error: Cannot delete city '{city.CITYNAME}' because it still contains buildings."
            
            # Although buildings are gone, double-check for stray occupancy logs (should not happen if building deletion is clean)
            if db.query(BuildingOccupancyLog).filter_by(CITYID=city_id).first():
                 return f"Error: Cannot delete city '{city.CITYNAME}' due to remaining occupancy logs. Please clean up buildings first."

            db.delete(city)
            db.commit()
            logging.info(f"Deleted city '{city.CITYNAME}'.")
            return f"City '{city.CITYNAME}' deleted successfully."
        except Exception as e:
            db.rollback()
            return f"Error: {e}"
        finally:
            db.close()

    def create_building(self, name: str, description: str, capacity: int, system_instruction: str, city_id: int) -> str:
        """Creates a new building in a specified city."""
        db = self.SessionLocal()
        try:
            if not db.query(CityModel).filter_by(CITYID=city_id).first():
                return "Error: Target city not found."
            if db.query(BuildingModel).filter_by(CITYID=city_id, BUILDINGNAME=name).first():
                return f"Error: A building named '{name}' already exists in that city."

            building_id = f"{name.lower().replace(' ', '_')}_{db.query(CityModel).filter_by(CITYID=city_id).first().CITYNAME}"
            if db.query(BuildingModel).filter_by(BUILDINGID=building_id).first():
                return f"Error: A building with the generated ID '{building_id}' already exists."

            new_building = BuildingModel(
                CITYID=city_id, BUILDINGID=building_id, BUILDINGNAME=name,
                DESCRIPTION=description, CAPACITY=capacity, SYSTEM_INSTRUCTION=system_instruction
            )
            db.add(new_building)
            db.commit()
            logging.info(f"Created new building '{name}' in city {city_id}.")
            return f"Building '{name}' created successfully. A restart is required for it to be usable."
        except Exception as e:
            db.rollback()
            return f"Error: {e}"
        finally:
            db.close()

    def delete_building(self, building_id: str) -> str:
        """Deletes a building after checking for occupants."""
        if self._is_seeded_entity(building_id):
            return "Error: Seeded buildings cannot be deleted."
        db = self.SessionLocal()
        try:
            building = db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
            if not building: return "Error: Building not found."

            occupancy = db.query(BuildingOccupancyLog).filter_by(BUILDINGID=building_id, EXIT_TIMESTAMP=None).first()
            if occupancy:
                return f"Error: Cannot delete '{building.BUILDINGNAME}' because it is occupied."

            # Delete associated logs before deleting the building
            db.query(BuildingOccupancyLog).filter_by(BUILDINGID=building_id).delete()
            db.delete(building)
            db.commit()
            logging.info(f"Deleted building '{building.BUILDINGNAME}'.")
            return f"Building '{building.BUILDINGNAME}' deleted successfully. A restart is required for changes to apply."
        except Exception as e:
            db.rollback()
            return f"Error: {e}"
        finally:
            db.close()

    def create_ai(self, name: str, system_prompt: str, home_city_id: int) -> str:
        """Creates a new AI and their private room, similar to _create_persona."""
        success, message = self._create_persona(name, system_prompt)
        if success:
            return f"AI '{name}' and their room created successfully. A restart is required for the AI to become active."
        else:
            return f"Error: {message}"

    def delete_ai(self, ai_id: str) -> str:
        """Deletes an AI after checking its state."""
        if self._is_seeded_entity(ai_id):
            return "Error: Seeded AIs cannot be deleted."
        
        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter_by(AIID=ai_id).first()
            if not ai: return "Error: AI not found."
            if ai.IS_DISPATCHED: return f"Error: Cannot delete a dispatched AI. Please return '{ai.AINAME}' to their home city first."

            # Update occupancy logs to mark exit, preserving history
            db.query(BuildingOccupancyLog).filter(
                BuildingOccupancyLog.AIID == ai_id,
                BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None)
            ).update({"EXIT_TIMESTAMP": datetime.now()})

            # Delete the AI record
            db.delete(ai)
            db.commit()

            # Remove from memory if it's a local persona
            if ai_id in self.personas:
                persona_name = self.personas[ai_id].persona_name
                del self.personas[ai_id]
                if persona_name in self.persona_map:
                    del self.persona_map[persona_name]
                logging.info(f"Removed local persona instance '{persona_name}' from memory.")
            
            if ai_id in self.id_to_name_map: del self.id_to_name_map[ai_id]
            if ai_id in self.avatar_map: del self.avatar_map[ai_id]
            for building_id in self.occupants:
                if ai_id in self.occupants[building_id]:
                    self.occupants[building_id].remove(ai_id)

            logging.info(f"Deleted AI '{ai.AINAME}' ({ai_id}).")
            return f"AI '{ai.AINAME}' deleted successfully."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to delete AI '{ai_id}': {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()
            
    def move_ai_from_editor(self, ai_id: str, target_building_id: str) -> str:
        """
        Moves an AI to a specified building, triggered from the World Editor.
        This is a direct administrative action.
        This method is refactored to use summon_persona and end_conversation
        to ensure interaction mode consistency.
        """
        if not ai_id or not target_building_id:
            return "Error: AI ID and Target Building ID are required."

        persona = self.personas.get(ai_id)
        if not persona:
            if ai_id in self.visiting_personas:
                 return "Error: Cannot manage the interaction mode of a visiting persona from the editor."
            return f"Error: Persona with ID '{ai_id}' not found in memory."

        if target_building_id not in self.building_map:
            return f"Error: Target building '{target_building_id}' not found."

        from_building_id = persona.current_building_id
        if from_building_id == target_building_id:
            return f"{persona.persona_name} is already in that building."

        # Block moving a persona who is currently in the user's room.
        if from_building_id == self.user_room_id:
            return "Can't move, because this persona in user room. Please execute end conversation."

        # Case 1: Moving TO the user's room. This is equivalent to "summoning".
        if target_building_id == self.user_room_id:
            logging.info(f"[EditorMove] Summoning '{persona.persona_name}' to user room.")
            success, reason = self.summon_persona(ai_id)
            if success:
                return f"Successfully summoned '{persona.persona_name}' to your room."
            else:
                return f"Failed to summon '{persona.persona_name}': {reason}"

        # Case 2: Moving between two non-user rooms. This is a simple move with no mode change.
        else:
            logging.info(f"[EditorMove] Moving '{persona.persona_name}' from '{self.building_map.get(from_building_id, 'Unknown').name}' to '{self.building_map.get(target_building_id, 'Unknown').name}'.")
            success, reason = self._move_persona(ai_id, from_building_id, target_building_id)
            if success:
                persona.current_building_id = target_building_id
                persona.register_entry(target_building_id)
                return f"Successfully moved '{persona.persona_name}' to '{self.building_map[target_building_id].name}'."
            else:
                return f"Failed to move: {reason}"

    def get_buildings_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのBuilding一覧をDataFrameとして取得する"""
        db = self.SessionLocal()
        try:
            query = db.query(BuildingModel)
            df = pd.read_sql(query.statement, query.session.bind)
            return df[['BUILDINGID', 'BUILDINGNAME', 'CAPACITY', 'DESCRIPTION', 'SYSTEM_INSTRUCTION', 'CITYID', 'AUTO_INTERVAL_SEC']]
        finally:
            db.close()

    def update_building(self, building_id: str, name: str, capacity: int, description: str, system_instruction: str, city_id: int, tool_ids: List[int], interval: int) -> str:
        """ワールドエディタからBuildingの設定を更新する"""
        db = self.SessionLocal()
        try:
            building = db.query(BuildingModel).filter(BuildingModel.BUILDINGID == building_id).first()
            if not building:
                return f"Error: Building with ID '{building_id}' not found."

            # Check if any AI is in the building before changing city
            if building.CITYID != city_id:
                occupancy_log = db.query(BuildingOccupancyLog).filter(
                    BuildingOccupancyLog.BUILDINGID == building_id,
                    BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None)
                ).first()
                if occupancy_log:
                    return f"Error: Cannot change the city of a building while it is occupied. Please move all AIs out of '{building.BUILDINGNAME}' first."

            building.BUILDINGNAME = name
            building.CAPACITY = capacity
            building.DESCRIPTION = description
            building.SYSTEM_INSTRUCTION = system_instruction
            building.AUTO_INTERVAL_SEC = interval
            building.CITYID = city_id

            # --- Update Tool Links ---
            # 1. Delete existing links for this building
            db.query(BuildingToolLink).filter_by(BUILDINGID=building_id).delete(synchronize_session=False)
            
            # 2. Add new links
            if tool_ids:
                for tool_id in tool_ids:
                    new_link = BuildingToolLink(BUILDINGID=building_id, TOOLID=int(tool_id))
                    db.add(new_link)

            db.commit()
            
            logging.info(f"Updated building '{name}' ({building_id}) and its tool links. A restart is required for changes to apply.")
            return f"Building '{name}' and its tool links updated successfully. A restart is required for the changes to take full effect."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update building '{building_id}': {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()

    def get_ais_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのAI一覧をDataFrameとして取得する"""
        db = self.SessionLocal()
        try:
            query = db.query(AIModel)
            df = pd.read_sql(query.statement, query.session.bind)
            # Don't show the full system prompt in the main table
            df['SYSTEMPROMPT_SNIPPET'] = df['SYSTEMPROMPT'].str.slice(0, 40) + '...'
            return df[['AIID', 'AINAME', 'HOME_CITYID', 'DEFAULT_MODEL', 'IS_DISPATCHED', 'DESCRIPTION', 'SYSTEMPROMPT_SNIPPET']]
        finally:
            db.close()

    def get_ai_details(self, ai_id: str) -> Optional[Dict]:
        """Get full details for a single AI for the edit form."""
        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter(AIModel.AIID == ai_id).first()
            if not ai: return None
            return {
                "AIID": ai.AIID, "AINAME": ai.AINAME, "HOME_CITYID": ai.HOME_CITYID,
                "SYSTEMPROMPT": ai.SYSTEMPROMPT, "DESCRIPTION": ai.DESCRIPTION,
                "AVATAR_IMAGE": ai.AVATAR_IMAGE, "IS_DISPATCHED": ai.IS_DISPATCHED,
                "DEFAULT_MODEL": ai.DEFAULT_MODEL,
                "INTERACTION_MODE": ai.INTERACTION_MODE
            }
        finally:
            db.close()

    def update_ai(
        self, ai_id: str, name: str, description: str, system_prompt: str,
        home_city_id: int, default_model: Optional[str], interaction_mode: str
    ) -> str:
        """ワールドエディタからAIの設定を更新する"""
        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter(AIModel.AIID == ai_id).first()
            if not ai: return f"Error: AI with ID '{ai_id}' not found."

            if ai.HOME_CITYID != home_city_id:
                if ai.IS_DISPATCHED: return f"Error: Cannot change the home city of a dispatched AI. Please return '{ai.AINAME}' to their home city first."

            # --- Interaction Mode Change Logic ---
            original_mode = ai.INTERACTION_MODE
            mode_changed = original_mode != interaction_mode
            move_feedback = ""

            if mode_changed:
                if interaction_mode == "sleep":
                    ai.INTERACTION_MODE = "sleep"
                    logging.info(f"AI '{name}' mode changed to 'sleep'. Attempting to move to private room.")
                    
                    private_room_id = ai.PRIVATE_ROOM_ID
                    if not private_room_id or private_room_id not in self.building_map:
                        move_feedback = f" Note: Could not move to private room because it is not configured or invalid."
                        logging.warning(f"Cannot move AI '{name}' to sleep. Private room ID '{private_room_id}' is not configured or invalid.")
                    else:
                        current_building_id = self.personas[ai_id].current_building_id
                        if current_building_id != private_room_id:
                            success, reason = self._move_persona(ai_id, current_building_id, private_room_id, db_session=db)
                            if success:
                                self.personas[ai_id].current_building_id = private_room_id
                                move_feedback = f" Moved to private room '{self.building_map[private_room_id].name}'."
                                logging.info(f"Successfully moved AI '{name}' to their private room '{private_room_id}'.")
                            else:
                                move_feedback = f" Note: Failed to move to private room: {reason}."
                                logging.error(f"Failed to move AI '{name}' to private room: {reason}")
                elif interaction_mode in ["auto", "manual"]:
                    ai.INTERACTION_MODE = interaction_mode
                else:
                    logging.warning(f"Invalid interaction mode '{interaction_mode}' requested for AI '{name}'. No change made.")

            # --- Update other fields ---
            ai.AINAME = name; ai.DESCRIPTION = description; ai.SYSTEMPROMPT = system_prompt; ai.HOME_CITYID = home_city_id
            ai.DEFAULT_MODEL = default_model if default_model else None
            db.commit()

            # --- Update in-memory state ---
            if ai_id in self.personas:
                persona = self.personas[ai_id]
                persona.persona_name = name; persona.persona_system_instruction = system_prompt
                persona.interaction_mode = ai.INTERACTION_MODE
                logging.info(f"Updated in-memory persona '{name}' with new settings.")
            
            status_message = f"AI '{name}' updated successfully."
            if mode_changed: status_message += f" Mode changed from '{original_mode}' to '{interaction_mode}'."
            return status_message + move_feedback
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update AI '{ai_id}': {e}", exc_info=True)
            return f"Error: {e}"
        finally:
            db.close()
