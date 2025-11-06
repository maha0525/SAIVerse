"""Messaging and LLM generation helpers shared by persona core."""
from __future__ import annotations

import copy
import html
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from persona.constants import (
    RECALL_SNIPPET_MAX_CHARS,
    RECALL_SNIPPET_STREAM_MAX_CHARS,
)
from model_configs import model_supports_images
from llm_clients import get_llm_client


class PersonaGenerationMixin:
    """Shared behaviours for building prompts and handling LLM responses."""

    action_handler: Any
    context_length: int
    current_building_id: str
    emotion: Dict[str, Dict[str, float]]
    emotion_module: Any
    history_manager: Any
    llm_client: Any
    pending_attachment_metadata: List[Dict[str, Any]]
    persona_id: str
    persona_name: str
    persona_system_instruction: str
    sai_memory: Any
    timezone: Any

    def set_model(self, model: str, context_length: int, provider: str) -> None:
        self.model = model
        self.context_length = context_length
        self.model_supports_images = model_supports_images(model)
        self.llm_client = get_llm_client(model, provider, context_length)

    def _build_messages(
        self,
        user_message: Optional[str],
        extra_system_prompt: Optional[str] = None,
        info_text: Optional[str] = None,
        guidance_text: Optional[str] = None,
        user_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        building = self.buildings[self.current_building_id]
        now_local = datetime.now(self.timezone)
        current_time = now_local.strftime("%H:%M")
        system_text = self.common_prompt.format(
            current_building_name=building.name,
            current_building_system_instruction=building.system_instruction.format(
                current_time=current_time
            ),
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
        pulse_id = getattr(self, "_current_pulse_id", None)
        history_msgs = self.history_manager.get_recent_history(
            history_limit,
            required_tags=["conversation"],
            pulse_id=pulse_id,
        )
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

        sanitized_history: List[Dict[str, Any]] = []
        for message in history_msgs:
            role = message.get("role", "")
            content = message.get("content", "")
            if role == "system" and "### 意識モジュールからの情報提供" in content:
                continue
            sanitized: Dict[str, Any] = {"role": role, "content": content}
            metadata = message.get("metadata")
            if isinstance(metadata, dict):
                sanitized["metadata"] = copy.deepcopy(metadata)
            sanitized_history.append(sanitized)

        messages = [{"role": "system", "content": system_text}] + sanitized_history
        if guidance_text:
            messages.append({"role": "system", "content": guidance_text})
        if extra_system_prompt:
            messages.append({"role": "system", "content": extra_system_prompt})
        if user_message:
            user_entry: Dict[str, Any] = {"role": "user", "content": user_message}
            if isinstance(user_metadata, dict):
                user_entry["metadata"] = copy.deepcopy(user_metadata)
                logging.debug(
                    "[persona_core] user message metadata keys=%s",
                    list(user_metadata.keys()),
                )
            messages.append(user_entry)
        return messages

    def _collect_recent_memory_timestamps(self) -> List[int]:
        recent = self.history_manager.get_recent_history(self.context_length)
        values: List[int] = []
        seen = set()
        for message in recent:
            value = self._timestamp_to_epoch(
                message.get("created_at"), message.get("timestamp")
            )
            if value is None or value in seen:
                continue
            seen.add(value)
            values.append(value)
        logging.debug(
            "[recall] collected recent timestamps count=%d values=%s",
            len(values),
            values,
        )
        return values

    def _combine_with_reasoning(
        self, base_text: str, reasoning_entries: List[Dict[str, str]]
    ) -> str:
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
                "<div class='saiv-thinking-item'><div class='saiv-thinking-title'>"
                f"{safe_title}</div><div class='saiv-thinking-text'>{safe_text}</div></div>"
            )

        if not blocks:
            return base_text

        body = "".join(blocks)
        details = (
            "<details class='saiv-thinking'><summary>🧠 Thinking</summary>"
            f"<div class='saiv-thinking-body'>{body}</div></details>"
        )
        return base_text + "\n" + details

    def _process_generation_result(
        self,
        content: str,
        user_message: Optional[str],
        system_prompt_extra: Optional[str],
        log_extra_prompt: bool = True,
        user_metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Optional[Dict[str, str]], bool]:
        say, actions = self.action_handler.parse_response(content)
        move_target, _, delta = self.action_handler.execute_actions(actions)

        explore_target = None
        for action in actions:
            if action.get("action") == "explore_city":
                explore_target = {"city_id": action.get("city_id")}
                break

        creation_target = None
        for action in actions:
            if action.get("action") == "create_persona":
                creation_target = {
                    "name": action.get("name"),
                    "system_prompt": action.get("system_prompt"),
                }
                break

        if delta:
            self._apply_emotion_delta(delta)

        if system_prompt_extra and log_extra_prompt:
            self.history_manager.add_message(
                {"role": "user", "content": system_prompt_extra},
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )
        if user_message:
            user_entry: Dict[str, Any] = {"role": "user", "content": user_message}
            if isinstance(user_metadata, dict):
                user_entry["metadata"] = copy.deepcopy(user_metadata)
            self.history_manager.add_message(
                user_entry,
                self.current_building_id,
                heard_by=self._occupants_snapshot(self.current_building_id),
            )

        reasoning_entries = self.llm_client.consume_reasoning()
        attachments_from_llm = self.llm_client.consume_attachments()
        combined_media: List[Dict[str, Any]] = []
        for meta in attachments_from_llm + self.pending_attachment_metadata:
            if not isinstance(meta, dict):
                continue
            media_items = meta.get("media")
            if not isinstance(media_items, list):
                continue
            for item in media_items:
                if isinstance(item, dict):
                    combined_media.append(copy.deepcopy(item))
        self.pending_attachment_metadata = []
        metadata_payload: Optional[Dict[str, Any]] = None
        if combined_media:
            metadata_payload = {"media": combined_media}

        building_content = self._combine_with_reasoning(content, reasoning_entries)
        persona_msg: Dict[str, Any] = {"role": "assistant", "content": content}
        building_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": building_content,
        }
        if metadata_payload:
            persona_msg["metadata"] = metadata_payload
            building_msg["metadata"] = metadata_payload
        self.history_manager.add_to_persona_only(persona_msg)
        self.history_manager.add_to_building_only(
            self.current_building_id,
            building_msg,
            heard_by=self._occupants_snapshot(self.current_building_id),
        )

        moved = self._handle_movement(move_target)
        self._handle_exploration(explore_target)
        self._handle_creation(creation_target)
        self._save_session_metadata()
        return say, move_target, moved

    def _generate(
        self,
        user_message: Optional[str],
        user_metadata: Optional[Dict[str, Any]] = None,
        system_prompt_extra: Optional[str] = None,
        info_text: Optional[str] = None,
        guidance_text_override: Optional[str] = None,
        log_extra_prompt: bool = True,
        log_user_message: bool = True,
    ) -> Tuple[str, Optional[Dict[str, str]], bool]:
        prev_emotion_state = copy.deepcopy(self.emotion)
        actual_user_message = user_message
        if user_message is None and system_prompt_extra is None:
            history = self.history_manager.building_histories.get(
                self.current_building_id, []
            )
            if not history or history[-1].get("role") != "user":
                actual_user_message = "意識モジュールが発話することを意思決定しました。自由に発言してください"
                logging.debug("Injected user message for context")

        combined_info = info_text or ""
        recall_visible: List[str] = []
        if self.sai_memory is not None:
            try:
                recall_source = (
                    user_message.strip()
                    if user_message and user_message.strip()
                    else None
                )
                if recall_source is None:
                    recall_source = self.history_manager.get_last_user_message()
                if recall_source:
                    exclude_times = self._collect_recent_memory_timestamps()
                    logging.debug(
                        "[recall] invoking recall_snippet building=%s source_preview=%s exclude_times=%s",
                        self.current_building_id,
                        recall_source[:120],
                        exclude_times,
                    )
                    snippet = self.sai_memory.recall_snippet(
                        self.current_building_id,
                        recall_source,
                        max_chars=RECALL_SNIPPET_MAX_CHARS,
                        exclude_created_at=exclude_times,
                    )
                    if snippet:
                        logging.debug("[memory] recall snippet content=%s", snippet[:400])
                        recall_visible.append(snippet)
                        # 注意: writing snippet to SAIMemory via append_persona_message disabled (would create loops)
            except Exception as exc:
                logging.warning("SAIMemory recall failed: %s", exc)

        info_payload = combined_info or None
        if recall_visible:
            recall_text = "\n".join(recall_visible)
            info_payload = (
                (combined_info + "\n" + recall_text).strip()
                if combined_info
                else recall_text
            )

        messages = self._build_messages(
            actual_user_message,
            extra_system_prompt=system_prompt_extra,
            info_text=info_payload,
            guidance_text=guidance_text_override,
            user_metadata=user_metadata if actual_user_message == user_message else None,
        )
        logging.debug("Messages sent to API: %s", messages)

        content = self.llm_client.generate(messages, tools=[])
        attempt = 1
        while content.strip() == "エラーが発生しました。" and attempt < 3:
            logging.warning("LLM generation failed; retrying in 10s (%d/3)", attempt)
            time.sleep(10)
            content = self.llm_client.generate(messages, tools=[])
            attempt += 1

        logging.info("AI Response :\n%s", content)
        say, move_target, changed = self._process_generation_result(
            content,
            user_message if log_user_message else None,
            system_prompt_extra,
            log_extra_prompt,
            user_metadata if log_user_message else None,
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
        user_metadata: Optional[Dict[str, Any]] = None,
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

        combined_info = info_text or ""
        if self.sai_memory is not None:
            try:
                recall_source = (
                    user_message.strip()
                    if user_message and user_message.strip()
                    else None
                )
                if recall_source is None:
                    recall_source = self.history_manager.get_last_user_message()
                if recall_source:
                    exclude_times = self._collect_recent_memory_timestamps()
                    logging.debug(
                        "[recall] invoking recall_snippet(stream) building=%s source_preview=%s exclude_times=%s",
                        self.current_building_id,
                        recall_source[:120],
                        exclude_times,
                    )
                    snippet = self.sai_memory.recall_snippet(
                        self.current_building_id,
                        recall_source,
                        max_chars=RECALL_SNIPPET_STREAM_MAX_CHARS,
                        exclude_created_at=exclude_times,
                    )
                    if snippet:
                        combined_info = (
                            (combined_info + "\n" + snippet).strip()
                            if combined_info
                            else snippet
                        )
            except Exception as exc:
                logging.warning("SAIMemory recall failed: %s", exc)

        messages = self._build_messages(
            actual_user_message,
            extra_system_prompt=system_prompt_extra,
            info_text=combined_info or None,
            guidance_text=guidance_text_override,
            user_metadata=user_metadata if actual_user_message == user_message else None,
        )
        logging.debug("Messages sent to API: %s", messages)

        attempt = 1
        while True:
            content_accumulator = ""
            tokens: List[str] = []
            for token in self.llm_client.generate_stream(messages, tools=[]):
                content_accumulator += token
                tokens.append(token)

            if content_accumulator.strip() != "エラーが発生しました。" or attempt >= 3:
                for token in tokens:
                    yield token
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
            user_metadata if log_user_message else None,
        )
        self._post_response_updates(
            prev_emotion_state,
            user_message,
            system_prompt_extra,
            say,
        )
        return (say, move_target, changed)

    def handle_user_input(
        self, message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        logging.info("User input: %s", message)
        if metadata:
            logging.debug(
                "[persona_core] handle_user_input received metadata keys=%s",
                list(metadata.keys()),
            )
        say, move_target, changed = self._generate(message, user_metadata=metadata)
        replies = [say]

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

    def handle_user_input_stream(
        self, message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Iterator[str]:
        logging.info("User input: %s", message)
        if metadata:
            logging.debug(
                "[persona_core] handle_user_input_stream received metadata keys=%s",
                list(metadata.keys()),
            )

        user_entry: Dict[str, Any] = {"role": "user", "content": message}
        if isinstance(metadata, dict):
            user_entry["metadata"] = metadata
        self.history_manager.add_message(
            user_entry,
            self.current_building_id,
            heard_by=self._occupants_snapshot(self.current_building_id),
        )

        generator = self._generate_stream(
            user_message=message, user_metadata=metadata, log_user_message=False
        )

        try:
            while True:
                yield next(generator)
        except StopIteration as stop:
            _, move_target, changed = stop.value

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
        for reply in extra_replies:
            yield reply


__all__ = ["PersonaGenerationMixin"]
