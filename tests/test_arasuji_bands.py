"""Chronicle 束ね (sai_memory/arasuji/bands.py) の回帰テスト。

2026-07-27 の世代交代 (「質量 U×B^k 発火・level 別の列」→「字数発火・質量選抜」)
に追従した新仕様のテスト。設計正典は docs/intent/chronicle_consolidation.md。

固定する仕様の骨子:

- 発火 = 列 (未束ね General ノード全部、level 混在) の excluded でない content
  字数合計 > X。X = 提示予算 (SAIVERSE_CHRONICLE_CHAR_BUDGET, 既定2万字) の 1/4。
- 群の三条件: 比率 (質量 max/min ≤ 10、境界含む) / 連続性 (未編纂の生ログを
  跨がない) / 卒業 (合算質量 ≥ 5 × 群内最大)。
- 治療 = 軽い側で行き詰まった群の比率免除合流 (発火と無関係に常時)。
- 非常弁 = 発火したのに束ねも治療も打てない形に限り最古の隣接2件 (WARNING)。
- 原子性: 親 INSERT + 子 mark_consolidated は単一 tx、tx 内で子の未束ねを再検査。
- Fragment callback は digest_origin='identity' の子でのみ発火。

TestBackfillCoverage (coverage_chars 帰化バックフィル) は旧ファイルからの
回帰維持でそのまま残している。
"""

import json
import os
import unittest
from unittest.mock import patch

from sai_memory.arasuji.bands import (
    _BOUNDARY_GAP,
    _load_column,
    _partition_runs,
    backfill_coverage,
    plan_band_overflow,
    run_band_overflow,
)
from sai_memory.arasuji.storage import (
    create_entry,
    get_entry,
    init_arasuji_tables,
)
from sai_memory.memory.storage import add_message, init_db

# 発火閾値 X = 予算 20,000 の 1/4 = 5,000 字。上限は既定と同じ 3。
BASE_ENV = {
    "SAIVERSE_CHRONICLE_CHAR_BUDGET": "20000",
    "SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN": "3",
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
    """列のノードを 1 件作る。質量 = coverage_chars 明示、字数 = content 長で操る。"""
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
    """entry の生 metadata dict (coverage_chars / band_kind / digest_origin の検証用)。"""
    row = conn.execute(
        "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry_id,)
    ).fetchone()
    return json.loads(row[0])


def _band_parents(conn):
    """束ね (bundle/treatment/valve) が作った親 (digest_origin='band') を作成順で返す。"""
    rows = conn.execute(
        "SELECT id FROM memopedia_pages WHERE category = 'chronicle' "
        "AND json_extract(metadata, '$.digest_origin') = 'band' "
        "ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    return [get_entry(conn, r[0]) for r in rows]


class BandTestBase(unittest.TestCase):
    def setUp(self):
        # bands の群分割 SQL (_uncompiled_gap) が messages / stelis_threads を参照
        # するため、arasuji テーブルだけでなくメッセージ側スキーマも同じ接続に
        # 初期化する (init_db がその正規の入口)。
        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)
        self.addCleanup(self.conn.close)

    def _run(self, client=None, *, env=None, **kwargs):
        merged = {**BASE_ENV, **(env or {})}
        with patch.dict(os.environ, merged):
            return run_band_overflow(self.conn, client or _Client(), **kwargs)

    def _plan(self, *, env=None, **kwargs):
        merged = {**BASE_ENV, **(env or {})}
        with patch.dict(os.environ, merged):
            return plan_band_overflow(self.conn, **kwargs)

    def _gap_message(self, created_at):
        """未編纂の生ログ 1 件 — Chronicle 対象タグ (除外タグなし)・非 Stelis
        スレッドで、どの entry の時間範囲にも覆われない位置に置くと連続性の
        切れ目になる。"""
        return add_message(
            self.conn, "main", "user", "未編纂の生ログ", created_at=created_at
        )


class TestFiring(BandTestBase):
    """発火判定 (字数 > X) — 束ねは発火時のみ、治療は発火と無関係。"""

    def test_below_threshold_no_action(self):
        # 固定するもの: 未発火 (字数合計 <= X) では、卒業可能な群があっても
        # 束ねゼロ・LLM コールゼロ。dry 予測も 0。
        # 質量 1,000×10 (卒業条件は満たす) だが字数 300×10=3,000 <= X=5,000。
        for i in range(10):
            _entry(self.conn, start=i * 100, coverage=1000, chars=300)
        self.assertEqual(self._plan(), 0)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 0)
        self.assertEqual(client.calls, 0)
        self.assertEqual(_band_parents(self.conn), [])

    def test_treatment_runs_even_unfired(self):
        # 固定するもの: 治療対象 (軽い側で行き詰まった群) があれば、発火と
        # 無関係に治療が走る。字数 100×4=400 <= X=5,000 で未発火のまま 1 コール。
        _entry(self.conn, start=0, coverage=300, chars=100)
        _entry(self.conn, start=100, coverage=400, chars=100)
        _entry(self.conn, start=200, coverage=10_000, chars=100)
        _entry(self.conn, start=300, coverage=10_000, chars=100)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(_entry_meta(self.conn, parent.id)["band_kind"], "treatment")


