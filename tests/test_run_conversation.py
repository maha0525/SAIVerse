"""scripts/run_conversation.py のテスト。

ランナーのオーケストレーション (増分抽出 / transcript 組み立て / 台本検証 /
本番ガード) を、LLM を呼ばないスタブドライバで検証する。
実チャット経路そのもの (RealConversationUserEventDriver) は一日シム側で
実証済みのため、ここでは対象にしない。
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_conversation import (
    ConversationError,
    _guard_not_production,
    format_transcript,
    main,
    normalize_script,
    run_conversation,
)


def _make_manager():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    persona = SimpleNamespace(persona_id="alice", current_building_id="lobby")
    return SimpleNamespace(SessionLocal=session_factory, personas={"alice": persona})


class StubDriver:
    """発話を building_messages に記録し、決まった応答を書き込むスタブ。"""

    def __init__(self, reply_texts=None):
        self.reply_texts = list(reply_texts or [])
        self.ended = False
        self._seq = 0

    def _add(self, manager, role, content, persona_id=None):
        from database.models import BuildingMessage
        self._seq += 1
        db = manager.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id="lobby", seq=self._seq, role=role,
                persona_id=persona_id, content=content,
                timestamp=datetime(2026, 7, 6, 12, 0, self._seq).isoformat(),
            ))
            db.commit()
        finally:
            db.close()

    def begin_conversation(self, manager, persona_id, text):
        self._add(manager, "user", text)
        if self.reply_texts:
            self._add(manager, "assistant", self.reply_texts.pop(0), persona_id=persona_id)

    def end_conversation(self, manager, persona_id):
        self.ended = True
        return True


class RunConversationTest(unittest.TestCase):
    def test_transcript_collects_replies_per_turn(self):
        manager = _make_manager()
        driver = StubDriver(reply_texts=["やあ、まはー", "昨日は本を読んでたよ"])
        script = normalize_script({
            "persona_id": "alice",
            "messages": ["おはよう", "昨日何してた？"],
        })
        transcript = run_conversation(manager, script, driver=driver)
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[0]["user"], "おはよう")
        self.assertEqual(
            [r["content"] for r in transcript[0]["replies"] if r["role"] == "assistant"],
            ["やあ、まはー"])
        self.assertEqual(
            [r["content"] for r in transcript[1]["replies"] if r["role"] == "assistant"],
            ["昨日は本を読んでたよ"])
        self.assertTrue(driver.ended)  # leave 既定 true

        text = format_transcript(script, transcript)
        self.assertIn("## ターン 1", text)
        self.assertIn("やあ、まはー", text)
        self.assertNotIn("(応答なし)", text)

    def test_no_reply_is_honest(self):
        manager = _make_manager()
        driver = StubDriver(reply_texts=[])  # 応答しない
        script = normalize_script({
            "persona_id": "alice", "messages": ["おーい"], "leave": False,
        })
        transcript = run_conversation(manager, script, driver=driver)
        self.assertEqual(
            [r for r in transcript[0]["replies"] if r["role"] == "assistant"], [])
        self.assertFalse(driver.ended)
        self.assertIn("(応答なし)", format_transcript(script, transcript))

    def test_unknown_persona_rejected(self):
        manager = _make_manager()
        script = normalize_script({"persona_id": "ghost", "messages": ["hi"]})
        with self.assertRaises(ConversationError):
            run_conversation(manager, script, driver=StubDriver())

    def test_script_validation(self):
        with self.assertRaises(ConversationError):
            normalize_script({"messages": ["hi"]})  # persona_id なし
        with self.assertRaises(ConversationError):
            normalize_script({"persona_id": "a", "messages": []})  # 空
        with self.assertRaises(ConversationError):
            normalize_script({"persona_id": "a", "messages": ["ok", ""]})  # 空文字列混入


class ProductionGuardTest(unittest.TestCase):
    """本番の場所は SAIVERSE_HOME ではなく ~/.saiverse で判定する (intent doc §2-1)。

    Path.home() を一時ディレクトリへ差し替えるので、本物の ~/.saiverse には触れない。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run_conversation_guard_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        user_home = self.tmp / "user_home"
        self.prod_home = user_home / ".saiverse"
        self.prod_db = self.prod_home / "user_data" / "database" / "saiverse.db"
        self.sandbox_home = self.tmp / "sandbox" / ".saiverse"
        self.sandbox_user_data = self.tmp / "sandbox" / "user_data"
        self.sandbox_db = self.sandbox_user_data / "database" / "saiverse.db"
        home_patcher = patch("pathlib.Path.home", return_value=user_home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)

    def _sandbox_env(self):
        return patch.dict(os.environ, {
            "SAIVERSE_HOME": str(self.sandbox_home),
            "SAIVERSE_USER_DATA_DIR": str(self.sandbox_user_data),
        })

    def test_db_in_production_refused_while_home_points_to_sandbox(self):
        # 2026-09-17 の穴: SAIVERSE_HOME をテスト環境へ向けた状態で本番の DB を渡すと通っていた。
        # 拒否の理由が DB であることまで確かめる (env を本番とみなす実装だと別の理由で落ちる)
        with self._sandbox_env(), self.assertRaises(ConversationError) as ctx:
            _guard_not_production(self.prod_db, self.sandbox_home, self.sandbox_user_data)
        self.assertIn("--db-file=", str(ctx.exception))
        self.assertNotIn("SAIVERSE_HOME=", str(ctx.exception))

    def test_env_dirs_in_production_refused(self):
        # DB がテスト環境でも、ペルソナの memory.db や建物ログの書き先が本番なら拒否する
        with self.assertRaises(ConversationError):
            _guard_not_production(self.sandbox_db, self.prod_home, self.sandbox_user_data)
        with self.assertRaises(ConversationError):
            _guard_not_production(
                self.sandbox_db, self.sandbox_home, self.prod_home / "user_data")

    def test_out_in_production_refused(self):
        with self.assertRaises(ConversationError):
            _guard_not_production(
                self.sandbox_db, self.sandbox_home, self.sandbox_user_data,
                self.prod_home / "personas" / "alice" / "transcript.md",
            )

    def test_sandbox_passes(self):
        with self._sandbox_env():
            _guard_not_production(
                self.sandbox_db, self.sandbox_home, self.sandbox_user_data,
                self.tmp / "out" / "transcript.md",
            )

    def _run_main(self, env, db_path):
        with patch.dict(os.environ, env):
            with self.assertLogs("scripts.run_conversation", level="ERROR") as logs:
                code = main(["--persona", "alice", "--message", "hi",
                             "--db-file", str(db_path)])
        return code, "\n".join(logs.output)

    def test_main_refuses_production_db_without_touching_it(self):
        self.prod_db.parent.mkdir(parents=True)
        self.prod_db.write_bytes(b"PRODUCTION_DB")
        code, log = self._run_main(
            {"SAIVERSE_HOME": str(self.sandbox_home),
             "SAIVERSE_USER_DATA_DIR": str(self.sandbox_user_data)},
            self.prod_db,
        )
        self.assertEqual(code, 1)
        self.assertIn("--db-file=", log)
        self.assertNotIn("SAIVERSE_HOME=", log)
        self.assertEqual(self.prod_db.read_bytes(), b"PRODUCTION_DB")

    def test_main_treats_empty_saiverse_home_as_production(self):
        # 空文字の SAIVERSE_HOME は setdefault で埋まらず、data_paths は ~/.saiverse を使う
        code, log = self._run_main(
            {"SAIVERSE_HOME": "", "SAIVERSE_USER_DATA_DIR": str(self.sandbox_user_data)},
            self.sandbox_db,
        )
        self.assertEqual(code, 1)
        self.assertIn("SAIVERSE_HOME=", log)


if __name__ == "__main__":
    unittest.main()
