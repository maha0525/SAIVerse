"""FacilitiesSection — 「行ける場所」の一覧を head に常駐させる。

判断プロンプト (起床判断) が毎朝 tail に貼り直していた [施設一覧] の移設先
(docs/issues/judgment_static_lists_to_head.md、まはー裁定 2026-07-29)。世界に
どんな場所があるかは判断のたびに変わるものではないので、head に一度置いて
凍結し、**増減・改名だけを差分通知で届ける**。

「読む情報」と「選べる選択肢」の分離:
    ここは読む情報 (どの場所が何なのか)。時間割のコマが選べる facility の
    enum は判断側 (``saiverse.judgment_points.collect_facility_ids``) が
    live state から供給し続ける — head が凍結して古くなっても、実在しない
    場所は構造的に選べないままになる。両者が同じ候補集合を見るように、
    候補の決定は ``saiverse.facility_map.candidate_buildings`` 1 箇所に置く。

cache 安定性:
    ``refresh_on_events = frozenset()`` = Metabolism のみ再 capture。順序は
    building_id 昇順で固定 (candidate_buildings が担保) — 同じ世界なら毎回
    同じ文字列が出ることが prefix キャッシュの前提。

詳細: docs/intent/persona_cognition/judgment_points.md §4 /
docs/intent/cached_head_architecture.md §5.3
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FacilityItem:
    """行ける場所 1 件。"""
    facility_id: str            # Building ID または "own_room"
    name: str
    roles: tuple[str, ...] = ()  # 公共施設ロール (plaza / workshop / ...)


@dataclass(frozen=True)
class FacilitiesSnapshot:
    items: tuple[FacilityItem, ...] = ()


def _own_room_item() -> FacilityItem:
    from saiverse.day_plan import FACILITY_OWN_ROOM

    return FacilityItem(facility_id=FACILITY_OWN_ROOM, name="自分の部屋")


def _format_item(item: FacilityItem) -> str:
    from saiverse.facility_map import ROLE_LABELS

    label = "・".join(ROLE_LABELS.get(r, r) for r in item.roles)
    return f"- {item.facility_id}: {item.name}" + (f"（{label}）" if label else "")


class FacilitiesSection:
    name = "facilities"
    order = 310  # building(300) の直後 = 「いまここ」の次に「行ける場所」
    refresh_on_events = frozenset()  # Metabolism のみ。増減・改名は差分通知で届く

    def capture(self, ctx: LineHeadInput) -> FacilitiesSnapshot:
        manager = ctx.manager
        if manager is None:
            return FacilitiesSnapshot(items=(_own_room_item(),))

        from saiverse.facility_map import building_roles, candidate_buildings

        items: list[FacilityItem] = []
        for b in candidate_buildings(manager):
            bid = getattr(b, "building_id", None)
            if not bid:
                continue
            items.append(FacilityItem(
                facility_id=bid,
                name=getattr(b, "name", "") or bid,
                roles=tuple(building_roles(b)),
            ))
        items.append(_own_room_item())
        return FacilitiesSnapshot(items=tuple(items))

    def render(self, snapshot: FacilitiesSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.items:
            return None
        lines = [
            "## 行ける場所",
            "この世界であなたが行ける場所です。",
        ]
        lines += [_format_item(item) for item in snapshot.items]
        return RenderedSection(text="\n".join(lines))

    def diff_to_notifications(
        self,
        old: Optional[FacilitiesSnapshot],
        new: Optional[FacilitiesSnapshot],
    ) -> list[NotificationLabel]:
        # head は凍結しているので、この差分通知が「head の一覧はもう古い」を
        # 埋める唯一の経路 (issue judgment_static_lists_to_head の中核要件:
        # 「head に静的な全体像・通知に差分」の対)。
        if old is None or new is None:
            return []
        old_by_id = {i.facility_id: i for i in old.items}
        new_by_id = {i.facility_id: i for i in new.items}

        added = [new_by_id[k] for k in new_by_id if k not in old_by_id]
        removed = [old_by_id[k] for k in old_by_id if k not in new_by_id]
        renamed = [
            (old_by_id[k], new_by_id[k]) for k in new_by_id
            if k in old_by_id and old_by_id[k].name != new_by_id[k].name
        ]
        rerolled = [
            new_by_id[k] for k in new_by_id
            if k in old_by_id and old_by_id[k].roles != new_by_id[k].roles
        ]
        if not (added or removed or renamed or rerolled):
            return []

        lines = ["行ける場所が変わりました。"]
        for item in added:
            lines.append(f"増えた場所: {_format_item(item)[2:]}")
        for item in removed:
            lines.append(f"無くなった場所: {item.facility_id}「{item.name}」")
        for before, after in renamed:
            lines.append(
                f"名前が変わった場所: {after.facility_id}"
                f"「{before.name}」→「{after.name}」"
            )
        for item in rerolled:
            lines.append(f"役割が変わった場所: {_format_item(item)[2:]}")
        return [NotificationLabel(kind="facilities_changed", label="\n".join(lines))]

    def serialize_snapshot(self, snapshot: FacilitiesSnapshot) -> str:
        return json.dumps(
            {"items": [asdict(i) for i in snapshot.items]}, ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> FacilitiesSnapshot:
        payload = json.loads(data)
        return FacilitiesSnapshot(items=tuple(
            FacilityItem(
                facility_id=i["facility_id"],
                name=i.get("name") or i["facility_id"],
                roles=tuple(i.get("roles") or ()),
            )
            for i in payload.get("items", [])
        ))
