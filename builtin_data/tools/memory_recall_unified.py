"""Unified memory recall: semantic search across Chronicle and Memopedia.

Chronicle results include the full arasuji text.
Memopedia results include the full page summary.
Both include the saiverse:// URI for further navigation.
"""

from __future__ import annotations

from typing import Optional

from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memory_recall_unified(
    query: str,
    topk: int = 5,
    search_chronicle: bool = True,
    search_memopedia: bool = True,
) -> str:
    """Search memory semantically across Chronicle and Memopedia.

    Returns ranked results with full Chronicle content and Memopedia summaries,
    plus saiverse:// URIs for further navigation via chronicle_context_up/down
    or memopedia_get_page.

    Args:
        query: What to search for (natural language).
        topk: Maximum number of results (default: 5).
        search_chronicle: Include Chronicle entries (default: true).
        search_memopedia: Include Memopedia pages (default: true).
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_dir = get_active_persona_path()

    from saiverse_memory import SAIMemoryAdapter
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.can_embed():
        raise RuntimeError("Semantic search not available (embedding model may be missing)")

    from sai_memory.unified_recall import unified_recall
    from sai_memory.arasuji.storage import get_entry
    from sai_memory.memopedia.storage import get_page

    hits = unified_recall(
        adapter.conn,
        adapter.embedder,
        query,
        topk=topk,
        search_chronicle=search_chronicle,
        search_memopedia=search_memopedia,
        persona_id=persona_id,
    )

    if not hits:
        return "関連する記憶が見つかりませんでした。"

    # Enrich hits with full content
    with adapter._db_lock:
        for hit in hits:
            if hit.source_type == "chronicle":
                entry = get_entry(adapter.conn, hit.source_id)
                if entry:
                    hit.content = entry.content
            elif hit.source_type == "memopedia":
                page = get_page(adapter.conn, hit.source_id)
                if page:
                    hit.content = page.summary or ""

    lines = [f"記憶検索結果: {len(hits)}件\n"]
    for i, hit in enumerate(hits, 1):
        if hit.source_type == "chronicle":
            from datetime import datetime
            start = datetime.fromtimestamp(hit.start_time).strftime("%Y-%m-%d %H:%M") if hit.start_time else "?"
            end = datetime.fromtimestamp(hit.end_time).strftime("%Y-%m-%d %H:%M") if hit.end_time else "?"
            lines.append(f"[{i}] Chronicle Lv{hit.level} | {start} ~ {end} | {hit.message_count}件")
            lines.append(f"    URI: {hit.uri}")
            lines.append(f"    {hit.content}")
        else:
            lines.append(f"[{i}] Memopedia: {hit.title}")
            if hit.category:
                lines.append(f"    カテゴリ: {hit.category}")
            lines.append(f"    URI: {hit.uri}")
            if hit.content:
                lines.append(f"    概要: {hit.content}")
        lines.append("")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_recall_unified",
        description=(
            "ChronicleとMemopediaを横断してセマンティック検索を行います。"
            "Chronicleはあらすじ全文、MemopediaはページのURIと概要を返します。"
            "取得したURIを使って chronicle_context_up/down や memopedia_get_page で"
            "さらに詳しく参照できます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索クエリ（自然言語で、思い出したい内容を記述）",
                },
                "topk": {
                    "type": "integer",
                    "description": "最大結果数（デフォルト: 5）",
                    "default": 5,
                },
                "search_chronicle": {
                    "type": "boolean",
                    "description": "Chronicleを検索対象に含める（デフォルト: true）",
                    "default": True,
                },
                "search_memopedia": {
                    "type": "boolean",
                    "description": "Memopediaを検索対象に含める（デフォルト: true）",
                    "default": True,
                },
            },
            "required": ["query"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶想起",
    )
