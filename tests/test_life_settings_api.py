"""ライフ設定 API (api/routes/people/life_settings.py) の HTTP テスト。

life.md v0.5 §9.2-1 (改修B): 起床・就寝・予算を「エアの一日」として1画面で
設定するための GET/PUT。TestClient で HTTP 経由の検証 (最低予算のバリデー
ション・不正値の 400・PersonaSchedule への実際の upsert を含む)。

TestClient はワーカースレッドでルートを実行するため、DB は :memory: でなく
file sqlite + check_same_thread=False で共有する (test_life_view_api.py と
同じ流儀)。
"""
from __future__ import annotations

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

        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas={
                PERSONA_ID: SimpleNamespace(persona_id=PERSONA_ID, model="claude-sonnet-5"),
            },
            schedule_manager=SimpleNamespace(register_schedule=lambda schedule_id: None),
        )

        from api.routes.people import life_settings as life_settings_route

        app = FastAPI()
        app.include_router(life_settings_route.router, prefix="/api/people")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def _cleanup_temp(self):
        import gc
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

    def _rows(self):
        db = self.Session()
        try:
            return db.query(PersonaSchedule).filter(
                PersonaSchedule.PERSONA_ID == PERSONA_ID,
            ).all()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def test_get_unset_returns_none_wake_close(self):
        resp = self.client.get(f"/api/people/{PERSONA_ID}/life-settings")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body["wake"])
        self.assertIsNone(body["close"])
        self.assertIsNone(body["daily_budget_rounds"])
        self.assertIsNone(body["daily_budget_pulses"])
        self.assertIsNone(body["life_mode_override"])
        self.assertEqual(body["derived_mode"], "even")  # claude-sonnet-5 は均等が自動判定
        self.assertEqual(body["effective_mode"], "even")
        self.assertIsNone(body["min_budget_pulses"])  # wake/close が揃っていないので計算不可
        self.assertIsNone(body["window_minutes"])
        self.assertEqual(body["default_budget_rounds"], 8)

    def test_get_unknown_persona_404(self):
        resp = self.client.get("/api/people/nobody/life-settings")
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # PUT: 保存と反映
    # ------------------------------------------------------------------

    def test_put_saves_and_get_reflects(self):
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "07:00", "close": "22:00", "daily_budget_pulses": 30},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["wake"], "07:00")
        self.assertEqual(body["close"], "22:00")
        self.assertEqual(body["daily_budget_pulses"], 30)
        self.assertEqual(body["window_minutes"], 900)
        self.assertEqual(body["min_budget_pulses"], 18)  # ceil(900/50)

        rows = self._rows()
        self.assertEqual(len(rows), 2)
        by_playbook = {r.META_PLAYBOOK: r for r in rows}
        self.assertEqual(by_playbook["judgment_day_open"].TIME_OF_DAY, "07:00")
        self.assertEqual(by_playbook["judgment_day_close"].TIME_OF_DAY, "22:00")
        self.assertTrue(by_playbook["judgment_day_open"].ENABLED)
        self.assertTrue(by_playbook["judgment_day_close"].ENABLED)
        params = json.loads(by_playbook["judgment_day_open"].PLAYBOOK_PARAMS)
        self.assertEqual(params["daily_budget_pulses"], 30)

        get_resp = self.client.get(f"/api/people/{PERSONA_ID}/life-settings")
        self.assertEqual(get_resp.json()["daily_budget_pulses"], 30)

    def test_put_updates_existing_row_without_duplicating(self):
        self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "07:00", "close": "22:00"},
        )
        self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "08:00", "close": "23:00"},
        )
        rows = self._rows()
        self.assertEqual(len(rows), 2)  # upsert — 重複行が増えない
        by_playbook = {r.META_PLAYBOOK: r for r in rows}
        self.assertEqual(by_playbook["judgment_day_open"].TIME_OF_DAY, "08:00")
        self.assertEqual(by_playbook["judgment_day_close"].TIME_OF_DAY, "23:00")

    # ------------------------------------------------------------------
    # PUT: バリデーション (life.md v0.5 §3「不正な値は書ける口をなくす」の
    # 設定 UI 側の実行 — 保存前に理由を明示して弾く)
    # ------------------------------------------------------------------

    def test_put_rejects_invalid_time_format(self):
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "7:00", "close": "22:00"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_rejects_same_wake_and_close(self):
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "07:00", "close": "07:00"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_rejects_budget_below_minimum(self):
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "07:00", "close": "13:00", "daily_budget_pulses": 3},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("最低", resp.json()["detail"])
        # 拒否された PUT はどの行も作らない (副作用なし)
        self.assertEqual(self._rows(), [])

    def test_put_rejects_invalid_mode_override(self):
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "07:00", "close": "22:00", "life_mode_override": "bogus"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_rejects_non_positive_budget_rounds(self):
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "07:00", "close": "22:00", "daily_budget_rounds": 0},
        )
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------------
    # PUT: モード上書き
    # ------------------------------------------------------------------

    def test_put_mode_override_reflected_in_effective_mode(self):
        # persona のモデルは "claude-sonnet-5" (均等が自動判定) だが free に上書き
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/life-settings",
            json={"wake": "07:00", "close": "22:00", "life_mode_override": "free"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["derived_mode"], "even")
        self.assertEqual(body["effective_mode"], "free")
        self.assertEqual(body["life_mode_override"], "free")
        self.assertEqual(body["min_budget_pulses"], 1)  # 自由モードの最低値


if __name__ == "__main__":
    unittest.main()
