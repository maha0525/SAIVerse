"""saiverse.loop_stall_watch の検査。

閾値を小さくして、テスト内で作る asyncio のループを別スレッドで回し、
ループの上で ``time.sleep`` して止める。記録はメモリに溜めるロガーへ書かせ、
本物のセッションのログのフォルダには書かない。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid

import pytest

from saiverse import loop_stall_watch
from saiverse.loop_stall_watch import start_loop_stall_watch

FAST = dict(
    interval_sec=0.05,
    stall_threshold_sec=0.3,
    repeat_interval_sec=0.2,
)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def diag():
    logger = logging.getLogger(f"test.loop_stall_watch.{uuid.uuid4().hex}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    yield logger, handler
    logger.removeHandler(handler)


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="test-event-loop", daemon=True)
    thread.start()
    started = []
    yield loop, thread, started
    for watch in started:
        watch.stop(timeout=5)
    if not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
    assert not thread.is_alive()


def _start(loop, logger, started, **overrides):
    kwargs = dict(FAST)
    kwargs.update(overrides)
    watch = start_loop_stall_watch(loop, diagnostics_logger=logger, **kwargs)
    started.append(watch)
    return watch


def _run_on_loop(loop, func, *args) -> None:
    """``func`` をループの上で走らせ、終わるまで待つ。"""
    done = threading.Event()

    def call():
        try:
            func(*args)
        finally:
            done.set()

    loop.call_soon_threadsafe(call)
    assert done.wait(30)


def _wait_until(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _stall_reports(handler: _ListHandler) -> list[str]:
    return [m for m in handler.messages if m.startswith("LOOP_STALL: イベントループの遅れ")]


def _recovery_lines(handler: _ListHandler) -> list[str]:
    return [m for m in handler.messages if m.startswith("LOOP_STALL: イベントループが動き出した")]


def block_event_loop_for_stall_test(seconds: float) -> None:
    # MARKER_SOURCE_COMMENT_7f3a は記録に出てはいけない
    marker_local_value = "MARKER_LOCAL_VALUE_9c1e"
    time.sleep(seconds)
    assert marker_local_value


def test_stall_is_recorded_with_loop_thread_position(diag, running_loop):
    logger, handler = diag
    loop, _thread, started = running_loop
    _start(loop, logger, started)
    _run_on_loop(loop, block_event_loop_for_stall_test, 0.8)
    assert _wait_until(lambda: _recovery_lines(handler))

    reports = _stall_reports(handler)
    assert reports, handler.messages
    first = reports[0]
    assert "1/6 回目" in first
    assert "監視スレッド:" in first
    loop_section = first.split("[イベントループのスレッド] ", 1)[1]
    assert loop_section.startswith("test-event-loop")
    loop_block = loop_section.split("\n[", 1)[0]
    assert "block_event_loop_for_stall_test" in loop_block
    assert "test_loop_stall_watch.py:" in loop_block
    # 監視スレッド自身は書かない
    assert "loop-stall-watch" not in first


def test_record_has_no_locals_or_source_lines(diag, running_loop):
    logger, handler = diag
    loop, _thread, started = running_loop
    _start(loop, logger, started)
    _run_on_loop(loop, block_event_loop_for_stall_test, 0.8)
    assert _wait_until(lambda: _recovery_lines(handler))
    assert _stall_reports(handler)
    text = "\n".join(handler.messages)
    assert "MARKER_LOCAL_VALUE_9c1e" not in text
    assert "MARKER_SOURCE_COMMENT_7f3a" not in text
    assert "time.sleep(seconds)" not in text


def test_no_record_without_stall(diag, running_loop):
    logger, handler = diag
    loop, _thread, started = running_loop
    # 閾値は本番と同じ 5 秒にする。フルスイートの並列実行で CPU が混んでいると、
    # 何もしていないループでも 0.3 秒程度は遅れることがあり、縮めた閾値では
    # 止めていないのに記録が出てしまう。
    _start(loop, logger, started, stall_threshold_sec=5.0)
    time.sleep(1.0)
    assert handler.messages == []


def _block_loop_until(loop, release: threading.Event, max_seconds: float = 30.0) -> threading.Event:
    """ループの上で ``release`` が立つまで待たせる。待ち終わったら立つ Event を返す。

    止める長さを秒数で決めると、CPU が混んでいるときに監視スレッドが期待した回数だけ
    目を覚ませず、記録の数がぶれる。記録が必要な数だけ出たのを見てから放す。
    """
    finished = threading.Event()

    def call():
        try:
            release.wait(max_seconds)
        finally:
            finished.set()

    loop.call_soon_threadsafe(call)
    return finished


def test_reports_are_capped_and_recovery_is_announced(diag, running_loop, caplog):
    logger, handler = diag
    loop, _thread, started = running_loop
    caplog.set_level(logging.WARNING, logger=loop_stall_watch.__name__)
    _start(loop, logger, started, max_reports_per_stall=3)

    release = threading.Event()
    blocked_at = time.monotonic()
    finished = _block_loop_until(loop, release)
    try:
        assert _wait_until(lambda: len(_stall_reports(handler)) >= 3, timeout=20)
        # 上限に達したあと、repeat_interval_sec の数倍待っても記録は増えない
        time.sleep(0.8)
        assert len(_stall_reports(handler)) == 3
    finally:
        release.set()
    assert finished.wait(10)
    blocked_for = time.monotonic() - blocked_at
    assert _wait_until(lambda: _recovery_lines(handler))

    reports = _stall_reports(handler)
    assert len(reports) == 3
    assert "3/3 回目" in reports[-1]
    recovery = _recovery_lines(handler)
    assert len(recovery) == 1
    seconds = float(recovery[0].rsplit(" ", 1)[1])
    assert 0.9 <= seconds <= blocked_for + 1.0

    warnings = [
        r for r in caplog.records
        if r.name == loop_stall_watch.__name__ and r.levelno == logging.WARNING
        and "止まっていた" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "timeout_diagnostics.log" in warnings[0].getMessage()
    # 案内の WARNING はイベントループのスレッドの上から出る
    assert warnings[0].threadName == "test-event-loop"

    # 次の停止では、また 1 回目から数える
    release = threading.Event()
    finished = _block_loop_until(loop, release)
    try:
        assert _wait_until(lambda: len(_stall_reports(handler)) >= 4, timeout=20)
    finally:
        release.set()
    assert finished.wait(10)
    assert "1/3 回目" in _stall_reports(handler)[3]


class _SlowOnceEvent(threading.Event):
    """``wait`` が一度だけ余分に時間をかける Event。監視スレッドが動けなかった形を作る。"""

    def __init__(self, extra_sec: float) -> None:
        super().__init__()
        self._extra_sec = extra_sec
        self._slowed = False

    def wait(self, timeout=None):
        result = super().wait(timeout)
        if not self._slowed and not result:
            self._slowed = True
            time.sleep(self._extra_sec)
        return result


def test_watch_thread_delay_is_recorded_without_pending_request(diag, running_loop, caplog):
    """ループも監視スレッドも同時に止まった形 (プロセス全体の停止) でも跡が残る。"""
    logger, handler = diag
    loop, _thread, started = running_loop
    caplog.set_level(logging.WARNING, logger=loop_stall_watch.__name__)
    watch = _start(loop, logger, started)
    # ループは止めない。監視スレッドの待ちだけを、閾値 (0.3 秒) より長く引き延ばす。
    watch._stop_event = _SlowOnceEvent(extra_sec=0.8)

    def delay_lines():
        return [m for m in handler.messages if m.startswith("LOOP_STALL: 監視スレッドの")]

    assert _wait_until(lambda: delay_lines())
    assert len(delay_lines()) == 1
    assert "プロセス全体が止まっていた可能性" in delay_lines()[0]

    def announcements():
        return [
            r for r in caplog.records
            if r.name == loop_stall_watch.__name__ and "動けなかった" in r.getMessage()
        ]

    assert _wait_until(lambda: announcements())
    assert announcements()[0].threadName == "test-event-loop"
    assert "timeout_diagnostics.log" in announcements()[0].getMessage()


def _idle_worker(event: threading.Event) -> None:
    event.wait(30)


def test_threads_at_same_position_are_grouped(diag, running_loop):
    logger, handler = diag
    loop, _thread, started = running_loop
    release = threading.Event()
    workers = [
        threading.Thread(target=_idle_worker, args=(release,), name=f"idle-worker-{i}", daemon=True)
        for i in range(3)
    ]
    for worker in workers:
        worker.start()
    try:
        _start(loop, logger, started)
        _run_on_loop(loop, block_event_loop_for_stall_test, 0.6)
        assert _wait_until(lambda: _recovery_lines(handler))
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)

    first = _stall_reports(handler)[0]
    assert "[3 本] idle-worker-0, idle-worker-1, idle-worker-2" in first
    assert first.count("_idle_worker") == 1


def test_watch_ends_quietly_after_loop_closes(diag, running_loop):
    logger, handler = diag
    loop, thread, started = running_loop
    watch = _start(loop, logger, started)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
    watch.thread.join(timeout=5)
    assert not watch.is_alive()
    assert not any("例外" in m for m in handler.messages)


def test_starting_twice_keeps_one_watch(diag, running_loop):
    logger, _handler = diag
    loop, _thread, started = running_loop
    first = _start(loop, logger, started)
    second = _start(loop, logger, started)
    assert first is second
    watch_threads = [t for t in threading.enumerate() if t.name == "loop-stall-watch"]
    assert len(watch_threads) == 1
