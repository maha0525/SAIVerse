"""track_abort: Track を中止する (途中で諦める)。

永続 Track は中止できない。

Intent A v0.14 / Intent B v0.11 以降、Pulse 完了時に適用される (deferred)。
"""
from __future__ import annotations

import json
from typing import Tuple

from _track_common import (
    DEFERRED_NOTICE,
    apply_track_op,
    get_pulse_context,
)
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


def track_abort(track_id: str) -> Tuple[str, ToolResult, None]:
    """Abort a non-terminal track.

    Within a Pulse: enqueued. Outside a Pulse: immediate.
    """
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )

    try:
        result = apply_track_op(
            get_pulse_context(), "abort",
            track_id=track_id, track_manager=_track_manager,
        )
    except TrackNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except (PersistentTrackError, InvalidTrackStateError) as exc:
        raise RuntimeError(f"track_abort failed: {exc}") from exc

    if result.deferred:
        snippet = ToolResult(
            history_snippet=json.dumps(
                {"track_id": track_id, "queued": "abort"},
                ensure_ascii=False,
            )
        )
        return (
            f"Track abort scheduled for end of Pulse (track_id={track_id[:8]}…). "
            f"{DEFERRED_NOTICE}",
            snippet,
            None,
        )

    track = result.track
    snippet = ToolResult(
        history_snippet=json.dumps(
            {"track_id": track.track_id, "status": "aborted"},
            ensure_ascii=False,
        )
    )
    label = track.title or track.track_type
    return f"Aborted track '{label}' ({track.track_id[:8]}…).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_abort",
        description=(
            "Abort a track without completion. Use when giving up on the work. "
            "Persistent core tracks (user_conversation, social) cannot be aborted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track_id": {"type": "string", "description": "Track ID to abort."},
            },
            "required": ["track_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="トラック中止",
    )
