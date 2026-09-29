"""入れ物 (bag) の削除 — 中身を出して消す / 中身をすべて消す。

- AdminService.delete_item: 直接の中身を入れ物があった場所 (部屋 / ペルソナの
  持ち物 / 外側の入れ物 / どこにも置かない) へ出してから入れ物を消す。
- AdminService.delete_bag_contents: 入れ子の中身まで含めて全部消し、入れ物は残す。
- API: DELETE /api/world/items/{id} と DELETE /api/world/items/{id}/contents

DB は隔離した file sqlite (TestClient はワーカースレッドでルートを実行するため)。
manager は SimpleNamespace + 実物の ItemService (キャッシュの再読込まで確かめる)。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import Base, Item, ItemLocation
from manager.admin import AdminService
from manager.items import ItemService


class BagDeleteTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_file = Path(self._tmp.name) / "saiverse_test.db"
        self.engine = create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            buildings=[],
            building_map={},
            personas={},
        )
        self.item_service = ItemService(self.manager, SimpleNamespace())
        self.manager.item_service = self.item_service
        self.manager._load_items_from_db = self.item_service.load_items_from_db

        self.svc = AdminService.__new__(AdminService)
        self.svc.SessionLocal = self.Session
        self.svc.manager = self.manager
        self.manager.delete_item = self.svc.delete_item
        self.manager.delete_bag_contents = self.svc.delete_bag_contents

    # --- helpers ---

    def _add(self, item_id, owner_kind=None, owner_id=None, slot=None, item_type="object"):
        db = self.Session()
        try:
            db.add(Item(ITEM_ID=item_id, NAME=f"{item_id}-name", TYPE=item_type, DESCRIPTION=""))
            if owner_kind is not None:
                db.add(ItemLocation(
                    ITEM_ID=item_id, OWNER_KIND=owner_kind, OWNER_ID=owner_id, SLOT_NUMBER=slot,
                ))
            db.commit()
        finally:
            db.close()
        self.item_service.load_items_from_db()

    def _loc(self, item_id):
        db = self.Session()
        try:
            row = db.query(ItemLocation).filter(ItemLocation.ITEM_ID == item_id).first()
            return (row.OWNER_KIND, row.OWNER_ID, row.SLOT_NUMBER) if row else None
        finally:
            db.close()

    def _exists(self, item_id):
        db = self.Session()
        try:
            return db.query(Item).filter(Item.ITEM_ID == item_id).first() is not None
        finally:
            db.close()

    def _count(self, model):
        db = self.Session()
        try:
            return db.query(model).count()
        finally:
            db.close()

    def _orphan_locations(self):
        """アイテム本体が無い置き場所の行と、消えた入れ物を指す置き場所の行。"""
        db = self.Session()
        try:
            item_ids = {r.ITEM_ID for r in db.query(Item).all()}
            bad = []
            for loc in db.query(ItemLocation).all():
                if loc.ITEM_ID not in item_ids:
                    bad.append(loc.ITEM_ID)
                elif loc.OWNER_KIND == "bag" and loc.OWNER_ID not in item_ids:
                    bad.append(loc.ITEM_ID)
            return bad
        finally:
            db.close()


class DeleteBagDumpsContentsTest(BagDeleteTestBase):
    def test_dump_into_building_takes_free_slots_including_bags_own(self):
        self._add("other", "building", "room", 2)
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("a", "bag", "bag", 1)
        self._add("b", "bag", "bag", 2)

        result = self.svc.delete_item("bag")

        self.assertFalse(result.startswith("Error"), result)
        self.assertIn("2 item(s)", result)
        self.assertFalse(self._exists("bag"))
        self.assertIsNone(self._loc("bag"))
        # 入れ物が使っていたスロット 1 が空き、中身が小さい番号から埋める
        self.assertEqual(self._loc("a"), ("building", "room", 1))
        self.assertEqual(self._loc("b"), ("building", "room", 3))
        self.assertEqual(self._loc("other"), ("building", "room", 2))
        # キャッシュも再読込されている
        self.assertCountEqual(self.item_service.items_by_building["room"], ["a", "b", "other"])
        self.assertNotIn("bag", self.item_service.items)
        self.assertNotIn("bag", self.item_service.items_by_bag)

    def test_dump_into_persona_inventory(self):
        self._add("bag", "persona", "p1", 1, item_type="bag")
        self._add("a", "bag", "bag", 1)

        result = self.svc.delete_item("bag")

        self.assertFalse(result.startswith("Error"), result)
        self.assertEqual(self._loc("a"), ("persona", "p1", 1))
        self.assertEqual(self.item_service.items_by_persona["p1"], ["a"])

    def test_dump_into_parent_bag(self):
        self._add("outer", "building", "room", 1, item_type="bag")
        self._add("x", "bag", "outer", 1)
        self._add("bag", "bag", "outer", 2, item_type="bag")
        self._add("a", "bag", "bag", 1)

        self.svc.delete_item("bag")

        self.assertEqual(self._loc("a"), ("bag", "outer", 2))
        self.assertEqual(self._loc("x"), ("bag", "outer", 1))
        self.assertCountEqual(self.item_service.items_by_bag["outer"], ["x", "a"])

    def test_dump_when_bag_is_unplaced_leaves_contents_unplaced(self):
        self._add("bag", item_type="bag")  # 置き場所の行が無い = どこにも置かない
        self._add("a", "bag", "bag", 1)

        result = self.svc.delete_item("bag")

        self.assertFalse(result.startswith("Error"), result)
        self.assertTrue(self._exists("a"))
        self.assertIsNone(self._loc("a"))
        self.assertIn("a", self.item_service.world_items)
        self.assertEqual(self._orphan_locations(), [])

    def test_nested_bag_keeps_its_own_contents(self):
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("inner", "bag", "bag", 1, item_type="bag")
        self._add("deep", "bag", "inner", 1)

        result = self.svc.delete_item("bag")

        self.assertIn("1 item(s)", result)
        self.assertEqual(self._loc("inner"), ("building", "room", 1))
        self.assertEqual(self._loc("deep"), ("bag", "inner", 1))
        self.assertEqual(self._orphan_locations(), [])

    def test_non_bag_delete_message_unchanged(self):
        self._add("thing", "building", "room", 1)
        result = self.svc.delete_item("thing")
        self.assertEqual(result, "Item 'thing-name' deleted successfully.")
        self.assertFalse(self._exists("thing"))
        self.assertIsNone(self._loc("thing"))

    def test_missing_item_is_error(self):
        self.assertTrue(self.svc.delete_item("nope").startswith("Error:"))

    def test_self_loop_from_broken_cycle_goes_unplaced(self):
        # 壊れた循環: bag は outer の中、outer は bag の中
        self._add("bag", item_type="bag")
        self._add("outer", "bag", "bag", 1, item_type="bag")
        db = self.Session()
        try:
            db.add(ItemLocation(ITEM_ID="bag", OWNER_KIND="bag", OWNER_ID="outer", SLOT_NUMBER=1))
            db.commit()
        finally:
            db.close()

        result = self.svc.delete_item("bag")

        self.assertFalse(result.startswith("Error"), result)
        self.assertTrue(self._exists("outer"))
        self.assertIsNone(self._loc("outer"))  # 自分自身の中には入れない
        self.assertEqual(self._orphan_locations(), [])

    def test_failure_rolls_back_dump_and_delete_together(self):
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("a", "bag", "bag", 1)
        self._add("b", "bag", "bag", 2)

        def failing_session():
            session = self.Session()

            def boom():
                raise RuntimeError("commit failed")

            session.commit = boom
            return session

        self.svc.SessionLocal = failing_session
        result = self.svc.delete_item("bag")

        self.assertTrue(result.startswith("Error:"), result)
        # 中身の移動 (flush 済み) も入れ物の削除も巻き戻っている
        self.assertTrue(self._exists("bag"))
        self.assertEqual(self._loc("bag"), ("building", "room", 1))
        self.assertEqual(self._loc("a"), ("bag", "bag", 1))
        self.assertEqual(self._loc("b"), ("bag", "bag", 2))


class DeleteBagContentsTest(BagDeleteTestBase):
    def test_deletes_all_nested_contents_and_keeps_bag(self):
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("a", "bag", "bag", 1)
        self._add("inner", "bag", "bag", 2, item_type="bag")
        self._add("deep", "bag", "inner", 1)
        self._add("deeper_bag", "bag", "inner", 2, item_type="bag")
        self._add("deepest", "bag", "deeper_bag", 1)
        self._add("bystander", "building", "room", 2)

        result = self.svc.delete_bag_contents("bag")

        self.assertEqual(result, "Deleted 5 item(s) from bag 'bag-name'.")
        for gone in ("a", "inner", "deep", "deeper_bag", "deepest"):
            self.assertFalse(self._exists(gone), gone)
            self.assertIsNone(self._loc(gone), gone)
        self.assertTrue(self._exists("bag"))
        self.assertEqual(self._loc("bag"), ("building", "room", 1))
        self.assertEqual(self._loc("bystander"), ("building", "room", 2))
        self.assertEqual(self._count(Item), 2)
        self.assertEqual(self._count(ItemLocation), 2)
        self.assertEqual(self._orphan_locations(), [])
        self.assertNotIn("bag", self.item_service.items_by_bag)
        self.assertNotIn("deep", self.item_service.items)

    def test_empty_bag_deletes_nothing(self):
        self._add("bag", "building", "room", 1, item_type="bag")
        self.assertEqual(self.svc.delete_bag_contents("bag"), "Deleted 0 item(s) from bag 'bag-name'.")
        self.assertTrue(self._exists("bag"))

    def test_non_bag_is_refused(self):
        self._add("thing", "building", "room", 1)
        result = self.svc.delete_bag_contents("thing")
        self.assertTrue(result.startswith("Error:"), result)
        self.assertIn("not a bag", result)
        self.assertTrue(self._exists("thing"))

    def test_missing_item_is_error(self):
        self.assertTrue(self.svc.delete_bag_contents("nope").startswith("Error:"))

    def test_broken_cycle_does_not_leave_bag_pointing_at_deleted_bag(self):
        self._add("bag", item_type="bag")
        self._add("inner", "bag", "bag", 1, item_type="bag")
        db = self.Session()
        try:
            db.add(ItemLocation(ITEM_ID="bag", OWNER_KIND="bag", OWNER_ID="inner", SLOT_NUMBER=1))
            db.commit()
        finally:
            db.close()

        result = self.svc.delete_bag_contents("bag")

        self.assertEqual(result, "Deleted 1 item(s) from bag 'bag-name'.")
        self.assertTrue(self._exists("bag"))
        self.assertIsNone(self._loc("bag"))
        self.assertEqual(self._orphan_locations(), [])

    def test_failure_rolls_back_everything(self):
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("inner", "bag", "bag", 1, item_type="bag")
        self._add("deep", "bag", "inner", 1)

        def failing_session():
            session = self.Session()

            def boom():
                raise RuntimeError("commit failed")

            session.commit = boom
            return session

        self.svc.SessionLocal = failing_session
        result = self.svc.delete_bag_contents("bag")

        self.assertTrue(result.startswith("Error:"), result)
        self.assertTrue(self._exists("inner"))
        self.assertTrue(self._exists("deep"))
        self.assertEqual(self._loc("deep"), ("bag", "inner", 1))

    def test_contents_then_bag_deletes_bag_with_contents(self):
        """「中身ごと削除」= 中身をすべて消す → 入れ物を消す。"""
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("a", "bag", "bag", 1)
        self._add("inner", "bag", "bag", 2, item_type="bag")
        self._add("deep", "bag", "inner", 1)

        self.svc.delete_bag_contents("bag")
        result = self.svc.delete_item("bag")

        self.assertEqual(result, "Item 'bag-name' deleted successfully.")
        self.assertEqual(self._count(Item), 0)
        self.assertEqual(self._count(ItemLocation), 0)


class BagDeleteApiTest(BagDeleteTestBase):
    def setUp(self):
        super().setUp()
        from api.routes import world as world_route

        app = FastAPI()
        app.include_router(world_route.router, prefix="/api/world")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def test_delete_contents_route(self):
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("a", "bag", "bag", 1)

        resp = self.client.delete("/api/world/items/bag/contents")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["message"], "Deleted 1 item(s) from bag 'bag-name'.")
        self.assertFalse(self._exists("a"))
        self.assertTrue(self._exists("bag"))

    def test_delete_contents_route_refuses_non_bag_and_missing(self):
        self._add("thing", "building", "room", 1)
        resp = self.client.delete("/api/world/items/thing/contents")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not a bag", resp.json()["detail"])

        resp = self.client.delete("/api/world/items/nope/contents")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not found", resp.json()["detail"])

    def test_delete_bag_route_dumps_contents(self):
        self._add("bag", "building", "room", 1, item_type="bag")
        self._add("a", "bag", "bag", 1)

        resp = self.client.delete("/api/world/items/bag")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("1 item(s) inside were moved out", resp.json()["message"])
        self.assertEqual(self._loc("a"), ("building", "room", 1))


if __name__ == "__main__":
    unittest.main()
