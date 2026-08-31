"""Unified memory recall: semantic search across Chronicle and Memopedia.

Chronicle results include the full arasuji text.
Memopedia results include the full page summary.
Both include the saiverse:// URI for further navigation.
"""

from __future__ import annotations

from typing import Optional

from tools.context import get_active_persona_id, open_persona_memory
from tools.core import ToolSchema


def memory_recall_unified(
    query: str,
    focus: str = "",
    search_chronicle: bool = True,
    search_memopedia: bool = True,
    search_fragments: bool = True,
) -> str:
    """Search memory semantically across Chronicle and Memopedia.

    Returns ranked results with full Chronicle content and Memopedia summaries,
    plus saiverse:// URIs for further navigation via chronicle_context_up/down
    or memory_read.

    Args:
        query: What to search for (natural language).
        topk: Maximum number of results (default: 5).
        search_chronicle: Include Chronicle entries (default: true).
        search_memopedia: Include Memopedia pages (default: true).
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    with open_persona_memory() as adapter:
        if not adapter.can_embed():
            raise RuntimeError("Semantic search not available (embedding model may be missing)")

        from sai_memory.unified_recall import unified_recall
        from sai_memory.arasuji.storage import get_entry
        from sai_memory.memopedia.storage import get_page

        # unified_recall は生 conn を直接読むため、共有 adapter では外側でロック
        with adapter._db_lock:
            hits = unified_recall(
                adapter.conn,
                adapter.embedder,
                query,
                focus=focus or None,
                search_chronicle=search_chronicle,
                search_memopedia=search_memopedia,
                search_fragments=search_fragments,
                # 生メッセージは検索対象にしない。このツールの契約は Chronicle /
                # Memopedia / Fragment（要約・整理済みの記憶）で、生メッセージは
                # Chronicle が要約形で既にカバーしている。search_messages はデフォルト
                # True なので明示的に False を渡さないと、検索を発行した spell 行その
                # もの（自メッセージ）を拾う自己ヒットが起きる。生メッセージの vivid
                # recall が要る自動想起 (sea/auto_recall.py) だけが True を渡す。
                search_messages=False,
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
                # fragment hits keep their original content (the matched fragment text)

    lines = [f"記憶検索結果: {len(hits)}件\n"]
    for i, hit in enumerate(hits, 1):
        if hit.source_type == "chronicle":
            from datetime import datetime
            start = datetime.fromtimestamp(hit.start_time).strftime("%Y-%m-%d %H:%M") if hit.start_time else "?"
            end = datetime.fromtimestamp(hit.end_time).strftime("%Y-%m-%d %H:%M") if hit.end_time else "?"
            lines.append(f"[{i}] Chronicle Lv{hit.level} | {start} ~ {end} | {hit.message_count}件")
            lines.append(f"    URI: {hit.uri}")
            lines.append("```")
            lines.append(hit.content)
            lines.append("```")
        elif hit.source_type == "fragment":
            date_str = f" ({hit.source_date})" if hit.source_date else ""
            lines.append(f"[{i}] Fragment{date_str}")
            lines.append("```")
            lines.append(f"{hit.title}: {hit.content}")
            lines.append("```")
            lines.append(f"    URI: {hit.uri}")
        elif hit.source_type == "message":
            # 生メッセージヒット。このツールは search_messages=False なので
            # 通常は現れないが、Memopedia の else に落として誤表示するのを防ぐ
            # ため専用分岐を持つ。hit.title は "role @ timestamp"。
            lines.append(f"[{i}] メッセージ: {hit.title}")
            lines.append(f"    URI: {hit.uri}")
            if hit.content:
                lines.append("```")
                lines.append(hit.content)
                lines.append("```")
        else:
            lines.append(f"[{i}] Memopedia: {hit.title}")
            if hit.category:
                lines.append(f"    カテゴリ: {hit.category}")
            lines.append(f"    URI: {hit.uri}")
            if hit.content:
                lines.append("```")
                lines.append(f"概要: {hit.content}")
                lines.append("```")
        lines.append("")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_recall_unified",
        description=(
            "ChronicleとMemopediaを横断してセマンティック検索を行います。"
            "Chronicleはあらすじ全文、MemopediaはページのURIと概要を返します。"
            "取得したURIを使って chronicle_context_up/down や memory_read で"
            "さらに詳しく参照できます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索クエリ（自然言語で、思い出したい内容を記述）",
                },
                "focus": {
                    "type": "string",
                    "enum": ["chronicle", "memopedia", "fragment"],
                    "description": "特定のソースを重点的に検索する場合に指定。指定したソースから4倍多くの結果を取得する",
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
                "search_fragments": {
                    "type": "boolean",
                    "description": "Memopedia Fragmentを検索対象に含める（デフォルト: true）",
                    "default": True,
                },
            },
            "required": ["query"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶想起",
    )
