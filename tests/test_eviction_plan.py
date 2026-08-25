"""退場計画 (sea/eviction_plan.py) の純関数テスト。

docs/intent/arasuji_levels.md §3 (一本規則) / §4 (レベル0 の特別さ) を固定する。

固定する仕様の骨子:

- 保護 = 残す量 (watermarks.target)。最新から遡ってこの分は退場させない。
  境界は pulse 関節へ古い側にスナップ。
- 保護より古い側は、古い順に U ずつの範囲に刻んで**全部**畳む。切り位置は
  pulse 関節に寄せる (U に達したら、いまの pulse を最後まで含めて切る)。
- エピソードに畳みを止める権利は無い — open episode も普通に畳まれる。
- 末尾 (保護範囲の直前) の U 未満の端数は畳まず残す (次回、新しい生ログと
  地続きで畳まれる — 小さい一次あらすじを作らない)。
- 既に畳まれた置き換え (壁) は材料に入れない。壁の手前の端数だけは、残すと
  永久に取り残されるので端数のまま畳む (旧世代データでのみ起きる経路)。
- **スペルの群は退場の境目で割れない** — 「唱え → 結果 → 結果を読んだ発話」の
  ひとまとまり (spell_origin_id の印) の内側に、保護境界も fold の切れ目も
  落とさない。
- compile_groups_from_folds: fold が「退場しないメッセージ」をまたいでいたら
  割ってから編纂へ渡す (偽の隣接の禁止)。
"""
from __future__ import annotations

import unittest

from sea.eviction_plan import (
    Fold,
    Watermarks,
    compile_groups_from_folds,
    plan_eviction,
)
from sea.session_window import FOLDED_MARKER

U = 2_000  # 一次あらすじの標準被覆 (テスト内での U)


def msg(mid, at, *, chars=1_000, ep=None, pulse=None, folded=False,
        spell_origin=None):
    """提示 payload 1 件。

    ``spell_origin`` は SAIMemory の ``spell_origin_id`` 列 (スペルの群の印)。
    **群の起点行 (最初の唱え) 自身は NULL** なので、起点は「自分の id が他行の
    spell_origin として現れる」ことでしか識別できない — テストの並びもその
    非対称のまま書く。
    """
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
    if spell_origin:
        payload["spell_origin_id"] = spell_origin
    return payload


def plan(messages, *, keep=2_000, high=None):
    """新仕様の呼び出し: watermarks.target = 残す量。low は互換用 (未使用)。"""
    return plan_eviction(
        messages, set(),
        Watermarks(low=0, target=keep, high=high),
        target_chars=U,
    )


def folded_ids(result):
    return [mid for f in result.folds for mid in f.message_ids]


class ProtectionTest(unittest.TestCase):
    """残す量: 直近はこの字数ぶん絶対に退場させない。"""

    def test_recent_keep_is_protected(self):
        msgs = [msg(f"m{i}", 100 + i) for i in range(6)]
        result = plan(msgs, keep=3_000)
        # 末尾 3,000字 = m3..m5 が保護範囲 → 候補は m0..m2
        self.assertEqual(result.protected_from, 3)
        for kept in ("m3", "m4", "m5"):
            self.assertNotIn(kept, folded_ids(result))

    def test_window_smaller_than_keep_is_all_protected(self):
        msgs = [msg("m0", 100), msg("m1", 101)]
        result = plan(msgs, keep=10_000)
        self.assertEqual(result.protected_from, 0)
        self.assertTrue(result.is_empty)

    def test_boundary_snaps_back_to_pulse_joint(self):
        """保護範囲の境界が pulse の途中に落ちたら古い側へ下げる
        (メッセージ単位でぶつ切りにしない = 保護を広げる向きに倒す)。"""
        msgs = [
            msg("a0", 100, pulse="p1"),
            msg("a1", 101, pulse="p2"),
            msg("a2", 102, pulse="p2"),
            msg("a3", 103, pulse="p2"),
        ]
        # 残す量 2,000字 → 素の境界は index 2 (a2,a3 で 2,000字) だが
        # a1/a2/a3 は同じ pulse なので index 1 まで下がる。
        result = plan(msgs, keep=2_000)
        self.assertEqual(result.protected_from, 1)

    def test_giant_single_pulse_does_not_deadlock_eviction(self):
        """1 つの pulse が残す量を超えていても、退場は必ず前進する。

        この並びは提示コンテキスト全体が 1 つの pulse = 1 単位なので、脱出弁は
        例外経路 (新しい側へ寄せると保護範囲が空になる) に入り、素の境界で切る
        (Codex レビュー 2026-07-28 medium)。
        """
        msgs = [
            msg(f"g{i}", 100 + i, chars=40_000, pulse="p1") for i in range(4)
        ]
        result = plan(msgs, keep=60_000)
        # 素の境界 = 新しい側 60,000字 (g2,g3) の手前 = index 2。
        self.assertEqual(result.protected_from, 2)
        self.assertFalse(result.is_empty)
        self.assertEqual(folded_ids(result), ["g0", "g1"])


