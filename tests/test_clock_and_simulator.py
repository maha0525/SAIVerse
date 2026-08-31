"""仮想クロック + EventScheduler シム駆動 + DaySimulator のテスト (自律行動 v2 §12)。

検証項目:
- clock: 実モード/仮想モードの切替、advance_to の後退拒否、スレッドセーフの基本
- EventScheduler.run_due / next_fire_time: 仮想モードでの同期駆動
- DaySimulator: 複数イベントが期限順に、callback から見た clock.now() が
  それぞれの仮想時刻で実行されること (実時間 1 秒未満)
- callback 内から次イベントを push する連鎖 (時間割 → 就寝)
- 仮想モード中は dispatch スレッドが発火しないこと
- 実モードの既存挙動が壊れていないこと (最小スモーク; 全量は test_event_scheduler.py)
"""
from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta

from saiverse import clock
from saiverse.day_simulator import DaySimulator
from saiverse.event_scheduler import EventScheduler


class ClockTest(unittest.TestCase):
    def tearDown(self) -> None:
        clock.disable_virtual()

    def test_real_mode_tracks_wall_clock(self) -> None:
        self.assertFalse(clock.is_virtual())
        before = datetime.now()
        got = clock.now()
        after = datetime.now()
        self.assertGreaterEqual(got, before)
        self.assertLessEqual(got, after)

    def test_virtual_mode_returns_fixed_time(self) -> None:
        start = datetime(2026, 7, 4, 9, 0, 0)
        clock.enable_virtual(start)
        self.assertTrue(clock.is_virtual())
        self.assertEqual(clock.now(), start)
        # 実時間が経過しても仮想時刻は動かない
        time.sleep(0.05)
        self.assertEqual(clock.now(), start)

    def test_advance_to_moves_forward(self) -> None:
        start = datetime(2026, 7, 4, 9, 0, 0)
        clock.enable_virtual(start)
        target = start + timedelta(hours=3)
        clock.advance_to(target)
        self.assertEqual(clock.now(), target)
        # 同時刻への advance は no-op として許容
        clock.advance_to(target)
        self.assertEqual(clock.now(), target)

    def test_advance_backwards_raises(self) -> None:
        start = datetime(2026, 7, 4, 12, 0, 0)
        clock.enable_virtual(start)
        with self.assertRaises(ValueError):
            clock.advance_to(start - timedelta(minutes=1))
        # 失敗後も時刻は変わらない
        self.assertEqual(clock.now(), start)

    def test_advance_without_virtual_mode_raises(self) -> None:
        self.assertFalse(clock.is_virtual())
        with self.assertRaises(RuntimeError):
            clock.advance_to(datetime.now())

    def test_disable_virtual_returns_to_real_time(self) -> None:
        clock.enable_virtual(datetime(2020, 1, 1, 0, 0, 0))
        clock.disable_virtual()
        self.assertFalse(clock.is_virtual())
        # 実時刻に戻っている (2020 年ではない)
        self.assertGreater(clock.now(), datetime(2025, 1, 1))
        # 非仮想時の disable は no-op
        clock.disable_virtual()

    def test_thread_safety_basic(self) -> None:
        """並行 read/advance で例外が出ず、読み値が開始〜終了の範囲に収まる。"""
        start = datetime(2026, 7, 4, 0, 0, 0)
        end = start + timedelta(hours=24)
        clock.enable_virtual(start)
        errors: list[Exception] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    got = clock.now()
                    if not (start <= got <= end):
                        raise AssertionError(f"out of range: {got}")
            except Exception as exc:  # noqa: BLE001 - テストの失敗収集
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        try:
            for i in range(1, 25):
                clock.advance_to(start + timedelta(hours=i))
        finally:
            stop.set()
            for t in readers:
                t.join(timeout=2)
        self.assertEqual(errors, [])
        self.assertEqual(clock.now(), end)


