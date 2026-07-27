"""退場計画 (sea/eviction_plan.py) の純関数テスト。

docs/intent/chronicle_eviction.md §3 (規範) / §4 (三水位) / §5 (手続き) と、
docs/intent/experience_structure.md §4 の圧縮七原則を、原則番号で固定する。
"""
from __future__ import annotations

import unittest

from sea.eviction_plan import (
    ESTIMATED_FOLD_PLACEHOLDER_CHARS,
    Fold,
    Watermarks,
    compile_groups_from_folds,
    plan_eviction,
)
from sea.session_window import FOLDED_MARKER

U = 2_000  # 一次あらすじの標準被覆 (テスト内での U)


def msg(mid, at, *, chars=1_000, ep=None, pulse=None, folded=False):
    payload = {"id": mid, "content": "x" * chars, "created_at": at}
    meta = {}
    if ep:
        meta["origin_episode"] = ep
    if folded:
        meta[FOLDED_MARKER] = True
    if meta:
        payload["metadata"] = meta
    if pulse:
        payload["pulse_id"] = pulse
    return payload


def plan(messages, open_refs=(), *, low=2_000, target=2_000, high=None):
    return plan_eviction(
        messages, set(open_refs),
        Watermarks(low=low, target=target, high=high),
        target_chars=U,
    )


class ProtectionBandTest(unittest.TestCase):
    """§5-1: 低水位ぶんの直近は絶対に退場させない。"""

    def test_recent_band_is_protected(self):
        msgs = [msg(f"m{i}", 100 + i) for i in range(6)]
        result = plan(msgs, low=3_000, target=1_000)
        # 末尾 3,000字 = m3..m5 が保護範囲 → 候補は m0..m2
        self.assertEqual(result.protected_from, 3)
        folded = [mid for f in result.folds for mid in f.message_ids]
        self.assertNotIn("m3", folded)
        self.assertNotIn("m4", folded)
        self.assertNotIn("m5", folded)

    def test_window_smaller_than_low_is_all_protected(self):
        msgs = [msg("m0", 100), msg("m1", 101)]
        result = plan(msgs, low=10_000, target=0)
        self.assertEqual(result.protected_from, 0)
        self.assertTrue(result.is_empty)

    def test_boundary_snaps_back_to_pulse_joint(self):
        """§5-4: 保護範囲の境界が pulse の途中に落ちたら古い側へ下げる
        (メッセージ単位でぶつ切りにしない = 保護を広げる向きに倒す)。"""
        msgs = [
            msg("a0", 100, pulse="p1"),
            msg("a1", 101, pulse="p2"),
            msg("a2", 102, pulse="p2"),
            msg("a3", 103, pulse="p2"),
        ]
        # 低水位 2,000字 → 素の境界は index 2 (a2,a3 で 2,000字) だが
        # a1/a2/a3 は同じ pulse なので index 1 まで下がる。
        result = plan(msgs, low=2_000, target=0)
        self.assertEqual(result.protected_from, 1)


