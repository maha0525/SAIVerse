"""整列計画器 (sai_memory/arasuji/alignment.py) の回帰テスト。

docs/intent/arasuji_levels.md §4 (レベル0 の畳み) の計画側を固定する。
旧仕様 (episode 整列 + 恒等圧縮 identity + digest 転写 episode) は 2026-07-28 に
世代交代した — 現設計のチャンクは全部 LLM バッチで、規則は:

- processed を跨いだ束ねをしない + run_groups の群をまたぐ束ねをしない (run 分割)
- run 内は被覆 (content 字数合計) が target_chars に達したら閉じる
- run 末尾の端数は直前のチャンクに吸収、吸収先が無ければ小さくてもそのまま
  LLM チャンク (小さくても要約する)
"""

import unittest

from sai_memory.arasuji.alignment import (
    CHUNK_LLM_BATCH,
    message_episode_ref,
    plan_alignment,
    truncate_plan,
)
from sai_memory.memory.storage import Message

# テスト用の小さいパラメータ (既定 10000 だと本文が長くなりすぎる)
TARGET = 100


def _msg(mid, content, episode=None, created_at=None):
    """テストメッセージ。episode は metadata 側 (実運用の主経路) に置く。"""
    return Message(
        id=mid,
        thread_id="main",
        role="user",
        content=content,
        resource_id=None,
        created_at=created_at if created_at is not None else int(mid.replace("m", "")),
        metadata={"origin_episode": episode} if episode else None,
    )


def _plan(messages, processed=(), run_groups=None):
    return plan_alignment(
        messages,
        set(processed),
        target_chars=TARGET,
        run_groups=run_groups,
    )


class TestMessageEpisodeRef(unittest.TestCase):
    def test_reads_dedicated_column_first(self):
        m = _msg("m1", "x")
        m.origin_episode = "episode:9"
        m.metadata = {"origin_episode": "episode:1"}
        self.assertEqual(message_episode_ref(m), "episode:9")

    def test_falls_back_to_metadata(self):
        m = _msg("m1", "x", episode="episode:1")
        self.assertEqual(message_episode_ref(m), "episode:1")

    def test_none_when_absent(self):
        self.assertIsNone(message_episode_ref(_msg("m1", "x")))


