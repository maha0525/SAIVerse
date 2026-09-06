"""知覚の合計提示量の二水位と、下ろした跡地の省略の印 (2026-09-04 まはー裁定)。

対象は sea/runtime_context.py の提示組成 (`list_presented_perception_blocks`) と、
その永続の相棒 sai_memory/perception_buffer.py の下ろした境界
(`perception_presentation` の 1 行)。

契約 (裁定の文面そのもの):

1. 合計が上の水位以下なら何も起きない。超えたら古い側から下の水位まで**まとめて**
   下りる (一個ずつではない)。
2. 境界は一方向にだけ進む — 一度下ろしたバッチは、後で圧力が下がっても提示に
   戻らない。
3. 下ろした区間には機構名義の省略の印が出る (黙って消さない)。
4. 台帳の行は消えない。下ろされた期間の編纂は従来どおり材料として引き取る。
5. 境界の前進が「部屋の様子」の全文バッチを越えるとき、残った差分へ全文が
   移管される (土台を失わない)。
6. のろけゆきさんの形 (会話 4 万字・知覚 18 万字) で、初回の下ろしが下の水位まで
   絞り、Metabolism の水位が満たせる状態に戻る。
7. 下ろす量は**移管で全文へ膨らんだ後**の字数で決める。新着が一件も無い次の
   呼び出しで境界がまた進むことはない (提示は測定と送信の間で安定する)。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sai_memory.perception_buffer import (
    advance_presentation_cutoff,
    create_consumption_batch,
    get_presentation_cutoff,
    init_perception_buffer_table,
    insert_presentation_batch,
    list_dropped_batches,
    list_pending,
    list_presented_batches,
    list_unannexed_batches,
    mark_batches_annexed,
    push_perception,
)
from sai_memory.room_state import (
    ROOM_STATE_KIND,
    render_room_diff,
    render_room_full,
    restore_room_state_bases,
    room_key,
    snapshot_digest,
)
from sea.eviction_plan import is_injected_perception, message_chars
from sea.runtime_context import (
    _perception_block_text,
    _perception_suffix_totals,
    _plan_perception_drop,
    _presented_chars_after_transfer,
    _room_reseat_projection,
    list_presented_perception_blocks,
    merge_perception_blocks,
)

#: Chronicle 有効相当 (lifecycle 無し = 判定不能 → 有効側に倒す)。
_RUNTIME = SimpleNamespace(session_lifecycle=None)
#: Chronicle 無効のペルソナ (「編纂なしで忘れる」を選んだ)。
_RUNTIME_NO_CHRONICLE = SimpleNamespace(
    session_lifecycle=SimpleNamespace(
        is_chronicle_enabled_for_persona=lambda persona: False,
    ),
)

#: ブロック 1 個ぶんの ``<system>`` 包みの長さ (提示字数の勘定に乗る)。
_WRAP_CHARS = len("<system></system>")


def _room_bundle(body_lines, *, building="b1", name="工房", key_id="1"):
    """開いたドキュメント 1 個を持つ部屋の束 (本文行でサイズを調整する)。

    部屋の様子の snapshot は 2026-09-06 からパッケージの束 (JSON オブジェクト)
    — 文字列の snapshot は旧形式 = 連なりの外になる (room_state_packages.md §9)
    ので、部屋の連なりを検査するテストの材料はこの形で作る。
    """
    label = f"[item:{key_id}] [Document] 覚え書き"
    return {
        "building_id": building,
        "building_name": name,
        "packages": [{
            "key": f"item:{key_id}", "family": "item", "label": label,
            "lines": [label, "(Open)", "```", *body_lines, "```"],
            "media": [], "state": "open",
        }],
    }


class PerceptionCapTestBase(unittest.TestCase):
    """生の conn に消費バッチを積んで、提示の組成を回す土台。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)
        self.clock = 1000
        self.persona = SimpleNamespace(
            persona_id="p1",
            model="test-model",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(), is_ready=lambda: True,
            ),
        )

    def _batch(
        self, rendered_text: str, *, room_state_json=None, records: int = 1,
        reduce_key=None,
    ) -> int:
        """確定文面を指定して消費バッチを 1 件作る。

        ``records`` は台帳に積む知覚の件数 — 1 枚のバッチは Beat 頭に溜まって
        いた知覚を全部束ねるので、バッチ数と記録の件数は別物 (省略の印の件数は
        後者で出す)。``reduce_key`` を渡すと消費時の reduce が効く形になる。
        """
        for i in range(records):
            push_perception(
                self.conn, "world_state", f"seed {i}", reduce_key=reduce_key,
            )
        pending = [it.id for it in list_pending(self.conn)]
        self.clock += 10
        return create_consumption_batch(
            self.conn, pending, consumed_at=self.clock,
            rendered_text=rendered_text, room_state_json=room_state_json,
        )

    def _blocks(self, runtime=_RUNTIME, recent=(), advance_cutoff=True):
        return list_presented_perception_blocks(
            runtime, self.persona, list(recent), raise_on_error=True,
            advance_cutoff=advance_cutoff,
        )

    def _watermarks(self, target: int, high):
        """知覚の二水位を差し替える (組み込み既定に依存しないため)。"""
        return patch(
            "sea.runtime_context.resolve_perception_watermarks",
            return_value=(target, high),
        )


class PerceptionCapDropTest(PerceptionCapTestBase):
    """上の水位を超えたら下の水位までまとめて下ろす。"""

    def setUp(self):
        super().setUp()
        # 1 件 1,000 字 × 5 件 = 提示 5,085 字 (包み込み)。
        self.ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]

    def test_below_the_high_watermark_nothing_happens(self):
        with self._watermarks(2_000, 60_000):
            blocks = self._blocks()
        self.assertEqual(len(blocks), 5)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)
        self.assertFalse(any("省略" in b["content"] for b in blocks))

    def test_over_the_high_watermark_drops_down_to_the_target(self):
        with self._watermarks(2_000, 3_000):
            blocks = self._blocks()
        # 印 1 枚 + 残った知覚ブロック。合計は下の水位以下。
        perception_chars = sum(
            len(b["content"]) for b in blocks if "省略" not in b["content"]
        )
        self.assertLessEqual(perception_chars, 2_000)
        self.assertEqual(get_presentation_cutoff(self.conn), self.ids[3])
        self.assertEqual(
            [b.id for b in list_presented_batches(self.conn)], [self.ids[4]],
        )

    def test_the_drop_happens_in_one_step_not_one_batch_at_a_time(self):
        """一回の提示で下の水位まで届く (次の提示で更に下がらない)。"""
        with self._watermarks(2_000, 3_000):
            self._blocks()
            first = get_presentation_cutoff(self.conn)
            self._blocks()
        self.assertEqual(get_presentation_cutoff(self.conn), first)

    def test_the_newest_block_is_never_dropped(self):
        """単独で下の水位を超える一個は下ろさず、超過を許して旗を立てる。"""
        with self._watermarks(100, 200):
            blocks = self._blocks()
        presented = [b for b in blocks if "省略" not in b["content"]]
        self.assertEqual(len(presented), 1)
        self.assertEqual(presented[0]["metadata"]["__perception_batch_id__"], self.ids[4])

    def test_a_model_can_opt_out_of_dropping(self):
        with self._watermarks(2_000, None):
            blocks = self._blocks()
        self.assertEqual(len(blocks), 5)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)


class PerceptionCapOneWayTest(PerceptionCapTestBase):
    """境界は一方向にだけ進む (下ろしたものは戻らない)。"""

    def setUp(self):
        super().setUp()
        self.ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]
        with self._watermarks(2_000, 3_000):
            self._blocks()
        self.cutoff = get_presentation_cutoff(self.conn)
        self.assertEqual(self.cutoff, self.ids[3])

    def test_lower_pressure_does_not_bring_dropped_batches_back(self):
        with self._watermarks(200_000, 300_000):
            blocks = self._blocks()
        shown = [
            b["metadata"].get("__perception_batch_id__") for b in blocks
            if "省略" not in b["content"]
        ]
        self.assertEqual(shown, [self.ids[4]])
        self.assertEqual(get_presentation_cutoff(self.conn), self.cutoff)

    def test_the_cutoff_never_moves_backwards(self):
        """新しいバッチが積まれても境界は下がらない。"""
        newest = self._batch("Z" * 100)
        with self._watermarks(200_000, 300_000):
            blocks = self._blocks()
        shown = [
            b["metadata"].get("__perception_batch_id__") for b in blocks
            if "省略" not in b["content"]
        ]
        self.assertEqual(shown, [self.ids[4], newest])
        self.assertEqual(get_presentation_cutoff(self.conn), self.cutoff)

    def test_a_new_batch_is_never_born_below_the_cutoff(self):
        """境界は id で持つので、後から積むバッチが黙って隠れることはない。"""
        newest = self._batch("Z" * 100)
        self.assertGreater(newest, get_presentation_cutoff(self.conn))


class PerceptionCutoffCrossProcessTest(PerceptionCapTestBase):
    """別プロセスが先に境界を進めていたら、提示は**実境界**に従う。

    ``advance_presentation_cutoff`` は一方向なので、こちらの planned より先まで
    進んでいたら no-op になり、返るのは**進んだ後の実境界**。戻り値を捨てて
    planned で振り分けると、既に下ろされたバッチが一回だけ提示に復活する
    (下ろす瞬間に一度きり、の片道性が破れる)。
    """

    def test_the_real_cutoff_wins_over_the_planned_one(self):
        import sai_memory.perception_buffer as pb

        ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]
        real = pb.advance_presentation_cutoff

        def racing(conn, batch_id, **kwargs):
            # この呼び出しの直前に、別プロセスがもっと先まで下ろした体。
            # kwargs (in_window — 九巡目修正 1 で増えた窓の篩) はこちらの
            # 呼び出しへそのまま中継する。先行プロセスの advance は別の組成
            # なので、この stub では篩なし (窓なし相当) のまま。
            real(conn, ids[3])
            return real(conn, batch_id, **kwargs)

        # 合計 5,085 字 > 上限 5,000 → 1 枚下ろせば下の水位 (4,500) に届く =
        # planned は ids[0] 止まり。実境界は ids[3] まで進んでいる。
        with self._watermarks(4_500, 5_000), patch.object(
            pb, "advance_presentation_cutoff", racing,
        ):
            blocks = self._blocks()

        shown = [
            b["metadata"].get("__perception_batch_id__") for b in blocks
            if "省略" not in b["content"]
        ]
        self.assertEqual(shown, [ids[4]])
        self.assertEqual(get_presentation_cutoff(self.conn), ids[3])


