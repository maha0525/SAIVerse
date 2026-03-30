"""Unified recall entry point: search across Chronicle and Memopedia."""

from __future__ import annotations

from typing import Optional

from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def recall_entry(
    query: str,
    topk: int = 5,
    search_chronicle: bool = True,
    search_memopedia: bool = True,
) -> str:
    """Search memory across Chronicle and Memopedia using semantic similarity.

    Returns ranked results with URIs for further navigation via recall_navigate.

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

    # Store recalled IDs in working memory
    for hit in hits:
        adapter.add_recalled_id(
            source_type=hit.source_type,
            source_id=hit.source_id,
            title=hit.title,
            uri=hit.uri,
        )

    lines = [f"検索結果: {len(hits)}件\n"]
    for i, hit in enumerate(hits, 1):
        source_label = "Chronicle" if hit.source_type == "chronicle" else "Memopedia"
        lines.append(f"[{i}] ({source_label}) {hit.title}")
        lines.append(f"    スコア: {hit.score:.4f}")
        lines.append(f"    URI: {hit.uri}")
        if hit.content:
            lines.append(f"    概要: {hit.content}")
        if hit.message_count:
            lines.append(f"    メッセージ数: {hit.message_count}")
        lines.append("")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="recall_entry",
        description=(
            "記憶を検索します。Chronicle（あらすじ）とMemopedia（知識ベース）を横断して、"
            "クエリに関連する記憶を探します。結果にはURIが含まれており、recall_navigateで"
            "詳細を確認できます。"
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
    )