class TestStandardBundle(BandTestBase):
    def test_ten_entries_bundle_whole_in_one_call(self):
        # 固定するもの: 標準リズム — 発火した等質量 10 件の列は 1 コールで丸ごと
        # 束ねる。親 level=2 / coverage=10,000 / digest_origin='band' (band_kind
        # なし = 平常の束ね)、子は全員 is_consolidated + parent_id。
        # 字数 600×10=6,000 > X=5,000 で発火。
        entries = [
            _entry(self.conn, start=i * 100, coverage=1000, chars=600)
            for i in range(10)
        ]
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parents = _band_parents(self.conn)
        self.assertEqual(len(parents), 1)
        parent = parents[0]
        self.assertEqual(parent.level, 2)
        self.assertEqual(parent.source_ids, [e.id for e in entries])
        self.assertEqual(parent.start_time, 0)
        self.assertEqual(parent.end_time, 999)
        meta = _entry_meta(self.conn, parent.id)
        self.assertEqual(meta["digest_origin"], "band")
        self.assertEqual(meta["coverage_chars"], 10_000)
        self.assertNotIn("band_kind", meta)
        for e in entries:
            child = get_entry(self.conn, e.id)
            self.assertTrue(child.is_consolidated)
            self.assertEqual(child.parent_id, parent.id)


class TestGraduationGate(BandTestBase):
    def test_heavy_head_left_out_of_window(self):
        # 固定するもの: 卒業ゲート — 質量 10 万の頭 + 1 万×10 は比率ちょうど
        # 10 倍 (境界含む) で同じ群になれるが、卒業する窓 (合算 >= 5×群内最大)
        # は 1万×10 の連続部分列だけ。頭は unconsolidated のまま、親 coverage は
        # 10 万 (頭を巻き込んだ 20 万にならない)。
        head = _entry(self.conn, start=0, coverage=100_000, chars=600, level=2)
        smalls = [
            _entry(self.conn, start=100 + i * 100, coverage=10_000, chars=600)
            for i in range(10)
        ]
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [e.id for e in smalls])
        self.assertNotIn(head.id, parent.source_ids)
        self.assertEqual(_entry_meta(self.conn, parent.id)["coverage_chars"], 100_000)
        self.assertFalse(get_entry(self.conn, head.id).is_consolidated)


class TestHeavyHeadsWait(BandTestBase):
    def test_two_heavy_heads_no_action(self):
        # 固定するもの: 重い頭は待つ — 20万+10万 の 2 件だけの列は、発火
        # (字数 3,000×2=6,000 > X=5,000) していても卒業不能・治療なし。
        # 非常弁も最新端の群の中なので撃たない = アクションゼロ・LLM コールゼロ。
        a = _entry(self.conn, start=0, coverage=200_000, chars=3000, level=2)
        b = _entry(self.conn, start=100, coverage=100_000, chars=3000, level=2)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 0)
        self.assertEqual(client.calls, 0)
        self.assertEqual(_band_parents(self.conn), [])
        self.assertFalse(get_entry(self.conn, a.id).is_consolidated)
        self.assertFalse(get_entry(self.conn, b.id).is_consolidated)


