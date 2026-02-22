from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, Optional
from llm_clients.exceptions import LLMError
from saiverse.logging_config import log_sea_trace
from sea.playbook_models import PlaybookSchema
from saiverse.usage_tracker import get_usage_tracker

LOGGER = logging.getLogger(__name__)

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
        variables = {
            "input": state.get("inputs", {}).get("input", ""),
            "last": state.get("last", ""),
            "persona_id": getattr(persona, "persona_id", None),
            "persona_name": getattr(persona, "persona_name", None),
            **{k: v for k, v in state.items() if k not in ["messages", "inputs", "outputs", "persona_obj", "_cancellation_token"]},  # Include all other state variables
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
            # Determine base messages: use context_profile if set, otherwise legacy state["messages"]
            _profile_name = getattr(node_def, "context_profile", None)
            if _profile_name:
                from sea.playbook_models import CONTEXT_PROFILES
                _profile = CONTEXT_PROFILES.get(_profile_name)
                if _profile:
                    _cache_key = f"_ctx_profile_{_profile_name}"
                    if _cache_key not in state:
                        state[_cache_key] = runtime._prepare_context(
                            persona, building_id,
                            state.get("inputs", {}).get("input") or None,
                            _profile["requirements"],
                            pulse_id=state.get("pulse_id"),
                        )
                        LOGGER.info("[sea] Prepared context for profile '%s' (node=%s, %d messages)",
                                    _profile_name, node_id, len(state[_cache_key]))
                    _profile_base = state[_cache_key]
                    _intermediate = state.get("_intermediate_msgs", [])
                    base_msgs = list(_profile_base) + list(_intermediate)
                else:
                    LOGGER.warning("[sea] Unknown context_profile '%s' on node '%s', falling back to state messages", _profile_name, node_id)
                    base_msgs = state.get("messages", [])
            else:
                base_msgs = state.get("messages", [])
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

            if available_tools:
                LOGGER.info("[DEBUG] Entering tools mode (generate with tools)")
                # Tool calling mode - use unified generate() with tools
                tools_spec = runtime._build_tools_spec(available_tools, llm_client)
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

                    # Format as JSON for logging
                    text = json.dumps({
                        "tool": result["tool_name"],
                        "args": result["tool_args"]
                    }, ensure_ascii=False)
                    LOGGER.info("[sea] Tool call detected: %s", text)

                elif result["type"] == "both":
                    LOGGER.info("[DEBUG] Entering 'both' branch (text + tool call)")
                    # Both text and tool call
                    if output_keys_spec:
                        # New behavior: use explicit output_keys
                        if text_key:
                            state[text_key] = result["content"]
                            LOGGER.debug("[sea] Stored %s = (text, length=%d)", text_key, len(result["content"]))
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
                        state["speak_content"] = result["content"]
                        # Expand tool_args for legacy args_input (tool_arg_*)
                        if isinstance(result["tool_args"], dict):
                            for key, value in result["tool_args"].items():
                                state[f"tool_arg_{key}"] = value
                                LOGGER.debug("[sea] Expanded tool_arg_%s = %s", key, value)

                    text = result["content"]
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
                    for stream_attempt in range(max_stream_retries):
                        text_chunks = []
                        for chunk in llm_client.generate_stream(
                            messages,
                            tools=[],
                            temperature=runtime._default_temperature(persona),
                            **runtime._get_cache_kwargs(),
                        ):
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
                        text = "".join(text_chunks)

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

                    # Record usage
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
                    pulse_id = state.get("pulse_id")
                    msg_metadata: Dict[str, Any] = {}
                    # Merge base metadata first (e.g., media from tool execution)
                    if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                        msg_metadata.update(_speak_base_metadata)
                    if llm_usage_metadata:
                        msg_metadata["llm_usage"] = llm_usage_metadata
                    if reasoning_text:
                        msg_metadata["reasoning"] = reasoning_text
                    _at_stream = state.get("_activity_trace")
                    if _at_stream:
                        msg_metadata["activity_trace"] = list(_at_stream)
                    accumulator = state.get("pulse_usage_accumulator")
                    if accumulator:
                        msg_metadata["llm_usage_total"] = dict(accumulator)
                    eff_bid = runtime._effective_building_id(persona, building_id)
                    runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
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

                    # If speak=true but streaming disabled, send complete text and record to Building history
                    LOGGER.info("[DEBUG] speak_flag=%s, event_callback=%s, text_len=%d",
                               speak_flag, event_callback is not None, len(text) if text else 0)
                    if speak_flag is True:
                        pulse_id = state.get("pulse_id")
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
                        _at_speak = state.get("_activity_trace")
                        if _at_speak:
                            msg_metadata["activity_trace"] = list(_at_speak)
                        accumulator = state.get("pulse_usage_accumulator")
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
        state["messages"] = messages + [{"role": "assistant", "content": _msg_content}]

        # Track intermediate messages for profile-based nodes in the same playbook
        if "_intermediate_msgs" in state:
            _im = list(state.get("_intermediate_msgs", []))
            if prompt:
                _im.append({"role": "user", "content": prompt})
            _im.append({"role": "assistant", "content": _msg_content})
            state["_intermediate_msgs"] = _im

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
            pulse_id = state.get("pulse_id")
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

                if not runtime._store_memory(
                    persona,
                    content_to_save,
                    role="assistant",
                    tags=list(memorize_tags),
                    pulse_id=pulse_id,
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

        # Debug: log speak_content at end of LLM node
        speak_content = state.get("speak_content", "")
        LOGGER.info("[DEBUG] LLM node end: state['speak_content'] = '%s'", speak_content)

        # Note: output_mapping in node definition handles state variable assignment
        # No special handling needed here anymore
        return state

    return node
