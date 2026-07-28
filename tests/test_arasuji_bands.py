"""Chronicle 束ね (sai_memory/arasuji/bands.py) の回帰テスト。

2026-07-28 の世代交代 (「字数発火・質量選抜」→「レベル別の並び + 予算超過で
畳む一本規則」) に追従した新仕様のテスト。設計正典は docs/intent/arasuji_levels.md。

固定する仕様の骨子:

- 並びはレベルごと。予算 = 上限 (BAND_CHAR_LIMIT=5,000) と残す量
  (BAND_CHAR_KEEP=2,500) の2つの数。合計字数 (excluded を除く) が上限を
  超えたら、古い側を「残す量」に収まるまで 1 個の親に畳み、1 つ上のレベルへ。
- レベル分離: 畳んだ結果は自分の並びに戻らない → 再要約回数は log 有限。
- メンバーの大きさ (被覆) は判定に使わない。被覆は合算で親へ引き継ぐ (保存)。
- 2 件未満しか取れないときは畳まない。excluded (提示中の圧縮区間) は畳み範囲が
  跨がない。
- 原子性: 親 INSERT + 子 mark_consolidated は単一 tx、tx 内で子の未束ねを再検査。
- Fragment callback は digest_origin='identity' の子 (旧世代データ) でのみ発火。

TestBackfillCoverage (coverage_chars 帰化バックフィル) は旧ファイルからの
回帰維持でそのまま残している。
"""

import json
import random
import unittest

from sai_memory.arasuji.bands import (
    BAND_CHAR_KEEP,
    BAND_CHAR_LIMIT,
    EST_PARENT_CHARS,
    _plan_folds,
    _RowItem,
    backfill_coverage,
    plan_band_overflow,
    run_band_overflow,
)
from sai_memory.arasuji.storage import (
    create_entry,
    get_entry,
    init_arasuji_tables,
)
from sai_memory.memory.storage import init_db


class _Client:
    def __init__(self, response="統合されたまとめ。", fail=False):
        self.calls = 0
        self.prompts = []
        self.response = response
        self.fail = fail

    def generate(self, messages, tools):
        self.calls += 1
        self.prompts.append(messages[0]["content"])
        if self.fail:
            raise RuntimeError("llm down")
        return self.response

    def consume_usage(self):
        return None


def _entry(
    conn,
    *,
    start,
    end=None,
    coverage,
    chars=600,
    level=1,
    origin="batch",
    source_ids=None,
):
    """並びのノードを 1 件作る。字数 = content 長で発火を操る。"""
    return create_entry(
        conn,
        level=level,
        content="あ" * chars,
        source_ids=source_ids if source_ids is not None else [f"src-{start}"],
        start_time=start,
        end_time=end if end is not None else start + 99,
        source_count=1,
        message_count=1,
        extra_metadata={"digest_origin": origin, "coverage_chars": coverage},
    )


def _entry_meta(conn, entry_id):
    row = conn.execute(
        "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry_id,)
    ).fetchone()
    return json.loads(row[0])


def _band_parents(conn):
    """束ねが作った親 (digest_origin='band') を作成順で返す。"""
    rows = conn.execute(
        "SELECT id FROM memopedia_pages WHERE category = 'chronicle' "
        "AND json_extract(metadata, '$.digest_origin') = 'band' "
        "ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    return [get_entry(conn, r[0]) for r in rows]


class BandTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)
        self.addCleanup(self.conn.close)


class TestFiring(BandTestBase):
    """発火 = 並びの字数合計 > 上限。"""

    def test_below_limit_no_action(self):
        # 8 × 600 = 4,800 ≤ 5,000 → 発火しない。dry も 0。
        for i in range(8):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        self.assertEqual(plan_band_overflow(self.conn), 0)
        client = _Client()
        self.assertEqual(run_band_overflow(self.conn, client), 0)
        self.assertEqual(client.calls, 0)

    def test_above_limit_folds_old_side_down_to_keep(self):
        # 9 × 600 = 5,400 > 5,000 → 発火。新しい側 ~2,400字 (4件) を残し、
        # 古い側 5 件が 1 個の親に畳まれる。
        entries = [
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
            for i in range(9)
        ]
        client = _Client()
        created = run_band_overflow(self.conn, client)
        self.assertEqual(created, 1)
        parents = _band_parents(self.conn)
        self.assertEqual(len(parents), 1)
        parent = parents[0]
        self.assertEqual(parent.level, 2)
        self.assertEqual(parent.source_ids, [e.id for e in entries[:5]])
        # 被覆の保存: 親の coverage = 子の合算。
        meta = _entry_meta(self.conn, parent.id)
        self.assertEqual(meta["coverage_chars"], 50_000)
        # 残った並びは予算内。
        self.assertEqual(plan_band_overflow(self.conn), 0)

    def test_coverage_does_not_affect_folding(self):
        """被覆がどれだけ極端に違っても判定に使われない (比率規則の廃止)。"""
        _entry(self.conn, start=1000, coverage=1)
        _entry(self.conn, start=1100, coverage=1_000_000)
        for i in range(7):
            _entry(self.conn, start=1200 + i * 100, coverage=10_000)
        self.assertEqual(run_band_overflow(self.conn, _Client()), 1)


