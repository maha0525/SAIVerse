"""EventScheduler — 全イベントの時刻指定ディスパッチャ (Phase 4-e)。

Intent A v0.10 / Intent B v0.7 の「メタレイヤーの定期実行入口」に対する
最終形の実装。固定間隔ポーリングを全廃し、min-heap + ``Condition.wait_until``
で **次に発火すべきイベントの時刻まで sleep する push 駆動** を実現する。

集約対象 (Phase 4-e で統合):
- メタ判断 Pulse の TTL 接近前倒し (anchor touch から push)
- メタ判断 Pulse の interval 経過 (judgment 完了から push)
- ACTIVITY_STATE=Active 化時の即時発火
- ペルソナスケジュール (PersonaSchedule の発火時刻)
- 内部 alert (Track 状態の遅延通知)
- inter-city DB poll (VisitingAI / ThinkingRequest)
- SDS heartbeat

addon 由来のポーリング (X 監視等) は本 Phase ではスコープ外。
詳細: ``docs/issues/addon_event_scheduler_integration.md``。

責務外:
- リトライポリシー (callback 側のドメイン責務)
- callback の例外による再試行 (WARN ログ + 該当予約消去のみ)
- Alert 観察ルート (TrackManager._notify_alert の同期 callback で別途処理)
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

LOGGER = logging.getLogger(__name__)


@dataclass(order=True)
class _Entry:
    """min-heap に積むエントリ。

    fire_at_ts (epoch float) で大小比較する。同時刻は seq で安定化。
    callback と key は比較対象から除外 (dataclass の order=True 用に compare=False)。
    """
    fire_at_ts: float
    seq: int
    key: str = field(compare=False)
    callback: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class EventScheduler:
    """汎用時刻ディスパッチャ。

    使い方:
        scheduler = EventScheduler()
        scheduler.start()
        scheduler.schedule(
            fire_at=datetime.now() + timedelta(seconds=30),
            callback=lambda: print("fired"),
            key="my_event",
        )
        # ... 30 秒後に "fired" が表示される
        scheduler.stop()

    同じ key で再 schedule すると古い予約は cancel される (上書き)。
    callback は EventScheduler の dispatch スレッドで同期実行される。
    重い処理は callback 内で別 thread / queue に投げること。
    """

    def __init__(self) -> None:
        self._heap: list[_Entry] = []
        # key → 最新 _Entry の参照 (古い同 key エントリを cancel するため)。
        self._entries_by_key: Dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # heap 内エントリの順序を安定化させるための単調増加カウンタ
        self._seq_counter = itertools.count()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """dispatch スレッドを起動する。既に起動済みなら何もしない。"""
        if self._thread and self._thread.is_alive():
            LOGGER.warning("[event_scheduler] already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="EventScheduler",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("[event_scheduler] started")

    def stop(self) -> None:
        """dispatch スレッドを停止する。pending callback は破棄される。"""
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        LOGGER.info("[event_scheduler] stopped")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def schedule(
        self,
        fire_at: datetime,
        callback: Callable[[], None],
        key: str,
    ) -> None:
        """指定時刻に callback を発火する予約を入れる。

        Args:
            fire_at: 発火時刻 (naive datetime, ローカルタイム想定)。
                過去時刻を渡すと即座に発火対象になる。
            callback: 発火時に呼ばれる関数。引数なし、戻り値無視。
                例外を投げてもよい (WARN ログ + 該当予約消去で吸収)。
            key: 予約識別子。同じ key で再 schedule すると古い予約は cancel される。
                慣習: ``"<source>:<id>"`` (例 ``"ttl:air_city_a"``)。
        """
        fire_ts = fire_at.timestamp()
        with self._cond:
            # 既存エントリがあれば cancel フラグを立てる (heap からの物理削除はせず、
            # dispatch ループで cancelled エントリをスキップする lazy deletion 方式)。
            old = self._entries_by_key.get(key)
            if old is not None:
                old.cancelled = True

            entry = _Entry(
                fire_at_ts=fire_ts,
                seq=next(self._seq_counter),
                key=key,
                callback=callback,
            )
            self._entries_by_key[key] = entry
            heapq.heappush(self._heap, entry)

            LOGGER.debug(
                "[event_scheduler] schedule key=%s fire_at=%s (in %.1fs)",
                key, fire_at.isoformat(timespec="seconds"),
                fire_ts - time.time(),
            )

            # heap の先頭が変わった可能性があるので dispatch ループを起こす。
            self._cond.notify()

    def cancel(self, key: str) -> bool:
        """予約をキャンセルする。

        Returns:
            該当する key の予約があってキャンセルした場合 True、なかった場合 False。
        """
        with self._cond:
            entry = self._entries_by_key.pop(key, None)
            if entry is None:
                return False
            entry.cancelled = True
            LOGGER.debug("[event_scheduler] cancel key=%s", key)
            # cancel 後、heap 先頭が長く待たされていれば、不要な wake は不要。
            # ただし heap 先頭が cancelled になった場合、dispatch ループは
            # 起きてからスキップして次のエントリを見るだけなので問題なし。
            return True

    def schedule_periodic(
        self,
        interval_seconds: float,
        callback: Callable[[], None],
        key: str,
        *,
        first_fire_immediate: bool = False,
    ) -> None:
        """周期的に callback を呼ぶラッパー。

        callback 完了後、自動的に次回を ``interval_seconds`` 後に再 schedule する。
        callback が例外を投げた場合は **周期処理を停止する** (再 schedule しない)。
        無限ログ汚染防止のため。再開したければ呼び出し側で再度 schedule_periodic を呼ぶ。

        Args:
            interval_seconds: 呼び出し間隔 (秒)。callback の実行時間は含まない。
            callback: 周期的に呼ぶ関数。
            key: 予約識別子。同じ key で上書き登録される。
            first_fire_immediate: True なら最初の発火を即時行う (interval_seconds を待たない)。
        """
        def wrapper() -> None:
            try:
                callback()
            except Exception:
                LOGGER.exception(
                    "[event_scheduler] periodic callback raised; "
                    "stopping periodic schedule for key=%s", key,
                )
                return
            # 成功時のみ次回を schedule
            self.schedule(
                fire_at=datetime.now() + timedelta(seconds=interval_seconds),
                callback=wrapper,
                key=key,
            )

        first_fire_at = (
            datetime.now()
            if first_fire_immediate
            else datetime.now() + timedelta(seconds=interval_seconds)
        )
        self.schedule(fire_at=first_fire_at, callback=wrapper, key=key)

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    def _dispatch_loop(self) -> None:
        """heap 先頭の fire_at まで wait し、時刻到来で callback を発火するメインループ。"""
        LOGGER.info("[event_scheduler] dispatch loop started")
        while not self._stop_event.is_set():
            with self._cond:
                # キャンセル済みのエントリを heap 先頭からスキップ
                while self._heap and self._heap[0].cancelled:
                    heapq.heappop(self._heap)

                if not self._heap:
                    # 何も予約がない: 新規予約か stop シグナルまで待つ
                    self._cond.wait()
                    continue

                next_entry = self._heap[0]
                now_ts = time.time()
                wait_seconds = next_entry.fire_at_ts - now_ts

                if wait_seconds > 0:
                    # まだ時刻未到来。新規予約 (より早い fire_at) で起こされるか
                    # stop シグナルまで wait。
                    self._cond.wait(timeout=wait_seconds)
                    continue

                # 時刻到来: heap から取り出して発火準備
                heapq.heappop(self._heap)
                # _entries_by_key から自分自身を削除 (cancel/再 schedule の race 回避)
                if self._entries_by_key.get(next_entry.key) is next_entry:
                    self._entries_by_key.pop(next_entry.key, None)

            # Lock を抜けた状態で callback を実行 (callback 内で再 schedule 可能にする)
            self._fire(next_entry)

        LOGGER.info("[event_scheduler] dispatch loop ended")

    def _fire(self, entry: _Entry) -> None:
        """callback を呼び、例外を WARN ログで吸収する。"""
        if entry.cancelled:
            # 取り出した直後にキャンセルされたケース。callback は呼ばない。
            return
        try:
            entry.callback()
        except Exception:
            LOGGER.exception(
                "[event_scheduler] callback raised; dropping key=%s",
                entry.key,
            )

    # ------------------------------------------------------------------
    # Introspection (debug / tests)
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        """現在の有効な予約数 (cancelled でないもの)。"""
        with self._cond:
            return sum(1 for e in self._heap if not e.cancelled)

    def has_key(self, key: str) -> bool:
        """指定 key の有効な予約があるか。"""
        with self._cond:
            entry = self._entries_by_key.get(key)
            return entry is not None and not entry.cancelled
