from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from saiverse.logging_config import log_sea_trace
from sea.playbook_models import PlaybookSchema
from sea.runtime_utils import _format

LOGGER = logging.getLogger(__name__)

StateNode = Callable[[dict], Awaitable[dict]]
EventCallback = Optional[Callable[[Dict[str, Any]], None]]


class RuntimeEngine:
    def __init__(self, runtime: Any, manager_ref: Any, llm_selector: Callable[..., Any], emitters: Dict[str, Callable[..., Any]]) -> None:
        self.runtime = runtime
        self.manager = manager_ref
        self.llm_selector = llm_selector
        self.emitters = emitters

    def lg_tool_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: EventCallback = None, auto_mode: bool = False) -> StateNode:
        from pathlib import Path

        from tools import TOOL_REGISTRY
        from tools.context import persona_context

        tool_name = node_def.action
        args_input = getattr(node_def, "args_input", None)
        output_key = getattr(node_def, "output_key", None)
        output_keys = getattr(node_def, "output_keys", None)

        async def node(state: dict) -> dict:
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
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
                kwargs: Dict[str, Any] = {}
                if args_input:
                    for arg_name, source in args_input.items():
                        if isinstance(source, str):
                            value = self.runtime._resolve_state_value(state, source)
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

    def lg_exec_node(
        self,
        node_def: Any,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        auto_mode: bool,
        outputs: Optional[List[str]] = None,
        event_callback: EventCallback = None,
    ) -> StateNode:
        # Get source variable names from node definition (with defaults for backward compatibility)
        playbook_source = getattr(node_def, "playbook_source", "selected_playbook") or "selected_playbook"
        args_source = getattr(node_def, "args_source", "selected_args") or "selected_args"

        async def node(state: dict) -> dict:
            # Check for cancellation at start of node
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()

            # Send status event for node execution
            node_id = getattr(node_def, "id", "exec")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            sub_name = state.get(playbook_source) or state.get("last") or "basic_chat"
            clean_name = str(sub_name).strip()
            sub_pb = self.runtime._load_playbook_for(clean_name, persona, building_id)
            if sub_pb is None:
                if clean_name == "basic_chat":
                    sub_pb = self.runtime._basic_chat_playbook()
                else:
                    error_msg = f"Sub-playbook not found: {clean_name}"
                    state["last"] = error_msg
                    state["_exec_error"] = True
                    state["_exec_error_detail"] = error_msg
                    if outputs is not None:
                        outputs.append(error_msg)
                    return state
            sub_input = None
            args = state.get(args_source) or {}
            if isinstance(args, dict):
                sub_input = args.get("input") or args.get("query")
            if not sub_input:
                sub_input = state.get("inputs", {}).get("input")

            eff_bid = self.runtime._effective_building_id(persona, building_id)

            # ── Playbook permission check ──
            if clean_name != "basic_chat":
                city_id = getattr(self.manager, "city_id", None)
                if city_id is not None:
                    perm = self.runtime._get_playbook_permission(city_id, clean_name)
                    log_sea_trace(playbook.name, node_id, "PERM", f"{clean_name} → {perm}")

                    if perm == "blocked":
                        denial_msg = f"Playbook '{clean_name}' is not available (permission: {perm})"
                        self.runtime._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                        return state

                    if perm == "ask_every_time":
                        if auto_mode:
                            denial_msg = f"Playbook '{clean_name}' requires user permission but running in auto mode. Skipped."
                            self.runtime._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                            return state

                        # Schedule-triggered executions: user pre-approved by creating the schedule
                        if state.get("pulse_type") == "schedule":
                            log_sea_trace(playbook.name, node_id, "PERM", f"{clean_name}: auto-allowed (schedule)")
                        else:
                            response = self.runtime._request_playbook_permission(clean_name, persona, event_callback)

                            if response in ("deny", "timeout"):
                                denial_msg = (
                                    f"User denied execution of playbook '{clean_name}'. Please respond without using this tool."
                                    if response == "deny"
                                    else f"Permission request for playbook '{clean_name}' timed out. Please respond without using this tool."
                                )
                                self.runtime._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                                return state

                            if response == "always_allow":
                                self.runtime._set_playbook_permission(city_id, clean_name, "auto_allow")

                            if response == "never_use":
                                denial_msg = f"User disabled playbook '{clean_name}'. This playbook will not be available in future. Please respond without using this tool."
                                self.runtime._set_playbook_permission(city_id, clean_name, "user_only")
                                self.runtime._notify_persona_permission_result(state, persona, clean_name, denial_msg, event_callback)
                                return state

                    # perm == "auto_allow" or allowed via dialog → continue

            # Determine execution mode
            execution = getattr(node_def, "execution", "inline") or "inline"
            subagent_thread_id = None
            subagent_parent_id = None

            if execution == "subagent":
                label = f"Subagent: {sub_name}"
                subagent_thread_id, subagent_parent_id = self.runtime._start_subagent_thread(persona, label=label)
                if not subagent_thread_id:
                    LOGGER.warning("[sea][exec] Failed to start subagent thread for '%s', falling back to inline", sub_name)
                    execution = "inline"  # Fallback
                else:
                    log_sea_trace(playbook.name, node_id, "EXEC", f"→ {sub_name} [subagent thread={subagent_thread_id}] (input=\"{str(sub_input)}\")")

            if execution == "inline":
                log_sea_trace(playbook.name, node_id, "EXEC", f"→ {sub_name} (input=\"{str(sub_input)}\")")

            try:
                cancellation_token = state.get("_cancellation_token")
                sub_outputs = await asyncio.to_thread(
                    self.runtime._run_playbook, sub_pb, persona, eff_bid, sub_input, auto_mode, True, state, event_callback,
                    cancellation_token=cancellation_token,
                )
            except Exception as exc:
                LOGGER.exception("SEA LangGraph exec sub-playbook failed")
                # End subagent thread on error (no chronicle)
                if execution == "subagent" and subagent_thread_id:
                    self.runtime._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=False)
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
                if not self.runtime._store_memory(
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
                chronicle = self.runtime._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=gen_chronicle)
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
            self.runtime._append_tool_result_message(state, str(sub_name).strip(), joined or "(completed)")
            if sub_outputs:
                state["last"] = sub_outputs[-1]
            return state

        return node

    def lg_memorize_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: EventCallback = None) -> StateNode:
        async def node(state: dict) -> dict:
            # Send status event for node execution
            node_id = getattr(node_def, "id", "memorize")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            # Include all state variables for template expansion (e.g., structured output like document_data.*)
            variables = dict(state)
            # Flatten nested dicts/lists for dot notation access (e.g., finalize_output.content)
            for key, value in list(state.items()):
                if isinstance(value, dict):
                    flat = self.runtime._flatten_dict(value)
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
            if not self.runtime._store_memory(persona, memo_text, role=role, tags=tags, pulse_id=pulse_id, metadata=metadata):
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

    def lg_speak_node(self, state: dict, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: EventCallback = None) -> dict:
        # Send status event for node execution
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / speak", "playbook": playbook.name, "node": "speak"})
        text = state.get("last") or ""
        reasoning_text = state.pop("_reasoning_text", "")
        reasoning_details_val = state.pop("_reasoning_details", None)
        activity_trace = state.get("_activity_trace")
        pulse_id = state.get("pulse_id")
        eff_bid = self.runtime._effective_building_id(persona, building_id)
        # Build extra metadata with reasoning for SAIMemory storage
        speak_metadata: Dict[str, Any] = {}
        if reasoning_text:
            speak_metadata["reasoning"] = reasoning_text
        if reasoning_details_val is not None:
            speak_metadata["reasoning_details"] = reasoning_details_val
        self.emitters["speak"](persona, eff_bid, text, pulse_id=pulse_id, extra_metadata=speak_metadata if speak_metadata else None)
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
