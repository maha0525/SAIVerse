"""document_append_content — Append text to the end of a document item."""
from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def document_append_content(item_id: str, content: str) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set.")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")
    item_id = manager.resolve_item_ref_for_persona(persona_id, item_id)
    return manager.append_document_content(persona_id, item_id, content)


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_append_content",
        description=(
            "Append text to the end of a document item. "
            "Use this to add new content without modifying existing text."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "ID of the document item to append to.",
                },
                "content": {
                    "type": "string",
                    "description": "The text to append.",
                },
            },
            "required": ["item_id", "content"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ドキュメント追記",
    )