class PerceptionCutoffGuardBlockedWriteTest(unittest.TestCase):
    """ガードで UPSERT が無効化された回でも、返り値は実境界 (十巡目)。

    上の CrossProcess テストは「呼び出し前の読みで既に先へ進んでいた」形 =
    早期 return が実境界を返す経路を受け持つ。こちらは残るもう一つの隙間 —
    A が current を読んだ**後**、UPSERT より**前**に、別接続 B が先にもっと
    大きい境界へ進めて commit した形。UPSERT は「大きい方だけ勝つ」ガードで
    無効になるのに、返り値だけが自分の target を名乗ると、呼び出し側
    (sea/runtime_context.py の実送信経路) はその値で提示を組み、B が既に
    下ろしたバッチを再提示する (一方向のはずの境界の見かけ上の揺り戻し)。

    二接続が要るので、ベース (:memory:) ではなくファイル DB を張る。
    """

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db_path = os.path.join(tmp, "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        self.addCleanup(self.conn.close)
        init_perception_buffer_table(self.conn)
        self.clock = 1000
        self.ids = [self._batch(chr(ord("A") + i) * 100) for i in range(5)]
        self.conn.commit()  # B (別接続) が書けるよう、A のロックを手放しておく

    def _batch(self, rendered_text: str) -> int:
        push_perception(self.conn, "world_state", "seed")
        pending = [it.id for it in list_pending(self.conn)]
        self.clock += 10
        return create_consumption_batch(
            self.conn, pending, consumed_at=self.clock,
            rendered_text=rendered_text,
        )

    def test_a_guard_blocked_write_returns_the_real_cutoff(self):
        import sai_memory.perception_buffer as pb

        other = sqlite3.connect(self.db_path)
        self.addCleanup(other.close)
        real_get = pb.get_presentation_cutoff
        raced = {"done": False}

        def read_then_lose_the_race(conn):
            value = real_get(conn)
            if not raced["done"]:
                raced["done"] = True
                # A の「current の読み」と UPSERT の間に、別接続 B が先へ
                # 進めて commit した体。A はロックを持っていない (SELECT は
                # tx を開かない) ので、B の書き込みはブロックされずに通る。
                real_advance(other, self.ids[3])
                other.commit()
            return value

        real_advance = pb.advance_presentation_cutoff
        with patch.object(pb, "get_presentation_cutoff", read_then_lose_the_race):
            returned = real_advance(self.conn, self.ids[0])
        self.conn.commit()

        # DB の実境界は B の値のまま (A のガード付き UPSERT は無効 = 後退なし)。
        self.assertEqual(get_presentation_cutoff(self.conn), self.ids[3])
        # 返り値も実境界 — 自分の target (ids[0]) を名乗らない。
        self.assertEqual(returned, self.ids[3])
        # 呼び出し側相当: 返り値で提示を組んでも、B が下ろした ids[1..3] は
        # 再提示されない (runtime_context の `b.id > dropped_through` と同じ篩)。
        self.assertEqual(
            [b.id for b in list_presented_batches(self.conn, cutoff=returned)],
            [self.ids[4]],
        )


class PerceptionCutoffMeasureOnlyTest(PerceptionCapTestBase):
    """``advance_cutoff=False`` = 測るだけ (2026-09-05 四巡目 #6)。

    読み取り専用の画面 (context-status) と仮定の窓の下見は、下ろし境界を進めて
    はいけない — 一方向で取り消せない値を、実際には送らない列で確定させない。
    判定は同じように行い、進めた**つもり**の提示を返すので、画面の数字と実送信
    はズレない。
    """

    def setUp(self):
        super().setUp()
        self.ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]

    def test_measuring_does_not_move_the_cutoff(self):
        with self._watermarks(2_000, 3_000):
            self._blocks(advance_cutoff=False)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)

    def test_measuring_many_times_still_does_not_move_it(self):
        with self._watermarks(2_000, 3_000):
            for _ in range(5):
                self._blocks(advance_cutoff=False)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)

    def test_the_measured_presentation_matches_the_sent_one(self):
        with self._watermarks(2_000, 3_000):
            measured = self._blocks(advance_cutoff=False)
            sent = self._blocks(advance_cutoff=True)
        self.assertEqual(measured, sent)
        self.assertEqual(get_presentation_cutoff(self.conn), self.ids[3])

    def test_the_measured_total_matches_the_sent_one(self):
        with self._watermarks(2_000, 3_000):
            measured = message_chars(self._blocks(advance_cutoff=False))
            sent = message_chars(self._blocks(advance_cutoff=True))
        self.assertEqual(measured, sent)


class PerceptionCutoffMeasureOnlyRoomStateTest(PerceptionCapTestBase):
    """測るだけの回でも、土台を失う差分は全文へ開いた姿で数える。

    進めるモードは境界の前進と同一 tx の回復 (``restore_room_state_bases``) が
    台帳の確定文面を書き換える。測るだけのモードはそれができないので、提示時の
    開き直し (``reopen_lost_bases``) が同じ結果を作る — 両者が違う姿を返すと、
    画面の数字が実送信より小さい嘘になる (勘定と送信は同じ一枚を見る、の規則)。
    """

    def setUp(self):
        super().setUp()
        self.bundle_a = _room_bundle(["定規と写真がある。"] * 100)
        self.bundle_b = _room_bundle(
            ["定規と写真がある。"] * 100 + ["ノギスが増えた。"],
        )
        self.full = render_room_full(self.bundle_a)
        self.full2 = render_room_full(self.bundle_b)
        self.diff_text = render_room_diff(self.bundle_a, self.bundle_b)["content"]
        key = room_key("b1")
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": key, "is_diff": False, "block": self.full,
            "snapshot": self.bundle_a,
        }], ensure_ascii=False))
        self.diff_id = self._batch(self.diff_text, room_state_json=json.dumps([{
            "key": key, "is_diff": True, "block": self.diff_text,
            "snapshot": self.bundle_b,
            "base_digest": snapshot_digest(self.bundle_a),
        }], ensure_ascii=False))
        self.filler = self._batch("Z" * 1_000)
        # 「土台の 1 枚だけが下りる」水位を材料から決める (魔法の数字を置かない)。
        # totals[1] = 1 枚下ろした後の合計 = そこで止まる下の水位。
        totals = _perception_suffix_totals(list_presented_batches(self.conn))
        self.cap = self._watermarks(totals[1], totals[1])
        self.assertGreater(totals[0], totals[1])

    def test_the_measured_text_is_the_reopened_one(self):
        with self.cap:
            measured = self._blocks(advance_cutoff=False)
        shown = [b for b in measured if "省略" not in b["content"]]
        self.assertEqual(
            [b["metadata"]["__perception_batch_id__"] for b in shown],
            [self.diff_id, self.filler],
        )
        self.assertIn("定規と写真がある。", shown[0]["content"])
        self.assertIn("ノギスが増えた。", shown[0]["content"])
        # 台帳の確定文面は差分のまま (測るだけの回は何も書かない)。
        batch = next(
            b for b in list_presented_batches(self.conn) if b.id == self.diff_id
        )
        self.assertEqual(batch.rendered_text, self.diff_text)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)

    def test_measuring_and_sending_agree_on_the_text(self):
        with self.cap:
            measured = self._blocks(advance_cutoff=False)
            sent = self._blocks(advance_cutoff=True)
        self.assertEqual(measured, sent)
        # 送る側は台帳へ回復を書き戻す (可視性が変わる書き込み点)。
        batch = next(
            b for b in list_presented_batches(self.conn) if b.id == self.diff_id
        )
        self.assertEqual(batch.rendered_text, self.full2)


class PerceptionOmissionMarkTest(PerceptionCapTestBase):
    """下ろした跡地の省略の印 (機構名義・連続区間を一つに束ねる)。"""

    def setUp(self):
        super().setUp()
        # 1 枚のバッチが 3 件の記録を束ねる形 (Beat 頭に溜まった知覚をまとめて
        # 消費した回)。件数はバッチ数ではなく記録数で出る、をここで固定する。
        self.ids = [
            self._batch(chr(ord("A") + i) * 1_000, records=3) for i in range(5)
        ]
        with self._watermarks(2_000, 3_000):
            self.blocks = self._blocks()
        self.mark = self.blocks[0]

    def test_the_mark_counts_records_not_batches(self):
        self.assertIn("[省略された記録]", self.mark["content"])
        # 4 バッチ × 3 件 = 12 件。バッチ数 (4) ではない。
        self.assertIn("12 件以上", self.mark["content"])
        self.assertEqual(self.mark["metadata"]["__perception_omitted__"], 12)
        self.assertEqual(
            self.mark["metadata"]["__perception_omitted_batches__"], 4,
        )

    def test_an_unreadable_ledger_still_counts_one_per_batch(self):
        """台帳の行を引けないバッチは 1 と数える (合計は必ず下限)。"""
        from sai_memory.perception_buffer import count_batch_records

        self.conn.execute("DELETE FROM perception_buffer")
        self.conn.commit()
        counts = count_batch_records(self.conn, self.ids[:4])
        self.assertEqual(sum(counts.values()), 4)

    def test_the_mark_does_not_promise_an_arasuji_that_has_not_run(self):
        """編纂前でも嘘にならない書き方 (「引き継がれます」= 将来の編纂が条件)。"""
        self.assertIn("記憶の整理の際にあらすじへ引き継がれます", self.mark["content"])
        self.assertNotIn("あらすじに残ります", self.mark["content"])

    def test_the_mark_is_a_mechanism_row_not_a_conversation_row(self):
        self.assertTrue(is_injected_perception(self.mark))
        self.assertIn("event_message", self.mark["metadata"]["tags"])
        self.assertNotIn("id", self.mark)

    def test_the_mark_sits_where_the_dropped_span_was(self):
        rest = [b for b in self.blocks[1:]]
        self.assertTrue(all(self.mark["created_at"] <= b["created_at"] for b in rest))
        merged = merge_perception_blocks([], self.blocks)
        self.assertEqual(merged[0], self.mark)

    def test_a_consecutive_span_is_bundled_into_one_mark(self):
        marks = [b for b in self.blocks if "省略" in b["content"]]
        self.assertEqual(len(marks), 1)

    def test_the_mark_promises_the_arasuji_only_when_chronicle_is_on(self):
        self.assertIn("あらすじ", self.mark["content"])
        with self._watermarks(2_000, 3_000):
            off = self._blocks(runtime=_RUNTIME_NO_CHRONICLE)
        self.assertIn("[省略された記録]", off[0]["content"])
        self.assertNotIn("あらすじ", off[0]["content"])

    def test_the_mark_disappears_once_the_span_is_annexed(self):
        mark_batches_annexed(self.conn, self.ids[:4], "entry-1")
        self.conn.commit()
        with self._watermarks(2_000, 3_000):
            blocks = self._blocks()
        self.assertFalse(any("省略" in b["content"] for b in blocks))


