from __future__ import annotations

import asyncio
import logging
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

    def lg_tool_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: EventCallback = None) -> StateNode:
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
                kwargs: Dict[str, Any] = {}
                if args_input:
                    for arg_name, source in args_input.items():
                        kwargs[arg_name] = self.runtime._resolve_state_value(state, source) if isinstance(source, str) else source
                if persona_id and persona_dir:
                    with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook.name):
                        result = tool_func(**kwargs) if callable(tool_func) else None
                else:
                    result = tool_func(**kwargs) if callable(tool_func) else None
                result_str = str(result)
                log_sea_trace(playbook.name, node_id, "TOOL", f"action={tool_name} → {result_str}")
                if output_keys and isinstance(result, tuple):
                    for i, key in enumerate(output_keys):
                        if i < len(result):
                            state[key] = result[i]
                    state["last"] = str(result[0]) if result else ""
                elif isinstance(result, tuple):
                    state["last"] = str(result[0]) if result else ""
                else:
                    state["last"] = str(result)
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
        playbook_source = getattr(node_def, "playbook_source", "selected_playbook") or "selected_playbook"
        args_source = getattr(node_def, "args_source", "selected_args") or "selected_args"

        async def node(state: dict) -> dict:
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            node_id = getattr(node_def, "id", "exec")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            sub_name = state.get(playbook_source) or state.get("last") or "basic_chat"
            sub_pb = self.runtime._load_playbook_for(str(sub_name).strip(), persona, building_id) or self.runtime._basic_chat_playbook()
            args = state.get(args_source) or {}
            sub_input = args.get("input") if isinstance(args, dict) else None
            if not sub_input and isinstance(args, dict):
                sub_input = args.get("query")
            if not sub_input:
                sub_input = state.get("inputs", {}).get("input")
            eff_bid = self.runtime._effective_building_id(persona, building_id)
            try:
                sub_outputs = await asyncio.to_thread(self.runtime._run_playbook, sub_pb, persona, eff_bid, sub_input, auto_mode, True, state, event_callback)
            except Exception as exc:
                error_msg = f"Sub-playbook error: {type(exc).__name__}: {exc}"
                state["last"] = error_msg
                state["_exec_error"] = True
                state["_exec_error_detail"] = error_msg
                if outputs is not None:
                    outputs.append(error_msg)
                return state
            state["_exec_error"] = False
            state.pop("_exec_error_detail", None)
            self.runtime._append_tool_result_message(state, str(sub_name).strip(), "\n".join(str(item).strip() for item in sub_outputs if str(item).strip()) or "(completed)")
            if sub_outputs:
                state["last"] = sub_outputs[-1]
            return state

        return node

    def lg_memorize_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: EventCallback = None) -> StateNode:
        async def node(state: dict) -> dict:
            node_id = getattr(node_def, "id", "memorize")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            variables = dict(state)
            for key, value in list(state.items()):
                if isinstance(value, dict):
                    for path, val in self.runtime._flatten_dict(value).items():
                        variables[f"{key}.{path}"] = val
            variables.update({"input": state.get("inputs", {}).get("input", ""), "last": state.get("last", "")})
            memo_text = _format(getattr(node_def, "action", None) or "{last}", variables)
            role = getattr(node_def, "role", "assistant") or "assistant"
            tags = getattr(node_def, "tags", None)
            pulse_id = state.get("pulse_id")
            metadata_key = getattr(node_def, "metadata_key", None)
            metadata = state.get(metadata_key) if metadata_key else None
            self.runtime._store_memory(persona, memo_text, role=role, tags=tags, pulse_id=pulse_id, metadata=metadata)
            log_sea_trace(playbook.name, node_id, "MEMORIZE", f"role={role} tags={tags} text=\"{memo_text}\"")
            state["last"] = memo_text
            if outputs is not None:
                outputs.append(memo_text)
            return state

        return node

    def lg_speak_node(self, state: dict, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: EventCallback = None) -> dict:
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / speak", "playbook": playbook.name, "node": "speak"})
        text = state.get("last") or ""
        pulse_id = state.get("pulse_id")
        eff_bid = self.runtime._effective_building_id(persona, building_id)
        self.emitters["speak"](persona, eff_bid, text, pulse_id=pulse_id)
        if outputs is not None:
            outputs.append(text)
        if event_callback:
            event_callback({"type": "say", "content": text, "persona_id": getattr(persona, "persona_id", None)})
        return state
