"""Backward-compatible re-export facade for ``saiverse.http_cancel``.

The implementation lives in ``saiverse.http_cancel`` so shared modules like
``saiverse.gemini_clients`` can use it without triggering ``llm_clients``
package initialization (avoiding circular imports via ``llm_clients.factory``).
"""
from __future__ import annotations

from saiverse.http_cancel import (
    CancellableHTTP2Transport,
    CancellableHTTPTransport,
    _CancellableSyncBackend,
    _CancellableSyncBackend2,
    cancellable_sleep,
    install_cancellable_transport,
    install_cancellable_transport2,
)

__all__ = [
    "CancellableHTTPTransport",
    "CancellableHTTP2Transport",
    "install_cancellable_transport",
    "install_cancellable_transport2",
    "cancellable_sleep",
    "_CancellableSyncBackend",
    "_CancellableSyncBackend2",
]