class TestTreatment(BandTestBase):
    def test_stuck_light_run_merges_into_neighbor_in_one_call(self):
        # 固定するもの: 治療 — [300,400] の群は比率割れ (隣 1 万 > 400×10 を
        # 境界に検知) かつ隣が自分の最大より重いので、群 + 隣 1 件を 1 コールで
        # 束ねる。親 coverage=10,700 / band_kind='treatment'。「束ねてから治療」
        # の 2 コールにならない (中間親 coverage=700 が存在しない)。
        a = _entry(self.conn, start=0, coverage=300, chars=100)
        b = _entry(self.conn, start=100, coverage=400, chars=100)
        n1 = _entry(self.conn, start=200, coverage=10_000, chars=100)
        n2 = _entry(self.conn, start=300, coverage=10_000, chars=100)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [a.id, b.id, n1.id])
        meta = _entry_meta(self.conn, parent.id)
        self.assertEqual(meta["coverage_chars"], 10_700)
        self.assertEqual(meta["band_kind"], "treatment")
        self.assertFalse(get_entry(self.conn, n2.id).is_consolidated)
        # 中間親 (300+400=700) が作られていない = 着地予測による治療直行
        row = self.conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE category = 'chronicle' "
            "AND json_extract(metadata, '$.coverage_chars') = 700"
        ).fetchone()
        self.assertEqual(row[0], 0)

    def test_treatment_found_beside_window(self):
        # 固定するもの: 窓のある区間でも、窓と重ならない残余の治療は走る
        # (Codex 再レビュー 必須1 — 区間単位のスキップは治療の即時性を破る)。
        # [質量100×5 (卒業窓), 1, 100] 連続・未発火 (4,200 <= X=5,000):
        # 窓の束ねは発火待ちで出ないが、[1,100] の治療は即時。
        for i in range(5):
            _entry(self.conn, start=i * 100, coverage=100, chars=600)
        tiny = _entry(self.conn, start=500, coverage=1, chars=600)
        tail = _entry(self.conn, start=600, coverage=100, chars=600)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [tiny.id, tail.id])
        meta = _entry_meta(self.conn, parent.id)
        self.assertEqual(meta["band_kind"], "treatment")
        self.assertEqual(meta["coverage_chars"], 101)
        # 発火させた dry では 窓 + 治療 = 2 アクション。
        # (新しい DB で同型を組み直して確認 — 上の実行で治療済みのため)
        conn2 = init_db(":memory:")
        init_arasuji_tables(conn2)
        self.addCleanup(conn2.close)
        for i in range(5):
            _entry(conn2, start=i * 100, coverage=100, chars=600)
        _entry(conn2, start=500, coverage=1, chars=600)
        _entry(conn2, start=600, coverage=100, chars=600)
        with patch.dict(os.environ, {**BASE_ENV,
                                     "SAIVERSE_CHRONICLE_CHAR_BUDGET": "8000"}):
            self.assertEqual(plan_band_overflow(conn2), 2)

    def test_treatment_not_blocked_by_unexecuted_window(self):
        # 固定するもの: 発火していない候補窓は実行されない — その窓に隣が触れて
        # いるだけで治療を落とさない (Codex 三巡 P1-A。衝突の解決は最終選抜の
        # 後の重なり除去で行う)。[1, 100×5] 未発火 (3,600 <= X=5,000):
        # 窓 [100×5] は発火待ちで出ないが、[1, 先頭の100] の治療は即時。
        tiny = _entry(self.conn, start=0, coverage=1, chars=600)
        heads = [
            _entry(self.conn, start=100 + i * 100, coverage=100, chars=600)
            for i in range(5)
        ]
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [tiny.id, heads[0].id])
        self.assertEqual(
            _entry_meta(self.conn, parent.id)["band_kind"], "treatment",
        )

    def test_deferred_window_does_not_block_treatment(self):
        # 固定するもの: 猶予で落ちた最新端の窓は「実行されない窓」— それが
        # 重なり除去で治療を負かしたままにしない (Codex 四巡 P1-a。選抜は
        # 候補から除いてやり直す = 実行されないアクションは子を予約しない)。
        # [50×5 (卒業可)] + ギャップ + [1, 100×5]: 発火するが古い窓で X を
        # 割れるので最新端の窓は猶予 → 治療 [1,100] が復活して同回で走る。
        for i in range(5):
            _entry(self.conn, start=i * 100, coverage=50, chars=600)
        self._gap_message(500)  # 古い区間と新しい区間の境界の未編纂
        tiny = _entry(self.conn, start=500, coverage=1, chars=600)
        heads = [
            _entry(self.conn, start=600 + i * 100, coverage=100, chars=600)
            for i in range(5)
        ]
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 2)
        parents = _band_parents(self.conn)
        by_cov = {
            _entry_meta(self.conn, p.id)["coverage_chars"]: p for p in parents
        }
        self.assertIn(250, by_cov)   # 古い窓 [50×5]
        self.assertIn(101, by_cov)   # 治療 [1, 100]
        self.assertEqual(by_cov[101].source_ids, [tiny.id, heads[0].id])
        self.assertEqual(
            _entry_meta(self.conn, by_cov[101].id)["band_kind"], "treatment",
        )
        for e in heads[1:]:  # 猶予された最新端の窓は無傷
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)

    def test_heavy_side_is_not_treated(self):
        # 固定するもの: 重い側は治療されない — [15万] の群は隣 (1万) が自分より
        # 軽いので待つ (新しい側に同格の親が育つのを待つのが正常)。治療ゼロ。
        big = _entry(self.conn, start=0, coverage=150_000, chars=300, level=2)
        n1 = _entry(self.conn, start=100, coverage=10_000, chars=300)
        n2 = _entry(self.conn, start=200, coverage=10_000, chars=300)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 0)
        self.assertEqual(client.calls, 0)
        for e in (big, n1, n2):
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)


