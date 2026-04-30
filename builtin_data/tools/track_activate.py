"""track_activate: Track をアクティブ化する。

既存の running Track があれば自動的に pending 状態に押し出される
(同時 running は 1 本という不変条件を保証)。

Intent A v0.14 / Intent B v0.11 以降、この操作は Pulse 完了時に適用される
(deferred)。同じ Pulse 内でペルソナが「切替後の Track でやる予定の作業」を
連発しないよう、戻り値で明示的に指示する。
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
    TrackManager,
    TrackNotFoundError,
)
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_activate(track_id: str) -> Tuple[str, ToolResult, None]:
    """Activate a track. Pushes any currently-running track to 'pending'.

    Within a Pulse: enqueued onto PulseContext.deferred_track_ops; runtime
    applies it at Pulse completion.
    Outside a Pulse (CLI / tests / MetaLayer-spawned Playbook): immediate.
    """
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )

    try:
        result = apply_track_op(
            get_pulse_context(), "activate",
            track_id=track_id, track_manager=_track_manager,
        )
    except TrackNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except InvalidTrackStateError as exc:
        raise RuntimeError(f"track_activate failed: {exc}") from exc

    if result.deferred:
        snippet = ToolResult(
            history_snippet=json.dumps(
                {"track_id": track_id, "queued": "activate"},
                ensure_ascii=False,
            )
        )
        return (
            f"Track activate scheduled for end of Pulse (track_id={track_id[:8]}…). "
            f"{DEFERRED_NOTICE}",
            snippet,
            None,
        )

    track = result.track
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
        spell=True,
        spell_display_name="トラック起動",
    )
