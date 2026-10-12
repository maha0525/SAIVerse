"""EventScheduler のユニットテスト (Phase 4-e)。

検証項目:
- schedule した時刻に callback が発火する
- 同じ key で再 schedule すると古い予約は cancel される
- cancel(key) で予約が消える
- 例外を投げる callback は WARN ログ + 該当予約のみ消去 (loop は止まらない)
- callback は発火ごとのワーカースレッドで走り、ブロックしても他の予約を遅らせない
- run_due (仮想クロックの同期駆動) は呼び出しスレッド上で同期実行する
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

    def test_cancel_prefix_removes_matching_entries_only(self) -> None:
        """prefix 一致の予約だけを一括 cancel し、他 persona の予約は残す
        (ライフ終端の keep-alive 一括 cancel 用、beat_execution_context.md §3.1)。"""
        cb, log, _ = self._make_recorder()
        target = datetime.now() + timedelta(milliseconds=200)
        self.scheduler.schedule(target, cb, key="ttl:air:claude-x")
        self.scheduler.schedule(target, cb, key="ttl:air:light-model")
        self.scheduler.schedule(target, cb, key="ttl:noa:claude-x")

        cancelled = self.scheduler.cancel_prefix("ttl:air:")
        self.assertEqual(sorted(cancelled), ["ttl:air:claude-x", "ttl:air:light-model"])
        self.assertFalse(self.scheduler.has_key("ttl:air:claude-x"))
        self.assertFalse(self.scheduler.has_key("ttl:air:light-model"))
        self.assertTrue(self.scheduler.has_key("ttl:noa:claude-x"))

        time.sleep(0.5)
        self.assertEqual(len(log), 1, "only the non-matching reservation should fire")

    def test_cancel_prefix_returns_empty_for_no_match(self) -> None:
        self.assertEqual(self.scheduler.cancel_prefix("ttl:nonexistent:"), [])

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

    def test_blocking_callback_does_not_delay_other_keys(self) -> None:
        """長くブロックする callback が走っていても、別 key の予約が時刻どおり発火する
        (v0.4 自律運転 §8.2 決定 A — 発火装置は手渡すだけ)。

        旧実装 (dispatch スレッドで同期実行) では、blocker が release されるまで
        "on_time" が発火できず、このテストは timeout で落ちる。
        """
        release = threading.Event()
        blocker_started = threading.Event()
        on_time_log: list[float] = []
        on_time_fired = threading.Event()
        blocker_thread_names: list[str] = []

        def blocker() -> None:
            blocker_thread_names.append(threading.current_thread().name)
            blocker_started.set()
            release.wait(timeout=10.0)  # LLM 呼び出し等の長いブロックを模す

        def on_time() -> None:
            on_time_log.append(time.time())
            on_time_fired.set()

        try:
            self.scheduler.schedule(datetime.now(), blocker, key="blocker")
            self.assertTrue(blocker_started.wait(timeout=2.0), "blocker did not start")

            target = datetime.now() + timedelta(milliseconds=200)
            self.scheduler.schedule(target, on_time, key="on_time")

            self.assertTrue(
                on_time_fired.wait(timeout=2.0),
                "a blocking callback must not hold back other reservations",
            )
            self.assertFalse(release.is_set())  # blocker はまだ走行中
            elapsed = on_time_log[0] - target.timestamp()
            self.assertGreaterEqual(elapsed, -0.05)
            self.assertLess(elapsed, 1.0)
            # callback は dispatch スレッドではなく発火ごとのワーカーで走る
            self.assertEqual(blocker_thread_names, ["evt-fire:blocker"])
        finally:
            release.set()

    def test_raising_callback_on_worker_does_not_affect_others(self) -> None:
        """例外を投げる callback はワーカー上で吸収され、同時刻の予約・後続の予約・
        dispatch ループのどれにも影響しない。"""
        good_same_time = threading.Event()
        good_later = threading.Event()
        raised_on: list[str] = []

        def boom() -> None:
            raised_on.append(threading.current_thread().name)
            raise RuntimeError("boom on worker")

        now = datetime.now()
        self.scheduler.schedule(now, boom, key="bad")
        self.scheduler.schedule(now, good_same_time.set, key="good_same_time")
        self.scheduler.schedule(
            now + timedelta(milliseconds=300), good_later.set, key="good_later",
        )

        self.assertTrue(good_same_time.wait(timeout=2.0))
        self.assertTrue(good_later.wait(timeout=2.0))
        self.assertEqual(raised_on, ["evt-fire:bad"])
        self.assertFalse(self.scheduler.has_key("bad"))
        # dispatch ループは生きている (例外がワーカーの外へ漏れていない)
        after = threading.Event()
        self.scheduler.schedule(datetime.now(), after.set, key="after")
        self.assertTrue(after.wait(timeout=2.0))

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


class RunDueSynchronousTest(unittest.TestCase):
    """run_due (仮想クロックの同期駆動) はワーカーへ手渡さず、呼び出しスレッド上で
    callback を実行し終えてから返る — シミュレーションの決定性のための契約。"""

    def test_run_due_executes_on_calling_thread_before_returning(self) -> None:
        scheduler = EventScheduler()  # start() しない
        ran_on: list[str] = []
        scheduler.schedule(
            datetime.now() - timedelta(seconds=1),
            lambda: ran_on.append(threading.current_thread().name),
            key="sync",
        )
        self.assertEqual(scheduler.run_due(datetime.now()), 1)
        # 返った時点で実行済み (ワーカーに投げていれば空のことがある)
        self.assertEqual(ran_on, [threading.current_thread().name])


class ScheduleIfAbsentTest(unittest.TestCase):
    """schedule_if_absent の契約 (2026-07-29 追加)。

    用途は「失われた予約を復旧する」操作。復旧は穴を埋める仕事であって、現に
    生きている予約を置き換える仕事ではない。上書きしてしまうと、その間に通常経路が
    張った予約を潰し、基準時刻が張り直しの瞬間へ丸め直されて期限が後退する。

    ``has_key`` で確認してから ``schedule`` を呼ぶ形 (check-then-act) では判定と
    登録の隙間に別スレッドが割り込めるため、同一ロック区間で行う専用 API を持つ。
    発火まで通して lazy deletion (cancelled は heap に残し dispatch でスキップ) の
    契約も一緒に固定する。

    **原子性そのものはここでは証明していない** — 当初 barrier で 50 回競合させる
    テストを置いたが、barrier が揃えるのは呼び出しの開始前だけで、その後どちらかが
    丸ごと先に完了すれば非原子的実装でも期待値どおりになる (Codex が非原子的な
    mutant で 1000 回実行し失敗 0 回と実測。私が「原子性を担保する」と書いたのは
    誇張だった)。原子性は「判定と登録が同一ロック区間にある」という実装の形で読む。
    回帰の歯止めは
    ``test_track_manager.test_recovery_never_uses_the_overwriting_schedule_api``
    が担う — 復旧経路が check-then-act に戻ると、そちらが落ちる (実測確認済み)。
    """

    def setUp(self) -> None:
        self.scheduler = EventScheduler()  # start() しない (run_due で同期実行)
        self.fired: list[str] = []

    def _at(self, seconds: float) -> datetime:
        return datetime.now() + timedelta(seconds=seconds)

    def test_registers_and_fires_when_absent(self) -> None:
        armed = self.scheduler.schedule_if_absent(
            fire_at=self._at(-1), callback=lambda: self.fired.append("new"), key="k1",
        )
        self.assertTrue(armed)
        self.assertEqual(self.scheduler.run_due(datetime.now()), 1)
        self.assertEqual(self.fired, ["new"])

    def test_existing_reservation_survives_and_fires(self) -> None:
        """既存があれば False。**発火するのは元の callback と元の期限**。"""
        self.scheduler.schedule(
            fire_at=self._at(-1), callback=lambda: self.fired.append("original"),
            key="k1",
        )
        armed = self.scheduler.schedule_if_absent(
            fire_at=self._at(600), callback=lambda: self.fired.append("intruder"),
            key="k1",
        )
        self.assertFalse(armed)
        # 元の期限 (過去) のまま生きているので、いま run_due で発火する。
        # 上書きされていれば +600 秒になり、ここで 0 件になる。
        self.assertEqual(self.scheduler.run_due(datetime.now()), 1)
        self.assertEqual(self.fired, ["original"])

    def test_cancelled_entry_does_not_block_recovery(self) -> None:
        """cancel 済みは「有効な予約」ではない → 復旧が張り直せて、一度だけ発火する。

        lazy deletion で heap には cancelled エントリが残るため、二重発火や
        取り消し済み callback の実行が起きないことも同時に確かめる。
        """
        self.scheduler.schedule(
            fire_at=self._at(-1), callback=lambda: self.fired.append("cancelled"),
            key="k1",
        )
        self.scheduler.cancel("k1")
        armed = self.scheduler.schedule_if_absent(
            fire_at=self._at(-1), callback=lambda: self.fired.append("recovered"),
            key="k1",
        )
        self.assertTrue(armed)
        self.assertEqual(self.scheduler.run_due(datetime.now()), 1)
        self.assertEqual(self.fired, ["recovered"])

    def test_plain_schedule_still_replaces(self) -> None:
        """通常の schedule は従来どおり上書きする (既存挙動を変えていない)。"""
        self.scheduler.schedule(
            fire_at=self._at(-1), callback=lambda: self.fired.append("old"), key="k1",
        )
        self.scheduler.schedule(
            fire_at=self._at(-1), callback=lambda: self.fired.append("new"), key="k1",
        )
        self.assertEqual(self.scheduler.run_due(datetime.now()), 1)
        self.assertEqual(self.fired, ["new"])

    def test_fires_through_the_real_dispatch_thread(self) -> None:
        """復旧予約が **本番の dispatch スレッド経由で** 期限どおり発火する。

        run_due は同期実行の別経路なので、これだけだと ``schedule_if_absent`` から
        ``Condition.notify()`` を削っても全テストが通ってしまう。本番では dispatch
        スレッドが「空の heap」または「遠い既存予約」で待機しており、起こさないと
        復旧した予約が期限どおり発火しない = 会話の出来事が閉じないままになる。
        """
        scheduler = EventScheduler()
        scheduler.start()
        try:
            # dispatch スレッドを「遠い予約で待機」させてから割り込ませる
            scheduler.schedule(
                fire_at=datetime.now() + timedelta(seconds=3600),
                callback=lambda: None, key="far",
            )
            time.sleep(0.05)  # 待機に入らせる

            fired = threading.Event()
            armed = scheduler.schedule_if_absent(
                fire_at=datetime.now() + timedelta(seconds=0.05),
                callback=fired.set, key="recovered",
            )
            self.assertTrue(armed)
            self.assertTrue(
                fired.wait(timeout=3.0),
                "schedule_if_absent が dispatch スレッドを起こしていない",
            )
        finally:
            scheduler.stop()


class ScheduleIfPresentTest(unittest.TestCase):
    """schedule_if_present / fire_time_of の契約 (v0.4 計画 段 3)。

    用途はティックの予約を「最後の標準呼び出しから T 分」へ押し直すこと。予約が
    無い (ライフ外) なら生やさない — 判定と置き換えを同じロック区間で行い、
    消された予約を check-then-act の隙間で生き返らせない。
    """

    def setUp(self) -> None:
        self.scheduler = EventScheduler()  # start() しない (run_due で同期実行)
        self.fired: list[str] = []

    def _at(self, seconds: float) -> datetime:
        return datetime.now() + timedelta(seconds=seconds)

    def test_does_nothing_when_absent(self) -> None:
        moved = self.scheduler.schedule_if_present(
            fire_at=self._at(-1), callback=lambda: self.fired.append("new"), key="k1",
        )
        self.assertFalse(moved)
        self.assertIsNone(self.scheduler.fire_time_of("k1"))
        self.assertEqual(self.scheduler.run_due(datetime.now()), 0)

    def test_does_nothing_after_cancel(self) -> None:
        self.scheduler.schedule(fire_at=self._at(60), callback=lambda: None, key="k1")
        self.scheduler.cancel("k1")
        self.assertFalse(self.scheduler.schedule_if_present(
            fire_at=self._at(-1), callback=lambda: self.fired.append("revived"), key="k1",
        ))
        self.assertEqual(self.scheduler.run_due(datetime.now()), 0)
        self.assertEqual(self.fired, [])

    def test_replaces_the_time_and_callback_of_a_live_reservation(self) -> None:
        self.scheduler.schedule(
            fire_at=self._at(-1), callback=lambda: self.fired.append("old"), key="k1",
        )
        target = self._at(600)
        self.assertTrue(self.scheduler.schedule_if_present(
            fire_at=target, callback=lambda: self.fired.append("new"), key="k1",
        ))
        self.assertEqual(self.scheduler.fire_time_of("k1"), datetime.fromtimestamp(target.timestamp()))
        # 元の期限 (過去) では発火しない
        self.assertEqual(self.scheduler.run_due(datetime.now()), 0)
        self.assertEqual(self.scheduler.run_due(target), 1)
        self.assertEqual(self.fired, ["new"])
        self.assertIsNone(self.scheduler.fire_time_of("k1"))


if __name__ == "__main__":
    unittest.main()
