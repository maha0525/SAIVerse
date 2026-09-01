"""退場計画 (sea/eviction_plan.py) の純関数テスト。

docs/intent/arasuji_levels.md §3 (一本規則) / §4 (レベル0 の特別さ) を固定する。

固定する仕様の骨子:

- 保護 = 残す量 (watermarks.target)。最新から遡ってこの分は退場させない。
  境界は pulse 関節へ古い側にスナップ。**残す量は生の提示字数で数える**
  (提示コスト経済の水位なので、材料判定の裁定後も生のまま)。
- 保護より古い側は、古い順に U ずつの範囲に刻んで**全部**畳む。切り位置は
  pulse 関節に寄せる (U に達したら、いまの pulse を最後まで含めて切る)。
- **U に達したかは材料字数で測る** (2026-08-29 まはー裁定)。機構名義タグ
  (handy_tool / spell / event_message) の行は 500 字超なら材料では決定論の
  一行に縮む — 生の字数で U を測ると、スペルを呼ぶ流れだけで圧縮の意義が
  薄いあらすじ区間が量産される。
- 非常経路 (close_undersized_tail=True) だけは、fold が一つも閉じられない
  とき材料 U 未満の端数を閉じて前進を保証する (小粒は最後の手段のみ)。
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

from sai_memory.arasuji.generator import material_len
from sea.eviction_plan import (
    CONSUMED_PERCEPTION_KEY,
    Fold,
    Watermarks,
    compile_groups_from_folds,
    plan_eviction,
)
from sea.session_window import FOLDED_MARKER

U = 2_000  # 一次あらすじの標準被覆 (テスト内での U)


def msg(mid, at, *, chars=1_000, ep=None, pulse=None, folded=False,
        spell_origin=None, tags=None):
    """提示 payload 1 件。

    ``spell_origin`` は SAIMemory の ``spell_origin_id`` 列 (スペルの群の印)。
    **群の起点行 (最初の唱え) 自身は NULL** なので、起点は「自分の id が他行の
    spell_origin として現れる」ことでしか識別できない — テストの並びもその
    非対称のまま書く。

    ``tags`` は metadata.tags (機構名義の印 — 材料の長さ規則が読む)。
    """
    payload = {"id": mid, "content": "x" * chars, "created_at": at}
    meta = {}
    if ep:
        meta["origin_episode"] = ep
    if folded:
        meta[FOLDED_MARKER] = True
    if tags:
        meta["tags"] = list(tags)
    if meta:
        payload["metadata"] = meta
    if pulse:
        payload["pulse_id"] = pulse
    if spell_origin:
        payload["spell_origin_id"] = spell_origin
    return payload


def perception(at, *, chars=1_000):
    """送信直前に差し込まれる知覚ブロック (保存行ではないので ``id`` が無い)。

    組成は sea/runtime_context.py::list_presented_perception_blocks。
    """
    return {
        "role": "user",
        "content": "p" * chars,
        "created_at": at,
        "metadata": {
            "tags": ["internal", "event_message", "perception"],
            CONSUMED_PERCEPTION_KEY: True,
        },
    }


def plan(messages, *, keep=2_000, high=None, **kwargs):
    """新仕様の呼び出し: watermarks.target = 残す量。low は互換用 (未使用)。"""
    return plan_eviction(
        messages, set(),
        Watermarks(low=0, target=keep, high=high),
        target_chars=U,
        **kwargs,
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


class MaterialMeasureTest(unittest.TestCase):
    """U 判定の物差しは材料字数 (2026-08-29 まはー裁定)。

    機構名義タグ (handy_tool / spell / event_message) の行は本文が 500 字
    (generator.MECHANISM_TEXT_MAX_CHARS) を超えると、材料を組む時だけ決定論の
    一行 (数十字) に縮む。あらすじを作る理由は圧縮なので、U に達したかも
    その圧縮後 (材料) の字数で測る — 生の字数で測ると、スペルを呼ぶ一連の
    流れだけで圧縮の意義が薄いあらすじ区間が量産される。
    """

    #: 生 10,000 字のスペル結果行が材料で何字に縮むか (共有関数で実測)。
    SPELL_MATERIAL = material_len("x" * 10_000, ("spell",))

    def test_raw_heavy_material_thin_run_does_not_close_a_fold(self):
        """生では U 超でも、材料が U 未満なら fold は閉じない (端数持ち越し)。

        旧基準 (生の字数) ではこの並びは fold になっていた — 材料判定を
        無効化するとこのテストが落ちる。
        """
        self.assertLess(self.SPELL_MATERIAL, 100)  # 前提: 一行に縮んでいる
        msgs = [
            msg("s0", 100, chars=10_000, tags=["spell"]),
            msg("m0", 101),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000)
        self.assertTrue(result.is_empty)
        self.assertEqual(
            result.pending_material_chars, self.SPELL_MATERIAL + 1_000,
        )

    def test_fold_closes_when_material_reaches_u(self):
        """材料が U に達したら閉じる。削減見込み (projected) は生のまま。"""
        msgs = [
            msg("s0", 100, chars=10_000, tags=["spell"]),
            msg("m0", 101), msg("m1", 102),
            msg("k0", 200), msg("k1", 201),
        ]
        # 材料 = 一行 (数十字) + 1,000 + 1,000 ≥ U=2,000 → m1 まで含めて閉じる。
        result = plan(msgs, keep=2_000)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["s0", "m0", "m1"])
        # 提示サイズの見積もりは生の字数のまま (提示コスト経済の水位):
        # 総量 14,000 − (fold 生 12,000 − 置き換え見込み 1,200) = 3,200。
        self.assertEqual(result.total_chars, 14_000)
        self.assertEqual(result.projected_chars, 3_200)
        self.assertEqual(result.pending_material_chars, 0)

    def test_mechanism_row_at_threshold_counts_raw(self):
        """閾値 (500 字) ちょうどの機構行は縮まない — 生の字数のまま数える。"""
        msgs = [
            msg(f"s{i}", 100 + i, chars=500, tags=["spell"]) for i in range(4)
        ] + [msg("k0", 200), msg("k1", 201)]
        # 材料 = 500 × 4 = 2,000 ≥ U → 閉じる。
        result = plan(msgs, keep=2_000)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(
            result.folds[0].message_ids, ["s0", "s1", "s2", "s3"],
        )

    def test_stranded_run_before_wall_logs_undersized_by_material(self):
        """壁の手前の端数判定も材料字数 — 生 3,000 字でも材料が U 未満なら
        「端数のまま畳んだ」の INFO が出る (生基準ならこのログは出ない)。"""
        msgs = [
            msg("s0", 100, chars=3_000, tags=["spell"]),
            msg("w0", 101, folded=True),
            msg("m0", 102), msg("m1", 103),
            msg("k0", 200), msg("k1", 201),
        ]
        with self.assertLogs("sea.eviction_plan", "INFO") as logs:
            result = plan(msgs, keep=2_000)
        self.assertTrue(any("undersized" in line for line in logs.output))
        ids = [f.message_ids for f in result.folds]
        self.assertIn(["s0"], ids)
        self.assertIn(["m0", "m1"], ids)

    def test_undersized_tail_closes_only_on_the_emergency_flag(self):
        """非常経路 (close_undersized_tail=True) は材料 U 未満の端数も閉じ、
        INFO ログを残す。既定 (False) では閉じない。"""
        msgs = [
            msg("s0", 100, chars=10_000, tags=["spell"]),
            msg("m0", 101),
            msg("k0", 200), msg("k1", 201),
        ]
        self.assertTrue(plan(msgs, keep=2_000).is_empty)
        with self.assertLogs("sea.eviction_plan", "INFO") as logs:
            result = plan(msgs, keep=2_000, close_undersized_tail=True)
        self.assertTrue(any("非常経路" in line for line in logs.output))
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["s0", "m0"])
        self.assertEqual(result.pending_material_chars, 0)

    def test_emergency_flag_is_a_last_resort_when_normal_folds_exist(self):
        """通常の fold が 1 つでも閉じた回は、非常フラグでも端数を閉じない —
        前進は既に保証されており、小粒のあらすじは最後の手段にだけ許す。"""
        msgs = [
            msg("m0", 100), msg("m1", 101),
            msg("s0", 102, chars=10_000, tags=["spell"]),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000, close_undersized_tail=True)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["m0", "m1"])
        self.assertNotIn("s0", folded_ids(result))
        self.assertEqual(result.pending_material_chars, self.SPELL_MATERIAL)


class EmergencyCloseTest(unittest.TestCase):
    """非常経路 (close_undersized_tail=True) の端数閉じの歯止めと観測。"""

    def test_emergency_close_skips_when_nothing_would_shrink(self):
        """端数の生字数が置き換えの見込み (1,200 字) 以下なら、非常経路でも
        閉じない — LLM を呼んで entry を作るのに提示が 1 字も減らない無駄骨に
        なる (Codex 指摘 2026-08-29)。端数は従来どおり次回へ持ち越す。"""
        msgs = [
            msg("m0", 100, chars=1_000),
            msg("k0", 200), msg("k1", 201),
        ]
        with self.assertLogs("sea.eviction_plan", "INFO") as logs:
            result = plan(msgs, keep=2_000, close_undersized_tail=True)
        self.assertTrue(result.is_empty)
        self.assertTrue(any("減量ゼロ" in line for line in logs.output))
        # 持ち越しの報告値 (UI の「あと何字」) は生きている。
        self.assertEqual(result.pending_material_chars, 1_000)

    def test_emergency_close_does_not_warn_at_a_clean_joint(self):
        """端数が関節単位の末尾まで含んで閉じる通常の非常畳みは WARNING を
        出さない — 出すと本当に単位を割った回が埋もれる。"""
        msgs = [
            msg("s0", 100, chars=10_000, tags=["spell"]),
            msg("m0", 101),
            msg("k0", 200), msg("k1", 201),
        ]
        with self.assertLogs("sea.eviction_plan", "INFO") as logs:
            result = plan(msgs, keep=2_000, close_undersized_tail=True)
        self.assertEqual(len(result.folds), 1)
        self.assertFalse(
            any(r.levelno >= 30 for r in logs.records),  # 30 = WARNING
            f"clean joint close should not warn: {logs.output}",
        )

    def test_emergency_close_warns_when_it_splits_the_whole_window_unit(self):
        """窓全体が一つのスペル群 (= 一つの関節単位) + 非常経路の組み合わせ:
        脱出弁例外が素の境界で単位を割り、端数閉じも群を割って閉じる。

        Codex レビュー (2026-08-29) の「単位の途中なら defer せよ」は採らない
        (割らないと畳めるものが永久に現れず、提示が痩せない手詰まりが再導入
        される — 割ること自体は 2026-08-25 に設計として受け入れ済み)。代わりに
        黙って通さない — 脱出弁と同じ格の WARNING を出したうえで fold は閉じる。
        """
        msgs = [
            msg("s0", 100, chars=40_000, pulse="p1", tags=["spell"]),
            msg("s1", 101, chars=40_000, pulse="p2", spell_origin="s0",
                tags=["spell"]),
            msg("s2", 102, chars=40_000, pulse="p3", spell_origin="s0",
                tags=["spell"]),
        ]
        # 生 120,000 字だが材料は一行 × 3 — 通常計画は fold を閉じられない。
        with self.assertLogs("sea.eviction_plan", "WARNING") as logs:
            result = plan(msgs, keep=60_000, close_undersized_tail=True)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["s0"])
        self.assertTrue(
            any("割って端数の fold を閉じた" in line for line in logs.output),
            f"expected the unit-split warning: {logs.output}",
        )


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


class InjectedPerceptionTest(unittest.TestCase):
    """差し込みの知覚ブロックは**重さだけ**が計画に参加する。

    docs/issues/context_accounting_excludes_injected_rows.md (2026-09-02 まはー
    裁定): 勘定の単位は「実際に送る中身」。知覚バッチは保存行を作らず送信直前に
    差し込まれるので、計画の入力にも時刻順マージして渡す。ただしブロックは
    退場の対象ではない (id が無く、提示から下りるのは付記印の仕事) —
    fold の中身にも、chunk の境目にもならない。
    """

    def test_blocks_add_weight_but_never_enter_a_fold(self):
        msgs = [
            msg("m0", 100), perception(101, chars=1_500), msg("m1", 102),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000)
        self.assertEqual(len(result.folds), 1)
        # 束ねの中身も境目 (start_at/end_at) も保存行のまま。
        self.assertEqual(result.folds[0].message_ids, ["m0", "m1"])
        self.assertEqual(result.folds[0].start_at, 100)
        self.assertEqual(result.folds[0].end_at, 102)
        # 合計は送る中身 (保存行 4,000 + 知覚 1,500)。
        self.assertEqual(result.total_chars, 5_500)

    def test_projected_chars_counts_the_folded_span_blocks_as_gone(self):
        """畳んだ範囲の知覚も「消える側」に数える。

        あらすじ確定と同一トランザクションで、その期間の知覚バッチに付記印が
        付いて提示から下りる (sai_memory/arasuji/executor.py)。
        """
        blocks = [perception(101, chars=1_500)]
        with_block = plan(
            [msg("m0", 100), blocks[0], msg("m1", 102),
             msg("k0", 200), msg("k1", 201)],
            keep=2_000,
        )
        without = plan(
            [msg("m0", 100), msg("m1", 102), msg("k0", 200), msg("k1", 201)],
            keep=2_000,
        )
        # 保存行ぶんの削減は同じ。知覚 1,500 字がそのまま上乗せで消える。
        self.assertEqual(
            with_block.total_chars - with_block.projected_chars,
            (without.total_chars - without.projected_chars) + 1_500,
        )

    def test_trailing_gap_blocks_are_not_counted_as_removed(self):
        """束の最後の保存行より後のブロックは「消える側」に数えない。

        付記の時刻範囲は群の末尾 + 1 まで (executor._annex_time_spans) なので、
        fold 末尾と次の保存行の間に消費されたブロックは付記から漏れて提示に
        残る (次回編纂の recover_before が拾うまで一巡残る)。消える側に数えると
        削減見込みが過大になる (Codex 指摘 2026-09-02)。
        """
        with_gap_block = plan(
            [msg("m0", 100), msg("m1", 101), perception(150, chars=1_500),
             msg("k0", 200), msg("k1", 201)],
            keep=2_000,
        )
        without = plan(
            [msg("m0", 100), msg("m1", 101), msg("k0", 200), msg("k1", 201)],
            keep=2_000,
        )
        # fold の中身は保存行のまま、削減見込みは素の計画と同じ (ブロックは
        # 残る側)。合計にはブロックが乗る。
        self.assertEqual(folded_ids(with_gap_block), ["m0", "m1"])
        self.assertEqual(
            with_gap_block.total_chars - with_gap_block.projected_chars,
            without.total_chars - without.projected_chars,
        )
        self.assertEqual(
            with_gap_block.total_chars, without.total_chars + 1_500,
        )

    def test_boundary_is_never_placed_between_a_row_and_its_block(self):
        """ブロックは直前の保存行の単位へ接着する — 境目はその間に落ちない。

        落ちると fold の端が id の無い行になり、編纂へ渡す範囲も圧縮区間の
        記録も作れない。
        """
        msgs = [
            msg("m0", 100), msg("m1", 101),
            msg("k0", 200), perception(201, chars=2_000),
        ]
        # 素の境界は末尾の知覚ブロック (添字 3) に落ちるが、単位の先頭 (k0) へ
        # 下がるので保護は k0 から。候補は m0/m1 = U ちょうど。
        result = plan(msgs, keep=2_000)
        self.assertEqual(result.protected_from, 2)
        self.assertEqual(folded_ids(result), ["m0", "m1"])

    def test_leading_blocks_are_absorbed_by_the_first_stored_unit(self):
        """窓の先頭より古いバッチ (提示は先頭に出る) も単独の単位を作らない。

        ただし削減見込みには数えない — 回収路 (recover_before) は前回編纂の
        末尾以前しか拾わないので、先頭の保存行より前のブロックは今回の付記
        から漏れて提示に残りうる (Codex 指摘 2026-09-02 四巡目)。次回編纂の
        回収路が拾うまで一巡残る側に倒す。
        """
        msgs = [
            perception(90, chars=800), msg("m0", 100), msg("m1", 101),
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000)
        self.assertEqual(len(result.folds), 1)
        self.assertEqual(result.folds[0].message_ids, ["m0", "m1"])
        # 先頭ブロックは m0 の単位に吸収される (境目にならない) が、削減は
        # 保存行ぶんだけ。ブロック 800 字は残る側 (合計には乗る)。
        self.assertEqual(
            result.total_chars - result.projected_chars,
            2_000 - 1_200,
        )
        self.assertEqual(result.total_chars, 4_800)

    def test_trailing_blocks_do_not_push_a_thin_run_to_U(self):
        """U 到達判定の母集合も _reduction_basis — 末尾の隙間ブロックは入れない。

        入れると、保存行の材料が乏しいのにブロックの重みで U 到達と誤認し、
        「畳んでも減らない fold」を発行して LLM 代だけ払う (Codex 指摘
        2026-09-02 三巡目)。500 字以下のブロックは機構縮約を受けず全文が材料に
        乗るので、その形で組む。
        """
        msgs = [msg("m0", 100, chars=500)] + [
            perception(110 + i, chars=450) for i in range(4)
        ] + [msg("k0", 200), msg("k1", 201)]
        # 旧判定: 材料 500 + 450×4 = 2,300 ≥ U=2,000 で閉じてしまう。
        # 新判定: 母集合 (m0 のみ) = 500 < 2,000 → 閉じない。
        result = plan(msgs, keep=2_000)
        self.assertTrue(result.is_empty)
        # 「あと何字」の報告も同じ母集合。
        self.assertEqual(result.pending_material_chars, 500)

    def test_a_run_of_only_blocks_does_not_close_an_empty_fold(self):
        """保存行を含まない束は畳まない (空の fold は退場も編纂もできない)。"""
        msgs = [perception(100 + i, chars=3_000) for i in range(3)] + [
            msg("k0", 200), msg("k1", 201),
        ]
        result = plan(msgs, keep=2_000)
        self.assertTrue(result.is_empty)


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
