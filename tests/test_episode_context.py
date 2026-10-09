"""Tests for episode context retrieval with level promotion control."""

import os
import sqlite3
import unittest
from typing import List
from unittest import mock

from sai_memory.arasuji.context import (
    DEFAULT_CHRONICLE_CHAR_BUDGET,
    MIN_ENTRIES_PER_LEVEL,
    USE_DEFAULT_BUDGET,
    ContextEntry,
    get_episode_context,
    get_episode_context_for_timerange,
)
from sai_memory.arasuji.storage import (
    create_entry,
    init_arasuji_tables,
    mark_consolidated,
)


def _create_lv1_entry(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    content: str = "",
    message_count: int = 20,
) -> str:
    """Create a Lv1 arasuji entry and return its ID."""
    if not content:
        content = f"Lv1 summary {start_time}-{end_time}"
    entry = create_entry(
        conn,
        level=1,
        content=content,
        source_ids=[],
        start_time=start_time,
        end_time=end_time,
        source_count=1,
        message_count=message_count,
    )
    return entry.id


def _create_lv2_entry(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    source_ids: List[str],
    content: str = "",
    message_count: int = 200,
) -> str:
    """Create a Lv2 arasuji entry and return its ID."""
    if not content:
        content = f"Lv2 summary {start_time}-{end_time}"
    entry = create_entry(
        conn,
        level=2,
        content=content,
        source_ids=source_ids,
        start_time=start_time,
        end_time=end_time,
        source_count=len(source_ids),
        message_count=message_count,
    )
    return entry.id


class TestLevelPromotionControl(unittest.TestCase):
    """Test that level promotion requires MIN_ENTRIES_PER_LEVEL entries."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_old_behavior_would_promote_after_one_lv1(self):
        """Verify that with MIN_ENTRIES_PER_LEVEL, Lv1 entries are retained.

        Create 15 Lv1 entries and a Lv2 covering entries 1-10.
        Old behavior: 1 Lv1 read → immediate Lv2 promotion → only 1 Lv1.
        New behavior: must read MIN_ENTRIES_PER_LEVEL Lv1s before Lv2 allowed.
        """
        # Create 15 Lv1 entries (time 100-1500, each covering 100 units)
        lv1_ids = []
        for i in range(15):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create a Lv2 covering entries 0-9 (time 100-1099)
        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=1099,
            source_ids=lv1_ids[:10],
        )

        context = get_episode_context(self.conn, max_entries=50)

        # Count Lv1 entries in context
        lv1_count = sum(1 for e in context if e.level == 1)

        # With new behavior, we should have at least MIN_ENTRIES_PER_LEVEL Lv1s
        # (5 remaining unconsolidated Lv1s: entries 10-14)
        # The Lv2 should only appear AFTER enough Lv1s have been read
        self.assertGreaterEqual(
            lv1_count, 5,  # All 5 unconsolidated Lv1s should be present
            f"Expected at least 5 Lv1 entries, got {lv1_count}",
        )

    def test_min_entries_before_promotion(self):
        """Exactly MIN_ENTRIES_PER_LEVEL Lv1 entries must be read before Lv2."""
        # Create enough Lv1 entries that we can test the boundary
        n_lv1 = MIN_ENTRIES_PER_LEVEL + 5
        lv1_ids = []
        for i in range(n_lv1):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create a Lv2 covering the first MIN_ENTRIES_PER_LEVEL entries
        first_n = MIN_ENTRIES_PER_LEVEL
        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=first_n * 100 + 99,
            source_ids=lv1_ids[:first_n],
        )

        context = get_episode_context(self.conn, max_entries=100)

        # Key assertion: total Lv1 entries should be at least 5
        # (the 5 unconsolidated ones that aren't covered by Lv2)
        lv1_in_context = sum(1 for e in context if e.level == 1)
        self.assertGreaterEqual(lv1_in_context, 5)

    def test_no_lv2_when_insufficient_lv1(self):
        """When fewer than MIN_ENTRIES_PER_LEVEL Lv1 entries exist, no Lv2."""
        # Create only 3 Lv1 entries
        lv1_ids = []
        for i in range(3):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create a Lv2 that would cover these if promotion were allowed
        # But since the Lv2 source_ids include the Lv1s, they're marked as read
        # Actually, we need Lv1s that are NOT covered by Lv2 for them to appear
        # Let's create Lv1s that aren't in the Lv2's sources
        extra_lv1_ids = []
        for i in range(3, 6):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            extra_lv1_ids.append(entry_id)

        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=399,
            source_ids=lv1_ids,
        )

        context = get_episode_context(self.conn, max_entries=50)
        levels = [e.level for e in context]

        # Only 3 Lv1 entries exist that aren't covered by Lv2
        # 3 < MIN_ENTRIES_PER_LEVEL, so Lv2 should NOT appear
        self.assertNotIn(
            2, levels,
            "Lv2 should not appear when fewer than MIN_ENTRIES_PER_LEVEL "
            "Lv1 entries have been read",
        )

    def test_only_lv1_no_promotion(self):
        """When only Lv1 entries exist (no Lv2), all should be returned."""
        for i in range(20):
            start = (i + 1) * 100
            end = start + 99
            _create_lv1_entry(self.conn, start, end)

        context = get_episode_context(self.conn, max_entries=50)

        # All entries should be Lv1
        for entry in context:
            self.assertEqual(entry.level, 1)

        self.assertEqual(len(context), 20)

    def test_timerange_also_respects_min_entries(self):
        """get_episode_context_for_timerange should also enforce min entries."""
        lv1_ids = []
        for i in range(15):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=1099,
            source_ids=lv1_ids[:10],
        )

        # Get context for events before time 1600
        result = get_episode_context_for_timerange(
            self.conn,
            start_time=1600,
            end_time=1700,
            max_entries=50,
        )

        # Should contain Lv1 entries, not jump to Lv2 immediately
        self.assertIn("あらすじ", result)


class TestTimerangeContextHierarchyInvariant(unittest.TestCase):
    """編纂中のチャンクが見る「これまでの流れ」の不変条件 (2026-09-03 まはー裁定)。

    近い過去はレベル1 のまま細かく、遠い過去はレベル2 で粗く — 階層があって
    初めて少ない件数で全史を覆える。束ねをチャンク確定のたびに挟む理由は
    この形を走行の途中でも成立させるため (executor.execute_plan の after_chunk)。

    昇格の規則 (context.MIN_ENTRIES_PER_LEVEL): あるレベルを MIN 件読むまで
    一つ上のレベルへは上がれない。だから直近の一次あらすじは MIN 件並べる。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _labels_in_order(text: str):
        """整形済み文字列から (レベル名, 本文) を古い順に取り出す。"""
        out = []
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("【") and ": " in line:
                out.append((line[1:].split(":", 1)[0], lines[i + 1]))
        return out

    def test_near_past_stays_lv1_and_far_past_is_read_as_lv2(self):
        # e1..eN (N = MIN + 4) を時刻順に。P (Lv2) は最古の 4 件 e1..e4 を覆う。
        n_near = MIN_ENTRIES_PER_LEVEL
        n_total = n_near + 4
        lv1_ids = []
        for i in range(1, n_total + 1):
            lv1_ids.append(_create_lv1_entry(
                self.conn, i * 100, i * 100 + 99, content=f"E{i}",
            ))
        _create_lv2_entry(
            self.conn, start_time=100, end_time=499,
            source_ids=lv1_ids[:4], content="P",
        )

        # eN の後ろから始まるチャンクの文脈。
        text = get_episode_context_for_timerange(
            self.conn,
            start_time=(n_total + 1) * 100,
            end_time=(n_total + 1) * 100 + 50,
            max_entries=20,
        )
        seq = self._labels_in_order(text)

        # 古い順: P (Lv2) が 1 件、続いて e5..eN が Lv1 で MIN 件。
        expected = [("あらすじのあらすじ", "P")] + [
            ("あらすじ", f"E{i}") for i in range(5, n_total + 1)
        ]
        self.assertEqual(seq, expected)
        # e1..e4 は個別に載らない (P が代弁する)。
        for i in range(1, 5):
            self.assertNotIn(f"\nE{i}\n", text)

    def test_without_lv2_the_walk_loses_the_far_past(self):
        """階層が無いと 20 件で届く範囲は直近 20 件の Lv1 だけ — 束ねを
        走行の最後まで遅らせた大量編纂の後半チャンクが見ていた形。"""
        n_total = 25
        for i in range(1, n_total + 1):
            _create_lv1_entry(self.conn, i * 100, i * 100 + 99, content=f"E{i}")
        text = get_episode_context_for_timerange(
            self.conn,
            start_time=(n_total + 1) * 100,
            end_time=(n_total + 1) * 100 + 50,
            max_entries=20,
        )
        seq = self._labels_in_order(text)
        self.assertEqual(len(seq), 20)
        self.assertEqual(seq[0][1], "E6", "最古の 5 件が文脈から落ちる")
        self.assertNotIn("\nE1\n", text)


