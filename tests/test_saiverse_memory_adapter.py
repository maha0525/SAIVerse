from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sai_memory.memory.storage import get_messages_last

class ActiveThreadAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name)
        os.environ["SAIMEMORY_MEMORY"] = "0"

        # Register temp dir cleanup first (LIFO → runs last, after adapter closes)
        self.addCleanup(self._cleanup_temp)

        # Lazy import to apply environment overrides before settings load.
        from saiverse_memory.adapter import SAIMemoryAdapter

        self.adapter_cls = SAIMemoryAdapter

    def _cleanup_temp(self) -> None:
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def tearDown(self) -> None:
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _create_adapter(self):
        return self.adapter_cls("tester", persona_dir=self.persona_dir)

    def test_default_persona_suffix(self) -> None:
        adapter = self._create_adapter()
        thread_id = adapter._thread_id()
        self.assertEqual(thread_id, "tester:__persona__")

    def test_reads_active_state_file(self) -> None:
        adapter = self._create_adapter()
        state_path = self.persona_dir / "active_state.json"
        state_path.write_text(json.dumps({"active_thread_id": "uuid-123"}), encoding="utf-8")

        thread_id = adapter._thread_id()
        self.assertEqual(thread_id, "tester:uuid-123")

    def test_thread_suffix_override(self) -> None:
        adapter = self._create_adapter()
        state_path = self.persona_dir / "active_state.json"
        state_path.write_text(json.dumps({"active_thread_id": "uuid-123"}), encoding="utf-8")

        thread_id = adapter._thread_id(thread_suffix="custom")
        self.assertEqual(thread_id, "tester:custom")

    def test_building_id_prioritised(self) -> None:
        adapter = self._create_adapter()
        thread_id = adapter._thread_id("room-42")
        self.assertEqual(thread_id, "tester:room-42")

    def test_metadata_links_expand_content(self) -> None:
        os.environ["SAIMEMORY_MEMORY"] = "1"

        class DummyEmbedder:
            def __init__(self, model: str | None = None, **kwargs) -> None:
                self.model_name = model

            def embed(self, texts, **kwargs):
                return [[0.0] * 3 for _ in texts]

        with patch("saiverse_memory.adapter.Embedder", DummyEmbedder):
            adapter = self.adapter_cls("tester", persona_dir=self.persona_dir)
            try:
                sns_suffix = "sns-thread"
                sns_thread_id = adapter._thread_id(None, thread_suffix=sns_suffix)
                adapter.append_persona_message(
                    {
                        "role": "user",
                        "content": "SNSを眺めていたよ",
                        "timestamp": "2025-01-01T00:00:00",
                        "embedding_chunks": 0,
                    },
                    thread_suffix=sns_suffix,
                )
                adapter.append_persona_message(
                    {
                        "role": "assistant",
                        "content": "猫ロボットの動画が面白かった",
                        "timestamp": "2025-01-01T00:01:00",
                        "embedding_chunks": 0,
                    },
                    thread_suffix=sns_suffix,
                )
                with adapter._db_lock:
                    sns_messages = get_messages_last(adapter.conn, sns_thread_id, 5)
                    anchor = sns_messages[-1]

                metadata = {
                    "other_thread_messages": [
                        {
                            "thread_id": sns_thread_id,
                            "message_id": anchor.id,
                            "range_before": 1,
                            "range_after": 0,
                        }
                    ]
                }
                adapter.append_persona_message(
                    {
                        "role": "system",
                        "content": "moved from sns-thread",
                        "timestamp": "2025-01-01T00:02:00",
                        "embedding_chunks": 0,
                        "metadata": metadata,
                    }
                )

                messages = adapter.recent_persona_messages(5000)
                linked = [m for m in messages if "linked-thread" in m.get("content", "")]
                self.assertTrue(linked, "Expected linked thread snippet in recent messages")
                self.assertIn("tester:sns-thread", linked[0]["content"])
                self.assertIn("猫ロボットの動画が面白かった", linked[0]["content"])
            finally:
                adapter.close()
        os.environ["SAIMEMORY_MEMORY"] = "0"