class TestContinuityGap(BandTestBase):
    def test_uncompiled_gap_splits_runs_and_blocks_bundling(self):
        # 固定するもの: 連続性条件 — 4 件目と 5 件目の間のギャップに未編纂の
        # 生ログが居ると列は 4+4 の群に分断され (boundary='gap')、どちらも卒業
        # 不能 (4,000 < 5×1,000) になる。束ねゼロ = ギャップを跨ぐ親が作られない。
        # 字数 400×8=3,200 <= X で未発火にしてある (発火させると非常弁が run 内の
        # 隣接 2 件を拾う仕様のため、「束ねゼロ」はこの形では未発火が前提)。
        olds = [
            _entry(self.conn, start=i * 100, coverage=1000, chars=400)
            for i in range(4)
        ]
        news = [
            _entry(self.conn, start=1000 + i * 100, coverage=1000, chars=400)
            for i in range(4)
        ]
        self._gap_message(550)  # 開区間 (399, 1000) の中、どの entry にも覆われない

        # 群分割そのものを直接固定する (発火の有無に依存しない検証)
        column = _load_column(self.conn)
        runs = _partition_runs(self.conn, column)
        self.assertEqual([len(r.items) for r in runs], [4, 4])
        self.assertEqual(runs[0].boundary_after, _BOUNDARY_GAP)

        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 0)
        self.assertEqual(client.calls, 0)
        self.assertEqual(_band_parents(self.conn), [])
        for e in olds + news:
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)


class TestExcludedBoundary(BandTestBase):
    """excluded_entry_ids (圧縮区間として提示中の digest) の勘定外し + 境界維持。"""

    def test_excluded_chars_do_not_count_toward_firing(self):
        # 固定するもの: excluded の字数は発火勘定に入らない — 真ん中の 1 件を
        # 除外すると 8×600=4,800 <= X=5,000 で未発火 (plan=0)、除外しなければ
        # 9×600=5,400 > X で発火 (plan=1)。
        entries = [
            _entry(self.conn, start=i * 100, coverage=1000, chars=600)
            for i in range(9)
        ]
        mid = entries[4]
        self.assertEqual(self._plan(), 1)
        self.assertEqual(self._plan(excluded_entry_ids={mid.id}), 0)
        client = _Client()
        created = self._run(client, excluded_entry_ids={mid.id})
        self.assertEqual(created, 0)
        self.assertEqual(client.calls, 0)

    def test_no_parent_across_excluded(self):
        # 固定するもの: excluded は列の境界として残る — 12 件の真ん中 (6 件目) を
        # 除外すると列は 5+6 の群に割れ、束ねられるのは古い側 5 件のみ (最新端
        # 6 件は見込み削減で X を割れるため猶予)。親の source_ids に excluded は
        # 入らず、親の被覆範囲も excluded の手前で止まる (跨いだ親なし)。
        entries = [
            _entry(self.conn, start=i * 100, coverage=1000, chars=600)
            for i in range(12)
        ]
        mid = entries[5]
        client = _Client()
        created = self._run(client, excluded_entry_ids={mid.id})
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [e.id for e in entries[:5]])
        self.assertNotIn(mid.id, parent.source_ids)
        self.assertEqual(parent.end_time, entries[4].end_time)  # 跨いでいない
        self.assertFalse(get_entry(self.conn, mid.id).is_consolidated)
        for e in entries[6:]:
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)


