"""Memory Atlas: ページを机から閉じる (棚に戻す) スペル (統一スペル動詞 ``memory_close``)。

concept_consolidation.md「開閉制御 — 机の物理」の実装。``memory_open`` で
机に開いたページを明示的に閉じる。閉じても目次 (検索・想起) からは消えない
ので、閉じることを怖がる必要はない。

対応 ref: ``m:N`` (Memopedia) / ``ch:N`` (Chronicle)。コア記憶は常時開の
システム常設ピンなので対象外 (``core`` / ``c:N`` は「閉じられません」を返す)。
``task:N`` (目的の地図) は P2b まで未対応。
"""
from __future__ import annotations

from saiverse import memory_atlas
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memory_close(ref: str) -> str:
    """机に開いているページを閉じる (棚に戻す)。"""
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
        return memory_atlas.close_page(adapter, ref)
    except memory_atlas.AtlasRefError as exc:
        return f"Error: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_close",
        description=(
            "机に開いた記憶の地図帳のページを閉じ、棚に戻します。"
            "閉じても目次（検索・想起）からは消えません。必要ならまた開けます。"
            "参照は m:N（Memopedia）/ ch:N（Chronicle）の形式です"
            "（コア記憶は常時開のため対象外です）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "閉じたいページの参照（例: m:3 / ch:5）",
                },
            },
            "required": ["ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページを机から閉じる",
    )
