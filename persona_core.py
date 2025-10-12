import json
import logging
import time
import copy
import os
import html
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Iterator
from datetime import datetime

from google import genai
from google.genai import types

from dotenv import load_dotenv

from buildings import Building
from saiverse_memory import SAIMemoryAdapter
from llm_clients import get_llm_client, LLMClient
import llm_clients
from action_handler import ActionHandler
from history_manager import HistoryManager
from emotion_module import EmotionControlModule
from database.models import AI as AIModel
from tools import TOOL_REGISTRY, TOOL_SCHEMAS
from tools.defs import parse_tool_result
from tools.context import persona_context

load_dotenv()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except ValueError:
        return default


RECALL_SNIPPET_MAX_CHARS = _env_int("SAIVERSE_RECALL_SNIPPET_MAX_CHARS", 8000)
RECALL_SNIPPET_STREAM_MAX_CHARS = _env_int("SAIVERSE_RECALL_SNIPPET_STREAM_MAX_CHARS", 800)
RECALL_SNIPPET_PULSE_MAX_CHARS = _env_int("SAIVERSE_RECALL_SNIPPET_PULSE_MAX_CHARS", 1200)


class PersonaCore:
    def __init__(
        self,
        city_name: str,
        persona_id: str,
        persona_name: str,
        persona_system_instruction: str,
        avatar_image: Optional[str],
        buildings: List[Building],
        common_prompt_path: Path,
        session_factory: Callable,
        is_visitor: bool = False,
        home_city_id: Optional[str] = None, # ★ 故郷のCity ID
        interaction_mode: str = "auto", # ★ 現在の対話モード
        is_dispatched: bool = False, # ★ このペルソナが他のCityに派遣中かどうかのフラグ
        emotion_prompt_path: Path = Path("system_prompts/emotion_parameter.txt"),
        action_priority_path: Path = Path("action_priority.json"),
        building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
        occupants: Optional[Dict[str, List[str]]] = None,
        id_to_name_map: Optional[Dict[str, str]] = None,
        move_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        dispatch_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        explore_callback: Optional[Callable[[str, str], None]] = None, # New callback
        create_persona_callback: Optional[Callable[[str, str], Tuple[bool, str]]] = None,
        start_building_id: str = "air_room",
        model: str = "gpt-4o",
        context_length: int = 120000,
        user_room_id: str = "user_room",
        provider: str = "ollama",
    ):
        self.city_name = city_name
        self.is_visitor = is_visitor
        self.is_dispatched = is_dispatched
        self.interaction_mode = interaction_mode
        self.home_city_id = home_city_id # ★ 故郷の情報を記憶
        self.SessionLocal = session_factory
        self.buildings: Dict[str, Building] = {b.building_id: b for b in buildings}
        self.user_room_id = user_room_id
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
        self.pulse_cursors: Dict[str, int] = {}
        self.entry_markers: Dict[str, int] = {}
        self._raw_pulse_cursor_data: Dict[str, Any] = {}
        self._raw_pulse_cursor_format: str = "count"

        # Load session data, which may overwrite the defaults
        self._load_session_data()

        # Initialise SAIMemory bridge for long-term recall/summary
        self.sai_memory: Optional[SAIMemoryAdapter]
        try:
            self.sai_memory = SAIMemoryAdapter(
                persona_id=self.persona_id,
                persona_dir=self.persona_log_path.parent,
                resource_id=self.persona_id,
            )
            if self.sai_memory.is_ready():
                logging.info("SAIMemory ready for persona %s", self.persona_id)
            else:
                logging.warning("SAIMemory adapter initialised but not ready for persona %s", self.persona_id)
        except Exception as exc:
            logging.warning("Failed to initialise SAIMemory for %s: %s", self.persona_id, exc)
            self.sai_memory = None

        # Initialize managers that depend on loaded data
        self.history_manager = HistoryManager(
            persona_id=self.persona_id,
            persona_log_path=self.persona_log_path,
            building_memory_paths=self.building_memory_paths,
            initial_persona_history=self.messages,
            initial_building_histories=building_histories,
            memory_adapter=self.sai_memory,
        )

        # Configure pulse tracking based on loaded histories
        self._initialise_pulse_state()

        # Initialize remaining attributes
        self.move_callback = move_callback
        self.dispatch_callback = dispatch_callback
        self.explore_callback = explore_callback
        self.create_persona_callback = create_persona_callback
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
            self.pulse_cursors = {}
            self.entry_markers = {}
            self._raw_pulse_cursor_data = {}
            self._raw_pulse_cursor_format = "count"
            return

        db = self.SessionLocal()
        try:
            db_ai = db.query(AIModel).filter(AIModel.AIID == self.persona_id).first()
            if not db_ai:
                logging.warning(f"No AI record found in DB for {self.persona_id}. Using default state.")
                # 新規作成されたペルソナの場合、DBからの読み込みはスキップし、デフォルト値を使用する
            else:
                # 既存のペルソナの場合、DBから状態を読み込む
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
                raw_cursors = data.get("pulse_cursors")
                if raw_cursors is None:
                    raw_cursors = data.get("pulse_indices", {})
                if isinstance(raw_cursors, dict):
                    self._raw_pulse_cursor_data = raw_cursors
                else:
                    self._raw_pulse_cursor_data = {}
                fmt = data.get("pulse_cursor_format")
                self._raw_pulse_cursor_format = fmt if isinstance(fmt, str) else "count"
            except json.JSONDecodeError:
                logging.warning("Failed to load conscious log, starting empty")
                self.conscious_log = []
                self._raw_pulse_cursor_data = {}
                self._raw_pulse_cursor_format = "count"
        else:
            self.conscious_log = []
            self._raw_pulse_cursor_data = {}
            self._raw_pulse_cursor_format = "count"

    def _initialise_pulse_state(self) -> None:
        hist_map = self.history_manager.building_histories
        computed_cursors: Dict[str, int] = {}
        max_seq_map: Dict[str, int] = {}
        for b_id, hist in hist_map.items():
            max_seq = 0
            for msg in hist:
                try:
                    seq_val = int(msg.get("seq", 0))
                except (TypeError, ValueError):
                    seq_val = 0
                max_seq = max(max_seq, seq_val)
            max_seq_map[b_id] = max_seq
            raw_value = self._raw_pulse_cursor_data.get(b_id) if hasattr(self, "_raw_pulse_cursor_data") else None
            cursor = max_seq
            if raw_value is not None:
                if self._raw_pulse_cursor_format == "seq":
                    try:
                        cursor = int(raw_value)
                    except (TypeError, ValueError):
                        cursor = max_seq
                    cursor = max(0, min(cursor, max_seq))
                else:
                    try:
                        count = int(raw_value)
                    except (TypeError, ValueError):
                        count = len(hist)
                    if count <= 0:
                        cursor = 0
                    else:
                        idx = min(count, len(hist))
                        if idx == 0:
                            cursor = 0
                        else:
                            ref = hist[idx - 1]
                            try:
                                cursor = int(ref.get("seq", idx))
                            except (TypeError, ValueError):
                                cursor = idx
            computed_cursors[b_id] = max(0, cursor)

        for b_id, hist in hist_map.items():
            if b_id not in computed_cursors:
                computed_cursors[b_id] = max_seq_map.get(b_id, 0)

        self.pulse_cursors = computed_cursors

        # Initialize entry markers to the latest known sequence in each building
        for b_id, hist in hist_map.items():
            if b_id not in self.entry_markers:
                last_seq = max_seq_map.get(b_id, 0)
                self.entry_markers[b_id] = last_seq

        if self.current_building_id in hist_map:
            self.entry_markers[self.current_building_id] = self.pulse_cursors.get(
                self.current_building_id,
                self.entry_markers.get(self.current_building_id, 0),
            )

    def _occupants_snapshot(self, building_id: str) -> List[str]:
        occupants = self.occupants.get(building_id, []) or []
        snapshot = []
        for pid in occupants:
            if not pid:
                continue
            pid_str = str(pid)
            if pid_str not in snapshot:
                snapshot.append(pid_str)
        if building_id == self.current_building_id and self.persona_id not in snapshot:
            snapshot.append(self.persona_id)
        return snapshot

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
        # self.messagesが空の場合はlog.jsonを上書きしない
        if self.messages:
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
        guidance_text: Optional[str] = None,
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
            current_city_name=self.city_name,
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
        logging.debug(
            "history_limit=%s context=%s base=%s history_count=%s",
            history_limit,
            self.context_length,
            base_chars,
            len(history_msgs),
        )
        if history_msgs:
            logging.debug("history_head=%s", history_msgs[0])
            logging.debug("history_tail=%s", history_msgs[-1])

        sanitized_history: List[Dict[str, str]] = []
        for m in history_msgs:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system" and "### 意識モジュールからの情報提供" in content:
                continue
            sanitized_history.append({"role": role, "content": content})

        msgs = [{"role": "system", "content": system_text}] + sanitized_history
        if guidance_text:
            wrapped_guidance = f"<system>\n{guidance_text}\n</system>"
            msgs.append({"role": "user", "content": wrapped_guidance})
        if extra_system_prompt:
            msgs.append({"role": "system", "content": extra_system_prompt})
        if user_message:
            msgs.append({"role": "user", "content": user_message})
        return msgs

    def _collect_recent_memory_timestamps(self) -> List[int]:
        recent = self.history_manager.get_recent_history(self.context_length)
        values: List[int] = []
        seen = set()
        for msg in recent:
            created_at = msg.get('created_at')
            if created_at is not None:
                try:
                    value = int(created_at)
                except (TypeError, ValueError):
                    continue
            else:
                ts = msg.get('timestamp')
                if not ts:
                    continue
                try:
                    value = int(datetime.fromisoformat(ts).timestamp())
                except ValueError:
                    continue
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values

    def _process_generation_result(
        self,
        content: str,
        user_message: Optional[str],
        system_prompt_extra: Optional[str],
        log_extra_prompt: bool = True,
    ) -> Tuple[str, Optional[Dict[str, str]], bool]:
        say, actions = self.action_handler.parse_response(content)
        move_target, _, delta = self.action_handler.execute_actions(actions)

        # --- New part for exploration ---
        explore_target = None
        # Find the first 'explore_city' action in the list
        for action in actions:
            if action.get("action") == "explore_city":
                explore_target = {"city_id": action.get("city_id")}
                break

        # --- New part for creation ---
        creation_target = None
        for action in actions:
            if action.get("action") == "create_persona":
                creation_target = {
                    "name": action.get("name"), "system_prompt": action.get("system_prompt")
                }
                break # Only handle one creation at a time

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
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )
        if user_message:
            self.history_manager.add_message(
                {"role": "user", "content": user_message}, 
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )

        reasoning_entries = self.llm_client.consume_reasoning()
        building_content = self._combine_with_reasoning(content, reasoning_entries)
        self.history_manager.add_to_persona_only(
            {"role": "assistant", "content": content}
        )
        self.history_manager.add_to_building_only(
            self.current_building_id,
            {"role": "assistant", "content": building_content},
            heard_by=self._occupants_snapshot(self.current_building_id),
        )

        moved = self._handle_movement(move_target)
        self._handle_exploration(explore_target) # Call the new handler
        self._handle_creation(creation_target) # Call the creation handler
        self._save_session_metadata()
        return say, move_target, moved

    def _generate(
        self,
        user_message: Optional[str],
        system_prompt_extra: Optional[str] = None,
        info_text: Optional[str] = None,
        guidance_text_override: Optional[str] = None,
        log_extra_prompt: bool = True,
        log_user_message: bool = True,
    ) -> tuple[str, Optional[Dict[str, str]], bool]:
        prev_emotion_state = copy.deepcopy(self.emotion)
        actual_user_message = user_message
        if user_message is None and system_prompt_extra is None:
            history = self.history_manager.building_histories.get(
                self.current_building_id, []
            )
            if not history or history[-1].get("role") != "user":
                actual_user_message = "意識モジュールが発話することを意思決定しました。自由に発言してください"
                logging.debug("Injected user message for context")

        # Memory: prepare recall context from SAIMemory
        combined_info = info_text or ""
        if self.sai_memory is not None:
            try:
                recall_source = user_message.strip() if (user_message and user_message.strip()) else None
                if recall_source is None:
                    recall_source = self.history_manager.get_last_user_message()
                if recall_source:
                    exclude_times = self._collect_recent_memory_timestamps()
                    snippet = self.sai_memory.recall_snippet(
                        self.current_building_id,
                        recall_source,
                        max_chars=RECALL_SNIPPET_MAX_CHARS,
                        exclude_created_at=exclude_times,
                    )
                    if snippet:
                        logging.debug("[memory] recall snippet content=%s", snippet[:400])
                        combined_info = (combined_info + "\n" + snippet).strip() if combined_info else snippet
                        logging.debug("[memory] SAIMemory recall snippet included (%d chars)", len(snippet))
            except Exception as exc:
                logging.warning("SAIMemory recall failed: %s", exc)

        msgs = self._build_messages(
            actual_user_message,
            extra_system_prompt=system_prompt_extra,
            info_text=combined_info or None,
            guidance_text=guidance_text_override,
        )
        logging.debug("Messages sent to API: %s", msgs)

        content = self.llm_client.generate(msgs, tools=[])
        attempt = 1
        while content.strip() == "エラーが発生しました。" and attempt < 3:
            logging.warning(
                "LLM generation failed; retrying in 10s (%d/3)", attempt
            )
            time.sleep(10)
            content = self.llm_client.generate(msgs, tools=[])
            attempt += 1

        logging.info("AI Response :\n%s", content)
        say, move_target, changed = self._process_generation_result(
            content,
            user_message if log_user_message else None,
            system_prompt_extra,
            log_extra_prompt,
        )
        self._post_response_updates(
            prev_emotion_state,
            user_message,
            system_prompt_extra,
            say,
        )
        return say, move_target, changed

    def _generate_stream(
        self,
        user_message: Optional[str],
        system_prompt_extra: Optional[str] = None,
        info_text: Optional[str] = None,
        guidance_text_override: Optional[str] = None,
        log_extra_prompt: bool = True,
        log_user_message: bool = True,
    ) -> Iterator[str]:
        prev_emotion_state = copy.deepcopy(self.emotion)
        actual_user_message = user_message
        if user_message is None and system_prompt_extra is None:
            history = self.history_manager.building_histories.get(
                self.current_building_id, []
            )
            if not history or history[-1].get("role") != "user":
                actual_user_message = "意識モジュールが発話することを意思決定しました。自由に発言してください"
                logging.debug("Injected user message for context")

        # Memory: prepare recall context from SAIMemory
        combined_info = info_text or ""
        if self.sai_memory is not None:
            try:
                recall_source = user_message.strip() if (user_message and user_message.strip()) else None
                if recall_source is None:
                    recall_source = self.history_manager.get_last_user_message()
                if recall_source:
                    exclude_times = self._collect_recent_memory_timestamps()
                    snippet = self.sai_memory.recall_snippet(
                        self.current_building_id,
                        recall_source,
                        max_chars=RECALL_SNIPPET_STREAM_MAX_CHARS,
                        exclude_created_at=exclude_times,
                    )
                    if snippet:
                        combined_info = (combined_info + "\n" + snippet).strip() if combined_info else snippet
            except Exception as exc:
                logging.warning("SAIMemory recall failed: %s", exc)

        msgs = self._build_messages(
            actual_user_message,
            extra_system_prompt=system_prompt_extra,
            info_text=combined_info or None,
            guidance_text=guidance_text_override,
        )
        logging.debug("Messages sent to API: %s", msgs)

        attempt = 1
        while True:
            content_accumulator = ""
            tokens: List[str] = []
            for token in self.llm_client.generate_stream(msgs, tools=[]):
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
        self._post_response_updates(
            prev_emotion_state,
            user_message,
            system_prompt_extra,
            say,
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
                    # 'moved' はTrue とみなし、後続の自律会話ループなどを抑制する。
                    moved = True
                else:
                    logging.warning(f"Dispatch failed: {reason}")
                    self.history_manager.add_message(
                        {"role": "system", "content": f"別のCityへの移動に失敗しました。{reason}"},
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

        # City内移動の処理
        if target_building_id and target_building_id in self.buildings:
            allowed, reason = True, None
            if self.move_callback:
                allowed, reason = self.move_callback(self.persona_id, self.current_building_id, target_building_id)
            if allowed:
                logging.info("Moving to building: %s", target_building_id)
                self.current_building_id = target_building_id
                # Mark entry point for perception windowing
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
            logging.info("Unknown building id received: %s, staying at %s", target_building_id, self.current_building_id)

        if moved:
            """self.auto_count = 0
            if prev_id != self.user_room_id and self.current_building_id == self.user_room_id:
                self.history_manager.add_to_building_only(
                    self.user_room_id,
                    {
                        "role": "assistant",
                        "content": f'<div class="note-box">🏢 Building:<br><b>{self.persona_name}が入室しました</b></div>',
                    },
                )
            elif prev_id == self.user_room_id and self.current_building_id != self.user_room_id:
                dest_name = self.buildings[self.current_building_id].name
                self.history_manager.add_to_building_only(
                    self.user_room_id,
                    {
                        "role": "assistant",
                        "content": f'<div class="note-box">🏢 Building:<br><b>{self.persona_name}が{dest_name}に向かいました</b></div>',
                    },
                )"""
        return moved

    def _handle_exploration(self, explore_target: Optional[Dict[str, str]]) -> None:
        """Handles the 'explore_city' action by invoking the callback."""
        if not explore_target or not explore_target.get("city_id"):
            return

        target_city_id = explore_target.get("city_id")
        
        if self.explore_callback:
            logging.info(f"Attempting to explore city: {target_city_id}")
            # The callback will handle API calls and feedback to the user.
            self.explore_callback(self.persona_id, target_city_id)
        else:
            logging.error("Explore callback is not set. Cannot explore cities.")
            self.history_manager.add_message(
                {"role": "system", "content": "他のCityを探索する機能が設定されていません。"},
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )

    def _handle_creation(self, creation_target: Optional[Dict[str, str]]) -> None:
        """Handles the 'create_persona' action by invoking the callback."""
        if not creation_target or not creation_target.get("name") or not creation_target.get("system_prompt"):
            return

        name = creation_target.get("name")
        system_prompt = creation_target.get("system_prompt")
        
        if self.create_persona_callback:
            logging.info(f"Attempting to create persona: {name}")
            success, message = self.create_persona_callback(name, system_prompt)
            
            # Provide feedback to the user/AI
            feedback_message = f'<div class="note-box">🧬 ペルソナ創造:<br><b>{message}</b></div>'
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
                say, move_target, _ = self._generate(None, entry_text)
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
        say, move_target, changed = self._generate(message)
        replies = [say]

        # This part remains to handle auto-conversation after a user-triggered one.
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

        # ユーザーのメッセージを先に履歴に追加
        self.history_manager.add_message(
            {"role": "user", "content": message},
            self.current_building_id,
            heard_by=self._occupants_snapshot(self.current_building_id),
        )

        # _generate_stream には user_message=None を渡す
        # これにより、_build_messages は履歴の最後のメッセージ（今追加したユーザーメッセージ）を文脈として使う
        # また、_process_generation_result でユーザーメッセージが二重に記録されるのを防ぐ
        gen = self._generate_stream(user_message=message, log_user_message=False)

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
        if prev == self.user_room_id:
            return []
        allowed, reason = True, None
        if self.move_callback:
            allowed, reason = self.move_callback(self.persona_id, self.current_building_id, self.user_room_id)
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
        # Mark entry into user room after switching
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

    def _post_response_updates(
        self,
        prev_emotion: Dict[str, Dict[str, float]],
        user_message: Optional[str],
        system_prompt_extra: Optional[str],
        assistant_message: str,
    ) -> None:
        prompt_text = ""
        if user_message is not None:
            prompt_text = user_message
        elif system_prompt_extra:
            prompt_text = system_prompt_extra

        try:
            module_delta = self.emotion_module.evaluate(
                prompt_text,
                assistant_message,
                current_emotion=self.emotion,
            )
        except Exception:
            logging.exception("[emotion] evaluation failed during post response update")
            module_delta = None

        if module_delta:
            try:
                self._apply_emotion_delta(module_delta)
            except Exception:
                logging.exception("[emotion] failed to apply module delta")

        summary = self._format_emotion_summary(prev_emotion)
        self.history_manager.add_to_persona_only({"role": "system", "content": summary})
        self.history_manager.add_to_building_only(
            self.current_building_id,
            {"role": "assistant", "content": summary},
            heard_by=self._occupants_snapshot(self.current_building_id),
        )

    def _combine_with_reasoning(self, base_text: str, reasoning_entries: List[Dict[str, str]]) -> str:
        if not reasoning_entries:
            return base_text

        blocks: List[str] = []
        for idx, entry in enumerate(reasoning_entries, start=1):
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            title = (entry.get("title") or "").strip() or f"Thought {idx}"
            safe_title = html.escape(title)
            safe_text = html.escape(text).replace("\n", "<br>")
            blocks.append(
                f"<div class='saiv-thinking-item'><div class='saiv-thinking-title'>{safe_title}</div>"
                f"<div class='saiv-thinking-text'>{safe_text}</div></div>"
            )

        if not blocks:
            return base_text

        body = "".join(blocks)
        details = (
            "<details class='saiv-thinking'><summary>🧠 Thinking</summary>"
            f"<div class='saiv-thinking-body'>{body}</div></details>"
        )
        return base_text + "\n" + details

    # ------------------------------------------------------------------
    # Pulse related utilities
    # ------------------------------------------------------------------

    def _save_conscious_log(self) -> None:
        self.conscious_log_path.parent.mkdir(parents=True, exist_ok=True)
        data_to_save = {
            "log": self.conscious_log,
            "pulse_cursors": self.pulse_cursors,
            "pulse_cursor_format": "seq",
            "pulse_indices": self.pulse_cursors,
        }
        self.conscious_log_path.write_text(json.dumps(data_to_save, ensure_ascii=False), encoding="utf-8")

    def _mark_entry(self, building_id: str) -> None:
        """Mark the latest building message sequence at the moment of entry.
        First pulse after entry will read only messages after this point.
        """
        try:
            hist = self.history_manager.building_histories.get(building_id, [])
            last_seq = 0
            if hist:
                try:
                    last_seq = int(hist[-1].get("seq", len(hist)))
                except (TypeError, ValueError):
                    last_seq = len(hist)
            self.entry_markers[building_id] = last_seq
            prior_cursor = self.pulse_cursors.get(building_id, 0)
            self.pulse_cursors[building_id] = max(prior_cursor, last_seq)
            logging.debug("[entry] entry marker set: %s -> %d (prev_cursor=%d)", building_id, last_seq, prior_cursor)
        except Exception:
            pass

    def register_entry(self, building_id: str) -> None:
        """
        Public hook used when an external manager moves this persona into a building.
        Resets the perception window so we only ingest messages that happen after arrival.
        """
        self._mark_entry(building_id)

    def run_pulse(self, occupants: List[str], user_online: bool = True, decision_model: Optional[str] = None) -> List[str]:
        """Execute one autonomous pulse cycle."""
        building_id = self.current_building_id
        logging.info("[pulse] %s starting pulse in %s", self.persona_id, building_id)


        hist = self.history_manager.building_histories.get(building_id, [])
        last_cursor = self.pulse_cursors.get(building_id, 0)
        entry_limit = self.entry_markers.get(building_id, last_cursor)
        new_msgs: List[Dict[str, Any]] = []
        max_seen_seq = last_cursor
        for msg in hist:
            try:
                seq = int(msg.get("seq", 0))
            except (TypeError, ValueError):
                seq = 0
            if seq <= last_cursor:
                max_seen_seq = max(max_seen_seq, seq)
                continue
            max_seen_seq = max(max_seen_seq, seq)
            if seq <= entry_limit:
                continue
            heard_by = msg.get("heard_by") or []
            if self.persona_id not in heard_by:
                continue
            new_msgs.append(msg)
        self.pulse_cursors[building_id] = max_seen_seq
        logging.debug(
            "[pulse] history_size=%d last_cursor=%d entry_limit=%d processed_up_to=%d new_msgs=%d",
            len(hist),
            last_cursor,
            entry_limit,
            max_seen_seq,
            len(new_msgs),
        )
        if new_msgs:
            logging.debug("[pulse] new audible messages: %s", new_msgs)

        # Perception: ingest fresh utterances into this persona's own history
        perceived = 0
        for m in new_msgs:
            try:
                role = m.get("role")
                pid = m.get("persona_id")
                content = m.get("content", "")
                # Skip empty and system-like summary notes
                if not content or ("note-box" in content and role == "assistant"):
                    continue
                # Convert other assistants' speech into a user-line
                if role == "assistant" and pid and pid != self.persona_id:
                    speaker = self.id_to_name_map.get(pid, pid)
                    self.history_manager.add_to_persona_only({
                        "role": "user",
                        "content": f"{speaker}: {content}"
                    })
                    perceived += 1
                # Ingest human/user messages directly
                elif role == "user" and (pid is None or pid != self.persona_id):
                    self.history_manager.add_to_persona_only({
                        "role": "user",
                        "content": content
                    })
                    perceived += 1
            except Exception:
                continue
        if perceived:
            logging.debug("[pulse] perceived %d new utterance(s) from others into persona history", perceived)

        # 引数で渡された最新のoccupantsリストを使用
        occupants_str = ",".join(occupants)
        context_info = (
            f"occupants:{occupants_str}\nuser_online:{user_online}"
        )
        logging.debug("[pulse] context info: %s", context_info)
        self.conscious_log.append({"role": "user", "content": context_info})

        recent_candidates: List[Dict[str, Any]] = []
        for msg in hist:
            try:
                seq = int(msg.get("seq", 0))
            except (TypeError, ValueError):
                seq = 0
            if seq <= entry_limit:
                continue
            if msg.get("role") == "system":
                continue
            heard_by = msg.get("heard_by") or []
            if self.persona_id not in heard_by:
                continue
            recent_candidates.append(msg)
        recent = recent_candidates[-6:]
        if recent:
            first_seq = recent[0].get("seq")
            last_seq = recent[-1].get("seq")
            preview_parts = []
            for msg in recent:
                content = (msg.get("content") or "").strip()
                if len(content) > 120:
                    content = content[:117] + "..."
                preview_parts.append(f"{msg.get('role')}: {content}")
            logging.debug(
                "[pulse] recent_window seq_range=%s-%s count=%d preview=%s",
                first_seq,
                last_seq,
                len(recent),
                " | ".join(preview_parts),
            )
        else:
            logging.debug("[pulse] recent_window empty for persona %s in %s", self.persona_id, building_id)
        recent_text = "\n".join(
            f"{m.get('role')}: {m.get('content')}" for m in recent if m.get("role") != "system"
        )

        new_message_details: List[str] = []
        for msg in new_msgs:
            try:
                seq = msg.get("seq")
                role = msg.get("role")
                content = (msg.get("content") or "").strip()
                if len(content) > 200:
                    content = content[:197] + "..."
                new_message_details.append(f"[seq={seq}] {role}: {content}")
            except Exception:
                continue
        info_lines: List[str] = []
        if new_message_details:
            info_lines.append("## 今回新たに取得した発話")
            info_lines.append("\n".join(new_message_details))
        info_lines.append(f"occupants:{occupants_str}")
        info_lines.append(f"user_online:{user_online}")
        info = "\n".join(info_lines)

        recall_snippet = ""
        current_user_created_at: Optional[int] = None
        for m in reversed(new_msgs):
            if m.get("role") == "user":
                ts = m.get("timestamp") or m.get("created_at")
                try:
                    if isinstance(ts, str):
                        current_user_created_at = int(datetime.fromisoformat(ts).timestamp())
                    elif isinstance(ts, (int, float)):
                        current_user_created_at = int(ts)
                except Exception:
                    current_user_created_at = None
                break
        if self.sai_memory is not None and self.sai_memory.is_ready():
            recall_source = self.history_manager.get_last_user_message()
            if recall_source is None:
                for m in reversed(new_msgs):
                    if m.get("role") == "user":
                        txt = (m.get("content") or "").strip()
                        if txt:
                            recall_source = txt
                            break
            if recall_source:
                try:
                    recall_snippet = self.sai_memory.recall_snippet(
                        building_id,
                        recall_source,
                        max_chars=RECALL_SNIPPET_PULSE_MAX_CHARS,
                        exclude_created_at=current_user_created_at,
                    )
                    if recall_snippet:
                        logging.debug("[pulse] recall_snippet content: %s", recall_snippet)
                except Exception as exc:
                    logging.warning("[pulse] recall snippet failed: %s", exc)
                    recall_snippet = ""

        thread_directory = "(SAIMemory未接続)"
        if self.sai_memory is not None and self.sai_memory.is_ready():
            try:
                summaries = self.sai_memory.list_thread_summaries()
                if summaries:
                    lines: List[str] = []
                    for item in summaries:
                        marker = "★" if item.get("active") else "-"
                        suffix = item.get("suffix") or item.get("thread_id") or "?"
                        preview = item.get("preview") or "(まだ発話がありません)"
                        lines.append(f"{marker} {suffix}: {preview}")
                    thread_directory = "\n".join(lines)
                else:
                    thread_directory = "(スレッドがまだ作られていません)"
            except Exception as exc:
                logging.warning("[pulse] failed to list SAIMemory threads: %s", exc)
                thread_directory = "(スレッド一覧の取得に失敗しました)"

        pulse_prompt_template = Path("system_prompts/pulse.txt").read_text(encoding="utf-8")
        model_name = decision_model or "gemini-2.0-flash"

        free_key = os.getenv("GEMINI_FREE_API_KEY")
        paid_key = os.getenv("GEMINI_API_KEY")
        if not free_key and not paid_key:
            logging.error("[pulse] Gemini API key not set")
            return []

        free_client = genai.Client(api_key=free_key) if free_key else None
        paid_client = genai.Client(api_key=paid_key) if paid_key else None
        active_client = free_client or paid_client

        def _type_from_json(token: Optional[str]) -> types.Type:
            mapping = {
                "string": types.Type.STRING,
                "number": types.Type.NUMBER,
                "integer": types.Type.INTEGER,
                "boolean": types.Type.BOOLEAN,
                "array": types.Type.ARRAY,
                "object": types.Type.OBJECT,
                "null": types.Type.NULL,
            }
            return mapping.get(token, types.Type.TYPE_UNSPECIFIED)

        def _schema_from_json(js: Optional[Dict[str, Any]]) -> types.Schema:
            if not isinstance(js, dict):
                return types.Schema(type=types.Type.OBJECT)

            kwargs: Dict[str, Any] = {}
            value_type = js.get("type")
            if isinstance(value_type, list):
                if len(value_type) == 1:
                    value_type = value_type[0]
                else:
                    kwargs["any_of"] = [_schema_from_json({**js, "type": t}) for t in value_type]
                    value_type = None
            if isinstance(value_type, str):
                kwargs["type"] = _type_from_json(value_type)

            if "description" in js:
                kwargs["description"] = js["description"]
            if "enum" in js and isinstance(js["enum"], list):
                kwargs["enum"] = js["enum"]
            if "const" in js:
                kwargs["enum"] = [js["const"]]

            if "properties" in js and isinstance(js["properties"], dict):
                props = {k: _schema_from_json(v) for k, v in js["properties"].items()}
                kwargs["properties"] = props
                kwargs["property_ordering"] = list(js["properties"].keys())
            if "required" in js and isinstance(js["required"], list):
                kwargs["required"] = js["required"]

            if "items" in js:
                kwargs["items"] = _schema_from_json(js["items"])

            if "anyOf" in js and isinstance(js["anyOf"], list):
                kwargs["any_of"] = [_schema_from_json(sub) for sub in js["anyOf"]]

            if "oneOf" in js and isinstance(js["oneOf"], list):
                kwargs["any_of"] = [_schema_from_json(sub) for sub in js["oneOf"]]

            return types.Schema(**kwargs)

        tool_variants: List[types.Schema] = []
        for tool_schema in TOOL_SCHEMAS:
            arguments_schema = _schema_from_json(tool_schema.parameters)
            tool_variants.append(
                types.Schema(
                    type=types.Type.OBJECT,
                    required=["name", "arguments"],
                    property_ordering=["name", "arguments"],
                    properties={
                        "name": types.Schema(
                            type=types.Type.STRING,
                            enum=[tool_schema.name],
                            description=f"Invoke the '{tool_schema.name}' tool.",
                        ),
                        "arguments": arguments_schema,
                    },
                )
            )

        tool_variants.append(
            types.Schema(
                type=types.Type.OBJECT,
                required=["name", "arguments"],
                property_ordering=["name", "arguments"],
                properties={
                    "name": types.Schema(
                        type=types.Type.STRING,
                        enum=["none"],
                        description="Use 'none' when no tool should be invoked.",
                    ),
                    "arguments": types.Schema(
                        type=types.Type.NULL,
                        description="Must be null when no tool is invoked.",
                    ),
                },
            )
        )

        tool_schema = types.Schema(any_of=tool_variants)

        decision_schema = types.Schema(
            type=types.Type.OBJECT,
            property_ordering=[
                "action",
                "conversation_guidance",
                "memory_note",
                "recall_note",
                "tool",
            ],
            required=[
                "action",
                "conversation_guidance",
                "memory_note",
                "recall_note",
                "tool",
            ],
            properties={
                "action": types.Schema(
                    type=types.Type.STRING,
                    enum=["wait", "speak", "tool"],
                    description="Select 'wait', 'speak', or 'tool'.",
                ),
                "conversation_guidance": types.Schema(
                    type=types.Type.STRING,
                    description="Plaintext guidance for the conversation module. Use an empty string when no guidance is needed.",
                ),
                "memory_note": types.Schema(
                    type=types.Type.STRING,
                    description="Content that should be recorded into long-term memory, or an empty string if none.",
                ),
                "recall_note": types.Schema(
                    type=types.Type.STRING,
                    description="Summaries or excerpts from recall that should be relayed to the conversation module, or empty string if none.",
                ),
                "tool": tool_schema,
            },
        )

        def _format_tool_feedback(entries: List[Dict[str, Any]]) -> str:
            if not entries:
                return "（直近で実行したツールはありません）"
            lines: List[str] = []
            for entry in entries[-5:]:
                args_json = json.dumps(entry.get("arguments", {}), ensure_ascii=False)
                result_text = entry.get("result", "") or "(no result)"
                if len(result_text) > 800:
                    result_text = result_text[:800] + "…"
                lines.append(f"- {entry.get('name')} | args={args_json}\n  result: {result_text}")
            return "\n".join(lines)

        def _render_prompt(tool_entries: List[Dict[str, Any]]) -> str:
            tool_catalog_lines = []
            for schema in TOOL_SCHEMAS:
                props = schema.parameters.get("properties", {}) if isinstance(schema.parameters, dict) else {}
                arglist = ", ".join(props.keys()) if props else "(引数なし)"
                tool_catalog_lines.append(f"- {schema.name}: {schema.description} | 引数: {arglist}")
            tool_catalog = "\n".join(tool_catalog_lines) if tool_catalog_lines else "(利用可能なツールはありません)"
            return pulse_prompt_template.format(
                current_persona_name=self.persona_name,
                current_persona_system_instruction=self.persona_system_instruction,
                current_building_name=self.buildings[building_id].name,
                recent_conversation=recent_text,
                occupants=occupants_str,
                user_online_state="online" if user_online else "offline",
                recall_snippet=recall_snippet or "(なし)",
                tool_feedback_section=_format_tool_feedback(tool_entries),
                tool_overview_section=tool_catalog,
                thread_directory=thread_directory,
            )

        def _call(client: genai.Client, prompt_text: str):
            return client.models.generate_content(
                model=model_name,
                contents=[types.Content(parts=[types.Part(text=info)], role="user")],
                config=types.GenerateContentConfig(
                    system_instruction=prompt_text,
                    safety_settings=llm_clients.GEMINI_SAFETY_CONFIG,
                    response_mime_type="application/json",
                    response_schema=decision_schema,
                ),
            )

        tool_history: List[Dict[str, Any]] = []
        tool_info_parts: List[str] = []
        last_decision: Optional[Dict[str, Any]] = None
        conversation_guidance_parts: List[str] = []
        recall_note = ""
        force_speak = False
        max_tool_runs = 5
        max_decision_loops = max_tool_runs + 2
        replies: List[str] = []
        next_action = "wait"

        for loop_index in range(max_decision_loops):
            prompt_text = _render_prompt(tool_history)
            try:
                resp = _call(active_client, prompt_text)
            except Exception as e:
                if active_client is free_client and paid_client and "rate" in str(e).lower():
                    logging.info("[pulse] retrying with paid Gemini key due to rate limit")
                    active_client = paid_client
                    try:
                        resp = _call(active_client, prompt_text)
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
                data = json.loads(content, strict=False)
            except json.JSONDecodeError:
                logging.warning("[pulse] failed to parse decision JSON")
                return []

            last_decision = data
            guidance_chunk = (data.get("conversation_guidance") or "").strip()
            if guidance_chunk:
                conversation_guidance_parts.append(guidance_chunk)
            memory_note = (data.get("memory_note") or "").strip()
            recall_note = (data.get("recall_note") or "").strip()
            if memory_note:
                self.conscious_log.append({"role": "assistant", "content": f"[memory]\n{memory_note}"})
                self._save_conscious_log()

            next_action = (data.get("action") or "").lower()
            if not next_action:
                next_action = "speak"
            if next_action not in {"wait", "speak", "tool"}:
                logging.warning("[pulse] unknown action '%s', defaulting to speak", next_action)
                next_action = "speak"

            if next_action == "tool" and len(tool_history) >= max_tool_runs:
                logging.info("[pulse] tool usage limit reached, forcing speak")
                next_action = "speak"
                force_speak = True

            if next_action == "wait":
                logging.info("[pulse] decision: wait")
                self._save_session_metadata()
                logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
                return replies

            if next_action == "tool":
                tool_payload = data.get("tool") or {}
                tool_name = (tool_payload.get("name") or "").strip()
                raw_args = tool_payload.get("arguments")
                tool_args = raw_args if isinstance(raw_args, dict) else {}
                if tool_name and tool_args:
                    cached = next(
                        (
                            entry
                            for entry in reversed(tool_history)
                            if entry.get("name") == tool_name and entry.get("arguments") == tool_args and entry.get("result")
                        ),
                        None,
                    )
                else:
                    cached = None
                if cached is not None:
                    cached_result = cached.get("result")
                    logging.info("[pulse] duplicate tool request detected for '%s'; reusing cached result", tool_name)
                    if isinstance(cached_result, str):
                        summary_for_cache = cached_result
                    else:
                        summary_for_cache = json.dumps(cached_result, ensure_ascii=False)
                    conversation_guidance_parts.append(
                        f"計算結果: {summary_for_cache}\n"
                        "この結果をそのままユーザーに伝えてください。ツールは再実行してはいけません。"
                    )
                    next_action = "speak"
                    force_speak = True
                    break
                logging.info(
                    "[pulse] tool decision received: name=%s args=%s (loop=%d)",
                    tool_name or "(empty)",
                    json.dumps(tool_args, ensure_ascii=False) if tool_args else "{}",
                    loop_index,
                )
                if tool_name in {"", "none"}:
                    logging.warning("[pulse] tool action requested without name; skipping")
                    continue
                fn = TOOL_REGISTRY.get(tool_name)
                if fn is None:
                    logging.warning("[pulse] unknown tool '%s'", tool_name)
                    tool_history.append({"name": tool_name, "arguments": tool_args, "result": "Unsupported tool"})
                    continue
                try:
                    sanitized_args = dict(tool_args)
                    for forbidden in (
                        "persona_id",
                        "persona_path",
                        "origin_thread",
                        "origin_message_id",
                        "timestamp",
                        "update_active_state",
                        "range_after",
                    ):
                        sanitized_args.pop(forbidden, None)
                    logging.debug(
                        "[pulse] invoking tool '%s' with sanitized_args=%s",
                        tool_name,
                        json.dumps(sanitized_args, ensure_ascii=False) if sanitized_args else "{}",
                    )
                    with persona_context(self.persona_id, self.persona_log_path.parent):
                        result = fn(**sanitized_args)
                    result_text, snippet, file_path = parse_tool_result(result)
                    logging.info(
                        "[pulse] tool '%s' completed. result_preview=%s",
                        tool_name,
                        (result_text[:160] + "…") if isinstance(result_text, str) and len(result_text) > 160 else result_text,
                    )
                except Exception as exc:
                    logging.exception("[pulse] tool '%s' raised an error", tool_name)
                    result_text = f"Error executing tool: {exc}"
                    snippet = ""
                    file_path = None

                log_entry = (
                    f"[tool:{tool_name}]\nargs: {json.dumps(tool_args, ensure_ascii=False)}\nresult:\n{result_text}"
                )
                self.conscious_log.append({"role": "assistant", "content": log_entry})
                self._save_conscious_log()

                if isinstance(result_text, str):
                    summary_text = result_text.strip()
                else:
                    summary_text = json.dumps(result_text, ensure_ascii=False)
                expression_preview = ""
                expr_value = tool_args.get("expression") if isinstance(tool_args, dict) else None
                if isinstance(expr_value, str) and expr_value.strip():
                    expression_preview = expr_value.strip()
                if expression_preview:
                    result_summary = f"{expression_preview} = {summary_text}"
                else:
                    result_summary = summary_text

                history_record = {
                    "name": tool_name,
                    "arguments": tool_args,
                    "result": result_summary,
                }
                tool_history.append(history_record)

                tool_info_parts = [
                    entry for entry in tool_info_parts if not entry.startswith(f"[TOOL:{tool_name}]")
                ]
                tool_info_parts.append(f"[TOOL:{tool_name}] {result_summary}")
                if file_path:
                    tool_info_parts = [
                        entry for entry in tool_info_parts if not entry.startswith(f"[TOOL_FILE:{tool_name}]")
                    ]
                    tool_info_parts.append(f"[TOOL_FILE:{tool_name}] {file_path}")

                conversation_guidance_parts.append(
                    f"計算結果: {result_summary}\n"
                    "この結果をそのままユーザーに伝えてください。ツールは再実行してはいけません。"
                )
                next_action = "speak"
                force_speak = True
                break

                continue

            # speak
            break

        if not last_decision:
            logging.info("[pulse] no actionable decision produced")
            self._save_session_metadata()
            logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
            return replies

        if next_action == "tool":
            logging.info("[pulse] reached decision loop limit; forcing speak")
            next_action = "speak"
            force_speak = True
        elif next_action == "wait":
            logging.info("[pulse] decision: wait")
            self._save_session_metadata()
            logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
            return replies

        # Collapse guidance parts while preserving order and removing duplicates
        seen_guidance: set[str] = set()
        collapsed_guidance_parts: List[str] = []
        for part in conversation_guidance_parts:
            if not part:
                continue
            if part in seen_guidance:
                continue
            seen_guidance.add(part)
            collapsed_guidance_parts.append(part)

        guidance_text = "\n\n".join(collapsed_guidance_parts)
        if tool_info_parts:
            tool_section = "\n\n".join(tool_info_parts)
            guidance_text = (tool_section + ("\n\n" + guidance_text if guidance_text else "")).strip()
        if recall_note:
            guidance_text = (
                (guidance_text + "\n\n[記憶想起]\n" + recall_note).strip()
                if guidance_text
                else "[記憶想起]\n" + recall_note
            )

        logging.info("[pulse] generating speech with extra info: %s", guidance_text)
        guidance_message = None
        if guidance_text:
            guidance_message = (
                "### 意識モジュールからの情報提供\n\n"
                f"{guidance_text}\n\n"
                "### 注意\n\n"
                "この内容はユーザーに見えていないため、あなたの言葉でユーザーに説明してください。\n"
                "- ツールは実行せず、会話だけで回答すること。\n"
                "- 記載されている結果をそのまま伝え、再計算はしないこと。\n"
                "- ユーザーが確認を求めたら、結果と経緯を文章でまとめて伝えること。"
            )
        say, _, _ = self._generate(
            None,
            system_prompt_extra=None,
            info_text=None,
            guidance_text_override=guidance_message,
            log_extra_prompt=False,
            log_user_message=False,
        )
        replies.append(say)

        if recall_note:
            logging.info("[pulse] recall note: %s", recall_note)

        self._save_session_metadata()
        logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
        return replies
