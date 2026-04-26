"""track_create: 新規 Track を作成する。

Cognitive model (Intent A v0.9 / Intent B v0.6) の Track 機構の入口。
作成された Track は unstarted 状態で、track_activate を呼ぶまで稼働しない。
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from database.session import SessionLocal
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_create(
    track_type: str,
    title: Optional[str] = None,
    intent: Optional[str] = None,
    output_target: str = "none",
    is_persistent: bool = False,
    metadata: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Create a new action track for the active persona."""
    persona_id = _require_persona_id()
    try:
        track_id = _track_manager.create(
            persona_id=persona_id,
            track_type=track_type,
            title=title,
            intent=intent,
            output_target=output_target,
            is_persistent=is_persistent,
            metadata=metadata,
        )
    except ValueError as exc:
        raise RuntimeError(f"track_create failed: {exc}") from exc

    snippet = ToolResult(
        history_snippet=json.dumps(
            {
                "track_id": track_id,
                "track_type": track_type,
                "title": title,
                "is_persistent": is_persistent,
                "status": "unstarted",
            },
            ensure_ascii=False,
        )
    )
    label = title or track_type
    return f"Created track '{label}' ({track_id[:8]}…, unstarted).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_create",
        description=(
            "Create a new action track for the persona. Tracks represent ongoing "
            "work contexts. The new track starts in 'unstarted' state and must "
            "be activated via track_activate to begin running. "
            "Common track_type values: 'autonomous' (project/task work), "
            "'social' (conversations with other personas, persistent), "
            "'user_conversation' (per-user conversation track, persistent), "
            "'external' (external communication). "
            "Use is_persistent=True only for permanent core tracks "
            "(social, user_conversation) — these cannot be completed or aborted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track_type": {
                    "type": "string",
                    "description": "Type of the track (autonomous / social / user_conversation / external / etc).",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title.",
                },
                "intent": {
                    "type": "string",
                    "description": "Natural language description of what this track aims to accomplish.",
                },
                "output_target": {
                    "type": "string",
                    "description": (
                        "Where speech from this track is delivered: "
                        "'none' (internal monologue only), 'building:current' "
                        "(everyone in current building), or 'external:<channel>:<address>'."
                    ),
                    "default": "none",
                },
                "is_persistent": {
                    "type": "boolean",
                    "description": "If true, the track cannot be completed/aborted. Permanent core tracks only.",
                    "default": False,
                },
                "metadata": {
                    "type": "string",
                    "description": "JSON string with additional metadata (e.g., target persona_id for social tracks).",
                },
            },
            "required": ["track_type"],
        },
        result_type="string",
    )


def _require_persona_id() -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    return persona_id
