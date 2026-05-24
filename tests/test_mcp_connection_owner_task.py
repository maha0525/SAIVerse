"""Regression test for the anyio cross-task cancel-scope spin.

See docs/issues/mcp_cancel_scope_spin_gil_starvation.md.

The bug: ``MCPServerConnection`` entered the transport's async context
(``stdio_client`` / ``ClientSession``, which use anyio cancel scopes
internally) during ``connect()`` in one task, but exited it during
``disconnect()`` in a *different* task. anyio binds a cancel scope to the
task it was entered in; exiting it from another task raises
``Attempted to exit cancel scope in a different task than it was entered in``.
Under the right timing this escaped as an unhandled async-generator cleanup
and left ``_deliver_cancellation`` busy-spinning, starving the whole process
of the GIL.

The fix routes the transport's ``async with`` through a single long-lived
"owner task" (``_run_connection``) that both opens and closes it. This test
asserts that invariant directly: even when ``connect()`` and ``disconnect()``
are dispatched from different tasks, the transport context is entered and
exited in the same task.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anyio

from tools.mcp_client import MCPServerConnection


class _ScopedTransport:
    """Fake MCP transport guarding its lifetime with an anyio CancelScope.

    Mimics the real ``stdio_client`` transport, which holds anyio cancel
    scopes / task groups open for the connection's duration. Records the task
    that entered and the task that exited the scope so the test can assert
    they are identical; ``CancelScope.__exit__`` itself also raises if they
    differ.
    """

    def __init__(self) -> None:
        self.enter_task = None
        self.exit_task = None
        self._scope = None

    async def __aenter__(self):
        self._scope = anyio.CancelScope()
        self._scope.__enter__()
        self.enter_task = asyncio.current_task()
        return (object(), object())  # (read_stream, write_stream)

    async def __aexit__(self, *exc_info):
        self.exit_task = asyncio.current_task()
        # Raises "Attempted to exit cancel scope in a different task than it
        # was entered in" if exit happens in a task other than enter.
        self._scope.__exit__(*exc_info)
        return False


class _FakeSession:
    """Minimal stand-in for ``mcp.ClientSession``."""

    def __init__(self, read_stream, write_stream) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        return SimpleNamespace(
            protocolVersion="test",
            serverInfo=SimpleNamespace(name="fake"),
        )

    async def list_tools(self):
        return SimpleNamespace(tools=[])


class MCPServerConnectionOwnerTaskTests(unittest.TestCase):
    @staticmethod
    def _run(coro):
        """Run ``coro`` on a dedicated loop without polluting global state.

        ``asyncio.run`` leaves the thread's current event loop set to ``None``
        on exit, which breaks sibling tests that rely on
        ``asyncio.get_event_loop()``. Restore a fresh, usable loop afterwards.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
            asyncio.set_event_loop(asyncio.new_event_loop())

    def test_connect_disconnect_from_different_tasks_keep_scope_in_one_task(self):
        transport = _ScopedTransport()

        def fake_stdio_client(server_params, errlog=None):
            return transport

        async def scenario():
            conn = MCPServerConnection("fake-stdio", {"command": "noop"})
            # connect() and disconnect() run in *separate* tasks on purpose.
            # The buggy implementation would enter the scope in connect()'s
            # task and exit it in disconnect()'s task -> anyio error.
            await asyncio.create_task(conn.connect())
            self.assertTrue(conn.connected)
            await asyncio.create_task(conn.disconnect())
            self.assertFalse(conn.connected)

        with patch("mcp.client.stdio.stdio_client", fake_stdio_client), patch(
            "mcp.ClientSession", _FakeSession
        ):
            self._run(scenario())

        self.assertIsNotNone(transport.enter_task, "transport was never entered")
        self.assertIsNotNone(transport.exit_task, "transport was never exited")
        self.assertIs(
            transport.enter_task,
            transport.exit_task,
            "transport cancel scope was exited in a different task than entered",
        )

    def test_reconnect_cycle_stays_in_one_task_each_time(self):
        async def scenario():
            conn = MCPServerConnection("fake-stdio", {"command": "noop"})
            for _ in range(3):
                transport = _ScopedTransport()

                def fake_stdio_client(server_params, errlog=None, _t=transport):
                    return _t

                with patch(
                    "mcp.client.stdio.stdio_client", fake_stdio_client
                ), patch("mcp.ClientSession", _FakeSession):
                    await asyncio.create_task(conn.connect())
                    self.assertTrue(conn.connected)
                    await asyncio.create_task(conn.disconnect())
                    self.assertFalse(conn.connected)

                self.assertIs(transport.enter_task, transport.exit_task)

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
