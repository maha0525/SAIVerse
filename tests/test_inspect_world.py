"""scripts/inspect_world.py のテスト。

検分 CLI の中核性質を検証する:
- 各 collect_* が実データを正しく読み出す (フィルタ含む)
- 対象は読み取り専用 (DB ファイルの mtime / バイト列が変わらない)
- 存在しない対象は InspectError で明示的に失敗する
"""
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from scripts.inspect_world import (
    EnvPaths,
    InspectError,
    collect_day_plan,
    collect_errors,
    collect_llm_io,
    collect_memory,
    collect_personas,
    collect_sessions,
    collect_tasks,
    collect_threads,
    collect_tracks,
    format_day_plan,
    format_errors,
    format_llm_io,
    format_memory,
    format_personas,
    format_tasks,
    format_tracks,
    main,
    resolve_session_dir,
)


def _make_world_db(path: Path) -> None:
    """saiverse.db 相当の最小 DB を SQLAlchemy モデルから作る。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import (
        AI, ActionTrack, Base, Building, BuildingOccupancyLog, City,
        PersonaDayPlan, PersonaTask, User,
    )

    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        session.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        session.flush()
        session.add(City(CITYID=1, USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001))
        session.add(Building(BUILDINGID="lobby", CITYID=1, BUILDINGNAME="ロビー", CAPACITY=10))
        session.add(AI(
            AIID="alice", AINAME="アリス", HOME_CITYID=1,
            AUTONOMY_ENABLED=True, DEFAULT_MODEL="model-x",
            PRIVATE_ROOM_ID="lobby",
        ))
        session.add(BuildingOccupancyLog(
            CITYID=1, BUILDINGID="lobby", AIID="alice",
            ENTRY_TIMESTAMP=datetime(2026, 7, 5, 9, 0), EXIT_TIMESTAMP=None,
        ))
        session.add(ActionTrack(
            track_id="track-1", persona_id="alice", short_id=1,
            title="調べ物", track_type="autonomous", status="running",
        ))
        session.add(ActionTrack(
            track_id="track-2", persona_id="alice", short_id=2,
            title="終わった作業", track_type="autonomous", status="completed",
        ))
        session.add(PersonaTask(
            id="task-1", persona_id="alice", short_id=1,
            title="下書きを書く", goal="本文が実在すること", status="active",
        ))
        session.add(PersonaTask(
            id="task-2", persona_id="alice", short_id=2,
            parent_kind="note", note_id=None,
            title="言葉の標本集", goal="", status="pending",
            desire_type="作る", desire_state="fresh", desire_source="会話で褒められた",
        ))
        session.add(PersonaDayPlan(
            persona_id="alice", plan_date="2026-07-05",
            slots_json=json.dumps([
                {"start": "10:00", "kind": "調べる", "title": "調べ物", "ref": "task:1",
                 "facility": "lobby", "budget_rounds": 4, "status": "done"},
                {"start": "20:00", "kind": "自室で過ごす", "title": "休む", "ref": "none",
                 "facility": "own_room", "budget_rounds": 0, "status": "skipped",
                 "skip_reason": "就寝が先に来た"},
            ]),
            meta_json=json.dumps({"day_theme": "テストの一日"}),
            created_at=datetime(2026, 7, 5, 9, 0),
            updated_at=datetime(2026, 7, 5, 22, 0),
        ))
        session.commit()
    finally:
        session.close()
        engine.dispose()


def _make_memory_db(path: Path) -> None:
    """memory.db 相当の最小 DB (実スキーマの必要列のみ)。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, resource_id TEXT,"
            " overview TEXT, overview_updated_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, role TEXT,"
            " content TEXT, resource_id TEXT, created_at INTEGER, metadata TEXT,"
            " origin_track_id TEXT, line_role TEXT, line_id TEXT,"
            " scope TEXT NOT NULL DEFAULT 'committed', paired_action_text TEXT,"
            " pulse_id TEXT, thought_signature TEXT, spell_origin_id TEXT, spell_seq INTEGER)"
        )
        conn.execute(
            "INSERT INTO threads (id, resource_id, overview) VALUES"
            " ('th1', 'alice', '日常会話のスレッド')"
        )
        base = int(datetime(2026, 7, 5, 12, 0).timestamp())
        rows = [
            ("m1", "th1", "user", "こんにちは", base,
             json.dumps({"tags": ["conversation"]}), None, "main_line", None, "committed"),
            ("m2", "th1", "assistant", "やあ、まはー！", base + 60,
             json.dumps({"tags": ["conversation"]}), "track-1", "main_line", None, "committed"),
            ("m3", "th1", "assistant", "内部で考えごとをしている", base + 120,
             json.dumps({"tags": ["internal"]}), "track-1", "sub_line", None, "volatile"),
        ]
        conn.executemany(
            "INSERT INTO messages (id, thread_id, role, content, created_at, metadata,"
            " origin_track_id, line_role, line_id, scope) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _make_logs(logs_dir: Path) -> Path:
    session_dir = logs_dir / "20260705_120000"
    session_dir.mkdir(parents=True)
    llm_entries = [
        ("LLM_REQUEST", {"type": "request", "source": "sea/pb", "node": "decide",
                         "persona_id": "alice", "persona_name": "アリス",
                         "messages": [{"role": "user", "content": "時間割を組んで"}]}),
        ("LLM_RESPONSE", {"type": "response", "source": "sea/pb", "node": "decide",
                          "persona_id": "alice", "persona_name": "アリス",
                          "output": "今日は調べ物から始める"}),
        ("LLM_REQUEST", {"type": "request", "source": "sea/other", "node": "speak",
                         "persona_id": "bob", "persona_name": "ボブ",
                         "messages": [{"role": "user", "content": "無関係"}]}),
    ]
    with (session_dir / "llm_io.log").open("w", encoding="utf-8") as f:
        for marker, entry in llm_entries:
            f.write(f"2026-07-05 12:00:00 [DEBUG] {marker}: "
                    f"{json.dumps(entry, ensure_ascii=False)}\n")
    with (session_dir / "error.log").open("w", encoding="utf-8") as f:
        f.write("2026-07-05 12:01:00 [WARNING] saiverse.foo: 何かの警告\n")
        f.write("2026-07-05 12:02:00 [ERROR] saiverse.bar: 落ちた\n")
        f.write("Traceback (most recent call last):\n")
        f.write("  ...\n")
    return session_dir


class InspectWorldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="inspect_world_test_"))
        cls.db = cls.tmp / "user_data" / "database" / "saiverse.db"
        cls.db.parent.mkdir(parents=True)
        _make_world_db(cls.db)
        cls.home = cls.tmp / ".saiverse"
        cls.memory_db = cls.home / "personas" / "alice" / "memory.db"
        cls.memory_db.parent.mkdir(parents=True)
        _make_memory_db(cls.memory_db)
        cls.logs_dir = cls.tmp / "user_data" / "logs"
        cls.session_dir = _make_logs(cls.logs_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- personas ---

    def test_personas(self):
        rows = collect_personas(self.db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["persona_id"], "alice")
        self.assertEqual(row["current_building"], "lobby")
        self.assertEqual(row["current_building_name"], "ロビー")
        self.assertIn("アリス", format_personas(rows))

    # --- memory / threads ---

    def test_memory_tag_filter(self):
        rows = collect_memory(self.memory_db, tags=["conversation"])
        self.assertEqual([r["id"] for r in rows], ["m1", "m2"])
        # 古い順で返ること
        self.assertEqual(rows[0]["role"], "user")
        self.assertIn("やあ、まはー！", format_memory(rows))

    def test_memory_scope_and_track_filter(self):
        rows = collect_memory(self.memory_db, scope="volatile")
        self.assertEqual([r["id"] for r in rows], ["m3"])
        rows = collect_memory(self.memory_db, track="track-1")
        self.assertEqual([r["id"] for r in rows], ["m2", "m3"])

    def test_memory_grep_and_tail(self):
        rows = collect_memory(self.memory_db, grep="こんにちは")
        self.assertEqual([r["id"] for r in rows], ["m1"])
        rows = collect_memory(self.memory_db, tail=1)
        self.assertEqual([r["id"] for r in rows], ["m3"])  # 最新 1 件

    def test_memory_date_filter(self):
        rows = collect_memory(self.memory_db, on_date="2026-07-05")
        self.assertEqual(len(rows), 3)
        rows = collect_memory(self.memory_db, on_date="2026-07-04")
        self.assertEqual(rows, [])

    def test_threads(self):
        rows = collect_threads(self.memory_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["msg_count"], 3)

    # --- tracks / tasks / day-plan ---

    def test_tracks_default_excludes_closed(self):
        rows = collect_tracks(self.db, "alice")
        self.assertEqual([r["track_id"] for r in rows], ["track-1"])
        rows_all = collect_tracks(self.db, "alice", include_all=True)
        self.assertEqual(len(rows_all), 2)
        self.assertIn("t:1", format_tracks(rows_all))

    def test_tasks_and_desires(self):
        rows = collect_tasks(self.db, "alice")
        self.assertEqual(len(rows), 2)
        desires = collect_tasks(self.db, "alice", desires_only=True)
        self.assertEqual([r["id"] for r in desires], ["task-2"])
        self.assertIn("desire", format_tasks(desires))

    def test_day_plan(self):
        plan = collect_day_plan(self.db, "alice")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["plan_date"], "2026-07-05")
        self.assertEqual(len(plan["slots"]), 2)
        text = format_day_plan(plan)
        self.assertIn("skip_reason=就寝が先に来た", text)
        self.assertIn("テストの一日", text)
        self.assertIsNone(collect_day_plan(self.db, "alice", plan_date="2020-01-01"))

    # --- sessions / llm-io / errors ---

    def test_sessions_and_resolve(self):
        rows = collect_sessions(self.logs_dir)
        self.assertEqual(rows[0]["session"], "20260705_120000")
        resolved = resolve_session_dir(self.logs_dir, "latest")
        self.assertEqual(resolved, self.session_dir)
        with self.assertRaises(InspectError):
            resolve_session_dir(self.logs_dir, "nonexistent")

    def test_llm_io_filters(self):
        entries = collect_llm_io(self.session_dir / "llm_io.log", persona="alice")
        self.assertEqual(len(entries), 2)
        entries = collect_llm_io(self.session_dir / "llm_io.log", entry_type="response")
        self.assertEqual(len(entries), 1)
        self.assertIn("今日は調べ物から始める", format_llm_io(entries))
        entries = collect_llm_io(self.session_dir / "llm_io.log", grep="時間割")
        self.assertEqual(len(entries), 1)

    def test_errors_digest(self):
        digest = collect_errors(self.session_dir)
        self.assertEqual(digest["counts"].get("WARNING saiverse.foo"), 1)
        self.assertEqual(digest["counts"].get("ERROR saiverse.bar"), 1)
        # traceback 継続行が ERROR エントリへ連結されること
        self.assertIn("Traceback", digest["entries"][-1])
        self.assertIn("saiverse.bar", format_errors(digest))

    # --- 読み取り専用性 ---

    def test_read_only(self):
        """検分後に対象 DB のバイト列が変わらないこと。"""
        before_world = self.db.read_bytes()
        before_memory = self.memory_db.read_bytes()
        collect_personas(self.db)
        collect_tracks(self.db, "alice", include_all=True)
        collect_memory(self.memory_db, tags=["conversation"])
        collect_threads(self.memory_db)
        self.assertEqual(self.db.read_bytes(), before_world)
        self.assertEqual(self.memory_db.read_bytes(), before_memory)

    def test_missing_db_is_explicit_error(self):
        with self.assertRaises(InspectError):
            collect_memory(self.tmp / "nope" / "memory.db")
        with self.assertRaises(InspectError):
            collect_personas(self.tmp / "nope.db")

    # --- CLI 経由 (main) ---

    def test_main_cli_smoke(self):
        rc = main([
            "memory", "alice", "--tags", "conversation", "--json",
            "--home", str(self.home), "--db", str(self.db),
        ])
        self.assertEqual(rc, 0)
        rc = main([
            "errors", "--session", "latest",
            "--home", str(self.home), "--db", str(self.db),
        ])
        self.assertEqual(rc, 0)

    def test_main_cli_missing_persona(self):
        rc = main([
            "memory", "ghost",
            "--home", str(self.home), "--db", str(self.db),
        ])
        self.assertEqual(rc, 1)


class EnvPathsTest(unittest.TestCase):
    def test_user_data_derivation(self):
        env = EnvPaths(Path("/h"), Path("/x/user_data/database/saiverse.db"))
        self.assertEqual(env.user_data, Path("/x/user_data"))
        self.assertEqual(env.logs_dir, Path("/x/user_data/logs"))
        env2 = EnvPaths(Path("/h"), Path("/elsewhere/saiverse.db"))
        self.assertEqual(env2.user_data, Path("/h/user_data"))

    def test_persona_memory_db(self):
        env = EnvPaths(Path("/h"), Path("/x/user_data/database/saiverse.db"))
        self.assertEqual(
            env.persona_memory_db("alice"), Path("/h/personas/alice/memory.db"),
        )


if __name__ == "__main__":
    unittest.main()
