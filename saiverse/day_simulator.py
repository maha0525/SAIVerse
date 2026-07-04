"""DaySimulator — 一日シミュレータの DES ドライバ骨格 (自律行動 v2 §12)。

v2 は離散イベントシステムであり、イベント間に何も起きないことを設計自体が
保証している。本ドライバは仮想クロック (``saiverse.clock``) を有効化し、
「EventScheduler のキューの次イベントへ時計をワープ → 期限到来分を同期実行
(派生した即時イベントも消化) → 次へ」を繰り返すことで、一日を実時間の
数分〜十数分に圧縮する。

前提 (単一スレッド):
- シム中は背景スケジューラを走らせない。EventScheduler の dispatch スレッド
  (``start()``) や SubLineScheduler 等の背景スレッドはシム実行中に起動しない
  こと。仮想モード中は EventScheduler 側でも dispatch スレッドの発火を抑止
  しているが (二重の防御)、実行経路は本ドライバの ``run()`` が呼ぶ
  ``run_due()`` のみであることが設計前提。
- callback は ``run()`` の呼び出しスレッド上で同期実行される。

シナリオプレイヤー・一日レポート等の上位層は後続フェーズで本ドライバの上に
載る (docs/intent/autonomous_behavior_v2.md §12)。

使い方::

    scheduler = EventScheduler()          # start() は呼ばない
    scheduler.schedule(fire_at=..., callback=..., key="wake_up")
    sim = DaySimulator(scheduler, start=day_9am, end=day_midnight)
    sim.run()
    # ... 終了後、仮想時刻は end。実時刻に戻すのは呼び出し側の責務:
    clock.disable_virtual()
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from saiverse import clock

if TYPE_CHECKING:
    from saiverse.event_scheduler import EventScheduler

LOGGER = logging.getLogger(__name__)


class DaySimulator:
    """仮想クロックで EventScheduler のキューを早回しする DES ドライバ。"""

    def __init__(
        self,
        scheduler: "EventScheduler",
        start: datetime,
        end: datetime,
    ) -> None:
        """
        Args:
            scheduler: 駆動対象の EventScheduler。``start()`` していないこと
                (シム中は背景スレッドを走らせない前提)。
            start: シミュレーション開始の仮想時刻 (naive datetime)。
            end: シミュレーション終了の仮想時刻。``start`` より過去は ValueError。
        """
        if end < start:
            raise ValueError(
                f"end must not be before start: start={start.isoformat()}, end={end.isoformat()}"
            )
        self.scheduler = scheduler
        self.start = start
        self.end = end

    def run(self) -> int:
        """シミュレーションを実行し、実行したイベント数の合計を返す。

        clock を仮想モード (``start``) にし、キューの次イベント時刻へ
        ``advance_to`` → ``run_due`` を繰り返す。キューが空になるか、
        次イベントが ``end`` より後になったら終了し、``end`` まで advance する。

        終了後も仮想モードは維持される (連続シナリオ・レポート生成で仮想時刻を
        参照できるようにするため)。実時刻に戻すのは呼び出し側の責務
        (``clock.disable_virtual()``)。
        """
        clock.enable_virtual(self.start)
        LOGGER.info(
            "[day_simulator] run start: %s -> %s (pending=%d)",
            self.start.isoformat(timespec="seconds"),
            self.end.isoformat(timespec="seconds"),
            self.scheduler.pending_count(),
        )

        total_executed = 0
        step = 0
        while True:
            next_at = self.scheduler.next_fire_time()
            if next_at is None:
                LOGGER.info("[day_simulator] queue empty at %s",
                            clock.now().isoformat(timespec="seconds"))
                break
            if next_at > self.end:
                LOGGER.info(
                    "[day_simulator] next event %s is beyond end %s; stopping",
                    next_at.isoformat(timespec="seconds"),
                    self.end.isoformat(timespec="seconds"),
                )
                break

            # start 以前が期限のイベント (開始時点で既に期限切れ) は start 時刻で
            # 実行する (時計は後退させない)。
            if next_at > clock.now():
                clock.advance_to(next_at)

            step += 1
            executed = self.scheduler.run_due(clock.now())
            total_executed += executed
            LOGGER.info(
                "[day_simulator] step %d: t=%s executed=%d pending=%d",
                step,
                clock.now().isoformat(timespec="seconds"),
                executed,
                self.scheduler.pending_count(),
            )

        # 終了時に end まで時計を進める (最終イベント後の残り時間を消化)
        if self.end > clock.now():
            clock.advance_to(self.end)

        LOGGER.info(
            "[day_simulator] run finished at %s: steps=%d, events=%d, remaining=%d",
            clock.now().isoformat(timespec="seconds"),
            step,
            total_executed,
            self.scheduler.pending_count(),
        )
        return total_executed
