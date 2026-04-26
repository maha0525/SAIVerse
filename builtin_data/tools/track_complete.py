"""track_complete: running な Track を完了状態にする。

永続 Track (is_persistent=true) は完了できない (Intent A v0.9 不変条件)。
"""
from __future__ import annotations

import json
from typing import Tuple

from database.session import SessionLocal
from saiverse.track_manager import (
    InvalidTrackStateError,
    PersistentTrackError,
    TrackManager,
    TrackNotFoundError,
)
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_complete(track_id: str) -> Tuple[str, ToolResult, None]:
    """Mark a running track as completed."""
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    try:
        track = _track_manager.complete(track_id)
    except TrackNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except PersistentTrackError as exc:
        raise RuntimeError(f"track_complete failed: {exc}") from exc
    except InvalidTrackStateError as exc:
        raise RuntimeError(f"track_complete failed: {exc}") from exc

    snippet = ToolResult(
        history_snippet=json.dumps(
            {"track_id": track.track_id, "status": "completed"},
            ensure_ascii=False,
        )
    )
    label = track.title or track.track_type
    return f"Completed track '{label}' ({track.track_id[:8]}…).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_complete",
        description=(
            "Mark a running track as 'completed'. The track must be currently "
            "running. Persistent core tracks (user_conversation, social) cannot "
            "be completed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track_id": {"type": "string", "description": "Track ID to complete."},
            },
            "required": ["track_id"],
        },
        result_type="string",
    )
