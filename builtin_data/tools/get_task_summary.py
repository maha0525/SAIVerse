"""Get task summary for the active persona."""
from __future__ import annotations

import logging
from typing import List

from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema

LOGGER = logging.getLogger(__name__)


def get_task_summary(limit: int = 12) -> str:
    """Get a summary of the active persona's tasks.

    Includes:
    - Active task with steps
    - Pending/paused tasks

    Returns formatted task summary.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    # Get task storage
    task_storage = getattr(persona, "task_storage", None)
    if task_storage is None:
        return "(タスクストレージが利用できません)"

    try:
        records = task_storage.list_tasks(include_steps=True, limit=limit)
    except Exception as exc:
        LOGGER.warning("Failed to load task summary: %s", exc)
        return f"(タスク読み込みエラー: {exc})"

    if not records:
        return "(現在登録されているタスクはありません)"

    lines: List[str] = []

    # Active task
    active = next((task for task in records if task.status == "active"), None)
    if active:
        lines.append(f"### アクティブタスク: {active.title} [{active.status}]")
        # Format steps
        for idx, step in enumerate(active.steps, start=1):
            marker = "→" if active.active_step_id == step.id else "・"
            lines.append(f"  {marker} Step{idx} [{step.status}] {step.title}")
    else:
        lines.append("### アクティブタスク: (なし)")

    # Pending/paused tasks
    pending = [task for task in records if task.status in {"pending", "paused"}]
    if pending:
        lines.append("### 待機中タスク")
        for task in pending[:3]:
            lines.append(f"- {task.title} [{task.status}]")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_task_summary",
        description="Get a summary of the active persona's tasks including active and pending tasks.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tasks to retrieve. Default: 12.",
                    "default": 12
                }
            },
            "required": [],
        },
        result_type="string",
    )
