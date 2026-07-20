"""スケジュール API の世代 bump + scheduler_synced (W3 Chunk A) の HTTP テスト。

docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md D2 / D7:
- create は SYNC_GENERATION=1、update / toggle は同一 commit で +1
- register/unregister の成否を scheduler_synced で応答に明示 (HTTP は 200 のまま)
- SYNC_GENERATION 列は try_additive_migration の軽量 ALTER パスで既存 DB に
  足せる (既存行は 0)

TestClient はワーカースレッドでルートを実行するため、DB は :memory: でなく
file sqlite + check_same_thread=False で共有する (test_life_settings_api.py と
同じ流儀)。
"""
from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import AI, Base, City, PersonaSchedule, User

PERSONA_ID = "alice"


class ScheduleApiSyncTest(unittest.TestCase):
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
        self._seed()

        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            schedule_manager=SimpleNamespace(
                # register_schedule は tri-state str を返す契約 (Codex W3 指摘 2)
                register_schedule=lambda schedule_id: "registered",
                unregister_schedule=lambda schedule_id: None,
            ),
        )

        from api.routes.people import schedule as schedule_route

        app = FastAPI()
        app.include_router(schedule_route.router, prefix="/api/people")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    def _seed(self):
        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITYNAME="city_a", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="アリス"))
            db.commit()
        finally:
            db.close()

    def _generation(self, schedule_id: int) -> int:
        db = self.Session()
        try:
            row = db.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id,
            ).first()
            self.assertIsNotNone(row)
            return row.SYNC_GENERATION
        finally:
            db.close()

    def _create(self, **overrides) -> dict:
        payload = {
            "schedule_type": "periodic",
            "meta_playbook": "meta_user",
            "time_of_day": "09:00",
        }
        payload.update(overrides)
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules", json=payload,
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _fail_scheduler(self):
        def _boom(schedule_id):
            raise RuntimeError("scheduler down")

        self.manager.schedule_manager = SimpleNamespace(
            register_schedule=_boom, unregister_schedule=_boom,
        )

    def _install_real_schedule_manager(self):
        """mock でなく実 ScheduleManager + EventScheduler を配線する。

        register_schedule の tri-state 分類 (registered /
        no_reservation_needed / not_registrable) を実コードで検証するため
        (Codex W3 指摘 2)。EventScheduler は start() しない (予約 heap のみ)。
        """
        from saiverse.event_scheduler import EventScheduler
        from saiverse.schedule_manager import ScheduleManager

        self.manager.event_scheduler = EventScheduler()
        self.manager.schedule_manager = ScheduleManager(
            saiverse_manager=self.manager
        )

    # ------------------------------------------------------------------
    # SYNC_GENERATION: create=1 / update・toggle は +1
    # ------------------------------------------------------------------

    def test_create_stamps_generation_1(self):
        body = self._create()
        self.assertEqual(self._generation(body["schedule_id"]), 1)

    def test_update_bumps_generation(self):
        schedule_id = self._create()["schedule_id"]
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"time_of_day": "10:00"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._generation(schedule_id), 2)

        # 二度目の update でさらに +1
        self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"time_of_day": "11:00"},
        )
        self.assertEqual(self._generation(schedule_id), 3)

    def test_toggle_bumps_generation(self):
        schedule_id = self._create()["schedule_id"]
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}/toggle",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])
        self.assertEqual(self._generation(schedule_id), 2)

    # ------------------------------------------------------------------
    # scheduler_synced: register/unregister の成否を応答に明示
    # ------------------------------------------------------------------

    def test_create_reports_scheduler_synced_true(self):
        body = self._create()
        self.assertIs(body["scheduler_synced"], True)

    def test_create_reports_scheduler_synced_false_when_register_fails(self):
        self._fail_scheduler()
        body = self._create()
        # HTTP 200 のまま (DB が正典、reconciliation が回復する)
        self.assertIs(body["scheduler_synced"], False)
        # DB への保存自体は成功している (世代も 1)
        self.assertEqual(self._generation(body["schedule_id"]), 1)

    def test_update_reports_scheduler_synced(self):
        schedule_id = self._create()["schedule_id"]
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"time_of_day": "10:00"},
        )
        self.assertIs(resp.json()["scheduler_synced"], True)

        self._fail_scheduler()
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"time_of_day": "11:00"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["scheduler_synced"], False)

    def test_toggle_reports_scheduler_synced(self):
        schedule_id = self._create()["schedule_id"]
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}/toggle",
        )
        self.assertIs(resp.json()["scheduler_synced"], True)

        self._fail_scheduler()
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}/toggle",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["scheduler_synced"], False)

    def test_delete_reports_scheduler_synced(self):
        schedule_id = self._create()["schedule_id"]
        resp = self.client.delete(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["scheduler_synced"], True)

        self._fail_scheduler()
        schedule_id = self._create()["schedule_id"]
        resp = self.client.delete(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["scheduler_synced"], False)

    # ------------------------------------------------------------------
    # tri-state (Codex W3 指摘 2): 例外なしの「登録不能」も synced=False、
    # 「予約が無いのが正」は True — 実 ScheduleManager で検証
    # ------------------------------------------------------------------

    def _set_completed(self, schedule_id: int):
        db = self.Session()
        try:
            row = db.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id,
            ).first()
            row.COMPLETED = True
            db.commit()
        finally:
            db.close()

    def test_create_periodic_without_time_of_day_reports_not_synced(self):
        """有効な periodic を TIME_OF_DAY なしで作成 → 予約を作れず、
        reconciliation でも回復不能なので scheduler_synced=False (Codex 再現)。"""
        self._install_real_schedule_manager()
        body = self._create(time_of_day=None)
        self.assertEqual(body["success"], True)
        self.assertIs(body["scheduler_synced"], False)

    def test_update_completed_oneshot_reports_synced(self):
        """完了済み oneshot の update は「予約が無いのが正」なので True。"""
        self._install_real_schedule_manager()
        body = self._create(
            schedule_type="oneshot",
            time_of_day=None,
            scheduled_datetime="2026-01-01 09:00",
        )
        schedule_id = body["schedule_id"]
        self._set_completed(schedule_id)
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"description": "done"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["scheduler_synced"], True)

    def test_toggle_completed_oneshot_reports_synced(self):
        """完了済み oneshot のトグル (OFF→ON とも) は True。"""
        self._install_real_schedule_manager()
        body = self._create(
            schedule_type="oneshot",
            time_of_day=None,
            scheduled_datetime="2026-01-01 09:00",
        )
        schedule_id = body["schedule_id"]
        self._set_completed(schedule_id)
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}/toggle",
        )  # disable
        self.assertIs(resp.json()["scheduler_synced"], True)
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}/toggle",
        )  # enable — 完了済みなので予約は不要のまま True
        self.assertIs(resp.json()["scheduler_synced"], True)

    def test_toggle_disable_reports_synced_with_real_manager(self):
        """有効な periodic を disable トグル → 予約解除のみで True。"""
        self._install_real_schedule_manager()
        schedule_id = self._create()["schedule_id"]
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}/toggle",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])
        self.assertIs(resp.json()["scheduler_synced"], True)

    def test_toggle_enable_unregistrable_reports_not_synced(self):
        """TIME_OF_DAY 欠落の periodic を enable トグル → False。"""
        self._install_real_schedule_manager()
        schedule_id = self._create(time_of_day=None, enabled=False)["schedule_id"]
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}/toggle",
        )  # enable
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["enabled"])
        self.assertIs(resp.json()["scheduler_synced"], False)


