"""Gemini SDK client setup shared without importing the llm_clients package."""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

from saiverse.gemini_timing import trace_gemini_request

LOGGER = logging.getLogger(__name__)


def _get_genai_module():
    module = sys.modules.get("llm_clients.gemini")
    if module is not None:
        custom = getattr(module, "genai", None)
        if custom is not None:
            return custom
    from google import genai as google_genai

    return google_genai


def build_gemini_clients(
    *,
    prefer_paid: bool = False,
    timeout_seconds: float | None = None,
    _genai_module: Any | None = None,
) -> tuple[Any | None, Any | None, Any | None]:
    """Create ``(free, paid, active)`` SDK clients from current environment.

    ``timeout_seconds`` (positive) overrides ``GEMINI_TIMEOUT_SECONDS``
    (default 300, ``0`` = no timeout). Model configs pass their ``timeout`` here.
    """
    genai = _genai_module or _get_genai_module()
    if timeout_seconds is not None and timeout_seconds > 0:
        effective_timeout = float(timeout_seconds)
    else:
        effective_timeout = float(int(os.getenv("GEMINI_TIMEOUT_SECONDS", "300")))
    # HttpOptions.timeout is in milliseconds.
    timeout_ms = None if effective_timeout == 0 else int(effective_timeout * 1000)

    def _http_options() -> Any:
        client_args = {"event_hooks": {"request": [trace_gemini_request]}}
        retry_options = genai.types.HttpRetryOptions(
            attempts=5,
            initial_delay=1.0,
            max_delay=30.0,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        )
        if timeout_ms is None:
            return genai.types.HttpOptions(retry_options=retry_options, client_args=client_args)
        return genai.types.HttpOptions(
            timeout=timeout_ms,
            retry_options=retry_options,
            client_args=client_args,
        )

    free_key = os.getenv("GEMINI_FREE_API_KEY")
    paid_key = os.getenv("GEMINI_API_KEY")
    if not free_key and not paid_key:
        LOGGER.warning(
            "No Gemini API key configured (GEMINI_FREE_API_KEY / GEMINI_API_KEY). "
            "Gemini features will be unavailable until a key is set."
        )
        return None, None, None

    def _make_client(api_key: str | None):
        if not api_key:
            return None
        return genai.Client(api_key=api_key, http_options=_http_options())

    free_client = _make_client(free_key)
    paid_client = _make_client(paid_key)
    active_client = (
        paid_client
        if prefer_paid and paid_client is not None
        else (free_client or paid_client)
    )
    if active_client is None:
        LOGGER.warning("Failed to initialize Gemini client despite having keys.")
        return None, None, None
    return free_client, paid_client, active_client


__all__ = ["build_gemini_clients"]