class OpenEpisodeTest(unittest.TestCase):
    """§3: open episode は単独でしか畳まない。"""

    def test_small_open_is_skipped_while_something_else_is_foldable(self):
        """一段目: U 未満の open は飛ばして、畳める方を先に畳む (優先度)。

        U は「優先度」の材料であって「畳んでいいか」の材料ではない。U 以上が
        残っている限り、U 未満の open には手を付けない。
        """
        msgs = [
            msg("a0", 100, chars=500, ep="episode:1"),
            msg("k0", 200), msg("k1", 201),   # closed 2 通で U 到達
        ]
        result = plan(msgs, ["episode:1"], low=0, target=2_000)
        self.assertEqual([f.message_ids for f in result.folds], [["k0", "k1"]])
        self.assertFalse(result.used_last_resort_fold)

    def test_small_open_is_folded_as_last_resort(self):
        """二段目: 他に畳めるものが無いなら U 未満の open も畳む (§5-5)。

        これが無いと、提示コンテキストの先頭に U 未満の端数が居座ったとき
        anchor が永久に進まない。
        """
        msgs = [
            msg("a0", 100, chars=500, ep="episode:1"),
            msg("a1", 101, chars=500, ep="episode:1"),
        ]
        result = plan(msgs, ["episode:1"], low=0, target=100)
        self.assertEqual([f.message_ids for f in result.folds], [["a0", "a1"]])
        self.assertEqual(result.folds[0].open_episode_ref, "episode:1")
        self.assertTrue(result.used_last_resort_fold)

    def test_undersized_open_fold_happens_at_most_once(self):
        """二段目は一回だけ。先頭が畳めれば適用側の anchor 前進が連鎖するので足りる。"""
        msgs = [
            msg("a0", 100, chars=500, ep="episode:1"),
            msg("b0", 200, chars=500, ep="episode:2"),
            msg("c0", 300, chars=500, ep="episode:3"),
        ]
        result = plan(msgs, ["episode:1", "episode:2", "episode:3"], low=0, target=100)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["a0"])

    def test_undersized_open_fold_is_not_skipped_for_zero_net_reduction(self):
        """端数は恒等圧縮で正味 0 になりうる。削減量で価値を測って飛ばさない。

        目的は文字数削減ではなく anchor を進めること。
        """
        # 置き換えの見込み (1,200字) より小さい端数 → _net_reduction は 0
        msgs = [msg("a0", 100, chars=300, ep="episode:1")]
        result = plan(msgs, ["episode:1"], low=0, target=100)
        self.assertEqual([f.message_ids for f in result.folds], [["a0"]])
        self.assertEqual(result.projected_chars, result.total_chars)  # 減っていない

    def test_last_resort_targets_the_front_not_the_open(self):
        """Sol P1: 手前に畳めない範囲が残るなら、端数を畳んでも anchor は進まない。

        先頭に U 未満の closed、その後ろに U 未満の open。open を畳んでも先頭の
        closed が残るので anchor は前進せず、しかも置き換えが壁になって先頭は
        以後どことも束ねられなくなる = **anchor が恒久的に詰まる**。
        だから最後の手段が撃つのは**未畳みの先頭** (ここでは closed の c0)。
        レビュー二巡目 P1: open だけを最後の手段にすると、この並びで永久に
        手詰まりになる。
        """
        msgs = [
            msg("c0", 100, chars=500),                    # closed / 無帰属 (U 未満)
            msg("o0", 101, chars=500, ep="episode:1"),    # open (U 未満)
        ]
        result = plan(msgs, ["episode:1"], low=0, target=100)
        self.assertEqual([f.message_ids for f in result.folds], [["c0"]])
        self.assertTrue(result.used_last_resort_fold)

    def test_big_open_folds_repeatedly_within_one_plan(self):
        """Sol P2: 一つの open に U が何個も入っているなら、一度の計画で反復して畳む。

        畳んだ prefix だけを壁として扱い、``open_offset`` 以降は計画対象に残す。
        """
        msgs = [
            msg(f"a{i}", 100 + i, chars=1_000, ep="episode:1", pulse=f"p{i}")
            for i in range(6)
        ]
        # U=2,000 → 2 通で 1 fold。目標まで反復して 2 本以上立つこと。
        result = plan(msgs, ["episode:1"], low=0, target=2_000)
        self.assertGreaterEqual(len(result.folds), 2)
        self.assertEqual(result.folds[0].message_ids, ["a0", "a1"])
        self.assertEqual(result.folds[1].message_ids, ["a2", "a3"])

    def test_last_resort_is_skipped_when_something_was_already_folded(self):
        """最後の手段は「anchor が今回まったく進まない」ときだけ撃つ。

        手前が普通に畳めていれば anchor はその分前進するので、候補の末尾に残った
        端数まで畳む必要はない。ここを緩めると**日常経路で小粒の一次あらすじを
        作る** (issue chronicle_undersized_lv1_chunks が消そうとしているもの) し、
        観測用の used_last_resort_fold も嘘になる。
        """
        msgs = [
            msg("c0", 100, chars=1_000),
            msg("c1", 101, chars=1_000),   # c0+c1 で U 到達 → 普通に畳める
            msg("c2", 102, chars=400),     # 残った端数 — 畳む必要はない
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, [], low=2_000, target=1_000)
        self.assertEqual([f.message_ids for f in result.folds], [["c0", "c1"]])
        self.assertFalse(result.used_last_resort_fold)

    def test_folds_are_returned_in_chronological_order(self):
        """Sol P2: 二段構えでも folds は時系列順で返す。

        一段目で後ろの B、二段目で先頭の端数 A を畳むと発見順は [B, A] になる。
        適用側はこの順に子 episode を刻んで short_id を採るので、逆順のまま
        返すと記録の並びが体験の並びと食い違う。
        """
        msgs = [
            msg("a0", 100, chars=500, ep="episode:1"),   # 先頭の open (U 未満)
            msg("b0", 101, chars=1_000),                 # 以降で U 到達
            msg("b1", 102, chars=1_000),
        ]
        result = plan(msgs, ["episode:1"], low=0, target=100)
        self.assertEqual(
            [f.message_ids for f in result.folds], [["a0"], ["b0", "b1"]],
        )

    def test_large_open_folds_by_pulse_joint(self):
        """§6: U に達した open は pulse を丸ごと単位で刻んで部分退場する。"""
        msgs = [
            msg("a0", 100, ep="episode:1", pulse="p1"),
            msg("a1", 101, ep="episode:1", pulse="p1"),
            msg("a2", 102, ep="episode:1", pulse="p2"),
            msg("k0", 200), msg("k1", 201),
        ]
        # 一段目で目標に届く水位にして、最後の手段の経路を誘発しない。
        result = plan(msgs, ["episode:1"], low=2_000, target=4_500)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["a0", "a1"])
        self.assertEqual(result.folds[0].open_episode_ref, "episode:1")
        self.assertFalse(result.used_last_resort_fold)

    def test_open_never_bundles_with_neighbours(self):
        """§4-5: open を挟んで前後の closed をまたいで束ねない。

        最後の手段で open 自身が畳まれることはあっても、それを跨いで前後の
        closed が一つの fold に入ることはない (偽の隣接を作らない)。
        """
        msgs = [
            msg("c0", 100, ep="episode:1"),      # closed
            msg("o0", 101, chars=500, ep="episode:2"),  # open (U 未満)
            msg("c1", 102),                      # 無帰属
            msg("k0", 200), msg("k1", 201),
        ]
        # c0 + c1 なら U=2,000 に届くが、間に open があるのでまたげない。
        result = plan(msgs, ["episode:2"], low=2_000, target=0)
        mixed = [
            f for f in result.folds
            if "c0" in f.message_ids and "c1" in f.message_ids
        ]
        self.assertEqual(mixed, [])
        # 最後の手段が撃つのは**未畳みの先頭**である c0 (open ではない) —
        # open を畳んでも手前の c0 が残って anchor は進まないため。
        self.assertEqual([f.message_ids for f in result.folds], [["c0"]])