class TestLevelPromotionEdgeCases(unittest.TestCase):
    """Edge cases for level promotion control."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_database(self):
        """No entries should return empty context."""
        context = get_episode_context(self.conn, max_entries=50)
        self.assertEqual(len(context), 0)

    def test_single_entry(self):
        """Single Lv1 entry should be returned."""
        _create_lv1_entry(self.conn, 100, 199)
        context = get_episode_context(self.conn, max_entries=50)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0].level, 1)

    def test_promotion_happens_after_threshold(self):
        """After reading MIN_ENTRIES_PER_LEVEL Lv1s, Lv2 becomes available."""
        # Create MIN_ENTRIES_PER_LEVEL + 10 Lv1 entries
        n_total = MIN_ENTRIES_PER_LEVEL + 10
        lv1_ids = []
        for i in range(n_total):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create Lv2 covering the first 10 entries
        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=1099,
            source_ids=lv1_ids[:10],
        )

        context = get_episode_context(self.conn, max_entries=100)

        # With enough remaining Lv1s (n_total - 10 = MIN_ENTRIES_PER_LEVEL),
        # exactly MIN_ENTRIES_PER_LEVEL Lv1s will be read, then Lv2 becomes
        # available for the consolidated range
        lv1_count = sum(1 for e in context if e.level == 1)
        self.assertGreaterEqual(lv1_count, MIN_ENTRIES_PER_LEVEL)


def _create_lv3_entry(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    source_ids: List[str],
    content: str = "",
    message_count: int = 2000,
) -> str:
    """Create a Lv3 arasuji entry and return its ID."""
    if not content:
        content = f"Lv3 summary {start_time}-{end_time}"
    entry = create_entry(
        conn,
        level=3,
        content=content,
        source_ids=source_ids,
        start_time=start_time,
        end_time=end_time,
        source_count=len(source_ids),
        message_count=message_count,
    )
    return entry.id


def _build_deep_hierarchy(conn: sqlite3.Connection) -> dict:
    """Build a deep 3-level hierarchy for budget tests.

    Layout (time ascending), designed so coarser promotion genuinely reduces
    char volume (unlike a flat set of unconsolidated Lv1):

      - 60 Lv1 entries, ~100 chars each, 100 time-units apart.
      - The oldest 40 Lv1 are consolidated into 4 Lv2 (10 each), and those 4 Lv2
        into 1 Lv3.  So the old two-thirds of history is *also* readable as coarse
        Lv2 (200 chars each) / Lv3 (single ~300-char entry).
      - The newest 20 Lv1 remain unconsolidated (detailed recent past that can
        never be coarsened — this is what keeps the newest end detailed).

    Char accounting: because every Lv1 is also consolidated into a Lv2, lowering
    the promotion threshold makes the traversal switch from many detailed Lv1 to
    few coarse Lv2 sooner, cutting char volume. A tiny unconsolidated tail keeps
    the newest end detailed at any threshold.

    Returns dict of ids for assertions.
    """
    lv1_ids: List[str] = []
    for i in range(60):
        start = (i + 1) * 100
        end = start + 99
        content = f"L1-{i:02d} " + ("x" * 94)  # ~100 chars
        lv1_ids.append(_create_lv1_entry(conn, start, end, content=content))

    # Consolidate the oldest 50 Lv1 into 5 Lv2 (10 each). The newest 10 Lv1
    # (index 50..59) stay unconsolidated → always-detailed recent past.
    lv2_ids: List[str] = []
    for b in range(5):
        s = b * 10
        lv2_ids.append(
            _create_lv2_entry(
                conn,
                start_time=(s + 1) * 100,
                end_time=(s + 10) * 100 + 99,
                source_ids=lv1_ids[s:s + 10],
                content=f"L2-{b} " + ("y" * 195),  # ~200 chars
            )
        )
    # Consolidate the 5 Lv2 into 1 Lv3
    lv3 = _create_lv3_entry(
        conn, start_time=100, end_time=5099, source_ids=lv2_ids,
        content="L3 " + ("z" * 297),  # ~300 chars
    )
    return {
        "lv1_ids": lv1_ids,
        "lv2_ids": lv2_ids,
        "lv3_id": lv3,
        "oldest_start": 100,
    }


class TestChronicleCharBudget(unittest.TestCase):
    """Phase 3 (§6.2, 不変条件 §10-4): char-budget reading."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def _oldest_covered(self, ctx: List[ContextEntry]) -> int:
        """start_time of the oldest entry in an oldest→newest ordered ctx."""
        return ctx[0].start_time

    def test_none_budget_is_legacy_count_based(self):
        """char_budget=None keeps legacy behavior (Track Chronicle path)."""
        info = _build_deep_hierarchy(self.conn)
        legacy = get_episode_context(self.conn, max_entries=100, char_budget=None)
        # Legacy uses MIN_ENTRIES_PER_LEVEL throughout (no budget re-run).
        # With 20 unconsolidated Lv1 + promotion, most recent are Lv1.
        lv1 = sum(1 for e in legacy if e.level == 1)
        self.assertGreaterEqual(lv1, MIN_ENTRIES_PER_LEVEL)
        # And it still reaches the oldest.
        self.assertEqual(self._oldest_covered(legacy), info["oldest_start"])

    def test_ample_budget_keeps_full_span_and_detail(self):
        """潤沢な予算では圧縮が掛からず、全期間を覆い最新端は細かいまま。

        旧仕様の「予算モード == 件数モード」の同一性検査は、粒度選択が累積
        質量ルールへ世代交代した際に退役した (chronicle_consolidation §6)。
        健全な実データでは両者はほぼ一致する (2026-07-26 ドライラン: aifi /
        quon で完全一致) が、質量の10倍境界ちょうどに置いたフィクスチャでは
        質量ルールが一段細かい側に倒れるため、同一性はもう不変条件ではない。
        残る不変条件は「全期間の被覆 (穴なし)」と「最新端の細かさ」。
        """
        info = _build_deep_hierarchy(self.conn)
        budgeted = get_episode_context(
            self.conn, max_entries=100, char_budget=1_000_000
        )
        # 全期間: 最古が落ちない。
        self.assertEqual(self._oldest_covered(budgeted), info["oldest_start"])
        # 最新端は細かいまま。
        self.assertEqual(budgeted[-1].level, 1)
        # 穴なし: 全 Lv1 が「自身 or 祖先」で提示に代表される。
        shown = {e.source_id for e in budgeted}
        for i, lv1_id in enumerate(info["lv1_ids"]):
            ancestors = {lv1_id, info["lv3_id"]}
            if i < 50:
                ancestors.add(info["lv2_ids"][i // 10])
            self.assertTrue(
                ancestors & shown,
                f"Lv1 #{i} is not represented (itself or ancestor) in output",
            )

    def test_tight_budget_promotes_early_and_keeps_oldest(self):
        """Deep hierarchy + tight budget → coarser levels, oldest retained."""
        info = _build_deep_hierarchy(self.conn)

        # Legacy would read ~20 detailed Lv1 (2000+ chars) → too big for 800.
        tight = get_episode_context(
            self.conn, max_entries=1000, char_budget=800
        )
        total = sum(len(e.content) for e in tight)

        # Oldest must still be covered (不変条件 §10-4).
        self.assertEqual(
            self._oldest_covered(tight), info["oldest_start"],
            "Oldest entry must never be dropped under a tight budget",
        )
        # It should have promoted to coarser levels (Lv2 or Lv3 present),
        # unlike the legacy detailed Lv1-heavy output.
        levels = {e.level for e in tight}
        self.assertTrue(
            2 in levels or 3 in levels,
            f"Expected coarse levels under tight budget, got levels={levels}",
        )
        # And it should be much smaller than the legacy content volume.
        legacy = get_episode_context(self.conn, max_entries=1000, char_budget=None)
        legacy_total = sum(len(e.content) for e in legacy)
        self.assertLess(total, legacy_total)

    def test_extreme_budget_still_includes_oldest(self):
        """Budget smaller than even the coarsest run → overflow but oldest kept."""
        info = _build_deep_hierarchy(self.conn)
        # A budget of 1 char is impossible to satisfy; the coarsest run wins and
        # the oldest is still present.
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING") as cm:
            ctx = get_episode_context(
                self.conn, max_entries=1000, char_budget=1
            )
        self.assertTrue(ctx, "Extreme budget must still return entries")
        self.assertEqual(
            self._oldest_covered(ctx), info["oldest_start"],
            "Oldest entry must be included even when budget is unsatisfiable",
        )
        self.assertTrue(
            any("char budget exceeded" in m for m in cm.output),
            "Overflow must emit a WARNING",
        )

    def test_newest_stays_detailed_oldest_coarsened(self):
        """Time-axis endpoints preserved: newest detailed, distant past coarse."""
        _build_deep_hierarchy(self.conn)
        ctx = get_episode_context(self.conn, max_entries=1000, char_budget=800)
        # ctx is oldest→newest. Oldest should be the coarsest available (Lv3),
        # newest should be a detailed Lv1.
        self.assertGreaterEqual(ctx[0].level, 2, "Distant past should be coarse")
        self.assertEqual(ctx[-1].level, 1, "Recent past should stay detailed")

    def test_lv3_reachable_for_old_portion(self):
        """Mirror the real DB: an old-portion Lv3 is reached under a tight budget.

        Shape observed on the real memory.db at budget 3000 (Lv3 for the distant
        past, then Lv2, then a detailed Lv1). Reaching a Lv3 that covers only the
        old portion requires the traversal to have climbed to level 2 (via
        interspersed mid-timeline Lv2) before it descends into the old region.
        """
        # OLD (time 100..2099): 20 Lv1 → 2 Lv2 → 1 Lv3
        old_lv1 = [
            _create_lv1_entry(self.conn, (i + 1) * 100, (i + 1) * 100 + 99,
                              content=f"O{i} " + ("x" * 100))
            for i in range(20)
        ]
        old_a = _create_lv2_entry(self.conn, 100, 1099, old_lv1[:10],
                                  content="OA " + ("y" * 200))
        old_b = _create_lv2_entry(self.conn, 1100, 2099, old_lv1[10:],
                                  content="OB " + ("y" * 200))
        _create_lv3_entry(self.conn, 100, 2099, [old_a, old_b],
                          content="O3 " + ("z" * 300))
        # MID (time 2100..4099): 20 Lv1 → 2 Lv2 (no Lv3) — lifts current_level to 2
        mid_lv1 = [
            _create_lv1_entry(self.conn, 2100 + i * 100, 2100 + i * 100 + 99,
                              content=f"M{i} " + ("x" * 100))
            for i in range(20)
        ]
        _create_lv2_entry(self.conn, 2100, 3099, mid_lv1[:10],
                          content="MA " + ("y" * 200))
        _create_lv2_entry(self.conn, 3100, 4099, mid_lv1[10:],
                          content="MB " + ("y" * 200))
        # RECENT (time 4100..4599): 5 unconsolidated Lv1 — always detailed
        for i in range(5):
            s = 4100 + i * 100
            _create_lv1_entry(self.conn, s, s + 99, content=f"R{i} " + ("x" * 100))

        # Budget small enough to force the coarsest run (threshold 1, prefer_coarse).
        ctx = get_episode_context(self.conn, max_entries=1000, char_budget=700)
        levels = [e.level for e in ctx]
        self.assertEqual(ctx[0].level, 3, f"Distant past should be Lv3, got {levels}")
        self.assertEqual(ctx[-1].level, 1, "Recent past should stay Lv1")
        self.assertEqual(ctx[0].start_time, 100, "Oldest (time=100) must be present")
        self.assertIn(3, levels)

    def test_env_override(self):
        """SAIVERSE_CHRONICLE_CHAR_BUDGET overrides the default budget."""
        _build_deep_hierarchy(self.conn)
        # With a tiny env budget, USE_DEFAULT_BUDGET must resolve to it and
        # promote hard (fewer entries than a large explicit budget).
        with mock.patch.dict(os.environ, {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "800"}):
            env_ctx = get_episode_context(
                self.conn, max_entries=1000, char_budget=USE_DEFAULT_BUDGET
            )
        big_ctx = get_episode_context(
            self.conn, max_entries=1000, char_budget=1_000_000
        )
        self.assertLess(
            sum(len(e.content) for e in env_ctx),
            sum(len(e.content) for e in big_ctx),
            "Env-overridden tiny budget must yield smaller context",
        )

    def test_default_budget_used_when_env_unset(self):
        """USE_DEFAULT_BUDGET falls back to DEFAULT_CHRONICLE_CHAR_BUDGET."""
        _build_deep_hierarchy(self.conn)
        env = dict(os.environ)
        env.pop("SAIVERSE_CHRONICLE_CHAR_BUDGET", None)
        with mock.patch.dict(os.environ, env, clear=True):
            default_ctx = get_episode_context(
                self.conn, max_entries=1000, char_budget=USE_DEFAULT_BUDGET
            )
        explicit_ctx = get_episode_context(
            self.conn, max_entries=1000, char_budget=DEFAULT_CHRONICLE_CHAR_BUDGET
        )
        self.assertEqual(
            [e.source_id for e in default_ctx],
            [e.source_id for e in explicit_ctx],
        )

    def test_invalid_env_falls_back_to_default(self):
        """A non-integer env value logs a warning and uses the default."""
        _build_deep_hierarchy(self.conn)
        with mock.patch.dict(
            os.environ, {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "not-a-number"}
        ):
            with self.assertLogs(
                "sai_memory.arasuji.context", level="WARNING"
            ) as cm:
                ctx = get_episode_context(
                    self.conn, max_entries=1000, char_budget=USE_DEFAULT_BUDGET
                )
        self.assertTrue(ctx)
        self.assertTrue(any("Invalid SAIVERSE_CHRONICLE_CHAR_BUDGET" in m for m in cm.output))

    def test_count_based_path_ignores_env_budget(self):
        """char_budget=None は env を無視して従来の件数ベースのままであること。

        (旧 test_track_chronicle_path_unaffected。Track Chronicle は退役したが
        char_budget=None の経路自体は生成スクリプト等に残るので、その保証を
        Track に依存しない形で残す。)
        """
        for i in range(15):
            start = (i + 1) * 100
            end = start + 99
            create_entry(
                self.conn, level=1, content=f"T{i} " + ("t" * 200),
                source_ids=[], start_time=start, end_time=end,
                source_count=1, message_count=20,
            )
        with mock.patch.dict(os.environ, {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "100"}):
            # Even with a tiny env budget, None means legacy: no promotion re-run.
            ctx = get_episode_context(
                self.conn, max_entries=50, char_budget=None,
            )
        # All 15 Lv1 present (no budget-driven coarsening), proving env ignored.
        self.assertEqual(len(ctx), 15)
        self.assertTrue(all(e.level == 1 for e in ctx))


class TestNoPresentationGap(unittest.TestCase):
    """提示に穴を空けないこと (2026-07-25 の実データ由来の回帰)。

    実害: eris_city_a で「上位あらすじが覆っていない直近 3.7 日 (一次あらすじ
    41 件)」が、aifi_city_a で「271 日 (98 件)」が、head の Chronicle から丸ごと
    落ちていた。予算を 11% 超えただけで粗さの段が飛び、その段が「粗いレベル
    優先」で上位あらすじを先に掴むと、それより新しい細かいあらすじは走査位置が
    過去へ動いた後なので二度と候補にならず、黙って消えていた。

    直し方: 走査は「読み残しの最前線」を飛び越えない。予算に収める圧縮は走査
    後の親への置き換え (範囲を保存する) に分離した。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_coarse_entry_must_not_skip_newer_fine_entries(self):
        """上位あらすじの外側にある、より新しい一次あらすじを飛び越えない。"""
        # 古い側: Lv1 10 件 → それを覆う Lv2 1 件 (time 100..1099)
        old_ids = [
            _create_lv1_entry(
                self.conn, (i + 1) * 100, (i + 1) * 100 + 99,
                content=f"O{i} " + ("x" * 200),
            )
            for i in range(10)
        ]
        _create_lv2_entry(
            self.conn, 100, 1099, old_ids, content="OLD " + ("y" * 200),
        )
        # 新しい側: どの上位にも覆われていない Lv1 20 件 (time 1100..3099)
        new_ids = [
            _create_lv1_entry(
                self.conn, 1100 + i * 100, 1100 + i * 100 + 99,
                content=f"N{i} " + ("x" * 200),
            )
            for i in range(20)
        ]

        # 旧実装はこの予算で粗い段へ落ち、Lv2 を掴んだ拍子に新しい 20 件を
        # まるごと落としていた。
        ctx = get_episode_context(self.conn, max_entries=1000, char_budget=500)

        shown = {e.source_id for e in ctx}
        missing = [i for i, nid in enumerate(new_ids) if nid not in shown]
        self.assertEqual(
            missing, [],
            "上位あらすじが覆っていない新しい一次あらすじが提示から落ちた "
            f"(落ちた index={missing})",
        )
        # 古い側は Lv2 が代弁していてよい (圧縮は正しい)。
        self.assertEqual(
            ctx[0].start_time, 100, "最古が落ちてはいけない (不変条件 §10-4)",
        )

    def test_partially_overlapping_entries_are_both_kept(self):
        """被覆が部分的に重なる entry を、片方を選んだ拍子に飛ばさない。"""
        a = _create_lv1_entry(self.conn, 100, 500, content="A" * 100)
        b = _create_lv1_entry(self.conn, 400, 800, content="B" * 100)

        ctx = get_episode_context(self.conn, max_entries=100)

        shown = {e.source_id for e in ctx}
        self.assertIn(a, shown, "先行する重なり entry が落ちた")
        self.assertIn(b, shown, "後続の重なり entry が落ちた")

    def test_compression_preserves_the_covered_span(self):
        """予算圧縮は粒度を粗くするだけで、被覆する時間範囲を変えない。"""
        _build_deep_hierarchy(self.conn)
        full = get_episode_context(
            self.conn, max_entries=1000, char_budget=1_000_000,
        )
        tight = get_episode_context(self.conn, max_entries=1000, char_budget=800)

        self.assertEqual(
            full[0].start_time, tight[0].start_time,
            "圧縮で最古の被覆開始が変わってはいけない",
        )
        self.assertEqual(
            full[-1].end_time, tight[-1].end_time,
            "圧縮で最新の被覆終了が変わってはいけない",
        )
        self.assertLess(
            sum(len(e.content) for e in tight),
            sum(len(e.content) for e in full),
            "圧縮が効いていない",
        )

    def test_compression_never_produces_overlapping_entries(self):
        """置き換えた親が隣の entry と範囲を重ねない (同じ体験の二重掲示)。"""
        _build_deep_hierarchy(self.conn)
        ctx = get_episode_context(self.conn, max_entries=1000, char_budget=800)

        for older, newer in zip(ctx, ctx[1:]):
            self.assertLessEqual(
                older.end_time, newer.start_time,
                "提示 entry の被覆が重なっている "
                f"({older.start_time}~{older.end_time} と "
                f"{newer.start_time}~{newer.end_time})",
            )

    def test_every_lv1_is_represented_by_itself_or_an_ancestor(self):
        """全ての一次あらすじが、自身か祖先のどちらかで代弁されている。

        一次あらすじは生ログを直接覆う最小粒度なので、これが成り立てば
        「体験が黙って消える」ことはない。
        """
        _build_deep_hierarchy(self.conn)
        ctx = get_episode_context(self.conn, max_entries=1000, char_budget=800)

        shown = {e.source_id for e in ctx}
        rows = self.conn.execute(
            "SELECT id, source_ids_json, level FROM arasuji_entries"
        ).fetchall()
        import json as _json
        parent_of = {}
        lv1_ids = []
        for entry_id, source_json, level in rows:
            if level == 1:
                lv1_ids.append(entry_id)
            for child in _json.loads(source_json or "[]"):
                parent_of[child] = entry_id

        unrepresented = []
        for lv1 in lv1_ids:
            cursor, seen = lv1, set()
            while cursor and cursor not in seen:
                seen.add(cursor)
                if cursor in shown:
                    break
                cursor = parent_of.get(cursor)
            else:
                unrepresented.append(lv1)

        self.assertEqual(
            unrepresented, [],
            f"{len(unrepresented)} 件の一次あらすじが自身も祖先も提示されていない",
        )


def _build_fixed_id_tree(conn: sqlite3.Connection) -> None:
    """降下規則のテスト用の、id を固定した三階層の木。

    - 一次あらすじ a00..a29 (各 ~100 字、時刻 100 刻み)
    - 二次あらすじ b0 (a00..a09) / b1 (a10..a19)、各 ~200 字
    - 三次あらすじ c0 (b0, b1)、~300 字
    - a20..a29 は未統合 (最新側の細かい過去)

    子には束ねと同じく統合済みの印を付ける (帯の組み立てがこの印を見ずに
    降下先の子を載せられることの確認を兼ねる)。
    """
    for i in range(30):
        s = (i + 1) * 100
        create_entry(
            conn, level=1, content=f"a{i:02d} " + "x" * 96, source_ids=[],
            start_time=s, end_time=s + 99, source_count=1, message_count=20,
            entry_id=f"a{i:02d}",
        )
    for b in range(2):
        children = [f"a{k:02d}" for k in range(b * 10, b * 10 + 10)]
        create_entry(
            conn, level=2, content=f"b{b} " + "y" * 197, source_ids=children,
            start_time=(b * 10 + 1) * 100, end_time=(b * 10 + 10) * 100 + 99,
            source_count=10, message_count=200, entry_id=f"b{b}",
        )
        mark_consolidated(conn, children, f"b{b}")
    create_entry(
        conn, level=3, content="c0 " + "z" * 297, source_ids=["b0", "b1"],
        start_time=100, end_time=2099, source_count=2, message_count=400,
        entry_id="c0",
    )
    mark_consolidated(conn, ["b0", "b1"], "c0")


_A = [f"a{i:02d}" for i in range(30)]
_RECENT = _A[20:]

# 除外名簿が空のときの出力 (降下規則の導入前の実装で採取した値を固定した golden)。
_GOLDEN_NO_EXCLUDE = {
    None: ["b0", "b1"] + _RECENT,
    1_000_000: ["b0"] + _A[10:],
    1500: ["b0", "b1"] + _RECENT,
    800: ["c0"] + _RECENT,
    1: ["c0"] + _RECENT,
}


class TestBandDescentAroundExcluded(unittest.TestCase):
    """帯の降下規則 (docs/intent/chronicle_consolidation_veto_removal.md 機構 B)。

    窓が digest で見せているエントリ (除外名簿) を子孫に含む親は帯に見せず、
    子に降りる。除外名簿のもの自身とその子孫は落とす。同じ期間が窓と帯の
    両方に出る二重提示を、予算ガード経由の再導入も含めて構造的に防ぐ。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)
        _build_fixed_id_tree(self.conn)

    def tearDown(self):
        self.conn.close()

    def _ids(self, *, budget, exclude=None):
        ctx = get_episode_context(
            self.conn, max_entries=1000, char_budget=budget,
            exclude_entry_ids=exclude,
        )
        return [e.source_id for e in ctx]

    def test_empty_exclude_matches_golden(self):
        """除外が空 (None / 空集合) なら降下は起きず、導入前と同一の出力。"""
        for budget, expected in _GOLDEN_NO_EXCLUDE.items():
            for exclude in (None, set()):
                with self.subTest(budget=budget, exclude=exclude):
                    self.assertEqual(
                        self._ids(budget=budget, exclude=exclude), expected,
                    )

    def test_lv2_parent_descends_when_one_child_is_excluded(self):
        """二次あらすじの子が一つ除外 → 親は消え、除外以外の子が並ぶ。

        読み戻し経路の既存欠陥の回帰でもある: 統合済みの一次あらすじ a05 が
        畳みに載ったとき、旧実装 (id の単純一致) は a05 だけを抜き、その親
        b0 (と祖父 c0) を帯に出して同じ期間を二重に見せていた。
        """
        expected_children = _A[0:5] + _A[6:10]
        # 件数ベース (予算なし): 走査は a29..a20 を読んでから粒度を上げて b1 を
        # 選び、そのあと降下先の a00..a09 (a05 抜き) を読む。
        self.assertEqual(
            self._ids(budget=None, exclude={"a05"}),
            expected_children + ["b1"] + _RECENT,
        )
        # 潤沢な予算: 粒度は質量ルールで細かいまま。
        self.assertEqual(
            self._ids(budget=1_000_000, exclude={"a05"}),
            expected_children + _A[10:],
        )

    def test_three_levels_deepest_excluded(self):
        """三階層の最深の子が除外 → Lv3 も Lv2 (b0) も降り、除外と交わらない
        Lv2 (b1) は予算ガードの畳み先として残る。"""
        ids = self._ids(budget=1500, exclude={"a05"})
        self.assertEqual(ids, _A[0:5] + _A[6:10] + ["b1"] + _RECENT)
        for gone in ("c0", "b0", "a05"):
            self.assertNotIn(gone, ids)

    def test_budget_guard_does_not_reintroduce_descended_parent(self):
        """予算超過でも、除外と交わる親は予算ガード経由で再導入されない。

        b0 に畳めば予算に近づくが、b0 は a05 を含むので候補に居ない。超過は
        受け入れて WARNING で観測する (intent「帰結」)。
        """
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING") as cm:
            ids = self._ids(budget=1, exclude={"a05"})
        self.assertEqual(ids, _A[0:5] + _A[6:10] + ["b1"] + _RECENT)
        for gone in ("c0", "b0", "a05"):
            self.assertNotIn(gone, ids)
        self.assertTrue(any("char budget exceeded" in m for m in cm.output))

    def test_excluded_parent_drops_its_descendants(self):
        """除外名簿のもの自身の子孫は、窓が見せている期間なので落とす。
        b0 の外側の期間は、予算に応じた粒度でそのまま残る。"""
        for budget, expected in {
            None: ["b1"] + _RECENT,
            1_000_000: _A[10:],
            1: ["b1"] + _RECENT,
        }.items():
            with self.subTest(budget=budget):
                self.assertEqual(self._ids(budget=budget, exclude={"b0"}), expected)

    def test_two_subtrees_excluded_at_once(self):
        """除外が別々の部分木に同時にある場合、両方の親 (と共通の祖父) が降り、
        除外以外の子が全期間を覆う。"""
        expected = [x for x in _A if x not in ("a05", "a15")]
        self.assertEqual(
            self._ids(budget=1_000_000, exclude={"a05", "a15"}), expected,
        )

    def test_missing_excluded_id_warns(self):
        """除外名簿の id が一覧に無いとき、降下は効かない — 無音にせず WARNING。"""
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING") as cm:
            self._ids(budget=1_000_000, exclude={"gone-entry"})
        self.assertTrue(any("not in the entry list" in m for m in cm.output))

    def test_unknown_excluded_id_is_ignored(self):
        """実在しない id が除外名簿にあっても無視し、従来どおりの出力。"""
        for budget, expected in _GOLDEN_NO_EXCLUDE.items():
            with self.subTest(budget=budget):
                self.assertEqual(
                    self._ids(budget=budget, exclude={"no-such-entry"}),
                    expected,
                )


class TestBandDescentSafetyValves(unittest.TestCase):
    """降下規則の安全弁 — 壊れた親子参照 (循環・自己参照) でも、除外の血族を
    帯に漏らさない (2026-09-27 ローカルレビュー指摘の固定)。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def _make(self, entry_id, level, source_ids, start, end):
        create_entry(
            self.conn, level=level, content=entry_id + " " + "x" * 50,
            source_ids=source_ids, start_time=start, end_time=end,
            source_count=max(1, len(source_ids)), message_count=10,
            entry_id=entry_id,
        )

    def test_cycle_does_not_leak_the_excluded_bloodline(self):
        """a↔b の循環の先 (孫の位置) に除外 c が居ても、循環の両方と c の親が
        帯から降りる。訪問順に依存しない (循環を踏んだ False はキャッシュに
        固定しない)。除外は直接の子ではなく孫に置く — 直接の子なら id 一致の
        検査が先に効いて、循環の安全弁まで到達しないため。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        self._make("c", 1, [], 100, 199)
        self._make("x", 2, ["c"], 100, 249)
        self._make("a", 2, ["b"], 100, 399)   # a → b → a の循環
        self._make("b", 2, ["a", "x"], 200, 299)
        self._make("d", 1, [], 400, 499)  # 無関係 — 残るべき
        entries = _get_all_arasuji_sorted(self.conn)
        for ordering in (entries, list(reversed(entries))):
            with self.subTest(first=ordering[0].id):
                out = _descend_around_excluded(ordering, {"c"})
                self.assertEqual({e.id for e in out}, {"d"})

    def test_self_reference_does_not_crash_and_still_descends(self):
        """自分自身を source_ids に持つ壊れた親も、除外の子を含むなら降りる。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        self._make("x", 1, [], 100, 199)
        self._make("p", 2, ["p", "x"], 100, 299)
        self._make("d", 1, [], 300, 399)
        out = _descend_around_excluded(
            _get_all_arasuji_sorted(self.conn), {"x"},
        )
        self.assertEqual({e.id for e in out}, {"d"})

    def test_missing_excluded_child_still_descends_the_parent(self):
        """親の source_ids に除外 id が書かれているが、その子の行が一覧に
        無い (削除済み・フィルタ済み) — 親は「除外を含む」と判定して降ろす
        (fail-open の禁止、2026-09-27 Codex 指摘)。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        self._make("kept", 1, [], 100, 199)
        self._make("p", 2, ["kept", "ghost"], 100, 299)  # ghost の行は無い
        self._make("d", 1, [], 300, 399)
        out = _descend_around_excluded(
            _get_all_arasuji_sorted(self.conn), {"ghost"},
        )
        self.assertEqual({e.id for e in out}, {"kept", "d"})

    def test_over_deep_chain_descends_every_ancestor(self):
        """異常に深い親子鎖 (壊れたデータ) でも落ちず、除外に到達できる祖先は
        **一件も残らない** — 深さで判定が途切れて祖先が帯に漏れる fail-open を
        禁止する (2026-09-27 Codex 二巡目指摘)。壊れた参照は収束までの反復
        回数の WARNING で観測する。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        depth = 120
        self._make("leaf", 1, [], 100, 199)
        prev = "leaf"
        for i in range(depth):
            node = f"n{i:03d}"
            # 全部 level 2 の鎖 = 同レベル参照 (壊れたデータ) — 収束に反復が要る
            self._make(node, 2, [prev], 100, 200 + i)
            prev = node
        self._make("d", 1, [], 900, 999)  # 無関係 — 残るべき
        entries = _get_all_arasuji_sorted(self.conn)
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING") as cm:
            out = _descend_around_excluded(entries, {"leaf"})
        self.assertTrue(any("reference" in m for m in cm.output))
        self.assertEqual({e.id for e in out}, {"d"})

    def test_missing_excluded_root_still_drops_its_children(self):
        """除外の根 (窓が見せている親) が一覧から欠けていても、その子は
        parent_id のリンクで拾って落とす — 窓と同じ期間の子が帯に漏れない
        (2026-09-27 Codex 四巡目指摘の固定)。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        from sai_memory.arasuji.storage import mark_consolidated
        self._make("c1", 1, [], 100, 199)
        self._make("c2", 1, [], 200, 299)
        # 親 P は作った後に一覧から消す (Track フィルタで欠ける形を模す)
        self._make("P", 2, ["c1", "c2"], 100, 299)
        mark_consolidated(self.conn, ["c1", "c2"], "P")
        self.conn.execute(
            "UPDATE memopedia_pages SET metadata = json_set(COALESCE(metadata,'{}'),"
            " '$.origin_track_id', 'legacy-track') WHERE id = 'P'",
        )
        self.conn.commit()
        self._make("d", 1, [], 300, 399)
        entries = _get_all_arasuji_sorted(self.conn)
        self.assertNotIn("P", {e.id for e in entries})  # 前提: 根が欠けている
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING") as cm:
            out = _descend_around_excluded(entries, {"P"})
        self.assertEqual({e.id for e in out}, {"d"})
        self.assertTrue(any("not in the entry list" in m for m in cm.output))

    def test_message_id_in_roster_does_not_drop_unrelated_entries(self):
        """壊れた畳みがメッセージ id を除外名簿に持ち込んでも、その id を
        source に持つ一次あらすじとその祖先は帯から消えない (2026-09-27
        Codex 七巡目指摘の固定)。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        from sai_memory.arasuji.storage import mark_consolidated
        self._make("L1", 1, ["msg-raw-001"], 100, 199)  # 正規のメッセージ参照
        self._make("P", 2, ["L1"], 100, 299)
        mark_consolidated(self.conn, ["L1"], "P")
        self._make("d", 1, [], 300, 399)
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING") as cm:
            out = _descend_around_excluded(
                _get_all_arasuji_sorted(self.conn), {"msg-raw-001"},
            )
        # 降下の関数は何も落とさない (統合済みの選別は後段の選定の仕事)。
        self.assertEqual({e.id for e in out}, {"L1", "P", "d"})
        self.assertTrue(any("not in the entry list" in m for m in cm.output))

    def test_parent_id_only_chain_is_dropped_below_excluded_root(self):
        """source_ids が空で parent_id だけで連なる壊れた鎖でも、除外の根の
        子孫は最下層まで落ちる (2026-09-27 Codex 五巡目指摘の固定)。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        self._make("P", 2, [], 100, 299)   # 除外の根 (子の記帳が欠けている)
        self._make("C", 1, [], 100, 199)
        self._make("G", 1, [], 200, 299)
        self.conn.execute(
            "UPDATE memopedia_pages SET parent_id = 'P' WHERE id = 'C'")
        self.conn.execute(
            "UPDATE memopedia_pages SET parent_id = 'C' WHERE id = 'G'")
        self.conn.commit()
        self._make("d", 1, [], 300, 399)
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING"):
            out = _descend_around_excluded(
                _get_all_arasuji_sorted(self.conn), {"P"},
            )
        self.assertEqual({e.id for e in out}, {"d"})

    def test_missing_intermediate_node_does_not_break_the_chain(self):
        """除外の根 P は居るが、中間の子 C の行だけが一覧から欠けている鎖
        (P.source_ids=[C]、孫 G.parent_id=C) でも、G まで落ちる — 欠けた id を
        中継点として辺に残す (2026-09-27 Codex 六巡目指摘の固定)。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        self._make("P", 3, ["C"], 100, 299)  # C の行は作らない (欠落)
        self._make("G", 1, [], 100, 199)
        self.conn.execute(
            "UPDATE memopedia_pages SET parent_id = 'C' WHERE id = 'G'")
        self.conn.commit()
        self._make("d", 1, [], 300, 399)
        out = _descend_around_excluded(
            _get_all_arasuji_sorted(self.conn), {"P"},
        )
        self.assertEqual({e.id for e in out}, {"d"})

    def test_corrupted_lv1_entry_reference_still_propagates(self):
        """一次あらすじの source_ids に Chronicle id が混入した壊れたデータでも、
        血族の伝播が途切れず、除外の祖先が帯に残らない (2026-09-27 Codex
        三巡目指摘の固定)。"""
        from sai_memory.arasuji.context import (
            _descend_around_excluded,
            _get_all_arasuji_sorted,
        )
        self._make("h2", 2, [], 100, 199)
        self._make("l1", 1, ["h2"], 100, 249)   # level 1 なのに entry を参照
        self._make("p3", 3, ["l1"], 100, 299)
        self._make("d", 1, [], 300, 399)
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING"):
            out = _descend_around_excluded(
                _get_all_arasuji_sorted(self.conn), {"h2"},
            )
        self.assertEqual({e.id for e in out}, {"d"})


if __name__ == "__main__":
    unittest.main()