class PerceptionOmissionCountTest(PerceptionCapTestBase):
    """省略の印の件数は「消費時と同じ reduce をかけた後の記録数」。"""

    def test_a_reduced_group_counts_as_the_one_line_it_became(self):
        # 1 枚目 = 同じ reduce_key の 4 件 (文面には 1 件として出た)、
        # 2 枚目 = 2 件、3 枚目 = 1 件 (最新なので下ろされない)。
        self._batch("X" * 1_000, records=4, reduce_key="c:5")
        self._batch("Y" * 1_000, records=2)
        self._batch("Z" * 1_000)
        with self._watermarks(1_100, 1_500):
            blocks = self._blocks()
        self.assertIn("3 件以上", blocks[0]["content"])
        self.assertEqual(blocks[0]["metadata"]["__perception_omitted__"], 3)


class PerceptionCapLedgerTest(PerceptionCapTestBase):
    """下ろすのは提示だけ — 台帳と編纂の一括回収はそのまま。"""

    def setUp(self):
        super().setUp()
        self.ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]
        with self._watermarks(2_000, 3_000):
            self._blocks()

    def test_the_ledger_rows_survive_the_drop(self):
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], self.ids,
        )
        self.assertEqual(
            [b.id for b in list_dropped_batches(self.conn)], self.ids[:4],
        )

    def test_the_annexation_of_the_dropped_span_still_collects_them(self):
        from sai_memory.arasuji.executor import collect_annex_items

        items, batch_ids = collect_annex_items(self.conn, 0, self.clock + 1)
        self.assertEqual(batch_ids, self.ids)
        self.assertEqual(items[0]["text"], "A" * 1_000)

    def test_the_recovery_sweep_also_reaches_dropped_batches(self):
        """先頭チャンクの一括回収 (recover_before) は境界を知らないままでよい。"""
        from sai_memory.arasuji.executor import collect_annex_items

        _, batch_ids = collect_annex_items(
            self.conn, self.clock, self.clock + 1,
            recover_before=self.clock - 1,
        )
        self.assertEqual(batch_ids, self.ids)

    def test_annexing_a_dropped_batch_stamps_it_as_usual(self):
        stamped = mark_batches_annexed(self.conn, [self.ids[0]], "entry-1")
        self.conn.commit()
        self.assertEqual(stamped, 1)
        self.assertEqual(
            [b.id for b in list_dropped_batches(self.conn)], self.ids[1:4],
        )


class PerceptionCapRoomStateTest(PerceptionCapTestBase):
    """境界の前進が部屋の様子の土台を越えるとき、差分の土台が読める。"""

    def setUp(self):
        super().setUp()
        self.bundle_a = _room_bundle(["定規と写真がある。"] * 100)
        self.bundle_b = _room_bundle(
            ["定規と写真がある。"] * 100 + ["ノギスが増えた。"],
        )
        self.full = render_room_full(self.bundle_a)
        self.full2 = render_room_full(self.bundle_b)
        self.diff_text = render_room_diff(self.bundle_a, self.bundle_b)["content"]
        self.key = room_key("b1")
        # 1 枚目 = 全文 (差分の土台)。2 枚目 = 差分。以降は無関係な知覚で圧力をかける。
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full, "snapshot": self.bundle_a,
        }], ensure_ascii=False))
        self.diff_id = self._batch(self.diff_text, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_text, "snapshot": self.bundle_b,
            "base_digest": snapshot_digest(self.bundle_a),
        }], ensure_ascii=False))
        self.filler = [self._batch(chr(ord("A") + i) * 1_000) for i in range(3)]

    def _only_the_base_is_dropped(self):
        """土台 1 枚を下ろせばちょうど下の水位に届く二水位。

        下の水位は**移管後**の合計 (差分が全文 ``full2`` へ膨らんだ後) で置く。
        差分のままの字数で置くと、下ろした直後に上の水位を超えたままになり、
        新着が無いのに次の呼び出しで境界がまた進む。
        """
        filler = 3 * (1_000 + _WRAP_CHARS)
        now = (
            len(self.full) + _WRAP_CHARS
            + len(self.diff_text) + _WRAP_CHARS
            + filler
        )
        after = len(self.full2) + _WRAP_CHARS + filler
        return self._watermarks(after, now - 1)

    def test_dropping_the_base_transfers_the_full_text_to_the_survivor(self):
        with self._only_the_base_is_dropped():
            blocks = self._blocks()
        self.assertEqual(get_presentation_cutoff(self.conn), self.base_id)
        survivor = [
            b for b in blocks
            if b["metadata"].get("__perception_batch_id__") == self.diff_id
        ]
        self.assertEqual(len(survivor), 1)
        # 差分だった位置に、その時点の部屋の全文が読める。
        self.assertIn("定規と写真がある。", survivor[0]["content"])
        self.assertIn("ノギスが増えた。", survivor[0]["content"])

    def test_the_transfer_is_recorded_on_the_batch(self):
        with self._only_the_base_is_dropped():
            self._blocks()
        survivor = [
            b for b in list_presented_batches(self.conn) if b.id == self.diff_id
        ][0]
        entry = json.loads(survivor.room_state_json)[0]
        self.assertFalse(entry["is_diff"])
        self.assertTrue(entry["transferred"])

    def test_dropping_every_carrier_reseats_the_room_at_the_oldest_end(self):
        """最後の運搬役まで下りる回は、最新の全文が提示の最古端へ置き直される。

        room_state_packages.md §6-4: 境界の前進と同じロック区間で、今いる部屋の
        最新の全文 (束から導出) が畳みの位置に立ち、以後の差分の土台になる。
        """
        from sai_memory.room_state import latest_visible_snapshot

        with self._watermarks(1_500, 2_000):
            blocks = self._blocks()
        self.assertGreaterEqual(get_presentation_cutoff(self.conn), self.diff_id)
        # 置き直された全文が提示に居る (部屋が消えない)。
        shown = [
            b for b in blocks
            if b["metadata"].get("__perception_batch_id__")
            and self.full2 in b["content"]
        ]
        self.assertEqual(len(shown), 1)
        # 位置は提示の最古端 (省略の印を除く先頭)。
        batch_blocks = [
            b for b in blocks if b["metadata"].get("__perception_batch_id__")
        ]
        self.assertEqual(batch_blocks[0], shown[0])
        # 置き直しは次の差分の土台になる (部屋の様子が途切れない)。
        self.assertEqual(
            latest_visible_snapshot(self.conn, self.key), self.bundle_b,
        )


