"""Document edit tool - Replace content in a document item."""
from __future__ import annotations

import logging
from typing import Optional

from tools.context import get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def document_edit(
    item_id: str,
    new_content: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    replacement: Optional[str] = None,
) -> str:
    """Edit a document item's content.

    Two modes:
    1. Full replacement: provide new_content to replace entire document.
    2. Line range replacement: provide start_line, end_line, and replacement
       to replace specific lines.

    Args:
        item_id: Identifier of the document item.
        new_content: Full replacement content (replaces entire document).
        start_line: Starting line number (1-based, inclusive) for partial edit.
        end_line: Ending line number (1-based, inclusive) for partial edit.
        replacement: Text to insert in place of lines start_line..end_line.

    Returns:
        Confirmation with line count information.
    """
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")

    item = manager.item_service.items.get(item_id)
    if not item:
        raise RuntimeError(f"Item '{item_id}' not found.")

    if (item.get("type") or "").lower() != "document":
        raise RuntimeError(f"Item '{item_id}' is not a document type.")

    file_path_str = item.get("file_path")
    if not file_path_str:
        raise RuntimeError("This document has no file_path set.")

    file_path = manager.item_service._resolve_file_path(file_path_str)
    if not file_path.exists():
        raise RuntimeError(f"File not found: {file_path}")

    item_name = item.get("name", item_id)

    if new_content is not None:
        # Full replacement mode
        file_path.write_text(new_content, encoding="utf-8")
        new_lines = new_content.count("\n") + 1
        LOGGER.info("document_edit: full replacement of '%s' (%d lines)", item_name, new_lines)
        return f"ドキュメント '{item_name}' を全文置換しました ({new_lines}行)"

    if start_line is not None and end_line is not None and replacement is not None:
        # Line range replacement mode
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Failed to read file: {exc}") from exc

        lines = content.split("\n")
        total_lines = len(lines)

        # Normalize
        start_line = int(start_line)
        end_line = int(end_line)
        start_idx = max(0, start_line - 1)
        end_idx = min(total_lines, end_line)

        # Replace lines
        replacement_lines = replacement.split("\n")
        new_lines = lines[:start_idx] + replacement_lines + lines[end_idx:]
        new_content = "\n".join(new_lines)

        file_path.write_text(new_content, encoding="utf-8")

        LOGGER.info(
            "document_edit: replaced lines %d-%d of '%s' (%d lines → %d lines)",
            start_line, end_line, item_name, end_idx - start_idx, len(replacement_lines),
        )
        return (
            f"ドキュメント '{item_name}' の {start_line}-{end_line}行を置換しました "
            f"({end_idx - start_idx}行 → {len(replacement_lines)}行, 合計 {len(new_lines)}行)"
        )

    return "new_content（全文置換）または start_line + end_line + replacement（部分置換）を指定してください"


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_edit",
        description=(
            "Edit a document item. Either replace the full content, "
            "or replace a specific line range. Use document_read first "
            "to see the current content and line numbers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the document item.",
                },
                "new_content": {
                    "type": "string",
                    "description": "Full replacement content (replaces entire document). Omit for partial edit.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Starting line number (1-based) for partial replacement.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Ending line number (1-based) for partial replacement.",
                },
                "replacement": {
                    "type": "string",
                    "description": "Text to insert in place of the specified line range.",
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
    )
