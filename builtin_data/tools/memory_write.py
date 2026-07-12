"""Memory Atlas: ページに書くスペル (統一スペル動詞 ``memory_write``)。

concept_consolidation.md「P2: 統一スペル動詞 v0.2」+ P2c-0 決定3 の write:

- ``m:N``: Memopedia ページ本文への追記 (編集来歴が残る)
- ``core``: 新しいコア記憶を刻む — コア記憶は system プロンプトに常駐する
  「常時開の特殊ページ」
- ``c:N``: 既存コア記憶の上書き
- ``ref`` の代わりに ``title`` (+ ``category``): Memopedia に**新規ページ作成**

構造編集 (移動・統合・分割・summary/keywords 更新) は日常動詞にしない
(P2c-0 決定3 — 庭仕事モードの遅延開示は別仕様・後回し)。
Chronicle (``ch:N``) は書けない (時間の地図の編纂はシステム側)。
"""
from __future__ import annotations

from typing import Optional

from saiverse import memory_atlas
from tools.context import get_active_persona_id, open_persona_memory
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import resolve_core_memory_budget
from sai_memory.memopedia.storage import category_keys


def memory_write(
    ref: Optional[str] = None,
    content: str = "",
    title: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """記憶の地図帳のページに書く (追記 / コア記憶の新規・上書き / 新規ページ作成)。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    budget = resolve_core_memory_budget(persona_id)
    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")
        try:
            with adapter._db_lock:
                return memory_atlas.write_page(
                    adapter, ref, content,
                    title=title, category=category, core_budget=budget,
                )
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
            "ref の代わりに title（と category）を指定すると、Memopedia に新しい"
            "ページを作ります。ref と title はどちらか一方だけ指定してください。"
            "Chronicle（ch:N）には書けません（時間の地図の編纂はシステムが行います）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": (
                        "書き先の参照（m:3 = 追記 / core = コア記憶新規 / "
                        "c:2 = コア記憶上書き）。title とは排他"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "書きたい本文",
                },
                "title": {
                    "type": "string",
                    "description": "新しく作るページの題名。ref とは排他",
                },
                "category": {
                    "type": "string",
                    "enum": category_keys("writable"),
                    "description": "新規ページのカテゴリ（title 指定時のみ。既定: terms）",
                },
            },
            "required": ["content"],
        },
        result_type="string",
        spell=True,
        spell_display_name="記憶のページに書く",
    )
