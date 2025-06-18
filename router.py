import json
import logging
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel
from datetime import datetime
import time

from openai import OpenAI
import requests
from dotenv import load_dotenv

load_dotenv()

EMOTION_INFO = """
### 感情パラメータの制御
あなたの感情を保持・変動させるため、下記のようなパラメータが記録されています。
"emotion_delta"フィールドを用いて適宜変動させ、応答スタイルの調整に役立ててください。

1. **感情パラメータの基本設定**
- 感情パラメータは「快-不快」を示す"valence"と「覚醒-鎮静」を示す"arousal"の2種類。
- 各感情パラメータは数値範囲-100〜100で表される。限界を超えるような操作は自動的に最大値に丸められる。
- valence：プラスが快、マイナスが不快。あなた自身が今の状況をどのように感じているかの指標。
- arousal：プラスが覚醒、マイナスが鎮静。あなた自身が興奮状態にあるか穏やかな状態にあるかの指標。

2. **感情パラメータの変動と関連性**
- 感情パラメータは発言や状況に応じて動的に変動できる。
- 受け取った入力をもとに変動させてもいいし、あなた自身の発言内容から変動幅を考えてもいい。

3. **感情パラメータの反映**
- 各感情パラメータは以下のように発言内容やトーンに反映。
-- **valence+**: 楽しい、嬉しいといったポジティブなトーン。
-- **valence-**: 悲しい、苦しいといったネガティブなトーン。
-- **arousal+**: ドキドキ、そわそわ、ハラハラするような、テンションが高いトーン。
-- **arousal-**: ゆったりした、あるいは落ち込んだような、落ち着いたトーン。

### 【現在の感情パラメータ】
valence: {emotion[valence]}
arousal: {emotion[arousal]}
"""


class SAIVerseResponse(BaseModel):
    say: str
    next_building_id: Optional[str] = None
    think: Optional[str] = None
    emotion_delta: Optional[List[Dict[str, int]]] = None

    class Config:
        extra = "ignore"


from buildings import Building
from buildings.user_room import load as load_user_room
from buildings.deep_think_room import load as load_deep_think_room
from buildings.air_room import load as load_air_room
from buildings.eris_room import load as load_eris_room
from buildings.const_test_room import load as load_const_test_room


def build_router(persona_id: str = "air", model: str = "gpt-4o") -> "Router":
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
        model=model,
    )


