"""Get conversation history as messages array for LLM context."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema

LOGGER = logging.getLogger(__name__)


def get_history(
    building_id: Optional[str] = None,
    max_chars: int = 100000,
    include_system_prompt: bool = True,
    include_inventory: bool = True,
    include_building_items: bool = True,
) -> List[Dict[str, str]]:
    """Get conversation history as messages array for LLM context.

    Builds a messages array containing:
    - System prompt (optional)
    - Persona conversation history

    Returns a list of message dicts with 'role' and 'content' keys.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    # Use current building if not specified
    if not building_id:
        building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        raise RuntimeError("Building ID not specified and persona has no current building")

    messages: List[Dict[str, str]] = []

    # 1. Add system prompt
    if include_system_prompt:
        try:
            from tools.defs.get_system_prompt import get_system_prompt
            system_prompt = get_system_prompt(
                building_id=building_id,
                include_inventory=include_inventory,
                include_building_items=include_building_items,
            )
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
        except Exception as exc:
            LOGGER.warning("Failed to get system prompt: %s", exc)

    # 2. Add conversation history from persona
    history_manager = getattr(persona, "history_manager", None)
    if history_manager:
        try:
            recent = history_manager.get_recent_history(
                max_chars,
                required_tags=["conversation"],
                pulse_id=None,
            )
            messages.extend(recent)
            LOGGER.debug("get_history: added %d messages from history", len(recent))
        except Exception as exc:
            LOGGER.warning("Failed to get history: %s", exc)

    return messages


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_history",
        description="Get conversation history as messages array for LLM context, including system prompt and persona history.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID. Defaults to current building."
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters of history to include. Default: 100000.",
                    "default": 100000
                },
                "include_system_prompt": {
                    "type": "boolean",
                    "description": "Include system prompt. Default: true.",
                    "default": True
                },
                "include_inventory": {
                    "type": "boolean",
                    "description": "Include persona inventory in system prompt. Default: true.",
                    "default": True
                },
                "include_building_items": {
                    "type": "boolean",
                    "description": "Include building items in system prompt. Default: true.",
                    "default": True
                }
            },
            "required": [],
        },
        result_type="array",
    )
