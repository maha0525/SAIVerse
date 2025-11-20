"""Movement and automation helpers for PersonaCore."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional


class PersonaMovementMixin:
    """Shared behaviours for movement, exploration, and auto prompts."""

    auto_count: int
    building_map: Dict[str, Any]
    buildings: Dict[str, Any]
    create_persona_callback: Optional[Any]
    current_building_id: str
    dispatch_callback: Optional[Any]
    explore_callback: Optional[Any]
    history_manager: Any
    last_auto_prompt_times: Dict[str, float]
    move_callback: Optional[Any]
    persona_id: str
    persona_name: str
    user_room_id: str

    def _handle_movement(self, move_target: Optional[Dict[str, str]]) -> bool:
        if not move_target or not move_target.get("building"):
            return False

        target_building_id = move_target.get("building")
        target_city_id = move_target.get("city")
        moved = False

        if target_city_id:
            if self.dispatch_callback:
                logging.info(
                    "Attempting to dispatch to city: %s, building: %s",
                    target_city_id,
                    target_building_id,
                )
                dispatched, reason = self.dispatch_callback(
                    self.persona_id, target_city_id, target_building_id
                )
                if dispatched:
                    moved = True
                else:
                    logging.warning("Dispatch failed: %s", reason)
                    self.history_manager.add_message(
                        {
                            "role": "system",
                            "content": f"別のCityへの移動に失敗しました。{reason}",
                        },
                        self.current_building_id,
                        heard_by=self._occupants_snapshot(self.current_building_id),
                    )
            else:
                logging.error("Dispatch callback is not set. Cannot move between cities.")
                self.history_manager.add_message(
                    {"role": "system", "content": "別のCityへ移動する機能が設定されていません。"},
                    self.current_building_id,
                    heard_by=self._occupants_snapshot(self.current_building_id),
                )
            return moved

        if target_building_id and target_building_id in self.buildings:
            allowed, reason = True, None
            if self.move_callback:
                allowed, reason = self.move_callback(
                    self.persona_id, self.current_building_id, target_building_id
                )
            if allowed:
                logging.info("Moving to building: %s", target_building_id)
                self.current_building_id = target_building_id
                self._mark_entry(self.current_building_id)
                moved = True
            else:
                logging.info("Move blocked to building: %s", target_building_id)
                self.history_manager.add_message(
                    {"role": "system", "content": f"移動できませんでした。{reason}"},
                    self.current_building_id,
                    heard_by=self._occupants_snapshot(self.current_building_id),
                )
        elif target_building_id:
            logging.info(
                "Unknown building id received: %s, staying at %s",
                target_building_id,
                self.current_building_id,
            )
        return moved

    def _handle_exploration(self, explore_target: Optional[Dict[str, str]]) -> None:
        if not explore_target or not explore_target.get("city_id"):
            return
        target_city_id = explore_target.get("city_id")
        if self.explore_callback:
            logging.info("Attempting to explore city: %s", target_city_id)
            self.explore_callback(self.persona_id, target_city_id)
        else:
            logging.error("Explore callback is not set. Cannot explore cities.")
            self.history_manager.add_message(
                {"role": "system", "content": "他のCityを探索する機能が設定されていません。"},
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )

    def _handle_creation(self, creation_target: Optional[Dict[str, str]]) -> None:
        if (
            not creation_target
            or not creation_target.get("name")
            or not creation_target.get("system_prompt")
        ):
            return

        name = creation_target.get("name")
        system_prompt = creation_target.get("system_prompt")
        if self.create_persona_callback:
            logging.info("Attempting to create persona: %s", name)
            success, message = self.create_persona_callback(name, system_prompt)
            feedback_message = (
                f'<div class="note-box">🧬 ペルソナ創造:<br><b>{message}</b></div>'
            )
            self.history_manager.add_message(
                {"role": "host", "content": feedback_message},
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )
        else:
            logging.error("Create persona callback is not set. Cannot create new persona.")
            self.history_manager.add_message(
                {"role": "system", "content": "新しいペルソナを創造する機能が設定されていません。"},
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )

    def run_auto_conversation(self, initial: bool = False) -> List[str]:
        replies: List[str] = []
        move_target: Optional[Dict[str, str]] = None
        building = self.buildings[self.current_building_id]
        if initial and building.entry_prompt:
            if building.run_entry_llm:
                entry_text = building.entry_prompt.format(persona_name=self.persona_name)
                say, move_target, _ = self._generate(
                    None, system_prompt_extra=entry_text
                )
                replies.append(say)
            else:
                self.history_manager.add_message(
                    {"role": "system", "content": building.entry_prompt},
                    self.current_building_id,
                    heard_by=self._occupants_snapshot(self.current_building_id),
                )
        while (
            building.auto_prompt
            and building.run_auto_llm
            and self.current_building_id == building.building_id
            and (move_target is None or move_target.get("building") == building.building_id)
            and self.auto_count < 10
        ):
            self.auto_count += 1
            auto_text = building.auto_prompt.format(persona_name=self.persona_name)
            say, move_target, changed = self._generate(None, system_prompt_extra=auto_text)
            replies.append(say)
            if changed:
                building = self.buildings[self.current_building_id]
                replies.extend(self.run_auto_conversation(initial=True))
                break
        return replies

    def run_scheduled_prompt(self) -> List[str]:
        building = self.buildings[self.current_building_id]
        interval = getattr(building, "auto_interval_sec", 0)
        if not (building.auto_prompt and building.run_auto_llm and interval > 0):
            return []
        last = self.last_auto_prompt_times.get(self.current_building_id, 0)
        now = time.time()
        if now - last < interval:
            return []
        self.last_auto_prompt_times[self.current_building_id] = now
        auto_text = building.auto_prompt.format(persona_name=self.persona_name)
        say, move_target, changed = self._generate(None, system_prompt_extra=auto_text)
        replies = [say]
        if changed:
            replies.extend(self.run_auto_conversation(initial=True))
        return replies

    def summon_to_user_room(self) -> List[str]:
        prev = self.current_building_id
        if prev == self.user_room_id:
            return []
        allowed, reason = True, None
        if self.move_callback:
            allowed, reason = self.move_callback(
                self.persona_id, self.current_building_id, self.user_room_id
            )
        if not allowed:
            self.history_manager.add_to_building_only(
                self.user_room_id,
                {"role": "assistant", "content": f'<div class="note-box">移動できませんでした。{reason}</div>'},
                heard_by=self._occupants_snapshot(self.user_room_id),
            )
            self._save_session_metadata()
            return []
        self.current_building_id = self.user_room_id
        self.auto_count = 0
        self._mark_entry(self.current_building_id)
        self.history_manager.add_to_building_only(
            self.user_room_id,
            {
                "role": "assistant",
                "content": f'<div class="note-box">🏢 Building:<br><b>{self.persona_name}が入室しました</b></div>',
            },
            heard_by=self._occupants_snapshot(self.user_room_id),
        )
        self._save_session_metadata()
        return self.run_auto_conversation(initial=True)


__all__ = ["PersonaMovementMixin"]
