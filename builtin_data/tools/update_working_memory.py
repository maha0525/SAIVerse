"""Update a key in working memory (persisted across pulses)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema

LOGGER = logging.getLogger(__name__)


def update_working_memory(key: str, value: Any) -> str:
    """Update a specific key in working memory.

    Working memory is persisted to DB and survives across pulses/restarts.
    Use this for short-term state that needs to persist but isn't worth
    storing in SAIMemory conversation history.

    Args:
        key: The key to update in working memory.
        value: The value to set. Can be any JSON-serializable value.

    Returns:
        Confirmation message.
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

    sai_mem = getattr(persona, "sai_memory", None)
    if not sai_mem or not sai_mem.is_ready():
        raise RuntimeError("SAIMemory is not available")

    # Load current working memory
    current_wm = sai_mem.load_working_memory()

    # Update the key
    current_wm[key] = value

    # Save back to DB
    sai_mem.save_working_memory(current_wm)

    LOGGER.debug("Updated working_memory[%s] for %s", key, persona_id)
    return f"Working memory updated: {key}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="update_working_memory",
        description="Update a key in working memory. Working memory persists across pulses and server restarts. Use for short-term state like situation snapshots.",
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The key to update in working memory (e.g., 'situation_snapshot', 'last_activity')."
                },
                "value": {
                    "description": "The value to set. Can be any JSON-serializable value (string, number, object, array)."
                }
            },
            "required": ["key", "value"],
        },
        result_type="string",
    )
