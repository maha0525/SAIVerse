"""仮想クロック — プロセス全体で使う時刻の一元供給源 (自律行動 v2 §12)。

v2 の新コードと、v2 経路が通る時刻刻印点 (記憶書き込み・イベント予約・
ログの時刻等) は ``datetime.now()`` を直接呼ばず、本モジュールの ``now()``
を読むこと。実時刻での刻印は一日シミュレータ実行中に「ペルソナの今日」を
壊す (朝の出来事に深夜のタイムスタンプが付く) ため、時刻刻印点の clock
経由化は不変条件に準ずる (docs/intent/autonomous_behavior_v2.md §12)。

動作モード:
- **実モード (既定)**: ``now()`` は ``datetime.now()`` をそのまま返す。
  本番経路の挙動は仮想クロック導入前と完全に同一。
- **仮想モード**: ``enable_virtual(start)`` で開始。``now()`` は固定の
  仮想時刻を返し、``advance_to(dt)`` で前進のみ許す (後退は ValueError)。
  DES ドライバ (``saiverse.day_simulator``) がこの前進を駆動する。

時刻は naive datetime (ローカルタイム想定) で扱う。既存 EventScheduler の
``schedule(fire_at)`` と同じ慣習。

スレッドセーフ: モジュールレベルの単一状態を ``threading.Lock`` で保護する。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

LOGGER = logging.getLogger(__name__)

_lock = threading.Lock()
_virtual_now: Optional[datetime] = None


def now() -> datetime:
    """現在時刻を返す。実モードでは ``datetime.now()``、仮想モードでは仮想時刻。"""
    with _lock:
        if _virtual_now is not None:
            return _virtual_now
    return datetime.now()


def is_virtual() -> bool:
    """仮想モード中なら True。"""
    with _lock:
        return _virtual_now is not None


def enable_virtual(start: datetime) -> None:
    """仮想モードを開始し、仮想時刻を ``start`` に設定する。

    既に仮想モードの場合は仮想時刻を ``start`` にリセットする
    (連続シナリオで日を跨いで再初期化するケースを許容)。
    """
    global _virtual_now
    with _lock:
        if _virtual_now is not None:
            LOGGER.info(
                "[clock] virtual mode re-enabled: %s -> %s",
                _virtual_now.isoformat(timespec="seconds"),
                start.isoformat(timespec="seconds"),
            )
        else:
            LOGGER.info(
                "[clock] virtual mode enabled at %s",
                start.isoformat(timespec="seconds"),
            )
        _virtual_now = start


def advance_to(dt: datetime) -> None:
    """仮想時刻を ``dt`` へ前進させる。

    Raises:
        RuntimeError: 仮想モードでないとき (実時刻は前進操作の対象外)。
        ValueError: ``dt`` が現在の仮想時刻より過去のとき (時間は戻せない)。
    """
    global _virtual_now
    with _lock:
        if _virtual_now is None:
            raise RuntimeError("clock.advance_to() requires virtual mode (call enable_virtual first)")
        if dt < _virtual_now:
            raise ValueError(
                f"cannot move virtual clock backwards: {_virtual_now.isoformat()} -> {dt.isoformat()}"
            )
        LOGGER.debug(
            "[clock] advance %s -> %s",
            _virtual_now.isoformat(timespec="seconds"),
            dt.isoformat(timespec="seconds"),
        )
        _virtual_now = dt


def disable_virtual() -> None:
    """仮想モードを終了し、実時刻 (``datetime.now()``) に戻す。非仮想時は no-op。"""
    global _virtual_now
    with _lock:
        if _virtual_now is not None:
            LOGGER.info(
                "[clock] virtual mode disabled (was %s)",
                _virtual_now.isoformat(timespec="seconds"),
            )
        _virtual_now = None
