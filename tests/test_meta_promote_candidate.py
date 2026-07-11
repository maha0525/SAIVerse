"""META が候補を Track 化（昇格）する経路のテスト (autonomous_desire.md §6/§11.4)。

候補→Track のループ閉じ: idle_with_pending のメタ判断スキーマに promote バリアントが
入り、finalize が track_create(from_candidate=) に整形する。
"""
import unittest
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, ActionTrack, Base, City, User
from saiverse.persona_task_manager import STAGE_CANDIDATE, PersonaTaskManager
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


class CandidatesInSchemaTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.Session = _make_db()
        self.manager = MagicMock()
        self.manager.SessionLocal = self.Session
        # life purpose set so we don't classify as life_purpose_unset
        from saiverse.life_purpose import set_life_purpose
        set_life_purpose(self.Session, "p1", "x")
        self.ptm = PersonaTaskManager(self.Session)
        # one pending track so we're in idle_with_pending
        db = self.Session()
        try:
            db.add(ActionTrack(
                track_id="trk1", persona_id="p1", short_id=1,
                track_type="autonomous", status="unstarted", title="探索",
            ))
            db.commit()
        finally:
            db.close()
        self.manager.track_manager.list_for_persona.return_value = self._tracks()
        self.layer = MetaLayer(self.manager)

    def tearDown(self):
        self.engine.dispose()

    def _tracks(self):
        db = self.Session()
        try:
            rows = db.query(ActionTrack).filter_by(persona_id="p1").all()
            for r in rows:
                db.expunge(r)
            return rows
        finally:
            db.close()

    def _add_candidate(self, title):
        return self.ptm.create_task(
            persona_id="p1", title=title,
            stage=STAGE_CANDIDATE, auto_activate=False,
        )

    def test_promote_variant_present_when_candidates_exist(self):
        self._add_candidate("新しい言語を学ぶ")
        sit = self.layer._classify_situation("p1", {})
        self.assertEqual(sit["kind"], "idle_with_pending")
        self.assertEqual(len(sit["candidates"]), 1)
        schema = self.layer._build_response_schema("idle_with_pending", sit)
        variants = schema["properties"]["decision"]["anyOf"]
        types = {v["properties"]["type"]["const"] for v in variants}
        self.assertIn("promote", types)
        promote = next(v for v in variants if v["properties"]["type"]["const"] == "promote")
        self.assertIn("task:1", promote["properties"]["candidate_ref"]["enum"])

    def test_no_promote_variant_without_candidates(self):
        sit = self.layer._classify_situation("p1", {})
        schema = self.layer._build_response_schema("idle_with_pending", sit)
        variants = schema["properties"]["decision"]["anyOf"]
        types = {v["properties"]["type"]["const"] for v in variants}
        self.assertNotIn("promote", types)

    def test_situation_text_lists_candidates(self):
        self._add_candidate("風景を描く")
        text = self.layer._build_situation_text("p1", "", {})
        self.assertIn("やりたいこと候補", text)
        self.assertIn("風景を描く", text)
        self.assertIn("task:1", text)


class FinalizePromoteTest(unittest.TestCase):
    def test_promote_decision_builds_track_create_from_candidate(self):
        from builtin_data.tools.meta_judgment_finalize import _format_spells

        out = {
            "monologue": "あの候補に取り組もう",
            "decision": {
                "type": "promote",
                "candidate_ref": "task:3",
                "title": "言語学習",
                "intent": "新しい言語を習得する",
            },
        }
        spells = _format_spells("idle_with_pending", out, "")
        self.assertEqual(len(spells), 1)
        name, args = spells[0]
        self.assertEqual(name, "track_create")
        self.assertEqual(args["from_candidate"], "task:3")
        self.assertEqual(args["title"], "言語学習")
        # 昇格した Track は即 running にする (次の META まで待たせない)
        self.assertTrue(args.get("activate"), "promoted track must auto-activate")

    def test_promote_without_ref_is_noop(self):
        from builtin_data.tools.meta_judgment_finalize import _format_spells

        out = {"decision": {"type": "promote", "title": "x"}}
        self.assertEqual(_format_spells("idle_with_pending", out, ""), [])

    def test_life_view_phrase_for_promotion(self):
        from saiverse.activity_view import _format_meta_judgment_result

        phrase = _format_meta_judgment_result([{
            "name": "track_create",
            "args": {"title": "言語学習", "from_candidate": "task:3"},
        }])
        self.assertIn("やりたかった", phrase)
        self.assertIn("言語学習", phrase)


class PromptSnapshotTest(unittest.TestCase):
    """メタ判断プロンプトの可視化 (Pulse タイムライン)。"""

    def test_build_prompt_snapshot_combines_dynamic_parts(self):
        from builtin_data.tools.meta_judgment_finalize import _build_prompt_snapshot

        snap = _build_prompt_snapshot(
            situation_text="[状況]\nTrack X が running 中です。",
            trigger_context='{"trigger": "periodic_tick"}',
            recent_judgments="[最近のメタ判断ログ]\n...",
        )
        self.assertIn("[状況]", snap)
        self.assertIn("periodic_tick", snap)
        self.assertIn("最近のメタ判断ログ", snap)

    def test_build_prompt_snapshot_empty_returns_none(self):
        from builtin_data.tools.meta_judgment_finalize import _build_prompt_snapshot

        self.assertIsNone(_build_prompt_snapshot("", "{}", ""))


if __name__ == "__main__":
    unittest.main()
