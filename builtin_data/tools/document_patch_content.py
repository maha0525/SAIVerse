"""document_patch_content — Replace a substring in a document item's content."""
from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def document_patch_content(item_id: str, old_string: str, new_string: str) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set.")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")
    item_id = manager.resolve_item_ref_for_persona(persona_id, item_id)
    return manager.patch_document_content(persona_id, item_id, old_string, new_string)


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_patch_content",
        description=(
            "Replace a specific substring in a document item's content. "
            "The old_string must match exactly one location in the document."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "ID of the document item to edit.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text.",
                },
            },
            "required": ["item_id", "old_string", "new_string"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ドキュメント部分置換",
    )