class PerceptionCapTransferGrowthTest(PerceptionCapTestBase):
    """下ろす量は「移管で全文へ膨らんだ後」の字数で決める (Codex 2026-09-05 #2)。

    差分の小さい文面 (ここでは 4 字) で境界を決めると、下ろした直後に移管が
    走って全文 (1,000 字) へ膨れ、上の水位を超えたままになる。すると**新着が
    一件も無い次の呼び出しで境界がまた進む** — 「まとめて下ろす」契約と、
    「新着が無ければ提示列は変わらない」提示の安定性が同時に破れる。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        self.bundle = _room_bundle(["全あ" * 40] * 20)
        self.full = render_room_full(self.bundle)
        self.diff = render_room_diff(self.bundle, self.bundle)["content"]
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full, "snapshot": self.bundle,
        }], ensure_ascii=False))
        self.diff_id = self._batch(self.diff, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff, "snapshot": self.bundle,
            "base_digest": snapshot_digest(self.bundle),
        }], ensure_ascii=False))
        self.filler = [self._batch(chr(ord("A") + i) * 1_000) for i in range(3)]
        # 土台だけ下ろすと差分が全文へ膨らむ (移管)。差分のままの字数 (naive) と
        # 膨らんだ後の字数 (grown) の間に下の水位を置く — 差し替え前の字数で
        # 見積もる実装なら「土台だけ下ろせば足りる」と誤答する形。
        w_full = len(self.full) + _WRAP_CHARS
        w_diff = len(self.diff) + _WRAP_CHARS
        self.after_all = 3 * (1_000 + _WRAP_CHARS)
        self.naive_after_base = self.after_all + w_diff
        self.grown_after_base = self.after_all + w_full
        self.total_now = self.grown_after_base + w_diff
        self.target = (self.naive_after_base + self.grown_after_base) // 2
        self.high = self.grown_after_base - 5

    def _presented_chars(self, blocks):
        return sum(
            len(b["content"]) for b in blocks
            if not b["metadata"].get("__perception_omitted__")
        )

    def test_the_first_drop_lands_under_the_target_after_the_transfer(self):
        with self._watermarks(self.target, self.high):
            blocks = self._blocks()
        # 移管込みの勘定では土台 1 枚では足りず、差分 (最後の運搬役 — 下ろすと
        # 置き直しの全文が同じ字数で戻る) を越えて filler まで下りる。
        self.assertEqual(get_presentation_cutoff(self.conn), self.filler[0])
        self.assertLessEqual(self._presented_chars(blocks), self.target)

    def test_no_new_batch_means_the_boundary_does_not_move_again(self):
        """新着が無ければ提示列は不変 (二度目の呼び出しで境界が進まない)。"""
        with self._watermarks(self.target, self.high):
            first = self._blocks()
            cutoff = get_presentation_cutoff(self.conn)
            second = self._blocks()
        self.assertEqual(get_presentation_cutoff(self.conn), cutoff)
        self.assertEqual(
            [b["metadata"].get("__perception_batch_id__") for b in first],
            [b["metadata"].get("__perception_batch_id__") for b in second],
        )
        self.assertEqual(
            [b["content"] for b in first], [b["content"] for b in second],
        )

    def test_a_survivor_that_grows_is_counted_at_its_grown_size(self):
        """土台だけを下ろす境界では上の水位を下回れない、と見積もれている。"""
        batches = list_presented_batches(self.conn)
        after_base_only = _presented_chars_after_transfer(batches[1:])
        self.assertEqual(after_base_only, self.grown_after_base)
        self.assertGreater(after_base_only, self.high)


class PerceptionCapConcurrentCompositionTest(PerceptionCapTestBase):
    """二つの組成が交差しても「土台のない差分」は返らない (Codex 2026-09-05 二巡)。

    提示の組成は同じペルソナに対して**同時に二本走る** (ペルソナの Pulse と、
    透明性の画面 context-status の勘定など)。候補の取得と、境界の読取・前進・
    移管が別々のロック区間に分かれていると、その隙間に相手の組成が丸ごと入り、

    1. こちらが候補 (移管前の文面) を読む
    2. 相手が境界を前進させ、全文の移管を commit する
    3. こちらは自分では進めていないので読み直さず、**古い候補**を**相手が
       進めた新しい境界**で振り分ける

    という順序が成立する。結果、土台 (全文のバッチ) だけが提示から下り、土台の
    ない差分がプロンプトへ乗る (再現時の返却本文は差分 4 字、DB の残存本文は
    全文 1,000 字だった)。候補取得から移管後の読み直しまでを一つのロック区間に
    畳んだのが修正で、ここはその交差を実際に起こして押さえる。

    錠前は**非再入の** ``threading.Lock`` で持つ — ロックを取る層が一枚である
    ことを、デッドロックという形で検査するため (本番の錠前は RLock なので、
    再入が紛れ込んでも自分では気づけない)。
    """

    #: 交差の待ち合わせに使う上限。修正後は相手が錠前で待たされるので必ず
    #: 使い切る (= テスト 1 本あたりの固定費)。修正前は待たずに交差が起きる。
    CROSS_TIMEOUT = 0.5

    def setUp(self):
        super().setUp()
        from sai_memory.room_state import room_key

        # 本番の SAIMemoryAdapter と同じ形 — 接続は 1 本で、直列化は _db_lock。
        self.conn.close()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)
        self.persona.sai_memory.conn = self.conn
        self.persona.sai_memory._db_lock = threading.Lock()

        self.bundle_a = _room_bundle(["定規と写真がある。"] * 100)
        self.bundle_b = _room_bundle(
            ["定規と写真がある。"] * 100 + ["ノギスが増えた。"],
        )
        self.full = render_room_full(self.bundle_a)
        self.full2 = render_room_full(self.bundle_b)
        self.diff_text = render_room_diff(self.bundle_a, self.bundle_b)["content"]
        self.key = room_key("b1")
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full, "snapshot": self.bundle_a,
        }], ensure_ascii=False))
        self.diff_id = self._batch(self.diff_text, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_text, "snapshot": self.bundle_b,
            "base_digest": snapshot_digest(self.bundle_a),
        }], ensure_ascii=False))
        self.filler = [self._batch(chr(ord("A") + i) * 1_000) for i in range(3)]
        # 土台 1 枚を下ろせばちょうど下の水位に届く二水位 (移管後の字数で置く)。
        filler_chars = 3 * (1_000 + _WRAP_CHARS)
        now = (
            len(self.full) + _WRAP_CHARS
            + len(self.diff_text) + _WRAP_CHARS
            + filler_chars
        )
        self.target = len(self.full2) + _WRAP_CHARS + filler_chars
        self.high = now - 1

    # -- 検査 -------------------------------------------------------------

    def _shown(self, blocks):
        """提示された知覚ブロックを batch id → 本文で引ける形にする。"""
        return {
            b["metadata"]["__perception_batch_id__"]: b["content"]
            for b in blocks if b["metadata"].get("__perception_batch_id__")
        }

    def _assert_no_orphan_diff(self, blocks, who):
        """土台が下りているのに差分のままの文面を返していないこと。"""
        shown = self._shown(blocks)
        if self.base_id in shown or self.diff_id not in shown:
            return  # 土台がまだ見えている / 差分も一緒に下りた = どちらも健全
        self.assertIn(
            "定規と写真がある。", shown[self.diff_id],
            f"{who} が土台のない差分を提示した — 全文のバッチ {self.base_id} は "
            f"下りているのに、残った {self.diff_id} が移管前の文面のままになって "
            "いる (提示された本文と DB の残存本文が食い違う)",
        )

    def _assert_matches_the_ledger(self, blocks, who):
        """提示した本文が、台帳に残っている確定文面と一致すること。"""
        shown = self._shown(blocks)
        stored = {
            b.id: f"<system>{b.rendered_text}</system>"
            for b in list_unannexed_batches(self.conn)
        }
        for batch_id, content in shown.items():
            self.assertEqual(
                content, stored.get(batch_id),
                f"{who} の提示本文がバッチ {batch_id} の確定文面と違う",
            )

    # -- 交差 -------------------------------------------------------------

    def _compose_crossed(self, make_probe):
        """二本の組成を交差させ、両方の結果を返す。

        主の組成 (テストのスレッド) を先に走らせ、背景の組成は主が候補を読み
        終えた地点から起こす — 起こす順を実行系のスケジューラに委ねると、
        「相手が先に全部済ませてから自分が読む」順に流れて交差そのものが
        起きない回ができる。``make_probe`` は「背景を起こす関数」を受け取り、
        ``list_unannexed_batches`` の差し替えを返す。
        """
        results = {}
        errors = {}

        def run(who):
            try:
                results[who] = self._blocks()
            except Exception as exc:  # pragma: no cover - 失敗時の診断用
                errors[who] = exc

        other = threading.Thread(target=run, args=("背景の組成",), daemon=True)
        probe = make_probe(other.start)
        with self._watermarks(self.target, self.high):
            with patch(
                "sai_memory.perception_buffer.list_unannexed_batches", probe,
            ):
                run("主の組成")
                other.join(timeout=5.0)
        self.assertFalse(other.is_alive(), "背景の組成が終わらなかった (デッドロック)")
        self.assertEqual(errors, {}, f"組成が例外で落ちた: {errors}")
        self.assertEqual(set(results), {"主の組成", "背景の組成"})
        return results

    def _barrier_probe(self, start_other):
        """両方が候補を読み終えた地点で待ち合わせる差し替え。"""
        barrier = threading.Barrier(2)
        started = threading.Event()
        waited = set()  # 待ち合わせは各スレッドの初回だけ (再取得は素通し)

        def probe(conn, **kwargs):
            found = list_unannexed_batches(conn, **kwargs)
            ident = threading.get_ident()
            if ident in waited:
                return found
            waited.add(ident)
            if not started.is_set():
                started.set()
                start_other()
            try:
                barrier.wait(timeout=self.CROSS_TIMEOUT)
            except threading.BrokenBarrierError:
                pass  # 相手が錠前で待たされている = 交差しなかった (修正後の姿)
            return found
        return probe

    def test_a_barrier_crossing_never_returns_a_diff_without_its_base(self):
        """両方が候補を読み終えた地点で待ち合わせる (典型的な同時進入)。

        ロック区間が割れていれば二本とも候補取得まで到達でき、先に境界を進めた
        方の移管を、もう一方が知らないまま新しい境界で振り分ける。畳んだ後は
        片方が錠前で待たされるので、待ち合わせは時間切れになる (= 交差しない)。
        """
        results = self._compose_crossed(self._barrier_probe)
        for who, blocks in results.items():
            self._assert_no_orphan_diff(blocks, who)
            self._assert_matches_the_ledger(blocks, who)

    def test_a_full_composition_cannot_slip_between_candidates_and_boundary(self):
        """主の組成を候補取得の直後で止め、背景に丸ごと走り抜けさせる。

        再現の順序そのもの: 止めた側は境界も移管も相手に進められた状態で再開し、
        自分では進めていないので読み直さない。
        """
        paused = threading.Event()
        other_done = threading.Event()
        main = threading.current_thread()

        def make_probe(start_other):
            def probe(conn, **kwargs):
                found = list_unannexed_batches(conn, **kwargs)
                if threading.current_thread() is main:
                    if not paused.is_set():
                        paused.set()
                        start_other()
                        # 背景が「候補取得と境界前進の隙間」へ入れるなら、ここで
                        # 走り抜けて境界を進め、全文を移管してしまう。
                        other_done.wait(timeout=self.CROSS_TIMEOUT)
                else:
                    other_done.set()
                return found
            return probe

        results = self._compose_crossed(make_probe)
        self.assertTrue(paused.is_set(), "主の組成が待ち合わせ地点を通らなかった")
        for who, blocks in results.items():
            self._assert_no_orphan_diff(blocks, who)
            self._assert_matches_the_ledger(blocks, who)

    def test_the_crossing_still_lands_on_the_target(self):
        """交差しても下ろしは一度きりで、境界は土台の 1 枚ぶんだけ進む。"""
        self._compose_crossed(self._barrier_probe)
        self.assertEqual(get_presentation_cutoff(self.conn), self.base_id)
        survivor = [
            b for b in list_presented_batches(self.conn) if b.id == self.diff_id
        ][0]
        self.assertIn("定規と写真がある。", survivor.rendered_text)


class PerceptionCapNorokeyukiShapeTest(PerceptionCapTestBase):
    """のろけゆきさんの形: 会話 4 万字・知覚 18 万字の初期条件。"""

    #: 実測に近い形 — 部屋の様子 1 枚 1 万字が 18 枚積もった状態。
    PERCEPTION_BLOCKS = 18
    PERCEPTION_BLOCK_CHARS = 10_000
    CONVERSATION_CHARS = 40_000

    def setUp(self):
        super().setUp()
        self.ids = [
            self._batch(f"部屋の様子 {i}\n" + "あ" * self.PERCEPTION_BLOCK_CHARS)
            for i in range(self.PERCEPTION_BLOCKS)
        ]
        # 会話 4 万字 (残す量ちょうど) の提示行。
        self.recent = [{
            "id": "m1", "role": "user", "created_at": 100,
            "content": "い" * self.CONVERSATION_CHARS, "metadata": {"tags": []},
        }]

    def test_the_first_drop_lands_on_the_target(self):
        blocks = self._blocks(recent=self.recent)  # 組み込み既定 (2万 / 6万)
        perception_chars = sum(
            len(b["content"]) for b in blocks
            if not b["metadata"].get("__perception_omitted__")
        )
        self.assertLessEqual(perception_chars, 20_000)
        # 最新の知覚は残っている (下ろすのは古い側から)。
        self.assertEqual(
            blocks[-1]["metadata"]["__perception_batch_id__"], self.ids[-1],
        )

    def test_the_metabolism_watermarks_become_satisfiable(self):
        """会話を残す量まで畳めば、合計が整理を始める量を下回る形に戻る。"""
        blocks = self._blocks(recent=self.recent)
        total = message_chars(merge_perception_blocks(self.recent, blocks))
        self.assertLess(total, 120_000)  # 組み込み既定の上限

    def test_nothing_is_lost_from_the_ledger(self):
        self._blocks(recent=self.recent)
        self.assertEqual(
            len(list_unannexed_batches(self.conn)), self.PERCEPTION_BLOCKS,
        )
        self.assertTrue(list_dropped_batches(self.conn))


class PerceptionCapExecutionModelTest(PerceptionCapTestBase):
    """水位を引くのは**その回の実行モデル** (2026-09-05 Codex 三巡 #2)。

    prepare_context も Metabolism も実行 model (``model_key``) で動く。下ろしの
    判定だけが ``persona.model`` を見ていると、実行モデルに個別の知覚水位を保存
    しても効かず、保存時の検査 (整理を始める量 − 残す量 > 知覚の上限 + 余裕) が
    保証したはずの余裕もその回には成立しない。

    下ろし境界 (``perception_presentation``) はペルソナ全体で一つのまま = 判定に
    使う水位だけが model ごとに変わる。境界は一方向にしか進まないので、厳しい
    モデルの回に多く進み、緩いモデルの回はそれを戻さない (一方向の共有)。
    """

    #: persona.model は緩い水位、実行 model は厳しい水位。
    WATERMARKS = {
        "test-model": (200_000, 300_000),   # persona.model — 何も下ろさない
        "strict-model": (2_000, 3_000),     # 実行 model — 下ろす
    }

    def setUp(self):
        super().setUp()
        self.ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]

    def _by_model(self):
        """model ごとに違う水位を返す差し替え (どの model で引いたかが見える)。"""
        return patch(
            "sea.runtime_context.resolve_perception_watermarks",
            side_effect=lambda model: self.WATERMARKS[model],
        )

    def _compose(self, model_key=None):
        return list_presented_perception_blocks(
            _RUNTIME, self.persona, [], raise_on_error=True, model_key=model_key,
        )

    def _shown(self, blocks):
        return [
            b["metadata"].get("__perception_batch_id__") for b in blocks
            if not b["metadata"].get("__perception_omitted__")
        ]

    def test_the_execution_model_watermarks_are_the_ones_that_apply(self):
        with self._by_model():
            blocks = self._compose(model_key="strict-model")
        self.assertEqual(get_presentation_cutoff(self.conn), self.ids[3])
        self.assertEqual(self._shown(blocks), [self.ids[4]])

    def test_without_an_execution_model_it_falls_back_to_the_persona_model(self):
        with self._by_model():
            blocks = self._compose()
        self.assertEqual(get_presentation_cutoff(self.conn), 0)
        self.assertEqual(self._shown(blocks), self.ids)

    def test_the_lenient_model_does_not_bring_the_dropped_batches_back(self):
        """境界は一つ・一方向 — 厳しい回で進んだ位置は緩い回にも共有される。"""
        with self._by_model():
            self._compose(model_key="strict-model")
            cutoff = get_presentation_cutoff(self.conn)
            blocks = self._compose(model_key="test-model")
        self.assertEqual(get_presentation_cutoff(self.conn), cutoff)
        self.assertEqual(self._shown(blocks), [self.ids[4]])

    def test_the_accounting_path_carries_the_same_execution_model(self):
        """勘定側 (SessionLifecycle) も同じ ``model_key`` で組成を呼ぶ。

        送る側と測る側が別の model の水位で動くと、勘定が「まだ余裕がある」と
        言っている裏で提示だけが下りる (逆も同じ)。
        """
        from sea.session_lifecycle import SessionLifecycle

        lifecycle = SimpleNamespace(runtime=_RUNTIME)
        with self._by_model():
            blocks = SessionLifecycle.perception_blocks_for(
                lifecycle, self.persona, [],
                raise_on_error=True, model_key="strict-model",
            )
        self.assertEqual(get_presentation_cutoff(self.conn), self.ids[3])
        self.assertEqual(self._shown(blocks), [self.ids[4]])


class PerceptionCapPlanMatchesTheRepairTest(PerceptionCapTestBase):
    """下ろし計画の見積もりと、実際に回復した後の提示が一致する。

    見積もり (:func:`sea.runtime_context._presented_chars_after_transfer`) は
    土台の回復規則をもう一枚辿り直したものなので、ずれると「下ろした直後に
    また上の水位を超える」形が戻る。連なりが中間で切れている形 (指紋の合わない
    差分) まで含めて突き合わせる。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        body = ["定規がある。"] * 100
        self.bundle_a = _room_bundle(body)
        self.bundle_b = _room_bundle(body + ["ノギスが増えた。"])
        self.bundle_c = _room_bundle(body + ["ノギスが増えた。", "鍵が増えた。"])
        self.full_a = render_room_full(self.bundle_a)
        self.diff_b = render_room_diff(self.bundle_a, self.bundle_b)["content"]
        self.diff_c = render_room_diff(self.bundle_b, self.bundle_c)["content"]
        self.a_id = self._batch(self.full_a, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full_a, "snapshot": self.bundle_a,
        }], ensure_ascii=False))
        self.b_id = self._batch(self.diff_b, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_b, "snapshot": self.bundle_b,
            "base_digest": snapshot_digest(self.bundle_a),
        }], ensure_ascii=False))
        self.c_id = self._batch(self.diff_c, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_c, "snapshot": self.bundle_c,
            "base_digest": snapshot_digest(self.bundle_b),
        }], ensure_ascii=False))

    def _actual_presented_chars(self):
        restore_room_state_bases(self.conn)
        self.conn.commit()
        return sum(
            len(_perception_block_text(b.rendered_text))
            for b in list_presented_batches(self.conn)
        )

    def test_an_intact_chain_is_predicted_as_unchanged(self):
        predicted = _presented_chars_after_transfer(list_presented_batches(self.conn))
        self.assertEqual(predicted, self._actual_presented_chars())

    def test_a_broken_middle_is_predicted_at_its_reopened_size(self):
        """中間 (B) を付記で外すと C が全文へ膨らむ — 見積もりもそう数える。"""
        mark_batches_annexed(self.conn, [self.b_id], "entry-1")
        self.conn.commit()
        predicted = _presented_chars_after_transfer(list_presented_batches(self.conn))
        self.assertEqual(predicted, self._actual_presented_chars())
        # 実際に膨らんでいる (この検査が空振りでない証拠)。
        self.assertGreater(
            predicted,
            len(_perception_block_text(self.full_a))
            + len(_perception_block_text(self.diff_c)),
        )

    def test_a_dropped_prefix_is_predicted_at_its_reopened_size(self):
        """境界が土台を越えて進む形 (下ろし計画が実際に使う見積もり)。"""
        presented = list_presented_batches(self.conn)
        predicted = _presented_chars_after_transfer(presented[1:])
        advance_presentation_cutoff(self.conn, self.a_id)
        self.conn.commit()
        self.assertEqual(predicted, self._actual_presented_chars())


