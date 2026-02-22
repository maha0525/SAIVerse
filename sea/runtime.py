from __future__ import annotations

import logging
import os
import uuid
import asyncio
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
import json
import re

from llm_clients.exceptions import LLMError
from saiverse.logging_config import log_sea_trace
from sea.playbook_models import NodeType, PlaybookSchema, PlaybookValidationError, validate_playbook_graph
from sea.langgraph_runner import compile_playbook
from sea.cancellation import CancellationToken, ExecutionCancelledException
from database.models import Playbook as PlaybookModel
from saiverse.model_configs import get_model_parameter_defaults
from saiverse.usage_tracker import get_usage_tracker

LOGGER = logging.getLogger(__name__)


def _get_default_lightweight_model() -> str:
    """Get the default lightweight model from environment or fallback."""
    return os.getenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gemini-2.5-flash-lite-preview-09-2025")


def _is_llm_streaming_enabled() -> bool:
    """Check if LLM streaming is enabled (default: True)."""
    val = os.getenv("SAIVERSE_LLM_STREAMING", "true")
    result = val.lower() not in ("false", "0", "off", "no")
    logging.info("[DEBUG] _is_llm_streaming_enabled: raw_val=%r, result=%s", val, result)
    return result


def _format(template: str, variables: Dict[str, Any]) -> str:
    """Format template with variables, supporting dot notation keys.

    Uses regex-based replacement to safely handle templates where variable values
    may contain curly braces (e.g., LLM-generated text with {}).
    """
    result = template

    # Build a lookup dict with all keys (including nested access via dot notation)
    lookup: Dict[str, str] = {}
    for key, value in variables.items():
        lookup[str(key)] = str(value) if value is not None else ""

    # Replace {key} patterns with corresponding values
    # Only replace if the key exists in our lookup
    def replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in lookup:
            return lookup[key]
        # Key not found, leave placeholder as-is
        return match.group(0)

    # Pattern: {word_chars_and_dots} but not empty
    result = re.sub(r"\{([\w.]+)\}", replacer, result)

    return result


