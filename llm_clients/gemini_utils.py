"""Shared helpers for Gemini SDK client setup."""
from __future__ import annotations

import os
import sys
from typing import Any, Tuple


def _get_genai_module():
    module = sys.modules.get("llm_clients.gemini")
    if module is not None:
        custom = getattr(module, "genai", None)
        if custom is not None:
            return custom
    from google import genai as google_genai  # type: ignore

    return google_genai


def build_gemini_clients(*, prefer_paid: bool = False) -> Tuple[Any | None, Any | None, Any]:
    """Create (free, paid, active) Gemini SDK clients with shared env handling."""
    genai = _get_genai_module()
    free_key = os.getenv("GEMINI_FREE_API_KEY")
    paid_key = os.getenv("GEMINI_API_KEY")
    if not free_key and not paid_key:
        raise RuntimeError("GEMINI_FREE_API_KEY or GEMINI_API_KEY environment variable is not set.")
    free_client = genai.Client(api_key=free_key) if free_key else None
    paid_client = genai.Client(api_key=paid_key) if paid_key else None
    active_client = paid_client if prefer_paid and paid_client is not None else (free_client or paid_client)
    if active_client is None:
        raise RuntimeError("Failed to initialize Gemini client; no API key available.")
    return free_client, paid_client, active_client

__all__ = ["build_gemini_clients"]
