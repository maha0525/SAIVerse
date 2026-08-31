"""W8 (柱6 — 時刻) / SEA 監査 S7 の回帰テスト。

秒精度 ``created_at`` だけで anchor 境界と履歴順を決めていたことによる
「同一秒衝突」の破れを固定する:

- anchor 境界: 同じ秒に anchor より前へ書かれた evicted prefix が
  ``get_messages_from_id`` で再混入しないこと (境界は正典順序キー
  ``(created_at, rowid)`` のキーセット)。
- pagination: ページ境界を同一 timestamp 群の中央に置いても重複・欠落が
  ないこと。
- 全 history / Chronicle クエリが同じ total order を共有すること。
- 過去時刻での後挿入 (インポート・移植) は rowid でなく created_at の
  歴史位置に並ぶこと (rowid はあくまで同秒内の tie-breaker)。
"""

import unittest

from sai_memory.memory.storage import (
    add_message,
    get_conversation_messages_between,
    get_conversation_window_around,
    get_messages_around,
    get_messages_for_chronicle,
    get_messages_from_id,
    get_messages_last,
    get_messages_paginated,
    init_db,
)

THREAD = "persona-1:default"
TS = 1_800_000_000


class CanonicalOrderTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _add(self, content, *, ts=TS, role="user", thread=THREAD, metadata=None):
        return add_message(
            self.conn, thread, role, content,
            created_at=ts, metadata=metadata,
        )


class AnchorBoundarySameSecondTest(CanonicalOrderTestBase):
    """anchor 境界: 同一秒群の中央に anchor を置いても正確にその行以後だけが返る。"""

    def test_anchor_mid_same_second_excludes_older_rows(self):
        """同じ秒に anchor より前へ書かれた行 (evicted prefix) は再混入しない。

        旧実装 (created_at >= anchor.created_at) では m1〜m3 が全て復活していた。
        """
        ids = [
            self._add("m1", role="user"),
            self._add("m2", role="assistant"),
            self._add("m3", role="tool"),
            self._add("m4", role="user"),      # anchor
            self._add("m5", role="assistant"),
            self._add("m6", role="tool"),
        ]
        rows = get_messages_from_id(self.conn, THREAD, ids[3])
        self.assertEqual([m.id for m in rows], ids[3:])

    def test_anchor_first_and_last_of_same_second_group(self):
        ids = [self._add(f"m{i}") for i in range(5)]
        self.assertEqual(
            [m.id for m in get_messages_from_id(self.conn, THREAD, ids[0])], ids
        )
        self.assertEqual(
            [m.id for m in get_messages_from_id(self.conn, THREAD, ids[-1])],
            [ids[-1]],
        )

    def test_anchor_across_seconds(self):
        """秒を跨ぐ通常ケース: 前秒は除外・後秒は含む・同秒後行は含む。"""
        old = self._add("old", ts=TS - 10)
        a = self._add("anchor", ts=TS)
        same = self._add("same-second-after", ts=TS)
        newer = self._add("newer", ts=TS + 10)
        rows = get_messages_from_id(self.conn, THREAD, a)
        self.assertEqual([m.id for m in rows], [a, same, newer])
        self.assertNotIn(old, [m.id for m in rows])

    def test_missing_anchor_returns_empty(self):
        self._add("m1")
        self.assertEqual(
            get_messages_from_id(self.conn, THREAD, "no-such-id"), []
        )

    def test_same_second_order_is_insertion_order(self):
        """同一秒群の並びは挿入順 (rowid) で決定的。"""
        ids = [self._add(f"m{i}") for i in range(8)]
        rows = get_messages_from_id(self.conn, THREAD, ids[0])
        self.assertEqual([m.id for m in rows], ids)


class PaginationSameSecondTest(CanonicalOrderTestBase):
    """pagination: ページ境界が同一 timestamp 群の中央でも重複・欠落しない。"""

    def test_page_boundary_in_middle_of_same_second_group(self):
        ids = [self._add(f"m{i}") for i in range(10)]
        collected = []
        page = 0
        while True:
            batch = get_messages_paginated(self.conn, THREAD, page=page, page_size=3)
            if not batch:
                break
            collected.extend(m.id for m in batch)
            page += 1
        self.assertEqual(collected, ids)          # 欠落なし・順序どおり
        self.assertEqual(len(set(collected)), 10)  # 重複なし

    def test_get_messages_last_same_second_tail(self):
        ids = [self._add(f"m{i}") for i in range(6)]
        rows = get_messages_last(self.conn, THREAD, 3)
        self.assertEqual([m.id for m in rows], ids[3:])


class BackdatedInsertTest(CanonicalOrderTestBase):
    """過去時刻の後挿入 (インポート・移植) は歴史位置に並ぶ — rowid は tie-breaker に留まる。"""

    def test_backdated_row_sorts_by_created_at(self):
        a = self._add("a", ts=TS)
        b = self._add("b", ts=TS + 100)
        c = self._add("c-backdated", ts=TS + 50)  # 挿入は最後、時刻は中間
        rows = get_messages_paginated(self.conn, THREAD, page=0, page_size=10)
        self.assertEqual([m.id for m in rows], [a, c, b])

    def test_anchor_query_respects_backdated_position(self):
        a = self._add("a", ts=TS)
        b = self._add("b", ts=TS + 100)
        c = self._add("c-backdated", ts=TS + 50)
        rows = get_messages_from_id(self.conn, THREAD, c)
        self.assertEqual([m.id for m in rows], [c, b])
        self.assertNotIn(a, [m.id for m in rows])


