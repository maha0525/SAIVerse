"""Tests for the AddonConfig -> MCP subprocess env propagation path.

The MCP subprocess env is fixed at spawn time, so AddonConfig changes
(e.g. ``vision_host`` after a Wi-Fi switch, ``master_token`` after a
re-pairing) only take effect after kill + respawn. The addon config
update endpoints therefore must call
``tools.mcp_client.reconnect_addon_mcp_servers`` to push DB changes
into the live subprocess.

This regression test was added after a session where the PC moved to
a different Wi-Fi, the user updated ``vision_host`` from the UI, but
the stackchan ESP32 still received the stale IP in its avatar fetch
URL (= avatar invisible).
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ReconnectAddonMcpServersTests(unittest.TestCase):
    """``tools.mcp_client.reconnect_addon_mcp_servers`` behavior."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_filters_servers_by_addon_name(self):
        """Only servers whose meta['addon_name'] matches are reconnected."""
        from tools import mcp_client

        manager = MagicMock()
        manager._server_meta = {
            "addonA__alpha": {"addon_name": "addonA"},
            "addonA__beta": {"addon_name": "addonA"},
            "addonB__gamma": {"addon_name": "addonB"},
            "builtin__delta": {"addon_name": None},
        }
        manager.reconnect_server = AsyncMock(return_value=True)

        async def _passthrough(coro):
            return await coro

        with patch.object(mcp_client, "get_mcp_manager", return_value=manager), \
             patch.object(mcp_client, "run_on_mcp_loop", side_effect=_passthrough):
            results = self._run(mcp_client.reconnect_addon_mcp_servers("addonA"))

        self.assertEqual(set(results.keys()), {"addonA__alpha", "addonA__beta"})
        self.assertTrue(all(results.values()))
        self.assertEqual(manager.reconnect_server.call_count, 2)

    def test_returns_empty_when_no_matching_servers(self):
        from tools import mcp_client

        manager = MagicMock()
        manager._server_meta = {
            "addonB__gamma": {"addon_name": "addonB"},
        }
        manager.reconnect_server = AsyncMock(return_value=True)

        with patch.object(mcp_client, "get_mcp_manager", return_value=manager):
            results = self._run(mcp_client.reconnect_addon_mcp_servers("addonA"))

        self.assertEqual(results, {})
        manager.reconnect_server.assert_not_called()

    def test_returns_empty_when_manager_uninitialized(self):
        from tools import mcp_client

        with patch.object(mcp_client, "get_mcp_manager", return_value=None):
            results = self._run(mcp_client.reconnect_addon_mcp_servers("addonA"))

        self.assertEqual(results, {})

    def test_per_server_failure_recorded_as_false(self):
        """If reconnect_server raises for one server, others still run and
        the failing one is reported as ``False`` in the results map."""
        from tools import mcp_client

        manager = MagicMock()
        manager._server_meta = {
            "addonA__alpha": {"addon_name": "addonA"},
            "addonA__beta": {"addon_name": "addonA"},
        }

        async def _reconnect(name):
            if name == "addonA__alpha":
                raise RuntimeError("boom")
            return True

        manager.reconnect_server = AsyncMock(side_effect=_reconnect)

        async def _passthrough(coro):
            return await coro

        with patch.object(mcp_client, "get_mcp_manager", return_value=manager), \
             patch.object(mcp_client, "run_on_mcp_loop", side_effect=_passthrough):
            results = self._run(mcp_client.reconnect_addon_mcp_servers("addonA"))

        self.assertEqual(results["addonA__alpha"], False)
        self.assertEqual(results["addonA__beta"], True)


class AddonConfigEndpointsTriggerReconnectTests(unittest.TestCase):
    """The three addon config write endpoints must call
    ``_reconnect_addon_mcp`` after committing to the DB.

    The wrapper ``_reconnect_addon_mcp`` is a thin layer over
    ``reconnect_addon_mcp_servers`` whose job is to log warnings on
    partial failure without surfacing them as 5xx. Verifying that the
    endpoints call it is sufficient to guard against the regression
    where AddonConfig changes silently failed to reach the subprocess
    env.
    """

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setUp(self):
        # Bypass the real DB by patching _get_session with a MagicMock
        # session whose query chain returns a row with mutable attrs.
        from api.routes import addon as addon_module

        self.addon_module = addon_module

        self._db_row = MagicMock()
        self._db_row.params_json = None
        self._db_row.is_enabled = True

        self._db = MagicMock()
        self._db.query.return_value.filter.return_value.first.return_value = self._db_row

        self._session_patch = patch.object(
            addon_module, "_get_session", return_value=self._db,
        )
        self._session_patch.start()

        # Also patch _get_or_create_config to return our row directly
        self._goc_patch = patch.object(
            addon_module, "_get_or_create_config", return_value=self._db_row,
        )
        self._goc_patch.start()

        # The thing we actually want to assert on
        self._reconnect_patch = patch.object(
            addon_module, "_reconnect_addon_mcp", new_callable=AsyncMock,
        )
        self._reconnect_mock = self._reconnect_patch.start()

    def tearDown(self):
        self._session_patch.stop()
        self._goc_patch.stop()
        self._reconnect_patch.stop()

    def test_update_addon_config_triggers_reconnect(self):
        from api.routes.addon import UpdateParamsRequest, update_addon_config

        body = UpdateParamsRequest(params={"vision_host": "192.168.0.9"})
        self._run(update_addon_config("addonA", body, _manager=MagicMock()))

        self._reconnect_mock.assert_awaited_once_with("addonA")

    def test_update_addon_persona_config_triggers_reconnect(self):
        from api.routes.addon import (
            UpdateParamsRequest,
            update_addon_persona_config,
        )

        # update_addon_persona_config queries AddonPersonaConfig, not the
        # _get_or_create_config helper. Pre-stage the row.
        self._db_row.params_json = '{"existing": "value"}'

        body = UpdateParamsRequest(params={"token": "new"})
        self._run(
            update_addon_persona_config(
                "addonA", "persona1", body, _manager=MagicMock(),
            )
        )

        self._reconnect_mock.assert_awaited_once_with("addonA")

    def test_delete_addon_persona_config_triggers_reconnect(self):
        from api.routes.addon import delete_addon_persona_config

        # Return a row so that the delete actually fires (the endpoint
        # skips reconnect if there was nothing to delete).
        self._run(
            delete_addon_persona_config(
                "addonA", "persona1", _manager=MagicMock(),
            )
        )

        self._reconnect_mock.assert_awaited_once_with("addonA")

    def test_delete_addon_persona_config_skips_reconnect_when_noop(self):
        from api.routes.addon import delete_addon_persona_config

        # No row -> no DB change -> no reconnect needed
        self._db.query.return_value.filter.return_value.first.return_value = None

        self._run(
            delete_addon_persona_config(
                "addonA", "persona1", _manager=MagicMock(),
            )
        )

        self._reconnect_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