class PerceptionCapSuffixTotalsTest(unittest.TestCase):
    """見積もりは回復規則の**二枚目**なので、素朴な計算と全 suffix で突き合わせる。

    :func:`sea.runtime_context._perception_suffix_totals` は
    :func:`sai_memory.room_state.restore_room_state_bases` と同じ規則を、字数だけ
    で線形に辿り直したもの。二枚あるかぎり片方だけがずれる余地が残るので、乱数
    で作った提示列 (旧エントリ・最初から切れた連なり・差し替え不能な block を
    混ぜる) の**すべての境界候補**について、規則をそのまま辿った値と一致する
    ことを見る。ずれると「下ろした直後にまた上の水位を超える」形が戻る。
    """

    TRIALS = 120
    SEED = 20260905

    def _naive_total(self, presented):
        """回復規則をそのまま辿った合計 (線形化していない素朴な計算)。"""
        from sai_memory.room_state import (
            batch_room_states,
            bundle_is_valid,
            chain_is_intact,
            is_legacy_entry,
        )

        previous = {}
        total = 0
        for batch in presented:
            rendered = batch.rendered_text
            for entry in batch_room_states(batch.room_state_json):
                if is_legacy_entry(entry):
                    continue  # 旧形式は連なりの外 (開き直しもされない)
                key = str(entry["key"])
                prev = previous.get(key)
                previous[key] = entry
                if not entry.get("is_diff") or chain_is_intact(entry, prev):
                    continue
                block = entry.get("block") or ""
                snapshot = entry.get("snapshot")
                if not block or not bundle_is_valid(snapshot) or block not in rendered:
                    continue
                rendered = rendered.replace(block, render_room_full(snapshot), 1)
            total += len(_perception_block_text(rendered))
        return total

    def _make_presented(self, rng, count):
        batches = []
        latest = {}
        for index in range(count):
            entries, blocks = [], []
            for room in rng.sample(range(4), rng.randint(0, 2)):
                key = room_key(f"b{room}")
                bundle = _room_bundle(
                    [f"全文 {index}", "あ" * rng.randint(20, 120)],
                    building=f"b{room}", name=f"部屋{room}",
                )
                full = render_room_full(bundle)
                base = latest.get(key)
                entry = {"key": key, "snapshot": bundle}
                style = rng.random()
                if base is None or style < 0.25:
                    block = full
                    entry["is_diff"] = False
                elif style < 0.40:
                    # 旧形式 (文字列 snapshot) — 連なりの外 (土台にも開き直しにも
                    # 参加しない)。previous も更新されない。
                    block = f"# {key} 旧差分 {index}"
                    entry["is_diff"] = True
                    entry["snapshot"] = full
                else:
                    block = f"# {key} 差分 {index}"
                    entry["is_diff"] = True
                    if style < 0.50:
                        pass  # 指紋を持たない壊れた記帳 (= 連なり切れ扱い)
                    elif style < 0.62:
                        # 別の全文を土台にした = 連なりが最初から切れている
                        entry["base_digest"] = snapshot_digest(
                            {"building_id": "zzz", "packages": []},
                        )
                    else:
                        entry["base_digest"] = snapshot_digest(base)
                # 確定文面に現れない block (差し替え不能なので見送られる)
                entry["block"] = block + ("　欠落" if style > 0.95 else "")
                if isinstance(entry["snapshot"], dict):
                    latest[key] = bundle
                entries.append(entry)
                blocks.append(block)
            batches.append(SimpleNamespace(
                id=index + 1,
                rendered_text="\n\n".join(blocks) if blocks else f"通知 {index}",
                room_state_json=(
                    json.dumps(entries, ensure_ascii=False) if entries else None
                ),
            ))
        return batches

    def test_every_boundary_candidate_matches_the_repair_rule(self):
        import random

        rng = random.Random(self.SEED)
        checked = 0
        for trial in range(self.TRIALS):
            presented = self._make_presented(rng, rng.randint(0, 12))
            totals = _perception_suffix_totals(presented)
            self.assertEqual(len(totals), len(presented) + 1)
            for index in range(len(presented) + 1):
                self.assertEqual(
                    totals[index], self._naive_total(presented[index:]),
                    f"trial={trial} の境界候補 {index} で見積もりがずれた",
                )
                checked += 1
        self.assertGreater(checked, 500)  # 検査が空振りでない証拠


