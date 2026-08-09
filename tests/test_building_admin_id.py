"""Building ID の文字種契約 (create_building) のテスト。

docs/issues/building_id_no_charset_constraint.md: ID はログのフォルダパス・
saiverse:// URI・API パス引数に素で入る永続キーのため、作成の口 (AdminService.
create_building — API・ツール・manager 委譲の全経路がここへ集約) で ASCII
文字種を強制する。既存の日本語 ID 3 件はリネームしない (実害が出るまで放置)。
"""
import os
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Building as BuildingModel, City as CityModel
from manager.admin import AdminService, _slugify_identifier


class BuildingAdminIdTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            db.add(CityModel(CITYID=1, USERID=1, CITYNAME="city_a", UI_PORT=3000, API_PORT=8000))
            db.commit()
        finally:
            db.close()

        self.svc = AdminService.__new__(AdminService)
        self.svc.SessionLocal = self.SessionLocal

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _get(self, building_id):
        db = self.SessionLocal()
        try:
            return db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
        finally:
            db.close()

    def _create(self, name, building_id=None):
        return self.svc.create_building(name, "desc", 5, "", 1, building_id)

    # --- 自動生成 ---

    def test_ascii_name_generates_same_id_as_before(self):
        result = self._create("Tea House")
        self.assertNotIn("Error", result)
        self.assertIsNotNone(self._get("tea_house_city_a"))

    def test_japanese_name_falls_back_to_numbered_id(self):
        result = self._create("鉄腕の道具店")
        self.assertNotIn("Error", result)
        self.assertIsNotNone(self._get("building_1_city_a"))

    def test_numbered_fallback_skips_taken_ids(self):
        self.assertNotIn("Error", self._create("鉄腕の道具店"))
        self.assertNotIn("Error", self._create("霧雨の宿亭"))
        self.assertIsNotNone(self._get("building_1_city_a"))
        self.assertIsNotNone(self._get("building_2_city_a"))

    def test_mixed_name_keeps_ascii_part_only(self):
        result = self._create("Cafe 森")
        self.assertNotIn("Error", result)
        self.assertIsNotNone(self._get("cafe_city_a"))

    # --- カスタム ID ---

    def test_custom_ascii_id_accepted(self):
        result = self._create("店", building_id="tool_shop")
        self.assertNotIn("Error", result)
        self.assertIsNotNone(self._get("tool_shop"))

    def test_custom_japanese_id_rejected(self):
        result = self._create("店", building_id="鉄腕の道具店")
        self.assertIn("Error", result)
        self.assertIsNone(self._get("鉄腕の道具店"))

    def test_custom_id_with_slash_rejected(self):
        result = self._create("店", building_id="a/b")
        self.assertIn("Error", result)

    def test_custom_id_leading_symbol_rejected(self):
        result = self._create("店", building_id="-shop")
        self.assertIn("Error", result)

    # --- slug ヘルパ ---

    def test_slugify_identifier(self):
        self.assertEqual(_slugify_identifier("Tea  House"), "tea_house")
        self.assertEqual(_slugify_identifier("鉄腕の道具店"), "")
        self.assertEqual(_slugify_identifier("Bob's Bar"), "bobs_bar")
        self.assertEqual(_slugify_identifier("  _edge-_ "), "edge")


if __name__ == "__main__":
    unittest.main()
