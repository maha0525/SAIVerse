"""メタ判断 Pulse 失敗時リカバリ (案C) のテスト。

phase4_meta_judgment_recovery.md 案C: 連続失敗が閾値に達したら ACTIVITY_STATE を
Idle に降格 + host イベント通知する挙動を検証する。
"""
import unittest
from unittest.mock import MagicMock

from saiverse.meta_layer import MetaLayer


def _make_layer():
    manager = MagicMock()
    manager.track_manager = MagicMock()
    return MetaLayer(manager), manager


class TestPersistentFailureHandling(unittest.TestCase):
    def test_default_config_has_threshold(self):
        self.assertIn("max_consecutive_failures", MetaLayer._DEFAULT_JUDGMENT_CONFIG)

    def test_handle_persistent_failure_downgrades_activity_state(self):
        layer, manager = _make_layer()
        persona = MagicMock()
        persona.persona_id = "p1"

        layer._handle_persistent_failure(persona, "b1", 3, "boom")

        # in-memory persona の降格
        self.assertEqual(persona.activity_state, "Idle")
        # DB セッションを開いて commit したこと
        manager.SessionLocal.assert_called()
        db = manager.SessionLocal.return_value
        db.commit.assert_called()
        db.close.assert_called()

    def test_handle_persistent_failure_emits_host_event(self):
        layer, manager = _make_layer()
        persona = MagicMock()
        persona.persona_id = "p1"

        layer._handle_persistent_failure(persona, "b1", 4, "boom")

        manager.add_building_event.assert_called_once()
        args, kwargs = manager.add_building_event.call_args
        self.assertEqual(args[0], "b1")
        self.assertEqual(kwargs.get("heard_by"), ["p1"])
        event = args[1]
        self.assertEqual(event["role"], "host")
        self.assertEqual(event["metadata"]["event"]["type"], "meta_judgment_failure")
        self.assertEqual(event["metadata"]["event"]["consecutive"], 4)

    def test_notification_failure_does_not_raise(self):
        # 通知が失敗しても降格は完了し、例外は外に漏れない (best-effort)。
        layer, manager = _make_layer()
        manager.add_building_event.side_effect = RuntimeError("notify down")
        persona = MagicMock()
        persona.persona_id = "p1"

        layer._handle_persistent_failure(persona, "b1", 3, "boom")  # should not raise
        self.assertEqual(persona.activity_state, "Idle")


class TestForceFailDebugFlag(unittest.TestCase):
    """開発者モード force_fail: meta_judgment を強制失敗させ ① 経路を検証可能にする。"""

    def test_default_config_has_force_fail_false(self):
        self.assertIn("force_fail", MetaLayer._DEFAULT_JUDGMENT_CONFIG)
        self.assertFalse(MetaLayer._DEFAULT_JUDGMENT_CONFIG["force_fail"])

    def test_force_fail_skips_submit_and_triggers_failure_path(self):
        layer, manager = _make_layer()
        persona = MagicMock()
        persona.persona_id = "p1"
        persona.current_building_id = "b1"
        manager.pulse_controller = MagicMock()

        running = MagicMock()
        running.track_id = "t1"

        # force_fail=True, リトライ無し, 1 回失敗で降格。
        layer._load_judgment_config = MagicMock(return_value={
            "force_fail": True, "max_retries": 0, "retry_backoff_seconds": 0,
            "max_consecutive_failures": 1,
        })
        layer._classify_situation = MagicMock(return_value={
            "kind": "running_active", "running": [running],
            "alert": [], "pending_or_unstarted": [],
        })
        layer._SITUATION_PLAYBOOK_MAP = {"running_active": "meta_running"}
        layer._build_situation_text = MagicMock(return_value="sit")
        layer._build_response_schema = MagicMock(return_value={})
        layer._build_recent_judgments_block = MagicMock(return_value="")
        layer._handle_persistent_failure = MagicMock()

        layer._run_judgment_via_playbook(persona, "", {})

        # submit は一度も呼ばれない (強制失敗で skip)。
        manager.pulse_controller.submit_meta_judgment.assert_not_called()
        # 連続失敗閾値 (=1) に達して降格処理が走る。
        layer._handle_persistent_failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
