"""帯あふれ束ね (sai_memory/arasuji/bands.py) の回帰テスト (W4 D6/D7)。

experience_structure.md の圧縮原則:

- §4-6 束ねはサイズ・駆動は帯のあふれ。上位級のノードは壁 (再要約されない)。
- §4-7 最古が黙って落ちない — 束ねは常に古い端から。
- M2-a: 親 INSERT + 子 mark_consolidated は単一 tx (途中状態なし)。
"""

import os
import sqlite3
import unittest
from unittest.mock import patch

from sai_memory.arasuji.bands import backfill_coverage, run_band_overflow
from sai_memory.arasuji.storage import (
    create_entry,
    get_entries_by_level,
    get_entry,
    init_arasuji_tables,
)

# テストの級パラメータ: U=100 / B=2 → 帯1 cap=200, 帯2 cap=400
ENV = {
    "SAIVERSE_CHRONICLE_BAND_BUDGET": "100",
    "SAIVERSE_CHRONICLE_BAND_BASE": "2",
    "SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN": "10",
}


class _Client:
    def __init__(self, response="統合されたまとめ。", fail=False):
        self.calls = 0
        self.response = response
        self.fail = fail

    def generate(self, messages, tools):
        self.calls += 1
        if self.fail:
            raise RuntimeError("llm down")
        return self.response

    def consume_usage(self):
        return None


def _lv1(conn, start, end, coverage, content=None):
    return create_entry(
        conn,
        level=1,
        content=content or f"lv1 {start}-{end}",
        source_ids=[f"src-{start}"],
        start_time=start,
        end_time=end,
        source_count=1,
        message_count=1,
        extra_metadata={"digest_origin": "batch", "coverage_chars": coverage},
    )


class BandTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)
        self.addCleanup(self.conn.close)

    def _run(self, client=None):
        with patch.dict(os.environ, ENV):
            return run_band_overflow(self.conn, client or _Client())


class TestOverflowTrigger(BandTestBase):
    def test_no_overflow_no_consolidation(self):
        """帯 1 の被覆合計 <= cap (200) なら束ねない。"""
        _lv1(self.conn, 100, 199, 100)
        _lv1(self.conn, 200, 299, 100)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 0)
        self.assertEqual(client.calls, 0)

    def test_overflow_consolidates_from_oldest(self):
        """§4-6/§4-7: あふれたら**古い端から**合計が次の級 (200) に達するまで束ねる。"""
        e1 = _lv1(self.conn, 100, 199, 100)
        e2 = _lv1(self.conn, 200, 299, 100)
        e3 = _lv1(self.conn, 300, 399, 100)
        e4 = _lv1(self.conn, 400, 499, 100)  # 合計 400 > cap 200
        client = _Client()
        created = self._run(client)
        self.assertGreaterEqual(created, 1)

        lv2 = get_entries_by_level(self.conn, 2, order_by_time=True)
        self.assertGreaterEqual(len(lv2), 1)
        first_parent = lv2[0]
        # 最古の 2 個 (100+100=200) が束ねられている
        self.assertEqual(first_parent.source_ids, [e1.id, e2.id])
        self.assertEqual(first_parent.start_time, 100)
        self.assertEqual(first_parent.end_time, 299)
        # 子は is_consolidated=1 + parent_id (単一 tx の帰結)
        for child_id in (e1.id, e2.id):
            child = get_entry(self.conn, child_id)
            self.assertTrue(child.is_consolidated)
            self.assertEqual(child.parent_id, first_parent.id)
        # e3/e4 は残り 200 <= cap で 2 本目の束ねは起きない
        remaining = [
            e.id for e in get_entries_by_level(self.conn, 1, order_by_time=True)
            if not e.is_consolidated
        ]
        self.assertEqual(remaining, [e3.id, e4.id])

    def test_parent_coverage_is_sum_of_children(self):
        _lv1(self.conn, 100, 199, 120)
        _lv1(self.conn, 200, 299, 90)
        _lv1(self.conn, 300, 399, 100)  # 合計 310 > 200
        self._run()
        lv2 = get_entries_by_level(self.conn, 2, order_by_time=True)
        self.assertEqual(len(lv2), 1)
        import json
        row = self.conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (lv2[0].id,)
        ).fetchone()
        meta = json.loads(row[0])
        self.assertEqual(meta["digest_origin"], "band")
        self.assertEqual(meta["coverage_chars"], 210)  # 120+90 (最古 2 個で 200 到達)

    def test_safety_valve_limits_consolidations_per_run(self):
        for i in range(10):
            _lv1(self.conn, 100 * (i + 1), 100 * (i + 1) + 99, 100)
        with patch.dict(os.environ, {
            **ENV, "SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN": "1",
        }):
            created = run_band_overflow(self.conn, _Client())
        self.assertEqual(created, 1)


