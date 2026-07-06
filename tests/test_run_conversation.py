"""scripts/run_conversation.py のテスト。

ランナーのオーケストレーション (増分抽出 / transcript 組み立て / 台本検証 /
本番ガード) を、LLM を呼ばないスタブドライバで検証する。
実チャット経路そのもの (RealConversationUserEventDriver) は一日シム側で
実証済みのため、ここでは対象にしない。
"""
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from scripts.run_conversation import (
    ConversationError,
    _guard_not_production,
    format_transcript,
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

    def test_production_guard(self):
        production_db = Path.home() / ".saiverse" / "user_data" / "database" / "saiverse.db"
        with self.assertRaises(ConversationError):
            _guard_not_production(production_db)
        _guard_not_production(Path("test_data/user_data/database/saiverse.db"))  # OK


if __name__ == "__main__":
    unittest.main()
