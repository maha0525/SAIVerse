from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Callable, Dict, Optional

from llm_clients.exceptions import LLMError
from sea.runtime_utils import _format, _is_llm_streaming_enabled
from saiverse.logging_config import log_sea_trace
from sea.playbook_models import PlaybookSchema
from saiverse.usage_tracker import get_usage_tracker

LOGGER = logging.getLogger(__name__)

# ── Spell system (text-based tool invocation) ──

_MAX_SPELL_LOOPS = 3
_SPELL_PATTERN = re.compile(
    r"^/spell\s+name='([^']+)'\s+args=(.+)$",
    re.MULTILINE,
)


def _parse_spell_line(text: str):
    """Parse the first /spell invocation in *text*.

    Returns ``(tool_name, tool_args, match)`` or ``None``.
    """
    m = _SPELL_PATTERN.search(text)
    if not m:
        return None
    tool_name = m.group(1)
    args_raw = m.group(2).strip()
    # Try ast.literal_eval first (Python dict syntax), then JSON
    import ast
    try:
        tool_args = ast.literal_eval(args_raw)
    except (ValueError, SyntaxError):
        try:
            tool_args = json.loads(args_raw)
        except json.JSONDecodeError:
            LOGGER.warning("[sea][spell] Failed to parse args: %s", args_raw)
            return None
    if not isinstance(tool_args, dict):
        LOGGER.warning("[sea][spell] Args is not a dict: %s", type(tool_args))
        return None
    return tool_name, tool_args, m


def _build_spell_details_html(tool_name: str, tool_args: dict, display_name: str, result_str: str = "") -> str:
    """Build a styled ``<details>`` HTML block for spell UI display."""
    args_str = str(tool_args)
    # Escape HTML in result to prevent injection
    result_escaped = (
        result_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if result_str else ""
    )
    result_section = (
        f'<div class="spellResultLabel">Result:</div>'
        f'<div class="spellResult">{result_escaped}</div>'
        if result_escaped else ""
    )
    return (
        f'<details class="spellBlock">'
        f'<summary class="spellSummary">'
        f'<span class="spellIcon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        f'<path d="M12 2L15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2z"/>'
        f'</svg></span>'
        f'<span>{display_name}</span>'
        f'</summary>'
        f'<div class="spellContent">'
        f'<div class="spellParams"><code>{args_str}</code></div>'
        f'{result_section}'
        f'</div>'
        f'</details>'
    )


# ── Handy Tool inline execution (legacy, kept for non-spell tool_call path) ──

_MAX_HANDY_TOOL_LOOPS = 3


def _execute_handy_tool_inline(
    tool_name: str,
    tool_args: dict,
    persona: Any,
    building_id: str,
    playbook_name: str,
    state: dict,
    messages: list,
    runtime: Any,
    event_callback: Optional[Callable] = None,
    thought_signature: Optional[str] = None,
) -> str:
    """Execute a handy tool inline within the LLM node and append protocol messages.

    Returns the tool result string. Modifies `messages` in place (appends
    assistant tool_call + tool result messages).
    """
    from tools import TOOL_REGISTRY
    from tools.context import persona_context
    from pathlib import Path
    from sea.pulse_context import PulseLogEntry

    tc_id = f"tc_{uuid.uuid4().hex}"

    # Append assistant tool_call message to conversation
    tc_entry: Dict[str, Any] = {
        "id": tc_id,
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)},
    }
    # Gemini thinking models require thought_signature echoed back on function call parts
    if thought_signature:
        tc_entry["thought_signature"] = thought_signature
    tool_call_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [tc_entry],
    }
    messages.append(tool_call_msg)

    # Execute the tool
    tool_func = TOOL_REGISTRY.get(tool_name)
    if not tool_func:
        result_str = f"Tool '{tool_name}' not found in registry"
        LOGGER.error("[sea][handy] %s", result_str)
    else:
        persona_obj = state.get("_persona_obj") or persona
        persona_id = getattr(persona_obj, "persona_id", "unknown")
        persona_dir = getattr(persona_obj, "persona_log_path", None)
        persona_dir = persona_dir.parent if persona_dir else Path.cwd()
        manager_ref = getattr(persona_obj, "manager_ref", None)
        try:
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback):
                raw_result = tool_func(**tool_args)
            result_str = str(raw_result)
            LOGGER.info("[sea][handy] Executed %s → %s", tool_name, result_str[:200])
        except Exception as exc:
            result_str = f"Handy tool error ({tool_name}): {exc}"
            LOGGER.exception("[sea][handy] %s failed", tool_name)

    # Append tool result message to conversation
    tool_result_msg = {
        "role": "tool",
        "tool_call_id": tc_id,
        "name": tool_name,
        "content": result_str,
    }
    messages.append(tool_result_msg)

    # Record to PulseContext
    _pulse_ctx = state.get("_pulse_context")
    if _pulse_ctx:
        # Assistant tool_call entry
        _pulse_ctx.append(PulseLogEntry(
            role="assistant", content="",
            node_id=f"handy_{tool_name}", playbook_name=playbook_name,
            tool_calls=[{
                "id": tc_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)},
            }],
        ))
        # Tool result entry
        _pulse_ctx.append(PulseLogEntry(
            role="tool", content=result_str,
            node_id=f"handy_{tool_name}", playbook_name=playbook_name,
            tool_call_id=tc_id, tool_name=tool_name,
        ))

    # Store to SAIMemory with handy_tool tag
    pulse_id = state.get("_pulse_id")
    runtime._store_memory(
        persona,
        f"[Handy Tool: {tool_name}]\n{result_str}",
        role="system",
        tags=["conversation", "handy_tool"],
        pulse_id=pulse_id,
        playbook_name=playbook_name,
    )

    # Record to activity trace (merged into final say event, not a separate bubble)
    _at = state.get("_activity_trace")
    if isinstance(_at, list):
        _at.append({"action": "handy_tool", "name": tool_name, "playbook": playbook_name})

    return result_str


def _execute_spell_inline(
    tool_name: str,
    tool_args: dict,
    persona: Any,
    building_id: str,
    playbook_name: str,
    state: dict,
    messages: list,
    runtime: Any,
    text_before: str,
    spell_line: str,
    event_callback: Optional[Callable] = None,
) -> str:
    """Execute a spell tool inline and append context messages.

    Unlike handy tools, spells do NOT use the tool_call/tool protocol.
    Instead they add assistant text + system result messages.
    Returns the tool result string.
    """
    from tools import TOOL_REGISTRY
    from tools.context import persona_context
    from pathlib import Path
    from sea.pulse_context import PulseLogEntry

    # Append assistant message (text up to and including spell line)
    assistant_content = (text_before + "\n" + spell_line).strip()
    messages.append({"role": "assistant", "content": assistant_content})

    # Execute the tool
    tool_func = TOOL_REGISTRY.get(tool_name)
    if not tool_func:
        result_str = f"Spell '{tool_name}' not found in registry"
        LOGGER.error("[sea][spell] %s", result_str)
    else:
        persona_obj = state.get("_persona_obj") or persona
        persona_id = getattr(persona_obj, "persona_id", "unknown")
        persona_dir = getattr(persona_obj, "persona_log_path", None)
        persona_dir = persona_dir.parent if persona_dir else Path.cwd()
        manager_ref = getattr(persona_obj, "manager_ref", None)
        try:
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback):
                raw_result = tool_func(**tool_args)
            result_str = str(raw_result)
            LOGGER.info("[sea][spell] Executed %s → %s", tool_name, result_str[:200])
        except Exception as exc:
            result_str = f"Spell error ({tool_name}): {exc}"
            LOGGER.exception("[sea][spell] %s failed", tool_name)

    # Append spell result as user message (wrapped in <system> tags).
    # Using "user" role because Gemini converts "system" role to system_instruction,
    # which would strip it from the conversation flow. The <system> wrapper
    # signals to the LLM that this is system-provided context, not user input.
    result_msg = {
        "role": "user",
        "content": f"<system>[Spell Result: {tool_name}]\n{result_str}</system>",
    }
    messages.append(result_msg)

    # Record to PulseContext
    _pulse_ctx = state.get("_pulse_context")
    if _pulse_ctx:
        _pulse_ctx.append(PulseLogEntry(
            role="assistant", content=assistant_content,
            node_id=f"spell_{tool_name}", playbook_name=playbook_name,
        ))
        _pulse_ctx.append(PulseLogEntry(
            role="system", content=result_str,
            node_id=f"spell_{tool_name}", playbook_name=playbook_name,
        ))

    # Store assistant message (text before spell) to SAIMemory
    pulse_id = state.get("_pulse_id")
    if text_before.strip():
        runtime._store_memory(
            persona,
            text_before,
            role="assistant",
            tags=["conversation"],
            pulse_id=pulse_id,
            playbook_name=playbook_name,
        )

    # Store spell result to SAIMemory with spell tag
    runtime._store_memory(
        persona,
        f"[Spell: {tool_name}]\n{result_str}",
        role="system",
        tags=["conversation", "spell"],
        pulse_id=pulse_id,
        playbook_name=playbook_name,
    )

    # Record to activity trace
    _at = state.get("_activity_trace")
    if isinstance(_at, list):
        _at.append({"action": "spell", "name": tool_name, "playbook": playbook_name})

    return result_str


