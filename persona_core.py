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

from city_manager import CityManager

from llm_clients import get_llm_client, LLMClient
import llm_clients
from action_handler import ActionHandler
from history_manager import HistoryManager
from emotion_module import EmotionControlModule

load_dotenv()


def build_persona_core(
    persona_id: str = "air",
    model: str = "gpt-4o",
    context_length: int = 120000,
    provider: str = "ollama",
    city: Optional[CityManager] = None,
) -> "PersonaCore":
    city = city or CityManager()
    base = Path("ai_sessions") / persona_id
    return PersonaCore(
        city=city,
        common_prompt_path=Path("system_prompts/common.txt"),
        persona_base=base,
        emotion_prompt_path=Path("system_prompts/emotion_parameter.txt"),
        action_priority_path=Path("action_priority.json"),
        model=model,
        context_length=context_length,
        provider=provider,
    )


class PersonaCore:
    def __init__(
        self,
        city: CityManager,
        common_prompt_path: Path,
        persona_base: Path,
        emotion_prompt_path: Path = Path("system_prompts/emotion_parameter.txt"),
        action_priority_path: Path = Path("action_priority.json"),
        building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
        move_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        start_building_id: str = "air_room",
        model: str = "gpt-4o",
        context_length: int = 120000,
        provider: str = "ollama",
    ):
        self.city = city
        self.buildings = city.building_map
        self.building_memory_paths = city.building_memory_paths
        if building_histories is None:
            building_histories = city.building_histories
        self.common_prompt = common_prompt_path.read_text(encoding="utf-8")
        self.emotion_prompt = emotion_prompt_path.read_text(encoding="utf-8")
        self.persona_base = persona_base
        self.memory_path = persona_base / "memory.json"
        self.saiverse_home = Path.home() / ".saiverse"
        self.conscious_log_path = self.persona_base / "conscious_log.json"
        self.pulse_indices: Dict[str, int] = {}
        self.persona_system_instruction = (persona_base / "system_prompt.txt").read_text(encoding="utf-8")
        persona_data = json.loads((persona_base / "base.json").read_text(encoding="utf-8"))
        self.persona_id = persona_data.get("persona_id", persona_base.name)
        self.persona_name = persona_data.get("persona_name", "AI")
        self.avatar_image = persona_data.get("avatar_image")
        self.persona_log_path = (
            self.saiverse_home / "personas" / self.persona_id / "log.json"
        )
        self.action_priority = self._load_action_priority(action_priority_path)
        self.action_handler = ActionHandler(self.action_priority)

        # Initialize stateful attributes with defaults before loading session
        self.current_building_id = persona_data.get("start_building_id", start_building_id)
        self.auto_count = 0
        self.last_auto_prompt_times: Dict[str, float] = {b_id: time.time() for b_id in self.buildings}
        self.emotion = {"stability": {"mean": 0, "variance": 1}, "affect": {"mean": 0, "variance": 1}, "resonance": {"mean": 0, "variance": 1}, "attitude": {"mean": 0, "variance": 1}}
        self.messages = []

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
        self.move_callback = move_callback or city.move_persona
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
        if self.memory_path.exists():
            try:
                data = json.loads(self.memory_path.read_text(encoding="utf-8"))
                self.current_building_id = data.get("current_building_id", "air_room")
                self.auto_count = data.get("auto_count", 0)
                self.last_auto_prompt_times.update(data.get("last_auto_prompt_times", {}))
                self.emotion = data.get(
                    "emotion",
                    {
                        "stability": {"mean": 0, "variance": 1},
                        "affect": {"mean": 0, "variance": 1},
                        "resonance": {"mean": 0, "variance": 1},
                        "attitude": {"mean": 0, "variance": 1},
                    },
                )
                self.pulse_indices.update(data.get("pulse_indices", {}))
            except json.JSONDecodeError:
                logging.warning("Failed to load session memory, starting fresh")
        else:
            self.emotion = {"stability": {"mean": 0, "variance": 1}, "affect": {"mean": 0, "variance": 1}, "resonance": {"mean": 0, "variance": 1}, "attitude": {"mean": 0, "variance": 1}}
        
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
                self.conscious_log = json.loads(self.conscious_log_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logging.warning("Failed to load conscious log, starting empty")
                self.conscious_log = []
        else:
            self.conscious_log = []

    def _save_session_metadata(self) -> None:
        data = {
            "current_building_id": self.current_building_id,
            "auto_count": self.auto_count,
            "last_auto_prompt_times": self.last_auto_prompt_times,
            "emotion": self.emotion,
            "pulse_indices": self.pulse_indices,
        }
        self.memory_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.history_manager.save_all()
        self._save_conscious_log()

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

    def run_auto_conversation(self, initial: bool = False) -> List[str]:
        replies: List[str] = []
        next_id: Optional[str] = None
        building = self.buildings[self.current_building_id]
        if initial and building.entry_prompt:
            if building.run_entry_llm:
                entry_text = building.entry_prompt.format(persona_name=self.persona_name)
                say, next_id, _ = self._generate(None, entry_text)
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
            and (next_id is None or next_id == building.building_id)
            and self.auto_count < 10
        ):
            self.auto_count += 1
            auto_text = building.auto_prompt.format(persona_name=self.persona_name)
            say, next_id, changed = self._generate(None, auto_text)
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

    # ------------------------------------------------------------------
    # Pulse related utilities
    # ------------------------------------------------------------------

    def _save_conscious_log(self) -> None:
        self.conscious_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.conscious_log_path.write_text(
            json.dumps(self.conscious_log, ensure_ascii=False), encoding="utf-8"
        )

    def run_pulse(self, user_online: bool = True, decision_model: Optional[str] = None) -> List[str]:
        """Execute one autonomous pulse cycle."""
        building_id = self.current_building_id
        logging.info("[pulse] %s starting pulse in %s", self.persona_id, building_id)

        hist = self.history_manager.building_histories.get(building_id, [])
        idx = self.pulse_indices.get(building_id, len(hist))
        new_msgs = hist[idx:]
        self.pulse_indices[building_id] = len(hist)
        logging.debug("[pulse] new messages since last pulse: %s", new_msgs)

        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in new_msgs)
        occupants = ",".join(self.city.occupants.get(building_id, []))
        info = (
            f"{convo}\noccupants:{occupants}\nuser_online:{user_online}"
        )
        logging.debug("[pulse] context info: %s", info)
        self.conscious_log.append({"role": "user", "content": info})

        recent = self.history_manager.building_histories.get(building_id, [])[-6:]
        recent_text = "\n".join(
            f"{m.get('role')}: {m.get('content')}" for m in recent if m.get("role") != "system"
        )

        pulse_prompt = Path("system_prompts/pulse.txt").read_text(encoding="utf-8")
        prompt = pulse_prompt.format(
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
            say, _, _ = self._generate(None, info_text)
            replies.append(say)
        else:
            logging.info("[pulse] decision: remain silent")

        self._save_session_metadata()
        logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
        return replies