class ChronicleOrderConsistencyTest(CanonicalOrderTestBase):
    """Chronicle 編纂の全順序が context / pagination と同じであること。"""

    def test_chronicle_order_matches_pagination_order(self):
        for i in range(6):
            self._add(f"m{i}", role="user" if i % 2 == 0 else "assistant")
        chron = [m.id for m in get_messages_for_chronicle(self.conn)]
        paged = [
            m.id
            for m in get_messages_paginated(self.conn, THREAD, page=0, page_size=100)
        ]
        self.assertEqual(chron, paged)


class WindowQueriesSameSecondTest(CanonicalOrderTestBase):
    """周辺窓・範囲クエリも正典順 (created_at, rowid) で切れること。"""

    def test_get_messages_around_same_second(self):
        ids = [self._add(f"m{i}") for i in range(5)]
        rows = get_messages_around(self.conn, THREAD, ids[2], before=2, after=2)
        self.assertEqual([m.id for m in rows], ids[0:2] + ids[3:5])

    def test_get_messages_around_backdated_neighbor(self):
        """過去時刻で後挿入された行は「前」側の隣人になる (rowid 単独だと後側に化けていた)。"""
        a = self._add("a", ts=TS)
        b = self._add("b", ts=TS + 100)
        c = self._add("c-backdated", ts=TS + 50)
        rows = get_messages_around(self.conn, THREAD, b, before=2, after=2)
        self.assertEqual([m.id for m in rows], [a, c])

    def test_conversation_between_same_second_endpoints(self):
        ids = [
            self._add(f"m{i}", role="user" if i % 2 == 0 else "assistant")
            for i in range(6)
        ]
        rows = get_conversation_messages_between(self.conn, ids[1], ids[4])
        self.assertEqual([m.id for m in rows], ids[1:5])
        # 端の逆順渡しも正規化される
        rows = get_conversation_messages_between(self.conn, ids[4], ids[1])
        self.assertEqual([m.id for m in rows], ids[1:5])

    def test_conversation_window_around_same_second(self):
        ids = [
            self._add(f"m{i}", role="user" if i % 2 == 0 else "assistant")
            for i in range(7)
        ]
        rows = get_conversation_window_around(self.conn, ids[3], rounds=1)
        # rounds=1 → 前後 2 件ずつ + anchor
        self.assertEqual([m.id for m in rows], ids[1:6])


class NullCreatedAtTest(CanonicalOrderTestBase):
    """created_at 欠落 (NULL) 行の正典順位置 = 全ての実時刻より前 (NULL 群は rowid 順)。

    native import は created_at 欠落行を明示的に受け入れる。Codex W8 二巡目
    P2: NULL 行を anchor にした周辺検索が常に空になる退行 (旧 rowid 検索では
    取得できていた) を封鎖する。
    """

    def _add_null_ts(self, content, *, role="user"):
        import uuid as _uuid
        mid = str(_uuid.uuid4())
        self.conn.execute(
            "INSERT INTO messages(id, thread_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (mid, THREAD, role, content),
        )
        self.conn.commit()
        return mid

    def test_null_anchor_get_messages_around(self):
        n1 = self._add_null_ts("n1")
        n2 = self._add_null_ts("n2")
        a = self._add("a", ts=TS)
        rows = get_messages_around(self.conn, THREAD, n1, before=2, after=2)
        self.assertEqual([m.id for m in rows], [n2, a])
        rows = get_messages_around(self.conn, THREAD, a, before=3, after=1)
        self.assertEqual([m.id for m in rows], [n1, n2])

    def test_null_anchor_get_messages_from_id(self):
        n1 = self._add_null_ts("n1")
        n2 = self._add_null_ts("n2")
        a = self._add("a", ts=TS)
        rows = get_messages_from_id(self.conn, THREAD, n2)
        self.assertEqual([m.id for m in rows], [n2, a])
        self.assertNotIn(n1, [m.id for m in rows])

    def test_null_rows_sort_first_and_materialize(self):
        a = self._add("a", ts=TS)
        n1 = self._add_null_ts("n1")
        rows = get_messages_paginated(self.conn, THREAD, page=0, page_size=10)
        self.assertEqual([m.id for m in rows], [n1, a])
        self.assertEqual(rows[0].created_at, 0)

    def test_null_endpoint_conversation_between(self):
        n1 = self._add_null_ts("n1", role="user")
        b = self._add("b", ts=TS, role="assistant")
        c = self._add("c", ts=TS + 1, role="user")
        rows = get_conversation_messages_between(self.conn, n1, b)
        self.assertEqual([m.id for m in rows], [n1, b])
        self.assertNotIn(c, [m.id for m in rows])


if __name__ == "__main__":
    unittest.main()
