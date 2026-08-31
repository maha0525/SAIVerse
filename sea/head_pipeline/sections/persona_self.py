"""PersonaSelfSection — "## あなたについて" の persona_system_instruction を head に。

`sea/runtime_context.py` 旧 system prompt の 2. ``## あなたについて`` を移植。
inventory は items の動的差分通知と一体化する Phase 3 で InventorySection に
分離するため、本 Section では扱わない。

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
class PersonaSelfSnapshot:
    persona_id: str
    persona_name: str
    persona_system_instruction: str


class PersonaSelfSection:
    name = "persona_self"
    order = 200
    # 人格の同一性を担う required Section (W6 fail-closed)。capture / render /
    # persist の失敗時は LLM を実行しない (SEA 監査 S6)。
    required = True
    refresh_on_events = frozenset({EventType.SYSTEM_PROMPT_EDITED})

    def capture(self, ctx: LineHeadInput) -> PersonaSelfSnapshot:
        persona = ctx.persona
        if persona is None:
            return PersonaSelfSnapshot(
                persona_id=ctx.persona_id, persona_name="",
                persona_system_instruction="",
            )
        return PersonaSelfSnapshot(
            persona_id=ctx.persona_id,
            persona_name=getattr(persona, "persona_name", "") or "",
            persona_system_instruction=getattr(persona, "persona_system_instruction", "") or "",
        )

    def render(self, snapshot: PersonaSelfSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        instruction = (snapshot.persona_system_instruction or "").strip()
        if not instruction:
            return None
        return RenderedSection(text=f"## あなたについて\n{instruction}")

    def diff_to_notifications(
        self, old: Optional[PersonaSelfSnapshot], new: Optional[PersonaSelfSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        labels: list[NotificationLabel] = []
        if old.persona_name != new.persona_name:
            labels.append(NotificationLabel(
                kind="persona_renamed",
                label=f"あなたの名前が「{old.persona_name}」から「{new.persona_name}」に変わりました",
            ))
        if old.persona_system_instruction != new.persona_system_instruction:
            labels.append(NotificationLabel(
                kind="persona_system_prompt_changed",
                label="あなたの設定 (system prompt) が更新されました",
            ))
        return labels

    def serialize_snapshot(self, snapshot: PersonaSelfSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> PersonaSelfSnapshot:
        return PersonaSelfSnapshot(**json.loads(data))