class FoldSlicingTest(unittest.TestCase):
    """古い側を U ずつに刻む — 一本規則のレベル0 形。"""

    def test_candidates_are_folded_in_u_sized_chunks(self):
        # 候補 6,000字 (m0..m5) → U=2,000 ずつ 3 fold。全部畳まれる。
        msgs = [msg(f"m{i}", 100 + i) for i in range(8)]
        result = plan(msgs, keep=2_000)
        self.assertEqual(result.protected_from, 6)
        self.assertEqual(len(result.folds), 3)
        self.assertEqual(folded_ids(result), [f"m{i}" for i in range(6)])
        for fold in result.folds:
            self.assertGreaterEqual(fold.chars, U)

    def test_folds_are_time_ordered_and_disjoint(self):
        msgs = [msg(f"m{i}", 100 + i, chars=700) for i in range(12)]
        result = plan(msgs, keep=2_000)
        seen = []
        for fold in result.folds:
            for mid in fold.message_ids:
                self.assertNotIn(mid, seen)
                seen.append(mid)
        order = [m["id"] for m in msgs]
        self.assertEqual(seen, sorted(seen, key=order.index))

    def test_trailing_remainder_stays_unfolded(self):
        """末尾の U 未満の端数は畳まない — 次回、新しい生ログと地続きで
        畳まれるので、小さい一次あらすじを作らない (豆粒の禁止)。"""
        # 候補 5,000字 → fold 2 個 (2,000 + 2,000)、端数 1,000 は残る。
        msgs = [msg(f"m{i}", 100 + i, chars=500) for i in range(10)] + [
            msg("k0", 300), msg("k1", 301),
        ]
        result = plan(msgs, keep=2_000)
        self.assertEqual(len(result.folds), 2)
        total_folded = sum(f.chars for f in result.folds)
        self.assertEqual(total_folded, 4_000)

    def test_cut_snaps_to_pulse_joint(self):
        """U に達しても、いまの pulse を最後まで含めてから切る (発言の
        切れ目に寄せる)。"""
        msgs = [
            msg("a0", 100, pulse="p1"),
            msg("a1", 101, pulse="p2"),
            msg("a2", 102, pulse="p2"),  # a0+a1 で U 到達だが p2 の途中
            msg("k0", 200), msg("k1", 201), msg("k2", 202),
        ]
        result = plan(msgs, keep=3_000)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["a0", "a1", "a2"])


class NoEpisodeVetoTest(unittest.TestCase):
    """エピソードに畳みを止める権利は無い (intent §4-1)。"""

    def test_open_episode_is_folded_like_anything_else(self):
        # 旧仕様なら open episode は単独畳み・二段構えの対象だった。
        # 新仕様では帰属も開閉も畳みに影響しない — U で刻まれるだけ。
        msgs = [
            msg("a0", 100, ep="episode:1"),
            msg("b0", 101, ep="episode:2"),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["a0", "b0"])
        # episode_refs は記録される (被覆元の錨 — 判定には使わない)。
        self.assertEqual(result.folds[0].episode_refs, ["episode:1", "episode:2"])

    def test_open_episode_ref_is_never_set(self):
        """旧設計の部分エピソード記録 (open_episode_ref) は立てない。"""
        msgs = [msg("a0", 100, ep="episode:1"), msg("a1", 101, ep="episode:1"),
                msg("k0", 200), msg("k1", 201)]
        result = plan(msgs, keep=2_000)
        for fold in result.folds:
            self.assertIsNone(fold.open_episode_ref)
        self.assertFalse(result.used_last_resort_fold)


class WallTest(unittest.TestCase):
    """既に畳まれた置き換え (壁) の扱い。"""

    def test_wall_is_not_folded(self):
        msgs = [
            msg("w0", 100, folded=True),
            msg("m0", 101), msg("m1", 102),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000)
        self.assertNotIn("w0", folded_ids(result))
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["m0", "m1"])

    def test_stranded_remainder_before_wall_is_folded_undersized(self):
        """壁の手前の U 未満の端数は、残すと永久に取り残される (新入りは
        末尾にしか来ない) ので、端数のまま畳む。"""
        msgs = [
            msg("s0", 100, chars=500),      # 端数 (U 未満)
            msg("w0", 101, folded=True),    # 壁
            msg("m0", 102), msg("m1", 103),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000)
        ids = [f.message_ids for f in result.folds]
        self.assertIn(["s0"], ids)
        self.assertIn(["m0", "m1"], ids)
        self.assertNotIn("w0", folded_ids(result))


