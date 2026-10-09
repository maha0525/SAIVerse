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
    if present:
        dead.disconnect.assert_awaited_once_with()
        assert dead._closed is True
    else:
        dead.disconnect.assert_not_awaited()
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
    dead.disconnect.assert_awaited_once_with()
    assert KEY not in h.manager._connections
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
    dead.disconnect.assert_awaited_once_with()
    assert KEY not in h.manager._connections
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
    assert h.manager._persona_membership_version[membership_key] == 2
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


@pytest.mark.parametrize("shared", [False, True], ids=["last-instance", "other-persona"])
def test_replacement_retires_old_connection_and_tools_before_registering(
    harness, monkeypatch, shared,
):
    """登録・解除も実経路を通し、差分のある次世代から旧ツールが消える。"""
    import tools

    h = harness
    for name, empty in (
        ("TOOL_REGISTRY", {}), ("TOOL_SCHEMAS", []),
        ("OPENAI_TOOLS_SPEC", []), ("GEMINI_TOOLS_SPEC", []),
        ("SPELL_TOOL_NAMES", set()), ("SPELL_TOOL_SCHEMAS", {}),
    ):
        monkeypatch.setattr(tools, name, empty)
    register = mcp_client.MCPClientManager._register_tools.__get__(h.manager)
    monkeypatch.setattr(h.manager, "_register_tools", register)
    dead = _connection(connected=False)
    dead.config = {"spell_tools_default": {"spell": True, "visible": True}}
    dead.tools = [SimpleNamespace(name="read"), SimpleNamespace(name="obsolete")]
    h.fresh.config = dead.config
    h.fresh.tools = [SimpleNamespace(name="read")]
    h.manager._connections[KEY] = dead
    h.manager._refs[KEY] = {f"persona:{PERSONA}", "addon:synthetic"}
    h.manager._persona_tool_names[(SERVER, PERSONA)] = frozenset({"read", "obsolete"})
    other_key = f"{SERVER}:persona:other"
    other = _connection()
    if shared:
        h.manager._connections[other_key] = other
        h.manager._refs[other_key] = {"persona:other"}
        h.manager._persona_tool_names[(SERVER, "other")] = frozenset({"read", "obsolete"})
    register(dead, SERVER, KEY, PERSONA)
    old_names = {f"{SERVER}__read", f"{SERVER}__obsolete"}
    assert set(tools.TOOL_REGISTRY) == old_names
    events = []
    unregister = tools.unregister_external_tool

    def trace_unregister(name):
        events.append(("unregister", name))
        unregister(name)

    async def trace_disconnect():
        events.append(("disconnect", KEY))

    async def trace_connect():
        events.append(("connect", KEY))
        assert set(tools.TOOL_REGISTRY) == (old_names if shared else set())
        assert set(h.manager._registered_tools) == (old_names if shared else set())

    def trace_register(*args):
        events.append(("register", KEY))
        register(*args)

    monkeypatch.setattr(tools, "unregister_external_tool", trace_unregister)
    monkeypatch.setattr(h.manager, "_register_tools", trace_register)
    dead.disconnect.side_effect = trace_disconnect
    h.fresh.connect.side_effect = trace_connect

    assert asyncio.run(h.wrapper()) == "result"

    dead.disconnect.assert_awaited_once_with()
    assert dead._closed is True
    unregister_events = [] if shared else [
        ("unregister", f"{SERVER}__read"), ("unregister", f"{SERVER}__obsolete"),
    ]
    assert events == unregister_events + [
        ("disconnect", KEY), ("connect", KEY), ("register", KEY),
    ]
    expected_names = old_names if shared else {f"{SERVER}__read"}
    assert set(h.manager._registered_tools) == expected_names
    assert set(tools.TOOL_REGISTRY) == expected_names
    assert tools.SPELL_TOOL_NAMES == expected_names
    assert set(tools.SPELL_TOOL_SCHEMAS) == expected_names
    assert {schema.name for schema in tools.TOOL_SCHEMAS} == expected_names
    if shared:
        assert h.manager._connections[other_key] is other
        assert h.manager._refs[other_key] == {"persona:other"}
        assert h.manager.is_tool_available_for_persona(f"{SERVER}__obsolete", "other")
        assert not h.manager.is_tool_available_for_persona(f"{SERVER}__obsolete", PERSONA)
        other.disconnect.assert_not_awaited()
    assert h.manager._refs[KEY] == {f"persona:{PERSONA}", "addon:synthetic"}
    assert h.manager._persona_tool_names[(SERVER, PERSONA)] == frozenset()
    assert h.manager._persona_membership_version[(SERVER, PERSONA)] == 1


