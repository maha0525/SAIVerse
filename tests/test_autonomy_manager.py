"""Tests for AutonomyManager (Phase 4-e: EventScheduler 駆動)。

旧 Decision/Execution/Stelis/PulseController-callback 連携は MetaLayer 経由に
統合されたため、このクラス自体の責務は「ペルソナごとの定期 tick タイマー」だけ。
さらに Phase 4-e で sleep ループ thread は廃止され、EventScheduler に push する
形に再構成された。Phase 4 (pulse_dispatch §9.4) で tick 発火経路は MetaLayer の
``on_periodic_tick`` 直叩きから ``PulseDispatcher.dispatch_autonomy_tick`` 経由に
切り替わっている。テストでは本物の EventScheduler を mock manager に持たせる。
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from saiverse.autonomy_manager import (
    AutonomyManager,
    AutonomyState,
)
from saiverse.event_scheduler import EventScheduler


def _make_manager_mock():
    """Create a mock SAIVerseManager with pulse_dispatcher + 本物の EventScheduler。"""
    manager = MagicMock()
    # pulse_dispatcher が tick 発火先 (pulse_dispatch §9.4)。
    manager.pulse_dispatcher = MagicMock()
    manager.pulse_dispatcher.dispatch_autonomy_tick = MagicMock()
    # meta_layer は AutonomyManager._load_judgment_config 用に残す。
    manager.meta_layer = MagicMock()
    manager.event_scheduler = EventScheduler()
    manager.event_scheduler.start()

    persona = MagicMock()
    persona.persona_id = "test_persona"
    persona.current_building_id = "hall_1"
    manager.all_personas = {"test_persona": persona}

    return manager, persona


def _stop_manager(manager):
    """tearDown 用: EventScheduler を停止する。"""
    sched = getattr(manager, "event_scheduler", None)
    if sched is not None:
        sched.stop()


class _BaseAutonomyTest(unittest.TestCase):
    """Mock manager + EventScheduler を毎回立てて tearDown で stop する基底。"""

    def setUp(self) -> None:
        self.manager, self.persona = _make_manager_mock()

    def tearDown(self) -> None:
        _stop_manager(self.manager)


class TestAutonomyManagerLifecycle(_BaseAutonomyTest):

    def test_initial_state_is_stopped(self):
        am = AutonomyManager("test_persona", self.manager)
        self.assertEqual(am.state, AutonomyState.STOPPED)
        self.assertFalse(am.is_running)

    def test_start_launches_loop(self):
        am = AutonomyManager("test_persona", self.manager, interval_minutes=0.01)
        try:
            self.assertTrue(am.start())
            self.assertTrue(am.is_running)
        finally:
            am.stop()

    def test_start_returns_false_if_already_running(self):
        am = AutonomyManager("test_persona", self.manager, interval_minutes=0.01)
        am.start()
        try:
            self.assertFalse(am.start())
        finally:
            am.stop()

    def test_stop_terminates_loop(self):
        am = AutonomyManager("test_persona", self.manager, interval_minutes=0.01)
        am.start()
        time.sleep(0.05)
        self.assertTrue(am.stop())
        self.assertEqual(am.state, AutonomyState.STOPPED)

    def test_stop_returns_false_if_not_running(self):
        am = AutonomyManager("test_persona", self.manager)
        self.assertFalse(am.stop())


class TestAutonomyManagerConfig(_BaseAutonomyTest):

    def test_set_interval(self):
        am = AutonomyManager("test_persona", self.manager)
        am.set_interval(10)
        self.assertEqual(am.interval_minutes, 10)

    def test_set_interval_minimum(self):
        am = AutonomyManager("test_persona", self.manager)
        am.set_interval(0.1)
        self.assertEqual(am.interval_minutes, 0.5)

    def test_set_models_compat(self):
        """API 互換: 旧 set_models は値を保持するだけ (Phase C-2 で no-op)."""
        am = AutonomyManager("test_persona", self.manager)
        am.set_models(decision_model="claude-opus", execution_model="gemini-flash")
        self.assertEqual(am.decision_model, "claude-opus")
        self.assertEqual(am.execution_model, "gemini-flash")

    def test_get_status(self):
        am = AutonomyManager("test_persona", self.manager, interval_minutes=10)
        status = am.get_status()
        self.assertEqual(status["persona_id"], "test_persona")
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["interval_minutes"], 10)
        self.assertIn("decision_model", status)
        self.assertIn("execution_model", status)
        self.assertIn("current_cycle_id", status)
        self.assertIn("last_report", status)


class TestSetIntervalReschedule(_BaseAutonomyTest):
    """set_interval が WAITING 中に呼ばれたら新 interval で即再 schedule する。"""

    def test_set_interval_while_waiting_reschedules(self):
        """tick 実行 → WAITING 状態で set_interval を呼ぶと _schedule_next_tick が即呼ばれる。

        旧バグ: state == RUNNING のみで判定していたため、WAITING 中の set_interval
        が無視され、新 interval が次回 tick まで反映されなかった (最大 50 分待機)。
        ``interval_minutes`` の最小値は 0.5 分なので実時間検証は重い。spy で
        re-schedule の呼び出し回数を直接確認する。
        """
        am = AutonomyManager("test_persona", self.manager, interval_minutes=50.0)
        am.start()
        try:
            # start 直後の即時 tick が走るのを待つ → WAITING に遷移
            time.sleep(0.2)
            self.assertEqual(am.state, AutonomyState.WAITING)

            with patch.object(
                am, "_schedule_next_tick", wraps=am._schedule_next_tick
            ) as spy:
                am.set_interval(40.0)  # 50 → 40 (差分あり、reschedule トリガ)
                spy.assert_called_once()

        finally:
            am.stop()

    def test_set_interval_while_deciding_skips_reschedule(self):
        """tick 実行中 (DECIDING) の set_interval は再 schedule しない。

        DECIDING 中に再 schedule すると tick 完了時の _schedule_next_tick と
        重複する。tick 完了時の call で新 interval は読まれるので、ここでは
        何もしないのが正しい。
        """
        # tick 内で時間稼ぎする mock callback
        # threading.Event で同期する (tick 開始 / tick 完了)
        import threading as _t
        evt_started = _t.Event()
        evt_finish = _t.Event()

        def slow_tick(*args, **kwargs):
            evt_started.set()
            evt_finish.wait(timeout=2.0)

        self.manager.pulse_dispatcher.dispatch_autonomy_tick.side_effect = slow_tick

        am = AutonomyManager("test_persona", self.manager, interval_minutes=50.0)
        am.start()
        try:
            # tick が始まるまで待つ (DECIDING 状態に入る)
            self.assertTrue(evt_started.wait(timeout=2.0))
            self.assertEqual(am.state, AutonomyState.DECIDING)

            with patch.object(
                am, "_schedule_next_tick", wraps=am._schedule_next_tick
            ) as spy:
                am.set_interval(30.0)
                spy.assert_not_called()

            # tick を完了させる → tick 完了時に _schedule_next_tick が
            # 自動的に呼ばれて新 interval (30) が反映される
            evt_finish.set()
        finally:
            evt_finish.set()
            am.stop()


class TestPeriodicTick(_BaseAutonomyTest):

    def test_loop_invokes_pulse_dispatcher_on_periodic_tick(self):
        """Tick loop calls PulseDispatcher.dispatch_autonomy_tick(persona_id, context=...) ."""
        am = AutonomyManager("test_persona", self.manager, interval_minutes=0.01)
        am.start()
        try:
            time.sleep(0.2)
            self.assertGreaterEqual(
                self.manager.pulse_dispatcher.dispatch_autonomy_tick.call_count, 1
            )
            args, kwargs = (
                self.manager.pulse_dispatcher.dispatch_autonomy_tick.call_args
            )
            self.assertEqual(args[0], "test_persona")
            ctx = kwargs.get("context") or (args[1] if len(args) > 1 else None)
            self.assertIsInstance(ctx, dict)
            self.assertIn("cycle_id", ctx)
            self.assertIn("interval_seconds", ctx)
        finally:
            am.stop()

    def test_loop_records_completed_status_on_success(self):
        am = AutonomyManager("test_persona", self.manager, interval_minutes=0.01)
        am.start()
        try:
            time.sleep(0.2)
            self.assertIsNotNone(am.last_report)
            self.assertEqual(am.last_report.status, "completed")
        finally:
            am.stop()

    def test_loop_records_error_when_pulse_dispatcher_missing(self):
        self.manager.pulse_dispatcher = None
        am = AutonomyManager("test_persona", self.manager, interval_minutes=0.01)
        am.start()
        try:
            time.sleep(0.2)
            self.assertIsNotNone(am.last_report)
            self.assertEqual(am.last_report.status, "error")
        finally:
            am.stop()

    def test_loop_records_error_when_tick_raises(self):
        self.manager.pulse_dispatcher.dispatch_autonomy_tick.side_effect = RuntimeError(
            "boom"
        )
        am = AutonomyManager("test_persona", self.manager, interval_minutes=0.01)
        am.start()
        try:
            time.sleep(0.2)
            self.assertIsNotNone(am.last_report)
            self.assertEqual(am.last_report.status, "error")
            self.assertIn("boom", am.last_report.error or "")
        finally:
            am.stop()


class TestTickDeadlineFollowsVirtualClock(_BaseAutonomyTest):
    """tick の期限は EventScheduler と同じ saiverse.clock.now() で刻む
    (issue autonomy_manager_tick_uses_wall_clock)。datetime.now() で刻むと、
    仮想クロックのシム中に期限がシム終了後 (実時刻) へ飛び、一度も発火しない。"""

    def test_start_and_next_tick_are_due_in_virtual_time(self):
        from datetime import datetime, timedelta

        from saiverse import clock

        # dispatch スレッドを使わず run_due で同期駆動する (シムの実行経路)
        self.manager.event_scheduler.stop()
        scheduler = EventScheduler()
        self.manager.event_scheduler = scheduler

        virtual_now = datetime(2020, 1, 1, 9, 0, 0)  # 実時刻よりずっと過去
        clock.enable_virtual(virtual_now)
        am = AutonomyManager("test_persona", self.manager, interval_minutes=10)
        try:
            self.assertTrue(am.start())
            # 即時 tick は仮想時刻の「いま」が期限 → 仮想時刻で消化できる
            self.assertEqual(scheduler.run_due(clock.now()), 1)
            self.assertEqual(
                self.manager.pulse_dispatcher.dispatch_autonomy_tick.call_count, 1,
            )
            # 次回は仮想時刻 + interval
            next_fire = scheduler.next_fire_time()
            self.assertEqual(
                next_fire, virtual_now + timedelta(minutes=am.interval_minutes),
            )
            # 残り秒数も仮想時刻基準 (実時刻基準だと 0 に潰れる)
            self.assertEqual(
                am.get_next_tick_eta_seconds(), int(am.interval_minutes * 60),
            )
        finally:
            am.stop()
            clock.disable_virtual()


if __name__ == "__main__":
    unittest.main()
