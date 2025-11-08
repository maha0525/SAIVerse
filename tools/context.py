from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Optional

_PERSONA_ID: ContextVar[Optional[str]] = ContextVar("saiverse_persona_id", default=None)
_PERSONA_PATH: ContextVar[Optional[str]] = ContextVar("saiverse_persona_path", default=None)
_MANAGER: ContextVar[Optional[Any]] = ContextVar("saiverse_manager_ref", default=None)


def get_active_persona_id() -> Optional[str]:
    return _PERSONA_ID.get()


def get_active_persona_path() -> Optional[Path]:
    value = _PERSONA_PATH.get()
    return Path(value) if value else None


def get_active_manager() -> Optional[Any]:
    return _MANAGER.get()


@contextmanager
def persona_context(persona_id: str, persona_path: Path | str, manager: Optional[Any] = None) -> Iterator[None]:
    """Temporarily set the active persona identity for tool execution."""
    token_id = _PERSONA_ID.set(persona_id)
    token_path = _PERSONA_PATH.set(str(persona_path))
    token_manager = _MANAGER.set(manager)
    try:
        yield
    finally:
        _PERSONA_ID.reset(token_id)
        _PERSONA_PATH.reset(token_path)
        _MANAGER.reset(token_manager)
