"""複合アクションが、アドオンの native tool を素の名前で呼べることのテスト。

2026-11-23、Stack-chan アドオンの「うなずき」(steps に ``move_head``) が
「ツール 'move_head' は native tool にも MCP サーバーにも見つかりません」で
失敗するという報告があった。2026-07-19 にアドオンの native tool は
TOOL_REGISTRY に ``<addon>__<tool>`` で登録されるようになったが、複合アクションは
素の名前のまま TOOL_REGISTRY を引いていた。

ツールの登録は本物の経路 (``tools._register_tool``) を通し、同じアドオンの MCP
ツールも ``<addon>__<server>__<tool>`` で並べて、末尾一致では一つに決まらない
状況を作る。
"""
from __future__ import annotations

import asyncio
import types

import pytest

import tools
from saiverse import composite_actions as ca
from tools.core import ToolSchema

ADDON = "fake-vessel-addon"


def _schema(name: str) -> ToolSchema:
    return ToolSchema(
        name=name,
        description="test",
        parameters={"type": "object", "properties": {}},
        result_type="string",
        spell=True,
    )


@pytest.fixture
def registered(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def move_head(**kwargs):
        calls.append(("native", kwargs))
        return "moved"

    module = types.SimpleNamespace(schema=lambda: _schema("move_head"), move_head=move_head)
    native_key = f"{ADDON}__move_head"
    mcp_key = f"{ADDON}__stackchan__move_head"
    assert native_key not in tools.TOOL_REGISTRY
    assert tools._register_tool(module, addon_name=ADDON)
    assert native_key in tools.TOOL_REGISTRY

    def mcp_move_head(**kwargs):
        calls.append(("mcp", kwargs))
        return "mcp"

    tools.register_external_tool(mcp_key, _schema("move_head"), mcp_move_head)

    # 機体ごとの gateway しか無い (global の MCP 接続が無い) 状態にする。
    def no_global_conn(addon_name):
        raise RuntimeError("no global connection")

    monkeypatch.setattr(ca, "_get_mcp_connection", no_global_conn)
    try:
        yield calls
    finally:
        tools._remove_registered_tool(native_key)
        tools.unregister_external_tool(mcp_key)


def _nod_action() -> ca.ActionDefinition:
    return ca.ActionDefinition(
        id="nod",
        display_name="うなずく",
        description="test",
        steps=[
            ca.StepDef(
                type="parallel",
                calls=[ca.ToolCall(tool="move_head", args={"yaw": 0, "pitch": 30})],
            )
        ],
    )


def test_bare_tool_name_runs_the_addons_native_tool(registered):
    result = asyncio.run(ca._execute_action_async(ADDON, _nod_action()))
    assert "見つかりません" not in result
    assert result == "move_head: moved"
    assert registered == [("native", {"yaw": 0, "pitch": 30})]


def test_another_addons_tool_is_not_picked_up(registered):
    result = asyncio.run(ca._execute_action_async("other-addon", _nod_action()))
    assert "見つかりません" in result
    assert registered == []
