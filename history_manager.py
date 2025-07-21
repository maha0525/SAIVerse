import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
import re
from datetime import datetime

class HistoryManager:
    def __init__(
        self, 
        persona_id: str,
        persona_log_path: Path, 
        building_memory_paths: Dict[str, Path],
        initial_persona_history: Optional[List[Dict[str, str]]] = None,
        initial_building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
    ):
        self.persona_id = persona_id
        self.persona_log_path = persona_log_path
        self.building_memory_paths = building_memory_paths
        self.messages = initial_persona_history if initial_persona_history is not None else []
        self.building_histories = initial_building_histories if initial_building_histories is not None else {}

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

    def _prepare_message(self, msg: Dict[str, str]) -> Dict[str, str]:
        """Ensures a message has a timestamp and persona_id if applicable."""
        new_msg = msg.copy()
        if "timestamp" not in new_msg:
            new_msg["timestamp"] = datetime.now().isoformat()
        if new_msg.get("role") == "assistant" and "persona_id" not in new_msg:
            new_msg["persona_id"] = self.persona_id
        return new_msg

    def add_message(self, msg: Dict[str, str], building_id: str) -> None:
        """Adds a message to both persona and building history."""
        prepared_msg = self._prepare_message(msg)
        # Add to persona history and trim by size
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)

        # Add to building history and trim
        hist = self.building_histories.setdefault(building_id, [])
        hist.append(prepared_msg)
        self._ensure_size_limit(hist, self.building_memory_paths[building_id])

    def add_to_building_only(self, building_id: str, msg: Dict[str, str]) -> None:
        """Adds a message only to a specific building's history."""
        prepared_msg = self._prepare_message(msg)
        hist = self.building_histories.setdefault(building_id, [])
        hist.append(prepared_msg)
        self._ensure_size_limit(hist, self.building_memory_paths[building_id])

    def add_to_persona_only(self, msg: Dict[str, str]) -> None:
        """Adds a message only to the persona's main history."""
        prepared_msg = self._prepare_message(msg)
        self.messages.append(prepared_msg)
        self._ensure_size_limit(self.messages, self.persona_log_path)

    def get_recent_history(self, max_chars: int) -> List[Dict[str, str]]:
        """Retrieves recent messages from persona history up to a character limit."""
        selected = []
        count = 0
        for msg in reversed(self.messages):
            count += len(msg.get("content", ""))
            if count > max_chars:
                break
            selected.append(msg)
        return list(reversed(selected))

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
