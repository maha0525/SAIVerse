"""occupants の「話しかけやすさ」表示 (life.md §9.1) のテスト。

GET /api/info/details の occupants[].life_state / life_until は
``saiverse.day_plan.get_life_status_now`` を既存の 10 秒ポーリングに相乗りさせて
計算する (新しい高頻度ポーリングを作らない設計)。ルート関数
``api.routes.info.get_building_details`` は FastAPI の Depends を介さず直接
呼び出せる (プレーンな関数) ので、TestClient を組まずに直接呼んでロジックだけを
検証する。

``database.session.SessionLocal`` (Building.IMAGE_PATH 解決用のグローバル
セッション) は本テストの一時 DB に差し替える — 本番/開発 DB に触れないため。
"""
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, Building, City, User
from saiverse import clock
from saiverse import day_plan

PERSONA_ID = "air"
BUILDING_ID = "cafe"
PLAN_DATE = "2026-07-06"


class InfoLifeStateTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.addCleanup(clock.disable_virtual)

        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITYNAME="city_a", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(Building(CITYID=city.CITYID, BUILDINGID=BUILDING_ID, BUILDINGNAME="カフェ"))
            db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="エア"))
            db.commit()
        finally:
            db.close()

        persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            persona_name="エア",
            avatar_image=None,
            activity_state="Active",
        )
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas={PERSONA_ID: persona},
            occupancy_manager=SimpleNamespace(occupants={BUILDING_ID: [PERSONA_ID]}),
            building_map={BUILDING_ID: SimpleNamespace(name="カフェ", description="")},
            state=SimpleNamespace(user_id=1),
            item_registry={},
            items_by_building={},
        )

        # Building.IMAGE_PATH 解決 (get_building_details 内、グローバル DB 参照) を
        # 一時 DB に差し替える。読み取り専用 (SELECT) だが本番 DB を触らないため。
        patcher = patch("database.session.SessionLocal", self.Session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get_life_state(self):
        from api.routes.info import get_building_details

        resp = get_building_details(building_id=BUILDING_ID, manager=self.manager)
        occupant = resp["occupants"][0]
        return occupant["life_state"], occupant["life_until"]

    def test_undeclared_shows_nothing(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 30))
        life_state, life_until = self._get_life_state()
        self.assertIsNone(life_state)
        self.assertIsNone(life_until)

    def test_in_life_shows_in_life_with_end_time(self):
        day_plan.save_lives(self.manager, PERSONA_ID, PLAN_DATE, [
            {"start": "07:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
        ])
        clock.enable_virtual(datetime(2026, 7, 6, 9, 30))
        life_state, life_until = self._get_life_state()
        self.assertEqual(life_state, "in_life")
        self.assertEqual(life_until, "12:00")

    def test_valley_shows_valley_without_end_time(self):
        day_plan.save_lives(self.manager, PERSONA_ID, PLAN_DATE, [
            {"start": "07:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
            {"start": "14:00", "end": "20:00", "budget_pulses": 8, "mode": "free"},
        ])
        clock.enable_virtual(datetime(2026, 7, 6, 13, 0))  # 二つのライフの間
        life_state, life_until = self._get_life_state()
        self.assertEqual(life_state, "valley")
        self.assertIsNone(life_until)


if __name__ == "__main__":
    unittest.main()