class TestLevelSeparation(BandTestBase):
    """レベル分離 — 畳んだ結果は 1 つ上の並びへ行き、自分の並びに戻らない。"""

    def test_levels_fold_independently(self):
        # レベル1 と レベル2 がそれぞれ超過 → それぞれ 1 回ずつ畳まれ、
        # 親のレベルは fold元 + 1。
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000, level=1)
        for i in range(9):
            _entry(self.conn, start=100_000 + i * 100, coverage=100_000, level=2,
                   origin="batch")
        created = run_band_overflow(self.conn, _Client())
        self.assertEqual(created, 2)
        levels = sorted(p.level for p in _band_parents(self.conn))
        self.assertEqual(levels, [2, 3])

    def test_cascade_is_planned(self):
        """レベル1 の畳みがレベル2 を溢れさせる連鎖も dry 予測に入る。"""
        # レベル2 は上限直下 (9×540=4,860)、レベル1 が溢れて親 (~500字) が
        # 届くと 5,360 > 5,000 で連鎖する。
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000, level=1)
        for i in range(9):
            _entry(self.conn, start=100_000 + i * 100, coverage=100_000, level=2,
                   origin="band", chars=540)
        self.assertEqual(plan_band_overflow(self.conn), 2)


class TestMinimumMembers(BandTestBase):
    """2 件未満は畳まない (1 個を 1 個に要約し直すのは無意味)。"""

    def test_single_huge_node_is_not_refolded(self):
        _entry(self.conn, start=1000, coverage=10_000, chars=6_000)
        _entry(self.conn, start=2000, coverage=10_000, chars=100)
        # 超過しているが、残す量を確保すると畳み範囲が 1 件になる → 待つ。
        self.assertEqual(plan_band_overflow(self.conn), 0)
        self.assertEqual(run_band_overflow(self.conn, _Client()), 0)


class TestExcluded(BandTestBase):
    """excluded (提示中の圧縮区間) は字数に数えず、畳み範囲が跨がない。"""

    def test_excluded_splits_segments_and_rear_segment_still_folds(self):
        """excluded の手前が 1 件でも、後ろの過予算区間は独立に畳める —
        一時的な境界の手前 1 件がその後ろを永久に人質に取らない
        (Codex レビュー 2026-07-28 high3)。"""
        first = _entry(self.conn, start=1000, coverage=10_000)
        excluded_entry = _entry(self.conn, start=2000, coverage=10_000)
        rear = [
            _entry(self.conn, start=3000 + i * 100, coverage=10_000)
            for i in range(9)
        ]
        client = _Client()
        created = run_band_overflow(
            self.conn, client, excluded_entry_ids={excluded_entry.id},
        )
        # 先頭区間は 1 件 (< 2) なので畳めないが、excluded の後ろの区間が
        # 畳まれる。範囲は excluded を跨がない (first は材料に入らない)。
        self.assertEqual(created, 1)
        parents = _band_parents(self.conn)
        self.assertNotIn(first.id, parents[0].source_ids)
        self.assertNotIn(excluded_entry.id, parents[0].source_ids)
        self.assertEqual(parents[0].source_ids[0], rear[0].id)


