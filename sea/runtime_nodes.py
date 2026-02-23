from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from llm_clients.exceptions import LLMError
from saiverse.logging_config import log_sea_trace

LOGGER = logging.getLogger(__name__)


def lg_tool_call_node(runtime: Any, node_def: Any, persona: Any, playbook: Any, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, auto_mode: bool = False):
    from tools import TOOL_REGISTRY
    from tools.context import persona_context

    call_source = getattr(node_def, "call_source", "fc") or "fc"
    output_key = getattr(node_def, "output_key", None)

    async def node(state: dict):
        cancellation_token = state.get("_cancellation_token")
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        node_id = getattr(node_def, "id", "tool_call")
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})

        tool_name = runtime._resolve_state_value(state, f"{call_source}.name")
        tool_args = runtime._resolve_state_value(state, f"{call_source}.args")
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
            if not playbook.name.startswith(("meta_", "sub_")):
                pb_display = playbook.display_name or playbook.name
                _at = state.get("_activity_trace")
                if isinstance(_at, list):
                    _at.append({"action": "tool_call", "name": tool_name, "playbook": pb_display})
                if event_callback:
                    event_callback({"type": "activity", "action": "tool_call", "name": tool_name, "playbook": pb_display, "status": "completed", "persona_id": getattr(persona, "persona_id", None), "persona_name": getattr(persona, "persona_name", None)})
            state["last"] = result_str
            if output_key:
                state[output_key] = result

            # Save tool call ID before _append_tool_result_message clears it
            _tc_id_for_im = state.get("_last_tool_call_id")

            # Append tool result to conversation messages (function calling protocol)
            runtime._append_tool_result_message(state, tool_name, result_str)

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
            runtime._append_tool_result_message(state, tool_name, error_msg)

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


def lg_exec_node(runtime: Any, node_def: Any, playbook: Any, persona: Any, building_id: str, auto_mode: bool, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
    return runtime._runtime_engine.lg_exec_node(node_def, playbook, persona, building_id, auto_mode, outputs, event_callback)


def lg_subplay_node(runtime: Any, node_def: Any, persona: Any, building_id: str, playbook: Any, auto_mode: bool, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
    async def node(state: dict):
        cancellation_token = state.get("_cancellation_token")
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        node_id = getattr(node_def, "id", "subplay")
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
        sub_name = getattr(node_def, "playbook", None) or getattr(node_def, "action", None)
        if not sub_name:
            state["last"] = "(sub-playbook missing name)"
            return state
        sub_pb = runtime._load_playbook_for(sub_name, persona, building_id)
        if not sub_pb:
            state["last"] = f"Sub-playbook {sub_name} not found"
            return state
        from .runtime_utils import _format as runtime_format

        template = getattr(node_def, "input_template", "{input}") or "{input}"
        variables = dict(state)
        variables.update({"input": state.get("inputs", {}).get("input", ""), "last": state.get("last", "")})
        sub_input = runtime_format(template, variables)
        eff_bid = runtime._effective_building_id(persona, building_id)
        execution = getattr(node_def, "execution", "inline") or "inline"
        subagent_thread_id = None
        subagent_parent_id = None
        if execution == "subagent":
            subagent_thread_id, subagent_parent_id = runtime._start_subagent_thread(persona, label=f"Subagent: {sub_name}")
            if not subagent_thread_id:
                LOGGER.warning("[sea][subplay] Failed to start subagent thread for '%s', falling back to inline", sub_name)
                execution = "inline"
            else:
                log_sea_trace(playbook.name, node_id, "SUBPLAY", f"→ {sub_name} [subagent thread={subagent_thread_id}] (input=\"{str(sub_input)}\")")
        if execution == "inline":
            log_sea_trace(playbook.name, node_id, "SUBPLAY", f"→ {sub_name} (input=\"{str(sub_input)}\")")
        try:
            sub_outputs = runtime._run_playbook(sub_pb, persona, eff_bid, sub_input, auto_mode, True, state, event_callback, cancellation_token=cancellation_token)
        except LLMError:
            LOGGER.exception("[sea][subplay] LLM error in subplaybook '%s'", sub_name)
            if execution == "subagent" and subagent_thread_id:
                runtime._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=False)
            raise
        except Exception as exc:
            LOGGER.exception("[sea][subplay] Failed to execute subplaybook '%s'", sub_name)
            if execution == "subagent" and subagent_thread_id:
                runtime._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=False)
            state["last"] = f"Sub-playbook error: {exc}"
            return state
        if execution == "subagent" and subagent_thread_id:
            gen_chronicle = getattr(node_def, "subagent_chronicle", True)
            chronicle = runtime._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=gen_chronicle)
            state["_subagent_chronicle"] = chronicle or ""
            log_sea_trace(playbook.name, node_id, "SUBPLAY", f"← {sub_name} [subagent ended, chronicle={'yes' if chronicle else 'no'}]")
        last_text = sub_outputs[-1] if sub_outputs else ""
        state["last"] = last_text
        if getattr(node_def, "propagate_output", False) and sub_outputs and outputs is not None:
            outputs.extend(sub_outputs)
        return state

    return node


