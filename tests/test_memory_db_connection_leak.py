"""テーブルの用意に失敗したとき、開いた接続がその場で閉じられること。

接続を開いてからスキーマを用意して返す関数は、用意で倒れると **開いた接続を
誰も閉じられない**。呼び出し側の ``finally: conn.close()`` は、変数に接続が
代入される前に例外が飛ぶので到達しない。

用意は CREATE / ALTER / 旧テーブルからの移行を伴う書き込みなので、閉じられ
なかった接続は SQLite の書き込みロックを握ったまま残る。結果、**同じ
memory.db を触る後続の処理が待たされる** — 2026-09-02 の「Chronicle を開いた
後に Memopedia の読み込みが終わらない」という報告の調査で、同じ形の欠陥が
3 箇所見つかった (この 2 つと、tools/utilities/memory_settings_ui.py の
``_get_arasuji_connection``)。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _ConnectionSpy:
    """``sqlite3.connect`` をくるんで、作られた接続を控えておく。"""

    def __init__(self) -> None:
        self.created: list[sqlite3.Connection] = []
        self._real = sqlite3.connect

    def __call__(self, *args, **kwargs):
        conn = self._real(*args, **kwargs)
        self.created.append(conn)
        return conn

    def assert_all_closed(self, case: unittest.TestCase) -> None:
        case.assertTrue(self.created, "接続が 1 つも作られていない")
        for conn in self.created:
            with case.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class ArasujiConnectionLeakTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="arasuji_leak_")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "memory.db"
        sqlite3.connect(str(self.db_path)).close()

    def test_closes_connection_when_table_setup_fails(self) -> None:
        from api.routes.people import arasuji as arasuji_routes

        spy = _ConnectionSpy()
        with patch("sqlite3.connect", new=spy), patch(
            "sai_memory.arasuji.storage.init_arasuji_tables",
            side_effect=RuntimeError("移行の途中で倒れた"),
        ), patch.object(
            arasuji_routes, "get_persona_memory_db", return_value=self.db_path
        ):
            with self.assertRaises(RuntimeError):
                arasuji_routes._get_arasuji_db("persona_1")

        spy.assert_all_closed(self)

    def test_returns_a_usable_connection_on_success(self) -> None:
        """正常時は開いたまま返す (呼び出し側が finally で閉じる)。"""
        from api.routes.people import arasuji as arasuji_routes

        with patch.object(
            arasuji_routes, "get_persona_memory_db", return_value=self.db_path
        ):
            conn = arasuji_routes._get_arasuji_db("persona_1")
        self.assertIsNotNone(conn)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()

    def test_returns_none_when_the_database_file_is_absent(self) -> None:
        from api.routes.people import arasuji as arasuji_routes

        missing = Path(self._tmp.name) / "nope" / "memory.db"
        with patch.object(
            arasuji_routes, "get_persona_memory_db", return_value=missing
        ):
            self.assertIsNone(arasuji_routes._get_arasuji_db("persona_1"))


class InitDbConnectionLeakTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="init_db_leak_")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "memory.db"

    def test_closes_connection_when_schema_setup_fails(self) -> None:
        from sai_memory.memory import storage

        spy = _ConnectionSpy()
        with patch("sqlite3.connect", new=spy), patch.object(
            storage, "_init_schema", side_effect=RuntimeError("CREATE で倒れた")
        ):
            with self.assertRaises(RuntimeError):
                storage.init_db(str(self.db_path))

        spy.assert_all_closed(self)

    def test_returns_a_usable_connection_on_success(self) -> None:
        from sai_memory.memory import storage

        conn = storage.init_db(str(self.db_path))
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("messages", names)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
