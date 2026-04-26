"""note_close: Track から Note を外す (Note 自体は残る、関連を切るだけ)。"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from database.session import SessionLocal
from saiverse.note_manager import NoteManager
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_note_manager = NoteManager(session_factory=SessionLocal)
_track_manager = TrackManager(session_factory=SessionLocal)


def note_close(
    note_id: str,
    track_id: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Detach a note from a track. The note itself is preserved."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )

    target_track_id = track_id
    if not target_track_id:
        running = _track_manager.get_running(persona_id)
        if running is None:
            raise RuntimeError(
                "No running track for the active persona. Specify track_id or activate a track first."
            )
        target_track_id = running.track_id

    _note_manager.detach_from_track(target_track_id, note_id)

    snippet = ToolResult(
        history_snippet=json.dumps(
            {"note_id": note_id, "track_id": target_track_id, "action": "detached"},
            ensure_ascii=False,
        )
    )
    return f"Closed note from track {target_track_id[:8]}….", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="note_close",
        description=(
            "Detach a note from a track (the note itself is preserved as a "
            "permanent asset). Use when a particular reference is no longer "
            "relevant to the current work."
        ),
        parameters={
            "type": "object",
            "properties": {
                "note_id": {"type": "string", "description": "Note ID to detach."},
                "track_id": {
                    "type": "string",
                    "description": "Track ID to detach from. Omit to use the running track.",
                },
            },
            "required": ["note_id"],
        },
        result_type="string",
    )
