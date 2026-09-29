"""建物の削除が中身を残さないこと (docs/issues/building_delete_leaves_contents.md)。

- AdminService.delete_building(item_policy=keep|delete): アイテム / 設置物 (と
  ぶら下がる全行) / 建物のリアルタイムスペルを一つの transaction で片付ける。
  会話の記録 (building_messages) は残す (FLOW-15)。
- AdminService.get_building_deletion_preview: 確認ダイアログ用の数。
- API: GET /api/world/buildings/{id}/deletion-preview と
  DELETE /api/world/buildings/{id}?items=keep|delete
- 起動時の残骸の片付け (saiverse/building_leftover_cleanup.py)。

DB は隔離した file sqlite (TestClient はワーカースレッドでルートを実行する
ため)。manager は SimpleNamespace + 実物の ItemService。
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import (
    AI,
    Base,
    Building,
    BuildingMessage,
    BuildingOccupancyLog,
    City,
    FeedFixtureConfig,
    FeedItem,
    FeedReadCursor,
    FeedSubscription,
    Fixture,
    Item,
    ItemLocation,
    ObserverConfig,
    ObserverMetric,
    RealtimeSpellBinding,
    User,
)
from manager.admin import AdminService
from manager.items import ItemService
from saiverse.building_leftover_cleanup import cleanup_deleted_building_leftovers

CITY_ID = 1
TARGET = "shop_city_a"
OTHER = "inn_city_a"

_OWNS_DB = "saiverse.runtime_marker.another_running_process_owns_db"


class BuildingDeleteTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        self.db_file = Path(self._tmp.name) / "saiverse_test.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_file}", connect_args={"check_same_thread": False}
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.add(City(
                CITYID=CITY_ID, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000,
            ))
            db.flush()
            db.add(Building(CITYID=CITY_ID, BUILDINGID=TARGET, BUILDINGNAME="道具店"))
            db.add(Building(CITYID=CITY_ID, BUILDINGID=OTHER, BUILDINGNAME="宿"))
            db.commit()
        finally:
            db.close()

        self.scheduler = MagicMock()
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            buildings=[],
            building_map={},
            personas={},
            event_scheduler=self.scheduler,
        )
        self.item_service = ItemService(self.manager, SimpleNamespace())
        self.manager.item_service = self.item_service
        self.manager._load_items_from_db = self.item_service.load_items_from_db

        self.svc = AdminService.__new__(AdminService)
        self.svc.SessionLocal = self.Session
        self.svc.manager = self.manager
        self.manager.delete_building = self.svc.delete_building
        self.manager.get_building_deletion_preview = self.svc.get_building_deletion_preview

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    # --- seed helpers ---

    def _add_item(self, item_id, owner_kind=None, owner_id=None, slot=None, item_type="object"):
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

    def _add_fixture(self, prefix, building_id, name=None):
        """設置物 1 つと、それにぶら下がる全種類の行を作る。"""
        fixture_id = f"{prefix}-fx"
        db = self.Session()
        try:
            db.add(Fixture(
                FIXTURE_ID=fixture_id, BUILDING_ID=building_id,
                NAME=name or f"{prefix} スタンド", TYPE="feed_stand",
                STATE_JSON=json.dumps({"feed_stand": {}}),
            ))
            db.add(ObserverConfig(
                OBSERVER_ID=f"{prefix}-obs", FIXTURE_ID=fixture_id, EXEC_KIND="tool",
                EXEC_TARGET="dummy", INTERVAL_SEC=60,
            ))
            db.add(ObserverMetric(
                OBSERVER_ID=f"{prefix}-obs", METRIC_NAME="temperature", VALUE_NUM=1.0,
            ))
            db.add(FeedSubscription(
                SUBSCRIPTION_ID=f"{prefix}-sub", FIXTURE_ID=fixture_id,
                FEED_URL=f"https://{prefix}.example.com/feed", TITLE="feed",
            ))
            db.add(FeedItem(SUBSCRIPTION_ID=f"{prefix}-sub", GUID="g1", TITLE="記事"))
            db.add(FeedReadCursor(
                PERSONA_ID="persona-a", SUBSCRIPTION_ID=f"{prefix}-sub", LAST_ITEM_ID=1,
            ))
            db.add(FeedFixtureConfig(FIXTURE_ID=fixture_id, FETCH_INTERVAL_SEC=3600))
            db.commit()
        finally:
            db.close()
        return fixture_id

    def _add_spell(self, owner_kind, owner_id, name="weather"):
        db = self.Session()
        try:
            db.add(RealtimeSpellBinding(
                OWNER_KIND=owner_kind, OWNER_ID=owner_id, SPELL_NAME=name,
            ))
            db.commit()
        finally:
            db.close()

    def _add_message(self, building_id, seq, content="こんにちは", event_type=None):
        db = self.Session()
        try:
            db.add(BuildingMessage(
                building_id=building_id, seq=seq, role="user", content=content,
                timestamp="2026-09-29T10:00:00", heard_by="[]", ingested_by="[]",
                event_type=event_type,
            ))
            db.commit()
        finally:
            db.close()

    def _add_building(self, building_id, name):
        db = self.Session()
        try:
            db.add(Building(CITYID=CITY_ID, BUILDINGID=building_id, BUILDINGNAME=name))
            db.commit()
        finally:
            db.close()

    # --- inspection helpers ---

    def _loc(self, item_id):
        db = self.Session()
        try:
            row = db.query(ItemLocation).filter(ItemLocation.ITEM_ID == item_id).first()
            return (row.OWNER_KIND, row.OWNER_ID, row.SLOT_NUMBER) if row else None
        finally:
            db.close()

    def _exists(self, model, **filters):
        db = self.Session()
        try:
            return db.query(model).filter_by(**filters).first() is not None
        finally:
            db.close()

    def _fixture_counts(self, prefix):
        db = self.Session()
        try:
            fixture_id = f"{prefix}-fx"
            return {
                "fixture": db.query(Fixture).filter(Fixture.FIXTURE_ID == fixture_id).count(),
                "observer_config": db.query(ObserverConfig).filter(
                    ObserverConfig.FIXTURE_ID == fixture_id).count(),
                "observer_metrics": db.query(ObserverMetric).filter(
                    ObserverMetric.OBSERVER_ID == f"{prefix}-obs").count(),
                "feed_subscription": db.query(FeedSubscription).filter(
                    FeedSubscription.FIXTURE_ID == fixture_id).count(),
                "feed_item": db.query(FeedItem).filter(
                    FeedItem.SUBSCRIPTION_ID == f"{prefix}-sub").count(),
                "feed_read_cursor": db.query(FeedReadCursor).filter(
                    FeedReadCursor.SUBSCRIPTION_ID == f"{prefix}-sub").count(),
                "feed_fixture_config": db.query(FeedFixtureConfig).filter(
                    FeedFixtureConfig.FIXTURE_ID == fixture_id).count(),
            }
        finally:
            db.close()

    def _spells(self):
        db = self.Session()
        try:
            return sorted(
                (r.OWNER_KIND, r.OWNER_ID) for r in db.query(RealtimeSpellBinding).all()
            )
        finally:
            db.close()

    def _message_count(self, building_id):
        db = self.Session()
        try:
            return db.query(BuildingMessage).filter(
                BuildingMessage.building_id == building_id).count()
        finally:
            db.close()

    def _seed_full_target(self):
        """道具店に、アイテム (入れ子の入れ物を含む)・設置物・スペル・会話を置く。"""
        self._add_item("map", "building", TARGET, 1)
        self._add_item("bag", "building", TARGET, 2, item_type="bag")
        self._add_item("coin", "bag", "bag", 1)
        self._add_item("pouch", "bag", "bag", 2, item_type="bag")
        self._add_item("gem", "bag", "pouch", 1)
        self._add_fixture("t", TARGET, name="ニューススタンド")
        self._add_spell("building", TARGET)
        self._add_message(TARGET, 1)
        # 別の建物・ペルソナのもの (触られてはいけない)
        self._add_item("inn_item", "building", OTHER, 1)
        self._add_item("held", "persona", "persona-a", 1)
        self._add_fixture("o", OTHER)
        self._add_spell("building", OTHER)
        self._add_spell("persona", "persona-a")
        self._add_message(OTHER, 1)
        self.item_service.load_items_from_db()


ALL_ONE = {
    "fixture": 1, "observer_config": 1, "observer_metrics": 1,
    "feed_subscription": 1, "feed_item": 1, "feed_read_cursor": 1,
    "feed_fixture_config": 1,
}
ALL_ZERO = {key: 0 for key in ALL_ONE}


class DeleteBuildingKeepItemsTest(BuildingDeleteTestBase):
    def test_keep_unplaces_direct_items_and_leaves_bag_contents_in_bag(self):
        self._seed_full_target()

        result = self.svc.delete_building(TARGET)  # 既定は keep

        self.assertFalse(result.startswith("Error"), result)
        self.assertNotIn("restart", result)
        self.assertIn("2 item(s) were kept", result)
        self.assertFalse(self._exists(Building, BUILDINGID=TARGET))
        # 直接置かれていたものは「どこにも置かれていない」(行が無い) で本体は残る
        self.assertIsNone(self._loc("map"))
        self.assertIsNone(self._loc("bag"))
        for item_id in ("map", "bag", "coin", "pouch", "gem"):
            self.assertTrue(self._exists(Item, ITEM_ID=item_id), item_id)
        # 入れ物の中身は入れ物に付いたまま
        self.assertEqual(self._loc("coin"), ("bag", "bag", 1))
        self.assertEqual(self._loc("pouch"), ("bag", "bag", 2))
        self.assertEqual(self._loc("gem"), ("bag", "pouch", 1))
        # 他の建物・ペルソナの物は動かない
        self.assertEqual(self._loc("inn_item"), ("building", OTHER, 1))
        self.assertEqual(self._loc("held"), ("persona", "persona-a", 1))
        # キャッシュも読み直されている
        self.assertNotIn(TARGET, self.item_service.items_by_building)
        self.assertIn("map", self.item_service.world_items)
        self.assertIn("bag", self.item_service.world_items)

    def test_refresh_failure_after_commit_still_reports_success(self):
        """削除は commit 済みなので、後始末の失敗で「削除に失敗」とは返さない。"""
        self._seed_full_target()

        def boom():
            raise RuntimeError("reload failed")

        self.manager._load_items_from_db = boom
        result = self.svc.delete_building(TARGET)

        self.assertFalse(result.startswith("Error"), result)
        self.assertFalse(self._exists(Building, BUILDINGID=TARGET))


class DeleteBuildingDeleteItemsTest(BuildingDeleteTestBase):
    def test_delete_removes_direct_items_and_nested_contents_only(self):
        self._seed_full_target()

        result = self.svc.delete_building(TARGET, item_policy="delete")

        self.assertFalse(result.startswith("Error"), result)
        self.assertIn("5 item(s) were deleted", result)
        for item_id in ("map", "bag", "coin", "pouch", "gem"):
            self.assertFalse(self._exists(Item, ITEM_ID=item_id), item_id)
            self.assertIsNone(self._loc(item_id), item_id)
        self.assertTrue(self._exists(Item, ITEM_ID="inn_item"))
        self.assertEqual(self._loc("inn_item"), ("building", OTHER, 1))
        self.assertTrue(self._exists(Item, ITEM_ID="held"))
        self.assertNotIn("map", self.item_service.items)
        self.assertNotIn("gem", self.item_service.items)


class DeleteBuildingFixturesAndSpellsTest(BuildingDeleteTestBase):
    def test_fixtures_with_every_dependent_are_deleted_for_this_building_only(self):
        self._seed_full_target()
        self._add_fixture("t2", TARGET)

        result = self.svc.delete_building(TARGET)

        self.assertIn("2 fixture(s) were deleted", result)
        self.assertEqual(self._fixture_counts("t"), ALL_ZERO)
        self.assertEqual(self._fixture_counts("t2"), ALL_ZERO)
        self.assertEqual(self._fixture_counts("o"), ALL_ONE)
        # 消した観測設定の定期実行は取り消す (他の建物のものは取り消さない)
        cancelled = sorted(c.args[0] for c in self.scheduler.cancel.call_args_list)
        self.assertEqual(cancelled, ["observer:t-obs", "observer:t2-obs"])

    def test_building_spells_removed_persona_and_other_building_spells_kept(self):
        self._seed_full_target()
        self.svc.delete_building(TARGET)
        self.assertEqual(
            self._spells(),
            [("building", OTHER), ("persona", "persona-a")],
        )

    def test_conversation_records_are_kept(self):
        self._seed_full_target()
        self._add_message(TARGET, 2, content="入室", event_type="occupancy")
        self.svc.delete_building(TARGET)
        # FLOW-15: 会話の記録は消さない
        self.assertEqual(self._message_count(TARGET), 2)
        self.assertEqual(self._message_count(OTHER), 1)

    def test_occupancy_log_rows_are_deleted(self):
        self._seed_full_target()
        db = self.Session()
        try:
            db.add(AI(AIID="ai-1", HOME_CITYID=CITY_ID, AINAME="テスト"))
            db.flush()
            db.add(BuildingOccupancyLog(
                CITYID=CITY_ID, AIID="ai-1", BUILDINGID=TARGET,
                ENTRY_TIMESTAMP=datetime(2026, 8, 31),
                EXIT_TIMESTAMP=datetime(2026, 9, 1),
            ))
            db.commit()
        finally:
            db.close()
        self.svc.delete_building(TARGET)
        self.assertFalse(self._exists(BuildingOccupancyLog, BUILDINGID=TARGET))


class DeleteBuildingRefusalTest(BuildingDeleteTestBase):
    def _assert_nothing_deleted(self):
        self.assertTrue(self._exists(Building, BUILDINGID=TARGET))
        self.assertEqual(self._loc("map"), ("building", TARGET, 1))
        self.assertEqual(self._loc("gem"), ("bag", "pouch", 1))
        self.assertEqual(self._fixture_counts("t"), ALL_ONE)
        self.assertIn(("building", TARGET), self._spells())
        self.scheduler.cancel.assert_not_called()

    def test_user_in_building_refuses_and_deletes_nothing(self):
        self._seed_full_target()
        db = self.Session()
        try:
            user = db.query(User).filter_by(USERID=1).first()
            user.CURRENT_BUILDINGID = TARGET
            db.commit()
        finally:
            db.close()

        for policy in ("keep", "delete"):
            with self.subTest(policy=policy):
                result = self.svc.delete_building(TARGET, item_policy=policy)
                self.assertTrue(result.startswith("Error:"), result)
                self.assertIn("user is currently in it", result)
                self._assert_nothing_deleted()

    def test_occupied_building_refuses_and_deletes_nothing(self):
        self._seed_full_target()
        db = self.Session()
        try:
            db.add(AI(AIID="ai-1", HOME_CITYID=CITY_ID, AINAME="テスト"))
            db.flush()
            db.add(BuildingOccupancyLog(
                CITYID=CITY_ID, AIID="ai-1", BUILDINGID=TARGET,
                ENTRY_TIMESTAMP=datetime(2026, 9, 1), EXIT_TIMESTAMP=None,
            ))
            db.commit()
        finally:
            db.close()

        result = self.svc.delete_building(TARGET, item_policy="delete")

        self.assertTrue(result.startswith("Error:"), result)
        self.assertIn("occupied", result)
        self._assert_nothing_deleted()
        self.assertTrue(self._exists(BuildingOccupancyLog, BUILDINGID=TARGET))

    def test_invalid_item_policy_deletes_nothing(self):
        self._seed_full_target()
        result = self.svc.delete_building(TARGET, item_policy="world")
        self.assertTrue(result.startswith("Error:"), result)
        self._assert_nothing_deleted()

    def test_missing_building_is_error(self):
        self.assertTrue(self.svc.delete_building("nope").startswith("Error:"))

    def test_failure_rolls_back_everything(self):
        self._seed_full_target()

        def failing_session():
            session = self.Session()

            def boom():
                raise RuntimeError("commit failed")

            session.commit = boom
            return session

        self.svc.SessionLocal = failing_session
        result = self.svc.delete_building(TARGET, item_policy="delete")

        self.assertTrue(result.startswith("Error:"), result)
        self.svc.SessionLocal = self.Session
        self._assert_nothing_deleted()
        self.assertTrue(self._exists(Item, ITEM_ID="gem"))


class DeletionPreviewTest(BuildingDeleteTestBase):
    def test_preview_counts(self):
        self._seed_full_target()
        self._add_fixture("t2", TARGET, name="温度計")
        self._add_message(TARGET, 2)
        self._add_message(TARGET, 3, content="入室", event_type="occupancy")

        preview = self.svc.get_building_deletion_preview(TARGET)

        self.assertEqual(preview["building_id"], TARGET)
        self.assertEqual(preview["building_name"], "道具店")
        self.assertEqual(preview["item_count"], 2)  # map, bag
        self.assertEqual(preview["nested_item_count"], 3)  # coin, pouch, gem
        self.assertEqual(preview["fixture_count"], 2)
        self.assertCountEqual(preview["fixture_names"], ["ニューススタンド", "温度計"])
        # 出来事 (event_type のある行) は数えない
        self.assertEqual(preview["conversation_message_count"], 2)

    def test_preview_of_empty_building(self):
        preview = self.svc.get_building_deletion_preview(OTHER)
        self.assertEqual(preview["item_count"], 0)
        self.assertEqual(preview["nested_item_count"], 0)
        self.assertEqual(preview["fixture_count"], 0)
        self.assertEqual(preview["fixture_names"], [])
        self.assertEqual(preview["conversation_message_count"], 0)

    def test_preview_of_missing_building_is_none(self):
        self.assertIsNone(self.svc.get_building_deletion_preview("nope"))


class IdReuseTest(BuildingDeleteTestBase):
    def test_new_building_with_reused_id_gets_no_items_or_fixtures(self):
        """issue の再現: 消した建物と同じ ID の新しい建物に、中身が戻ってこない。"""
        # 日本語名なので building_1_city_a が付く
        created = self.svc.create_building("鉄腕の道具店", "", 5, "", CITY_ID)
        self.assertNotIn("Error", created)
        self.assertNotIn("restart", created)
        reused = "building_1_city_a"
        self._add_item("old_map", "building", reused, 1)
        self._add_fixture("news", reused)
        self._add_spell("building", reused)
        self._add_message(reused, 1, content="道具店での会話")

        self.assertFalse(self.svc.delete_building(reused).startswith("Error"))
        created = self.svc.create_building("霧雨の宿亭", "", 5, "", CITY_ID)
        self.assertIn(f"(ID: {reused})", created)

        preview = self.svc.get_building_deletion_preview(reused)
        self.assertEqual(preview["item_count"], 0)
        self.assertEqual(preview["fixture_count"], 0)
        self.assertEqual(self._fixture_counts("news"), ALL_ZERO)
        self.assertNotIn(("building", reused), self._spells())
        self.assertTrue(self._exists(Item, ITEM_ID="old_map"))  # keep で残っている
        self.assertIsNone(self._loc("old_map"))
        # 会話の記録は今の段階ではまだ同じ ID に残り、新しい部屋の過去として
        # 見えてしまう。残す会話を特殊な ID へ付け替えるのは後の段階
        # (issue の「3. ID の使い回し」)。そこが入ったらこの期待値は 0 になる。
        self.assertEqual(preview["conversation_message_count"], 1)


class BuildingDeleteApiTest(BuildingDeleteTestBase):
    def setUp(self):
        super().setUp()
        from api.routes import world as world_route

        app = FastAPI()
        app.include_router(world_route.router, prefix="/api/world")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def test_preview_route(self):
        self._seed_full_target()
        resp = self.client.get(f"/api/world/buildings/{TARGET}/deletion-preview")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["item_count"], 2)
        self.assertEqual(body["nested_item_count"], 3)
        self.assertEqual(body["fixture_names"], ["ニューススタンド"])
        self.assertEqual(body["conversation_message_count"], 1)

    def test_preview_route_404(self):
        resp = self.client.get("/api/world/buildings/nope/deletion-preview")
        self.assertEqual(resp.status_code, 404)

    def test_delete_route_default_keeps_items(self):
        self._seed_full_target()
        resp = self.client.delete(f"/api/world/buildings/{TARGET}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(self._exists(Item, ITEM_ID="map"))
        self.assertIsNone(self._loc("map"))

    def test_delete_route_items_delete(self):
        self._seed_full_target()
        resp = self.client.delete(f"/api/world/buildings/{TARGET}?items=delete")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(self._exists(Item, ITEM_ID="map"))
        self.assertFalse(self._exists(Item, ITEM_ID="gem"))

    def test_delete_route_invalid_items_is_400_and_deletes_nothing(self):
        self._seed_full_target()
        resp = self.client.delete(f"/api/world/buildings/{TARGET}?items=world")
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(self._exists(Building, BUILDINGID=TARGET))
        self.assertEqual(self._loc("map"), ("building", TARGET, 1))

    def test_delete_route_guard_is_400(self):
        self._seed_full_target()
        db = self.Session()
        try:
            db.query(User).filter_by(USERID=1).first().CURRENT_BUILDINGID = TARGET
            db.commit()
        finally:
            db.close()
        resp = self.client.delete(f"/api/world/buildings/{TARGET}?items=delete")
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(self._exists(Item, ITEM_ID="map"))


class StartupLeftoverCleanupTest(BuildingDeleteTestBase):
    def _seed_leftovers(self):
        """昔の削除が残した残骸 (建物の行が無い) と、正常な行を並べる。"""
        gone = "building_9_city_a"
        self._add_item("orphan", "building", gone, 1)
        self._add_item("orphan_bag", "building", gone, 2, item_type="bag")
        self._add_item("in_orphan_bag", "bag", "orphan_bag", 1)
        self._add_fixture("g", gone)
        self._add_spell("building", gone)
        self._add_message(gone, 1)
        # 正常な行
        self._add_item("valid", "building", OTHER, 1)
        self._add_item("held", "persona", "persona-a", 1)
        self._add_fixture("o", OTHER)
        self._add_spell("building", OTHER)
        self._add_spell("persona", "persona-a")
        return gone

    def _run(self):
        with patch(_OWNS_DB, return_value=(False, "")):
            return cleanup_deleted_building_leftovers(
                session_factory=self.Session, db_path=self.db_file,
            )

    def test_cleanup_unplaces_items_deletes_fixtures_and_spells(self):
        gone = self._seed_leftovers()

        counts = self._run()

        self.assertEqual(counts, {
            "unplaced_items": 2, "deleted_fixtures": 1, "deleted_realtime_spells": 1,
        })
        # アイテムは消さず、どこにも置かれていない状態へ
        self.assertTrue(self._exists(Item, ITEM_ID="orphan"))
        self.assertIsNone(self._loc("orphan"))
        self.assertIsNone(self._loc("orphan_bag"))
        self.assertEqual(self._loc("in_orphan_bag"), ("bag", "orphan_bag", 1))
        self.assertEqual(self._fixture_counts("g"), ALL_ZERO)
        # 正常な行は触らない
        self.assertEqual(self._loc("valid"), ("building", OTHER, 1))
        self.assertEqual(self._loc("held"), ("persona", "persona-a", 1))
        self.assertEqual(self._fixture_counts("o"), ALL_ONE)
        self.assertEqual(self._spells(), [("building", OTHER), ("persona", "persona-a")])
        # 会話の記録には触らない
        self.assertEqual(self._message_count(gone), 1)

    def test_cleanup_is_idempotent(self):
        self._seed_leftovers()
        self._run()
        counts = self._run()
        self.assertEqual(counts, {
            "unplaced_items": 0, "deleted_fixtures": 0, "deleted_realtime_spells": 0,
        })
        self.assertEqual(self._fixture_counts("o"), ALL_ONE)
        self.assertEqual(self._loc("valid"), ("building", OTHER, 1))

    def test_cleanup_skips_when_another_process_owns_db(self):
        self._seed_leftovers()
        with patch(_OWNS_DB, return_value=(True, "pid 1234")):
            result = cleanup_deleted_building_leftovers(
                session_factory=self.Session, db_path=self.db_file,
            )
        self.assertIsNone(result)
        self.assertEqual(self._loc("orphan"), ("building", "building_9_city_a", 1))
        self.assertEqual(self._fixture_counts("g"), ALL_ONE)


if __name__ == "__main__":
    unittest.main()
