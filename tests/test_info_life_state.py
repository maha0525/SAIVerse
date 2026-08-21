"""occupants の暮らし系表示が v0.3 で消えていることの回帰。

`GET /api/info/details` の occupants[] は、かつて「話しかけやすさ」(life_state /
life_until、life.md §9.1) と「いま何をしているか」(activity_label) を返していた。
束 6c (2026-08-22) でどちらも撤去した:

- ``activity_label`` の供給源は running な自律 Track で、Track ランタイムの退役
  (track_retirement.md §8.6) で書き手ごと消えていた。
- ``life_state`` / ``life_until`` の唯一の読み手 (RightSidebar のチップと
  ライフビュー) は、v0.3 で運転 UI を隠す裁定 (autonomous_behavior_v3.md §11)
  で外れた。動いていない運転の状態を UI へ出さない。

作り直しは v0.4 の「暮らしの窓」(v3 §9-9)。**そのときはこのテストも一緒に更新
する** — 欄が黙って復活しないための歯止めなので、消す判断はここで一度目に入る。

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

#: 束 6c で occupants[] から外した欄。復活は v0.4 の「暮らしの窓」の工事。
RETIRED_OCCUPANT_FIELDS = ("life_state", "life_until", "activity_label")


class InfoOccupantFieldsTest(unittest.TestCase):
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
            city = City(USERID=1, CITY_SLUG="city_a", UI_PORT=3001, API_PORT=8001)
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
            autonomy_enabled=True,
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

    def _occupant(self):
        from api.routes.info import get_building_details

        resp = get_building_details(building_id=BUILDING_ID, manager=self.manager)
        return resp["occupants"][0]

    def test_the_occupant_keeps_identity_and_the_autonomy_flag(self):
        """残す欄: 名前・アバター・自律 ON/OFF。ここは v0.3 でも生きている。"""
        clock.enable_virtual(datetime(2026, 7, 6, 9, 30))
        occupant = self._occupant()
        self.assertEqual(occupant["id"], PERSONA_ID)
        self.assertEqual(occupant["name"], "エア")
        self.assertIs(occupant["autonomy_enabled"], True)

    def test_the_life_fields_are_gone_even_while_a_life_is_declared(self):
        """ライフを宣言していても暮らし系の欄は返さない (運転 UI は隠す)。"""
        day_plan.save_lives(self.manager, PERSONA_ID, PLAN_DATE, [
            {"start": "07:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
        ])
        clock.enable_virtual(datetime(2026, 7, 6, 9, 30))
        occupant = self._occupant()
        for field in RETIRED_OCCUPANT_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, occupant)


if __name__ == "__main__":
    unittest.main()
