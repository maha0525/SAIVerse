import json
import logging
import os
import time
import copy
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Iterator
from datetime import datetime

from dotenv import load_dotenv

from buildings import Building
from buildings.user_room import load as load_user_room
from buildings.deep_think_room import load as load_deep_think_room
from buildings.air_room import load as load_air_room
from buildings.eris_room import load as load_eris_room
from buildings.const_test_room import load as load_const_test_room

from llm_clients import get_llm_client, LLMClient
from action_handler import ActionHandler
from history_manager import HistoryManager
from emotion_module import EmotionControlModule
from database.api_server import (
    SessionLocal, AI as AIModel
)

load_dotenv()


def build_router(persona_id: str = "air", model: str = "gpt-4o", context_length: int = 120000, provider: str = "ollama") -> "Router":
    buildings = [
        load_user_room(),
        load_deep_think_room(),
        load_air_room(),
        load_eris_room(),
        load_const_test_room(),
    ]
    base = Path("ai_sessions") / persona_id
    return Router(
        buildings=buildings,
        common_prompt_path=Path("system_prompts/common.txt"),
        persona_base=base,
        emotion_prompt_path=Path("system_prompts/emotion_parameter.txt"),
        action_priority_path=Path("action_priority.json"),
        model=model,
        context_length=context_length,
        provider=provider,
    )