class TestRunSplitting(unittest.TestCase):
    """run 分割 — 編纂済み (processed) を跨いだ束ねをしない。"""

    def test_processed_message_splits_runs(self):
        msgs = [
            _msg("m1", "a" * 30),
            _msg("m2", "b" * 30),  # processed → run 境界
            _msg("m3", "c" * 30),
        ]
        plan = _plan(msgs, processed=["m2"])
        # m1 と m3 は別チャンク (跨いで束ねない)
        self.assertEqual(len(plan.chunks), 2)
        self.assertEqual(plan.chunks[0].message_ids, ["m1"])
        self.assertEqual(plan.chunks[1].message_ids, ["m3"])
        self.assertEqual(plan.total_unprocessed, 2)

    def test_all_processed_yields_empty_plan(self):
        msgs = [_msg("m1", "a"), _msg("m2", "b")]
        plan = _plan(msgs, processed=["m1", "m2"])
        self.assertEqual(plan.chunks, [])
        self.assertEqual(plan.total_unprocessed, 0)

    def test_same_run_group_is_not_split(self):
        """同じ群 (退場 fold) の中は連続 — 群を渡しただけでは切れない。"""
        msgs = [_msg("m1", "a" * 30), _msg("m2", "b" * 30)]
        plan = _plan(msgs, run_groups=[["m1", "m2"]])
        self.assertEqual([c.message_ids for c in plan.chunks], [["m1", "m2"]])

    def test_different_run_groups_split(self):
        """別の群は別 run — 提示コンテキストの途中を畳んだ結果の飛び地を束ねない。"""
        msgs = [_msg("m1", "a" * 30), _msg("m2", "b" * 30)]
        plan = _plan(msgs, run_groups=[["m1"], ["m2"]])
        self.assertEqual([c.message_ids for c in plan.chunks], [["m1"], ["m2"]])

    def test_run_group_boundary_survives_missing_head(self):
        """群の先頭が ``messages`` に居なくても境界は立つ (回帰)。

        Chronicle 除外対象 (除外タグ / line_role / Stelis スレッド) の
        メッセージは編纂対象に現れない。境界を「群の先頭 id」で表していた
        ときは、先頭が落ちた群の境界が一度も立たず、離れた群が黙って一つの
        あらすじに混ざっていた (偽の隣接 = 時系列の嘘)。
        docs/issues/archive/chronicle_run_boundary_lost_by_excluded_tag.md
        """
        msgs = [_msg("m1", "a" * 30), _msg("m2", "b" * 30), _msg("m4", "d" * 30)]
        # 群2 = [m3, m4] だが、先頭の m3 は除外されて messages に居ない
        plan = _plan(msgs, run_groups=[["m1", "m2"], ["m3", "m4"]])
        self.assertEqual(
            [c.message_ids for c in plan.chunks], [["m1", "m2"], ["m4"]],
        )

    def test_message_outside_every_run_group_is_isolated(self):
        """群を渡したのに未所属の id があったら、前後どちらとも束ねない。

        契約 (編纂対象の各 id はちょうど一つの群に属する) が破れた入力。
        束ねない側へ倒すのは、禁じているのが偽の隣接であって余分な分割では
        ないから (Codex 攻撃レビュー 2026-07-27)。
        """
        msgs = [_msg("m1", "a" * 30), _msg("m2", "b" * 30), _msg("m3", "c" * 30)]
        with self.assertLogs("sai_memory.arasuji.alignment", "WARNING") as logs:
            # m2 はどの群にも属さない
            plan = _plan(msgs, run_groups=[["m1"], ["m3"]])
        self.assertEqual(
            [c.message_ids for c in plan.chunks], [["m1"], ["m2"], ["m3"]],
        )
        self.assertTrue(any("どの群にも属さない" in line for line in logs.output))

    def test_duplicate_membership_is_isolated(self):
        """同じ id が複数の群にあったら所属を決めず孤立させる。

        どちらかの群へ寄せると、寄せた先の隣と束ねてしまう。その隣が
        本当は穴の向こう側なら偽の隣接になり、しかも群の並び順で結果が
        変わる (Codex 攻撃レビュー 2026-07-27 の指摘)。
        """
        msgs = [_msg("m1", "a" * 30), _msg("m2", "b" * 30), _msg("m3", "c" * 30)]
        with self.assertLogs("sai_memory.arasuji.alignment", "WARNING") as logs:
            # m2 が群0 と群1 の両方にいる
            plan = _plan(msgs, run_groups=[["m1", "m2"], ["m2", "m3"]])
        self.assertEqual(
            [c.message_ids for c in plan.chunks], [["m1"], ["m2"], ["m3"]],
        )
        self.assertTrue(any("複数の群に属している" in line for line in logs.output))


class TestChunkCutting(unittest.TestCase):
    """run 内のチャンク切り — 被覆が target に達したら閉じ、端数は直前に吸収。"""

    def test_run_splits_at_target_chars(self):
        """被覆 (content 字数合計) が target に達したメッセージでチャンクが閉じる。"""
        msgs = [_msg(f"m{i}", "a" * 40) for i in range(1, 9)]  # 40×8=320
        plan = _plan(msgs)
        # 40×3=120 ≥ 100 で 1 個目、次も m4〜m6 で閉じ、端数 m7,m8 (80) は
        # 2 個目に吸収される
        self.assertEqual(
            [c.message_ids for c in plan.chunks],
            [["m1", "m2", "m3"], ["m4", "m5", "m6", "m7", "m8"]],
        )
        self.assertTrue(all(c.kind == CHUNK_LLM_BATCH for c in plan.chunks))
        self.assertEqual([c.coverage_chars for c in plan.chunks], [120, 200])

    def test_trailing_remainder_absorbed_into_previous_chunk(self):
        """run 末尾の target 未達の端数は独立チャンクにせず直前へ吸収する。"""
        msgs = [_msg(f"m{i}", "a" * 40) for i in range(1, 5)]  # 120 で閉+端数 40
        plan = _plan(msgs)
        self.assertEqual(
            [c.message_ids for c in plan.chunks], [["m1", "m2", "m3", "m4"]],
        )
        self.assertEqual(plan.chunks[0].coverage_chars, 160)

    def test_whole_run_below_target_is_small_llm_chunk(self):
        """run 全体が target 未満なら小さくてもそのまま LLM チャンク。

        恒等圧縮 (生ログを生のまま一次あらすじの席に置く) は廃止 —
        小さくても要約する。
        """
        plan = _plan([_msg("m1", "ab"), _msg("m2", "cd")])  # 合計 4 字
        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].kind, CHUNK_LLM_BATCH)
        self.assertEqual(plan.chunks[0].message_ids, ["m1", "m2"])

    def test_episode_has_no_veto_over_cutting(self):
        """切り位置は字数だけで決まる — episode の途中でもチャンクは閉じる。

        エピソードに畳みを止める権利は無い (arasuji_levels.md §4-1。
        開いているエピソードを守る設計が取り残しと行き詰まりの温床だった)。
        """
        msgs = [_msg(f"m{i}", "a" * 60, episode="episode:1") for i in range(1, 5)]
        plan = _plan(msgs)
        # 60×2=120 ≥ 100 で閉じる × 2 — episode:1 は 2 チャンクに割れる
        self.assertEqual(
            [c.message_ids for c in plan.chunks], [["m1", "m2"], ["m3", "m4"]],
        )
        for c in plan.chunks:
            self.assertEqual(c.episode_refs, ["episode:1"])

    def test_episode_refs_recorded_in_order_without_duplicates(self):
        """episode_refs は被覆 episode の一覧 (出現順・重複なし) — 想起の錨。"""
        msgs = [
            _msg("m1", "a" * 30, episode="episode:1"),
            _msg("m2", "b" * 30, episode="episode:1"),
            _msg("m3", "c" * 30, episode="episode:2"),
            _msg("m4", "d" * 30),  # 無帰属は refs に入らない
        ]
        plan = _plan(msgs)
        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].episode_refs, ["episode:1", "episode:2"])


