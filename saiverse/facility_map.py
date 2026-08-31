"""型 → 公共施設の解決 (自律行動 v2 §6.1)。

欲求の六型が行き先 (公共 Building) を決めるためのマッピング層。Building の
ロールタグ (``database/models.py`` Building.FACILITY_ROLES、JSON 配列) が
「その Building が何の施設か」を表し、本モジュールが型からロール経由で
実在の Building へ解決する。

ロール語彙 (``FACILITY_ROLE_VOCAB``):

- ``plaza``    広場: 「話す」「聞く」(社交型の欲求を持つペルソナが集まる)
- ``workshop`` 工房: 「作る」(成果物が公共空間に置かれ、他者の目に入る)
- ``library``  図書館: 「知る」(Web 閲覧室 + 他ペルソナの document の書架)
- ``park``     公園: 「経験する」(出来事との遭遇)

「自分を更新する」だけは私的な営みであり、常に自室 (``own_room``) に解決する
(Building タグでは表現しない)。該当施設が無い型は None を返し、呼び出し側が
own_room へのフォールバック + WARN を行う。

タグ付けの UI/CLI は将来フェーズ。当面は手動 SQL で付与する::

    sqlite3 ~/.saiverse/user_data/database/saiverse.db \\
      "UPDATE building SET FACILITY_ROLES='[\\"library\\"]' WHERE BUILDINGID='<building_id>';"

1 Building 複数ロール可 (例: ``'["plaza", "park"]'``)。タグを外すには::

    sqlite3 ~/.saiverse/user_data/database/saiverse.db \\
      "UPDATE building SET FACILITY_ROLES=NULL WHERE BUILDINGID='<building_id>';"

Building はメモリにロードされるため、SQL での変更はアプリ再起動で反映される。
既存ユーザーの Building 構成に勝手にタグを付けるシードはしない (v2 §10-6) —
タグがゼロの DB では ``saiverse.judgment_points.collect_facility_ids`` が
従来どおり全 Building を提示する後方互換にフォールバックする。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from saiverse.day_plan import (
    FACILITY_OWN_ROOM,
    KIND_CREATE,
    KIND_EXPERIENCE,
    KIND_LEARN,
    KIND_LISTEN,
    KIND_SELF_UPDATE,
    KIND_TALK,
)

LOGGER = logging.getLogger(__name__)

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

#: 六型 → ロールの対応 (v2 §6.1 の表)。「自分を更新する」はロールを持たず
#: 常に own_room (resolve_facility 内の特別扱い)。
KIND_TO_ROLE = {
    KIND_TALK: ROLE_PLAZA,
    KIND_LISTEN: ROLE_PLAZA,
    KIND_CREATE: ROLE_WORKSHOP,
    KIND_LEARN: ROLE_LIBRARY,
    KIND_EXPERIENCE: ROLE_PARK,
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

    **供給先は 2 つあり、両方が同じ集合を見る必要がある**:
    ``saiverse.judgment_points.collect_facility_ids`` (コマの facility enum =
    選べる選択肢) と ``sea.head_pipeline.sections.facilities`` (head の
    「行ける場所」= 読む情報)。片方だけ別実装にすると「head に無い場所が
    enum にある / その逆」が起きるため、候補集合はここ 1 箇所で決める。

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


def buildings_for_role(manager: Any, role: str) -> List[Any]:
    """指定ロールを持つ Building の一覧 (building_id 昇順の決定論順)。"""
    return [b for b in list_tagged_buildings(manager) if role in building_roles(b)]


def resolve_facility(manager: Any, kind: str) -> Optional[str]:
    """欲求の型 (六型) から行き先の Building ID を解決する。

    - 「自分を更新する」 → 常に ``"own_room"`` (私的な営み)
    - その他の六型 → ロール (``KIND_TO_ROLE``) を持つ Building のうち
      building_id 昇順の先頭 (複数あれば決定論で先頭)
    - 該当施設が無い型 / 六型でない kind (暮らし・休む等) → None。
      呼び出し側が own_room フォールバック + WARN を行うこと。
    """
    if kind == KIND_SELF_UPDATE:
        return FACILITY_OWN_ROOM
    role = KIND_TO_ROLE.get(kind)
    if role is None:
        return None
    candidates = buildings_for_role(manager, role)
    if not candidates:
        LOGGER.debug(
            "[facility_map] no building tagged with role=%r for kind=%r", role, kind,
        )
        return None
    return candidates[0].building_id