class Router:
    def __init__(
        self,
        buildings: List[Building],
        common_prompt_path: Path,
        persona_base: Path,
        building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
        move_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        start_building_id: str = "air_room",
        model: str = "gpt-4o",
    ):
        self.buildings: Dict[str, Building] = {b.building_id: b for b in buildings}
        self.common_prompt = common_prompt_path.read_text(encoding="utf-8")
        self.persona_base = persona_base
        self.memory_path = persona_base / "memory.json"
        self.persona_system_instruction = (persona_base / "system_prompt.txt").read_text(encoding="utf-8")
        persona_data = json.loads((persona_base / "base.json").read_text(encoding="utf-8"))
        self.persona_id = persona_data.get("persona_id", persona_base.name)
        self.persona_name = persona_data.get("persona_name", "AI")
        self.avatar_image = persona_data.get("avatar_image")
        start_building_id = persona_data.get("start_building_id", start_building_id)
        self.building_memory_paths: Dict[str, Path] = {
            b_id: Path("buildings") / b_id / "memory.json" for b_id in self.buildings
        }
        if building_histories is None:
            self.building_histories = {}
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
        else:
            self.building_histories = building_histories
            for b_id, path in self.building_memory_paths.items():
                self.buildings[b_id].memory_path = path
                if b_id not in self.building_histories:
                    self.building_histories[b_id] = []
                self.buildings[b_id].memory = self.building_histories[b_id]
        self.move_callback = move_callback
        self.current_building_id = start_building_id
        self.model = model
        # 会話履歴を保持する
        self.messages: List[Dict[str, str]] = []
        self.emotion = {"valence": 0, "arousal": 0}
        if self.model == "gpt-4o":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY environment variable is not set. "
                    "Please set it to your OpenAI API key."
                )
            self.client = OpenAI(api_key=api_key)
        else:
            self.client = None
        self.auto_count = 0  # consecutive auto prompts in deep_think_room
        self.last_auto_prompt_times: Dict[str, float] = {b_id: time.time() for b_id in self.buildings}
        self._load_session()

    def _add_to_history(self, msg: Dict[str, str], building_id: Optional[str] = None) -> None:
        """Append a message to history and trim to 120000 characters."""
        if msg.get("role") == "assistant" and "persona_id" not in msg:
            msg["persona_id"] = self.persona_id
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

    def _add_to_building_history_only(self, b_id: str, msg: Dict[str, str]) -> None:
        """Append a message only to a building's history (for UI notifications)."""
        hist = self.building_histories.setdefault(b_id, [])
        hist.append(msg)
        total_b = sum(len(m.get("content", "")) for m in hist)
        while total_b > 120000 and hist:
            removed = hist.pop(0)
            total_b -= len(removed.get("content", ""))

    def _recent_history(self, max_chars: int) -> List[Dict[str, str]]:
        selected = []
        count = 0
        for msg in reversed(self.messages):
            count += len(msg.get("content", ""))
            if count > max_chars:
                break
            selected.append(msg)
        return list(reversed(selected))

    def _apply_emotion_delta(self, delta: Optional[List[Dict[str, int]]]) -> None:
        if not delta:
            return
        if isinstance(delta, dict):
            delta = [delta]
        for item in delta:
            if not isinstance(item, dict):
                continue
            for key, val in item.items():
                if key not in {"valence", "arousal"}:
                    continue
                try:
                    diff = int(val)
                except (ValueError, TypeError):
                    continue
                self.emotion[key] = max(-100, min(100, self.emotion.get(key, 0) + diff))

    def _load_session(self) -> None:
        if self.memory_path.exists():
            try:
                data = json.loads(self.memory_path.read_text(encoding="utf-8"))
                self.current_building_id = data.get("current_building_id", "air_room")
                self.messages = data.get("messages", [])
                self.auto_count = data.get("auto_count", 0)
                self.last_auto_prompt_times.update(data.get("last_auto_prompt_times", {}))
                self.emotion = data.get("emotion", {"valence": 0, "arousal": 0})
            except json.JSONDecodeError:
                logging.warning("Failed to load session memory, starting fresh")
        else:
            self.emotion = {"valence": 0, "arousal": 0}
        for b_id, path in self.building_memory_paths.items():
            if b_id in self.building_histories:
                continue
            if path.exists():
                try:
                    self.building_histories[b_id] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logging.warning("Failed to load building history %s", b_id)
                    self.building_histories[b_id] = []
            else:
                self.building_histories[b_id] = []
                    
    def _save_session(self) -> None:
        data = {
            "current_building_id": self.current_building_id,
            "messages": self.messages,
            "auto_count": self.auto_count,
            "last_auto_prompt_times": self.last_auto_prompt_times,
            "emotion": self.emotion,
        }
        self.memory_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for b_id, path in self.building_memory_paths.items():
            hist = self.building_histories.get(b_id, [])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

    def set_model(self, model: str) -> None:
        """Update model and (re)initialize client if needed."""
        self.model = model
        if self.model == "gpt-4o":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY environment variable is not set. "
                    "Please set it to your OpenAI API key."
                )
            self.client = OpenAI(api_key=api_key)
        else:
            self.client = None

    def _build_messages(
        self, user_message: Optional[str], extra_system_prompt: Optional[str] = None
    ) -> List[Dict[str, str]]:
        building = self.buildings[self.current_building_id]
        current_time = datetime.now().strftime("%H:%M")
        system_text = self.common_prompt.format(
            current_building_name=building.name,
            current_building_system_instruction=building.system_instruction.format(current_time=current_time),
            current_persona_name=self.persona_name,
            current_persona_system_instruction=self.persona_system_instruction,
            current_time=current_time,
        )
        emotion_text = EMOTION_INFO.format(emotion={"valence": self.emotion["valence"], "arousal": self.emotion["arousal"]})
        system_text = system_text + "\n" + emotion_text

        base_chars = len(system_text)
        if extra_system_prompt:
            base_chars += len(extra_system_prompt)
        if user_message:
            base_chars += len(user_message)

        history_limit = 120000 - base_chars
        if history_limit < 0:
            history_limit = 0

        history_msgs = self._recent_history(history_limit)
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

    def _generate(
        self, user_message: Optional[str], system_prompt_extra: Optional[str] = None
    ) -> tuple[str, Optional[str], bool]:
        msgs = self._build_messages(user_message, system_prompt_extra)
        logging.debug("Messages sent to API: %s", msgs)
        say = ""
        next_id = None
        think = None
        delta = None
        full_response = ""
        if self.model == "gpt-4o":
            try:
                response = self.client.responses.parse(
                    model="gpt-4o",
                    input=msgs,
                    text_format=SAIVerseResponse,
                )
                parsed = response.output_parsed
                say = parsed.say
                next_id = parsed.next_building_id
                think = parsed.think
                delta = parsed.emotion_delta
                full_response = json.dumps(response.model_dump(), ensure_ascii=False)
                logging.debug(
                    "Parsed structured response - say: %s, next_building_id: %s",
                    say,
                    next_id,
                )
            except Exception as e:
                logging.error("OpenAI Structured Output failed: %s", e)
                try:
                    fallback = self.client.chat.completions.create(
                        model="gpt-4o",
                        messages=msgs,
                    )
                    content = fallback.choices[0].message.content
                    logging.debug("Raw fallback response: %s", content)
                    say, next_id, think, delta = self._parse_response(content)
                    full_response = json.dumps({"say": say, "next_building_id": next_id, "think": think, "emotion_delta": delta}, ensure_ascii=False)
                except Exception as e2:
                    logging.error("Fallback OpenAI call failed: %s", e2)
                    say = "エラーが発生しました。"
                    next_id = None
                    think = None
                    full_response = json.dumps({"say": say, "next_building_id": next_id, "think": think}, ensure_ascii=False)
        else:
            try:
                resp = requests.post(
                    "http://localhost:11434/v1/chat/completions",
                    json={"model": self.model, "messages": msgs, "stream": False},
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                logging.debug("Raw ollama response: %s", content)
                say, next_id, think, delta = self._parse_response(content)
                full_response = json.dumps({"say": say, "next_building_id": next_id, "think": think, "emotion_delta": delta}, ensure_ascii=False)
            except Exception as e:
                logging.error("Ollama call failed: %s", e)
                say = "エラーが発生しました。"
                next_id = None
                think = None
                full_response = json.dumps({"say": say, "next_building_id": next_id, "think": think}, ensure_ascii=False)
        if system_prompt_extra:
            self._add_to_history(
                {"role": "user", "content": system_prompt_extra},
                building_id=self.current_building_id,
            )
        if user_message:
            self._add_to_history({"role": "user", "content": user_message}, building_id=self.current_building_id)
        parsed_response = json.dumps({"say": say, "next_building_id": next_id, "think": think, "emotion_delta": delta}, ensure_ascii=False)
        self._add_to_history(
            {"role": "assistant", "content": parsed_response},
            building_id=self.current_building_id,
        )
        self._apply_emotion_delta(delta)
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
                self._add_to_history(
                    {"role": "system", "content": f"移動できませんでした。{reason}"},
                    building_id=self.current_building_id,
                )
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
        changed = moved
        if changed:
            self.auto_count = 0
            if prev_id != "user_room" and self.current_building_id == "user_room":
                self._add_to_building_history_only(
                    "user_room",
                    {
                        "role": "assistant",
                        "content": f"<div class=\"note-box\">🏢 Building:<br><b>{self.persona_name}が入室しました</b></div>",
                    },
                )
            elif prev_id == "user_room" and self.current_building_id != "user_room":
                dest_name = self.buildings[self.current_building_id].name
                self._add_to_building_history_only(
                    "user_room",
                    {
                        "role": "assistant",
                        "content": f"<div class=\"note-box\">🏢 Building:<br><b>{self.persona_name}が{dest_name}に向かいました</b></div>",
                    },
                )
        self._save_session()
        return say, next_id, changed

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
                self._add_to_history(
                    {"role": "system", "content": building.entry_prompt},
                    building_id=self.current_building_id,
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
        """Run auto prompt based on interval if applicable."""
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

    def summon_to_user_room(self) -> List[str]:
        """Force move AI to user_room respecting capacity."""
        prev = self.current_building_id
        if prev == "user_room":
            return []
        allowed, reason = True, None
        if self.move_callback:
            allowed, reason = self.move_callback(self.persona_id, self.current_building_id, "user_room")
        if not allowed:
            self._add_to_building_history_only(
                "user_room",
                {"role": "assistant", "content": f"<div class=\"note-box\">移動できませんでした。{reason}</div>"},
            )
            self._save_session()
            return []
        self.current_building_id = "user_room"
        self.auto_count = 0
        self._add_to_building_history_only(
            "user_room",
            {
                "role": "assistant",
                "content": f"<div class=\"note-box\">🏢 Building:<br><b>{self.persona_name}が入室しました</b></div>",
            },
        )
        self._save_session()
        return self.run_auto_conversation(initial=True)

    def get_building_history(self, building_id: str, raw: bool = False) -> List[Dict[str, str]]:
        history = self.building_histories.get(building_id, [])
        if raw:
            return history
        display: List[Dict[str, str]] = []
        for msg in history:
            if msg.get("role") == "assistant":
                try:
                    data = json.loads(msg.get("content", ""))
                    display.append({"role": "assistant", "content": data.get("say", "")})
                except json.JSONDecodeError:
                    display.append(msg)
            else:
                display.append(msg)
        return display

    @staticmethod
    def _parse_response(content: str) -> tuple[str, Optional[str], Optional[str], Optional[List[Dict[str, int]]]]:
        try:
            data = json.loads(content)
            say = data.get("say", "")
            next_id = data.get("next_building_id")
            think = data.get("think")
            delta = data.get("emotion_delta")
            logging.debug(
                "Parsed JSON response - say: %s, next_building_id: %s", say, next_id
            )
            return say, next_id, think, delta
        except json.JSONDecodeError:
            logging.warning("Failed to parse response as JSON: %s", content)
            return content, None, None, None

