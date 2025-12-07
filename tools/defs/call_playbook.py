from __future__ import annotations

from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema


def call_playbook(playbook_name: str) -> str:
    """Dynamically call another playbook and return its result.

    This allows LLM nodes to invoke specialized playbooks when needed,
    bypassing the normal router selection.

    Args:
        playbook_name: Name of the playbook to execute

    Returns:
        The result of the playbook execution
    """
    import logging
    logger = logging.getLogger(__name__)

    persona_id = get_active_persona_id()
    logger.info("[call_playbook] persona_id: %s", persona_id)
    if not persona_id:
        raise RuntimeError("Active persona is not set (use tools.context.persona_context)")

    manager_ref = get_active_manager()
    logger.info("[call_playbook] manager_ref: %s (type: %s)", manager_ref, type(manager_ref).__name__ if manager_ref else None)
    if not manager_ref:
        raise RuntimeError("Manager reference not available in persona context")

    # Get SEA runtime from manager
    sea_runtime = getattr(manager_ref, "sea_runtime", None)
    logger.info("[call_playbook] sea_runtime: %s", sea_runtime)
    if not sea_runtime:
        raise RuntimeError("SEA runtime not available in manager")

    # Get persona object from manager's personas dict
    personas = getattr(manager_ref, "personas", {})
    logger.info("[call_playbook] personas dict keys: %s", list(personas.keys()))
    persona_obj = personas.get(persona_id)
    logger.info("[call_playbook] persona_obj: %s (type: %s)", persona_obj, type(persona_obj).__name__ if persona_obj else None)
    if not persona_obj:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    # Get current building ID
    building_id = getattr(persona_obj, "current_building_id", None)
    logger.info("[call_playbook] building_id: %s", building_id)
    if not building_id:
        raise RuntimeError("Building ID not available in persona")

    # Load and execute meta_exec_speak playbook
    # This playbook will execute the target playbook and generate a response
    meta_exec_speak = sea_runtime._load_playbook_for("meta_exec_speak", persona_obj, building_id)
    if not meta_exec_speak:
        raise RuntimeError("meta_exec_speak playbook not found")

    # Create a state dict with the target playbook name
    parent_state = {"selected_playbook": playbook_name}

    # Execute the playbook
    outputs = sea_runtime._run_playbook(
        meta_exec_speak,
        persona_obj,
        building_id,
        user_input=None,
        auto_mode=False,
        record_history=True,
        parent_state=parent_state,
    )

    # Return the last output (should be the final speech)
    return outputs[-1] if outputs else "(no output from playbook)"


def schema() -> ToolSchema:
    return ToolSchema(
        name="call_playbook",
        description="Call another playbook to perform a specialized task. Use this when you need to execute a specific capability (like sending email, generating images, etc.) instead of responding directly.",
        parameters={
            "type": "object",
            "properties": {
                "playbook_name": {
                    "type": "string",
                    "description": "Name of the playbook to execute (e.g., 'send_email_to_user', 'generate_image', 'memory_recall')"
                }
            },
            "required": ["playbook_name"],
        },
        result_type="string",
    )
