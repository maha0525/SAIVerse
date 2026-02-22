import importlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import requests
import threading
import queue
from google.genai import errors

from api.deps import avatar_path_to_url
from llm_clients.exceptions import LLMError
from discord_gateway.translator import GatewayCommand
from manager.persona import PersonaMixin
from manager.visitors import VisitorMixin
from manager.gateway import GatewayMixin
from manager.sds import SDSMixin
from manager.background import DatabasePollingMixin
from manager.state import CoreState
from database.models import (
    BuildingOccupancyLog,
    BuildingToolLink,
    Tool as ToolModel,
    VisitingAI,
    ThinkingRequest,
    User as UserModel,
)
import tools.core

# Import trigger types for phenomenon system
try:
    from phenomena.triggers import TriggerEvent, TriggerType
    TRIGGERS_AVAILABLE = True
except ImportError:
    TRIGGERS_AVAILABLE = False

class RuntimeService(
    VisitorMixin, GatewayMixin, SDSMixin, DatabasePollingMixin, PersonaMixin
):
    """Runtime-facing operations: conversations, SDS/gateway loops, movement."""

    def __init__(self, manager, state: CoreState):
        self.manager = manager
        self.state = state

        self.SessionLocal = manager.SessionLocal
        self.sds_session = manager.sds_session
        self.sds_url = manager.sds_url
        self.cities_config = manager.cities_config
        self.dispatch_timeout_seconds = getattr(
            manager, "dispatch_timeout_seconds", 300
        )

        # shared collections
        self.personas = state.personas
        self.visiting_personas = state.visiting_personas
        self.avatar_map = state.avatar_map
        self.persona_map = state.persona_map
        self.occupants = state.occupants
        self.id_to_name_map = state.id_to_name_map
        self.building_histories = state.building_histories
        self.building_map = state.building_map
        self.buildings = state.buildings
        self.user_room_id = state.user_room_id
        self.model = state.model
        self.provider = state.provider
        self.context_length = state.context_length
        self.city_id = state.city_id
        self.city_name = state.city_name
        self.default_avatar = state.default_avatar
        self.host_avatar = state.host_avatar
        self.saiverse_home = state.saiverse_home
        self.capacities = state.capacities
        self.building_memory_paths = state.building_memory_paths

        # passthrough hooks
        self._handle_visitor_arrival = manager._handle_visitor_arrival
        self._save_building_histories = manager._save_building_histories
        self._register_with_sds = manager._register_with_sds
        self._update_cities_from_sds = manager._update_cities_from_sds
        self._load_cities_from_db = manager._load_cities_from_db
        self.conversation_managers = manager.conversation_managers
        self.occupancy_manager = manager.occupancy_manager
        self._gateway_memory_transfers = manager._gateway_memory_transfers
        self._gateway_memory_active_persona = manager._gateway_memory_active_persona
        self.gateway_runtime = manager.gateway_runtime
        self.gateway_mapping = manager.gateway_mapping

    # ----- Background loops -----

    def process_thinking_requests(self) -> None:
        """DBをポーリングして新しい思考依頼を処理する"""
        self._process_thinking_requests()

    def check_for_visitors(self) -> None:
        """DBをポーリングして新しい訪問者を検知し、Cityに配置する"""
        self._check_for_visitors()

    def check_dispatch_status(self) -> None:
        """自身が要求した移動トランザクションの状態を監視し、プロセスを確定させる"""
        self._check_dispatch_status()

    # ----- User and persona movement -----

    def load_user_state_from_db(self) -> None:
        db = self.SessionLocal()
        try:
            user = (
                db.query(UserModel)
                .filter(UserModel.USERID == self.state.user_id)
                .first()
            )
            if user:
                # Map DB boolean to presence status string
                self.state.user_presence_status = "online" if user.LOGGED_IN else "offline"
                self.state.user_current_city_id = user.CURRENT_CITYID
                self.state.user_current_building_id = user.CURRENT_BUILDINGID
                self.state.user_display_name = (
                    (user.USERNAME or "ユーザー").strip() or "ユーザー"
                )
                avatar_data = None
                if getattr(user, "AVATAR_IMAGE", None):
                    from manager.user_state import UserStateMixin
                    avatar_path = UserStateMixin._resolve_avatar_to_path(
                        user.AVATAR_IMAGE
                    )
                    if avatar_path:
                        avatar_data = self.manager._load_avatar_data(avatar_path)
                self.state.user_avatar_data = avatar_data or self.manager.default_avatar
                self.id_to_name_map[str(self.state.user_id)] = (
                    self.state.user_display_name
                )
                logging.info(
                    "Loaded user state: %s at %s",
                    self.state.user_presence_status,
                    self.state.user_current_building_id,
                )
            else:
                logging.warning(
                    "User with USERID=%s not found. Defaulting to Offline.",
                    self.state.user_id,
                )
                self.state.user_presence_status = "offline"
                self.state.user_current_building_id = None
                self.state.user_current_city_id = None
                self.state.user_display_name = "ユーザー"
                self.state.user_avatar_data = self.manager.default_avatar
                self.id_to_name_map[str(self.state.user_id)] = (
                    self.state.user_display_name
                )
        except Exception as exc:
            logging.error(
                "Failed to load user status from DB: %s", exc, exc_info=True
            )
            self.state.user_presence_status = "offline"
            self.state.user_current_building_id = None
            self.state.user_display_name = "ユーザー"
            self.state.user_avatar_data = self.manager.default_avatar
            self.id_to_name_map[str(self.state.user_id)] = (
                self.state.user_display_name
            )
        finally:
            db.close()

    def move_user(self, target_building_id: str) -> Tuple[bool, str]:
        logging.debug("[MANAGER_MOVE] Attempting move to %s. Current: %s", 
                     target_building_id, self.state.user_current_building_id)

        if target_building_id not in self.building_map:
            logging.debug("[MANAGER_MOVE] Target %s invalid.", target_building_id)
            return False, "Invalid building ID"

        from_building_id = self.state.user_current_building_id
        if not from_building_id:
            logging.debug("[MANAGER_MOVE] Current building unknown.")
            return False, "移動失敗: 現在地が不明です。"
        if from_building_id == target_building_id:
            return True, "同じ場所にいます。"

        logging.debug(
            "[runtime] move_user requested %s -> %s",
            from_building_id,
            target_building_id,
        )

        success, message = self.occupancy_manager.move_entity(
            str(self.state.user_id),
            "user",
            from_building_id,
            target_building_id,
        )
        if success:
            self.state.user_current_building_id = target_building_id
            logging.debug("[runtime] move_user success: now %s", target_building_id)
            logging.debug("[MANAGER_MOVE] Move success. New state bid: %s", self.state.user_current_building_id)
            # Emit user_move trigger
            self._emit_user_move_trigger(from_building_id, target_building_id)
        else:
            logging.debug("[runtime] move_user failed: %s", message)
            logging.debug("[MANAGER_MOVE] Move failed: %s", message)
        return success, message

    def _emit_user_move_trigger(self, from_building: str, to_building: str) -> None:
        """Emit user_move trigger to PhenomenonManager."""
        if not TRIGGERS_AVAILABLE:
            return
        if not hasattr(self.manager, "_emit_trigger"):
            return
        self.manager._emit_trigger(
            TriggerType.USER_MOVE,
            {"from_building": from_building, "to_building": to_building},
        )

    def _move_persona(
        self,
        persona_id: str,
        from_id: str,
        to_id: str,
        db_session=None,
    ) -> Tuple[bool, Optional[str]]:
        result = self.occupancy_manager.move_entity(
            entity_id=persona_id,
            entity_type="ai",
            from_id=from_id,
            to_id=to_id,
            db_session=db_session,
        )
        # Emit persona_move trigger on success
        if result[0] and TRIGGERS_AVAILABLE and hasattr(self.manager, "_emit_trigger"):
            self.manager._emit_trigger(
                TriggerType.PERSONA_MOVE,
                {"persona_id": persona_id, "from_building": from_id, "to_building": to_id},
            )
        return result

    # ----- Conversation helpers -----

    def get_summonable_personas(self) -> List[str]:
        if not self.state.user_current_building_id:
            return []

        here = self.state.user_current_building_id
        summonable = [
            p.persona_name
            for p in self.personas.values()
            if not p.is_dispatched and p.current_building_id != here
        ]
        return sorted(summonable)

    def get_conversing_personas(self) -> List[Tuple[str, str]]:
        if (
            not self.state.user_current_building_id
            or self.state.user_current_building_id != self.user_room_id
        ):
            return []

        conversing_ids = self.occupants.get(self.user_room_id, [])
        return [
            (p.persona_name, p.persona_id)
            for pid, p in self.personas.items()
            if pid in conversing_ids
        ]

    def summon_persona(self, persona_id: str) -> Tuple[bool, Optional[str]]:
        persona = self.personas.get(persona_id)
        if not persona:
            return False, "Persona not found."

        # Move to user's CURRENT building, not just user_room_id
        target_building_id = self.state.user_current_building_id
        if not target_building_id:
            return False, "User's current building is unknown."

        prev = persona.current_building_id
        if prev == target_building_id:
            return True, None

        allowed, reason = True, None
        if self._move_persona:
            allowed, reason = self._move_persona(
                persona.persona_id, prev, target_building_id
            )
        if not allowed:
            persona.history_manager.add_to_building_only(
                target_building_id,
                {
                    "role": "assistant",
                    "content": f'<div class="note-box">移動できませんでした。{reason}</div>',
                },
                heard_by=self._occupants_snapshot(target_building_id),
            )
            persona._save_session_metadata()
            return False, reason

        persona.current_building_id = target_building_id
        persona.auto_count = 0
        persona._mark_entry(target_building_id)
        persona.history_manager.add_to_building_only(
            target_building_id,
            {
                "role": "assistant",
                "content": f'<div class="note-box">🏢 Building:<br><b>{persona.persona_name}が入室しました</b></div>',
            },
            heard_by=self._occupants_snapshot(target_building_id),
        )
        persona._save_session_metadata()
        persona.run_auto_conversation(initial=True)
        return True, None

    def end_conversation(self, persona_id: str) -> str:
        persona = self.personas.get(persona_id)
        if not persona:
            return f"Error: Persona with ID '{persona_id}' not found."

        # Check if persona is in the same building as the user (not just user_room_id)
        current_user_building = self.state.user_current_building_id
        if not current_user_building:
            return "Error: User's current building is unknown."

        if persona.current_building_id != current_user_building:
            return f"{persona.persona_name} is not in the current building."

        private_room_id = getattr(persona, "private_room_id", None)
        if not private_room_id:
            return "Error: Private room not configured for this persona."
        if private_room_id not in self.building_map:
            return "Error: Private room not found for this persona."

        success, reason = self._move_persona(
            persona_id, current_user_building, private_room_id
        )
        if not success:
            return f"Error: Failed to move: {reason}"

        persona.current_building_id = private_room_id
        persona.history_manager.add_to_building_only(
            current_user_building,
            {
                "role": "assistant",
                "content": f'<div class="note-box">🏢 Building:<br><b>{persona.persona_name}が退室しました</b></div>',
            },
            heard_by=self._occupants_snapshot(current_user_building),
        )
        persona._save_session_metadata()
        return f"Conversation with '{persona.persona_name}' ended."

    # ----- Conversation handlers -----

    def handle_user_input(
        self, message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        logging.debug(
            "[runtime] handle_user_input called (metadata_present=%s)", bool(metadata)
        )
        if not message or not str(message).strip():
            logging.error("[runtime] handle_user_input got empty message; aborting to avoid corrupt routing")
            return ['<div class="note-box">入力が空でした。再送してください。</div>']

        if not self.state.user_current_building_id:
            return ['<div class="note-box">エラー: ユーザーの現在地が不明です。</div>']

        building_id = self.state.user_current_building_id
        logging.debug("[runtime] handle_user_input building_id=%s", building_id)
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]
        logging.debug(
            "[runtime] handle_user_input responding_personas=%s occupants=%s",
            [p.persona_id for p in responding_personas],
            self.occupants.get(building_id, []),
        )

        user_entry = {"role": "user", "content": message}
        if metadata:
            user_entry["metadata"] = metadata

        if metadata:
            logging.debug(
                "[runtime] received metadata with keys=%s", list(metadata.keys())
            )

        # SEA runtime handles history recording internally
        replies: List[str] = []
        for persona in responding_personas:
            # SEA経由でユーザー入力を処理
            self.manager.run_sea_user(persona, building_id, message)
        logging.debug("[runtime] handle_user_input collected %d replies", len(replies))

        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies

    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None,
        playbook_params: Optional[Dict[str, Any]] = None, building_id: Optional[str] = None,
    ) -> Iterator[str]:
        logging.debug(
            "[runtime] handle_user_input_stream called (metadata_present=%s, meta_playbook=%s, playbook_params=%s, building_id=%s)",
            bool(metadata),
            meta_playbook,
            bool(playbook_params),
            building_id,
        )
        if not message or not str(message).strip():
            logging.error("[runtime] handle_user_input_stream got empty message; aborting to avoid corrupt routing")
            yield '<div class="note-box">入力が空でした。再送してください。</div>'
            return

        building_id = building_id or self.state.user_current_building_id
        if not building_id:
            yield '<div class="note-box">エラー: ユーザーの現在地が不明です。</div>'
            return
        logging.debug("[runtime] handle_user_input_stream building_id=%s", building_id)
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]
        logging.debug(
            "[runtime] handle_user_input_stream responding_personas=%s occupants=%s",
            [p.persona_id for p in responding_personas],
            self.occupants.get(building_id, []),
        )

        user_entry = {"role": "user", "content": message}
        if metadata:
            user_entry["metadata"] = metadata

        # SEA runtime handles history recording internally
        # SEAモード: タイムアウト回避のためのスレッド実行とKeep-Alive
        response_queue = queue.Queue()

        # Stop event for user-initiated cancellation
        stop_event = threading.Event()
        self.manager._active_stop_events[building_id] = stop_event

        def _enrich_event(event):
            """Enrich streaming events with resolved persona name and avatar URL."""
            if isinstance(event, dict) and event.get("persona_id"):
                pid = event["persona_id"]
                p = self.personas.get(pid)
                if p:
                    if not event.get("persona_name"):
                        event["persona_name"] = p.persona_name
                    if not event.get("persona_avatar"):
                        event["persona_avatar"] = (
                            avatar_path_to_url(p.avatar_image)
                            or "/api/static/builtin_icons/host.png"
                        )
            response_queue.put(event)

        def backend_worker():
            try:
                for persona in responding_personas:
                    if stop_event.is_set():
                        logging.info("[runtime] Stop event detected; breaking persona loop for building %s", building_id)
                        response_queue.put({"type": "cancelled", "content": "生成を中止しました。"})
                        break
                    # SEA実行。イベントはコールバック経由でキューに送る
                    self.manager.run_sea_user(
                        persona, building_id, message,
                        metadata=metadata,
                        meta_playbook=meta_playbook,
                        playbook_params=playbook_params,
                        event_callback=_enrich_event
                    )
                    # Check stop event after each persona completes
                    if stop_event.is_set():
                        logging.info("[runtime] Stop event detected after persona %s; breaking loop", persona.persona_id)
                        response_queue.put({"type": "cancelled", "content": "生成を中止しました。"})
                        break
            except LLMError as e:
                logging.error("SEA worker LLM error: %s", e, exc_info=True)
                if stop_event.is_set():
                    response_queue.put({"type": "cancelled", "content": "生成を中止しました。"})
                else:
                    response_queue.put(e.to_dict())
            except Exception as e:
                logging.error("SEA worker error", exc_info=True)
                if stop_event.is_set():
                    response_queue.put({"type": "cancelled", "content": "生成を中止しました。"})
                else:
                    response_queue.put({
                        "type": "error",
                        "error_code": "unknown",
                        "content": "予期せぬエラーが発生しました。",
                        "technical_detail": str(e),
                    })
            finally:
                response_queue.put(None)  # 番兵

        threading.Thread(target=backend_worker, daemon=True).start()

        # メインスレッド: キューを監視してクライアントに送信
        try:
            while True:
                try:
                    # 2.0秒待機 (Keep-Aliveのため)
                    item = response_queue.get(timeout=2.0)
                    if item is None:
                        break
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    # cancelled イベント送信後はストリーム終了
                    if isinstance(item, dict) and item.get("type") == "cancelled":
                        # Drain remaining items until sentinel
                        while True:
                            remaining = response_queue.get(timeout=5.0)
                            if remaining is None:
                                break
                        break
                except queue.Empty:
                    # プロキシ等のタイムアウトを防ぐためのPing
                    yield json.dumps({"type": "ping"}, ensure_ascii=False) + "\n"
        finally:
            # Clean up stop event
            self.manager._active_stop_events.pop(building_id, None)

        bh_sizes = {bid: len(h) for bid, h in self.building_histories.items() if h}
        logging.debug("[runtime] pre-save building_histories sizes: %s", bh_sizes)
        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()

    def preview_context(
        self, message: str, building_id: Optional[str] = None,
        meta_playbook: Optional[str] = None,
        image_count: int = 0, document_count: int = 0,
    ) -> List[Dict[str, Any]]:
        """Preview context for all responding personas without sending to LLM.

        Returns a list of preview dicts (one per persona).
        """
        building_id = building_id or self.state.user_current_building_id
        if not building_id:
            return []

        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]

        sea_runtime = self.manager.sea_runtime
        results = []
        for persona in responding_personas:
            try:
                preview = sea_runtime.preview_context(
                    persona, building_id, message,
                    meta_playbook=meta_playbook,
                    image_count=image_count,
                    document_count=document_count,
                )
                results.append(preview)
            except Exception:
                logging.exception("preview_context failed for persona %s", persona.persona_id)
        return results

    def run_scheduled_prompts(self) -> List[str]:
        replies: List[str] = []
        for persona in self.personas.values():
            if getattr(persona, "interaction_mode", "auto") == "auto":
                replies.extend(persona.run_scheduled_prompt())
        if replies:
            self._save_building_histories()
            for persona in self.personas.values():
                persona._save_session_metadata()
        return replies

    def start_autonomous_conversations(self) -> None:
        if self.state.autonomous_conversation_running:
            logging.warning("Autonomous conversations are already running.")
            return

        logging.info("Starting all autonomous conversation managers...")
        for manager in self.conversation_managers.values():
            manager.start()
        self.state.autonomous_conversation_running = True
        logging.info("All autonomous conversation managers have been started.")

    def stop_autonomous_conversations(self) -> None:
        if not self.state.autonomous_conversation_running:
            logging.warning("Autonomous conversations are not running.")
            return

        logging.info("Stopping all autonomous conversation managers...")
        for manager in self.conversation_managers.values():
            manager.stop()
        self.state.autonomous_conversation_running = False
        logging.info("All autonomous conversation managers have been stopped.")

    def execute_tool(
        self, tool_id: int, persona_id: str, arguments: Dict[str, Any]
    ) -> str:
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
                return (
                    f"Error: ツールID {tool_id} は '{building.name}' で利用できません。"
                )

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

    # ----- Exploration -----

    def explore_city(self, persona_id: str, target_city_id: str) -> None:
        persona = self.personas.get(persona_id)
        if not persona:
            logging.error("Cannot explore: Persona %s not found.", persona_id)
            return

        feedback_message = ""
        if target_city_id == self.state.city_name:
            logging.info(
                "Persona %s is exploring the current city: %s",
                persona_id,
                self.state.city_name,
            )
            building_list_str = "\n".join(
                [
                    f"- {b.name} ({b.building_id}): {b.description}"
                    for b in self.buildings
                ]
            )
            feedback_message = (
                f"現在いるCity '{self.state.city_name}' を探索した結果、以下の建物が見つかりました。\n"
                f"{building_list_str}"
            )
        else:
            target_city_info = self.cities_config.get(target_city_id)
            if not target_city_info:
                feedback_message = (
                    f"探索失敗: City '{target_city_id}' は見つかりませんでした。"
                )
                logging.warning(
                    "Persona %s tried to explore non-existent city '%s'.",
                    persona_id,
                    target_city_id,
                )
            else:
                target_api_url = (
                    f"{target_city_info['api_base_url']}/inter-city/buildings"
                )
                try:
                    logging.info(
                        "Persona %s is exploring %s at %s",
                        persona_id,
                        target_city_id,
                        target_api_url,
                    )
                    response = self.sds_session.get(target_api_url, timeout=10)
                    response.raise_for_status()
                    buildings_data = response.json()

                    if not buildings_data:
                        feedback_message = (
                            f"City '{target_city_id}' を探索しましたが、公開されている建物は見つかりませんでした。"
                        )
                    else:
                        building_list_str = "\n".join(
                            [
                                f"- {b['building_name']} ({b['building_id']}): {b['description']}"
                                for b in buildings_data
                            ]
                        )
                        feedback_message = (
                            f"City '{target_city_id}' を探索した結果、以下の建物が見つかりました。\n"
                            f"{building_list_str}"
                        )
                except requests.exceptions.RequestException as exc:
                    feedback_message = (
                        f"探索失敗: City '{target_city_id}' との通信中にエラーが発生しました。"
                    )
                    logging.error(
                        "Failed to connect to target city '%s' for exploration: %s",
                        target_city_id,
                        exc,
                    )
                except json.JSONDecodeError:
                    feedback_message = (
                        f"探索失敗: City '{target_city_id}' からの応答が不正でした。"
                    )
                    logging.error(
                        "Failed to parse JSON response from '%s' during exploration.",
                        target_city_id,
                    )

        system_feedback = (
            '<div class="note-box">🔎 探索結果:<br><b>'
            f"{feedback_message.replace(chr(10), '<br>')}"
            "</b></div>"
        )

        persona.history_manager.add_message(
            {"role": "host", "content": system_feedback},
            persona.current_building_id,
            heard_by=list(self.occupants.get(persona.current_building_id, [])),
        )
        self._save_building_histories()

    # ---- Conversation helpers for mixins ----

    def _occupants_snapshot(self, building_id: str) -> List[str]:
        return list(self.occupants.get(building_id, []))