def lg_stelis_start_node(runtime: Any, node_def: Any, persona: Any, playbook: Any, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
    import time
    from .runtime_utils import _format as runtime_format

    async def node(state: dict):
        cancellation_token = state.get("_cancellation_token")
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        node_id = getattr(node_def, "id", "stelis_start")
        label = runtime_format(getattr(node_def, "label", None) or "Stelis Session", state)
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
        stelis_config = getattr(node_def, "stelis_config", None) or {}
        if hasattr(stelis_config, "__dict__"):
            stelis_config = {"window_ratio": getattr(stelis_config, "window_ratio", 0.8), "max_depth": getattr(stelis_config, "max_depth", 3), "chronicle_prompt": getattr(stelis_config, "chronicle_prompt", None)}
        window_ratio = stelis_config.get("window_ratio", 0.8)
        max_depth = stelis_config.get("max_depth", 3)
        chronicle_prompt = stelis_config.get("chronicle_prompt")
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            LOGGER.warning("[stelis] No memory adapter found for persona %s", persona.persona_id)
            state["stelis_error"] = "No memory adapter available"
            state["stelis_available"] = False
            return state
        if not memory_adapter.can_start_stelis(max_depth=max_depth):
            error_msg = f"Stelis max depth exceeded (max={max_depth})"
            LOGGER.warning("[stelis] %s for persona %s", error_msg, persona.persona_id)
            state["stelis_error"] = error_msg
            state["stelis_available"] = False
            return state
        parent_thread_id = memory_adapter.get_current_thread() or memory_adapter._thread_id(None)
        stelis = memory_adapter.start_stelis_thread(parent_thread_id=parent_thread_id, window_ratio=window_ratio, chronicle_prompt=chronicle_prompt, max_depth=max_depth, label=label)
        if not stelis:
            LOGGER.error("[stelis] Failed to create Stelis thread for persona %s", persona.persona_id)
            state["stelis_error"] = "Failed to create Stelis thread"
            state["stelis_available"] = False
            return state
        anchor_message = {"role": "system", "content": "", "metadata": {"type": "stelis_anchor", "stelis_thread_id": stelis.thread_id, "stelis_label": label, "created_at": int(time.time())}, "embedding_chunks": 0}
        memory_adapter.append_persona_message(anchor_message, thread_suffix=parent_thread_id.split(":")[-1] if ":" in parent_thread_id else parent_thread_id)
        memory_adapter.set_active_thread(stelis.thread_id)
        log_sea_trace(playbook.name, node_id, "STELIS_START", f"thread={stelis.thread_id} label=\"{label}\"")
        state.update({"stelis_thread_id": stelis.thread_id, "stelis_parent_thread_id": parent_thread_id, "stelis_depth": stelis.depth, "stelis_window_ratio": window_ratio, "stelis_label": label, "stelis_available": True})
        if event_callback:
            event_callback({"type": "stelis_start", "thread_id": stelis.thread_id, "parent_thread_id": parent_thread_id, "depth": stelis.depth, "label": label})
        return state

    return node


def lg_stelis_end_node(runtime: Any, node_def: Any, persona: Any, playbook: Any, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
    async def node(state: dict):
        cancellation_token = state.get("_cancellation_token")
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        node_id = getattr(node_def, "id", "stelis_end")
        generate_chronicle = getattr(node_def, "generate_chronicle", True)
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            LOGGER.warning("[stelis] No memory adapter found for persona %s", persona.persona_id)
            return state
        current_thread_id = state.get("stelis_thread_id")
        parent_thread_id = state.get("stelis_parent_thread_id")
        if not current_thread_id or not parent_thread_id:
            LOGGER.warning("[stelis] STELIS_END called without active Stelis context")
            return state
        stelis_info = memory_adapter.get_stelis_info(current_thread_id)
        if not stelis_info:
            LOGGER.warning("[stelis] Current thread %s is not a Stelis thread", current_thread_id)
            return state
        chronicle_summary = runtime._generate_stelis_chronicle(persona, current_thread_id, stelis_info.chronicle_prompt) if generate_chronicle else None
        memory_adapter.end_stelis_thread(thread_id=current_thread_id, status="completed", chronicle_summary=chronicle_summary)
        memory_adapter.set_active_thread(parent_thread_id)
        if chronicle_summary:
            state["stelis_chronicle"] = chronicle_summary
        state["stelis_thread_id"] = None
        state["stelis_parent_thread_id"] = None
        state["stelis_depth"] = None
        _chron_str = chronicle_summary or "(none)"
        log_sea_trace(playbook.name, node_id, "STELIS_END", f"thread={current_thread_id} chronicle=\"{_chron_str}\"")
        if event_callback:
            event_callback({"type": "stelis_end", "thread_id": current_thread_id, "parent_thread_id": parent_thread_id, "chronicle_generated": generate_chronicle})
        return state

    return node