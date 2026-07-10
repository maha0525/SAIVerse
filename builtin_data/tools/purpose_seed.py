"""purpose_seed: やりたいこと候補を生む (旧 desire_add 後継、P2c-1)。

**seed = 候補を生む** — 木の外の候補プール (desire ノート) に「いつかやりたい」
を書き留める。木に**接ぐ** (採用する) のは別の動詞 ``purpose_adopt``
(concept_consolidation.md P2c-0 決定4: 候補を生む操作と採用する操作は分ける)。

候補 = desire ノートに紐づく Task (parent_kind='note')。後で自律制御モード
(META) がここから Track を作る (= 昇格)。AUTONOMOUS は Track を作れない
(mode_spell_permissions.md) ため、思いついたやりたいことはこのスペルで候補
プールに渡す。

自律行動 v2 §5 の六型拡張: 欲求は六型 (話す/聞く/作る/知る/経験する/自分を
更新する) の ``type`` と、この欲求を生んだ実経験への参照 ``source`` (接地の
証跡) を持てる。どちらも省略可 — 省略時は「未分類」として保存される
(旧 desire_add と同一の接地規律・後方互換)。

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


def purpose_seed(
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
    # 符号は task:N に一本化 (参照アドレッシング統一 Q2)。欲求もバックログタスクも
    # 同じ short_id 参照空間なので task:N で一意。欲求らしさは提示文脈で伝わる。
    ref = task.get("task_ref") or "task:?"
    return f"やりたいこと候補を追加: {ref} [{type or '未分類'}] {title}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="purpose_seed",
        description=(
            "「いつかやりたい」と思いついたことを、候補として書き留めます"
            "（seed = 候補を生む。木に接ぐ = 採用は purpose_adopt が担います）。"
            "候補はやりたいことの候補プールに保管され、後から採用されて"
            "目的の木に接がれます。1件につき1つの具体的なやりたいことを"
            "書いてください。type には欲求の六型のいずれかを、source には"
            "このやりたいことが生まれた実経験の引用や参照を添えてください — "
            "実経験に根ざした候補ほど、関心として育ちます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "やりたいこと（例: 風景スケッチの練習をする）",
                },
                "goal": {
                    "type": "string",
                    "description": "任意: 何を達成したいか・なぜやりたいか",
                },
                "type": {
                    "type": "string",
                    "enum": list(DESIRE_TYPES),
                    "description": (
                        "欲求の型（六型）: 話す（誰かに伝える）/ 聞く（誰かから聞く）/ "
                        "作る（何かを作る）/ 知る（調べて知る）/ 経験する（場所や出来事を"
                        "体験する）/ 自分を更新する。省略時は未分類。"
                    ),
                },
                "source": {
                    "type": "string",
                    "description": (
                        "任意: このやりたいことを生んだ実経験への参照や引用"
                        "（誰かの一言・出来事・読んだ一節など）"
                    ),
                },
            },
            "required": ["title"],
        },
        result_type="string",
        spell=True,
        spell_display_name="やりたいことを書き留める",
    )