@pytest.mark.parametrize("phase", ["unregister", "disconnect"])
@pytest.mark.parametrize("stop", [False, True], ids=["competing-start", "stop"])
def test_replacement_guards_entire_retirement(harness, monkeypatch, phase, stop):
    h = harness
    dead = _connection(connected=False)
    h.manager._connections[KEY] = dead
    h.manager._refs[KEY] = {f"persona:{PERSONA}"}

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pause_retirement(*args):
            entered.set()
            await release.wait()

        if phase == "disconnect":
            dead.disconnect.side_effect = pause_retirement
        else:
            original_unregister = h.manager._unregister_instance_tools
            first = True

            async def unregister(key):
                nonlocal first
                if first:
                    first = False
                    await pause_retirement()
                await original_unregister(key)

            monkeypatch.setattr(h.manager, "_unregister_instance_tools", unregister)
        task = asyncio.create_task(h.wrapper())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert KEY in h.manager._starting
            assert KEY in h.manager._stopping
            h.factory.assert_not_called()
            with pytest.raises(mcp_client.MCPInstanceBusyError):
                await h.manager._start_instance(KEY, SERVER, persona_id=PERSONA)
            if stop:
                await h.manager._shutdown_instance(KEY)
                assert KEY in h.manager._stop_requested
                # A nested stop may release _stopping; _starting still excludes replacement.
                with pytest.raises(mcp_client.MCPInstanceBusyError):
                    await h.manager._start_instance(KEY, SERVER, persona_id=PERSONA)
        finally:
            release.set()
            result = await task
        if stop:
            assert "停止が要求されました" in result
            h.factory.assert_not_called()
            h.register.assert_not_called()
            assert KEY not in h.manager._connections
            assert KEY not in h.manager._refs
        else:
            assert result == "result"
            h.fresh.connect.assert_awaited_once_with()
            assert h.manager._refs[KEY] == {f"persona:{PERSONA}"}
        assert not h.manager._starting
        assert not h.manager._stopping
        assert not h.manager._stop_requested
        assert not h.manager._failed_instances
        dead.disconnect.assert_awaited_once_with()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["disconnect", "connect"])
@pytest.mark.parametrize("invalidate", [False, True], ids=["replacement", "external-invalidation"])
def test_pulse_refresh_distinguishes_own_retirement_from_external_invalidation(
    harness, invalidate, phase,
):
    h = harness
    dead = _connection(connected=False)
    h.manager._connections[KEY] = dead
    membership_key = (SERVER, PERSONA)
    h.manager._persona_tool_names[membership_key] = frozenset({"obsolete"})
    h.fresh.tools = [SimpleNamespace(name="read")]

    async def connect():
        if invalidate:
            h.manager._mark_persona_tools_unavailable(membership_key)

    if phase == "disconnect":
        dead.disconnect.side_effect = connect
    else:
        h.fresh.connect.side_effect = connect
    changed = asyncio.run(h.manager.refresh_persona_tools(PERSONA))

    assert changed is (not invalidate)
    assert h.manager._persona_tool_names[membership_key] == (
        frozenset() if invalidate else frozenset({"read"})
    )
    assert h.manager._persona_membership_version[membership_key] == 1 + int(invalidate)
    dead.disconnect.assert_awaited_once_with()
    h.fresh.call_tool.assert_not_awaited()
    assert h.manager._refs[KEY] == {f"persona:{PERSONA}"}



def test_stop_guard_remains_until_just_started_connection_is_disconnected(harness):
    h = harness
    h.manager._connections[KEY] = _connection(connected=False)

    async def scenario():
        disconnecting = asyncio.Event()
        release = asyncio.Event()

        async def stop_during_connect():
            await h.manager._shutdown_instance(KEY)

        async def pause_disconnect():
            disconnecting.set()
            await release.wait()

        h.fresh.connect.side_effect = stop_during_connect
        h.fresh.disconnect.side_effect = pause_disconnect
        task = asyncio.create_task(h.wrapper())
        try:
            await asyncio.wait_for(disconnecting.wait(), timeout=1)
            with pytest.raises(mcp_client.MCPInstanceBusyError):
                await h.manager._start_instance(KEY, SERVER, persona_id=PERSONA)
        finally:
            release.set()
            result = await task
        assert "停止が要求されました" in result
        h.factory.assert_called_once()
        h.fresh.disconnect.assert_awaited_once_with()
        h.register.assert_not_called()
        assert not h.manager._connections
        assert not h.manager._refs
        assert not h.manager._starting
        assert not h.manager._stop_requested

    asyncio.run(scenario())


def test_cancellation_during_retirement_releases_transition_guards(harness):
    h = harness
    dead = _connection(connected=False)
    h.manager._connections[KEY] = dead
    h.manager._refs[KEY] = {f"persona:{PERSONA}"}

    async def scenario():
        disconnecting = asyncio.Event()

        async def pause_disconnect():
            disconnecting.set()
            await asyncio.Future()

        dead.disconnect.side_effect = pause_disconnect
        task = asyncio.create_task(h.wrapper())
        try:
            await asyncio.wait_for(disconnecting.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        h.factory.assert_not_called()
        assert not h.manager._starting
        assert not h.manager._stopping
        assert not h.manager._stop_requested
        assert not h.manager._failed_instances
        assert h.manager._refs[KEY] == {f"persona:{PERSONA}"}
        assert await h.wrapper() == "result"

    asyncio.run(scenario())
