"""Cancellation token for interrupting playbook execution.

This module provides a mechanism to safely interrupt running playbook
executions when higher priority requests arrive.
"""
from __future__ import annotations

import threading
from typing import Optional


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
        self._interrupted_by: Optional[str] = None
        self._lock = threading.Lock()
    
    def cancel(self, interrupted_by: Optional[str] = None) -> None:
        """Signal cancellation to the running execution.
        
        Args:
            interrupted_by: Type of request that caused the interruption (user/schedule/auto)
        """
        with self._lock:
            # 一度受けた「止める」合図は、そのあとの普通の割り込みで上書き
            # しない。停止ボタンの直後に新しい発言が届くと、割り込みが原因を
            # "user" に書き換え、止めたはずの処理が固まったツールを待ち直して
            # しまう (PR #467 の Codex 指摘)。「止める」同士では最初の原因を残す。
            if self._interrupted_by not in STOP_INTERRUPTED_BY:
                self._interrupted_by = interrupted_by
            self._cancelled.set()
    
    def is_cancelled(self) -> bool:
        """Check if cancellation has been requested."""
        return self._cancelled.is_set()
    
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
            self._interrupted_by = None


__all__ = ["CancellationToken", "ExecutionCancelledException", "STOP_INTERRUPTED_BY"]
