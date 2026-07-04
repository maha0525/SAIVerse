"""desire_add: やりたいこと候補を desire ノートに追加する。

自律作業モード (AUTONOMOUS) が「いつかやりたい」と思いついたことを候補として
溜める。候補 = desire ノートに紐づく Task (parent_kind='note')。後で自律制御
モード (META) がここから Track を作る (= 昇格)。

AUTONOMOUS は Track を作れない (mode_spell_permissions.md) ため、思いついた
やりたいことはこのスペルで候補プールに渡す。

自律行動 v2 §5 の六型拡張: 欲求は六型 (話す/聞く/作る/知る/経験する/自分を
更新する) の ``type`` と、この欲求を生んだ実経験への参照 ``source`` (接地の
証跡) を持てる。どちらも省略可 — 既存呼び出し (track_autonomous 等) は
type/source なしで従来どおり動き、その場合は「未分類」として保存される。

詳細: docs/intent/persona_cognition/autonomous_desire.md §5 /
docs/intent/autonomous_behavior_v2.md §5.2
"""
from __future__ import annotations

from typing import Optional

from database.session import SessionLocal
from saiverse.desire_engine import DESIRE_TYPES
from saiverse.note_manager import NoteManager
from saiverse.persona_task_manager import PARENT_NOTE, PersonaTaskManager
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_note_manager = NoteManager(session_factory=SessionLocal)
_task_manager = PersonaTaskManager(SessionLocal)


def desire_add(
    title: str,
    goal: Optional[str] = None,
    type: Optional[str] = None,
    source: Optional[str] = None,
) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"

    if type is not None and type not in DESIRE_TYPES:
        return (
            f"Error: invalid type: {type!r}. "
            f"有効な型: {', '.join(DESIRE_TYPES)} (省略も可)"
        )

    note_id = _note_manager.ensure_desire_note(persona_id)
    task = _task_manager.create_task(
        persona_id=persona_id,
        title=title,
        goal=goal or "",
        parent_kind=PARENT_NOTE,
        note_id=note_id,
        origin="autonomous",
        auto_activate=False,
        desire_type=type,
        desire_source=source,
    )
    ref = task.get("task_ref") or "task:?"
    return f"やりたいこと候補を追加: {ref} [{type or '未分類'}] {title}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="desire_add",
        description=(
            "Add a 'want to do someday' candidate to your desire pool. "
            "Use this when, during autonomous work, you think of something you'd "
            "like to pursue but that isn't part of the current Track. The candidate "
            "is held in your desire note; later, in autonomous-control mode, a Track "
            "can be created from it. Keep candidates concrete (one want each). "
            "Give each desire its type (one of the six primitive desire types) and "
            "quote the real experience that sparked it in 'source' — desires grounded "
            "in real experience are the ones that grow into interests."
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
                "type": {
                    "type": "string",
                    "enum": list(DESIRE_TYPES),
                    "description": (
                        "Desire type (六型): 話す (tell someone) / 聞く (hear from someone) / "
                        "作る (make something) / 知る (find out) / 経験する (experience a place "
                        "or event) / 自分を更新する (update yourself). Optional; omitted = "
                        "unclassified."
                    ),
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Optional: reference to / quote from the real experience that gave "
                        "birth to this desire (a remark, an event, a passage you read)."
                    ),
                },
            },
            "required": ["title"],
        },
        result_type="string",
        spell=True,
        spell_display_name="やりたいこと追加",
    )
