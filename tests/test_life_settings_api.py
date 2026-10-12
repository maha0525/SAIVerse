"""ライフ設定のまとめ読み書き API のテスト (v0.4 計画 段 3-8 の管理画面の土台)。

対象: GET / PUT /api/people/{id}/life-settings (api/routes/people/life.py)

ここで固定すること:

- GET は起床・就寝の行 (PersonaSchedule) と起床の行の PLAYBOOK_PARAMS から、
  設定値 (T・モード上書き) と実効値 (床・均等の上限)・一日の目安をまとめて返す
- PUT は渡された欄だけを変える。T は**設定値のまま**保存し、丸めは読み時に掛かる
- 起床・就寝の新設・変更が ScheduleManager の再登録 (予約の再照合) まで届く。
  null は行の無効化 (読み手から見えない = 未設定) で、予約も取り消される
- autonomy_enabled の切り替えは DB・in-memory・ensure_autonomy_for に届く
- 不正な値は 400、未知のペルソナは 404

TestClient はワーカースレッドでルートを実行するため、DB は :memory: でなく
file sqlite + check_same_thread=False で共有する (test_schedule_api_sync.py と
同じ流儀)。LLM は呼ばない。
"""
from __future__ import annotations

import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import AI, Base, City, PersonaSchedule, User
from saiverse.autonomy_wiring import (
    LIFE_END_SCHEDULE_PLAYBOOK,
    LIFE_START_SCHEDULE_PLAYBOOK,
)

PERSONA_ID = "alice"


