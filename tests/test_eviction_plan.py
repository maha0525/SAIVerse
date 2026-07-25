"""退場計画 (sea/eviction_plan.py) の純関数テスト。

docs/intent/chronicle_eviction.md §3 (規範) / §4 (三水位) / §5 (手続き) と、
docs/intent/experience_structure.md §4 の圧縮七原則を、原則番号で固定する。
"""
from __future__ import annotations

import unittest

from sea.eviction_plan import (
    ESTIMATED_FOLD_PLACEHOLDER_CHARS,
    Watermarks,
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

    def test_small_open_is_not_folded(self):
        """U 未満の open は畳めない = 生のまま残る (小粒の会話 A が守られる)。"""
        msgs = [
            msg("a0", 100, chars=500, ep="episode:1"),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, ["episode:1"], low=2_000, target=0)
        self.assertTrue(result.is_empty)
        self.assertEqual(result.force_close_episode_ref, "episode:1")

    def test_large_open_folds_by_pulse_joint(self):
        """§6: U に達した open は pulse を丸ごと単位で刻んで部分退場する。"""
        msgs = [
            msg("a0", 100, ep="episode:1", pulse="p1"),
            msg("a1", 101, ep="episode:1", pulse="p1"),
            msg("a2", 102, ep="episode:1", pulse="p2"),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, ["episode:1"], low=2_000, target=2_000)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["a0", "a1"])
        self.assertEqual(result.folds[0].open_episode_ref, "episode:1")

    def test_open_never_bundles_with_neighbours(self):
        """§4-5: open を挟んで前後の closed をまたいで束ねない。"""
        msgs = [
            msg("c0", 100, ep="episode:1"),      # closed
            msg("o0", 101, chars=500, ep="episode:2"),  # open (U 未満)
            msg("c1", 102),                      # 無帰属
            msg("k0", 200), msg("k1", 201),
        ]
        # c0 + c1 なら U=2,000 に届くが、間に open があるのでまたげない。
        result = plan(msgs, ["episode:2"], low=2_000, target=0)
        self.assertTrue(result.is_empty)


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

    def test_under_u_run_is_not_folded(self):
        """U に届かない列は畳まない (小粒の一次あらすじを作らない)。"""
        msgs = [msg("c0", 100, chars=500), msg("k0", 200), msg("k1", 201)]
        result = plan(msgs, [], low=2_000, target=0)
        self.assertTrue(result.is_empty)


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
        self.assertTrue(result.is_empty)


if __name__ == "__main__":
    unittest.main()
