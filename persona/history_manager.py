import copy
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING, Any
import re
from datetime import datetime

if TYPE_CHECKING:
    from saiverse_memory import SAIMemoryAdapter

LOGGER = logging.getLogger(__name__)

class HistoryManager:
    def __init__(
        self, 
        persona_id: str,
        persona_log_path: Path, 
        building_memory_paths: Dict[str, Path],
        initial_persona_history: Optional[List[Dict[str, str]]] = None,
        initial_building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
        memory_adapter: Optional["SAIMemoryAdapter"] = None,
    ):
        self.persona_id = persona_id
        self.persona_log_path = persona_log_path
        self.building_memory_paths = building_memory_paths
        self.messages = initial_persona_history if initial_persona_history is not None else []
        self.building_histories = initial_building_histories if initial_building_histories is not None else {}
        self.memory_adapter = memory_adapter
        self._building_seq_counter: Dict[str, int] = {}
        self.metabolism_anchor_message_id: Optional[str] = None

        self._normalise_building_histories()

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

    def _prepare_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures a message has a timestamp and persona_id if applicable."""
        new_msg = msg.copy()
        if "timestamp" not in new_msg:
            new_msg["timestamp"] = datetime.now().isoformat()
        if new_msg.get("role") == "assistant" and "persona_id" not in new_msg:
            new_msg["persona_id"] = self.persona_id
        metadata = new_msg.get("metadata")
        if metadata is not None:
            if isinstance(metadata, dict):
                new_msg["metadata"] = copy.deepcopy(metadata)
            else:
                LOGGER.warning("Discarding metadata with invalid type %s", type(metadata).__name__)
                new_msg.pop("metadata", None)
        return new_msg

    def _normalise_building_histories(self) -> None:
        for b_id, path in self.building_memory_paths.items():
            hist = self.building_histories.setdefault(b_id, [])
            max_seq = 0
            for idx, msg in enumerate(hist):
                seq_value = msg.get("seq")
                seq: int
                if isinstance(seq_value, int):
                    seq = seq_value
                else:
                    try:
                        seq = int(seq_value)
                    except (TypeError, ValueError):
                        LOGGER.debug("Failed to parse seq value %r, defaulting to %d", seq_value, idx + 1)
                        seq = idx + 1
                msg["seq"] = seq
                if not msg.get("message_id"):
                    msg["message_id"] = msg.get("id") or f"{b_id}:{seq}"
                heard_raw = msg.get("heard_by")
                if isinstance(heard_raw, list):
                    heard_candidates = [str(p) for p in heard_raw if p]
                elif heard_raw is None:
                    heard_candidates = []
                else:
                    heard_candidates = [str(heard_raw)]
                deduped: List[str] = []
                for pid in heard_candidates:
                    if pid not in deduped:
                        deduped.append(pid)
                msg["heard_by"] = sorted(deduped)
                ingested_raw = msg.get("ingested_by")
                if isinstance(ingested_raw, list):
                    msg["ingested_by"] = sorted({str(pid) for pid in ingested_raw if pid})
                else:
                    msg["ingested_by"] = []
                max_seq = max(max_seq, seq)
            self._building_seq_counter[b_id] = max_seq + 1
        for b_id in self.building_memory_paths.keys():
            self._building_seq_counter.setdefault(b_id, 1)
            self.building_histories.setdefault(b_id, [])

    def _decorate_building_message(
        self,
        building_id: str,
        msg: Dict[str, str],
        heard_by: Optional[List[str]],
    ) -> Dict[str, str]:
        enriched = msg.copy()
        seq_value = enriched.get("seq")
        # building_histories は全ペルソナ共有なので、実際の末尾 seq から次候補を導出する。
        # ペルソナ固有カウンターだけを使うと他ペルソナのメッセージと seq が衝突する。
        hist = self.building_histories.get(building_id)
        if hist:
            last_seq = int(hist[-1].get("seq", 0))
            next_candidate = max(self._building_seq_counter.get(building_id, 1), last_seq + 1)
        else:
            next_candidate = self._building_seq_counter.get(building_id, 1)
        if isinstance(seq_value, int):
            seq = seq_value
        else:
            try:
                seq = int(seq_value)
            except (TypeError, ValueError):
                LOGGER.debug("Failed to parse seq value %r, defaulting to %d", seq_value, next_candidate)
                seq = next_candidate
        if seq_value is None:
            seq = next_candidate
        if seq < 1:
            seq = next_candidate
        self._building_seq_counter[building_id] = max(next_candidate, seq + 1)
        enriched["seq"] = seq
        if not enriched.get("message_id"):
            enriched["message_id"] = msg.get("id") or f"{building_id}:{seq}"
        heard_set = {str(pid) for pid in (heard_by or []) if pid}
        enriched["heard_by"] = sorted(heard_set)
        ingested_raw = enriched.get("ingested_by")
        if isinstance(ingested_raw, list):
            ingested_set = {str(pid) for pid in ingested_raw if pid}
            enriched["ingested_by"] = sorted(ingested_set)
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
    ) -> Dict[str, Any]:
        """Adds a message to both persona and building history.

        Returns the saved building message dict (including confirmed message_id).
        Callers can use the returned message_id to associate addon metadata.
        """
        prepared_msg = self._prepare_message(msg)

        # Add audience metadata for SAIMemory
        if heard_by:
            metadata = prepared_msg.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["audience"] = {
                    "personas": [pid for pid in heard_by if not pid.startswith("user_")],
                    "users": [pid for pid in heard_by if pid.startswith("user_")]
                }

        # Add to persona history and trim by size
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)
        self._sync_to_memory(channel="persona", building_id=None, message=prepared_msg)

        # Add to building history and trim
        hist = self.building_histories.setdefault(building_id, [])
        building_msg = self._decorate_building_message(building_id, prepared_msg, heard_by)
        hist.append(building_msg)
        self._ensure_size_limit(hist, self._get_building_memory_path(building_id))
        return building_msg

    def _get_building_memory_path(self, building_id: str) -> Path:
        path = self.building_memory_paths.get(building_id)
        if path is None:
            raise ValueError(
                f"Unknown building_id '{building_id}'. Expected one of: {sorted(self.building_memory_paths.keys())}"
            )
        return path

    def add_to_building_only(
        self,
        building_id: str,
        msg: Dict[str, str],
        *,
        heard_by: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Adds a message only to a specific building's history.

        building_id must be the canonical building ID present in building_memory_paths.
        Returns the saved building message dict (including confirmed message_id) so
        callers can associate addon metadata (same contract as add_message).
        """
        prepared_msg = self._prepare_message(msg)
        hist = self.building_histories.setdefault(building_id, [])
        building_msg = self._decorate_building_message(building_id, prepared_msg, heard_by)
        hist.append(building_msg)
        self._ensure_size_limit(hist, self._get_building_memory_path(building_id))
        return building_msg

    def add_to_persona_only(self, msg: Dict[str, str]) -> None:
        """Adds a message only to the persona's main history."""
        prepared_msg = self._prepare_message(msg)
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)
        self._sync_to_memory(channel="persona", building_id=None, message=prepared_msg)

    def get_recent_history(
        self,
        max_chars: int,
        *,
        required_tags: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
        exclude_pulse_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Retrieves recent messages from persona history up to a character limit."""
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
                    pulse_id=pulse_id,
                    exclude_pulse_id=exclude_pulse_id,
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
        pulse_id: Optional[str] = None,
        exclude_pulse_id: Optional[str] = None,
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
                    pulse_id=pulse_id,
                    exclude_pulse_id=exclude_pulse_id,
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
        pulse_id: Optional[str] = None,
        exclude_pulse_id: Optional[str] = None,
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
                    pulse_id=pulse_id,
                    exclude_pulse_id=exclude_pulse_id,
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
        pulse_id: Optional[str] = None,
        exclude_pulse_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Retrieves recent messages balanced across conversation partners.

        Args:
            max_chars: Total character budget
            participant_ids: List of partner IDs to balance (e.g., ["user", "persona_b"])
            required_tags: Only include messages with these tags
            pulse_id: Always include messages with this pulse ID
            exclude_pulse_id: Exclude messages with this pulse ID

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
                pulse_id=pulse_id,
                exclude_pulse_id=exclude_pulse_id,
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
            pulse_id=pulse_id,
            exclude_pulse_id=exclude_pulse_id,
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

    def get_building_recent_history(self, building_id: str, max_chars: int) -> List[Dict[str, str]]:
        """Retrieves recent messages from a specific building's history up to a character limit."""
        history = self.building_histories.get(building_id, [])
        selected = []
        count = 0
        for msg in reversed(history):
            # HTMLタグを含む可能性があるため、簡易的に除去して文字数をカウント
            content = msg.get("content", "")
            plain_content = re.sub('<[^<]+?>', '', content)
            count += len(plain_content)
            if count > max_chars:
                break
            selected.append(msg)
        return list(reversed(selected))

    def get_recent_entrants(
        self,
        building_id: str,
        *,
        lookback_messages: int = 10,
    ) -> List[str]:
        """Get persona IDs who recently entered the building.

        Parses building history for entrance events and extracts persona IDs
        from data-entity-id attributes.

        Args:
            building_id: Building to check
            lookback_messages: Number of recent messages to check (default: 10)

        Returns:
            List of persona IDs who recently entered (unique, in reverse chronological order)
        """
        history = self.building_histories.get(building_id, [])
        if not history:
            return []

        entrants: List[str] = []
        seen: set = set()

        # Check recent messages for entrance events
        for msg in reversed(history[-lookback_messages:]):
            content = msg.get("content", "")
            if not content:
                continue

            # Parse data-entity-id from HTML
            import re
            match = re.search(r'data-entity-id="([^"]+)"', content)
            if match:
                entity_id = match.group(1)
                # Filter for AI personas (exclude users)
                if entity_id and not entity_id.startswith("user_"):
                    if entity_id not in seen:
                        seen.add(entity_id)
                        entrants.append(entity_id)

        return entrants

    def should_recall_persona(
        self,
        target_persona_id: str,
        *,
        check_messages: int = 20,
    ) -> bool:
        """Check if we should recall past conversation with target persona.

        Returns True if the target persona has no messages in recent context
        AND we haven't already recalled conversation with them.

        Args:
            target_persona_id: Persona to check for
            check_messages: Number of recent messages to check (default: 20)

        Returns:
            True if recall is needed (no messages from target in context,
                                    and no previous recall message found)
        """
        # Get recent messages from persona history
        recent = self.messages[-check_messages:] if len(self.messages) > check_messages else self.messages

        # Check if we already have a recall message for this persona
        recall_header = f"[想起: {target_persona_id}との過去の会話]"
        for msg in recent:
            content = msg.get("content", "")
            if recall_header in content:
                return False  # Already recalled, no need to recall again

        # Check if any message has the target persona in audience
        for msg in recent:
            metadata = msg.get("metadata")
            if isinstance(metadata, dict):
                audience = metadata.get("audience")
                if isinstance(audience, dict):
                    personas = audience.get("personas", [])
                    if isinstance(personas, list) and target_persona_id in personas:
                        return False  # Found target persona in context, no recall needed

        # Also check if any message is FROM the target persona
        for msg in recent:
            if msg.get("persona_id") == target_persona_id:
                return False  # Found message from target, no recall needed

        return True  # Target persona not found in context, recall needed

    def recall_conversation_with(
        self,
        target_persona_id: str,
        *,
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
            if self.memory_adapter.memopedia_adapter:
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
                update_page,
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

            # Check if page with same title exists in people category
            title_page = get_page_by_title(
                self.memory_adapter.conn,
                persona_name,
                category=CATEGORY_PEOPLE,
            )

            if title_page:
                # Update existing page with persona_id metadata
                update_page(
                    self.memory_adapter.conn,
                    title_page.id,
                    metadata={"persona_id": target_persona_id},
                )
                LOGGER.info(
                    "Updated Memopedia page %s with persona_id=%s",
                    title_page.id,
                    target_persona_id,
                )
                return True

            # Create new page for this persona
            from sai_memory.memopedia.storage import get_page
            root_people = get_page(self.memory_adapter.conn, "root_people")
            if not root_people:
                LOGGER.error("root_people page not found in Memopedia")
                return False

            new_page = create_page(
                self.memory_adapter.conn,
                parent_id=root_people.id,
                title=persona_name,
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
        """Saves all persona and building histories to their respective files."""
        self.persona_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.persona_log_path.write_text(
            json.dumps(self.messages, ensure_ascii=False), encoding="utf-8"
        )
        for b_id, path in self.building_memory_paths.items():
            hist = self.building_histories.get(b_id, [])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
