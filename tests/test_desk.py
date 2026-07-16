"""机 (desk) ストア sai_memory/desk.py のテスト (concept_consolidation.md「開閉制御」)。

検証項目:
- init の冪等性 / open・close・touch の基本動作
- 再 open は touch 扱い (opened_at は初回のまま、last_touched_at のみ更新)
- list_open は opened_at 昇順
- evict_lru: 予算境界 (超えない間は追い出さない) / LRU 順 (最古 touch から) /
  touch で追い出し順が動くこと
- created_at (opened_at/last_touched_at) が saiverse.clock 経由 (仮想クロック尊重)
- env SAIVERSE_DESK_BUDGET_CHARS による既定予算の上書き
"""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sai_memory import desk as D
from saiverse import clock


class DeskTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        D.init_desk_tables(self.conn)

    def tearDown(self):
        clock.disable_virtual()
        os.environ.pop("SAIVERSE_DESK_BUDGET_CHARS", None)
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_init_is_idempotent(self):
        D.init_desk_tables(self.conn)  # 2 回目も例外なし
        D.open_item(self.conn, "m:1")
        D.init_desk_tables(self.conn)  # 既存データを壊さない
        self.assertEqual(len(D.list_open(self.conn)), 1)

    def test_open_creates_item(self):
        item = D.open_item(self.conn, "m:1", purpose_ref="task:2")
        self.assertEqual(item.ref, "m:1")
        self.assertEqual(item.purpose_ref, "task:2")
        self.assertEqual(item.opened_at, item.last_touched_at)
        got = D.list_open(self.conn)
        self.assertEqual([i.ref for i in got], ["m:1"])

    def test_reopen_is_touch_not_new_entry(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        first = D.open_item(self.conn, "m:1")
        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        second = D.open_item(self.conn, "m:1")

        # opened_at は初回のまま、last_touched_at だけ動く
        self.assertEqual(second.opened_at, first.opened_at)
        self.assertGreater(second.last_touched_at, first.last_touched_at)
        # 二重登録されない
        self.assertEqual(len(D.list_open(self.conn)), 1)

    def test_reopen_updates_purpose_ref_when_given(self):
        D.open_item(self.conn, "m:1", purpose_ref="task:1")
        updated = D.open_item(self.conn, "m:1", purpose_ref="task:9")
        self.assertEqual(updated.purpose_ref, "task:9")

    def test_reopen_without_purpose_ref_keeps_existing(self):
        D.open_item(self.conn, "m:1", purpose_ref="task:1")
        updated = D.open_item(self.conn, "m:1")  # purpose_ref 省略
        self.assertEqual(updated.purpose_ref, "task:1")

    def test_close_removes_item(self):
        D.open_item(self.conn, "m:1")
        self.assertTrue(D.close_item(self.conn, "m:1"))
        self.assertEqual(D.list_open(self.conn), [])

    def test_close_unknown_ref_returns_false(self):
        self.assertFalse(D.close_item(self.conn, "m:999"))

    def test_touch_item_updates_only_open_items(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        D.open_item(self.conn, "m:1")
        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        touched = D.touch_item(self.conn, "m:1")
        self.assertEqual(touched.last_touched_at, int(clock.now().timestamp()))

        # 開いていない ref を touch しても何も起きない (勝手に開かない)
        self.assertIsNone(D.touch_item(self.conn, "m:2"))
        self.assertEqual(D.list_open(self.conn), [touched])

    def test_list_open_ordered_by_opened_at(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        first = D.open_item(self.conn, "m:1")
        clock.advance_to(datetime(2026, 7, 6, 9, 30, 0))
        second = D.open_item(self.conn, "ch:2")
        got = D.list_open(self.conn)
        self.assertEqual([i.ref for i in got], [first.ref, second.ref])

    def test_created_at_follows_virtual_clock(self):
        t = datetime(2026, 7, 6, 14, 0, 0)
        clock.enable_virtual(t)
        item = D.open_item(self.conn, "m:1")
        self.assertEqual(item.opened_at, int(t.timestamp()))
        self.assertEqual(item.last_touched_at, int(t.timestamp()))

    def test_evict_lru_no_eviction_within_budget(self):
        D.open_item(self.conn, "m:1")
        D.open_item(self.conn, "m:2")
        sizes = {"m:1": 100, "m:2": 100}
        evicted = D.evict_lru(self.conn, budget_chars=200, size_resolver=lambda r: sizes[r])
        self.assertEqual(evicted, [])
        self.assertEqual(len(D.list_open(self.conn)), 2)

    def test_evict_lru_evicts_oldest_touched_first(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        D.open_item(self.conn, "m:1")  # oldest touch
        clock.advance_to(datetime(2026, 7, 6, 9, 10, 0))
        D.open_item(self.conn, "m:2")
        clock.advance_to(datetime(2026, 7, 6, 9, 20, 0))
        D.open_item(self.conn, "m:3")  # newest touch

        sizes = {"m:1": 100, "m:2": 100, "m:3": 100}
        # 合計 300、予算 250: 最古 (m:1, 100字) を1件閉じれば 200 <= 250 で停止
        evicted = D.evict_lru(self.conn, budget_chars=250, size_resolver=lambda r: sizes[r])
        self.assertEqual(evicted, ["m:1"])
        remaining = {i.ref for i in D.list_open(self.conn)}
        self.assertEqual(remaining, {"m:2", "m:3"})

    def test_evict_lru_touch_changes_eviction_order(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        D.open_item(self.conn, "m:1")
        clock.advance_to(datetime(2026, 7, 6, 9, 10, 0))
        D.open_item(self.conn, "m:2")
        clock.advance_to(datetime(2026, 7, 6, 9, 20, 0))
        # m:1 を touch して LRU の最後尾に送る (m:2 が最古になる)
        D.touch_item(self.conn, "m:1")

        sizes = {"m:1": 100, "m:2": 100}
        evicted = D.evict_lru(self.conn, budget_chars=100, size_resolver=lambda r: sizes[r])
        self.assertEqual(evicted, ["m:2"])

    def test_evict_lru_keep_ref_skips_oldest_and_evicts_others(self):
        # keep_ref (いま open した本人) が LRU 最古でも追い出されず、
        # 次に古いものが代わりに追い出される (メイン修正の回帰テスト)
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        D.open_item(self.conn, "m:1")  # 最古 touch = 本来の LRU 先頭
        clock.advance_to(datetime(2026, 7, 6, 9, 10, 0))
        D.open_item(self.conn, "m:2")
        clock.advance_to(datetime(2026, 7, 6, 9, 20, 0))
        D.open_item(self.conn, "m:3")

        sizes = {"m:1": 100, "m:2": 100, "m:3": 100}
        # 合計 300、予算 200: keep_ref=m:1 は skip され、m:2 が閉じて 200 <= 200 で停止
        evicted = D.evict_lru(
            self.conn, budget_chars=200,
            size_resolver=lambda r: sizes[r], keep_ref="m:1",
        )
        self.assertEqual(evicted, ["m:2"])
        remaining = {i.ref for i in D.list_open(self.conn)}
        self.assertEqual(remaining, {"m:1", "m:3"})

    def test_evict_lru_keep_ref_survives_alone_over_budget(self):
        # keep_ref 単独で予算超過でも残る (予算より大きいページを開いた直後、
        # 「開きました」と「棚に戻しました」の同居矛盾を作らない)
        D.open_item(self.conn, "m:1")
        evicted = D.evict_lru(
            self.conn, budget_chars=50,
            size_resolver=lambda r: 500, keep_ref="m:1",
        )
        self.assertEqual(evicted, [])
        self.assertEqual([i.ref for i in D.list_open(self.conn)], ["m:1"])

    def test_evict_lru_evicts_all_when_budget_zero(self):
        D.open_item(self.conn, "m:1")
        D.open_item(self.conn, "m:2")
        evicted = D.evict_lru(self.conn, budget_chars=0, size_resolver=lambda r: 10)
        self.assertEqual(set(evicted), {"m:1", "m:2"})
        self.assertEqual(D.list_open(self.conn), [])

    def test_resolve_desk_budget_chars_default(self):
        self.assertEqual(D.resolve_desk_budget_chars(), D.DEFAULT_DESK_BUDGET_CHARS)

    def test_resolve_desk_budget_chars_env_override(self):
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "1234"
        self.assertEqual(D.resolve_desk_budget_chars(), 1234)

    def test_resolve_desk_budget_chars_invalid_env_falls_back(self):
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "not-a-number"
        self.assertEqual(D.resolve_desk_budget_chars(), D.DEFAULT_DESK_BUDGET_CHARS)


class LegacyRefBackfillTest(unittest.TestCase):
    """旧 prefix の ref を正典形へ寄せる移行 (2026-07-15 統一グラマー吸収)。

    机の ref は主キー。旧表記を残すと同じページが二枚の紙として並ぶ。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        D.init_desk_tables(self.conn)

    def tearDown(self):
        self.conn.close()
        gc.collect()
        self._tmpdir.cleanup()

    def _insert(self, ref, opened_at, touched_at):
        self.conn.execute(
            "INSERT INTO desk_items (ref, opened_at, last_touched_at, purpose_ref) "
            "VALUES (?, ?, ?, NULL)",
            (ref, opened_at, touched_at),
        )
        self.conn.commit()

    def _refs(self):
        return {r[0] for r in self.conn.execute("SELECT ref FROM desk_items")}

    def test_legacy_refs_become_canonical(self):
        self._insert("m:2", 100, 200)
        self._insert("ch:7", 100, 200)
        D._backfill_canonical_refs(self.conn)
        self.assertEqual(self._refs(), {"memopedia:2", "chronicle:7"})

    def test_canonical_refs_are_left_alone(self):
        self._insert("memopedia:2", 100, 200)
        self.assertEqual(D._backfill_canonical_refs(self.conn), 0)
        self.assertEqual(self._refs(), {"memopedia:2"})

    def test_collision_merges_into_one_sheet_keeping_desk_semantics(self):
        # 同じページを新旧表記で開いていた場合、一枚に畳む。
        # opened_at = 早い方 (いつから開いているか) / last_touched_at = 遅い方
        self._insert("m:2", 100, 500)
        self._insert("memopedia:2", 300, 900)
        D._backfill_canonical_refs(self.conn)

        rows = self.conn.execute(
            "SELECT ref, opened_at, last_touched_at FROM desk_items"
        ).fetchall()
        self.assertEqual(rows, [("memopedia:2", 100, 900)])

    def test_unparseable_ref_is_left_untouched(self):
        # 未知の値を移行が壊さない (正規化ループの安全性)
        self._insert("なんだこれ", 100, 200)
        D._backfill_canonical_refs(self.conn)
        self.assertEqual(self._refs(), {"なんだこれ"})

    def test_backfill_is_idempotent(self):
        self._insert("m:2", 100, 200)
        self.assertEqual(D._backfill_canonical_refs(self.conn), 1)
        self.assertEqual(D._backfill_canonical_refs(self.conn), 0)
        self.assertEqual(self._refs(), {"memopedia:2"})


if __name__ == "__main__":
    unittest.main()