class SEARuntime:
    """Lightweight executor for meta playbooks until full LangGraph port."""

    def __init__(self, manager_ref: Any):
        self.manager = manager_ref
        self.playbooks_dir = Path(__file__).parent / "playbooks"
        self._playbook_cache: Dict[str, PlaybookSchema] = {}
        self._trace = bool(os.getenv("SAIVERSE_SEA_TRACE"))

    # ---------------- meta entrypoints -----------------
    def run_meta_user(
        self,
        persona,
        user_input: str,
        building_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        playbook_params: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: str = "user",
    ) -> List[str]:
        """Router -> subgraph -> speak. Returns spoken strings for gateway/UI."""
        # Check for cancellation before starting
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        
        # Store pulse_type in persona for tools to access
        persona._current_pulse_type = pulse_type
        
        # Record user input to history before processing
        if user_input:
            try:
                user_msg: Dict[str, Any] = {"role": "user", "content": user_input}
                # Build metadata with "with" field for user messages
                msg_metadata: Dict[str, Any] = {"with": ["user"]}
                if metadata:
                    msg_metadata.update(metadata)
                user_msg["metadata"] = msg_metadata
                persona.history_manager.add_message(user_msg, building_id, heard_by=None)
            except Exception:
                LOGGER.exception("Failed to record user input to history")

        # Use user-selected meta playbook if specified, otherwise choose automatically
        if meta_playbook:
            playbook = self._load_playbook_for(meta_playbook, persona, building_id)
            if playbook is None:
                LOGGER.warning("Meta playbook '%s' not found, falling back to automatic selection", meta_playbook)
                playbook = self._choose_playbook(kind="user", persona=persona, building_id=building_id)
        else:
            playbook = self._choose_playbook(kind="user", persona=persona, building_id=building_id)
        result = self._run_playbook(
            playbook, persona, building_id, user_input,
            auto_mode=False, record_history=True, event_callback=event_callback,
            cancellation_token=cancellation_token, pulse_type=pulse_type,
            initial_params=playbook_params,
        )

        # Post-response metabolism check
        bh_before = len(self.manager.building_histories.get(building_id, []))
        try:
            self._maybe_run_metabolism(persona, building_id, event_callback)
        except Exception:
            LOGGER.exception("[metabolism] Post-response metabolism failed")
        bh_after = len(self.manager.building_histories.get(building_id, []))
        if bh_before != bh_after:
            LOGGER.warning(
                "[metabolism] building_histories[%s] changed during metabolism: %d -> %d",
                building_id, bh_before, bh_after,
            )

        return result

    def run_meta_auto(
        self,
        persona,
        building_id: str,
        occupants: List[str],
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: str = "auto",
    ) -> None:
        """Router -> subgraph -> think. For autonomous loop, no direct user output."""
        # Check for cancellation before starting
        if cancellation_token:
            cancellation_token.raise_if_cancelled()

        # Store pulse_type in persona for tools to access
        persona._current_pulse_type = pulse_type

        # Update last pulse time for get_situation_snapshot
        persona._last_conscious_prompt_time_utc = datetime.now(dt_timezone.utc)
        playbook = self._choose_playbook(kind="auto", persona=persona, building_id=building_id)
        self._run_playbook(
            playbook, persona, building_id, user_input=None,
            auto_mode=True, record_history=True,
            cancellation_token=cancellation_token, pulse_type=pulse_type,
        )

        # Post-auto metabolism check (no event_callback for auto pulses)
        try:
            self._maybe_run_metabolism(persona, building_id)
        except Exception:
            LOGGER.exception("[metabolism] Post-auto metabolism failed")

    # ---------------- core runner -----------------
    def _run_playbook(
        self,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        user_input: Optional[str],
        auto_mode: bool,
        record_history: bool = True,
        parent_state: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: Optional[str] = None,
        initial_params: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        # Check for cancellation at start
        if cancellation_token:
            cancellation_token.raise_if_cancelled()

        # Generate or inherit pulse_id
        parent = parent_state or {}

        # Merge initial_params into parent state (these are user-provided playbook parameters)
        if initial_params:
            LOGGER.debug("[sea] _run_playbook merging initial_params: %s", list(initial_params.keys()))
            parent.update(initial_params)
        LOGGER.debug("[sea] _run_playbook called for %s, parent_state keys: %s", playbook.name, list(parent.keys()) if parent else "(none)")
        if "pulse_id" in parent:
            pulse_id = str(parent["pulse_id"])
        else:
            pulse_id = str(uuid.uuid4())

        # Build playbook chain for status display (e.g., "meta_user/exec > basic_chat/generate")
        parent_chain = parent.get("_playbook_chain", "")
        if parent_chain:
            current_chain = f"{parent_chain} > {playbook.name}"
        else:
            current_chain = playbook.name

        # Store chain in parent_state for sub-playbooks to inherit
        parent["_playbook_chain"] = current_chain
        
        # Store cancellation token in parent_state for propagation
        if cancellation_token:
            parent["_cancellation_token"] = cancellation_token

        # Wrap event_callback to include playbook chain in status events
        def wrapped_event_callback(event: Dict[str, Any]) -> None:
            if event_callback:
                if event.get("type") == "status":
                    # Replace playbook name with full chain
                    node = event.get("node", "")
                    event["content"] = f"{current_chain} / {node}"
                    event["playbook_chain"] = current_chain
                event_callback(event)

        # Update execution state: playbook started
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = playbook.name
            persona.execution_state["node"] = playbook.start_node
            persona.execution_state["status"] = "running"

        # Prepare shared context (system prompt, history, inventories)
        LOGGER.info("[sea][run-playbook] %s: calling _prepare_context with history_depth=%s, pulse_id=%s",
                    playbook.name,
                    playbook.context_requirements.history_depth if playbook.context_requirements else "None",
                    pulse_id)
        context_warnings: List[Dict[str, Any]] = []
        base_messages = self._prepare_context(persona, building_id, user_input, playbook.context_requirements, pulse_id=pulse_id, warnings=context_warnings, event_callback=wrapped_event_callback, cancellation_token=cancellation_token)
        LOGGER.info("[sea][run-playbook] %s: _prepare_context returned %d messages", playbook.name, len(base_messages))
        conversation_msgs = list(base_messages)

        # Emit context budget warnings via event callback
        for warn in context_warnings:
            if event_callback:
                wrapped_event_callback(warn)

        # Execute playbook with LangGraph (use wrapped callback)
        compiled_ok = self._compile_with_langgraph(
            playbook, persona, building_id, user_input, auto_mode,
            conversation_msgs, pulse_id, parent_state=parent,
            event_callback=wrapped_event_callback,
            cancellation_token=cancellation_token,
            pulse_type=pulse_type,
        )
        if compiled_ok is None:
            # LangGraph compilation failed - this should not happen as all node types are now supported
            LOGGER.error("LangGraph compilation failed for playbook '%s'. This indicates a configuration or dependency issue.", playbook.name)
            # Update execution state: playbook failed
            if hasattr(persona, "execution_state"):
                persona.execution_state["playbook"] = None
                persona.execution_state["node"] = None
                persona.execution_state["status"] = "idle"
            return []

        return compiled_ok

    # LangGraph compile wrapper -----------------------------------------
    def _compile_with_langgraph(
        self,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        user_input: Optional[str],
        auto_mode: bool,
        base_messages: List[Dict[str, Any]],
        pulse_id: str,
        parent_state: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: Optional[str] = None,
    ) -> Optional[List[str]]:
        _lg_outputs: List[str] = []
        temperature = self._default_temperature(persona)
        parent = parent_state or {}

        # Update execution state: playbook started (LangGraph path)
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = playbook.name
            persona.execution_state["node"] = playbook.start_node
            persona.execution_state["status"] = "running"

        compiled = compile_playbook(
            playbook,
            llm_node_factory=lambda node_def: self._lg_llm_node(node_def, persona, building_id, playbook, event_callback),
            tool_node_factory=lambda node_def: self._lg_tool_node(node_def, persona, playbook, event_callback, auto_mode=auto_mode),
            tool_call_node_factory=lambda node_def: self._lg_tool_call_node(node_def, persona, playbook, event_callback, auto_mode=auto_mode),
            speak_node=lambda state: self._lg_speak_node(state, persona, building_id, playbook, _lg_outputs, event_callback),
            think_node=lambda state: self._lg_think_node(state, persona, playbook, _lg_outputs, event_callback),
            say_node_factory=lambda node_def: self._lg_say_node(node_def, persona, building_id, playbook, _lg_outputs, event_callback),
            memorize_node_factory=lambda node_def: self._lg_memorize_node(node_def, persona, playbook, _lg_outputs, event_callback),
            exec_node_factory=lambda node_def: self._lg_exec_node(node_def, playbook, persona, building_id, auto_mode, _lg_outputs, event_callback),
            subplay_node_factory=lambda node_def: self._lg_subplay_node(node_def, persona, building_id, playbook, auto_mode, _lg_outputs, event_callback),
            set_node_factory=lambda node_def: self._lg_set_node(node_def, playbook, event_callback),
            stelis_start_node_factory=lambda node_def: self._lg_stelis_start_node(node_def, persona, playbook, event_callback),
            stelis_end_node_factory=lambda node_def: self._lg_stelis_end_node(node_def, persona, playbook, event_callback),
        )
        if not compiled:
            # Update execution state: compilation failed, reset to idle
            if hasattr(persona, "execution_state"):
                persona.execution_state["playbook"] = None
                persona.execution_state["node"] = None
                persona.execution_state["status"] = "idle"
            raise LLMError(
                f"Playbook '{playbook.name}' graph compilation failed",
                user_message=f"プレイブック '{playbook.name}' のグラフ構築に失敗しました。",
            )

        # Process input_schema to inherit variables from parent_state
        inherited_vars = {}
        for param in playbook.input_schema:
            param_name = param.name
            source_key = param.source if param.source else "input"

            # If this param already exists in parent (e.g., from initial_params/playbook_params),
            # use that value instead of resolving from source
            if param_name in parent and parent[param_name] is not None:
                value = parent[param_name]
                LOGGER.debug("[sea][LangGraph] Using existing value for %s from parent: %s", param_name, str(value) if value else "(empty)")
            # Resolve value from parent_state or fallback
            elif source_key.startswith("parent."):
                actual_key = source_key[7:]  # strip "parent."
                # Use _resolve_state_value for dot-notation support (e.g., "current_task.objective")
                value = self._resolve_state_value(parent, actual_key)
                if value is None:
                    value = ""
                LOGGER.debug("[sea][LangGraph] Resolved %s from parent.%s: %s", param_name, actual_key, str(value) if value else "(empty)")
            elif source_key == "input":
                value = user_input or ""
            else:
                value = parent.get(source_key, "")

            inherited_vars[param_name] = value

        # Inherit pulse_usage_accumulator from parent_state if it exists (for sub-playbook calls)
        # This ensures usage is accumulated across all LLM calls in the entire pulse chain
        parent_accumulator = parent.get("pulse_usage_accumulator")
        if parent_accumulator:
            # Use the same accumulator (reference) to accumulate across sub-playbooks
            usage_accumulator = parent_accumulator
        else:
            # Create new accumulator for this pulse
            usage_accumulator = {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cached_tokens": 0,
                "total_cache_write_tokens": 0,
                "total_cost_usd": 0.0,
                "call_count": 0,
                "models_used": [],
            }

        # Inherit activity trace list (shared reference, same pattern as accumulator)
        parent_activity_trace = parent.get("_activity_trace")
        if parent_activity_trace is not None:
            activity_trace = parent_activity_trace
        else:
            activity_trace = []

        # Inherit cancellation token from parent state if not explicitly provided
        effective_cancellation_token = cancellation_token or parent.get("_cancellation_token")

        initial_state = {
            "messages": list(base_messages),
            "inputs": {"input": user_input or ""},
            "context": {},
            "last": user_input or "",
            "outputs": _lg_outputs,
            "persona_obj": persona,
            "pulse_id": pulse_id,
            "pulse_type": pulse_type,  # user/schedule/auto
            "_cancellation_token": effective_cancellation_token,  # For node-level cancellation checks
            "pulse_usage_accumulator": usage_accumulator,  # Inherit from parent or create new
            "_activity_trace": activity_trace,  # Shared trace of exec/tool activities
            "_intermediate_msgs": [],  # Track intermediate node outputs for profile-based context
            **inherited_vars,  # Add inherited variables from input_schema
        }

        # Execute compiled playbook
        # Set recursion limit high enough for agentic loops (default is 25, too low for multi-step agents)
        langgraph_config = {"recursion_limit": 1000}

        try:
            # Check cancellation before starting execution
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            
            # Check if we're inside an existing event loop
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop and running_loop.is_running():
                # We're inside an existing loop (e.g., Gradio), use run_in_executor
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, compiled(initial_state, langgraph_config))
                    final_state = future.result()
            else:
                # No running loop, use asyncio.run directly
                final_state = asyncio.run(compiled(initial_state, langgraph_config))
        except ExecutionCancelledException:
            # Re-raise cancellation exceptions
            raise
        except LLMError:
            # Re-raise LLM errors for proper error handling in caller
            # Update execution state: execution failed, reset to idle
            if hasattr(persona, "execution_state"):
                persona.execution_state["playbook"] = None
                persona.execution_state["node"] = None
                persona.execution_state["status"] = "idle"
            raise
        except Exception as exc:
            LOGGER.exception("SEA LangGraph execution failed")
            # Update execution state: execution failed, reset to idle
            if hasattr(persona, "execution_state"):
                persona.execution_state["playbook"] = None
                persona.execution_state["node"] = None
                persona.execution_state["status"] = "idle"
            # Wrap as LLMError so existing error propagation chain
            # delivers it to the frontend instead of silently swallowing.
            raise LLMError(
                f"Playbook execution failed: {type(exc).__name__}: {exc}",
                original_error=exc,
                user_message=f"プレイブックの実行中にエラーが発生しました: {exc}",
            ) from exc

        # Write back state variables to parent_state based on output_schema
        if parent_state is not None and isinstance(final_state, dict) and playbook.output_schema:
            for key in playbook.output_schema:
                if key in final_state:
                    value = final_state[key]
                    # Use _store_structured_result to also create flattened dot-notation keys
                    # (e.g., research_result.summary, research_result.status)
                    if isinstance(value, dict):
                        self._store_structured_result(parent_state, key, value)
                    else:
                        parent_state[key] = value
                    LOGGER.debug("[sea][LangGraph] Propagated %s to parent_state: %s", key, str(value))

        # Update execution state: playbook completed (LangGraph path)
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"

        # speak/think nodes already emitted; return collected texts for UI consistency
        return list(_lg_outputs)

    def _lg_llm_node(self, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
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
                            state[_cache_key] = self._prepare_context(
                                persona, building_id,
                                state.get("inputs", {}).get("input") or None,
                                _profile["requirements"],
                                pulse_id=state.get("pulse_id"),
                                event_callback=event_callback,
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
                    response_schema = self._add_playbook_enum(response_schema, state.get("available_playbooks"))

                # Select LLM client based on model_type and structured output needs
                needs_structured_output = response_schema is not None
                llm_client = self._select_llm_client(node_def, persona, needs_structured_output=needs_structured_output)

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
                    tools_spec = self._build_tools_spec(available_tools, llm_client)

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
                                temperature=self._default_temperature(persona),
                                **self._get_cache_kwargs(),
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
                            self._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)

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
                                pulse_id = state.get("pulse_id")
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
                                eff_bid = self._effective_building_id(persona, building_id)
                                self._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
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
                            pulse_id = state.get("pulse_id")
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
                            accumulator = state.get("pulse_usage_accumulator")
                            if accumulator:
                                msg_metadata["llm_usage_total"] = dict(accumulator)
                            eff_bid = self._effective_building_id(persona, building_id)
                            self._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)

                    else:
                        # ── Synchronous tool mode (original) ──
                        result = llm_client.generate(
                            messages,
                            tools=tools_spec,
                            temperature=self._default_temperature(persona),
                            **self._get_cache_kwargs(),
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
                            self._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)

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

                    self._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
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
                                temperature=self._default_temperature(persona),
                                **self._get_cache_kwargs(),
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
                            self._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)
                        else:
                            LOGGER.warning("[DEBUG] No usage data from LLM client")

                        # Consume reasoning (thinking) from LLM — store as metadata, not in content
                        reasoning_entries = llm_client.consume_reasoning()
                        reasoning_text = "\n\n".join(
                            e.get("text", "") for e in reasoning_entries if e.get("text")
                        ) if reasoning_entries else ""
                        reasoning_details = llm_client.consume_reasoning_details()

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
                        if reasoning_details is not None:
                            msg_metadata["reasoning_details"] = reasoning_details
                        _at_stream = state.get("_activity_trace")
                        if _at_stream:
                            msg_metadata["activity_trace"] = list(_at_stream)
                        accumulator = state.get("pulse_usage_accumulator")
                        if accumulator:
                            msg_metadata["llm_usage_total"] = dict(accumulator)
                        eff_bid = self._effective_building_id(persona, building_id)
                        self._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)

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
                            temperature=self._default_temperature(persona),
                            response_schema=response_schema,
                            **self._get_cache_kwargs(),
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
                            self._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)

                        # Consume reasoning (thinking) from LLM — store as metadata
                        reasoning_entries = llm_client.consume_reasoning()
                        reasoning_text = "\n\n".join(
                            e.get("text", "") for e in reasoning_entries if e.get("text")
                        ) if reasoning_entries else ""
                        reasoning_details = llm_client.consume_reasoning_details()

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
                            if reasoning_details is not None:
                                msg_metadata["reasoning_details"] = reasoning_details
                            _at_speak = state.get("_activity_trace")
                            if _at_speak:
                                msg_metadata["activity_trace"] = list(_at_speak)
                            accumulator = state.get("pulse_usage_accumulator")
                            if accumulator:
                                msg_metadata["llm_usage_total"] = dict(accumulator)
                            eff_bid = self._effective_building_id(persona, building_id)
                            self._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
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

                    self._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
                    schema_consumed = self._process_structured_output(node_def, text, state)

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
                state["messages"] = messages + [_assistant_msg]
                LOGGER.info("[sea][llm] Appended assistant message with tool_calls (id=%s, tool=%s)",
                           state["_last_tool_call_id"], state.get("_last_tool_name"))
            else:
                state["messages"] = messages + [{"role": "assistant", "content": _msg_content}]

            # Track intermediate messages for profile-based nodes in the same playbook
            if "_intermediate_msgs" in state:
                _im = list(state.get("_intermediate_msgs", []))
                if prompt:
                    _im.append({"role": "user", "content": prompt})
                # When tool called, include tool_calls in intermediate msgs so
                # context_profile-based nodes see the function calling protocol
                if state.get("tool_called") and state.get("_last_tool_call_id"):
                    _im_tc: Dict[str, Any] = {
                        "id": state["_last_tool_call_id"],
                        "type": "function",
                        "function": {
                            "name": state.get("_last_tool_name", ""),
                            "arguments": state.get("_last_tool_args_json", "{}"),
                        },
                    }
                    _im_ts = state.get("_last_thought_signature")
                    if _im_ts:
                        _im_tc["thought_signature"] = _im_ts
                    _im.append({
                        "role": "assistant",
                        "content": _msg_content if state.get("has_speak_content") else "",
                        "tool_calls": [_im_tc],
                    })
                else:
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
                    if not self._store_memory(
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

                    # Build metadata for memorize (reasoning text + reasoning_details for multi-turn)
                    _memorize_metadata: Dict[str, Any] = {}
                    _mem_reasoning = state.get("_reasoning_text", "")
                    if _mem_reasoning:
                        _memorize_metadata["reasoning"] = _mem_reasoning
                    _mem_rd = state.get("_reasoning_details")
                    if _mem_rd is not None:
                        _memorize_metadata["reasoning_details"] = _mem_rd

                    if not self._store_memory(
                        persona,
                        content_to_save,
                        role="assistant",
                        tags=list(memorize_tags),
                        pulse_id=pulse_id,
                        metadata=_memorize_metadata if _memorize_metadata else None,
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

    def _default_temperature(self, persona: Any) -> Optional[float]:
        try:
            model_name = getattr(persona, "model", None)
            if not model_name:
                return None
            defaults = get_model_parameter_defaults(model_name)
            temp = defaults.get("temperature")
            if temp is None:
                return None
            try:
                return float(temp)
            except Exception:
                return None
        except Exception:
            return None

    def _accumulate_usage(
        self,
        state: Dict[str, Any],
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Accumulate LLM usage into the pulse-level accumulator.

        Args:
            state: Current state dict containing pulse_usage_accumulator
            model: Model identifier
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cost_usd: Cost in USD
            cached_tokens: Number of tokens served from cache
            cache_write_tokens: Number of tokens written to cache
        """
        accumulator = state.get("pulse_usage_accumulator")
        if accumulator is None:
            return
        accumulator["total_input_tokens"] += input_tokens
        accumulator["total_output_tokens"] += output_tokens
        accumulator["total_cached_tokens"] += cached_tokens
        accumulator["total_cache_write_tokens"] += cache_write_tokens
        accumulator["total_cost_usd"] += cost_usd
        accumulator["call_count"] += 1
        if model and model not in accumulator["models_used"]:
            accumulator["models_used"].append(model)

    def _get_cache_kwargs(self) -> Dict[str, Any]:
        """Get cache settings from manager state for LLM client calls.

        Returns:
            Dict with enable_cache and cache_ttl kwargs for Anthropic client.
            Non-Anthropic clients will ignore these kwargs.
        """
        if self.manager and hasattr(self.manager, "state"):
            return {
                "enable_cache": getattr(self.manager.state, "cache_enabled", True),
                "cache_ttl": getattr(self.manager.state, "cache_ttl", "5m"),
            }
        return {"enable_cache": True, "cache_ttl": "5m"}

    def _select_llm_client(self, node_def: Any, persona: Any, needs_structured_output: bool = False) -> Any:
        """Select the appropriate LLM client based on node's model_type and structured output needs.

        Args:
            node_def: Node definition from playbook
            persona: Persona object
            needs_structured_output: Whether this node requires structured output
        """
        # Determine model_type: context_profile takes precedence over explicit model_type
        _profile_name = getattr(node_def, "context_profile", None)
        if _profile_name:
            from sea.playbook_models import CONTEXT_PROFILES
            _profile = CONTEXT_PROFILES.get(_profile_name)
            model_type = _profile["model_type"] if _profile else (getattr(node_def, "model_type", "normal") or "normal")
        else:
            model_type = getattr(node_def, "model_type", "normal") or "normal"
        LOGGER.info("[sea] Node model_type: %s (node_id=%s, profile=%s)", model_type, getattr(node_def, "id", "unknown"), _profile_name or "none")

        # First, select base client based on model_type
        if model_type == "lightweight":
            # Try persona's lightweight_llm_client first
            lightweight_client = getattr(persona, "lightweight_llm_client", None)
            LOGGER.info("[sea] lightweight_client exists: %s", lightweight_client is not None)
            if lightweight_client:
                LOGGER.info("[sea] Using persona's lightweight_llm_client")
                base_client = lightweight_client
                base_model = getattr(persona, "lightweight_model", None) or _get_default_lightweight_model()
            else:
                # Fallback: create a temporary lightweight client
                LOGGER.info("[sea] Persona has no lightweight_llm_client; creating temporary client with default model")
                lightweight_model_name = getattr(persona, "lightweight_model", None) or _get_default_lightweight_model()
                LOGGER.info("[sea] Using lightweight model: %s", lightweight_model_name)
                try:
                    from llm_clients import get_llm_client
                    from saiverse.model_configs import get_context_length, get_model_provider
                    lw_context = get_context_length(lightweight_model_name)
                    provider = get_model_provider(lightweight_model_name)
                    base_client = get_llm_client(lightweight_model_name, provider, lw_context)
                    base_model = lightweight_model_name
                except Exception as exc:
                    LOGGER.warning("[sea] Failed to create lightweight client: %s; falling back to normal client", exc)
                    base_client = persona.llm_client
                    base_model = getattr(persona, "model", "unknown")
        else:
            # Default: use normal client
            LOGGER.info("[sea] Using normal llm_client")
            base_client = persona.llm_client
            base_model = getattr(persona, "model", "unknown")
            LOGGER.info("[sea] persona.model=%s, llm_client type=%s", base_model, type(base_client).__name__)

        # Guard: if no client was resolved, raise a clear error
        if base_client is None:
            persona_name = getattr(persona, "persona_name", "unknown")
            raise LLMError(
                f"LLM client is not initialized for persona '{persona_name}' (model={base_model})",
                user_message=f"ペルソナ「{persona_name}」のLLMクライアントが初期化されていません。チャットオプションでモデルを選択してください。",
            )

        # If structured output is needed, check if the selected model supports it
        if needs_structured_output:
            from saiverse.model_configs import supports_structured_output, get_agentic_model, get_context_length, get_model_provider
            if not supports_structured_output(base_model):
                # Model doesn't support structured output, switch to agentic model
                agentic_model = get_agentic_model()
                # Guard: if the agentic model itself doesn't support structured output,
                # fall back to the built-in default instead
                if not supports_structured_output(agentic_model):
                    builtin_default = "gemini-2.5-flash-lite-preview-09-2025"
                    LOGGER.warning(
                        "[sea] Agentic model '%s' also doesn't support structured output, "
                        "falling back to built-in default: %s", agentic_model, builtin_default)
                    agentic_model = builtin_default
                LOGGER.info("[sea] Model '%s' doesn't support structured output, switching to agentic model: %s",
                           base_model, agentic_model)
                try:
                    from llm_clients import get_llm_client
                    ag_context = get_context_length(agentic_model)
                    ag_provider = get_model_provider(agentic_model)
                    return get_llm_client(agentic_model, ag_provider, ag_context)
                except Exception as exc:
                    LOGGER.warning("[sea] Failed to create agentic client: %s; using base client", exc)
                    return base_client

        return base_client

    def _build_tools_spec(self, tool_names: List[str], llm_client: Any) -> List[Any]:
        """Build tools spec for LLM based on available tool names and llm_client type."""
        from tools import OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC

        LOGGER.info("[sea] _build_tools_spec called with tool_names: %s", tool_names)

        # Determine provider from llm_client class name
        client_class_name = type(llm_client).__name__
        LOGGER.info("[sea] LLM client class: %s", client_class_name)

        if client_class_name in ("OpenAIClient", "AnthropicClient", "OllamaClient", "NvidiaNIMClient", "LlamaCppClient"):
            # Filter OpenAI tools spec (OpenAI-compatible)
            LOGGER.info("[sea] Using OpenAI-compatible tools format (client: %s)", client_class_name)
            LOGGER.info("[sea] Filtering from OPENAI_TOOLS_SPEC (total: %d)", len(OPENAI_TOOLS_SPEC))
            filtered = [
                tool for tool in OPENAI_TOOLS_SPEC
                if tool.get("function", {}).get("name") in tool_names
            ]
            LOGGER.info("[sea] Built OpenAI tools spec: %d tools", len(filtered))
            for tool in filtered:
                LOGGER.info("[sea] - OpenAI tool: %s", tool.get("function", {}).get("name"))
                LOGGER.info("[sea]   Full spec: %s", tool)
            return filtered
        else:
            # Filter Gemini tools spec - combine all matching declarations into a single Tool
            LOGGER.info("[sea] Using Gemini tools format (client: %s)", client_class_name)
            from google.genai import types
            all_matching_decls = []
            for tool in GEMINI_TOOLS_SPEC:
                if hasattr(tool, "function_declarations"):
                    matching_decls = [
                        decl for decl in tool.function_declarations
                        if decl.name in tool_names
                    ]
                    all_matching_decls.extend(matching_decls)

            if all_matching_decls:
                # Gemini requires all function_declarations in a single Tool object
                filtered = [types.Tool(function_declarations=all_matching_decls)]
                LOGGER.info("[sea] Built Gemini tools spec: 1 Tool with %d function_declarations", len(all_matching_decls))
                for decl in all_matching_decls:
                    LOGGER.info("[sea] - Gemini function_declaration: name=%s, description=%s", decl.name, decl.description)
                    LOGGER.info("[sea]   parameters: %s", decl.parameters)
            else:
                filtered = []
                LOGGER.info("[sea] Built Gemini tools spec: 0 tools")
            return filtered

    def _dump_llm_io(
        self,
        playbook_name: str,
        node_id: str,
        persona: Any,
        messages: List[Dict[str, Any]],
        output_text: str,
    ) -> None:
        """Log LLM I/O to the unified LLM log file."""
        try:
            from saiverse.logging_config import log_llm_request, log_llm_response
            persona_id = getattr(persona, "persona_id", None)
            persona_name = getattr(persona, "persona_name", None)
            source = f"sea/{playbook_name}"
            log_llm_request(source, node_id, persona_id, persona_name, messages)
            log_llm_response(source, node_id, persona_id, persona_name, output_text)
        except Exception:
            LOGGER.debug("failed to dump LLM io", exc_info=True)

    def _debug_playbook(self, pb: PlaybookSchema, source: str) -> None:
        if not self._trace:
            return
        try:
            summary = {
                "source": source,
                "name": pb.name,
                "start": pb.start_node,
                "nodes": [
                    {
                        "id": n.id,
                        "type": getattr(n, "type", None),
                        "next": getattr(n, "next", None),
                        "action": getattr(n, "action", None),
                    }
                    for n in pb.nodes
                ],
            }
            LOGGER.debug("[sea] playbook loaded: %s", json.dumps(summary, ensure_ascii=False))
        except Exception:
            LOGGER.debug("[sea] playbook debug failed", exc_info=True)

    def _add_playbook_enum(self, schema: Dict[str, Any], available_playbooks_json: str) -> Dict[str, Any]:
        """Dynamically add enum constraint to playbook field in response_schema."""
        import json
        import copy

        try:
            # Parse available_playbooks JSON
            playbooks_list = json.loads(available_playbooks_json) if isinstance(available_playbooks_json, str) else available_playbooks_json
            if not isinstance(playbooks_list, list):
                return schema

            # Extract playbook names
            playbook_names = [pb.get("name") for pb in playbooks_list if isinstance(pb, dict) and "name" in pb]
            if not playbook_names:
                return schema

            # Deep copy schema to avoid modifying the original
            schema_copy = copy.deepcopy(schema)

            # Add enum to playbook field if it exists
            if "properties" in schema_copy and "playbook" in schema_copy["properties"]:
                schema_copy["properties"]["playbook"]["enum"] = playbook_names
                LOGGER.debug("[sea] Added dynamic enum to playbook field: %s", playbook_names)

            return schema_copy

        except Exception as exc:
            LOGGER.warning("[sea] Failed to add playbook enum: %s", exc)
            return schema

    def _process_structured_output(self, node_def: Any, text: str, state: Dict[str, Any]) -> bool:
        schema = getattr(node_def, "response_schema", None)
        if not schema:
            return False

        node_id = getattr(node_def, "id", "?")
        LOGGER.debug("[sea] _process_structured_output: node=%s, text type=%s",
                    node_id, type(text).__name__)

        # Check if text is already a dict (already parsed by LLM client with response_schema)
        if isinstance(text, dict):
            parsed = text
            LOGGER.debug("[sea] _process_structured_output: text is already a dict, keys=%s",
                        list(parsed.keys()) if isinstance(parsed, dict) else "(not a dict)")
        else:
            parsed = self._extract_structured_json(text)
            LOGGER.debug("[sea] _process_structured_output: extracted JSON, parsed=%s",
                        parsed is not None)

        if parsed is None:
            LOGGER.warning("[sea] structured output parse failed for node %s", node_id)
            return False

        key = getattr(node_def, "output_key", None) or getattr(node_def, "id", "") or "node"
        LOGGER.debug("[sea] _process_structured_output: storing to state['%s']", key)
        self._store_structured_result(state, key, parsed)

        # Apply output_mapping if defined
        output_mapping = getattr(node_def, "output_mapping", None)
        if output_mapping:
            LOGGER.debug("[sea] _process_structured_output: applying output_mapping: %s", output_mapping)
            self._apply_output_mapping(state, key, output_mapping)

        return True

    def _apply_output_mapping(self, state: Dict[str, Any], output_key: str, mapping: Dict[str, str]) -> None:
        """Apply output_mapping to copy structured output fields to state variables.

        Args:
            state: Current state dict
            output_key: The key where structured output was stored (e.g., "router")
            mapping: Dict mapping source paths to target state keys
                     e.g., {"router.playbook": "selected_playbook"}
        """
        # Get the structured output data
        output_data = state.get(output_key)
        if output_data is None:
            LOGGER.warning("[sea] output_mapping: output_key %s not found in state (available keys: %s)",
                          output_key, list(state.keys())[:20])
            return

        LOGGER.debug("[sea] output_mapping: output_data type=%s, keys=%s",
                    type(output_data).__name__, list(output_data.keys()) if isinstance(output_data, dict) else "(not a dict)")

        for source_path, target_key in mapping.items():
            # Source path can be either:
            # 1. Absolute path like "router.playbook" (starts with output_key)
            # 2. Relative path like "playbook" (within output_key namespace)
            if source_path.startswith(f"{output_key}."):
                # Already absolute path - need to parse it correctly
                # Remove output_key prefix from source_path
                # e.g., "structure.novel_title" -> "novel_title"
                relative_path = source_path[len(output_key) + 1:]
                value = self._resolve_nested_value(output_data, relative_path)
            else:
                # Relative path, resolve from output_data
                value = self._resolve_nested_value(output_data, source_path)

            if value is not None:
                state[target_key] = value
                LOGGER.debug("[sea] output_mapping: %s -> %s = %s", source_path, target_key, str(value))
            else:
                LOGGER.warning("[sea] output_mapping: failed to resolve %s from %s (keys: %s)",
                             source_path, output_key,
                             list(output_data.keys()) if isinstance(output_data, dict) else "(not a dict)")


    def _resolve_nested_value(self, data: Any, path: str) -> Any:
        """Resolve a nested key from data using dot notation.

        Args:
            data: The data structure to traverse (dict or list)
            path: Dot-notated path (e.g., "novel_title" or "items.0.name")

        Returns:
            The value at the path, or None if not found
        """
        if path == "":
            return data
        keys = path.split(".")
        current = data
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            elif isinstance(current, list) and key.isdigit():
                idx = int(key)
                if idx < len(current):
                    current = current[idx]
                else:
                    return None
            else:
                return None
            if current is None:
                return None
        return current

    def _store_structured_result(self, state: Dict[str, Any], key: str, data: Any) -> None:
        state[key] = data
        flat = self._flatten_dict(data)
        for path, value in flat.items():
            state[f"{key}.{path}"] = value

    def _flatten_dict(self, value: Any, prefix: str = "") -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if isinstance(value, dict):
            for k, v in value.items():
                new_prefix = f"{prefix}.{k}" if prefix else str(k)
                result.update(self._flatten_dict(v, new_prefix))
        elif isinstance(value, list):
            # Also store the array itself as JSON string for direct template access
            if prefix:
                result[prefix] = json.dumps(value, ensure_ascii=False)
            # Store individual elements with .N format
            for idx, item in enumerate(value):
                new_prefix = f"{prefix}.{idx}" if prefix else str(idx)
                result.update(self._flatten_dict(item, new_prefix))
        else:
            result[prefix or "value"] = value
        return result

    def _resolve_state_value(self, state: Dict[str, Any], key: str) -> Any:
        """Resolve a nested key from state using dot notation.

        Supports:
        - Simple keys: "foo" -> state["foo"]
        - Nested dict keys: "foo.bar" -> state["foo"]["bar"]
        - Array indexing: "foo.0" or "foo.items.0" -> state["foo"][0] or state["foo"]["items"][0]

        Tries nested resolution first to preserve actual types (arrays, dicts),
        then falls back to direct/flattened lookup.
        """
        # For simple keys without dots, do direct lookup
        if "." not in key:
            return state.get(key, "")

        # Try nested resolution first (preserves actual types like arrays)
        parts = key.split(".")
        value = state
        for part in parts:
            if value is None:
                break
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list):
                # Support array indexing: "0", "1", etc.
                if part.isdigit():
                    idx = int(part)
                    value = value[idx] if idx < len(value) else None
                else:
                    value = None
                    break
            else:
                value = None
                break

        if value is not None:
            return value

        # Fall back to direct lookup (for flattened keys)
        if key in state:
            return state[key]

        return ""


    def _extract_structured_json(self, text: str) -> Optional[Dict[str, Any]]:
        candidate = text.strip()
        if not candidate:
            return None
        if candidate.startswith("```"):
            parts = candidate.split("```")
            for seg in parts:
                seg = seg.strip()
                if seg.startswith("{") and seg.endswith("}"):
                    candidate = seg
                    break
        if not candidate.startswith("{"):
            match = re.search(r"\{.*\}", candidate, re.DOTALL)
            if match:
                candidate = match.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            return None

    def _update_router_selection(self, state: Dict[str, Any], text: str, parsed: Optional[Dict[str, Any]] = None) -> None:
        selection = parsed or {}
        playbook_value = selection.get("playbook") if isinstance(selection, dict) else None
        if not playbook_value:
            playbook_value = selection.get("playbook_name") if isinstance(selection, dict) else None

        # Parse available playbooks to validate selection
        available_names: List[str] = []
        try:
            avail_raw = state.get("available_playbooks")
            if isinstance(avail_raw, str):
                avail_list = json.loads(avail_raw)
            else:
                avail_list = avail_raw
            if isinstance(avail_list, list):
                for pb in avail_list:
                    if isinstance(pb, dict) and pb.get("name"):
                        available_names.append(pb.get("name"))
        except Exception:
            LOGGER.warning("Failed to parse available_playbooks from state", exc_info=True)

        if not playbook_value:
            stripped = str(text).strip()
            playbook_value = stripped.split()[0] if stripped else "basic_chat"

        # Fallback to basic_chat when selection is not in available list
        if available_names and playbook_value not in available_names:
            playbook_value = "basic_chat"

        state["selected_playbook"] = playbook_value or "basic_chat"
        args_obj = selection.get("args") if isinstance(selection, dict) else None
        if isinstance(args_obj, dict):
            state["selected_args"] = args_obj
        else:
            state["selected_args"] = {"input": state.get("input")}

    def _lg_tool_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, auto_mode: bool = False):
        from tools import TOOL_REGISTRY
        from tools.context import persona_context

        tool_name = node_def.action
        args_input = getattr(node_def, "args_input", None)
        output_key = getattr(node_def, "output_key", None)
        output_keys = getattr(node_def, "output_keys", None)

        async def node(state: dict):
            # Check for cancellation at start of node
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            
            # Send status event for node execution
            node_id = getattr(node_def, "id", "tool")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            tool_func = TOOL_REGISTRY.get(tool_name)
            persona_obj = state.get("persona_obj") or persona
            persona_id = getattr(persona_obj, "persona_id", "unknown")
            
            try:
                persona_dir = getattr(persona_obj, "persona_log_path", None)
                persona_dir = persona_dir.parent if persona_dir else Path.cwd()
                manager_ref = getattr(persona_obj, "manager_ref", None)

                # Build kwargs from args_input (None or {} = no args)
                # Supports nested keys via dot notation (e.g., "tool_call.args.playbook_name")
                kwargs = {}
                if args_input:
                    for arg_name, source in args_input.items():
                        if isinstance(source, str):
                            value = self._resolve_state_value(state, source)
                            LOGGER.debug("[sea][tool] Mapping arg '%s' <- state['%s'] = %s", arg_name, source, value)
                        else:
                            value = source
                            LOGGER.debug("[sea][tool] Using literal arg '%s' = %s", arg_name, value)
                        kwargs[arg_name] = value

                # ===== Tool execution logging (centralized) =====
                LOGGER.info("[sea][tool] CALL %s (persona=%s) args=%s", tool_name, persona_id, kwargs)
                
                if tool_func is None:
                    LOGGER.error("[sea][tool] CRITICAL: Tool function '%s' not found in registry! TOOL_REGISTRY keys: %s", tool_name, list(TOOL_REGISTRY.keys()))
                else:
                    LOGGER.info("[sea][tool] Tool function found: %s", tool_func)

                # Execute tool with persona context
                if persona_id and persona_dir:
                    with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook.name, auto_mode=auto_mode):
                        result = tool_func(**kwargs) if callable(tool_func) else None
                else:
                    result = tool_func(**kwargs) if callable(tool_func) else None

                # Log tool result
                result_str = str(result)
                result_preview = result_str[:200] + "..." if len(result_str) > 200 else result_str
                LOGGER.info("[sea][tool] RESULT %s -> %s", tool_name, result_preview)
                log_sea_trace(playbook.name, node_id, "TOOL", f"action={tool_name} → {result_str}")

                # Activity trace: record tool execution (skip infrastructure playbooks)
                if not playbook.name.startswith(("meta_", "sub_")):
                    pb_display = playbook.display_name or playbook.name
                    _at = state.get("_activity_trace")
                    if isinstance(_at, list):
                        _at.append({"action": "tool", "name": tool_name, "playbook": pb_display})
                    if event_callback:
                        event_callback({
                            "type": "activity", "action": "tool", "name": tool_name,
                            "playbook": pb_display, "status": "completed",
                            "persona_id": getattr(persona, "persona_id", None),
                            "persona_name": getattr(persona, "persona_name", None),
                        })

                # Handle tuple results with output_keys (for multi-value returns)
                if output_keys and isinstance(result, tuple):
                    # Expand tuple to multiple state variables
                    for i, key in enumerate(output_keys):
                        if i < len(result):
                            state[key] = result[i]
                            LOGGER.debug("[sea][LangGraph] Stored tuple[%d] in state[%s]: %s", i, key, str(result[i]))
                    # Set last to first element (primary result)
                    state["last"] = str(result[0]) if result else ""
                elif isinstance(result, tuple):
                    # Legacy: extract first element
                    state["last"] = str(result[0]) if result else ""
                else:
                    state["last"] = str(result)

                # Store result in state if output_key is specified (legacy single-value)
                if output_key and not output_keys:
                    state[output_key] = result
            except Exception as exc:
                state["last"] = f"Tool error: {exc}"
                LOGGER.exception("SEA LangGraph tool %s failed", tool_name)
            return state

        return node

    def _lg_tool_call_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, auto_mode: bool = False):
        """Execute a tool dynamically based on an LLM node's tool call decision.

        Reads tool name and arguments from state (stored by an LLM node with
        available_tools), looks up the tool in TOOL_REGISTRY, and executes it.
        This enables agentic loops without per-tool branching.
        """
        from tools import TOOL_REGISTRY
        from tools.context import persona_context

        call_source = getattr(node_def, "call_source", "fc") or "fc"
        output_key = getattr(node_def, "output_key", None)

        async def node(state: dict):
            # Check for cancellation
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()

            node_id = getattr(node_def, "id", "tool_call")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})

            # Resolve tool name and args from state
            tool_name = self._resolve_state_value(state, f"{call_source}.name")
            tool_args = self._resolve_state_value(state, f"{call_source}.args")

            # Fallback to legacy state keys
            if not tool_name:
                tool_name = state.get("tool_name", "")
                tool_args = state.get("tool_args", {})

            if not tool_name:
                error_msg = f"[sea][tool_call] No tool name found in state (call_source={call_source})"
                LOGGER.error(error_msg)
                state["last"] = error_msg
                if output_key:
                    state[output_key] = error_msg
                return state

            if not isinstance(tool_args, dict):
                LOGGER.warning("[sea][tool_call] tool_args is not a dict (%s), using empty args", type(tool_args).__name__)
                tool_args = {}

            tool_func = TOOL_REGISTRY.get(tool_name)
            if tool_func is None:
                error_msg = f"[sea][tool_call] Tool '{tool_name}' not found in registry"
                LOGGER.error(error_msg)
                state["last"] = error_msg
                if output_key:
                    state[output_key] = error_msg
                return state

            persona_obj = state.get("persona_obj") or persona
            persona_id = getattr(persona_obj, "persona_id", "unknown")

            try:
                persona_dir = getattr(persona_obj, "persona_log_path", None)
                persona_dir = persona_dir.parent if persona_dir else Path.cwd()
                manager_ref = getattr(persona_obj, "manager_ref", None)

                LOGGER.info("[sea][tool_call] CALL %s (persona=%s) args=%s", tool_name, persona_id, tool_args)

                if persona_id and persona_dir:
                    with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook.name, auto_mode=auto_mode):
                        result = tool_func(**tool_args)
                else:
                    result = tool_func(**tool_args)

                result_str = str(result)
                result_preview = result_str[:500] + "..." if len(result_str) > 500 else result_str
                LOGGER.info("[sea][tool_call] RESULT %s -> %s", tool_name, result_preview)
                log_sea_trace(playbook.name, node_id, "TOOL_CALL", f"action={tool_name} args={tool_args} → {result_str}")

                # Activity trace
                if not playbook.name.startswith(("meta_", "sub_")):
                    pb_display = playbook.display_name or playbook.name
                    _at = state.get("_activity_trace")
                    if isinstance(_at, list):
                        _at.append({"action": "tool_call", "name": tool_name, "playbook": pb_display})
                    if event_callback:
                        event_callback({
                            "type": "activity", "action": "tool_call", "name": tool_name,
                            "playbook": pb_display, "status": "completed",
                            "persona_id": getattr(persona, "persona_id", None),
                            "persona_name": getattr(persona, "persona_name", None),
                        })

                state["last"] = result_str
                if output_key:
                    state[output_key] = result

                # Save tool call ID before _append_tool_result_message clears it
                _tc_id_for_im = state.get("_last_tool_call_id")

                # Append tool result to conversation messages (function calling protocol)
                self._append_tool_result_message(state, tool_name, result_str)

                # Also update _intermediate_msgs for context_profile-based nodes
                if "_intermediate_msgs" in state and _tc_id_for_im:
                    _im = list(state.get("_intermediate_msgs", []))
                    _im.append({
                        "role": "tool",
                        "tool_call_id": _tc_id_for_im,
                        "name": tool_name,
                        "content": result_str,
                    })
                    state["_intermediate_msgs"] = _im

            except Exception as exc:
                error_msg = f"Tool error ({tool_name}): {exc}"
                state["last"] = error_msg
                if output_key:
                    state[output_key] = error_msg
                LOGGER.exception("[sea][tool_call] %s failed", tool_name)

                # Save tool call ID before _append_tool_result_message clears it
                _tc_id_for_err = state.get("_last_tool_call_id")

                # Append error as tool result so LLM knows the tool failed
                self._append_tool_result_message(state, tool_name, error_msg)

                # Also update _intermediate_msgs
                if "_intermediate_msgs" in state and _tc_id_for_err:
                    _im = list(state.get("_intermediate_msgs", []))
                    _im.append({
                        "role": "tool",
                        "tool_call_id": _tc_id_for_err,
                        "name": tool_name,
                        "content": error_msg,
                    })
                    state["_intermediate_msgs"] = _im

            return state

        return node

    # ── Playbook permission helpers ──────────────────────────────────
    def _get_playbook_permission(self, city_id: int, playbook_name: str) -> str:
        """Return the permission level for *playbook_name* in *city_id*.

        When no row exists the default is ``"ask_every_time"``.
        """
        from database.models import PlaybookPermission
        try:
            db = self.manager.SessionLocal()
            try:
                row = (
                    db.query(PlaybookPermission)
                    .filter(
                        PlaybookPermission.CITYID == city_id,
                        PlaybookPermission.playbook_name == playbook_name,
                    )
                    .first()
                )
                return row.permission_level if row else "ask_every_time"
            finally:
                db.close()
        except Exception:
            LOGGER.warning("[sea][perm] Failed to query permission for %s", playbook_name, exc_info=True)
            return "ask_every_time"

    def _set_playbook_permission(self, city_id: int, playbook_name: str, level: str) -> None:
        """Insert or update the permission level for *playbook_name* in *city_id*."""
        from database.models import PlaybookPermission
        try:
            db = self.manager.SessionLocal()
            try:
                row = (
                    db.query(PlaybookPermission)
                    .filter(
                        PlaybookPermission.CITYID == city_id,
                        PlaybookPermission.playbook_name == playbook_name,
                    )
                    .first()
                )
                if row:
                    row.permission_level = level
                else:
                    db.add(PlaybookPermission(
                        CITYID=city_id,
                        playbook_name=playbook_name,
                        permission_level=level,
                    ))
                db.commit()
                LOGGER.info("[sea][perm] Set %s → %s (city=%s)", playbook_name, level, city_id)
            finally:
                db.close()
        except Exception:
            LOGGER.warning("[sea][perm] Failed to set permission for %s", playbook_name, exc_info=True)

    def _request_playbook_permission(
        self,
        playbook_name: str,
        persona: Any,
        event_callback: Optional[Callable[[Dict[str, Any]], None]],
    ) -> str:
        """Send a ``permission_request`` event and block until the user responds.

        Returns one of: ``"allow"``, ``"deny"``, ``"always_allow"``,
        ``"never_use"``, or ``"timeout"``.
        """
        import threading as _threading

        if not event_callback:
            LOGGER.warning("[sea][perm] No event_callback — auto-denying %s", playbook_name)
            return "deny"

        request_id = str(uuid.uuid4())
        event = _threading.Event()
        self.manager._pending_permission_requests[request_id] = event

        # Look up display name / description for the dialog
        display_name = playbook_name
        description = ""
        try:
            db = self.manager.SessionLocal()
            try:
                pb = db.query(PlaybookModel).filter(PlaybookModel.name == playbook_name).first()
                if pb:
                    display_name = pb.display_name or pb.name
                    description = pb.description or ""
            finally:
                db.close()
        except Exception:
            LOGGER.warning("[sea][perm] Failed to look up playbook info for %s", playbook_name, exc_info=True)

        persona_id = getattr(persona, "persona_id", None)
        persona_name = getattr(persona, "persona_name", None)

        event_callback({
            "type": "permission_request",
            "request_id": request_id,
            "playbook_name": playbook_name,
            "playbook_display_name": display_name,
            "playbook_description": description,
            "persona_id": persona_id,
            "persona_name": persona_name,
        })
        LOGGER.info("[sea][perm] Sent permission_request for %s (id=%s)", playbook_name, request_id)

        # Block until the user responds or timeout
        timeout_sec = 60
        responded = event.wait(timeout=timeout_sec)

        # Cleanup
        self.manager._pending_permission_requests.pop(request_id, None)
        response = self.manager._permission_responses.pop(request_id, None)

        if not responded or response is None:
            LOGGER.info("[sea][perm] Permission request %s timed out after %ds", request_id, timeout_sec)
            if event_callback:
                event_callback({
                    "type": "warning",
                    "content": f"Playbook実行の許可リクエストがタイムアウトしました（{display_name}）。スキップします。",
                    "warning_code": "permission_timeout",
                    "display": "toast",
                })
            return "timeout"

        LOGGER.info("[sea][perm] Permission response for %s: %s", playbook_name, response)
        return response

    def _notify_persona_permission_result(
        self,
        state: Dict[str, Any],
        persona: Any,
        playbook_name: str,
        message: str,
        event_callback: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        """Inform the persona about a permission denial/timeout.

        Records the result in state, message history, and SAIMemory so the
        persona can adjust its response accordingly.
        """
        state["last"] = message
        self._append_tool_result_message(state, playbook_name, message)
        if not self._store_memory(
            persona, message,
            role="system",
            tags=["permission", "denied", playbook_name],
            pulse_id=state.get("pulse_id"),
        ):
            LOGGER.warning("[sea][perm] Failed to store permission result to SAIMemory")

    def _lg_exec_node(
        self,
        node_def: Any,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        auto_mode: bool,
        outputs: Optional[List[str]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        # Get source variable names from node definition (with defaults for backward compatibility)
        playbook_source = getattr(node_def, "playbook_source", "selected_playbook") or "selected_playbook"
        args_source = getattr(node_def, "args_source", "selected_args") or "selected_args"

        async def node(state: dict):
            # Check for cancellation at start of node
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            
            # Send status event for node execution
            node_id = getattr(node_def, "id", "exec")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            sub_name = state.get(playbook_source) or state.get("last") or "basic_chat"
            sub_pb = self._load_playbook_for(str(sub_name).strip(), persona, building_id) or self._basic_chat_playbook()
            sub_input = None
            args = state.get(args_source) or {}
            if isinstance(args, dict):
                sub_input = args.get("input") or args.get("query")
            if not sub_input:
                sub_input = state.get("inputs", {}).get("input")

            eff_bid = self._effective_building_id(persona, building_id)

            # ── Playbook permission check ──
            clean_name = str(sub_name).strip()
            if clean_name != "basic_chat":
                city_id = getattr(self.manager, "city_id", None)
                if city_id is not None:
                    perm = self._get_playbook_permission(city_id, clean_name)
                    log_sea_trace(playbook.name, node_id, "PERM", f"{clean_name} → {perm}")

                    if perm == "blocked":
                        denial_msg = f"Playbook '{clean_name}' is not available (permission: {perm})"
                        self._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                        return state

                    if perm == "ask_every_time":
                        if auto_mode:
                            denial_msg = f"Playbook '{clean_name}' requires user permission but running in auto mode. Skipped."
                            self._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                            return state

                        response = self._request_playbook_permission(clean_name, persona, event_callback)

                        if response in ("deny", "timeout"):
                            denial_msg = (
                                f"User denied execution of playbook '{clean_name}'. Please respond without using this tool."
                                if response == "deny"
                                else f"Permission request for playbook '{clean_name}' timed out. Please respond without using this tool."
                            )
                            self._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                            return state

                        if response == "always_allow":
                            self._set_playbook_permission(city_id, clean_name, "auto_allow")

                        if response == "never_use":
                            denial_msg = f"User disabled playbook '{clean_name}'. This playbook will not be available in future. Please respond without using this tool."
                            self._set_playbook_permission(city_id, clean_name, "user_only")
                            self._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                            return state

                    # perm == "auto_allow" or allowed via dialog → continue

            # Determine execution mode
            execution = getattr(node_def, "execution", "inline") or "inline"
            subagent_thread_id = None
            subagent_parent_id = None

            if execution == "subagent":
                label = f"Subagent: {sub_name}"
                subagent_thread_id, subagent_parent_id = self._start_subagent_thread(persona, label=label)
                if not subagent_thread_id:
                    LOGGER.warning("[sea][exec] Failed to start subagent thread for '%s', falling back to inline", sub_name)
                    execution = "inline"  # Fallback
                else:
                    log_sea_trace(playbook.name, node_id, "EXEC", f"→ {sub_name} [subagent thread={subagent_thread_id}] (input=\"{str(sub_input)}\")")

            if execution == "inline":
                log_sea_trace(playbook.name, node_id, "EXEC", f"→ {sub_name} (input=\"{str(sub_input)}\")")

            try:
                sub_outputs = await asyncio.to_thread(
                    self._run_playbook, sub_pb, persona, eff_bid, sub_input, auto_mode, True, state, event_callback
                )
            except Exception as exc:
                LOGGER.exception("SEA LangGraph exec sub-playbook failed")
                # End subagent thread on error (no chronicle)
                if execution == "subagent" and subagent_thread_id:
                    self._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=False)
                error_msg = f"Sub-playbook error: {type(exc).__name__}: {exc}"
                state["last"] = error_msg
                state["_exec_error"] = True
                state["_exec_error_detail"] = error_msg
                log_sea_trace(playbook.name, node_id, "EXEC", f"→ {error_msg}")
                if event_callback:
                    event_callback({
                        "type": "error",
                        "content": f"[{sub_name}] {type(exc).__name__}: {exc}",
                        "playbook": playbook.name,
                        "node": node_id,
                    })
                # Record error to SAIMemory so the persona (and subsequent LLM calls) can see it
                if not self._store_memory(
                    persona, error_msg,
                    role="system",
                    tags=["error", "exec", str(sub_name).strip()],
                    pulse_id=state.get("pulse_id"),
                ):
                    LOGGER.warning("Failed to store exec error to SAIMemory for node %s", node_id)
                    if event_callback:
                        event_callback({
                            "type": "warning",
                            "content": "記憶の保存に失敗しました。会話内容が記録されていない可能性があります。",
                            "warning_code": "memorize_failed",
                            "display": "toast",
                        })
                if outputs is not None:
                    outputs.append(error_msg)
                return state

            # End subagent thread on success
            if execution == "subagent" and subagent_thread_id:
                gen_chronicle = getattr(node_def, "subagent_chronicle", True)
                chronicle = self._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=gen_chronicle)
                state["_subagent_chronicle"] = chronicle or ""
                log_sea_trace(playbook.name, node_id, "EXEC", f"← {sub_name} [subagent ended, chronicle={'yes' if chronicle else 'no'}]")

            # Success path: clear error flag
            state["_exec_error"] = False
            state.pop("_exec_error_detail", None)

            # Track executed playbook in executed_playbooks list
            executed_list = state.get("executed_playbooks")
            if isinstance(executed_list, list):
                executed_list.append(str(sub_name).strip())
                LOGGER.debug("[sea][exec] Added '%s' to executed_playbooks: %s", sub_name, executed_list)

            # Append tool result message to close the router function call pair
            joined = ""
            if sub_outputs:
                joined = "\n".join(str(item).strip() for item in sub_outputs if str(item).strip())
            self._append_tool_result_message(state, str(sub_name).strip(), joined or "(completed)")
            if sub_outputs:
                state["last"] = sub_outputs[-1]
            return state

        return node


    def _lg_memorize_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        async def node(state: dict):
            # Send status event for node execution
            node_id = getattr(node_def, "id", "memorize")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            # Include all state variables for template expansion (e.g., structured output like document_data.*)
            variables = dict(state)
            # Flatten nested dicts/lists for dot notation access (e.g., finalize_output.content)
            for key, value in list(state.items()):
                if isinstance(value, dict):
                    flat = self._flatten_dict(value)
                    for path, val in flat.items():
                        variables[f"{key}.{path}"] = val
            variables.update({
                "input": state.get("inputs", {}).get("input", ""),
                "last": state.get("last", ""),
                "persona_id": getattr(persona, "persona_id", None),
                "persona_name": getattr(persona, "persona_name", None),
            })
            action_template = getattr(node_def, "action", None) or "{last}"
            LOGGER.debug("[memorize] action_template=%s", action_template)
            LOGGER.debug("[memorize] available variables containing 'finalize': %s", 
                        {k: v for k, v in variables.items() if 'finalize' in str(k).lower()})
            memo_text = _format(action_template, variables)
            LOGGER.debug("[memorize] memo_text=%s", memo_text)
            role = getattr(node_def, "role", "assistant") or "assistant"
            tags = getattr(node_def, "tags", None)
            pulse_id = state.get("pulse_id")
            metadata_key = getattr(node_def, "metadata_key", None)
            metadata = state.get(metadata_key) if metadata_key else None
            if not self._store_memory(persona, memo_text, role=role, tags=tags, pulse_id=pulse_id, metadata=metadata):
                LOGGER.warning("Failed to store memory in MEMORIZE node %s", node_id)
                if event_callback:
                    event_callback({
                        "type": "warning",
                        "content": "記憶の保存に失敗しました。会話内容が記録されていない可能性があります。",
                        "warning_code": "memorize_failed",
                        "display": "toast",
                    })
            log_sea_trace(playbook.name, node_id, "MEMORIZE", f"role={role} tags={tags} text=\"{memo_text}\"")
            state["last"] = memo_text
            if outputs is not None:
                outputs.append(memo_text)

            # Activity trace: record memorize execution
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

            # Debug: log speak_content at end of memorize node
            speak_content = state.get("speak_content", "")
            LOGGER.info("[DEBUG] memorize node end: state['speak_content'] = '%s'", speak_content)
            
            return state

        return node

    def _lg_speak_node(self, state: dict, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        # Send status event for node execution
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / speak", "playbook": playbook.name, "node": "speak"})
        text = state.get("last") or ""
        reasoning_text = state.pop("_reasoning_text", "")
        reasoning_details_val = state.pop("_reasoning_details", None)
        activity_trace = state.get("_activity_trace")
        pulse_id = state.get("pulse_id")
        eff_bid = self._effective_building_id(persona, building_id)
        # Build extra metadata with reasoning for SAIMemory storage
        speak_metadata: Dict[str, Any] = {}
        if reasoning_text:
            speak_metadata["reasoning"] = reasoning_text
        if reasoning_details_val is not None:
            speak_metadata["reasoning_details"] = reasoning_details_val
        self._emit_speak(persona, eff_bid, text, pulse_id=pulse_id, extra_metadata=speak_metadata if speak_metadata else None)
        if outputs is not None:
            outputs.append(text)
        if event_callback:
            say_event: Dict[str, Any] = {"type": "say", "content": text, "persona_id": getattr(persona, "persona_id", None)}
            if reasoning_text:
                say_event["reasoning"] = reasoning_text
            if reasoning_details_val is not None:
                say_event["reasoning_details"] = reasoning_details_val
            if activity_trace:
                say_event["activity_trace"] = list(activity_trace)
            event_callback(say_event)
        return state

    def _lg_say_node(self, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        async def node(state: dict):
            # Send status event for node execution
            node_id = getattr(node_def, "id", "say")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            text = state.get("last") or ""
            reasoning_text = state.pop("_reasoning_text", "")
            reasoning_details_val = state.pop("_reasoning_details", None)
            pulse_id = state.get("pulse_id")
            metadata_key = getattr(node_def, "metadata_key", None)
            base_metadata = state.get(metadata_key) if metadata_key else None

            # Build metadata with usage total from accumulator
            msg_metadata: Dict[str, Any] = {}
            if base_metadata:
                if isinstance(base_metadata, dict):
                    msg_metadata.update(base_metadata)
                else:
                    msg_metadata["metadata"] = base_metadata
            if reasoning_text:
                msg_metadata["reasoning"] = reasoning_text
            if reasoning_details_val is not None:
                msg_metadata["reasoning_details"] = reasoning_details_val

            # Include pulse usage accumulator total for UI display
            accumulator = state.get("pulse_usage_accumulator")
            if accumulator and accumulator.get("call_count", 0) > 0:
                msg_metadata["llm_usage_total"] = dict(accumulator)

            # Include activity trace for UI display
            activity_trace = state.get("_activity_trace")
            if activity_trace:
                msg_metadata["activity_trace"] = list(activity_trace)

            eff_bid = self._effective_building_id(persona, building_id)
            self._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
            if outputs is not None:
                outputs.append(text)
            if event_callback:
                say_event: Dict[str, Any] = {"type": "say", "content": text, "persona_id": getattr(persona, "persona_id", None), "metadata": msg_metadata if msg_metadata else None}
                if reasoning_text:
                    say_event["reasoning"] = reasoning_text
                if activity_trace:
                    say_event["activity_trace"] = list(activity_trace)
                event_callback(say_event)

            # Debug: log speak_content at end of say node
            speak_content = state.get("speak_content", "")
            LOGGER.info("[DEBUG] say node end: state['speak_content'] = '%s'", speak_content)
            
            return state
        return node

    def _lg_think_node(self, state: dict, persona: Any, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        # Send status event for node execution
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / think", "playbook": playbook.name, "node": "think"})
        text = state.get("last") or ""
        pulse_id = state.get("pulse_id") or str(uuid.uuid4())
        self._emit_think(persona, pulse_id, text)
        if outputs is not None:
            outputs.append(text)
        if event_callback:
            event_callback({"type": "think", "content": text, "persona_id": getattr(persona, "persona_id", None)})
        return state

    def _lg_subplay_node(self, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, auto_mode: bool, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        async def node(state: dict):
            # Check for cancellation at start of node
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            
            # Send status event for node execution
            node_id = getattr(node_def, "id", "subplay")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            # Get subplaybook name
            sub_name = getattr(node_def, "playbook", None) or getattr(node_def, "action", None)
            if not sub_name:
                msg = "(sub-playbook missing name)"
                state["last"] = msg
                return state

            # Load subplaybook
            sub_pb = self._load_playbook_for(sub_name, persona, building_id)
            if not sub_pb:
                msg = f"Sub-playbook {sub_name} not found"
                state["last"] = msg
                return state

            # Format input template with state variables
            template = getattr(node_def, "input_template", "{input}") or "{input}"
            variables = dict(state)
            variables.update({
                "input": state.get("inputs", {}).get("input", ""),
                "last": state.get("last", ""),
            })
            sub_input = _format(template, variables)
            eff_bid = self._effective_building_id(persona, building_id)

            # Determine execution mode
            execution = getattr(node_def, "execution", "inline") or "inline"
            subagent_thread_id = None
            subagent_parent_id = None

            if execution == "subagent":
                label = f"Subagent: {sub_name}"
                subagent_thread_id, subagent_parent_id = self._start_subagent_thread(persona, label=label)
                if not subagent_thread_id:
                    LOGGER.warning("[sea][subplay] Failed to start subagent thread for '%s', falling back to inline", sub_name)
                    execution = "inline"  # Fallback
                else:
                    log_sea_trace(playbook.name, node_id, "SUBPLAY", f"→ {sub_name} [subagent thread={subagent_thread_id}] (input=\"{str(sub_input)}\")")

            if execution == "inline":
                log_sea_trace(playbook.name, node_id, "SUBPLAY", f"→ {sub_name} (input=\"{str(sub_input)}\")")

            # Execute subplaybook
            # Note: We call _run_playbook directly (not via asyncio.to_thread) to keep
            # SQLite connections on the same thread. _run_playbook handles its own
            # async/sync boundary internally via ThreadPoolExecutor.
            try:
                sub_outputs = self._run_playbook(sub_pb, persona, eff_bid, sub_input, auto_mode, True, state, event_callback)
            except LLMError:
                LOGGER.exception("[sea][subplay] LLM error in subplaybook '%s'", sub_name)
                if execution == "subagent" and subagent_thread_id:
                    self._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=False)
                raise
            except Exception as exc:
                LOGGER.exception("[sea][subplay] Failed to execute subplaybook '%s'", sub_name)
                # End subagent thread on error (no chronicle)
                if execution == "subagent" and subagent_thread_id:
                    self._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=False)
                state["last"] = f"Sub-playbook error: {exc}"
                return state

            # End subagent thread on success
            if execution == "subagent" and subagent_thread_id:
                gen_chronicle = getattr(node_def, "subagent_chronicle", True)
                chronicle = self._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=gen_chronicle)
                state["_subagent_chronicle"] = chronicle or ""
                log_sea_trace(playbook.name, node_id, "SUBPLAY", f"← {sub_name} [subagent ended, chronicle={'yes' if chronicle else 'no'}]")

            last_text = sub_outputs[-1] if sub_outputs else ""
            state["last"] = last_text

            # Propagate outputs if requested
            if getattr(node_def, "propagate_output", False) and sub_outputs and outputs is not None:
                outputs.extend(sub_outputs)

            # Note: State variables are propagated via output_schema in _compile_with_langgraph
            # No special handling needed here anymore

            return state
        return node

    def _lg_set_node(self, node_def: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        """Create a node that sets/modifies state variables."""
        assignments = getattr(node_def, "assignments", {}) or {}

        async def node(state: dict):
            # Send status event for node execution
            node_id = getattr(node_def, "id", "set")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            trace_parts = []
            for key, value_template in assignments.items():
                resolved_value = self._resolve_set_value(value_template, state)
                state[key] = resolved_value
                LOGGER.debug("[sea][set] %s = %s", key, resolved_value)
                trace_parts.append(f"{key}={str(resolved_value)[:80]}")
            log_sea_trace(playbook.name, node_id, "SET", ", ".join(trace_parts))

            # Special handling: if executed_playbooks_init is set, initialize executed_playbooks as empty list
            if state.get("executed_playbooks_init") and "executed_playbooks" not in state:
                state["executed_playbooks"] = []
                LOGGER.debug("[sea][set] Initialized executed_playbooks = []")

            return state
        return node

    def _resolve_set_value(self, value_template: Any, state: Dict[str, Any]) -> Any:
        """Resolve a value template for SET node assignments.

        Handles:
        - Literal values (int, float, bool, None): returned as-is
        - Arithmetic expressions with "=" prefix: "={count} + 1" evaluated safely
        - Template strings with {var} placeholders: expanded with state values
        """
        # Literal values
        if isinstance(value_template, (int, float, bool, type(None))):
            return value_template

        if not isinstance(value_template, str):
            return value_template

        # Explicit arithmetic expression with "=" prefix
        # e.g., "={loop_count} + 1" or "={a} * {b}"
        if value_template.startswith("="):
            return self._eval_arithmetic_expression(value_template[1:], state)

        # Simple template expansion
        try:
            result = _format(value_template, state)
            if result == value_template and "{" in value_template:
                # Template was not expanded - log for debugging
                LOGGER.debug("[sea][set] Template not expanded. Keys in state: %s", list(state.keys()))
            return result
        except Exception as exc:
            LOGGER.warning("[sea][set] _format failed: %s", exc)
            return value_template

    def _eval_arithmetic_expression(self, expr: str, state: Dict[str, Any]) -> Any:
        """Safely evaluate arithmetic expressions with state variable substitution.

        Examples:
        - "{count} + 1" -> state['count'] + 1
        - "{a} * {b}" -> state['a'] * state['b']
        """
        import ast

        # Expand {var} placeholders with state values
        expanded = expr
        placeholder_pattern = re.compile(r"\{(\w+)\}")
        for match in placeholder_pattern.finditer(expr):
            var_name = match.group(1)
            var_value = state.get(var_name, 0)
            # Convert to number if possible
            try:
                if isinstance(var_value, str):
                    var_value = float(var_value) if "." in var_value else int(var_value)
            except (ValueError, TypeError):
                var_value = 0
            expanded = expanded.replace(match.group(0), str(var_value))

        # Safely evaluate the arithmetic expression
        try:
            # Parse and validate the expression
            tree = ast.parse(expanded, mode='eval')

            # Only allow safe operations
            allowed_node_types = (
                ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Num,
                # Operators (these appear as children of BinOp/UnaryOp)
                ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
                ast.UAdd, ast.USub,
            )
            for node in ast.walk(tree):
                if not isinstance(node, allowed_node_types):
                    raise ValueError(f"Unsupported node type: {type(node).__name__}")

            result = eval(compile(tree, '<string>', 'eval'))
            # Return int if result is a whole number
            if isinstance(result, float) and result.is_integer():
                return int(result)
            return result
        except Exception as exc:
            LOGGER.warning("[sea][set] Failed to evaluate expression '%s': %s", expr, exc)
            return 0

    # ---------------- Subagent Thread Helpers -----------------

    def _start_subagent_thread(self, persona, label: Optional[str] = None):
        """Create a temporary Stelis thread and switch the active thread to it.

        Used by subplay/exec nodes with execution='subagent' to isolate
        sub-playbook execution in a temporary thread.

        Returns:
            (thread_id, parent_thread_id) on success, (None, None) on failure.
        """
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            LOGGER.warning("[subagent] No memory adapter found for persona %s", persona.persona_id)
            return None, None

        # Check depth limit (subagent uses max_depth=2 to prevent deep nesting)
        if not memory_adapter.can_start_stelis(max_depth=2):
            LOGGER.warning("[subagent] Stelis max depth exceeded for persona %s", persona.persona_id)
            return None, None

        # Get current thread as parent
        parent_thread_id = memory_adapter.get_current_thread()
        if parent_thread_id is None:
            parent_thread_id = memory_adapter._thread_id(None)

        # Create a new Stelis thread (no anchor message — subagent is transparent)
        stelis = memory_adapter.start_stelis_thread(
            parent_thread_id=parent_thread_id,
            window_ratio=0.8,
            max_depth=2,
            label=label or "Subagent",
        )

        if not stelis:
            LOGGER.error("[subagent] Failed to create subagent thread for persona %s", persona.persona_id)
            return None, None

        # Switch to the new thread
        memory_adapter.set_active_thread(stelis.thread_id)
        LOGGER.info(
            "[subagent] Started subagent thread %s (parent=%s, label=%s)",
            stelis.thread_id, parent_thread_id, label,
        )
        return stelis.thread_id, parent_thread_id

    def _end_subagent_thread(
        self,
        persona,
        thread_id: str,
        parent_thread_id: str,
        generate_chronicle: bool = True,
    ) -> Optional[str]:
        """End a subagent thread and switch back to the parent thread.

        Args:
            generate_chronicle: If True, generate a Chronicle summary before ending.

        Returns:
            Chronicle summary string if generated, else None.
        """
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            return None

        chronicle_summary = None
        if generate_chronicle:
            stelis_info = memory_adapter.get_stelis_info(thread_id)
            chronicle_prompt = stelis_info.chronicle_prompt if stelis_info else None
            chronicle_summary = self._generate_stelis_chronicle(
                persona, thread_id, chronicle_prompt
            )
            if chronicle_summary:
                LOGGER.info(
                    "[subagent] Generated Chronicle for thread %s: %s...",
                    thread_id, chronicle_summary[:100],
                )

        # End the Stelis thread
        success = memory_adapter.end_stelis_thread(
            thread_id=thread_id,
            status="completed",
            chronicle_summary=chronicle_summary,
        )
        if not success:
            LOGGER.error("[subagent] Failed to end subagent thread %s", thread_id)

        # Switch back to parent thread
        memory_adapter.set_active_thread(parent_thread_id)
        LOGGER.info(
            "[subagent] Ended subagent thread %s, returned to parent %s",
            thread_id, parent_thread_id,
        )
        return chronicle_summary

    # ---------------- Stelis Thread Nodes -----------------

    def _lg_stelis_start_node(
        self,
        node_def: Any,
        persona: Any,
        playbook: PlaybookSchema,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        """Create a node that starts a new Stelis thread for hierarchical context management."""

        async def node(state: dict):
            # Check for cancellation
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()

            node_id = getattr(node_def, "id", "stelis_start")
            label_raw = getattr(node_def, "label", None) or "Stelis Session"
            label = _format(label_raw, state)

            # Send status event
            if event_callback:
                event_callback({
                    "type": "status",
                    "content": f"{playbook.name} / {node_id}",
                    "playbook": playbook.name,
                    "node": node_id
                })

            # Get Stelis configuration
            stelis_config = getattr(node_def, "stelis_config", None) or {}
            if hasattr(stelis_config, "__dict__"):
                # Convert Pydantic model to dict if needed
                stelis_config = {
                    "window_ratio": getattr(stelis_config, "window_ratio", 0.8),
                    "max_depth": getattr(stelis_config, "max_depth", 3),
                    "chronicle_prompt": getattr(stelis_config, "chronicle_prompt", None),
                }

            window_ratio = stelis_config.get("window_ratio", 0.8)
            max_depth = stelis_config.get("max_depth", 3)
            chronicle_prompt = stelis_config.get("chronicle_prompt")

            # Get memory adapter from persona
            memory_adapter = getattr(persona, "sai_memory", None)
            if not memory_adapter:
                LOGGER.warning("[stelis] No memory adapter found for persona %s", persona.persona_id)
                state["stelis_error"] = "No memory adapter available"
                state["stelis_available"] = False
                return state

            # Check if we can start a new Stelis thread
            if not memory_adapter.can_start_stelis(max_depth=max_depth):
                error_msg = f"Stelis max depth exceeded (max={max_depth})"
                LOGGER.warning("[stelis] %s for persona %s", error_msg, persona.persona_id)
                state["stelis_error"] = error_msg
                state["stelis_available"] = False
                return state

            # Get current thread as parent
            # get_current_thread() returns full thread ID (e.g., "air_city_a:__persona__")
            # Use it directly as parent_thread_id, don't pass to _thread_id which adds prefix
            parent_thread_id = memory_adapter.get_current_thread()
            if parent_thread_id is None:
                # Fallback to default persona thread if no active thread set
                parent_thread_id = memory_adapter._thread_id(None)

            # Create new Stelis thread
            stelis = memory_adapter.start_stelis_thread(
                parent_thread_id=parent_thread_id,
                window_ratio=window_ratio,
                chronicle_prompt=chronicle_prompt,
                max_depth=max_depth,
                label=label,
            )

            if not stelis:
                LOGGER.error("[stelis] Failed to create Stelis thread for persona %s", persona.persona_id)
                state["stelis_error"] = "Failed to create Stelis thread"
                state["stelis_available"] = False
                return state

            # Add anchor message to PARENT thread (before switching)
            # This message will be dynamically expanded when viewing parent thread
            import time
            anchor_message = {
                "role": "system",
                "content": "",  # Content is dynamically generated
                "metadata": {
                    "type": "stelis_anchor",
                    "stelis_thread_id": stelis.thread_id,
                    "stelis_label": label,
                    "created_at": int(time.time()),
                },
                "embedding_chunks": 0,  # Don't embed this message
            }
            memory_adapter.append_persona_message(
                anchor_message,
                thread_suffix=parent_thread_id.split(":")[-1] if ":" in parent_thread_id else parent_thread_id,
            )
            LOGGER.debug(
                "[stelis] Added anchor message to parent thread %s for Stelis %s",
                parent_thread_id, stelis.thread_id
            )

            # Switch to new Stelis thread
            memory_adapter.set_active_thread(stelis.thread_id)
            log_sea_trace(playbook.name, node_id, "STELIS_START", f"thread={stelis.thread_id} label=\"{label}\"")

            # Update state with Stelis info
            state["stelis_thread_id"] = stelis.thread_id
            state["stelis_parent_thread_id"] = parent_thread_id
            state["stelis_depth"] = stelis.depth
            state["stelis_window_ratio"] = window_ratio
            state["stelis_label"] = label
            state["stelis_available"] = True

            LOGGER.info(
                "[stelis] Started Stelis thread %s (parent=%s, depth=%d, ratio=%.2f, label=%s)",
                stelis.thread_id, parent_thread_id, stelis.depth, window_ratio, label
            )

            # Emit event for UI
            if event_callback:
                event_callback({
                    "type": "stelis_start",
                    "thread_id": stelis.thread_id,
                    "parent_thread_id": parent_thread_id,
                    "depth": stelis.depth,
                    "label": label,
                })

            return state

        return node

    def _lg_stelis_end_node(
        self,
        node_def: Any,
        persona: Any,
        playbook: PlaybookSchema,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        """Create a node that ends the current Stelis thread and returns to parent context."""

        async def node(state: dict):
            # Check for cancellation
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()

            node_id = getattr(node_def, "id", "stelis_end")
            label = getattr(node_def, "label", None) or "End Stelis Session"
            generate_chronicle = getattr(node_def, "generate_chronicle", True)

            # Send status event
            if event_callback:
                event_callback({
                    "type": "status",
                    "content": f"{playbook.name} / {node_id}",
                    "playbook": playbook.name,
                    "node": node_id
                })

            # Get memory adapter from persona
            memory_adapter = getattr(persona, "sai_memory", None)
            if not memory_adapter:
                LOGGER.warning("[stelis] No memory adapter found for persona %s", persona.persona_id)
                return state

            # Get current Stelis thread info from state
            current_thread_id = state.get("stelis_thread_id")
            parent_thread_id = state.get("stelis_parent_thread_id")

            if not current_thread_id or not parent_thread_id:
                LOGGER.warning("[stelis] STELIS_END called without active Stelis context")
                return state

            # Verify we're in a Stelis thread
            stelis_info = memory_adapter.get_stelis_info(current_thread_id)
            if not stelis_info:
                LOGGER.warning("[stelis] Current thread %s is not a Stelis thread", current_thread_id)
                return state

            # Generate Chronicle summary if requested
            chronicle_summary = None
            if generate_chronicle:
                chronicle_summary = self._generate_stelis_chronicle(
                    persona,
                    current_thread_id,
                    stelis_info.chronicle_prompt
                )
                LOGGER.info(
                    "[stelis] Generated Chronicle for thread %s: %s...",
                    current_thread_id,
                    chronicle_summary[:100] if chronicle_summary else "(empty)"
                )

            # End the Stelis thread
            success = memory_adapter.end_stelis_thread(
                thread_id=current_thread_id,
                status="completed",
                chronicle_summary=chronicle_summary,
            )

            if not success:
                LOGGER.error("[stelis] Failed to end Stelis thread %s", current_thread_id)

            # Switch back to parent thread
            memory_adapter.set_active_thread(parent_thread_id)

            # Store Chronicle in state for potential use
            if chronicle_summary:
                state["stelis_chronicle"] = chronicle_summary

            # Clear Stelis state
            state["stelis_thread_id"] = None
            state["stelis_parent_thread_id"] = None
            state["stelis_depth"] = None

            LOGGER.info(
                "[stelis] Ended Stelis thread %s, returned to parent %s",
                current_thread_id, parent_thread_id
            )

            _chron_str = chronicle_summary or "(none)"
            log_sea_trace(playbook.name, node_id, "STELIS_END", f"thread={current_thread_id} chronicle=\"{_chron_str}\"")

            # Emit event for UI
            if event_callback:
                event_callback({
                    "type": "stelis_end",
                    "thread_id": current_thread_id,
                    "parent_thread_id": parent_thread_id,
                    "chronicle_generated": generate_chronicle,
                })

            return state

        return node

    def _generate_stelis_chronicle(
        self,
        persona: Any,
        thread_id: str,
        chronicle_prompt: Optional[str] = None,
    ) -> Optional[str]:
        """Generate a Chronicle summary for a Stelis thread.

        This creates a concise summary of the conversation/work done in the
        Stelis thread, which will be stored and can be referenced later.
        """
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            return None

        # Get messages from the Stelis thread
        try:
            messages = memory_adapter.get_thread_messages(thread_id, page=0, page_size=1000)
        except Exception as exc:
            LOGGER.warning("[stelis] Failed to get messages for Chronicle: %s", exc)
            return None

        if not messages:
            return None

        # Build full conversation content for summarization (no per-message truncation)
        content_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                content_parts.append(f"[{role}]: {content}")

        if not content_parts:
            return None

        conversation_text = "\n".join(content_parts)

        # Use default prompt if not specified
        if not chronicle_prompt:
            chronicle_prompt = (
                "Please summarize the following conversation/work session concisely. "
                "Focus on: what was done, key decisions made, and any important outcomes."
            )

        # Get LLM client for summarization
        try:
            # Prefer persona's existing lightweight client (already configured)
            client = getattr(persona, "lightweight_llm_client", None)
            if client is None:
                # Fallback: create a temporary client
                from llm_clients import get_llm_client
                from saiverse.model_configs import get_context_length, get_model_provider

                lightweight_model = getattr(persona, "lightweight_model", None) or _get_default_lightweight_model()
                lw_context = get_context_length(lightweight_model)
                provider = get_model_provider(lightweight_model)
                client = get_llm_client(lightweight_model, provider, lw_context)

            summary_messages = [
                {"role": "system", "content": chronicle_prompt},
                {"role": "user", "content": f"Session content:\n\n{conversation_text}"}
            ]

            response = client.generate(summary_messages, temperature=0.3)
            if response and isinstance(response, str):
                return response.strip()

        except Exception as exc:
            LOGGER.warning("[stelis] Chronicle generation failed: %s", exc)

        return None

    # ---------------- context helpers -----------------
    def _append_router_function_call(
        self,
        state: Dict[str, Any],
        selection: Optional[Dict[str, Any]],
        raw_text: str,
    ) -> None:
        payload = selection if isinstance(selection, dict) else None
        if payload is None:
            payload = {"raw": raw_text}
        try:
            args_text = json.dumps(payload, ensure_ascii=False)
        except Exception:
            args_text = json.dumps({"raw": str(raw_text)}, ensure_ascii=False)
        conv = state.get("messages")
        if not isinstance(conv, list):
            conv = []
        call_id = f"router_call_{uuid.uuid4().hex}"
        call_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "route_playbook",
                        "arguments": args_text,
                    },
                }
            ],
        }
        if conv and isinstance(conv[-1], dict) and conv[-1].get("role") == "assistant":
            conv[-1] = call_msg
        else:
            conv.append(call_msg)
        state["messages"] = conv
        state["_last_tool_call_id"] = call_id
        state["_last_tool_name"] = payload.get("playbook") or "sub_playbook"

    def _store_memory(
        self,
        persona: Any,
        text: str,
        *,
        role: str = "assistant",
        tags: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Store a message to SAIMemory. Returns True on success, False on failure."""
        if not text:
            return True
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning(
                "[_store_memory] SAIMemory adapter unavailable for persona=%s — message will NOT be stored. "
                "Check embedding model setup.",
                getattr(persona, "persona_id", None),
            )
            return False
        try:
            if adapter and adapter.is_ready():
                current_thread = adapter.get_current_thread()
                LOGGER.debug("[_store_memory] Active thread: %s (persona_id=%s)", current_thread, getattr(persona, "persona_id", None))
                # If no active thread, initialize the default __persona__ thread
                if current_thread is None:
                    pid = getattr(persona, "persona_id", None) or "unknown"
                    default_thread = f"{pid}:{adapter._PERSONA_THREAD_SUFFIX}"
                    adapter.set_active_thread(default_thread)
                    current_thread = default_thread
                    LOGGER.info("[_store_memory] No active thread for %s — initialized default: %s", pid, default_thread)
                message = {"role": role or "assistant", "content": text}
                clean_tags = [str(tag) for tag in (tags or []) if tag]
                # Add pulse:uuid tag
                if pulse_id:
                    clean_tags.append(f"pulse:{pulse_id}")
                # Build metadata dict
                msg_metadata: Dict[str, Any] = {}
                if clean_tags:
                    msg_metadata["tags"] = clean_tags
                # Merge additional metadata (e.g., media attachments)
                if isinstance(metadata, dict):
                    for key, value in metadata.items():
                        if key == "tags":
                            # Merge tags
                            extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                            msg_metadata.setdefault("tags", []).extend(extra_tags)
                        else:
                            msg_metadata[key] = value
                if msg_metadata:
                    message["metadata"] = msg_metadata
                # Pass thread_suffix to ensure message is saved to correct thread
                thread_suffix = current_thread.split(":", 1)[1] if ":" in current_thread else current_thread
                adapter.append_persona_message(message, thread_suffix=thread_suffix)
            return True
        except Exception:
            LOGGER.warning("memorize node not stored", exc_info=True)
            return False

    def _append_tool_result_message(
        self,
        state: Dict[str, Any],
        source: str,
        payload: str,
    ) -> None:
        call_id = state.get("_last_tool_call_id")
        if not call_id:
            return
        conv = state.get("messages")
        if not isinstance(conv, list):
            conv = []
        message = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": source or state.get("_last_tool_name") or "sub_playbook",
            "content": payload,
        }
        conv.append(message)
        state["messages"] = conv
        state["_last_tool_call_id"] = None

    # ---------------- helpers -----------------
    def _effective_building_id(self, persona: Any, fallback: str) -> str:
        """Return persona's actual building from occupancy map.

        After a move_persona tool changes the occupants dict, this returns
        the new building so that post-move utterances land in the correct
        building history.  For normal (non-move) conversations it returns
        the same value as *fallback*.
        """
        pid = getattr(persona, "persona_id", None)
        if pid:
            for bid, occ_list in self.manager.occupants.items():
                if pid in occ_list:
                    return bid
        return fallback

    def _emit_speak(self, persona: Any, building_id: str, text: str, pulse_id: Optional[str] = None, record_history: bool = True, extra_metadata: Optional[Dict[str, Any]] = None) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        # Build metadata with tags and conversation partners
        metadata: Dict[str, Any] = {"tags": ["conversation"]}
        if pulse_id:
            metadata["tags"].append(f"pulse:{pulse_id}")
        # Merge extra metadata (reasoning, reasoning_details, etc.)
        if isinstance(extra_metadata, dict):
            for key, value in extra_metadata.items():
                if key == "tags":
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    metadata["tags"].extend(extra_tags)
                else:
                    metadata[key] = value
        # Add conversation partners to "with" field
        partners = []
        occupants = self.manager.occupants.get(building_id, [])
        for oid in occupants:
            if oid != persona.persona_id:
                partners.append(oid)
        # Add user if online/away
        presence = getattr(self.manager, "user_presence_status", "offline")
        if presence in ("online", "away"):
            partners.append("user")
        if partners:
            metadata["with"] = partners
        msg["metadata"] = metadata
        if record_history:
            try:
                persona.history_manager.add_message(msg, building_id, heard_by=None)
                self.manager.gateway_handle_ai_replies(building_id, persona, [text])
            except Exception:
                LOGGER.exception("Failed to emit speak message")
        # Notify Unity Gateway
        self._notify_unity_speak(persona, text)

    def _emit_say(self, persona: Any, building_id: str, text: str, pulse_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        # Build metadata dict
        msg_metadata: Dict[str, Any] = {}
        if pulse_id:
            msg_metadata["tags"] = [f"pulse:{pulse_id}"]
        # Merge additional metadata (e.g., media attachments)
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if key == "tags":
                    # Merge tags
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    msg_metadata.setdefault("tags", []).extend(extra_tags)
                else:
                    msg_metadata[key] = value
        # Add conversation partners to "with" field
        partners = []
        occupants = self.manager.occupants.get(building_id, [])
        for oid in occupants:
            if oid != persona.persona_id:
                partners.append(oid)
        presence = getattr(self.manager, "user_presence_status", "offline")
        if presence in ("online", "away"):
            partners.append("user")
        if partners:
            msg_metadata["with"] = partners
        if msg_metadata:
            msg["metadata"] = msg_metadata
        try:
            persona.history_manager.add_to_building_only(building_id, msg)
            self.manager.gateway_handle_ai_replies(building_id, persona, [text])
        except Exception:
            LOGGER.exception("Failed to emit say message")
        # Notify Unity Gateway
        self._notify_unity_speak(persona, text)

    def _emit_think(self, persona: Any, pulse_id: str, text: str, record_history: bool = True) -> None:
        if not record_history:
            return
        adapter = getattr(persona, "sai_memory", None)
        try:
            if adapter and adapter.is_ready():
                adapter.append_persona_message(
                    {
                        "role": "assistant",
                        "content": text,
                        "metadata": {"tags": ["internal", f"pulse:{pulse_id}"]},
                        "persona_id": persona.persona_id,
                    }
                )
        except Exception:
            LOGGER.warning("think message not stored", exc_info=True)

    def _notify_unity_speak(self, persona: Any, text: str) -> None:
        """Send persona speak event to Unity Gateway if connected."""
        if not text:
            return
        unity_gateway = getattr(self.manager, "unity_gateway", None)
        if not unity_gateway:
            return
        try:
            import asyncio
            persona_id = getattr(persona, "persona_id", "unknown")
            # Run async send_speak in a new event loop if not in async context
            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(unity_gateway.send_speak(persona_id, text))
            except RuntimeError:
                # No running event loop
                loop = asyncio.new_event_loop()
                loop.run_until_complete(unity_gateway.send_speak(persona_id, text))
                loop.close()
        except Exception as exc:
            LOGGER.debug("Failed to notify Unity Gateway: %s", exc)

    # ---------------- history metabolism -----------------

    def _get_high_watermark(self, persona) -> Optional[int]:
        """Get the high watermark (max history messages) for metabolism."""
        override = getattr(self.manager, "max_history_messages_override", None) if self.manager else None
        if override is not None:
            return override
        from saiverse.model_configs import get_default_max_history_messages
        persona_model = getattr(persona, "model", None)
        if persona_model:
            return get_default_max_history_messages(persona_model)
        return None

    def _get_low_watermark(self, persona) -> Optional[int]:
        """Get the low watermark (keep messages after metabolism) for metabolism."""
        override = getattr(self.manager, "metabolism_keep_messages_override", None) if self.manager else None
        if override is not None:
            return override
        from saiverse.model_configs import get_metabolism_keep_messages
        persona_model = getattr(persona, "model", None)
        if persona_model:
            return get_metabolism_keep_messages(persona_model)
        return None

    # ---- anchor persistence helpers ----

    def _load_anchors(self, persona) -> Dict[str, Any]:
        """Load per-model metabolism anchors from DB (AI.METABOLISM_ANCHORS)."""
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return {}
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return {}
        db = self.manager.SessionLocal()
        try:
            from database.models import AI
            ai_row = db.query(AI).filter_by(AIID=persona_id).first()
            if ai_row and ai_row.METABOLISM_ANCHORS:
                return json.loads(ai_row.METABOLISM_ANCHORS)
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to load anchors for %s: %s", persona_id, exc)
        finally:
            db.close()
        return {}

    def _save_anchors(self, persona, anchors: Dict[str, Any]) -> None:
        """Persist per-model metabolism anchors to DB."""
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return
        db = self.manager.SessionLocal()
        try:
            from database.models import AI
            ai_row = db.query(AI).filter_by(AIID=persona_id).first()
            if ai_row:
                ai_row.METABOLISM_ANCHORS = json.dumps(anchors, ensure_ascii=False)
                db.commit()
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to save anchors for %s: %s", persona_id, exc)
        finally:
            db.close()

    def _get_anchor_validity_seconds(self, model_key: str) -> int:
        """Get anchor validity duration in seconds based on model cache config.

        - Anthropic (explicit cache): current manager.state.cache_ttl (300s or 3600s)
        - Others (implicit/no cache): 1200s (20 min)
        """
        try:
            from saiverse.model_configs import get_cache_config
            cache_config = get_cache_config(model_key)
            cache_type = cache_config.get("type", "implicit")
            if cache_type == "explicit":
                current_ttl = "5m"
                if self.manager and hasattr(self.manager, "state"):
                    current_ttl = getattr(self.manager.state, "cache_ttl", "5m")
                return 300 if current_ttl == "5m" else 3600
        except Exception:
            LOGGER.warning("Failed to resolve cache TTL for model %s", model_key, exc_info=True)
        return 1200  # 20 minutes default

    def _resolve_metabolism_anchor(self, persona) -> tuple:
        """Resolve the best metabolism anchor using 3-level fallback.

        Returns:
            (anchor_id, resolution_type) where resolution_type is
            "self" | "other" | "minimal".
            anchor_id is None for "minimal" (no valid anchor found).
        """
        persona_model = getattr(persona, "model", None)
        if not persona_model:
            return (None, "minimal")

        anchors = self._load_anchors(persona)
        now = datetime.now()

        # Case 1: self model's anchor exists and is valid
        self_entry = anchors.get(persona_model)
        if self_entry:
            try:
                updated_at = datetime.fromisoformat(self_entry["updated_at"])
                validity = self._get_anchor_validity_seconds(persona_model)
                age = (now - updated_at).total_seconds()
                if age <= validity:
                    LOGGER.debug(
                        "[metabolism] Anchor resolved: self model '%s' (age=%.0fs, validity=%ds)",
                        persona_model, age, validity,
                    )
                    return (self_entry["anchor_id"], "self")
                else:
                    LOGGER.debug(
                        "[metabolism] Self model anchor expired: '%s' (age=%.0fs > validity=%ds)",
                        persona_model, age, validity,
                    )
            except (KeyError, ValueError, TypeError) as exc:
                LOGGER.debug("[metabolism] Invalid self anchor entry: %s", exc)

        # Case 2: most recent valid anchor from any model
        best_entry = None
        best_updated = None
        for model_key, entry in anchors.items():
            if model_key == persona_model:
                continue  # already checked
            try:
                updated_at = datetime.fromisoformat(entry["updated_at"])
                validity = self._get_anchor_validity_seconds(model_key)
                age = (now - updated_at).total_seconds()
                if age <= validity:
                    if best_updated is None or updated_at > best_updated:
                        best_entry = entry
                        best_updated = updated_at
            except (KeyError, ValueError, TypeError):
                continue

        if best_entry:
            LOGGER.debug(
                "[metabolism] Anchor resolved: other model (age=%.0fs)",
                (now - best_updated).total_seconds(),
            )
            return (best_entry["anchor_id"], "other")

        # Case 3: no valid anchor
        LOGGER.debug("[metabolism] No valid anchor found — will use minimal load")
        return (None, "minimal")

    def _update_anchor_for_model(self, persona, model_key: str, anchor_id: str) -> None:
        """Update the anchor for a specific model and persist to DB."""
        if not model_key or not anchor_id:
            return
        anchors = self._load_anchors(persona)
        anchors[model_key] = {
            "anchor_id": anchor_id,
            "updated_at": datetime.now().isoformat(),
        }
        self._save_anchors(persona, anchors)

    def _maybe_run_metabolism(
        self,
        persona,
        building_id: str,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """Check if metabolism is needed after response and run if so."""
        if not getattr(self.manager, "metabolism_enabled", False):
            return

        history_mgr = getattr(persona, "history_manager", None)
        anchor = getattr(history_mgr, "metabolism_anchor_message_id", None)
        if not history_mgr or not anchor:
            return

        high_wm = self._get_high_watermark(persona)
        if high_wm is None:
            return

        # Get current message count from anchor
        current_messages = history_mgr.get_history_from_anchor(
            anchor, required_tags=["conversation"],
        )
        if len(current_messages) <= high_wm:
            return  # Haven't reached high watermark yet

        low_wm = self._get_low_watermark(persona)
        if low_wm is None or high_wm - low_wm < 20:
            return  # Gap too small for a Chronicle batch

        LOGGER.info(
            "[metabolism] Triggering metabolism for %s: %d messages > high_wm=%d, will keep %d",
            getattr(persona, "persona_id", "?"), len(current_messages), high_wm, low_wm,
        )
        self._run_metabolism(persona, building_id, current_messages, low_wm, event_callback)

    def _run_metabolism(
        self,
        persona,
        building_id: str,
        current_messages: List[Dict[str, Any]],
        keep_count: int,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """Execute history metabolism: Chronicle generation + anchor update."""
        evict_count = len(current_messages) - keep_count

        # 1. Notify start
        if event_callback:
            event_callback({
                "type": "metabolism",
                "status": "started",
                "content": f"記憶を整理しています（{len(current_messages)}件 → {keep_count}件）...",
            })

        # 2. Chronicle generation (only if Memory Weave is enabled AND per-persona toggle is on)
        memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
        if memory_weave_enabled and self._is_chronicle_enabled_for_persona(persona):
            try:
                self._generate_chronicle(persona, event_callback)
            except Exception as exc:
                LOGGER.warning("[metabolism] Chronicle generation failed: %s", exc)

        # 3. Update anchor to new window start
        new_anchor_id = current_messages[evict_count].get("id")
        if new_anchor_id:
            persona.history_manager.metabolism_anchor_message_id = new_anchor_id
            persona_model = getattr(persona, "model", None)
            if persona_model:
                self._update_anchor_for_model(persona, persona_model, new_anchor_id)
            LOGGER.info("[metabolism] Updated anchor to %s (evicted %d, kept %d)", new_anchor_id, evict_count, keep_count)

        # 4. Notify completion
        if event_callback:
            event_callback({
                "type": "metabolism",
                "status": "completed",
                "content": f"記憶の整理が完了しました（{evict_count}件の会話をChronicleに圧縮）",
                "evicted": evict_count,
                "kept": keep_count,
            })

    def _is_chronicle_enabled_for_persona(self, persona) -> bool:
        """Check per-persona Chronicle auto-generation toggle from DB."""
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return True  # fallback: enabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.CHRONICLE_ENABLED if ai else True
        finally:
            db.close()

    def _is_memory_weave_context_enabled(self, persona) -> bool:
        """Check per-persona Memory Weave context injection toggle from DB."""
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return True  # fallback: enabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.MEMORY_WEAVE_CONTEXT if ai else True
        finally:
            db.close()

    def _generate_chronicle(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> None:
        """Generate Chronicle entries from all unprocessed messages."""
        from llm_clients.factory import get_llm_client
        from saiverse.model_configs import find_model_config
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.generator import ArasujiGenerator, DEFAULT_BATCH_SIZE
        from sai_memory.memory.storage import Message

        # Get LLM client using MEMORY_WEAVE_MODEL
        model_name = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.5-flash-lite-preview-09-2025")
        model_id, model_config = find_model_config(model_name)
        if not model_config:
            LOGGER.warning("[metabolism] Model '%s' not found for Chronicle generation", model_name)
            return

        actual_model_id = model_config.get("model", model_name)
        provider = model_config.get("provider")
        context_length = model_config.get("context_length", 128000)
        client = get_llm_client(model_id, provider, context_length, config=model_config)

        # Initialize arasuji tables and fetch all messages
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning("[metabolism] SAIMemory not available for Chronicle generation")
            return

        init_arasuji_tables(adapter.conn)

        # Fetch ALL messages across all threads (same as UI-triggered generation).
        # Previously this only fetched from the default persona thread, missing
        # messages logged on building-specific threads.
        import json as _json
        cur = adapter.conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            "FROM messages ORDER BY created_at ASC"
        )
        all_messages = []
        for row in cur.fetchall():
            msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
            metadata = None
            if metadata_raw:
                try:
                    metadata = _json.loads(metadata_raw)
                except Exception:
                    pass
            all_messages.append(Message(
                id=msg_id, thread_id=tid, role=role, content=content,
                resource_id=resource_id, created_at=int(created_at),
                metadata=metadata,
            ))

        if not all_messages:
            return

        # Calculate unprocessed message count before confirming
        from sai_memory.arasuji.storage import get_total_message_count
        processed_count = get_total_message_count(adapter.conn)
        total_count = len(all_messages)
        unprocessed_count = total_count - processed_count

        if unprocessed_count <= 0:
            LOGGER.info("[metabolism] No unprocessed messages for Chronicle generation")
            return

        batch_size_for_estimate = int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
        estimated_llm_calls = max(1, (unprocessed_count + batch_size_for_estimate - 1) // batch_size_for_estimate)

        # Request user confirmation before generating
        if event_callback:
            import threading as _threading

            request_id = str(uuid.uuid4())
            confirm_event = _threading.Event()
            self.manager._pending_permission_requests[request_id] = confirm_event

            persona_name = getattr(persona, "persona_name", None)
            display_model = model_config.get("display_name", model_name)

            event_callback({
                "type": "chronicle_confirm",
                "request_id": request_id,
                "unprocessed_messages": unprocessed_count,
                "total_messages": total_count,
                "estimated_llm_calls": estimated_llm_calls,
                "model_name": display_model,
                "persona_name": persona_name,
            })
            LOGGER.info(
                "[metabolism] Sent chronicle_confirm: %d unprocessed messages, model=%s (id=%s)",
                unprocessed_count, display_model, request_id,
            )

            # Block until user responds or timeout (60s)
            responded = confirm_event.wait(timeout=60)
            self.manager._pending_permission_requests.pop(request_id, None)
            response = self.manager._permission_responses.pop(request_id, None)

            if not responded or response != "allow":
                reason = "timeout" if not responded else response
                LOGGER.info("[metabolism] Chronicle generation skipped (user %s)", reason)
                if event_callback:
                    event_callback({
                        "type": "warning",
                        "content": "Chronicle生成をスキップしました。",
                        "warning_code": "chronicle_skipped",
                        "display": "toast",
                    })
                return
            LOGGER.info("[metabolism] Chronicle generation approved by user")
        else:
            # No event_callback (e.g. auto pulse without UI) — skip generation
            LOGGER.info("[metabolism] No event_callback — skipping Chronicle generation confirmation (%d unprocessed)", unprocessed_count)
            return

        # Build progress callback for streaming status to frontend
        def progress_fn(processed, total):
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": f"Chronicleを生成しています ({processed}/{total})...",
                })

        # Build cancellation check from cancellation token
        cancel_fn = None
        if cancellation_token:
            cancel_fn = lambda: cancellation_token.is_cancelled

        batch_size = int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
        generator = ArasujiGenerator(
            client, adapter.conn,
            batch_size=batch_size,
            consolidation_size=10,
            persona_id=getattr(persona, "persona_id", None),
        )
        level1, consolidated = generator.generate_unprocessed(
            all_messages,
            progress_callback=progress_fn,
            cancel_check=cancel_fn,
        )
        LOGGER.info(
            "[metabolism] Chronicle generation complete: %d level1, %d consolidated entries",
            len(level1), len(consolidated),
        )

    # ---------------- context preparation -----------------

    def _prepare_context(self, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None, warnings: Optional[List[Dict[str, Any]]] = None, preview_only: bool = False, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, cancellation_token: Optional[CancellationToken] = None) -> List[Dict[str, Any]]:
        from sea.playbook_models import ContextRequirements

        # Use provided requirements or default to full context
        reqs = requirements if requirements else ContextRequirements()

        messages: List[Dict[str, Any]] = []

        # ---- system prompt ----
        if reqs.system_prompt:
            system_sections: List[str] = []

            # 1. Common prompt (world setting, framework explanation)
            common_prompt_template = getattr(persona, "common_prompt", None)
            LOGGER.debug("common_prompt_template is %s (type=%s)", common_prompt_template, type(common_prompt_template))
            if common_prompt_template:
                try:
                    # Get building info for variable expansion
                    building_obj = getattr(persona, "buildings", {}).get(building_id)
                    building_name = building_obj.name if building_obj else building_id
                    city_name = getattr(persona, "current_city_id", "unknown_city")

                    # Expand variables in common prompt using safe replace (avoid conflict with JSON examples)
                    common_text = common_prompt_template
                    replacements = {
                        "{current_persona_name}": getattr(persona, "persona_name", "Unknown"),
                        "{current_persona_id}": getattr(persona, "persona_id", "unknown_id"),
                        "{current_building_name}": building_name,
                        "{current_city_name}": city_name,
                        "{current_persona_system_instruction}": getattr(persona, "persona_system_instruction", ""),
                        "{current_building_system_instruction}": getattr(building_obj, "base_system_instruction" if reqs.visual_context else "system_instruction", "") if building_obj else "",
                        "{linked_user_name}": getattr(persona, "linked_user_name", "the user"),
                    }
                    for placeholder, value in replacements.items():
                        common_text = common_text.replace(placeholder, value)
                    system_sections.append(common_text.strip())
                except Exception as exc:
                    LOGGER.error("Failed to format common prompt: %s", exc, exc_info=True)

            # 2. "## あなたについて" section
            persona_section_parts: List[str] = []
            persona_sys = getattr(persona, "persona_system_instruction", "") or ""
            if persona_sys:
                persona_section_parts.append(persona_sys.strip())

            # persona inventory -- skip when visual_context handles it
            if reqs.inventory and not reqs.visual_context:
                try:
                    inv_builder = getattr(persona, "_inventory_summary_lines", None)
                    inv_lines: List[str] = inv_builder() if callable(inv_builder) else []
                except Exception:
                    inv_lines = []
                if inv_lines:
                    persona_section_parts.append("### インベントリ\n" + "\n".join(inv_lines))

            if persona_section_parts:
                system_sections.append("## あなたについて\n" + "\n\n".join(persona_section_parts))

            # 3. "## {building_name}" section (current location)
            # Skip when visual_context handles building info and items
            if not reqs.visual_context:
                try:
                    building_obj = getattr(persona, "buildings", {}).get(building_id)
                    if building_obj:
                        building_section_parts: List[str] = []

                        # Building system instruction
                        # NOTE: Datetime variables ({current_time}, etc.) are no longer expanded here.
                        # Time information is now provided via Realtime Context at the end of messages
                        # to improve LLM context caching efficiency.
                        # Use base_system_instruction (without items) to avoid duplication
                        # with the building_items block below.
                        building_sys = getattr(building_obj, "base_system_instruction", None) or getattr(building_obj, "system_instruction", None)
                        if building_sys:
                            building_section_parts.append(str(building_sys).strip())

                        # Building items
                        if reqs.building_items:
                            try:
                                items_by_building = getattr(self.manager, "items_by_building", {}) or {}
                                item_registry = getattr(self.manager, "item_registry", {}) or {}
                                b_items = items_by_building.get(building_id, [])
                                lines = []
                                for iid in b_items:
                                    data = item_registry.get(iid, {})
                                    raw_name = data.get("name", "") or ""
                                    name = raw_name.strip() if raw_name.strip() else "(名前なし)"
                                    desc = (data.get("description") or "").strip() or "(説明なし)"
                                    lines.append(f"- [{iid}] {name}: {desc}")
                                if lines:
                                    building_section_parts.append("### 建物内のアイテム\n" + "\n".join(lines))
                            except Exception:
                                LOGGER.warning("Failed to collect building items for %s", building_id, exc_info=True)

                        if building_section_parts:
                            building_name = getattr(building_obj, "name", building_id)
                            system_sections.append(f"## {building_name} (ID: {building_id})\n" + "\n\n".join(building_section_parts))
                except Exception:
                    LOGGER.warning("Failed to build building section for system prompt", exc_info=True)

            # 4. "## 利用可能な能力" section (available playbooks)
            if reqs.available_playbooks:
                try:
                    from tools import TOOL_REGISTRY
                    list_playbooks_func = TOOL_REGISTRY.get("list_available_playbooks")
                    if list_playbooks_func:
                        # Get available playbooks JSON (tool returns string; accept old tuple form)
                        playbooks_raw = list_playbooks_func(
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id
                        )
                        playbooks_json = playbooks_raw[0] if isinstance(playbooks_raw, tuple) else playbooks_raw
                        if playbooks_json:
                            import json
                            playbooks_list = json.loads(playbooks_json)
                            if playbooks_list:
                                playbooks_formatted = json.dumps(playbooks_list, ensure_ascii=False, indent=2)
                                system_sections.append(f"## 利用可能な能力\n以下のPlaybookを実行できます：\n```json\n{playbooks_formatted}\n```")
                except Exception as exc:
                    LOGGER.debug("Failed to add available playbooks section: %s", exc)

            # 5. "## 現在の状況" section (working memory)
            if reqs.working_memory:
                try:
                    sai_mem = getattr(persona, "sai_memory", None)
                    if sai_mem and sai_mem.is_ready():
                        wm_data = sai_mem.load_working_memory()
                        if wm_data:
                            import json as json_mod
                            wm_text = json_mod.dumps(wm_data, ensure_ascii=False, indent=2)
                            system_sections.append(f"## 現在の状況\n```json\n{wm_text}\n```")
                            LOGGER.debug("[sea][prepare-context] Added working_memory section")
                except Exception as exc:
                    LOGGER.debug("Failed to add working_memory section: %s", exc)

            # NOTE: Spatial context (Unity) has been moved to Realtime Context
            # to improve LLM context caching efficiency.

            system_text = "\n\n---\n\n".join([s for s in system_sections if s])
            if system_text:
                messages.append({"role": "system", "content": system_text})

        # ---- Memory Weave context (Chronicle + Memopedia) ----
        # Inserted between system prompt and visual context
        _mw_persona_enabled = self._is_memory_weave_context_enabled(persona) if reqs.memory_weave else False
        LOGGER.info("[sea][prepare-context] memory_weave=%s, persona_enabled=%s", reqs.memory_weave, _mw_persona_enabled)
        if reqs.memory_weave and _mw_persona_enabled:
            try:
                from builtin_data.tools.get_memory_weave_context import get_memory_weave_context
                from tools.context import persona_context
                persona_id = getattr(persona, "persona_id", None)
                
                # Get persona_dir from sai_memory adapter (same pattern as working_memory)
                sai_mem = getattr(persona, "sai_memory", None)
                persona_dir_path = getattr(sai_mem, "persona_dir", None) if sai_mem else None
                persona_dir = str(persona_dir_path) if persona_dir_path else None
                
                LOGGER.info("[sea][prepare-context] Calling get_memory_weave_context for persona=%s dir=%s", persona_id, persona_dir)
                with persona_context(persona_id, persona_dir, self.manager):
                    mw_messages = get_memory_weave_context(persona_id=persona_id, persona_dir=persona_dir)
                LOGGER.info("[sea][prepare-context] get_memory_weave_context returned %d messages", len(mw_messages))
                if mw_messages:
                    messages.extend(mw_messages)
                    LOGGER.debug("[sea][prepare-context] Added %d Memory Weave context messages", len(mw_messages))
            except Exception as exc:
                LOGGER.exception("[sea][prepare-context] Failed to get Memory Weave context: %s", exc)

        # ---- visual context (Building / Persona images) ----
        # Inserted right after system prompt but before conversation history
        if reqs.visual_context:
            try:
                from builtin_data.tools.get_visual_context import get_visual_context
                from tools.context import persona_context, get_active_manager
                persona_id = getattr(persona, "persona_id", None)
                persona_dir = getattr(persona, "persona_dir", None)
                with persona_context(persona_id, persona_dir, self.manager):
                    visual_messages = get_visual_context(building_id=building_id)
                if visual_messages:
                    messages.extend(visual_messages)
                    LOGGER.debug("[sea][prepare-context] Added %d visual context messages", len(visual_messages))
            except Exception as exc:
                LOGGER.debug("[sea][prepare-context] Failed to get visual context: %s", exc)

        # ---- history ----
        history_depth = reqs.history_depth
        if history_depth not in [0, "none"]:
            history_mgr = getattr(persona, "history_manager", None)
            if history_mgr:
                try:
                    # Determine which tags to include
                    required_tags = ["conversation"]
                    if reqs.include_internal:
                        required_tags.append("internal")

                    # Parse history_depth format
                    # - "full": use max_history_messages (message count) or context_length (character limit)
                    # - "Nmessages" (e.g., "10messages"): message count limit
                    # - integer or numeric string: character limit
                    use_message_count = False
                    limit_value = 2000  # fallback
                    used_anchor = False
                    recent = []

                    if history_depth == "full":
                        metabolism_enabled = getattr(self.manager, "metabolism_enabled", False) if self.manager else False

                        if metabolism_enabled and not preview_only:
                            # Persistent anchor resolution with 3-level fallback
                            anchor_id, resolution = self._resolve_metabolism_anchor(persona)

                            if anchor_id:
                                # Case 1 or 2: valid anchor found
                                recent_from_anchor = history_mgr.get_history_from_anchor(
                                    anchor_id, required_tags=required_tags, pulse_id=pulse_id,
                                )
                                if recent_from_anchor:
                                    recent = recent_from_anchor
                                    used_anchor = True
                                    history_mgr.metabolism_anchor_message_id = anchor_id
                                    LOGGER.debug(
                                        "[sea][prepare-context] Anchor-based retrieval (%s): %d messages from anchor %s",
                                        resolution, len(recent), anchor_id,
                                    )
                                    # Persist anchor for current model (touch updated_at)
                                    persona_model = getattr(persona, "model", None)
                                    if persona_model:
                                        self._update_anchor_for_model(persona, persona_model, anchor_id)
                            else:
                                # Case 3: no valid anchor — minimal load + Chronicle generation
                                memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
                                if memory_weave_enabled and self._is_chronicle_enabled_for_persona(persona):
                                    if event_callback:
                                        event_callback({
                                            "type": "metabolism",
                                            "status": "started",
                                            "content": "Chronicleを生成しています...",
                                        })
                                    try:
                                        LOGGER.info("[metabolism] Triggering Chronicle generation on anchor expiry")
                                        self._generate_chronicle(
                                            persona,
                                            event_callback=event_callback,
                                            cancellation_token=cancellation_token,
                                        )
                                    except Exception as exc:
                                        LOGGER.warning("[metabolism] Chronicle generation on anchor expiry failed: %s", exc)
                                    if event_callback:
                                        event_callback({
                                            "type": "metabolism",
                                            "status": "completed",
                                            "content": "Chronicle生成が完了しました",
                                        })

                                # Load minimal history (low watermark)
                                low_wm = self._get_low_watermark(persona)
                                limit_value = low_wm if low_wm and low_wm > 0 else 20
                                use_message_count = True
                                LOGGER.debug(
                                    "[sea][prepare-context] Minimal load (no valid anchor): %d messages",
                                    limit_value,
                                )

                        elif metabolism_enabled and preview_only:
                            # Preview mode: use anchor for retrieval but don't persist or generate Chronicle
                            anchor_id, resolution = self._resolve_metabolism_anchor(persona)
                            if anchor_id:
                                recent_from_anchor = history_mgr.get_history_from_anchor(
                                    anchor_id, required_tags=required_tags, pulse_id=pulse_id,
                                )
                                if recent_from_anchor:
                                    recent = recent_from_anchor
                                    used_anchor = True
                            if not used_anchor:
                                low_wm = self._get_low_watermark(persona)
                                limit_value = low_wm if low_wm and low_wm > 0 else 20
                                use_message_count = True

                        if not used_anchor and not metabolism_enabled:
                            # Metabolism disabled — traditional count/char retrieval
                            max_hist_msgs = getattr(self.manager, "max_history_messages_override", None) if self.manager else None
                            if max_hist_msgs is None:
                                from saiverse.model_configs import get_default_max_history_messages
                                persona_model = getattr(persona, "model", None)
                                if persona_model:
                                    max_hist_msgs = get_default_max_history_messages(persona_model)
                            if max_hist_msgs is not None:
                                limit_value = max_hist_msgs
                                use_message_count = True
                                LOGGER.debug("[sea][prepare-context] Using max_history_messages=%d", max_hist_msgs)
                            else:
                                limit_value = getattr(persona, "context_length", 2000)

                    elif isinstance(history_depth, str) and history_depth.endswith("messages"):
                        # Message count mode: "10messages", "20messages", etc.
                        try:
                            limit_value = int(history_depth[:-8])  # Remove "messages" suffix
                            use_message_count = True
                        except ValueError:
                            limit_value = 10  # fallback for message count
                            use_message_count = True
                    else:
                        try:
                            limit_value = int(history_depth)
                        except (ValueError, TypeError):
                            limit_value = 2000  # fallback

                    # Fetch history if not already retrieved via anchor
                    if not used_anchor:
                        LOGGER.debug("[sea][prepare-context] Fetching history: limit=%d, mode=%s, pulse_id=%s, balanced=%s, tags=%s",
                                    limit_value, "messages" if use_message_count else "chars", pulse_id, reqs.history_balanced, required_tags)

                        if use_message_count:
                            # Message count mode - balanced not supported yet
                            recent = history_mgr.get_recent_history_by_count(
                                limit_value,
                                required_tags=required_tags,
                                pulse_id=pulse_id,
                            )
                        elif reqs.history_balanced:
                            # Get conversation partners for balanced retrieval
                            participant_ids = ["user"]
                            occupants = self.manager.occupants.get(building_id, [])
                            persona_id = getattr(persona, "persona_id", None)
                            for oid in occupants:
                                if oid != persona_id:
                                    participant_ids.append(oid)
                            LOGGER.debug("[sea][prepare-context] Balancing across: %s", participant_ids)
                            recent = history_mgr.get_recent_history_balanced(
                                limit_value,
                                participant_ids,
                                required_tags=required_tags,
                                pulse_id=pulse_id,
                            )
                        else:
                            # Filter by required tags or current pulse_id
                            recent = history_mgr.get_recent_history(
                                limit_value,
                                required_tags=required_tags,
                                pulse_id=pulse_id,
                            )

                        # Set metabolism anchor on first count-based retrieval and persist (skip in preview)
                        metabolism_enabled_for_anchor = getattr(self.manager, "metabolism_enabled", False) if self.manager else False
                        if metabolism_enabled_for_anchor and recent and not preview_only:
                            oldest_id = recent[0].get("id")
                            if oldest_id:
                                history_mgr.metabolism_anchor_message_id = oldest_id
                                persona_model = getattr(persona, "model", None)
                                if persona_model:
                                    self._update_anchor_for_model(persona, persona_model, oldest_id)
                                LOGGER.debug("[sea][prepare-context] Set metabolism anchor to %s (persisted)", oldest_id)

                    LOGGER.debug("[sea][prepare-context] Got %d history messages", len(recent))
                    # Enrich messages with attachment context
                    enriched_recent = self._enrich_history_with_attachments(recent)
                    messages.extend(enriched_recent)
                except Exception as exc:
                    LOGGER.exception("[sea][prepare-context] Failed to get history: %s", exc)

        # ---- Realtime Context ----
        # Time-sensitive info placed just BEFORE the last user message to improve LLM caching.
        # This ensures LLM responds to user input, not the realtime context.
        if reqs.realtime_context:
            try:
                realtime_msg = self._build_realtime_context(persona, building_id, messages)
                if realtime_msg:
                    # Find the last user message and insert realtime context before it
                    last_user_idx = None
                    for i in range(len(messages) - 1, -1, -1):
                        if messages[i].get("role") == "user" and not messages[i].get("metadata", {}).get("__realtime_context__"):
                            last_user_idx = i
                            break

                    if last_user_idx is not None:
                        # Insert before last user message
                        messages.insert(last_user_idx, realtime_msg)
                        LOGGER.debug("[sea][prepare-context] Added realtime context before last user message (idx=%d)", last_user_idx)
                    else:
                        # No user message found, append at end
                        messages.append(realtime_msg)
                        LOGGER.debug("[sea][prepare-context] Added realtime context at end (no user message found)")
            except Exception as exc:
                LOGGER.debug("[sea][prepare-context] Failed to build realtime context: %s", exc)

        # ---- Token budget check ----
        try:
            from saiverse.token_estimator import estimate_messages_tokens
            from saiverse.model_configs import get_context_length, get_model_provider

            persona_model = getattr(persona, "model", None)
            if persona_model:
                provider = get_model_provider(persona_model)
                context_length = get_context_length(persona_model)
                estimated_tokens = estimate_messages_tokens(messages, provider)
                LOGGER.debug(
                    "[sea][prepare-context] Token budget: estimated=%d, limit=%d (model=%s)",
                    estimated_tokens, context_length, persona_model,
                )

                if estimated_tokens > context_length:
                    # Over budget: trim history messages from oldest until within budget
                    # Find indices of history messages (not system, not visual context, not realtime)
                    history_indices = []
                    for i, msg in enumerate(messages):
                        meta = msg.get("metadata") or {}
                        if (
                            msg.get("role") != "system"
                            and not meta.get("__visual_context__")
                            and not meta.get("__realtime_context__")
                            and not meta.get("__memory_weave_context__")
                        ):
                            history_indices.append(i)

                    original_count = len(history_indices)
                    # Remove oldest history messages until under budget
                    while history_indices and estimated_tokens > context_length:
                        remove_idx = history_indices.pop(0)
                        removed_msg = messages[remove_idx]
                        removed_tokens = estimate_messages_tokens([removed_msg], provider)
                        estimated_tokens -= removed_tokens
                        messages[remove_idx] = None  # mark for removal

                    # Clean up None entries
                    messages = [m for m in messages if m is not None]
                    remaining_count = len(history_indices)

                    warning_msg = {
                        "type": "warning",
                        "warning_code": "context_auto_trimmed",
                        "content": (
                            f"コンテキスト超過のため、履歴を直近{original_count}件→{remaining_count}件に"
                            f"自動削減しました（推定: {estimated_tokens:,} / {context_length:,}トークン）。"
                            f"ChatOptionsでメッセージ数上限を下げてください。"
                        ),
                    }
                    LOGGER.warning(
                        "[sea][prepare-context] Context auto-trimmed: %d -> %d messages (est=%d, limit=%d)",
                        original_count, remaining_count, estimated_tokens, context_length,
                    )
                    if warnings is not None:
                        warnings.append(warning_msg)

                elif estimated_tokens > context_length * 0.85:
                    # Approaching limit: warn but continue
                    warning_msg = {
                        "type": "warning",
                        "warning_code": "context_approaching_limit",
                        "content": (
                            f"コンテキスト使用量がモデルの上限に近づいています"
                            f"（推定: {estimated_tokens:,} / {context_length:,}トークン）。"
                            f"ChatOptionsでメッセージ数上限を下げることを検討してください。"
                        ),
                    }
                    LOGGER.warning(
                        "[sea][prepare-context] Context approaching limit: est=%d, limit=%d (%.0f%%)",
                        estimated_tokens, context_length, estimated_tokens / context_length * 100,
                    )
                    if warnings is not None:
                        warnings.append(warning_msg)
        except Exception as exc:
            LOGGER.debug("[sea][prepare-context] Token budget check failed: %s", exc)

        return messages

    # ---- Context Preview (read-only, no side effects) ----

    def preview_context(
        self,
        persona: Any,
        building_id: str,
        user_input: str,
        meta_playbook: Optional[str] = None,
        image_count: int = 0,
        document_count: int = 0,
    ) -> Dict[str, Any]:
        """Build the context that would be sent to the LLM, without executing anything.

        Returns a dict with messages, token estimates, cost estimates, and model info.
        Does NOT record the user message to history or call any LLM.
        """
        from saiverse.token_estimator import estimate_messages_tokens, estimate_image_tokens
        from saiverse.model_configs import (
            get_model_provider, get_context_length,
            get_model_display_name, get_model_pricing, calculate_cost,
        )

        # Select playbook (same logic as run_meta_user)
        if meta_playbook:
            playbook = self._load_playbook_for(meta_playbook, persona, building_id)
            if playbook is None:
                playbook = self._choose_playbook(kind="user", persona=persona, building_id=building_id)
        else:
            playbook = self._choose_playbook(kind="user", persona=persona, building_id=building_id)

        # Build context messages (without recording user message to history)
        # Use "conversation" profile requirements to match what sub_speak actually sees,
        # not the meta-playbook's own context_requirements (which may lack memory_weave etc.)
        from sea.playbook_models import CONTEXT_PROFILES
        preview_requirements = CONTEXT_PROFILES["conversation"]["requirements"]
        context_warnings: List[Dict[str, Any]] = []
        messages = self._prepare_context(
            persona, building_id, user_input=None,
            requirements=preview_requirements,
            warnings=context_warnings,
            preview_only=True,
        )

        # Append the user message manually (in real flow it comes from history)
        if user_input:
            messages.append({"role": "user", "content": user_input})

        # Classify each message into a section
        persona_model = getattr(persona, "model", None) or "gemini-2.5-flash-lite-preview-09-2025"
        provider = get_model_provider(persona_model)

        section_order = [
            "system_prompt", "memory_weave_chronicle", "memory_weave_memopedia",
            "memory_weave", "visual_context",
            "history", "realtime_context", "user_message",
        ]
        section_labels = {
            "system_prompt": "System Prompt",
            "memory_weave_chronicle": "Memory Weave — Chronicle",
            "memory_weave_memopedia": "Memory Weave — Memopedia",
            "memory_weave": "Memory Weave",
            "visual_context": "Visual Context",
            "history": "Conversation History",
            "realtime_context": "Realtime Context",
            "user_message": "Your Message",
            "attachments": "Attachments",
        }
        section_tokens: Dict[str, int] = {s: 0 for s in section_order}
        section_tokens["attachments"] = 0
        section_msg_counts: Dict[str, int] = {s: 0 for s in section_order}
        section_msg_counts["attachments"] = 0

        annotated_messages: List[Dict[str, Any]] = []
        for i, msg in enumerate(messages):
            meta = msg.get("metadata") or {}
            # Determine section
            if msg.get("role") == "system":
                section = "system_prompt"
            elif meta.get("__memory_weave_context__"):
                mw_type = meta.get("__memory_weave_type__", "")
                if mw_type == "chronicle":
                    section = "memory_weave_chronicle"
                elif mw_type == "memopedia":
                    section = "memory_weave_memopedia"
                else:
                    section = "memory_weave"
            elif meta.get("__visual_context__"):
                section = "visual_context"
            elif meta.get("__realtime_context__"):
                section = "realtime_context"
            elif i == len(messages) - 1 and msg.get("role") == "user" and user_input and msg.get("content") == user_input:
                section = "user_message"
            else:
                section = "history"

            msg_tokens = estimate_messages_tokens([msg], provider)
            section_tokens[section] += msg_tokens
            section_msg_counts[section] += 1

            annotated_messages.append({
                "role": msg.get("role", "unknown"),
                "content": msg.get("content", ""),
                "section": section,
                "tokens": msg_tokens,
            })

        # Add estimated attachment tokens
        attachment_tokens = 0
        if image_count > 0:
            attachment_tokens += image_count * estimate_image_tokens(provider)
        if document_count > 0:
            # Rough estimate: ~500 tokens per document (varies widely)
            attachment_tokens += document_count * 500
        section_tokens["attachments"] = attachment_tokens

        total_input_tokens = sum(section_tokens.values())
        context_length = get_context_length(persona_model)
        pricing = get_model_pricing(persona_model)

        # Cost range: best case (all cached) to worst case (all cache-write)
        cache_kwargs = self._get_cache_kwargs()
        cache_enabled = cache_kwargs.get("enable_cache", False)
        cache_ttl = cache_kwargs.get("cache_ttl", "5m")

        # Determine cache type (explicit for Anthropic, implicit for Gemini, etc.)
        from saiverse.model_configs import get_cache_config
        cache_config = get_cache_config(persona_model)
        cache_type = cache_config.get("type", "implicit")

        if cache_enabled and pricing and pricing.get("cached_input_per_1m_tokens") is not None:
            # Best case: everything is a cache hit
            cost_best = calculate_cost(
                persona_model, total_input_tokens, 0,
                cached_tokens=total_input_tokens, cache_write_tokens=0,
            )
            # Worst case: everything is a cache write
            cost_worst = calculate_cost(
                persona_model, total_input_tokens, 0,
                cached_tokens=0, cache_write_tokens=total_input_tokens,
                cache_ttl=cache_ttl,
            )
        else:
            # No cache: single estimate
            cost_best = calculate_cost(persona_model, total_input_tokens, 0)
            cost_worst = cost_best

        # Build sections summary
        all_sections = section_order + ["attachments"]
        sections_summary = []
        for s in all_sections:
            if section_tokens.get(s, 0) > 0 or section_msg_counts.get(s, 0) > 0:
                sections_summary.append({
                    "name": s,
                    "label": section_labels.get(s, s),
                    "tokens": section_tokens.get(s, 0),
                    "message_count": section_msg_counts.get(s, 0),
                })

        return {
            "persona_id": getattr(persona, "persona_id", "unknown"),
            "persona_name": getattr(persona, "persona_name", "Unknown"),
            "model": persona_model,
            "model_display_name": get_model_display_name(persona_model),
            "provider": provider,
            "context_length": context_length,
            "sections": sections_summary,
            "total_input_tokens": total_input_tokens,
            "estimated_cost_best_usd": round(cost_best, 6),
            "estimated_cost_worst_usd": round(cost_worst, 6),
            "cache_enabled": cache_enabled,
            "cache_ttl": cache_ttl if cache_enabled else None,
            "cache_type": cache_type if cache_enabled else None,
            "pricing": pricing or {},
            "messages": annotated_messages,
        }

    def _enrich_history_with_attachments(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Enrich history messages with attachment context.

        If a message has metadata with attached items (images/documents),
        append a system note about the created items to help persona understand context.
        """
        enriched = []
        for msg in messages:
            metadata = msg.get("metadata", {})
            if not metadata:
                enriched.append(msg)
                continue

            # Collect attachment info
            attachment_notes = []

            # Check for images with item_name
            images = metadata.get("images", [])
            for img in images:
                item_name = img.get("item_name")
                if item_name:
                    attachment_notes.append(f"画像「{item_name}」")

            # Check for documents with item_name
            documents = metadata.get("documents", [])
            for doc in documents:
                item_name = doc.get("item_name")
                if item_name:
                    attachment_notes.append(f"ドキュメント「{item_name}」")

            if attachment_notes:
                # Append system note to content
                original_content = msg.get("content", "")
                items_str = "、".join(attachment_notes)
                note = f"\n<system>添付アイテム作成: {items_str}</system>"
                enriched_msg = {**msg, "content": original_content + note}
                enriched.append(enriched_msg)
            else:
                enriched.append(msg)

        return enriched

    def _build_realtime_context(
        self,
        persona: Any,
        building_id: str,
        history_messages: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Build realtime context message with time-sensitive information.

        This message is placed near the end of context (before the current prompt)
        to improve LLM context caching efficiency. Time-sensitive info here doesn't
        invalidate the cached prefix (system prompt, persona info, building info, etc.).

        Contents:
        - Current timestamp (year/month/day, weekday, hour:minute)
        - Previous AI response timestamp (for time passage awareness)
        - Spatial info from Unity gateway (if connected)
        - (Future) Auto-recalled memory content

        Returns:
            Message dict with role="user" and <system> wrapper, or None if no content.
        """
        from datetime import datetime

        sections: List[str] = []

        # 1. Current timestamp
        now = datetime.now(persona.timezone)
        weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
        current_time_str = now.strftime(f"%Y年%m月%d日({weekday_names[now.weekday()]}) %H:%M")
        sections.append(f"現在時刻: {current_time_str}")

        # 2. Previous AI response timestamp
        # Find the last assistant/persona message in history with a timestamp
        prev_ai_timestamp = None
        persona_id = getattr(persona, "persona_id", None)
        persona_name = getattr(persona, "persona_name", None)
        for msg in reversed(history_messages):
            role = msg.get("role", "")
            # Check if this is an assistant message or a message from this persona
            if role == "assistant" or (persona_name and msg.get("sender") == persona_name):
                # Try 'created_at' first (SAIMemory format), then 'timestamp' (fallback)
                ts_str = msg.get("created_at") or msg.get("timestamp")
                if ts_str:
                    try:
                        # Handle both ISO format and datetime objects
                        if isinstance(ts_str, datetime):
                            prev_ai_timestamp = ts_str
                        else:
                            prev_ai_timestamp = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                        break
                    except (ValueError, TypeError):
                        pass

        if prev_ai_timestamp:
            # Convert to persona's timezone for display
            if prev_ai_timestamp.tzinfo is not None:
                prev_ai_timestamp = prev_ai_timestamp.astimezone(persona.timezone)
            prev_time_str = prev_ai_timestamp.strftime(f"%Y年%m月%d日({weekday_names[prev_ai_timestamp.weekday()]}) %H:%M")
            sections.append(f"あなたの前回発言: {prev_time_str}")

        # 3. Spatial context (Unity gateway)
        try:
            unity_gateway = getattr(self.manager, "unity_gateway", None)
            if unity_gateway and getattr(unity_gateway, "is_running", False):
                spatial_state = unity_gateway.spatial_state.get(persona_id) if persona_id else None
                if spatial_state:
                    distance = getattr(spatial_state, "distance_to_player", None)
                    is_visible = getattr(spatial_state, "is_visible", None)

                    spatial_lines = []
                    if distance is not None:
                        spatial_lines.append(f"プレイヤーとの距離: {distance:.1f}m")
                    if is_visible is not None:
                        visibility_text = "見える" if is_visible else "見えない"
                        spatial_lines.append(f"プレイヤーの視認: {visibility_text}")

                    if spatial_lines:
                        sections.append("空間情報: " + " / ".join(spatial_lines))
                        LOGGER.debug("[sea][realtime-context] Added spatial info: distance=%.1f, visible=%s", distance, is_visible)
        except Exception as exc:
            LOGGER.debug("[sea][realtime-context] Failed to get spatial context: %s", exc)

        if not sections:
            return None

        # Format as user message with <system> wrapper (compatible with all LLM providers)
        content = "<system>\n## リアルタイム情報\n" + "\n".join(f"- {s}" for s in sections) + "\n</system>"
        return {
            "role": "user",
            "content": content,
            "metadata": {"__realtime_context__": True},  # Mark for identification
        }

    def _choose_playbook(self, kind: str, persona: Any, building_id: str) -> PlaybookSchema:
        """Resolve playbook by kind with DB→disk→fallback."""
        candidates = ["meta_user" if kind == "user" else "meta_auto", "basic_chat"]
        for name in candidates:
            pb = self._load_playbook_for(name, persona, building_id)
            if pb:
                return pb
        return self._basic_chat_playbook()

    def _basic_chat_playbook(self) -> PlaybookSchema:
        return PlaybookSchema(
            name="basic_chat",
            description="No-op fallback for simple conversations handled by meta layer",
            input_schema=[{"name": "input", "description": "User or system input"}],
            nodes=[
                {
                    "id": "noop",
                    "type": "pass",
                    "next": None,
                },
            ],
            start_node="noop",
        )

    # playbook loading helpers -----------------------------------------
    def _load_playbook_for(self, name: str, persona: Any, building_id: str) -> Optional[PlaybookSchema]:
        pb = self._load_playbook_from_db(name, persona, building_id)
        if not pb:
            LOGGER.warning("[sea] playbook '%s' not found in DB (persona=%s building=%s)", name, getattr(persona, "persona_id", None), building_id)
        return pb

    def _visible(self, model: PlaybookModel, persona: Any, building_id: str) -> bool:
        scope = (model.scope or "public").lower()
        if scope == "public":
            return True
        if scope == "personal":
            return model.created_by_persona_id == getattr(persona, "persona_id", None)
        if scope == "building":
            return model.building_id == building_id
        return False

    def _load_playbook_from_db(self, name: str, persona: Any, building_id: str) -> Optional[PlaybookSchema]:
        session_maker = getattr(self.manager, "SessionLocal", None)
        if session_maker is None:
            return None
        try:
            session = session_maker()
        except Exception:
            return None
        try:
            try:
                rec = (
                    session.query(PlaybookModel)
                    .filter(PlaybookModel.name == name)
                    .first()
                )
            except Exception:
                LOGGER.debug("Playbook table not ready; skipping DB load")
                return None
            if not rec or not self._visible(rec, persona, building_id):
                return None
            # dev_only playbooks require developer mode
            if getattr(rec, "dev_only", False):
                dev_mode = False
                if self.manager and hasattr(self.manager, "state"):
                    dev_mode = getattr(self.manager.state, "developer_mode", False)
                if not dev_mode:
                    LOGGER.debug("[sea] playbook '%s' is dev_only but developer mode is off", name)
                    return None
            try:
                data = json.loads(rec.nodes_json)
                pb = PlaybookSchema(**data)
                validate_playbook_graph(pb)
                LOGGER.debug("[sea] Loaded playbook '%s' with %d input_schema params: %s", pb.name, len(pb.input_schema), [p.name for p in pb.input_schema])
                self._debug_playbook(pb, source="db")
                return pb
            except PlaybookValidationError as exc:
                LOGGER.error("[sea] playbook %s failed validation: %s", name, exc)
                return None
            except Exception:
                LOGGER.exception("Failed to parse playbook %s from DB", name)
                return None
        finally:
            session.close()

    # Disk fallbackを無効化（バグ隠し防止のため）
    def _load_playbook_from_disk(self, name: str) -> Optional[PlaybookSchema]:
        return None
