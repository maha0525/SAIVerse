"""Shared utilities for people API routes."""
from contextlib import contextmanager
from typing import Generator, Any
from fastapi import HTTPException


@contextmanager
def get_adapter(persona_id: str, manager: Any) -> Generator[Any, None, None]:
    """Context manager for safely acquiring and releasing SAIMemoryAdapter.
    
    Tries to use the adapter attached to the persona first.
    Falls back to creating a temporary adapter if not available.
    
    Usage:
        with get_adapter(persona_id, manager) as adapter:
            # use adapter
    """
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")
    
    try:
        yield adapter
    finally:
        if should_close and adapter:
            adapter.close()
