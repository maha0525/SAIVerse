"""EventScheduler のユニットテスト (Phase 4-e)。

検証項目:
- schedule した時刻に callback が発火する
- 同じ key で再 schedule すると古い予約は cancel される
- cancel(key) で予約が消える
- 例外を投げる callback は WARN ログ + 該当予約のみ消去 (loop は止まらない)
- schedule_periodic は callback 完了ごとに再 schedule される
- schedule_periodic は callback 例外で停止する
- start/stop が複数回呼ばれても安全
"""
from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta

from saiverse.event_scheduler import EventScheduler


class EventSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = EventScheduler()
        self.scheduler.start()

    def tearDown(self) -> None:
        self.scheduler.stop()

    def _make_recorder(self):
        """発火回数とタイムスタンプを記録する callback とログを返す。"""
        log: list[float] = []
        evt = threading.Event()

        def cb() -> None:
            log.append(time.time())
            evt.set()

        return cb, log, evt

    # ------------------------------------------------------------------

    def test_schedule_fires_at_target_time(self) -> None:
        cb, log, evt = self._make_recorder()
        target = datetime.now() + timedelta(milliseconds=200)
        self.scheduler.schedule(target, cb, key="t1")

        self.assertTrue(evt.wait(timeout=2.0), "callback did not fire")
        self.assertEqual(len(log), 1)
        # 多少の遅延は許容、早すぎないこと
        elapsed = log[0] - target.timestamp()
        self.assertGreaterEqual(elapsed, -0.05)
        self.assertLess(elapsed, 1.0)

    def test_reschedule_overrides_previous_entry(self) -> None:
        """同じ key で再 schedule すると古い予約は cancel される。"""
        cb1, log1, _ = self._make_recorder()
        cb2, log2, evt2 = self._make_recorder()

        far_future = datetime.now() + timedelta(seconds=10)
        soon = datetime.now() + timedelta(milliseconds=200)

        # 先に far_future で登録
        self.scheduler.schedule(far_future, cb1, key="t1")
        # 上書きで soon に変更
        self.scheduler.schedule(soon, cb2, key="t1")

        self.assertTrue(evt2.wait(timeout=1.0), "second callback did not fire")
        time.sleep(0.3)  # cb1 が発火しないことの確認待ち

        self.assertEqual(len(log1), 0, "old callback should have been cancelled")
        self.assertEqual(len(log2), 1)

    def test_cancel_removes_entry(self) -> None:
        cb, log, _ = self._make_recorder()
        target = datetime.now() + timedelta(milliseconds=200)
        self.scheduler.schedule(target, cb, key="t1")

        self.assertTrue(self.scheduler.has_key("t1"))
        cancelled = self.scheduler.cancel("t1")
        self.assertTrue(cancelled)
        self.assertFalse(self.scheduler.has_key("t1"))

        time.sleep(0.5)
        self.assertEqual(len(log), 0, "cancelled callback should not fire")

    def test_cancel_returns_false_for_unknown_key(self) -> None:
        self.assertFalse(self.scheduler.cancel("nonexistent"))

    def test_callback_exception_is_swallowed(self) -> None:
        """例外を投げる callback の後でも、別の予約が正常に発火すること。"""
        evt_after = threading.Event()

        def boom() -> None:
            raise RuntimeError("boom")

        def good() -> None:
            evt_after.set()

        soon = datetime.now() + timedelta(milliseconds=100)
        soon_after = datetime.now() + timedelta(milliseconds=300)

        self.scheduler.schedule(soon, boom, key="bad")
        self.scheduler.schedule(soon_after, good, key="good")

        self.assertTrue(
            evt_after.wait(timeout=2.0),
            "scheduler should keep running after callback exception",
        )

    def test_periodic_repeats(self) -> None:
        """schedule_periodic は完了ごとに再 schedule される。"""
        log: list[float] = []
        target_count = 3
        done = threading.Event()

        def cb() -> None:
            log.append(time.time())
            if len(log) >= target_count:
                done.set()

        self.scheduler.schedule_periodic(
            interval_seconds=0.1,
            callback=cb,
            key="periodic",
            first_fire_immediate=True,
        )

        self.assertTrue(done.wait(timeout=2.0))
        self.assertGreaterEqual(len(log), target_count)
        self.scheduler.cancel("periodic")

    def test_periodic_stops_on_exception(self) -> None:
        """schedule_periodic は callback 例外で停止する。"""
        call_count = 0
        lock = threading.Lock()

        def cb() -> None:
            nonlocal call_count
            with lock:
                call_count += 1
            raise RuntimeError("periodic boom")

        self.scheduler.schedule_periodic(
            interval_seconds=0.05,
            callback=cb,
            key="periodic_fail",
            first_fire_immediate=True,
        )

        # 0.5 秒待っても 1 回しか呼ばれていないこと
        time.sleep(0.5)
        with lock:
            self.assertEqual(call_count, 1)
        self.assertFalse(self.scheduler.has_key("periodic_fail"))

    def test_pending_count(self) -> None:
        """有効な予約数を返す。"""
        self.assertEqual(self.scheduler.pending_count(), 0)

        far = datetime.now() + timedelta(seconds=10)
        self.scheduler.schedule(far, lambda: None, key="a")
        self.scheduler.schedule(far, lambda: None, key="b")
        self.assertEqual(self.scheduler.pending_count(), 2)

        self.scheduler.cancel("a")
        self.assertEqual(self.scheduler.pending_count(), 1)


class EventSchedulerLifecycleTest(unittest.TestCase):
    """start/stop の冪等性。"""

    def test_double_start_is_safe(self) -> None:
        sched = EventScheduler()
        sched.start()
        sched.start()  # 二重起動でも例外にならない
        sched.stop()

    def test_double_stop_is_safe(self) -> None:
        sched = EventScheduler()
        sched.start()
        sched.stop()
        sched.stop()  # 二重停止でも例外にならない

    def test_stop_without_start_is_safe(self) -> None:
        sched = EventScheduler()
        sched.stop()  # 起動してなくても例外にならない


if __name__ == "__main__":
    unittest.main()
