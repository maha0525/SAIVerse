"""Memory Atlas: ページをごみ箱へ移すスペル (統一スペル動詞 ``memory_delete``)。

concept_consolidation.md P2c-0 決定1: core + Memopedia の soft-delete を統一する
動詞。どちらのストアも soft-delete (コア記憶 = ``deleted_at`` / Memopedia =
``is_deleted``) なので「ごみ箱に移動 — 完全には消えない」が事実。ごみ箱を漁る
動詞 (閲覧・復元) は後回し (同決定)。

対応 ref: ``c:N`` (コア記憶) / ``m:N`` (Memopedia)。``ch:N`` (編纂はシステム側) /
``p:N`` (写真は歴史として残る) / ``core`` 全体 / ``task:N`` (purpose_close の
領分) は消せない。削除したページが机に開いていたら机からも即時下ろす。
"""
from __future__ import annotations

from saiverse import memory_atlas
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memory_delete(ref: str) -> str:
    """記憶の地図帳のページをごみ箱へ移す (soft-delete)。"""
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
        return memory_atlas.delete_page(adapter, ref)
    except memory_atlas.AtlasRefError as exc:
        return f"Error: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_delete",
        description=(
            "記憶の地図帳（Memory Atlas）のページをごみ箱に移動します"
            "（完全に消えるわけではありません）。"
            "対象は c:N（コア記憶1件）と m:N（Memopedia ページ）です。"
            "Chronicle（ch:N）と写真（p:N）は消せません。"
            "目的ノード（task:N）を終えるには purpose_close を使ってください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "ごみ箱へ移すページの参照（例: c:3 / m:5）",
                },
            },
            "required": ["ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページをごみ箱へ",
    )