class TestWalls(BandTestBase):
    def test_node_inside_wall_is_not_bundled(self):
        """壁 (上位級ノード) の被覆内側の穴ノードは束ねに巻き込まない。"""
        # 壁: level 2 が [100, 399] を被覆
        create_entry(
            self.conn, level=2, content="旧Lv2", source_ids=[],
            start_time=100, end_time=399, source_count=2, message_count=40,
            extra_metadata={"coverage_chars": 200},
        )
        # 穴: 壁の内側に後から編纂された Lv1
        hole = _lv1(self.conn, 150, 249, 100)
        # 壁の外の Lv1 (あふれを作る)
        a = _lv1(self.conn, 400, 499, 100)
        b = _lv1(self.conn, 500, 599, 100)
        c = _lv1(self.conn, 600, 699, 100)
        self._run()
        # 穴は束ねられていない
        self.assertFalse(get_entry(self.conn, hole.id).is_consolidated)
        # 壁の外側は束ねられる (a+b で 200 到達)
        lv2 = [e for e in get_entries_by_level(self.conn, 2, order_by_time=True)
               if e.content != "旧Lv2"]
        self.assertEqual(len(lv2), 1)
        self.assertEqual(lv2[0].source_ids, [a.id, b.id])
        self.assertFalse(get_entry(self.conn, c.id).is_consolidated)

    def test_wall_content_never_rewritten(self):
        """移行の約束: 既存上位ノードの content は帯あふれで変化しない。"""
        wall = create_entry(
            self.conn, level=3, content="時代の語り (旧Lv3)", source_ids=[],
            start_time=0, end_time=10_000, source_count=10, message_count=400,
            extra_metadata={"coverage_chars": 400},
        )
        for i in range(4):
            _lv1(self.conn, 20_000 + i * 100, 20_000 + i * 100 + 99, 100)
        self._run()
        self.assertEqual(get_entry(self.conn, wall.id).content, "時代の語り (旧Lv3)")

    def test_gap_wall_stops_bundling_run(self):
        """束ね列の時間ギャップに壁が挟まる場合はそこで打ち切る (跨がない)。"""
        # 古い孤立 Lv1 が 1 個 → 壁 → 新しい Lv1 群
        old = _lv1(self.conn, 100, 199, 100)
        create_entry(
            self.conn, level=2, content="壁Lv2", source_ids=[],
            start_time=200, end_time=299, source_count=2, message_count=40,
            extra_metadata={"coverage_chars": 200},
        )
        a = _lv1(self.conn, 300, 399, 100)
        b = _lv1(self.conn, 400, 499, 100)
        c = _lv1(self.conn, 500, 599, 100)  # 帯合計 400 > 200
        self._run()
        # old は壁を跨いで a と束ねられない
        old_after = get_entry(self.conn, old.id)
        if old_after.is_consolidated:
            parent = get_entry(self.conn, old_after.parent_id)
            self.assertNotIn(a.id, parent.source_ids)
        # 壁の後の連続列 (a+b) は束ねられる
        new_lv2 = [e for e in get_entries_by_level(self.conn, 2, order_by_time=True)
                   if e.content not in ("壁Lv2",)]
        self.assertEqual(len(new_lv2), 1)
        self.assertEqual(new_lv2[0].source_ids, [a.id, b.id])
        self.assertFalse(get_entry(self.conn, c.id).is_consolidated)


