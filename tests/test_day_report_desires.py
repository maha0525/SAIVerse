"""day_report の「やりたいことの動き」節 — 旅立ったもの (Track へ昇格) のテスト。

P3c-0 レビュー裁定「(b) 新聞への掲載 — 無限に増えるのはダメ、載せるなら
昇格・成就したその日の一回だけ」の回帰防止: 昇格 (promote_to_track) した
当日は day_report に「旅立ったもの」として載り、翌日以降に同じ行へ無関係な
活動があって updated_at が動いても再掲されないことを検証する
(判定は persona_task_history の promote_to_track イベント日で行う)。
"""
from __future__ import annotations

import gc
import unittest
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from saiverse import clock, day_report, purpose_tree
from saiverse.persona_task_manager import PersonaTaskManager
from saiverse.track_manager import TrackManager

PERSONA_ID = "air"
DAY1 = "2026-07-04"
DAY2 = "2026-07-05"


class DepartedDesireSectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        db = Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="t"))
            db.flush()
            city = City(USERID=1, CITY_SLUG="c", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Air"))
            db.commit()
        finally:
            db.close()
        self.manager = SimpleNamespace(SessionLocal=Session, personas={}, buildings=[])
        self.ptm = PersonaTaskManager(Session)
        self.tm = TrackManager(session_factory=Session)
        clock.enable_virtual(datetime(2026, 7, 4, 9, 0, 0))

    def tearDown(self):
        clock.disable_virtual()
        self.engine.dispose()
        gc.collect()

    def test_promoted_desire_appears_as_departed_on_promotion_day_only(self):
        node = purpose_tree.create_candidate(
            self.manager, PERSONA_ID, "新しい言語を学ぶ", "message:abc",
        )
        self.tm.create(PERSONA_ID, "autonomous", title="言語学習")
        adopted = purpose_tree.adopt(
            self.manager, PERSONA_ID, node["ref"], parent_ref="track:1",
        )
        self.assertEqual(adopted["stage"], "adopted")

        report_day1 = day_report.generate_day_report(self.manager, PERSONA_ID, DAY1)
        self.assertIn("旅立ったもの:", report_day1)
        self.assertIn("目的の木へ旅立ちました", report_day1)
        self.assertIn("新しい言語を学ぶ", report_day1)

        # 翌日、同じ行に無関係な活動があって updated_at が動いても、
        # 昇格イベント自体は前日日付なので「旅立ったもの」に再掲されない。
        clock.advance_to(datetime(2026, 7, 5, 10, 0, 0))
        self.ptm.set_purpose_fields(
            adopted["id"], persona_id=PERSONA_ID, actor="test", nature="venture",
        )

        report_day2 = day_report.generate_day_report(self.manager, PERSONA_ID, DAY2)
        section_start = report_day2.index("旅立ったもの:")
        section_text = report_day2[section_start:]
        self.assertNotIn("新しい言語を学ぶ", section_text)

    def test_unpromoted_candidate_never_appears_as_departed(self):
        purpose_tree.create_candidate(
            self.manager, PERSONA_ID, "まだ採用していない候補", "message:def",
        )
        report = day_report.generate_day_report(self.manager, PERSONA_ID, DAY1)
        self.assertIn("旅立ったもの:", report)
        section_start = report.index("旅立ったもの:")
        section_text = report[section_start:]
        self.assertNotIn("まだ採用していない候補", section_text)


if __name__ == "__main__":
    unittest.main()