class TestOrphan(BandTestBase):
    def test_orphan_stays_out_of_column(self):
        # 固定するもの: 孤児 (他 entry の時間範囲に真に内包される未束ねノード) は
        # 列から除外され、周囲が束ねられてもどの親にも入らず unconsolidated のまま。
        wall = _entry(self.conn, start=0, end=9999, coverage=100_000, chars=600, level=2)
        orphan = _entry(self.conn, start=500, end=599, coverage=1000, chars=600)
        fresh = [
            _entry(self.conn, start=10_000 + i * 100, coverage=1000, chars=600)
            for i in range(10)
        ]
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [e.id for e in fresh])
        self.assertNotIn(orphan.id, parent.source_ids)
        self.assertFalse(get_entry(self.conn, orphan.id).is_consolidated)
        # 壁 (重い頭) 自身も待ちのまま
        self.assertFalse(get_entry(self.conn, wall.id).is_consolidated)


class TestNewestRunDeferral(BandTestBase):
    def test_only_old_run_bundles_when_it_frees_budget(self):
        # 固定するもの: 最新端の猶予 — gap で分断された古い群 (卒業可) と最新端の
        # 群 (卒業可) が両方候補のとき、古い群の見込み削減 (字数 7,000−500) で
        # 発火字数 13,000 が X=6,500 以下に落ちるため、最新端の群は束ねられない。
        # 実行されるのは古い群だけ (親 1 件)。
        env = {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "26000"}  # X = 6,500
        olds = [
            _entry(self.conn, start=i * 100, coverage=1000, chars=700)
            for i in range(10)
        ]
        self._gap_message(1500)  # 開区間 (999, 2000) の未編纂生ログで群を分断
        news = [
            _entry(self.conn, start=2000 + i * 100, coverage=1000, chars=600)
            for i in range(10)
        ]
        client = _Client()
        created = self._run(client, env=env)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [e.id for e in olds])
        for e in news:
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)


class TestWindowSearch(BandTestBase):
    """窓探しは比率の区切りより先 (Codex レビュー P1-1, 2026-07-27)。"""

    def test_window_not_blocked_by_heavy_head_reach(self):
        # 固定するもの: 先に列全体を比率で区切ると、重い頭 (1000) が射程を
        # 伸ばして [1000,101×4] / [99×2] に分断され、本来卒業できる
        # [101×4,99×2] (比率 101/99、合算 602 >= 5×101) を見落とす。窓探しは
        # 区間の生の並びに対して行い、この窓を見つけて束ねる。頭は残る。
        env = {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "8000"}  # X=2,000 < 600×7
        head = _entry(self.conn, start=0, coverage=1000, chars=600)
        smalls = [
            _entry(self.conn, start=100 + i * 100, coverage=101, chars=600)
            for i in range(4)
        ] + [
            _entry(self.conn, start=500 + i * 100, coverage=99, chars=600)
            for i in range(2)
        ]
        client = _Client()
        created = self._run(client, env=env)
        self.assertEqual(created, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [e.id for e in smalls])
        meta = _entry_meta(self.conn, parent.id)
        self.assertEqual(meta["coverage_chars"], 101 * 4 + 99 * 2)
        self.assertNotIn("band_kind", meta)
        self.assertFalse(get_entry(self.conn, head.id).is_consolidated)


