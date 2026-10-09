"""公共施設のロールタグと「行ける場所」の候補集合 (自律行動 v2 §6.1 の残り)。

Building のロールタグ (``database/models.py`` Building.FACILITY_ROLES、JSON
配列) が「その Building が何の施設か」を表す。本モジュールはタグの読み口と、
head の「行ける場所」(``sea.head_pipeline.sections.facilities``) が提示する
候補集合を一箇所で決める。

ロール語彙 (``FACILITY_ROLE_VOCAB``):

- ``plaza``    広場
- ``workshop`` 工房
- ``library``  図書館
- ``park``     公園

v2 の時間割では欲求の六型 (話す/聞く/作る/知る/経験する/自分を更新する) から
ロール経由でコマの行き先を解決していた (``resolve_facility``)。時間割の撤去
(autonomous_behavior_v04_plan.md 段 1-4) で型からの解決は読み手ごと消えた。

タグ付けの UI/CLI は将来フェーズ。当面は手動 SQL で付与する::

    sqlite3 ~/.saiverse/user_data/database/saiverse.db \\
      "UPDATE building SET FACILITY_ROLES='[\\"library\\"]' WHERE BUILDINGID='<building_id>';"

1 Building 複数ロール可 (例: ``'["plaza", "park"]'``)。タグを外すには::

    sqlite3 ~/.saiverse/user_data/database/saiverse.db \\
      "UPDATE building SET FACILITY_ROLES=NULL WHERE BUILDINGID='<building_id>';"

Building はメモリにロードされるため、SQL での変更はアプリ再起動で反映される。
既存ユーザーの Building 構成に勝手にタグを付けるシードはしない (v2 §10-6) —
タグがゼロの DB では :func:`candidate_buildings` が全 Building を提示する
後方互換にフォールバックする。
"""
from __future__ import annotations

import logging
from typing import Any, List

LOGGER = logging.getLogger(__name__)

#: 自室を指す施設 ID (Building ID ではない特別値)。head の「行ける場所」の
#: 末尾に「自分の部屋」として常に載る。
FACILITY_OWN_ROOM = "own_room"

# ロール語彙 (Building.FACILITY_ROLES に入る値)
ROLE_PLAZA = "plaza"
ROLE_WORKSHOP = "workshop"
ROLE_LIBRARY = "library"
ROLE_PARK = "park"

FACILITY_ROLE_VOCAB = (ROLE_PLAZA, ROLE_WORKSHOP, ROLE_LIBRARY, ROLE_PARK)

#: 表示用の日本語ラベル (状況テキスト等)
ROLE_LABELS = {
    ROLE_PLAZA: "広場",
    ROLE_WORKSHOP: "工房",
    ROLE_LIBRARY: "図書館",
    ROLE_PARK: "公園",
}


def building_roles(building: Any) -> List[str]:
    """Building オブジェクトのロールタグ (無ければ空リスト)。

    runtime Building (``saiverse/buildings.py``) の ``facility_roles`` 属性を
    読む。属性が無い / list でないオブジェクトにも安全 (テストスタブ等)。
    """
    roles = getattr(building, "facility_roles", None)
    if not isinstance(roles, list):
        return []
    return [r for r in roles if isinstance(r, str) and r.strip()]


def list_tagged_buildings(manager: Any) -> List[Any]:
    """ロールタグ付き Building の一覧 (building_id 昇順の決定論順)。"""
    tagged = [
        b for b in (getattr(manager, "buildings", None) or [])
        if getattr(b, "building_id", None) and building_roles(b)
    ]
    tagged.sort(key=lambda b: b.building_id)
    return tagged


def candidate_buildings(manager: Any) -> List[Any]:
    """施設として提示する Building 群 (building_id 昇順の決定論順)。

    公共施設タグ (FACILITY_ROLES) 付き Building が 1 つでもあればそれのみ、
    ゼロなら後方互換で全 Building (まだ誰もタグ付けしていない DB で従来挙動を
    壊さない — v2 §6.1)。

    供給先は head の「行ける場所」(``sea.head_pipeline.sections.facilities``)。
    候補集合はここ 1 箇所で決める。

    順序を building_id で固定するのは head の prefix キャッシュのため
    (同じ世界なら毎回同じ文字列が出ること)。
    """
    tagged = list_tagged_buildings(manager)
    if tagged:
        return tagged
    all_buildings = [
        b for b in (getattr(manager, "buildings", None) or [])
        if getattr(b, "building_id", None)
    ]
    all_buildings.sort(key=lambda b: b.building_id)
    return all_buildings
