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

#: 「ユーザーに何かが届いた」と数える NDJSON イベントの型。
#:
#: 数えないのは進み具合の表示 (status / think / activity / auto_recall /
#: streaming_thinking / streaming_discard / metabolism / user_message_id / ping)。
#: 考えている様子だけ出して黙って終わるのは、ユーザーから見れば無言だから。
#: 確認待ちのダイアログ (permission_request 等) は、ストリームが閉じても画面に
#: 操作が残るので「届いた」に数える。
#:
#: ``streaming_complete`` は**数えない**。これは「ストリームが閉じた」の合図で
#: あって、画面に何かが出た証拠ではない。LLM が空を返した回 (3 回の再試行後も
#: 空だった回を含む) もこのイベントは送られるが、本文が一文字も無いので
#: ``streaming_chunk`` は一度も出ておらず、画面には吹き出しすら作られていない。
#: 数えてしまうと、発言は保存されているのに画面が無言のまま終わる。
#: 本文が出た回は ``streaming_chunk`` が先に来ているので、取りこぼしはない。
#: 設計: docs/issues/user_utterance_path_failure_inventory.md
_OUTCOME_EVENT_TYPES = frozenset({
    "say",
    "streaming_chunk",
    "error",
    "cancelled",
    "duplicate_command",
    "permission_request",
    "spell_confirmation",
    "chronicle_confirm",
    "warning",
    "info",
})

#: ペルソナが実際に言葉を出したと数えるイベントの型。
#:
#: 上の ``_OUTCOME_EVENT_TYPES`` はエラー通知も含む — 「出口が塞がっていない」
#: を見る側の判定としては正しいが、「新しい発言が生まれたか」には使えない。
#: 続きの生成のように「生まれたときだけ状態を進めたい」側はこちらを見る。
_SPEECH_EVENT_TYPES = frozenset({"say", "streaming_chunk"})

#: 本文を運ぶ型。中身が空なら画面には何も出ていないので、届いたとは数えない。
_CONTENT_BEARING_TYPES = frozenset({"say", "streaming_chunk"})


def _stream_key(event: Dict[str, Any]) -> str:
    """撤回を照合するための、断片の出どころ。

    一度の送信で複数のペルソナが並行して喋ることがある。断片の取り消しを
    出どころで照合しないと、**あとから来た誰かの取り消しが、別の誰かの届いた
    発言まで無かったことにする**。
    """
    return f"{event.get('persona_id') or ''}/{event.get('pulse_id') or ''}"


def _note_outcome(outcome_seen: Dict[str, Any], event: Any) -> None:
    """「ユーザーに何かが届いたか」の判定を、イベント 1 件分だけ進める。

    ストリームの断片は**引っ込められることがある**。``streaming_discard`` は
    「いま出した吹き出しを捨てて、整形済みのものを後で出す」ための合図で、
    その後 ``say`` が来ない終わり方 (ツール呼び出しだけで終わった回など) も
    ある。断片で立てた印をそのままにすると、画面には何も残っていないのに
    「届いた」と数えてしまい、無言のまま終わる。

    そこで断片は出どころごとに ``pending`` へ置き、取り消しは同じ出どころの
    ものだけを消す。確定した発言やエラー通知は撤回されないので ``value`` に
    立てる。
    """
    if not isinstance(event, dict):
        return
    etype = event.get("type")
    pending: Dict[str, bool] = outcome_seen.setdefault("pending", {})
    if etype == "streaming_discard":
        pending.pop(_stream_key(event), None)
        return
    if etype not in _OUTCOME_EVENT_TYPES:
        return
    if etype in _CONTENT_BEARING_TYPES and not str(event.get("content") or "").strip():
        return
    if etype == "streaming_chunk":
        pending[_stream_key(event)] = True
        return
    outcome_seen["value"] = True


def _outcome_reached(outcome_seen: Dict[str, Any]) -> bool:
    """「ユーザーに何かが届いた」と言えるか。

    確定した結果が一つでもあるか、取り消されずに残っている断片があれば届いている。
    """
    return bool(outcome_seen.get("value")) or bool(outcome_seen.get("pending"))


