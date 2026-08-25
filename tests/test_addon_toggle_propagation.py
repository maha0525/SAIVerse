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

import concurrent.futures
import logging
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


class ToggleOutcomeReachesTheCallerTests(unittest.TestCase):
    """待った結果が呼び出し元に伝わるか。失敗が沈黙しないか。

    ローカルレビュー (2026-08-26) の指摘。待ち切れなかった回も例外を握って
    黙って戻っていたため、**API は常に成功を返し、画面は「畳み終わった」前提で
    一覧を取り直していた**。teardown が待ち時間を超えると、直したはずの
    「切ったのに行が残る」がそのまま再発する。加えて、待ちのタイムアウトと
    反映そのものの失敗が同じ except に落ちて同じ文面でログされていたため、
    ログを読んだ人が失敗を「継続中」と読む。
    """

    def _notify(self, *, side_effect=None, **kwargs):
        from tools import mcp_client

        future = MagicMock()
        if side_effect is not None:
            future.result.side_effect = side_effect
        with patch.object(mcp_client, "get_mcp_manager", return_value=MagicMock()), \
             patch.object(mcp_client, "_loop", MagicMock()), \
             patch.object(
                 mcp_client.asyncio, "run_coroutine_threadsafe", return_value=future,
             ) as scheduled:
            outcome = mcp_client.notify_addon_toggled_sync("addonA", False, **kwargs)
        scheduled.call_args.args[0].close()
        return outcome, future

    def test_settled_reports_true(self):
        outcome, _ = self._notify(wait_timeout=5.0)

        self.assertIs(outcome, True)

    def test_timeout_reports_false(self):
        """待ち切れなかったのを「成功」として返さない。"""
        outcome, _ = self._notify(
            side_effect=concurrent.futures.TimeoutError("still applying"),
            wait_timeout=5.0,
        )

        self.assertIs(outcome, False)

    def test_failure_reports_false(self):
        outcome, _ = self._notify(side_effect=RuntimeError("boom"), wait_timeout=5.0)

        self.assertIs(outcome, False)

    def test_not_waiting_reports_unknown_not_success(self):
        """待っていない回は None (分からない)。True にすると嘘になる。"""
        outcome, _ = self._notify()

        self.assertIsNone(outcome)

    def test_timeout_leaves_a_watcher_so_a_later_failure_is_not_silent(self):
        """タイムアウト後に反映が失敗しても、誰も result() を呼ばない。

        concurrent.futures.Future は未回収の例外を自分では報告しないので、
        見届け役を付けないと痕跡なく消える。
        """
        _, future = self._notify(
            side_effect=concurrent.futures.TimeoutError("still applying"),
            wait_timeout=5.0,
        )

        future.add_done_callback.assert_called_once()

    def test_failure_is_not_logged_as_still_in_progress(self):
        """反映の失敗と、待ちのタイムアウトを同じ文面にしない。"""
        from tools import mcp_client

        with self.assertLogs(mcp_client.LOGGER.name, level=logging.WARNING) as captured:
            self._notify(side_effect=RuntimeError("boom"), wait_timeout=5.0)
        failure_msg = captured.records[0].getMessage()

        with self.assertLogs(mcp_client.LOGGER.name, level=logging.WARNING) as captured:
            self._notify(
                side_effect=concurrent.futures.TimeoutError("x"), wait_timeout=5.0,
            )
        timeout_msg = captured.records[0].getMessage()

        self.assertIn("failed", failure_msg)
        self.assertNotIn("still being applied", failure_msg)
        self.assertIn("still being applied", timeout_msg)


class SetEnabledResponseTests(SetEnabledWaitsOnlyWhenDisablingTests):
    """``PUT /{addon}/enabled`` のレスポンスが反映状況を運ぶか。

    画面はこれを見て「終わっていなければ少し置いて取り直す」を決める。
    """

    def _toggle_with_outcome(self, is_enabled: bool, outcome):
        from api.routes.addon import SetEnabledRequest, set_addon_enabled
        from tools import mcp_client

        notify = MagicMock(return_value=outcome)
        with patch.object(mcp_client, "notify_addon_toggled_sync", new=notify):
            return set_addon_enabled(
                "addonA", SetEnabledRequest(is_enabled=is_enabled), manager=MagicMock(),
            )

    def test_settled_is_reported(self):
        result = self._toggle_with_outcome(False, True)

        self.assertIs(result["mcp_settled"], True)

    def test_unsettled_is_reported(self):
        """待ち切れなかったことが画面まで届く (ここが塞がっていないと再発する)。"""
        result = self._toggle_with_outcome(False, False)

        self.assertIs(result["mcp_settled"], False)

    def test_not_waiting_reports_none(self):
        result = self._toggle_with_outcome(True, None)

        self.assertIsNone(result["mcp_settled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
