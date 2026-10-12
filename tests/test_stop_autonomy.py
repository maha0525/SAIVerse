"""manager.stop_autonomy (自律行動の実効停止) のテスト。

旧: メタ判断連続失敗リカバリ (案C) と停止ボタンの共用経路のテスト
(test_meta_judgment_recovery.py) だったが、v1 メタ判断の退役
(track_retirement.md §7.4) でリカバリ側が消えたため、停止ボタン経路の
検証だけをここへ移した。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User


class TestStopAutonomy(unittest.TestCase):
    """manager.stop_autonomy: 実効停止の 2 ステップ (停止ボタンの経路)。

    撤去された旧ステップ:

    - 旧ステップ 4「対ユーザー Track をサイレント activate」は 2026-08-21 の
      会話経路の Track なし化で対象消滅した (プロンプト待ち = 会話の出来事が
      開いていない状態そのものになったため、戻すべき帳簿が無い)。
    - 旧ステップ 2「running な autonomous Track を全 pause」は 2026-08-22
      (束 6c) の Track ランタイム退役で対象消滅した。running Track を作る
      書き手が消え、揃えるべき帳簿そのものが無くなったため、戻り値からも
      ``paused_tracks`` が落ちた (いまは ``{"autonomy_running": bool}`` だけ)。
    """

    def test_stop_autonomy_runs_both_steps(self):
        from saiverse.saiverse_manager import SAIVerseManager

        mgr = MagicMock()
        persona = MagicMock()
        mgr.personas = {"p1": persona}
        am = MagicMock()
        am.is_running = False
        mgr._autonomy_managers = {"p1": am}

        result = SAIVerseManager.stop_autonomy(mgr, "p1")

        # 1. AM 停止 / 2. AUTONOMY_ENABLED=False
        am.stop.assert_called_once()
        self.assertEqual(persona.autonomy_enabled, False)
        self.assertEqual(result, {"autonomy_running": False})


class TestStopAutonomyWritesThroughTheSharedWriter(unittest.TestCase):
    """停止ボタンの AUTONOMY_ENABLED=False は、自律の設定の書き手全員が通る一本
    (autonomy_wiring.write_persona_autonomy_enabled) で書く — DB の commit が
    成功してから in-memory へ公開する (2026-10-12 段 3 敵対レビューの消し込み:
    旧実装は DB の書き込みが失敗しても persona.autonomy_enabled=False を公開し、
    DB は ON・メモリは OFF — 再起動で ON に戻る — が残った)。
    """

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        db = self.SessionLocal()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
            db.flush()
            db.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
            db.add(AI(AIID="p1", HOME_CITYID=1, AINAME="P1", AUTONOMY_ENABLED=True))
            db.commit()
        finally:
            db.close()

        self.persona = SimpleNamespace(persona_id="p1", autonomy_enabled=True)
        self.stopped = []
        self.synced = []
        self.am = SimpleNamespace(
            stop=lambda: self.stopped.append("p1"), is_running=False,
        )
        self.mgr = SimpleNamespace(
            SessionLocal=self.SessionLocal,
            personas={"p1": self.persona},
            _autonomy_managers={"p1": self.am},
            ensure_autonomy_for=lambda pid: self.synced.append(
                (pid, self.persona.autonomy_enabled)
            ),
        )

    def _db_value(self):
        db = self.SessionLocal()
        try:
            return bool(db.query(AI).filter(AI.AIID == "p1").one().AUTONOMY_ENABLED)
        finally:
            db.close()

    def test_stop_writes_db_then_publishes_and_resyncs(self):
        from saiverse.saiverse_manager import SAIVerseManager

        result = SAIVerseManager.stop_autonomy(self.mgr, "p1")

        self.assertEqual(result, {"autonomy_running": False})
        self.assertEqual(self.stopped, ["p1"])
        self.assertFalse(self._db_value())
        self.assertFalse(self.persona.autonomy_enabled)
        self.assertEqual(self.synced, [("p1", False)])

    def test_db_write_failure_does_not_publish_off_to_memory(self):
        """③ DB の書き込みが失敗したら persona.autonomy_enabled を公開しない。"""
        from saiverse.saiverse_manager import SAIVerseManager

        def _failing_session():
            session = self.SessionLocal()

            def _boom():
                raise RuntimeError("database is locked")

            session.commit = _boom
            return session

        self.mgr.SessionLocal = _failing_session

        with self.assertRaises(RuntimeError):
            SAIVerseManager.stop_autonomy(self.mgr, "p1")

        # DB は ON のまま、メモリも ON のまま (食い違わない)。再同期も起きない
        self.assertTrue(self._db_value())
        self.assertTrue(self.persona.autonomy_enabled)
        self.assertEqual(self.synced, [])
        # 見張りの停止 (ステップ 1) は課金を止める側なので行われたまま
        self.assertEqual(self.stopped, ["p1"])


if __name__ == "__main__":
    unittest.main()
