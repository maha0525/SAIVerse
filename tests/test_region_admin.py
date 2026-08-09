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

    # --- 文字種契約 (docs/issues/building_id_no_charset_constraint.md 論点 1) ---
    # Region ID は入口 Building の ID (entrance_<region_id>) の材料なので、
    # ここが素通しだと Building ID 側の契約ごと破れる。日本語名の SubRegion は
    # game_create_subregion (Ruler ペルソナの口) から実際に来る。

    def test_japanese_region_name_generates_ascii_id_and_entrance(self):
        result = self.svc.create_region("霧降りの森", "", "generic", 1)
        self.assertNotIn("Error", result)
        region = self._get_region("region_1_city_a")
        self.assertIsNotNone(region)
        self.assertEqual(region.NAME, "霧降りの森")
        # 入口 Building の ID も ASCII に収まる
        self.assertEqual(region.ENTRANCE_BUILDING_ID, "entrance_region_1_city_a")
        entrance = self._get_building("entrance_region_1_city_a")
        self.assertIsNotNone(entrance)
        self.assertEqual(entrance.BUILDINGNAME, "霧降りの森: 入口")

    def test_japanese_region_names_get_distinct_numbered_ids(self):
        self.assertNotIn("Error", self.svc.create_region("霧降りの森", "", "generic", 1))
        self.assertNotIn("Error", self.svc.create_region("白霧の社", "", "generic", 1))
        self.assertIsNotNone(self._get_region("region_1_city_a"))
        self.assertIsNotNone(self._get_region("region_2_city_a"))

    def test_custom_japanese_region_id_rejected(self):
        result = self.svc.create_region("森", "", "generic", 1, region_id="霧降りの森")
        self.assertIn("Error", result)
        self.assertIsNone(self._get_region("霧降りの森"))
        # 入口 Building も作られない
        self.assertIsNone(self._get_building("entrance_霧降りの森"))

    def test_custom_region_id_with_slash_rejected(self):
        result = self.svc.create_region("X", "", "generic", 1, region_id="a/b")
        self.assertIn("Error", result)

    def test_numbered_region_id_skips_candidate_whose_entrance_is_taken(self):
        # Region が消えても入口 Building が残る経路がある — delete_region は
        # Region の削除を commit した後で入口を消しにいき、そこが失敗しても
        # 続行する。その孤児が若い連番に居座ると、Region 側が空いていても
        # 入口作成の段で作成が止まる。候補選びで入口 ID ごと予約して避ける
        db = self.SessionLocal()
        try:
            db.add(BuildingModel(
                CITYID=1,
                BUILDINGID="entrance_region_1_city_a",
                BUILDINGNAME="孤児になった入口",
            ))
            db.commit()
        finally:
            db.close()

        result = self.svc.create_region("霧降りの森", "", "generic", 1)
        self.assertNotIn("Error", result)
        self.assertIsNone(self._get_region("region_1_city_a"))
        region = self._get_region("region_2_city_a")
        self.assertIsNotNone(region)
        self.assertEqual(region.ENTRANCE_BUILDING_ID, "entrance_region_2_city_a")

    def _add_building(self, building_id, name="無関係な建物"):
        db = self.SessionLocal()
        try:
            db.add(BuildingModel(CITYID=1, BUILDINGID=building_id, BUILDINGNAME=name))
            db.commit()
        finally:
            db.close()

    def test_explicit_entrance_with_the_reserved_derived_id_is_rejected(self):
        # entrance_<region_id> は自動生成入口の予約名。delete_region は ID の形
        # だけで自動生成物かを判定して消すので、ユーザー所有の Building がこの
        # 名前で入口になると Region 削除の巻き添えで消える
        self._add_building("entrance_r1", "ユーザーが作った建物")
        result = self.svc.create_region(
            "X", "", "generic", 1, region_id="r1", entrance_building_id="entrance_r1",
        )
        self.assertIn("Error", result)
        self.assertIsNone(self._get_region("r1"))
        self.assertIsNotNone(self._get_building("entrance_r1"))

    def test_explicit_entrance_does_not_reserve_the_derived_entrance_id(self):
        # 入口を自動作成しない分岐は entrance_<rid> を使わない。無関係な同名
        # Building があっても連番を飛ばさない
        self._add_building("entrance_region_1_city_a")
        result = self.svc.create_region(
            "霧降りの森", "", "generic", 1, entrance_building_id="bldg_a",
        )
        self.assertNotIn("Error", result)
        region = self._get_region("region_1_city_a")
        self.assertIsNotNone(region)
        self.assertEqual(region.ENTRANCE_BUILDING_ID, "bldg_a")

    def test_game_top_region_does_not_reserve_the_derived_entrance_id(self):
        # game のトップ Region の入口は create_ruler の控室なのでここでは作らない
        self._add_building("entrance_region_1_city_a")
        result = self.svc.create_region("霧の谷", "", "game", 1)
        self.assertNotIn("Error", result)
        region = self._get_region("region_1_city_a")
        self.assertIsNotNone(region)
        self.assertIsNone(region.ENTRANCE_BUILDING_ID)

    def test_existing_non_ascii_region_id_still_loads_updates_and_deletes(self):
        # 既存の非 ASCII ID は裁定どおり放置する (作成の口だけ塞ぐ)。作成時
        # 検証を足したことで既存データの読み・更新・削除が壊れていないことを、
        # 散文の裁定でなく実行可能な形で固定する
        db = self.SessionLocal()
        try:
            db.add(RegionModel(
                REGION_ID="霧降りの森",
                CITYID=1,
                NAME="霧降りの森",
                DESCRIPTION="",
                REGION_TYPE="generic",
                ENTRANCE_BUILDING_ID="entrance_霧降りの森",
            ))
            db.add(BuildingModel(
                CITYID=1,
                BUILDINGID="entrance_霧降りの森",
                BUILDINGNAME="霧降りの森: 入口",
            ))
            db.commit()
        finally:
            db.close()

        self.assertIsNotNone(self._get_region("霧降りの森"))
        self.assertNotIn(
            "Error", self.svc.update_region("霧降りの森", "改名後", "d", "generic"),
        )
        self.assertEqual(self._get_region("霧降りの森").NAME, "改名後")
        self.assertNotIn("Error", self.svc.delete_region("霧降りの森"))
        self.assertIsNone(self._get_region("霧降りの森"))
        # 自動生成の規則に合致する入口なので Region と運命を共にする
        self.assertIsNone(self._get_building("entrance_霧降りの森"))

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

    # --- W7/柱5: 入口は親スコープ (分離監査 P1-6) ---

    def test_update_region_top_to_sub_moves_entrance_into_parent_scope(self):
        self.svc.create_region("Top", "", "generic", 1, region_id="top")
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        result = self.svc.update_region("r1", "X", "", "generic", parent_region_id="top")
        self.assertNotIn("Error", result)
        # sub 化に伴い入口が親 Region スコープへ追従する
        self.assertEqual(self._get_building("entrance_r1").REGION_ID, "top")

    def test_update_region_sub_to_top_moves_entrance_to_city_scope(self):
        self.svc.create_region("Top", "", "generic", 1, region_id="top")
        self.svc.create_region(
            "Sub", "", "generic", 1, parent_region_id="top", region_id="sub"
        )
        self.assertEqual(self._get_building("entrance_sub").REGION_ID, "top")
        result = self.svc.update_region("sub", "Sub", "", "generic")
        self.assertNotIn("Error", result)
        # top 化で入口は City 直下へ
        self.assertIsNone(self._get_building("entrance_sub").REGION_ID)

    def test_update_region_without_parent_change_keeps_entrance(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        result = self.svc.update_region("r1", "Renamed", "d", "generic")
        self.assertNotIn("Error", result)
        self.assertIsNone(self._get_building("entrance_r1").REGION_ID)

    def test_update_region_rejects_parent_change_with_missing_entrance(self):
        self.svc.create_region("Top", "", "generic", 1, region_id="top")
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        db = self.SessionLocal()
        try:
            db.query(BuildingModel).filter_by(BUILDINGID="entrance_r1").delete()
            db.commit()
        finally:
            db.close()
        result = self.svc.update_region("r1", "X", "", "generic", parent_region_id="top")
        self.assertIn("Error", result)
        # Region 側もロールバックされている (中途半端に sub 化しない)
        self.assertIsNone(self._get_region("r1").PARENT_REGION_ID)

    def test_create_region_rejects_shared_entrance(self):
        """入口所有は一意 (Codex 第二巡): 共有すると片方の親変更が他方の
        不変条件を壊すため、作成時に拒否する。"""
        self.svc.create_region(
            "X", "", "generic", 1, region_id="r1", entrance_building_id="bldg_a"
        )
        result = self.svc.create_region(
            "Y", "", "generic", 1, region_id="r2", entrance_building_id="bldg_a"
        )
        self.assertIn("Error", result)
        self.assertIsNone(self._get_region("r2"))

    def test_update_region_rejects_parent_change_with_shared_entrance(self):
        """レガシーデータで共有された入口は、解消するまで親変更を拒否する。"""
        self.svc.create_region("Top", "", "generic", 1, region_id="top")
        self.svc.create_region(
            "X", "", "generic", 1, region_id="r1", entrance_building_id="bldg_a"
        )
        # 共有状態を直接 DB に作る (create_region は拒否するようになったため)
        db = self.SessionLocal()
        try:
            other = RegionModel(
                REGION_ID="r2", CITYID=1, NAME="Y", DESCRIPTION="",
                REGION_TYPE="generic", ENTRANCE_BUILDING_ID="bldg_a",
            )
            db.add(other)
            db.commit()
        finally:
            db.close()
        result = self.svc.update_region("r1", "X", "", "generic", parent_region_id="top")
        self.assertIn("Error", result)
        self.assertIsNone(self._get_region("r1").PARENT_REGION_ID)

    def test_set_building_region_rejects_entrance_building(self):
        self.svc.create_region("X", "", "generic", 1, region_id="r1")
        # 入口の detach / 自 Region 内取り込み / 別 Region 付け替えの全てを拒否
        self.assertIn("Error", self.svc.set_building_region("entrance_r1", None))
        self.assertIn("Error", self.svc.set_building_region("entrance_r1", "r1"))
        self.svc.create_region("Y", "", "generic", 1, region_id="r2")
        self.assertIn("Error", self.svc.set_building_region("entrance_r1", "r2"))
        # 所属は元のまま (トップ Region の入口 = City 直下)
        self.assertIsNone(self._get_building("entrance_r1").REGION_ID)


class BuildingCityImmutableTestCase(unittest.TestCase):
    """W7/柱5: Building の City は通常更新では immutable (分離監査 P1-7)。"""

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
            db.add(BuildingModel(
                CITYID=1, BUILDINGID="bldg_a", BUILDINGNAME="A", CAPACITY=3,
            ))
            db.commit()
        finally:
            db.close()

        self.svc = AdminService.__new__(AdminService)
        self.svc.SessionLocal = self.SessionLocal

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _get_building(self, building_id):
        db = self.SessionLocal()
        try:
            return db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
        finally:
            db.close()

    def test_update_building_rejects_city_change(self):
        result = self.svc.update_building(
            "bldg_a", "A", 3, "", "", city_id=2, tool_ids=[], interval=0,
        )
        self.assertIn("Error", result)
        self.assertEqual(self._get_building("bldg_a").CITYID, 1)

    def test_update_building_normal_fields_still_work(self):
        result = self.svc.update_building(
            "bldg_a", "A改", 5, "desc", "sys", city_id=1, tool_ids=[], interval=60,
        )
        self.assertNotIn("Error", result)
        building = self._get_building("bldg_a")
        self.assertEqual(building.BUILDINGNAME, "A改")
        self.assertEqual(building.CAPACITY, 5)
        self.assertEqual(building.CITYID, 1)


if __name__ == "__main__":
    unittest.main()
