import json
import logging
import time
import copy
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Iterator
from datetime import datetime

from google import genai
from google.genai import types

from dotenv import load_dotenv

from buildings import Building
from llm_clients import get_llm_client, LLMClient
import llm_clients
from action_handler import ActionHandler
from history_manager import HistoryManager
from emotion_module import EmotionControlModule
from database.models import AI as AIModel

load_dotenv()


class PersonaCore:
    def __init__(
        self,
        persona_id: str,
        persona_name: str,
        persona_system_instruction: str,
        avatar_image: Optional[str],
        buildings: List[Building],
        common_prompt_path: Path,
        session_factory: Callable,
        is_visitor: bool = False,
        emotion_prompt_path: Path = Path("system_prompts/emotion_parameter.txt"),
        action_priority_path: Path = Path("action_priority.json"),
        building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
        occupants: Optional[Dict[str, List[str]]] = None,
        id_to_name_map: Optional[Dict[str, str]] = None,
        move_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        dispatch_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        start_building_id: str = "air_room",
        model: str = "gpt-4o",
        context_length: int = 120000,
        provider: str = "ollama",
    ):
        self.is_visitor = is_visitor
        self.SessionLocal = session_factory
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
        self.conscious_log_path = (
            self.saiverse_home / "personas" / self.persona_id / "conscious_log.json"
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
        self.pulse_indices: Dict[str, int] = {}

        # Load session data, which may overwrite the defaults
        self._load_session_data()

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
        self.dispatch_callback = dispatch_callback
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
        if self.is_visitor:
            self.messages = []
            self.conscious_log = []
            self.pulse_indices = {}
            return

        db = self.SessionLocal()
        try:
            db_ai = db.query(AIModel).filter(AIModel.AIID == self.persona_id).first()
            if not db_ai:
                logging.warning(f"No AI record found in DB for {self.persona_id}. Using default state.")
                return

            self.auto_count = db_ai.AUTO_COUNT or 0

            if db_ai.LAST_AUTO_PROMPT_TIMES:
                try:
                    self.last_auto_prompt_times.update(json.loads(db_ai.LAST_AUTO_PROMPT_TIMES))
                except json.JSONDecodeError:
                    logging.warning(f"Could not parse LAST_AUTO_PROMPT_TIMES from DB for {self.persona_name}.")
            
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

        if self.persona_log_path.exists():
            try:
                self.messages = json.loads(self.persona_log_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logging.warning("Failed to load persona log, starting empty")
                self.messages = []
        else:
            self.messages = []

        if self.conscious_log_path.exists():
            try:
                data = json.loads(self.conscious_log_path.read_text(encoding="utf-8"))
                self.conscious_log = data.get("log", [])
                self.pulse_indices = data.get("pulse_indices", {})
            except json.JSONDecodeError:
                logging.warning("Failed to load conscious log, starting empty")
                self.conscious_log = []
                self.pulse_indices = {}
        else:
            self.conscious_log = []
            self.pulse_indices = {}

    def _save_session_metadata(self) -> None:
        """ペルソナの動的な状態をDBに保存し、各種ログファイルを保存する"""
        if self.is_visitor:
            # Visitors only save file-based logs, not DB state.
            self.history_manager.save_all()
            self._save_conscious_log()
            return

        db = self.SessionLocal()
        try:
            update_data = {
                "EMOTION": json.dumps(self.emotion, ensure_ascii=False),
                "AUTO_COUNT": self.auto_count,
                "LAST_AUTO_PROMPT_TIMES": json.dumps(self.last_auto_prompt_times, ensure_ascii=False)
            }
            db.query(AIModel).filter(AIModel.AIID == self.persona_id).update(update_data)
            db.commit()
            logging.info(f"Saved dynamic state to DB for {self.persona_name}.")
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to save session data to DB for {self.persona_name}: {e}", exc_info=True)
        finally:
            db.close()

        self.history_manager.save_all()
        self._save_conscious_log()

    def set_model(self, model: str, context_length: int, provider: str) -> None:
        self.model = model
        self.context_length = context_length
        self.llm_client = get_llm_client(model, provider, context_length)

    def _build_messages(
        self,
        user_message: Optional[str],
        extra_system_prompt: Optional[str] = None,
        info_text: Optional[str] = None,
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
        if info_text:
            system_text += (
                "\n\n## 追加情報\n"
                "常時稼働モジュールから以下の情報が提供されています。今回の発話にこの情報を利用してください。\n"
                f"{info_text}"
            )

        base_chars = len(system_text)
        if extra_system_prompt:
            base_chars += len(extra_system_prompt)
        if user_message:
            base_chars += len(user_message)

        history_limit = max(0, self.context_length - base_chars)
        history_msgs = self.history_manager.get_recent_history(history_limit)
        
        sanitized_history = [
            {"role": m.get("role", ""), "content": m.get("content", "")}
            for m in history_msgs
        ]

        msgs = [{"role": "system", "content": system_text}] + sanitized_history
        if extra_system_prompt:
            msgs.append({"role": "system", "content": extra_system_prompt})
        if user_message:
            msgs.append({"role": "user", "content": user_message})
        return msgs

    def _process_generation_result(
        self,
        content: str,
        user_message: Optional[str],
        system_prompt_extra: Optional[str],
        log_extra_prompt: bool = True,
    ) -> Tuple[str, Optional[Dict[str, str]], bool]:
        prev_emotion = copy.deepcopy(self.emotion)
        say, actions = self.action_handler.parse_response(content)
        move_target, _, delta = self.action_handler.execute_actions(actions)

        if delta:
            self._apply_emotion_delta(delta)

        prompt_text = user_message if user_message is not None else system_prompt_extra or ""
        module_delta = self.emotion_module.evaluate(
            prompt_text, say, current_emotion=self.emotion
        )
        if module_delta:
            self._apply_emotion_delta(module_delta)

        if system_prompt_extra and log_extra_prompt:
            self.history_manager.add_message(
                {"role": "user", "content": system_prompt_extra},
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

        moved = self._handle_movement(move_target)
        self._save_session_metadata()
        return say, move_target, moved

    def _generate(
        self,
        user_message: Optional[str],
        system_prompt_extra: Optional[str] = None,
        info_text: Optional[str] = None,
        log_extra_prompt: bool = True,
        log_user_message: bool = True,
    ) -> tuple[str, Optional[Dict[str, str]], bool]:
        actual_user_message = user_message
        if user_message is None and system_prompt_extra is None:
            history = self.history_manager.building_histories.get(
                self.current_building_id, []
            )
            if not history or history[-1].get("role") != "user":
                actual_user_message = "意識モジュールが発話することを意思決定しました。自由に発言してください"
                logging.debug("Injected user message for context")

        msgs = self._build_messages(actual_user_message, system_prompt_extra, info_text)
        logging.debug("Messages sent to API: %s", msgs)

        content = self.llm_client.generate(msgs)
        attempt = 1
        while content.strip() == "エラーが発生しました。" and attempt < 3:
            logging.warning(
                "LLM generation failed; retrying in 10s (%d/3)", attempt
            )
            time.sleep(10)
            content = self.llm_client.generate(msgs)
            attempt += 1

        logging.info("AI Response :\n%s", content)
        return self._process_generation_result(
            content,
            user_message if log_user_message else None,
            system_prompt_extra,
            log_extra_prompt,
        )

    def _generate_stream(
        self,
        user_message: Optional[str],
        system_prompt_extra: Optional[str] = None,
        info_text: Optional[str] = None,
        log_extra_prompt: bool = True,
        log_user_message: bool = True,
    ) -> Iterator[str]:
        actual_user_message = user_message
        if user_message is None and system_prompt_extra is None:
            history = self.history_manager.building_histories.get(
                self.current_building_id, []
            )
            if not history or history[-1].get("role") != "user":
                actual_user_message = "意識モジュールが発話することを意思決定しました。自由に発言してください"
                logging.debug("Injected user message for context")

        msgs = self._build_messages(actual_user_message, system_prompt_extra, info_text)
        logging.debug("Messages sent to API: %s", msgs)

        attempt = 1
        while True:
            content_accumulator = ""
            tokens: List[str] = []
            for token in self.llm_client.generate_stream(msgs):
                content_accumulator += token
                tokens.append(token)

            if content_accumulator.strip() != "エラーが発生しました。" or attempt >= 3:
                for t in tokens:
                    yield t
                break

            logging.warning(
                "LLM stream generation failed; retrying in 10s (%d/3)", attempt
            )
            attempt += 1
            time.sleep(10)

        logging.info("AI Response :\n%s", content_accumulator)
        say, move_target, changed = self._process_generation_result(
            content_accumulator,
            user_message if log_user_message else None,
            system_prompt_extra,
            log_extra_prompt,
        )
        return (say, move_target, changed)

    def _handle_movement(self, move_target: Optional[Dict[str, str]]) -> bool:
        if not move_target or not move_target.get("building"):
            return False

        prev_id = self.current_building_id
        target_building_id = move_target.get("building")
        target_city_id = move_target.get("city")
        moved = False

        # City間移動の処理
        if target_city_id:
            if self.dispatch_callback:
                logging.info(f"Attempting to dispatch to city: {target_city_id}, building: {target_building_id}")
                dispatched, reason = self.dispatch_callback(self.persona_id, target_city_id, target_building_id)
                if dispatched:
                    # 派遣が成功した場合、このインスタンスはまもなく削除されるため、
                    # これ以上の状態変更は行わない。
                    # 'moved' は True とみなし、後続の自律会話ループなどを抑制する。
                    moved = True
                else:
                    logging.warning(f"Dispatch failed: {reason}")
                    self.history_manager.add_message(
                        {"role": "system", "content": f"別のCityへの移動に失敗しました。{reason}"},
                        self.current_building_id
                    )
            else:
                logging.error("Dispatch callback is not set. Cannot move between cities.")
                self.history_manager.add_message(
                    {"role": "system", "content": "別のCityへ移動する機能が設定されていません。"},
                    self.current_building_id
                )
            return moved

        # City内移動の処理
        if target_building_id and target_building_id in self.buildings:
            allowed, reason = True, None
            if self.move_callback:
                allowed, reason = self.move_callback(self.persona_id, self.current_building_id, target_building_id)
            if allowed:
                logging.info("Moving to building: %s", target_building_id)
                self.current_building_id = target_building_id
                moved = True
            else:
                logging.info("Move blocked to building: %s", target_building_id)
                self.history_manager.add_message(
                    {"role": "system", "content": f"移動できませんでした。{reason}"},
                    self.current_building_id
                )
        elif target_building_id:
            logging.info("Unknown building id received: %s, staying at %s", target_building_id, self.current_building_id)

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

    def run_auto_conversation(self, initial: bool = False) -> List[str]:
        replies: List[str] = []
        move_target: Optional[Dict[str, str]] = None
        building = self.buildings[self.current_building_id]
        if initial and building.entry_prompt:
            if building.run_entry_llm:
                entry_text = building.entry_prompt.format(persona_name=self.persona_name)
                say, move_target, _ = self._generate(None, entry_text)
                replies.append(say)
            else:
                self.history_manager.add_message(
                    {"role": "system", "content": building.entry_prompt},
                    self.current_building_id
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
            say, move_target, changed = self._generate(None, auto_text)
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
        say, move_target, changed = self._generate(None, auto_text)
        replies = [say]
        if changed:
            replies.extend(self.run_auto_conversation(initial=True))
        return replies

    def handle_user_input(self, message: str) -> List[str]:
        logging.info("User input: %s", message)
        building = self.buildings[self.current_building_id]
        if self.current_building_id == "user_room":
            say, move_target, changed = self._generate(message)
            replies = [say]
        else:
            logging.info("User input ignored outside user_room")
            if building.run_auto_llm:
                say, move_target, changed = self._generate("")
                replies = [say]
            else:
                return []

        building = self.buildings[self.current_building_id]
        if changed:
            replies.extend(self.run_auto_conversation(initial=True))
        elif (
            building.auto_prompt
            and building.run_auto_llm
            and (move_target is None or move_target.get("building") == building.building_id)
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
            _, move_target, changed = e.value

        building = self.buildings[self.current_building_id]
        extra_replies: List[str] = []
        if changed:
            extra_replies.extend(self.run_auto_conversation(initial=True))
        elif (
            building.auto_prompt
            and building.run_auto_llm
            and (move_target is None or move_target.get("building") == building.building_id)
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

    # ------------------------------------------------------------------
    # Pulse related utilities
    # ------------------------------------------------------------------

    def _save_conscious_log(self) -> None:
        self.conscious_log_path.parent.mkdir(parents=True, exist_ok=True)
        data_to_save = {
            "log": self.conscious_log,
            "pulse_indices": self.pulse_indices
        }
        self.conscious_log_path.write_text(json.dumps(data_to_save, ensure_ascii=False), encoding="utf-8")

    def run_pulse(self, user_online: bool = True, decision_model: Optional[str] = None) -> List[str]:
        """Execute one autonomous pulse cycle."""
        building_id = self.current_building_id
        logging.info("[pulse] %s starting pulse in %s", self.persona_id, building_id)

        hist = self.history_manager.building_histories.get(building_id, [])
        idx = self.pulse_indices.get(building_id, len(hist))
        new_msgs = hist[idx:]
        self.pulse_indices[building_id] = len(hist)
        logging.debug("[pulse] new messages since last pulse: %s", new_msgs)

        occupants = ",".join(self.occupants.get(building_id, []))
        info = (
            f"occupants:{occupants}\nuser_online:{user_online}"
        )
        logging.debug("[pulse] context info: %s", info)
        self.conscious_log.append({"role": "user", "content": info})

        recent = self.history_manager.building_histories.get(building_id, [])[-6:]
        recent_text = "\n".join(
            f"{m.get('role')}: {m.get('content')}" for m in recent if m.get("role") != "system"
        )

        pulse_prompt = Path("system_prompts/pulse.txt").read_text(encoding="utf-8")
        prompt = pulse_prompt.format(
            current_persona_name=self.persona_name,
            current_persona_system_instruction=self.persona_system_instruction,
            current_building_name=self.buildings[building_id].name,
            recent_conversation=recent_text,
            occupants=occupants,
            user_online_state="online" if user_online else "offline",
        )
        model_name = decision_model or "gemini-2.0-flash"

        free_key = os.getenv("GEMINI_FREE_API_KEY")
        paid_key = os.getenv("GEMINI_API_KEY")
        if not free_key and not paid_key:
            logging.error("[pulse] Gemini API key not set")
            return []

        free_client = genai.Client(api_key=free_key) if free_key else None
        paid_client = genai.Client(api_key=paid_key) if paid_key else None
        active_client = free_client or paid_client

        def _call(client: genai.Client):
            return client.models.generate_content(
                model=model_name,
                contents=[types.Content(parts=[types.Part(text=info)], role="user")],
                config=types.GenerateContentConfig(
                    system_instruction=prompt,
                    safety_settings=llm_clients.GEMINI_SAFETY_CONFIG,
                    response_mime_type="application/json",
                ),
            )

        try:
            resp = _call(active_client)
        except Exception as e:
            if active_client is free_client and paid_client and "rate" in str(e).lower():
                logging.info("[pulse] retrying with paid Gemini key due to rate limit")
                active_client = paid_client
                try:
                    resp = _call(active_client)
                except Exception as e2:
                    logging.error("[pulse] Gemini call failed: %s", e2)
                    return []
            else:
                logging.error("[pulse] Gemini call failed: %s", e)
                return []

        content = resp.text.strip()
        logging.info("[pulse] raw decision:\n%s", content)
        self.conscious_log.append({"role": "assistant", "content": content})
        self._save_conscious_log()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logging.warning("[pulse] failed to parse decision JSON")
            return []

        replies: List[str] = []
        if data.get("speak"):
            info_text = data.get("info", "")
            logging.info("[pulse] generating speech with extra info: %s", info_text)
            say, _, _ = self._generate(
                None,
                system_prompt_extra=None,
                info_text=info_text,
                log_extra_prompt=False,
                log_user_message=False,
            )
            replies.append(say)
        else:
            logging.info("[pulse] decision: remain silent")

        self._save_session_metadata()
        logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
        return replies
