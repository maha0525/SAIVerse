"""Memory Atlas: ページを読むスペル (統一スペル動詞 ``memory_read``)。

concept_consolidation.md「P2: 統一スペル動詞 v0.2」の read。中身がその場の
会話の流れ (tail) に入り、Metabolism で圧縮されて流れていく — 机の場所は
取らない、既定の行為。常に見える状態を保ちたいときは ``memory_open`` を使う。

対応 ref: ``m:N`` (Memopedia) / ``core`` (コア記憶全件) / ``c:N`` (コア記憶1件)
/ ``ch:N`` (Chronicle) / ``p:N`` (写真 — 写真が写す生ログの全文) / ``task:N``
(目的ノード — 段階・状態・ステップ・貼られた写真。P2c-1 で解決)。
"""
from __future__ import annotations

from saiverse import memory_atlas
from saiverse_memory import SAIMemoryAdapter
from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_persona_path,
)
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import resolve_persona_display_name


def memory_read(ref: str) -> str:
    """記憶の地図帳の1ページをその場で読む (机の場所は取らない)。"""
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

    persona_name = resolve_persona_display_name(persona_id)
    # manager は task:N (目的ノード = main DB 在住) の解決にのみ使われる
    manager = get_active_manager()
    try:
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
            "参照は m:N（Memopedia）/ core（コア記憶全件）/ c:N（コア記憶1件）/ "
            "ch:N（Chronicle）/ p:N（写真 — その写真が写す会話の生ログ全文）/ "
            "task:N（目的ノード — 段階・ステップ・貼られた写真）の形式です。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "読みたいページの参照（例: m:3 / core / c:2 / ch:5 / p:1 / task:4）",
                },
            },
            "required": ["ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページを読む",
    )
