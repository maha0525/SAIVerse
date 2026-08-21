"""manager.stop_autonomy (自律行動の実効停止) のテスト。

旧: メタ判断連続失敗リカバリ (案C) と停止ボタンの共用経路のテスト
(test_meta_judgment_recovery.py) だったが、v1 メタ判断の退役
(track_retirement.md §7.4) でリカバリ側が消えたため、停止ボタン経路の
検証だけをここへ移した。
"""
import unittest
from unittest.mock import MagicMock


class TestStopAutonomy(unittest.TestCase):
    """manager.stop_autonomy: 実効停止の 2 ステップ (停止ボタンの経路)。

    撤去された旧ステップ:

    - 旧ステップ 4「対ユーザー Track をサイレント activate」は 2026-08-21 の
      会話経路の Track なし化で対象消滅した (プロンプト待ち = 会話の出来事が
      開いていない状態そのものになったため、戻すべき帳簿が無い)。
    - 旧ステップ 2「running な autonomous Track を全 pause」は 2026-08-22
      (束 6c) の Track ランタイム退役で対象消滅した。running Track を作る
      書き手が消え、揃えるべき帳簿そのものが無くなったため、戻り値からも
      ``paused_tracks`` が落ちた (いまは ``{"autonomy_running": bool}`` だけ)。
    """

    def test_stop_autonomy_runs_both_steps(self):
        from saiverse.saiverse_manager import SAIVerseManager

        mgr = MagicMock()
        persona = MagicMock()
        mgr.personas = {"p1": persona}
        am = MagicMock()
        am.is_running = False
        mgr._autonomy_managers = {"p1": am}

        result = SAIVerseManager.stop_autonomy(mgr, "p1")

        # 1. AM 停止 / 2. AUTONOMY_ENABLED=False
        am.stop.assert_called_once()
        self.assertEqual(persona.autonomy_enabled, False)
        self.assertEqual(result, {"autonomy_running": False})


if __name__ == "__main__":
    unittest.main()
