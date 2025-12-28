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


__all__ = ["CancellationToken", "ExecutionCancelledException"]
