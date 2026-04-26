"""track_pause: running な Track を pending (後回し) にする。"""
from __future__ import annotations

import json
from typing import Tuple

from database.session import SessionLocal
from saiverse.track_manager import (
    InvalidTrackStateError,
    TrackManager,
    TrackNotFoundError,
)
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_pause(track_id: str) -> Tuple[str, ToolResult, None]:
    """Pause a running (or alert) track to 'pending'."""
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    try:
        track = _track_manager.pause(track_id)
    except TrackNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except InvalidTrackStateError as exc:
        raise RuntimeError(f"track_pause failed: {exc}") from exc

    snippet = ToolResult(
        history_snippet=json.dumps(
            {"track_id": track.track_id, "status": track.status},
            ensure_ascii=False,
        )
    )
    label = track.title or track.track_type
    return f"Paused track '{label}' ({track.track_id[:8]}…).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_pause",
        description=(
            "Pause a running track to 'pending' state. Use this when switching "
            "to another task without finishing the current one. Resumable later "
            "via track_activate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track_id": {"type": "string", "description": "Track ID to pause."},
            },
            "required": ["track_id"],
        },
        result_type="string",
    )