class ClosedBundlingTest(unittest.TestCase):
    """§3: closed 同士は episode をまたいで束ねてよい。"""

    def test_closed_episodes_bundle_to_reach_u(self):
        msgs = [
            msg("c0", 100, ep="episode:1"),
            msg("c1", 101, ep="episode:2"),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, [], low=2_000, target=2_000)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["c0", "c1"])

    def test_under_u_run_is_folded_only_as_last_resort(self):
        """U に届かない列は日常経路では畳まない。他に手が無ければ畳む (§5-5)。

        「小粒の一次あらすじを一切作らない」という旧不変条件は撤回した
        (issue chronicle_undersized_lv1_chunks の目的改訂)。作らないのは
        日常経路だけで、anchor が詰まるなら畳む。
        """
        msgs = [msg("c0", 100, chars=500), msg("k0", 200), msg("k1", 201)]
        # 目標に既に届いている → 何も畳まない
        self.assertTrue(plan(msgs, [], low=2_000, target=10_000).is_empty)
        # 目標に届かない → 最後の手段で先頭の小さい列を畳む
        result = plan(msgs, [], low=2_000, target=0)
        self.assertEqual([f.message_ids for f in result.folds], [["c0"]])
        self.assertTrue(result.used_last_resort_fold)


class TargetWatermarkTest(unittest.TestCase):
    """§5-3: 目標水位に達するまで繰り返し、達したら止める。"""

    def test_repeats_until_target(self):
        """畳んだ位置には置き換えが 1 通立つので、**減るのは差し引き**。

        生ログ 2,000字を畳んでも提示は 0 にならない (あらすじ + 圧縮マークが
        残る) ため、目標到達の判定はその見込みを差し引いた量で行う。
        """
        msgs = [msg(f"m{i}", 100 + i) for i in range(8)]  # 8,000字
        net = 2 * 1_000 - ESTIMATED_FOLD_PLACEHOLDER_CHARS  # 1 束あたりの正味削減
        # 保護 2,000字 (m6,m7) / 候補 m0..m5 / 目標 = 2 束ぶん削った量
        result = plan(msgs, [], low=2_000, target=8_000 - 2 * net)
        self.assertEqual(len(result.folds), 2)
        self.assertEqual(result.folds[0].message_ids, ["m0", "m1"])
        self.assertEqual(result.folds[1].message_ids, ["m2", "m3"])
        self.assertEqual(result.projected_chars, 8_000 - 2 * net)

    def test_stops_when_candidates_run_out(self):
        """候補範囲を出し切っても目標に届かないときは、そこで止まる (best-effort)。"""
        msgs = [msg(f"m{i}", 100 + i) for i in range(6)]
        result = plan(msgs, [], low=2_000, target=0)
        # 候補は m0..m3 の 2 束まで。保護範囲 (m4,m5) には手を付けない。
        self.assertEqual(len(result.folds), 2)
        self.assertGreater(result.projected_chars, 0)

    def test_stops_when_already_at_target(self):
        msgs = [msg(f"m{i}", 100 + i) for i in range(3)]
        result = plan(msgs, [], low=1_000, target=5_000)
        self.assertTrue(result.is_empty)