class OriginEpisodeColumnTest(unittest.TestCase):
    """W1 Chunk C / D10: origin_episode の専用列昇格 + 読み口。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

    def _cleanup_temp(self) -> None:
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def tearDown(self) -> None:
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _db_path(self, name: str = "memory.db") -> str:
        return str(self.persona_dir / name)

    def test_new_db_has_column_and_index(self) -> None:
        from sai_memory.memory.storage import init_db
        conn = init_db(self._db_path())
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            self.assertIn("origin_episode", cols)
            indexes = {r[1] for r in conn.execute("PRAGMA index_list(messages)")}
            self.assertIn("idx_messages_origin_episode", indexes)
        finally:
            conn.close()

    def test_add_message_transcribes_metadata_to_column(self) -> None:
        from sai_memory.memory.storage import add_message, init_db
        conn = init_db(self._db_path())
        try:
            with_ep = add_message(
                conn, "t1", "assistant", "作業した",
                metadata={"tags": ["work_session"], "origin_episode": "episode:3"},
            )
            without_ep = add_message(conn, "t1", "assistant", "普通の発話")
            row = conn.execute(
                "SELECT origin_episode FROM messages WHERE id=?", (with_ep,)
            ).fetchone()
            self.assertEqual(row[0], "episode:3")
            row = conn.execute(
                "SELECT origin_episode FROM messages WHERE id=?", (without_ep,)
            ).fetchone()
            self.assertIsNone(row[0])
        finally:
            conn.close()

    def test_backfill_from_legacy_metadata(self) -> None:
        """列の無い旧 DB を init_db が開いたとき、一度だけ metadata から転記する。"""
        import json as json_mod
        import sqlite3

        path = self._db_path("legacy.db")
        legacy = sqlite3.connect(path)
        try:
            legacy.execute(
                "CREATE TABLE messages ("
                "id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
                "resource_id TEXT, created_at INTEGER, metadata TEXT)"
            )
            legacy.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("m1", "t1", "assistant", "セッションの発話", "r", 100,
                 json_mod.dumps({"origin_episode": "episode:7"})),
            )
            legacy.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("m2", "t1", "assistant", "無関係の発話", "r", 101, None),
            )
            legacy.commit()
        finally:
            legacy.close()

        from sai_memory.memory.storage import init_db
        conn = init_db(path)
        try:
            rows = dict(conn.execute(
                "SELECT id, origin_episode FROM messages"
            ).fetchall())
            self.assertEqual(rows["m1"], "episode:7")
            self.assertIsNone(rows["m2"])
        finally:
            conn.close()

    def test_adapter_get_messages_by_origin_episode(self) -> None:
        """読み口: origin_episode 厳密フィルタ + 時系列順 + レンダリングに足る列。"""

        class DummyEmbedder:
            def __init__(self, model: str | None = None, **kwargs) -> None:
                self.model_name = model

            def embed(self, texts, **kwargs):
                return [[0.0] * 3 for _ in texts]

        with patch("saiverse_memory.adapter.Embedder", DummyEmbedder):
            from saiverse_memory.adapter import SAIMemoryAdapter
            adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_dir)
            try:
                def _add(content, ep, ts, role="assistant"):
                    adapter.append_persona_message({
                        "role": role,
                        "content": content,
                        "timestamp": ts,
                        "metadata": {"origin_episode": ep} if ep else {},
                        "line_role": "sub_line",
                        "scope": "volatile",
                    })

                _add("2 番目の発話", "episode:1", "2026-07-04T10:05:00+00:00")
                _add("1 番目の発話", "episode:1", "2026-07-04T10:00:00+00:00")
                _add("別の出来事の発話", "episode:2", "2026-07-04T10:02:00+00:00")
                _add("出来事の外の発話", None, "2026-07-04T10:03:00+00:00")
                _add("スペル結果", "episode:1", "2026-07-04T10:06:00+00:00",
                     role="system")

                rows = adapter.get_messages_by_origin_episode("episode:1")
                self.assertEqual(
                    [r["content"] for r in rows],
                    ["1 番目の発話", "2 番目の発話", "スペル結果"],
                )
                self.assertEqual(rows[2]["role"], "system")
                self.assertEqual(rows[0]["scope"], "volatile")
                self.assertEqual(rows[0]["line_role"], "sub_line")
                self.assertIsInstance(rows[0]["created_at"], int)
                self.assertEqual(
                    rows[0]["metadata"], {"origin_episode": "episode:1"},
                )
                self.assertEqual(adapter.get_messages_by_origin_episode("episode:9"), [])
            finally:
                adapter.close()


if __name__ == "__main__":
    unittest.main()
