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

        self._normalise_building_histories()

    def set_memory_adapter(self, adapter: Optional["SAIMemoryAdapter"]) -> None:
        self.memory_adapter = adapter

    def _ensure_size_limit(self, log_list: List[Dict[str, str]], path: Path) -> None:
        while log_list and len(json.dumps(log_list, ensure_ascii=False).encode("utf-8")) > 2000 * 1024:
            removed = log_list.pop(0)
            self._append_to_old_log(path.parent, [removed])

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
        next_candidate = self._building_seq_counter.get(building_id, 1)
        if isinstance(seq_value, int):
            seq = seq_value
        else:
            try:
                seq = int(seq_value)
            except (TypeError, ValueError):
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
        return enriched

    def _sync_to_memory(self, *, channel: str, building_id: Optional[str], message: Dict[str, str]) -> None:
        if self.memory_adapter is None or not self.memory_adapter.is_ready():
            return
        if (message.get("role") or "").lower() == "system":
            return
        try:
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
    ) -> None:
        """Adds a message to both persona and building history."""
        prepared_msg = self._prepare_message(msg)
        # Add to persona history and trim by size
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)
        self._sync_to_memory(channel="persona", building_id=None, message=prepared_msg)

        # Add to building history and trim
        hist = self.building_histories.setdefault(building_id, [])
        building_msg = self._decorate_building_message(building_id, prepared_msg, heard_by)
        hist.append(building_msg)
        self._ensure_size_limit(hist, self.building_memory_paths[building_id])

    def add_to_building_only(
        self,
        building_id: str,
        msg: Dict[str, str],
        *,
        heard_by: Optional[List[str]] = None,
    ) -> None:
        """Adds a message only to a specific building's history."""
        prepared_msg = self._prepare_message(msg)
        hist = self.building_histories.setdefault(building_id, [])
        building_msg = self._decorate_building_message(building_id, prepared_msg, heard_by)
        hist.append(building_msg)
        self._ensure_size_limit(hist, self.building_memory_paths[building_id])

    def add_to_persona_only(self, msg: Dict[str, str]) -> None:
        """Adds a message only to the persona's main history."""
        prepared_msg = self._prepare_message(msg)
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)
        self._sync_to_memory(channel="persona", building_id=None, message=prepared_msg)

    def get_recent_history(self, max_chars: int) -> List[Dict[str, str]]:
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
                msgs = self.memory_adapter.recent_persona_messages(max_chars)
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