class TestSameSecondGap(BandTestBase):
    """連続性の判定は時間範囲でなく source 帰属 (Codex レビュー P1-2 / 必須3)。"""

    def test_same_second_uncompiled_message_blocks_adjacency(self):
        # 固定するもの: 正典順序 (created_at, rowid) では entry 境界と同じ秒に
        # 未編纂メッセージが挟まりうる。A の末尾秒 (100) に、どの一次あらすじの
        # source_ids にも入っていないメッセージが居るなら、A と B は隣接でない
        # (時間範囲の近似だと「A の範囲内 = 編纂済み」と誤読していた)。
        _entry(self.conn, start=0, end=100, coverage=1000, chars=600)
        _entry(self.conn, start=101, end=200, coverage=1000, chars=600)
        self._gap_message(100)  # A の末尾と同じ秒・source 未帰属
        with patch.dict(os.environ, BASE_ENV):
            runs = _partition_runs(self.conn, _load_column(self.conn))
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].boundary_after, _BOUNDARY_GAP)

    def test_same_second_after_next_head_is_not_a_gap(self):
        # 固定するもの: B の先頭 source と同じ秒でも rowid が後ろのメッセージは
        # 正典順序で B より後 = A–B の間ではない (Codex 再レビュー 必須3)。
        # lv1 同士の隣接では端の source の rowid まで含めた keyset 比較になる。
        ida = self._gap_message(100)
        idb = self._gap_message(101)
        _entry(self.conn, start=0, end=100, coverage=1000, chars=600,
               source_ids=[ida])
        _entry(self.conn, start=101, end=200, coverage=1000, chars=600,
               source_ids=[idb])
        self._gap_message(101)  # idb と同じ秒・rowid は後 = B の後ろ
        with patch.dict(os.environ, BASE_ENV):
            runs = _partition_runs(self.conn, _load_column(self.conn))
        self.assertEqual(len(runs), 1)

    def test_same_second_before_next_head_is_a_gap(self):
        # 固定するもの: 同じ秒でも rowid が B の先頭 source より前なら
        # A–B の間の未編纂 = ギャップ。
        ida = self._gap_message(100)
        stray = self._gap_message(101)  # 先に挿入 = rowid が idb より前
        idb = self._gap_message(101)
        del stray
        _entry(self.conn, start=0, end=100, coverage=1000, chars=600,
               source_ids=[ida])
        _entry(self.conn, start=101, end=200, coverage=1000, chars=600,
               source_ids=[idb])
        with patch.dict(os.environ, BASE_ENV):
            runs = _partition_runs(self.conn, _load_column(self.conn))
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].boundary_after, _BOUNDARY_GAP)


class TestSelectionOrder(BandTestBase):
    def test_cap_one_picks_lightest_run(self):
        # 固定するもの: 軽い群 (最大メンバー質量が小さい) 優先 + 上限 env=1 —
        # 古くて重い群 (2万×5、卒業可) と新しくて軽い凍結群 (1千×5、卒業可、
        # 後ろに更に群があるので最新端でない) が枠を取り合うと、実行 1 件は軽い群。
        env = {"SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN": "1"}
        heavy = [
            _entry(self.conn, start=i * 100, coverage=20_000, chars=600, level=2)
            for i in range(5)
        ]
        light = [
            _entry(self.conn, start=500 + i * 100, coverage=1000, chars=600)
            for i in range(5)
        ]
        _entry(self.conn, start=1000, coverage=100_000, chars=600, level=2)  # 最新端の別群
        client = _Client()
        created = self._run(client, env=env)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [e.id for e in light])
        for e in heavy:
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)


