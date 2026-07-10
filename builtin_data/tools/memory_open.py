"""Memory Atlas: ページを机に開いておくスペル (統一スペル動詞 ``memory_open``)。

concept_consolidation.md「開閉制御 — 机の物理」の実装。机に開いたページは
Metabolism を跨いで head に残り続ける — 高価で明示的な行為。机は文字数予算を
持ち、溢れると最も長く触られていないページから自動的に棚へ戻る (LRU)。

対応 ref: ``m:N`` (Memopedia) / ``ch:N`` (Chronicle)。コア記憶は常時開の
システム常設ピンなので対象外 (``core`` / ``c:N`` は「既に開いています」を
返す)。``task:N`` (目的の地図) は P2c まで未対応。
"""
from __future__ import annotations

from typing import Optional

from saiverse import memory_atlas
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memory_open(ref: str, purpose_ref: Optional[str] = None) -> str:
    """ページを机に開いたままにする (Metabolism を跨いで head に残り続ける)。"""
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

    try:
        return memory_atlas.open_page(adapter, ref, purpose_ref=purpose_ref)
    except memory_atlas.AtlasRefError as exc:
        return f"Error: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_open",
        description=(
            "記憶の地図帳（Memory Atlas）の1ページを机に開いたままにします。"
            "以後の思考で常に見える状態が続きますが、机の広さ（文字数予算）を"
            "消費します。机が溢れると、長く触っていないページから自動的に"
            "棚へ戻ります。"
            "参照は m:N（Memopedia）/ ch:N（Chronicle）の形式です"
            "（コア記憶は常時開のため対象外です）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "机に開きたいページの参照（例: m:3 / ch:5）",
                },
                "purpose_ref": {
                    "type": "string",
                    "description": "この開きが紐づく目的の参照（任意、例: task:2）",
                },
            },
            "required": ["ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページを机に開く",
    )
