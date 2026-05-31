"""List fragments of a Memopedia page with numbered indices."""

from __future__ import annotations

import re
from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

_MEMOPEDIA_URI_RE = re.compile(
    r"^saiverse://[^/]+(?:/[^/]+)?/memopedia/page/(?P<page_id>[^?/]+)"
)


def _resolve_page_id(value: str) -> str:
    value = value.strip()
    m = _MEMOPEDIA_URI_RE.match(value)
    if m:
        return m.group("page_id")
    return value


def memopedia_list_fragments(
    page_id: str,
) -> str:
    """List all fragments of a Memopedia page with numbered indices.

    Returns a numbered list showing each fragment's content, date, and ID.
    Use the fragment ID with memopedia_delete_fragment or memopedia_edit_fragment.
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

    resolved_id = _resolve_page_id(page_id)
    page = memopedia.get_page(resolved_id)
    if page is None:
        return f"ページが見つかりません: {resolved_id}"

    fragments = memopedia.get_fragments(resolved_id)
    if not fragments:
        return f"'{page.title}' にフラグメントはありません。"

    lines = [f"'{page.title}' のフラグメント一覧 ({len(fragments)}件):\n"]
    for i, f in enumerate(fragments, 1):
        date_str = f.source_date or "日付なし"
        lines.append(f"[{i}] ({date_str}) {f.content}")
        lines.append(f"    ID: {f.id}")
    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_list_fragments",
        description=(
            "Memopediaページのフラグメント（断片知識）を番号付き一覧で表示します。"
            "重複確認や整理の前に使用してください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "ページID または saiverse:// URI",
                },
            },
            "required": ["page_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="フラグメント一覧",
    )
