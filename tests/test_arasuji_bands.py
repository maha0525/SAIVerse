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
    FOLD_MATERIAL_CHAR_LIMIT,
    _any_level_over_limit,
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

    def test_empty_llm_response_is_retried_before_giving_up(self):
        """束ねの LLM が空応答を 2 回返しても 3 回目の本文で親が確定する
        (generator.generate_text_with_empty_retry、2026-09-03)。"""

        class _EmptyTwiceClient(_Client):
            def generate(inner, messages, tools):
                inner.calls += 1
                inner.prompts.append(messages[0]["content"])
                return "" if inner.calls <= 2 else "三度目の統合まとめ。"

        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        client = _EmptyTwiceClient()
        self.assertEqual(run_band_overflow(self.conn, client), 1)
        self.assertEqual(client.calls, 3)
        parents = _band_parents(self.conn)
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0].content, "三度目の統合まとめ。")

    def test_rate_limit_propagates_instead_of_being_folded_into_none(self):
        """レート制限は None に丸めず送出する (2026-09-09 Codex 指摘)。

        他の LLM 失敗と同じく None に落としていた頃は、呼び出し元 (Metabolism の
        束ね) が「1 回失敗しただけ」として残り予算のぶん呼び直し、429 の最中に
        承認件数ぶんの課金試行を撃っていた。送出することで呼び出し元が走行を
        閉じて小休止を置ける。試行の勘定 (attempts) は他の失敗と同じく 1。
        """
        from llm_clients.exceptions import RateLimitError

        class _RateLimitedClient(_Client):
            def generate(inner, messages, tools):
                inner.calls += 1
                raise RateLimitError("429 tokens per min")

        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        client = _RateLimitedClient()
        stats = {}
        with self.assertRaises(RateLimitError):
            run_band_overflow(self.conn, client, stats=stats)
        self.assertEqual(client.calls, 1)  # 呼び直しの巡には入らない
        self.assertEqual(stats.get("attempts"), 1)
        self.assertEqual(_band_parents(self.conn), [])  # 親は確定していない

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