class TestAtomicity(BandTestBase):
    """M2-a: 親 + 子は単一 tx — LLM 失敗では何も変わらない。"""

    def test_llm_failure_changes_nothing(self):
        ids = [
            _lv1(self.conn, 100 * (i + 1), 100 * (i + 1) + 99, 100).id
            for i in range(4)
        ]
        created = self._run(_Client(fail=True))
        self.assertEqual(created, 0)
        self.assertEqual(get_entries_by_level(self.conn, 2, order_by_time=True), [])
        for eid in ids:
            self.assertFalse(get_entry(self.conn, eid).is_consolidated)

    def test_concurrent_child_consolidation_abandons(self):
        """LLM 中に並走ジョブが同じ子列を束ねたら放棄する
        (親の二重生成・parent_id 後勝ちの防止 — Codex W4 #2)。"""
        entries = [
            _lv1(self.conn, 100 * (i + 1), 100 * (i + 1) + 99, 100)
            for i in range(4)
        ]
        outer_conn = self.conn

        class _RacingClient:
            def generate(self, messages, tools):
                # 並走ジョブが最古の子を先に束ねてしまう状況を模擬
                from sai_memory.arasuji.storage import create_entry, mark_consolidated
                rival = create_entry(
                    outer_conn, level=2, content="先着の親", source_ids=[entries[0].id],
                    start_time=100, end_time=199, source_count=1, message_count=1,
                    extra_metadata={"coverage_chars": 100},
                )
                mark_consolidated(outer_conn, [entries[0].id], rival.id)
                return "後着のまとめ"

            def consume_usage(self):
                return None

        created = self._run(_RacingClient())
        self.assertEqual(created, 0)
        # 後着の親は作られない (先着の親 1 個のみ)
        lv2 = get_entries_by_level(self.conn, 2, order_by_time=True)
        self.assertEqual([e.content for e in lv2], ["先着の親"])
        # 先着の parent_id は上書きされていない
        child = get_entry(self.conn, entries[0].id)
        self.assertEqual(child.parent_id, lv2[0].id)


class TestWallRemainder(BandTestBase):
    """Codex W4 #5: 壁で打ち切られた目標未達の列は束ねない (持ち越し)。"""

    def test_short_run_before_wall_not_consolidated(self):
        # 壁前に 50+50 (target 200 未満)、壁後に十分なノード
        a = _lv1(self.conn, 100, 149, 50)
        b = _lv1(self.conn, 150, 199, 50)
        create_entry(
            self.conn, level=2, content="壁", source_ids=[],
            start_time=200, end_time=299, source_count=2, message_count=40,
            extra_metadata={"coverage_chars": 200},
        )
        c = _lv1(self.conn, 300, 399, 100)
        d = _lv1(self.conn, 400, 499, 100)
        e = _lv1(self.conn, 500, 599, 100)  # 帯合計 400 > cap 200
        self._run()
        # 壁前の 100 字列は親化されない
        self.assertFalse(get_entry(self.conn, a.id).is_consolidated)
        self.assertFalse(get_entry(self.conn, b.id).is_consolidated)
        # 壁後の連続列 (c+d = 200 到達) が束ねられる
        new_lv2 = [x for x in get_entries_by_level(self.conn, 2, order_by_time=True)
                   if x.content != "壁"]
        self.assertEqual(len(new_lv2), 1)
        self.assertEqual(new_lv2[0].source_ids, [c.id, d.id])
        self.assertFalse(get_entry(self.conn, e.id).is_consolidated)


class TestDryPlan(BandTestBase):
    """plan_band_overflow (dry) は実行と同じ回数を予測する (Codex W4 #4/#9)。"""

    def test_dry_matches_execution(self):
        from sai_memory.arasuji.bands import plan_band_overflow
        for i in range(4):
            _lv1(self.conn, 100 * (i + 1), 100 * (i + 1) + 99, 100)
        with patch.dict(os.environ, ENV):
            predicted = plan_band_overflow(self.conn)
        client = _Client()
        executed = self._run(client)
        self.assertEqual(predicted, executed)
        self.assertEqual(client.calls, predicted)

    def test_dry_counts_extra_leaves(self):
        """既存帯 + 新チャンク (extra_leaves) の合算であふれを予測する。"""
        from sai_memory.arasuji.bands import plan_band_overflow
        # 既存 150 字 (cap 200 未満) — 新チャンク 100 字が来るとあふれる
        _lv1(self.conn, 100, 199, 100)
        _lv1(self.conn, 200, 299, 50)
        with patch.dict(os.environ, ENV):
            without = plan_band_overflow(self.conn)
            with_extra = plan_band_overflow(
                self.conn, extra_leaves=[(100, 300, 399)],
            )
        self.assertEqual(without, 0)
        self.assertEqual(with_extra, 1)

    def test_dry_no_overflow_zero(self):
        from sai_memory.arasuji.bands import plan_band_overflow
        _lv1(self.conn, 100, 199, 100)
        with patch.dict(os.environ, ENV):
            self.assertEqual(plan_band_overflow(self.conn), 0)


