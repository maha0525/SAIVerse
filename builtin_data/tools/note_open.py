"""note_open: 現在 running な Track に Note を開く (関連付ける)。

開いた Note は Track の文脈として参照される。中断後の再開時は
Note の差分が再開コンテキストに挿入される。
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from database.session import SessionLocal
from saiverse.note_manager import NoteManager, NoteNotFoundError
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_note_manager = NoteManager(session_factory=SessionLocal)
_track_manager = TrackManager(session_factory=SessionLocal)


def note_open(
    note_id: str,
    track_id: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Attach a note to a track. If track_id is omitted, the currently running track is used."""
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

    try:
        _note_manager.attach_to_track(target_track_id, note_id)
        _note_manager.touch_opened(note_id)
        note = _note_manager.get(note_id)
    except NoteNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc

    snippet = ToolResult(
        history_snippet=json.dumps(
            {"note_id": note_id, "track_id": target_track_id, "title": note.title},
            ensure_ascii=False,
        )
    )
    return (
        f"Opened note '{note.title}' on track {target_track_id[:8]}….",
        snippet,
        None,
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="note_open",
        description=(
            "Open (attach) a note to a track so it stays referenced while the "
            "track is running. If track_id is omitted, the currently running "
            "track is used. Use this when starting work that benefits from a "
            "particular note's accumulated knowledge (e.g., open the relevant "
            "vocation note before creative work)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "note_id": {"type": "string", "description": "Note ID to open."},
                "track_id": {
                    "type": "string",
                    "description": "Track ID to attach the note to. Omit to use the running track.",
                },
            },
            "required": ["note_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ノートを開く",
    )