class EventSchedulerVirtualDriveTest(unittest.TestCase):
    """仮想モードでの同期駆動 API (next_fire_time / run_due)。"""

    def setUp(self) -> None:
        # シム前提: start() は呼ばない (背景スレッドなし)
        self.scheduler = EventScheduler()

    def tearDown(self) -> None:
        clock.disable_virtual()

    def test_next_fire_time(self) -> None:
        self.assertIsNone(self.scheduler.next_fire_time())
        at_12 = datetime(2026, 7, 4, 12, 0, 0)
        at_9 = datetime(2026, 7, 4, 9, 0, 0)
        self.scheduler.schedule(at_12, lambda: None, key="noon")
        self.scheduler.schedule(at_9, lambda: None, key="morning")
        self.assertEqual(self.scheduler.next_fire_time(), at_9)
        self.scheduler.cancel("morning")
        self.assertEqual(self.scheduler.next_fire_time(), at_12)

    def test_run_due_executes_only_due_entries_in_order(self) -> None:
        fired: list[str] = []
        base = datetime(2026, 7, 4, 0, 0, 0)
        self.scheduler.schedule(base + timedelta(hours=21), lambda: fired.append("night"), key="night")
        self.scheduler.schedule(base + timedelta(hours=9), lambda: fired.append("morning"), key="morning")
        self.scheduler.schedule(base + timedelta(hours=12), lambda: fired.append("noon"), key="noon")

        executed = self.scheduler.run_due(base + timedelta(hours=12))
        self.assertEqual(executed, 2)
        self.assertEqual(fired, ["morning", "noon"])
        self.assertEqual(self.scheduler.pending_count(), 1)

    def test_run_due_consumes_chained_pushes_within_deadline(self) -> None:
        """callback 内で push された「期限内」イベントも同じ呼び出しで消化する。"""
        fired: list[str] = []
        base = datetime(2026, 7, 4, 9, 0, 0)

        def first() -> None:
            fired.append("first")
            # 期限 (10:00) 以前の派生イベント → 同じ run_due で消化される
            self.scheduler.schedule(base + timedelta(minutes=30), lambda: fired.append("chained"), key="chained")
            # 期限より後の派生イベント → 残る
            self.scheduler.schedule(base + timedelta(hours=12), lambda: fired.append("later"), key="later")

        self.scheduler.schedule(base, first, key="first")
        executed = self.scheduler.run_due(base + timedelta(hours=1))
        self.assertEqual(executed, 2)
        self.assertEqual(fired, ["first", "chained"])
        self.assertTrue(self.scheduler.has_key("later"))

    def test_dispatch_thread_does_not_fire_in_virtual_mode(self) -> None:
        """仮想モード中は背景 dispatch スレッドが発火しない (run_due のみが実行経路)。"""
        clock.enable_virtual(datetime(2026, 7, 4, 9, 0, 0))
        scheduler = EventScheduler()
        scheduler.start()  # 背景スレッドが居ても発火しないことの確認
        try:
            fired = threading.Event()
            # 仮想時刻では既に期限切れの予約
            scheduler.schedule(datetime(2026, 7, 4, 8, 0, 0), fired.set, key="overdue")
            # 背景スレッドに発火の機会を与えるが、仮想モード中は発火しないはず
            self.assertFalse(fired.wait(timeout=0.7))
            # 同期駆動では発火する
            executed = scheduler.run_due(clock.now())
            self.assertEqual(executed, 1)
            self.assertTrue(fired.is_set())
        finally:
            scheduler.stop()


class DaySimulatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = EventScheduler()

    def tearDown(self) -> None:
        clock.disable_virtual()

    def test_three_events_fire_in_order_at_virtual_times(self) -> None:
        """9:00/12:00/21:00 が順に、callback から見た clock.now() が各仮想時刻で実行される。"""
        base = datetime(2026, 7, 4, 0, 0, 0)
        seen: list[tuple[str, datetime]] = []

        def make_cb(name: str):
            return lambda: seen.append((name, clock.now()))

        self.scheduler.schedule(base + timedelta(hours=12), make_cb("noon"), key="noon")
        self.scheduler.schedule(base + timedelta(hours=9), make_cb("morning"), key="morning")
        self.scheduler.schedule(base + timedelta(hours=21), make_cb("night"), key="night")

        wall_start = time.monotonic()
        sim = DaySimulator(self.scheduler, start=base + timedelta(hours=8), end=base + timedelta(hours=24))
        total = sim.run()
        wall_elapsed = time.monotonic() - wall_start

        self.assertEqual(total, 3)
        self.assertEqual(
            seen,
            [
                ("morning", base + timedelta(hours=9)),
                ("noon", base + timedelta(hours=12)),
                ("night", base + timedelta(hours=21)),
            ],
        )
        # 終了時に end まで advance されている
        self.assertEqual(clock.now(), base + timedelta(hours=24))
        # 16 仮想時間が実時間 1 秒未満で完走する
        self.assertLess(wall_elapsed, 1.0)

    def test_chained_schedule_from_callback(self) -> None:
        """時間割 → 就寝のような、callback 内から未来イベントを push する連鎖。"""
        base = datetime(2026, 7, 4, 0, 0, 0)
        seen: list[tuple[str, datetime]] = []

        def bedtime() -> None:
            seen.append(("bedtime", clock.now()))

        def timetable() -> None:
            seen.append(("timetable", clock.now()))
            # 時間割が就寝 (21:00) を決めて push する
            self.scheduler.schedule(base + timedelta(hours=21), bedtime, key="bedtime")

        self.scheduler.schedule(base + timedelta(hours=9), timetable, key="timetable")

        sim = DaySimulator(self.scheduler, start=base + timedelta(hours=8), end=base + timedelta(hours=24))
        total = sim.run()

        self.assertEqual(total, 2)
        self.assertEqual(
            seen,
            [
                ("timetable", base + timedelta(hours=9)),
                ("bedtime", base + timedelta(hours=21)),
            ],
        )

    def test_events_beyond_end_are_not_executed(self) -> None:
        base = datetime(2026, 7, 4, 0, 0, 0)
        fired: list[str] = []
        self.scheduler.schedule(base + timedelta(hours=9), lambda: fired.append("in"), key="in")
        self.scheduler.schedule(base + timedelta(hours=30), lambda: fired.append("out"), key="out")

        sim = DaySimulator(self.scheduler, start=base + timedelta(hours=8), end=base + timedelta(hours=24))
        total = sim.run()

        self.assertEqual(total, 1)
        self.assertEqual(fired, ["in"])
        self.assertTrue(self.scheduler.has_key("out"))
        self.assertEqual(clock.now(), base + timedelta(hours=24))

    def test_empty_queue_advances_to_end(self) -> None:
        base = datetime(2026, 7, 4, 8, 0, 0)
        sim = DaySimulator(self.scheduler, start=base, end=base + timedelta(hours=16))
        total = sim.run()
        self.assertEqual(total, 0)
        self.assertEqual(clock.now(), base + timedelta(hours=16))

    def test_overdue_at_start_runs_at_start_time(self) -> None:
        """start 以前が期限のイベントは start 時刻で実行される (時計は後退しない)。"""
        base = datetime(2026, 7, 4, 8, 0, 0)
        seen: list[datetime] = []
        self.scheduler.schedule(base - timedelta(hours=1), lambda: seen.append(clock.now()), key="overdue")

        sim = DaySimulator(self.scheduler, start=base, end=base + timedelta(hours=1))
        total = sim.run()
        self.assertEqual(total, 1)
        self.assertEqual(seen, [base])

    def test_end_before_start_raises(self) -> None:
        base = datetime(2026, 7, 4, 8, 0, 0)
        with self.assertRaises(ValueError):
            DaySimulator(self.scheduler, start=base, end=base - timedelta(hours=1))


class RealModeSmokeTest(unittest.TestCase):
    """実モードの既存挙動が壊れていないことの最小スモーク。

    網羅的な実モードテストは tests/test_event_scheduler.py が担う。
    """

    def test_schedule_fires_on_wall_clock(self) -> None:
        self.assertFalse(clock.is_virtual())
        scheduler = EventScheduler()
        scheduler.start()
        try:
            fired = threading.Event()
            scheduler.schedule(
                datetime.now() + timedelta(milliseconds=150),
                fired.set,
                key="real_smoke",
            )
            self.assertTrue(fired.wait(timeout=2.0), "real-mode callback did not fire")
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
