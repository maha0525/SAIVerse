"""BuildingItemsSection — Building 内アイテム + ペルソナのインベントリの差分検知。

旧 ``DynamicStateManager`` の items 差分計算ロジックを Section interface に
移植したもの。head には何も render しない (= visual_context が item の見せ方を
担当している)。差分が出たら ``diff_to_notifications`` で末尾通知ラベルを返す。

snapshot は ItemEntry tuple。``capture`` は manager.item_service の in-memory
キャッシュを参照する。

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
class ItemEntry:
    item_id: str
    name: str
    item_type: str
    slot: str           # "b:3" (building slot) or "i:2" (inventory slot)


@dataclass(frozen=True)
class BuildingItemsSnapshot:
    building_id: Optional[str]
    entries: tuple[ItemEntry, ...]


class BuildingItemsSection:
    name = "building_items"
    order = 1000   # head 描画は無いので order は他より後ろ
    refresh_on_events = frozenset({EventType.BUILDING_ENTERED})

    def capture(self, ctx: LineHeadInput) -> BuildingItemsSnapshot:
        manager = ctx.manager
        building_id = ctx.current_building_id
        persona_id = ctx.persona_id
        if manager is None or building_id is None:
            return BuildingItemsSnapshot(building_id=building_id, entries=())

        item_service = getattr(manager, "item_service", None)
        if item_service is None:
            return BuildingItemsSnapshot(building_id=building_id, entries=())

        entries: list[ItemEntry] = []
        # Building 内アイテム (slot prefix "b:")
        building_item_ids = list(getattr(item_service, "items_by_building", {}).get(building_id, []))
        items_map = getattr(item_service, "items", {})
        locations_map = getattr(item_service, "item_locations", {})
        for item_id in building_item_ids:
            item_data = items_map.get(item_id)
            if not item_data:
                continue
            loc = locations_map.get(item_id, {})
            slot_num = loc.get("slot_number")
            slot = f"b:{slot_num}" if slot_num is not None else "b:?"
            entries.append(ItemEntry(
                item_id=item_id,
                name=item_data.get("name", "") or "",
                item_type=item_data.get("type", "object") or "object",
                slot=slot,
            ))

        # ペルソナのインベントリ (slot prefix "i:")
        if persona_id:
            inv_item_ids = list(getattr(item_service, "items_by_persona", {}).get(persona_id, []))
            for item_id in inv_item_ids:
                item_data = items_map.get(item_id)
                if not item_data:
                    continue
                loc = locations_map.get(item_id, {})
                slot_num = loc.get("slot_number")
                slot = f"i:{slot_num}" if slot_num is not None else "i:?"
                entries.append(ItemEntry(
                    item_id=item_id,
                    name=item_data.get("name", "") or "",
                    item_type=item_data.get("type", "object") or "object",
                    slot=slot,
                ))

        entries.sort(key=lambda e: (e.slot, e.item_id))
        return BuildingItemsSnapshot(
            building_id=building_id, entries=tuple(entries),
        )

    def render(self, snapshot: BuildingItemsSnapshot) -> Optional[RenderedSection]:
        # head には何も載せない (visual_context が表示を担当)
        return None

    def diff_to_notifications(
        self,
        old: Optional[BuildingItemsSnapshot],
        new: Optional[BuildingItemsSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        if old.building_id != new.building_id:
            # Building が違う = 別 snapshot として扱う。アイテム差分は出さない
            # (= 旧 dynamic_state は building 単位の baseline を持っていたため、
            # 移動先 building の差分はそちらで判定される)。
            return []

        labels: list[NotificationLabel] = []
        old_items = {e.item_id: e for e in old.entries}
        new_items = {e.item_id: e for e in new.entries}

        for item_id, c_item in new_items.items():
            if item_id not in old_items:
                labels.append(NotificationLabel(
                    kind="item_added",
                    label=f"アイテム「{c_item.name}」({c_item.slot}) が追加されました",
                ))
            elif old_items[item_id].name != c_item.name:
                labels.append(NotificationLabel(
                    kind="item_renamed",
                    label=f"アイテム「{old_items[item_id].name}」が「{c_item.name}」に名前変更されました",
                ))
            elif old_items[item_id].slot != c_item.slot:
                labels.append(NotificationLabel(
                    kind="item_moved",
                    label=f"アイテム「{c_item.name}」が {old_items[item_id].slot} から {c_item.slot} へ移動されました",
                ))

        for item_id, b_item in old_items.items():
            if item_id not in new_items:
                labels.append(NotificationLabel(
                    kind="item_removed",
                    label=f"アイテム「{b_item.name}」({b_item.slot}) が削除されました",
                ))

        return labels

    def serialize_snapshot(self, snapshot: BuildingItemsSnapshot) -> str:
        return json.dumps(
            {
                "building_id": snapshot.building_id,
                "entries": [asdict(e) for e in snapshot.entries],
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> BuildingItemsSnapshot:
        payload = json.loads(data)
        entries = tuple(ItemEntry(**e) for e in payload.get("entries", []))
        return BuildingItemsSnapshot(
            building_id=payload.get("building_id"),
            entries=entries,
        )
