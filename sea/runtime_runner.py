from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from sea.playbook_models import PlaybookSchema

LOGGER = logging.getLogger(__name__)


def run_playbook(
    runtime: Any,
    playbook: PlaybookSchema,
    persona: Any,
    building_id: str,
    user_input: Optional[str],
    auto_mode: bool,
    record_history: bool = True,
    parent_state: Optional[Dict[str, Any]] = None,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancellation_token: Optional[Any] = None,
    pulse_type: Optional[str] = None,
    initial_params: Optional[Dict[str, Any]] = None,
) -> List[str]:
    if cancellation_token:
        cancellation_token.raise_if_cancelled()

    parent = parent_state or {}

    if initial_params:
        LOGGER.debug("[sea] _run_playbook merging initial_params: %s", list(initial_params.keys()))
        parent.update(initial_params)
    LOGGER.debug("[sea] _run_playbook called for %s, parent_state keys: %s", playbook.name, list(parent.keys()) if parent else "(none)")
    if "pulse_id" in parent:
        pulse_id = str(parent["pulse_id"])
    else:
        pulse_id = str(uuid.uuid4())

    parent_chain = parent.get("_playbook_chain", "")
    if parent_chain:
        current_chain = f"{parent_chain} > {playbook.name}"
    else:
        current_chain = playbook.name

    parent["_playbook_chain"] = current_chain

    if cancellation_token:
        parent["_cancellation_token"] = cancellation_token

    def wrapped_event_callback(event: Dict[str, Any]) -> None:
        if event_callback:
            if event.get("type") == "status":
                node = event.get("node", "")
                event["content"] = f"{current_chain} / {node}"
                event["playbook_chain"] = current_chain
            event_callback(event)

    if hasattr(persona, "execution_state"):
        persona.execution_state["playbook"] = playbook.name
        persona.execution_state["node"] = playbook.start_node
        persona.execution_state["status"] = "running"

    LOGGER.info(
        "[sea][run-playbook] %s: calling _prepare_context with history_depth=%s, pulse_id=%s",
        playbook.name,
        playbook.context_requirements.history_depth if playbook.context_requirements else "None",
        pulse_id,
    )
    context_warnings: List[Dict[str, Any]] = []
    base_messages = runtime._prepare_context(
        persona,
        building_id,
        user_input,
        playbook.context_requirements,
        pulse_id=pulse_id,
        warnings=context_warnings,
    )
    LOGGER.info("[sea][run-playbook] %s: _prepare_context returned %d messages", playbook.name, len(base_messages))
    conversation_msgs = list(base_messages)

    for warn in context_warnings:
        if event_callback:
            wrapped_event_callback(warn)

    compiled_ok = runtime._compile_with_langgraph(
        playbook,
        persona,
        building_id,
        user_input,
        auto_mode,
        conversation_msgs,
        pulse_id,
        parent_state=parent,
        event_callback=wrapped_event_callback,
        cancellation_token=cancellation_token,
        pulse_type=pulse_type,
    )
    if compiled_ok is None:
        LOGGER.error(
            "LangGraph compilation failed for playbook '%s'. This indicates a configuration or dependency issue.",
            playbook.name,
        )
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"
        return []

    return compiled_ok
