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
        self.user_display_name: str = "ユーザー"
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
                    info_text_parts.append("You are currently in a remote city. Here is the context from there:")
                    info_text_parts.append(f"- Building: {context.get('building_id')}")
                    info_text_parts.append(f"- Occupants: {', '.join(context.get('occupants', []))}")
                    info_text_parts.append(f"- User is {'online' if context.get('user_online') else 'offline'}")
                    info_text_parts.append("- Recent History:")
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
                self.user_display_name = (user.USERNAME or "ユーザー").strip() or "ユーザー"
                self.id_to_name_map[str(self.user_id)] = self.user_display_name
                logging.info(f"Loaded user state: {'Online' if self.user_is_online else 'Offline'} at {self.user_current_building_id}")
            else:
                logging.warning("User with USERID=1 not found. Defaulting to Offline.")
                self.user_is_online = False
                self.user_current_building_id = None
                self.user_current_city_id = None
                self.user_display_name = "ユーザー"
                self.id_to_name_map[str(self.user_id)] = self.user_display_name
        except Exception as e:
            logging.error(f"Failed to load user status from DB: {e}", exc_info=True)
            self.user_is_online = False
            self.user_current_building_id = None
            self.user_display_name = "ユーザー"
            self.id_to_name_map[str(self.user_id)] = self.user_display_name
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
                self.user_display_name = (user.USERNAME or "ユーザー").strip() or "ユーザー"
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

    def handle_user_input(self, message: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        logging.debug("[saiverse_manager] handle_user_input called (metadata_present=%s)", bool(metadata))
        if not self.user_current_building_id:
            return ['<div class="note-box">エラー: ユーザーの現在地が不明です。</div>']

        building_id = self.user_current_building_id
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]

        user_entry = {"role": "user", "content": message}
        if metadata:
            user_entry["metadata"] = metadata

        if metadata:
            logging.debug("[saiverse_manager] received metadata with keys=%s", list(metadata.keys()))

        # Always inject the user's message into building history once for perception
        if responding_personas:
            try:
                responding_personas[0].history_manager.add_to_building_only(
                    building_id,
                    user_entry,
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
                    **({"metadata": metadata} if metadata else {}),
                })

        replies: List[str] = []
        for persona in responding_personas:
            if persona.interaction_mode == 'manual':
                # Immediate response path
                replies.extend(persona.handle_user_input(message, metadata=metadata))
            else:
                # pulse-driven for 'user' and 'auto'
                replies.extend(persona.run_pulse(occupants=self.occupants.get(building_id, []), user_online=True))

        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies


    def handle_user_input_stream(self, message: str, metadata: Optional[Dict[str, Any]] = None) -> Iterator[str]:
        logging.debug("[saiverse_manager] handle_user_input_stream called (metadata_present=%s)", bool(metadata))
        if not self.user_current_building_id:
            yield '<div class="note-box">エラー: ユーザーの現在地が不明です。</div>'
            return

        building_id = self.user_current_building_id
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]

        user_entry = {"role": "user", "content": message}
        if metadata:
            user_entry["metadata"] = metadata

        # Inject once into building history for perception
        if responding_personas:
            try:
                responding_personas[0].history_manager.add_to_building_only(
                    building_id,
                    user_entry,
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
                    **({"metadata": metadata} if metadata else {}),
                })

        for persona in responding_personas:
            if persona.interaction_mode == 'manual':
                for token in persona.handle_user_input_stream(message, metadata=metadata):
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
            cols = ['CITYID', 'CITYNAME', 'DESCRIPTION', 'TIMEZONE', 'START_IN_ONLINE_MODE', 'UI_PORT', 'API_PORT']
            existing_cols = [c for c in cols if c in df.columns]
            return df[existing_cols]
        finally:
            db.close()

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
        db = self.SessionLocal()
        try:
            city = db.query(CityModel).filter(CityModel.CITYID == city_id).first()
            if not city:
                return "Error: City not found."

            tz_candidate = (timezone_name or "UTC").strip() or "UTC"
            try:
                ZoneInfo(tz_candidate)
            except Exception:
                return f"Error: Invalid timezone '{tz_candidate}'. Please provide an IANA timezone name (e.g., Asia/Tokyo)."
            
            city.CITYNAME = name
            city.DESCRIPTION = description
            city.START_IN_ONLINE_MODE = online_mode
            city.UI_PORT = ui_port
            city.API_PORT = api_port
            city.TIMEZONE = tz_candidate
            db.commit()

            # If we are updating the current city, update some in-memory state
            if city.CITYID == self.city_id:
                self.start_in_online_mode = online_mode
                self.city_name = name
                self.ui_port = ui_port
                self.api_port = api_port
                self.user_room_id = f"user_room_{self.city_name}"
                self._update_timezone_cache(tz_candidate)

            # Refresh cached city config
            self._load_cities_from_db()
            
            logging.info(f"Updated city settings for City ID {city_id}. A restart may be required.")
            return "City settings updated successfully. A restart is required for changes to apply."
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to update city settings for ID {city_id}: {e}", exc_info=True)
            return f"Error: {e}"
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

    def create_city(self, name: str, description: str, ui_port: int, api_port: int, timezone_name: str) -> str:
        """Creates a new city."""
        db = self.SessionLocal()
        try:
            if db.query(CityModel).filter_by(CITYNAME=name).first():
                return f"Error: A city named '{name}' already exists."
            if db.query(CityModel).filter((CityModel.UI_PORT == ui_port) | (CityModel.API_PORT == api_port)).first():
                return f"Error: UI Port {ui_port} or API Port {api_port} is already in use."

            tz_candidate = (timezone_name or "UTC").strip() or "UTC"
            try:
                ZoneInfo(tz_candidate)
            except Exception:
                return f"Error: Invalid timezone '{tz_candidate}'. Please provide an IANA timezone name (e.g., Asia/Tokyo)."

            new_city = CityModel(
                USERID=self.user_id,
                CITYNAME=name,
                DESCRIPTION=description,
                UI_PORT=ui_port,
                API_PORT=api_port,
                TIMEZONE=tz_candidate,
            )
            db.add(new_city)
            db.commit()
            self._load_cities_from_db()
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
            if not city:
                return "Error: City not found."
            if city.CITYNAME in ["city_a", "city_b"]:
                return "Error: Seeded cities (city_a, city_b) cannot be deleted."
            if city.CITYID == self.city_id:
                return "Error: Cannot delete the currently running city."

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
            if not building:
                return "Error: Building not found."

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
