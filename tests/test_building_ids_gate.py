"""Tests for building_ids visibility filter + execute-time gate.

Phase 4' / A-3-c: native tools (e.g. saiverse-stackchan-addon's see / env3)
declare ``building_ids`` in their ToolSchema. The filter must apply to both
listing (spell_list section) and execution (TOOL_REGISTRY callable).
"""
from typing import Any, Optional
from unittest.mock import patch

import pytest

from tools.core import ToolSchema


def _make_schema(name: str, building_ids=None) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=f"{name} desc",
        parameters={"type": "object", "properties": {}},
        result_type="string",
        spell=True,
        spell_display_name=name + " 表示",
        spell_visible=True,
        building_ids=building_ids,
    )


class _FakePersona:
    def __init__(self, building_id: Optional[str]) -> None:
        self.current_building_id = building_id


class _FakeManager:
    def __init__(self, persona_id: str, building_id: Optional[str]) -> None:
        self.all_personas = {persona_id: _FakePersona(building_id)}
        self.personas = self.all_personas


# ---- is_tool_available_for_persona (visibility filter) ----


def test_visibility_native_tool_inside_vessel(monkeypatch):
    """native tool with building_ids should be visible inside the vessel building."""
    import tools as tools_module
    from tools.mcp_client import MCPClientManager

    monkeypatch.setitem(
        tools_module.SPELL_TOOL_SCHEMAS, "see",
        _make_schema("see", building_ids=["b_vessel"]),
    )
    mgr = MCPClientManager()
    assert mgr.is_tool_available_for_persona("see", "air", "b_vessel") is True


def test_visibility_native_tool_outside_vessel_hidden(monkeypatch):
    """native tool with building_ids should be hidden outside the vessel building."""
    import tools as tools_module
    from tools.mcp_client import MCPClientManager

    monkeypatch.setitem(
        tools_module.SPELL_TOOL_SCHEMAS, "see",
        _make_schema("see", building_ids=["b_vessel"]),
    )
    mgr = MCPClientManager()
    assert mgr.is_tool_available_for_persona("see", "air", "b_lobby") is False


def test_visibility_native_tool_without_building_ids_unrestricted(monkeypatch):
    """native tool without building_ids should be visible everywhere."""
    import tools as tools_module
    from tools.mcp_client import MCPClientManager

    monkeypatch.setitem(
        tools_module.SPELL_TOOL_SCHEMAS, "memory_recall",
        _make_schema("memory_recall", building_ids=None),
    )
    mgr = MCPClientManager()
    assert mgr.is_tool_available_for_persona("memory_recall", "air", "b_anywhere") is True


def test_visibility_unknown_tool_fail_open(monkeypatch):
    """A tool not in SPELL_TOOL_SCHEMAS and not in _registered_tools fails open
    (= visible) — preserves existing fail-open semantics for non-spell tools."""
    from tools.mcp_client import MCPClientManager

    mgr = MCPClientManager()
    assert mgr.is_tool_available_for_persona("not_a_spell", "air", "b_anywhere") is True


def test_visibility_building_id_none_skips_filter(monkeypatch):
    """building_id=None (e.g. UI summon listing) skips the filter so callers
    without a building context see the otherwise-visible tools."""
    import tools as tools_module
    from tools.mcp_client import MCPClientManager

    monkeypatch.setitem(
        tools_module.SPELL_TOOL_SCHEMAS, "see",
        _make_schema("see", building_ids=["b_vessel"]),
    )
    mgr = MCPClientManager()
    assert mgr.is_tool_available_for_persona("see", "air", None) is True


# ---- check_building_gate (execute-time gate) ----


def test_gate_returns_none_when_no_constraint():
    from tools.mcp_client import check_building_gate

    assert check_building_gate("any_tool", None) is None
    assert check_building_gate("any_tool", []) is None


def test_gate_blocks_outside_building(monkeypatch):
    from tools import mcp_client

    monkeypatch.setattr(mcp_client, "get_active_persona_id", lambda: "air")
    monkeypatch.setattr(
        mcp_client, "get_active_manager",
        lambda: _FakeManager("air", "b_lobby"),
    )

    error = mcp_client.check_building_gate("see", ["b_vessel"])
    assert error is not None
    assert "b_lobby" in error
    assert "b_vessel" in error


def test_gate_allows_inside_building(monkeypatch):
    from tools import mcp_client

    monkeypatch.setattr(mcp_client, "get_active_persona_id", lambda: "air")
    monkeypatch.setattr(
        mcp_client, "get_active_manager",
        lambda: _FakeManager("air", "b_vessel"),
    )

    assert mcp_client.check_building_gate("see", ["b_vessel"]) is None


def test_gate_blocks_when_no_active_persona(monkeypatch):
    from tools import mcp_client

    monkeypatch.setattr(mcp_client, "get_active_persona_id", lambda: None)
    monkeypatch.setattr(mcp_client, "get_active_manager", lambda: None)

    error = mcp_client.check_building_gate("see", ["b_vessel"])
    assert error is not None


