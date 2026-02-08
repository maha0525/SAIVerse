"""Direct write / append to Memopedia page (no research loop)."""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Map category names to root page IDs
_CATEGORY_ROOT_MAP = {
    "people": "root_people",
    "terms": "root_terms",
    "plans": "root_plans",
}

# Pattern: saiverse://self/memopedia/page/{page_id}  or  saiverse://{city}/{persona}/memopedia/page/{page_id}
_MEMOPEDIA_URI_RE = re.compile(
    r"^saiverse://[^/]+(?:/[^/]+)?/memopedia/page/(?P<page_id>[^?/]+)"
)


def _extract_page_id(value: str) -> str:
    """Extract page_id from a raw string that may be a saiverse:// URI or a plain ID."""
    value = value.strip()
    m = _MEMOPEDIA_URI_RE.match(value)
    if m:
        return m.group("page_id")
    return value


def memopedia_note(
    content: str,
    title: str = "",
    summary: str = "",
    category: str = "terms",
    keywords: Optional[List[str]] = None,
    page_id: str = "",
) -> str:
    """Directly write or append to a Memopedia page.

    - content: text to write (Markdown)
    - title: page title (required for new pages, optional for append)
    - summary: 1-2 sentence summary (optional)
    - category: one of 'people', 'terms', 'plans' (default: terms)
    - keywords: list of keywords for search (optional)
    - page_id: existing page ID or saiverse:// URI to append to (optional)

    If page_id is provided, content is appended to the existing page.
    If page_id is empty and title is provided, a new page is created
    (or the existing page with the same title is updated).
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(
            persona_id, persona_dir=persona_dir, resource_id=persona_id
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    from sai_memory.memopedia import Memopedia

    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)

    # --- Append mode: page_id (or URI) is provided ---
    if page_id:
        resolved_id = _extract_page_id(page_id)
        page = memopedia.get_page(resolved_id)
        if page is None:
            return f"Page not found: {resolved_id}"

        # Append content
        updated = memopedia.append_to_content(
            resolved_id,
            content,
            edit_source="ai_conversation",
        )
        if updated is None:
            return f"Failed to append to page '{resolved_id}'"

        # Optionally update metadata
        meta_updates = {}
        if summary:
            meta_updates["summary"] = summary
        if keywords:
            meta_updates["keywords"] = keywords
        if title and title != page.title:
            meta_updates["title"] = title
        if meta_updates:
            memopedia.update_page(
                resolved_id,
                edit_source="ai_conversation",
                **meta_updates,
            )

        return (
            f"Appended to page '{updated.title}' (id: {resolved_id})\n"
            f"URI: saiverse://self/memopedia/page/{resolved_id}"
        )

    # --- Create mode: new page or find-by-title update ---
    if not title:
        return "Error: title is required when creating a new page (no page_id given)"

    cat = category.lower().strip()
    if cat not in _CATEGORY_ROOT_MAP:
        cat = "terms"

    existing = memopedia.find_by_title(title, category=cat)
    if existing:
        page = memopedia.update_page(
            existing.id,
            summary=summary or None,
            content=content,
            keywords=keywords,
            edit_source="ai_conversation",
        )
        if page:
            if existing.vividness in ("buried", "faint"):
                memopedia.update_page(existing.id, vividness="rough")
            return (
                f"Updated page '{title}' (id: {existing.id})\n"
                f"URI: saiverse://self/memopedia/page/{existing.id}"
            )
        return f"Failed to update page '{title}'"

    parent_id = _CATEGORY_ROOT_MAP[cat]
    page = memopedia.create_page(
        parent_id=parent_id,
        title=title,
        summary=summary,
        content=content,
        keywords=keywords,
        vividness="rough",
        edit_source="ai_conversation",
    )
    return (
        f"Created page '{title}' (id: {page.id}, category: {cat})\n"
        f"URI: saiverse://self/memopedia/page/{page.id}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_note",
        description=(
            "Directly write or append to a Memopedia knowledge page. "
            "Use this for quick note-taking from the current conversation context "
            "without running a research loop. "
            "If page_id is provided, content is appended to the existing page. "
            "If page_id is empty, a new page is created (or existing page with "
            "same title is updated)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Content to write (Markdown format)",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Page title. Required for new pages. "
                        "Optional when appending to existing page via page_id."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence summary of the page",
                },
                "category": {
                    "type": "string",
                    "enum": ["people", "terms", "plans"],
                    "description": "Page category (default: terms)",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords for search indexing",
                },
                "page_id": {
                    "type": "string",
                    "description": (
                        "Existing page ID or saiverse:// URI to append to. "
                        "Leave empty to create a new page."
                    ),
                },
            },
            "required": ["content"],
        },
        result_type="string",
    )
