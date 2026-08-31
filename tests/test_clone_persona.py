"""scripts/clone_persona_to_test_env.py のテスト。

一時ディレクトリに source (本番相当) / dest (テスト環境相当) の小さな DB と
ペルソナディレクトリを作り、複製・再マップ・上書き確認・source 非破壊を検証する。
"""
import hashlib
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, Building, BuildingOccupancyLog, City, User
from scripts.clone_persona_to_test_env import CloneError, clone_persona

PERSONA_ID = "air_city_a"


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_tree(root: Path) -> dict:
    """ディレクトリ配下の全ファイルの (mtime_ns, sha256) を記録する。"""
    snapshot = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            snapshot[str(p.relative_to(root))] = (p.stat().st_mtime_ns, _file_digest(p))
    return snapshot


class CloneTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clone_persona_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # --- source (本番相当) ---
        self.source_home = self.tmp / "prod_home"
        self.source_user_data = self.source_home / "user_data"
        self.source_db = self.source_user_data / "database" / "saiverse.db"
        self.source_db.parent.mkdir(parents=True)
        self._seed_source()

        # source ペルソナディレクトリ
        self.source_persona_dir = self.source_home / "personas" / PERSONA_ID
        (self.source_persona_dir / "day_reports").mkdir(parents=True)
        (self.source_persona_dir / "memory.db").write_bytes(b"MEMORY_DB_CONTENT" * 100)
        (self.source_persona_dir / "tasks.db").write_bytes(b"TASKS_DB_CONTENT")
        (self.source_persona_dir / "log.json").write_text("[]", encoding="utf-8")
        (self.source_persona_dir / "day_reports" / "2026-07-01.md").write_text(
            "# report", encoding="utf-8"
        )

        # source user_data/models にだけあるカスタムモデル + プロバイダ
        models_dir = self.source_user_data / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "my-local-model.json").write_text(
            '{"model": "my-local-model", "display_name": "Local", "provider_ref": "my_provider"}',
            encoding="utf-8",
        )
        providers_dir = self.source_user_data / "providers"
        providers_dir.mkdir(parents=True)
        (providers_dir / "my_provider.json").write_text(
            '{"id": "my_provider", "protocol": "openai_compat", "base_url": "http://localhost:1234/v1"}',
            encoding="utf-8",
        )

        # --- dest (テスト環境相当、setup_test_env のレイアウト) ---
        self.dest_home = self.tmp / "test_data" / ".saiverse"
        self.dest_user_data = self.tmp / "test_data" / "user_data"
        self.dest_db = self.dest_user_data / "database" / "saiverse.db"
        self.dest_db.parent.mkdir(parents=True)
        (self.dest_home / "personas").mkdir(parents=True)
        self._seed_dest()

    def _seed_source(self):
        engine = create_engine(f"sqlite:///{self.source_db}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            session.add(User(USERID=1, USERNAME="maha", PASSWORD="x", LOGGED_IN=False))
            session.add(City(CITYID=42, USERID=1, CITY_SLUG="city_a",
                             UI_PORT=8000, API_PORT=8001))
            session.add(Building(CITYID=42, BUILDINGID="prod_private_room",
                                 BUILDINGNAME="エアの私室"))
            session.add(Building(CITYID=42, BUILDINGID="prod_lobby",
                                 BUILDINGNAME="Test Lobby"))
            session.add(AI(
                AIID=PERSONA_ID,
                HOME_CITYID=42,
                AINAME="エア",
                SYSTEMPROMPT="you are air",
                DESCRIPTION="production persona",
                EMOTION='{"joy": 0.5}',
                AUTO_COUNT=7,
                LAST_AUTO_PROMPT_TIMES='{"prod_lobby": "2026-07-01"}',
                IS_DISPATCHED=True,
                DEFAULT_MODEL="my-local-model",
                LIGHTWEIGHT_MODEL="claude-haiku-4-5",
                PRIVATE_ROOM_ID="prod_private_room",
                AUTONOMY_ENABLED=True,
                LIFE_PURPOSE='{"purpose": "live"}',
            ))
            # 開いている occupancy 行 (現在位置): dest に同名 Building がある "Test Lobby"
            session.add(BuildingOccupancyLog(
                CITYID=42, BUILDINGID="prod_lobby", AIID=PERSONA_ID,
                ENTRY_TIMESTAMP=datetime(2026, 7, 1, 9, 0), EXIT_TIMESTAMP=None,
            ))
            session.commit()
        engine.dispose()

    def _seed_dest(self, with_city: bool = True):
        engine = create_engine(f"sqlite:///{self.dest_db}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            session.add(User(USERID=1, USERNAME="test_user", PASSWORD="x", LOGGED_IN=True))
            if with_city:
                session.add(City(CITYID=1, USERID=1, CITY_SLUG="test_city",
                                 UI_PORT=18000, API_PORT=18001))
                session.add(Building(CITYID=1, BUILDINGID="test_lobby",
                                     BUILDINGNAME="Test Lobby"))
                session.add(Building(CITYID=1, BUILDINGID="test_room_a",
                                     BUILDINGNAME="Test Room A"))
            session.commit()
        engine.dispose()

    def _clone(self, **kwargs):
        kwargs.setdefault("force", False)
        kwargs.setdefault("confirm", lambda _msg: False)
        return clone_persona(
            PERSONA_ID,
            source_db=self.source_db,
            source_home=self.source_home,
            dest_db=self.dest_db,
            dest_home=self.dest_home,
            **kwargs,
        )

    def _dest_session(self):
        engine = create_engine(f"sqlite:///{self.dest_db}")
        return sessionmaker(bind=engine)(), engine


class TestCloneBasics(CloneTestBase):
    def test_ai_row_cloned_and_remapped(self):
        self._clone()

        session, engine = self._dest_session()
        try:
            row = session.query(AI).filter(AI.AIID == PERSONA_ID).one()
            # 恒常設定は複製される
            self.assertEqual(row.AINAME, "エア")
            self.assertEqual(row.SYSTEMPROMPT, "you are air")
            self.assertEqual(row.EMOTION, '{"joy": 0.5}')
            self.assertEqual(row.DEFAULT_MODEL, "my-local-model")
            self.assertEqual(row.AUTONOMY_ENABLED, True)
            self.assertEqual(row.LIFE_PURPOSE, '{"purpose": "live"}')
            # HOME_CITYID は dest の City に再マップ
            self.assertEqual(row.HOME_CITYID, 1)
            # PRIVATE_ROOM_ID: dest に同 ID も同名も無い → dest の先頭 Building
            self.assertEqual(row.PRIVATE_ROOM_ID, "test_lobby")
            # 実行時状態はリセット
            self.assertFalse(row.IS_DISPATCHED)
            self.assertEqual(row.AUTO_COUNT, 0)
            self.assertIsNone(row.LAST_AUTO_PROMPT_TIMES)

            # 現在位置: 同名マッチ (Test Lobby) で開いている occupancy 行が挿入される
            occ = (
                session.query(BuildingOccupancyLog)
                .filter(BuildingOccupancyLog.AIID == PERSONA_ID)
                .filter(BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None))
                .one()
            )
            self.assertEqual(occ.BUILDINGID, "test_lobby")
            self.assertEqual(occ.CITYID, 1)
        finally:
            session.close()
            engine.dispose()

    def test_persona_dir_copied(self):
        self._clone()
        dest_dir = self.dest_home / "personas" / PERSONA_ID
        for rel in ["memory.db", "tasks.db", "log.json", "day_reports/2026-07-01.md"]:
            src = self.source_persona_dir / rel
            dst = dest_dir / rel
            self.assertTrue(dst.is_file(), f"missing: {rel}")
            self.assertEqual(_file_digest(src), _file_digest(dst), f"content differs: {rel}")

    def test_model_and_provider_json_copied(self):
        summary = self._clone()
        # my-local-model は source user_data にだけある → コピーされる
        copied_model = self.dest_user_data / "models" / "my-local-model.json"
        self.assertTrue(copied_model.is_file())
        copied_provider = self.dest_user_data / "providers" / "my_provider.json"
        self.assertTrue(copied_provider.is_file())
        self.assertIn("my-local-model", summary["models"]["copied_models"])
        self.assertIn("my_provider", summary["models"]["copied_providers"])
        # claude-haiku-4-5 は builtin にある → コピーしない
        self.assertIn("claude-haiku-4-5", summary["models"]["builtin"])
        self.assertFalse((self.dest_user_data / "models" / "claude-haiku-4-5.json").exists())


class TestCloneErrors(CloneTestBase):
    def test_dest_without_city_raises_clear_error(self):
        # dest DB を City 無しで作り直す
        self.dest_db.unlink()
        self._seed_dest(with_city=False)
        with self.assertRaises(CloneError) as ctx:
            self._clone()
        self.assertIn("setup_test_env", str(ctx.exception))

    def test_missing_source_persona_raises(self):
        with self.assertRaises(CloneError) as ctx:
            clone_persona(
                "no_such_persona",
                source_db=self.source_db,
                source_home=self.source_home,
                dest_db=self.dest_db,
                dest_home=self.dest_home,
                confirm=lambda _msg: False,
            )
        self.assertIn("no_such_persona", str(ctx.exception))


class TestOverwriteConfirmation(CloneTestBase):
    def test_existing_clone_aborts_without_force(self):
        self._clone()  # 1 回目は素通り
        prompts = []

        def deny(message):
            prompts.append(message)
            return False

        with self.assertRaises(CloneError):
            self._clone(confirm=deny)
        self.assertEqual(len(prompts), 1)
        self.assertIn(PERSONA_ID, prompts[0])

    def test_force_overwrites_without_prompt(self):
        self._clone()
        # dest 側を書き換えて、上書きされることを確認できる状態にする
        dest_memory = self.dest_home / "personas" / PERSONA_ID / "memory.db"
        dest_memory.write_bytes(b"STALE")

        def fail_confirm(_msg):
            raise AssertionError("--force 時は確認プロンプトを出してはいけない")

        self._clone(force=True, confirm=fail_confirm)
        self.assertEqual(
            _file_digest(dest_memory),
            _file_digest(self.source_persona_dir / "memory.db"),
        )
        # AI 行も 1 行のまま (重複しない)
        session, engine = self._dest_session()
        try:
            self.assertEqual(session.query(AI).filter(AI.AIID == PERSONA_ID).count(), 1)
            open_occ = (
                session.query(BuildingOccupancyLog)
                .filter(BuildingOccupancyLog.AIID == PERSONA_ID)
                .filter(BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None))
                .count()
            )
            self.assertEqual(open_occ, 1)
        finally:
            session.close()
            engine.dispose()


class TestSourceIsReadOnly(CloneTestBase):
    def test_source_untouched(self):
        before_db = _file_digest(self.source_db)
        before_home = _snapshot_tree(self.source_home)

        self._clone()
        # 上書き経路 (--force) でも source が無傷であること
        self._clone(force=True)

        self.assertEqual(before_db, _file_digest(self.source_db), "source DB が変更された")
        self.assertEqual(
            before_home, _snapshot_tree(self.source_home),
            "source home 配下のファイルが変更された",
        )
        # WAL/journal 等の新規ファイルも作られていないこと (_snapshot_tree はキー比較で検知)


if __name__ == "__main__":
    unittest.main()