class TestFoldMaterialCap(BandTestBase):
    """畳み 1 回の材料上限 (FOLD_MATERIAL_CHAR_LIMIT = U/2、2026-09-08 裁定)。

    恒常運転 (あふれたらすぐ畳む) では区間が小さく効かないが、全量補修・
    大量インポートの一括編纂では無境界の区間が巨大になる。上限なしだと
    数十本を 1 個の親に握らせる粗い上位あらすじができ、dry 計画も
    「巨大区間 = 畳み 1 回」と数えて補修の束ね予算が枯渇していた
    (2026-09-08 実機で確認、W4 移行が旧 consolidation_size=10 の「量の上限」
    の役割を引き継ぎ損ねていた回帰)。
    """

    @staticmethod
    def _pure_row(n, chars):
        """DB なしの純計画用の並び (レベル1)。"""
        return {1: [
            _RowItem(coverage=10_000, chars=chars,
                     start_time=1000 + i * 100, end_time=1000 + i * 100 + 99)
            for i in range(n)
        ]}

    def test_long_unbounded_run_splits_into_capped_folds(self):
        # 500 字 × 30 本 = 15,000 字の無境界区間。旧仕様は「残す量より古い側」
        # 25 本を丸ごと 1 fold にしたが、新仕様は材料合計が上限 (U/2 = 5,000)
        # 以下の複数 fold に割れる。
        rows = self._pure_row(30, 500)
        folds = _plan_folds(rows)
        self.assertGreaterEqual(len(folds), 2, "巨大区間が 1 fold に握られている")
        for fold in folds:
            self.assertGreaterEqual(len(fold.items), 2)
            self.assertLessEqual(
                sum(i.chars for i in fold.items), FOLD_MATERIAL_CHAR_LIMIT,
            )

    def test_two_oversized_heads_still_fold_as_a_pair(self):
        # 先頭 2 本だけで上限を超える (3,000 + 3,000 = 6,000 > 5,000) ケース
        # でも 2 本は畳む — 2 本未満に切ると過大な子が永久に畳まれず滞留する。
        rows = {1: []}
        for i, chars in enumerate([3_000, 3_000, 600, 600, 600, 600]):
            rows[1].append(_RowItem(
                coverage=10_000, chars=chars,
                start_time=1000 + i * 100, end_time=1000 + i * 100 + 99,
            ))
        folds = _plan_folds(rows)
        self.assertGreaterEqual(len(folds), 1)
        first = folds[0]
        self.assertEqual(len(first.items), 2)
        self.assertEqual(sum(i.chars for i in first.items), 6_000)

    def test_dry_count_matches_execution_count_on_bulk_backlog(self):
        # 予算枯渇の回帰: dry (plan_band_overflow) と実行の畳み総数が同じ入力で
        # 一致する。LLM 出力を見込み (EST_PARENT_CHARS=500) と同じ長さにして、
        # generate_chronicle と同じく承認件数を予算に繰り返し呼ぶ。
        for i in range(30):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000, chars=500)
        approved = plan_band_overflow(self.conn)
        self.assertGreaterEqual(approved, 2, "dry が巨大区間を 1 回と数えている")
        client = _Client(response="ま" * EST_PARENT_CHARS)
        total = 0
        while total < approved:
            created = run_band_overflow(
                self.conn, client, max_folds=approved - total,
            )
            if created == 0:
                break
            total += created
        self.assertEqual(total, approved)
        # 予算内で帯は静止している (肥大したまま予算切れにならない)。
        self.assertEqual(plan_band_overflow(self.conn), 0)


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
    """穴 (未編纂の生ログ) と未統合の下位ノードの扱い。

    2026-09-08 の設計変更 (docs/intent/chronicle_coverage_gaps.md 機構 A・B)
    で仕様が変わった: 穴は畳みの境界ではなくなり (統合は穴を跨ぐ)、代わりに
    材料へ不明期間の一行として明示される。未統合の下位ノードの境界だけが残る
    (順に畳めば解消する一時状態のため)。旧テスト
    ``test_fold_does_not_span_uncompiled_gap`` (穴で切れることの固定) は
    新仕様の検証 (跨いで一つに計画 + 注記) へ書き換えた。"""

    def _eligible_message(self, created_at):
        from sai_memory.memory.storage import add_message
        return add_message(
            self.conn, "main", "user", "未編纂の取り残し", created_at=created_at,
        )

    def test_fold_spans_uncompiled_hole_as_one(self):
        # 前半 2 件と後半 7 件の間に、どの一次あらすじにも入っていない
        # 編纂対象メッセージ (穴) が居る。機構 A: 畳み範囲は穴を跨いで
        # 一つに計画される (旧仕様は穴の手前で切れて帯が肥大した)。
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
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        # 古い側 5 件 = 前半 2 件 + 後半 3 件 — 穴を跨いだ一つの畳み。
        self.assertEqual(
            parent.source_ids,
            [e.id for e in front] + [e.id for e in rear[:3]],
        )

    def test_hole_note_is_in_the_material_with_count_and_period(self):
        # 機構 B: 材料組成の穴の位置に、件数と期間つきの決定論の一行が挟まり、
        # 地続きに繋げない指示が足される (本文へ「不明」と書けとは指示しない)。
        from sai_memory.arasuji.generator import _format_timestamp
        for i in range(2):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        self._eligible_message(1500)
        self._eligible_message(2500)
        for i in range(7):
            _entry(self.conn, start=3000 + i * 100, coverage=10_000)
        client = _Client()
        self.assertEqual(run_band_overflow(self.conn, client), 1)
        prompt = client.prompts[0]
        self.assertIn(
            "(この間に、まだあらすじになっていない記録が 2 件ある: "
            f"期間 {_format_timestamp(1500)}〜{_format_timestamp(2500)})",
            prompt,
        )
        self.assertIn("地続きの出来事として繋げない", prompt)
        self.assertNotIn("不明」と書", prompt)

    def test_compiled_messages_do_not_create_hole_note(self):
        # 全メッセージがいずれかの一次あらすじの source なら穴は無く、
        # 注記も指示も入らない。
        from sai_memory.memory.storage import add_message
        mids = []
        for i in range(9):
            mid = add_message(
                self.conn, "main", "user", "本文", created_at=1000 + i * 100,
            )
            mids.append(mid)
            _entry(self.conn, start=1000 + i * 100, coverage=10_000,
                   source_ids=[mid])
        client = _Client()
        created = run_band_overflow(self.conn, client)
        self.assertEqual(created, 1)
        self.assertNotIn("まだあらすじになっていない記録", client.prompts[0])
        self.assertNotIn("地続きの出来事として繋げない", client.prompts[0])

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

    def test_same_second_later_message_is_not_a_false_hole(self):
        """穴の判定は正典順序 (created_at, rowid) — 同じ秒でも両ノードの
        source より後の rowid のメッセージは「間」ではない
        (Codex レビュー 2026-07-28 二巡 medium。境界の判定から穴の検出
        (_hole_between、機構 B) へ移した後も同じ精度を保つ)。"""
        from sai_memory.arasuji.bands import _hole_between
        from sai_memory.memory.storage import add_message
        m_a = add_message(self.conn, "main", "user", "a", created_at=1000)
        m_b = add_message(self.conn, "main", "user", "b", created_at=1000)
        e1 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_a])
        e2 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_b])
        # 同じ秒だが rowid は m_b より後 = 正典順序では両ノードより新しい。
        add_message(self.conn, "main", "user", "後から来た未編纂", created_at=1000)
        self.assertIsNone(_hole_between(self.conn, e1, e2))

    def test_same_second_in_between_message_is_a_hole(self):
        """同じ秒でも rowid が両ノードの source の間なら穴として数えられる。"""
        from sai_memory.arasuji.bands import _hole_between
        from sai_memory.memory.storage import add_message
        m_a = add_message(self.conn, "main", "user", "a", created_at=1000)
        add_message(self.conn, "main", "user", "間の未編纂", created_at=1000)
        m_b = add_message(self.conn, "main", "user", "b", created_at=1000)
        e1 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_a])
        e2 = _entry(self.conn, start=1000, end=1000, coverage=5_000,
                    source_ids=[m_b])
        self.assertEqual(_hole_between(self.conn, e1, e2), (1, 1000, 1000))


