"""list_meta_playbooks の include_day_rhythm 挙動テスト。

起床 (judgment_day_open)・就寝 (judgment_day_close) は user_selectable=false
(召喚ダイアログに出すのは誤り) だが、PersonaSchedule への登録は自律行動 v2 の
正規の儀式。スケジュールダイアログは include_day_rhythm=true で取得し、
これらを選択肢に含める (2026-07-12、実機テスト準備で発覚した UI の穴の修正)。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routes.people.summon import list_meta_playbooks
from database.models import Base, Playbook


class MetaPlaybooksDayRhythmTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        db = self.SessionLocal()
        try:
            db.add(Playbook(
                name="track_user_conversation", scope="public",
                schema_json="{}", nodes_json="[]", user_selectable=True,
            ))
            db.add(Playbook(
                name="judgment_day_open", scope="public",
                schema_json="{}", nodes_json="[]", user_selectable=False,
            ))
            db.add(Playbook(
                name="judgment_day_close", scope="public",
                schema_json="{}", nodes_json="[]", user_selectable=False,
            ))
            # 文脈必須の判断点 — include_day_rhythm でも出てはいけない
            db.add(Playbook(
                name="judgment_post_session", scope="public",
                schema_json="{}", nodes_json="[]", user_selectable=False,
            ))
            db.commit()
        finally:
            db.close()
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)

    def test_default_excludes_day_rhythm(self):
        """既定 (召喚ダイアログ) では user_selectable=true のみ。"""
        names = list_meta_playbooks(manager=self.manager)
        self.assertIn("track_user_conversation", names)
        self.assertNotIn("judgment_day_open", names)
        self.assertNotIn("judgment_day_close", names)

    def test_include_day_rhythm_adds_wake_and_close(self):
        """スケジュールダイアログ用は起床・就寝が選択肢に含まれる。"""
        names = list_meta_playbooks(include_day_rhythm=True, manager=self.manager)
        self.assertIn("track_user_conversation", names)
        self.assertIn("judgment_day_open", names)
        self.assertIn("judgment_day_close", names)
        # 文脈必須の判断点はスケジュールから撃てないので含めない
        self.assertNotIn("judgment_post_session", names)

    def test_include_day_rhythm_only_lists_imported(self):
        """DB に import されていない判断点は含めない (未 import の環境で幽霊選択肢を出さない)。"""
        db = self.SessionLocal()
        try:
            db.query(Playbook).filter(Playbook.name == "judgment_day_close").delete()
            db.commit()
        finally:
            db.close()
        names = list_meta_playbooks(include_day_rhythm=True, manager=self.manager)
        self.assertIn("judgment_day_open", names)
        self.assertNotIn("judgment_day_close", names)


if __name__ == "__main__":
    unittest.main()