class TestUncompiledGap(BandTestBase):
    """隣接ノード間の未編纂の生ログを跨いで畳まない (偽の隣接と孤児化の防止 —
    Codex レビュー 2026-07-28 high1)。"""

    def _eligible_message(self, created_at):
        from sai_memory.memory.storage import add_message
        return add_message(
            self.conn, "main", "user", "未編纂の取り残し", created_at=created_at,
        )

    def test_fold_does_not_span_uncompiled_gap(self):
        # 前半 2 件と後半 7 件の間に、どの一次あらすじにも入っていない
        # 編纂対象メッセージが居る。
        front = [
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
            for i in range(2)
        ]
        self._eligible_message(1500)
        rear = [
            _entry(self.conn, start=3000 + i * 100, coverage=10_000)
            for i in range(7)
        ]
        client = _Client()
        created = run_band_overflow(self.conn, client)
        self.assertEqual(created, 1)
        parent = _band_parents(self.conn)[0]
        ids = set(parent.source_ids)
        # 範囲はギャップを跨がない — 前半だけ、または後半だけ。
        self.assertTrue(
            ids <= {e.id for e in front} or ids <= {e.id for e in rear},
            parent.source_ids,
        )

    def test_compiled_messages_do_not_create_gap(self):
        # 全メッセージがいずれかの一次あらすじの source なら境界は立たない。
        from sai_memory.memory.storage import add_message
        mids = []
        for i in range(9):
            mid = add_message(
                self.conn, "main", "user", "本文", created_at=1000 + i * 100,
            )
            mids.append(mid)
            _entry(self.conn, start=1000 + i * 100, coverage=10_000,
                   source_ids=[mid])
        created = run_band_overflow(self.conn, _Client())
        self.assertEqual(created, 1)

    def test_upper_level_does_not_span_unconsolidated_lower_node(self):
        """レベル2 の並びは、間に居る未統合の一次あらすじを跨がない —
        跨ぐと後からその一次あらすじが上位親に内包されて孤児化する
        (Codex レビュー 2026-07-28 二巡 high1)。"""
        from sai_memory.arasuji.bands import _load_rows
        lv2 = [
            _entry(self.conn, start=i * 10_000, end=i * 10_000 + 5_000,
                   coverage=100_000, level=2, origin="batch")
            for i in range(9)
        ]
        # lv2[3] と lv2[4] の間に、まだ束なっていない一次あらすじが居る。
        straggler = _entry(
            self.conn, start=36_000, end=37_000, coverage=3_000, level=1,
        )
        created = run_band_overflow(self.conn, _Client())
        self.assertEqual(created, 1)
        parent = _band_parents(self.conn)[0]
        # 畳み範囲は境界の手前 (lv2[0..3]) — straggler の範囲を内包しない。
        self.assertEqual(parent.source_ids, [e.id for e in lv2[:4]])
        # straggler は孤児化せず、レベル1 の並びに残っている。
        rows = _load_rows(self.conn)
        lv1_ids = [i.entry.id for i in rows.get(1, [])]
        self.assertIn(straggler.id, lv1_ids)

    def test_same_second_later_message_is_not_a_false_gap(self):
        """境界の判定は正典順序 (created_at, rowid) — 同じ秒でも両ノードの
        source より後の rowid のメッセージは「間」ではない
        (Codex レビュー 2026-07-28 二巡 medium)。"""
        from sai_memory.arasuji.bands import _load_rows
        from sai_memory.memory.storage import add_message
        m_a = add_message(self.conn, "main", "user", "a", created_at=1000)
        m_b = add_message(self.conn, "main", "user", "b", created_at=1000)
        e1 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_a])
        e2 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_b])
        # 同じ秒だが rowid は m_b より後 = 正典順序では両ノードより新しい。
        add_message(self.conn, "main", "user", "後から来た未編纂", created_at=1000)
        rows = _load_rows(self.conn)
        row1 = rows[1]
        self.assertEqual([i.entry.id for i in row1], [e1.id, e2.id])
        self.assertFalse(row1[1].gap_before)

    def test_same_second_in_between_message_is_a_gap(self):
        """同じ秒でも rowid が両ノードの source の間なら境界が立つ。"""
        from sai_memory.arasuji.bands import _load_rows
        from sai_memory.memory.storage import add_message
        m_a = add_message(self.conn, "main", "user", "a", created_at=1000)
        add_message(self.conn, "main", "user", "間の未編纂", created_at=1000)
        m_b = add_message(self.conn, "main", "user", "b", created_at=1000)
        e1 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_a])
        e2 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_b])
        rows = _load_rows(self.conn)
        row1 = rows[1]
        self.assertEqual([i.entry.id for i in row1], [e1.id, e2.id])
        self.assertTrue(row1[1].gap_before)


