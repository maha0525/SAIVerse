import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
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

    def _append_to_old_log(self, base_dir: Path, msgs: List[Dict[str, str]]) -> None:
        """Append messages to a rotating log under base_dir/old_log."""
        old_dir = base_dir / "old_log"
        old_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(old_dir.glob("*.json"))
        target = files[-1] if files else None
        if target is None or target.stat().st_size > 100 * 1024:
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

    def add_message(self, msg: Dict[str, str], building_id: str) -> None:
        """Adds a message to both persona and building history."""
        if msg.get("role") == "assistant" and "persona_id" not in msg:
            msg["persona_id"] = self.persona_id
        
        # Add to persona history and trim
        self.messages.append(msg)
        total = sum(len(m.get("content", "")) for m in self.messages)
        while total > 120000 and self.messages:
            removed = self.messages.pop(0)
            self._append_to_old_log(self.persona_log_path.parent, [removed])
            total -= len(removed.get("content", ""))

        # Add to building history and trim
        hist = self.building_histories.setdefault(building_id, [])
        hist.append(msg)
        total_b = sum(len(m.get("content", "")) for m in hist)
        while total_b > 120000 and hist:
            removed = hist.pop(0)
            self._append_to_old_log(self.building_memory_paths[building_id].parent, [removed])
            total_b -= len(removed.get("content", ""))

    def add_to_building_only(self, building_id: str, msg: Dict[str, str]) -> None:
        """Adds a message only to a specific building's history."""
        hist = self.building_histories.setdefault(building_id, [])
        hist.append(msg)
        total_b = sum(len(m.get("content", "")) for m in hist)
        while total_b > 120000 and hist:
            removed = hist.pop(0)
            self._append_to_old_log(self.building_memory_paths[building_id].parent, [removed])
            total_b -= len(removed.get("content", ""))

    def add_to_persona_only(self, msg: Dict[str, str]) -> None:
        """Adds a message only to the persona's main history."""
        if msg.get("role") == "assistant" and "persona_id" not in msg:
            msg["persona_id"] = self.persona_id
        self.messages.append(msg)
        total = sum(len(m.get("content", "")) for m in self.messages)
        while total > 120000 and self.messages:
            removed = self.messages.pop(0)
            self._append_to_old_log(self.persona_log_path.parent, [removed])
            total -= len(removed.get("content", ""))

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
