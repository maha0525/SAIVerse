"""manager.stop_autonomy (自律行動の実効停止) のテスト。

旧: メタ判断連続失敗リカバリ (案C) と停止ボタンの共用経路のテスト
(test_meta_judgment_recovery.py) だったが、v1 メタ判断の退役
(track_retirement.md §7.4) でリカバリ側が消えたため、停止ボタン経路の
検証だけをここへ移した。
"""
import unittest
from unittest.mock import MagicMock


class TestStopAutonomy(unittest.TestCase):
    """manager.stop_autonomy: 実効停止の 3 ステップ (停止ボタンの経路)。

    旧ステップ 4「対ユーザー Track をサイレント activate」は 2026-08-21 の
    会話経路の Track なし化で対象消滅した (プロンプト待ち = 会話の出来事が
    開いていない状態そのものになったため、戻すべき帳簿が無い)。
    """

    def test_stop_autonomy_runs_all_three_steps(self):
        from saiverse.saiverse_manager import SAIVerseManager

        mgr = MagicMock()
        persona = MagicMock()
        mgr.personas = {"p1": persona}
        am = MagicMock()
        am.is_running = False
        mgr._autonomy_managers = {"p1": am}

        tm = mgr.track_manager
        auto_track = MagicMock()
        auto_track.track_type = "autonomous"
        auto_track.track_id = "t:1"
        auto_track.status = "running"

        tm.list_for_persona.return_value = [auto_track]

        result = SAIVerseManager.stop_autonomy(mgr, "p1")

        # 1. AM 停止 / 2. autonomous Track pause / 3. AUTONOMY_ENABLED=False
        am.stop.assert_called_once()
        tm.pause.assert_called_once_with("t:1")
        self.assertEqual(persona.autonomy_enabled, False)
        tm.activate.assert_not_called()
        self.assertEqual(result["paused_tracks"], ["t:1"])
        self.assertNotIn("user_track_activated", result)


if __name__ == "__main__":
    unittest.main()