class TestApprovedCallCap(BandTestBase):
    """実行は承認済みの dry 件数 (max_folds) を超えない
    (Codex レビュー 2026-07-28 high2)。"""

    def test_execution_stops_at_approved_count_even_if_cascade_grows(self):
        # レベル2 は 9×495=4,455 字。dry は親を 500 字と見込むので 4,955 ≤ 上限
        # = 連鎖なし (1 回) と予測するが、実際の LLM 出力が 600 字だと
        # レベル2 が 5,055 字になり実行は 2 回目を畳みたくなる。
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000, level=1)
        for i in range(9):
            _entry(self.conn, start=100_000 + i * 100, coverage=100_000, level=2,
                   origin="batch", chars=495)
        approved = plan_band_overflow(self.conn)
        self.assertEqual(approved, 1)
        client = _Client(response="ま" * 600)
        created = run_band_overflow(self.conn, client, max_folds=approved)
        # 承認 1 件で停止。積み残しは次回の dry が数え直す。
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        self.assertEqual(plan_band_overflow(self.conn), 1)


class TestAtomicity(BandTestBase):
    """原子性と並走防御。"""

    def test_llm_failure_creates_nothing(self):
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        created = run_band_overflow(self.conn, _Client(fail=True))
        self.assertEqual(created, 0)
        self.assertEqual(_band_parents(self.conn), [])
        # 子は誰も consolidated になっていない。
        row = self.conn.execute(
            "SELECT COUNT(*) FROM arasuji_entries WHERE is_consolidated = 1"
        ).fetchone()
        self.assertEqual(row[0], 0)

    def test_gap_inserted_during_llm_wait_abandons(self):
        """計画〜LLM 応答の間に畳み区間へ未統合の下位ノードが挿入されたら、
        確定 tx 内の再検査で放棄する (Codex レビュー 2026-07-28 三巡 high —
        跨ぎ親による恒久孤児化の TOCTOU)。"""
        from sai_memory.arasuji.bands import _load_rows
        lv2 = [
            _entry(self.conn, start=i * 10_000, end=i * 10_000 + 5_000,
                   coverage=100_000, level=2, origin="batch")
            for i in range(9)
        ]
        inserted = []

        class _RaceClient(_Client):
            def generate(inner, messages, tools):
                # LLM 応答待ちの間に、畳み区間 (lv2[0..4] のどこか) へ
                # 未統合の一次あらすじが挿入される状況を再現する。
                inserted.append(_entry(
                    self.conn, start=16_000, end=17_000, coverage=3_000,
                    level=1,
                ))
                return super(_RaceClient, inner).generate(messages, tools)

        created = run_band_overflow(self.conn, _RaceClient())
        self.assertEqual(created, 0)
        self.assertEqual(_band_parents(self.conn), [])
        # 挿入された一次あらすじは孤児化していない。
        rows = _load_rows(self.conn)
        self.assertIn(inserted[0].id, [i.entry.id for i in rows.get(1, [])])
        # 次の実行 (再計画) は境界を跨がずに畳める。
        created = run_band_overflow(self.conn, _Client())
        self.assertEqual(created, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [e.id for e in lv2[:2]])

    def test_same_level_node_inserted_during_llm_wait_abandons(self):
        """同一レベルの並走挿入も tx 内再検査で放棄する (四巡 high —
        下位レベル検査に掛からない挿入の TOCTOU)。"""
        from sai_memory.arasuji.bands import _load_rows
        lv2 = [
            _entry(self.conn, start=i * 10_000, end=i * 10_000 + 5_000,
                   coverage=100_000, level=2, origin="batch")
            for i in range(9)
        ]
        inserted = []

        class _RaceClient(_Client):
            def generate(inner, messages, tools):
                # 畳み区間の内側へ、同じレベル2 のノードが挿入される。
                inserted.append(_entry(
                    self.conn, start=16_000, end=17_000, coverage=50_000,
                    level=2, origin="batch",
                ))
                return super(_RaceClient, inner).generate(messages, tools)

        created = run_band_overflow(self.conn, _RaceClient())
        self.assertEqual(created, 0)
        self.assertEqual(_band_parents(self.conn), [])
        # 挿入されたノードは孤児化せず、レベル2 の並びに居る。
        rows = _load_rows(self.conn)
        self.assertIn(inserted[0].id, [i.entry.id for i in rows.get(2, [])])
        # 再計画では挿入ノードも並びの一員として普通に材料に入る。
        created = run_band_overflow(self.conn, _Client())
        self.assertEqual(created, 1)
        parent = _band_parents(self.conn)[0]
        self.assertIn(inserted[0].id, parent.source_ids)
        self.assertIn(lv2[0].id, parent.source_ids)

    def test_same_second_planned_siblings_are_not_intruders(self):
        """同じ秒に並ぶ計画済みの兄弟ノードを並走挿入と誤認しない —
        誤認すると状態が変わらないまま毎回 LLM 課金して放棄する永久停止に
        なる (五巡 high)。dry=1 かつ実行=1 を固定する。"""
        for _ in range(9):
            _entry(self.conn, start=1000, end=1000, coverage=10_000, level=2,
                   origin="batch")
        self.assertEqual(plan_band_overflow(self.conn), 1)
        client = _Client()
        created = run_band_overflow(self.conn, client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(_band_parents(self.conn)), 1)

    def test_children_consolidated_concurrently_abandons(self):
        entries = [
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
            for i in range(9)
        ]

        class _RaceClient(_Client):
            def generate(inner, messages, tools):
                # LLM 応答中に別ジョブが子を束ねた状況を再現する。
                from sai_memory.arasuji.storage import mark_consolidated
                mark_consolidated(self.conn, [entries[0].id], "someone-else")
                return super(_RaceClient, inner).generate(messages, tools)

        created = run_band_overflow(self.conn, _RaceClient())
        self.assertEqual(created, 0)
        self.assertEqual(_band_parents(self.conn), [])


