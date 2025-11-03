"""Pulse execution helpers for PersonaCore."""
from __future__ import annotations

import copy
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    genai = None  # type: ignore
    types = None  # type: ignore

import llm_clients
from llm_clients import get_llm_client
from llm_clients.gemini_utils import build_gemini_clients
from model_configs import get_context_length, get_model_provider
from persona.constants import RECALL_SNIPPET_PULSE_MAX_CHARS
from persona.tasks import TaskRecord
from tools import TOOL_REGISTRY, TOOL_SCHEMAS
from tools.context import persona_context
from tools.defs import parse_tool_result


class PersonaPulseMixin:
    """Manage autonomous pulse cycles, tool orchestration, and prompt assembly."""

    action_handler: Any
    buildings: Dict[str, Any]
    conscious_log: List[Dict[str, Any]]
    conscious_log_path: Any
    current_building_id: str
    entry_markers: Dict[str, int]
    history_manager: Any
    id_to_name_map: Dict[str, str]
    last_auto_prompt_times: Dict[str, float]
    llm_client: Any
    model: str
    persona_id: str
    persona_log_path: Any
    persona_name: str
    persona_system_instruction: str
    pulse_cursors: Dict[str, int]
    sai_memory: Any
    timezone: Any
    timezone_name: str

    def run_pulse(self, occupants: List[str], user_online: bool = True, decision_model: Optional[str] = None) -> List[str]:
        """Execute one autonomous pulse cycle."""
        building_id = self.current_building_id
        logging.info("[pulse] %s starting pulse in %s", self.persona_id, building_id)
        pulse_id = uuid.uuid4().hex
        previous_pulse_id = getattr(self, "_current_pulse_id", None)
        self._current_pulse_id = pulse_id
        def _log_phase(label: str) -> None:
            logging.debug(
                "[pulse] phase=%s persona=%s model=%s",
                label,
                self.persona_id,
                self.model,
            )

        phase = "start"
        _log_phase(phase)
        phase = "start"


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
                metadata = m.get("metadata") if isinstance(m, dict) else None
                if role == "assistant" and pid and pid != self.persona_id:
                    speaker = self.id_to_name_map.get(pid, pid)
                    entry = {
                        "role": "user",
                        "content": f"{speaker}: {content}"
                    }
                    if isinstance(metadata, dict):
                        entry["metadata"] = copy.deepcopy(metadata)
                    ts_value = m.get("timestamp")
                    if isinstance(ts_value, str):
                        entry["timestamp"] = ts_value
                    created_value = self._timestamp_to_epoch(m.get("created_at"), ts_value)
                    if created_value is not None:
                        entry["created_at"] = created_value
                    self.history_manager.add_to_persona_only(entry)
                    perceived += 1
                # Ingest human/user messages directly
                elif role == "user" and (pid is None or pid != self.persona_id):
                    entry = {
                        "role": "user",
                        "content": content
                    }
                    if isinstance(metadata, dict):
                        entry["metadata"] = copy.deepcopy(metadata)
                    ts_value = m.get("timestamp")
                    if isinstance(ts_value, str):
                        entry["timestamp"] = ts_value
                    created_value = self._timestamp_to_epoch(m.get("created_at"), ts_value)
                    if created_value is not None:
                        entry["created_at"] = created_value
                    self.history_manager.add_to_persona_only(entry)
                    perceived += 1
            except Exception:
                continue
        if perceived:
            logging.debug("[pulse] perceived %d new utterance(s) from others into persona history", perceived)
        phase = "perceived"
        _log_phase(phase)
        phase = "perceived"

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
        now_utc = datetime.now(dt_timezone.utc)
        now_local = now_utc.astimezone(self.timezone)
        current_datetime_local_str = self._format_local_timestamp(now_local)
        current_datetime_utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC+00:00")
        timezone_display = f"{self.timezone_name} ({self._format_timezone_offset(now_local)})"

        recent_lines: List[str] = []
        for msg in recent:
            if msg.get("role") == "system":
                continue
            content = (msg.get("content") or "").strip()
            content_formatted = content.replace("\n", "\n  ") if content else "(内容なし)"
            ts_raw = msg.get("timestamp") or msg.get("created_at")
            ts_utc = self._parse_timestamp_to_utc(ts_raw)
            if ts_utc:
                ts_label = self._format_local_timestamp(ts_utc.astimezone(self.timezone))
            else:
                ts_label = "時刻不明"
            recent_lines.append(f"- [{ts_label}] {msg.get('role')}: {content_formatted}")

        recent_text = "\n".join(recent_lines) if recent_lines else "(最近のメッセージはありません)"

        if self._last_conscious_prompt_time_utc is not None:
            elapsed_prompt = now_utc - self._last_conscious_prompt_time_utc
            elapsed_text = self._format_elapsed(elapsed_prompt)
            time_since_last_prompt = f"前回の意識パルスから {elapsed_text}経過しています。"
        else:
            time_since_last_prompt = "今回が初めての意識パルス実行です。"

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
        task_section = self._compose_task_summary()
        if task_section:
            info_lines.append("## タスク状況")
            info_lines.append(task_section)
        info = "\n".join(info_lines)
        self._record_perception_entry(info, pulse_id)

        recall_snippet = ""
        phase = "prompt_context"
        _log_phase(phase)
        phase = "prompt_context"
        current_user_created_at: Optional[int] = None
        for m in reversed(new_msgs):
            if m.get("role") == "user":
                current_user_created_at = self._timestamp_to_epoch(m.get("created_at"), m.get("timestamp"))
                break
        if self.sai_memory is not None and self.sai_memory.is_ready():
            recall_source = self.history_manager.get_last_user_message()
            logging.debug(
                "[pulse] recall prep new_msgs=%d last_user_message_preview=%s exclude_created_at=%s",
                len(new_msgs),
                (recall_source or "")[:120] if recall_source else None,
                current_user_created_at,
            )
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
                    else:
                        logging.debug("[pulse] recall_snippet returned empty")
                except Exception as exc:
                    logging.warning("[pulse] recall snippet failed: %s", exc)
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
        env_model = (os.getenv("SAIVERSE_PULSE_MODEL") or "").strip()
        model_name = decision_model or env_model or "gemini-2.0-flash"
        provider = get_model_provider(model_name)
        context_length = get_context_length(model_name)
        using_gemini = provider == "gemini"
        if decision_model is None and env_model:
            logging.info("[pulse] using SAIVERSE_PULSE_MODEL override: %s", model_name)

        free_client = None
        paid_client = None
        active_client = None
        pulse_client = None

        if using_gemini:
            if genai is None or types is None:
                logging.error("[pulse] Gemini SDK is not available but model '%s' requires it.", model_name)
                phase = "init_gemini_sdk_missing"
                logging.warning("[pulse] early exit (persona=%s phase=%s)", self.persona_id, phase)
                self._current_pulse_id = previous_pulse_id
                return []
            try:
                free_client, paid_client, active_client = build_gemini_clients()
            except RuntimeError:
                logging.error("[pulse] Gemini API key not set")
                phase = "init_gemini_key_missing"
                logging.warning("[pulse] early exit (persona=%s phase=%s)", self.persona_id, phase)
                self._current_pulse_id = previous_pulse_id
                return []
            logging.info("[pulse] using Gemini decision model %s (context=%d)", model_name, context_length)
        else:
            if not hasattr(self, "_pulse_llm_clients"):
                self._pulse_llm_clients: Dict[str, Any] = {}
            pulse_cache: Dict[str, Any] = self._pulse_llm_clients
            pulse_client = pulse_cache.get(model_name)
            if pulse_client is None:
                try:
                    pulse_client = get_llm_client(model_name, provider, context_length)
                except Exception as exc:
                    logging.error("[pulse] failed to initialize %s client for %s: %s", provider, model_name, exc)
                    phase = "init_model_client_failed"
                    logging.warning(
                        "[pulse] early exit (persona=%s phase=%s provider=%s model=%s)",
                        self.persona_id,
                        phase,
                        provider,
                        model_name,
                    )
                    self._current_pulse_id = previous_pulse_id
                    return []
                pulse_cache[model_name] = pulse_client
            logging.info(
                "[pulse] using %s decision model %s (context=%d)",
                provider,
                model_name,
                context_length,
            )
        phase = "decision_ready"
        _log_phase(phase)

        prompt_generated_at = now_utc

        def _finalize_and_return(result: List[str]) -> List[str]:
            logging.debug(
                "[pulse] finalize return (persona=%s phase=%s replies=%d)",
                self.persona_id,
                phase,
                len(result) if isinstance(result, list) else -1,
            )
            self._last_conscious_prompt_time_utc = prompt_generated_at
            self._current_pulse_id = previous_pulse_id
            return result

        decision_schema_dict: Dict[str, Any] = {
            "title": "PersonaPulseDecision",
            "type": "object",
            "additionalProperties": False,
            "required": ["perception", "todo", "decision"],
            "properties": {
                "perception": {
                    "type": "string",
                    "description": "Current self-observation and salient context.",
                },
                "todo": {
                    "type": "string",
                    "description": "Immediate plan. When speaking, write the actual utterance here.",
                },
                "decision": {
                    "type": "string",
                    "enum": ["wait", "speak", "tool"],
                    "description": "Select 'wait', 'speak', or 'tool'.",
                },
                "action": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "Identifier of the tool to execute when decision is 'tool'.",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments for the tool call.",
                        },
                    },
                    "required": ["tool"],
                },
            },
        }

        decision_schema = None
        if using_gemini:
            decision_schema = llm_clients.GeminiClient._schema_from_json(decision_schema_dict)  # type: ignore[attr-defined]

        phase = "decision_loop_setup"

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
            prompt = pulse_prompt_template.format(
                current_persona_name=self.persona_name,
                current_persona_system_instruction=self.persona_system_instruction,
                current_building_name=self.buildings[building_id].name,
                current_datetime_local=current_datetime_local_str,
                current_datetime_utc=current_datetime_utc_str,
                timezone_display=timezone_display,
                time_since_last_prompt=time_since_last_prompt,
                recent_conversation=recent_text,
                occupants=occupants_str,
                user_online_state="online" if user_online else "offline",
                recall_snippet=recall_snippet or "(なし)",
                tool_feedback_section=_format_tool_feedback(tool_entries),
                tool_overview_section=tool_catalog,
                thread_directory=thread_directory,
            )
            logging.debug(
                "[pulse] prompt preview for %s in %s:\n%s",
                self.persona_id,
                building_id,
                prompt,
            )
            return prompt

        def _call(client: Any, prompt_text: str):
            if using_gemini:
                config_kwargs: Dict[str, Any] = {
                    "system_instruction": prompt_text,
                    "safety_settings": llm_clients.GEMINI_SAFETY_CONFIG,
                    "response_mime_type": "application/json",
                }
                if decision_schema is not None:
                    config_kwargs["response_schema"] = decision_schema
                else:
                    logging.warning("[pulse] Gemini decision schema conversion failed; proceeding without schema enforcement.")
                return client.models.generate_content(
                    model=model_name,
                    contents=[types.Content(parts=[types.Part(text=info)], role="user")],
                    config=types.GenerateContentConfig(**config_kwargs),
                )
            assert pulse_client is not None
            messages = [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": info},
            ]
            return pulse_client.generate(messages, response_schema=decision_schema_dict)

        def _extract_json_text(raw: str) -> str:
            text = (raw or "").strip()
            if text.startswith("```"):
                segments = text.split("```")
                for segment in segments:
                    seg = segment.strip()
                    if seg.startswith("{") and seg.endswith("}"):
                        return seg
            if text.startswith("{") and text.endswith("}"):
                return text
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return text[start : end + 1]
            return text

        tool_history: List[Dict[str, Any]] = []
        tool_info_parts: List[str] = []
        last_decision: Optional[Dict[str, Any]] = None
        current_perception = ""
        current_todo = ""
        force_speak = False
        max_tool_runs = 5
        max_decision_loops = max_tool_runs + 2
        replies: List[str] = []
        next_action = "wait"
        phase = "decision_loop"
        _log_phase(phase)

        for loop_index in range(max_decision_loops):
            prompt_text = _render_prompt(tool_history)
            if using_gemini:
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
                            _log_phase("decision_model_exception_gemini_retry")
                            return _finalize_and_return([])
                    else:
                        logging.error("[pulse] Gemini call failed: %s", e)
                        _log_phase("decision_model_exception_gemini")
                        return _finalize_and_return([])
                content_raw = resp.text if hasattr(resp, "text") else str(resp)
            else:
                try:
                    raw_output = _call(None, prompt_text)
                except Exception as e:
                    logging.error("[pulse] decision model call failed: %s", e)
                    phase = "decision_model_exception"
                    _log_phase(phase)
                    return _finalize_and_return([])
                if isinstance(raw_output, str):
                    content_raw = raw_output
                else:
                    content_raw = json.dumps(raw_output, ensure_ascii=False)

            content = content_raw.strip()
            logging.info("[pulse] raw decision:\n%s", content)
            self.conscious_log.append({"role": "assistant", "content": content})
            self._save_conscious_log()

            try:
                data = json.loads(_extract_json_text(content), strict=False)
            except json.JSONDecodeError:
                logging.warning("[pulse] failed to parse decision JSON. raw=%s", content)
                phase = "decision_json_parse_error"
                _log_phase(phase)
                return _finalize_and_return([])

            last_decision = data
            current_perception = (data.get("perception") or "").strip()
            current_todo = (data.get("todo") or "").strip()

            next_action = (data.get("decision") or "").lower()
            if not next_action:
                next_action = "speak"
            if next_action not in {"wait", "speak", "tool"}:
                logging.warning("[pulse] unknown action '%s', defaulting to speak", next_action)
                next_action = "speak"

            action_payload = data.get("action") if isinstance(data.get("action"), dict) else {}

            if next_action == "tool" and len(tool_history) >= max_tool_runs:
                logging.info("[pulse] tool usage limit reached, forcing speak")
                next_action = "speak"
                force_speak = True

            if next_action == "wait":
                logging.info("[pulse] decision: wait")
                self._save_session_metadata()
                logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
                phase = "decision_wait"
                _log_phase(phase)
                return _finalize_and_return(replies)

            if next_action == "tool":
                tool_payload = action_payload or {}
                tool_name = (tool_payload.get("tool") or tool_payload.get("name") or "").strip()
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
                    current_todo = f"ツール {tool_name} の前回結果: {summary_for_cache}\n同じ結果をユーザーに共有してください。"
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
                    result_text, snippet, file_path, metadata = parse_tool_result(result)
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
                    metadata = None

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
                if metadata:
                    history_record["metadata"] = metadata
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
                if metadata:
                    self.pending_attachment_metadata.append(metadata)
                    media_list = metadata.get("media") if isinstance(metadata, dict) else None
                    if media_list:
                        try:
                            refs = ", ".join(item.get("uri", "") or item.get("path", "") for item in media_list if isinstance(item, dict))
                            tool_info_parts.append(f"[MEDIA:{tool_name}] {refs}")
                        except Exception:
                            tool_info_parts.append(f"[MEDIA:{tool_name}] 生成画像があります。ユーザーに共有してください。")
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
            phase = "no_decision"
            _log_phase(phase)
            return _finalize_and_return(replies)

        initial_decision = next_action
        if initial_decision == "tool":
            self._record_decision_entry(
                pulse_id,
                current_perception,
                current_todo,
                "tool",
                action_payload,
            )

        if next_action == "tool":
            logging.info("[pulse] reached decision loop limit; forcing speak")
            next_action = "speak"
            force_speak = True
        elif next_action == "wait":
            self._record_decision_entry(pulse_id, current_perception, current_todo, "wait", None)
            logging.info("[pulse] decision: wait")
            self._save_session_metadata()
            logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
            phase = "decision_wait_final"
            _log_phase(phase)
            return _finalize_and_return(replies)

        if next_action == "speak":
            self._record_decision_entry(pulse_id, current_perception, current_todo, "speak", None)

        guidance_sections: List[str] = []
        if current_perception:
            guidance_sections.append(f"### 状況認識\n{current_perception}")
        if tool_info_parts:
            guidance_sections.append("### ツール結果\n" + "\n".join(tool_info_parts))
        if current_todo:
            guidance_sections.append(f"### TODO\n{current_todo}")

        guidance_message = None
        if guidance_sections:
            guidance_sections.append(
                "### 注意\n"
                "- 上記TODOをそのまま反映し、あなたの言葉で丁寧に説明してください。\n"
                "- ツールは実行せず、会話のみで応答してください。\n"
                "- 必要であればユーザーに確認を取りながら進めてください。"
            )
            guidance_message = "\n\n".join(guidance_sections)
        previous_pulse = getattr(self, "_current_pulse_id", None)
        self._current_pulse_id = pulse_id
        try:
            say, _, _ = self._generate(
                None,
                system_prompt_extra=None,
                info_text=None,
                guidance_text_override=guidance_message,
                log_extra_prompt=False,
                log_user_message=False,
            )
        finally:
            if previous_pulse is None:
                self._current_pulse_id = None
            else:
                self._current_pulse_id = previous_pulse
        replies.append(say)

        phase = "response_generated"
        _log_phase(phase)

        self._save_session_metadata()
        logging.info("[pulse] %s finished pulse with %d replies", self.persona_id, len(replies))
        phase = "completed"
        _log_phase(phase)
        return _finalize_and_return(replies)

    def _compose_task_summary(self) -> str:
        storage = getattr(self, "task_storage", None)
        if storage is None:
            return ""
        try:
            records = storage.list_tasks(include_steps=True, limit=12)
        except Exception as exc:
            logging.warning("[pulse] failed to load task summary: %s", exc)
            return ""
        if not records:
            return "(現在登録されているタスクはありません)"

        lines: List[str] = []
        active = next((task for task in records if task.status == "active"), None)
        if active:
            lines.append(f"### アクティブタスク: {active.title} [{active.status}]")
            lines.extend(self._format_task_steps(active))
        else:
            lines.append("### アクティブタスク: (なし)")

        pending = [task for task in records if task.status in {"pending", "paused"}]
        if pending:
            lines.append("### 待機中タスク")
            for task in pending[:3]:
                lines.append(f"- {task.title} [{task.status}]")
        return "\n".join(lines)

    def _format_task_steps(self, task: TaskRecord) -> List[str]:
        lines: List[str] = []
        for idx, step in enumerate(task.steps, start=1):
            marker = "→" if task.active_step_id == step.id else "・"
            title = step.title
            lines.append(f"  {marker} Step{idx} [{step.status}] {title}")
        return lines

    def _record_perception_entry(self, content: str, pulse_id: str) -> None:
        if not content or not content.strip():
            return
        self._append_memory_entry(
            pulse_id=pulse_id,
            role="system",
            content=content,
            tags=["perception", f"pulse:{pulse_id}"],
        )

    def _record_decision_entry(
        self,
        pulse_id: str,
        perception: Optional[str],
        todo: Optional[str],
        decision: str,
        action_payload: Optional[Dict[str, Any]],
    ) -> None:
        payload: Dict[str, Any] = {
            "decision": decision,
        }
        if perception and perception.strip():
            payload["perception"] = perception.strip()
        if todo and todo.strip():
            payload["todo"] = todo.strip()
        if decision == "tool" and action_payload:
            payload["action"] = action_payload
        try:
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        except TypeError:
            safe_payload = {
                key: (value if isinstance(value, (str, int, float, list, dict, bool, type(None))) else str(value))
                for key, value in payload.items()
            }
            content = json.dumps(safe_payload, ensure_ascii=False, indent=2)
        tags = ["conscious", f"decision:{decision}", f"pulse:{pulse_id}"]
        self._append_memory_entry(
            pulse_id=pulse_id,
            role="assistant",
            content=content,
            tags=tags,
        )

    def _record_tool_action(
        self,
        *,
        pulse_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result_summary: str,
        metadata: Optional[Dict[str, Any]],
        attachment_path: Optional[str],
    ) -> None:
        payload: Dict[str, Any] = {
            "tool": tool_name,
            "arguments": arguments,
            "result": result_summary,
        }
        if metadata:
            payload["metadata"] = metadata
        if attachment_path:
            payload["attachment"] = attachment_path
        try:
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        except TypeError:
            safe_payload = {
                key: (value if isinstance(value, (str, int, float, list, dict, bool, type(None))) else str(value))
                for key, value in payload.items()
            }
            content = json.dumps(safe_payload, ensure_ascii=False, indent=2)
        tags = ["action", "tool", f"tool:{tool_name}", f"pulse:{pulse_id}"]
        self._append_memory_entry(
            pulse_id=pulse_id,
            role="assistant",
            content=content,
            tags=tags,
        )

    def _append_memory_entry(
        self,
        *,
        pulse_id: str,
        role: str,
        content: str,
        tags: List[str],
    ) -> None:
        if self.sai_memory is None or not getattr(self.sai_memory, "is_ready", lambda: False)():
            return
        entry_tags = list(dict.fromkeys(tag for tag in tags if tag))
        active_task = self._active_task_id()
        if active_task:
            task_tag = f"task:{active_task}"
            if task_tag not in entry_tags:
                entry_tags.append(task_tag)
        metadata = {
            "tags": entry_tags,
            "pulse_id": pulse_id,
        }
        if active_task:
            metadata["task_id"] = active_task
        message = {
            "role": role,
            "content": content,
            "metadata": metadata,
        }
        try:
            self.sai_memory.append_persona_message(message)
        except Exception as exc:
            logging.debug("[pulse] failed to append memory entry: %s", exc)

    def _active_task_id(self) -> Optional[str]:
        storage = getattr(self, "task_storage", None)
        if storage is None:
            return None
        try:
            tasks = storage.list_tasks(statuses=["active"], limit=1, include_steps=False)
            if tasks:
                return tasks[0].id
        except Exception as exc:
            logging.debug("[pulse] failed to resolve active task: %s", exc)
        return None


__all__ = ["PersonaPulseMixin"]
