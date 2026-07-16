"""Memory Atlas: 地図帳を検索するスペル (統一スペル動詞 ``memory_search``)。

concept_consolidation.md「P2: 統一スペル動詞 v0.2」の search。Memopedia の
タイトル/全文検索と Chronicle の content 全文検索を横断し、上位数件を
タイトル + ref + 一行プレビューで返す。随意想起 (memory_recall) とは別物
(検索は明示的なキーワード照合、想起は意味的な連想)。
"""
from __future__ import annotations

from saiverse import memory_atlas
from tools.context import get_active_persona_id, open_persona_memory
from tools.core import ToolSchema


def memory_search(query: str) -> str:
    """記憶の地図帳をタイトル/全文検索する。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")
        # search_pages は Chronicle 検索で生 conn を無ロックで読むため外側でロック
        with adapter._db_lock:
            return memory_atlas.search_pages(adapter, query)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_search",
        description=(
            "記憶の地図帳（Memopedia のページ・Chronicle の章）をキーワードで"
            "検索します。タイトル・本文に含まれる語句で照合し、一致したページを "
            "参照（memopedia:N / chronicle:N）と一行プレビューで一覧します。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索したいキーワード",
                },
            },
            "required": ["query"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶の地図帳を検索する",
    )
