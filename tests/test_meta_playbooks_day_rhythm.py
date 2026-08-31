"""list_meta_playbooks が判断点の Playbook を出さないことのテスト。

起床 (judgment_day_open)・就寝 (judgment_day_close) をはじめとする判断点は
``user_selectable=false``。一日のリズムはライフ設定が所有し、判断点はコードが
決定論的に発火させるので、ユーザーが一覧から選ぶ対象ではない。

2026-09-01 の裁定でアラーム管理ダイアログの Playbook 選択欄そのものを撤去し、
そこ専用だった ``include_day_rhythm`` 引数も併せて撤去した。この一覧の口から
判断点の名前が漏れないことを、ここで固定する。
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
            db.add(Playbook(
                name="judgment_post_session", scope="public",
                schema_json="{}", nodes_json="[]", user_selectable=False,
            ))
            db.commit()
        finally:
            db.close()
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)

    def test_lists_only_user_selectable_playbooks(self):
        names = list_meta_playbooks(manager=self.manager)
        self.assertEqual(names, ["track_user_conversation"])

    def test_judgment_playbooks_are_never_listed(self):
        """判断点はどの呼び方でも一覧に出ない (出す口をなくした)。"""
        names = list_meta_playbooks(manager=self.manager)
        for hidden in (
            "judgment_day_open", "judgment_day_close", "judgment_post_session",
        ):
            self.assertNotIn(hidden, names)


if __name__ == "__main__":
    unittest.main()
