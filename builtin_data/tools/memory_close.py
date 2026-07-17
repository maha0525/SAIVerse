"""Memory Atlas: ページを机から閉じる (棚に戻す) スペル (統一スペル動詞 ``memory_close``)。

concept_consolidation.md「開閉制御 — 机の物理」の実装。``memory_open`` で
机に開いたページを明示的に閉じる。閉じても目次 (検索・想起) からは消えない
ので、閉じることを怖がる必要はない。

対応 ref: ``m:N`` (Memopedia) / ``ch:N`` (Chronicle) / ``task:N`` (目的ノード。
P3c①②で対応)。コア記憶は常時開のシステム常設ピンなので対象外 (``core`` /
``c:N`` は「閉じられません」を返す)。
"""
from __future__ import annotations

from saiverse import memory_atlas
from tools.context import (
    get_active_manager,
    get_active_persona_id,
    open_persona_memory,
)
from tools.core import ToolSchema


def memory_close(ref: str) -> str:
    """机に開いているページを閉じる (棚に戻す)。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    # manager は task:N (目的ノード = main DB 在住) の解決にのみ使われる
    manager = get_active_manager()
    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")
        try:
            # close_page は ref 正規化で生 conn を読むため外側でロック
            with adapter._db_lock:
                result = memory_atlas.close_page(adapter, ref, manager=manager)
        except memory_atlas.AtlasRefError as exc:
            return f"Error: {exc}"

    # head 操作の内容型通知 (§6-4): 机 (desk) の render 断片を全 Session 窓へ。
    # 失敗 (Error 文字列) 時は通知しない。ヘルパー側は決して raise しない。
    if not (result or "").startswith("Error"):
        from sea.head_pipeline.notify import notify_head_mutation_from_tool_context

        notify_head_mutation_from_tool_context(
            "desk",
            operation_label=f"ページを机から閉じました ({ref})",
        )
    return result


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_close",
        description=(
            "机に開いた記憶の地図帳のページを閉じ、棚に戻します。"
            "閉じても目次（検索・想起）からは消えません。必要ならまた開けます。"
            "参照は memopedia:N（Memopedia）/ chronicle:N（Chronicle）/ task:N（目的ノード）"
            "の形式です（コア記憶は常時開のため対象外です）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "閉じたいページの参照（例: m:3 / ch:5 / task:2）",
                },
            },
            "required": ["ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページを机から閉じる",
    )
