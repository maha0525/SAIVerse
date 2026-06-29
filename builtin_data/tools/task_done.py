"""task_done: タスクを完了にする (task:N 参照)。"""
from __future__ import annotations

from database.session import SessionLocal
from saiverse.persona_task_manager import (
    STATUS_COMPLETED,
    PersonaTaskManager,
    TaskNotFoundError,
)
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_task_manager = PersonaTaskManager(session_factory=SessionLocal)


def task_done(task_ref: str) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"

    try:
        task_id = _task_manager.resolve_task_ref(persona_id, task_ref)
        task = _task_manager.update_task_status(
            task_id, status=STATUS_COMPLETED, actor=persona_id, persona_id=persona_id
        )
    except TaskNotFoundError as e:
        return f"Error: {e}"
    return f"タスク完了: {task.get('task_ref') or task_ref} {task['title']}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="task_done",
        description="Mark a task as done by its task reference (e.g. task:5).",
        parameters={
            "type": "object",
            "properties": {
                "task_ref": {
                    "type": "string",
                    "description": "Task ref (e.g. task:5), shown in the task list.",
                },
            },
            "required": ["task_ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="タスク完了",
    )
