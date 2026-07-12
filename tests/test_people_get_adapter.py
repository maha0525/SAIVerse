"""people API 共通 get_adapter / persona ID 検証のテスト (P2 監査修正)。

対象: api/routes/people/utils.py
- validate_persona_id: `..`・パス区切り・ドライブ指定・空文字の拒否 (400)
- get_adapter: 不明 persona ID は 404 になり、personas/<id>/ が作られないこと
- get_adapter: ロード済み persona は従来どおり本人の adapter が返ること
- get_adapter: 未ロードだが main DB の AI テーブルに実在する persona は
  一時 adapter が作られ、with 終了時に close されること
- ensure_persona_exists: import 系エンドポイントの関所 (400/404)

出典: docs/handoff/2026-07-12_memory_persona_boundary_audit.md
「[P2] 共通 get_adapter() が存在しない・未ロードの persona ID を受け入れて
memory.db を新規作成する」
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.people.utils import (
    ensure_persona_exists,
    get_adapter,
    validate_persona_id,
)
from database.models import AI, Base, City, User


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class PeopleGetAdapterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.personas_root = self.home / "personas"
        self.addCleanup(self._cleanup_temp)

        # get_personas_dir() / SAIMemoryAdapter の解決先を temp に向ける
        os.environ["SAIVERSE_HOME"] = str(self.home)
        self.addCleanup(os.environ.pop, "SAIVERSE_HOME", None)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)
        # 起動時バックアップの背景スレッドはテストでは不要 (temp 掃除の邪魔)
        os.environ["SAIMEMORY_BACKUP_ON_START"] = "0"
        self.addCleanup(os.environ.pop, "SAIMEMORY_BACKUP_ON_START", None)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        # main DB (in-memory): loaded_ai はロード済み、offline_ai は DB のみ
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(AI(AIID="loaded_ai", HOME_CITYID=city.CITYID, AINAME="ロード済み"))
            db.add(AI(AIID="offline_ai", HOME_CITYID=city.CITYID, AINAME="未ロード"))
            db.commit()
        finally:
            db.close()

        # ロード済み persona には事前生成 adapter を積む
        from saiverse_memory import SAIMemoryAdapter

        loaded_dir = self.personas_root / "loaded_ai"
        loaded_dir.mkdir(parents=True, exist_ok=True)
        self.loaded_adapter = SAIMemoryAdapter(
            "loaded_ai", persona_dir=loaded_dir, resource_id="loaded_ai"
        )
        self.addCleanup(self.loaded_adapter.close)
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas={"loaded_ai": SimpleNamespace(sai_memory=self.loaded_adapter)},
        )

    def _cleanup_temp(self):
        import gc

        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            # Windows: sqlite ハンドル解放が rmtree に間に合わないことがある
            pass

    # ------------------------------------------------------------------
    # validate_persona_id
    # ------------------------------------------------------------------

    def test_validator_rejects_malformed_ids(self):
        bad_ids = [
            "",
            "   ",
            ".",
            "..",
            "a/b",
            "a\\b",
            "../escape",
            "..\\escape",
            "/absolute",
            "\\absolute",
            "C:evil",
            "C:\\evil",
            "a\x00b",
            None,
            123,
        ]
        for pid in bad_ids:
            with self.subTest(pid=pid):
                with self.assertRaises(HTTPException) as ctx:
                    validate_persona_id(pid)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_validator_accepts_normal_ids(self):
        for pid in ["air", "air_city_a", "quon-2", "tester.01"]:
            with self.subTest(pid=pid):
                self.assertEqual(validate_persona_id(pid), pid)

    # ------------------------------------------------------------------
    # get_adapter
    # ------------------------------------------------------------------

    def test_unknown_id_raises_404_and_creates_no_directory(self):
        with self.assertRaises(HTTPException) as ctx:
            with get_adapter("ghost", self.manager):
                pass
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertFalse((self.personas_root / "ghost").exists())

    def test_malformed_id_raises_400_and_creates_no_directory(self):
        before = set(self.personas_root.iterdir())
        for pid in ["..", "a/b", "a\\b", "C:evil", ""]:
            with self.subTest(pid=pid):
                with self.assertRaises(HTTPException) as ctx:
                    with get_adapter(pid, self.manager):
                        pass
                self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(set(self.personas_root.iterdir()), before)

    def test_loaded_persona_returns_attached_adapter(self):
        with get_adapter("loaded_ai", self.manager) as adapter:
            self.assertIs(adapter, self.loaded_adapter)
        # 本人の adapter は with 終了後も閉じられない
        self.assertTrue(self.loaded_adapter.is_ready())
        self.loaded_adapter.conn.execute("SELECT 1")

    def test_offline_persona_in_db_gets_temporary_adapter(self):
        with get_adapter("offline_ai", self.manager) as adapter:
            self.assertIsNotNone(adapter)
            self.assertTrue(adapter.is_ready())
            self.assertEqual(adapter.persona_id, "offline_ai")
            temp_conn = adapter.conn
        # 一時 adapter は with 終了時に close される
        with self.assertRaises(sqlite3.ProgrammingError):
            temp_conn.execute("SELECT 1")

    # ------------------------------------------------------------------
    # ensure_persona_exists (import 系エンドポイントの関所)
    # ------------------------------------------------------------------

    def test_ensure_persona_exists_accepts_loaded_and_db_personas(self):
        self.assertEqual(ensure_persona_exists("loaded_ai", self.manager), "loaded_ai")
        self.assertEqual(ensure_persona_exists("offline_ai", self.manager), "offline_ai")

    def test_ensure_persona_exists_rejects_unknown_and_malformed(self):
        with self.assertRaises(HTTPException) as ctx:
            ensure_persona_exists("ghost", self.manager)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertFalse((self.personas_root / "ghost").exists())

        with self.assertRaises(HTTPException) as ctx:
            ensure_persona_exists("../escape", self.manager)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_ensure_persona_exists_without_session_factory_falls_back_to_404(self):
        manager = SimpleNamespace(personas={})
        with self.assertRaises(HTTPException) as ctx:
            ensure_persona_exists("ghost", manager)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
