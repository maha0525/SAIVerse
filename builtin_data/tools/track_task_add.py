"""track_task_add: Track にタスクを追加する。"""
from __future__ import annotations

from database.session import SessionLocal
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_task_add(track_id: str, title: str) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"

    resolved = _track_manager.resolve_track_ref(persona_id, track_id)
    _track_manager.add_task(resolved, title)
    return f"タスク追加: {title}\n\n{_track_manager.format_task_list(resolved)}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_task_add",
        description="Add a task to a Track's task list.",
        parameters={
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "string",
                    "description": "Track ref (e.g. t:3)",
                },
                "title": {
                    "type": "string",
                    "description": "Task title",
                },
            },
            "required": ["track_id", "title"],
        },
        result_type="string",
        spell=True,
        spell_display_name="タスク追加",
    )
