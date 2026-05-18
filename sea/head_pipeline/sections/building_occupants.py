"""BuildingOccupantsSection — Building 内の他ペルソナ/ユーザーの入退室差分検知。

旧 ``DynamicStateManager`` の occupants 差分計算ロジックを Section interface に
移植。head には何も render しない (= visual_context が ## ペルソナ / ## ユーザー
の表示を担当)。

注意: ``OccupancyManager.move_entity`` は移動の度に host メッセージとして
"X が Y から入室しました" を building_histories に書き込んでおり、auto_ingest を
経由してペルソナの SAIMemory にも届く (= 別経路の通知)。本 Section の diff は
Pulse 開始時の **「自分が居ない間に起きた入退室をまとめて知る」** 経路として
機能する (= 旧 dynamic_state と同じ役割)。

詳細: docs/intent/cached_head_architecture.md §5.1
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
class OccupantEntry:
    occupant_id: str
    name: str
    kind: str       # "persona" | "user"


@dataclass(frozen=True)
class BuildingOccupantsSnapshot:
    building_id: Optional[str]
    entries: tuple[OccupantEntry, ...]


class BuildingOccupantsSection:
    name = "building_occupants"
    order = 1100
    refresh_on_events = frozenset({EventType.BUILDING_ENTERED})

    def capture(self, ctx: LineHeadInput) -> BuildingOccupantsSnapshot:
        manager = ctx.manager
        building_id = ctx.current_building_id
        persona_id = ctx.persona_id
        if manager is None or building_id is None:
            return BuildingOccupantsSnapshot(building_id=building_id, entries=())

        raw_occupants = list(getattr(manager, "occupants", {}).get(building_id, []))
        persona_ids = set(getattr(manager, "personas", {}).keys())
        id_to_name = getattr(manager, "id_to_name_map", {})

        entries: list[OccupantEntry] = []
        for oid in raw_occupants:
            if oid == persona_id:
                continue  # 自分自身は除外
            name = id_to_name.get(str(oid), str(oid))
            kind = "persona" if oid in persona_ids else "user"
            entries.append(OccupantEntry(occupant_id=str(oid), name=name, kind=kind))

        entries.sort(key=lambda e: (e.kind, e.occupant_id))
        return BuildingOccupantsSnapshot(
            building_id=building_id, entries=tuple(entries),
        )

    def render(self, snapshot: BuildingOccupantsSnapshot) -> Optional[RenderedSection]:
        # head には何も載せない (visual_context が描画担当)
        return None

    def diff_to_notifications(
        self,
        old: Optional[BuildingOccupantsSnapshot],
        new: Optional[BuildingOccupantsSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        if old.building_id != new.building_id:
            return []
        labels: list[NotificationLabel] = []
        old_map = {e.occupant_id: e for e in old.entries}
        new_map = {e.occupant_id: e for e in new.entries}
        for oid, entry in new_map.items():
            if oid not in old_map:
                labels.append(NotificationLabel(
                    kind="occupant_entered",
                    label=f"{entry.name} が入室しました",
                ))
        for oid, entry in old_map.items():
            if oid not in new_map:
                labels.append(NotificationLabel(
                    kind="occupant_left",
                    label=f"{entry.name} が退室しました",
                ))
        return labels

    def serialize_snapshot(self, snapshot: BuildingOccupantsSnapshot) -> str:
        return json.dumps(
            {
                "building_id": snapshot.building_id,
                "entries": [asdict(e) for e in snapshot.entries],
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> BuildingOccupantsSnapshot:
        payload = json.loads(data)
        entries = tuple(OccupantEntry(**e) for e in payload.get("entries", []))
        return BuildingOccupantsSnapshot(
            building_id=payload.get("building_id"),
            entries=entries,
        )