# ---- register_external_tool gate wrap (native execute path) ----


def test_native_tool_registration_wraps_with_gate(monkeypatch):
    """When a tool is registered with building_ids in its schema, the
    callable in TOOL_REGISTRY must enforce the gate at execution time."""
    import tools as tools_module
    from tools import mcp_client

    calls = []

    def _impl(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "executed"

    schema = _make_schema("test_gated_tool", building_ids=["b_vessel"])

    saved_registry = tools_module.TOOL_REGISTRY.copy()
    saved_schemas = tools_module.SPELL_TOOL_SCHEMAS.copy()
    saved_names = tools_module.SPELL_TOOL_NAMES.copy()
    try:
        tools_module._add_registered_tool("test_gated_tool", schema, _impl)
        gated = tools_module.TOOL_REGISTRY["test_gated_tool"]

        # Outside vessel: gate blocks, impl is not called
        monkeypatch.setattr(mcp_client, "get_active_persona_id", lambda: "air")
        monkeypatch.setattr(
            mcp_client, "get_active_manager",
            lambda: _FakeManager("air", "b_lobby"),
        )
        result = gated()
        assert "実行できません" in result
        assert calls == []

        # Inside vessel: gate allows, impl runs
        monkeypatch.setattr(
            mcp_client, "get_active_manager",
            lambda: _FakeManager("air", "b_vessel"),
        )
        result = gated(arg="x")
        assert result == "executed"
        assert calls == [{"arg": "x"}]
    finally:
        tools_module.TOOL_REGISTRY.clear()
        tools_module.TOOL_REGISTRY.update(saved_registry)
        tools_module.SPELL_TOOL_SCHEMAS.clear()
        tools_module.SPELL_TOOL_SCHEMAS.update(saved_schemas)
        tools_module.SPELL_TOOL_NAMES.clear()
        tools_module.SPELL_TOOL_NAMES.update(saved_names)


def test_native_tool_without_building_ids_still_has_authorization_gate():
    """Unrestricted placement must not bypass the common authorization gate."""
    import tools as tools_module

    def _impl(**kwargs: Any) -> str:
        return "raw"

    schema = _make_schema("test_unrestricted_tool", building_ids=None)

    saved_registry = tools_module.TOOL_REGISTRY.copy()
    saved_schemas = tools_module.SPELL_TOOL_SCHEMAS.copy()
    saved_names = tools_module.SPELL_TOOL_NAMES.copy()
    try:
        tools_module._add_registered_tool("test_unrestricted_tool", schema, _impl)
        registered = tools_module.TOOL_REGISTRY["test_unrestricted_tool"]
        assert registered is not _impl
        assert registered.__wrapped__ is _impl
        assert registered._saiverse_authorization_gate is True
    finally:
        tools_module.TOOL_REGISTRY.clear()
        tools_module.TOOL_REGISTRY.update(saved_registry)
        tools_module.SPELL_TOOL_SCHEMAS.clear()
        tools_module.SPELL_TOOL_SCHEMAS.update(saved_schemas)
        tools_module.SPELL_TOOL_NAMES.clear()
        tools_module.SPELL_TOOL_NAMES.update(saved_names)


@pytest.mark.asyncio
async def test_native_async_tool_gate(monkeypatch):
    """async native tool also gets gate-wrapped and respects building_ids."""
    import tools as tools_module
    from tools import mcp_client

    async def _impl(**kwargs: Any) -> str:
        return "async-ok"

    schema = _make_schema("test_async_gated", building_ids=["b_vessel"])

    saved_registry = tools_module.TOOL_REGISTRY.copy()
    saved_schemas = tools_module.SPELL_TOOL_SCHEMAS.copy()
    saved_names = tools_module.SPELL_TOOL_NAMES.copy()
    try:
        tools_module._add_registered_tool("test_async_gated", schema, _impl)
        gated = tools_module.TOOL_REGISTRY["test_async_gated"]

        monkeypatch.setattr(mcp_client, "get_active_persona_id", lambda: "air")
        monkeypatch.setattr(
            mcp_client, "get_active_manager",
            lambda: _FakeManager("air", "b_lobby"),
        )
        result = await gated()
        assert "実行できません" in result

        monkeypatch.setattr(
            mcp_client, "get_active_manager",
            lambda: _FakeManager("air", "b_vessel"),
        )
        result = await gated()
        assert result == "async-ok"
    finally:
        tools_module.TOOL_REGISTRY.clear()
        tools_module.TOOL_REGISTRY.update(saved_registry)
        tools_module.SPELL_TOOL_SCHEMAS.clear()
        tools_module.SPELL_TOOL_SCHEMAS.update(saved_schemas)
        tools_module.SPELL_TOOL_NAMES.clear()
        tools_module.SPELL_TOOL_NAMES.update(saved_names)
