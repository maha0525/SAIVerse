import base64
import json
from sqlalchemy import create_engine, inspect
import threading
import requests
import logging
from pathlib import Path
import mimetypes
from typing import Dict, List, Optional, Tuple, Iterator, Union
from datetime import datetime, timedelta

from google.genai import errors
from buildings import Building
from persona_core import PersonaCore
from model_configs import get_model_provider, get_context_length
from conversation_manager import ConversationManager
from sqlalchemy.orm import sessionmaker
from remote_persona_proxy import RemotePersonaProxy
from database.models import Base, AI as AIModel, Building as BuildingModel, BuildingOccupancyLog, User as UserModel, City as CityModel, VisitingAI, ThinkingRequest


#DEFAULT_MODEL = "gpt-4o"
DEFAULT_MODEL = "gemini-2.0-flash"


class SAIVerseManager:
    """Manage multiple personas and building occupancy."""

    def __init__(
        self,
        city_name: str,
        db_path: str,
        model: str = DEFAULT_MODEL,
        sds_url: str = "http://127.0.0.1:8080",
    ):
        # --- Step 0: Database and Configuration Setup ---
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
        self.user_is_online: bool = False

        # --- Step 5: Load Dynamic States from DB ---
        # データベースから動的な状態（ペルソナ、ユーザー状態、入室状況）を読み込み、
        # メモリ上のオブジェクトに反映させます。
        self._load_personas_from_db()
        self._load_user_status_from_db()
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
                manager = ConversationManager(building_id=b_id, saiverse_manager=self)
                self.conversation_managers[b_id] = manager
        logging.info(f"Initialized {len(self.conversation_managers)} conversation managers.")

        # --- Step 7: Register with SDS and start background tasks ---
        self.sds_url = sds_url
        self.sds_session = requests.Session()
        self.sds_status = "Offline (Connecting...)"

        self._load_cities_from_db() # Load local config as a fallback first
        self._register_with_sds()
        self._update_cities_from_sds()
        
        # Start background thread for SDS communication
        self.sds_stop_event = threading.Event()
        self.sds_thread = threading.Thread(target=self._sds_background_loop, daemon=True)
        self.sds_thread.start()

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
        if self.sds_thread.is_alive():
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
                        persona.current_building_id
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
                    description=db_b.DESCRIPTION or "" # 探索結果で説明を表示するために追加
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
                    session_factory=self.SessionLocal,
                    start_building_id=start_id,
                    model=persona_model,
                    context_length=persona_context_length,
                    user_room_id=self.user_room_id,
                    provider=self.provider,
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
            persona.current_building_id
        )
        self._save_building_histories()

    def _load_user_status_from_db(self):
        """DBからユーザーのログイン状態を読み込む (現在はUSERID=1固定)"""
        db = self.SessionLocal()
        try:
            # USERID=1のユーザーを想定
            user = db.query(UserModel).filter(UserModel.USERID == 1).first()
            if user:
                self.user_is_online = user.LOGGED_IN
                logging.info(f"Loaded user login status: {'Online' if self.user_is_online else 'Offline'}")
            else:
                logging.warning("User with USERID=1 not found. Defaulting to Offline.")
                self.user_is_online = False
        except Exception as e:
            logging.error(f"Failed to load user status from DB: {e}", exc_info=True)
            self.user_is_online = False
        finally:
            db.close()

    def set_user_login_status(self, user_id: int, status: bool) -> str:
        """ユーザーのログイン状態を更新する"""
        db = self.SessionLocal()
        try:
            user = db.query(UserModel).filter(UserModel.USERID == user_id).first()
            if user:
                user.LOGGED_IN = status
                db.commit()
                self.user_is_online = status
                status_text = "オンライン" if status else "オフライン"
                logging.info(f"User {user_id} login status set to: {status_text}")
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

    def _move_persona(self, persona_id: str, from_id: str, to_id: str, db_session=None) -> Tuple[bool, Optional[str]]:
        if len(self.occupants.get(to_id, [])) >= self.capacities.get(to_id, 1):
            return False, f"{self.building_map[to_id].name}は定員オーバーです"
        
        # セッションが渡されなかった場合は、新しいセッションを作成
        db = db_session if db_session else self.SessionLocal()
        
        # この関数内でセッションを生成した場合にのみ、最後に閉じるフラグ
        manage_session_locally = not db_session

        try:
            now = datetime.now()
            # 1. 退室記録を更新
            last_log = db.query(BuildingOccupancyLog).filter(
                BuildingOccupancyLog.AIID == persona_id,
                BuildingOccupancyLog.BUILDINGID == from_id,
                BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None)
            ).order_by(BuildingOccupancyLog.ENTRY_TIMESTAMP.desc()).first()

            if last_log:
                last_log.EXIT_TIMESTAMP = now
                db.merge(last_log)
            else:
                logging.warning(f"Could not find an open session for {persona_id} in {from_id} to close.")

            # 2. 入室記録を作成
            new_log = BuildingOccupancyLog(
                CITYID=self.city_id,
                AIID=persona_id,
                BUILDINGID=to_id,
                ENTRY_TIMESTAMP=now
            )
            db.add(new_log)
            
            # ローカルでセッションを管理している場合のみコミット
            if manage_session_locally:
                db.commit()

            # 3. メモリ上の状態を更新
            if persona_id in self.occupants.get(from_id, []):
                self.occupants[from_id].remove(persona_id)
            self.occupants.setdefault(to_id, []).append(persona_id)
            
            logging.info(f"Moved {persona_id} from {from_id} to {to_id} and updated DB.")
            return True, None

        except Exception as e:
            # ローカルでセッションを管理している場合のみロールバック
            if manage_session_locally:
                db.rollback()
            logging.error(f"Failed to move persona {persona_id} in DB: {e}", exc_info=True)
            return False, "データベースの更新中にエラーが発生しました。"
        finally:
            # ローカルでセッションを管理している場合のみクローズ
            if manage_session_locally:
                db.close()

    def dispatch_persona(self, persona_id: str, target_city_id: str, target_building_id: str) -> Tuple[bool, str]:
        """
        Dispatches a persona to another city.
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
                persona.current_building_id
            )
            return True, "移動要求を送信しました。"
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to create dispatch request for {persona.persona_name}: {e}", exc_info=True)
            return False, "移動要求の作成中にデータベースエラーが発生しました。"
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
        # Stop the SDS background thread
        self.sds_stop_event.set()
        if self.sds_thread.is_alive():
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
        replies: List[str] = []
        for pid in list(self.occupants.get(self.user_room_id, [])):
            replies.extend(self.personas[pid].handle_user_input(message))
        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies

    def handle_user_input_stream(self, message: str) -> Iterator[str]:
        for pid in list(self.occupants.get(self.user_room_id, [])):
            for token in self.personas[pid].handle_user_input_stream(message):
                yield token
        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()

    def summon_persona(self, persona_id: str) -> List[str]:
        if persona_id not in self.personas:
            return []
        
        # --- DBを更新してペルソナの対話モードを'user'に設定 ---
        db = self.SessionLocal()
        try:
            db.query(AIModel).filter(AIModel.AIID == persona_id).update({"INTERACTION_MODE": "user"})
            db.commit()
            logging.info(f"Set INTERACTION_MODE to 'user' for {persona_id}.")
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update INTERACTION_MODE for {persona_id}: {e}", exc_info=True)
            # エラーが発生した場合、ユーザーに通知して処理を中断
            msg = f"{self.id_to_name_map.get(persona_id, persona_id)}を呼び出す際にデータベースエラーが発生しました。"
            self.building_histories["user_room"].append(
                {"role": "host", "content": f"<div class=\"note-box\">{msg}</div>"}
            )
            self._save_building_histories()
            return []
        finally:
            db.close()

        if (
            len(self.occupants.get(self.user_room_id, [])) >= self.capacities.get(self.user_room_id, 1)
            and persona_id not in self.occupants.get(self.user_room_id, [])
        ):
            msg = f"移動できませんでした。{self.building_map[self.user_room_id].name}は定員オーバーです"
            self.building_histories[self.user_room_id].append(
                {"role": "assistant", "content": f"<div class=\"note-box\">{msg}</div>"}
            )
            self._save_building_histories()
            return []
        replies = self.personas[persona_id].summon_to_user_room()
        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies

    def end_conversation(self, persona_id: str) -> None:
        """Release a persona from user_room and return it to its previous building."""
        if persona_id not in self.personas:
            logging.error(f"Attempted to end conversation with non-existent persona: {persona_id}")
            return

        db = self.SessionLocal()
        try:
            # 1. Find the previous building for the persona
            # Get the last two entries to find the previous location
            logs = db.query(BuildingOccupancyLog).filter(
                BuildingOccupancyLog.AIID == persona_id
            ).order_by(BuildingOccupancyLog.ENTRY_TIMESTAMP.desc()).limit(2).all()

            # The most recent log should be for user_room.
            if not logs or logs[0].BUILDINGID != self.user_room_id:
                logging.warning(f"Could not determine previous location for {persona_id}. Sending to home room.")
                destination_id = f"{persona_id}_room"
            elif len(logs) < 2:
                # Only one log entry (user_room_id), so no "previous" location. Fallback to home room.
                logging.info(f"{persona_id} has no previous location. Sending to home room.")
                destination_id = f"{persona_id}_room"
            else:
                # The second-to-last entry is the previous location.
                destination_id = logs[1].BUILDINGID

            # Ensure destination is valid
            if destination_id not in self.building_map:
                logging.error(f"Invalid destination building '{destination_id}' found for {persona_id}. Falling back to home room.")
                destination_id = f"{persona_id}_room"
                if destination_id not in self.building_map:
                    # This is a critical failure, the persona has no valid place to go.
                    logging.error(f"Home room '{destination_id}' not found. Cannot move persona.")
                    msg = f"{self.id_to_name_map.get(persona_id, persona_id)}の帰る場所が見つかりませんでした。"
                    self.building_histories[self.user_room_id].append(
                        {"role": "host", "content": f"<div class=\"note-box\">{msg}</div>"}
                    )
                    self._save_building_histories()
                    return # Abort the move

            # 2. Update interaction mode to 'auto'
            db.query(AIModel).filter(AIModel.AIID == persona_id).update({"INTERACTION_MODE": "auto"})

            # 3. Move the persona (同じセッションを渡す)
            success, reason = self._move_persona(persona_id, self.user_room_id, destination_id, db_session=db)

            if success:
                persona = self.personas.get(persona_id)
                if persona:
                    persona.current_building_id = destination_id
                    # 退室メッセージをuser_roomの履歴に追加
                    dest_name = self.building_map[destination_id].name
                    msg = f"{self.id_to_name_map.get(persona_id, persona_id)}が{dest_name}に向かいました。"
                    self.building_histories[self.user_room_id].append(
                        {
                            "role": "assistant",
                            "persona_id": persona_id,
                            "content": f'<div class="note-box">🏢 Building:<br><b>{msg}</b></div>'
                        }
                    )
                    self._save_building_histories()
                    logging.info(f"Updated {persona_id}'s internal location to {destination_id}.")
                else:
                    # This case should not happen if persona_id is in self.personas
                    logging.error(f"Could not find PersonaCore instance for {persona_id} to update location.")
            else:
                # Log failure to UI if move fails
                msg = f"{self.id_to_name_map.get(persona_id, persona_id)}を移動できませんでした: {reason}"
                self.building_histories["user_room"].append(
                    {"role": "host", "content": f'<div class="note-box">{msg}</div>'}
                )
                self._save_building_histories()
                # 移動に失敗したら、トランザクション全体をロールバック
                db.rollback() 
                return # 処理を中断

            # 4. すべてのDB操作が成功したら、ここで一度にコミット
            db.commit()
            logging.info(f"Successfully ended conversation with {persona_id}.")

        except Exception as e:
            db.rollback()
            logging.error(f"Failed to end conversation for {persona_id}: {e}", exc_info=True)
        finally:
            db.close()

    def set_model(self, model: str) -> None:
        """
        Update LLM model for all personas temporarily.
        This method updates the model for all currently active persona instances in memory.
        This change is not persistent and will be reset to the DB-defined default upon city restart.
        """
        logging.info(f"Temporarily setting model to '{model}' for all active personas.")
        self.model = model
        self.context_length = get_context_length(model)
        self.provider = get_model_provider(model)
        for persona in self.personas.values():
            # This overrides any individual model settings from the DB for the current session.
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
            replies.extend(persona.run_scheduled_prompt())
        if replies:
            self._save_building_histories()
            for persona in self.personas.values():
                persona._save_session_metadata()
        return replies
