"""公共施設タグと「行ける場所」の候補集合 (saiverse/facility_map.py、自律行動 v2 §6.1)。

- candidate_buildings: ロールタグ付き Building があればそれのみ、タグゼロの DB
  では全 Building にフォールバック (後方互換)
- head の「行ける場所」が同じ候補集合 + own_room + ロールの日本語ラベルを出す
- runtime Building (saiverse/buildings.py) が facility_roles を保持する

型 (六型) から施設への解決と、時間割の facility enum は時間割の撤去
(v0.4 段 1-4) で消えた。
"""
from __future__ import annotations

from types import SimpleNamespace

from saiverse.buildings import Building
from saiverse.facility_map import (
    FACILITY_OWN_ROOM,
    building_roles,
    candidate_buildings,
    list_tagged_buildings,
)


def _b(building_id: str, roles=None, name: str = ""):
    return SimpleNamespace(
        building_id=building_id, name=name or building_id, facility_roles=roles or [],
    )


def _manager(buildings):
    return SimpleNamespace(buildings=buildings)


TAGGED = [
    _b("cafe", ["plaza"], name="カフェ"),
    _b("atelier", ["workshop"], name="工房"),
    _b("archive", ["library"], name="図書館"),
    _b("green", ["park"], name="公園"),
    _b("plain", None, name="ただの部屋"),  # タグなし
]


# ---------------------------------------------------------------------------
# building_roles / list_tagged_buildings の頑健性
# ---------------------------------------------------------------------------


def test_building_roles_is_robust_to_stub_objects():
    assert building_roles(SimpleNamespace()) == []  # 属性なし
    assert building_roles(SimpleNamespace(facility_roles="library")) == []  # list でない
    assert building_roles(SimpleNamespace(facility_roles=[1, "library", ""])) == ["library"]


def test_list_tagged_and_role_filter_sorted():
    manager = _manager([
        _b("z_plaza", ["plaza"]),
        _b("a_plaza", ["plaza"]),
        _b("plain"),
    ])
    assert [b.building_id for b in list_tagged_buildings(manager)] == ["a_plaza", "z_plaza"]


# ---------------------------------------------------------------------------
# candidate_buildings: タグ優先 + タグ無し DB フォールバック (後方互換)
# ---------------------------------------------------------------------------


def test_candidate_buildings_prefers_tagged_buildings():
    manager = _manager(TAGGED)
    ids = [b.building_id for b in candidate_buildings(manager)]
    # タグ付きのみ (building_id 昇順)。タグ無し 'plain' は載らない
    assert ids == ["archive", "atelier", "cafe", "green"]


def test_candidate_buildings_falls_back_to_all_when_untagged():
    # まだ誰もタグ付けしていない DB では従来どおり全 Building を提示する
    manager = _manager([
        SimpleNamespace(building_id="library", name="図書館"),  # facility_roles 属性なし
        _b("workshop", []),
    ])
    assert [b.building_id for b in candidate_buildings(manager)] == ["library", "workshop"]


def test_head_facilities_section_matches_enum_and_labels_roles():
    """head の「行ける場所」が candidate_buildings の候補集合 + own_room を出す。

    一覧は 2026-07-30 に判断プロンプトの tail から head へ移設した
    (docs/issues/judgment_static_lists_to_head.md)。
    """
    from sea.head_pipeline.sections.facilities import FacilitiesSection

    section = FacilitiesSection()
    manager = _manager(TAGGED)
    ctx = SimpleNamespace(persona_id="air", manager=manager)
    text = section.render(section.capture(ctx)).text
    assert "- archive: 図書館（図書館）" in text
    assert "- cafe: カフェ（広場）" in text
    assert "plain" not in text  # 候補集合 (タグ無しは載らない)
    assert f"- {FACILITY_OWN_ROOM}: 自分の部屋" in text
    # 一覧に出る id の集合が候補集合 + own_room と一致する
    listed = [
        line[2:].split(":")[0] for line in text.splitlines() if line.startswith("- ")
    ]
    assert listed == [
        b.building_id for b in candidate_buildings(manager)
    ] + [FACILITY_OWN_ROOM]

    untagged = _manager([SimpleNamespace(building_id="b1", name="部屋")])
    text2 = section.render(
        section.capture(SimpleNamespace(persona_id="air", manager=untagged))
    ).text
    assert "- b1: 部屋" in text2


def test_head_facilities_section_notifies_changes():
    """head は凍結されるので、場所の増減・改名は差分通知で届く必要がある。

    「head に静的な全体像・通知に差分」の対 — 通知が無ければ、head の一覧は
    無くなった場所を載せ続け、新しい場所を隠し続ける。
    """
    from sea.head_pipeline.sections.facilities import FacilitiesSection

    section = FacilitiesSection()

    def snap(buildings):
        return section.capture(
            SimpleNamespace(persona_id="air", manager=_manager(buildings))
        )

    before = snap([_b("cafe", ["plaza"])])
    assert section.diff_to_notifications(before, before) == []

    added = snap([_b("cafe", ["plaza"]), _b("archive", ["library"])])
    labels = section.diff_to_notifications(before, added)
    assert len(labels) == 1 and labels[0].kind == "facilities_changed"
    assert "増えた場所: archive" in labels[0].label

    removed = section.diff_to_notifications(added, before)
    assert "無くなった場所: archive" in removed[0].label

    renamed = snap([SimpleNamespace(
        building_id="cafe", name="喫茶室", facility_roles=["plaza"],
    )])
    assert "名前が変わった場所: cafe" in section.diff_to_notifications(
        before, renamed,
    )[0].label


# ---------------------------------------------------------------------------
# runtime Building が facility_roles を保持する
# ---------------------------------------------------------------------------


def test_runtime_building_holds_facility_roles():
    b = Building(building_id="lib", name="図書館", facility_roles=["library"])
    assert b.facility_roles == ["library"]
    assert building_roles(b) == ["library"]
    # 省略時は空リスト (ロールなし)
    assert Building(building_id="room", name="部屋").facility_roles == []
