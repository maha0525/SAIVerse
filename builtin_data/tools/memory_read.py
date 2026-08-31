"""Memory Atlas: ページを読むスペル (統一スペル動詞 ``memory_read``)。

concept_consolidation.md「P2: 統一スペル動詞 v0.2」の read。中身がその場の
会話の流れ (tail) に入り、Metabolism で圧縮されて流れていく — 机の場所は
取らない、既定の行為。常に見える状態を保ちたいときは ``memory_open`` を使う。

対応 ref: ``m:N`` (Memopedia) / ``core`` (コア記憶全件) / ``c:N`` (コア記憶1件)
/ ``ch:N`` (Chronicle) / ``p:N`` (クリップ — クリップが写す生ログの全文)。

``task:N`` (目的ノード) の読み取りも通る — ただし目的の木は 2026-08-23 に
退役したので、説明文からは降ろしてある (自動想起が古い参照を出したときに
読めないと困るための読み取り専用の残置。
docs/issues/purpose_tree_vs_pocketbook_succession.md)。
"""
from __future__ import annotations

from saiverse import memory_atlas
from tools.context import (
    get_active_manager,
    get_active_persona_id,
    open_persona_memory,
)
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import resolve_persona_display_name


def memory_read(ref: str) -> str:
    """記憶の地図帳の1ページをその場で読む (机の場所は取らない)。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_name = resolve_persona_display_name(persona_id)
    # manager は task:N (目的ノード = main DB 在住) の解決にのみ使われる
    manager = get_active_manager()
    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")
        try:
            # read_page は生 conn を部分的に無ロックで読むため、共有 adapter
            # では外側でロックを取る (RLock なので内部ロックと重ねて安全)
            with adapter._db_lock:
                return memory_atlas.read_page(
                    adapter, ref, persona_name=persona_name, manager=manager,
                )
        except memory_atlas.AtlasRefError as exc:
            return f"Error: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_read",
        description=(
            "記憶の地図帳（Memory Atlas）の1ページをその場で読みます。"
            "読んだ内容は会話の流れに残り、時間とともに流れていきます"
            "（机の場所は取りません）。常に見える状態を保ちたい場合は "
            "memory_open を使ってください。"
            "参照は memopedia:N（Memopedia）/ core（コア記憶全件）/ core:N（コア記憶1件）/ "
            "chronicle:N（Chronicle）/ clip:N（クリップ — そのクリップが写す会話の生ログ全文）"
            "の形式です。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "読みたいページの参照（例: m:3 / core / c:2 / ch:5 / p:1）",
                },
            },
            "required": ["ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページを読む",
    )