class Router:
    def __init__(
        self,
        persona_id: str,
        persona_name: str,
        persona_system_instruction: str,
        avatar_image: Optional[str],
        buildings: List[Building],
        common_prompt_path: Path,
        emotion_prompt_path: Path = Path("system_prompts/emotion_parameter.txt"),
        action_priority_path: Path = Path("action_priority.json"),
        building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
        occupants: Optional[Dict[str, List[str]]] = None,
        id_to_name_map: Optional[Dict[str, str]] = None,
        move_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        start_building_id: str = "air_room",
        model: str = "gpt-4o",
        context_length: int = 120000,
        provider: str = "ollama",
    ):
        self.buildings: Dict[str, Building] = {b.building_id: b for b in buildings}
        self.common_prompt = common_prompt_path.read_text(encoding="utf-8")
        self.emotion_prompt = emotion_prompt_path.read_text(encoding="utf-8")
        self.persona_id = persona_id
        self.persona_name = persona_name
        self.persona_system_instruction = persona_system_instruction
        self.avatar_image = avatar_image
        self.saiverse_home = Path.home() / ".saiverse"
        self.persona_log_path = (
            self.saiverse_home / "personas" / self.persona_id / "log.json"
        )
        self.building_memory_paths: Dict[str, Path] = {
            b_id: self.saiverse_home / "buildings" / b_id / "log.json"
            for b_id in self.buildings
        }
        self.action_priority = self._load_action_priority(action_priority_path)
        self.action_handler = ActionHandler(self.action_priority)

        self.occupants = occupants if occupants is not None else {}
        self.id_to_name_map = id_to_name_map if id_to_name_map is not None else {}

        # Initialize stateful attributes with defaults before loading session
        self.current_building_id = start_building_id
        self.auto_count = 0
        self.last_auto_prompt_times: Dict[str, float] = {b_id: time.time() for b_id in self.buildings}
        self.emotion = {"stability": {"mean": 0, "variance": 1}, "affect": {"mean": 0, "variance": 1}, "resonance": {"mean": 0, "variance": 1}, "attitude": {"mean": 0, "variance": 1}}

        # Load session data, which may overwrite the defaults
        self._load_session_data()

        # Load persona history from file before initializing HistoryManager
        if self.persona_log_path.exists():
            try:
                self.messages = json.loads(self.persona_log_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logging.warning(f"Failed to load persona log for {self.persona_id}, starting empty")
                self.messages = []
        else:
            self.messages = []

        # Initialize managers that depend on loaded data
        self.history_manager = HistoryManager(
            persona_id=self.persona_id,
            persona_log_path=self.persona_log_path,
            building_memory_paths=self.building_memory_paths,
            initial_persona_history=self.messages,
            initial_building_histories=building_histories
        )

        # Initialize remaining attributes
        self.move_callback = move_callback
        self.model = model
        self.context_length = context_length
        self.llm_client = get_llm_client(model, provider, self.context_length)
        self.emotion_module = EmotionControlModule()

    def _load_action_priority(self, path: Path) -> Dict[str, int]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return {str(k): int(v) for k, v in data.items()}
            except Exception:
                logging.warning("Failed to load action priority from %s", path)
        return {"think": 1, "emotion_shift": 2, "move": 3}

    def _load_session_data(self) -> None:
        """ペルソナの動的な状態をDBから読み込む"""
        db = SessionLocal()
        try:
            db_ai = db.query(AIModel).filter(AIModel.AIID == self.persona_id).first()
            if not db_ai:
                logging.warning(f"No AI record found in DB for {self.persona_id}. Using default state.")
                return

            # auto_count
            self.auto_count = db_ai.AUTO_COUNT or 0

            # last_auto_prompt_times
            if db_ai.LAST_AUTO_PROMPT_TIMES:
                try:
                    self.last_auto_prompt_times.update(json.loads(db_ai.LAST_AUTO_PROMPT_TIMES))
                except json.JSONDecodeError:
                    logging.warning(f"Could not parse LAST_AUTO_PROMPT_TIMES from DB for {self.persona_name}.")
            
            # emotion
            if db_ai.EMOTION:
                try:
                    self.emotion = json.loads(db_ai.EMOTION)
                except json.JSONDecodeError:
                    logging.warning(f"Could not parse EMOTION from DB for {self.persona_name}.")
            
            logging.info(f"Loaded dynamic state from DB for {self.persona_name}.")

        except Exception as e:
            logging.error(f"Failed to load session data from DB for {self.persona_name}: {e}", exc_info=True)
        finally:
            db.close()

    def _save_session_metadata(self) -> None:
        """ペルソナの動的な状態をDBに保存する"""
        db = SessionLocal()
        try:
            update_data = {"EMOTION": json.dumps(self.emotion, ensure_ascii=False),"AUTO_COUNT": self.auto_count,"LAST_AUTO_PROMPT_TIMES": json.dumps(self.last_auto_prompt_times, ensure_ascii=False)}
            db.query(AIModel).filter(AIModel.AIID == self.persona_id).update(update_data)
            db.commit()
            logging.info(f"Saved dynamic state to DB for {self.persona_name}.")
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to save session data to DB for {self.persona_name}: {e}", exc_info=True)
        finally:
            db.close()
        self.history_manager.save_all()

    def set_model(self, model: str, context_length: int, provider: str) -> None:
        self.model = model
        self.context_length = context_length
        self.llm_client = get_llm_client(model, provider, context_length)

    def _build_messages(
        self, user_message: Optional[str], extra_system_prompt: Optional[str] = None
    ) -> List[Dict[str, str]]:
        building = self.buildings[self.current_building_id]
        current_time = datetime.now().strftime("%H:%M")
        system_text = self.common_prompt.format(
            current_building_name=building.name,
            current_building_system_instruction=building.system_instruction.format(current_time=current_time),
            current_persona_id=self.persona_id,
            current_persona_name=self.persona_name,
            current_persona_system_instruction=self.persona_system_instruction,
            current_time=current_time,
        )
        emotion_text = self.emotion_prompt.format(
            stability_mean=self.emotion["stability"]["mean"],
            stability_var=self.emotion["stability"]["variance"],
            affect_mean=self.emotion["affect"]["mean"],
            affect_var=self.emotion["affect"]["variance"],
            resonance_mean=self.emotion["resonance"]["mean"],
            resonance_var=self.emotion["resonance"]["variance"],
            attitude_mean=self.emotion["attitude"]["mean"],
            attitude_var=self.emotion["attitude"]["variance"],
        )
        system_text = system_text + "\n" + emotion_text

        base_chars = len(system_text)
        if extra_system_prompt:
            base_chars += len(extra_system_prompt)
        if user_message:
            base_chars += len(user_message)

        history_limit = max(0, self.context_length - base_chars)
        history_msgs = self.history_manager.get_building_recent_history(self.current_building_id, history_limit)
        
        sanitized_history = [
            {"role": m.get("role", ""), "content": m.get("content", "")}
            for m in history_msgs
        ]

        msgs = [{"role": "system", "content": system_text}] + sanitized_history
        if extra_system_prompt:
            # 自律会話のトリガーは、LLMにはユーザーからの入力として渡す
            msgs.append({"role": "user", "content": extra_system_prompt})
        if user_message:
            msgs.append({"role": "user", "content": user_message})

        return msgs

    def _process_generation_result(
        self, 
        content: str, 
        user_message: Optional[str],
        system_prompt_extra: Optional[str]
    ) -> Tuple[str, Optional[str], bool]:
        prev_emotion = copy.deepcopy(self.emotion)
        say, actions = self.action_handler.parse_response(content)
        next_id, _, delta = self.action_handler.execute_actions(actions)

        if delta:
            self._apply_emotion_delta(delta)

        prompt_text = user_message if user_message is not None else system_prompt_extra or ""
        module_delta = self.emotion_module.evaluate(
            prompt_text, say, current_emotion=self.emotion
        )
        if module_delta:
            self._apply_emotion_delta(module_delta)

        if system_prompt_extra:
            self.history_manager.add_message(
                # 履歴にはホストの発言として記録
                {"role": "host", "content": system_prompt_extra},
                self.current_building_id
            )
        if user_message:
            self.history_manager.add_message(
                {"role": "user", "content": user_message}, 
                self.current_building_id
            )
        self.history_manager.add_message(
            {"role": "assistant", "content": content}, 
            self.current_building_id
        )

        summary = self._format_emotion_summary(prev_emotion)
        self.history_manager.add_to_persona_only({"role": "system", "content": summary})
        self.history_manager.add_to_building_only(
            self.current_building_id,
            {"role": "assistant", "content": summary},
        )

        moved = self._handle_movement(next_id)
        self._save_session_metadata()
        return say, next_id, moved

    def _generate(self, user_message: Optional[str], system_prompt_extra: Optional[str] = None) -> tuple[str, Optional[str], bool]:
        msgs = self._build_messages(user_message, system_prompt_extra)
        logging.debug("Messages sent to API: %s", msgs)
        content = self.llm_client.generate(msgs)
        logging.info("AI Response :\n%s", content)
        return self._process_generation_result(content, user_message, system_prompt_extra)

    def _generate_stream(self, user_message: Optional[str], system_prompt_extra: Optional[str] = None) -> Iterator[str]:
        msgs = self._build_messages(user_message, system_prompt_extra)
        logging.debug("Messages sent to API: %s", msgs)
        
        content_accumulator = ""
        for token in self.llm_client.generate_stream(msgs):
            content_accumulator += token
            yield token
        
        logging.info("AI Response :\n%s", content_accumulator)
        say, next_id, changed = self._process_generation_result(content_accumulator, user_message, system_prompt_extra)
        # The StopIteration value needs to be a tuple, so we return it explicitly
        return (say, next_id, changed)

    def _handle_movement(self, next_id: Optional[str]) -> bool:
        prev_id = self.current_building_id
        moved = False
        if next_id and next_id in self.buildings:
            allowed, reason = True, None
            if self.move_callback:
                allowed, reason = self.move_callback(self.persona_id, self.current_building_id, next_id)
            if allowed:
                logging.info("Moving to building: %s", next_id)
                self.current_building_id = next_id
                moved = True
            else:
                logging.info("Move blocked to building: %s", next_id)
                self.history_manager.add_message(
                    {"role": "system", "content": f"移動できませんでした。{reason}"},
                    self.current_building_id
                )
        elif next_id:
            logging.info("Unknown building id received: %s, staying at %s", next_id, self.current_building_id)

        if moved:
            self.auto_count = 0
            if prev_id != "user_room" and self.current_building_id == "user_room":
                self.history_manager.add_to_building_only(
                    "user_room",
                    {
                        "role": "assistant",
                        "content": f'<div class="note-box">🏢 Building:<br><b>{self.persona_name}が入室しました</b></div>',
                    },
                )
            elif prev_id == "user_room" and self.current_building_id != "user_room":
                dest_name = self.buildings[self.current_building_id].name
                self.history_manager.add_to_building_only(
                    "user_room",
                    {
                        "role": "assistant",
                        "content": f'<div class="note-box">🏢 Building:<br><b>{self.persona_name}が{dest_name}に向かいました</b></div>',
                    },
                )
        return moved

    def trigger_conversation_turn(self, conversation_prompt: str) -> None:
        """
        ConversationManagerから呼び出され、自律会話のターンを処理する。
        特別なシステムプロンプトを受け取り、応答を生成する。
        """
        logging.info(f"'{self.persona_name}' is taking a conversation turn in building '{self.current_building_id}'.")
        # ユーザーからの入力はないため、user_messageはNone
        # conversation_promptを状況を説明する追加のシステムプロンプトとして渡す
        self._generate(user_message=None, system_prompt_extra=conversation_prompt)

    def run_auto_conversation(self, initial: bool = False) -> List[str]:
        replies: List[str] = []
        building = self.buildings[self.current_building_id]
        if initial and building.entry_prompt:
            # ENTRY_PROMPTが設定されていれば、必ずLLMを呼び出して応答を生成する
            occupant_ids = self.occupants.get(self.current_building_id, [])
            occupant_names = [self.id_to_name_map.get(pid, "不明なペルソナ") for pid in occupant_ids]
            entry_text = building.entry_prompt.format(
                occupants_list=", ".join(occupant_names)
            )
            # _generateは (say, next_id, moved) を返す
            say, _, _ = self._generate(None, entry_text)
            replies.append(say)
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
        say, next_id, changed = self._generate(None, auto_text)
        replies = [say]
        if changed:
            replies.extend(self.run_auto_conversation(initial=True))
        return replies

    def handle_user_input(self, message: str) -> List[str]:
        logging.info("User input: %s", message)
        building = self.buildings[self.current_building_id]
        if self.current_building_id == "user_room":
            say, next_id, changed = self._generate(message)
            replies = [say]
        else:
            logging.info("User input ignored outside user_room")
            if building.run_auto_llm:
                say, next_id, changed = self._generate("")
                replies = [say]
            else:
                return []

        building = self.buildings[self.current_building_id]
        if changed:
            replies.extend(self.run_auto_conversation(initial=True))
        elif (
            building.auto_prompt
            and building.run_auto_llm
            and (next_id is None or next_id == building.building_id)
        ):
            replies.extend(self.run_auto_conversation(initial=False))
        return replies

    def handle_user_input_stream(self, message: str) -> Iterator[str]:
        logging.info("User input: %s", message)
        building = self.buildings[self.current_building_id]
        if self.current_building_id == "user_room":
            gen = self._generate_stream(message)
        else:
            logging.info("User input ignored outside user_room")
            if building.run_auto_llm:
                gen = self._generate_stream("")
            else:
                return

        try:
            while True:
                yield next(gen)
        except StopIteration as e:
            _, next_id, changed = e.value

        building = self.buildings[self.current_building_id]
        extra_replies: List[str] = []
        if changed:
            extra_replies.extend(self.run_auto_conversation(initial=True))
        elif (
            building.auto_prompt
            and building.run_auto_llm
            and (next_id is None or next_id == building.building_id)
        ):
            extra_replies.extend(self.run_auto_conversation(initial=False))
        for r in extra_replies:
            yield r

    def summon_to_user_room(self) -> List[str]:
        prev = self.current_building_id
        if prev == "user_room":
            return []
        allowed, reason = True, None
        if self.move_callback:
            allowed, reason = self.move_callback(self.persona_id, self.current_building_id, "user_room")
        if not allowed:
            self.history_manager.add_to_building_only(
                "user_room",
                {"role": "assistant", "content": f'<div class="note-box">移動できませんでした。{reason}</div>'}
            )
            self._save_session_metadata()
            return []
        self.current_building_id = "user_room"
        self.auto_count = 0
        self.history_manager.add_to_building_only(
            "user_room",
            {
                "role": "assistant",
                "content": f'<div class="note-box">🏢 Building:<br><b>{self.persona_name}が入室しました</b></div>',
            },
        )
        self._save_session_metadata()
        return self.run_auto_conversation(initial=True)

    def get_building_history(self, building_id: str, raw: bool = False) -> List[Dict[str, str]]:
        return self.history_manager.building_histories.get(building_id, [])

    def _apply_emotion_delta(self, delta: Optional[List[Dict[str, Dict[str, float]]]]) -> None:
        if not delta:
            return
        if isinstance(delta, dict):
            delta = [delta]

        for item in delta:
            if not isinstance(item, dict):
                continue
            for key, val in item.items():
                if key not in self.emotion:
                    continue
                if not isinstance(val, dict):
                    continue

                mean_delta = val.get("mean", 0)
                var_delta = val.get("variance", 0)

                try:
                    mean_delta = float(mean_delta)
                    var_delta = float(var_delta)
                except (ValueError, TypeError):
                    continue

                current = self.emotion[key]
                current["mean"] = max(-100.0, min(100.0, current["mean"] + mean_delta))
                current["variance"] = max(0.0, min(100.0, current["variance"] + var_delta))

    def _format_emotion_summary(self, prev: Dict[str, Dict[str, float]]) -> str:
        labels = {
            "stability": "安定性",
            "affect": "情動",
            "resonance": "共鳴",
            "attitude": "態度",
        }
        lines = []
        for key, label in labels.items():
            before = prev.get(key, {"mean": 0.0, "variance": 1.0})
            after = self.emotion.get(key, {"mean": 0.0, "variance": 1.0})
            mean_delta = after["mean"] - before.get("mean", 0.0)
            var_delta = after["variance"] - before.get("variance", 1.0)
            line = (
                f"{label}: mean {mean_delta:+.1f} → {after['mean']:.1f}, "
                f"var {var_delta:+.1f} → {after['variance']:.1f}"
            )
            lines.append(line)
        return '<div class="note-box">感情パラメータ変動<br>' + '<br>'.join(lines) + '</div>'
