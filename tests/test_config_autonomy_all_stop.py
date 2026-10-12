"""開発者モードの連動の撤去とグローバル自律全停止 API のテスト (v0.4 計画 決定 12)。

対象 (api/routes/config.py):

- POST /api/config/developer-mode — 表示の切り替えだけ。OFF にしても
  ペルソナの AUTONOMY_ENABLED を書き換えない (旧連動の撤去)
- GET / POST /api/config/autonomy-all-stop — 全停止の読み書き。全停止中は
  is_autonomy_on が全員 False、解除で元の設定どおりに戻る。ペルソナごとの
  設定値は書き換えない。DB に書けなければ 500 でキャッシュも変えない

TestClient はワーカースレッドでルートを実行するため、DB は file sqlite +
check_same_thread=False で共有する。LLM は呼ばない。
"""
from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import AI, Base, City, User, UserSettings
from saiverse import autonomy_wiring as wiring

PERSONAS = {"alice": True, "bob": False, "carol": True}


class AutonomyAllStopApiTest(unittest.TestCase):
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

        self.personas = {
            pid: SimpleNamespace(persona_id=pid, autonomy_enabled=enabled)
            for pid, enabled in PERSONAS.items()
        }
        self.ensure_calls = []
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas=self.personas,
            state=SimpleNamespace(developer_mode=True, personas=self.personas),
            ensure_autonomy_for=self.ensure_calls.append,
        )

        from api.routes import config as config_route

        app = FastAPI()
        app.include_router(config_route.router, prefix="/api/config")
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
            for pid, enabled in PERSONAS.items():
                db.add(AI(
                    AIID=pid, HOME_CITYID=city.CITYID, AINAME=pid,
                    AUTONOMY_ENABLED=enabled,
                ))
            db.commit()
        finally:
            db.close()

    def _db_autonomy(self):
        db = self.Session()
        try:
            return {r.AIID: r.AUTONOMY_ENABLED for r in db.query(AI).all()}
        finally:
            db.close()

    def _db_all_stop(self):
        db = self.Session()
        try:
            row = db.query(UserSettings).filter(UserSettings.USERID == 1).first()
            return None if row is None else row.AUTONOMY_ALL_STOP
        finally:
            db.close()

    def _gate(self):
        return {pid: wiring.is_autonomy_on(self.manager, pid) for pid in PERSONAS}

    # ------------------------------------------------------------------
    # 開発者モード: 連動の撤去
    # ------------------------------------------------------------------

    def test_developer_mode_off_does_not_touch_autonomy(self):
        resp = self.client.post("/api/config/developer-mode", json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "enabled": False})
        self.assertFalse(self.manager.state.developer_mode)

        self.assertEqual(self._db_autonomy(), PERSONAS)
        self.assertEqual(
            {pid: p.autonomy_enabled for pid, p in self.personas.items()}, PERSONAS,
        )
        self.assertEqual(self.ensure_calls, [])

        resp = self.client.post("/api/config/developer-mode", json={"enabled": True})
        self.assertTrue(resp.json()["enabled"])
        self.assertEqual(self._db_autonomy(), PERSONAS)

    # ------------------------------------------------------------------
    # グローバル自律全停止
    # ------------------------------------------------------------------

    def test_all_stop_get_and_post_round_trip(self):
        self.assertEqual(self.client.get("/api/config/autonomy-all-stop").json(), {"enabled": False})
        self.assertEqual(self._gate(), PERSONAS)

        resp = self.client.post("/api/config/autonomy-all-stop", json={"enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "enabled": True})
        self.assertEqual(self.client.get("/api/config/autonomy-all-stop").json(), {"enabled": True})
        self.assertIs(self._db_all_stop(), True)
        # 全員の門が閉じる。設定値は書き換えない
        self.assertEqual(self._gate(), {pid: False for pid in PERSONAS})
        self.assertEqual(self._db_autonomy(), PERSONAS)
        # 全ペルソナの AutonomyManager を同期し直す
        self.assertEqual(sorted(self.ensure_calls), sorted(PERSONAS))

        resp = self.client.post("/api/config/autonomy-all-stop", json={"enabled": False})
        self.assertEqual(resp.json(), {"success": True, "enabled": False})
        self.assertIs(self._db_all_stop(), False)
        # 解除で元の設定どおりに戻る
        self.assertEqual(self._gate(), PERSONAS)

    def test_all_stop_is_read_back_from_the_db_by_a_fresh_manager(self):
        self.client.post("/api/config/autonomy-all-stop", json={"enabled": True})
        fresh = SimpleNamespace(SessionLocal=self.Session, personas={})
        self.assertIs(wiring.is_autonomy_all_stopped(fresh), True)

    def test_all_stop_save_failure_is_500_and_keeps_the_cache(self):
        self.assertEqual(self.client.get("/api/config/autonomy-all-stop").json(), {"enabled": False})

        def _broken_session():
            raise RuntimeError("database is locked")

        self.manager.SessionLocal = _broken_session
        resp = self.client.post("/api/config/autonomy-all-stop", json={"enabled": True})
        self.assertEqual(resp.status_code, 500)
        self.assertIs(wiring.is_autonomy_all_stopped(self.manager), False)
        self.assertEqual(self.ensure_calls, [])

    def test_all_stop_rejects_a_non_bool_body(self):
        resp = self.client.post("/api/config/autonomy-all-stop", json={})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