class TestDryPlan(BandTestBase):
    """plan_band_overflow は run と同じ計画を共有する dry (LLM なし)。"""

    def test_plan_equals_run_equals_calls(self):
        # 固定するもの: dry == run — 複数アクション (重い群 + 軽い群の 2 束ね) の
        # fixture で plan の予測回数 = run が作る親の数 = LLM コール数。
        for i in range(5):
            _entry(self.conn, start=i * 100, coverage=20_000, chars=600, level=2)
        for i in range(5):
            _entry(self.conn, start=500 + i * 100, coverage=1000, chars=600)
        _entry(self.conn, start=1000, coverage=100_000, chars=600, level=2)
        predicted = self._plan()
        self.assertEqual(predicted, 2)
        client = _Client()
        executed = self._run(client)
        self.assertEqual(executed, predicted)
        self.assertEqual(client.calls, predicted)
        self.assertEqual(len(_band_parents(self.conn)), predicted)

    def test_pending_sources_do_not_split_dry_column(self):
        # 固定するもの: 実運用の dry では編纂予定の生メッセージが実在し、
        # 模擬ノード自身の生ログが「未編纂ギャップ」に見えて列が分断される
        # (Codex 再レビュー 必須2)。pending_source_ids で除外すると実行後と
        # 同じ一本の区間になり、dry == run の決定論が保たれる。
        env = {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "16000"}  # X = 4,000
        mids = [self._gap_message((i + 1) * 100) for i in range(10)]
        leaves = [(1000, (i + 1) * 100, (i + 1) * 100) for i in range(10)]
        # 除外なし: 各模擬ノードの境界秒に自分の生ログが「未編纂」で立ち、
        # 全部が単独の区間に割れて窓が組めない。
        self.assertEqual(self._plan(env=env, extra_leaves=leaves), 0)
        # 除外あり: 一本の区間 → 10 件の窓 1 つ。
        self.assertEqual(
            self._plan(env=env, extra_leaves=leaves,
                       pending_source_ids=set(mids)),
            1,
        )

    def test_plan_counts_extra_leaves(self):
        # 固定するもの: extra_leaves — 空の列でも、これから確定する新チャンク
        # (質量 1,000×10、時刻連続) を模擬ノードとして列に加算して予測する。
        # 模擬ノードの字数見込みは min(質量, 500)=500 → 5,000 > X=4,000 で発火し
        # plan >= 1 (この形では丸ごと 1 束ね = 1)。
        env = {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "16000"}  # X = 4,000
        self.assertEqual(self._plan(env=env), 0)
        leaves = [(1000, i * 100, i * 100 + 99) for i in range(10)]
        self.assertEqual(self._plan(env=env, extra_leaves=leaves), 1)


class TestAtomicity(BandTestBase):
    """原子性: 親 INSERT + 子 mark_consolidated は単一 tx + tx 内再検査。"""

    def test_llm_failure_changes_nothing(self):
        # 固定するもの: LLM 例外では DB が一切変化しない (親なし・子は未束ね)。
        entries = [
            _entry(self.conn, start=i * 100, coverage=1000, chars=600)
            for i in range(10)
        ]
        created = self._run(_Client(fail=True))
        self.assertEqual(created, 0)
        self.assertEqual(_band_parents(self.conn), [])
        for e in entries:
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)

    def test_concurrent_child_consolidation_abandons(self):
        # 固定するもの: tx 内再検査 — LLM 成功後、generate の間に並走ジョブが
        # 子 1 件を先に is_consolidated にしていたら、その束ねは放棄され親は
        # 作られない (先着の親の parent_id も上書きされない)。
        entries = [
            _entry(self.conn, start=i * 100, coverage=1000, chars=600)
            for i in range(10)
        ]
        outer_conn = self.conn

        class _RacingClient:
            def generate(self, messages, tools):
                # 並走ジョブが最古の子を先に束ねてしまう状況を模擬
                from sai_memory.arasuji.storage import create_entry, mark_consolidated

                rival = create_entry(
                    outer_conn,
                    level=2,
                    content="先着の親",
                    source_ids=[entries[0].id],
                    start_time=0,
                    end_time=99,
                    source_count=1,
                    message_count=1,
                    extra_metadata={"coverage_chars": 1000},
                )
                mark_consolidated(outer_conn, [entries[0].id], rival.id)
                self.rival_id = rival.id
                return "後着のまとめ"

            def consume_usage(self):
                return None

        racing = _RacingClient()
        created = self._run(racing)
        self.assertEqual(created, 0)
        # 後着の親 (digest_origin='band') は作られていない
        self.assertEqual(_band_parents(self.conn), [])
        # 先着の parent_id は上書きされていない
        child = get_entry(self.conn, entries[0].id)
        self.assertEqual(child.parent_id, racing.rival_id)
        # 残りの子は未束ねのまま
        for e in entries[1:]:
            self.assertFalse(get_entry(self.conn, e.id).is_consolidated)


