"""Save or update a Memopedia page (find-or-create by title)."""

from __future__ import annotations

from typing import List, Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


# Map category names to root page IDs
_CATEGORY_ROOT_MAP = {
    "people": "root_people",
    "terms": "root_terms",
    "plans": "root_plans",
}


def memopedia_save_page(
    title: str,
    summary: str = "",
    content: str = "",
    category: str = "terms",
    keywords: Optional[List[str]] = None,
) -> str:
    """Save a Memopedia page. Creates new or updates existing (matched by title).

    - title: page title
    - summary: 1-2 sentence summary
    - content: full page content (Markdown)
    - category: one of 'people', 'terms', 'plans'
    - keywords: list of keywords for search
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    from sai_memory.memopedia import Memopedia

    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)

    # Normalize category
    cat = category.lower().strip()
    if cat not in _CATEGORY_ROOT_MAP:
        cat = "terms"

    # Check if page already exists by title
    existing = memopedia.find_by_title(title, category=cat)

    if existing:
        # Update existing page
        page = memopedia.update_page(
            existing.id,
            summary=summary,
            content=content,
            keywords=keywords,
            edit_source="ai_conversation",
        )
        if page:
            # Promote buried/faint pages on update
            if existing.vividness in ("buried", "faint"):
                memopedia.update_page(existing.id, vividness="rough")
            return f"Updated page '{title}' (id: {existing.id})"
        return f"Failed to update page '{title}'"
    else:
        # Create new page under the appropriate root
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
        return f"Created page '{title}' (id: {page.id}, category: {cat})"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_save_page",
        description=(
            "Save a Memopedia knowledge page. "
            "If a page with the same title exists, it is updated. "
            "Otherwise a new page is created under the specified category."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Page title",
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence summary of the page",
                },
                "content": {
                    "type": "string",
                    "description": "Full page content in Markdown",
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
            },
            "required": ["title"],
        },
        result_type="string",
    )
