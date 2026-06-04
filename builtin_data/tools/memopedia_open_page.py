"""Tool to open a Memopedia page and get its content."""

from __future__ import annotations

from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memopedia_open_page(page_id: str) -> str:
    """Open a Memopedia page and return its full content.

    - page_id: The ID of the page to open

    Returns the page content in Markdown format, including title, summary, and full content.
    Also lists any child pages.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_dir = get_active_persona_path()
    adapter: Optional[SAIMemoryAdapter]
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    from sai_memory.memopedia import Memopedia

    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)

    # Get thread_id from adapter's active state
    thread_id = adapter._thread_id(None)

    # Resolve page ref (m:N / UUID) then open
    page = memopedia.get_page(page_id)
    if page is None:
        return f"ページが見つかりません: {page_id}"
    resolved_id = page.id

    result = memopedia.open_page(thread_id, resolved_id)
    if "error" in result:
        return f"Error: {result['error']}"

    short_ref = f"m:{page.short_id}" if page.short_id else resolved_id
    lines = [f"# {result['title']} ({short_ref})"]
    if result.get("summary"):
        lines.append(f"\n*{result['summary']}*")

    body = memopedia.render_page_body(resolved_id)
    if body:
        lines.append(f"\n{body}")

    children = result.get("children", [])
    if children:
        lines.append("\n## 子ページ")
        for child in children:
            child_page = memopedia.get_page(child['id'])
            child_ref = f"m:{child_page.short_id}" if child_page and child_page.short_id else child['id'][:8]
            lines.append(f"- **{child['title']}** ({child_ref}): {child.get('summary', '')}")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_open_page",
        description="Open a Memopedia page to read its full content. The page will be marked as 'open' and its content will be included in context.",
        parameters={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Page ref (m:1), UUID, or saiverse:// URI",
                },
            },
            "required": ["page_id"],
        },
        result_type="string",
    )
