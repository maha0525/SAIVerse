"""Handler resolution helpers for Track-driven Pulse routing.

This module previously hosted ``prepare_pulse_root_context`` /
``build_fixed_section`` / ``build_dynamic_section`` and ``cache_built_at``
bookkeeping (Phase 1.1). Those entry points were never integrated into the
runtime path (verified 2026-05-09 — no callers in the codebase) and have
been removed in v0.32 in favor of the **Track Chronicle** mechanism.

What remains here is the small ``get_handler_for_track`` lookup used by
``MetaLayer`` and similar dispatchers to find the Handler instance for a
given Track type.

See:
- docs/intent/persona_cognition/track_chronicle.md  (Track 内必要情報の維持機構)
- docs/intent/persona_cognition/revisions.md         v0.32 (2026-05-09)
"""

from __future__ import annotations

from typing import Any, Optional


# Map track_type → attribute on SAIVerseManager. Kept here (instead of
# inside each Handler module) so the runtime can resolve a Handler from a
# Track without importing every Handler subclass.
_HANDLER_ATTR_BY_TYPE = {
    "user_conversation": "user_conversation_handler",
    "social": "social_track_handler",
    "autonomous": "autonomous_track_handler",
}


def get_handler_for_track(manager: Any, track: Any) -> Optional[Any]:
    """Resolve the Handler instance responsible for the given Track.

    Returns None when no Handler matches the track_type — the caller should
    fall back to the legacy context path. This keeps custom / experimental
    Track types working without forcing them to register a Handler.
    """
    track_type = getattr(track, "track_type", None)
    if not track_type:
        return None
    attr = _HANDLER_ATTR_BY_TYPE.get(track_type)
    if not attr:
        return None
    return getattr(manager, attr, None)
