import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import requests

from discord_gateway.translator import GatewayCommand
from manager.persona import PersonaMixin
from manager.visitors import VisitorMixin
from manager.gateway import GatewayMixin
from manager.sds import SDSMixin
from manager.background import DatabasePollingMixin
from manager.state import CoreState
from database.models import (
    BuildingOccupancyLog,
    VisitingAI,
    ThinkingRequest,
    User as UserModel,
)


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
        db = self.SessionLocal()
        try:
            pending_requests = db.query(ThinkingRequest).filter(
                ThinkingRequest.city_id == self.state.city_id,
                ThinkingRequest.status == "pending",
            ).all()
            if not pending_requests:
                return

            logging.info(
                "Found %d new thinking request(s).", len(pending_requests)
            )

            for req in pending_requests:
                persona = self.personas.get(req.persona_id)
                if not persona:
                    logging.error(
                        "Persona %s not found for thinking request %s.",
                        req.persona_id,
                        req.request_id,
                    )
                    req.status = "error"
                    req.response_text = "Persona not found in this city."
                    continue

                try:
                    context = json.loads(req.request_context_json)
                    info_text_parts = [
                        "You are currently in a remote city. Here is the context from there:",
                        f"- Building: {context.get('building_id')}",
                        f"- Occupants: {', '.join(context.get('occupants', []))}",
                        f"- User is {'online' if context.get('user_online') else 'offline'}",
                        "- Recent History:",
                    ]
                    for msg in context.get("recent_history", []):
                        info_text_parts.append(
                            f"  - {msg.get('role')}: {msg.get('content')}"
                        )
                    info_text = "\n".join(info_text_parts)

                    response_text, _, _ = persona._generate(
                        user_message=None,
                        system_prompt_extra=None,
                        info_text=info_text,
                        log_extra_prompt=False,
                        log_user_message=False,
                    )

                    req.response_text = response_text
                    req.status = "processed"
                    logging.info(
                        "Processed thinking request %s for %s.",
                        req.request_id,
                        req.persona_id,
                    )

                except errors.ServerError as exc:
                    logging.warning(
                        "LLM Server Error on thinking request %s: %s. Marking as error.",
                        req.request_id,
                        exc,
                    )
                    req.status = "error"
                    if "503" in str(exc):
                        req.response_text = (
                            "[SAIVERSE_ERROR] LLMモデルが一時的に利用できませんでした (503 Server Error)。"
                            "時間をおいて再度試行してください。詳細: "
                            f"{exc}"
                        )
                    else:
                        req.response_text = (
                            "[SAIVERSE_ERROR] LLMサーバーで予期せぬエラーが発生しました。詳細: "
                            f"{exc}"
                        )
                except Exception as exc:
                    logging.error(
                        "Error processing thinking request %s: %s",
                        req.request_id,
                        exc,
                        exc_info=True,
                    )
                    req.status = "error"
                    req.response_text = (
                        f"[SAIVERSE_ERROR] An internal error occurred during thinking: {exc}"
                    )
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.error(
                "Error during thinking request check: %s", exc, exc_info=True
            )
        finally:
            db.close()

    def check_for_visitors(self) -> None:
        """DBをポーリングして新しい訪問者を検知し、Cityに配置する"""
        db = self.SessionLocal()
        try:
            visitors_to_process = db.query(VisitingAI).filter(
                VisitingAI.city_id == self.state.city_id,
                VisitingAI.status == "requested",
            ).all()
            if not visitors_to_process:
                return

            logging.info(
                "Found %d new visitor request(s) in the database.",
                len(visitors_to_process),
            )

            for visitor in visitors_to_process:
                try:
                    self._handle_visitor_arrival(visitor)
                except Exception as exc:
                    logging.error(
                        "Unexpected error processing visitor ID %s: %s. "
                        "Setting status to 'rejected'.",
                        visitor.id,
                        exc,
                        exc_info=True,
                    )
                    error_db = self.SessionLocal()
                    try:
                        error_visitor = (
                            error_db.query(VisitingAI)
                            .filter_by(id=visitor.id)
                            .first()
                        )
                        if error_visitor:
                            error_visitor.status = "rejected"
                            error_visitor.reason = (
                                f"Internal server error during arrival: {exc}"
                            )
                            error_db.commit()
                    finally:
                        error_db.close()
        except Exception as exc:
            logging.error(
                "Error during visitor check loop: %s", exc, exc_info=True
            )
        finally:
            db.close()

    def check_dispatch_status(self) -> None:
        """自身が要求した移動トランザクションの状態を監視し、プロセスを確定させる"""
        db = self.SessionLocal()
        try:
            dispatches = db.query(VisitingAI).filter(
                VisitingAI.profile_json.like(
                    f'%"source_city_id": "{self.state.city_name}"%'
                )
            ).all()

            for dispatch in dispatches:
                persona_id = dispatch.persona_id
                persona = self.personas.get(persona_id)
                if not persona:
                    continue

                is_timed_out = (
                    dispatch.status == "requested"
                    and hasattr(dispatch, "created_at")
                    and dispatch.created_at
                    < datetime.now()
                    - timedelta(seconds=self.dispatch_timeout_seconds)
                )

                if dispatch.status == "accepted":
                    logging.info(
                        "Dispatch for %s was accepted. Finalizing departure.",
                        persona.persona_name,
                    )
                    self._finalize_dispatch(persona_id, db_session=db)
                    db.delete(dispatch)

                elif dispatch.status in {"rejected", "failed"} or is_timed_out:
                    reason = dispatch.reason or "No reason provided."
                    logging.warning(
                        "Dispatch for %s failed or timed out: %s",
                        persona.persona_name,
                        reason,
                    )
                    persona.is_dispatched = False
                    persona.interaction_mode = "auto"
                    db.delete(dispatch)

            db.commit()
        except Exception as exc:
            db.rollback()
            logging.error(
                "Error during dispatch status check: %s", exc, exc_info=True
            )
        finally:
            db.close()

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
                self.state.user_is_online = user.LOGGED_IN
                self.state.user_current_city_id = user.CURRENT_CITYID
                self.state.user_current_building_id = user.CURRENT_BUILDINGID
                self.state.user_display_name = (
                    (user.USERNAME or "ユーザー").strip() or "ユーザー"
                )
                self.id_to_name_map[str(self.state.user_id)] = (
                    self.state.user_display_name
                )
                logging.info(
                    "Loaded user state: %s at %s",
                    "Online" if self.state.user_is_online else "Offline",
                    self.state.user_current_building_id,
                )
            else:
                logging.warning(
                    "User with USERID=%s not found. Defaulting to Offline.",
                    self.state.user_id,
                )
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
            self.state.user_display_name = "ユーザー"
            self.id_to_name_map[str(self.state.user_id)] = (
                self.state.user_display_name
            )
        finally:
            db.close()

    def move_user(self, target_building_id: str) -> Tuple[bool, str]:
        if target_building_id not in self.building_map:
            return False, f"移動失敗: 建物 '{target_building_id}' が見つかりません。"

        from_building_id = self.state.user_current_building_id
        if not from_building_id:
            return False, "移動失敗: 現在地が不明です。"
        if from_building_id == target_building_id:
            return True, "同じ場所にいます。"

        success, message = self.occupancy_manager.move_entity(
            str(self.state.user_id),
            "user",
            from_building_id,
            target_building_id,
        )
        if success:
            self.state.user_current_building_id = target_building_id
        return success, message

    def _move_persona(
        self,
        persona_id: str,
        from_id: str,
        to_id: str,
        db_session=None,
    ) -> Tuple[bool, Optional[str]]:
        return self.occupancy_manager.move_entity(
            entity_id=persona_id,
            entity_type="ai",
            from_id=from_id,
            to_id=to_id,
            db_session=db_session,
        )

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

        prev = persona.current_building_id
        if prev == self.user_room_id:
            return True, None

        allowed, reason = True, None
        if self._move_persona:
            allowed, reason = self._move_persona(
                persona.persona_id, prev, self.user_room_id
            )
        if not allowed:
            persona.history_manager.add_to_building_only(
                self.user_room_id,
                {
                    "role": "assistant",
                    "content": f'<div class="note-box">移動できませんでした。{reason}</div>',
                },
                heard_by=self._occupants_snapshot(self.user_room_id),
            )
            persona._save_session_metadata()
            return False, reason

        persona.current_building_id = self.user_room_id
        persona.auto_count = 0
        persona._mark_entry(self.user_room_id)
        persona.history_manager.add_to_building_only(
            self.user_room_id,
            {
                "role": "assistant",
                "content": f'<div class="note-box">🏢 Building:<br><b>{persona.persona_name}が入室しました</b></div>',
            },
            heard_by=self._occupants_snapshot(self.user_room_id),
        )
        persona._save_session_metadata()
        persona.run_auto_conversation(initial=True)
        return True, None

    def end_conversation(self, persona_id: str) -> str:
        persona = self.personas.get(persona_id)
        if not persona:
            return f"Error: Persona with ID '{persona_id}' not found."

        if persona.current_building_id != self.user_room_id:
            return f"{persona.persona_name} is not in the user room."

        private_room_id = getattr(persona, "private_room_id", None) or getattr(
            persona, "home_building_id", None
        )
        if not private_room_id or private_room_id not in self.building_map:
            return "Error: Private room not found for this persona."

        success, reason = self._move_persona(
            persona_id, self.user_room_id, private_room_id
        )
        if not success:
            return f"Error: Failed to move: {reason}"

        persona.current_building_id = private_room_id
        persona.history_manager.add_to_building_only(
            self.user_room_id,
            {
                "role": "assistant",
                "content": f'<div class="note-box">🏢 Building:<br><b>{persona.persona_name}が退室しました</b></div>',
            },
            heard_by=self._occupants_snapshot(self.user_room_id),
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
        if not self.state.user_current_building_id:
            return ['<div class="note-box">エラー: ユーザーの現在地が不明です。</div>']

        building_id = self.state.user_current_building_id
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]

        user_entry = {"role": "user", "content": message}
        if metadata:
            user_entry["metadata"] = metadata

        if metadata:
            logging.debug(
                "[runtime] received metadata with keys=%s", list(metadata.keys())
            )

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
                hist.append(
                    {
                        "role": "user",
                        "content": message,
                        "seq": next_seq,
                        "message_id": f"{building_id}:{next_seq}",
                        "heard_by": list(self.occupants.get(building_id, [])),
                        **({"metadata": metadata} if metadata else {}),
                    }
                )

        replies: List[str] = []
        for persona in responding_personas:
            if persona.interaction_mode == "manual":
                replies.extend(
                    persona.handle_user_input(message, metadata=metadata)
                )
            else:
                replies.extend(
                    persona.run_pulse(
                        occupants=self.occupants.get(building_id, []), user_online=True
                    )
                )

        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies

    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Iterator[str]:
        logging.debug(
            "[runtime] handle_user_input_stream called (metadata_present=%s)",
            bool(metadata),
        )
        if not self.state.user_current_building_id:
            yield '<div class="note-box">エラー: ユーザーの現在地が不明です。</div>'
            return

        building_id = self.state.user_current_building_id
        responding_personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]

        user_entry = {"role": "user", "content": message}
        if metadata:
            user_entry["metadata"] = metadata

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
                hist.append(
                    {
                        "role": "user",
                        "content": message,
                        "seq": next_seq,
                        "message_id": f"{building_id}:{next_seq}",
                        "heard_by": list(self.occupants.get(building_id, [])),
                        **({"metadata": metadata} if metadata else {}),
                    }
                )

        for persona in responding_personas:
            if persona.interaction_mode == "manual":
                for token in persona.handle_user_input_stream(
                    message, metadata=metadata
                ):
                    yield token
            else:
                occupants = self.occupants.get(building_id, [])
                for reply in persona.run_pulse(occupants=occupants, user_online=True):
                    yield reply

        self._save_building_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()

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
