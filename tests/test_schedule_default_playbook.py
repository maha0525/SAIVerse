"""アラーム作成・更新での meta_playbook の扱い (2026-09-01 裁定)。

どの Playbook で動くかはアラームの作成者に選ばせない。判断点の Playbook は
コードが決定論的に発火させ、生活リズムはライフ設定が所有するので、UI から
選ぶ意味のある選択肢が無い。そこでアラーム管理 UI の Playbook 選択欄を撤去し、
REST もツール (schedule_add) も「省略できて既定へ落ちる」契約に揃えた。

ここで固定するのは二つ:

- **作成**: meta_playbook 未指定 / 空文字は既定 Playbook へ正規化される
  (空のまま入ると、作成は通ったのに発火時に Playbook を引けないアラームになる)
- **更新**: meta_playbook を送らなければ既存の値がそのまま残る。ライフ設定が
  登録した起床 (judgment_day_open) のアラームを UI から編集しても、Playbook が
  会話用に化けてはならない — これが今回の一番の事故ポイント

DB は TestClient のワーカースレッドと共有するので :memory: ではなく
file sqlite + check_same_thread=False にする (test_schedule_api_sync.py と同じ流儀)。
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
from database.models import AI, Base, City, PersonaSchedule, User
from saiverse.schedule_manager import DEFAULT_META_PLAYBOOK

PERSONA_ID = "alice"


class ScheduleDefaultPlaybookTest(unittest.TestCase):
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
            city = City(USERID=1, CITY_SLUG="city_a", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="アリス"))
            db.commit()
        finally:
            db.close()

    def _playbook_of(self, schedule_id: int) -> str:
        db = self.Session()
        try:
            row = db.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id,
            ).first()
            self.assertIsNotNone(row)
            return row.META_PLAYBOOK
        finally:
            db.close()

    def _create(self, **overrides) -> int:
        payload = {"schedule_type": "periodic", "time_of_day": "09:00"}
        payload.update(overrides)
        resp = self.client.post(
            f"/api/people/{PERSONA_ID}/schedules", json=payload,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["schedule_id"]

    # ---- 作成 ----

    def test_create_without_meta_playbook_uses_the_default(self):
        """UI は meta_playbook を送らない。既定 Playbook が入ること。"""
        schedule_id = self._create()
        self.assertEqual(self._playbook_of(schedule_id), DEFAULT_META_PLAYBOOK)

    def test_create_with_empty_meta_playbook_uses_the_default(self):
        """空文字でも空のまま保存しない (鳴らないアラームを作らない)。"""
        schedule_id = self._create(meta_playbook="")
        self.assertEqual(self._playbook_of(schedule_id), DEFAULT_META_PLAYBOOK)

    def test_create_with_blank_meta_playbook_uses_the_default(self):
        """空白だけの値も既定へ ("   " は名前として引けない)。"""
        schedule_id = self._create(meta_playbook="   ")
        self.assertEqual(self._playbook_of(schedule_id), DEFAULT_META_PLAYBOOK)

    def test_create_strips_surrounding_whitespace(self):
        """前後の空白を落としてから保存する。"""
        schedule_id = self._create(meta_playbook="  judgment_day_open  ")
        self.assertEqual(self._playbook_of(schedule_id), "judgment_day_open")

    def test_create_with_explicit_meta_playbook_is_honored(self):
        """明示指定は尊重する (ライフ設定など UI 以外の作成経路のため)。"""
        schedule_id = self._create(meta_playbook="judgment_day_open")
        self.assertEqual(self._playbook_of(schedule_id), "judgment_day_open")

    # ---- 更新 ----

    def test_update_without_meta_playbook_preserves_the_existing_one(self):
        """起床のアラームを UI から編集しても Playbook が会話用に化けない。"""
        schedule_id = self._create(meta_playbook="judgment_day_open")
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"schedule_type": "periodic", "time_of_day": "07:30",
                  "description": "起きる"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._playbook_of(schedule_id), "judgment_day_open")

    def test_update_with_empty_meta_playbook_preserves_the_existing_one(self):
        """空文字での上書きも拒む (META_PLAYBOOK を空にする口をなくす)。"""
        schedule_id = self._create(meta_playbook="judgment_day_close")
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"meta_playbook": "", "time_of_day": "23:00"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._playbook_of(schedule_id), "judgment_day_close")

    def test_update_with_blank_meta_playbook_preserves_the_existing_one(self):
        """空白だけの上書きも拒む。"""
        schedule_id = self._create(meta_playbook="judgment_day_close")
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"meta_playbook": "   "},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._playbook_of(schedule_id), "judgment_day_close")

    def test_update_strips_surrounding_whitespace(self):
        schedule_id = self._create()
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"meta_playbook": "  judgment_day_open  "},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._playbook_of(schedule_id), "judgment_day_open")

    def test_update_with_explicit_meta_playbook_still_overwrites(self):
        """明示指定での差し替えは従来どおり効く。"""
        schedule_id = self._create()
        resp = self.client.put(
            f"/api/people/{PERSONA_ID}/schedules/{schedule_id}",
            json={"meta_playbook": "judgment_day_open"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._playbook_of(schedule_id), "judgment_day_open")


class ScheduleAddToolDefaultPlaybookTest(unittest.TestCase):
    """ペルソナが唱える schedule_add スペルも REST と同じ正規化をすること。

    アラームを作る口が二つある以上、片方だけ直すともう片方から空の
    META_PLAYBOOK が入る (同じ既定値を両方が ScheduleManager から引く)。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        db_file = Path(self._tmp.name) / "saiverse_tool_test.db"
        self.engine = create_engine(f"sqlite:///{db_file}")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITY_SLUG="city_a", UI_PORT=3001, API_PORT=8001,
                        TIMEZONE="Asia/Tokyo")
            db.add(city)
            db.flush()
            db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="アリス"))
            db.commit()
        finally:
            db.close()

        self.manager = SimpleNamespace(SessionLocal=self.Session)

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _call(self, **kwargs) -> str:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tool_loader import load_builtin_tool
        from tools.context import persona_context

        mod = load_builtin_tool("schedule_add")
        with persona_context(PERSONA_ID, Path(self._tmp.name), manager=self.manager):
            return mod.schedule_add(
                schedule_type="periodic", time_of_day="09:00", **kwargs
            )

    def _only_playbook(self) -> str:
        db = self.Session()
        try:
            rows = db.query(PersonaSchedule).all()
            self.assertEqual(len(rows), 1)
            return rows[0].META_PLAYBOOK
        finally:
            db.close()

    def test_omitted_meta_playbook_uses_the_default(self):
        result = self._call()
        self.assertIn("追加しました", result)
        self.assertEqual(self._only_playbook(), DEFAULT_META_PLAYBOOK)

    def test_blank_meta_playbook_uses_the_default(self):
        self._call(meta_playbook="   ")
        self.assertEqual(self._only_playbook(), DEFAULT_META_PLAYBOOK)

    def test_surrounding_whitespace_is_stripped(self):
        self._call(meta_playbook="  judgment_day_open  ")
        self.assertEqual(self._only_playbook(), "judgment_day_open")


if __name__ == "__main__":
    unittest.main()
