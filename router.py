import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

from buildings import Building
from buildings.user_room import load as load_user_room
from buildings.deep_think_room import load as load_deep_think_room


def build_router(persona_id: str = "air") -> "Router":
    buildings = [load_user_room(), load_deep_think_room()]
    base = Path("ai_sessions") / persona_id
    return Router(
        buildings=buildings,
        common_prompt_path=Path("system_prompts/common.txt"),
        persona_base=base,
    )


class Router:
    def __init__(self, buildings: List[Building], common_prompt_path: Path, persona_base: Path):
        self.buildings: Dict[str, Building] = {b.building_id: b for b in buildings}
        self.common_prompt = common_prompt_path.read_text(encoding="utf-8")
        self.persona_base = persona_base
        self.memory_path = persona_base / "memory.json"
        self.persona_system_instruction = (persona_base / "system_prompt.txt").read_text(encoding="utf-8")
        persona_data = json.loads((persona_base / "base.json").read_text(encoding="utf-8"))
        self.persona_name = persona_data.get("persona_name", "AI")
        self.building_memory_paths: Dict[str, Path] = {b_id: persona_base / "buildings" / b_id / "memory.json" for b_id in self.buildings}
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
            self.buildings[b_id].memory_path = path
            self.buildings[b_id].memory = self.building_histories[b_id]
        self.current_building_id = "user_room"
        # 会話履歴を保持する
        self.messages: List[Dict[str, str]] = []
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY environment variable is not set. "
                "Please set it to your OpenAI API key."
            )
        self.client = OpenAI(api_key=api_key)
        self.auto_count = 0  # consecutive auto prompts in deep_think_room
        self._load_session()

    def _add_to_history(self, msg: Dict[str, str], building_id: Optional[str] = None) -> None:
        """Append a message to history and trim to 120000 characters."""
        self.messages.append(msg)
        b_id = building_id or self.current_building_id
        hist = self.building_histories.setdefault(b_id, [])
        hist.append(msg)
        total_b = sum(len(m.get("content", "")) for m in hist)
        while total_b > 120000 and hist:
            removed = hist.pop(0)
            total_b -= len(removed.get("content", ""))
        total = sum(len(m.get("content", "")) for m in self.messages)
        while total > 120000 and self.messages:
            removed = self.messages.pop(0)
            total -= len(removed.get("content", ""))

    def _recent_history(self, max_chars: int) -> List[Dict[str, str]]:
        selected = []
        count = 0
        for msg in reversed(self.messages):
            count += len(msg.get("content", ""))
            if count > max_chars:
                break
            selected.append(msg)
        return list(reversed(selected))

    def _load_session(self) -> None:
        if self.memory_path.exists():
            try:
                data = json.loads(self.memory_path.read_text(encoding="utf-8"))
                self.current_building_id = data.get("current_building_id", "user_room")
                self.messages = data.get("messages", [])
                self.auto_count = data.get("auto_count", 0)
            except json.JSONDecodeError:
                logging.warning("Failed to load session memory, starting fresh")
        for b_id, path in self.building_memory_paths.items():
            if path.exists():
                try:
                    self.building_histories[b_id] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logging.warning("Failed to load building history %s", b_id)

    def _save_session(self) -> None:
        data = {
            "current_building_id": self.current_building_id,
            "messages": self.messages,
            "auto_count": self.auto_count,
        }
        self.memory_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for b_id, path in self.building_memory_paths.items():
            hist = self.building_histories.get(b_id, [])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

    def _build_messages(self, user_message: Optional[str]) -> List[Dict[str, str]]:
        building = self.buildings[self.current_building_id]
        current_time = datetime.now().strftime("%H:%M")
        system_text = self.common_prompt.format(
            current_building_name=building.name,
            current_building_system_instruction=building.system_instruction.format(current_time=current_time),
            current_persona_name=self.persona_name,
            current_persona_system_instruction=self.persona_system_instruction,
            current_time=current_time,
        )

        auto_prompt = building.auto_prompt

        base_chars = len(system_text) + len(auto_prompt)
        if user_message:
            base_chars += len(user_message)

        history_limit = 120000 - base_chars
        if history_limit < 0:
            history_limit = 0

        history_msgs = self._recent_history(history_limit)

        msgs = [{"role": "system", "content": system_text}] + history_msgs
        msgs.append({"role": "system", "content": auto_prompt})
        if user_message:
            msgs.append({"role": "user", "content": user_message})
        return msgs

    def _generate(self, user_message: Optional[str]) -> tuple[str, Optional[str], bool]:
        msgs = self._build_messages(user_message)
        logging.debug("Messages sent to API: %s", msgs)
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=msgs,
            )
            content = response.choices[0].message.content
        except Exception as e:
            logging.error("OpenAI API call failed: %s", e)
            content = "{\"say\": \"エラーが発生しました。\"}"
        logging.debug("Raw response: %s", content)
        say, next_id = self._parse_response(content)
        building = self.buildings[self.current_building_id]
        self._add_to_history({"role": "user", "content": building.auto_prompt}, building_id=self.current_building_id)
        if user_message:
            self._add_to_history({"role": "user", "content": user_message}, building_id=self.current_building_id)
        self._add_to_history({"role": "assistant", "content": say}, building_id=self.current_building_id)
        prev_id = self.current_building_id
        if next_id and next_id in self.buildings:
            logging.info("Moving to building: %s", next_id)
            self.current_building_id = next_id
        else:
            if next_id and next_id not in self.buildings:
                logging.info(
                    "Unknown building id received: %s, staying at %s",
                    next_id,
                    self.current_building_id,
                )
            elif next_id is None:
                logging.debug(
                    "No next_building_id provided, staying at %s", self.current_building_id
                )
        changed = self.current_building_id != prev_id
        if changed:
            self.auto_count = 0
        self._save_session()
        return say, next_id, changed

    def run_auto_conversation(self, initial: bool = False) -> List[str]:
        replies: List[str] = []
        next_id: Optional[str] = None
        if initial:
            say, next_id, _ = self._generate(None)
            replies.append(say)
        while (
            self.current_building_id == "deep_think_room"
            and (next_id is None or next_id == "deep_think_room")
            and self.auto_count < 10
        ):
            self.auto_count += 1
            say, next_id, changed = self._generate(None)
            replies.append(say)
            if changed and self.current_building_id != "deep_think_room":
                if self.current_building_id in ("user_room", "deep_think_room"):
                    replies.extend(self.run_auto_conversation(initial=True))
                break
        return replies

    def handle_user_input(self, message: str) -> List[str]:
        logging.info("User input: %s", message)
        if self.current_building_id != "user_room":
            logging.info("User input ignored outside user_room")
            message = ""
        say, next_id, changed = self._generate(message)
        replies = [say]
        if changed and self.current_building_id in ("user_room", "deep_think_room"):
            replies.extend(self.run_auto_conversation(initial=True))
        elif self.current_building_id == "deep_think_room" and (next_id is None or next_id == "deep_think_room"):
            replies.extend(self.run_auto_conversation(initial=False))
        return replies

    def get_building_history(self, building_id: str) -> List[Dict[str, str]]:
        return self.building_histories.get(building_id, [])

    @staticmethod
    def _parse_response(content: str) -> tuple[str, Optional[str]]:
        try:
            data = json.loads(content)
            say = data.get("say", "")
            next_id = data.get("next_building_id")
            logging.debug(
                "Parsed JSON response - say: %s, next_building_id: %s", say, next_id
            )
            return say, next_id
        except json.JSONDecodeError:
            logging.warning("Failed to parse response as JSON: %s", content)
            return content, None