class TestMaterialLabels(BandTestBase):
    """材料の種別明示 (intent §3-4) — 生ログ断片が混ざっても LLM に分かる形。"""

    def test_identity_child_is_labeled_as_fragment(self):
        _entry(self.conn, start=1000, coverage=900, origin="identity")
        for i in range(9):
            _entry(self.conn, start=2000 + i * 100, coverage=10_000)
        client = _Client()
        self.assertEqual(run_band_overflow(self.conn, client), 1)
        prompt = client.prompts[0]
        self.assertIn("【生ログ断片】", prompt)
        self.assertIn("【あらすじ】", prompt)
        self.assertIn("生ログ断片】は要約前の会話の断片", prompt)

    def test_normal_children_have_no_fragment_instruction(self):
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        client = _Client()
        self.assertEqual(run_band_overflow(self.conn, client), 1)
        prompt = client.prompts[0]
        self.assertNotIn("【生ログ断片】", prompt)


class TestFragmentCallback(BandTestBase):
    """Fragment 抽出 — 恒等圧縮の子 (旧世代データ) が要約に変わる瞬間に一度。"""

    def test_callback_fires_only_for_identity_children(self):
        from sai_memory.memory.storage import add_message
        mid = add_message(self.conn, "main", "user", "生ログ本文", created_at=900)
        _entry(self.conn, start=1000, coverage=900, origin="identity",
               source_ids=[mid])
        for i in range(9):
            _entry(self.conn, start=2000 + i * 100, coverage=10_000)
        seen = []
        created = run_band_overflow(
            self.conn, _Client(),
            batch_callback=lambda msgs, eid: seen.append((len(msgs), eid)),
        )
        self.assertEqual(created, 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 1)


