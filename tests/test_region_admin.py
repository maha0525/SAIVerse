"""Region CRUD (AdminService) のバリデーションと DB 反映のテスト。

AdminService の region 系メソッドは self.SessionLocal しか使わないため、
__new__ で生成して SessionLocal だけ差し込む軽量構成でテストする。
"""
import os
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Building as BuildingModel, City as CityModel, Region as RegionModel
from manager.admin import AdminService


class RegionAdminTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            db.add(CityModel(CITYID=1, USERID=1, CITYNAME="city_a", UI_PORT=3000, API_PORT=8000))
            db.add(CityModel(CITYID=2, USERID=1, CITYNAME="city_b", UI_PORT=3001, API_PORT=9000))
            db.add(BuildingModel(CITYID=1, BUILDINGID="bldg_a", BUILDINGNAME="A"))
            db.add(BuildingModel(CITYID=2, BUILDINGID="bldg_b", BUILDINGNAME="B"))
            db.commit()
        finally:
            db.close()

        self.svc = AdminService.__new__(AdminService)
        self.svc.SessionLocal = self.SessionLocal

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _get_region(self, region_id):
        db = self.SessionLocal()
        try:
            return db.query(RegionModel).filter_by(REGION_ID=region_id).first()
        finally:
            db.close()

    # --- create ---

    def test_create_region_generates_id(self):
        result = self.svc.create_region("Mist Valley", "desc", "game", 1)
        self.assertNotIn("Error", result)
        region = self._get_region("region_mist_valley_city_a")
        self.assertIsNotNone(region)
        self.assertEqual(region.REGION_TYPE, "game")
        self.assertIsNone(region.PARENT_REGION_ID)

    def test_create_region_custom_id(self):
        result = self.svc.create_region("名前", "", "generic", 1, region_id="custom_id")
        self.assertNotIn("Error", result)
        self.assertIsNotNone(self._get_region("custom_id"))

    def test_create_region_invalid_type(self):
        result = self.svc.create_region("X", "", "dungeon", 1)
        self.assertIn("Error", result)

    def test_create_region_missing_city(self):
        result = self.svc.create_region("X", "", "generic", 99)
        self.assertIn("Error", result)

    def test_create_region_duplicate_id(self):
        self.svc.create_region("X", "", "generic", 1, region_id="dup")
        result = self.svc.create_region("Y", "", "generic", 1, region_id="dup")
        self.assertIn("Error", result)

    def test_create_subregion(self):
        self.svc.create_region("Top", "", "game", 1, region_id="top")
        result = self.svc.create_region("Sub", "", "game", 1, parent_region_id="top", region_id="sub")
        self.assertNotIn("Error", result)
        self.assertEqual(self._get_region("sub").PARENT_REGION_ID, "top")

    def test_create_subregion_rejects_two_level_nesting(self):
        self.svc.create_region("Top", "", "game", 1, region_id="top")
        self.svc.create_region("Sub", "", "game", 1, parent_region_id="top", region_id="sub")
        result = self.svc.create_region("SubSub", "", "game", 1, parent_region_id="sub")
        self.assertIn("Error", result)

    def test_create_subregion_rejects_cross_city_parent(self):
        self.svc.create_region("Top", "", "game", 1, region_id="top")
        result = self.svc.create_region("Sub", "", "game", 2, parent_region_id="top")
        self.assertIn("Error", result)

    def test_create_subregion_rejects_missing_parent(self):
        result = self.svc.create_region("Sub", "", "game", 1, parent_region_id="nope")
        self.assertIn("Error", result)

    # --- entrance (入口必須の不変条件。docs/intent/region.md §3, §6) ---

    def _get_building(self, building_id):
        db = self.SessionLocal()
        try:
            return db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
        finally:
            db.close()

    def test_create_generic_region_auto_creates_entrance(self):
        result = self.svc.create_region("エアの家", "", "generic", 1, region_id="r1")
        self.assertNotIn("Error", result)
        self.assertEqual(self._get_region("r1").ENTRANCE_BUILDING_ID, "entrance_r1")
        entrance = self._get_building("entrance_r1")
        self.assertIsNotNone(entrance)
        self.assertEqual(entrance.BUILDINGNAME, "エアの家: 入口")
        # トップ Region の入口は親スコープ (= City 直属) に属する
        self.assertIsNone(entrance.REGION_ID)

    def test_create_game_top_region_defers_entrance_to_ruler(self):
        result = self.svc.create_region("Mist", "", "game", 1, region_id="r1")
        self.assertNotIn("Error", result)
        self.assertIsNone(self._get_region("r1").ENTRANCE_BUILDING_ID)
        self.assertIsNone(self._get_building("entrance_r1"))

    def test_create_subregion_auto_creates_entrance_in_parent_scope(self):
        self.svc.create_region("Top", "", "game", 1, region_id="top")
        result = self.svc.create_region("街", "", "game", 1, parent_region_id="top", region_id="sub")
        self.assertNotIn("Error", result)
        self.assertEqual(self._get_region("sub").ENTRANCE_BUILDING_ID, "entrance_sub")
        entrance = self._get_building("entrance_sub")
        self.assertIsNotNone(entrance)
        # SubRegion の入口は親 Region に属する
        self.assertEqual(entrance.REGION_ID, "top")

    def test_create_region_with_explicit_entrance(self):
        result = self.svc.create_region(
            "X", "", "generic", 1, region_id="r1", entrance_building_id="bldg_a"
        )
        self.assertNotIn("Error", result)
        self.assertEqual(self._get_region("r1").ENTRANCE_BUILDING_ID, "bldg_a")
        self.assertIsNone(self._get_building("bldg_a").REGION_ID)
        # 自動生成はされない
        self.assertIsNone(self._get_building("entrance_r1"))

    def test_create_region_rejects_missing_entrance(self):
        result = self.svc.create_region(
            "X", "", "generic", 1, entrance_building_id="nope"
        )
        self.assertIn("Error", result)

    def test_create_region_rejects_cross_city_entrance(self):
        result = self.svc.create_region(
            "X", "", "generic", 1, entrance_building_id="bldg_b"
        )
        self.assertIn("Error", result)

    def test_create_region_rejects_entrance_name_conflict(self):
        db = self.SessionLocal()
        try:
            db.add(BuildingModel(CITYID=1, BUILDINGID="dup_e", BUILDINGNAME="X: 入口"))
            db.commit()
        finally:
            db.close()
        result = self.svc.create_region("X", "", "generic", 1, region_id="r1")
        self.assertIn("Error", result)
        # Region 行もロールバックされている (部分作成しない)
        self.assertIsNone(self._get_region("r1"))

    def test_delete_region_removes_auto_created_entrance(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        result = self.svc.delete_region("r1")
        self.assertNotIn("Error", result)
        self.assertIsNone(self._get_building("entrance_r1"))

    def test_delete_region_keeps_user_specified_entrance(self):
        self.svc.create_region(
            "X", "", "generic", 1, region_id="r1", entrance_building_id="bldg_a"
        )
        result = self.svc.delete_region("r1")
        self.assertNotIn("Error", result)
        self.assertIsNotNone(self._get_building("bldg_a"))

    # --- update ---

    def test_update_region(self):
        self.svc.create_region("Old", "", "generic", 1, region_id="r1")
        result = self.svc.update_region("r1", "New", "d2", "game")
        self.assertNotIn("Error", result)
        region = self._get_region("r1")
        self.assertEqual(region.NAME, "New")
        self.assertEqual(region.REGION_TYPE, "game")

    def test_update_region_rejects_self_parent(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        result = self.svc.update_region("r1", "X", "", "generic", parent_region_id="r1")
        self.assertIn("Error", result)

    def test_update_region_rejects_becoming_subregion_with_children(self):
        self.svc.create_region("Top", "", "game", 1, region_id="top")
        self.svc.create_region("Sub", "", "game", 1, parent_region_id="top", region_id="sub")
        self.svc.create_region("Other", "", "game", 1, region_id="other")
        result = self.svc.update_region("top", "Top", "", "game", parent_region_id="other")
        self.assertIn("Error", result)

    # --- delete ---

    def test_delete_region(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        result = self.svc.delete_region("r1")
        self.assertNotIn("Error", result)
        self.assertIsNone(self._get_region("r1"))

    def test_delete_region_rejects_with_subregions(self):
        self.svc.create_region("Top", "", "game", 1, region_id="top")
        self.svc.create_region("Sub", "", "game", 1, parent_region_id="top", region_id="sub")
        result = self.svc.delete_region("top")
        self.assertIn("Error", result)

    def test_delete_region_rejects_with_buildings(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        self.svc.set_building_region("bldg_a", "r1")
        result = self.svc.delete_region("r1")
        self.assertIn("Error", result)
        # 解除すれば消せる
        self.svc.set_building_region("bldg_a", None)
        result = self.svc.delete_region("r1")
        self.assertNotIn("Error", result)

    # --- building assignment ---

    def test_set_building_region(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        result = self.svc.set_building_region("bldg_a", "r1")
        self.assertNotIn("Error", result)
        db = self.SessionLocal()
        try:
            building = db.query(BuildingModel).filter_by(BUILDINGID="bldg_a").first()
            self.assertEqual(building.REGION_ID, "r1")
        finally:
            db.close()

    def test_set_building_region_rejects_cross_city(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        result = self.svc.set_building_region("bldg_b", "r1")
        self.assertIn("Error", result)

    def test_set_building_region_missing_targets(self):
        self.assertIn("Error", self.svc.set_building_region("nope", None))
        self.assertIn("Error", self.svc.set_building_region("bldg_a", "nope"))


if __name__ == "__main__":
    unittest.main()
