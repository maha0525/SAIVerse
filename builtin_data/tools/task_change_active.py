from __future__ import annotations

import json
from typing import Optional, Tuple

from persona.tasks import TaskStorage, TaskNotFoundError
from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolResult, ToolSchema


def task_change_active(task_id: Optional[str] = None) -> Tuple[str, ToolResult, None]:
    """Activate a task (or the most recent pending task) for the current persona."""

    persona_id = _require_persona_id()
    base_dir = _derive_base_dir()
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        if task_id:
            record = storage.set_active_task(task_id, actor=persona_id)
        else:
            candidates = storage.list_tasks(statuses=["pending", "paused"], limit=1, include_steps=True)
            if not candidates:
                raise RuntimeError("No pending or paused tasks available to activate.")
            record = storage.set_active_task(candidates[0].id, actor=persona_id)

        next_step = _pick_next_step(record)
        record = storage.set_active_step(record.id, step_id=next_step, actor=persona_id)

        snippet_payload = {
            "task_id": record.id,
            "active_step_id": record.active_step_id,
            "title": record.title,
            "status": record.status,
        }
        snippet = ToolResult(history_snippet=json.dumps(snippet_payload, ensure_ascii=False))
        return f"Activated task '{record.title}'.", snippet, None
    except TaskNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        storage.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="task_change_active",
        description=(
            "Activate a task for the current persona. If no task_id is supplied, "
            "the most recently updated pending/paused task is activated."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional task identifier to activate.",
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
    # persona_path === ~/.saiverse/personas/<persona_id>
    # base_dir should be ~/.saiverse
    return persona_path.parent.parent


def _pick_next_step(task) -> Optional[str]:
    for step in task.steps:
        if step.status not in {"completed", "skipped"}:
            return step.id
    return None
