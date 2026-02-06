"""Document search tool - Search for patterns in a document item."""
from __future__ import annotations

import re
from typing import Optional, List

from tools.context import get_active_manager
from tools.core import ToolSchema


def document_search(
    item_id: str,
    pattern: str,
    case_sensitive: bool = False,
    context_lines: int = 2,
    max_matches: int = 10
) -> str:
    """Search for a pattern in a document item.

    Args:
        item_id: Identifier of the document item.
        pattern: Search pattern (supports regex).
        case_sensitive: Whether the search is case-sensitive. Default: False
        context_lines: Number of context lines to show before and after each match. Default: 2
        max_matches: Maximum number of matches to return. Default: 10

    Returns:
        Matching lines with context and line numbers.
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

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to read file: {exc}") from exc

    lines = content.split('\n')
    total_lines = len(lines)

    # Normalize parameters (SEA runtime may pass empty strings)
    if case_sensitive == "" or case_sensitive is None:
        case_sensitive = False
    if context_lines == "" or context_lines is None:
        context_lines = 2
    else:
        context_lines = int(context_lines)
    if max_matches == "" or max_matches is None:
        max_matches = 10
    else:
        max_matches = int(max_matches)

    # Compile regex
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise RuntimeError(f"Invalid regex pattern: {exc}") from exc

    # Find matching lines
    matches: List[int] = []
    for i, line in enumerate(lines):
        if regex.search(line):
            matches.append(i)
            if len(matches) >= max_matches:
                break

    if not matches:
        return f"No matches found for pattern '{pattern}'."

    # Build result
    results = []
    item_name = item.get("name", item_id)
    results.append(f"[{item_name}] Search results: {len(matches)} matches (total {total_lines} lines)\n")
    results.append(f"Pattern: {pattern}\n")
    results.append("=" * 60)

    for match_idx in matches:
        start = max(0, match_idx - context_lines)
        end = min(total_lines, match_idx + context_lines + 1)

        results.append(f"\n--- Line {match_idx + 1} ---")

        for i in range(start, end):
            prefix = ">" if i == match_idx else " "
            results.append(f"{prefix}{i + 1:6d}  {lines[i]}")

    if len(matches) >= max_matches:
        results.append(f"\n... (showing first {max_matches} matches only)")

    return "\n".join(results)


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_search",
        description=(
            "Search for a pattern in a document item using regex. "
            "Returns matching lines with context. "
            "Similar to grep with context lines."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the document item to search.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (supports regular expressions).",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether the search is case-sensitive. Default: false",
                    "default": False,
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Number of context lines to show before and after each match. Default: 2",
                    "default": 2,
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Maximum number of matches to return. Default: 10",
                    "default": 10,
                },
            },
            "required": ["item_id", "pattern"],
        },
        result_type="string",
    )