class TestOrphanReconnection(BandTestBase):
    """後から埋まった穴の繋ぎ直し (chronicle_coverage_gaps 機構 C、2026-09-08)。

    上位あらすじの被覆期間に内包された未統合エントリは、孤児として並びから
    除外するのではなく、内包する最下層の上位の子として繋ぎ、上位から最上位
    までを content_stale に回す (語り直しは既存の stale flush に乗せる)。"""

    def test_contained_lv1_reconnects_into_lowest_upper_and_marks_stale(self):
        from sai_memory.arasuji.bands import reconnect_contained_orphans
        from sai_memory.arasuji.storage import mark_consolidated
        c1 = _entry(self.conn, start=0, end=2_000, coverage=10_000)
        c2 = _entry(self.conn, start=8_000, end=10_000, coverage=10_000)
        lv2 = _entry(self.conn, start=0, end=10_000, coverage=20_000, level=2,
                     origin="band", source_ids=[c1.id, c2.id])
        lv3 = _entry(self.conn, start=0, end=50_000, coverage=40_000, level=3,
                     origin="band", source_ids=[lv2.id])
        mark_consolidated(self.conn, [c1.id, c2.id], lv2.id)
        mark_consolidated(self.conn, [lv2.id], lv3.id)
        # 後から埋まった穴の一次あらすじ — lv2 (と lv3) の被覆期間に内包。
        late = _entry(self.conn, start=3_000, end=4_000, coverage=1_000)
        reconnected = reconnect_contained_orphans(self.conn)
        self.assertEqual(reconnected, [late.id])
        # 繋ぎ先は内包する最下層の上位 = lv2 (lv3 ではない)。
        lv2_after = get_entry(self.conn, lv2.id)
        self.assertIn(late.id, lv2_after.source_ids)
        late_after = get_entry(self.conn, late.id)
        self.assertTrue(late_after.is_consolidated)
        self.assertEqual(late_after.parent_id, lv2.id)
        # 上位から最上位まで stale — 次の flush が語り直す。
        self.assertEqual(_entry_meta(self.conn, lv2.id).get("content_stale"), 1)
        self.assertEqual(_entry_meta(self.conn, lv3.id).get("content_stale"), 1)
        # 冪等 — もう孤児は居ない。
        self.assertEqual(reconnect_contained_orphans(self.conn), [])

    def test_shrunken_parent_does_not_adopt_the_next_orphan_in_the_same_run(self):
        """1 パスにつき縁組 1 件 + 毎パス読み直し (Codex 三巡目): 縁組後の帳簿の
        引き直しで親の期間は実子の合算まで**縮み**うる。同じ回の次の孤児を古い
        スナップショットで判定すると、縮んだ期間の外の孤児を誤って縁組する。"""
        from sai_memory.arasuji.bands import reconnect_contained_orphans
        from sai_memory.arasuji.storage import mark_consolidated
        c1 = _entry(self.conn, start=0, end=2_000, coverage=10_000)
        # 宣言期間 0..10,000 だが実子は c1 (0..2,000) だけの親。
        parent = _entry(self.conn, start=0, end=10_000, coverage=20_000, level=2,
                        origin="band", source_ids=[c1.id])
        mark_consolidated(self.conn, [c1.id], parent.id)
        # 孤児 Y (3,000..4,000): 縁組されると引き直しで親の期間は 0..4,000 に縮む。
        orphan_y = _entry(self.conn, start=3_000, end=4_000, coverage=1_000)
        # 孤児 Z (8,000..9,000): 旧宣言では内包、縮んだ期間では外。
        orphan_z = _entry(self.conn, start=8_000, end=9_000, coverage=1_000)

        reconnected = reconnect_contained_orphans(self.conn)

        self.assertEqual(reconnected, [orphan_y.id])
        parent_after = get_entry(self.conn, parent.id)
        self.assertEqual(parent_after.end_time, 4_000)  # 縮みの検算
        z_after = get_entry(self.conn, orphan_z.id)
        self.assertFalse(z_after.is_consolidated)  # 誤縁組されない
        self.assertNotIn(orphan_z.id, parent_after.source_ids)

    def test_refresh_failure_before_adoption_keeps_child_unconsolidated(self):
        """書き込み順の fail-safe: stale (refresh_ancestor_bookkeeping) が先、
        縁組 (add_to_parent_source_ids) が後。refresh が失敗したら縁組は行われず、
        子は未統合のまま次回の走査対象に残る — 逆順だと、子は並びから消えたのに
        上位が永久に古いまま残る (次回の走査は is_consolidated=0 しか見ない)。"""
        from unittest.mock import patch
        from sai_memory.arasuji.bands import reconnect_contained_orphans
        from sai_memory.arasuji.storage import mark_consolidated
        c1 = _entry(self.conn, start=0, end=2_000, coverage=10_000)
        lv2 = _entry(self.conn, start=0, end=10_000, coverage=10_000, level=2,
                     origin="band", source_ids=[c1.id])
        mark_consolidated(self.conn, [c1.id], lv2.id)
        late = _entry(self.conn, start=3_000, end=4_000, coverage=1_000)
        with patch(
            "sai_memory.arasuji.storage.refresh_ancestor_bookkeeping",
            side_effect=RuntimeError("bookkeeping down"),
        ):
            with self.assertRaises(RuntimeError):
                reconnect_contained_orphans(self.conn)
        # 縁組は行われていない — 子は並びに残り、親の source_ids も無傷。
        self.assertFalse(get_entry(self.conn, late.id).is_consolidated)
        self.assertNotIn(late.id, get_entry(self.conn, lv2.id).source_ids)
        # 帳簿が直った次回の走査が拾い直す。
        self.assertEqual(reconnect_contained_orphans(self.conn), [late.id])
        self.assertTrue(get_entry(self.conn, late.id).is_consolidated)

    def test_run_band_overflow_reconnects_even_without_folds(self):
        """繋ぎ直しの実行場所は統合の実行経路 (run_band_overflow の冒頭) —
        予算超過が無い回でも保守ステップとして走る。"""
        from sai_memory.arasuji.storage import mark_consolidated
        c1 = _entry(self.conn, start=0, end=2_000, coverage=10_000)
        lv2 = _entry(self.conn, start=0, end=10_000, coverage=10_000, level=2,
                     origin="band", source_ids=[c1.id])
        mark_consolidated(self.conn, [c1.id], lv2.id)
        late = _entry(self.conn, start=3_000, end=4_000, coverage=1_000)
        client = _Client()
        self.assertEqual(run_band_overflow(self.conn, client), 0)
        self.assertEqual(client.calls, 0)
        self.assertTrue(get_entry(self.conn, late.id).is_consolidated)
        self.assertIn(late.id, get_entry(self.conn, lv2.id).source_ids)

    def test_reconnect_failure_sets_the_incomplete_marker(self):
        """繋ぎ直しの例外は握り潰したまま完了の顔をしない (2026-09-08) —
        未完了の印を立てて帯の「前回の処理が完了していません」に乗せる。
        統合そのものは従来どおり続行する (途中で止めない — 印だけ)。"""
        from unittest.mock import patch
        from sai_memory.arasuji.absorption import is_repair_incomplete
        for i in range(9):  # 9 × 600 = 5,400 > 5,000 → 発火する
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        client = _Client()
        with patch(
            "sai_memory.arasuji.bands.reconnect_contained_orphans",
            side_effect=RuntimeError("reconnect down"),
        ):
            created = run_band_overflow(self.conn, client)
        self.assertEqual(created, 1)  # 統合は止まらない
        self.assertTrue(is_repair_incomplete(self.conn))

    def test_reconnect_marker_survives_absorption_no_work(self):
        """reconnect 失敗の印は吸収の未完了の印と別の理由 (Codex 二巡採用 1) —
        次回の run_absorption が仕事なし (no_work) で「完了」と判定しても、
        消えるのは吸収の印だけで、reconnect の残債は帯の促しに残り続ける。
        消えるのは reconnect が例外なく走り切った回 (run_band_overflow 冒頭の
        保守ステップ)。"""
        from unittest.mock import patch
        from sai_memory.arasuji.absorption import (
            is_repair_incomplete,
            run_absorption,
        )
        client = _Client()
        with patch(
            "sai_memory.arasuji.bands.reconnect_contained_orphans",
            side_effect=RuntimeError("reconnect down"),
        ):
            run_band_overflow(self.conn, client)
        self.assertTrue(is_repair_incomplete(self.conn))
        # 吸収の no_work は自分の理由の印だけを外す — reconnect の印は残る。
        run_absorption(self.conn, client, None)
        self.assertTrue(is_repair_incomplete(self.conn))
        # reconnect が走り切った回 (畳みゼロでも保守ステップは走る) に消える。
        self.assertEqual(run_band_overflow(self.conn, client), 0)
        self.assertFalse(is_repair_incomplete(self.conn))

    def test_clean_reconnect_run_does_not_clear_the_absorption_marker(self):
        """逆方向の検算: reconnect が走り切っても、吸収の未完了の印 (別の
        理由の残債) は消えない — 消してよいのは run_absorption だけ。"""
        from sai_memory.arasuji.absorption import (
            is_repair_incomplete,
            set_repair_incomplete,
        )
        set_repair_incomplete(self.conn)  # 吸収の未完了 (reason 既定値)
        self.assertEqual(run_band_overflow(self.conn, _Client()), 0)
        self.assertTrue(is_repair_incomplete(self.conn))

    def test_adoption_refreshes_parent_bookkeeping_immediately(self):
        """縁組成功の直後に親の帳簿を引き直す (Codex 二巡採用 2)。縁組
        (add_to_parent_source_ids) は source_ids / parent_id / is_consolidated
        しか書かないので、引き直さないと親の期間・件数系は次の stale flush
        まで新しい子を含まない — その間、診断や次の繋ぎ直しの内包判定が
        古い期間で行われる。"""
        from sai_memory.arasuji.bands import reconnect_contained_orphans
        from sai_memory.arasuji.storage import mark_consolidated
        c1 = _entry(self.conn, start=0, end=2_000, coverage=10_000)
        lv2 = _entry(self.conn, start=0, end=10_000, coverage=10_000, level=2,
                     origin="band", source_ids=[c1.id])
        mark_consolidated(self.conn, [c1.id], lv2.id)
        late = _entry(self.conn, start=3_000, end=4_000, coverage=1_000)
        self.assertEqual(reconnect_contained_orphans(self.conn), [late.id])
        # 期間は生存子 (c1 + late) の合算 — 新しい子 (end=4,000) を含む。
        lv2_after = get_entry(self.conn, lv2.id)
        self.assertEqual(lv2_after.start_time, 0)
        self.assertEqual(lv2_after.end_time, 4_000)
        meta = _entry_meta(self.conn, lv2.id)
        self.assertEqual(meta.get("source_count"), 2)
        self.assertEqual(meta.get("message_count"), 2)

    def test_deleted_container_does_not_orphan_the_entry(self):
        """孤児判定の親候補は繋ぎ直しの親候補と同じ集合 (勘定の一致、
        2026-09-08) — 削除済みページに内包されただけのエントリを孤児として
        並びから外すと、繋ぎ直し (削除済みを親にできない) が永久に回収できず、
        発火の前検査だけが数え続ける空振りになる。"""
        from sai_memory.arasuji.bands import (
            _load_rows,
            reconnect_contained_orphans,
        )
        from sai_memory.arasuji.storage import mark_consolidated
        c1 = _entry(self.conn, start=0, end=2_000, coverage=10_000)
        lv2 = _entry(self.conn, start=0, end=10_000, coverage=10_000, level=2,
                     origin="band", source_ids=[c1.id])
        mark_consolidated(self.conn, [c1.id], lv2.id)
        late = _entry(self.conn, start=3_000, end=4_000, coverage=1_000)
        self.conn.execute(
            "UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?",
            (lv2.id,),
        )
        self.conn.commit()
        # 繋ぎ直しは削除済みを親にしない
        self.assertEqual(reconnect_contained_orphans(self.conn), [])
        # だから並びからも外さない — 通常の統合対象として立つ
        rows = _load_rows(self.conn)
        self.assertIn(late.id, [i.entry.id for i in rows.get(1, [])])

    def test_same_level_containment_is_not_orphaned(self):
        """同レベルの entry に内包されるだけのもの (期間 0 秒の一括インポート
        産 Lv1 等) は孤児ではなく、通常の並びに立つ。繋ぎ直しの対象でもない
        (重複被覆は別課題 — docs/issues/lv1_source_ids_duplicates_and_orphans.md)。"""
        from sai_memory.arasuji.bands import (
            _load_rows,
            reconnect_contained_orphans,
        )
        big = _entry(self.conn, start=1_000, end=5_000, coverage=10_000)
        small = _entry(self.conn, start=2_000, end=3_000, coverage=1_000)
        rows = _load_rows(self.conn)
        ids = [i.entry.id for i in rows.get(1, [])]
        self.assertIn(big.id, ids)
        self.assertIn(small.id, ids)
        self.assertEqual(reconnect_contained_orphans(self.conn), [])
        self.assertFalse(get_entry(self.conn, small.id).is_consolidated)

    def test_same_level_contained_entry_still_folds_normally(self):
        """同レベル内包のエントリが並びに戻っても統合は壊れない — 発火すれば
        普通に材料へ入って畳まれる。"""
        big = _entry(self.conn, start=1_000, end=5_000, coverage=10_000)
        small = _entry(self.conn, start=2_000, end=3_000, coverage=1_000)
        for i in range(7):
            _entry(self.conn, start=6_000 + i * 100, coverage=10_000)
        client = _Client()
        self.assertEqual(run_band_overflow(self.conn, client), 1)
        parent = _band_parents(self.conn)[0]
        self.assertIn(big.id, parent.source_ids)
        self.assertIn(small.id, parent.source_ids)


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


