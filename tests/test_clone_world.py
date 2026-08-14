"""scripts/clone_world_to_test_env.py のテスト。

world clone の中核性質を検証する:
- 世界の全体 (Building / Item / persona_task / playbooks 相当) が dest に写る
- 実行時状態のリセット (ポート / オンラインモード / addon / visiting_ai /
  thinking_request / 非対象ペルソナの自律行動 OFF) が intent doc §4 の表どおり
- source (本番) は一切変更されない (バイト列比較)
- 前提未達 (dest==source / 不在ペルソナ) は CloneError
"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.clone_world_to_test_env import CloneError, clone_world


def _make_source_db(path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import (
        AI, AddonConfig, Base, Building, BuildingOccupancyLog, City, Item,
        PersonaTask, ThinkingRequest, User, VisitingAI,
    )

    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        session.add(User(USERID=1, PASSWORD="x", USERNAME="maha"))
        session.flush()
        session.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a",
                         UI_PORT=3000, API_PORT=8000, START_IN_ONLINE_MODE=True))
        session.add(Building(BUILDINGID="library_a", CITYID=1,
                             BUILDINGNAME="図書館", CAPACITY=10,
                             FACILITY_ROLES='["library"]'))
        session.add(Building(BUILDINGID="quon_room", CITYID=1,
                             BUILDINGNAME="クオンの部屋", CAPACITY=2))
        session.add(AI(AIID="quon", AINAME="クオン", HOME_CITYID=1,
                       AUTONOMY_ENABLED=True, PRIVATE_ROOM_ID="quon_room",
                       IS_DISPATCHED=True, AUTO_COUNT=5,
                       LAST_AUTO_PROMPT_TIMES='["x"]'))
        session.add(AI(AIID="other", AINAME="他の子", HOME_CITYID=1,
                       AUTONOMY_ENABLED=True))
        session.add(BuildingOccupancyLog(
            CITYID=1, BUILDINGID="quon_room", AIID="quon",
            ENTRY_TIMESTAMP=datetime(2026, 7, 5, 9, 0), EXIT_TIMESTAMP=None,
        ))
        session.add(Item(ITEM_ID="item-1", NAME="過去の成果物", TYPE="document",
                         FILE_PATH="documents/artifact.md",
                         CREATED_AT=datetime(2026, 7, 1), UPDATED_AT=datetime(2026, 7, 1)))
        session.add(PersonaTask(id="task-1", persona_id="quon", short_id=1,
                                title="育ったタスク", goal="継続中", status="active"))
        session.add(VisitingAI(city_id=1, persona_id="visitor", profile_json="{}"))
        session.add(ThinkingRequest(request_id="req-1", city_id=1, persona_id="quon",
                                    request_context_json="{}"))
        session.add(AddonConfig(addon_name="discord_gateway", is_enabled=True))
        session.commit()
    finally:
        session.close()
        engine.dispose()


class CloneWorldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clone_world_test_"))
        # source 側の構築
        self.source_home = self.tmp / "source_home"
        self.source_user_data = self.source_home / "user_data"
        self.source_db = self.source_user_data / "database" / "saiverse.db"
        self.source_db.parent.mkdir(parents=True)
        _make_source_db(self.source_db)

        (self.source_user_data / "models").mkdir()
        (self.source_user_data / "models" / "custom.json").write_text(
            '{"model": "custom"}', encoding="utf-8")
        (self.source_home / "documents").mkdir()
        (self.source_home / "documents" / "artifact.md").write_text(
            "成果物の中身", encoding="utf-8")
        persona_dir = self.source_home / "personas" / "quon"
        persona_dir.mkdir(parents=True)
        conn = sqlite3.connect(persona_dir / "memory.db")
        conn.execute("CREATE TABLE messages (id TEXT, content TEXT)")
        conn.execute("INSERT INTO messages VALUES ('m1', '本物の記憶')")
        conn.commit()
        conn.close()
        (persona_dir / "log.json").write_text("[]", encoding="utf-8")

        self.dest_home = self.tmp / "dest_home"
        self.dest_db = self.tmp / "dest_user_data" / "database" / "saiverse.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clone(self, **kwargs):
        return clone_world(
            kwargs.pop("targets", ["quon"]),
            source_db=self.source_db,
            source_home=self.source_home,
            dest_db=self.dest_db,
            dest_home=self.dest_home,
            force=True,
            **kwargs,
        )

    def _dest_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.dest_db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_world_is_fully_copied(self):
        self._clone()
        conn = self._dest_conn()
        try:
            buildings = {r["BUILDINGID"]: r for r in conn.execute("SELECT * FROM building")}
            self.assertIn("library_a", buildings)
            self.assertEqual(buildings["library_a"]["FACILITY_ROLES"], '["library"]')
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM item").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT status FROM persona_task WHERE id='task-1'").fetchone()[0],
                "active")
            # 現在位置 (開いている occupancy 行) もそのまま
            row = conn.execute(
                "SELECT BUILDINGID FROM building_occupancy_log"
                " WHERE AIID='quon' AND EXIT_TIMESTAMP IS NULL").fetchone()
            self.assertEqual(row[0], "quon_room")
        finally:
            conn.close()

    def test_runtime_state_reset(self):
        summary = self._clone()
        conn = self._dest_conn()
        try:
            quon = conn.execute("SELECT * FROM ai WHERE AIID='quon'").fetchone()
            self.assertEqual(quon["AUTONOMY_ENABLED"], 1)  # 対象は本番の状態を保つ
            self.assertEqual(quon["IS_DISPATCHED"], 0)
            self.assertEqual(quon["AUTO_COUNT"], 0)
            self.assertIsNone(quon["LAST_AUTO_PROMPT_TIMES"])
            other = conn.execute("SELECT * FROM ai WHERE AIID='other'").fetchone()
            self.assertEqual(other["AUTONOMY_ENABLED"], 0)  # 非対象は自律行動 OFF
            city = conn.execute("SELECT * FROM city").fetchone()
            self.assertEqual(city["UI_PORT"], 18000)
            self.assertEqual(city["API_PORT"], 18001)
            self.assertEqual(city["START_IN_ONLINE_MODE"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM visiting_ai").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM thinking_request").fetchone()[0], 0)
            addon = conn.execute("SELECT is_enabled FROM addon_config").fetchone()
            self.assertEqual(addon[0], 0)
        finally:
            conn.close()
        self.assertEqual(summary["reset"]["autonomy_disabled_personas"], 1)

    def test_keep_addons(self):
        self._clone(keep_addons=True)
        conn = self._dest_conn()
        try:
            self.assertEqual(
                conn.execute("SELECT is_enabled FROM addon_config").fetchone()[0], 1)
        finally:
            conn.close()

    def test_files_copied(self):
        self._clone()
        # user_data 資源
        self.assertEqual(
            json.loads((self.dest_db.parent.parent / "models" / "custom.json")
                       .read_text(encoding="utf-8"))["model"],
            "custom")
        # メディア (Item.FILE_PATH の実体)
        self.assertEqual(
            (self.dest_home / "documents" / "artifact.md").read_text(encoding="utf-8"),
            "成果物の中身")
        # ペルソナディレクトリ (memory.db は backup API 経由でも中身が読める)
        conn = sqlite3.connect(self.dest_home / "personas" / "quon" / "memory.db")
        try:
            self.assertEqual(
                conn.execute("SELECT content FROM messages").fetchone()[0], "本物の記憶")
        finally:
            conn.close()
        self.assertTrue((self.dest_home / "personas" / "quon" / "log.json").exists())

    def test_media_incremental_sync(self):
        self._clone()
        # 前回クローンの残骸 (source に無いファイル) は再クローンで消える
        stale = self.dest_home / "documents" / "stale.md"
        stale.write_text("残骸", encoding="utf-8")
        # 変更なしファイルはスキップされる (mtime が保存されたまま)
        target = self.dest_home / "documents" / "artifact.md"
        mtime_before = target.stat().st_mtime_ns
        summary = self._clone()
        self.assertFalse(stale.exists())
        self.assertEqual(target.stat().st_mtime_ns, mtime_before)
        self.assertEqual(summary["copied_dirs"]["documents"], 0)  # 全部スキップ

    def test_no_media(self):
        self._clone(include_media=False)
        self.assertFalse((self.dest_home / "documents").exists())

    def test_source_untouched(self):
        before_db = self.source_db.read_bytes()
        before_memory = (self.source_home / "personas" / "quon" / "memory.db").read_bytes()
        self._clone()
        self.assertEqual(self.source_db.read_bytes(), before_db)
        self.assertEqual(
            (self.source_home / "personas" / "quon" / "memory.db").read_bytes(),
            before_memory)

    def test_same_path_rejected(self):
        with self.assertRaises(CloneError):
            clone_world(
                ["quon"],
                source_db=self.source_db, source_home=self.source_home,
                dest_db=self.source_db, dest_home=self.dest_home, force=True,
            )

    def test_missing_persona_rejected(self):
        with self.assertRaises(CloneError):
            self._clone(targets=["ghost"])

    def test_overwrite_requires_confirmation(self):
        self._clone()
        asked = []

        def deny(message):
            asked.append(message)
            return False

        with self.assertRaises(CloneError):
            clone_world(
                ["quon"],
                source_db=self.source_db, source_home=self.source_home,
                dest_db=self.dest_db, dest_home=self.dest_home,
                confirm=deny,
            )
        self.assertEqual(len(asked), 1)


if __name__ == "__main__":
    unittest.main()
