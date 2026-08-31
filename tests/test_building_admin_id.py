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
from manager.admin import AdminService
from manager.ids import (
    build_identifier,
    entrance_id_for,
    is_valid_identifier,
    slugify_identifier,
)


class BuildingAdminIdTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            db.add(CityModel(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
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

    def test_case_folded_duplicate_id_rejected(self):
        # Building ID はフォルダ名 (~/.saiverse/cities/<city>/buildings/<id>/)
        # になり、Windows は大文字小文字を区別しないため、'Cafe' と 'cafe' を
        # 別 Building として通すとログの保存先が同じになる
        self.assertNotIn("Error", self._create("店", building_id="cafe"))
        result = self._create("店2", building_id="Cafe")
        self.assertIn("Error", result)
        self.assertIsNone(self._get("Cafe"))


class IdentifierHelperTestCase(unittest.TestCase):
    """manager/ids.py — 全作成経路が共有する契約と生成式。"""

    def test_slugify_identifier(self):
        self.assertEqual(slugify_identifier("Tea  House"), "tea_house")
        self.assertEqual(slugify_identifier("鉄腕の道具店"), "")
        self.assertEqual(slugify_identifier("Bob's Bar"), "bobs_bar")
        self.assertEqual(slugify_identifier("  _edge-_ "), "edge")

    def test_is_valid_identifier(self):
        self.assertTrue(is_valid_identifier("tool_shop"))
        self.assertTrue(is_valid_identifier("a-b-1"))
        self.assertFalse(is_valid_identifier(""))
        self.assertFalse(is_valid_identifier("鉄腕"))
        self.assertFalse(is_valid_identifier("a/b"))
        self.assertFalse(is_valid_identifier("-shop"))

    # 各作成経路が渡す形をここで固定する (呼び出し側の引数ミスは
    # それぞれの経路のテストが受け持つ)

    def test_building_shape(self):
        taken = set()
        self.assertEqual(
            build_identifier("Tea House", "city_a", stem="building",
                             exists=taken.__contains__),
            "tea_house_city_a",
        )
        self.assertEqual(
            build_identifier("鉄腕の道具店", "city_a", stem="building",
                             exists=taken.__contains__),
            "building_1_city_a",
        )

    def test_region_shape_keeps_prefix_in_both_branches(self):
        taken = set()
        self.assertEqual(
            build_identifier("Mist Valley", "city_a", prefix="region",
                             exists=taken.__contains__),
            "region_mist_valley_city_a",
        )
        self.assertEqual(
            build_identifier("霧降りの森", "city_a", prefix="region",
                             exists=taken.__contains__),
            "region_1_city_a",
        )

    def test_private_room_shape(self):
        taken = set()
        self.assertEqual(
            build_identifier("sophie", "city_a", "room", stem="persona",
                             exists=taken.__contains__),
            "sophie_city_a_room",
        )
        self.assertEqual(
            build_identifier("エア", "city_a", "room", stem="persona",
                             exists=taken.__contains__),
            "persona_1_city_a_room",
        )

    # ensure_unique — ユーザーが ID を選んでいない派生 ID (私室) 用

    def test_ensure_unique_falls_to_numbered_when_name_derived_is_taken(self):
        # slug 化は情報を落とすので「A店」と「A森」はどちらも a になる。
        # 名前・AIID の重複検査を通っても私室 ID は衝突しうる
        taken = {"a_city_a_room"}
        self.assertEqual(
            build_identifier("A森", "city_a", "room", stem="persona",
                             ensure_unique=True, exists=taken.__contains__),
            "persona_1_city_a_room",
        )

    def test_ensure_unique_keeps_name_derived_when_free(self):
        taken = set()
        self.assertEqual(
            build_identifier("A森", "city_a", "room", stem="persona",
                             ensure_unique=True, exists=taken.__contains__),
            "a_city_a_room",
        )

    def test_without_ensure_unique_the_collision_is_left_to_the_caller(self):
        # Building / Region はユーザーが名前を選ぶので、衝突は呼び出し元が
        # 「already exists」で伝える (連番で黙って別 ID にしない)
        taken = {"tea_house_city_a"}
        self.assertEqual(
            build_identifier("Tea House", "city_a", stem="building",
                             exists=taken.__contains__),
            "tea_house_city_a",
        )

    def test_windows_hostile_names_are_rejected_as_path_components(self):
        from manager.ids import is_safe_path_component

        # 末尾のドット・空白は Windows が黙って落とすので別名になる
        self.assertFalse(is_safe_path_component("alice."))
        self.assertFalse(is_safe_path_component("alice "))
        # DOS デバイス名は拡張子を付けてもデバイス扱い
        self.assertFalse(is_safe_path_component("CON"))
        self.assertFalse(is_safe_path_component("con.txt"))
        self.assertFalse(is_safe_path_component("LPT1"))
        # 普通の名前は通る (日本語は文字種の話であってパス境界の話ではない)
        self.assertTrue(is_safe_path_component("alice_city_a"))
        self.assertTrue(is_safe_path_component("エア_city_a"))
        self.assertTrue(is_safe_path_component("console_city_a"))

    def test_entrance_id_is_derived_in_one_place(self):
        self.assertEqual(entrance_id_for("region_1_city_a"),
                         "entrance_region_1_city_a")

    def test_numbered_fallback_skips_taken(self):
        taken = {"building_1_city_a", "building_2_city_a"}
        self.assertEqual(
            build_identifier("霧雨の宿亭", "city_a", stem="building",
                             exists=taken.__contains__),
            "building_3_city_a",
        )

    def test_non_ascii_city_drops_out_of_the_tail(self):
        # City 名の文字種は issue 論点 3 で未着手 — tail に日本語が来ても
        # ID には混ぜない (混ぜると Building ID の契約が City 経由で破れる)
        taken = set()
        generated = build_identifier("Tea House", "都", stem="building",
                                     exists=taken.__contains__)
        self.assertEqual(generated, "tea_house")
        self.assertTrue(is_valid_identifier(generated))

    def test_every_branch_satisfies_the_contract(self):
        taken = set()
        for head in ("Tea House", "鉄腕の道具店", "Bob's Bar", "  ", "Cafe 森"):
            for kwargs in ({"stem": "building"}, {"prefix": "region"}):
                generated = build_identifier(head, "city_a",
                                             exists=taken.__contains__, **kwargs)
                self.assertTrue(
                    is_valid_identifier(generated),
                    f"{head!r} {kwargs} -> {generated!r} が契約を満たさない",
                )


if __name__ == "__main__":
    unittest.main()
