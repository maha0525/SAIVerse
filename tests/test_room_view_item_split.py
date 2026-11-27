"""``/api/info/details`` の見えるアイテム / 埋もれたアイテムの分け方の契約テスト。

設計の正典: docs/intent/screen_avatar.md 設計 5・必ず守ること 2、
docs/intent/room_item_display_cap.md 設計 1・4。

固定する契約:

- ``select_displayed_building_item_ids`` の「見える」は、同じ部屋の束
  (``build_room_bundle``) に載るアイテムと同じ集合、「埋もれた」は束の
  ``capped_keys`` と同じ集合 (選ぶ処理は一つ)。
- 二つのリストは「最近触られた順」(同時刻は SHORT_ID の降順)。
- ``item_display_limit`` は ``manager.building_map`` の Building から読む
  (``_reload_buildings`` の後に ``persona.buildings`` と別のオブジェクトに
  なっても、利用者が変えた値が効く)。``building_map`` が無ければ
  ``persona.buildings`` から読む。
- エンドポイントの返りで、``items`` の各 id は二つのリストのちょうど片方に
  一度だけ現れ、``appearance_image`` は設定のあるペルソナにだけ入る。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from builtin_data.tools import get_visual_context as gvc
from database.models import AI, Base, Building, City, User
from sai_memory.room_state import bundle_capped_keys

_BASE_TIME = datetime(2026, 11, 22, 12, 0, 0)


def _make_item(short_id, *, touched_min=0, item_id=None):
    """建物直下のアイテム 1 件 (``get_all_items_in_building`` の返りと同じ形)。"""
    return {
        "item_id": item_id or f"uuid-{short_id}",
        "short_id": short_id,
        "type": "object",
        "name": f"物{short_id}",
        "description": f"物{short_id}の説明。",
        "created_at": _BASE_TIME,
        "updated_at": _BASE_TIME + timedelta(minutes=touched_min),
    }


def _building(limit):
    return SimpleNamespace(
        name="工房", description="", base_system_instruction="",
        item_display_limit=limit,
    )


def _fake_manager(items, *, persona_building, building_map=None):
    persona = SimpleNamespace(
        persona_name="アイフィ",
        current_building_id="b1",
        buildings={"b1": persona_building},
    )
    manager = SimpleNamespace(
        saiverse_home=None,
        occupants={"b1": ["p1"]},
        all_personas={"p1": persona},
        get_all_items_in_building=lambda bid: [dict(i) for i in items],
        get_bag_contents_recursive=lambda item_id: [],
        observer_manager=None,
    )
    if building_map is not None:
        manager.building_map = building_map
    return manager


def _bundle(manager):
    with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
            patch.object(gvc, "get_active_manager", return_value=manager), \
            patch.object(gvc, "_get_building_image_path", return_value=None):
        return gvc.build_room_bundle("b1")


def _bundle_item_keys(bundle):
    return {p["key"] for p in bundle["packages"] if p.get("family") == "item"}


def _key_of(item_id):
    return "item:" + item_id[len("uuid-"):]


class SameSelectionAsBundleTest(unittest.TestCase):
    """選ぶ処理は一つ: 束と新しい関数が同じ物を見える・埋もれたに分ける。"""

    def _assert_same(self, items, limit):
        # 本物の manager と同じく building_map を持たせる (起動直後の形:
        # persona.buildings と同じオブジェクト)。
        building = _building(limit)
        manager = _fake_manager(
            items, persona_building=building, building_map={"b1": building},
        )
        bundle = _bundle(manager)
        visible, buried = gvc.select_displayed_building_item_ids(manager, "b1")
        self.assertEqual({_key_of(i) for i in visible}, _bundle_item_keys(bundle))
        self.assertEqual({_key_of(i) for i in buried}, set(bundle_capped_keys(bundle)))
        self.assertEqual(
            sorted(visible + buried), sorted(i["item_id"] for i in items),
        )
        return visible, buried

    def test_each_limit(self):
        items = [
            _make_item(n, touched_min=(n * 7) % 13) for n in range(1, 16)
        ]
        for limit, expected_visible in (
            (None, 10), (0, 0), (100, 15), (4, 4),
        ):
            with self.subTest(limit=limit):
                visible, buried = self._assert_same(items, limit)
                self.assertEqual(len(visible), expected_visible)
                self.assertEqual(len(buried), 15 - expected_visible)

    def test_empty_room(self):
        visible, buried = self._assert_same([], None)
        self.assertEqual((visible, buried), ([], []))

    def test_manager_without_item_accessor(self):
        self.assertEqual(
            gvc.select_displayed_building_item_ids(SimpleNamespace(), "b1"),
            ([], []),
        )


class OrderTest(unittest.TestCase):
    """二つのリストの並びは「最近触られた順」(同時刻は SHORT_ID の降順)。"""

    def test_recently_touched_first_and_ties_on_short_id_descending(self):
        items = [
            _make_item(1, touched_min=50),
            _make_item(2, touched_min=10),
            _make_item(3, touched_min=90),
            _make_item(4, touched_min=50),
            _make_item(5, touched_min=10),
            _make_item(6, touched_min=0),
        ]
        building = _building(3)
        manager = _fake_manager(
            items, persona_building=building, building_map={"b1": building},
        )
        visible, buried = gvc.select_displayed_building_item_ids(manager, "b1")
        self.assertEqual(visible, ["uuid-3", "uuid-4", "uuid-1"])
        self.assertEqual(buried, ["uuid-5", "uuid-2", "uuid-6"])

    def test_same_key_twice_does_not_mix_up_item_ids(self):
        """同じキーの物が二つあっても、item_id はそれぞれの物のまま返る。"""
        items = [
            _make_item(5, touched_min=0, item_id="uuid-old"),
            _make_item(5, touched_min=10, item_id="uuid-new"),
        ]
        building = _building(1)
        manager = _fake_manager(
            items, persona_building=building, building_map={"b1": building},
        )
        visible, buried = gvc.select_displayed_building_item_ids(manager, "b1")
        self.assertEqual(visible, ["uuid-new"])
        self.assertEqual(buried, ["uuid-old"])


class DisplayLimitSourceTest(unittest.TestCase):
    """item_display_limit は manager.building_map の Building から読む。"""

    def setUp(self):
        self.items = [_make_item(n, touched_min=n) for n in range(1, 8)]

    def test_building_map_wins_over_a_stale_persona_building(self):
        """_reload_buildings の後と同じ形: 二つが別のオブジェクトで値が違う。"""
        stale = _building(5)
        current = _building(2)
        self.assertIsNot(stale, current)
        manager = _fake_manager(
            self.items, persona_building=stale, building_map={"b1": current},
        )
        bundle = _bundle(manager)
        self.assertEqual(_bundle_item_keys(bundle), {"item:6", "item:7"})
        self.assertEqual(len(bundle_capped_keys(bundle)), 5)
        visible, buried = gvc.select_displayed_building_item_ids(manager, "b1")
        self.assertEqual(visible, ["uuid-7", "uuid-6"])
        self.assertEqual(len(buried), 5)
        # 部屋の名前とシステムプロンプトは今までどおり persona.buildings から読む。
        self.assertEqual(bundle["building_name"], stale.name)

    def test_falls_back_to_persona_buildings_without_building_map(self):
        manager = _fake_manager(self.items, persona_building=_building(3))
        self.assertFalse(hasattr(manager, "building_map"))
        bundle = _bundle(manager)
        self.assertEqual(
            _bundle_item_keys(bundle), {"item:5", "item:6", "item:7"},
        )
        # persona の無い呼び出しでは既定の 10 個 (7 個なので全部見える)。
        visible, buried = gvc.select_displayed_building_item_ids(manager, "b1")
        self.assertEqual(len(visible), 7)
        self.assertEqual(buried, [])

    def test_building_map_without_the_building_falls_back(self):
        manager = _fake_manager(
            self.items, persona_building=_building(3),
            building_map={"other": _building(1)},
        )
        self.assertEqual(
            _bundle_item_keys(_bundle(manager)), {"item:5", "item:6", "item:7"},
        )


class DetailsEndpointTest(unittest.TestCase):
    """エンドポイントの返りの性質と appearance_image。"""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITY_SLUG="city_a", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(Building(CITYID=city.CITYID, BUILDINGID="b1", BUILDINGNAME="工房"))
            db.add(AI(
                AIID="air", HOME_CITYID=city.CITYID, AINAME="エア",
                APPEARANCE_IMAGE_PATH="builtin_data/icons/air_full.png",
            ))
            db.add(AI(AIID="eris", HOME_CITYID=city.CITYID, AINAME="エリス"))
            db.commit()
        finally:
            db.close()

        patcher = patch("database.session.SessionLocal", self.Session)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.items = [_make_item(n, touched_min=n) for n in range(1, 14)]
        self.building = _building(None)
        self.manager = self._manager(self.items)

    def _manager(self, items, **overrides):
        by_id = {i["item_id"]: i for i in items}
        personas = {
            "air": SimpleNamespace(
                persona_name="エア", avatar_image=None, autonomy_enabled=True,
            ),
            "eris": SimpleNamespace(
                persona_name="エリス", avatar_image=None, autonomy_enabled=False,
            ),
        }
        fields = dict(
            personas=personas,
            occupancy_manager=SimpleNamespace(
                occupants={"b1": ["air", "eris", "1"]},
            ),
            building_map={"b1": self.building},
            state=SimpleNamespace(
                user_id=1, user_display_name="まはー", user_avatar_data=None,
            ),
            item_registry=dict(by_id),
            items=dict(by_id),
            items_by_building={"b1": list(by_id)},
            item_service=SimpleNamespace(
                get_items_inside_bags_in_building=lambda bid: set(),
            ),
            get_all_items_in_building=lambda bid: [dict(i) for i in items],
        )
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def _details(self, manager=None):
        from api.routes.info import BuildingDetailsResponse, get_building_details

        resp = get_building_details(building_id="b1", manager=manager or self.manager)
        BuildingDetailsResponse.model_validate(resp)
        return resp

    def _assert_partition(self, resp):
        ids = [i["id"] for i in resp["items"]]
        both = resp["visible_item_ids"] + resp["buried_item_ids"]
        self.assertEqual(sorted(both), sorted(ids))
        self.assertEqual(len(both), len(set(both)))

    def test_items_are_split_into_visible_and_buried(self):
        resp = self._details()
        self._assert_partition(resp)
        self.assertEqual(
            resp["visible_item_ids"], [f"uuid-{n}" for n in range(13, 3, -1)],
        )
        self.assertEqual(resp["buried_item_ids"], ["uuid-3", "uuid-2", "uuid-1"])
        # items 自体は全件・今の並びのまま。
        self.assertEqual(len(resp["items"]), 13)
        self.assertEqual(
            [i["id"] for i in resp["items"]],
            sorted(i["item_id"] for i in self.items),
        )

    def test_limit_change_on_building_map_is_reflected(self):
        self.building.item_display_limit = 0
        resp = self._details()
        self._assert_partition(resp)
        self.assertEqual(resp["visible_item_ids"], [])
        self.assertEqual(len(resp["buried_item_ids"]), 13)

    def test_selection_failure_returns_everything_as_visible(self):
        def _boom(bid):
            raise RuntimeError("boom")

        manager = self._manager(self.items, get_all_items_in_building=_boom)
        with self.assertLogs("api.routes.info", level="WARNING"):
            resp = self._details(manager)
        self._assert_partition(resp)
        self.assertEqual(resp["buried_item_ids"], [])
        self.assertEqual(len(resp["visible_item_ids"]), 13)

    def test_ids_missing_from_selection_go_to_the_end_of_buried(self):
        """選別の返りに無い id も消えずに buried_item_ids の末尾に付く。"""
        listed_only = _make_item(99, touched_min=999, item_id="uuid-listed-only")
        selected = [_make_item(1, touched_min=1), _make_item(2, touched_min=2)]
        stray = _make_item(50, touched_min=500, item_id="uuid-stray")
        by_id = {i["item_id"]: i for i in selected + [listed_only]}
        manager = self._manager(
            selected + [listed_only],
            get_all_items_in_building=lambda bid: [
                dict(i) for i in selected + [stray]
            ],
        )
        manager.items_by_building = {"b1": list(by_id)}
        with self.assertLogs("api.routes.info", level="WARNING"):
            resp = self._details(manager)
        self._assert_partition(resp)
        self.assertNotIn("uuid-stray", resp["visible_item_ids"] + resp["buried_item_ids"])
        self.assertEqual(resp["buried_item_ids"][-1], "uuid-listed-only")

    def test_unknown_building_returns_empty_lists(self):
        from api.routes.info import get_building_details

        resp = get_building_details(building_id="nowhere", manager=self.manager)
        self.assertEqual(resp["visible_item_ids"], [])
        self.assertEqual(resp["buried_item_ids"], [])

    def test_appearance_image_only_for_personas_with_one(self):
        resp = self._details()
        by_id = {o["id"]: o for o in resp["occupants"]}
        self.assertEqual(
            by_id["air"]["appearance_image"], "/api/static/builtin_icons/air_full.png",
        )
        self.assertIsNone(by_id["eris"]["appearance_image"])
        self.assertEqual(len(resp["users"]), 1)
        self.assertNotIn("appearance_image", resp["users"][0])


if __name__ == "__main__":
    unittest.main()