class PerceptionCapPendingRoomGateTest(PerceptionCapTestBase):
    """同部屋の pending が居る回は、置き直しの全文を見積もりに足さない。

    実物の置き直し (:func:`sai_memory.room_state.reseat_current_room`) は
    同部屋の pending (未消費) があれば発火しない — 次の消費がその部屋を運ぶ
    (:func:`sai_memory.room_state.pending_has_room` の門)。有効な pending が
    無くても、材料探し (:func:`sai_memory.room_state._latest_room_bundle`) は
    pending を先に見る — 同部屋の最初の一致が旧形式の遺物なら材料なしで、
    やはり発火しない。見積もり
    (:func:`sea.runtime_context._room_reseat_projection`) だけが提示列から
    全文一枚を加算すると、起きない置き直しのぶん境界が必要より進む
    (保守側だが、測る列と送る列の一致が破れる)。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        self.bundle_a = _room_bundle(["定規と写真がある。"] * 30)
        self.bundle_b = _room_bundle(
            ["定規と写真がある。"] * 30 + ["ノギスが増えた。"],
        )
        full = render_room_full(self.bundle_a)
        diff = render_room_diff(self.bundle_a, self.bundle_b)["content"]
        self.base_id = self._batch(full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False, "block": full,
            "snapshot": self.bundle_a,
        }], ensure_ascii=False))
        self.diff_id = self._batch(diff, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True, "block": diff,
            "snapshot": self.bundle_b,
            "base_digest": snapshot_digest(self.bundle_a),
        }], ensure_ascii=False))
        self.filler = [self._batch(chr(ord("A") + i) * 1_000) for i in range(3)]
        self.presented = list_presented_batches(self.conn)
        self.totals = _perception_suffix_totals(self.presented)
        # 下の水位 = 運搬役 2 枚 (base + diff) を下ろした残り。置き直しの全文を
        # 加算しなければちょうど届く位置なので、加算の有無が境界に出る。

    def _push_pending_room(self, key, *, snapshot):
        """未消費の部屋の記録。``snapshot`` は呼び出し側が明示する —
        本物の push (build_room_state_push) は必ず有効な束を載せるので、
        文字列を渡した形は既存 DB の遺物 (回収 §11-2 が落とす) の再現。"""
        push_perception(
            self.conn, ROOM_STATE_KIND, "(未消費の部屋の記録)",
            metadata=json.dumps(
                {"room_state":
                     {"key": key, "is_diff": True, "snapshot": snapshot}},
                ensure_ascii=False,
            ),
        )

    def test_a_pending_room_removes_the_reseat_cost_from_the_plan(self):
        self._push_pending_room(self.key, snapshot=self.bundle_b)
        with self._watermarks(self.totals[2], self.totals[2]):
            planned = _plan_perception_drop(self.persona, self.presented, 0)
        # pending が運ぶので置き直しは起きない — 運搬役 2 枚で止まる。
        self.assertEqual(planned, self.diff_id)

    def test_a_legacy_pending_also_removes_the_reseat_cost(self):
        """遺物の pending が同部屋の最新記録なら、置き直しコストは載せない。

        門 (:func:`sai_memory.room_state.pending_has_room`) は遺物を数えない
        (回収 §11-2 が落とすので「次の消費が運ぶ」が成立しない) が、実物には
        もう一枚、材料探し (:func:`sai_memory.room_state._latest_room_bundle`)
        の止まり方がある — pending を先に見て、同部屋の最初の一致 (この遺物)
        が旧形式なら材料なし = hook の置き直しは発火しない (下の実測)。旧版の
        このテストは「遺物は運ばれると数えないのでコストは載る」を仕様として
        固定していたが、それは pending を見ずに提示列の有効束でコストを加算
        する見積もりの誤りの写しだった — 起きない置き直しのぶん境界が必要以上
        に進み、まだ提示できた履歴を不可逆に下ろす (2026-09-06 Codex 指摘で
        確定。検証は下の hook との突き合わせ)。
        """
        self._push_pending_room(self.key, snapshot="旧世代の全文 (文字列)。")
        # 見積もりと計画 — どちらも読みだけ (境界は書かない)。
        projection = _room_reseat_projection(self.presented, self.conn)
        with self._watermarks(self.totals[2], self.totals[2]):
            planned = _plan_perception_drop(self.persona, self.presented, 0)
        # 実物: 同じ状態で運搬役 2 枚 (base + diff) を実際に下ろす。境界前進と
        # 同一 tx の hook (fresh_bundle なしの reseat_current_room) が走るが、
        # 材料探しが pending の遺物で止まるので置き直しは立たない。
        advance_presentation_cutoff(self.conn, self.diff_id)
        self.conn.commit()
        remaining = list_presented_batches(self.conn)
        self.assertEqual(
            [b.id for b in remaining], self.filler,
            "hook は置き直しを作らないはず (材料探しが pending の遺物で止まる)",
        )
        self.assertNotIn(
            render_room_full(self.bundle_b),
            [b.rendered_text for b in remaining],
        )
        # 見積もりは実物に一致する: コストは載らず、計画は運搬役 2 枚で止まる。
        self.assertEqual(projection, (None, 0))
        self.assertEqual(planned, self.diff_id)

    def test_a_pending_in_another_room_means_we_moved_and_no_cost_is_added(self):
        """移動直後の形 (旧部屋の提示列 + 新部屋の pending) — 2026-09-06 二巡目修正 3。

        実物の置き直しは現在地を
        :func:`sai_memory.room_state.find_current_room_key` (pending 優先) で
        決める — 新部屋 b2 の pending が最新の記録なので現在地は b2 になり、
        b2 の pending が門になって置き直しは発火しない。見積もりが現在地を
        提示列の最新エントリ (旧部屋 b1) から推定すると、起きない b1 の
        置き直しの全文を残量に足して境界が必要以上に進む — まだ提示できた
        履歴まで下ろす (境界は一方向で取り消せない)。旧版のこのテストは
        「別の部屋の pending は門にならない」を仕様として固定していたが、
        それは見積もりの現在地の誤りの写しだった。
        """
        self._push_pending_room(
            room_key("b2"),
            snapshot=_room_bundle(
                ["移動先の様子。"], building="b2", name="第二工房", key_id="2",
            ),
        )
        with self._watermarks(self.totals[2], self.totals[2]):
            planned = _plan_perception_drop(self.persona, self.presented, 0)
        # 現在地は b2 — b1 の置き直しコストは載らず、運搬役 2 枚で止まる。
        self.assertEqual(planned, self.diff_id)


class PerceptionCapLegacyNewestGateTest(PerceptionCapTestBase):
    """最新の同部屋記録が旧形式・不正束なら、置き直しコストを見積もりに載せない。

    実物の材料探し (:func:`sai_memory.room_state._latest_room_bundle`) は同部屋の
    **最初の一致**で判定を確定し、それが旧形式 (文字列 snapshot) や不正な束なら
    材料なし — 置き直しは発火せず、全文は提示へ戻らない (2026-09-06 五巡目
    修正 1、:class:`tests.test_room_state_diff.LegacyNewestReseatTest`)。見積もり
    (:func:`sea.runtime_context._room_reseat_projection`) が旧形式を飛ばして
    さらに古い構造化束のコストを加算すると、起きない置き直しのぶん境界が必要
    以上に進み、まだ提示できた履歴を不可逆に下ろす (2026-09-06 六巡目 —
    五巡目修正 1 の見積もり側の掃き忘れ)。止まり方の規則は両者が同じ一枚
    (:func:`sai_memory.room_state.first_room_bundle`) を通る。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        # 全文一枚 (約 3,000 字) が filler 一枚 (約 1,000 字) より十分大きい束 —
        # コストが誤って載ると境界の着地が filler まで進み、正しければ部屋の
        # 2 枚で止まる (加算の有無が境界に出るサイズ)。
        self.bundle = _room_bundle(["定規と写真がある。"] * 300)
        self.full = render_room_full(self.bundle)
        self.structured_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False, "block": self.full,
            "snapshot": self.bundle,
        }], ensure_ascii=False))

    def _newer_record(self, snapshot):
        """構造化束より新しい同部屋の記録 (snapshot の形は呼び出し側が選ぶ)。"""
        text = "# 「工房」の様子\n旧世代の記録。"
        return self._batch(text, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False, "block": text,
            "snapshot": snapshot,
        }], ensure_ascii=False))

    def _assert_boundary_matches_reality(self, newest_id):
        filler = [self._batch(chr(ord("A") + i) * 1_000) for i in range(3)]
        presented = list_presented_batches(self.conn)
        # 止まり方の規則そのもの: 同部屋の最初の一致が材料にならなければ
        # (None, 0) — 古い構造化束へは遡らない。
        self.assertEqual(
            _room_reseat_projection(presented, self.conn), (None, 0),
        )
        totals = _perception_suffix_totals(presented)
        # 下の水位 = 部屋の 2 枚を下ろした残り。実物は材料なしで置き直さない
        # のでここで止まれる — コストを誤って載せると filler まで下りる。
        with self._watermarks(totals[2], totals[2]):
            planned = _plan_perception_drop(self.persona, presented, 0)
        self.assertEqual(
            planned, newest_id,
            "実物は材料なしで置き直さないのに、見積もりが古い構造化束の"
            "コストを加算して境界を必要以上に進めた",
        )
        # 実物の挙動と突き合わせる: この境界まで実際に下ろしても置き直しは
        # 立たず (材料なし)、残りは filler だけで下の水位に収まっている。
        advance_presentation_cutoff(self.conn, planned)
        self.conn.commit()
        remaining = list_presented_batches(self.conn)
        self.assertEqual([b.id for b in remaining], filler)
        self.assertNotIn(self.full, [b.rendered_text for b in remaining])
        self.assertLessEqual(
            sum(
                len(_perception_block_text(b.rendered_text))
                for b in remaining
            ),
            totals[2],
        )

    def test_a_newer_legacy_record_removes_the_reseat_cost(self):
        newest = self._newer_record("# 「工房」の様子\n旧世代の全文。")
        self._assert_boundary_matches_reality(newest)

    def test_a_newer_invalid_bundle_removes_the_reseat_cost_too(self):
        newest = self._newer_record(
            {"building_id": "b1", "note": "束の形をしていない"},
        )
        self._assert_boundary_matches_reality(newest)

    def test_a_newer_type_broken_bundle_removes_the_reseat_cost_too(self):
        """2026-09-06 七巡目修正 1: 浅い検査を通る型の壊れた束も不正束。

        packages は list なので浅い検査 (dict + packages が list) は通るが、
        パッケージの lines の型が壊れていて利用側が読める形ではない。
        bundle_is_valid が浅いままだと、見積もり (_room_reseat_projection) が
        この束を材料と数えて置き直しコストを加算し、境界が必要以上に進む。
        """
        newest = self._newer_record({
            "building_id": "b1", "building_name": "工房",
            "packages": [{
                "key": "item:1", "family": "item",
                "label": "[item:1] 壊れた記帳",
                "lines": "一枚の文字列 (list でない)", "media": [],
                "state": "open",
            }],
        })
        self._assert_boundary_matches_reality(newest)

    def test_a_newer_media_broken_bundle_removes_the_reseat_cost_too(self):
        """2026-09-06 八巡目修正 2: media の型が壊れた束も不正束。

        lines は正当なので render_room_full (コストの導出) は通ってしまうが、
        path が list なので実物の置き直しはメディアの復元
        (:func:`sai_memory.room_state.bundle_media` の ``path in seen``) で
        TypeError になる — media の型 (path = 非空文字列 / mime_type = 存在
        するなら文字列) まで検めて、材料なしの停止に落とす。
        """
        newest = self._newer_record({
            "building_id": "b1", "building_name": "工房",
            "packages": [{
                "key": "item:1", "family": "item",
                "label": "[item:1] 壊れた絵の記帳",
                "lines": ["[item:1] 壊れた絵の記帳"],
                "media": [{"path": ["x.png"], "mime_type": []}],
                "state": "open",
            }],
        })
        self._assert_boundary_matches_reality(newest)

    def test_a_newer_duplicate_key_bundle_removes_the_reseat_cost_too(self):
        """2026-09-06 九巡目修正 2: キーの重複した束も不正束 — コストを載せない。

        パッケージの型は全部正当なので型検査だけでは通るが、差分の組成
        (render_room_diff) はパッケージをキーで辞書化するので片方が静かに
        上書きされる記帳破損。停止の規則は bundle_is_valid ごしの
        first_room_bundle の一枚 — 材料探し (実物) と見積もりの片方だけが
        止まる形は六・七巡目と同じ割れ方になる。
        """
        package = {
            "key": "item:1", "family": "item", "label": "[item:1] 一枚目",
            "lines": ["[item:1] 一枚目"], "media": [], "state": "open",
        }
        newest = self._newer_record({
            "building_id": "b1", "building_name": "工房",
            "packages": [
                package,
                dict(package, label="[item:1] 二枚目",
                     lines=["[item:1] 二枚目"]),
            ],
        })
        self._assert_boundary_matches_reality(newest)


