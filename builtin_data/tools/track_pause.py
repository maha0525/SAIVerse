"""track_pause: running な Track を pending (後回し) にする。

Intent A v0.14 / Intent B v0.11 以降、Pulse 完了時に適用される (deferred)。
"""
from __future__ import annotations

import json
from typing import Tuple

from _track_common import (
    DEFERRED_NOTICE,
    enqueue_or_warn,
    get_pulse_context,
)
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
    """Pause a running (or alert) track to 'pending'.

    Within a Pulse: enqueued. Outside a Pulse: immediate.
    """
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )

    pulse_ctx = get_pulse_context()
    if enqueue_or_warn(pulse_ctx, "pause", track_id=track_id):
        snippet = ToolResult(
            history_snippet=json.dumps(
                {"track_id": track_id, "queued": "pause"},
                ensure_ascii=False,
            )
        )
        return (
            f"Track pause scheduled for end of Pulse (track_id={track_id[:8]}…). "
            f"{DEFERRED_NOTICE}",
            snippet,
            None,
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
        spell=True,
        spell_display_name="トラック後回し",
    )
