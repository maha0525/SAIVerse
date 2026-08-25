"""Tests for how an addon enable/disable reaches the MCP layer and the UI.

アドオンを無効にすると、そのアドオンの MCP サーバーは畳まれる。 ところが
反映は MCP 専用ループへ**予約するだけ**だったので、API が戻った時点ではまだ
畳み終わっていない。 画面はその直後に一覧を取り直すため、切ったはずのサーバー
の行が残って見えた (2026-08-25 実機)。

そこで無効化のときだけ完了を待つ。 有効化を待たないのは、global サーバーの
subprocess 起動 (spawn + 初期化) を含んで数十秒かかるからで、そちらは起動中の
行が一覧に出るので待つ必要がない。 **この非対称は意図であり、対称に揃えると
どちらかが壊れる**ので、ここで固定する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SetEnabledWaitsOnlyWhenDisablingTests(unittest.TestCase):
    """``PUT /{addon}/enabled`` が MCP 層へ渡す待ち時間。"""

    def setUp(self):
        from api.routes import addon as addon_module

        self.addon_module = addon_module

        addon_dir = MagicMock()
        addon_dir.exists.return_value = True
        self._dir_patch = patch.object(
            addon_module, "_get_addon_dir", return_value=addon_dir,
        )
        self._dir_patch.start()

        self._db = MagicMock()
        self._session_patch = patch.object(
            addon_module, "_get_session", return_value=self._db,
        )
        self._session_patch.start()

        self._goc_patch = patch.object(
            addon_module, "_get_or_create_config", return_value=MagicMock(),
        )
        self._goc_patch.start()

    def tearDown(self):
        self._dir_patch.stop()
        self._session_patch.stop()
        self._goc_patch.stop()

    def _toggle(self, is_enabled: bool):
        from api.routes.addon import SetEnabledRequest, set_addon_enabled
        from tools import mcp_client

        notify = MagicMock()
        with patch.object(mcp_client, "notify_addon_toggled_sync", new=notify):
            set_addon_enabled(
                "addonA", SetEnabledRequest(is_enabled=is_enabled), manager=MagicMock(),
            )
        return notify

    def test_disabling_waits_for_the_teardown(self):
        """切った直後に一覧を取り直しても行が残らないよう、畳み終わりを待つ。"""
        notify = self._toggle(False)

        notify.assert_called_once()
        self.assertIsNotNone(notify.call_args.kwargs.get("wait_timeout"))

    def test_enabling_does_not_wait(self):
        """有効化は subprocess の起動を含むので待たない (待つと画面が固まる)。"""
        notify = self._toggle(True)

        notify.assert_called_once()
        self.assertIsNone(notify.call_args.kwargs.get("wait_timeout"))


class NotifyAddonToggledWaitTests(unittest.TestCase):
    """``notify_addon_toggled_sync`` の待ち方。"""

    def _notify(self, **kwargs):
        from tools import mcp_client

        future = MagicMock()
        with patch.object(mcp_client, "get_mcp_manager", return_value=MagicMock()), \
             patch.object(mcp_client, "_loop", MagicMock()), \
             patch.object(
                 mcp_client.asyncio, "run_coroutine_threadsafe", return_value=future,
             ) as scheduled:
            mcp_client.notify_addon_toggled_sync("addonA", False, **kwargs)
        # スケジュールした coroutine を閉じ、未await の警告を出さない
        scheduled.call_args.args[0].close()
        return future

    def test_waits_when_a_timeout_is_given(self):
        future = self._notify(wait_timeout=5.0)

        future.result.assert_called_once()
        self.assertEqual(future.result.call_args.kwargs.get("timeout"), 5.0)

    def test_does_not_wait_by_default(self):
        future = self._notify()

        future.result.assert_not_called()

    def test_a_timeout_is_not_an_error(self):
        """待ち切れなくても反映は続いている。呼び出し元を落とさない。"""
        from tools import mcp_client

        future = MagicMock()
        future.result.side_effect = TimeoutError("still applying")
        with patch.object(mcp_client, "get_mcp_manager", return_value=MagicMock()), \
             patch.object(mcp_client, "_loop", MagicMock()), \
             patch.object(
                 mcp_client.asyncio, "run_coroutine_threadsafe", return_value=future,
             ) as scheduled:
            mcp_client.notify_addon_toggled_sync("addonA", False, wait_timeout=0.01)
        scheduled.call_args.args[0].close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
