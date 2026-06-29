"""task_decompose: タスクをステップに分解する (task:N + steps 配列)。

分解の知性は本スペルを撃つ側の LLM (自律モノローグ) が担う。本スペルは
受け取った ``steps`` 配列をそのままタスクのステップとして記録する純粋な
決定論スペル (= 余計な LLM 呼び出しなし・キャッシュ共用)。
"""
from __future__ import annotations

from database.session import SessionLocal
from saiverse.persona_task_manager import PersonaTaskManager, TaskNotFoundError
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_task_manager = PersonaTaskManager(session_factory=SessionLocal)


def task_decompose(task_ref: str, steps: list) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"
    if not isinstance(steps, list) or not steps:
        return "Error: steps must be a non-empty array of {title, description?} objects"

    # 各要素を {title, description?} に正規化 (title 必須)
    normalized = []
    for i, s in enumerate(steps, start=1):
        if isinstance(s, str):
            normalized.append({"title": s})
        elif isinstance(s, dict) and (s.get("title") or s.get("summary")):
            normalized.append({
                "title": s.get("title") or s.get("summary"),
                "description": s.get("description"),
            })
        else:
            return f"Error: step #{i} must have a title"

    try:
        task_id = _task_manager.resolve_task_ref(persona_id, task_ref)
        task = _task_manager.set_steps(
            task_id, normalized, persona_id=persona_id, actor=persona_id
        )
    except TaskNotFoundError as e:
        return f"Error: {e}"

    ref = task.get("task_ref") or task_ref
    lines = [f"タスク分解: {ref} {task['title']}"]
    for idx, st in enumerate(task["steps"], start=1):
        lines.append(f"  {idx}. {st['title']}")
    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="task_decompose",
        description=(
            "Decompose a task into steps. Provide the task reference (task:N) and "
            "a steps array; each step is an object with a 'title' (and optional "
            "'description'). Replaces any existing steps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_ref": {
                    "type": "string",
                    "description": "Task ref (e.g. task:5), shown in the task list.",
                },
                "steps": {
                    "type": "array",
                    "description": "Ordered steps, each {title, description?}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["task_ref", "steps"],
        },
        result_type="string",
        spell=True,
        spell_display_name="タスク分解",
    )
