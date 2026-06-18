import copy
import json
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, TYPE_CHECKING, Any
import re
from datetime import datetime, timezone

if TYPE_CHECKING:
    from saiverse_memory import SAIMemoryAdapter
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)

class HistoryManager:
    """ペルソナ視点の persona log (= self.messages / persona_log_path) と、
    Building 共有ログ (= ``building_messages`` テーブル) の操作 API。

    Phase 2+3 以降、 Building 側は DB が単一の source of truth。 旧来の
    ``building_histories`` in-memory dict は廃止された。
    See docs/intent/building_memory_unified.md
    """

    def __init__(
        self,
        persona_id: str,
        persona_log_path: Path,
        building_memory_paths: Dict[str, Path],
        initial_persona_history: Optional[List[Dict[str, str]]] = None,
        memory_adapter: Optional["SAIMemoryAdapter"] = None,
        quarantined_buildings: Optional[Dict[str, Any]] = None,
        db_session_factory: Optional[Callable[[], "Session"]] = None,
    ):
        self.persona_id = persona_id
        self.persona_log_path = persona_log_path
        # building_memory_paths は legacy log.json (archive) のパス参照のために残す。
        # 書き込みには使わない。
        self.building_memory_paths = building_memory_paths
        self.messages = initial_persona_history if initial_persona_history is not None else []
        self.memory_adapter = memory_adapter
        # 隔離フラグ参照 (= 防御的にしか使わないが互換のため保持)
        self._quarantined_buildings = quarantined_buildings if quarantined_buildings is not None else {}
        self.metabolism_anchor_message_id: Optional[str] = None
        # building_messages テーブルアクセス用 SessionLocal。 None なら no-op
        # (= 既存 MagicMock テスト互換のフォールバック)。 本番では PersonaCore が必須で渡す。
        self._db_session_factory = db_session_factory

    def reset_seq_counter_for_building(self, building_id: str, value: int) -> None:
        """[Deprecated] DB が seq を管理するため no-op。 旧 caller 互換のため残存。"""
        return

    def set_memory_adapter(self, adapter: Optional["SAIMemoryAdapter"]) -> None:
        self.memory_adapter = adapter

    def _ensure_size_limit(self, log_list: List[Dict[str, str]], path: Path) -> None:
        count_before = len(log_list)
        while log_list and len(json.dumps(log_list, ensure_ascii=False).encode("utf-8")) > 2000 * 1024:
            removed = log_list.pop(0)
            self._append_to_old_log(path.parent, [removed])
        removed_count = count_before - len(log_list)
        if removed_count > 0:
            LOGGER.info(
                "[size_limit] Trimmed %d messages from %s (was %d, now %d)",
                removed_count, path.name, count_before, len(log_list),
            )

    def _append_to_old_log(self, base_dir: Path, msgs: List[Dict[str, str]]) -> None:
        """Append messages to a rotating log under base_dir/old_log."""
        old_dir = base_dir / "old_log"
        old_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(old_dir.glob("*.json"))
        target = files[-1] if files else None
        if target is None or target.stat().st_size > 2000 * 1024:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = old_dir / f"{timestamp}.json"
            if not target.exists():
                target.write_text("[]", encoding="utf-8")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            data = []
        data.extend(msgs)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _prepare_message(
        self,
        msg: Dict[str, Any],
        *,
        origin_track_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ensures a message has a timestamp and persona_id if applicable.

        ``origin_track_id`` is forwarded to SAIMemory's ``messages.origin_track_id``
        column via ``_sync_to_memory`` → ``append_persona_message`` (which reads
        the key from the message dict). Explicit msg-level key wins; the argument
        only fills in when not pre-set.
        """
        new_msg = msg.copy()
        if "timestamp" not in new_msg:
            new_msg["timestamp"] = datetime.now(timezone.utc).isoformat()
        if new_msg.get("role") == "assistant" and "persona_id" not in new_msg:
            new_msg["persona_id"] = self.persona_id
        if origin_track_id is not None and "origin_track_id" not in new_msg:
            new_msg["origin_track_id"] = origin_track_id
        metadata = new_msg.get("metadata")
        if metadata is not None:
            if isinstance(metadata, dict):
                new_msg["metadata"] = copy.deepcopy(metadata)
            else:
                LOGGER.warning("Discarding metadata with invalid type %s", type(metadata).__name__)
                new_msg.pop("metadata", None)
        return new_msg

    def _extract_occupancy_event(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        metadata = msg.get("metadata")
        if not isinstance(metadata, dict):
            return None
        event = metadata.get("event")
        if not isinstance(event, dict):
            return None
        if event.get("type") != "occupancy":
            return None
        return event

    def _prepare_for_insert(
        self,
        msg: Dict[str, Any],
        heard_by: Optional[List[str]],
    ) -> Dict[str, Any]:
        """``insert_building_message`` に渡す前の正規化。 dual-write 時代の
        ``_decorate_building_message`` の代替 (seq / message_id は DB 採番なので、
        ここでは heard_by / ingested_by の正規化のみ)。
        """
        enriched = msg.copy()
        heard_set = {str(pid) for pid in (heard_by or []) if pid}
        enriched["heard_by"] = sorted(heard_set)
        ingested_raw = enriched.get("ingested_by")
        if isinstance(ingested_raw, list):
            enriched["ingested_by"] = sorted({str(pid) for pid in ingested_raw if pid})
        else:
            enriched["ingested_by"] = []
        return enriched

    def _sync_to_memory(self, *, channel: str, building_id: Optional[str], message: Dict[str, str]) -> None:
        if self.memory_adapter is None or not self.memory_adapter.is_ready():
            return
        if (message.get("role") or "").lower() == "system":
            return
        try:
            metadata = message.setdefault("metadata", {})
            if isinstance(metadata, dict):
                tags = metadata.setdefault("tags", [])
                if isinstance(tags, list) and "conversation" not in tags:
                    tags.append("conversation")
            if channel == "persona":
                self.memory_adapter.append_persona_message(message)
                LOGGER.debug("Synced persona message to SAIMemory for %s", self.persona_id)
            else:
                LOGGER.debug(
                    "Skipped SAIMemory sync for channel=%s target=%s", channel, building_id or self.persona_id
                )
        except Exception:
            LOGGER.exception("Failed to sync message to SAIMemory")

    def add_message(
        self,
        msg: Dict[str, str],
        building_id: str,
        *,
        heard_by: Optional[List[str]] = None,
        origin_track_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Adds a message to both persona log and building DB.

        Returns the saved building message dict (with DB-assigned seq / message_id).
        """
        if building_id in self._quarantined_buildings:
            LOGGER.warning(
                "add_message: building %s is quarantined — refusing", building_id
            )
            return {}
        prepared_msg = self._prepare_message(msg, origin_track_id=origin_track_id)
        if heard_by:
            metadata = prepared_msg.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["audience"] = {
                    "personas": [pid for pid in heard_by if not pid.startswith("user_")],
                    "users": [pid for pid in heard_by if pid.startswith("user_")]
                }
        # Persona log + SAIMemory
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)
        self._sync_to_memory(channel="persona", building_id=None, message=prepared_msg)
        # Building DB (sole source of truth)
        for_insert = self._prepare_for_insert(prepared_msg, heard_by)
        from database.building_messages import insert_building_message
        building_msg = insert_building_message(self._db_session_factory, building_id, for_insert)
        return building_msg or for_insert

    def add_to_building_only(
        self,
        building_id: str,
        msg: Dict[str, str],
        *,
        heard_by: Optional[List[str]] = None,
        origin_track_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Adds a message only to the building DB (skip persona log)."""
        if building_id in self._quarantined_buildings:
            LOGGER.warning(
                "add_to_building_only: building %s is quarantined — refusing", building_id
            )
            return {}
        prepared_msg = self._prepare_message(msg, origin_track_id=origin_track_id)
        for_insert = self._prepare_for_insert(prepared_msg, heard_by)
        from database.building_messages import insert_building_message
        building_msg = insert_building_message(self._db_session_factory, building_id, for_insert)
        return building_msg or for_insert

    def add_to_persona_only(
        self,
        msg: Dict[str, str],
        *,
        origin_track_id: Optional[str] = None,
        sync_to_memory: bool = True,
    ) -> None:
        """Adds a message only to the persona's main history.

        ``sync_to_memory`` (= 既定 True): 末尾で SAIMemory にも 1 件転写する
        既定挙動。 ``False`` を渡すと SAIMemory への自動転写をスキップする。
        Pipeline Streaming の ``emit_speak_finalize`` 経路で使う: スペル入り
        応答ではラウンドごとの記録と最終発言の単独レコードを別経路で既に
        SAIMemory に保存しているので、 ここで全文をさらに転写すると同 pulse
        内に内容完全一致の巨大重複レコードが生まれる。 それを防ぐ。
        """
        prepared_msg = self._prepare_message(msg, origin_track_id=origin_track_id)
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)
        if sync_to_memory:
            self._sync_to_memory(channel="persona", building_id=None, message=prepared_msg)

    def update_building_message(
        self,
        building_id: str,
        message_id: str,
        *,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """既存 building message の content / metadata を更新する (DB 単一)。

        Pipeline Streaming (= LLM streaming 中の文区切り sub-speak) で使う。
        詳細: docs/intent/voice_tts_pipeline_streaming.md

        Returns: 更新後の dict、 該当なしなら None。
        """
        from database.building_messages import update_building_message_in_db, fetch_building_messages
        update_building_message_in_db(
            self._db_session_factory, building_id, message_id,
            content=content, metadata=metadata,
        )
        # 確認のために更新後の行を返す
        if self._db_session_factory is None:
            return None
        from database.models import BuildingMessage
        db = self._db_session_factory()
        try:
            row = db.query(BuildingMessage).filter_by(
                building_id=building_id, message_id=message_id
            ).first()
            if row is None:
                LOGGER.debug(
                    "update_building_message: message_id=%s not found in building=%s",
                    message_id, building_id,
                )
                return None
            from database.building_messages import deserialize_building_message
            return deserialize_building_message(row)
        finally:
            db.close()

    def get_recent_history(
        self,
        max_chars: int,
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Retrieves recent messages from persona history up to a character limit.

        Context-construction callers should pass ``required_line_roles`` /
        ``required_scopes`` (line-based, Phase 1+). ``required_tags`` is
        retained for search/recall compatibility (4-D will retire it).
        """
        if self.memory_adapter is not None:
            if not self.memory_adapter.is_ready():
                LOGGER.debug("SAIMemory adapter not ready for %s; falling back to in-memory", self.persona_id)
            else:
                LOGGER.debug(
                    "Fetching recent persona history from SAIMemory for %s (max_chars=%d)",
                    self.persona_id,
                    max_chars,
                )
                msgs = self.memory_adapter.recent_persona_messages(
                    max_chars,
                    required_tags=required_tags,
                    required_line_roles=required_line_roles,
                    required_scopes=required_scopes,
                    pulse_id=pulse_id,
                )
                LOGGER.debug(
                    "SAIMemory returned %d persona messages for %s",
                    len(msgs),
                    self.persona_id,
                )
                for idx, msg in enumerate(msgs[:3]):
                    LOGGER.debug("SAIMemory head[%d]=%s", idx, msg)
                for idx, msg in enumerate(msgs[-3:]):
                    LOGGER.debug("SAIMemory tail[%d]=%s", idx, msg)
                return msgs

        selected: List[Dict[str, str]] = []
        count = 0
        for msg in reversed(self.messages):
            count += len(msg.get("content", ""))
            if count > max_chars:
                break
            selected.append(msg)
        return list(reversed(selected))

    def get_recent_history_by_count(
        self,
        max_messages: int,
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Retrieves recent messages from persona history up to a message count limit."""
        if self.memory_adapter is not None:
            if not self.memory_adapter.is_ready():
                LOGGER.debug("SAIMemory adapter not ready for %s; falling back to in-memory", self.persona_id)
            else:
                LOGGER.debug(
                    "Fetching recent persona history from SAIMemory for %s (max_messages=%d)",
                    self.persona_id,
                    max_messages,
                )
                msgs = self.memory_adapter.recent_persona_messages_by_count(
                    max_messages,
                    required_tags=required_tags,
                    required_line_roles=required_line_roles,
                    required_scopes=required_scopes,
                    pulse_id=pulse_id,
                )
                LOGGER.debug(
                    "SAIMemory returned %d persona messages for %s",
                    len(msgs),
                    self.persona_id,
                )
                return msgs

        # Fallback to in-memory
        selected: List[Dict[str, str]] = []
        for msg in reversed(self.messages):
            selected.append(msg)
            if len(selected) >= max_messages:
                break
        return list(reversed(selected))

    def get_history_from_anchor(
        self,
        anchor_message_id: str,
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Retrieves messages from anchor onwards (for metabolism anchor-based retrieval)."""
        if self.memory_adapter is not None:
            if not self.memory_adapter.is_ready():
                LOGGER.debug("SAIMemory adapter not ready for %s; falling back to in-memory", self.persona_id)
            else:
                LOGGER.debug(
                    "Fetching persona history from anchor for %s (anchor=%s)",
                    self.persona_id,
                    anchor_message_id,
                )
                msgs = self.memory_adapter.persona_messages_from_anchor(
                    anchor_message_id,
                    required_tags=required_tags,
                    required_line_roles=required_line_roles,
                    required_scopes=required_scopes,
                    pulse_id=pulse_id,
                )
                LOGGER.debug(
                    "SAIMemory returned %d messages from anchor for %s",
                    len(msgs),
                    self.persona_id,
                )
                return msgs

        # Fallback: scan in-memory messages for anchor and return from there
        anchor_found = False
        selected: List[Dict[str, str]] = []
        for msg in self.messages:
            if not anchor_found:
                if msg.get("id") == anchor_message_id:
                    anchor_found = True
                else:
                    continue
            selected.append(msg)
        return selected

    def get_recent_history_balanced(
        self,
        max_chars: int,
        participant_ids: List[str],
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Retrieves recent messages balanced across conversation partners.

        Args:
            max_chars: Total character budget
            participant_ids: List of partner IDs to balance (e.g., ["user", "persona_b"])
            required_tags: Optional legacy tag filter (search/recall compat)
            required_line_roles: Line-role filter for context construction
            required_scopes: Scope filter for context construction
            pulse_id: Always include messages with this pulse ID

        Returns:
            List of messages balanced across participants
        """
        if self.memory_adapter is not None and self.memory_adapter.is_ready():
            LOGGER.debug(
                "Fetching balanced persona history from SAIMemory for %s (max_chars=%d, participants=%s)",
                self.persona_id,
                max_chars,
                participant_ids,
            )
            msgs = self.memory_adapter.recent_persona_messages_balanced(
                max_chars,
                participant_ids,
                required_tags=required_tags,
                required_line_roles=required_line_roles,
                required_scopes=required_scopes,
                pulse_id=pulse_id,
            )
            LOGGER.debug(
                "SAIMemory returned %d balanced messages for %s",
                len(msgs),
                self.persona_id,
            )
            return msgs

        # Fallback: just return recent messages without balancing
        return self.get_recent_history(
            max_chars,
            required_tags=required_tags,
            required_line_roles=required_line_roles,
            required_scopes=required_scopes,
            pulse_id=pulse_id,
        )

    def get_last_user_message(self) -> Optional[str]:
        if self.memory_adapter is not None:
            if not self.memory_adapter.is_ready():
                LOGGER.debug("SAIMemory adapter not ready when retrieving last user message for %s", self.persona_id)
            else:
                LOGGER.debug("Fetching last user message from SAIMemory for %s", self.persona_id)
                recent = self.memory_adapter.recent_persona_messages(self.memory_adapter.settings.summary_max_chars)
                for msg in reversed(recent):
                    if msg.get("role") == "user":
                        text = msg.get("content", "")
                        if text:
                            LOGGER.debug("Last user message from SAIMemory for %s found", self.persona_id)
                            return text
                LOGGER.debug("No user message found in SAIMemory for %s", self.persona_id)
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                text = (msg.get("content") or "").strip()
                if text:
                    return text
        return None

    def _render_message_for_self(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """intent §E 視点別レンダリング: occupancy event で entity_id ==
        self.persona_id なら content を一人称表現に rewrite する。 それ以外は
        msg をそのまま返す。

        フロントエンドが受け取る building_messages の content (= DB) は変えない
        (= 三人称のまま、 後方互換)。 本関数は AI に渡される
        building_recent_history 経路でのみ呼ぶ。 「自分が自分のことを三人称で
        語る違和感」 を消すための rewrite。
        """
        metadata = msg.get("metadata") or {}
        event = metadata.get("event") if isinstance(metadata, dict) else None
        if not isinstance(event, dict):
            return msg
        if event.get("type") != "occupancy":
            return msg
        if str(event.get("entity_id") or "") != self.persona_id:
            return msg
        # ここまで来たら occupancy event で 「自分自身」 の移動
        action = event.get("action", "")
        from_name = event.get("from_building_name") or "別の場所"
        to_name = event.get("to_building_name") or "別の場所"
        if action == "enter":
            new_content = (
                f'<div class="note-box" data-entity-id="{self.persona_id}">'
                f'🚶 自分:<br><b>{from_name}から{to_name}へ入室した</b></div>'
            )
        elif action == "leave":
            new_content = (
                f'<div class="note-box" data-entity-id="{self.persona_id}">'
                f'🚶 自分:<br><b>{from_name}から{to_name}へ移動した</b></div>'
            )
        else:
            return msg
        rewritten = dict(msg)
        rewritten["content"] = new_content
        return rewritten

    def get_building_recent_history(self, building_id: str, max_chars: int) -> List[Dict[str, str]]:
        """Retrieves recent messages from building DB up to a character limit.

        intent §E に従い、 自分自身の occupancy event は一人称に rewrite して
        AI に渡す。 三人称 → 一人称の置き換えは _render_message_for_self で行う。
        """
        from database.building_messages import fetch_building_messages
        # 全件取って末尾から char limit で削る (本格的な大量データでは要最適化)
        history = fetch_building_messages(self._db_session_factory, building_id)
        selected: List[Dict[str, Any]] = []
        count = 0
        for msg in reversed(history):
            content = msg.get("content", "")
            plain_content = re.sub('<[^<]+?>', '', content)
            count += len(plain_content)
            if count > max_chars:
                break
            selected.append(self._render_message_for_self(msg))
        return list(reversed(selected))

    def get_building_history(self, building_id: str) -> List[Dict[str, Any]]:
        """Building 内全メッセージを seq 昇順で返す (DB クエリ)。"""
        from database.building_messages import fetch_building_messages
        return fetch_building_messages(self._db_session_factory, building_id)

    def get_recent_entrant_events(
        self,
        building_id: str,
        *,
        lookback_messages: int = 10,
    ) -> List[Dict[str, str]]:
        """Get recent structured AI entrant events from building DB."""
        from database.building_messages import fetch_building_messages
        history = fetch_building_messages(
            self._db_session_factory, building_id, limit=lookback_messages
        )
        if not history:
            return []
        entrants: List[Dict[str, str]] = []
        seen: set = set()
        for msg in reversed(history):
            event = self._extract_occupancy_event(msg)
            if not event:
                continue
            if event.get("action") != "enter":
                continue
            if event.get("entity_type") != "ai":
                continue
            entity_id = str(event.get("entity_id") or "")
            event_key = str(event.get("event_key") or "")
            if not entity_id or not event_key:
                continue
            if event_key in seen:
                continue
            seen.add(event_key)
            entrants.append({"entity_id": entity_id, "event_key": event_key})
        return entrants

    def mark_entrant_event_recalled(self, building_id: str, event_key: str) -> bool:
        """Mark a structured entrant event as already recalled for this persona (DB)."""
        from database.building_messages import mark_event_recalled
        return mark_event_recalled(
            self._db_session_factory, building_id, event_key, self.persona_id
        )

    def should_recall_persona(
        self,
        target_persona_id: str,
        *,
        building_id: Optional[str] = None,
        event_key: Optional[str] = None,
        check_messages: int = 20,
    ) -> bool:
        """Check if we should recall past conversation with target persona.

        Returns True if the target persona has no messages in recent context
        AND the latest entrant event has not already been recalled.

        Args:
            target_persona_id: Persona to check for
            check_messages: Number of recent messages to check (default: 20)

        Returns:
            True if recall is needed (no messages from target in context,
                                    and no previous recall message found)
        """
        if building_id and event_key:
            from database.building_messages import fetch_building_messages
            history = fetch_building_messages(self._db_session_factory, building_id, limit=200)
            for msg in reversed(history):
                event = self._extract_occupancy_event(msg)
                if not event or event.get("event_key") != event_key:
                    continue
                recalled_by = event.get("recalled_by", [])
                if isinstance(recalled_by, list) and self.persona_id in recalled_by:
                    return False
                break

        recent = self.messages[-check_messages:] if len(self.messages) > check_messages else self.messages

        for msg in recent:
            metadata = msg.get("metadata")
            if isinstance(metadata, dict):
                audience = metadata.get("audience")
                if isinstance(audience, dict):
                    personas = audience.get("personas", [])
                    if isinstance(personas, list) and target_persona_id in personas:
                        return False

        for msg in recent:
            if msg.get("persona_id") == target_persona_id:
                return False

        return True

    def recall_conversation_with(
        self,
        target_persona_id: str,
        *,
        current_thread_only: bool = True,
        max_results: int = 6,
    ) -> Optional[str]:
        """Recall past conversation with a specific persona from SAIMemory.

        Returns formatted recall message with past conversations.

        Args:
            target_persona_id: Persona to recall conversation with
            max_results: Maximum number of conversation snippets to return (default: 6)

        Returns:
            Formatted recall message, or None if no conversations found
        """
        if not self.memory_adapter or not self.memory_adapter.is_ready():
            LOGGER.debug("SAIMemory not ready for recall")
            return None

        try:
            # Get recent message IDs to exclude duplicates
            recent_msg_ids = {msg.get("id") for msg in self.messages[-20:]}

            # Query SAIMemory for conversations with target persona
            messages = self.memory_adapter.get_messages_with_persona_in_audience(
                target_persona_id,
                exclude_message_ids=recent_msg_ids,
                required_tags=["conversation"],
                current_thread_only=current_thread_only,
                limit=max_results,
            )

            # Collect recall parts
            recall_parts: List[str] = []

            # Part 1: Past conversation snippets
            if messages:
                lines = [f"[想起: {target_persona_id}との過去の会話]"]
                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "").strip()
                    created_at = msg.get("created_at")
                    if created_at:
                        from datetime import datetime
                        dt = datetime.fromtimestamp(created_at)
                        timestamp = dt.strftime("%Y-%m-%d %H:%M")
                    else:
                        timestamp = "不明"

                    if content:
                        # Limit content to 2000 characters to preserve important info at the end
                        content_preview = content[:2000]
                        if len(content) > 2000:
                            content_preview += "..."
                        lines.append(f"- [{role}] @ {timestamp}: {content_preview}")

                recall_text = "\n".join(lines)
                recall_parts.append(recall_text)
                LOGGER.debug(
                    "Recalled %d messages with persona=%s", len(messages), target_persona_id
                )

            # Part 2: Memopedia page content
            try:
                from sai_memory.memopedia.storage import get_page_by_persona_id
                page = get_page_by_persona_id(self.memory_adapter.conn, target_persona_id)
                if page and page.content:
                    memopedia_section = f"[想起: {target_persona_id}についてのMemopedia記録]\n{page.content}"
                    recall_parts.append(memopedia_section)
                    LOGGER.debug(
                        "Added Memopedia content for persona=%s (%d chars)",
                        target_persona_id,
                        len(page.content)
                    )
            except Exception:
                LOGGER.exception("Failed to load Memopedia page for persona=%s", target_persona_id)

            if not recall_parts:
                LOGGER.debug(
                    "No past conversations or Memopedia entries found with persona=%s", target_persona_id
                )
                return None

            # Combine all recall parts
            return "\n\n".join(recall_parts)

        except Exception:
            LOGGER.exception("Failed to recall conversation with %s", target_persona_id)
            return None

    def ensure_persona_page(
        self,
        target_persona_id: str,
        persona_name: str,
    ) -> bool:
        """Ensure a Memopedia page exists for a specific persona.

        Creates a new page if one doesn't exist, or returns the existing page.
        The page will have persona_id in its metadata.

        Args:
            target_persona_id: Persona ID (e.g., "elis_city_a")
            persona_name: Persona display name (e.g., "エリス")

        Returns:
            True if page exists or was created, False otherwise
        """
        if not self.memory_adapter or not self.memory_adapter.is_ready():
            LOGGER.debug("SAIMemory not ready for persona page creation")
            return False

        try:
            from sai_memory.memopedia.storage import (
                get_page_by_persona_id,
                get_page_by_title,
                create_page,
                CATEGORY_PEOPLE,
            )

            # Check if page already exists with persona_id metadata
            existing_page = get_page_by_persona_id(self.memory_adapter.conn, target_persona_id)
            if existing_page:
                LOGGER.debug(
                    "Memopedia page already exists for persona=%s (page_id=%s)",
                    target_persona_id,
                    existing_page.id,
                )
                return True

            # Create new page for this persona
            from sai_memory.memopedia.storage import get_page
            root_people = get_page(self.memory_adapter.conn, "root_people")
            if not root_people:
                LOGGER.error("root_people page not found in Memopedia")
                return False

            existing_title_page = get_page_by_title(
                self.memory_adapter.conn,
                persona_name,
                category=CATEGORY_PEOPLE,
            )
            page_title = (
                f"{persona_name} ({target_persona_id})"
                if existing_title_page
                else persona_name
            )

            new_page = create_page(
                self.memory_adapter.conn,
                parent_id=root_people.id,
                title=page_title,
                summary=f"{persona_name}についての記録",
                content="",
                category=CATEGORY_PEOPLE,
                keywords=[persona_name],
                metadata={"persona_id": target_persona_id},
            )
            LOGGER.info(
                "Created Memopedia page %s for persona=%s (persona_name=%s)",
                new_page.id,
                target_persona_id,
                persona_name,
            )
            return True

        except Exception:
            LOGGER.exception(
                "Failed to ensure Memopedia page for persona=%s (persona_name=%s)",
                target_persona_id,
                persona_name,
            )
            return False

    def save_all(self) -> None:
        """Saves the persona's own log file (persona_log_path).

        **Note**: this method NO LONGER saves building histories. Building
        histories are saved at the manager level via
        ``manager._save_modified_buildings()`` which respects the modified
        set and quarantine state. The previous behavior — iterating ALL
        ``building_memory_paths`` and writing ``[]`` for missing keys —
        was the root cause of a 24-building data loss event (see
        docs/intent/building_log_safety.md).
        """
        self.persona_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.persona_log_path.write_text(
            json.dumps(self.messages, ensure_ascii=False), encoding="utf-8"
        )
