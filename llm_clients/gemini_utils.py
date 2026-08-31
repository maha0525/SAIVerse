"""Backward-compatible facade for shared Gemini SDK client setup."""
from __future__ import annotations

from typing import Any

from saiverse import gemini_clients as _shared


def _get_genai_module() -> Any:
    """Keep the historical test/integration patch point available."""
    return _shared._get_genai_module()


def build_gemini_clients(
    *,
    prefer_paid: bool = False,
) -> tuple[Any | None, Any | None, Any | None]:
    return _shared.build_gemini_clients(
        prefer_paid=prefer_paid,
        _genai_module=_get_genai_module(),
    )

__all__ = ["build_gemini_clients"]