class TestFragmentCallback(BandTestBase):
    """Fragment 抽出コールバック — 恒等圧縮 (identity) の子が要約に変わる瞬間のみ。"""

    def _identity_fixture(self):
        # identity 子 1 件 (source_ids = 実メッセージ 2 件) + batch 子 9 件の列。
        # 実メッセージは identity の時間範囲内に置く (= 被覆済みなのでギャップに
        # ならない)。字数 600×10=6,000 > X=5,000 で発火し 10 件丸ごと束ねになる。
        m1 = add_message(
            self.conn, "main", "user", "恒等圧縮の生ログ1", created_at=110
        )
        m2 = add_message(
            self.conn, "main", "assistant", "恒等圧縮の生ログ2", created_at=120
        )
        identity = _entry(
            self.conn,
            start=100,
            coverage=1000,
            chars=600,
            origin="identity",
            source_ids=[m1, m2],
        )
        batches = [
            _entry(self.conn, start=200 + i * 100, coverage=1000, chars=600)
            for i in range(9)
        ]
        return m1, m2, identity, batches

    def test_callback_fires_once_for_identity_child_only(self):
        # 固定するもの: callback は identity 子 1 回だけ、引数は (その子の実
        # メッセージ列 [created_at 昇順], 子の entry_id)。batch 子では呼ばれない。
        m1, m2, identity, _batches = self._identity_fixture()
        calls = []
        created = self._run(
            batch_callback=lambda msgs, eid: calls.append((msgs, eid))
        )
        self.assertEqual(created, 1)
        self.assertEqual(len(calls), 1)
        msgs, eid = calls[0]
        self.assertEqual(eid, identity.id)
        self.assertEqual([m.id for m in msgs], [m1, m2])
        self.assertEqual(msgs[0].content, "恒等圧縮の生ログ1")

    def test_callback_exception_does_not_break_bundle(self):
        # 固定するもの: callback 例外は握り潰され、束ねは成功のまま
        # (親は作られ、identity 子も束ね済みになる)。
        _m1, _m2, identity, batches = self._identity_fixture()

        def boom(msgs, eid):
            raise RuntimeError("fragment down")

        created = self._run(batch_callback=boom)
        self.assertEqual(created, 1)
        self.assertEqual(len(_band_parents(self.conn)), 1)
        for e in [identity, *batches]:
            self.assertTrue(get_entry(self.conn, e.id).is_consolidated)


class TestValve(BandTestBase):
    def test_valve_bundles_small_stuck_pair_with_warning(self):
        # 固定するもの: 非常弁 — 発火 (字数 2,600×2+600=5,800 > X=5,000) した
        # のに束ねも治療も打てない形では、最古の隣接 2 件を比率無視で束ね、
        # band_kind='valve' の親 1 件 + WARNING を出す。
        # 形: [質量4,000, 3,000] は卒業不能 (7,000 < 5×4,000)、隣の最新端
        # (質量300) は軽いので治療対象でもない (重い側は待つ)。a/b は共に
        # U=1万未満なので非常弁の対象になれる。最新端の群は巻き込まれない。
        a = _entry(self.conn, start=0, coverage=4_000, chars=2600)
        b = _entry(self.conn, start=100, coverage=3_000, chars=2600)
        t = _entry(self.conn, start=200, coverage=300, chars=600)
        client = _Client()
        with self.assertLogs("sai_memory.arasuji.bands", level="WARNING") as logs:
            created = self._run(client)
        self.assertEqual(created, 1)
        self.assertEqual(client.calls, 1)
        self.assertTrue(any("valve" in line for line in logs.output))
        parent = _band_parents(self.conn)[0]
        self.assertEqual(parent.source_ids, [a.id, b.id])
        meta = _entry_meta(self.conn, parent.id)
        self.assertEqual(meta["band_kind"], "valve")
        self.assertEqual(meta["coverage_chars"], 7_000)
        self.assertEqual(parent.level, 2)
        self.assertFalse(get_entry(self.conn, t.id).is_consolidated)

    def test_heavy_heads_are_not_valve_food(self):
        # 固定するもの: U (一次あらすじの標準被覆 1万字) 以上のノードは非常弁の
        # 対象にならない。重い頭 (束ね済みの親たち) は最新端の群でなくても、
        # 発火 + アクション不能の過渡期に非常弁で溶かされない (歯止めは目的 =
        # 「重い記憶を巻き込まない」から — intent §5-1)。
        _entry(self.conn, start=0, coverage=50_000, chars=2600, level=2)
        _entry(self.conn, start=100, coverage=400_000, chars=2600, level=2)
        _entry(self.conn, start=200, coverage=300, chars=600)
        client = _Client()
        created = self._run(client)
        self.assertEqual(created, 0)
        self.assertEqual(client.calls, 0)
        self.assertEqual(_band_parents(self.conn), [])


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
