"""document_replace_content — Replace the entire content of a document item."""
from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def document_replace_content(item_id: str, content: str) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set.")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")
    item_id = manager.resolve_item_ref_for_persona(persona_id, item_id)
    return manager.replace_document_content(persona_id, item_id, content)


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_replace_content",
        description=(
            "Overwrite the entire content of a document item with new text. "
            "Use this for full rewrites. For partial edits, use document_patch_content instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "ID of the document item to overwrite.",
                },
                "content": {
                    "type": "string",
                    "description": "The new content to write.",
                },
            },
            "required": ["item_id", "content"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ドキュメント全置換",
    )