class FoldedPlaceholderTest(unittest.TestCase):
    """§4-4: 既に畳んだ digest は再圧縮しない。跨いだ束ねも作らない。"""

    def test_placeholder_is_not_refolded(self):
        msgs = [
            msg("folded:x", 100, chars=300, folded=True),
            msg("m0", 101), msg("m1", 102),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, [], low=2_000, target=2_000)
        folded_ids = [mid for f in result.folds for mid in f.message_ids]
        self.assertNotIn("folded:x", folded_ids)
        self.assertEqual(result.folds[0].message_ids, ["m0", "m1"])

    def test_placeholder_blocks_bundling_across_it(self):
        msgs = [
            msg("c0", 100),
            msg("folded:x", 101, chars=300, folded=True),
            msg("c1", 102),
            msg("k0", 200), msg("k1", 201),
        ]
        # c0 + c1 = 2,000字 (U) だが、間の digest を跨いで束ねない。
        result = plan(msgs, [], low=2_000, target=0)
        mixed = [
            f for f in result.folds
            if "c0" in f.message_ids and "c1" in f.message_ids
        ]
        self.assertEqual(mixed, [])


class CompileGroupsFromFoldsTest(unittest.TestCase):
    """編纂へ渡す範囲は「提示コンテキストの連続区間」であることを渡す側で検算する。

    experience_structure.md §4-5 連続束ねのみ — 間に退場しない生ログが残る
    範囲を一つのあらすじにすると、時系列の嘘 (偽の隣接) になる。
    """

    def _folds(self, *id_groups):
        return [
            Fold(messages=[{"id": mid} for mid in ids]) for ids in id_groups
        ]

    def test_contiguous_folds_pass_through(self):
        """正しい計画 (fold は連続・非重複) では何も割れない。"""
        presented = [msg("m0", 100), msg("m1", 101), msg("m2", 102)]
        groups = compile_groups_from_folds(
            self._folds(["m0", "m1"], ["m2"]), presented,
        )
        self.assertEqual(groups, [["m0", "m1"], ["m2"]])

    def test_fold_spanning_a_retained_message_is_split(self):
        """fold の内側に退場しないメッセージ → その位置で割る。"""
        presented = [msg("m0", 100), msg("m1", 101), msg("m2", 102)]
        with self.assertLogs("sea.eviction_plan", "WARNING") as logs:
            groups = compile_groups_from_folds(
                self._folds(["m0", "m2"]), presented,  # m1 は退場しない
            )
        self.assertEqual(groups, [["m0"], ["m2"]])
        self.assertTrue(any("またいでいる" in line for line in logs.output))

    def test_retained_chronicle_excluded_message_is_a_hole(self):
        """残るのが Chronicle 除外メッセージでも穴は穴。

        編纂側は除外メッセージを落とした列しか見られないので、この形の抜けは
        あちらでは原理的に検出できない。提示コンテキストの完全な並びを持つ
        この層で割る (Codex 攻撃レビュー 三巡目 2026-07-27)。
        """
        presented = [
            msg("m0", 100),
            {"id": "spell0", "content": "x", "created_at": 101,
             "metadata": {"tags": ["spell"]}},
            msg("m2", 102),
        ]
        with self.assertLogs("sea.eviction_plan", "WARNING"):
            groups = compile_groups_from_folds(
                self._folds(["m0", "m2"]), presented,
            )
        self.assertEqual(groups, [["m0"], ["m2"]])

    def test_excluded_message_evicted_together_is_not_a_hole(self):
        """同じ fold で一緒に退場するなら穴ではない (提示コンテキストに何も残らない)。"""
        presented = [
            msg("m0", 100),
            {"id": "spell0", "content": "x", "created_at": 101,
             "metadata": {"tags": ["spell"]}},
            msg("m2", 102),
        ]
        groups = compile_groups_from_folds(
            self._folds(["m0", "spell0", "m2"]), presented,
        )
        self.assertEqual(groups, [["m0", "spell0", "m2"]])

    def test_message_in_another_fold_is_not_a_hole(self):
        """間にあるのが別 fold (今回一緒に退場する) なら割らない — 群として別なので束ねられない。"""
        presented = [msg("m0", 100), msg("m1", 101), msg("m2", 102)]
        groups = compile_groups_from_folds(
            self._folds(["m0", "m2"], ["m1"]), presented,
        )
        self.assertEqual(groups, [["m0", "m2"], ["m1"]])

    def test_unplaced_ids_are_isolated(self):
        """提示コンテキストに居ない id は位置不明 → 1 件ずつ孤立させる。

        まとめて先頭の片へ寄せると、位置の根拠が無いまま隣接を作ってしまう
        (編纂側 _run_group_keys の「所属不明は孤立」と揃える。
        Codex 攻撃レビュー 四巡目 2026-07-27)。
        """
        presented = [msg("m0", 100), msg("m1", 101)]
        with self.assertLogs("sea.eviction_plan", "WARNING"):
            groups = compile_groups_from_folds(
                self._folds(["ghost1", "m0", "ghost2", "m1"]), presented,
            )
        self.assertEqual(groups, [["ghost1"], ["ghost2"], ["m0", "m1"]])

    def test_plan_eviction_output_is_never_split(self):
        """実際の退場計画を通した往復では割れない (契約が守られていることの検算)。"""
        msgs = [msg(f"m{i}", 100 + i) for i in range(8)]
        result = plan(msgs, low=2_000, target=0)
        self.assertTrue(result.folds)
        with self.assertNoLogs("sea.eviction_plan", "WARNING"):
            groups = compile_groups_from_folds(result.folds, msgs)
        self.assertEqual(groups, [f.message_ids for f in result.folds])


if __name__ == "__main__":
    unittest.main()
