"""Autonomous behavior manager for individual personas.

Phase C-2 統合 (intent A v0.14 / intent B v0.7) 後の責務:
  ペルソナごとの定期 tick タイマーとして動作し、間隔経過のたびに
  ``MetaLayer.on_periodic_tick(persona_id)`` を発火する。
  判断ロジック本体 (旧 Decision / Execution) は MetaLayer の
  ``meta_judgment`` Playbook に委譲済み。

旧版 (Phase C-1 以前) は ``Decision (heavy) → Execution (light)`` の
2段サイクルを内部で抱え、Stelis スレッドや PulseController callback の
登録も行っていたが、不変条件 11 ("メタ判断 = ペルソナ自身の思考の流れ")
を厳密に守るため、判断は Playbook 経由に統一した。Stelis 管理 /
ユーザー割り込み連携は MetaLayer 側の alert observer に集約され、
このクラスは純粋なタイマーとなった。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager
    from persona.core import PersonaCore

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
    """Report from a completed periodic tick.

    Phase C-2 移行で旧 Decision/Execution の playbook/intent/output 情報は
    持たない。シンプルな実行記録のみ。
    """
    cycle_id: str
    started_at: float = 0.0
    completed_at: float = 0.0
    status: str = "pending"  # pending, completed, error
    error: Optional[str] = None


class AutonomyManager:
    """Manages the periodic meta-layer tick loop for a single persona.

    Phase C-2 後の Lifecycle:
        1. start() → background loop 起動
        2. Loop runs: MetaLayer.on_periodic_tick → wait → repeat
        3. stop() → loop 停止
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
        # Phase C-2: env override (intent A v0.13 §"Pulse サイクルの 7 つの制御点" #3).
        # 引数 > env > module default の優先順位。env は秒単位なので分に変換する。
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
        # Phase C-2 移行で Decision/Execution は MetaLayer の meta_judgment Playbook
        # に委譲済み。これらの引数は API 互換性のためだけに残す (使われない)。
        self.decision_model = decision_model
        self.execution_model = execution_model

        self._state = AutonomyState.STOPPED
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
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
        """Start the periodic tick loop.

        Returns True if started successfully, False if already running.
        """
        with self._lock:
            if self._state != AutonomyState.STOPPED:
                LOGGER.warning(
                    "[Autonomy:%s] Cannot start: already in state %s",
                    self.persona_id, self._state,
                )
                return False

            self._stop_event.clear()
            self._state = AutonomyState.RUNNING
            self._thread = threading.Thread(
                target=self._loop,
                name=f"autonomy-{self.persona_id}",
                daemon=True,
            )
            self._thread.start()

            LOGGER.info(
                "[Autonomy:%s] Started (interval=%.1f min)",
                self.persona_id, self.interval_minutes,
            )
            return True

    def stop(self) -> bool:
        """Stop the periodic tick loop.

        Returns True if stopped, False if not running.
        """
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return False
            self._stop_event.set()
            self._state = AutonomyState.STOPPED

        # Wait for thread to finish (outside lock)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

        LOGGER.info("[Autonomy:%s] Stopped", self.persona_id)
        return True

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_interval(self, minutes: float) -> None:
        """Update the loop interval (takes effect next cycle)."""
        self.interval_minutes = max(0.5, minutes)
        LOGGER.info(
            "[Autonomy:%s] Interval set to %.1f min",
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

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Background loop: tick → wait → tick → ...

        Phase C-2 統合 (intent A v0.14 / intent B v0.7) 後の流れ:
        1. ``MetaLayer.on_periodic_tick(persona_id)`` を発火
           - 内部で ACTIVITY_STATE / post_complete_behavior による抑止判定
           - Active かつ wait_response でなければ meta_judgment Playbook 実行
        2. ``interval_minutes`` 待機 → 1 へ戻る
        """
        LOGGER.info(
            "[Autonomy:%s] Periodic tick loop started (interval=%.1f min)",
            self.persona_id, self.interval_minutes,
        )

        while not self._stop_event.is_set():
            cycle_id = str(uuid.uuid4())[:8]
            self._current_cycle_id = cycle_id
            report = CycleReport(cycle_id=cycle_id, started_at=time.time())

            try:
                self._state = AutonomyState.DECIDING
                meta_layer = getattr(self.manager, "meta_layer", None)
                if meta_layer is None:
                    LOGGER.warning(
                        "[Autonomy:%s] No meta_layer on manager; skipping tick %s",
                        self.persona_id, cycle_id,
                    )
                    report.status = "error"
                    report.error = "meta_layer unavailable"
                else:
                    meta_layer.on_periodic_tick(
                        self.persona_id,
                        context={"cycle_id": cycle_id, "interval_seconds": int(self.interval_minutes * 60)},
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

            self._state = AutonomyState.WAITING
            self._current_cycle_id = None
            wait_seconds = self.interval_minutes * 60
            self._stop_event.wait(wait_seconds)

        self._state = AutonomyState.STOPPED
        LOGGER.info("[Autonomy:%s] Loop ended", self.persona_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_persona(self) -> Optional["PersonaCore"]:
        """Get the persona object from the manager."""
        return self.manager.all_personas.get(self.persona_id)