class TestPlanProperties(unittest.TestCase):
    """一本規則の性質 (プロパティテスト — DB なしの純計画で検査)。

    intent §7 の不変条件を、ランダムな到着系列に対して固定する:
    1. 詰まらない — どんな大きさの混入があっても、静止状態の並びは
       「上限 + 端数 1 件」を超えて溜まらない。
    2. 再要約の有限性 — レベル数は到着総数の log でしか増えない。
    3. 被覆の保存 — 畳みは被覆を合算で引き継ぎ、総和が変わらない。
    """

    @staticmethod
    def _simulate(arrivals):
        """到着列 (chars, coverage) を新規則で最後まで流し、rows を返す。"""
        rows = {1: []}
        t = 0
        for chars, coverage in arrivals:
            t += 1
            rows.setdefault(1, []).append(_RowItem(
                coverage=coverage, chars=chars, start_time=t, end_time=t,
            ))
            # _plan_folds は rows を破壊的に更新して畳みを適用した後の姿にする
            # (親は EST_PARENT_CHARS の模擬ノードとして上の並びへ入る)。
            folds = _plan_folds(rows)
            for fold in folds:
                assert len(fold.items) >= 2
        return rows

    def test_no_stall_and_bounded_rows(self):
        rng = random.Random(20260728)
        arrivals = [
            (rng.randint(1, 2_000), rng.randint(1, 100_000)) for _ in range(600)
        ]
        rows = self._simulate(arrivals)
        for level, row in rows.items():
            chars = sum(i.chars for i in row)
            # 静止状態: 上限 + 直近 1 件ぶんの余裕を超えて溜まらない。
            self.assertLessEqual(
                chars, BAND_CHAR_LIMIT + 2_000,
                f"level {level} is stalled with {chars} chars",
            )

    def test_levels_grow_logarithmically(self):
        arrivals = [(500, 10_000)] * 1_000
        rows = self._simulate(arrivals)
        # 1,000 到着 (各500字) で総量 50万字。1 段 ≈ 5〜10 倍の縮約なので
        # レベルは高々 5 — 線形に増えたら分離が壊れている。
        self.assertLessEqual(max(rows.keys()), 5)

    def test_coverage_is_preserved(self):
        rng = random.Random(7)
        arrivals = [
            (rng.randint(1, 2_000), rng.randint(1, 50_000)) for _ in range(300)
        ]
        rows = self._simulate(arrivals)
        total = sum(i.coverage for row in rows.values() for i in row)
        self.assertEqual(total, sum(cov for _, cov in arrivals))

    def test_keep_amount_buffers_firings(self):
        """発火は「たまに・まとめて」— 残す量のバッファがあるので、500字の
        到着 1 件ごとに毎回発火することはない。"""
        rows = {1: []}
        folds_at = []
        for t in range(40):
            rows.setdefault(1, []).append(_RowItem(
                coverage=10_000, chars=500, start_time=t, end_time=t,
            ))
            if _plan_folds(rows):
                folds_at.append(t)
        self.assertGreaterEqual(len(folds_at), 2)
        gaps = [b - a for a, b in zip(folds_at, folds_at[1:])]
        # バッファ = (上限 - 残す量) = 2,500字 ≈ 5 件ぶんの到着間隔。
        self.assertTrue(all(g >= (BAND_CHAR_LIMIT - BAND_CHAR_KEEP) // 500
                            for g in gaps), gaps)


class TestBackfillCoverage(BandTestBase):
    """coverage_chars 帰化バックフィル (旧ファイルからの回帰維持)。"""

    def test_backfill_level1_from_sources(self):
        from sai_memory.memory.storage import add_message
        m1 = add_message(self.conn, "main", "user", "あ" * 120, created_at=100)
        m2 = add_message(self.conn, "main", "assistant", "い" * 80, created_at=101)
        entry = create_entry(
            self.conn, level=1, content="要約", source_ids=[m1, m2],
            start_time=100, end_time=101, source_count=2, message_count=2,
        )
        filled = backfill_coverage(self.conn)
        self.assertEqual(filled, 1)
        meta = _entry_meta(self.conn, entry.id)
        self.assertEqual(meta["coverage_chars"], 200)
        self.assertNotIn("coverage_estimated", meta)

    def test_backfill_parent_sums_children(self):
        from sai_memory.memory.storage import add_message
        mids = [
            add_message(self.conn, "main", "user", "あ" * 100, created_at=100 + i)
            for i in range(2)
        ]
        c1 = create_entry(
            self.conn, level=1, content="子1", source_ids=[mids[0]],
            start_time=100, end_time=100, source_count=1, message_count=1,
        )
        c2 = create_entry(
            self.conn, level=1, content="子2", source_ids=[mids[1]],
            start_time=101, end_time=101, source_count=1, message_count=1,
        )
        parent = create_entry(
            self.conn, level=2, content="親", source_ids=[c1.id, c2.id],
            start_time=100, end_time=101, source_count=2, message_count=2,
        )
        backfill_coverage(self.conn)
        meta = _entry_meta(self.conn, parent.id)
        self.assertEqual(meta["coverage_chars"], 200)

    def test_backfill_missing_source_estimates(self):
        entry = create_entry(
            self.conn, level=1, content="要約テキスト", source_ids=["gone-1"],
            start_time=100, end_time=101, source_count=1, message_count=1,
        )
        backfill_coverage(self.conn)
        meta = _entry_meta(self.conn, entry.id)
        self.assertEqual(meta["coverage_chars"], len("要約テキスト") * 10)
        self.assertTrue(meta["coverage_estimated"])

    def test_backfill_is_idempotent(self):
        from sai_memory.memory.storage import add_message
        mid = add_message(self.conn, "main", "user", "あ" * 50, created_at=100)
        create_entry(
            self.conn, level=1, content="要約", source_ids=[mid],
            start_time=100, end_time=100, source_count=1, message_count=1,
        )
        self.assertEqual(backfill_coverage(self.conn), 1)
        self.assertEqual(backfill_coverage(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
