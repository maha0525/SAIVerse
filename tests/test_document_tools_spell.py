"""document ツールの spell 宣言テスト (自律行動v2 §2.2 / §11)。

作業セッション (WORKER) の生産手段である document スペル群が
スペル語彙に載る (= schema が spell=True を宣言する) ことを守る。
道具はあったのにスペル語彙から抜けていた、を再発させない。
"""
import pytest

from tool_loader import load_builtin_tool
from tools.core import ToolSchema


DOCUMENT_SPELL_TOOLS = [
    "document_create",
    "document_read",
    "document_edit",
    "document_append_content",
    "document_search",
]


@pytest.mark.parametrize("name", DOCUMENT_SPELL_TOOLS)
def test_document_tool_is_spell_with_display_name(name):
    module = load_builtin_tool(name)
    schema = module.schema()
    assert isinstance(schema, ToolSchema)
    assert schema.name == name
    assert schema.spell is True, f"{name} must have spell=True"
    assert schema.spell_display_name, f"{name} must have non-empty spell_display_name"
