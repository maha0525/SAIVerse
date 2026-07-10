"""Memory Atlas: 写真を撮ってクリップで貼るスペル (統一スペル動詞 ``memory_clip``)。

concept_consolidation.md「clip と写真の見え方」の実装。写真＝土地 (生ログ) を
そのまま写す参照で、貼り方は**参照貼り**のみ (ページには抜粋が表示され、全文は
``memory_read p:N``)。本文への転写 (常に生で見える形) は既存の
core_memory_add_scene の役割のままで、このスペルは行わない。

- 点写真: ``quote`` (逐語引用) を指定。本文との一致を検証する
- 範囲写真: ``quote`` を省略し、``anchor`` 前後 ``rounds`` 往復の実会話窓を写す
  (core_memory_add_scene と同じ窓切り出し)
"""
from __future__ import annotations

from typing import Optional

from saiverse import memory_atlas
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import (
    parse_message_ref,
    resolve_persona_display_name,
)

DEFAULT_ROUNDS = 3


def memory_clip(
    anchor: str,
    quote: Optional[str] = None,
    rounds: int = DEFAULT_ROUNDS,
    paste_to: Optional[str] = None,
) -> str:
    """写真を撮る (点=引用 / 範囲=切り抜き)。paste_to 指定で即貼り。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    mid = parse_message_ref(anchor)
    if not mid:
        return f"Error: anchor を解釈できませんでした: {anchor}"

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    persona_name = resolve_persona_display_name(persona_id)
    try:
        with adapter._db_lock:
            return memory_atlas.clip_photo(
                adapter, mid,
                quote=quote, rounds=rounds, paste_to=paste_to,
                persona_name=persona_name,
            )
    except memory_atlas.AtlasRefError as exc:
        return f"Error: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_clip",
        description=(
            "会話の生ログから写真を撮り、記憶の地図帳のページにクリップで貼ります。"
            "quote を指定すると点写真（そのメッセージ内の逐語引用。本文と一字一句"
            "一致している必要があります）、省略すると範囲写真（anchor の前後 rounds "
            "往復の会話の切り抜き）になります。"
            "写真は参照であり、貼り先のページには抜粋が表示されます"
            "（全文は memory_read p:N で読めます）。"
            "paste_to（m:N / c:N）を指定すると撮った瞬間に貼ります。"
            "anchor は自動想起で示されたハンドル（saiverse://self/message/... 形式）"
            "や生のメッセージ ID をそのまま渡せます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "anchor": {
                    "type": "string",
                    "description": "対象メッセージの ID（URI 形式可）。点写真の対象 / 範囲写真の中心",
                },
                "quote": {
                    "type": "string",
                    "description": "点写真にする場合の逐語引用（本文にある言葉をそのまま）",
                },
                "rounds": {
                    "type": "integer",
                    "description": "範囲写真の場合、anchor 前後に含めるおおよその往復数（既定 3）",
                },
                "paste_to": {
                    "type": "string",
                    "description": "貼り先の参照（任意、例: m:3 / c:2）。省略すると貼らずに保管",
                },
            },
            "required": ["anchor"],
        },
        result_type="string",
        spell=True,
        spell_display_name="写真を撮って貼る",
    )
