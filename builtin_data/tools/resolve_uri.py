"""Resolve SAIVerse URIs to their content."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema, ToolResult

LOGGER = logging.getLogger(__name__)


def resolve_uri(
    uris,
    max_total_chars: int = 8000,
) -> str:
    """Resolve one or more SAIVerse URIs and return their content.

    Args:
        uris: List of saiverse:// URI strings, or a single comma-separated string
        max_total_chars: Maximum total characters across all resolved contents

    Returns:
        Formatted text containing the resolved content of each URI
    """
    # Accept comma-separated string as well as list
    if isinstance(uris, str):
        uris = [u.strip() for u in uris.split(",") if u.strip()]

    persona_id = get_active_persona_id()
    manager = get_active_manager()

    from uri_resolver import UriResolver

    resolver = UriResolver(manager=manager)
    results = resolver.resolve_many(
        uris,
        persona_id=persona_id,
        max_total_chars=max_total_chars,
        priority="first",
    )

    if not results:
        return "(no URIs to resolve)"

    parts = []
    for r in results:
        header = f"--- {r.uri} ({r.content_type}, {r.char_count}chars) ---"
        parts.append(f"{header}\n{r.content}")

    return "\n\n".join(parts)


def schema() -> ToolSchema:
    return ToolSchema(
        name="resolve_uri",
        description=(
            "Resolve SAIVerse URIs to retrieve their content. "
            "Supports messagelog, memopedia, chronicle, item, building, web, and more. "
            "Use saiverse://self/... for own resources."
        ),
        parameters={
            "type": "object",
            "properties": {
                "uris": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "SAIVerse URI strings to resolve (e.g., saiverse://self/memopedia/page/abc123)",
                },
                "max_total_chars": {
                    "type": "integer",
                    "description": "Maximum total characters for all resolved content (default: 8000)",
                    "default": 8000,
                },
            },
            "required": ["uris"],
        },
        result_type="string",
    )