class SpellGroupIsNotSplitTest(unittest.TestCase):
    """スペルの群は退場の境目で割れない。

    群 = 「唱え (assistant) → 結果 (system ``[Spell Result: ...]``) → 結果を
    読んだ発話 (assistant)」のひとまとまり。境目が群の内側に落ちると、ペルソナの
    窓が結果の行から始まり「唱えた記憶が無いのに結果だけある」= 記憶の捏造に
    なる。

    群の内側には別 pulse の行が割り込む — 提示ウィンドウの絞り込み
    (main_line / committed) を通り抜ける 2 種類:

    1. event_message 行 (line_role NULL の legacy 救済で通る。pulse_id 無し)
    2. committed なメタ判断 (別 pulse_id)

    この割り込みで pulse の連続が切れるため、pulse だけを関節にしていた頃は
    スナップがそこで止まって境目が群の内側に落ちていた。
    """

    def _window_with_interloper(self, interloper):
        """[a0][唱え s0][割り込み][s0 を受けた発話][k0][k1] の並び。"""
        return [
            msg("a0", 100, pulse="p0"),
            msg("s0", 101, pulse="p1"),          # 唱え = 群の起点 (印は NULL)
            interloper,
            msg("s1", 103, pulse="p2", spell_origin="s0"),
            msg("k0", 104, pulse="p3"),
            msg("k1", 105, pulse="p4"),
        ]

    def _assert_group_not_split(self, result, group_ids):
        evicted = set(folded_ids(result))
        inside = [mid for mid in group_ids if mid in evicted]
        self.assertIn(
            len(inside), (0, len(group_ids)),
            f"スペルの群が退場の境目で割れている (退場側: {inside})",
        )

    def test_event_message_interloper_does_not_split_the_group(self):
        """pulse を持たない event_message が割り込んでも保護境界は群の先頭へ。

        これが本番で最頻の形 — スペルでペルソナが移動した直後の
        「[システム通知] 現在地が…に変わりました」が唱えと結果の間に入る。
        """
        msgs = self._window_with_interloper(msg("ev", 102))  # pulse_id 無し
        result = plan(msgs, keep=3_000)
        # 素の境界は index 3 (s1) — 群の内側。群の先頭 (index 1) まで下がる。
        self.assertEqual(result.protected_from, 1)
        self._assert_group_not_split(result, ["s0", "s1"])

    def test_meta_judgment_interloper_does_not_split_the_group(self):
        """committed なメタ判断 (別 pulse_id) が割り込んでも同じ。"""
        msgs = self._window_with_interloper(msg("mj", 102, pulse="pm"))
        result = plan(msgs, keep=3_000)
        self.assertEqual(result.protected_from, 1)
        self._assert_group_not_split(result, ["s0", "s1"])

    def test_origin_row_with_null_marker_is_part_of_the_group(self):
        """起点行の spell_origin_id は NULL — それでも群に含めて境目を下げる。

        印の付いた行 (s1) だけを群と見なすと区間の幅がゼロになり、境目が
        唱え (s0) と結果を読んだ発話 (s1) の間に落ちる。
        """
        msgs = self._window_with_interloper(msg("ev", 102))
        result = plan(msgs, keep=3_000)
        # 印を持つ最初の行 (index 3) ではなく、起点 (index 1) まで下がる。
        self.assertEqual(result.protected_from, 1)

    def test_fold_cut_does_not_close_inside_the_group(self):
        """U に達しても群の途中では fold を閉じない (最後のメンバーまで含める)。

        fold の切れ目も「唱えとその結果が別のあらすじに分かれる」境目なので、
        保護境界と同じ不変条件で守る。
        """
        msgs = [
            msg("a0", 100, pulse="p0"),
            msg("s0", 101, pulse="p1"),          # 唱え (U=2,000 はここで到達)
            msg("ev", 102),                       # 割り込み
            msg("s1", 103, pulse="p2", spell_origin="s0"),
            msg("a1", 104, pulse="p4"),
            msg("k0", 105, pulse="p5"),
            msg("k1", 106, pulse="p6"),
        ]
        result = plan(msgs, keep=2_000)
        self.assertEqual(result.protected_from, 5)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(
            result.folds[0].message_ids, ["a0", "s0", "ev", "s1"],
        )

    def test_escape_valve_pushes_the_boundary_past_the_unit_instead_of_splitting(self):
        """脱出弁の原則: 古い側へ寄せられないなら**新しい側へ**寄せる。

        素の境界を含む単位が提示の先頭から始まっていると、古い側へのスナップで
        境界が 0 になり退場候補が空になる。そこで境界を単位の終わりの次へ動かし、
        単位をまるごと退場候補に入れる — 群を割らずに前進できる。保護範囲は
        残す量より少し狭くなるが、``protected_from`` は報告用の値であって
        残す量を保証する契約は誰も持っていない。
        """
        msgs = [
            msg("s0", 100, chars=40_000, pulse="p1"),   # 唱え = 群の起点
            msg("ev", 101, chars=40_000),                # 割り込み (pulse 無し)
            msg("s1", 102, chars=40_000, pulse="p2", spell_origin="s0"),
            msg("k0", 103, chars=40_000, pulse="p3"),
            msg("k1", 104, chars=40_000, pulse="p4"),
        ]
        # 残す量 100,000字 → 素の境界は index 2 (s1) で群の内側。古い側へ寄せると
        # 群の先頭 = index 0 になり候補が消えるので、新しい側 (index 3) へ寄せる。
        result = plan(msgs, keep=100_000)
        self.assertEqual(result.protected_from, 3)
        # 群のメンバーは全員まとめて退場候補側 = 境目で割れていない。
        self.assertFalse(result.is_empty)
        self.assertEqual(folded_ids(result), ["s0", "ev", "s1"])
        self._assert_group_not_split(result, ["s0", "s1"])

    def test_escape_valve_does_not_warn_when_it_avoids_splitting(self):
        """新しい側へ寄せられた回は群を割っていないので WARNING を出さない。

        WARNING は「不変条件を手放した」印なので、手放していない回に出すと
        本当に割れた回が埋もれる。
        """
        msgs = [
            msg("s0", 100, chars=40_000, pulse="p1"),
            msg("ev", 101, chars=40_000),
            msg("s1", 102, chars=40_000, pulse="p2", spell_origin="s0"),
            msg("k0", 103, chars=40_000, pulse="p3"),
            msg("k1", 104, chars=40_000, pulse="p4"),
        ]
        with self.assertNoLogs("sea.eviction_plan", "WARNING"):
            plan(msgs, keep=100_000)

    def test_escape_valve_splits_and_warns_when_the_unit_is_the_whole_window(self):
        """例外: 提示コンテキスト全体が 1 つの単位なら素の境界で切る。

        新しい側へ寄せると保護範囲が空になり anchor の指す先が無くなるので、
        ここだけは単位を割ってでも前進する (「上限を超えたら必ず前進する」
        arasuji_levels.md §4-1)。ただし黙って通さない — どの群を割ったかが
        分かる WARNING を出す (INFO ではなく)。
        """
        msgs = [
            msg("s0", 100, chars=40_000, pulse="p1"),
            msg("ev", 101, chars=40_000),
            msg("s1", 102, chars=40_000, pulse="p2", spell_origin="s0"),
            msg("s2", 103, chars=40_000, pulse="p3", spell_origin="s0"),
        ]
        # 群が [s0..s2] 全体を覆う = 提示コンテキスト全体が 1 単位。
        with self.assertLogs("sea.eviction_plan", "WARNING") as logs:
            result = plan(msgs, keep=60_000)
        self.assertEqual(result.protected_from, 2)
        self.assertFalse(result.is_empty)
        self.assertEqual(folded_ids(result), ["s0", "ev"])
        self.assertTrue(any("s0" in line for line in logs.output))


