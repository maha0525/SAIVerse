"""_wait_response_timeout_provider の ACTIVITY_STATE ゲートのテスト。

2026-07-07: user_conversation は Idle でもタイマーを予約する例外を追加
(会話 episode の close [A1] がこのタイマーに乗っており、記録系は
「認知不変・全ペルソナ」が原則 — life_concept_map.md §8)。
social 等それ以外の wait_response Track は従来通り Active のみ。
post_conversation 判断の Active ゲートは fire_judgment_point 側 (別テスト)。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from saiverse.saiverse_manager import SAIVerseManager


def _fake_manager(activity_state: str) -> SimpleNamespace:
    persona = SimpleNamespace(activity_state=activity_state, sai_memory=None)
    return SimpleNamespace(
        _debug_manual_mode_personas=set(),
        personas={"p1": persona},
        # USER_CONV_TIMEOUT_MINUTES の読み出しは例外 → 既定値フォールバック
        # (provider 内の try/except が WARNING を出して既定 30 分を使う)
        SessionLocal=None,
        _DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES=(
            SAIVerseManager._DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES
        ),
    )


def _track(track_type: str) -> SimpleNamespace:
    return SimpleNamespace(persona_id="p1", track_id="t1", track_type=track_type)


def _call_provider(mgr, track):
    handler = SimpleNamespace(post_complete_behavior="wait_response")
    with patch(
        "sea.pulse_root_context.get_handler_for_track", return_value=handler
    ):
        return SAIVerseManager._wait_response_timeout_provider(mgr, track)


class WaitResponseTimeoutGateTest(unittest.TestCase):
    def test_idle_user_conversation_schedules(self):
        """Idle でも user_conversation はタイマー対象 (episode close のため)。"""
        result = _call_provider(_fake_manager("Idle"), _track("user_conversation"))
        self.assertIsNotNone(result)
        timeout_minutes, _last = result
        self.assertGreater(timeout_minutes, 0)

    def test_idle_social_skips(self):
        """Idle の user_conversation 以外は従来通り予約しない。"""
        result = _call_provider(_fake_manager("Idle"), _track("social"))
        self.assertIsNone(result)

    def test_active_social_schedules(self):
        """Active なら従来通り全 wait_response が対象。"""
        result = _call_provider(_fake_manager("Active"), _track("social"))
        self.assertIsNotNone(result)

    def test_debug_manual_mode_still_skips(self):
        """完全手動モードは user_conversation でも予約しない (既存仕様維持)。"""
        mgr = _fake_manager("Active")
        mgr._debug_manual_mode_personas = {"p1"}
        result = _call_provider(mgr, _track("user_conversation"))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
