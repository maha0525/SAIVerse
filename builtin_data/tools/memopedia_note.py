"""Write knowledge fragments to Memopedia pages.

P4 庭仕事ワーカーの素材として内部専用化 (concept_consolidation.md)。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import List, Optional

from sai_memory.memopedia.storage import resolve_page_ref, category_keys
from saiverse.references import to_short_ref, to_uri
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Map category names to root page IDs (レジストリから生成 — ハードコード禁止)
_CATEGORY_ROOT_MAP = {k: f"root_{k}" for k in category_keys("writable")}


def memopedia_note(
    content: str,
    title: str = "",
    summary: str = "",
    category: str = "terms",
    keywords: Optional[List[str]] = None,
    page_id: str = "",
) -> str:
    """Write a knowledge fragment to a Memopedia page.

    - content: the fact or note to record (one concise statement)
    - title: page (entity) title — required for new pages, optional when page_id given
    - summary: 1-2 sentence page summary (optional, updates page-level summary)
    - category: CATEGORY_DEFS の writable キーのいずれか (default: terms)
    - keywords: list of keywords for search (optional)
    - page_id: existing page ID or saiverse:// URI to write to (optional)

    Content is stored as a Fragment linked to the page, making it
    individually searchable via embedding recall. If the page doesn't
    exist, it is created first.
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
    today = date.today().isoformat()

    # --- Resolve target page ---
    target_page = None

    if page_id:
        resolved_id = resolve_page_ref(adapter.conn, page_id)
        target_page = memopedia.get_page(resolved_id) if resolved_id else None
        if target_page is None:
            return f"Page not found: {page_id}"
    else:
        if not title:
            return "Error: title is required when creating a new page (no page_id given)"

        cat = category.lower().strip()
        if cat not in _CATEGORY_ROOT_MAP:
            cat = "terms"

        existing = memopedia.find_by_title(title, category=cat)
        if existing:
            target_page = existing
        else:
            parent_id = _CATEGORY_ROOT_MAP[cat]
            target_page = memopedia.create_page(
                parent_id=parent_id,
                title=title,
                summary=summary or "",
                content="",
                keywords=keywords,
                vividness="rough",
                edit_source="ai_conversation",
            )

    # --- Update page metadata if provided ---
    meta_updates = {}
    if summary:
        meta_updates["summary"] = summary
    if keywords:
        meta_updates["keywords"] = keywords
    if title and target_page.title != title:
        meta_updates["title"] = title
    if meta_updates:
        memopedia.update_page(
            target_page.id,
            edit_source="ai_conversation",
            **meta_updates,
        )

    # --- Create fragment ---
    memopedia.create_fragment(
        entity_id=target_page.id,
        content=content,
        source_date=today,
    )

    if target_page.short_id is not None:
        short_ref = to_short_ref("memopedia", target_page.short_id)
        uri = to_uri("memopedia", target_page.short_id)
    else:
        short_ref = target_page.id
        uri = to_uri("memopedia", target_page.id)
    return (
        f"Fragment written to '{target_page.title}' ({short_ref})\n"
        f"URI: {uri}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_note",
        description=(
            "Write a knowledge fragment to a Memopedia page. "
            "Each call creates one fragment (a single fact or note) linked to the page. "
            "Fragments are individually searchable via embedding recall. "
            "If the page doesn't exist, it is created. "
            "Use for quick note-taking from conversation context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact or note to record (one concise statement)",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Page (entity) title. Required for new pages. "
                        "Optional when writing to existing page via page_id."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence page summary (updates page-level summary)",
                },
                "category": {
                    "type": "string",
                    "enum": category_keys("writable"),
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
                        "Existing page ref (m:1), UUID, or saiverse:// URI. "
                        "Leave empty to create a new page or find by title."
                    ),
                },
            },
            "required": ["content"],
        },
        result_type="string",
        spell=False,
        spell_display_name="メモペディア書き込み",
    )
