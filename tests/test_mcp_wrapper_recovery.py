"""per_persona wrapper の接続回復。実 MCP・鍵・persona は使わない。

接続の生成だけを fake に置き、wrapper → _start_instance の既存の
backoff / 遷移ガード / 参照の契約を通す。送信後の失敗は再送しない。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tools import mcp_client

SERVER = "synthetic__service"
PERSONA = "synthetic"
KEY = f"{SERVER}:persona:{PERSONA}"


def _connection(*, connected=True):
    return SimpleNamespace(
        connected=connected,
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        call_tool=AsyncMock(
            return_value="result",
            side_effect=None if connected else ConnectionError("synthetic disconnected"),
        ),
    )


@pytest.fixture
def harness(monkeypatch):
    manager = mcp_client.MCPClientManager()
    manager._server_meta[SERVER] = {
        "scope": "per_persona", "raw_config": {}, "addon_name": None,
    }
    fresh = _connection()
    factory = Mock(return_value=fresh)
    resolve = Mock(return_value={})
    register = Mock()
    loop_calls = []

    async def local_loop(coro):
        loop_calls.append(coro)
        return await coro

    monkeypatch.setattr(mcp_client, "MCPServerConnection", factory)
    monkeypatch.setattr(mcp_client, "get_active_persona_id", lambda: PERSONA)
    monkeypatch.setattr(mcp_client, "run_on_mcp_loop", local_loop)
    monkeypatch.setattr("tools.mcp_config.resolve_config_placeholders", resolve)
    monkeypatch.setattr(manager, "_register_tools", register)
    wrapper = mcp_client._make_mcp_tool_wrapper(manager, SERVER, "read", "per_persona")
    return SimpleNamespace(
        manager=manager, fresh=fresh, factory=factory, resolve=resolve,
        register=register, wrapper=wrapper, loop_calls=loop_calls,
    )


@pytest.mark.parametrize("present", [False, True], ids=["missing", "disconnected"])
def test_unavailable_connection_uses_the_existing_start_path(harness, present):
    h = harness
    dead = _connection(connected=False)
    if present:
        h.manager._connections[KEY] = dead
    h.manager._refs[KEY] = {"addon:synthetic", f"persona:{PERSONA}"}
    h.manager._failed_instances[KEY] = {"next_retry_at": 0}
    other_key = f"{SERVER}:persona:other"
    other = _connection()
    h.manager._connections[other_key] = other

    assert asyncio.run(h.wrapper(value=3)) == "result"

    h.factory.assert_called_once_with(SERVER, {}, instance_key=KEY)
    h.resolve.assert_called_once_with({}, persona_id=PERSONA, instance_context=None)
    h.fresh.connect.assert_awaited_once_with()
    h.register.assert_called_once_with(h.fresh, SERVER, KEY, PERSONA)
    h.fresh.call_tool.assert_awaited_once_with("read", {"value": 3})
    dead.call_tool.assert_not_awaited()
    assert h.manager._connections[KEY] is h.fresh
    assert h.manager._connections[other_key] is other
    other.call_tool.assert_not_awaited()
    assert h.manager._refs[KEY] == {"addon:synthetic", f"persona:{PERSONA}"}
    assert KEY not in h.manager._failed_instances
    assert len(h.loop_calls) == 2  # start と call をどちらも MCP loop へ渡す


def test_live_connection_is_reused_without_restart(harness):
    h = harness
    live = _connection()
    h.manager._connections[KEY] = live

    assert asyncio.run(h.wrapper()) == "result"

    h.factory.assert_not_called()
    live.call_tool.assert_awaited_once_with("read", {})
    assert h.manager._refs == {}


@pytest.mark.parametrize("present", [False, True], ids=["missing", "disconnected"])
def test_backoff_prevents_restart_and_tool_dispatch(harness, present):
    h = harness
    dead = _connection(connected=False)
    if present:
        h.manager._connections[KEY] = dead
    h.manager._record_failure(KEY, mcp_client.ERROR_CATEGORY_NETWORK, "synthetic offline")

    result = asyncio.run(h.wrapper())

    assert "synthetic offline" in result
    h.factory.assert_not_called()
    dead.call_tool.assert_not_awaited()
    assert h.manager._refs == {}
    assert not h.loop_calls


@pytest.mark.parametrize("transition", ["_starting", "_stopping"])
def test_busy_transition_does_not_start_dispatch_or_add_reference(harness, transition):
    h = harness
    dead = _connection(connected=False)
    h.manager._connections[KEY] = dead
    getattr(h.manager, transition).add(KEY)

    result = asyncio.run(h.wrapper())

    assert "処理が進行中です" in result
    h.factory.assert_not_called()
    dead.call_tool.assert_not_awaited()
    assert h.manager._refs == {}
    assert h.manager._failed_instances == {}  # busy は故障として backoff しない


def test_start_failure_records_backoff_without_dispatch_or_new_reference(harness):
    h = harness
    dead = _connection(connected=False)
    h.manager._connections[KEY] = dead
    h.manager._refs[KEY] = {"addon:synthetic"}
    h.fresh.connect.side_effect = ConnectionError("synthetic offline")

    result = asyncio.run(h.wrapper())

    assert "synthetic offline" in result
    h.fresh.connect.assert_awaited_once_with()
    h.fresh.call_tool.assert_not_awaited()
    dead.call_tool.assert_not_awaited()
    assert h.manager._refs[KEY] == {"addon:synthetic"}
    assert h.manager._is_in_backoff(KEY)
    assert KEY not in h.manager._starting


def test_stop_during_start_discards_connection_without_dispatch_or_reference(harness):
    h = harness
    dead = _connection(connected=False)
    h.manager._connections[KEY] = dead

    async def stop_during_connect():
        h.manager._stop_requested.add(KEY)

    h.fresh.connect.side_effect = stop_during_connect
    result = asyncio.run(h.wrapper())

    assert "停止が要求されました" in result
    h.fresh.disconnect.assert_awaited_once_with()
    h.fresh.call_tool.assert_not_awaited()
    dead.call_tool.assert_not_awaited()
    assert h.manager._connections[KEY] is dead
    assert h.manager._refs == {}
    assert KEY not in h.manager._starting
    assert KEY not in h.manager._stop_requested
    assert h.manager._failed_instances == {}


def test_real_shutdown_while_replacing_dead_connection_cleans_up_both(harness):
    h = harness
    dead = _connection(connected=False)
    h.manager._connections[KEY] = dead
    h.manager._refs[KEY] = {f"persona:{PERSONA}"}
    membership_key = (SERVER, PERSONA)
    h.manager._persona_tool_names[membership_key] = frozenset({"read"})

    async def stop_during_connect():
        await h.manager._shutdown_instance(KEY)

    h.fresh.connect.side_effect = stop_during_connect
    result = asyncio.run(h.wrapper())

    assert "停止が要求されました" in result
    dead.disconnect.assert_awaited_once_with()
    h.fresh.disconnect.assert_awaited_once_with()
    dead.call_tool.assert_not_awaited()
    h.fresh.call_tool.assert_not_awaited()
    h.register.assert_not_called()
    assert dead._closed is True
    assert KEY not in h.manager._connections
    assert KEY not in h.manager._refs
    assert h.manager._persona_tool_names[membership_key] == frozenset()
    assert h.manager._persona_membership_version[membership_key] == 1
    assert not h.manager._starting
    assert not h.manager._stopping
    assert not h.manager._stop_requested
    assert not h.manager._failed_instances


@pytest.mark.parametrize("recover_first", [False, True], ids=["live", "recovered"])
def test_failure_after_dispatch_is_never_replayed(harness, recover_first):
    h = harness
    h.manager._connections[KEY] = (
        _connection(connected=False) if recover_first else h.fresh
    )
    h.fresh.call_tool.side_effect = ConnectionError("result uncertain")

    with pytest.raises(ConnectionError, match="result uncertain"):
        asyncio.run(h.wrapper())

    h.fresh.call_tool.assert_awaited_once_with("read", {})
    assert h.factory.call_count == int(recover_first)
    assert len(h.loop_calls) == 1 + int(recover_first)


def test_global_scope_does_not_gain_automatic_start(harness):
    h = harness
    dead = _connection(connected=False)
    dead.call_tool.side_effect = ConnectionError("synthetic disconnected")
    h.manager._connections[f"{SERVER}:global"] = dead
    wrapper = mcp_client._make_mcp_tool_wrapper(h.manager, SERVER, "read", "global")

    with pytest.raises(ConnectionError, match="synthetic disconnected"):
        asyncio.run(wrapper())

    h.factory.assert_not_called()
    assert h.manager._refs == {}
