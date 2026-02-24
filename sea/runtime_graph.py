from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from llm_clients.exceptions import LLMError
from sea.cancellation import CancellationToken, ExecutionCancelledException
from sea.langgraph_runner import compile_playbook
from sea.playbook_models import PlaybookSchema

LOGGER = logging.getLogger(__name__)

def compile_with_langgraph(
    runtime,
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
    temperature = runtime._default_temperature(persona)
    parent = parent_state or {}

    # Update execution state: playbook started (LangGraph path)
    if hasattr(persona, "execution_state"):
        persona.execution_state["playbook"] = playbook.name
        persona.execution_state["node"] = playbook.start_node
        persona.execution_state["status"] = "running"

    compiled = compile_playbook(
        playbook,
        llm_node_factory=lambda node_def: runtime._lg_llm_node(node_def, persona, building_id, playbook, event_callback),
        tool_node_factory=lambda node_def: runtime._lg_tool_node(node_def, persona, playbook, event_callback, auto_mode=auto_mode),
        tool_call_node_factory=lambda node_def: runtime._lg_tool_call_node(node_def, persona, playbook, event_callback, auto_mode=auto_mode),
        speak_node=lambda state: runtime._lg_speak_node(state, persona, building_id, playbook, _lg_outputs, event_callback),
        think_node=lambda state: runtime._lg_think_node(state, persona, playbook, _lg_outputs, event_callback),
        say_node_factory=lambda node_def: runtime._lg_say_node(node_def, persona, building_id, playbook, _lg_outputs, event_callback),
        memorize_node_factory=lambda node_def: runtime._lg_memorize_node(node_def, persona, playbook, _lg_outputs, event_callback),
        exec_node_factory=lambda node_def: runtime._lg_exec_node(node_def, playbook, persona, building_id, auto_mode, _lg_outputs, event_callback),
        subplay_node_factory=lambda node_def: runtime._lg_subplay_node(node_def, persona, building_id, playbook, auto_mode, _lg_outputs, event_callback),
        set_node_factory=lambda node_def: runtime._lg_set_node(node_def, playbook, event_callback),
        stelis_start_node_factory=lambda node_def: runtime._lg_stelis_start_node(node_def, persona, playbook, event_callback),
        stelis_end_node_factory=lambda node_def: runtime._lg_stelis_end_node(node_def, persona, playbook, event_callback),
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

    # Process input_schema to inherit variables from parent_state.
    # source_key (from param.source) is the primary resolution mechanism.
    # param_name in parent is a fallback for when source_key yields nothing.
    inherited_vars = {}
    for param in playbook.input_schema:
        param_name = param.name
        source_key = param.source if param.source else "input"

        # Primary: resolve based on source_key
        if source_key == "input":
            value = user_input or ""
        elif source_key.startswith("parent."):
            actual_key = source_key[7:]  # strip "parent."
            value = runtime._resolve_state_value(parent, actual_key)
            if value is None:
                value = ""
            LOGGER.debug("[sea][LangGraph] Resolved %s from parent.%s: %s", param_name, actual_key, str(value)[:120] if value else "(empty)")
        else:
            value = parent.get(source_key, "")

        # Fallback: if source_key resolution yielded nothing, check parent by param name
        if not value and param_name in parent and parent[param_name] is not None:
            value = parent[param_name]
            LOGGER.debug("[sea][LangGraph] Fallback: using parent value for %s: %s", param_name, str(value)[:120] if value else "(empty)")

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
                    runtime._store_structured_result(parent_state, key, value)
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
