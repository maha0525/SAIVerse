from __future__ import annotations

import json
from typing import Optional, Tuple

from database.session import SessionLocal
from saiverse.persona_task_manager import PersonaTaskManager, TaskNotFoundError
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

ALLOWED_STATUSES = {"pending", "in_progress", "completed", "skipped"}

_task_manager = PersonaTaskManager(session_factory=SessionLocal)


def task_update_step(
    task_ref: str,
    step_position: int,
    status: str,
    notes: Optional[str] = None,
    auto_advance: bool = True,
) -> Tuple[str, ToolResult, None]:
    """Update the status of a step within the task addressed by ``task_ref``."""

    if status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_STATUSES)}")
    if step_position <= 0:
        raise ValueError("step_position must be >= 1")

    persona_id = _require_persona_id()
    try:
        task_id = _task_manager.resolve_task_ref(persona_id, task_ref)
        task = _task_manager.get_task(task_id, persona_id=persona_id)
        steps = task["steps"]
        if step_position > len(steps):
            raise ValueError(
                f"step_position {step_position} exceeds step count {len(steps)}"
            )
        target_step = steps[step_position - 1]

        updated_task = _task_manager.update_step_status(
            target_step["id"], status=status, actor=persona_id, notes=notes
        )

        if auto_advance:
            next_step_id = _determine_next_step(updated_task, status, target_step["id"])
            updated_task = _task_manager.set_active_step(
                updated_task["id"], step_id=next_step_id, persona_id=persona_id, actor=persona_id
            )

        snippet_payload = {
            "task_ref": updated_task.get("task_ref"),
            "task_id": updated_task["id"],
            "step_id": target_step["id"],
            "status": status,
            "notes": notes,
        }
        snippet = ToolResult(history_snippet=json.dumps(snippet_payload, ensure_ascii=False))
        ref = updated_task.get("task_ref") or task_ref
        message = f"Updated step {step_position} of '{updated_task['title']}' ({ref}) to {status}."
        return message, snippet, None
    except TaskNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc


def schema() -> ToolSchema:
    return ToolSchema(
        name="task_update_step",
        description="Update the status and notes of a step within a task (addressed by task:N).",
        parameters={
            "type": "object",
            "properties": {
                "task_ref": {
                    "type": "string",
                    "description": "Task ref (e.g. task:5), shown in the task list.",
                },
                "step_position": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based position of the step within the task.",
                },
                "status": {
                    "type": "string",
                    "enum": sorted(ALLOWED_STATUSES),
                    "description": "New status for the step.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes or remarks for this step.",
                },
                "auto_advance": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "If true, automatically set active_step to the next pending step "
                        "when the current step is completed or skipped."
                    ),
                },
            },
            "required": ["task_ref", "step_position", "status"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ステップ更新",
    )


def _require_persona_id() -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    return persona_id


def _determine_next_step(task: dict, status: str, current_step_id: str) -> Optional[str]:
    if status == "in_progress":
        return current_step_id
    if status in {"completed", "skipped"}:
        for step in task["steps"]:
            if step["status"] not in {"completed", "skipped"}:
                return step["id"]
        return None
    return current_step_id