class PerceptionCapReseatCrossingTest(PerceptionCapTestBase):
    """境界が置き直しバッチの id を跨ぐ回も、見積もりは全文一枚を保ち続ける。

    置き直しバッチは consumed_at が最古で id が新しい。walk はそれを下ろし候補
    から外す (skip) が、境界は id 一本なので、より新しい id のバッチを下ろすと
    境界が置き直しの id を跨ぎ、置き直しバッチ自身も提示から外れる — その瞬間、
    境界前進と同一 tx の hook (reseat_current_room) が新しい置き直しを作る。

    walk の skip は retained (skip した置き直しの字数) を running に残し続ける
    ので、見積もりはこの新しい置き直しの全文一枚と釣り合う — 下ろした直後の
    実提示が見積もりと一致し、新着なしの次の呼び出しで境界が再び進むことは
    ない (2026-09-06 レビュー修正 4 の検証で確認した契約)。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        self.bundle = _room_bundle(["定規と写真がある。"] * 30)
        self.full = render_room_full(self.bundle)
        # 部屋の全文 (id1) → ノイズ (id2) → 境界を id1 へ = 置き直し (id3) が
        # 立つ。その後のノイズ (id4〜id6) は置き直しより新しい id。
        base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False, "block": self.full,
            "snapshot": self.bundle,
        }], ensure_ascii=False))
        self.n1 = self._batch("B" * 1_000)
        advance_presentation_cutoff(self.conn, base_id)
        self.conn.commit()
        self.reseat_id = next(
            b.id for b in list_presented_batches(self.conn) if b.room_state_json
        )
        self.n2 = self._batch("C" * 1_000)
        self.n3 = self._batch("D" * 1_000)
        self.n4 = self._batch("E" * 1_000)
        presented = list_presented_batches(self.conn)
        totals = _perception_suffix_totals(presented)
        # 提示列は [置き直し, n1, n2, n3, n4]。n1〜n3 を下ろす計画は境界が
        # n3 の id (> 置き直しの id) まで進む = 置き直し自身も跨いで下りる。
        # 下の水位は「最後の 1 枚 + 全文一枚」の位置 — 見積もりが全文一枚を
        # 織り込んでいないと、この target には n2 までで届いたことになり、
        # 下ろした直後にまた上を超えて次の呼び出しで境界が進む。
        retained = totals[0] - totals[1]
        self.assertEqual(retained, len(self.full) + _WRAP_CHARS)
        self.cap = self._watermarks(totals[4] + retained, totals[0] - 1)

    def test_the_crossing_keeps_one_full_sheet_in_the_estimate(self):
        with self.cap:
            blocks = self._blocks()
        cutoff = get_presentation_cutoff(self.conn)
        self.assertEqual(cutoff, self.n3)
        self.assertGreater(cutoff, self.reseat_id)  # 置き直しの id を跨いだ
        # 古い置き直しは下り、新しい置き直しが提示の最古端に立つ。
        shown = [
            b for b in blocks if b["metadata"].get("__perception_batch_id__")
        ]
        self.assertIn(self.full, shown[0]["content"])
        self.assertGreater(
            shown[0]["metadata"]["__perception_batch_id__"], cutoff,
        )
        # 実提示の合計 = 見積もりの着地点 (全文一枚が織り込まれている証拠)。
        self.assertEqual(
            sum(len(b["content"]) for b in shown),
            len(self.full) + 1_000 + 2 * _WRAP_CHARS,
        )

    def test_no_new_batch_means_no_second_advance_after_the_crossing(self):
        with self.cap:
            first = self._blocks()
            cutoff = get_presentation_cutoff(self.conn)
            second = self._blocks()
        self.assertEqual(get_presentation_cutoff(self.conn), cutoff)
        self.assertEqual(
            [b["content"] for b in first], [b["content"] for b in second],
        )

    def test_measuring_matches_sending_when_the_boundary_crosses_the_reseat(self):
        """測る列 (advance_cutoff=False) にも置き直しの全文一枚が立つ。

        実送信では、境界前進と同一 tx の hook (reseat_current_room) が新しい
        全文を提示の最古端に作る。測るだけのモードは境界を書かないのでその
        hook が走らない — 置き直しの下見 (dry_run) で同じ一枚を合成しないと、
        測る列だけが全文一枚ぶん小さくなる (画面の表示が実送信より少なく出る
        = 測る列と送る列の同一性の破れ)。
        """
        cutoff_before = get_presentation_cutoff(self.conn)
        with self.cap:
            measured = self._blocks(advance_cutoff=False)
            # 測るだけの回は境界を書かない (setUp が進めた位置のまま)。
            self.assertEqual(get_presentation_cutoff(self.conn), cutoff_before)
            sent = self._blocks(advance_cutoff=True)
        measured_chars = sum(len(b["content"]) for b in measured)
        sent_chars = sum(len(b["content"]) for b in sent)
        print(
            f"[measure-vs-send] measured={measured_chars} sent={sent_chars}",
        )
        self.assertEqual(measured_chars, sent_chars)
        self.assertEqual(
            [b["content"] for b in measured], [b["content"] for b in sent],
        )
        # 位置 (時刻順マージの並び) も同じ — 幻のブロックは実物の置き直しと
        # 同じ consumed_at (提示の最古端) に立つ。
        self.assertEqual(
            [b["created_at"] for b in measured], [b["created_at"] for b in sent],
        )

    def test_measuring_does_not_materialize_the_reseat(self):
        """下見は読むだけ — 台帳に新しいバッチ行も境界も書かない。"""
        before = self.conn.execute(
            "SELECT COUNT(*) FROM perception_batches",
        ).fetchone()[0]
        cutoff_before = get_presentation_cutoff(self.conn)
        with self.cap:
            first = self._blocks(advance_cutoff=False)
            second = self._blocks(advance_cutoff=False)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM perception_batches",
        ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(get_presentation_cutoff(self.conn), cutoff_before)
        # 何度測っても同じ列 (下見は決定論)。
        self.assertEqual(
            [b["content"] for b in first], [b["content"] for b in second],
        )


class PerceptionCapWindowedCutoffHookTest(PerceptionCapTestBase):
    """2026-09-06 九巡目修正 1: 境界前進の hook の運搬役判定にも提示窓の篩が渡る。

    Chronicle 無効ペルソナ (提示窓 = anchor / recent の床) の組成も知覚の合計
    上限で境界を進める — その同一 tx の hook (reseat_current_room) は「今いる
    部屋の運搬役が生きているか」を提示バッチの走査で判定する。呼び出し側
    (list_presented_perception_blocks) は窓の述語を持っているのに
    advance_presentation_cutoff へ渡していないと、窓の外に立つ古い同部屋束
    (境界キーの無い旧世代の置き直しバッチ — consumed_at が最古端なので窓判定の
    epoch フォールバックで窓の外) を運搬役に数えて置き直しを抑止する — 境界
    だけが確定し、このペルソナの提示から部屋の全文が一拍 (次の Pulse 頭の
    自己回復まで) 消える。検知の自己回復・測るだけの下見は同じ篩を通っている
    (二巡目修正 1) — 「判定の規則は一枚」の、実送信の呼び口の取りこぼし。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        # 窓の内の最後の運搬役 (id が最小・consumed_at 1010)。
        self.bundle_new = _room_bundle(["定規と写真がある。"] * 30)
        self.full_new = render_room_full(self.bundle_new)
        self.carrier_id = self._batch(self.full_new, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False, "block": self.full_new,
            "snapshot": self.bundle_new,
        }], ensure_ascii=False))
        # 窓の内のノイズ (「最新の 1 件は下ろさない」規則の受け皿)。
        self.noise_id = self._batch("N" * 1_000)
        # 窓の外の古い同部屋束: 境界キーの無い旧世代の置き直しバッチの形 —
        # id は新しく consumed_at は最古端。境界キーが無いので窓判定
        # (batch_in_window) は consumed_at の epoch フォールバックになり、窓の
        # 床 (1005) より古いこのバッチは窓の外に立つ。id は下ろし境界より
        # 新しいので提示バッチの走査には残る — 篩なしの運搬役判定だけが
        # これを「生きている」と数える。
        self.bundle_old = _room_bundle(["定規だけがある。"] * 30)
        self.old_full = render_room_full(self.bundle_old)
        self.outside_id = insert_presentation_batch(
            self.conn, consumed_at=1_000, rendered_text=self.old_full,
            room_state_json=json.dumps([{
                "key": self.key, "is_diff": False, "block": self.old_full,
                "snapshot": self.bundle_old, "reseated": True,
            }], ensure_ascii=False),
        )
        self.conn.commit()
        # 窓 (床 epoch 1005) から見える提示 = [運搬役, ノイズ] の 2 枚だけ。
        visible = [
            b for b in list_presented_batches(self.conn)
            if b.consumed_at >= 1_005
        ]
        self.assertEqual(
            [b.id for b in visible], [self.carrier_id, self.noise_id],
        )
        totals = _perception_suffix_totals(visible)
        # 上の水位を 1 字だけ下回らせ、運搬役 1 枚を下ろす計画にする。
        self.cap = self._watermarks(totals[0] - 1, totals[0] - 1)
        # recent の最古 epoch 1005 が窓の床になる (anchor なしの床解決)。
        self.recent = [{"created_at": 1_005}]

    def test_the_cutoff_hook_judges_the_carrier_through_the_window(self):
        with self.cap:
            self._blocks(runtime=_RUNTIME_NO_CHRONICLE, recent=self.recent)
        # 実送信の経路: 境界は運搬役の id まで進んだ。
        self.assertEqual(get_presentation_cutoff(self.conn), self.carrier_id)
        # hook の置き直しが窓付きの判定で発火している — 窓の外の古い束を
        # 運搬役に数えると、新しい置き直しが立たず、境界だけ確定してこの
        # ペルソナの提示から部屋の全文が消える。
        reseats = [
            b for b in list_presented_batches(self.conn)
            if b.id != self.outside_id and b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]
        self.assertTrue(
            reseats,
            "窓の外の古い同部屋束が運搬役に数えられ、境界前進の hook の"
            "置き直しが抑止された (advance_presentation_cutoff に窓の篩が"
            "渡っていない)",
        )
        # 材料は台帳の最新の同部屋束 (材料探しは提示可否を問わない契約)。
        self.assertEqual(reseats[0].rendered_text, self.old_full)


