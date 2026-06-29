"""META 判断による生きる目的設定 (autonomous_desire.md §10) のテスト。

- 未設定なら最優先状況 life_purpose_unset に分類される (毎回・META)。
- finalize が life_purpose_set スペルに整形する。
- ライフビューに「生きる目的を考えた」が出る。
"""
import unittest
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.activity_view import _format_meta_judgment_result
from saiverse.life_purpose import set_life_purpose
from saiverse.meta_layer import MetaLayer


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="t"))
        db.flush()
        city = City(USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="p1", HOME_CITYID=city.CITYID, AINAME="P1"))
        db.commit()
    finally:
        db.close()
    return engine, Session


class ClassifyLifePurposeTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.Session = _make_db()
        self.manager = MagicMock()
        self.manager.SessionLocal = self.Session
        self.manager.track_manager.list_for_persona.return_value = []
        self.layer = MetaLayer(self.manager)

    def tearDown(self):
        self.engine.dispose()

    def test_unset_classifies_as_life_purpose(self):
        sit = self.layer._classify_situation("p1", {})
        self.assertEqual(sit["kind"], "life_purpose_unset")

    def test_set_falls_through_to_normal(self):
        set_life_purpose(self.Session, "p1", "誰かの隣にいる", ["創作"], ["支援"])
        sit = self.layer._classify_situation("p1", {})
        # Track が無いので idle_no_pending (= 通常フロー) に落ちる。
        self.assertEqual(sit["kind"], "idle_no_pending")

    def test_preempt_collision_still_wins(self):
        # 未設定でも preempt_collision (no-op 衝突) が最優先。
        sit = self.layer._classify_situation("p1", {"target_already_running": True})
        self.assertEqual(sit["kind"], "preempt_collision")

    def test_response_schema_and_situation_text(self):
        schema = self.layer._build_response_schema("life_purpose_unset", {})
        self.assertEqual(schema["required"], ["monologue", "purpose"])
        self.assertIn("interests", schema["properties"])
        text = self.layer._build_situation_text("p1", "", {})
        self.assertIn("生きる目的", text)

    def test_playbook_mapped(self):
        self.assertEqual(
            self.layer._SITUATION_PLAYBOOK_MAP["life_purpose_unset"],
            "meta_judgment_life_purpose",
        )


class FinalizeAndLifeViewTest(unittest.TestCase):
    def test_format_spells_builds_life_purpose_set(self):
        from builtin_data.tools.meta_judgment_finalize import _format_spells

        out = {
            "monologue": "私はこう在りたい",
            "purpose": "誰かの隣にいる",
            "interests": ["創作", "対話"],
            "vocations": ["支援"],
        }
        spells = _format_spells("life_purpose_unset", out, "")
        self.assertEqual(len(spells), 1)
        name, args = spells[0]
        self.assertEqual(name, "life_purpose_set")
        self.assertEqual(args["purpose"], "誰かの隣にいる")
        self.assertEqual(args["interests"], ["創作", "対話"])

    def test_format_spells_empty_without_purpose(self):
        from builtin_data.tools.meta_judgment_finalize import _format_spells

        self.assertEqual(_format_spells("life_purpose_unset", {"monologue": "..."}, ""), [])

    def test_spell_text_json_encodes_arrays(self):
        from builtin_data.tools.meta_judgment_finalize import _spell_to_text

        text = _spell_to_text("life_purpose_set", {"purpose": "x", "interests": ["a", "b"]})
        self.assertIn("life_purpose_set", text)
        self.assertIn('["a", "b"]', text)

    def test_life_view_shows_thought(self):
        phrase = _format_meta_judgment_result([{"name": "life_purpose_set", "args": {}}])
        self.assertEqual(phrase, "生きる目的を考えた")


if __name__ == "__main__":
    unittest.main()
