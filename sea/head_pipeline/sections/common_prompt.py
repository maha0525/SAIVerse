"""CommonPromptSection — common_prompt template の placeholder 展開を head に。

`sea/runtime_context.py` 旧 system prompt の 1. common_prompt 展開を移植。
``{current_persona_name}`` / ``{current_building_name}`` 等の placeholder を
capture 時点の値で焼き込み、render は展開済み文字列をそのまま返す。

詳細: docs/intent/cached_head_architecture.md §5.3
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)


@dataclass(frozen=True)
class CommonPromptSnapshot:
    text: str  # placeholder 展開済み


class CommonPromptSection:
    name = "common_prompt"
    order = 100  # 一番先頭
    # NOTE: BUILDING_ENTERED は意図的に含めない (= cache 中変えない原則)。
    # template に {current_building_name} 等の placeholder があると、移動後も
    # 古い名前のまま残る trade-off は許容する (= 末尾通知でペルソナに伝わる)。
    refresh_on_events = frozenset({EventType.SYSTEM_PROMPT_EDITED})

    def capture(self, ctx: LineHeadInput) -> CommonPromptSnapshot:
        persona = ctx.persona
        if persona is None:
            return CommonPromptSnapshot(text="")
        template = getattr(persona, "common_prompt", None)
        if not template:
            return CommonPromptSnapshot(text="")

        building_id = ctx.current_building_id
        buildings = getattr(persona, "buildings", None) or {}
        building_obj = buildings.get(building_id) if isinstance(buildings, dict) else None
        building_name = (
            getattr(building_obj, "name", None) if building_obj else None
        ) or (building_id or "")

        # visual_context を section 化したので、ここでは常に
        # base_system_instruction を採用する (= 旧コードで visual_context 有無で
        # 切り替えていた system_instruction / base_system_instruction の分岐は廃止)。
        building_sys = (
            getattr(building_obj, "base_system_instruction", "")
            if building_obj else ""
        ) or ""

        replacements = {
            "{current_persona_name}": getattr(persona, "persona_name", "Unknown") or "Unknown",
            "{current_persona_id}": getattr(persona, "persona_id", "unknown_id") or "unknown_id",
            "{current_building_name}": building_name,
            "{current_city_name}": getattr(persona, "current_city_id", "unknown_city") or "unknown_city",
            "{current_persona_system_instruction}": getattr(persona, "persona_system_instruction", "") or "",
            "{current_building_system_instruction}": building_sys,
            "{linked_user_name}": getattr(persona, "linked_user_name", "the user") or "the user",
        }
        text = template
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, str(value))
        return CommonPromptSnapshot(text=text.strip())

    def render(self, snapshot: CommonPromptSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.text:
            return None
        return RenderedSection(text=snapshot.text)

    def diff_to_notifications(
        self,
        old: Optional[CommonPromptSnapshot],
        new: Optional[CommonPromptSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None or old == new:
            return []
        # common_prompt は実質常に展開差分は他 Section が個別通知するため、
        # ここでは fallback の単発通知のみ。
        return [NotificationLabel(
            kind="common_prompt_changed",
            label="あなたを取り巻く前提情報が更新されました",
        )]

    def serialize_snapshot(self, snapshot: CommonPromptSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> CommonPromptSnapshot:
        return CommonPromptSnapshot(**json.loads(data))
