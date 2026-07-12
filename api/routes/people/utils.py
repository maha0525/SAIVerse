"""Shared utilities for people API routes."""
from contextlib import contextmanager
from typing import Generator, Any

from fastapi import HTTPException

# Characters that must never appear in a persona ID used as a path component.
# Separators and drive designators would let the ID escape the personas root
# when joined to a path (on Windows, ``Path("C:/base") / "D:x"`` resolves to
# ``D:x``).
_PERSONA_ID_FORBIDDEN_CHARS = ("/", "\\", ":", "\x00")


def validate_persona_id(persona_id: Any) -> str:
    """Validate a persona ID for use as a single path component.

    Single validator shared by the people API helpers (P2 audit fix:
    2026-07-12 memory/persona boundary audit). Rejects empty values,
    ``.``/``..``, path separators, drive designators, and anything whose
    resolved directory would fall outside the canonical personas root
    (path traversal defence). Runs *before* any SAIMemoryAdapter is built,
    so no directory is ever created for a malformed ID.

    Returns the persona_id unchanged on success.
    Raises HTTPException(400) on failure.
    """
    if not isinstance(persona_id, str) or not persona_id.strip():
        raise HTTPException(status_code=400, detail="Invalid persona ID")
    if persona_id in (".", "..") or any(
        ch in persona_id for ch in _PERSONA_ID_FORBIDDEN_CHARS
    ):
        raise HTTPException(
            status_code=400, detail=f"Invalid persona ID: {persona_id!r}"
        )

    # Defence in depth: the resolved directory must sit directly under the
    # canonical personas root.
    from saiverse.data_paths import get_personas_dir

    personas_root = get_personas_dir().resolve()
    candidate = (personas_root / persona_id).resolve()
    if candidate.parent != personas_root:
        raise HTTPException(
            status_code=400, detail=f"Invalid persona ID: {persona_id!r}"
        )
    return persona_id


def _persona_exists_in_db(persona_id: str, manager: Any) -> bool:
    """Check whether the persona has an AI row in the main DB.

    Supports personas that are registered but not currently loaded in
    ``manager.personas`` (e.g. stopped or dispatched personas).
    """
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        return False
    from database.models import AI

    db = session_factory()
    try:
        return (
            db.query(AI.AIID).filter(AI.AIID == persona_id).first() is not None
        )
    finally:
        db.close()


def ensure_persona_exists(persona_id: str, manager: Any) -> str:
    """Validate the ID and require the persona to be loaded or in the main DB.

    API-side gate for the P2 orphan-memory.db bug: endpoints that end up
    constructing a SAIMemoryAdapter directly (e.g. chatlog / native import)
    must call this first so an unknown or malformed persona ID results in
    404/400 instead of a freshly created ``personas/<id>/memory.db``.

    Returns the persona_id on success.
    Raises HTTPException 400 (malformed ID) or 404 (unknown persona).
    """
    validate_persona_id(persona_id)
    personas = getattr(manager, "personas", None) or {}
    if persona_id in personas:
        return persona_id
    if _persona_exists_in_db(persona_id, manager):
        return persona_id
    raise HTTPException(status_code=404, detail=f"Persona not found: {persona_id}")


@contextmanager
def get_adapter(persona_id: str, manager: Any) -> Generator[Any, None, None]:
    """Context manager for safely acquiring and releasing SAIMemoryAdapter.

    The persona ID is validated (400 on malformed / traversal-style IDs) and
    must belong to a loaded persona (``manager.personas``) or an AI row in
    the main DB; otherwise 404 is raised *before* any adapter is created, so
    unknown IDs can no longer spawn orphan ``personas/<id>/memory.db``
    directories (P2 audit fix).

    Uses the adapter attached to the loaded persona when ready. Falls back
    to a temporary adapter only for personas that verifiably exist (loaded
    persona without a ready adapter, or a DB-registered persona that is not
    loaded).

    Usage:
        with get_adapter(persona_id, manager) as adapter:
            # use adapter
    """
    validate_persona_id(persona_id)
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False

    if not adapter or not adapter.is_ready():
        if persona is None and not _persona_exists_in_db(persona_id, manager):
            raise HTTPException(
                status_code=404, detail=f"Persona not found: {persona_id}"
            )
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
