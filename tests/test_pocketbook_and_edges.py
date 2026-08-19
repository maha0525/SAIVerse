"""自律行動 v3 形の層 束1: memory.db の新テーブル4枚と読み書き関数の契約。

- 手帳 (activities / memos): docs/intent/autonomous_behavior_v3.md §13.1 / §13.6
- thread_edges: docs/issues/memory_continuity_graph.md 決着節
- chunk_page_edges: 同 intent §13.6「B2 欄と辺の格納」

一時ディレクトリの DB だけを使う。本番の ~/.saiverse には触れない。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sai_memory.memory import continuity, pocketbook, recall_edges
from sai_memory.memory.storage import add_message, get_message, init_db


class PocketbookTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "memory.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass


class TestInitIsAdditiveAndIdempotent(PocketbookTestBase):
    def _table_names(self, conn: sqlite3.Connection) -> set:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {r[0] for r in rows}

    def test_init_db_creates_the_four_new_tables(self):
        names = self._table_names(self.conn)
        for table in ("activities", "memos", "thread_edges", "chunk_page_edges"):
            self.assertIn(table, names)

    def test_reopening_an_existing_db_preserves_data(self):
        """⭐ 既存 DB に対する init が壊さない (IF NOT EXISTS の検算)。"""
        mid = add_message(self.conn, "t1", "user", "hello")
        act = pocketbook.add_activity(self.conn, "小説を書く", "sluice")
        self.conn.close()

        conn2 = init_db(self.db_path)
        self.addCleanup(conn2.close)
        self.assertIsNotNone(get_message(conn2, mid))
        got = pocketbook.get_activity(conn2, act.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "小説を書く")

        # 再 init 後もスキーマは同一 (テーブルの重複定義や作り直しをしていない)。
        self.conn = init_db(self.db_path)  # tearDown 用に差し替え
        conn2_names = self._table_names(conn2)
        self.assertEqual(self._table_names(self.conn), conn2_names)


class TestActivities(PocketbookTestBase):
    def test_add_and_list_activities(self):
        a = pocketbook.add_activity(self.conn, "小説を書く", "sluice", born_at=100)
        b = pocketbook.add_activity(self.conn, "絵の練習", "initial", born_at=200)

        opened = pocketbook.list_activities(self.conn)
        self.assertEqual([x.id for x in opened], [a.id, b.id])
        self.assertEqual(opened[0].status, "open")
        self.assertEqual(opened[0].origin, "sluice")
        self.assertEqual(opened[0].born_at, 100)
        self.assertIsNone(opened[0].closed_at)

    def test_origin_is_a_closed_vocabulary(self):
        with self.assertRaises(ValueError):
            pocketbook.add_activity(self.conn, "散歩", "llm_invented")

    def test_rename_activity(self):
        a = pocketbook.add_activity(self.conn, "小説", "user")
        self.assertTrue(pocketbook.rename_activity(self.conn, a.id, "小説を書く"))
        self.assertEqual(pocketbook.get_activity(self.conn, a.id).name, "小説を書く")
        self.assertFalse(pocketbook.rename_activity(self.conn, 9999, "無い"))

    def test_born_at_and_closed_at_must_be_int_epochs(self):
        """SQLite は列型を強制しない — 文字列・float・bool の epoch は入口で
        拒否する (永続すると born_at 順の一覧を毒する)。bool は int の
        サブクラスなので明示拒否の検算対象。"""
        for bad in ("123", 1.5, True):
            with self.assertRaises(ValueError):
                pocketbook.add_activity(self.conn, "散歩", "user", born_at=bad)
        count = self.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        self.assertEqual(count, 0)

        a = pocketbook.add_activity(self.conn, "散歩", "user")
        for bad in ("123", 1.5, True):
            with self.assertRaises(ValueError):
                pocketbook.close_activity(self.conn, a.id, closed_at=bad)
        self.assertEqual(pocketbook.get_activity(self.conn, a.id).status, "open")

    def test_explicit_close_is_the_only_close_and_keeps_the_event_date(self):
        """⭐ 閉じるのは明示操作の関数一つだけで、closed_at は明示の日付。"""
        a = pocketbook.add_activity(self.conn, "小説を書く", "user")
        self.assertTrue(pocketbook.close_activity(self.conn, a.id, closed_at=500))

        got = pocketbook.get_activity(self.conn, a.id)
        self.assertEqual(got.status, "closed")
        self.assertEqual(got.closed_at, 500)

        # 再クローズは明示の出来事の日付を上書きしない。
        self.assertFalse(pocketbook.close_activity(self.conn, a.id, closed_at=900))
        self.assertEqual(pocketbook.get_activity(self.conn, a.id).closed_at, 500)

        # 一覧: 既定は open のみ、include_closed で全部。
        self.assertEqual(pocketbook.list_activities(self.conn), [])
        every = pocketbook.list_activities(self.conn, include_closed=True)
        self.assertEqual([x.id for x in every], [a.id])


class TestMemos(PocketbookTestBase):
    def setUp(self):
        super().setUp()
        self.act = pocketbook.add_activity(self.conn, "小説を書く", "sluice")

    def test_add_memo_persists_span(self):
        m = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-19", "did", "冒頭の三行を書いた",
            span_start_id="msg-a", span_end_id="msg-b",
        )
        rows = pocketbook.list_memos(self.conn, self.act.id)
        self.assertEqual([x.id for x in rows], [m.id])
        self.assertEqual(rows[0].span_start_id, "msg-a")
        self.assertEqual(rows[0].span_end_id, "msg-b")
        self.assertEqual(rows[0].kind, "did")
        self.assertEqual(rows[0].text, "冒頭の三行を書いた")

    def test_memo_validation(self):
        with self.assertRaises(ValueError):
            pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "hope", "x")
        with self.assertRaises(ValueError):
            pocketbook.add_memo(self.conn, self.act.id, "8/19", "did", "x")
        with self.assertRaises(ValueError):
            pocketbook.add_memo(self.conn, 9999, "2026-08-19", "did", "x")

    def test_memo_date_must_be_a_real_calendar_date(self):
        """字面が 'YYYY-MM-DD' でも暦に無い日付は拒否 — '9999-99-99' や
        '2026-02-31' が通ると MAX(date) の文字列比較の導出 (最終メモ日付・
        未消化) を毒する。全角数字は \\d が通すので [0-9] 明記で拒否。"""
        for bad in ("9999-99-99", "2026-02-31", "2026-13-01", "2026-00-10",
                    "２０２６-08-19"):
            with self.assertRaises(ValueError):
                pocketbook.add_memo(self.conn, self.act.id, bad, "did", "x")
        count = self.conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
        self.assertEqual(count, 0)
        # 実在する暦日は通る (うるう日を含む)。
        m = pocketbook.add_memo(self.conn, self.act.id, "2028-02-29", "did", "x")
        self.assertEqual(m.date, "2028-02-29")

    def test_activity_id_must_be_int_not_bool_or_str(self):
        """activity_id は bool 拒否つき int 検査 — True は id=1 に、str '1' は
        INTEGER affinity で 1 に一致して、無関係な行を黙って読み書きする。"""
        for bad in (True, "1", 1.0):
            with self.assertRaises(ValueError):
                pocketbook.get_activity(self.conn, bad)
            with self.assertRaises(ValueError):
                pocketbook.close_activity(self.conn, bad)
            with self.assertRaises(ValueError):
                pocketbook.add_memo(self.conn, bad, "2026-08-19", "did", "x")
            with self.assertRaises(ValueError):
                pocketbook.list_memos(self.conn, bad)
            with self.assertRaises(ValueError):
                pocketbook.get_last_memo_date(self.conn, bad)
            with self.assertRaises(ValueError):
                pocketbook.list_undigested_want_memos(self.conn, bad)
        # 必須引数の関数では None も拒否 (list_undigested_want_memos の None は
        # 「全アクティビティ横断」の正当な省略なので対象外)。
        with self.assertRaises(ValueError):
            pocketbook.get_activity(self.conn, None)
        with self.assertRaises(ValueError):
            pocketbook.rename_activity(self.conn, None, "新しい名前")
        self.assertEqual(pocketbook.get_activity(self.conn, self.act.id).status,
                         "open")

    def test_memo_span_and_idem_key_must_be_str_or_none(self):
        """span_start_id / span_end_id / idem_key は str | None 限定 —
        非文字列は messages.id への降り口・冪等キーとして照合できない。"""
        with self.assertRaises(ValueError):
            pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "did", "x",
                                span_start_id=42)
        with self.assertRaises(ValueError):
            pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "did", "x",
                                span_end_id=42)
        with self.assertRaises(ValueError):
            pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "did", "x",
                                idem_key=42)
        with self.assertRaises(ValueError):
            pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "did", "x",
                                idem_key="   ")
        count = self.conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
        self.assertEqual(count, 0)

    def test_list_memos_filters_by_kind_and_orders_by_date(self):
        pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "did", "b")
        pocketbook.add_memo(self.conn, self.act.id, "2026-08-17", "want", "a")
        pocketbook.add_memo(self.conn, self.act.id, "2026-08-18", "did", "c")

        all_rows = pocketbook.list_memos(self.conn, self.act.id)
        self.assertEqual([m.date for m in all_rows],
                         ["2026-08-17", "2026-08-18", "2026-08-19"])
        wants = pocketbook.list_memos(self.conn, self.act.id, kind="want")
        self.assertEqual([m.text for m in wants], ["a"])

    def test_undigested_want_memos_are_derived_not_stored(self):
        """⭐ 未消化 = その日付より後の did が無い、の導出 (§13.6)。"""
        w1 = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-15", "want", "続きを書きたい")
        w2 = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-18", "want", "推敲したい")

        # did がまだ無い → 両方未消化。
        got = pocketbook.list_undigested_want_memos(self.conn, self.act.id)
        self.assertEqual([m.id for m in got], [w1.id, w2.id])

        # 8/16 の did は 8/15 の want だけを消化する。8/18 の同日 did は
        # 「その日付より後」ではないので消化しない。
        pocketbook.add_memo(self.conn, self.act.id, "2026-08-16", "did", "書いた")
        pocketbook.add_memo(self.conn, self.act.id, "2026-08-18", "did", "同日")
        got = pocketbook.list_undigested_want_memos(self.conn, self.act.id)
        self.assertEqual([m.id for m in got], [w2.id])

        # 別アクティビティの did は消化に数えない。
        other = pocketbook.add_activity(self.conn, "絵の練習", "user")
        pocketbook.add_memo(self.conn, other.id, "2026-08-19", "did", "描いた")
        got = pocketbook.list_undigested_want_memos(self.conn, self.act.id)
        self.assertEqual([m.id for m in got], [w2.id])

        # activity_id 省略時は全アクティビティ横断。
        w3 = pocketbook.add_memo(self.conn, other.id, "2026-08-19", "want", "水彩")
        got_all = pocketbook.list_undigested_want_memos(self.conn)
        self.assertEqual([m.id for m in got_all], [w2.id, w3.id])

    def test_last_memo_date_for_dormancy_derivation(self):
        self.assertIsNone(pocketbook.get_last_memo_date(self.conn, self.act.id))
        pocketbook.add_memo(self.conn, self.act.id, "2026-08-15", "want", "a")
        pocketbook.add_memo(self.conn, self.act.id, "2026-08-18", "did", "b")
        self.assertEqual(
            pocketbook.get_last_memo_date(self.conn, self.act.id), "2026-08-18")


class TestRetryIdempotency(PocketbookTestBase):
    """スルースの丸ごと再試行で手帳が二重記帳にならない (idem_key / get-or-create)。"""

    def setUp(self):
        super().setUp()
        self.act = pocketbook.add_activity(self.conn, "小説を書く", "sluice")

    def _memo_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0]

    def test_add_memo_same_idem_key_returns_same_row(self):
        """⭐ 同じ idem_key で二度 add しても行は増えず同じ id (既存が勝つ)。"""
        m1 = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-19", "did", "冒頭を書いた",
            idem_key="sluice-run-1:op-1",
        )
        m2 = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-19", "did", "冒頭を書いた (再試行)",
            idem_key="sluice-run-1:op-1",
        )
        self.assertEqual(m2.id, m1.id)
        self.assertEqual(m2.text, m1.text)  # 既存が勝つ
        self.assertEqual(self._memo_count(), 1)

    def test_add_memo_without_idem_key_is_new_each_time(self):
        """キー無しは従来どおり毎回新規 (NULL は UNIQUE にかからない)。"""
        m1 = pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "did", "x")
        m2 = pocketbook.add_memo(self.conn, self.act.id, "2026-08-19", "did", "x")
        self.assertNotEqual(m1.id, m2.id)
        self.assertEqual(self._memo_count(), 2)

    def test_get_or_create_activity_reuses_open_same_name(self):
        """⭐ open な同名は再利用 — 再試行で同じ new_activity_name が二度来ても
        一本に収束する。名前は strip して比較する。"""
        again = pocketbook.get_or_create_activity(self.conn, "小説を書く", "sluice")
        self.assertEqual(again.id, self.act.id)
        stripped = pocketbook.get_or_create_activity(
            self.conn, "  小説を書く  ", "sluice")
        self.assertEqual(stripped.id, self.act.id)
        count = self.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        self.assertEqual(count, 1)

    def test_get_or_create_activity_does_not_revive_closed(self):
        """closed の同名は再利用しない — 明示的に閉じたものを機械が蘇らせない。"""
        pocketbook.close_activity(self.conn, self.act.id)
        fresh = pocketbook.get_or_create_activity(self.conn, "小説を書く", "sluice")
        self.assertNotEqual(fresh.id, self.act.id)
        self.assertEqual(fresh.status, "open")
        self.assertEqual(
            pocketbook.get_activity(self.conn, self.act.id).status, "closed")

    def test_get_or_create_activity_validates_origin_even_when_reusing(self):
        with self.assertRaises(ValueError):
            pocketbook.get_or_create_activity(self.conn, "小説を書く", "llm_invented")

    def test_commit_false_lets_caller_bundle_and_rollback(self):
        """⭐ commit=False の一連の操作は呼び手のトランザクション — rollback で
        丸ごと消える (書かれていない)。"""
        act = pocketbook.add_activity(
            self.conn, "絵の練習", "sluice", commit=False)
        pocketbook.add_memo(
            self.conn, act.id, "2026-08-19", "did", "描いた", commit=False)
        pocketbook.close_activity(self.conn, act.id, commit=False)
        self.conn.rollback()

        self.assertIsNone(pocketbook.get_activity(self.conn, act.id))
        self.assertEqual(self._memo_count(), 0)
        # 束ねて最後に commit すれば全部残る。
        act2 = pocketbook.add_activity(
            self.conn, "絵の練習", "sluice", commit=False)
        pocketbook.add_memo(
            self.conn, act2.id, "2026-08-19", "did", "描いた", commit=False)
        self.conn.commit()
        self.assertIsNotNone(pocketbook.get_activity(self.conn, act2.id))
        self.assertEqual(self._memo_count(), 1)


class TestMemosIdemKeyBackfill(unittest.TestCase):
    """⭐ 旧スキーマ (idem_key の無い memos) の既存 DB に列と UNIQUE インデックスを
    後付けし、後付け後に idem_key の get-or-create が効く。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "memory.db")

    def tearDown(self):
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def test_idem_key_column_is_added_to_old_schema(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    born_at INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    closed_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE memos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    span_start_id TEXT,
                    span_end_id TEXT,
                    FOREIGN KEY (activity_id) REFERENCES activities(id)
                )
                """
            )
            conn.execute(
                "INSERT INTO activities(name, status, born_at, origin) "
                "VALUES ('小説を書く', 'open', 100, 'sluice')"
            )
            conn.execute(
                "INSERT INTO memos(activity_id, date, kind, text) "
                "VALUES (1, '2026-08-18', 'did', '旧スキーマ時代のメモ')"
            )
            conn.commit()
        finally:
            conn.close()

        conn = init_db(self.db_path)
        self.addCleanup(conn.close)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memos)")}
        self.assertIn("idem_key", cols)
        idx_names = {r[1] for r in conn.execute("PRAGMA index_list(memos)")}
        self.assertIn("idx_memos_idem", idx_names)
        # 旧行は idem_key NULL のまま生きている。
        old = pocketbook.list_memos(conn, 1)
        self.assertEqual([m.text for m in old], ["旧スキーマ時代のメモ"])
        # 後付け後の DB で idem_key の get-or-create が効く。
        m1 = pocketbook.add_memo(
            conn, 1, "2026-08-19", "did", "新しいメモ", idem_key="run-1:op-1")
        m2 = pocketbook.add_memo(
            conn, 1, "2026-08-19", "did", "新しいメモ (再試行)",
            idem_key="run-1:op-1")
        self.assertEqual(m2.id, m1.id)


class TestThreadEdges(PocketbookTestBase):
    def test_add_and_traverse_both_directions(self):
        e1 = continuity.add_thread_edge(
            self.conn, "child-1", "parent-1", "fact",
            anchor_message_id="msg-x", origin="branch",
            created_at=100, meta={"note": "分岐"},
        )
        e2 = continuity.add_thread_edge(
            self.conn, "child-1", "parent-2", "digest", created_at=200)

        parents = continuity.get_parent_edges(self.conn, "child-1")
        self.assertEqual([e.edge_id for e in parents], [e1.edge_id, e2.edge_id])
        self.assertEqual(parents[0].anchor_message_id, "msg-x")
        self.assertEqual(parents[0].origin, "branch")
        self.assertEqual(parents[0].meta, {"note": "分岐"})
        # anchor NULL = 親スレッド丸ごと継承。
        self.assertIsNone(parents[1].anchor_message_id)

        children = continuity.get_child_edges(self.conn, "parent-1")
        self.assertEqual([e.edge_id for e in children], [e1.edge_id])
        self.assertEqual(continuity.get_child_edges(self.conn, "no-such"), [])

    def test_duplicate_edge_is_idempotent_and_returns_the_existing_row(self):
        """⭐ (child, parent, layer) の UNIQUE で記帳は冪等 (既存返し)。"""
        e1 = continuity.add_thread_edge(
            self.conn, "c", "p", "fact", anchor_message_id="msg-1")
        e2 = continuity.add_thread_edge(
            self.conn, "c", "p", "fact", anchor_message_id="msg-other")

        self.assertEqual(e2.edge_id, e1.edge_id)
        self.assertEqual(e2.anchor_message_id, "msg-1")  # 既存が勝つ
        count = self.conn.execute(
            "SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 1)

        # layer が違えば別のエッジ。
        e3 = continuity.add_thread_edge(self.conn, "c", "p", "digest")
        self.assertNotEqual(e3.edge_id, e1.edge_id)

    def test_layer_is_a_closed_vocabulary(self):
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "c", "p", "rumor")

    # ---- DAG 保証: 空 ID・自己辺・循環の拒否 ----

    def test_empty_thread_ids_are_rejected(self):
        for child, parent in (("", "p"), ("c", ""), (None, "p"), ("c", None),
                              ("   ", "p"), ("c", "   ")):
            with self.assertRaises(ValueError):
                continuity.add_thread_edge(self.conn, child, parent, "fact")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 0)

    def test_self_edge_is_rejected(self):
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "t1", "t1", "fact")

    def test_non_string_thread_ids_are_rejected(self):
        """⭐ ID は文字列限定 — int の 1 と str の '1' が TEXT affinity で同じ
        '1' に着地し、自己辺判定 (1 == '1' は False) だけをすり抜けて自己辺が
        永続する口を塞ぐ。child 側・parent 側それぞれ ValueError。"""
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, 1, "p", "fact")
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "c", 1, "fact")
        # 危険な組そのもの: int の 1 と str の '1' は保存されない。
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, 1, "1", "fact")
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "1", 1, "fact")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 0)

    def test_non_string_anchor_and_origin_are_rejected(self):
        """anchor_message_id / origin も None でなければ文字列限定 (同族の口)。"""
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(
                self.conn, "c", "p", "fact", anchor_message_id=42)
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "c", "p", "fact", origin=42)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 0)

    def test_query_helpers_reject_non_string_ids(self):
        """問い合わせ側も同族 — int を渡すと TEXT affinity で一致せず
        「エッジ無し」の嘘が返るため ValueError で拒否。"""
        continuity.add_thread_edge(self.conn, "1", "2", "fact")
        with self.assertRaises(ValueError):
            continuity.get_parent_edges(self.conn, 1)
        with self.assertRaises(ValueError):
            continuity.get_child_edges(self.conn, 2)

    def test_created_at_must_be_int_epoch(self):
        """created_at も epoch の同族 — 文字列・float・bool は ValueError。"""
        for bad in ("123", 1.5, True):
            with self.assertRaises(ValueError):
                continuity.add_thread_edge(
                    self.conn, "c", "p", "fact", created_at=bad)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 0)

    def test_meta_must_be_dict_with_str_keys(self):
        """meta は dict | None、キーは str 限定 — 非 dict は読み手
        (_row_to_edge) が黙って捨てて「書けたのに読めない」行になる。"""
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "c", "p", "fact", meta="note")
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "c", "p", "fact", meta=[1, 2])
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "c", "p", "fact", meta={1: "v"})
        count = self.conn.execute(
            "SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 0)

    def test_two_cycle_is_rejected(self):
        """A が B の続き、と記帳した後の「B が A の続き」は循環 — 拒否。"""
        continuity.add_thread_edge(self.conn, "A", "B", "fact")
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "B", "A", "fact")
        # layer が違っても循環は循環 (検査は layer を区別しない)。
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "B", "A", "digest")

    def test_three_cycle_is_rejected(self):
        """B は A の続き、C は B の続き、の後の「A は C の続き」は循環 — 拒否。"""
        continuity.add_thread_edge(self.conn, "B", "A", "fact")
        continuity.add_thread_edge(self.conn, "C", "B", "digest")
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "A", "C", "fact")
        # 循環にならない向き (C は A の続き = 祖先への近道) は正当な DAG。
        edge = continuity.add_thread_edge(self.conn, "C", "A", "fact")
        self.assertEqual(edge.child_thread_id, "C")

    def test_deep_cycle_beyond_1000_is_rejected(self):
        """⭐ 深さ上限の撤廃 (fail-closed) — 1001 段の鎖 N1001→…→N0 の果ての
        祖先も循環検査に見え、N0→N1001 の辺 (循環) は拒否される。旧実装の
        深さ 1000 打ち切りでは N0 が見えず素通しだった (fail-open)。"""
        self.conn.execute("BEGIN")
        for i in range(1001):
            continuity.add_thread_edge(self.conn, f"N{i + 1}", f"N{i}", "fact")
        self.conn.commit()

        with self.assertRaises(ValueError):
            continuity.add_thread_edge(self.conn, "N0", "N1001", "fact")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 1001)

    # ---- 検査と挿入の原子化 (BEGIN IMMEDIATE) ----

    def _second_conn(self) -> sqlite3.Connection:
        conn2 = sqlite3.connect(self.db_path)
        self.addCleanup(conn2.close)
        return conn2

    def test_cross_connection_cycle_is_rejected(self):
        """⭐ 二接続で A→B 挿入後、別接続の B→A は循環として拒否される。
        拒否後にトランザクションを開きっぱなしにしない (rollback 済み)。"""
        continuity.add_thread_edge(self.conn, "A", "B", "fact")
        conn2 = self._second_conn()
        with self.assertRaises(ValueError):
            continuity.add_thread_edge(conn2, "B", "A", "fact")
        self.assertFalse(conn2.in_transaction)
        count = conn2.execute("SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 1)

    def test_cross_connection_duplicate_edge_is_idempotent(self):
        """並行の同一辺: 別接続が先に入れた辺への再挿入は既存返し (冪等)。"""
        e1 = continuity.add_thread_edge(
            self.conn, "c", "p", "fact", anchor_message_id="msg-1")
        conn2 = self._second_conn()
        e2 = continuity.add_thread_edge(conn2, "c", "p", "fact")
        self.assertEqual(e2.edge_id, e1.edge_id)
        self.assertEqual(e2.anchor_message_id, "msg-1")  # 既存が勝つ
        self.assertFalse(conn2.in_transaction)
        count = conn2.execute("SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 1)

    def test_unique_violation_falls_back_to_existing_row(self):
        """既存チェックの直後に並行挿入が滑り込んだ体 (チェックを一度だけ
        空振りさせる) — INSERT の UNIQUE 違反は rollback + 既存返しに倒れる。"""
        e1 = continuity.add_thread_edge(
            self.conn, "c", "p", "fact", anchor_message_id="msg-1")

        real = self._second_conn()
        existing_check_sql = (
            "WHERE child_thread_id = ? AND parent_thread_id = ? AND layer = ?"
        )

        class _EmptyCursor:
            def fetchone(self):
                return None

        class _RacyConn:
            """existing-check の SELECT を一度だけ空振りさせる委譲ラッパ。"""

            def __init__(self, inner):
                self._inner = inner
                self.missed = False

            def execute(self, sql, params=()):
                if not self.missed and existing_check_sql in sql:
                    self.missed = True
                    return _EmptyCursor()
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        racy = _RacyConn(real)
        e2 = continuity.add_thread_edge(racy, "c", "p", "fact")
        self.assertTrue(racy.missed)  # 空振りが実際に起きた (体の検算)
        self.assertEqual(e2.edge_id, e1.edge_id)
        self.assertFalse(real.in_transaction)  # rollback 済み
        count = real.execute("SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 1)

    def test_caller_transaction_is_respected(self):
        """呼び手が既にトランザクション中なら BEGIN/commit を発行せず委ねる —
        呼び手の rollback で辺も消える。"""
        self.conn.execute("BEGIN IMMEDIATE")
        continuity.add_thread_edge(self.conn, "c", "p", "fact")
        self.assertTrue(self.conn.in_transaction)  # commit されていない
        self.conn.rollback()
        count = self.conn.execute("SELECT COUNT(*) FROM thread_edges").fetchone()[0]
        self.assertEqual(count, 0)


class TestPocketbookConcurrency(PocketbookTestBase):
    """手帳の並行系: idem 衝突後のトランザクション状態と get-or-create の原子化。"""

    def setUp(self):
        super().setUp()
        self.act = pocketbook.add_activity(self.conn, "小説を書く", "sluice")

    def _second_conn(self, timeout: float = 5.0) -> sqlite3.Connection:
        conn2 = sqlite3.connect(self.db_path, timeout=timeout)
        self.addCleanup(conn2.close)
        return conn2

    def test_add_memo_idem_collision_leaves_no_open_transaction(self):
        """⭐ 並行 idem 衝突 (UNIQUE) の既存返し後、トランザクションは閉じている
        (rollback 済み) — 直後に別接続の書き込みが成功する。"""
        m1 = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-19", "did", "先勝ち",
            idem_key="run-1:op-1")

        real = self._second_conn()
        idem_check_sql = "FROM memos WHERE idem_key = ?"

        class _EmptyCursor:
            def fetchone(self):
                return None

        class _RacyConn:
            """idem_key の事前チェック SELECT を一度だけ空振りさせる委譲ラッパ —
            事前チェックと INSERT の間に並行の add が滑り込んだ体。"""

            def __init__(self, inner):
                self._inner = inner
                self.missed = False

            def execute(self, sql, params=()):
                if not self.missed and idem_check_sql in sql:
                    self.missed = True
                    return _EmptyCursor()
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        racy = _RacyConn(real)
        m2 = pocketbook.add_memo(
            racy, self.act.id, "2026-08-19", "did", "後着 (再試行)",
            idem_key="run-1:op-1")
        self.assertTrue(racy.missed)  # 空振りが実際に起きた (体の検算)
        self.assertEqual(m2.id, m1.id)
        self.assertEqual(m2.text, "先勝ち")  # 既存が勝つ
        self.assertFalse(real.in_transaction)  # rollback 済み

        # 開きっぱなしなら別接続の書き込みが locked で失敗する — 成功が証拠。
        writer = sqlite3.connect(self.db_path, timeout=0.5)
        self.addCleanup(writer.close)
        writer.execute(
            "INSERT INTO memos(activity_id, date, kind, text) "
            "VALUES (?, '2026-08-19', 'did', '別接続の書き込み')",
            (self.act.id,))
        writer.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
        self.assertEqual(count, 2)

    def test_add_memo_idem_hit_with_commit_true_commits_the_transaction(self):
        """⭐ 冪等ヒットの早期 return も commit=True の契約どおりトランザクション
        を確定する — 同接続の未確定分 (commit=False の add_activity) も一緒に
        確定され、return 後に書き込みロックが残らない。"""
        m1 = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-19", "did", "一度目",
            idem_key="run-1:op-1")

        # 同接続に未確定の書き込みを置いてから、冪等ヒットを commit=True で呼ぶ。
        pending = pocketbook.add_activity(
            self.conn, "未確定の活動", "sluice", commit=False)
        self.assertTrue(self.conn.in_transaction)
        m2 = pocketbook.add_memo(
            self.conn, self.act.id, "2026-08-19", "did", "二度目 (再試行)",
            idem_key="run-1:op-1", commit=True)
        self.assertEqual(m2.id, m1.id)
        self.assertFalse(self.conn.in_transaction)  # 確定済み — ロックが残らない

        # 別接続 (timeout 短め) の書き込みが成功する — ロック残りなしの証拠。
        writer = sqlite3.connect(self.db_path, timeout=0.5)
        self.addCleanup(writer.close)
        writer.execute(
            "INSERT INTO memos(activity_id, date, kind, text) "
            "VALUES (?, '2026-08-19', 'did', '別接続の書き込み')",
            (self.act.id,))
        writer.commit()

        # 未確定分は rollback ではなく確定された (契約: 呼び手の分も一緒に確定)。
        self.assertIsNotNone(pocketbook.get_activity(self.conn, pending.id))

    def test_get_or_create_activity_two_connection_interleave_converges(self):
        """⭐ 二接続のインターリーブで open な同名が二本にならない —
        SELECT 空振りの窓は BEGIN IMMEDIATE の予約ロック内なので、相手接続の
        同名 get-or-create は locked になり、収束後の再試行は既存返し。"""
        conn2 = self._second_conn(timeout=0.1)
        outcome = {}
        open_check_sql = "WHERE name = ? AND status = 'open'"

        class _InterleavedConn:
            """open 確認の SELECT の実行点 (BEGIN IMMEDIATE 直後) で相手接続を
            割り込ませる委譲ラッパ。"""

            def __init__(self, inner):
                self._inner = inner
                self.fired = False

            def execute(self, sql, params=()):
                if not self.fired and open_check_sql in sql:
                    self.fired = True
                    try:
                        pocketbook.get_or_create_activity(
                            conn2, "競合する名前", "sluice")
                        outcome["result"] = "created"
                    except sqlite3.OperationalError:
                        outcome["result"] = "locked"
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        wrapped = _InterleavedConn(self.conn)
        first = pocketbook.get_or_create_activity(wrapped, "競合する名前", "sluice")
        self.assertTrue(wrapped.fired)  # 割り込みが実際に起きた (体の検算)
        self.assertEqual(outcome.get("result"), "locked")
        self.assertFalse(self.conn.in_transaction)  # commit 済み

        # 相手の再試行は既存の一本に収束する。
        retry = pocketbook.get_or_create_activity(conn2, "競合する名前", "sluice")
        self.assertEqual(retry.id, first.id)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM activities WHERE name = ? AND status = 'open'",
            ("競合する名前",)).fetchone()[0]
        self.assertEqual(count, 1)


    def test_open_same_name_unique_is_a_db_boundary(self):
        """⭐ open 同名一意は DB 境界 (部分 UNIQUE idx_activities_open_name) が
        持つ — アプリ層を通らない生 SQL の二本目も弾かれる。closed の同名は
        歴史として合法。"""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO activities(name, status, born_at, origin) "
                "VALUES ('小説を書く', 'open', 100, 'sluice')"
            )
        self.conn.rollback()
        # closed の同名は入る (歴史として合法 — 部分 UNIQUE は open だけに効く)。
        self.conn.execute(
            "INSERT INTO activities(name, status, born_at, origin, closed_at) "
            "VALUES ('小説を書く', 'closed', 100, 'sluice', 200)"
        )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM activities WHERE name = '小説を書く'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_commit_false_interleave_converges_to_one_open_row(self):
        """⭐ commit=False (BEGIN IMMEDIATE が発行されない並び) の二接続交差でも
        open 同名は二本にならない — 確認の空振り後の INSERT が部分 UNIQUE に
        当たり、既存の open 行へ収束する (DB 境界の最終保証)。"""
        conn2 = self._second_conn()
        first = pocketbook.get_or_create_activity(conn2, "競合する名前", "sluice")

        open_check_sql = "WHERE name = ? AND status = 'open'"

        class _EmptyCursor:
            def fetchone(self):
                return None

        class _RacyConn:
            """open 確認の SELECT を一度だけ空振りさせる委譲ラッパ — 確認と
            INSERT の間に相手接続の同名追加が滑り込んだ体。"""

            def __init__(self, inner):
                self._inner = inner
                self.missed = False

            def execute(self, sql, params=()):
                if not self.missed and open_check_sql in sql:
                    self.missed = True
                    return _EmptyCursor()
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        racy = _RacyConn(self.conn)
        got = pocketbook.get_or_create_activity(
            racy, "競合する名前", "sluice", commit=False)
        self.assertTrue(racy.missed)  # 空振りが実際に起きた (体の検算)
        self.assertEqual(got.id, first.id)  # 既存の一本へ収束
        self.conn.commit()  # commit=False の束は呼び手が閉じる
        count = self.conn.execute(
            "SELECT COUNT(*) FROM activities WHERE name = ? AND status = 'open'",
            ("競合する名前",)).fetchone()[0]
        self.assertEqual(count, 1)


class TestChunkPageEdges(PocketbookTestBase):
    def test_add_is_idempotent(self):
        self.assertTrue(recall_edges.add_chunk_page_edge(
            self.conn, "chron-1", "page-a"))
        self.assertFalse(recall_edges.add_chunk_page_edge(
            self.conn, "chron-1", "page-a"))
        count = self.conn.execute(
            "SELECT COUNT(*) FROM chunk_page_edges").fetchone()[0]
        self.assertEqual(count, 1)

    def test_both_directions(self):
        recall_edges.add_chunk_page_edge(self.conn, "chron-1", "page-a", created_at=1)
        recall_edges.add_chunk_page_edge(self.conn, "chron-1", "page-b", created_at=2)
        recall_edges.add_chunk_page_edge(self.conn, "chron-2", "page-a", created_at=3)

        self.assertEqual(
            recall_edges.list_entity_pages_for_chronicle(self.conn, "chron-1"),
            ["page-a", "page-b"],
        )
        self.assertEqual(
            recall_edges.list_chronicle_pages_for_entity(self.conn, "page-a"),
            ["chron-1", "chron-2"],
        )
        self.assertEqual(
            recall_edges.list_entity_pages_for_chronicle(self.conn, "no-such"), [])

    def test_empty_page_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            recall_edges.add_chunk_page_edge(self.conn, "", "page-a")
        with self.assertRaises(ValueError):
            recall_edges.add_chunk_page_edge(self.conn, "chron-1", "")
        with self.assertRaises(ValueError):
            recall_edges.add_chunk_page_edge(self.conn, "   ", "page-a")

    def test_non_string_page_ids_are_rejected(self):
        """ID は文字列限定 (continuity と同族の口) — 非文字列は TEXT affinity の
        変換で '123' の顔に着地して照合できない辺が永続する。記帳・列挙とも
        入口で拒否。"""
        with self.assertRaises(ValueError):
            recall_edges.add_chunk_page_edge(self.conn, 123, "page-a")
        with self.assertRaises(ValueError):
            recall_edges.add_chunk_page_edge(self.conn, "chron-1", 123)
        with self.assertRaises(ValueError):
            recall_edges.list_entity_pages_for_chronicle(self.conn, 123)
        with self.assertRaises(ValueError):
            recall_edges.list_chronicle_pages_for_entity(self.conn, 123)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM chunk_page_edges").fetchone()[0]
        self.assertEqual(count, 0)

    def test_created_at_must_be_int_epoch(self):
        """created_at も epoch の同族 — 文字列・float・bool は ValueError。"""
        for bad in ("123", 1.5, True):
            with self.assertRaises(ValueError):
                recall_edges.add_chunk_page_edge(
                    self.conn, "chron-1", "page-a", created_at=bad)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM chunk_page_edges").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
