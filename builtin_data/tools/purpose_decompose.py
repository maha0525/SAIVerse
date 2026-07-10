"""purpose_decompose: 目的ノードをステップに分解する (旧 task_decompose 後継、P2c-1)。

分解の知性は本スペルを撃つ側の LLM (自律モノローグ) が担う。本スペルは
受け取った ``steps`` 配列をそのままノードのステップとして記録する純粋な
決定論スペル (= 余計な LLM 呼び出しなし・キャッシュ共用)。

purpose_step (1 ステップの状態更新) とは分けた 2 本構成 — 配列全置換と
位置指定の状態遷移は引数の系統が完全に別で、1 本に混ぜると required が
書けず誤爆源になる (P2c-1 実装判断)。
"""
from __future__ import annotations

from database.session import SessionLocal
from saiverse.persona_task_manager import PersonaTaskManager, TaskNotFoundError
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_task_manager = PersonaTaskManager(SessionLocal)


def purpose_decompose(node_ref: str, steps: list) -> str:
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
        task_id = _task_manager.resolve_task_ref(persona_id, node_ref)
        task = _task_manager.set_steps(
            task_id, normalized, persona_id=persona_id, actor=persona_id
        )
    except TaskNotFoundError as e:
        return f"Error: {e}"

    ref = task.get("task_ref") or node_ref
    lines = [f"目的を分解: {ref} {task['title']}"]
    for idx, st in enumerate(task["steps"], start=1):
        lines.append(f"  {idx}. {st['title']}")
    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="purpose_decompose",
        description=(
            "目的ノード（task:N）をステップに分解します。steps 配列の各要素は "
            "title（と任意の description）を持つオブジェクトで、既存のステップは"
            "すべて置き換えられます。1つのステップの進捗を更新するには "
            "purpose_step を使ってください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ref": {
                    "type": "string",
                    "description": "分解する目的ノードの参照（例: task:5）",
                },
                "steps": {
                    "type": "array",
                    "description": "順序どおりのステップ列。各要素は {title, description?}",
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
            "required": ["node_ref", "steps"],
        },
        result_type="string",
        spell=True,
        spell_display_name="目的をステップに分解",
    )
