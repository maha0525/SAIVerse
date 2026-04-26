"""track_list: アクティブなペルソナの Track 一覧を取得する。"""
from __future__ import annotations

import json
from typing import List, Optional, Tuple

from database.session import SessionLocal
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_list(
    statuses: Optional[List[str]] = None,
    include_forgotten: bool = False,
) -> Tuple[str, ToolResult, None]:
    """List tracks for the active persona."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    tracks = _track_manager.list_for_persona(
        persona_id=persona_id,
        statuses=statuses,
        include_forgotten=include_forgotten,
    )
    payload = [
        {
            "track_id": t.track_id,
            "title": t.title,
            "track_type": t.track_type,
            "status": t.status,
            "is_persistent": t.is_persistent,
            "is_forgotten": t.is_forgotten,
            "intent": t.intent,
            "last_active_at": t.last_active_at.isoformat() if t.last_active_at else None,
        }
        for t in tracks
    ]
    snippet = ToolResult(history_snippet=json.dumps(payload, ensure_ascii=False))
    if not tracks:
        return "No tracks found.", snippet, None
    return f"Found {len(tracks)} track(s).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_list",
        description=(
            "List the persona's tracks. By default, forgotten tracks are excluded. "
            "Use 'statuses' to filter by status (e.g., ['running', 'pending', 'waiting'])."
        ),
        parameters={
            "type": "object",
            "properties": {
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by status. Values: running, alert, pending, waiting, unstarted, completed, aborted.",
                },
                "include_forgotten": {
                    "type": "boolean",
                    "description": "If true, include tracks with is_forgotten=true.",
                    "default": False,
                },
            },
        },
        result_type="string",
    )
