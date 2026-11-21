"""バックエンドのイベントループの遅れを監視し、止まったら各スレッドの実行位置を記録する。

バックエンドは全部のリクエストの受け取りと返事の送り出しを、uvicorn の asyncio
イベントループ一本でさばいている。これが数秒止まると、画面には「通信が途中で
切れました」が出る。止まっている間に誰が止めているかを残すのがこのモジュールの役目
(docs/intent/backend_stall_handling.md の「仕組み 2」)。

動き方:

- デーモンスレッド一本 (``LoopStallWatch``) が約 ``WATCH_INTERVAL_SEC`` 秒ごとに
  ``loop.call_soon_threadsafe`` でループに ``_mark`` の呼び出しを予約し、予約してから
  実行されるまでの経過時間を遅れとして測る。前の予約が実行されるまで、次は予約しない。
- 遅れが ``STALL_THRESHOLD_SEC`` を超えたら、全スレッドの実行位置を
  ``timeout_diagnostics.log`` に一つの記録として書く。止まり続けている間は
  ``REPEAT_INTERVAL_SEC`` 秒おきに重ね、一回の停止につき ``MAX_REPORTS_PER_STALL``
  回で打ち切る。動き出したら止まっていた秒数を一行書く。
- 記録を書いた停止のあとは、イベントループのスレッドの上から、普通のロガーで
  WARNING を一行出す (backend.log / error.log に入る)。
- 監視スレッド自身が ``STALL_THRESHOLD_SEC`` を超えて動けなかったときも一行書く。
  プロセス全体が止まっていた場合 (PC のスリープ、メモリ不足など) は、実行待ちの
  予約が残っていないことが多く、上の測り方では遅れとして現れないため。

書かないもの: 変数の中身 (会話の内容がログに入らないように) と、ソースコードの行の
文面 (文面を取るにはファイルを読みにいく必要があり、止まっている最中だと記録が
何分もかかるため)。実行位置はフレームのコードオブジェクトから直接取り、
``linecache`` にも ``traceback`` の行の取得にも触らない。

監視スレッドは、root ロガーやコンソールへ流れる経路に一切書かない。
コンソールへの書き出しが停止の原因だったとき、監視スレッドまで一緒に止まるのを避けるため。

監視スレッドは本体に影響させない。監視スレッドの中の例外はすべてその中で受け止めて、監視スレッドだけを終える。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from typing import Optional

from saiverse.logging_config import get_timeout_diagnostics_logger

LOGGER = logging.getLogger(__name__)

# 何秒ごとにループへ _mark を予約するか。
WATCH_INTERVAL_SEC = 1.0
# 遅れがこの秒数を超えたら記録する。画面がストリーミングを切断と判定する 15 秒の手前。
STALL_THRESHOLD_SEC = 5.0
# 止まり続けている間、何秒おきに記録を重ねるか。
REPEAT_INTERVAL_SEC = 10.0
# 一回の停止につき記録を何回まで書くか。
MAX_REPORTS_PER_STALL = 6
# 一つのスレッドについて、内側から何フレームまで書くか。
MAX_FRAMES_PER_THREAD = 30

_THREAD_NAME = "loop-stall-watch"

_active_watch: Optional["LoopStallWatch"] = None
_active_watch_lock = threading.Lock()


class LoopStallWatch:
    """イベントループ一つを監視するデーモンスレッドと、その共有状態。

    ``start_loop_stall_watch`` から作る。``stop`` はテストの後始末用で、本番の
    ``main._lifespan`` では呼ばない (プロセスの終了とともにデーモンスレッドが消える)。
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        interval_sec: float,
        stall_threshold_sec: float,
        repeat_interval_sec: float,
        max_reports_per_stall: int,
        diagnostics_logger: logging.Logger,
    ) -> None:
        self._loop = loop
        self._interval_sec = interval_sec
        self._stall_threshold_sec = stall_threshold_sec
        self._repeat_interval_sec = repeat_interval_sec
        self._max_reports_per_stall = max_reports_per_stall
        self._diagnostics_logger = diagnostics_logger

        self._stop_event = threading.Event()
        # 監視スレッドと、ループの上で走る _mark が共有する状態。_state_lock で守る。
        self._state_lock = threading.Lock()
        self._requested_at: Optional[float] = None  # 実行待ちの _mark を予約した時刻 (monotonic)
        self._reports_this_stall = 0
        self._recovered_after_sec: Optional[float] = None  # 監視スレッドへ渡す「止まっていた秒数」
        self._loop_thread_id: Optional[int] = None

        self.thread = threading.Thread(
            target=self._run, name=_THREAD_NAME, daemon=True
        )

    # ------------------------------------------------------------------
    # 公開の操作
    # ------------------------------------------------------------------

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def stop(self, timeout: Optional[float] = None) -> None:
        """監視スレッドを止めて、その終わりを待つ (テストの後始末用)。"""
        global _active_watch
        self._stop_event.set()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout)
        with _active_watch_lock:
            if _active_watch is self:
                _active_watch = None

    # ------------------------------------------------------------------
    # イベントループのスレッドの上で走る処理
    # ------------------------------------------------------------------

    def _mark(self, requested_at: float) -> None:
        """監視スレッドがループに予約する処理。予約してからここまでの時間がループの遅れ。"""
        now = time.monotonic()
        with self._state_lock:
            if self._loop_thread_id is None:
                self._loop_thread_id = threading.get_ident()
            if self._requested_at != requested_at:
                return
            delay = now - requested_at
            reports = self._reports_this_stall
            self._requested_at = None
            self._reports_this_stall = 0
            if delay > self._stall_threshold_sec:
                self._recovered_after_sec = delay
        if reports > 0:
            # ここはイベントループのスレッドの上なので、普通のロガーへ書いてよい。
            # 知らせに失敗しても、ループの上へ例外を出さない。
            try:
                LOGGER.warning(
                    "イベントループが %.1f 秒止まっていた。各スレッドの実行位置は "
                    "timeout_diagnostics.log にある。",
                    delay,
                )
            except Exception:
                pass

    def _announce_watch_thread_delay(self, actual_sleep: float) -> None:
        """監視スレッドが長く動けなかったことを、ループのスレッドから普通のロガーへ知らせる。"""
        try:
            LOGGER.warning(
                "バックエンドのプロセスが %.1f 秒ほど動けなかった可能性がある。"
                "記録は timeout_diagnostics.log にある。",
                actual_sleep,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 監視スレッド
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._watch()
        except Exception:
            try:
                self._diagnostics_logger.error(
                    "LOOP_STALL_WATCH: 監視スレッドの中で例外が起きたので、監視を終える",
                    exc_info=True,
                )
            except Exception:
                pass

    def _watch(self) -> None:
        last_report_at: Optional[float] = None
        while not self._stop_event.is_set():
            if self._loop.is_closed():
                return

            with self._state_lock:
                need_request = self._requested_at is None
                if need_request:
                    requested_at = time.monotonic()
                    self._requested_at = requested_at
            if need_request:
                # 新しく予約するのは前の予約が実行されたあと = 前の停止は終わっている。
                last_report_at = None
                try:
                    self._loop.call_soon_threadsafe(self._mark, requested_at)
                except RuntimeError:
                    # ループが閉じた。監視スレッドの役目は終わり。
                    return

            sleep_started = time.monotonic()
            if self._stop_event.wait(self._interval_sec):
                return
            actual_sleep = time.monotonic() - sleep_started

            self._write_recovery_line_if_any()
            if actual_sleep - self._interval_sec > self._stall_threshold_sec:
                self._write_watch_thread_delay(actual_sleep)

            now = time.monotonic()
            with self._state_lock:
                requested_at = self._requested_at
                if requested_at is None:
                    continue
                lag = now - requested_at
                if not self._loop.is_running():
                    # 止まったループ (起動前・終了後) は遅れではない。
                    continue
                if lag <= self._stall_threshold_sec:
                    continue
                if self._reports_this_stall >= self._max_reports_per_stall:
                    continue
                if (
                    last_report_at is not None
                    and now - last_report_at < self._repeat_interval_sec
                ):
                    continue
                self._reports_this_stall += 1
                report_number = self._reports_this_stall
                loop_thread_id = self._loop_thread_id
            last_report_at = now
            self._write_stall_report(lag, report_number, actual_sleep, loop_thread_id)

    def _write_recovery_line_if_any(self) -> None:
        with self._state_lock:
            recovered_after = self._recovered_after_sec
            self._recovered_after_sec = None
        if recovered_after is not None:
            self._diagnostics_logger.warning(
                "LOOP_STALL: イベントループが動き出した。止まっていた秒数: %.1f",
                recovered_after,
            )

    def _write_watch_thread_delay(self, actual_sleep: float) -> None:
        """監視スレッド自身が長く動けなかったことを一行書く。

        イベントループと監視スレッドが同時に止まると (プロセス全体が止まる、PC が
        スリープする、別のスレッドが GIL を長く離さない)、実行待ちの ``_mark`` が残って
        いなければ ``lag`` には現れない。待機の予定の時間と実際にかかった時間の差が、
        その停止の唯一の跡になる。
        """
        self._diagnostics_logger.warning(
            "LOOP_STALL: 監視スレッドの %.2f 秒の待機に %.2f 秒かかった。"
            "この間、プロセス全体が止まっていた可能性がある "
            "(PC のスリープ、メモリ不足、別のスレッドが Python 全体を占有、など)",
            self._interval_sec,
            actual_sleep,
        )
        with self._state_lock:
            probe_pending = self._requested_at is not None
        if probe_pending:
            # 実行待ちの _mark が残っているなら、遅れとしての記録と、ループが動き出した
            # あとの知らせは、いつもの経路 (_write_stall_report と _mark) が出す。
            return
        try:
            self._loop.call_soon_threadsafe(self._announce_watch_thread_delay, actual_sleep)
        except RuntimeError:
            # ループが閉じた。知らせる相手がいない。
            pass

    def _write_stall_report(
        self,
        lag: float,
        report_number: int,
        actual_sleep: float,
        loop_thread_id: Optional[int],
    ) -> None:
        lines = [
            f"LOOP_STALL: イベントループの遅れ {lag:.1f} 秒 "
            f"(この停止の {report_number}/{self._max_reports_per_stall} 回目の記録)",
            f"監視スレッド: {self._interval_sec:.2f} 秒の待機に "
            f"{actual_sleep:.2f} 秒かかった (遅れ {actual_sleep - self._interval_sec:.2f} 秒)",
            "スレッドごとの実行位置 (外側から内側へ。内側の "
            f"{MAX_FRAMES_PER_THREAD} フレームまで):",
        ]
        lines.extend(
            format_thread_positions(
                loop_thread_id=loop_thread_id,
                skip_thread_id=threading.get_ident(),
            )
        )
        # 全文を組み立ててから、ロガー呼び出し一回で書く。
        self._diagnostics_logger.warning("\n".join(lines))


def _frame_positions(frame, limit: int) -> list[tuple[str, int, str]]:
    """フレームから内側 ``limit`` 個の (ファイルパス, 行番号, 関数名) を外側→内側の順で返す。

    コードオブジェクトの属性だけを読む。変数の中身にもソースの行の文面にも触らない。
    """
    positions: list[tuple[str, int, str]] = []
    current = frame
    while current is not None and len(positions) < limit:
        code = current.f_code
        positions.append((code.co_filename, current.f_lineno or 0, code.co_name))
        current = current.f_back
    positions.reverse()
    return positions


def format_thread_positions(
    *,
    loop_thread_id: Optional[int],
    skip_thread_id: Optional[int],
    max_frames: int = MAX_FRAMES_PER_THREAD,
) -> list[str]:
    """全スレッドの実行位置を、記録に載せる行の列にする。

    同じ実行位置のスレッドは一つにまとめ、スレッド名の一覧と本数で一回だけ書く。
    イベントループのスレッドは印を付けて先頭に出す。``skip_thread_id`` は省く
    (監視スレッド自身)。
    """
    names = {t.ident: t.name for t in threading.enumerate()}
    frames = sys._current_frames()

    loop_entry: Optional[tuple[str, tuple[tuple[str, int, str], ...]]] = None
    groups: dict[tuple[tuple[str, int, str], ...], list[str]] = {}
    for thread_id, frame in frames.items():
        if thread_id == skip_thread_id:
            continue
        name = names.get(thread_id, f"thread-{thread_id}")
        positions = tuple(_frame_positions(frame, max_frames))
        if thread_id == loop_thread_id:
            loop_entry = (name, positions)
            continue
        groups.setdefault(positions, []).append(name)
    del frames

    lines: list[str] = []

    def _append_positions(positions: tuple[tuple[str, int, str], ...]) -> None:
        if not positions:
            lines.append("    (フレームなし)")
        for filename, lineno, func in positions:
            lines.append(f"    {filename}:{lineno} {func}")

    if loop_entry is not None:
        lines.append(f"[イベントループのスレッド] {loop_entry[0]}")
        _append_positions(loop_entry[1])
    else:
        lines.append("[イベントループのスレッド] (見つからなかった)")

    for positions, thread_names in groups.items():
        thread_names.sort()
        lines.append(f"[{len(thread_names)} 本] {', '.join(thread_names)}")
        _append_positions(positions)
    return lines


def start_loop_stall_watch(
    loop: asyncio.AbstractEventLoop,
    *,
    interval_sec: float = WATCH_INTERVAL_SEC,
    stall_threshold_sec: float = STALL_THRESHOLD_SEC,
    repeat_interval_sec: float = REPEAT_INTERVAL_SEC,
    max_reports_per_stall: int = MAX_REPORTS_PER_STALL,
    diagnostics_logger: Optional[logging.Logger] = None,
) -> LoopStallWatch:
    """``loop`` を監視するデーモンスレッドを始めて、その ``LoopStallWatch`` を返す。

    プロセスの中で監視スレッドは一本だけ。動いている監視スレッドがあるときに呼ぶと、新しくは
    始めずに、動いている方を返す (引数は使われない)。

    ``diagnostics_logger`` を省くと ``get_timeout_diagnostics_logger()`` に書く。
    テストはここにメモリへ溜めるロガーを渡す。
    """
    global _active_watch
    with _active_watch_lock:
        if _active_watch is not None and _active_watch.is_alive():
            return _active_watch
        if diagnostics_logger is None:
            diagnostics_logger = get_timeout_diagnostics_logger()
        watch = LoopStallWatch(
            loop,
            interval_sec=interval_sec,
            stall_threshold_sec=stall_threshold_sec,
            repeat_interval_sec=repeat_interval_sec,
            max_reports_per_stall=max_reports_per_stall,
            diagnostics_logger=diagnostics_logger,
        )
        watch.thread.start()
        _active_watch = watch
        return watch