class TestProgressCallback(BandTestBase):
    """progress_callback (2026-09-03): 畳み 1 件ごとに (done, total) で呼ぶ。
    呼び出し元が画面の進捗と実行台帳の心拍に使う。"""

    def test_called_once_per_fold_with_done_and_total(self):
        # レベル1・レベル2 がそれぞれ超過 → 2 畳み (上限 = 既定 3)。
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000, level=1)
        for i in range(9):
            _entry(self.conn, start=100_000 + i * 100, coverage=100_000, level=2,
                   origin="batch")
        seen = []
        created = run_band_overflow(
            self.conn, _Client(), max_folds=3,
            progress_callback=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual(created, 2)
        self.assertEqual(seen, [(1, 3), (2, 3)])

    def test_not_called_when_nothing_folds(self):
        for i in range(8):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        seen = []
        self.assertEqual(run_band_overflow(
            self.conn, _Client(),
            progress_callback=lambda done, total: seen.append((done, total)),
        ), 0)
        self.assertEqual(seen, [])

    def test_raising_callback_keeps_the_fold_count_and_continues(self):
        """callback (画面へのイベント送出) が失敗しても、確定済みの畳みは
        戻り値に数え、次の畳みへ進む。ここで例外が抜けると呼び出し元の累計
        (承認済み予算の消化) がずれ、次の呼び出しに過大な max_folds が渡る。"""
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000, level=1)
        for i in range(9):
            _entry(self.conn, start=100_000 + i * 100, coverage=100_000, level=2,
                   origin="batch")
        seen = []

        def bad_progress(done, total):
            seen.append((done, total))
            raise RuntimeError("ui event emitter down")

        client = _Client()
        with self.assertLogs("sai_memory.arasuji.bands", level="WARNING") as logs:
            created = run_band_overflow(
                self.conn, client, max_folds=3, progress_callback=bad_progress,
            )
        # 2 畳みとも確定して数えられ、callback は畳みごとに呼ばれている。
        self.assertEqual(created, 2)
        self.assertEqual(client.calls, 2)
        self.assertEqual(seen, [(1, 3), (2, 3)])
        self.assertEqual(len(_band_parents(self.conn)), 2)
        self.assertTrue(any("progress callback failed" in m for m in logs.output))


