"""Tests for how a reconnect's outcome reaches the user.

再接続には 3 つの結果がある — 繋ぎ直した / 繋ごうとして失敗した / 繋ぎ直す
相手がいなかった。 最後のものは **失敗ではない**: per_persona のサーバーは
ペルソナが動き出すまで接続を持たないので、接続が無いのが常態である。

2026-08-25、この 3 つが bool ひとつに潰れていたせいで、再起動直後に再接続
ボタンを押しても画面が無反応になった。 潰れた False を API が HTTP 200 に
包み、画面が HTTP ステータスしか見ていなかったため、失敗も通知も表に出な
かった (三段重ね)。 ここで見張るのは:

- 「相手がいない」が ``message`` (通知) として返り、``error`` にはならない
- per_persona では「次にそのペルソナが動いたとき繋がる」まで伝える
- 本当の失敗は ``error`` として返る
- 設定保存時の自動再接続が、「相手がいない」を警告に混ぜない
"""
from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.mcp_client import (  # noqa: E402
    RECONNECT_FAILED,
    RECONNECT_NO_INSTANCES,
    RECONNECT_RECONNECTED,
)


def _run(coro):
    return asyncio.run(coro)


class ReconnectApiOutcomeTests(unittest.TestCase):
    """``POST /api/mcp/servers/{name}/reconnect`` の返し方。"""

    def _call(self, outcome: str, *, scope: str = "per_persona") -> dict:
        from api.routes.mcp import reconnect_server
        from tools import mcp_client

        manager = MagicMock()
        manager._server_meta = {"addonA__srv": {"scope": scope}}

        with patch.object(mcp_client, "get_mcp_manager", return_value=manager), \
             patch.object(
                 mcp_client, "reconnect_mcp_server", new=AsyncMock(return_value=outcome)
             ):
            return _run(reconnect_server("addonA__srv"))

    def test_reconnected_reports_success(self):
        result = self._call(RECONNECT_RECONNECTED)

        self.assertTrue(result["success"])
        self.assertNotIn("error", result)

    def test_no_instances_is_a_notice_not_an_error(self):
        """繋ぐ相手がいないのは故障ではない。error に載せない。"""
        result = self._call(RECONNECT_NO_INSTANCES)

        self.assertFalse(result["success"])
        self.assertNotIn("error", result)
        self.assertIn("message", result)

    def test_no_instances_on_per_persona_explains_when_it_will_connect(self):
        """「じゃあどうすれば繋がるのか」まで書いていないと、押した人は詰む。"""
        result = self._call(RECONNECT_NO_INSTANCES, scope="per_persona")

        self.assertIn("ペルソナ", result["message"])
        self.assertIn("Pulse", result["message"])

    def test_no_instances_on_global_omits_the_per_persona_explanation(self):
        """global サーバーに per_persona の説明を付けると的外れになる。"""
        result = self._call(RECONNECT_NO_INSTANCES, scope="global")

        self.assertIn("繋ぎ直す接続がありません", result["message"])
        self.assertNotIn("Pulse", result["message"])

    def test_failure_reports_an_error(self):
        result = self._call(RECONNECT_FAILED)

        self.assertFalse(result["success"])
        self.assertIn("error", result)
        self.assertNotIn("message", result)


class AddonAutoReconnectLoggingTests(unittest.TestCase):
    """設定保存時の自動再接続 (``_reconnect_addon_mcp``) のログ。"""

    def _reconnect_with(self, results: dict):
        from api.routes import addon as addon_module

        with patch.object(
            addon_module, "_get_session", return_value=MagicMock(),
        ), patch(
            "tools.mcp_client.reconnect_addon_mcp_servers",
            new=AsyncMock(return_value=results),
        ):
            with self.assertLogs("api.routes.addon", level=logging.INFO) as captured:
                _run(addon_module._reconnect_addon_mcp("addonA"))
        return captured

    def test_no_instances_does_not_warn(self):
        """per_persona の常態を警告に混ぜない。

        混ぜると、後からログを読んだ人が正常な状態を失敗として数える
        (実際 2026-08-25 のログには鍵を保存するたび partial failure が出ていた)。
        """
        captured = self._reconnect_with({"addonA__srv": RECONNECT_NO_INSTANCES})

        self.assertFalse([r for r in captured.records if r.levelno >= logging.WARNING])

    def test_real_failure_still_warns(self):
        captured = self._reconnect_with({"addonA__srv": RECONNECT_FAILED})

        warnings = [r for r in captured.records if r.levelno >= logging.WARNING]
        self.assertTrue(warnings)
        self.assertIn("addonA__srv", warnings[0].getMessage())

    def test_mixed_outcomes_warn_only_about_the_real_failure(self):
        captured = self._reconnect_with({
            "addonA__ok": RECONNECT_RECONNECTED,
            "addonA__idle": RECONNECT_NO_INSTANCES,
            "addonA__bad": RECONNECT_FAILED,
        })

        warnings = [r for r in captured.records if r.levelno >= logging.WARNING]
        self.assertEqual(len(warnings), 1)
        message = warnings[0].getMessage()
        self.assertIn("addonA__bad", message)
        self.assertNotIn("addonA__idle", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
