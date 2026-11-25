"""Cancellation token for interrupting playbook execution.

This module provides a mechanism to safely interrupt running playbook
executions when higher priority requests arrive.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from saiverse.http_cancel import (
    bind_llm_cancellation_token,
    current_llm_cancellation_token,
    current_llm_stop_reason,
    raise_if_llm_stopped,
)

LOGGER = logging.getLogger(__name__)


class ExecutionCancelledException(Exception):
    """Raised when execution is cancelled due to higher priority request."""
    
    def __init__(self, message: str = "Execution cancelled", interrupted_by: Optional[str] = None):
        super().__init__(message)
        self.interrupted_by = interrupted_by


#: 「止める」合図の原因。停止ボタン (``user_stop``) と SAIVerse の終了処理
#: (``server_shutdown``)。新しい発言・自律・スケジュールによる割り込み
#: ("user" / "auto" / "schedule") は、走っている処理の完了を待つ合図で、
#: これとは別物 (docs/intent/stop_button.md)。
STOP_INTERRUPTED_BY = frozenset({"user_stop", "server_shutdown"})


class CancellationToken:
    """Token for signaling cancellation to running playbook executions.
    
    Usage:
        token = CancellationToken()
        
        # In another thread, to cancel:
        token.cancel(interrupted_by="user")
        
        # In the executing code, to check:
        token.raise_if_cancelled()  # raises ExecutionCancelledException
        # or
        if token.is_cancelled():
            # handle cancellation
    """
    
    def __init__(self):
        self._cancelled = threading.Event()
        self._stopped = threading.Event()
        self._interrupted_by: Optional[str] = None
        self._lock = threading.Lock()
        self._stop_callbacks: List[Callable[[Optional[str]], None]] = []
    
    def cancel(self, interrupted_by: Optional[str] = None) -> None:
        """Signal cancellation to the running execution.
        
        Args:
            interrupted_by: Type of request that caused the interruption (user/schedule/auto)
        """
        callbacks_to_fire: List[Callable[[Optional[str]], None]] = []
        stop_reason: Optional[str] = None
        with self._lock:
            # 一度受けた「止める」合図は、そのあとの普通の割り込みで上書き
            # しない。停止ボタンの直後に新しい発言が届くと、割り込みが原因を
            # "user" に書き換え、止めたはずの処理が固まったツールを待ち直して
            # しまう (PR #467 の Codex 指摘)。「止める」同士では最初の原因を残す。
            if self._interrupted_by not in STOP_INTERRUPTED_BY:
                self._interrupted_by = interrupted_by
            self._cancelled.set()
            if self._interrupted_by in STOP_INTERRUPTED_BY:
                was_stopped = self._stopped.is_set()
                self._stopped.set()
                if not was_stopped and self._stop_callbacks:
                    callbacks_to_fire = list(self._stop_callbacks)
                    stop_reason = self._interrupted_by
        for cb in callbacks_to_fire:
            try:
                cb(stop_reason)
            except Exception:  # noqa: BLE001 — 停止通知自体は絶対に落とさない
                LOGGER.debug("[cancellation] stop callback failed", exc_info=True)
    
    def is_cancelled(self) -> bool:
        """Check if cancellation has been requested."""
        return self._cancelled.is_set()

    def is_stopped(self) -> bool:
        """Check if an explicit stop (user_stop / server_shutdown) was requested."""
        return self._stopped.is_set()

    def register_on_stop(
        self,
        callback: Callable[[Optional[str]], None],
    ) -> Callable[[], None]:
        """Register a callback invoked immediately on user_stop / server_shutdown.

        If the token is already stopped, ``callback`` is invoked immediately on
        the caller thread and a no-op unregister function is returned.
        """
        fire_now = False
        reason: Optional[str] = None
        with self._lock:
            if self._stopped.is_set():
                fire_now = True
                reason = self._interrupted_by
            else:
                self._stop_callbacks.append(callback)

        if fire_now:
            try:
                callback(reason)
            except Exception:  # noqa: BLE001
                LOGGER.debug("[cancellation] immediate stop callback failed", exc_info=True)
            return lambda: None

        def _unregister() -> None:
            with self._lock:
                try:
                    self._stop_callbacks.remove(callback)
                except ValueError:
                    pass

        return _unregister

    def wait_for_stop(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a stop signal; returns True if stopped."""
        return self._stopped.wait(timeout=max(0.0, float(timeout)))
    
    def raise_if_cancelled(self) -> None:
        """Raise ExecutionCancelledException if cancellation was requested."""
        if self._cancelled.is_set():
            raise ExecutionCancelledException(
                message="Execution interrupted by higher priority request",
                interrupted_by=self._interrupted_by
            )

    def raise_if_stopped(self) -> None:
        """Raise ExecutionCancelledException if an explicit stop was requested."""
        if self._stopped.is_set():
            reason = self.interrupted_by
            raise ExecutionCancelledException(
                message=f"LLM request stopped ({reason})",
                interrupted_by=reason,
            )
    
    @property
    def interrupted_by(self) -> Optional[str]:
        """Get the type of request that caused the interruption."""
        with self._lock:
            return self._interrupted_by
    
    def reset(self) -> None:
        """Reset the token for reuse (use with caution)."""
        with self._lock:
            self._cancelled.clear()
            self._stopped.clear()
            self._interrupted_by = None
            self._stop_callbacks.clear()


__all__ = [
    "CancellationToken",
    "ExecutionCancelledException",
    "STOP_INTERRUPTED_BY",
    "bind_llm_cancellation_token",
    "current_llm_cancellation_token",
    "current_llm_stop_reason",
    "raise_if_llm_stopped",
]