class LifeSettingsApiTest(unittest.TestCase):
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

        from saiverse.event_scheduler import EventScheduler
        from saiverse.schedule_manager import ScheduleManager

        self.persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            autonomy_enabled=False,
            model=None,  # provider 不明 → 自由モード
        )
        self.ensure_calls = []
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas={PERSONA_ID: self.persona},
            event_scheduler=EventScheduler(),  # start しない (予約 heap のみ)
            ensure_autonomy_for=self.ensure_calls.append,
        )
        self.manager.schedule_manager = ScheduleManager(saiverse_manager=self.manager)

        from api.routes.people import life as life_route

        app = FastAPI()
        app.include_router(life_route.router, prefix="/api/people")
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
            city = City(USERID=1, CITY_SLUG="city_a", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(AI(
                AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="アリス",
                AUTONOMY_ENABLED=False,
            ))
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _url(self, persona_id=PERSONA_ID):
        return f"/api/people/{persona_id}/life-settings"

    def _put(self, payload, status=200):
        resp = self.client.put(self._url(), json=payload)
        self.assertEqual(resp.status_code, status, resp.text)
        return resp.json()

    def _rows(self, meta_playbook):
        db = self.Session()
        try:
            rows = (
                db.query(PersonaSchedule)
                .filter(
                    PersonaSchedule.PERSONA_ID == PERSONA_ID,
                    PersonaSchedule.META_PLAYBOOK == meta_playbook,
                )
                .order_by(PersonaSchedule.SCHEDULE_ID.asc())
                .all()
            )
            return [
                SimpleNamespace(
                    id=r.SCHEDULE_ID, time=r.TIME_OF_DAY, enabled=r.ENABLED,
                    params=json.loads(r.PLAYBOOK_PARAMS) if r.PLAYBOOK_PARAMS else None,
                    generation=r.SYNC_GENERATION,
                )
                for r in rows
            ]
        finally:
            db.close()

    def _assert_reserved_at_current_generation(self, row):
        from saiverse.schedule_manager import _schedule_key

        self.assertTrue(self.manager.event_scheduler.has_key(_schedule_key(row.id)))
        registered = self.manager.schedule_manager._registered.get(row.id)
        self.assertIsNotNone(registered)
        self.assertEqual(registered[1], row.generation)

    def _autonomy_in_db(self):
        db = self.Session()
        try:
            return db.query(AI.AUTONOMY_ENABLED).filter(AI.AIID == PERSONA_ID).scalar()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def test_get_without_any_life_rows(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "wake": None,
            "close": None,
            "autonomy_enabled": False,
            "tick_interval_minutes": None,
            "effective_tick_interval_minutes": 30,
            "mode": {"derived": "free", "override": None, "effective": "free"},
            "window_minutes": None,
            "estimated_ticks_per_day": None,
            "scheduler_synced": None,
        })

    def test_get_unknown_persona_is_404(self):
        self.assertEqual(self.client.get(self._url("nobody")).status_code, 404)
        self.assertEqual(
            self.client.put(self._url("nobody"), json={"wake": "07:00"}).status_code, 404,
        )

    # ------------------------------------------------------------------
    # PUT: 起床・就寝の新設と変更 → 予約の再照合
    # ------------------------------------------------------------------

    def test_put_creates_wake_and_close_and_registers_them(self):
        body = self._put({"wake": "07:00", "close": "22:00"})
        self.assertEqual(body["wake"], "07:00")
        self.assertEqual(body["close"], "22:00")
        self.assertEqual(body["window_minutes"], 15 * 60)
        self.assertEqual(body["estimated_ticks_per_day"], 30)  # 900 / 30
        self.assertIs(body["scheduler_synced"], True)

        (wake_row,) = self._rows(LIFE_START_SCHEDULE_PLAYBOOK)
        (close_row,) = self._rows(LIFE_END_SCHEDULE_PLAYBOOK)
        self.assertEqual((wake_row.time, wake_row.enabled, wake_row.generation), ("07:00", True, 1))
        self.assertEqual((close_row.time, close_row.enabled), ("22:00", True))
        self._assert_reserved_at_current_generation(wake_row)
        self._assert_reserved_at_current_generation(close_row)

    def test_put_changes_only_the_given_field_and_reregisters(self):
        self._put({"wake": "07:00", "close": "22:00"})
        (close_before,) = self._rows(LIFE_END_SCHEDULE_PLAYBOOK)

        body = self._put({"wake": "08:30"})
        self.assertEqual(body["wake"], "08:30")
        self.assertEqual(body["close"], "22:00")

        (wake_row,) = self._rows(LIFE_START_SCHEDULE_PLAYBOOK)  # 増えず、同じ行を書き換える
        self.assertEqual(wake_row.time, "08:30")
        self.assertEqual(wake_row.generation, 2)  # 世代 +1
        self._assert_reserved_at_current_generation(wake_row)
        # 就寝の行は触らない
        (close_after,) = self._rows(LIFE_END_SCHEDULE_PLAYBOOK)
        self.assertEqual(close_after.generation, close_before.generation)

    def test_put_null_wake_disables_the_row_and_cancels_the_reservation(self):
        from saiverse.schedule_manager import _schedule_key

        self._put({"wake": "07:00", "close": "22:00", "tick_interval_minutes": 20})
        (wake_row,) = self._rows(LIFE_START_SCHEDULE_PLAYBOOK)

        body = self._put({"wake": None})
        self.assertIsNone(body["wake"])
        self.assertIsNone(body["tick_interval_minutes"])  # 読み手から見えない
        self.assertIsNone(body["estimated_ticks_per_day"])
        (after,) = self._rows(LIFE_START_SCHEDULE_PLAYBOOK)
        self.assertFalse(after.enabled)  # 行は消さない
        self.assertEqual(after.params, {"tick_interval_minutes": 20})
        self.assertFalse(
            self.manager.event_scheduler.has_key(_schedule_key(wake_row.id)),
        )

        # 起床を設定し直すと、同じ行が戻り T も戻ってくる
        body = self._put({"wake": "06:00"})
        self.assertEqual(body["wake"], "06:00")
        self.assertEqual(body["tick_interval_minutes"], 20)
        (again,) = self._rows(LIFE_START_SCHEDULE_PLAYBOOK)
        self.assertEqual(again.id, wake_row.id)
        self._assert_reserved_at_current_generation(again)

    def test_put_same_wake_and_close_is_400(self):
        self._put({"wake": "07:00", "close": "07:00"}, status=400)
        self._put({"wake": "07:00", "close": "22:00"})
        self._put({"close": "07:00"}, status=400)
        self.assertEqual(self._rows(LIFE_END_SCHEDULE_PLAYBOOK)[0].time, "22:00")

    # ------------------------------------------------------------------
    # PUT: T とモード上書き
    # ------------------------------------------------------------------

    def test_tick_interval_is_saved_as_set_and_rounded_only_when_read(self):
        self._put({"wake": "07:00", "close": "22:00"})

        body = self._put({"tick_interval_minutes": 5})
        self.assertEqual(body["tick_interval_minutes"], 5)            # 設定値のまま
        self.assertEqual(body["effective_tick_interval_minutes"], 10)  # 床 10
        (wake_row,) = self._rows(LIFE_START_SCHEDULE_PLAYBOOK)
        self.assertEqual(wake_row.params, {"tick_interval_minutes": 5})
        self._assert_reserved_at_current_generation(wake_row)

        body = self._put({"tick_interval_minutes": 90, "life_mode_override": "even"})
        self.assertEqual(body["tick_interval_minutes"], 90)
        self.assertEqual(body["effective_tick_interval_minutes"], 50)  # 均等の上限
        self.assertEqual(body["mode"], {"derived": "free", "override": "even", "effective": "even"})
        self.assertEqual(body["estimated_ticks_per_day"], 18)  # ceil(900 / 50)
        self.assertEqual(
            self._rows(LIFE_START_SCHEDULE_PLAYBOOK)[0].params,
            {"tick_interval_minutes": 90, "life_mode_override": "even"},
        )

        # null で既定へ戻す (キーを消す)。他の鍵は残る
        body = self._put({"tick_interval_minutes": None})
        self.assertIsNone(body["tick_interval_minutes"])
        self.assertEqual(body["effective_tick_interval_minutes"], 30)
        self.assertEqual(
            self._rows(LIFE_START_SCHEDULE_PLAYBOOK)[0].params,
            {"life_mode_override": "even"},
        )
        body = self._put({"life_mode_override": None})
        self.assertEqual(body["mode"]["override"], None)
        self.assertIsNone(self._rows(LIFE_START_SCHEDULE_PLAYBOOK)[0].params)

    def test_tick_interval_keeps_unknown_params(self):
        """起床の行の PLAYBOOK_PARAMS にある他の鍵は消さない。"""
        db = self.Session()
        try:
            db.add(PersonaSchedule(
                PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
                META_PLAYBOOK=LIFE_START_SCHEDULE_PLAYBOOK, ENABLED=True,
                TIME_OF_DAY="07:00", SYNC_GENERATION=1,
                PLAYBOOK_PARAMS=json.dumps({"daily_budget_pulses": 12}),
            ))
            db.commit()
        finally:
            db.close()
        self._put({"tick_interval_minutes": 45})
        self.assertEqual(
            self._rows(LIFE_START_SCHEDULE_PLAYBOOK)[0].params,
            {"daily_budget_pulses": 12, "tick_interval_minutes": 45},
        )

    def test_tick_interval_without_wake_is_400(self):
        self._put({"tick_interval_minutes": 20}, status=400)
        self._put({"life_mode_override": "free"}, status=400)
        self.assertEqual(self._rows(LIFE_START_SCHEDULE_PLAYBOOK), [])
        # 同じ要求で起床を新設すれば通る
        body = self._put({"wake": "07:00", "tick_interval_minutes": 20})
        self.assertEqual(body["tick_interval_minutes"], 20)

    def test_invalid_values_are_400_and_nothing_is_written(self):
        self._put({"wake": "07:00", "close": "22:00"})
        for payload in (
            {"tick_interval_minutes": 0},
            {"tick_interval_minutes": -5},
            {"tick_interval_minutes": "30"},
            {"tick_interval_minutes": 12.5},
            {"tick_interval_minutes": True},
            {"life_mode_override": "fast"},
            {"wake": "7:00"},
            {"close": "25:00"},
            {"autonomy_enabled": "yes"},
            {"autonomy_enabled": None},
        ):
            with self.subTest(payload=payload):
                self._put(payload, status=400)
        (wake_row,) = self._rows(LIFE_START_SCHEDULE_PLAYBOOK)
        self.assertEqual((wake_row.time, wake_row.params, wake_row.generation), ("07:00", None, 1))
        self.assertEqual(self.ensure_calls, [])

    # ------------------------------------------------------------------
    # PUT: 自律 ON/OFF
    # ------------------------------------------------------------------

    def test_autonomy_toggle_reaches_db_persona_and_ensure_autonomy_for(self):
        body = self._put({"autonomy_enabled": True})
        self.assertIs(body["autonomy_enabled"], True)
        self.assertIs(self._autonomy_in_db(), True)
        self.assertIs(self.persona.autonomy_enabled, True)
        self.assertEqual(self.ensure_calls, [PERSONA_ID])
        self.assertIsNone(body["scheduler_synced"])  # 行に触れていない

        # 同じ値の保存は同期を起こさない (ペルソナ設定の保存と同じ)
        self._put({"autonomy_enabled": True})
        self.assertEqual(self.ensure_calls, [PERSONA_ID])

        body = self._put({"autonomy_enabled": False})
        self.assertIs(body["autonomy_enabled"], False)
        self.assertIs(self._autonomy_in_db(), False)
        self.assertIs(self.persona.autonomy_enabled, False)
        self.assertEqual(self.ensure_calls, [PERSONA_ID, PERSONA_ID])

    def test_autonomy_setting_is_shown_as_set_even_under_the_global_all_stop(self):
        """表示するのは設定値 (全停止の門は通さない)。"""
        self.manager._autonomy_all_stop = True
        body = self._put({"autonomy_enabled": True})
        self.assertIs(body["autonomy_enabled"], True)
        self.assertIs(
            self.client.get(self._url()).json()["autonomy_enabled"], True,
        )


if __name__ == "__main__":
    unittest.main()
