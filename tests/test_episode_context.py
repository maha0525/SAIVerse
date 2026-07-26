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

    def test_track_chronicle_path_unaffected(self):
        """origin_track_id path (Track Chronicle) must stay count-based (§6.2 item 5).

        Calling with char_budget=None (as the Track caller does) must not invoke
        the budget ladder, regardless of the env var.
        """
        # Build a Track-scoped hierarchy.
        lv1_ids = []
        for i in range(15):
            start = (i + 1) * 100
            end = start + 99
            entry = create_entry(
                self.conn, level=1, content=f"T{i} " + ("t" * 200),
                source_ids=[], start_time=start, end_time=end,
                source_count=1, message_count=20, origin_track_id="track-x",
            )
            lv1_ids.append(entry.id)
        with mock.patch.dict(os.environ, {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "100"}):
            # Even with a tiny env budget, None means legacy: no promotion re-run.
            ctx = get_episode_context(
                self.conn, max_entries=50, char_budget=None,
                origin_track_id="track-x",
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


if __name__ == "__main__":
    unittest.main()