class TestCheapPrecheck(BandTestBase):
    """_any_level_over_limit (2026-09-03): チャンク確定ごとに呼ばれるように
    なった run_band_overflow が、超過の無い回を 1 クエリで抜けるための門。
    見落とし (False なのに畳みがある) が無いことが契約。"""

    def test_under_limit_skips_the_full_scan(self):
        from unittest.mock import patch
        for i in range(8):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        with patch("sai_memory.arasuji.bands._load_rows",
                   side_effect=AssertionError("full scan must not run")):
            self.assertEqual(run_band_overflow(self.conn, _Client()), 0)

    def test_over_limit_still_folds(self):
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        self.assertTrue(_any_level_over_limit(self.conn))
        self.assertEqual(run_band_overflow(self.conn, _Client()), 1)

    def test_excluded_entries_do_not_count(self):
        ids = [
            _entry(self.conn, start=1000 + i * 100, coverage=10_000).id
            for i in range(9)
        ]
        # 9 × 600 = 5,400 > 5,000 だが 1 件を提示中 (excluded) にすると 4,800。
        self.assertTrue(_any_level_over_limit(self.conn))
        self.assertFalse(_any_level_over_limit(self.conn, {ids[0]}))

    def test_precheck_never_says_no_when_plan_would_fold(self):
        """前検査の合計は計画の判定値以上 (孤児を除かない) — 計画が畳むなら
        前検査も必ず True。ランダムな並びで検算する。"""
        rng = random.Random(7)
        for trial in range(20):
            conn = init_db(":memory:")
            init_arasuji_tables(conn)
            n = rng.randint(1, 14)
            for i in range(n):
                _entry(conn, start=1000 + i * 100, coverage=1_000,
                       chars=rng.randint(100, 900))
            planned = plan_band_overflow(conn)
            if planned > 0:
                self.assertTrue(_any_level_over_limit(conn), f"trial {trial}")
            conn.close()


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

    def test_stats_count_the_failed_attempt(self):
        """stats (2026-09-03): LLM が落ちた畳みは created=0 でも attempts=1 —
        呼び出し元 (generate_chronicle) は承認予算を試行回数で消費する。"""
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        stats = {}
        created = run_band_overflow(self.conn, _Client(fail=True), stats=stats)
        self.assertEqual(created, 0)
        self.assertEqual(stats, {"attempts": 1, "created": 0})

    def test_stats_count_the_successful_attempt(self):
        for i in range(9):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        stats = {}
        created = run_band_overflow(self.conn, _Client(), stats=stats)
        self.assertEqual(created, 1)
        self.assertEqual(stats, {"attempts": 1, "created": 1})

    def test_stats_are_zero_when_nothing_folds(self):
        for i in range(8):
            _entry(self.conn, start=1000 + i * 100, coverage=10_000)
        stats = {}
        self.assertEqual(run_band_overflow(self.conn, _Client(), stats=stats), 0)
        self.assertEqual(stats, {"attempts": 0, "created": 0})

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

    def test_missing_source_messages_are_a_failure_not_a_partial_success(self):
        """⭐ 元メッセージが欠けていたら、残った分だけ抽出して成功にしない。

        束ねが確定した子は「初めて要約に変わる瞬間」を二度と迎えない。部分的な
        抽出を成功として通すと、欠けた分の知識が抽出済みの顔で落ちる。
        """
        from sai_memory.memory.storage import add_message
        mid = add_message(self.conn, "main", "user", "生ログ本文", created_at=900)
        _entry(self.conn, start=1000, coverage=900, origin="identity",
               source_ids=[mid, "消えたメッセージ"])
        for i in range(9):
            _entry(self.conn, start=2000 + i * 100, coverage=10_000)

        seen = []
        failures = []
        created = run_band_overflow(
            self.conn, _Client(),
            batch_callback=lambda msgs, eid: seen.append(eid),
            extraction_failures=failures,
        )
        self.assertEqual(created, 1, "束ね自体は成立する")
        self.assertEqual(seen, [], "欠けたまま抽出を走らせている")
        self.assertEqual(len(failures), 1, "失敗として記録されていない")
        # 付箋にも載る — 拾い直しが「辿れない」として正直に片づける
        backlog = self.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(backlog, 1)

    def test_duplicated_source_ids_are_not_mistaken_for_missing(self):
        """同じ id が source_ids に重複していても「欠損」と誤判定しない。

        件数で見ると重複のぶんだけ引けた数が減り、正常な束ねが毎回失敗として
        付箋に積まれる。見るのは引けなかった id そのもの。
        """
        from sai_memory.memory.storage import add_message
        mid = add_message(self.conn, "main", "user", "生ログ本文", created_at=900)
        _entry(self.conn, start=1000, coverage=900, origin="identity",
               source_ids=[mid, mid])
        for i in range(9):
            _entry(self.conn, start=2000 + i * 100, coverage=10_000)

        seen = []
        failures = []
        run_band_overflow(
            self.conn, _Client(),
            batch_callback=lambda msgs, eid: seen.append(eid),
            extraction_failures=failures,
        )
        self.assertEqual(len(seen), 1, "正常な束ねを失敗として扱っている")
        self.assertEqual(failures, [])


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
