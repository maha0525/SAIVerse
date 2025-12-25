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

from sea.playbook_models import NodeType, PlaybookSchema, PlaybookValidationError, validate_playbook_graph
from sea.langgraph_runner import compile_playbook
from sea.cancellation import CancellationToken, ExecutionCancelledException
from database.models import Playbook as PlaybookModel
from model_configs import get_model_parameter_defaults

LOGGER = logging.getLogger(__name__)


def _get_default_lightweight_model() -> str:
    """Get the default lightweight model from environment or fallback."""
    return os.getenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gemini-2.5-flash-lite")


def _format(template: str, variables: Dict[str, Any]) -> str:
    try:
        return template.format(**variables)
    except Exception:
        # 安全側でそのまま返す
        return template


class SEARuntime:
    """Lightweight executor for meta playbooks until full LangGraph port."""

    def __init__(self, manager_ref: Any):
        self.manager = manager_ref
        self.playbooks_dir = Path(__file__).parent / "playbooks"
        self._playbook_cache: Dict[str, PlaybookSchema] = {}
        self._dump_path = os.getenv("SAIVERSE_SEA_DUMP")  # set to a filepath to capture LLM I/O
        self._trace = bool(os.getenv("SAIVERSE_SEA_TRACE"))

    # ---------------- meta entrypoints -----------------
    def run_meta_user(
        self,
        persona,
        user_input: str,
        building_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
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
    ) -> List[str]:
        # Check for cancellation at start
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        
        # Generate or inherit pulse_id
        parent = parent_state or {}
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
        base_messages = self._prepare_context(persona, building_id, user_input, playbook.context_requirements, pulse_id=pulse_id)
        LOGGER.info("[sea][run-playbook] %s: _prepare_context returned %d messages", playbook.name, len(base_messages))
        conversation_msgs = list(base_messages)

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
            llm_node_factory=lambda node_def: self._lg_llm_node(node_def, persona, playbook, event_callback),
            tool_node_factory=lambda node_def: self._lg_tool_node(node_def, persona, playbook, event_callback),
            speak_node=lambda state: self._lg_speak_node(state, persona, building_id, playbook, _lg_outputs, event_callback),
            think_node=lambda state: self._lg_think_node(state, persona, playbook, _lg_outputs, event_callback),
            say_node_factory=lambda node_def: self._lg_say_node(node_def, persona, building_id, playbook, _lg_outputs, event_callback),
            memorize_node_factory=lambda node_def: self._lg_memorize_node(node_def, persona, playbook, _lg_outputs, event_callback),
            exec_node_factory=lambda node_def: self._lg_exec_node(node_def, playbook, persona, building_id, auto_mode, _lg_outputs, event_callback),
            subplay_node_factory=lambda node_def: self._lg_subplay_node(node_def, persona, building_id, playbook, auto_mode, _lg_outputs, event_callback),
            set_node_factory=lambda node_def: self._lg_set_node(node_def, playbook, event_callback),
        )
        if not compiled:
            # Update execution state: compilation failed, reset to idle
            if hasattr(persona, "execution_state"):
                persona.execution_state["playbook"] = None
                persona.execution_state["node"] = None
                persona.execution_state["status"] = "idle"
            return None

        # Process input_schema to inherit variables from parent_state
        inherited_vars = {}
        for param in playbook.input_schema:
            param_name = param.name
            source_key = param.source if param.source else "input"

            # Resolve value from parent_state or fallback
            if source_key.startswith("parent."):
                actual_key = source_key[7:]  # strip "parent."
                value = parent.get(actual_key, "")
                LOGGER.debug("[sea][LangGraph] Resolved %s from parent.%s: %s", param_name, actual_key, str(value)[:200] if value else "(empty)")
            elif source_key == "input":
                value = user_input or ""
            else:
                value = parent.get(source_key, "")

            inherited_vars[param_name] = value

        initial_state = {
            "messages": list(base_messages),
            "inputs": {"input": user_input or ""},
            "context": {},
            "last": user_input or "",
            "outputs": _lg_outputs,
            "persona_obj": persona,
            "context_bundle": [],
            "context_bundle_text": "",
            "pulse_id": pulse_id,
            "pulse_type": pulse_type,  # user/schedule/auto
            "_cancellation_token": cancellation_token,  # For node-level cancellation checks
            **inherited_vars,  # Add inherited variables from input_schema
        }

        # Execute compiled playbook
        # Set recursion limit high enough for agentic loops (default is 25, too low for multi-step agents)
        langgraph_config = {"recursion_limit": 100}

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
        except Exception:
            LOGGER.exception("SEA LangGraph execution failed")
            # Update execution state: execution failed, reset to idle
            if hasattr(persona, "execution_state"):
                persona.execution_state["playbook"] = None
                persona.execution_state["node"] = None
                persona.execution_state["status"] = "idle"
            return None

        # Write back state variables to parent_state based on output_schema
        if parent_state is not None and isinstance(final_state, dict) and playbook.output_schema:
            for key in playbook.output_schema:
                if key in final_state:
                    parent_state[key] = final_state[key]
                    LOGGER.debug("[sea][LangGraph] Propagated %s to parent_state: %s", key, str(final_state[key])[:200])

        # Update execution state: playbook completed (LangGraph path)
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"

        # speak/think nodes already emitted; return collected texts for UI consistency
        return list(_lg_outputs)

    def _lg_llm_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
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
            variables = {
                "input": state.get("inputs", {}).get("input", ""),
                "last": state.get("last", ""),
                "persona_id": getattr(persona, "persona_id", None),
                "persona_name": getattr(persona, "persona_name", None),
                "context_bundle_text": state.get("context_bundle_text", ""),
                **{k: v for k, v in state.items() if k not in ["messages", "inputs", "outputs", "persona_obj", "_cancellation_token"]},  # Include all other state variables
            }
            text = ""
            schema_consumed = False
            try:
                base_msgs = state.get("messages", [])
                action_template = getattr(node_def, "action", None)
                if action_template:
                    prompt = _format(action_template, variables)
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

                # Check if tools are available for this node
                available_tools = getattr(node_def, "available_tools", None)
                if available_tools:
                    # Tool calling mode
                    tools_spec = self._build_tools_spec(available_tools, llm_client)
                    result = llm_client.generate_with_tool_detection(
                        messages,
                        tools=tools_spec,
                        temperature=self._default_temperature(persona),
                    )

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

                    import json

                    if result["type"] == "tool_call":
                        # Only tool call, no text
                        if output_keys_spec:
                            # New behavior: use explicit output_keys
                            if function_call_key:
                                state[f"{function_call_key}.name"] = result["tool_name"]
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
                        # Both text and tool call
                        if output_keys_spec:
                            # New behavior: use explicit output_keys
                            if text_key:
                                state[text_key] = result["content"]
                                LOGGER.debug("[sea] Stored %s = (text, length=%d)", text_key, len(result["content"]))
                            if function_call_key:
                                state[f"{function_call_key}.name"] = result["tool_name"]
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
                        # Normal text response
                        if output_keys_spec and text_key:
                            # New behavior: store in explicit text_key
                            state[text_key] = result["content"]
                            LOGGER.debug("[sea] Stored %s = (text, length=%d)", text_key, len(result["content"]))
                            state["has_speak_content"] = True
                        else:
                            # Legacy behavior: no specific text storage (just in "last")
                            state["has_speak_content"] = True

                        state["tool_called"] = False
                        text = result["content"]

                    self._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
                else:
                    # Normal mode (no tools)
                    state["tool_called"] = False
                    text = llm_client.generate(
                        messages,
                        tools=[],
                        temperature=self._default_temperature(persona),
                        response_schema=response_schema,
                    )
                    self._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
                    schema_consumed = self._process_structured_output(node_def, text, state)
            except Exception as exc:
                LOGGER.error("SEA LangGraph LLM failed: %s: %s", type(exc).__name__, exc)
                text = "(error in llm node)"
                state["tool_called"] = False
            state["last"] = text
            state["messages"] = messages + [{"role": "assistant", "content": text}]

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

    def _select_llm_client(self, node_def: Any, persona: Any, needs_structured_output: bool = False) -> Any:
        """Select the appropriate LLM client based on node's model_type and structured output needs.

        Args:
            node_def: Node definition from playbook
            persona: Persona object
            needs_structured_output: Whether this node requires structured output
        """
        model_type = getattr(node_def, "model_type", "normal") or "normal"
        LOGGER.info("[sea] Node model_type: %s (node_id=%s)", model_type, getattr(node_def, "id", "unknown"))

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
                    from model_configs import get_context_length, get_model_provider
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

        # If structured output is needed, check if the selected model supports it
        if needs_structured_output:
            from model_configs import supports_structured_output, get_agentic_model, get_context_length, get_model_provider
            if not supports_structured_output(base_model):
                # Model doesn't support structured output, switch to agentic model
                agentic_model = get_agentic_model()
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

        if client_class_name in ("OpenAIClient", "AnthropicClient", "OllamaClient", "NvidiaNIMClient"):
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
                    LOGGER.info("[sea] - Gemini function_declaration: name=%s, description=%s", decl.name, decl.description[:100] if decl.description else None)
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
        if not self._dump_path:
            return
        try:
            entry = {
                "playbook": playbook_name,
                "node": node_id,
                "persona_id": getattr(persona, "persona_id", None),
                "persona_name": getattr(persona, "persona_name", None),
                "messages": messages,
                "output": output_text,
            }
            Path(self._dump_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._dump_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False))
                f.write("\n")
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
        parsed = self._extract_structured_json(text)
        if parsed is None:
            LOGGER.warning("[sea] structured output parse failed for node %s", getattr(node_def, "id", "?"))
            return False
        key = getattr(node_def, "output_key", None) or getattr(node_def, "id", "") or "node"
        self._store_structured_result(state, key, parsed)

        # Apply output_mapping if defined
        output_mapping = getattr(node_def, "output_mapping", None)
        if output_mapping:
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
        for source_path, target_key in mapping.items():
            # Source path can be either:
            # 1. Absolute path like "router.playbook" (starts with output_key)
            # 2. Relative path like "playbook" (within the output_key namespace)
            if source_path.startswith(f"{output_key}."):
                # Already absolute path
                full_path = source_path
            else:
                # Relative path, prepend output_key
                full_path = f"{output_key}.{source_path}"

            value = state.get(full_path)
            if value is not None:
                state[target_key] = value
                LOGGER.debug("[sea] output_mapping: %s -> %s = %s", full_path, target_key, str(value)[:100])

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
            for idx, item in enumerate(value):
                new_prefix = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
                result.update(self._flatten_dict(item, new_prefix))
        else:
            result[prefix or "value"] = value
        return result

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
            pass

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

    def _lg_tool_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
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
            try:
                persona_dir = getattr(persona_obj, "persona_log_path", None)
                persona_dir = persona_dir.parent if persona_dir else Path.cwd()
                persona_id = getattr(persona_obj, "persona_id", None)
                manager_ref = getattr(persona_obj, "manager_ref", None)

                # Build kwargs from args_input (None or {} = no args)
                # Supports nested keys via dot notation (e.g., "tool_call.args.playbook_name")
                kwargs = {}
                if args_input:
                    for arg_name, source in args_input.items():
                        if isinstance(source, str):
                            value = state.get(source, "")
                            LOGGER.debug("[sea][tool] Mapping arg '%s' <- state['%s'] = %s", arg_name, source, value)
                        else:
                            value = source
                            LOGGER.debug("[sea][tool] Using literal arg '%s' = %s", arg_name, value)
                        kwargs[arg_name] = value

                # Execute tool with persona context
                if persona_id and persona_dir:
                    with persona_context(persona_id, persona_dir, manager_ref):
                        result = tool_func(**kwargs) if callable(tool_func) else None
                else:
                    result = tool_func(**kwargs) if callable(tool_func) else None

                # Handle tuple results with output_keys (for multi-value returns)
                if output_keys and isinstance(result, tuple):
                    # Expand tuple to multiple state variables
                    for i, key in enumerate(output_keys):
                        if i < len(result):
                            state[key] = result[i]
                            LOGGER.debug("[sea][LangGraph] Stored tuple[%d] in state[%s]: %s", i, key, str(result[i])[:200])
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

            try:
                sub_outputs = await asyncio.to_thread(
                    self._run_playbook, sub_pb, persona, building_id, sub_input, auto_mode, True, state, event_callback
                )
            except Exception as exc:
                LOGGER.exception("SEA LangGraph exec sub-playbook failed")
                state["last"] = f"Sub-playbook error: {exc}"
                if outputs is not None:
                    outputs.append(state["last"])
                return state

            # Track executed playbook in executed_playbooks list
            executed_list = state.get("executed_playbooks")
            if isinstance(executed_list, list):
                executed_list.append(str(sub_name).strip())
                LOGGER.debug("[sea][exec] Added '%s' to executed_playbooks: %s", sub_name, executed_list)

            ingested = self._ingest_context_from_subplaybook(state, sub_name, sub_outputs)
            if ingested:
                state["last"] = state.get("context_bundle_text") or state.get("last")
            elif sub_outputs:
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
            variables.update({
                "input": state.get("inputs", {}).get("input", ""),
                "last": state.get("last", ""),
                "persona_id": getattr(persona, "persona_id", None),
                "persona_name": getattr(persona, "persona_name", None),
            })
            memo_text = _format(getattr(node_def, "action", None) or "{last}", variables)
            role = getattr(node_def, "role", "assistant") or "assistant"
            tags = getattr(node_def, "tags", None)
            pulse_id = state.get("pulse_id")
            metadata_key = getattr(node_def, "metadata_key", None)
            metadata = state.get(metadata_key) if metadata_key else None
            self._store_memory(persona, memo_text, role=role, tags=tags, pulse_id=pulse_id, metadata=metadata)
            state["last"] = memo_text
            if outputs is not None and self._should_collect_memory_output(playbook):
                outputs.append(memo_text)
            return state

        return node

    def _lg_speak_node(self, state: dict, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        # Send status event for node execution
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / speak", "playbook": playbook.name, "node": "speak"})
        text = state.get("last") or ""
        pulse_id = state.get("pulse_id")
        self._emit_speak(persona, building_id, text, pulse_id=pulse_id)
        if outputs is not None:
            outputs.append(text)
        if event_callback:
            event_callback({"type": "say", "content": text, "persona_id": getattr(persona, "persona_id", None)})
        return state

    def _lg_say_node(self, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        async def node(state: dict):
            # Send status event for node execution
            node_id = getattr(node_def, "id", "say")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            text = state.get("last") or ""
            pulse_id = state.get("pulse_id")
            metadata_key = getattr(node_def, "metadata_key", None)
            metadata = state.get(metadata_key) if metadata_key else None
            self._emit_say(persona, building_id, text, pulse_id=pulse_id, metadata=metadata)
            if outputs is not None:
                outputs.append(text)
            if event_callback:
                event_callback({"type": "say", "content": text, "persona_id": getattr(persona, "persona_id", None), "metadata": metadata})
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

            # Execute subplaybook
            # Note: We call _run_playbook directly (not via asyncio.to_thread) to keep
            # SQLite connections on the same thread. _run_playbook handles its own
            # async/sync boundary internally via ThreadPoolExecutor.
            try:
                sub_outputs = self._run_playbook(sub_pb, persona, building_id, sub_input, auto_mode, True, state, event_callback)
            except Exception as exc:
                LOGGER.exception("[sea][subplay] Failed to execute subplaybook '%s'", sub_name)
                state["last"] = f"Sub-playbook error: {exc}"
                return state
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
            for key, value_template in assignments.items():
                resolved_value = self._resolve_set_value(value_template, state)
                state[key] = resolved_value
                LOGGER.debug("[sea][set] %s = %s", key, resolved_value)

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
        - Template strings with {var} placeholders: expanded with state values
        - Arithmetic expressions like "{count} + 1": evaluated safely
        """
        # Literal values
        if isinstance(value_template, (int, float, bool, type(None))):
            return value_template

        if not isinstance(value_template, str):
            return value_template

        # Check if it looks like an arithmetic expression
        # Pattern: contains operators and {var} placeholders
        if any(op in value_template for op in ["+", "-", "*", "/", "%"]):
            return self._eval_arithmetic_expression(value_template, state)

        # Simple template expansion
        try:
            return _format(value_template, state)
        except Exception:
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

    def _ingest_context_from_subplaybook(
        self,
        state: Dict[str, Any],
        source: str,
        sub_outputs: Optional[List[str]],
    ) -> bool:
        bundle = state.setdefault("context_bundle", [])
        state.setdefault("context_bundle_text", "")
        if not sub_outputs:
            return False
        joined = "\n".join(str(item).strip() for item in sub_outputs if str(item).strip())
        if not joined:
            return False
        entry: Dict[str, Any] = {"source": source, "raw": joined}
        parsed = self._extract_structured_json(joined)
        if parsed is not None:
            entry["data"] = parsed
        bundle.append(entry)
        state["context_bundle_text"] = self._render_context_bundle(bundle)
        self._append_tool_result_message(state, source, joined)
        return True

    def _render_context_bundle(self, bundle: List[Dict[str, Any]]) -> str:
        blocks: List[str] = []
        for idx, entry in enumerate(bundle, 1):
            label = entry.get("source") or f"context_{idx}"
            payload = entry.get("raw") or ""
            data = entry.get("data")
            if isinstance(data, (dict, list)):
                try:
                    payload = json.dumps(data, ensure_ascii=False)
                except Exception:
                    payload = str(data)
            payload = str(payload).strip()
            blocks.append(f"[{label}]\n{payload}" if payload else f"[{label}]")
        return "\n\n".join(blocks)

    def _should_collect_memory_output(self, playbook: PlaybookSchema) -> bool:
        return not playbook.name.startswith("meta_")

    def _store_memory(
        self,
        persona: Any,
        text: str,
        *,
        role: str = "assistant",
        tags: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not text:
            return
        adapter = getattr(persona, "sai_memory", None)
        try:
            if adapter and adapter.is_ready():
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
                adapter.append_persona_message(message)
        except Exception:
            LOGGER.debug("memorize node not stored", exc_info=True)

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
    def _emit_speak(self, persona: Any, building_id: str, text: str, pulse_id: Optional[str] = None, record_history: bool = True) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        # Build metadata with tags and conversation partners
        metadata: Dict[str, Any] = {"tags": ["conversation"]}
        if pulse_id:
            metadata["tags"].append(f"pulse:{pulse_id}")
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
            LOGGER.debug("think message not stored", exc_info=True)

    def _prepare_context(self, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None) -> List[Dict[str, Any]]:
        from sea.playbook_models import ContextRequirements

        # Use provided requirements or default to full context
        reqs = requirements if requirements else ContextRequirements()

        messages: List[Dict[str, Any]] = []

        # ---- system prompt ----
        if reqs.system_prompt:
            system_sections: List[str] = []

            # 1. Common prompt (world setting, framework explanation)
            common_prompt_template = getattr(persona, "common_prompt", None)
            if common_prompt_template:
                try:
                    # Get building info for variable expansion
                    building_obj = getattr(persona, "buildings", {}).get(building_id)
                    building_name = building_obj.name if building_obj else building_id
                    city_name = getattr(persona, "current_city_id", "unknown_city")

                    # Expand variables in common prompt
                    common_text = common_prompt_template.format(
                        current_persona_name=getattr(persona, "persona_name", "Unknown"),
                        current_persona_id=getattr(persona, "persona_id", "unknown_id"),
                        current_building_name=building_name,
                        current_city_name=city_name,
                        current_persona_system_instruction=getattr(persona, "persona_system_instruction", ""),
                        current_building_system_instruction=getattr(building_obj, "system_instruction", "") if building_obj else "",
                    )
                    system_sections.append(common_text.strip())
                except Exception as exc:
                    LOGGER.debug("Failed to format common prompt: %s", exc)

            # 2. "## あなたについて" section
            persona_section_parts: List[str] = []
            persona_sys = getattr(persona, "persona_system_instruction", "") or ""
            if persona_sys:
                persona_section_parts.append(persona_sys.strip())

            # persona inventory
            if reqs.inventory:
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
            try:
                building_obj = getattr(persona, "buildings", {}).get(building_id)
                if building_obj:
                    building_section_parts: List[str] = []

                    # Building system instruction (with variable expansion)
                    building_sys = getattr(building_obj, "system_instruction", None)
                    if building_sys:
                        # Get current time in persona's timezone
                        from datetime import datetime
                        now = datetime.now(persona.timezone)
                        time_vars = {
                            "current_time": now.strftime("%H:%M"),
                            "current_date": now.strftime("%Y年%m月%d日"),
                            "current_datetime": now.strftime("%Y年%m月%d日 %H:%M"),
                            "current_weekday": ["月", "火", "水", "木", "金", "土", "日"][now.weekday()],
                        }
                        # Expand variables in building system instruction
                        expanded_sys = _format(str(building_sys), time_vars)
                        building_section_parts.append(expanded_sys.strip())

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
                            pass

                    if building_section_parts:
                        building_name = getattr(building_obj, "name", building_id)
                        system_sections.append(f"## {building_name}\n" + "\n\n".join(building_section_parts))
            except Exception:
                pass

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

            system_text = "\n\n---\n\n".join([s for s in system_sections if s])
            if system_text:
                messages.append({"role": "system", "content": system_text})

        # ---- visual context (Building / Persona images) ----
        # Inserted right after system prompt but before conversation history
        if reqs.visual_context:
            try:
                from tools.defs.get_visual_context import get_visual_context
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
                    # Determine character count limit
                    if history_depth == "full":
                        char_limit = getattr(persona, "context_length", 2000)
                    else:
                        try:
                            char_limit = int(history_depth)
                        except (ValueError, TypeError):
                            char_limit = 2000  # fallback

                    # Determine which tags to include
                    required_tags = ["conversation"]
                    if reqs.include_internal:
                        required_tags.append("internal")

                    LOGGER.debug("[sea][prepare-context] Fetching history: char_limit=%d, pulse_id=%s, balanced=%s, tags=%s", char_limit, pulse_id, reqs.history_balanced, required_tags)

                    if reqs.history_balanced:
                        # Get conversation partners for balanced retrieval
                        participant_ids = ["user"]
                        occupants = self.manager.occupants.get(building_id, [])
                        persona_id = getattr(persona, "persona_id", None)
                        for oid in occupants:
                            if oid != persona_id:
                                participant_ids.append(oid)
                        LOGGER.debug("[sea][prepare-context] Balancing across: %s", participant_ids)
                        recent = history_mgr.get_recent_history_balanced(
                            char_limit,
                            participant_ids,
                            required_tags=required_tags,
                            pulse_id=pulse_id,
                        )
                    else:
                        # Filter by required tags or current pulse_id
                        recent = history_mgr.get_recent_history(
                            char_limit,
                            required_tags=required_tags,
                            pulse_id=pulse_id,
                        )
                    LOGGER.debug("[sea][prepare-context] Got %d history messages", len(recent))
                    messages.extend(recent)
                except Exception as exc:
                    LOGGER.exception("[sea][prepare-context] Failed to get history: %s", exc)

        return messages

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
