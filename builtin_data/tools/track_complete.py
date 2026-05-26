"""track_complete: running な Track を完了状態にする。

永続 Track (is_persistent=true) は完了できない (Intent A v0.9 不変条件)。

Intent A v0.14 / Intent B v0.11 以降、Pulse 完了時に適用される (deferred)。
"""
from __future__ import annotations

import json
from typing import Tuple

from _track_common import (
    DEFERRED_NOTICE,
    apply_track_op,
    format_short_id,
    get_pulse_context,
    resolve_track_ref,
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


def track_complete(track_id: str) -> Tuple[str, ToolResult, None]:
    """Mark a running track as completed.

    Within a Pulse: enqueued. Outside a Pulse: immediate.
    """
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )

    track_id = resolve_track_ref(track_id, _track_manager)

    try:
        result = apply_track_op(
            get_pulse_context(), "complete",
            track_id=track_id, track_manager=_track_manager,
        )
    except TrackNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except (PersistentTrackError, InvalidTrackStateError) as exc:
        raise RuntimeError(f"track_complete failed: {exc}") from exc

    if result.deferred:
        snippet = ToolResult(
            history_snippet=json.dumps(
                {"track_id": track_id, "queued": "complete"},
                ensure_ascii=False,
            )
        )
        return (
            f"Track complete scheduled for end of Pulse (track_id={track_id[:8]}…). "
            f"{DEFERRED_NOTICE}",
            snippet,
            None,
        )

    track = result.track
    sid = format_short_id(track)
    snippet = ToolResult(
        history_snippet=json.dumps(
            {"track_id": track.track_id, "short_id": sid, "status": "completed"},
            ensure_ascii=False,
        )
    )
    label = track.title or track.track_type
    return f"Completed track '{label}' ({sid}).", snippet, None


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
                "track_id": {"type": "string", "description": "Track short ref (e.g. 't:1') or full UUID."},
            },
            "required": ["track_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="トラック完了",
    )
