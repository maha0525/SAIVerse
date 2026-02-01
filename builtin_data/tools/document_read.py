"""Document read tool - Read specific lines from a document item."""
from __future__ import annotations

from typing import Optional

from tools.context import get_active_manager
from tools.core import ToolSchema


def document_read(
    item_id: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    limit: int = 100
) -> str:
    """Read specific lines from a document item.

    Args:
        item_id: Identifier of the document item.
        start_line: Starting line number (1-based, inclusive). Default: 1
        end_line: Ending line number (inclusive). If None, reads `limit` lines from start.
        limit: Maximum number of lines to return if end_line is not specified. Default: 100

    Returns:
        The requested lines with line numbers prefixed.
    """
    manager = get_active_manager()

    if manager is None:
        raise RuntimeError("Manager context is not available.")

    # Get item from ItemService
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

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to read file: {exc}") from exc

    lines = content.split('\n')
    total_lines = len(lines)

    # Normalize parameters (SEA runtime may pass empty strings)
    if start_line == "" or start_line is None:
        start_line = 1
    else:
        start_line = int(start_line)

    if limit == "" or limit is None:
        limit = 100
    else:
        limit = int(limit)

    if end_line == "" or end_line is None:
        end_line = None
    elif end_line is not None:
        end_line = int(end_line)

    # 1-based to 0-based
    start_idx = max(0, start_line - 1)

    if end_line is not None:
        end_idx = min(total_lines, end_line)
    else:
        end_idx = min(total_lines, start_idx + limit)

    selected_lines = lines[start_idx:end_idx]

    # Format with line numbers (cat -n style)
    result_lines = []
    for i, line in enumerate(selected_lines, start=start_idx + 1):
        result_lines.append(f"{i:6d}  {line}")

    item_name = item.get("name", item_id)
    header = f"[{item_name}] Lines {start_idx + 1}-{end_idx} / Total {total_lines} lines\n"
    return header + "\n".join(result_lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_read",
        description=(
            "Read specific lines from a document item. "
            "Useful for reading large documents section by section. "
            "Line numbers are 1-based."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the document item.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Starting line number (1-based, inclusive). Default: 1",
                    "default": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Ending line number (inclusive). If not specified, reads 'limit' lines from start.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to return if end_line is not specified. Default: 100",
                    "default": 100,
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
    )
