"""LIFE_PURPOSE ヘルパ + LifePurposeSection + life_purpose_set スペルのテスト。

autonomous_desire.md §3 (① 駆動文) / §4 (② 生きる目的)。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.life_purpose import (
    DESIRE_DRIVE_TEXT,
    get_life_purpose,
    parse_life_purpose,
    render_life_purpose_text,
    serialize_life_purpose,
    set_life_purpose,
)
from sea.head_pipeline.types import LineHeadInput
from sea.head_pipeline.sections.life_purpose import (
    LifePurposeSection,
    LifePurposeSnapshot,
)


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
        db.add(AI(AIID="air", HOME_CITYID=city.CITYID, AINAME="Air"))
        db.commit()
    finally:
        db.close()
    return engine, Session


class LifePurposeHelperTest(unittest.TestCase):
    def test_parse_none_and_empty(self):
        self.assertIsNone(parse_life_purpose(None))
        self.assertIsNone(parse_life_purpose(""))
        self.assertIsNone(parse_life_purpose("not json"))
        self.assertIsNone(parse_life_purpose('{"purpose":"","interests":[],"vocations":[]}'))

    def test_serialize_roundtrip_strips_blanks(self):
        raw = serialize_life_purpose("生きる", ["絵", "  ", "音楽"], [])
        data = parse_life_purpose(raw)
        self.assertEqual(data["purpose"], "生きる")
        self.assertEqual(data["interests"], ["絵", "音楽"])
        self.assertEqual(data["vocations"], [])

    def test_render_text(self):
        data = {"purpose": "誰かの隣にいる", "interests": ["創作"], "vocations": ["支援"]}
        text = render_life_purpose_text(data)
        self.assertIn("生きる目的", text)
        self.assertIn("誰かの隣にいる", text)
        self.assertIn("創作", text)
        self.assertEqual(render_life_purpose_text(None), "")

    def test_get_set_via_db(self):
        engine, Session = _make_db()
        try:
            self.assertIsNone(get_life_purpose(Session, "air"))
            set_life_purpose(Session, "air", "目的", ["趣味A"], ["仕事A"])
            data = get_life_purpose(Session, "air")
            self.assertEqual(data["purpose"], "目的")
            self.assertEqual(data["interests"], ["趣味A"])
        finally:
            engine.dispose()

    def test_set_unknown_persona_raises(self):
        engine, Session = _make_db()
        try:
            with self.assertRaises(ValueError):
                set_life_purpose(Session, "ghost", "x")
        finally:
            engine.dispose()


class LifePurposeSectionTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.Session = _make_db()
        self.manager = SimpleNamespace(SessionLocal=self.Session)

    def tearDown(self):
        self.engine.dispose()

    def _ctx(self):
        return LineHeadInput(
            persona_id="air", line_id="main", line_role="main_line",
            model_key="m", current_building_id="b", persona=None, manager=self.manager,
        )

    def test_unset_purpose_renders_neutral_text(self):
        """未設定でも省略しない — 中立文を出す (life_concept_map.md §15 ①)。"""
        snap = LifePurposeSection().capture(self._ctx())
        self.assertEqual(snap.drive_text, DESIRE_DRIVE_TEXT)
        self.assertEqual(snap.purpose_text, "")
        rendered = LifePurposeSection().render(snap)
        self.assertIsNotNone(rendered)
        self.assertIn("内発的な動機", rendered.text)
        self.assertIn("まだ言葉になっていません", rendered.text)
        # 行動喚起 (命令文) は head に置かない — META 専用状況が担うため。
        # ただしスペル名の教示 (能力と所有権) は命令ではないので出してよい
        # (まはー 2026-07-07: 操作する本人に固有名詞を隠すのは飼い馴らし)。
        self.assertIn("/spell life_purpose_set", rendered.text)
        self.assertNotIn("定めてください", rendered.text)
        self.assertNotIn("設定してください", rendered.text)

    def test_capture_includes_purpose_when_set(self):
        set_life_purpose(self.Session, "air", "誰かの隣にいる", ["創作"], ["支援"])
        snap = LifePurposeSection().capture(self._ctx())
        self.assertIn("誰かの隣にいる", snap.purpose_text)
        rendered = LifePurposeSection().render(snap)
        self.assertIn("生きる目的", rendered.text)
        self.assertIn("誰かの隣にいる", rendered.text)
        # 設定済みなら中立文は出ない
        self.assertNotIn("まだ言葉になっていません", rendered.text)

    def test_render_includes_bark_and_mark_notation(self):
        """樹皮の要旨と ==語句== 記法の教示が恒常で出る (§15 ① / §9.1 層1)。"""
        snap = LifePurposeSection().capture(self._ctx())
        rendered = LifePurposeSection().render(snap)
        # 樹皮 (§4.1): 6 件の要旨。システムプロンプト遵守が最上位
        self.assertIn("保護対象（世界との約束）", rendered.text)
        self.assertIn("システムプロンプトの遵守", rendered.text)
        self.assertIn("最上位", rendered.text)
        # 記法教示
        self.assertIn("==語句==", rendered.text)

    def test_track_menu_moved_to_purpose_backlog(self):
        """Track の一覧はこの Section から出ない (2026-07-30 PurposeBacklog へ統合)。

        同じ Track を head 内で二度並べないための統合。こちらのメニューは
        差分通知を持たなかったので、残すと「通知される一覧」と「されない一覧」が
        同じ head で食い違う。
        """
        from saiverse.track_manager import TrackManager

        section = LifePurposeSection()
        tm = TrackManager(session_factory=self.Session)
        tm.create(persona_id="air", track_type="autonomous", title="言葉の標本集")

        snap = section.capture(self._ctx())
        self.assertFalse(hasattr(snap, "first_tier_titles"))
        text = section.render(snap).text
        self.assertNotIn("言葉の標本集", text)
        self.assertNotIn("いま開いている Track", text)

    def test_render_is_line_role_independent(self):
        """render は snapshot のみに依存 — 用途/ラインで出し分けない (head 固定)。"""
        section = LifePurposeSection()
        snap = section.capture(self._ctx())
        self.assertEqual(section.render(snap).text, section.render(snap).text)

    def test_diff_notifies_on_purpose_set(self):
        old = LifePurposeSnapshot(drive_text=DESIRE_DRIVE_TEXT, purpose_text="")
        new = LifePurposeSnapshot(drive_text=DESIRE_DRIVE_TEXT, purpose_text="目的: X")
        labels = LifePurposeSection().diff_to_notifications(old, new)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].kind, "life_purpose_set")

    def test_snapshot_roundtrip(self):
        snap = LifePurposeSnapshot(drive_text="d", purpose_text="p")
        section = LifePurposeSection()
        restored = section.deserialize_snapshot(section.serialize_snapshot(snap))
        self.assertEqual(restored, snap)

    def test_deserialize_drops_retired_fields(self):
        """統合前に保存された行 (first_tier_titles つき) も静かに復元できる。

        TypeError にすると store の load が ERROR + traceback を吐くだけで、
        結局 recapture に落ちる — 想定内の移行を error 面に出さない。
        """
        section = LifePurposeSection()
        restored = section.deserialize_snapshot(
            '{"drive_text": "d", "purpose_text": "p", "first_tier_titles": ["a"]}'
        )
        self.assertEqual(restored, LifePurposeSnapshot(drive_text="d", purpose_text="p"))


class LifePurposeSetSpellTest(unittest.TestCase):
    def setUp(self):
        from tool_loader import load_builtin_tool

        self.engine, self.Session = _make_db()
        self.mod = load_builtin_tool("life_purpose_set")
        self.mod._session_factory = self.Session
        self.tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self.tmp.name) / "air"
        self.persona_dir.mkdir(parents=True)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_spell_saves_purpose(self):
        from tools.context import persona_context

        with persona_context("air", self.persona_dir):
            out = self.mod.life_purpose_set(
                purpose="誰かの隣にいる", interests=["創作", "対話"], vocations=["支援"],
            )
        self.assertIn("保存しました", out)
        data = get_life_purpose(self.Session, "air")
        self.assertEqual(data["purpose"], "誰かの隣にいる")
        self.assertEqual(data["interests"], ["創作", "対話"])


if __name__ == "__main__":
    unittest.main()
