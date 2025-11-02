from __future__ import annotations

import json
from typing import Optional, Tuple

from persona.tasks import TaskStorage, TaskNotFoundError
from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolResult, ToolSchema

CLOSE_STATUSES = {"completed", "cancelled"}


def task_close(
    status: str = "completed",
    reason: Optional[str] = None,
    auto_activate_next: bool = True,
) -> Tuple[str, ToolResult, None]:
    """Close the current active task and optionally activate the next pending task."""

    if status not in CLOSE_STATUSES:
        raise ValueError(f"status must be one of {sorted(CLOSE_STATUSES)}")

    persona_id = _require_persona_id()
    base_dir = _derive_base_dir()
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        active_tasks = storage.list_tasks(statuses=["active"], limit=1, include_steps=True)
        if not active_tasks:
            raise RuntimeError("No active task to close.")
        task = active_tasks[0]

        updated = storage.update_task_status(
            task.id,
            status=status,
            actor=persona_id,
            reason=reason,
        )
        storage.set_active_step(updated.id, step_id=None, actor=persona_id)

        message = f"Marked task '{updated.title}' as {status}."

        if auto_activate_next:
            next_candidates = storage.list_tasks(statuses=["pending", "paused"], limit=1, include_steps=True)
            if next_candidates:
                storage.set_active_task(next_candidates[0].id, actor=persona_id)
                storage.set_active_step(
                    next_candidates[0].id,
                    step_id=_pick_next_step(next_candidates[0]),
                    actor=persona_id,
                )
                message += f" Activated next task '{next_candidates[0].title}'."

        snippet_payload = {
            "task_id": updated.id,
            "status": status,
            "reason": reason,
        }
        snippet = ToolResult(history_snippet=json.dumps(snippet_payload, ensure_ascii=False))
        return message, snippet, None
    except TaskNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        storage.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="task_close",
        description="Close the current active task and optionally activate the next pending task.",
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": sorted(CLOSE_STATUSES),
                    "default": "completed",
                    "description": "Closure status for the task.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional explanation for the closure.",
                },
                "auto_activate_next": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true, automatically activates the next pending task after closing.",
                },
            },
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


def _pick_next_step(task) -> Optional[str]:
    for step in task.steps:
        if step.status not in {"completed", "skipped"}:
            return step.id
    return None
