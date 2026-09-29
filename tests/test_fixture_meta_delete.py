"""設置物 (Fixture) の名前・説明文の更新と削除。

- ObserverManager.update_fixture_meta / delete_fixture (道連れ削除・City 境界)
- API: PATCH / DELETE /api/observer/fixture/{fixture_id}
- record_metrics: 設置物が消えた後の観測は履歴を書かない

DB は隔離した file sqlite (TestClient はワーカースレッドでルートを実行する
ため — test_feeds_api.py と同じ流儀)。manager は SimpleNamespace + 実物の
ObserverManager。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import (
    Base,
    Building,
    City,
    FeedFixtureConfig,
    FeedItem,
    FeedReadCursor,
    FeedSubscription,
    Fixture,
    ObserverConfig,
    ObserverMetric,
    User,
)
from saiverse.observer_manager import ObserverManager

BUILDING_ID = "bldg-1"
OTHER_BUILDING_ID = "bldg-other"


class FixtureTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        db_file = Path(self._tmp.name) / "saiverse_test.db"
        self.engine = create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
            other = City(USERID=1, CITY_SLUG="other_city", UI_PORT=3002, API_PORT=8002)
            db.add_all([city, other])
            db.flush()
            db.add(Building(
                CITYID=city.CITYID, BUILDINGID=BUILDING_ID, BUILDINGNAME="図書館",
            ))
            db.add(Building(
                CITYID=other.CITYID, BUILDINGID=OTHER_BUILDING_ID, BUILDINGNAME="他所",
            ))
            db.commit()
            self.city_id = city.CITYID
        finally:
            db.close()

        self.scheduler = MagicMock()
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            city_id=self.city_id,
            event_scheduler=self.scheduler,
        )
        self.obs = ObserverManager(self.manager)

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    # ------------------------------------------------------------------

    def _seed_fixture_with_dependents(self, prefix: str, building_id: str) -> str:
        """設置物 1 つと、それに属する全種類の行 (観測設定 + 履歴、購読 +
        記事 + 既読カーソル、スタンド設定) を作る。"""
        fixture_id = f"{prefix}-fx"
        db = self.Session()
        try:
            db.add(Fixture(
                FIXTURE_ID=fixture_id, BUILDING_ID=building_id,
                NAME=f"{prefix} スタンド", TYPE="feed_stand",
                DESCRIPTION="元の説明",
                STATE_JSON=json.dumps({"feed_stand": {"latest": ["x"]}}),
            ))
            db.add(ObserverConfig(
                OBSERVER_ID=f"{prefix}-obs", FIXTURE_ID=fixture_id, EXEC_KIND="push",
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

    def _counts(self, prefix: str) -> dict:
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

    def _fixture_row(self, fixture_id: str) -> Fixture:
        db = self.Session()
        try:
            return db.query(Fixture).filter(Fixture.FIXTURE_ID == fixture_id).first()
        finally:
            db.close()


ALL_ONE = {
    "fixture": 1, "observer_config": 1, "observer_metrics": 1,
    "feed_subscription": 1, "feed_item": 1, "feed_read_cursor": 1,
    "feed_fixture_config": 1,
}
ALL_ZERO = {key: 0 for key in ALL_ONE}


class DeleteFixtureTest(FixtureTestBase):
    def test_delete_removes_fixture_and_every_dependent(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        self._seed_fixture_with_dependents("b", BUILDING_ID)

        self.assertTrue(self.obs.delete_fixture(target))

        self.assertEqual(self._counts("a"), ALL_ZERO)
        # 同じ City の別の設置物の行は一切触らない
        self.assertEqual(self._counts("b"), ALL_ONE)
        # pull 型 observer の定期実行を取り消す
        self.scheduler.cancel.assert_called_once_with("observer:a-obs")

    def test_other_city_fixture_is_not_deleted(self):
        target = self._seed_fixture_with_dependents("o", OTHER_BUILDING_ID)

        self.assertFalse(self.obs.delete_fixture(target))

        # 本体も道連れの子も残る (本体の DELETE が 0 行なら子に進まない)
        self.assertEqual(self._counts("o"), ALL_ONE)
        self.scheduler.cancel.assert_not_called()

    def test_unknown_fixture_returns_false(self):
        self.assertFalse(self.obs.delete_fixture("no-such-fixture"))

    def test_delete_without_scheduler_attribute(self):
        """event_scheduler を持たない manager でも削除できる。"""
        del self.manager.event_scheduler
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        self.assertTrue(self.obs.delete_fixture(target))
        self.assertEqual(self._counts("a"), ALL_ZERO)


class UpdateFixtureMetaTest(FixtureTestBase):
    def test_description_only_update_keeps_name_and_state(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        before = self._fixture_row(target)

        updated = self.obs.update_fixture_meta(target, description="新しい説明")

        self.assertIsNotNone(updated)
        self.assertEqual(updated.DESCRIPTION, "新しい説明")
        after = self._fixture_row(target)
        self.assertEqual(after.DESCRIPTION, "新しい説明")
        self.assertEqual(after.NAME, before.NAME)
        self.assertEqual(after.STATE_JSON, before.STATE_JSON)
        self.assertEqual(after.TYPE, before.TYPE)

    def test_name_is_stripped_and_description_may_be_empty(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        updated = self.obs.update_fixture_meta(
            target, name="  新しい名前  ", description="",
        )
        self.assertEqual(updated.NAME, "新しい名前")
        self.assertEqual(updated.DESCRIPTION, "")

    def test_name_validation(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        for bad in ("", "   ", "x" * 256):
            with self.subTest(name=bad[:10]):
                with self.assertRaises(ValueError):
                    self.obs.update_fixture_meta(target, name=bad)
        # 255 文字ちょうどは通る
        self.assertEqual(
            self.obs.update_fixture_meta(target, name="y" * 255).NAME, "y" * 255,
        )

    def test_description_length_limit(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        with self.assertRaises(ValueError):
            self.obs.update_fixture_meta(target, description="d" * 2049)
        self.assertEqual(
            len(self.obs.update_fixture_meta(target, description="d" * 2048).DESCRIPTION),
            2048,
        )

    def test_no_fields_is_rejected(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        with self.assertRaises(ValueError):
            self.obs.update_fixture_meta(target)

    def test_other_city_fixture_is_not_updated(self):
        target = self._seed_fixture_with_dependents("o", OTHER_BUILDING_ID)
        self.assertIsNone(self.obs.update_fixture_meta(target, name="乗っ取り"))
        self.assertEqual(self._fixture_row(target).NAME, "o スタンド")

    def test_unknown_fixture_returns_none(self):
        self.assertIsNone(self.obs.update_fixture_meta("no-such", name="x"))


class RecordMetricsAfterFixtureDeletedTest(FixtureTestBase):
    def test_metrics_for_vanished_fixture_are_discarded(self):
        """設置物の行が無い observer への record_metrics は履歴を書かない
        (設定確認の後に delete_fixture が走った場合に親の無い履歴を残さない)。"""
        db = self.Session()
        try:
            db.add(ObserverConfig(
                OBSERVER_ID="orphan-obs", FIXTURE_ID="vanished-fx", EXEC_KIND="push",
            ))
            db.commit()
        finally:
            db.close()

        recorded = self.obs.record_metrics(
            "orphan-obs", {"temperature": {"value_num": 3.0}},
        )

        self.assertEqual(recorded, [])
        db = self.Session()
        try:
            self.assertEqual(
                db.query(ObserverMetric).filter(
                    ObserverMetric.OBSERVER_ID == "orphan-obs").count(),
                0,
            )
        finally:
            db.close()

    def test_metrics_for_existing_fixture_are_recorded(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        recorded = self.obs.record_metrics(
            "a-obs", {"humidity": {"value_num": 40.0}},
        )
        self.assertEqual(len(recorded), 1)
        state = json.loads(self._fixture_row(target).STATE_JSON)
        self.assertEqual(state["humidity"]["value_num"], 40.0)
        self.assertIn("feed_stand", state)  # 他の書き手のキーは残る


class FixtureApiTest(FixtureTestBase):
    def setUp(self):
        super().setUp()
        self.manager.observer_manager = self.obs
        from api.routes import observer as observer_route

        app = FastAPI()
        app.include_router(observer_route.router, prefix="/api/observer")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def test_patch_updates_and_returns_fixture(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        resp = self.client.patch(
            f"/api/observer/fixture/{target}", json={"name": "改名"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["fixture_id"], target)
        self.assertEqual(body["name"], "改名")
        self.assertEqual(body["description"], "元の説明")  # 送っていない欄は据え置き
        self.assertEqual(body["building_id"], BUILDING_ID)
        self.assertEqual(body["type"], "feed_stand")
        self.assertIn("state_json", body)

    def test_patch_404_for_unknown_and_other_city(self):
        other = self._seed_fixture_with_dependents("o", OTHER_BUILDING_ID)
        for fixture_id in ("no-such", other):
            with self.subTest(fixture_id=fixture_id):
                resp = self.client.patch(
                    f"/api/observer/fixture/{fixture_id}", json={"name": "x"},
                )
                self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._fixture_row(other).NAME, "o スタンド")

    def test_patch_422_on_invalid_body(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        bad_bodies = [
            {},                              # 変更する欄が無い
            {"name": ""},                    # 空の名前
            {"name": "   "},                 # 空白だけ
            {"name": None},                  # null は省略と区別して拒否
            {"description": None},
            {"name": 123},                   # 文字列以外 (strict)
            {"description": ["x"]},
            {"name": "ok", "type": "bag"},   # 未知の欄 (extra=forbid)
            {"name": "x" * 256},
            {"description": "d" * 2049},
        ]
        for body in bad_bodies:
            with self.subTest(body=str(body)[:40]):
                resp = self.client.patch(f"/api/observer/fixture/{target}", json=body)
                self.assertEqual(resp.status_code, 422, resp.text)
        row = self._fixture_row(target)
        self.assertEqual(row.NAME, "a スタンド")
        self.assertEqual(row.DESCRIPTION, "元の説明")

    def test_delete_then_404(self):
        target = self._seed_fixture_with_dependents("a", BUILDING_ID)
        resp = self.client.delete(f"/api/observer/fixture/{target}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"deleted": True})
        self.assertEqual(self._counts("a"), ALL_ZERO)

        resp = self.client.delete(f"/api/observer/fixture/{target}")
        self.assertEqual(resp.status_code, 404)

    def test_delete_404_for_other_city(self):
        other = self._seed_fixture_with_dependents("o", OTHER_BUILDING_ID)
        resp = self.client.delete(f"/api/observer/fixture/{other}")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._counts("o"), ALL_ONE)


if __name__ == "__main__":
    unittest.main()
