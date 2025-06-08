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


def build_router() -> "Router":
    buildings = [load_user_room(), load_deep_think_room()]
    return Router(
        buildings=buildings,
        common_prompt_path=Path("system_prompts/common.txt"),
        memory_path=Path("ai_sessions/memory.json"),
    )


class Router:
    def __init__(self, buildings: List[Building], common_prompt_path: Path, memory_path: Path):
        self.buildings: Dict[str, Building] = {b.building_id: b for b in buildings}
        self.common_prompt = common_prompt_path.read_text(encoding="utf-8")
        self.memory_path = memory_path
        self.current_building_id = "user_room"
        self.messages: List[Dict[str, str]] = []
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY environment variable is not set. "
                "Please set it to your OpenAI API key."
            )
        self.client = OpenAI(api_key=api_key)
        self._load_session()

    def _load_session(self) -> None:
        if self.memory_path.exists():
            try:
                data = json.loads(self.memory_path.read_text(encoding="utf-8"))
                self.current_building_id = data.get("current_building_id", "user_room")
            except json.JSONDecodeError:
                logging.warning("Failed to load session memory, starting fresh")

    def _save_session(self) -> None:
        data = {"current_building_id": self.current_building_id}
        self.memory_path.write_text(json.dumps(data), encoding="utf-8")

    def _build_messages(self, user_message: Optional[str]) -> List[Dict[str, str]]:
        building = self.buildings[self.current_building_id]
        current_time = datetime.now().strftime("%H:%M")
        system_text = self.common_prompt.format(
            current_building_name=building.name,
            current_building_system_instruction=building.system_instruction.format(current_time=current_time),
            current_persona_name="AI",
            current_persona_system_instruction="",
            current_time=current_time,
        )
        msgs = [
            {"role": "system", "content": system_text},
            {"role": "system", "content": building.auto_prompt},
        ]
        if user_message:
            msgs.append({"role": "user", "content": user_message})
        return msgs

    def handle_user_input(self, message: str) -> str:
        logging.info("User input: %s", message)
        if self.current_building_id != "user_room":
            logging.info("User input ignored outside user_room")
            message = ""
        msgs = self._build_messages(message)
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
        if next_id and next_id in self.buildings:
            logging.info("Moving to building: %s", next_id)
            self.current_building_id = next_id
        self._save_session()
        return say

    @staticmethod
    def _parse_response(content: str) -> tuple[str, Optional[str]]:
        try:
            data = json.loads(content)
            say = data.get("say", "")
            next_id = data.get("next_building_id")
            return say, next_id
        except json.JSONDecodeError:
            return content, None

