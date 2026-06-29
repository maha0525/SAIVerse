"""life_purpose_set: 確定した「生きる目的 / 趣味 / 仕事」を保存する。

autonomous_desire.md §4 (C 案): 自律行動の初回、ペルソナが自分の人格と記憶から
生きる目的のドラフトを作り、メインモードでユーザーに提示・確認する。ユーザーが
確認・修正して確定したら、このスペルで保存する。保存後は AUTONOMOUS / META
プロンプトに常駐注入され、候補・Track の方向付けになる。

配列引数 (interests / vocations) を持つため、呼び出しは canonical 形式
``/spell name='life_purpose_set' args={...}`` で行う (fuzzy KV は配列を解析不可)。
"""
from __future__ import annotations

from typing import List, Optional

from database.session import SessionLocal
from saiverse.life_purpose import set_life_purpose
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_session_factory = SessionLocal


def life_purpose_set(
    purpose: str,
    interests: Optional[List[str]] = None,
    vocations: Optional[List[str]] = None,
) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"
    if not (purpose or interests or vocations):
        return "Error: 生きる目的・趣味・仕事のいずれかを指定してください"

    data = set_life_purpose(
        _session_factory, persona_id,
        purpose=purpose or "", interests=interests, vocations=vocations,
    )
    parts = []
    if data.get("purpose"):
        parts.append(f"目的「{data['purpose']}」")
    if data.get("interests"):
        parts.append(f"趣味 {len(data['interests'])} 件")
    if data.get("vocations"):
        parts.append(f"仕事 {len(data['vocations'])} 件")
    return "生きる目的を保存しました: " + "、".join(parts)


def schema() -> ToolSchema:
    return ToolSchema(
        name="life_purpose_set",
        description=(
            "Save your confirmed life purpose / interests / vocations. Use this once, "
            "during the first-time self-definition: after you draft your purpose and the "
            "user confirms (or revises) it in main mode. The saved values are then kept "
            "in your prompt as a persistent reminder that guides your autonomous wants "
            "and Track choices. Call with canonical form: "
            "/spell name='life_purpose_set' args={\"purpose\":\"...\",\"interests\":[...],\"vocations\":[...]}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string",
                    "description": "Your reason for living / overall direction (one or two sentences).",
                },
                "interests": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hobbies / things you pursue for their own sake.",
                },
                "vocations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Your work / calling / how you contribute.",
                },
            },
            "required": ["purpose"],
        },
        result_type="string",
        spell=True,
        spell_display_name="生きる目的を保存",
    )
