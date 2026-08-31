"""Autonomous behavior manager for individual personas (Phase 4-e: push 駆動)。

Phase 4-e で **EventScheduler 駆動の薄いラッパー** に再構成された。

- 旧実装 (Phase C-2): per-persona の sleep ループ thread が ``interval_minutes``
  ごとに ``MetaLayer.on_periodic_tick`` を発火していた
- Phase 4-e: EventScheduler に「次回発火時刻」を push する。fire 時に callback で
  tick を呼び、完了後に次回を再 push する
- 自律行動 v2 (2026-07): tick の中身が **watchdog に縮退** した (intent §4.2)。
  定期メタ判断 (状況分類ディスパッチ) は判断点 5 種が後継となり、tick は
  ``PulseDispatcher.dispatch_autonomy_tick`` →
  ``saiverse.autonomy_wiring.watchdog_tick`` — 「Active・起床時間帯・今日の
  day_plan が無い or コマ予約が途絶」のときだけ火を入れ直す保守的な見張りで、
  正常時は何もしない (LLM も呼ばない)

API 互換性 (``start`` / ``stop`` / ``set_interval`` / ``get_status`` / ``set_models``)
は維持する。``/people/{id}/autonomy`` API ルートと既存テストはそのまま動く。

将来的に ``/people/{id}/autonomy`` API そのものを ``META_JUDGMENT_CONFIG`` 経由
に統合する想定だが、Phase 4-e では UI 互換性維持のためインターフェースは据え置き。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager
    from persona.core import PersonaCore  # noqa: F401

LOGGER = logging.getLogger(__name__)

# Default interval between cycles (minutes). Override via env
# ``SAIVERSE_META_LAYER_INTERVAL_SECONDS`` (intent A v0.13 §"Pulse サイクルの 7 つの制御点" #3).
DEFAULT_INTERVAL_MINUTES = 50  # 3000 秒 = Anthropic prompt cache TTL ベース


class AutonomyState(str, Enum):
    """Current state of the autonomy manager."""
    STOPPED = "stopped"
    RUNNING = "running"
    DECIDING = "deciding"     # tick 実行中 (互換維持)
    EXECUTING = "executing"   # 互換維持 (現状未使用)
    WAITING = "waiting"       # Interval wait between ticks
    INTERRUPTED = "interrupted"  # 互換維持 (現状未使用)


@dataclass
class CycleReport:
    """Report from a completed periodic tick."""
    cycle_id: str
    started_at: float = 0.0
    completed_at: float = 0.0
    status: str = "pending"  # pending, completed, error
    error: Optional[str] = None


def _autonomy_key(persona_id: str) -> str:
    """EventScheduler に渡す key (persona_id 単位で一意)。"""
    return f"autonomy:{persona_id}"


class AutonomyManager:
    """ペルソナの自律稼働 watchdog タイマー (EventScheduler 駆動)。

    自律行動 v2 で tick の中身は watchdog (autonomy_wiring.watchdog_tick) に
    縮退した — 正常時は何もしない見張り。start/stop は従来どおり
    AUTONOMY_ENABLED 同期 (ensure_autonomy_for) が握る。
    """

    def __init__(
        self,
        persona_id: str,
        manager: "SAIVerseManager",
        *,
        interval_minutes: Optional[float] = None,
        decision_model: Optional[str] = None,
        execution_model: Optional[str] = None,
    ):
        self.persona_id = persona_id
        self.manager = manager
        # 優先順: 引数 > META_JUDGMENT_CONFIG.periodic_interval_minutes (DB 永続値) >
        # env (SAIVERSE_META_LAYER_INTERVAL_SECONDS) > module default
        if interval_minutes is None:
            interval_minutes = self._load_interval_from_judgment_config()
        if interval_minutes is None:
            env_seconds = os.environ.get("SAIVERSE_META_LAYER_INTERVAL_SECONDS")
            if env_seconds:
                try:
                    interval_minutes = max(0.5, float(env_seconds) / 60.0)
                except ValueError:
                    LOGGER.warning(
                        "Invalid SAIVERSE_META_LAYER_INTERVAL_SECONDS=%r; using default",
                        env_seconds,
                    )
                    interval_minutes = DEFAULT_INTERVAL_MINUTES
            else:
                interval_minutes = DEFAULT_INTERVAL_MINUTES
        self.interval_minutes = interval_minutes
        # 互換のため残す (Phase C-2 移行で no-op)
        self.decision_model = decision_model
        self.execution_model = execution_model

        self._state = AutonomyState.STOPPED
        self._current_cycle_id: Optional[str] = None
        self._last_report: Optional[CycleReport] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> AutonomyState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state != AutonomyState.STOPPED

    @property
    def last_report(self) -> Optional[CycleReport]:
        return self._last_report

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start the periodic tick. Returns True if started, False if already running.

        旧実装と同じく **start 直後に最初の tick が即時走る** — v2 では
        watchdog の即時チェックになる (Active 化した時点で今日の day_plan が
        無ければ、起床時間帯なら day_open がその場で火入れされる)。
        次回以降は ``interval_minutes`` 待機。
        """
        with self._lock:
            if self._state != AutonomyState.STOPPED:
                LOGGER.warning(
                    "[Autonomy:%s] Cannot start: already in state %s",
                    self.persona_id, self._state,
                )
                return False
            self._state = AutonomyState.RUNNING

        # 即時 fire (fire_at=now で EventScheduler に積む)
        self._schedule_tick_at(datetime.now())
        LOGGER.info(
            "[Autonomy:%s] Started (interval=%.1f min, push-driven)",
            self.persona_id, self.interval_minutes,
        )
        return True

    def stop(self) -> bool:
        """Stop the periodic tick. Returns True if stopped, False if not running."""
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return False
            self._state = AutonomyState.STOPPED

        self._cancel_pending_tick()
        LOGGER.info("[Autonomy:%s] Stopped", self.persona_id)
        return True

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_interval(self, minutes: float) -> None:
        """Update the loop interval (effective immediately if running).

        Re-schedule policy:
        - ``WAITING`` / ``RUNNING``: 既存予約を新 interval で上書きする (即時反映)
        - ``DECIDING`` (tick 実行中): 再 schedule しない。tick 完了時の
          ``_schedule_next_tick`` が ``self.interval_minutes`` の最新値を読むため、
          新 interval は次回 tick で自動的に反映される
        - ``STOPPED``: 再 schedule しない (start で初めて push される)
        """
        new_interval = max(0.5, minutes)
        with self._lock:
            if abs(self.interval_minutes - new_interval) < 1e-9:
                return
            self.interval_minutes = new_interval
            # WAITING (次回待ち) と RUNNING (start 直後の一瞬) はどちらも
            # 既存予約を上書きしたい。DECIDING (tick 中) は tick 完了時の
            # _schedule_next_tick が新 interval を読むので何もしない。
            should_reschedule = self._state in (
                AutonomyState.RUNNING,
                AutonomyState.WAITING,
            )

        LOGGER.info(
            "[Autonomy:%s] Interval set to %.1f min (reschedule=%s)",
            self.persona_id, new_interval, should_reschedule,
        )
        if should_reschedule:
            self._schedule_next_tick()

    def defer_next_tick(self) -> None:
        """Push the next periodic tick to ``now + interval``.

        Called after an out-of-band judgment (e.g. wait_response timeout) to
        avoid a duplicate fire shortly after the manual trigger. No-op when
        STOPPED (autonomy not active) or DECIDING (tick currently running —
        its completion will reschedule freshly via ``_schedule_next_tick``).
        """
        with self._lock:
            should_reschedule = self._state in (
                AutonomyState.RUNNING,
                AutonomyState.WAITING,
            )
        if not should_reschedule:
            return
        self._schedule_next_tick()
        LOGGER.debug(
            "[Autonomy:%s] Next tick deferred to now+%.1fmin (out-of-band trigger)",
            self.persona_id, self.interval_minutes,
        )

    def set_models(
        self,
        decision_model: Optional[str] = None,
        execution_model: Optional[str] = None,
    ) -> None:
        """API 互換: 旧 Decision/Execution モデル指定 (Phase C-2 移行で no-op)."""
        if decision_model is not None:
            self.decision_model = decision_model
        if execution_model is not None:
            self.execution_model = execution_model

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current status as a dict (for API responses)."""
        return {
            "persona_id": self.persona_id,
            "state": self._state.value,
            "interval_minutes": self.interval_minutes,
            "decision_model": self.decision_model,
            "execution_model": self.execution_model,
            "current_cycle_id": self._current_cycle_id,
            "last_report": {
                "cycle_id": self._last_report.cycle_id,
                "status": self._last_report.status,
                "error": self._last_report.error,
            } if self._last_report else None,
        }

    def get_next_tick_eta_seconds(self) -> Optional[int]:
        """次のメタ判断 tick までの残り秒数を返す。予約がなければ None。"""
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            return None
        key = _autonomy_key(self.persona_id)
        entry = scheduler._entries_by_key.get(key)
        if entry is None or entry.cancelled:
            return None
        remaining = entry.fire_at_ts - time.time()
        return max(0, int(remaining))

    # ------------------------------------------------------------------
    # EventScheduler 連携
    # ------------------------------------------------------------------

    def _schedule_next_tick(self) -> None:
        """Push the next tick (interval_minutes 後) to the EventScheduler."""
        fire_at = datetime.now() + timedelta(seconds=self.interval_minutes * 60)
        self._schedule_tick_at(fire_at)

    def _schedule_tick_at(self, fire_at: datetime) -> None:
        """Push a tick at the given time to the EventScheduler (used by start + next)."""
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            LOGGER.warning(
                "[Autonomy:%s] event_scheduler not available; cannot schedule tick",
                self.persona_id,
            )
            return
        scheduler.schedule(
            fire_at=fire_at,
            callback=self._handle_tick,
            key=_autonomy_key(self.persona_id),
        )
        LOGGER.debug(
            "[Autonomy:%s] Scheduled tick at %s",
            self.persona_id, fire_at.isoformat(timespec="seconds"),
        )

    def _cancel_pending_tick(self) -> None:
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            return
        scheduler.cancel(_autonomy_key(self.persona_id))

    def _handle_tick(self) -> None:
        """Fire callback: meta-layer の periodic tick を発火 + 次回再 push。"""
        # stop されていたら何もしない
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return
            self._state = AutonomyState.DECIDING

        cycle_id = str(uuid.uuid4())[:8]
        self._current_cycle_id = cycle_id
        report = CycleReport(cycle_id=cycle_id, started_at=time.time())

        try:
            # pulse_dispatch.md §7: PulseDispatcher 経由で起動
            dispatcher = getattr(self.manager, "pulse_dispatcher", None)
            if dispatcher is None:
                LOGGER.warning(
                    "[Autonomy:%s] No pulse_dispatcher on manager; skipping tick %s",
                    self.persona_id, cycle_id,
                )
                report.status = "error"
                report.error = "pulse_dispatcher unavailable"
            else:
                dispatcher.dispatch_autonomy_tick(
                    self.persona_id,
                    context={
                        "cycle_id": cycle_id,
                        "interval_seconds": int(self.interval_minutes * 60),
                    },
                )
                report.status = "completed"
        except Exception as exc:
            LOGGER.exception(
                "[Autonomy:%s] Periodic tick error: %s", self.persona_id, exc,
            )
            report.status = "error"
            report.error = str(exc)

        report.completed_at = time.time()
        self._last_report = report
        elapsed = report.completed_at - report.started_at
        LOGGER.info(
            "[Autonomy:%s] Tick %s: %s (%.1fs)",
            self.persona_id, cycle_id, report.status, elapsed,
        )

        self._current_cycle_id = None

        # 次回 push (stop 済みなら登録しない)
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return
            self._state = AutonomyState.WAITING
        self._schedule_next_tick()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_persona(self) -> Optional["PersonaCore"]:
        return self.manager.all_personas.get(self.persona_id)

    def _load_interval_from_judgment_config(self) -> Optional[float]:
        """META_JUDGMENT_CONFIG.periodic_interval_minutes を読み出す (Phase 4-e)。

        ペルソナ別の永続値を真実とする方針。MetaLayer 経由で安全にデフォルトと
        マージされた値を取得する。読み出せなければ None を返してフォールバック
        (env / module default) に委ねる。
        """
        persona = self._get_persona()
        meta_layer = getattr(self.manager, "meta_layer", None)
        if persona is None or meta_layer is None:
            return None
        try:
            config = meta_layer._load_judgment_config(persona)
            value = config.get("periodic_interval_minutes")
            if value is None:
                return None
            return max(0.5, float(value))
        except Exception:
            LOGGER.warning(
                "[Autonomy:%s] Failed to load periodic_interval_minutes from META_JUDGMENT_CONFIG",
                self.persona_id, exc_info=True,
            )
            return None
