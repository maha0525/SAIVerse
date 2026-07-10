"""Memory Atlas: ページに書くスペル (統一スペル動詞 ``memory_write``)。

concept_consolidation.md「P2: 統一スペル動詞 v0.2」の write。宛先ごとの挙動:

- ``m:N``: Memopedia ページ本文への追記 (編集来歴が残る)
- ``core``: 新しいコア記憶を刻む — コア記憶は system プロンプトに常駐する
  「常時開の特殊ページ」
- ``c:N``: 既存コア記憶の上書き

Chronicle (``ch:N``) は書けない (時間の地図の編纂はシステム側)。
``task:N`` (目的の地図) は P2c まで未対応。
"""
from __future__ import annotations

from saiverse import memory_atlas
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import resolve_core_memory_budget


def memory_write(ref: str, content: str) -> str:
    """記憶の地図帳のページに書く (Memopedia 追記 / コア記憶の新規・上書き)。"""
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

    budget = resolve_core_memory_budget(persona_id)
    try:
        with adapter._db_lock:
            return memory_atlas.write_page(adapter, ref, content, core_budget=budget)
    except memory_atlas.AtlasRefError as exc:
        return f"Error: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_write",
        description=(
            "記憶の地図帳（Memory Atlas）のページに書きます。"
            "宛先 m:N は Memopedia ページ本文への追記（編集来歴が残ります）。"
            "宛先 core は新しいコア記憶を刻みます — コア記憶は常時開の特殊ページで、"
            "system プロンプトに常駐し続けます。宛先 c:N は既存コア記憶の上書きです。"
            "Chronicle（ch:N）には書けません（時間の地図の編纂はシステムが行います）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "書き先の参照（m:3 = 追記 / core = コア記憶新規 / c:2 = コア記憶上書き）",
                },
                "content": {
                    "type": "string",
                    "description": "書きたい本文",
                },
            },
            "required": ["ref", "content"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページに書く",
    )
