from __future__ import annotations

import json
from typing import Optional, Tuple

from persona.tasks import TaskStorage, TaskNotFoundError
from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolResult, ToolSchema

ALLOWED_STATUSES = {"pending", "in_progress", "completed", "skipped"}


def task_update_step(
    step_position: int,
    status: str,
    notes: Optional[str] = None,
    auto_advance: bool = True,
) -> Tuple[str, ToolResult, None]:
    """Update the status of a step in the active task."""

    if status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_STATUSES)}")
    if step_position <= 0:
        raise ValueError("step_position must be >= 1")

    persona_id = _require_persona_id()
    base_dir = _derive_base_dir()
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        active_tasks = storage.list_tasks(statuses=["active"], limit=1, include_steps=True)
        if not active_tasks:
            raise RuntimeError("No active task found. Use task_change_active first.")
        task = active_tasks[0]
        if step_position > len(task.steps):
            raise ValueError(f"step_position {step_position} exceeds step count {len(task.steps)}")
        target_step = task.steps[step_position - 1]

        updated_task = storage.update_step_status(
            target_step.id,
            status=status,
            notes=notes,
            actor=persona_id,
        )

        if auto_advance:
            next_step_id = _determine_next_step(updated_task, status, target_step.id)
            updated_task = storage.set_active_step(
                updated_task.id,
                step_id=next_step_id,
                actor=persona_id,
            )

        snippet_payload = {
            "task_id": updated_task.id,
            "step_id": target_step.id,
            "status": status,
            "notes": notes,
        }
        snippet = ToolResult(history_snippet=json.dumps(snippet_payload, ensure_ascii=False))
        message = f"Updated step {step_position} of '{updated_task.title}' to {status}."
        return message, snippet, None
    except TaskNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        storage.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="task_update_step",
        description="Update the status and notes of a step within the active task.",
        parameters={
            "type": "object",
            "properties": {
                "step_position": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based position of the step within the active task.",
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
            "required": ["step_position", "status"],
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


def _derive_base_dir():
    persona_path = get_active_persona_path()
    if persona_path is None:
        return None
    return persona_path.parent.parent


def _determine_next_step(task, status: str, current_step_id: str) -> Optional[str]:
    if status == "in_progress":
        return current_step_id
    if status in {"completed", "skipped"}:
        for step in task.steps:
            if step.status not in {"completed", "skipped"}:
                return step.id
        return None
    return current_step_id
