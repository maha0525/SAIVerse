"""City の識別子 (CITY_SLUG) と表示名 (CITYNAME) の分離。

docs/intent/city_identity.md。旧スキーマでは CITYNAME が内部の識別子で、表示名は
DESCRIPTION に置かれていた (チュートリアルの書き込み先がそこだった)。この分離で

- CITY_SLUG = 内部の識別子。起動引数・user_room の BUILDINGID・ペルソナ ID・
  建物ログの保存先・二重起動チェックの鍵の材料。**City 作成後は変更できない**
- CITYNAME  = 表示名。自由な文字列で一意性を要求しない。空なら CITY_SLUG を表示
- DESCRIPTION = 街の説明文

を不変条件とする。ここでは既存 DB の移行・識別子の不変性・表示名の編集経路を固定する。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.migrate import (
    apply_known_column_renames,
    backfill_city_display_names,
    try_additive_migration,
)
from database.models import Base, Building as BuildingModel, City as CityModel, User as UserModel
from manager.admin import AdminService

# 旧スキーマの city テーブル (CITYNAME = 識別子、表示名の列は無い)。
# まはーの実 DB から写した形 — 一意制約が CITYNAME に載っているところまで再現する
# (改名 ALTER がこの制約を CITY_SLUG へ連れて行けるかが移行の要)。
_LEGACY_CITY_DDL = """
CREATE TABLE city (
    "USERID" INTEGER NOT NULL,
    "CITYID" INTEGER NOT NULL,
    "CITYNAME" VARCHAR(32) NOT NULL,
    "DESCRIPTION" VARCHAR(1024) NOT NULL,
    "TIMEZONE" VARCHAR(64) NOT NULL,
    "UI_PORT" INTEGER NOT NULL,
    "API_PORT" INTEGER NOT NULL,
    "START_IN_ONLINE_MODE" BOOLEAN NOT NULL,
    "HOST_AVATAR_IMAGE" VARCHAR(255),
    "MAP_BACKGROUND_IMAGE" VARCHAR(512),
    "LAST_KNOWN_VERSION" VARCHAR(64),
    PRIMARY KEY ("CITYID"),
    CONSTRAINT uq_user_city_name UNIQUE ("USERID", "CITYNAME"),
    CONSTRAINT uq_ui_port UNIQUE ("UI_PORT"),
    CONSTRAINT uq_api_port UNIQUE ("API_PORT"),
    CONSTRAINT fk_city_user FOREIGN KEY("USERID") REFERENCES user ("USERID")
)
"""


class CityIdentityMigrationTest(unittest.TestCase):
    """既存ユーザーの DB が壊れずに新しい形へ移ることを固定する。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # 現行スキーマを一式作ってから city だけ旧スキーマへ差し替える
        engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(engine)
        engine.dispose()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("DROP TABLE city")
            con.execute(_LEGACY_CITY_DDL)
            con.commit()
        finally:
            con.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _insert_legacy(self, cityname, description, ui_port=3000, api_port=8000):
        con = sqlite3.connect(self.db_path)
        try:
            next_id = con.execute(
                "SELECT COALESCE(MAX(CITYID), 0) + 1 FROM city"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO city (CITYID, USERID, CITYNAME, DESCRIPTION, TIMEZONE, "
                "UI_PORT, API_PORT, START_IN_ONLINE_MODE) "
                "VALUES (?, 1, ?, ?, 'UTC', ?, ?, 0)",
                (next_id, cityname, description, ui_port, api_port),
            )
            con.commit()
        finally:
            con.close()

    def _migrate(self):
        self.assertTrue(
            try_additive_migration(self.db_path),
            "識別子の改名 + 表示名の列追加は追加系だけで当たるはず (全書換に落ちない)",
        )
        backfill_city_display_names(self.db_path)

    def _rows(self):
        con = sqlite3.connect(self.db_path)
        try:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(
                "SELECT CITYID, CITY_SLUG, CITYNAME, DESCRIPTION FROM city ORDER BY CITYID"
            )]
        finally:
            con.close()

    def test_identifier_moves_to_slug_and_display_name_comes_from_description(self):
        # チュートリアルで街の名前を入力した世界: DESCRIPTION に表示名が入っている
        self._insert_legacy("city_a", "星降りの街")
        self._migrate()

        row = self._rows()[0]
        self.assertEqual(row["CITY_SLUG"], "city_a", "識別子は改名で退避される")
        self.assertEqual(row["CITYNAME"], "星降りの街", "表示名が復元される")
        self.assertEqual(
            row["DESCRIPTION"], "星降りの街",
            "まはー裁定 (2026-08-14): DESCRIPTION は消さずコピーする",
        )

    def test_seeded_description_becomes_the_display_name(self):
        # チュートリアルを通っていない世界: DESCRIPTION は seed の説明文のまま。
        # 表示名が一度だけ説明文になるが、マップ画面の編集で直せる (取り返しがつく)
        self._insert_legacy("city_a", "city_aの街です。")
        self._migrate()

        row = self._rows()[0]
        self.assertEqual(row["CITY_SLUG"], "city_a")
        self.assertEqual(row["CITYNAME"], "city_aの街です。")

    def test_empty_description_falls_back_to_the_slug(self):
        self._insert_legacy("city_a", "")
        self._migrate()

        row = self._rows()[0]
        self.assertEqual(row["CITYNAME"], "city_a")

    def test_multiple_cities_all_migrate(self):
        self._insert_legacy("city_a", "星降りの街", 3000, 8000)
        self._insert_legacy("city_b", "", 3001, 8001)
        self._migrate()

        rows = self._rows()
        self.assertEqual([r["CITY_SLUG"] for r in rows], ["city_a", "city_b"])
        self.assertEqual([r["CITYNAME"] for r in rows], ["星降りの街", "city_b"])

    def test_backfill_does_not_overwrite_a_name_the_user_already_set(self):
        self._insert_legacy("city_a", "古い説明文")
        self._migrate()
        # ユーザーがマップ画面から改名した後、起動のたびに走っても踏まない
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("UPDATE city SET CITYNAME = ?", ("まはーの街",))
            con.commit()
        finally:
            con.close()

        backfill_city_display_names(self.db_path)
        backfill_city_display_names(self.db_path)

        self.assertEqual(self._rows()[0]["CITYNAME"], "まはーの街")

    def test_rename_does_not_fire_twice(self):
        """移行後の DB は CITYNAME と CITY_SLUG を両方持つ。再改名で表示名を
        識別子で上書きしてはならない (この冪等性が崩れると表示名が消える)。"""
        self._insert_legacy("city_a", "星降りの街")
        self._migrate()
        apply_known_column_renames(self.db_path)

        row = self._rows()[0]
        self.assertEqual(row["CITY_SLUG"], "city_a")
        self.assertEqual(row["CITYNAME"], "星降りの街")

    def test_slug_uniqueness_survives_the_rename(self):
        """一意制約は改名に追随する (SQLite の RENAME COLUMN が索引を書き換える)。
        識別子の重複が通ると Building ID / ペルソナ ID が衝突する。"""
        self._insert_legacy("city_a", "星降りの街")
        self._migrate()

        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            with self.assertRaises(Exception):
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO city (USERID, CITY_SLUG, CITYNAME, UI_PORT, API_PORT) "
                        "VALUES (1, 'city_a', '別の街', 3009, 8009)"
                    ))
        finally:
            engine.dispose()


