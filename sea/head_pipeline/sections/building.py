"""BuildingSection — 現在地 Building の静的情報 (name + system_instruction) を head に。

`sea/runtime_context.py` 旧 system prompt の 3. ``## {building_name}`` を移植。
items / occupants は Phase 3 で BuildingItemsSection / BuildingOccupantsSection に
分離するため、本 Section では扱わない (= dynamic_state との二重通知回避)。

詳細: docs/intent/cached_head_architecture.md §5.3
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildingSnapshot:
    building_id: Optional[str]
    name: str
    base_system_instruction: str
    physical_vessel_id: Optional[str]


class BuildingSection:
    """現在地 Building の静的情報セクション。"""

    name = "building"
    order = 300
    # NOTE: BUILDING_ENTERED は意図的に含めない。移動で head が refresh されると
    # cache が壊れる ("cache 中変えない" 原則違反)。移動の通知は auto_ingest +
    # flush_diffs (本 Section の building_changed diff label) で末尾に流れる。
    refresh_on_events = frozenset({EventType.SYSTEM_PROMPT_EDITED})

    # ---- capture ----

    def capture(self, ctx: LineHeadInput) -> BuildingSnapshot:
        building_id = ctx.current_building_id
        building_obj = self._resolve_building(ctx, building_id)
        if building_obj is None:
            return BuildingSnapshot(
                building_id=building_id, name=building_id or "",
                base_system_instruction="", physical_vessel_id=None,
            )
        return BuildingSnapshot(
            building_id=building_id,
            name=getattr(building_obj, "name", "") or (building_id or ""),
            base_system_instruction=(
                getattr(building_obj, "base_system_instruction", None)
                or getattr(building_obj, "system_instruction", None)
                or ""
            ),
            physical_vessel_id=getattr(building_obj, "physical_vessel_id", None),
        )

    # ---- render ----

    def render(self, snapshot: BuildingSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        if not snapshot.base_system_instruction.strip() and not snapshot.name:
            return None
        parts: list[str] = []
        if snapshot.base_system_instruction.strip():
            parts.append(snapshot.base_system_instruction.strip())
        header = f"## {snapshot.name}"
        if snapshot.building_id:
            header += f" (ID: {snapshot.building_id})"
        if parts:
            body = "\n\n".join(parts)
            return RenderedSection(text=f"{header}\n{body}")
        return RenderedSection(text=header)

    # ---- diff ----

    def diff_to_notifications(
        self, old: Optional[BuildingSnapshot], new: Optional[BuildingSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        labels: list[NotificationLabel] = []
        if old.building_id != new.building_id:
            # 移動: 新 Building の system_prompt / vessel_id を multi-line で同梱
            # する (= 旧 auto_ingest の _format_own_enter_with_building_info 経路を
            # 引き継ぎ、通知を pipeline diff に統一する)。
            lines: list[str] = [
                f"現在地が「{old.name or old.building_id}」から"
                f"「{new.name or new.building_id}」に変わりました",
            ]
            if new.physical_vessel_id:
                lines.append(f"物理身体: あり (vessel_id={new.physical_vessel_id})")
            system_prompt = (new.base_system_instruction or "").strip()
            if system_prompt:
                lines.append("")
                lines.append(f"# 「{new.name or new.building_id}」の役割・指示")
                lines.append(system_prompt)
            labels.append(NotificationLabel(
                kind="building_changed",
                label="\n".join(lines),
            ))
            # Building が違うと name / system_instruction の比較は意味がないので
            # 移動通知だけで打ち切る。
            return labels
        if old.name != new.name:
            labels.append(NotificationLabel(
                kind="building_renamed",
                label=f"Building 名が「{old.name}」から「{new.name}」に変わりました",
            ))
        if old.base_system_instruction != new.base_system_instruction:
            labels.append(NotificationLabel(
                kind="building_system_prompt_changed",
                label=f"「{new.name}」の説明が更新されました",
            ))
        if old.physical_vessel_id != new.physical_vessel_id:
            if new.physical_vessel_id:
                labels.append(NotificationLabel(
                    kind="building_vessel_attached",
                    label=f"「{new.name}」が物理身体 ({new.physical_vessel_id}) と紐付きました",
                ))
            else:
                labels.append(NotificationLabel(
                    kind="building_vessel_detached",
                    label=f"「{new.name}」が物理身体との紐付けを解除されました",
                ))
        return labels

    # ---- serialize / deserialize ----

    def serialize_snapshot(self, snapshot: BuildingSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> BuildingSnapshot:
        return BuildingSnapshot(**json.loads(data))

    # ---- 内部ヘルパー ----

    def _resolve_building(self, ctx: LineHeadInput, building_id: Optional[str]):
        if not building_id:
            return None
        persona = ctx.persona
        if persona is not None:
            buildings = getattr(persona, "buildings", None)
            if isinstance(buildings, dict) and building_id in buildings:
                return buildings[building_id]
        manager = ctx.manager
        if manager is not None:
            building_map = getattr(manager, "building_map", None)
            if isinstance(building_map, dict) and building_id in building_map:
                return building_map[building_id]
        return None