def lg_llm_node(runtime, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
    async def node(state: dict):
        # Check for cancellation at start of node
        cancellation_token = state.get("_cancellation_token")
        if cancellation_token:
            cancellation_token.raise_if_cancelled()

        # Send status event for node execution
        node_id = getattr(node_def, "id", "llm")
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
        # Merge state into variables for template formatting
        if playbook.name == 'sub_router_user':
            action_dbg = getattr(node_def, 'action', None)
            LOGGER.debug('[sea][router-debug] action=%s model_type=%s avail_len=%s',
                         (action_dbg[:120] + '...') if isinstance(action_dbg, str) and len(action_dbg) > 120 else action_dbg,
                         getattr(node_def, 'model_type', None),
                         len(str(state.get('available_playbooks'))) if state.get('available_playbooks') is not None else None)

        # Build variables for template formatting
        # System variables (_ prefix) are excluded — only playbook variables are exposed to templates
        variables = {
            "input": state.get("input", ""),
            "last": state.get("last", ""),
            "persona_id": getattr(persona, "persona_id", None),
            "persona_name": getattr(persona, "persona_name", None),
            **{k: v for k, v in state.items() if not k.startswith("_")},
        }

        # Debug: log template variables for novel_writing playbook
        if playbook.name == "novel_writing":
            node_id = getattr(node_def, "id", "")
            if node_id.startswith("chapter_"):
                # Log specific variables used in chapter templates
                relevant_keys = ["novel_title", "chapter_1_title", "chapter_2_title", "chapter_3_title", "chapter_4_title"]
                relevant_vars = {k: variables.get(k) for k in relevant_keys}
                LOGGER.debug("[sea][novel_writing] Node %s: relevant variables = %s", node_id, relevant_vars)
        text = ""
        schema_consumed = False
        prompt = None  # Will store the expanded prompt for memorize
        try:
            # Determine base messages: use context_profile if set, otherwise state["_messages"]
            _profile_name = getattr(node_def, "context_profile", None)
            if _profile_name:
                from sea.playbook_models import CONTEXT_PROFILES
                _profile = CONTEXT_PROFILES.get(_profile_name)
                if _profile:
                    _cache_key = f"_ctx_profile_{_profile_name}"
                    if _cache_key not in state:
                        # Exclude current pulse messages from SAIMemory — PulseContext
                        # provides them instead, avoiding duplication of memorized messages.
                        state[_cache_key] = runtime._prepare_context(
                            persona, building_id,
                            state.get("input") or None,
                            _profile["requirements"],
                            pulse_id=state.get("_pulse_id"),
                            exclude_pulse_id=state.get("_pulse_id"),
                            event_callback=event_callback,
                        )
                        LOGGER.info("[sea] Prepared context for profile '%s' (node=%s, %d messages, exclude_pulse=%s)",
                                    _profile_name, node_id, len(state[_cache_key]), state.get("_pulse_id"))
                    _profile_base = state[_cache_key]
                    _pulse_ctx = state.get("_pulse_context")
                    _intermediate = _pulse_ctx.get_protocol_messages() if _pulse_ctx else []
                    base_msgs = list(_profile_base) + list(_intermediate)
                else:
                    LOGGER.warning("[sea] Unknown context_profile '%s' on node '%s', falling back to state messages", _profile_name, node_id)
                    base_msgs = state.get("_messages", [])
            else:
                base_msgs = state.get("_messages", [])
            action_template = getattr(node_def, "action", None)
            if action_template:
                prompt = _format(action_template, variables)
                # Auto-wrap in <system> tags to distinguish from user messages
                if not prompt.lstrip().startswith("<system>"):
                    prompt = f"<system>{prompt}</system>"
                messages = list(base_msgs) + [{"role": "user", "content": prompt}]
            else:
                messages = list(base_msgs)

            # Dynamically add enum to response_schema if available_playbooks exists
            response_schema = getattr(node_def, "response_schema", None)
            if response_schema and "available_playbooks" in state:
                response_schema = runtime._add_playbook_enum(response_schema, state.get("available_playbooks"))

            # Select LLM client based on model_type and structured output needs
            needs_structured_output = response_schema is not None
            llm_client = runtime._select_llm_client(node_def, persona, needs_structured_output=needs_structured_output)

            # Inject model-specific system prompt if configured
            _model_config_key = getattr(llm_client, "config_key", None)
            if _model_config_key:
                from saiverse.model_configs import get_model_system_prompt
                _model_sys_prompt = get_model_system_prompt(_model_config_key)
                if _model_sys_prompt:
                    _injected = False
                    for _mi, _msg in enumerate(messages):
                        if _msg.get("role") == "system":
                            # Create new dict to avoid mutating shared base_msgs
                            messages[_mi] = {**_msg, "content": _msg["content"] + "\n\n---\n\n" + _model_sys_prompt}
                            _injected = True
                            break
                    if not _injected:
                        messages.insert(0, {"role": "system", "content": _model_sys_prompt})
                    LOGGER.debug("[sea] Injected model-specific system prompt for %s", _model_config_key)

            # Check if tools are available for this node
            available_tools = getattr(node_def, "available_tools", None)
            LOGGER.info("[DEBUG] available_tools = %s", available_tools)

            # Check if spells are enabled for this persona (spells replace handy tool injection)
            _spell_enabled = state.get("_spell_enabled", False)

            effective_tools: list[str] = list(available_tools or [])

            if effective_tools:
                LOGGER.info("[DEBUG] Entering tools mode (generate with tools)")
                # Tool calling mode - use unified generate() with tools
                tools_spec = runtime._build_tools_spec(effective_tools, llm_client)

                # Check if we should use streaming in tool mode
                speak_flag = getattr(node_def, "speak", None)
                streaming_enabled = _is_llm_streaming_enabled()
                use_tool_streaming = (
                    speak_flag is True
                    and response_schema is None
                    and streaming_enabled
                    and event_callback is not None
                )
                LOGGER.info("[DEBUG] Tool mode streaming check: speak=%s, streaming=%s, event_cb=%s → use_tool_streaming=%s",
                           speak_flag, streaming_enabled, event_callback is not None, use_tool_streaming)

                if use_tool_streaming:
                    # ── Streaming tool mode ──
                    # Stream text chunks to UI while tools are buffered internally.
                    # After stream ends, consume_tool_detection() tells us whether
                    # LLM chose a tool or just produced text.
                    LOGGER.info("[DEBUG] Using streaming generation with tools")
                    max_stream_retries = 3
                    text = ""
                    cancelled_during_stream = False
                    for stream_attempt in range(max_stream_retries):
                        text_chunks: list[str] = []
                        stream_iter = llm_client.generate_stream(
                            messages,
                            tools=tools_spec,
                            temperature=runtime._default_temperature(persona),
                            **runtime._get_cache_kwargs(),
                        )
                        try:
                            for chunk in stream_iter:
                                if cancellation_token and cancellation_token.is_cancelled():
                                    LOGGER.info("[sea] Tool streaming cancelled by user")
                                    cancelled_during_stream = True
                                    break
                                if isinstance(chunk, dict) and chunk.get("type") == "thinking":
                                    event_callback({
                                        "type": "streaming_thinking",
                                        "content": chunk["content"],
                                        "persona_id": getattr(persona, "persona_id", None),
                                        "node_id": getattr(node_def, "id", "llm"),
                                    })
                                    continue
                                text_chunks.append(chunk)
                                event_callback({
                                    "type": "streaming_chunk",
                                    "content": chunk,
                                    "persona_id": getattr(persona, "persona_id", None),
                                    "node_id": getattr(node_def, "id", "llm"),
                                })
                        finally:
                            if hasattr(stream_iter, 'close'):
                                stream_iter.close()
                        text = "".join(text_chunks)

                        if cancelled_during_stream:
                            break
                        if text.strip():
                            break
                        # Tool call with no text is valid — check before retrying
                        _peek_tool = llm_client.consume_tool_detection()
                        if _peek_tool and _peek_tool.get("type") in ("tool_call", "both"):
                            # Put it back for later consumption
                            llm_client._store_tool_detection(_peek_tool)
                            break
                        # Truly empty (no text, no tool call) — discard and retry
                        discarded_usage = llm_client.consume_usage()
                        LOGGER.warning(
                            "[sea][llm] Empty tool-streaming response (attempt %d/%d). "
                            "Discarding usage (in=%d, out=%d) and retrying...",
                            stream_attempt + 1, max_stream_retries,
                            discarded_usage.input_tokens if discarded_usage else 0,
                            discarded_usage.output_tokens if discarded_usage else 0,
                        )
                    else:
                        LOGGER.error(
                            "[sea][llm] Empty tool-streaming response after %d attempts.",
                            max_stream_retries,
                        )

                    # Consume reasoning
                    _tool_reasoning = llm_client.consume_reasoning()
                    _tool_reasoning_text = "\n\n".join(
                        e.get("text", "") for e in _tool_reasoning if e.get("text")
                    ) if _tool_reasoning else ""
                    if _tool_reasoning_text:
                        state["_reasoning_text"] = _tool_reasoning_text
                    _tool_reasoning_details = llm_client.consume_reasoning_details()
                    if _tool_reasoning_details is not None:
                        state["_reasoning_details"] = _tool_reasoning_details

                    # Record usage
                    usage = llm_client.consume_usage()
                    llm_usage_metadata: Dict[str, Any] | None = None
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm_tool_stream",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        from saiverse.model_configs import calculate_cost, get_model_display_name
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        llm_usage_metadata = {
                            "model": usage.model,
                            "model_display_name": get_model_display_name(usage.model),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cached_tokens": usage.cached_tokens,
                            "cache_write_tokens": usage.cache_write_tokens,
                            "cost_usd": cost,
                        }
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)

                    # Check tool detection — did LLM call a tool?
                    tool_detection = llm_client.consume_tool_detection()
                    LOGGER.info("[DEBUG] Tool detection after streaming: %s",
                               tool_detection.get("type") if tool_detection else None)

                    # Use tool_detection as the result for the common tool branching below
                    if tool_detection and tool_detection.get("type") in ("tool_call", "both"):
                        result = tool_detection

                        if tool_detection.get("type") == "both" and text.strip():
                            # "both": text + tool call — keep the streamed text in UI and Building history
                            _speak_metadata_key = getattr(node_def, "metadata_key", None)
                            _speak_base_metadata = state.get(_speak_metadata_key) if _speak_metadata_key else None

                            completion_event: Dict[str, Any] = {
                                "type": "streaming_complete",
                                "persona_id": getattr(persona, "persona_id", None),
                                "node_id": getattr(node_def, "id", "llm"),
                            }
                            if _tool_reasoning_text:
                                completion_event["reasoning"] = _tool_reasoning_text
                            if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                                completion_event["metadata"] = _speak_base_metadata
                            event_callback(completion_event)

                            # Record to Building history
                            pulse_id = state.get("_pulse_id")
                            msg_metadata: Dict[str, Any] = {}
                            if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                                msg_metadata.update(_speak_base_metadata)
                            if llm_usage_metadata:
                                msg_metadata["llm_usage"] = llm_usage_metadata
                            if _tool_reasoning_text:
                                msg_metadata["reasoning"] = _tool_reasoning_text
                            if _tool_reasoning_details is not None:
                                msg_metadata["reasoning_details"] = _tool_reasoning_details
                            _at_both = state.get("_activity_trace")
                            if _at_both:
                                msg_metadata["activity_trace"] = list(_at_both)
                            eff_bid = runtime._effective_building_id(persona, building_id)
                            runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
                            LOGGER.info("[sea] 'both' response: text kept in UI and Building history (len=%d), tool call continues", len(text))
                        elif text_chunks:
                            # "tool_call" only — discard streamed text
                            event_callback({
                                "type": "streaming_discard",
                                "persona_id": getattr(persona, "persona_id", None),
                                "node_id": getattr(node_def, "id", "llm"),
                            })
                            LOGGER.info("[sea] Streaming text discarded — tool_call only (no speak content)")
                    else:
                        # No tool call — this is a normal text response
                        result = {"type": "text", "content": text}

                        # Send streaming_complete + emit say (same as normal streaming mode)
                        _speak_metadata_key = getattr(node_def, "metadata_key", None)
                        _speak_base_metadata = state.get(_speak_metadata_key) if _speak_metadata_key else None

                        completion_event: Dict[str, Any] = {
                            "type": "streaming_complete",
                            "persona_id": getattr(persona, "persona_id", None),
                            "node_id": getattr(node_def, "id", "llm"),
                        }
                        if _tool_reasoning_text:
                            completion_event["reasoning"] = _tool_reasoning_text
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            completion_event["metadata"] = _speak_base_metadata
                        event_callback(completion_event)

                        # Record to Building history
                        pulse_id = state.get("_pulse_id")
                        msg_metadata: Dict[str, Any] = {}
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            msg_metadata.update(_speak_base_metadata)
                        if llm_usage_metadata:
                            msg_metadata["llm_usage"] = llm_usage_metadata
                        if _tool_reasoning_text:
                            msg_metadata["reasoning"] = _tool_reasoning_text
                        if _tool_reasoning_details is not None:
                            msg_metadata["reasoning_details"] = _tool_reasoning_details
                        _at_stream = state.get("_activity_trace")
                        if _at_stream:
                            msg_metadata["activity_trace"] = list(_at_stream)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            msg_metadata["llm_usage_total"] = dict(accumulator)
                        eff_bid = runtime._effective_building_id(persona, building_id)
                        runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)

                else:
                    # ── Synchronous tool mode (original) ──
                    result = llm_client.generate(
                        messages,
                        tools=tools_spec,
                        temperature=runtime._default_temperature(persona),
                        **runtime._get_cache_kwargs(),
                    )

                    # Consume reasoning (thinking) from tool-mode LLM call
                    _tool_reasoning = llm_client.consume_reasoning()
                    _tool_reasoning_text = "\n\n".join(
                        e.get("text", "") for e in _tool_reasoning if e.get("text")
                    ) if _tool_reasoning else ""
                    if _tool_reasoning_text:
                        state["_reasoning_text"] = _tool_reasoning_text
                    _tool_reasoning_details = llm_client.consume_reasoning_details()
                    if _tool_reasoning_details is not None:
                        state["_reasoning_details"] = _tool_reasoning_details

                    # Record usage
                    usage = llm_client.consume_usage()
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm_tool",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        # Accumulate into pulse total
                        from saiverse.model_configs import calculate_cost
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)

                # ── Common tool result handling (shared by streaming & sync) ──
                # Parse output_keys to determine where to store results
                output_keys_spec = getattr(node_def, "output_keys", None)
                text_key = None
                function_call_key = None
                thought_key = None

                if output_keys_spec:
                    for mapping in output_keys_spec:
                        if "text" in mapping:
                            text_key = mapping["text"]
                        if "function_call" in mapping:
                            function_call_key = mapping["function_call"]
                        if "thought" in mapping:
                            thought_key = mapping["thought"]

                # Debug: log result type and keys
                LOGGER.info("[DEBUG] LLM result type='%s', has content=%s, has tool_name=%s",
                           result.get("type"), "content" in result, "tool_name" in result)

                # ── Spell inline loop ──
                # If LLM output contains /spell lines and spells are enabled,
                # execute them and re-invoke LLM with results.
                _spell_loop_count = 0
                _spell_details_blocks: list[str] = []  # Accumulated <details> blocks for UI
                if _spell_enabled and result.get("type") == "text" and result.get("content"):
                    _spell_parsed = _parse_spell_line(result["content"])
                    while _spell_parsed and _spell_loop_count < _MAX_SPELL_LOOPS:
                        _spell_loop_count += 1
                        _sp_name, _sp_args, _sp_match = _spell_parsed
                        LOGGER.info("[sea][spell] Loop %d: executing %s (args=%s)", _spell_loop_count, _sp_name, _sp_args)

                        from tools import SPELL_TOOL_NAMES, SPELL_TOOL_SCHEMAS
                        if _sp_name not in SPELL_TOOL_NAMES:
                            LOGGER.warning("[sea][spell] Unknown spell '%s', skipping", _sp_name)
                            break

                        _sp_schema = SPELL_TOOL_SCHEMAS.get(_sp_name)
                        _sp_display = (_sp_schema.spell_display_name if _sp_schema else "") or _sp_name

                        # Text before the spell line
                        _text_before = result["content"][:_sp_match.start()].rstrip()
                        _spell_line = _sp_match.group(0)

                        # Execute spell
                        _sp_result = _execute_spell_inline(
                            _sp_name, _sp_args,
                            persona, building_id, playbook.name,
                            state, messages, runtime,
                            text_before=_text_before,
                            spell_line=_spell_line,
                            event_callback=event_callback,
                        )

                        # Build <details> block for UI (after execution, includes result)
                        _details = _build_spell_details_html(_sp_name, _sp_args, _sp_display, _sp_result)
                        _spell_details_blocks.append((_text_before, _details))

                        # Re-invoke LLM without tools (spell results are in messages)
                        LOGGER.info("[sea][spell] Re-invoking LLM after spell %s", _sp_name)
                        _retry_result = llm_client.generate(
                            messages,
                            tools=None,
                            temperature=runtime._default_temperature(persona),
                            **runtime._get_cache_kwargs(),
                        )
                        # Record usage
                        _retry_usage = llm_client.consume_usage()
                        if _retry_usage:
                            get_usage_tracker().record_usage(
                                model_id=_retry_usage.model,
                                input_tokens=_retry_usage.input_tokens,
                                output_tokens=_retry_usage.output_tokens,
                                cached_tokens=_retry_usage.cached_tokens,
                                cache_write_tokens=_retry_usage.cache_write_tokens,
                                cache_ttl=_retry_usage.cache_ttl,
                                persona_id=getattr(persona, "persona_id", None),
                                building_id=building_id,
                                node_type="llm_spell_retry",
                                playbook_name=playbook.name,
                                category="persona_speak",
                            )
                            from saiverse.model_configs import calculate_cost
                            _retry_cost = calculate_cost(
                                _retry_usage.model, _retry_usage.input_tokens, _retry_usage.output_tokens,
                                _retry_usage.cached_tokens, _retry_usage.cache_write_tokens, cache_ttl=_retry_usage.cache_ttl,
                            )
                            runtime._accumulate_usage(
                                state, _retry_usage.model, _retry_usage.input_tokens,
                                _retry_usage.output_tokens, _retry_cost,
                                _retry_usage.cached_tokens, _retry_usage.cache_write_tokens,
                            )

                        # Extract text from retry result
                        _retry_content = ""
                        if isinstance(_retry_result, dict):
                            _retry_content = _retry_result.get("content", "")
                        elif isinstance(_retry_result, str):
                            _retry_content = _retry_result
                        result = {"type": "text", "content": _retry_content}

                        # Check for more spells in new output
                        _spell_parsed = _parse_spell_line(result["content"])
                        LOGGER.info("[sea][spell] After retry: has_more_spells=%s", _spell_parsed is not None)

                if _spell_loop_count > 0:
                    LOGGER.info("[sea][spell] Completed %d spell loop(s)", _spell_loop_count)

                    pulse_id = state.get("_pulse_id")
                    eff_bid = runtime._effective_building_id(persona, building_id)

                    # Bubble 1: text_before of first spell (no metadata)
                    _first_text_before = _spell_details_blocks[0][0] if _spell_details_blocks else ""
                    if _first_text_before.strip():
                        if event_callback:
                            event_callback({
                                "type": "say",
                                "content": _first_text_before,
                                "persona_id": getattr(persona, "persona_id", None),
                            })
                        runtime._emit_say(persona, eff_bid, _first_text_before, pulse_id=pulse_id)

                    # Bubble 2: details + continuation (with metadata)
                    _bubble2_parts: list[str] = []
                    for _i, (_tb, _db) in enumerate(_spell_details_blocks):
                        if _i > 0 and _tb:
                            _bubble2_parts.append(_tb)
                        _bubble2_parts.append(_db)
                    if result.get("content"):
                        _bubble2_parts.append(result["content"])
                    _spell_bubble2 = "\n".join(_bubble2_parts)

                    _spell_msg_meta: Dict[str, Any] = {}
                    _spell_at = state.get("_activity_trace")
                    if _spell_at:
                        _spell_msg_meta["activity_trace"] = list(_spell_at)

                    if event_callback:
                        _say_event: Dict[str, Any] = {
                            "type": "say",
                            "content": _spell_bubble2,
                            "persona_id": getattr(persona, "persona_id", None),
                        }
                        if _spell_at:
                            _say_event["activity_trace"] = list(_spell_at)
                        event_callback(_say_event)

                    runtime._emit_say(persona, eff_bid, _spell_bubble2, pulse_id=pulse_id,
                                      metadata=_spell_msg_meta if _spell_msg_meta else None)
                    LOGGER.info("[sea][spell] Emitted bubble1 + bubble2 to UI and Building history")

                if result["type"] == "tool_call":
                    LOGGER.info("[DEBUG] Entering tool_call branch")
                    # Only tool call, no text
                    if output_keys_spec:
                        # New behavior: use explicit output_keys
                        if function_call_key:
                            state[f"{function_call_key}.name"] = result["tool_name"]
                            # Store full args dict (for tool_call node dynamic execution)
                            state[f"{function_call_key}.args"] = result["tool_args"] if isinstance(result["tool_args"], dict) else {}
                            if isinstance(result["tool_args"], dict):
                                for arg_name, arg_value in result["tool_args"].items():
                                    state[f"{function_call_key}.args.{arg_name}"] = arg_value
                                    LOGGER.debug("[sea] Stored %s.args.%s = %s", function_call_key, arg_name, arg_value)
                        # Set conditional_next flags
                        state["tool_called"] = True
                        state["has_speak_content"] = False
                    else:
                        # Legacy behavior: use predefined keys
                        state["tool_called"] = True
                        state["tool_name"] = result["tool_name"]
                        state["tool_args"] = result["tool_args"]
                        state["has_speak_content"] = False
                        # Expand tool_args for legacy args_input (tool_arg_*)
                        if isinstance(result["tool_args"], dict):
                            for key, value in result["tool_args"].items():
                                state[f"tool_arg_{key}"] = value
                                LOGGER.debug("[sea] Expanded tool_arg_%s = %s", key, value)

                    # Record tool call info for message protocol (function calling)
                    _tc_id = f"tc_{uuid.uuid4().hex}"
                    state["_last_tool_call_id"] = _tc_id
                    state["_last_tool_name"] = result["tool_name"]
                    state["_last_tool_args_json"] = json.dumps(
                        result["tool_args"], ensure_ascii=False
                    ) if isinstance(result["tool_args"], dict) else "{}"
                    # Gemini thinking models require thought_signature on function call parts
                    state["_last_thought_signature"] = result.get("thought_signature")

                    # Format as JSON for logging
                    text = json.dumps({
                        "tool": result["tool_name"],
                        "args": result["tool_args"]
                    }, ensure_ascii=False)
                    LOGGER.info("[sea] Tool call detected: %s", text)

                elif result["type"] == "both":
                    LOGGER.info("[DEBUG] Entering 'both' branch (text + tool call)")
                    # Both text and tool call
                    # In streaming mode, text from text_chunks is authoritative
                    # (tool_detection content may be truncated if LLM client accumulation has issues).
                    # In sync mode, result["content"] is the only source.
                    _both_text = text if (use_tool_streaming and text) else result.get("content", "")
                    if output_keys_spec:
                        # New behavior: use explicit output_keys
                        if text_key:
                            state[text_key] = _both_text
                            LOGGER.debug("[sea] Stored %s = (text, length=%d)", text_key, len(_both_text))
                        if function_call_key:
                            state[f"{function_call_key}.name"] = result["tool_name"]
                            # Store full args dict (for tool_call node dynamic execution)
                            state[f"{function_call_key}.args"] = result["tool_args"] if isinstance(result["tool_args"], dict) else {}
                            if isinstance(result["tool_args"], dict):
                                for arg_name, arg_value in result["tool_args"].items():
                                    state[f"{function_call_key}.args.{arg_name}"] = arg_value
                                    LOGGER.debug("[sea] Stored %s.args.%s = %s", function_call_key, arg_name, arg_value)
                        # Set conditional_next flags
                        state["tool_called"] = True
                        state["has_speak_content"] = bool(text_key)
                    else:
                        # Legacy behavior: use predefined keys
                        state["tool_called"] = True
                        state["tool_name"] = result["tool_name"]
                        state["tool_args"] = result["tool_args"]
                        state["has_speak_content"] = True
                        state["speak_content"] = _both_text
                        # Expand tool_args for legacy args_input (tool_arg_*)
                        if isinstance(result["tool_args"], dict):
                            for key, value in result["tool_args"].items():
                                state[f"tool_arg_{key}"] = value
                                LOGGER.debug("[sea] Expanded tool_arg_%s = %s", key, value)

                    # Record tool call info for message protocol (function calling)
                    _tc_id = f"tc_{uuid.uuid4().hex}"
                    state["_last_tool_call_id"] = _tc_id
                    state["_last_tool_name"] = result["tool_name"]
                    state["_last_tool_args_json"] = json.dumps(
                        result["tool_args"], ensure_ascii=False
                    ) if isinstance(result["tool_args"], dict) else "{}"
                    # Gemini thinking models require thought_signature on function call parts
                    state["_last_thought_signature"] = result.get("thought_signature")

                    text = _both_text
                    LOGGER.info("[sea] Both text and tool call detected: tool=%s, text_length=%d",
                                result["tool_name"], len(text))

                else:
                    LOGGER.info("[DEBUG] Entering 'else' branch (normal text response)")
                    # Normal text response (no tool call)
                    state["tool_called"] = False

                    if output_keys_spec and text_key:
                        # New behavior: store in explicit text_key
                        state[text_key] = result["content"]
                        LOGGER.info("[sea][llm] Stored state['%s'] = %s", text_key, result["content"])
                        state["has_speak_content"] = True
                    else:
                        # Legacy behavior: no specific text storage (just in "last")
                        state["has_speak_content"] = True

                    text = result["content"]

                runtime._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
            else:
                LOGGER.info("[DEBUG] Entering normal mode (no tools)")
                # Normal mode (no tools)
                state["tool_called"] = False

                # Check speak flag for streaming output
                speak_flag = getattr(node_def, "speak", None)
                streaming_enabled = _is_llm_streaming_enabled()
                LOGGER.info("[DEBUG] Streaming check: speak_flag=%s, response_schema=%s, streaming_enabled=%s, event_callback=%s",
                           speak_flag, response_schema is None, streaming_enabled, event_callback is not None)
                use_streaming = (
                    speak_flag is True
                    and response_schema is None
                    and streaming_enabled
                    and event_callback is not None
                )

                if use_streaming:
                    LOGGER.info("[DEBUG] Using streaming generation (speak=true)")
                    # Streaming mode: yield chunks to UI (with retry for empty response)
                    max_stream_retries = 3
                    text = ""
                    cancelled_during_stream = False
                    for stream_attempt in range(max_stream_retries):
                        text_chunks = []
                        stream_iter = llm_client.generate_stream(
                            messages,
                            tools=[],
                            temperature=runtime._default_temperature(persona),
                            **runtime._get_cache_kwargs(),
                        )
                        try:
                            for chunk in stream_iter:
                                # Check cancellation between chunks
                                if cancellation_token and cancellation_token.is_cancelled():
                                    LOGGER.info("[sea] Streaming cancelled by user during chunk loop")
                                    cancelled_during_stream = True
                                    break

                                # Thinking chunks are dicts, text chunks are strings
                                if isinstance(chunk, dict) and chunk.get("type") == "thinking":
                                    event_callback({
                                        "type": "streaming_thinking",
                                        "content": chunk["content"],
                                        "persona_id": getattr(persona, "persona_id", None),
                                        "node_id": getattr(node_def, "id", "llm"),
                                    })
                                    continue
                                text_chunks.append(chunk)
                                # Send each text chunk to UI
                                event_callback({
                                    "type": "streaming_chunk",
                                    "content": chunk,
                                    "persona_id": getattr(persona, "persona_id", None),
                                    "node_id": getattr(node_def, "id", "llm"),
                                })
                        finally:
                            # Explicitly close to disconnect HTTP streaming from LLM API
                            # This stops API-side token generation and billing
                            if hasattr(stream_iter, 'close'):
                                stream_iter.close()
                        text = "".join(text_chunks)

                        if cancelled_during_stream:
                            break  # Don't retry on cancellation

                        # Check for empty response
                        if text.strip():
                            break  # Got valid response

                        # Empty response - discard usage and retry
                        discarded_usage = llm_client.consume_usage()
                        LOGGER.warning(
                            "[sea][llm] Empty streaming response (attempt %d/%d). "
                            "Discarding usage (in=%d, out=%d) and retrying...",
                            stream_attempt + 1, max_stream_retries,
                            discarded_usage.input_tokens if discarded_usage else 0,
                            discarded_usage.output_tokens if discarded_usage else 0,
                        )
                    else:
                        # All retries exhausted
                        LOGGER.error(
                            "[sea][llm] Empty streaming response after %d attempts. "
                            "Proceeding with empty response.",
                            max_stream_retries
                        )

                    # Record usage (even if cancelled — tokens were consumed)
                    usage = llm_client.consume_usage()
                    LOGGER.info("[DEBUG] consume_usage returned: %s", usage)
                    llm_usage_metadata: Dict[str, Any] | None = None
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm_stream",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        LOGGER.info("[DEBUG] Usage recorded: model=%s in=%d out=%d cached=%d cache_write=%d", usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens)
                        # Build llm_usage metadata for message
                        from saiverse.model_configs import calculate_cost, get_model_display_name
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        llm_usage_metadata = {
                            "model": usage.model,
                            "model_display_name": get_model_display_name(usage.model),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cached_tokens": usage.cached_tokens,
                            "cache_write_tokens": usage.cache_write_tokens,
                            "cost_usd": cost,
                        }
                        # Accumulate into pulse total
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)
                    else:
                        LOGGER.warning("[DEBUG] No usage data from LLM client")

                    # Consume reasoning (thinking) from LLM — store as metadata, not in content
                    reasoning_entries = llm_client.consume_reasoning()
                    reasoning_text = "\n\n".join(
                        e.get("text", "") for e in reasoning_entries if e.get("text")
                    ) if reasoning_entries else ""
                    reasoning_details = llm_client.consume_reasoning_details()

                    # ── Spell detection in normal streaming mode ──
                    _spell_loop_count_ns = 0
                    _spell_details_blocks_ns: list[str] = []
                    if _spell_enabled and text and _parse_spell_line(text):
                        _spell_parsed_ns = _parse_spell_line(text)
                        while _spell_parsed_ns and _spell_loop_count_ns < _MAX_SPELL_LOOPS:
                            _spell_loop_count_ns += 1
                            _sp_name_ns, _sp_args_ns, _sp_match_ns = _spell_parsed_ns
                            LOGGER.info("[sea][spell] Normal-stream loop %d: executing %s (args=%s)", _spell_loop_count_ns, _sp_name_ns, _sp_args_ns)

                            from tools import SPELL_TOOL_NAMES, SPELL_TOOL_SCHEMAS
                            if _sp_name_ns not in SPELL_TOOL_NAMES:
                                LOGGER.warning("[sea][spell] Unknown spell '%s', skipping", _sp_name_ns)
                                break

                            _sp_schema_ns = SPELL_TOOL_SCHEMAS.get(_sp_name_ns)
                            _sp_display_ns = (_sp_schema_ns.spell_display_name if _sp_schema_ns else "") or _sp_name_ns

                            _text_before_ns = text[:_sp_match_ns.start()].rstrip()
                            _spell_line_ns = _sp_match_ns.group(0)

                            _sp_result_ns = _execute_spell_inline(
                                _sp_name_ns, _sp_args_ns,
                                persona, building_id, playbook.name,
                                state, messages, runtime,
                                text_before=_text_before_ns,
                                spell_line=_spell_line_ns,
                                event_callback=event_callback,
                            )

                            _details_ns = _build_spell_details_html(_sp_name_ns, _sp_args_ns, _sp_display_ns, _sp_result_ns)
                            _spell_details_blocks_ns.append((_text_before_ns, _details_ns))

                            LOGGER.info("[sea][spell] Re-invoking LLM after spell %s", _sp_name_ns)
                            _retry_result_ns = llm_client.generate(
                                messages,
                                tools=None,
                                temperature=runtime._default_temperature(persona),
                                **runtime._get_cache_kwargs(),
                            )
                            _retry_usage_ns = llm_client.consume_usage()
                            if _retry_usage_ns:
                                get_usage_tracker().record_usage(
                                    model_id=_retry_usage_ns.model,
                                    input_tokens=_retry_usage_ns.input_tokens,
                                    output_tokens=_retry_usage_ns.output_tokens,
                                    cached_tokens=_retry_usage_ns.cached_tokens,
                                    cache_write_tokens=_retry_usage_ns.cache_write_tokens,
                                    cache_ttl=_retry_usage_ns.cache_ttl,
                                    persona_id=getattr(persona, "persona_id", None),
                                    building_id=building_id,
                                    node_type="llm_spell_retry",
                                    playbook_name=playbook.name,
                                    category="persona_speak",
                                )
                                from saiverse.model_configs import calculate_cost
                                _retry_cost_ns = calculate_cost(
                                    _retry_usage_ns.model, _retry_usage_ns.input_tokens, _retry_usage_ns.output_tokens,
                                    _retry_usage_ns.cached_tokens, _retry_usage_ns.cache_write_tokens, cache_ttl=_retry_usage_ns.cache_ttl,
                                )
                                runtime._accumulate_usage(
                                    state, _retry_usage_ns.model, _retry_usage_ns.input_tokens,
                                    _retry_usage_ns.output_tokens, _retry_cost_ns,
                                    _retry_usage_ns.cached_tokens, _retry_usage_ns.cache_write_tokens,
                                )

                            if isinstance(_retry_result_ns, dict):
                                text = _retry_result_ns.get("content", "")
                            elif isinstance(_retry_result_ns, str):
                                text = _retry_result_ns
                            else:
                                text = ""

                            _spell_parsed_ns = _parse_spell_line(text)
                            LOGGER.info("[sea][spell] After retry: has_more_spells=%s", _spell_parsed_ns is not None)

                    if _spell_loop_count_ns > 0:
                        LOGGER.info("[sea][spell] Normal-stream: completed %d spell loop(s)", _spell_loop_count_ns)

                        pulse_id = state.get("_pulse_id")
                        eff_bid = runtime._effective_building_id(persona, building_id)

                        # ── Bubble 1: text_before (discard streamed content, re-emit clean) ──
                        # The streaming already sent the full text (including /spell line) as chunks.
                        # Discard that and replace with just text_before.
                        _first_text_before_ns = _spell_details_blocks_ns[0][0] if _spell_details_blocks_ns else ""
                        if event_callback:
                            event_callback({
                                "type": "streaming_discard",
                                "persona_id": getattr(persona, "persona_id", None),
                                "node_id": getattr(node_def, "id", "llm"),
                            })
                        if _first_text_before_ns.strip():
                            if event_callback:
                                event_callback({
                                    "type": "say",
                                    "content": _first_text_before_ns,
                                    "persona_id": getattr(persona, "persona_id", None),
                                })
                            runtime._emit_say(persona, eff_bid, _first_text_before_ns, pulse_id=pulse_id)
                            LOGGER.info("[sea][spell] Bubble 1: text_before emitted (len=%d)", len(_first_text_before_ns))

                        # ── Bubble 2: details blocks + continuation (with metadata) ──
                        _bubble2_parts: list[str] = []
                        for _i_ns, (_tb_ns, _db_ns) in enumerate(_spell_details_blocks_ns):
                            # text_before of first spell → already in Bubble 1
                            # text_before of subsequent spells (from retry results) → include
                            if _i_ns > 0 and _tb_ns:
                                _bubble2_parts.append(_tb_ns)
                            _bubble2_parts.append(_db_ns)
                        if text:
                            _bubble2_parts.append(text)
                        _spell_bubble2_ns = "\n".join(_bubble2_parts)

                        _spell_msg_meta_ns: Dict[str, Any] = {}
                        if llm_usage_metadata:
                            _spell_msg_meta_ns["llm_usage"] = llm_usage_metadata
                        _spell_at_ns = state.get("_activity_trace")
                        if _spell_at_ns:
                            _spell_msg_meta_ns["activity_trace"] = list(_spell_at_ns)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            _spell_msg_meta_ns["llm_usage_total"] = dict(accumulator)

                        if event_callback:
                            _say_event_ns: Dict[str, Any] = {
                                "type": "say",
                                "content": _spell_bubble2_ns,
                                "persona_id": getattr(persona, "persona_id", None),
                            }
                            if _spell_at_ns:
                                _say_event_ns["activity_trace"] = list(_spell_at_ns)
                            if _spell_msg_meta_ns:
                                _say_event_ns["metadata"] = _spell_msg_meta_ns
                            event_callback(_say_event_ns)

                        runtime._emit_say(persona, eff_bid, _spell_bubble2_ns, pulse_id=pulse_id,
                                          metadata=_spell_msg_meta_ns if _spell_msg_meta_ns else None)
                        LOGGER.info("[sea][spell] Bubble 2: details+continuation emitted (len=%d)", len(_spell_bubble2_ns))

                        # text = continuation only (for state["last"] / memorize — no duplication)
                        # text is already the last retry result (continuation), don't overwrite
                    else:
                        # No spells — normal completion path
                        # Resolve metadata_key for speak (e.g., media attachments from tool execution)
                        _speak_metadata_key = getattr(node_def, "metadata_key", None)
                        _speak_base_metadata = state.get(_speak_metadata_key) if _speak_metadata_key else None

                        # Send completion event with reasoning and metadata
                        completion_event: Dict[str, Any] = {
                            "type": "streaming_complete",
                            "persona_id": getattr(persona, "persona_id", None),
                            "node_id": getattr(node_def, "id", "llm"),
                        }
                        if reasoning_text:
                            completion_event["reasoning"] = reasoning_text
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            completion_event["metadata"] = _speak_base_metadata
                        event_callback(completion_event)

                        # Record to Building history with usage metadata (include pulse total)
                        pulse_id = state.get("_pulse_id")
                        msg_metadata: Dict[str, Any] = {}
                        # Merge base metadata first (e.g., media from tool execution)
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            msg_metadata.update(_speak_base_metadata)
                        if llm_usage_metadata:
                            msg_metadata["llm_usage"] = llm_usage_metadata
                        if reasoning_text:
                            msg_metadata["reasoning"] = reasoning_text
                        if reasoning_details is not None:
                            msg_metadata["reasoning_details"] = reasoning_details
                        _at_stream = state.get("_activity_trace")
                        if _at_stream:
                            msg_metadata["activity_trace"] = list(_at_stream)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            msg_metadata["llm_usage_total"] = dict(accumulator)
                        eff_bid = runtime._effective_building_id(persona, building_id)
                        runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)

                    # Store reasoning in state for downstream speak/say nodes
                    if reasoning_text:
                        state["_reasoning_text"] = reasoning_text
                    if reasoning_details is not None:
                        state["_reasoning_details"] = reasoning_details
                else:
                    # Non-streaming mode
                    text = llm_client.generate(
                        messages,
                        tools=[],
                        temperature=runtime._default_temperature(persona),
                        response_schema=response_schema,
                        **runtime._get_cache_kwargs(),
                    )

                    # Record usage
                    usage = llm_client.consume_usage()
                    llm_usage_metadata: Dict[str, Any] | None = None
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        # Build llm_usage metadata for message
                        from saiverse.model_configs import calculate_cost, get_model_display_name
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        llm_usage_metadata = {
                            "model": usage.model,
                            "model_display_name": get_model_display_name(usage.model),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cached_tokens": usage.cached_tokens,
                            "cache_write_tokens": usage.cache_write_tokens,
                            "cost_usd": cost,
                        }
                        # Accumulate into pulse total
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)

                    # Consume reasoning (thinking) from LLM — store as metadata
                    reasoning_entries = llm_client.consume_reasoning()
                    reasoning_text = "\n\n".join(
                        e.get("text", "") for e in reasoning_entries if e.get("text")
                    ) if reasoning_entries else ""
                    reasoning_details = llm_client.consume_reasoning_details()

                    # ── Spell detection in non-streaming normal mode ──
                    _spell_loop_count_sync = 0
                    _spell_details_blocks_sync: list[str] = []
                    if _spell_enabled and not response_schema and isinstance(text, str) and text and _parse_spell_line(text):
                        _spell_parsed_sync = _parse_spell_line(text)
                        while _spell_parsed_sync and _spell_loop_count_sync < _MAX_SPELL_LOOPS:
                            _spell_loop_count_sync += 1
                            _sp_name_sync, _sp_args_sync, _sp_match_sync = _spell_parsed_sync
                            LOGGER.info("[sea][spell] Sync loop %d: executing %s (args=%s)", _spell_loop_count_sync, _sp_name_sync, _sp_args_sync)

                            from tools import SPELL_TOOL_NAMES, SPELL_TOOL_SCHEMAS
                            if _sp_name_sync not in SPELL_TOOL_NAMES:
                                LOGGER.warning("[sea][spell] Unknown spell '%s', skipping", _sp_name_sync)
                                break

                            _sp_schema_sync = SPELL_TOOL_SCHEMAS.get(_sp_name_sync)
                            _sp_display_sync = (_sp_schema_sync.spell_display_name if _sp_schema_sync else "") or _sp_name_sync

                            _text_before_sync = text[:_sp_match_sync.start()].rstrip()
                            _spell_line_sync = _sp_match_sync.group(0)

                            _sp_result_sync = _execute_spell_inline(
                                _sp_name_sync, _sp_args_sync,
                                persona, building_id, playbook.name,
                                state, messages, runtime,
                                text_before=_text_before_sync,
                                spell_line=_spell_line_sync,
                                event_callback=event_callback,
                            )

                            _details_sync = _build_spell_details_html(_sp_name_sync, _sp_args_sync, _sp_display_sync, _sp_result_sync)
                            _spell_details_blocks_sync.append((_text_before_sync, _details_sync))

                            LOGGER.info("[sea][spell] Re-invoking LLM after spell %s", _sp_name_sync)
                            _retry_result_sync = llm_client.generate(
                                messages,
                                tools=None,
                                temperature=runtime._default_temperature(persona),
                                **runtime._get_cache_kwargs(),
                            )
                            _retry_usage_sync = llm_client.consume_usage()
                            if _retry_usage_sync:
                                get_usage_tracker().record_usage(
                                    model_id=_retry_usage_sync.model,
                                    input_tokens=_retry_usage_sync.input_tokens,
                                    output_tokens=_retry_usage_sync.output_tokens,
                                    cached_tokens=_retry_usage_sync.cached_tokens,
                                    cache_write_tokens=_retry_usage_sync.cache_write_tokens,
                                    cache_ttl=_retry_usage_sync.cache_ttl,
                                    persona_id=getattr(persona, "persona_id", None),
                                    building_id=building_id,
                                    node_type="llm_spell_retry",
                                    playbook_name=playbook.name,
                                    category="persona_speak",
                                )
                                from saiverse.model_configs import calculate_cost
                                _retry_cost_sync = calculate_cost(
                                    _retry_usage_sync.model, _retry_usage_sync.input_tokens, _retry_usage_sync.output_tokens,
                                    _retry_usage_sync.cached_tokens, _retry_usage_sync.cache_write_tokens, cache_ttl=_retry_usage_sync.cache_ttl,
                                )
                                runtime._accumulate_usage(
                                    state, _retry_usage_sync.model, _retry_usage_sync.input_tokens,
                                    _retry_usage_sync.output_tokens, _retry_cost_sync,
                                    _retry_usage_sync.cached_tokens, _retry_usage_sync.cache_write_tokens,
                                )

                            if isinstance(_retry_result_sync, dict):
                                text = _retry_result_sync.get("content", "")
                            elif isinstance(_retry_result_sync, str):
                                text = _retry_result_sync
                            else:
                                text = ""

                            _spell_parsed_sync = _parse_spell_line(text)

                    if _spell_loop_count_sync > 0:
                        LOGGER.info("[sea][spell] Sync: completed %d spell loop(s)", _spell_loop_count_sync)

                        # Bubble 1: text_before of first spell (no metadata)
                        _first_text_before_sync = _spell_details_blocks_sync[0][0] if _spell_details_blocks_sync else ""
                        if _first_text_before_sync.strip() and speak_flag is True:
                            pulse_id = state.get("_pulse_id")
                            eff_bid = runtime._effective_building_id(persona, building_id)
                            runtime._emit_say(persona, eff_bid, _first_text_before_sync, pulse_id=pulse_id)
                            if event_callback is not None:
                                event_callback({
                                    "type": "say",
                                    "content": _first_text_before_sync,
                                    "persona_id": getattr(persona, "persona_id", None),
                                })

                        # Bubble 2: details + continuation (text_before excluded)
                        _bubble2_parts_sync: list[str] = []
                        for _i_sync, (_tb_sync, _db_sync) in enumerate(_spell_details_blocks_sync):
                            if _i_sync > 0 and _tb_sync:
                                _bubble2_parts_sync.append(_tb_sync)
                            _bubble2_parts_sync.append(_db_sync)
                        if text:
                            _bubble2_parts_sync.append(text)
                        # text = bubble2 content for the speak_flag path below to emit with metadata
                        text = "\n".join(_bubble2_parts_sync)

                    # If speak=true but streaming disabled, send complete text and record to Building history
                    LOGGER.info("[DEBUG] speak_flag=%s, event_callback=%s, text_len=%d",
                               speak_flag, event_callback is not None, len(text) if text else 0)
                    if speak_flag is True:
                        pulse_id = state.get("_pulse_id")
                        # Resolve metadata_key for speak (e.g., media attachments from tool execution)
                        _speak_metadata_key2 = getattr(node_def, "metadata_key", None)
                        _speak_base_metadata2 = state.get(_speak_metadata_key2) if _speak_metadata_key2 else None
                        msg_metadata: Dict[str, Any] = {}
                        # Merge base metadata first (e.g., media from tool execution)
                        if _speak_base_metadata2 and isinstance(_speak_base_metadata2, dict):
                            msg_metadata.update(_speak_base_metadata2)
                        if llm_usage_metadata:
                            msg_metadata["llm_usage"] = llm_usage_metadata
                        if reasoning_text:
                            msg_metadata["reasoning"] = reasoning_text
                        if reasoning_details is not None:
                            msg_metadata["reasoning_details"] = reasoning_details
                        _at_speak = state.get("_activity_trace")
                        if _at_speak:
                            msg_metadata["activity_trace"] = list(_at_speak)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            msg_metadata["llm_usage_total"] = dict(accumulator)
                        eff_bid = runtime._effective_building_id(persona, building_id)
                        runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
                        if event_callback is not None:
                            LOGGER.info("[DEBUG] Sending 'say' event with content: %s", text[:100] if text else "(empty)")
                            say_event: Dict[str, Any] = {
                                "type": "say",
                                "content": text,
                                "persona_id": getattr(persona, "persona_id", None),
                            }
                            if reasoning_text:
                                say_event["reasoning"] = reasoning_text
                            if _at_speak:
                                say_event["activity_trace"] = list(_at_speak)
                            if msg_metadata:
                                say_event["metadata"] = msg_metadata
                            event_callback(say_event)

                    # Store remaining reasoning for say/speak node (non-speak path)
                    if reasoning_text:
                        state["_reasoning_text"] = reasoning_text
                    if reasoning_details is not None:
                        state["_reasoning_details"] = reasoning_details

                runtime._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
                schema_consumed = runtime._process_structured_output(node_def, text, state)

                # Set has_speak_content based on schema_consumed
                # If structured output was consumed, we need to set this flag
                # Otherwise, it's already set in the tool handling code above
                if schema_consumed:
                    # Structured output means we have usable data, set flag to True
                    # This allows conditional_next to proceed correctly
                    state["has_speak_content"] = True

                # If output_key is specified but no response_schema, store the raw text
                if not schema_consumed:
                    output_key = getattr(node_def, "output_key", None)
                    if output_key:
                        state[output_key] = text
                        LOGGER.info("[sea][llm] Stored plain text to state['%s'] = %s", output_key, text)

                # Process output_keys even in normal mode (no tools)
                output_keys_spec = getattr(node_def, "output_keys", None)
                if output_keys_spec:
                    for mapping in output_keys_spec:
                        if "text" in mapping:
                            text_key = mapping["text"]
                            state[text_key] = text
                            LOGGER.info("[sea][llm] (normal mode) Stored state['%s'] = %s", text_key, text)
                            state["has_speak_content"] = True
                            break
        except LLMError:
            # Propagate LLM errors to the caller for proper handling
            raise
        except Exception as exc:
            LOGGER.error("SEA LangGraph LLM failed: %s: %s", type(exc).__name__, exc)
            # Convert to LLMError so it propagates to the frontend
            raise LLMError(
                f"LLM node failed: {type(exc).__name__}: {exc}",
                original_error=exc,
            ) from exc
        state["last"] = text
        # Structured output may return a dict; serialise to JSON string
        # so that subsequent LLM calls receive valid message content.
        _msg_content = json.dumps(text, ensure_ascii=False) if isinstance(text, dict) else text

        # When tool call detected, create proper function-calling assistant message
        if state.get("tool_called") and state.get("_last_tool_call_id"):
            _tc_speak = _msg_content if state.get("has_speak_content") else ""
            _tc_entry: Dict[str, Any] = {
                "id": state["_last_tool_call_id"],
                "type": "function",
                "function": {
                    "name": state.get("_last_tool_name", ""),
                    "arguments": state.get("_last_tool_args_json", "{}"),
                },
            }
            # Gemini thinking models require thought_signature echoed back
            _thought_sig = state.get("_last_thought_signature")
            if _thought_sig:
                _tc_entry["thought_signature"] = _thought_sig
            _assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": _tc_speak,
                "tool_calls": [_tc_entry],
            }
            state["_messages"] = messages + [_assistant_msg]
            LOGGER.info("[sea][llm] Appended assistant message with tool_calls (id=%s, tool=%s)",
                       state["_last_tool_call_id"], state.get("_last_tool_name"))
        else:
            state["_messages"] = messages + [{"role": "assistant", "content": _msg_content}]

        # Append LLM interaction to PulseContext (replaces _intermediate_msgs)
        _pulse_ctx = state.get("_pulse_context")
        if _pulse_ctx:
            from sea.pulse_context import PulseLogEntry
            # Record the prompt (user message)
            if prompt:
                _pulse_ctx.append(PulseLogEntry(
                    role="user", content=prompt,
                    node_id=node_id, playbook_name=playbook.name))
            # Record the assistant response (with optional tool_calls)
            _tc_list = None
            if state.get("tool_called") and state.get("_last_tool_call_id"):
                _tc_entry_pc: Dict[str, Any] = {
                    "id": state["_last_tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": state.get("_last_tool_name", ""),
                        "arguments": state.get("_last_tool_args_json", "{}"),
                    },
                }
                _ts_pc = state.get("_last_thought_signature")
                if _ts_pc:
                    _tc_entry_pc["thought_signature"] = _ts_pc
                _tc_list = [_tc_entry_pc]
            _pulse_ctx.append(PulseLogEntry(
                role="assistant",
                content=_msg_content if state.get("has_speak_content") else "",
                node_id=node_id, playbook_name=playbook.name,
                tool_calls=_tc_list,
                important=getattr(node_def, "important", False) or False))

        # Trace: log prompt→response (truncation handled by log_sea_trace)
        _prompt_str = prompt or "(no prompt)"
        if schema_consumed:
            _output_key = getattr(node_def, "output_key", None) or node_id
            _out_val = state.get(_output_key, text)
            if isinstance(_out_val, dict):
                import json as _json
                _resp_str = _json.dumps(_out_val, ensure_ascii=False, default=str)
            else:
                _resp_str = str(_out_val)
            log_sea_trace(playbook.name, node_id, "LLM", f"prompt=\"{_prompt_str}\" → {_resp_str}")
        else:
            _resp_str = str(text) if text else "(empty)"
            log_sea_trace(playbook.name, node_id, "LLM", f"prompt=\"{_prompt_str}\" → \"{_resp_str}\"")

        # Handle memorize option - save prompt and response to SAIMemory
        memorize_config = getattr(node_def, "memorize", None)
        LOGGER.debug("[_lg_llm_node] node=%s memorize_config=%s type=%s schema_consumed=%s",
                   getattr(node_def, "id", "?"), memorize_config, type(memorize_config), schema_consumed)
        if memorize_config:
            pulse_id = state.get("_pulse_id")
            # Parse memorize config - can be True or {"tags": [...]}
            if isinstance(memorize_config, dict):
                memorize_tags = memorize_config.get("tags", [])
            else:
                memorize_tags = []

            # Save prompt (user role) - use the pre-expanded prompt variable
            _memorize_ok = True
            if prompt:
                if not runtime._store_memory(
                    persona,
                    prompt,
                    role="user",
                    tags=list(memorize_tags),
                    pulse_id=pulse_id,
                    playbook_name=playbook.name,
                ):
                    _memorize_ok = False
                else:
                    LOGGER.debug("[sea][llm] Memorized prompt (user): %s", prompt)

            # Save response (assistant role)
            if text and text != "(error in llm node)":
                # If structured output was consumed, format as JSON string for memory
                content_to_save = text
                if schema_consumed and isinstance(text, dict):
                    content_to_save = json.dumps(text, ensure_ascii=False, indent=2)
                    LOGGER.debug("[sea][llm] Structured output formatted as JSON for memory")

                # Build metadata for memorize (reasoning text + reasoning_details for multi-turn)
                _memorize_metadata: Dict[str, Any] = {}
                _mem_reasoning = state.get("_reasoning_text", "")
                if _mem_reasoning:
                    _memorize_metadata["reasoning"] = _mem_reasoning
                _mem_rd = state.get("_reasoning_details")
                if _mem_rd is not None:
                    _memorize_metadata["reasoning_details"] = _mem_rd

                if not runtime._store_memory(
                    persona,
                    content_to_save,
                    role="assistant",
                    tags=list(memorize_tags),
                    pulse_id=pulse_id,
                    metadata=_memorize_metadata if _memorize_metadata else None,
                    playbook_name=playbook.name,
                ):
                    _memorize_ok = False
                else:
                    LOGGER.debug("[sea][llm] Memorized response (assistant): %s", str(content_to_save))

            if not _memorize_ok and event_callback:
                event_callback({"type": "warning", "content": "記憶の保存に失敗しました。会話内容が記録されていない可能性があります。", "warning_code": "memorize_failed", "display": "toast"})

            # Activity trace: record LLM memorize
            if not playbook.name.startswith(("meta_", "sub_")):
                pb_display = playbook.display_name or playbook.name
                node_label = getattr(node_def, "label", None) or node_id
                _at = state.get("_activity_trace")
                if isinstance(_at, list):
                    _at.append({"action": "memorize", "name": node_label, "playbook": pb_display})
                if event_callback:
                    event_callback({
                        "type": "activity", "action": "memorize", "name": node_label,
                        "playbook": pb_display, "status": "completed",
                        "persona_id": getattr(persona, "persona_id", None),
                        "persona_name": getattr(persona, "persona_name", None),
                    })

        # Important flag: dual-write to messages (long-term memory) if not already memorized
        _is_important = getattr(node_def, "important", False)
        if _is_important and not memorize_config and text and text != "(error in llm node)":
            pulse_id = state.get("_pulse_id")
            content_to_save = text
            if schema_consumed and isinstance(text, dict):
                content_to_save = json.dumps(text, ensure_ascii=False, indent=2)
            if not runtime._store_memory(
                persona, content_to_save,
                role="assistant",
                tags=["conversation"],
                pulse_id=pulse_id,
                playbook_name=playbook.name,
            ):
                LOGGER.warning("[sea][llm] Important dual-write failed for node %s", node_id)

        # Debug: log speak_content at end of LLM node
        speak_content = state.get("speak_content", "")
        LOGGER.info("[DEBUG] LLM node end: state['speak_content'] = '%s'", speak_content)

        # Note: output_mapping in node definition handles state variable assignment
        # No special handling needed here anymore
        return state

    return node