class TestPlanSummary(unittest.TestCase):
    def test_llm_calls_and_summary(self):
        """全チャンクが LLM — llm_calls はチャンク数、summary は 3 キーのみ。"""
        msgs = [
            _msg("m1", "a" * 120),  # 単独で target 達成
            _msg("m2", "x"),        # processed → run 境界
            _msg("m3", "b" * 30),   # 小さい run → 小チャンク
        ]
        plan = _plan(msgs, processed=["m2"])
        self.assertEqual(len(plan.chunks), 2)
        self.assertEqual(plan.llm_calls, 2)
        self.assertEqual(
            plan.summary,
            {"chunks_total": 2, "chunks_llm": 2, "total_unprocessed": 2},
        )

    def test_coverage_chars_counts_source_chars(self):
        """coverage_chars は被覆生ログ字数 (あらすじ→被覆元の錨・統計)。"""
        msgs = [
            _msg("m1", "a" * 30, episode="episode:1"),
            _msg("m2", "b" * 30, episode="episode:1"),
        ]
        plan = _plan(msgs)
        self.assertEqual(plan.chunks[0].coverage_chars, 60)


class TestTruncatePlan(unittest.TestCase):
    """UI の手動生成 (最大 N 件まで処理) 用の切り詰め — チャンクは分割しない。"""

    def _three_single_chunks(self):
        # 群で 1 件ずつ孤立させ、1 メッセージ = 1 チャンクの計画を作る
        msgs = [_msg("m1", "a" * 30), _msg("m2", "b" * 30), _msg("m3", "c" * 30)]
        return _plan(msgs, run_groups=[["m1"], ["m2"], ["m3"]])

    def test_zero_or_negative_means_unlimited(self):
        plan = self._three_single_chunks()
        self.assertIs(truncate_plan(plan, 0), plan)
        self.assertIs(truncate_plan(plan, -1), plan)

    def test_stops_before_chunk_exceeding_limit(self):
        plan = self._three_single_chunks()
        truncated = truncate_plan(plan, 2)
        self.assertEqual(
            [c.message_ids for c in truncated.chunks], [["m1"], ["m2"]],
        )
        # total_unprocessed は元の全量のまま (進捗表示の分母)
        self.assertEqual(truncated.total_unprocessed, 3)

    def test_first_oversized_chunk_still_runs(self):
        """1 個目のチャンクが単独で上限超過でもそれだけは実行する
        (0 件では「処理した」と言えない)。"""
        msgs = [_msg("m1", "a" * 30), _msg("m2", "b" * 30), _msg("m3", "c" * 30)]
        plan = _plan(msgs)  # 合計 90 < target → 1 チャンク 3 件
        truncated = truncate_plan(plan, 2)
        self.assertEqual(
            [c.message_ids for c in truncated.chunks], [["m1", "m2", "m3"]],
        )


if __name__ == "__main__":
    unittest.main()
