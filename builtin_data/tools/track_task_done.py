"""track_task_done: Track のタスクを完了にする。"""
from __future__ import annotations

from database.session import SessionLocal
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_task_done(track_id: str, index: int) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"

    resolved = _track_manager.resolve_track_ref(persona_id, track_id)
    try:
        _track_manager.complete_task(resolved, index)
    except ValueError as e:
        return f"Error: {e}"
    return f"タスク完了: #{index}\n\n{_track_manager.format_task_list(resolved)}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_task_done",
        description="Mark a task as done in a Track's task list.",
        parameters={
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "string",
                    "description": "Track ref (e.g. t:3)",
                },
                "index": {
                    "type": "integer",
                    "description": "Task index (0-based, shown in task list)",
                },
            },
            "required": ["track_id", "index"],
        },
        result_type="string",
        spell=True,
        spell_display_name="タスク完了",
    )