def _note_speech(spoke: Dict[str, Any], event: Any) -> None:
    """「ペルソナが実際に言葉を出したか」を、イベント 1 件分だけ進める。

    ``_note_outcome`` と同じ理由で断片は取り消されうる。ここを取り消さないと、
    引っ込めた断片だけで「喋った」と数え、続きの生成の印を降ろしてしまう
    (= 続きを取る手段が消える)。
    """
    if not isinstance(event, dict):
        return
    etype = event.get("type")
    pending: Dict[str, bool] = spoke.setdefault("pending", {})
    if etype == "streaming_discard":
        pending.pop(_stream_key(event), None)
        spoke["value"] = bool(spoke.get("said")) or bool(pending)
        return
    if etype not in _SPEECH_EVENT_TYPES:
        return
    if not str(event.get("content") or "").strip():
        return
    if etype == "streaming_chunk":
        pending[_stream_key(event)] = True
    else:
        spoke["said"] = True
    spoke["value"] = True

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
        self._save_modified_buildings = manager._save_modified_buildings
        self.add_building_event = manager.add_building_event
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
            # state.user_current_building_id は move_entity が canonical sync
            # 済み (W7 柱5: 位置属性の更新は移動 service の責務)
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
    ) -> Tuple[bool, Optional[str]]:
        result = self.occupancy_manager.move_entity(
            entity_id=persona_id,
            entity_type="ai",
            from_id=from_id,
            to_id=to_id,
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
            # 特殊ペルソナ (Ruler 等) は通常世界の召喚対象にしない
            and p.persona_role is None
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

    def _canonical_building_id(self, building_id: str) -> str:
        """Resolve building_id to the canonical key used by building_memory_paths."""
        if building_id in self.building_memory_paths:
            return building_id
        if building_id.endswith("_room"):
            candidate = building_id[:-5]
            if candidate in self.building_memory_paths:
                logging.debug(
                    "[runtime] canonicalized building_id %s -> %s for history routing",
                    building_id,
                    candidate,
                )
                return candidate
        return building_id

    def summon_persona(
        self, persona_id: str, target_building_id: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        persona = self.personas.get(persona_id)
        if not persona:
            return False, "Persona not found."

        # C-1 閲覧モード以降、 frontend が viewing 中の building を渡してくる場合は
        # それを優先する (= 「閲覧中の部屋を管理対象にする」)。 fallback として
        # サーバ側の実滞在地 user_current_building_id を使う。
        target_building_id = (
            target_building_id or self.state.user_current_building_id
        )
        if not target_building_id:
            return False, "Target building is unknown."

        target_history_building_id = self._canonical_building_id(target_building_id)
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
                target_history_building_id,
                {
                    "role": "assistant",
                    "content": f'<div class="note-box">移動できませんでした。{reason}</div>',
                },
                heard_by=self._occupants_snapshot(target_building_id),
            )
            persona._save_session_metadata()
            return False, reason

        # persona.current_building_id / _mark_entry / _save_session_metadata は
        # move_entity が canonical sync 済み (W7 柱5)
        return True, None

    def end_conversation(
        self, persona_id: str, building_id: Optional[str] = None
    ) -> str:
        persona = self.personas.get(persona_id)
        if not persona:
            return f"Error: Persona with ID '{persona_id}' not found."

        # C-1 閲覧モード以降、 frontend が viewing 中の building を渡してくる場合は
        # それを優先する (= 「閲覧中の部屋から persona を退室させる」)。 fallback と
        # してサーバ側の実滞在地 user_current_building_id を使う。
        current_user_building = (
            building_id or self.state.user_current_building_id
        )
        if not current_user_building:
            return "Error: Target building is unknown."

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

        # 位置属性と cursor 儀式は move_entity が canonical sync 済み (W7 柱5)
        return f"Conversation with '{persona.persona_name}' ended."

    # ----- Conversation handlers -----

    def _build_responding_personas(self, building_id: str) -> List[Any]:
        """Building 内発話に応答するペルソナのリストを構築する。

        通常は building の occupants (派遣中を除く)。game Region 内の Building では
        Ruler を**先頭**に注入する (ruler_first: Ruler が最初に裁定し、その後に
        同行ペルソナが反応する)。Ruler は控室に常駐したまま Region 内全 Building の
        発話を受ける。設計: temp/region_rpg_intent.md §B, §E-2
        """
        personas = [
            self.personas[pid]
            for pid in self.occupants.get(building_id, [])
            if pid in self.personas and not self.personas[pid].is_dispatched
        ]
        try:
            top_region = self.manager.get_top_region_of_building(building_id)
        except Exception:
            logging.exception(
                "[runtime] Failed to resolve region of building %s; skipping ruler injection",
                building_id,
            )
            return personas
        if top_region and top_region.is_game_region and top_region.ruler_id:
            ruler = self.personas.get(top_region.ruler_id)
            if ruler is None:
                logging.warning(
                    "[runtime] Region '%s' has ruler_id '%s' but no such persona is loaded",
                    top_region.region_id, top_region.ruler_id,
                )
            elif not ruler.is_dispatched:
                personas = [ruler] + [p for p in personas if p.persona_id != ruler.persona_id]
        return personas

    def _persist_user_utterance(
        self,
        building_id: str,
        message: str,
        metadata: Optional[Dict[str, Any]],
        responding_personas: Sequence[Any],
        client_message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Durably store a user command before any Pulse or side effect starts.

        現在地検証は INSERT と同一トランザクション (W7 柱5 / Codex 第六巡 P1):
        入口の in-memory 照合の後・永続化の前に別デバイスの移動が確定しても、
        旧 Building へ発言が残らない。競合時は ``_location_conflict`` dict を
        返す (何も書いていない)。
        """
        from database.building_messages import (
            insert_building_message_with_location_guard,
        )

        canonical_bid = self._canonical_building_id(building_id)
        heard = [
            *[str(persona.persona_id) for persona in responding_personas],
            str(self.state.user_id),
        ]
        entry: Dict[str, Any] = {
            "role": "user",
            "content": message,
            "timestamp": datetime.now().astimezone().isoformat(),
            "heard_by": sorted(set(heard)),
            "ingested_by": [],
        }
        if metadata:
            entry["metadata"] = dict(metadata)
        if client_message_id:
            entry["client_message_id"] = client_message_id

        saved = insert_building_message_with_location_guard(
            self.SessionLocal, canonical_bid, entry,
            user_id=self.state.user_id,
            expected_building_id=building_id,
        )
        if saved is not None and saved.get("_location_conflict"):
            return saved
        if saved is None or not saved.get("message_id"):
            logging.error(
                "[runtime] Refusing user dispatch because durable insert failed: "
                "building=%s client_message_id=%s",
                canonical_bid,
                client_message_id,
            )
            return None
        return saved

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
        responding_personas = self._build_responding_personas(building_id)
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

        # Durable insert is a hard precondition for cognition and side effects.
        # This also records the utterance in an empty room.
        try:
            saved_user_message = self._persist_user_utterance(
                building_id,
                message,
                metadata,
                responding_personas,
            )
        except Exception:
            logging.exception("[runtime] Failed to persist user utterance")
            saved_user_message = None
        if saved_user_message is not None and saved_user_message.get(
            "_location_conflict"
        ):
            return [
                '<div class="note-box">送信処理中に現在地が変わったため、発言は受け付けられませんでした。</div>'
            ]
        if saved_user_message is None:
            return [
                '<div class="note-box">発言を保存できなかったため、処理を開始しませんでした。再送してください。</div>'
            ]

        # ユーザー発話イベントの受け口 (saiverse.user_conversation)。
        # 会話が開いていれば直接応答を起動し、閉じていて別の活動中なら on_event
        # 判断点で仲裁する (track_retirement.md §7.4 直結化)。
        user_id_str = str(self.state.user_id)
        replies: List[str] = []
        for persona in responding_personas:
            captured_persona = persona

            def _invoke_main_line(p=captured_persona):
                self.manager.run_sea_user(p, building_id, message)

            # pulse_dispatch.md §7: PulseDispatcher 経由で起動 (例外時の
            # フォールバックは Dispatcher が担う)
            self.manager.pulse_dispatcher.dispatch_user_utterance(
                persona_id=captured_persona.persona_id,
                user_id=user_id_str,
                event=user_entry,
                invoke_main_line=_invoke_main_line,
            )
        logging.debug("[runtime] handle_user_input collected %d replies", len(replies))

        self._save_modified_buildings()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies

    def _cancel_after_disconnect(
        self, building_id: str, stop_event: threading.Event,
    ) -> None:
        """読み手が去ったあと、走っている生成を実際に止める。

        停止イベントだけでは足りない。あれは worker が「次のペルソナへ進むか」を
        見るためのもので、**いま走っている LLM / Spell / tool には届かない**。
        止めるには実行中の要求が持つ取り消しの合図 (cancellation token) を立てる
        必要があり、それは画面の「停止」ボタンと同じ経路
        (``cancel_active_generation``) が持っている。

        止めないと、誰も受け取らない応答のために料金が発生し、ペルソナが喋って
        履歴に残る。

        **正常に終わった回では呼ばない。** 後片付け (保存・音声の締め・記憶への
        転記) の途中で取り消しが立つと、そちらが中断される。
        """
        stop_event.set()
        try:
            self.manager.cancel_active_generation()
        except Exception:
            logging.exception(
                "[runtime] could not cancel the generation after the reader left "
                "(building=%s)", building_id,
            )

    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None, meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None, building_id: Optional[str] = None,
        pre_spells: Optional[List[str]] = None,
        client_message_id: Optional[str] = None,
    ) -> Iterator[str]:
        logging.debug(
            "[runtime] handle_user_input_stream called (metadata_present=%s, meta_playbook=%s, args=%s, building_id=%s, pre_spells=%s, client_message_id=%s)",
            bool(metadata),
            meta_playbook,
            bool(args),
            building_id,
            pre_spells,
            client_message_id,
        )
        # 生 HTML を NDJSON へ流すと、フロントの JSON.parse が落ちて
        # console.error だけになり、画面には何も出ない (下の not_in_building が
        # 同じ理由で JSON へ直された 2026-07-21 の指摘が、この 2 箇所には
        # 届いていなかった)。
        if not message or not str(message).strip():
            logging.error("[runtime] handle_user_input_stream got empty message; aborting to avoid corrupt routing")
            yield json.dumps({
                "type": "error",
                "error_code": "empty_message",
                "content": "入力が空でした。もう一度送ってください。",
            }, ensure_ascii=False) + "\n"
            return

        building_id = building_id or self.state.user_current_building_id
        if not building_id:
            yield json.dumps({
                "type": "error",
                "error_code": "no_current_building",
                "content": "現在いる場所が分からないため、発言を届けられませんでした。",
            }, ensure_ascii=False) + "\n"
            return
        # 分離監査 P1-3 (W7 柱5) の多層防御: HTTP 層 (/chat/send) と同じ現在地
        # 照合。HTTP を通らない呼び出し元が別 Building へ発言を配送する経路も塞ぐ。
        if building_id != self.state.user_current_building_id:
            logging.warning(
                "[runtime] refusing utterance to non-current building %s "
                "(current=%s)", building_id, self.state.user_current_building_id,
            )
            # NDJSON ストリームの契約に合わせて JSON イベントで返す (生 HTML は
            # フロントの JSON.parse で破棄され、エラーが表示されない —
            # 2026-07-21 Codex 第五巡 P2)
            yield json.dumps({
                "type": "error",
                "error_code": "not_in_building",
                "content": "現在地ではない建物への発言はできません。",
                "current_building_id": self.state.user_current_building_id,
            }, ensure_ascii=False) + "\n"
            return
        logging.debug("[runtime] handle_user_input_stream building_id=%s", building_id)
        responding_personas = self._build_responding_personas(building_id)
        logging.debug(
            "[runtime] handle_user_input_stream responding_personas=%s occupants=%s",
            [p.persona_id for p in responding_personas],
            self.occupants.get(building_id, []),
        )

        user_id_str = str(self.state.user_id)

        user_entry = {"role": "user", "content": message}
        if metadata:
            user_entry["metadata"] = metadata

        # SEA runtime handles history recording internally
        # SEAモード: タイムアウト回避のためのスレッド実行とKeep-Alive
        response_queue = queue.Queue()

        # Stop event for user-initiated cancellation
        stop_event = threading.Event()
        self.manager._active_stop_events[building_id] = stop_event

        # 「ユーザーに何かが届いたか」の記録。失敗の入口を一つずつ塞ぐのではなく、
        # **結果を作る場所で一度だけ検査する** — 入口を数え切る守り方は必ず漏れる
        # (応答者ゼロ・握り潰された例外・早期 return が、どれも同じ「無言」に
        # 落ちていた)。設計: docs/issues/user_utterance_path_failure_inventory.md
        outcome_seen: Dict[str, Any] = {"value": False, "pending": {}}

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
            _note_outcome(outcome_seen, event)
            response_queue.put(event)

        # Register the SSE callback so on_track_activated 経由で起動される
        # main_line pulse もこの SSE response_queue に events を流せる。
        # finally 節で必ず削除する。
        self.manager._active_sse_callbacks[building_id] = _enrich_event

        def backend_worker():
            try:
                try:
                    saved_user_message = self._persist_user_utterance(
                        building_id,
                        message,
                        metadata,
                        responding_personas,
                        client_message_id,
                    )
                except Exception:
                    logging.exception("[runtime] Failed to persist user utterance")
                    saved_user_message = None

                if saved_user_message is not None and saved_user_message.get(
                    "_location_conflict"
                ):
                    # 永続化 tx 内の現在地検証で競合検出 (W7 / Codex 第六巡 P1)
                    _enrich_event({
                        "type": "error",
                        "error_code": "not_in_building",
                        "content": (
                            "送信処理中に現在地が変わったため、発言は受け付け"
                            "られませんでした。最新状態に同期します。"
                        ),
                        "current_building_id": saved_user_message.get(
                            "current_building_id"
                        ),
                    })
                    return

                if saved_user_message is None:
                    _enrich_event({
                        "type": "error",
                        "error_code": "persistence_failed",
                        "content": (
                            "発言を保存できなかったため、応答処理を開始しませんでした。"
                            "同じ内容を再送できます。"
                        ),
                    })
                    return

                user_msg_id = str(saved_user_message["message_id"])
                _enrich_event({"type": "user_message_id", "message_id": user_msg_id})

                # A duplicate idempotency key is the same utter command, not a
                # new request.  Never restart LLM/tool side effects; clients can
                # refresh history using the returned canonical message id.
                if saved_user_message.get("_was_inserted") is False:
                    _enrich_event({
                        "type": "duplicate_command",
                        "message_id": user_msg_id,
                        "client_message_id": client_message_id,
                        "content": "同じ送信IDの発言は既に受理されています。",
                    })
                    return

                for persona in responding_personas:
                    if stop_event.is_set():
                        logging.info("[runtime] Stop event detected; breaking persona loop for building %s", building_id)
                        response_queue.put({"type": "cancelled", "content": "生成を中止しました。"})
                        break

                    # ユーザー発話イベントの受け口。pulse_dispatch.md §7 で
                    # PulseDispatcher 経由に統一済。会話が開いているかの判定と、
                    # 別の活動中の仲裁 (on_event 判断点直結) / 会話の開始は
                    # saiverse.user_conversation 内部で行う。
                    captured_persona = persona

                    def _invoke_main_line(p=captured_persona):
                        self.manager.run_sea_user(
                            p, building_id, message,
                            metadata=metadata,
                            meta_playbook=meta_playbook,
                            args=args,
                            event_callback=_enrich_event,
                            pre_spells=pre_spells,
                        )

                    # 会話開始経路 (user_input は空で auto_ingest が拾う) にも同じ
                    # 起動オプションを届ける。渡さないと、新規会話の初回発話だけ
                    # 選択 Playbook・引数・pre-spell・SSE 出力が落ちて、継続発話と
                    # 挙動が変わる (2026-08-21 Codex 指摘 5)。
                    pulse_options = {
                        "metadata": metadata,
                        "meta_playbook": meta_playbook,
                        "args": args,
                        "pre_spells": pre_spells,
                        "event_callback": _enrich_event,
                    }

                    self.manager.pulse_dispatcher.dispatch_user_utterance(
                        persona_id=captured_persona.persona_id,
                        user_id=user_id_str,
                        event=user_entry,
                        invoke_main_line=_invoke_main_line,
                        pulse_options=pulse_options,
                    )
                    # Check stop event after each persona completes
                    if stop_event.is_set():
                        logging.info("[runtime] Stop event detected after persona %s; breaking loop", persona.persona_id)
                        response_queue.put({"type": "cancelled", "content": "生成を中止しました。"})
                        break
            except LLMError as e:
                logging.error("SEA worker LLM error: %s", e, exc_info=True)
                if stop_event.is_set():
                    _enrich_event({"type": "cancelled", "content": "生成を中止しました。"})
                else:
                    _enrich_event(e.to_dict())
            except Exception as e:
                logging.error("SEA worker error", exc_info=True)
                if stop_event.is_set():
                    _enrich_event({"type": "cancelled", "content": "生成を中止しました。"})
                else:
                    _enrich_event({
                        "type": "error",
                        "error_code": "unknown",
                        "content": "予期せぬエラーが発生しました。",
                        "technical_detail": str(e),
                    })
            finally:
                # 出口 3: 発言は受け取ったのに、何ひとつ画面へ出ないまま終わる回を
                # ここで捕まえる。上のどの経路を通っても (応答者ゼロ / 早期 return /
                # 握り潰された失敗 / 例外) 最後は必ずここへ来るので、検査は一箇所で
                # 足りる。理由が言えないときも「起きたこと」だけは必ず伝える。
                if not _outcome_reached(outcome_seen) and not stop_event.is_set():
                    logging.warning(
                        "[runtime] the utterance was accepted but nothing reached the "
                        "user (building=%s, responding_personas=%d)",
                        building_id, len(responding_personas),
                    )
                    # 「返事が生まれなかった」には二種類ある。応答できる相手が
                    # そもそも居ない回は、やり直しても永久に結果が変わらない —
                    # 同じ札で扱うと、画面が「再送から応答をもう一度求められ
                    # ます」という**果たせない約束**を出す。ここは理由を知って
                    # いる (数を数えてログにも出している) ので、知っていること
                    # を言う。
                    if not responding_personas:
                        _enrich_event({
                            "type": "error",
                            "error_code": "no_responder",
                            "content": (
                                "この場所には、応答できる相手がいません。"
                            ),
                        })
                    else:
                        _enrich_event({
                            "type": "error",
                            "error_code": "no_response",
                            "content": (
                                "発言は受け取りましたが、返事が生まれませんでした。"
                            ),
                        })
                response_queue.put(None)  # 番兵

        threading.Thread(target=backend_worker, daemon=True).start()

        disconnected = False
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
        except GeneratorExit:
            disconnected = True
            raise
        finally:
            if disconnected:
                self._cancel_after_disconnect(building_id, stop_event)
            # 片付けるのは**自分が置いたもの**だけ。この registry は建物ごとに
            # 一枠しかないので、同じ建物で次のストリームが始まると上書きされる。
            # 無条件に pop すると、先に終わった側が後から始まった側の停止イベント
            # とコールバックまで巻き添えで消し、そちらの「停止」が効かなくなる。
            if self.manager._active_stop_events.get(building_id) is stop_event:
                self.manager._active_stop_events.pop(building_id, None)
            if self.manager._active_sse_callbacks.get(building_id) is _enrich_event:
                self.manager._active_sse_callbacks.pop(building_id, None)

    # ------------------------------------------------------------------
    # やり直しと続き — ユーザーの一押しから起こす生成
    #
    # 追加の推論はすべてボタンの後ろに置く (2026-08-25 まはー裁定)。この二つの
    # 口はどちらも「発言はもう履歴にある」前提で、**応答だけ**を起こす。だから
    # ユーザーの発言を作り直したり、二重に保存したりしない。
    # 設計: docs/issues/user_utterance_path_failure_inventory.md
    # ------------------------------------------------------------------

    #: 中断された発言の続きを頼むとき、プロンプトの入力欄に載せる文。
    #:
    #: ``<system>`` で包むのは「これは機構の言葉であって、ユーザーの発言では
    #: ない」という印。ユーザー名義のテキストを機構が作らないことは SAIVerse の
    #: 一線なので、入力欄の席を借りるときは必ずこの形にする (建物ログの取り込みが
    #: システム通知に使っている包みと同じ)。この文は永続化されない — ユーザー
    #: レーンの ``user_input`` はプロンプトへ渡るだけで、記憶には残らない。
    CONTINUE_INSTRUCTION = (
        "<system>あなたの直前の発言は、途中で途切れたまま終わっています。"
        "その続きを、前の発言にそのままつながる形で話してください。"
        "言い直しや要約はせず、続きだけを述べてください。</system>"
    )

    def _find_building_message(
        self, building_id: str, message_id: str,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """建物履歴から message_id の 1 件を引く。

        戻り値は ``(件, 理由)`` で、理由は ``"found"`` / ``"not_found"`` /
        ``"unavailable"`` の三択。**「記録に無い」と「履歴を読めなかった」を
        同じ値に潰さない** — 読めなかった回に「記録に残っていません。もう一度
        送ってください」と案内すると、ユーザーが送り直して同じ発言が二度載る。
        それを防ぐのが「再送」の口の存在理由なので、潰すと裏口から破ることに
        なる。兄弟の ``withdraw_building_message_in_db`` は最初からこの区別を
        持っており (``"unavailable"``)、こちらだけが持っていなかった。
        """
        try:
            history = self.manager.get_building_history(building_id)
        except Exception:
            logging.exception(
                "[runtime] failed to read the building history (building=%s)",
                building_id,
            )
            return None, "unavailable"
        for msg in history or []:
            if str(msg.get("message_id")) == str(message_id):
                return msg, "found"
        return None, "not_found"

    def _stream_persona_pulse(
        self, building_id: str, persona: Any, user_input: str,
        spoke: Optional[Dict[str, bool]] = None,
    ) -> Iterator[str]:
        """1 体のペルソナの Pulse を起こし、その NDJSON イベントを流す。

        ``handle_user_input_stream`` と同じ器 (キュー + ワーカー + 出口 3 の検査)
        を使うが、**ユーザー発言の永続化は行わない** — 発言はもう履歴にあるので、
        ここでもう一度書くと同じ発言が二度載る。

        ``spoke`` を渡すと、ペルソナが実際に言葉を出したときだけ
        ``spoke["value"]`` が ``True`` になる。呼び出し元が「走らせた」と
        「言葉が生まれた」を取り違えないための報告口で、``outcome_seen`` とは
        別物 — あちらはエラー通知も「出口が塞がっていない」として数えるので、
        「新しい発言ができたか」の判定には使えない。
        """
        response_queue: queue.Queue = queue.Queue()
        stop_event = threading.Event()
        self.manager._active_stop_events[building_id] = stop_event

        outcome_seen: Dict[str, Any] = {"value": False, "pending": {}}

        def _enrich_event(event):
            if isinstance(event, dict) and event.get("persona_id"):
                p = self.personas.get(event["persona_id"])
                if p:
                    if not event.get("persona_name"):
                        event["persona_name"] = p.persona_name
                    if not event.get("persona_avatar"):
                        event["persona_avatar"] = (
                            avatar_path_to_url(p.avatar_image)
                            or "/api/static/builtin_icons/host.png"
                        )
            _note_outcome(outcome_seen, event)
            if spoke is not None:
                _note_speech(spoke, event)
            response_queue.put(event)

        self.manager._active_sse_callbacks[building_id] = _enrich_event

        def worker():
            try:
                self.manager.run_sea_user(
                    persona, building_id, user_input,
                    event_callback=_enrich_event,
                )
            except LLMError as e:
                logging.error("pulse worker LLM error: %s", e, exc_info=True)
                if stop_event.is_set():
                    _enrich_event({"type": "cancelled", "content": "生成を中止しました。"})
                else:
                    _enrich_event(e.to_dict())
            except Exception as e:
                logging.error("pulse worker error", exc_info=True)
                if stop_event.is_set():
                    _enrich_event({"type": "cancelled", "content": "生成を中止しました。"})
                else:
                    _enrich_event({
                        "type": "error",
                        "error_code": "unknown",
                        "content": "予期せぬエラーが発生しました。",
                        "technical_detail": str(e),
                    })
            finally:
                if not _outcome_reached(outcome_seen) and not stop_event.is_set():
                    logging.warning(
                        "[runtime] the pulse produced nothing for the user "
                        "(building=%s persona=%s)",
                        building_id, getattr(persona, "persona_id", None),
                    )
                    _enrich_event({
                        "type": "error",
                        "error_code": "no_response",
                        "content": "返事が生まれませんでした。",
                    })
                response_queue.put(None)

        threading.Thread(target=worker, daemon=True).start()

        disconnected = False
        try:
            while True:
                try:
                    item = response_queue.get(timeout=2.0)
                    if item is None:
                        break
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    if isinstance(item, dict) and item.get("type") == "cancelled":
                        while True:
                            if response_queue.get(timeout=5.0) is None:
                                break
                        break
                except queue.Empty:
                    yield json.dumps({"type": "ping"}, ensure_ascii=False) + "\n"
        except GeneratorExit:
            disconnected = True
            raise
        finally:
            if disconnected:
                # 「続きの生成」と「再送」もここを通る。
                self._cancel_after_disconnect(building_id, stop_event)
            # 片付けるのは自分が置いたものだけ (同上)。
            if self.manager._active_stop_events.get(building_id) is stop_event:
                self.manager._active_stop_events.pop(building_id, None)
            if self.manager._active_sse_callbacks.get(building_id) is _enrich_event:
                self.manager._active_sse_callbacks.pop(building_id, None)

    def continue_persona_message_stream(self, message_id: str) -> Iterator[str]:
        """途中で終わったペルソナの発言の、続きだけを起こす。"""
        building_id = self.state.user_current_building_id
        if not building_id:
            yield json.dumps({
                "type": "error",
                "error_code": "no_current_building",
                "content": "現在いる場所が分からないため、続きを起こせませんでした。",
            }, ensure_ascii=False) + "\n"
            return

        target, lookup = self._find_building_message(building_id, message_id)
        if lookup == "unavailable":
            yield json.dumps({
                "type": "error",
                "error_code": "history_unavailable",
                "content": "履歴を読めなかったため、続きを起こせませんでした。"
                           "少し置いてもう一度お試しください。",
            }, ensure_ascii=False) + "\n"
            return
        if target is None or target.get("role") != "assistant":
            yield json.dumps({
                "type": "error",
                "error_code": "message_not_found",
                "content": "続きを起こす発言が見つかりませんでした。",
            }, ensure_ascii=False) + "\n"
            return
        if not (target.get("metadata") or {}).get("_interrupted"):
            yield json.dumps({
                "type": "error",
                "error_code": "not_interrupted",
                "content": "この発言は途中で終わっていないため、続きはありません。",
            }, ensure_ascii=False) + "\n"
            return

        persona = self.personas.get(target.get("persona_id"))
        if persona is None:
            yield json.dumps({
                "type": "error",
                "error_code": "persona_not_found",
                "content": "発言したペルソナが見つかりませんでした。",
            }, ensure_ascii=False) + "\n"
            return

        spoke: Dict[str, bool] = {"value": False}
        disconnected = False
        try:
            yield from self._stream_persona_pulse(
                building_id, persona, self.CONTINUE_INSTRUCTION, spoke=spoke,
            )
        except GeneratorExit:
            # 読み手が去った。**ここで印を降ろしてはいけない** — ``spoke`` が立つのは
            # 最初の一片が画面へ流れた時点で、続きが保存し終えた時点ではない。降ろすと
            # 「保存されていないのにボタンが消える」ことが起こり、続きを取る手段が
            # 消える。残した印はもう一度押せるだけなので、そちらの害の方が小さい。
            #
            # 逆に「保存されたのに印が残る」回は残る (押すと二つ目の続きが生まれる)。
            # これは**保存できたことを表す信号が無い**という根の症状で、そちらを
            # 直さない限りどちらへ倒しても穴が開く。設計案件として切り出してある:
            # docs/issues/user_utterance_path_failure_inventory.md
            disconnected = True
            raise
        finally:
            if spoke["value"] and not disconnected:
                # 続きが生まれたので、元の発言はもう「続きを待っている」状態では
                # ない。印を降ろしてボタンを消す (中断があった事実は、続けて並ぶ
                # 2 つの発言そのものが残す)。**印の書き込みに失敗しても本体の結果
                # は変えない** — 印が残った回はもう一度押せるだけで、害にならない。
                try:
                    persona.history_manager.update_building_message(
                        building_id, str(message_id),
                        metadata={**(target.get("metadata") or {}), "_interrupted": False},
                    )
                except Exception:
                    logging.exception(
                        "[runtime] failed to clear the interrupted mark (msg=%s)",
                        message_id,
                    )
            # 続きが一言も生まれていない回 (LLM エラー等) は印を残す。ここで降ろすと
            # ボタンが消え、二度目は ``not_interrupted`` の関所に「この発言は途中で
            # 終わっていないため、続きはありません」で拒まれる — 途中で終わって
            # いるのに、そう言うことになる。

    def retry_user_message_stream(self, message_id: str) -> Iterator[str]:
        """既にある自分の発言に対して、応答だけをやり直す。

        発言は履歴に残っているので、**送り直さない**。同じ発言が二度載るのを
        防ぐのがこの口の存在理由で、ユーザーには「再送」の 1 ボタンに見える。
        """
        building_id = self.state.user_current_building_id
        if not building_id:
            yield json.dumps({
                "type": "error",
                "error_code": "no_current_building",
                "content": "現在いる場所が分からないため、やり直せませんでした。",
            }, ensure_ascii=False) + "\n"
            return

        target, lookup = self._find_building_message(building_id, message_id)
        if lookup == "unavailable":
            # 読めなかっただけで、発言は残っている可能性が高い。ここで
            # 「もう一度送ってください」と言うと、送り直しで同じ発言が
            # 二度載る — この口が防ごうとしているものそのもの。
            yield json.dumps({
                "type": "error",
                "error_code": "history_unavailable",
                "content": "履歴を読めなかったため、やり直せませんでした。"
                           "同じ内容を送り直す前に、履歴に残っているかを"
                           "確認してください。",
            }, ensure_ascii=False) + "\n"
            return
        if target is None or target.get("role") != "user":
            # 発言が残っていない = 出口 4。手元の文をそのまま送り直せばよいので、
            # フロントは入力欄への差し戻しへ倒す。
            yield json.dumps({
                "type": "error",
                "error_code": "message_not_found",
                "content": "この発言は記録に残っていません。もう一度送ってください。",
            }, ensure_ascii=False) + "\n"
            return

        responding_personas = self._build_responding_personas(building_id)
        if not responding_personas:
            # ① と同じ事実なので同じ札。別々の札で呼ぶと、画面の側が
            # 「やり直しても無駄」を経路ごとに判断する羽目になる。
            yield json.dumps({
                "type": "error",
                "error_code": "no_responder",
                "content": "この場所には、応答できる相手がいません。",
            }, ensure_ascii=False) + "\n"
            return

        # 応答は最初の 1 体に絞る。全員に振り直すと、既に答えた相手まで
        # もう一度喋ることになる。
        yield from self._stream_persona_pulse(
            building_id, responding_personas[0], "",
        )

    def withdraw_user_message(self, message_id: str) -> Dict[str, Any]:
        """まだ誰も読んでいない自分の発言を取り下げ、本文を手元へ返す。

        取り消せるかどうかは好みでは決まらず、**ペルソナがもう読んだか**で決まる
        (2026-08-25 まはー裁定)。読まれた後に消すと、ペルソナは「聞いた覚えが
        あるのに記録が無い」状態になり、無言で消えるより悪い。
        """
        building_id = self.state.user_current_building_id
        if not building_id:
            return {
                "withdrawn": False, "reason": "unavailable",
                "message": "現在いる場所が分からないため、取り消せませんでした。",
            }

        from database.building_messages import withdraw_building_message_in_db

        session_factory = getattr(self, "SessionLocal", None)
        withdrawn, reason, content = withdraw_building_message_in_db(
            session_factory, building_id, str(message_id),
        )
        if withdrawn:
            # メモリ内の建物履歴からも落とす (DB だけ消すと、再読込まで画面に残る)。
            try:
                history = self.building_histories.get(building_id)
                if isinstance(history, list):
                    history[:] = [
                        m for m in history
                        if str(m.get("message_id")) != str(message_id)
                    ]
            except Exception:
                logging.exception(
                    "[runtime] failed to drop the withdrawn message from the "
                    "in-memory history (msg=%s)", message_id,
                )
            return {"withdrawn": True, "reason": reason, "content": content or ""}

        explain = {
            "already_heard": (
                "この発言はもうペルソナが聞いています。取り消すと、"
                "聞いた覚えがあるのに記録が無い状態になるため、取り消せません。"
            ),
            "not_found": "この発言は記録に残っていません。",
            "wrong_role": "取り消せるのは自分の発言だけです。",
            "unavailable": "取り消しを実行できませんでした。",
        }
        return {
            "withdrawn": False, "reason": reason,
            "message": explain.get(reason, explain["unavailable"]),
        }

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

        responding_personas = self._build_responding_personas(building_id)

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

    # ---- Conversation helpers for mixins ----

    def _occupants_snapshot(self, building_id: str) -> List[str]:
        return list(self.occupants.get(building_id, []))
