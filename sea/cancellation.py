"""Cancellation token for interrupting playbook execution.

This module provides a mechanism to safely interrupt running playbook
executions when higher priority requests arrive.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, List, Optional

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


#: 現在実行中の LLM 呼び出しに紐づく CancellationToken。
#: HTTP トランスポートやリトライ待機 (sleep) がスレッドを跨がずに停止合図へ
#: 反応できるよう、呼び出し元が ``bind_llm_cancellation_token`` でセットする。
_ACTIVE_LLM_CANCEL_TOKEN: ContextVar[Optional[CancellationToken]] = ContextVar(
    "active_llm_cancel_token", default=None,
)


def current_llm_cancellation_token() -> Optional[CancellationToken]:
    """Return the CancellationToken bound to the current LLM call, if any."""
    return _ACTIVE_LLM_CANCEL_TOKEN.get()


def current_llm_stop_reason() -> Optional[str]:
    """Return interrupted_by if the active LLM call received user_stop / server_shutdown."""
    token = _ACTIVE_LLM_CANCEL_TOKEN.get()
    if token is not None and token.is_stopped():
        return token.interrupted_by
    return None


def raise_if_llm_stopped() -> None:
    """Raise ExecutionCancelledException if the active LLM call was stopped."""
    reason = current_llm_stop_reason()
    if reason is not None:
        raise ExecutionCancelledException(
            message=f"LLM request stopped ({reason})",
            interrupted_by=reason,
        )


@contextmanager
def bind_llm_cancellation_token(token: Optional[CancellationToken]) -> Iterator[None]:
    """Bind ``token`` to the current execution context for the duration of an LLM call."""
    if token is None:
        yield
        return
    ctx_token = _ACTIVE_LLM_CANCEL_TOKEN.set(token)
    try:
        yield
    finally:
        _ACTIVE_LLM_CANCEL_TOKEN.reset(ctx_token)


__all__ = [
    "CancellationToken",
    "ExecutionCancelledException",
    "STOP_INTERRUPTED_BY",
    "bind_llm_cancellation_token",
    "current_llm_cancellation_token",
    "current_llm_stop_reason",
    "raise_if_llm_stopped",
]
