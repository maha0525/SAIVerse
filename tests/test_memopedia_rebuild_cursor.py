"""ログからの Memopedia 再構築の「再開位置」の契約 (Codex 三巡 #2 / 四巡 #1)。

再開位置は (時刻, 行番号) の組。時刻だけだと同じ秒のメッセージの順序を表せず、

- 「その時刻より後」だと、同じ秒がバッチの境目をまたいだとき境目の行が落ちて
  二度と処理されない
- 「その時刻から」だと、同じバッチを何度も取り直して永久に進まない

の両方が起きる。ここでは取得側の条件だけを取り出して、その二つが起きないことを見る。
"""

import sqlite3
import unittest


def _fetch(conn, *, start_after=0.0, start_after_rowid=0, limit=0):
    """api/routes/people/memopedia.py と同じ取得条件。"""
    query = "SELECT rowid, id, created_at FROM messages WHERE 1=1"
    params = []
    if start_after > 0:
        query += " AND (created_at > ? OR (created_at = ? AND rowid > ?))"
        params.extend([start_after, start_after, start_after_rowid])
    query += " ORDER BY created_at ASC, rowid ASC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


class TestRebuildResumeCursor(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, created_at REAL)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _add(self, msg_id, created_at):
        self.conn.execute(
            "INSERT INTO messages (id, created_at) VALUES (?, ?)",
            (msg_id, created_at),
        )
        self.conn.commit()

    def test_same_second_rows_are_not_skipped(self):
        """⭐ 同じ秒がバッチの境目をまたいでも、続きから漏れなく取れる。"""
        for i in range(5):
            self._add(f"m{i}", 1000.0)  # 全部同じ秒

        first = _fetch(self.conn, limit=2)
        self.assertEqual([r[1] for r in first], ["m0", "m1"])

        last_rowid, _, last_ts = first[-1]
        rest = _fetch(self.conn, start_after=last_ts, start_after_rowid=last_rowid)
        self.assertEqual([r[1] for r in rest], ["m2", "m3", "m4"])

    def test_cursor_makes_progress_on_same_second_rows(self):
        """⭐ 同じ秒だけの並びでも、再開のたびに必ず前へ進む (無限に取り直さない)。"""
        for i in range(6):
            self._add(f"m{i}", 1000.0)

        seen = []
        ts, rowid = 0.0, 0
        for _ in range(10):  # 進まなければ回り続ける
            rows = _fetch(self.conn, start_after=ts, start_after_rowid=rowid, limit=2)
            if not rows:
                break
            seen.extend(r[1] for r in rows)
            rowid, _, ts = rows[-1]

        self.assertEqual(seen, [f"m{i}" for i in range(6)], "同じ範囲を取り直している")

    def test_cursor_covers_rows_with_later_timestamps(self):
        """時刻が進んだ行は、行番号に関係なく続きとして取れる。"""
        self._add("old", 1000.0)
        self._add("new", 2000.0)

        rows = _fetch(self.conn, start_after=1000.0, start_after_rowid=1)
        self.assertEqual([r[1] for r in rows], ["new"])


if __name__ == "__main__":
    unittest.main()