class CompileGroupsTest(unittest.TestCase):
    """compile_groups_from_folds — 連続性の検算 (偽の隣接の禁止)。"""

    def test_contiguous_fold_passes_through(self):
        presented = [msg(f"m{i}", 100 + i) for i in range(4)]
        folds = [Fold(messages=presented[:2])]
        groups = compile_groups_from_folds(folds, presented)
        self.assertEqual(groups, [["m0", "m1"]])

    def test_fold_spanning_retained_message_is_split(self):
        presented = [msg("m0", 100), msg("r0", 101), msg("m1", 102)]
        folds = [Fold(messages=[presented[0], presented[2]])]
        groups = compile_groups_from_folds(folds, presented)
        self.assertEqual(groups, [["m0"], ["m1"]])

    def test_unplaced_ids_are_isolated(self):
        presented = [msg("m0", 100), msg("m1", 101)]
        folds = [Fold(messages=[presented[0], presented[1], msg("ghost", 999)])]
        groups = compile_groups_from_folds(folds, presented)
        self.assertIn(["ghost"], groups)
        self.assertIn(["m0", "m1"], groups)


class GuardTest(unittest.TestCase):
    """設定ミスの歯止め。"""

    def test_tiny_target_chars_skips_eviction(self):
        """U が置き換え見込み以下だと正味削減が常に 0 — 過剰退場になるので
        退場を見送る (WARNING)。"""
        msgs = [msg(f"m{i}", 100 + i) for i in range(6)]
        result = plan_eviction(
            msgs, set(), Watermarks(low=0, target=1_000, high=None),
            target_chars=500,
        )
        self.assertTrue(result.is_empty)


if __name__ == "__main__":
    unittest.main()
