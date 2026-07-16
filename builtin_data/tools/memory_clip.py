"""Memory Atlas: クリップを切り出して貼るスペル (統一スペル動詞 ``memory_clip``)。

concept_consolidation.md「クリップの見え方」+ P2c-0 決定2 の実装。
クリップ＝土地 (生ログ) をそのまま写す参照。貼り方は 2 種:

- **参照貼り** (``mode='attach'``、既定): ページには抜粋が表示され、全文は
  ``memory_read clip:N``。ページがクリップに食われない
- **転写** (``mode='transcribe'``): 本文に焼き込み、常に生で見える。貼り先必須。
  core 宛の範囲転写は旧 SCENE (core_memory_add_scene) と同一の共有ロジック。
  転写でもクリップは切り出され、由来参照が残る

- 点クリップ: ``quote`` (逐語引用) を指定。本文との一致を検証する
- 範囲クリップ: ``quote`` を省略し、``anchor`` 前後 ``rounds`` 往復の実会話窓を写す
  (core_memory_add_scene と同じ窓切り出し)
"""
from __future__ import annotations

from typing import Optional

from saiverse import memory_atlas
from tools.context import get_active_persona_id, open_persona_memory
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import (
    parse_message_ref,
    resolve_core_memory_budget,
    resolve_persona_display_name,
)

DEFAULT_ROUNDS = 3


def memory_clip(
    anchor: str,
    quote: Optional[str] = None,
    rounds: int = DEFAULT_ROUNDS,
    paste_to: Optional[str] = None,
    mode: str = "attach",
) -> str:
    """クリップを切り出す (点=引用 / 範囲=切り抜き)。paste_to 指定で即貼り。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    mid = parse_message_ref(anchor)
    if not mid:
        return f"Error: anchor を解釈できませんでした: {anchor}"

    persona_name = resolve_persona_display_name(persona_id)
    budget = resolve_core_memory_budget(persona_id)
    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")
        try:
            with adapter._db_lock:
                return memory_atlas.make_clip(
                    adapter, mid,
                    quote=quote, rounds=rounds, paste_to=paste_to,
                    persona_name=persona_name, mode=mode, core_budget=budget,
                )
        except memory_atlas.AtlasRefError as exc:
            return f"Error: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_clip",
        description=(
            "会話の生ログからクリップを切り出し、記憶の地図帳のページに貼ります。"
            "quote を指定すると点クリップ（そのメッセージ内の逐語引用。本文と一字一句"
            "一致している必要があります）、省略すると範囲クリップ（anchor の前後 rounds "
            "往復の会話の切り抜き）になります。"
            "貼り方は2種: mode='attach'（既定・参照貼り — ページには抜粋が表示され、"
            "全文は memory_read clip:N で読めます）/ mode='transcribe'（転写 — 本文に"
            "焼き込み、常に生の全文が見えます。paste_to 必須。転写先は memopedia:N または "
            "core = 新しいコア記憶）。参照貼りの paste_to は memopedia:N / core:N、省略すると"
            "貼らずに保管します。"
            "anchor は自動想起で示されたハンドル（saiverse://self/message/... 形式）"
            "や生のメッセージ ID をそのまま渡せます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "anchor": {
                    "type": "string",
                    "description": "対象メッセージの ID（URI 形式可）。点クリップの対象 / 範囲クリップの中心",
                },
                "quote": {
                    "type": "string",
                    "description": "点クリップにする場合の逐語引用（本文にある言葉をそのまま）",
                },
                "rounds": {
                    "type": "integer",
                    "description": "範囲クリップの場合、anchor 前後に含めるおおよその往復数（既定 3）",
                },
                "paste_to": {
                    "type": "string",
                    "description": (
                        "貼り先の参照（attach: 任意、例 m:3 / c:2。省略で保管のみ。"
                        "transcribe: 必須、m:3 または core）"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["attach", "transcribe"],
                    "default": "attach",
                    "description": "貼り方（attach = 参照貼り・抜粋表示 / transcribe = 本文に転写・常に全文）",
                },
            },
            "required": ["anchor"],
        },
        result_type="string",
        spell=True,
        spell_display_name="クリップを切り出して貼る",
    )