class PerceptionCapPlanCostTest(unittest.TestCase):
    """下ろし計画は堆積件数に線形 (2026-09-05 Codex 三巡 #3)。

    境界候補ごとに残り全部の ``room_state_json`` を読み直して回復後の文面を
    組み直していた頃は二乗時間だった (実測 1,000 件 4.1 秒 / 2,000 件 28.7 秒)。
    しかもこれは ``_db_lock`` を握ったままの区間なので、知覚が堆積した環境 —
    まさにこの機構が救おうとしている形 — でロックが空かなくなる。
    """

    #: 本番の堆積を上回る規模。1 件あたりの本文は 500 字程度に抑える。
    BATCHES = 2_000
    #: 粗い上限。線形の実装は 0.05 秒ほどで終わり、候補ごとに数え直す実装は
    #: 同じ材料で 4.8 秒かかった (計測 2026-09-05) ので、ここで十分に分かれる。
    BUDGET_SECONDS = 1.0
    #: 全部が同じ部屋だと連なりが 1 本になるので、部屋も混ぜる。
    ROOMS = 5

    def _presented(self, count):
        """全文 → 差分 → 差分 … の連なりを持つ提示列 (DB は使わない)。"""
        batches = []
        latest = {}
        for index in range(count):
            room = f"b{index % self.ROOMS}"
            key = room_key(room)
            bundle = _room_bundle(
                ["あ" * 500, str(index)], building=room, name=room,
            )
            base = latest.get(key)
            entry = {"key": key, "snapshot": bundle}
            if base is None:
                block = render_room_full(bundle)
                entry["is_diff"] = False
            else:
                block = f"# 「{room}」の様子 (前回見たときからの変化)\n{index}"
                entry["is_diff"] = True
                entry["base_digest"] = snapshot_digest(base)
            entry["block"] = block
            latest[key] = bundle
            batches.append(SimpleNamespace(
                id=index + 1, rendered_text=block,
                room_state_json=json.dumps([entry], ensure_ascii=False),
            ))
        return batches

    def test_two_thousand_batches_are_planned_well_under_the_budget(self):
        presented = self._presented(self.BATCHES)
        persona = SimpleNamespace(persona_id="p1", model="test-model")
        with patch(
            "sea.runtime_context.resolve_perception_watermarks",
            return_value=(1_000, 2_000),  # ほぼ全部を下ろす = 候補を全部辿る
        ):
            started = time.perf_counter()
            cutoff = _plan_perception_drop(persona, presented, 0)
            elapsed = time.perf_counter() - started
        # 実際に下ろす道を通っている (計測が空振りでない証拠)。
        self.assertGreater(cutoff, presented[len(presented) // 2].id)
        self.assertLess(
            elapsed, self.BUDGET_SECONDS,
            f"下ろし計画に {elapsed:.1f} 秒かかった "
            f"({self.BATCHES} 件, 上限 {self.BUDGET_SECONDS} 秒) — "
            "境界候補ごとの再計算が戻っている疑い",
        )


if __name__ == "__main__":
    unittest.main()