class TestRecursiveBands(BandTestBase):
    def test_band2_overflow_consolidates_to_level3(self):
        """検算退化 (§4-6): 帯 2 もあふれたら級 3 へ (再帰)。"""
        # 級 2 ノードを直接 5 個置く (帯 2 cap = 100×2^2 = 400 を超える計 1000)
        lv2_ids = []
        for i in range(5):
            e = create_entry(
                self.conn, level=2, content=f"lv2-{i}", source_ids=[],
                start_time=1000 * (i + 1), end_time=1000 * (i + 1) + 999,
                source_count=2, message_count=40,
                extra_metadata={"coverage_chars": 200},
            )
            lv2_ids.append(e.id)
        self._run()
        lv3 = get_entries_by_level(self.conn, 3, order_by_time=True)
        # 1000 > 400 → 古い端から [0,1] を束ね (残 600 > 400) → [2,3] を束ね
        # (残 200+400=… unconsolidated は 200 <= 400) で停止 = 親 2 個
        self.assertEqual(len(lv3), 2)
        self.assertEqual(lv3[0].source_ids, lv2_ids[:2])
        self.assertEqual(lv3[1].source_ids, lv2_ids[2:4])
        # 最後の 1 個は束ねられず残る (§4-7: 情報は消えない)
        remaining_lv2 = [
            e for e in get_entries_by_level(self.conn, 2, order_by_time=True)
            if not e.is_consolidated
        ]
        self.assertEqual([e.id for e in remaining_lv2], [lv2_ids[4]])


class TestBackfillCoverage(BandTestBase):
    def _add_message(self, mid, content):
        # backfill は messages.id / content しか見ない — 最小スキーマで足りる
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, content TEXT)"
        )
        self.conn.execute(
            "INSERT INTO messages (id, content) VALUES (?, ?)", (mid, content)
        )
        self.conn.commit()

    def test_lv1_backfill_from_sources(self):
        self._add_message("s1", "あ" * 30)
        self._add_message("s2", "い" * 20)
        entry = create_entry(
            self.conn, level=1, content="要約", source_ids=["s1", "s2"],
            start_time=1, end_time=2, source_count=2, message_count=2,
        )
        filled = backfill_coverage(self.conn)
        self.assertEqual(filled, 1)
        import json
        meta = json.loads(self.conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry.id,)
        ).fetchone()[0])
        self.assertEqual(meta["coverage_chars"], 50)
        self.assertNotIn("coverage_estimated", meta)

    def test_lv2_backfill_sums_children(self):
        self._add_message("s1", "x" * 40)
        c1 = create_entry(
            self.conn, level=1, content="c1", source_ids=["s1"],
            start_time=1, end_time=2, source_count=1, message_count=1,
        )
        c2 = create_entry(
            self.conn, level=1, content="c2", source_ids=[],
            start_time=3, end_time=4, source_count=1, message_count=1,
            extra_metadata={"coverage_chars": 60},
        )
        parent = create_entry(
            self.conn, level=2, content="p" * 10, source_ids=[c1.id, c2.id],
            start_time=1, end_time=4, source_count=2, message_count=2,
        )
        filled = backfill_coverage(self.conn)
        # c1 (実測 40) と parent (40+60) が埋まる (c2 は既にある)
        self.assertEqual(filled, 2)
        import json
        meta = json.loads(self.conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (parent.id,)
        ).fetchone()[0])
        self.assertEqual(meta["coverage_chars"], 100)

    def test_missing_sources_estimated(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, content TEXT)"
        )
        entry = create_entry(
            self.conn, level=1, content="要約テキスト", source_ids=["gone-1"],
            start_time=1, end_time=2, source_count=1, message_count=1,
        )
        backfill_coverage(self.conn)
        import json
        meta = json.loads(self.conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry.id,)
        ).fetchone()[0])
        self.assertEqual(meta["coverage_chars"], len("要約テキスト") * 10)
        self.assertTrue(meta["coverage_estimated"])

    def test_backfill_is_idempotent(self):
        self._add_message("s1", "x" * 40)
        create_entry(
            self.conn, level=1, content="c1", source_ids=["s1"],
            start_time=1, end_time=2, source_count=1, message_count=1,
        )
        self.assertEqual(backfill_coverage(self.conn), 1)
        self.assertEqual(backfill_coverage(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