class SyncGenerationMigrationTest(unittest.TestCase):
    """W3 で足した 2 列が追加系 (軽量 ALTER) パスで適用されること。

    try_additive_migration はモデル (Base.metadata) と DB のスキーマ差分から
    自動で ALTER TABLE ADD COLUMN を発行する。SYNC_GENERATION は NOT NULL +
    scalar default 0、INSTANCE_TOKEN (Codex W3 第三陣) は nullable なので、
    どちらも全書換に落ちない。INSTANCE_TOKEN の既存行 NULL は
    backfill_schedule_instance_tokens が行ごとに異なるトークンで埋める。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "old.db")
        # 現行スキーマで作ってから W3 の 2 列を落とし「W3 以前」の状態に
        # 戻す (SQLite 3.35+ の DROP COLUMN)。既存行を 2 件入れておく
        # (backfill のトークンが行ごとに異なることを見るため)。
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            Base.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text(
                    'ALTER TABLE persona_schedule DROP COLUMN "SYNC_GENERATION"'
                ))
                conn.execute(text(
                    'ALTER TABLE persona_schedule DROP COLUMN "INSTANCE_TOKEN"'
                ))
                conn.execute(text(
                    "INSERT INTO persona_schedule "
                    "(PERSONA_ID, SCHEDULE_TYPE, META_PLAYBOOK, ENABLED, "
                    " DESCRIPTION, PRIORITY, TIME_OF_DAY, COMPLETED) "
                    "VALUES ('p1', 'periodic', 'meta_user', 1, '', 0, '09:00', 0)"
                ))
                conn.execute(text(
                    "INSERT INTO persona_schedule "
                    "(PERSONA_ID, SCHEDULE_TYPE, META_PLAYBOOK, ENABLED, "
                    " DESCRIPTION, PRIORITY, TIME_OF_DAY, COMPLETED) "
                    "VALUES ('p2', 'periodic', 'meta_user', 1, '', 0, '10:00', 0)"
                ))
        finally:
            engine.dispose()

    def tearDown(self):
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_additive_migration_adds_column_with_default_0(self):
        from database.migrate import needs_migration, try_additive_migration

        self.assertTrue(needs_migration(self.db_path))
        # 追加系パスだけで差分が解消される (= 全書換に落ちない) こと
        self.assertTrue(try_additive_migration(self.db_path))
        self.assertFalse(needs_migration(self.db_path))

        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            insp = inspect(engine)
            cols = {c["name"] for c in insp.get_columns("persona_schedule")}
            self.assertIn("SYNC_GENERATION", cols)
            self.assertIn("INSTANCE_TOKEN", cols)
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT SYNC_GENERATION, INSTANCE_TOKEN FROM persona_schedule"
                )).fetchall()
            # 既存行の世代は 0、トークンは NULL (backfill が埋めるまで)
            self.assertEqual([tuple(r) for r in rows], [(0, None), (0, None)])
        finally:
            engine.dispose()

    def test_backfill_fills_null_instance_tokens(self):
        """migration 後の NULL INSTANCE_TOKEN が backfill で行ごとに異なる
        トークンで埋まる (Codex W3 第三陣 回帰 (d))。冪等: 再実行で変わらない。"""
        from database.migrate import (
            backfill_schedule_instance_tokens,
            try_additive_migration,
        )

        self.assertTrue(try_additive_migration(self.db_path))
        backfill_schedule_instance_tokens(self.db_path)

        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            with engine.connect() as conn:
                tokens = [r[0] for r in conn.execute(text(
                    "SELECT INSTANCE_TOKEN FROM persona_schedule ORDER BY SCHEDULE_ID"
                )).fetchall()]
            self.assertEqual(len(tokens), 2)
            for token in tokens:
                self.assertIsNotNone(token)
                self.assertEqual(len(token), 12)  # randomblob(6) → hex 12 文字
                self.assertEqual(token, token.lower())
            self.assertNotEqual(tokens[0], tokens[1])

            # 冪等: 既存トークンは再実行で変わらない
            backfill_schedule_instance_tokens(self.db_path)
            with engine.connect() as conn:
                tokens_after = [r[0] for r in conn.execute(text(
                    "SELECT INSTANCE_TOKEN FROM persona_schedule ORDER BY SCHEDULE_ID"
                )).fetchall()]
            self.assertEqual(tokens, tokens_after)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
