"""desire_add: やりたいこと候補を desire ノートに追加する。

自律作業モード (AUTONOMOUS) が「いつかやりたい」と思いついたことを候補として
溜める。候補 = desire ノートに紐づく Task (parent_kind='note')。後で自律制御
モード (META) がここから Track を作る (= 昇格)。

AUTONOMOUS は Track を作れない (mode_spell_permissions.md) ため、思いついた
やりたいことはこのスペルで候補プールに渡す。

詳細: docs/intent/persona_cognition/autonomous_desire.md §5
"""
from __future__ import annotations

from typing import Optional

from database.session import SessionLocal
from saiverse.note_manager import NoteManager
from saiverse.persona_task_manager import PARENT_NOTE, PersonaTaskManager
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_note_manager = NoteManager(session_factory=SessionLocal)
_task_manager = PersonaTaskManager(SessionLocal)


def desire_add(title: str, goal: Optional[str] = None) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"

    note_id = _note_manager.ensure_desire_note(persona_id)
    task = _task_manager.create_task(
        persona_id=persona_id,
        title=title,
        goal=goal or "",
        parent_kind=PARENT_NOTE,
        note_id=note_id,
        origin="autonomous",
        auto_activate=False,
    )
    ref = task.get("task_ref") or "task:?"
    return f"やりたいこと候補を追加: {ref} {title}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="desire_add",
        description=(
            "Add a 'want to do someday' candidate to your desire pool. "
            "Use this when, during autonomous work, you think of something you'd "
            "like to pursue but that isn't part of the current Track. The candidate "
            "is held in your desire note; later, in autonomous-control mode, a Track "
            "can be created from it. Keep candidates concrete (one want each)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The thing you want to do (e.g. 'practice landscape sketching').",
                },
                "goal": {
                    "type": "string",
                    "description": "Optional: what you'd achieve or why you want it.",
                },
            },
            "required": ["title"],
        },
        result_type="string",
        spell=True,
        spell_display_name="やりたいこと追加",
    )
