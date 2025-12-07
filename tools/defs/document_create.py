from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.defs import ToolSchema


def document_create(name: str, description: str, content: str) -> str:
    """
    Create a new document item with text content and place it in the current building.

    Args:
        name: Name of the document (e.g., '私の日記', 'SAIVerse使い方ガイド').
        description: Brief description of the document. This will be used as initial summary.
        content: Full text content of the document.

    Returns:
        Success message with item ID.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")

    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; document_create cannot be executed.")

    return manager.create_document_item(persona_id, name, description, content)


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_create",
        description="Create a new document item with text content and place it in the current building.",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the document (e.g., '私の日記', 'SAIVerse使い方ガイド').",
                },
                "description": {
                    "type": "string",
                    "description": "Brief description of the document. This will be visible in item lists.",
                },
                "content": {
                    "type": "string",
                    "description": "Full text content of the document.",
                },
            },
            "required": ["name", "description", "content"],
        },
        result_type="string",
    )