class CityAdminIdentityTest(unittest.TestCase):
    """AdminService — 識別子は作成時にしか決められない。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            db.add(UserModel(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.add(CityModel(
                CITYID=1, USERID=1, CITY_SLUG="city_a", CITYNAME="星降りの街",
                DESCRIPTION="静かな港町。", UI_PORT=3000, API_PORT=8000,
            ))
            db.add(BuildingModel(
                BUILDINGID="user_room_city_a", CITYID=1, BUILDINGNAME="まはーの部屋",
            ))
            db.commit()
        finally:
            db.close()

        self.svc = AdminService.__new__(AdminService)
        self.svc.SessionLocal = self.SessionLocal
        self.svc.user_room_id = "user_room_city_a"
        self.svc.state = SimpleNamespace(
            user_id=1, city_id=1, city_name="city_a",
            user_room_id="user_room_city_a", start_in_online_mode=False,
            ui_port=3000, api_port=8000, timezone_name="UTC", timezone_info=None,
            personas={},
        )
        self.svc.manager = SimpleNamespace(
            city_name="city_a", user_room_id="user_room_city_a",
            start_in_online_mode=False, ui_port=3000, api_port=8000,
            reload_host_avatar=lambda _v: None,
        )
        self.svc._update_timezone_cache = lambda _tz: None
        self.svc._load_cities_from_db = lambda: None

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _city(self):
        db = self.SessionLocal()
        try:
            return db.query(CityModel).filter_by(CITYID=1).first()
        finally:
            db.close()

    def _update(self, name, description="静かな港町。"):
        return self.svc.update_city(
            1, name, description, False, 3000, 8000, "Asia/Tokyo",
        )

    # --- 表示名の更新 ---

    def test_update_changes_the_display_name_only(self):
        result = self._update("まはーの街")
        self.assertNotIn("Error", result)

        city = self._city()
        self.assertEqual(city.CITYNAME, "まはーの街")
        self.assertEqual(city.CITY_SLUG, "city_a", "識別子は動かない")

    def test_display_name_accepts_free_text(self):
        for name in ("まはーの街", "Star-fall City", "街 その1", "★"):
            self.assertNotIn("Error", self._update(name))
            self.assertEqual(self._city().CITYNAME, name)

    def test_update_does_not_repoint_the_user_room(self):
        """旧実装はここで user_room_id をメモリ上だけ張り替え、DB の building 行と
        ディスクのログフォルダが旧名のまま取り残されていた (intent §2-4 欠陥 A)。"""
        self._update("まはーの街")

        self.assertEqual(self.svc.state.user_room_id, "user_room_city_a")
        self.assertEqual(self.svc.manager.user_room_id, "user_room_city_a")
        self.assertEqual(self.svc.user_room_id, "user_room_city_a")
        db = self.SessionLocal()
        try:
            self.assertIsNotNone(
                db.query(BuildingModel).filter_by(BUILDINGID="user_room_city_a").first()
            )
        finally:
            db.close()

    def test_update_does_not_move_the_log_directory_key(self):
        """建物ログの保存先は city_name から作られる。表示名の変更で動いてはならない。"""
        self._update("まはーの街")
        self.assertEqual(self.svc.state.city_name, "city_a")
        self.assertEqual(self.svc.manager.city_name, "city_a")

    # --- 作成 ---

    def test_create_requires_an_ascii_slug(self):
        result = self.svc.create_city("星の街", "星の街", "", 3002, 8002, "UTC")
        self.assertIn("Error", result)

    def test_create_accepts_ascii_slug_with_free_text_name(self):
        result = self.svc.create_city("city_c", "星降りの街", "説明", 3003, 8003, "UTC")
        self.assertNotIn("Error", result)

        db = self.SessionLocal()
        try:
            city = db.query(CityModel).filter_by(CITY_SLUG="city_c").first()
            self.assertIsNotNone(city)
            self.assertEqual(city.CITYNAME, "星降りの街")
            self.assertEqual(city.DESCRIPTION, "説明")
        finally:
            db.close()

    def test_create_rejects_a_duplicate_slug(self):
        result = self.svc.create_city("city_a", "別名でも", "", 3004, 8004, "UTC")
        self.assertIn("Error", result)

    def test_create_rejects_an_empty_slug(self):
        self.assertIn("Error", self.svc.create_city("", "街", "", 3005, 8005, "UTC"))


class CityDisplayNameApiTest(unittest.TestCase):
    """PATCH /api/world/cities/{id}/name — マップ画面の編集ボタンの経路。"""

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
            db.add(UserModel(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.add(CityModel(
                CITYID=1, USERID=1, CITY_SLUG="city_a", CITYNAME="星降りの街",
                DESCRIPTION="静かな港町。", UI_PORT=3000, API_PORT=8000,
            ))
            db.commit()
        finally:
            db.close()

        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            city_id=1,
            regions={},
            buildings=[],
            personas={},
            occupancy_manager=SimpleNamespace(occupants={}),
            state=SimpleNamespace(user_current_building_id=None),
        )

        from api.routes import world as world_route
        from api.routes import info as info_route

        app = FastAPI()
        app.include_router(world_route.router, prefix="/api/world")
        app.include_router(info_route.router, prefix="/api/info")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    def _city(self):
        db = self.Session()
        try:
            return db.query(CityModel).filter_by(CITYID=1).first()
        finally:
            db.close()

    def test_patch_updates_the_display_name(self):
        res = self.client.patch("/api/world/cities/1/name", json={"name": "まはーの街"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["display_name"], "まはーの街")
        self.assertEqual(self._city().CITYNAME, "まはーの街")

    def test_patch_leaves_the_identifier_and_description_alone(self):
        self.client.patch("/api/world/cities/1/name", json={"name": "まはーの街"})
        city = self._city()
        self.assertEqual(city.CITY_SLUG, "city_a")
        self.assertEqual(city.DESCRIPTION, "静かな港町。")

    def test_patch_trims_surrounding_space(self):
        res = self.client.patch("/api/world/cities/1/name", json={"name": "  余白の街  "})
        self.assertEqual(res.json()["name"], "余白の街")

    def test_patch_on_a_missing_city_is_404(self):
        res = self.client.patch("/api/world/cities/99/name", json={"name": "無い街"})
        self.assertEqual(res.status_code, 404)

    def test_empty_name_falls_back_to_the_slug_for_display(self):
        res = self.client.patch("/api/world/cities/1/name", json={"name": "   "})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "")
        self.assertEqual(res.json()["display_name"], "city_a")

    # --- 見出しの供給源 ---

    def test_city_map_returns_the_display_name(self):
        res = self.client.get("/api/info/city-map")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["city_name"], "星降りの街")

    def test_city_map_falls_back_to_the_slug_when_the_name_is_empty(self):
        db = self.Session()
        try:
            db.query(CityModel).filter_by(CITYID=1).update({"CITYNAME": ""})
            db.commit()
        finally:
            db.close()

        res = self.client.get("/api/info/city-map")
        self.assertEqual(res.json()["city_name"], "city_a")


if __name__ == "__main__":
    unittest.main()
