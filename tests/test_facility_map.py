"""公共施設タグと型→施設の解決 (saiverse/facility_map.py、自律行動 v2 §6.1)。

- resolve_facility: 六型それぞれのロール解決 / 「自分を更新する」は常に own_room /
  該当施設が無い型・六型でない kind は None / 複数候補は building_id 昇順の先頭
  (決定論) / 複数ロール Building
- collect_facility_ids: ロールタグ付き Building があればそれのみ + own_room、
  タグゼロの DB では全 Building にフォールバック (後方互換)
- 状況テキストの施設一覧が enum と同じ候補集合 + ロールの日本語ラベルを出す
- runtime Building (saiverse/buildings.py) が facility_roles を保持する
"""
from __future__ import annotations

from types import SimpleNamespace

from saiverse import judgment_points as jp
from saiverse.buildings import Building
from saiverse.day_plan import (
    FACILITY_OWN_ROOM,
    KIND_CREATE,
    KIND_EXPERIENCE,
    KIND_LEARN,
    KIND_LISTEN,
    KIND_LIVING,
    KIND_REST,
    KIND_SELF_UPDATE,
    KIND_TALK,
)
from saiverse.facility_map import (
    FACILITY_ROLE_VOCAB,
    KIND_TO_ROLE,
    ROLE_LIBRARY,
    building_roles,
    buildings_for_role,
    list_tagged_buildings,
    resolve_facility,
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
# resolve_facility: 型別解決
# ---------------------------------------------------------------------------


def test_resolve_facility_by_kind():
    manager = _manager(TAGGED)
    assert resolve_facility(manager, KIND_TALK) == "cafe"
    assert resolve_facility(manager, KIND_LISTEN) == "cafe"  # 話す/聞く は同じ広場
    assert resolve_facility(manager, KIND_CREATE) == "atelier"
    assert resolve_facility(manager, KIND_LEARN) == "archive"
    assert resolve_facility(manager, KIND_EXPERIENCE) == "green"


def test_self_update_always_resolves_to_own_room():
    # タグ付き Building がゼロでも own_room (私的な営みは Building タグで表現しない)
    assert resolve_facility(_manager([]), KIND_SELF_UPDATE) == FACILITY_OWN_ROOM
    assert resolve_facility(_manager(TAGGED), KIND_SELF_UPDATE) == FACILITY_OWN_ROOM


def test_resolve_facility_none_when_role_untagged():
    # park タグの Building が無い → 経験する は None (呼び出し側が own_room フォールバック)
    manager = _manager([_b("archive", ["library"])])
    assert resolve_facility(manager, KIND_EXPERIENCE) is None
    assert resolve_facility(manager, KIND_CREATE) is None
    assert resolve_facility(manager, KIND_LEARN) == "archive"


def test_resolve_facility_non_six_kind_returns_none():
    manager = _manager(TAGGED)
    assert resolve_facility(manager, KIND_LIVING) is None
    assert resolve_facility(manager, KIND_REST) is None
    assert resolve_facility(manager, "未知の型") is None


def test_resolve_facility_deterministic_first_by_building_id():
    manager = _manager([
        _b("lib_b", ["library"]),
        _b("lib_a", ["library"]),
        _b("lib_c", ["library"]),
    ])
    # manager.buildings の並び順に依らず building_id 昇順の先頭
    assert resolve_facility(manager, KIND_LEARN) == "lib_a"


def test_multi_role_building_serves_multiple_kinds():
    manager = _manager([_b("commons", ["plaza", "park"])])
    assert resolve_facility(manager, KIND_TALK) == "commons"
    assert resolve_facility(manager, KIND_EXPERIENCE) == "commons"
    assert resolve_facility(manager, KIND_CREATE) is None


def test_kind_to_role_covers_five_kinds_with_known_vocab():
    # 自分を更新する 以外の五型が語彙内のロールへ対応する
    assert set(KIND_TO_ROLE) == {
        KIND_TALK, KIND_LISTEN, KIND_CREATE, KIND_LEARN, KIND_EXPERIENCE,
    }
    assert set(KIND_TO_ROLE.values()) <= set(FACILITY_ROLE_VOCAB)


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
    assert buildings_for_role(manager, ROLE_LIBRARY) == []


# ---------------------------------------------------------------------------
# collect_facility_ids: タグ優先 + タグ無し DB フォールバック (後方互換)
# ---------------------------------------------------------------------------


def test_collect_facility_ids_prefers_tagged_buildings():
    manager = _manager(TAGGED)
    ids = jp.collect_facility_ids(manager)
    # タグ付きのみ (building_id 昇順) + own_room。タグ無し 'plain' は載らない
    assert ids == ["archive", "atelier", "cafe", "green", FACILITY_OWN_ROOM]


def test_collect_facility_ids_falls_back_to_all_when_untagged():
    # まだ誰もタグ付けしていない DB では従来どおり全 Building を提示する
    manager = _manager([
        SimpleNamespace(building_id="library", name="図書館"),  # facility_roles 属性なし
        _b("workshop", []),
    ])
    assert jp.collect_facility_ids(manager) == ["library", "workshop", FACILITY_OWN_ROOM]


def test_head_facilities_section_matches_enum_and_labels_roles():
    """head の「行ける場所」が enum (collect_facility_ids) と同じ候補集合を出す。

    一覧は 2026-07-30 に判断プロンプトの tail から head へ移設した
    (docs/issues/judgment_static_lists_to_head.md)。読む情報 (head) と
    選べる選択肢 (enum) が同じ集合を見ることが移設の前提条件。
    """
    from sea.head_pipeline.sections.facilities import FacilitiesSection

    section = FacilitiesSection()
    manager = _manager(TAGGED)
    ctx = SimpleNamespace(persona_id="air", manager=manager)
    text = section.render(section.capture(ctx)).text
    assert "- archive: 図書館（図書館）" in text
    assert "- cafe: カフェ（広場）" in text
    assert "plain" not in text  # enum と同じ候補集合 (タグ無しは載らない)
    assert f"- {FACILITY_OWN_ROOM}: 自分の部屋" in text
    # 一覧に出る id の集合が enum と一致する
    listed = [
        line[2:].split(":")[0] for line in text.splitlines() if line.startswith("- ")
    ]
    assert listed == jp.collect_facility_ids(manager)

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
