"""Read-only addon providers for volatile realtime context, never the head."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)
_providers: dict[str, Callable[..., str]] = {}
_lock = threading.Lock()


def register_provider(addon_name: str, provider: Callable[..., str]) -> None:
    with _lock:
        _providers[addon_name] = provider


def unregister_provider(addon_name: str) -> None:
    with _lock:
        _providers.pop(addon_name, None)


def context_sections(persona: Any, building_id: str) -> list[str]:
    with _lock:
        providers = sorted(_providers.items())
    sections = []
    for name, provider in providers:
        try:
            text = provider(persona=persona, building_id=building_id)
            if isinstance(text, str) and text.strip():
                sections.append(text)
        except Exception:
            LOGGER.warning("addon context provider failed addon=%s", name, exc_info=True)
    return sections
