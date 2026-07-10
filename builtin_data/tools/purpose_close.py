"""purpose_close: 目的ノードの stage 遷移 — 完了・中止・休眠 (P2c-1)。

旧 task_done (完了) の後継 + 中止 (cancelled)・休眠 (dormant) の明示遷移。
休眠は「薄れても消滅でなく休眠」(life_concept_map.md §5) — status は
cancelled (論理アーカイブ) にしつつ stage=dormant を明示的に刻む
(derive_stage の期限切れ経路と同じ着地。いつか命名で再び木に戻れる)。
"""
from __future__ import annotations

from typing import Optional

from database.session import SessionLocal
from saiverse.persona_task_manager import (
    STAGE_DORMANT,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
    TaskNotFoundError,
)
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_task_manager = PersonaTaskManager(SessionLocal)

_OUTCOME_LABELS = {
    "completed": "完了",
    "cancelled": "中止",
    "dormant": "休眠",
}


def purpose_close(node_ref: str, outcome: str = "completed", reason: Optional[str] = None) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"
    if outcome not in _OUTCOME_LABELS:
        return (
            f"Error: invalid outcome: {outcome!r}. "
            f"有効な値: {', '.join(sorted(_OUTCOME_LABELS))}"
        )

    status = STATUS_COMPLETED if outcome == "completed" else STATUS_CANCELLED
    try:
        task_id = _task_manager.resolve_task_ref(persona_id, node_ref)
        task = _task_manager.update_task_status(
            task_id, status=status, actor=persona_id, persona_id=persona_id,
            reason=reason,
        )
        if outcome == "dormant":
            # 休眠は中止 (論理アーカイブ) と区別して stage に明示的に刻む
            # (§5「薄れても消滅でなく休眠」— 命名でいつか木に戻れる)
            task = _task_manager.set_purpose_fields(
                task_id, persona_id=persona_id, actor=persona_id,
                stage=STAGE_DORMANT,
            )
    except TaskNotFoundError as e:
        return f"Error: {e}"

    label = _OUTCOME_LABELS[outcome]
    ref = task.get("task_ref") or node_ref
    return f"目的を{label}にしました: {ref} {task['title']}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="purpose_close",
        description=(
            "目的ノード（task:N）を閉じます。outcome で閉じ方を選びます: "
            "completed（やり遂げた）/ cancelled（やらないと決めた）/ "
            "dormant（今は続けないが、いつか戻るかもしれない — 休眠）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ref": {
                    "type": "string",
                    "description": "閉じる目的ノードの参照（例: task:5）",
                },
                "outcome": {
                    "type": "string",
                    "enum": sorted(_OUTCOME_LABELS),
                    "default": "completed",
                    "description": "閉じ方（completed / cancelled / dormant）",
                },
                "reason": {
                    "type": "string",
                    "description": "任意: 閉じる理由（履歴に残ります）",
                },
            },
            "required": ["node_ref"],
        },
        result_type="string",
        spell=True,
        spell_display_name="目的を閉じる",
    )
