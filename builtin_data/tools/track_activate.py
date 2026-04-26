"""track_activate: Track をアクティブ化する。

既存の running Track があれば自動的に pending 状態に押し出される
(同時 running は 1 本という不変条件を保証)。
"""
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


def track_activate(track_id: str) -> Tuple[str, ToolResult, None]:
    """Activate a track. Pushes any currently-running track to 'pending'."""
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    try:
        track = _track_manager.activate(track_id)
    except TrackNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except InvalidTrackStateError as exc:
        raise RuntimeError(f"track_activate failed: {exc}") from exc

    snippet = ToolResult(
        history_snippet=json.dumps(
            {"track_id": track.track_id, "status": track.status, "title": track.title},
            ensure_ascii=False,
        )
    )
    label = track.title or track.track_type
    return f"Activated track '{label}' ({track.track_id[:8]}…).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_activate",
        description=(
            "Activate a track (set its status to 'running'). If another track was "
            "running, it is automatically moved to 'pending'. The persona has at "
            "most one running track at any time."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "string",
                    "description": "Track ID to activate.",
                },
            },
            "required": ["track_id"],
        },
        result_type="string",
    )
