"""Track Chronicle の生メッセージダンプが metabolism anchor 以降を除外することの回帰テスト。

自律 Track の生発言が head(track_chronicle) と tail(履歴) に二重に乗り 10k+ トークンを
浪費していた件 (2026-06-29) の修正検証。anchor (= 履歴の開始点) 以降のメッセージは履歴に
既にあるので生ダンプから除外する。
"""
import json
import sqlite3
import unittest

from builtin_data.tools.get_memory_weave_context import (
    _get_track_unprocessed_messages_text,
)

TRACK = "trk-1"


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE messages (id TEXT, role TEXT, content TEXT, created_at INTEGER, "
        "origin_track_id TEXT, line_role TEXT, scope TEXT, metadata TEXT)"
    )
    conn.execute(
        "CREATE TABLE arasuji_entries (source_ids_json TEXT, level INTEGER, "
        "origin_track_id TEXT, is_incomplete INTEGER)"
    )
    meta = json.dumps({"tags": []})
    # 古い→新しい順に 3 件 (main_line / committed / 同一 Track)
    for mid, ts, content in [
        ("m1", 100, "古い発言1"),
        ("m2", 200, "中間の発言2"),
        ("m3", 300, "最新の発言3"),
    ]:
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
            (mid, "assistant", content, ts, TRACK, "main_line", "committed", meta),
        )
    conn.commit()
    return conn


class TrackDumpAnchorCutoffTest(unittest.TestCase):
    def test_no_anchor_returns_all(self):
        conn = _make_conn()
        text = _get_track_unprocessed_messages_text(conn, TRACK)
        self.assertIn("古い発言1", text)
        self.assertIn("中間の発言2", text)
        self.assertIn("最新の発言3", text)

    def test_anchor_excludes_messages_at_or_after_anchor(self):
        conn = _make_conn()
        # anchor=m2 (ts=200) → created_at < 200 のみ = m1 だけ残る
        text = _get_track_unprocessed_messages_text(
            conn, TRACK, history_anchor_message_id="m2"
        )
        self.assertIn("古い発言1", text)
        self.assertNotIn("中間の発言2", text)   # anchor 自体 (履歴に載る) は除外
        self.assertNotIn("最新の発言3", text)   # anchor より新しい (履歴に載る) も除外

    def test_invalid_anchor_falls_back_to_full(self):
        conn = _make_conn()
        full = _get_track_unprocessed_messages_text(conn, TRACK)
        fallback = _get_track_unprocessed_messages_text(
            conn, TRACK, history_anchor_message_id="does-not-exist"
        )
        self.assertEqual(full, fallback)

    def test_all_newer_than_anchor_yields_empty(self):
        conn = _make_conn()
        # anchor=m1 (ts=100, 最古) → それより古いものは無い = 生ダンプ空
        text = _get_track_unprocessed_messages_text(
            conn, TRACK, history_anchor_message_id="m1"
        )
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
