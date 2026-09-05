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
import sqlite3
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sai_memory.perception_buffer import (
    create_consumption_batch,
    get_presentation_cutoff,
    init_perception_buffer_table,
    list_dropped_batches,
    list_pending,
    list_presented_batches,
    list_unannexed_batches,
    mark_batches_annexed,
    push_perception,
)
from sai_memory.room_state import (
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

        def racing(conn, batch_id):
            # この呼び出しの直前に、別プロセスがもっと先まで下ろした体。
            real(conn, ids[3])
            return real(conn, batch_id)

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
        self.full = "# 「工房」の様子\n" + "定規と写真がある。" * 100
        self.full2 = self.full + "\nノギスが増えた。"
        self.diff_text = "# 「工房」の様子 (前回見たときからの変化)\nノギスが増えた。"
        key = room_key("b1")
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": key, "is_diff": False, "block": self.full,
            "snapshot": self.full,
        }]))
        self.diff_id = self._batch(self.diff_text, room_state_json=json.dumps([{
            "key": key, "is_diff": True, "block": self.diff_text,
            "snapshot": self.full2, "base_digest": snapshot_digest(self.full),
        }]))
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
        from sai_memory.room_state import room_key

        self.full = "# 「工房」の様子\n" + "定規と写真がある。" * 100
        self.full2 = self.full + "\nノギスが増えた。"
        self.diff_text = "# 「工房」の様子 (前回見たときからの変化)\nノギスが増えた。"
        self.key = room_key("b1")
        # 1 枚目 = 全文 (差分の土台)。2 枚目 = 差分。以降は無関係な知覚で圧力をかける。
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full, "snapshot": self.full,
        }], ensure_ascii=False))
        self.diff_id = self._batch(self.diff_text, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_text, "snapshot": self.full2,
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

    def test_a_dropped_batch_is_no_longer_a_base_for_new_diffs(self):
        from sai_memory.room_state import latest_visible_snapshot

        with self._watermarks(1_500, 2_000):
            self._blocks()
        # 部屋のエントリを持つバッチが両方とも下りたら、土台は見えない
        # = 次の入室は全文を積む。
        self.assertGreaterEqual(get_presentation_cutoff(self.conn), self.diff_id)
        self.assertIsNone(latest_visible_snapshot(self.conn, self.key))


class PerceptionCapTransferGrowthTest(PerceptionCapTestBase):
    """下ろす量は「移管で全文へ膨らんだ後」の字数で決める (Codex 2026-09-05 #2)。

    差分の小さい文面 (ここでは 4 字) で境界を決めると、下ろした直後に移管が
    走って全文 (1,000 字) へ膨れ、上の水位を超えたままになる。すると**新着が
    一件も無い次の呼び出しで境界がまた進む** — 「まとめて下ろす」契約と、
    「新着が無ければ提示列は変わらない」提示の安定性が同時に破れる。
    """

    def setUp(self):
        super().setUp()
        from sai_memory.room_state import room_key

        self.key = room_key("b1")
        self.full = "全" * 1_000
        self.diff = "差" * 4
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full, "snapshot": self.full,
        }], ensure_ascii=False))
        self.diff_id = self._batch(self.diff, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff, "snapshot": self.full,
        }], ensure_ascii=False))
        self.filler = [self._batch(chr(ord("A") + i) * 1_000) for i in range(3)]
        # 提示は 4,089 字 = 1,017 + 21 + 1,017×3。土台だけ下ろすと 3,072 字に
        # 見えるが、移管で差分が全文へ膨らむので実際は 4,068 字 (上の水位超過)。
        self.target, self.high = 3_200, 3_500

    def _presented_chars(self, blocks):
        return sum(
            len(b["content"]) for b in blocks
            if not b["metadata"].get("__perception_omitted__")
        )

    def test_the_first_drop_lands_under_the_target_after_the_transfer(self):
        with self._watermarks(self.target, self.high):
            blocks = self._blocks()
        # 差分バッチまで下ろさないと下の水位には届かない (移管込みの勘定)。
        self.assertEqual(get_presentation_cutoff(self.conn), self.diff_id)
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
        from sea.runtime_context import _presented_chars_after_transfer

        batches = list_presented_batches(self.conn)
        after_base_only = _presented_chars_after_transfer(batches[1:])
        self.assertEqual(after_base_only, 1_017 + 3 * 1_017)
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

        self.full = "# 「工房」の様子\n" + "定規と写真がある。" * 100
        self.full2 = self.full + "\nノギスが増えた。"
        self.diff_text = "# 「工房」の様子 (前回見たときからの変化)\nノギスが増えた。"
        self.key = room_key("b1")
        self.base_id = self._batch(self.full, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full, "snapshot": self.full,
        }], ensure_ascii=False))
        self.diff_id = self._batch(self.diff_text, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_text, "snapshot": self.full2,
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
        blocks = self._blocks(recent=self.recent)  # 組み込み既定 (4万 / 6万)
        perception_chars = sum(
            len(b["content"]) for b in blocks
            if not b["metadata"].get("__perception_omitted__")
        )
        self.assertLessEqual(perception_chars, 40_000)
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
        self.full_a = "# 「工房」の様子\n" + "定規がある。" * 100
        self.full_b = self.full_a + "\nノギスが増えた。"
        self.full_c = self.full_b + "\n鍵が増えた。"
        self.diff_b = "# 「工房」の様子 (前回見たときからの変化)\nノギスが増えた。"
        self.diff_c = "# 「工房」の様子 (前回見たときからの変化)\n鍵が増えた。"
        self.a_id = self._batch(self.full_a, room_state_json=json.dumps([{
            "key": self.key, "is_diff": False,
            "block": self.full_a, "snapshot": self.full_a,
        }], ensure_ascii=False))
        self.b_id = self._batch(self.diff_b, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_b, "snapshot": self.full_b,
            "base_digest": snapshot_digest(self.full_a),
        }], ensure_ascii=False))
        self.c_id = self._batch(self.diff_c, room_state_json=json.dumps([{
            "key": self.key, "is_diff": True,
            "block": self.diff_c, "snapshot": self.full_c,
            "base_digest": snapshot_digest(self.full_b),
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
        from sai_memory.perception_buffer import advance_presentation_cutoff
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
        from sai_memory.room_state import batch_room_states, chain_is_intact

        previous = {}
        total = 0
        for batch in presented:
            rendered = batch.rendered_text
            for entry in batch_room_states(batch.room_state_json):
                key = str(entry["key"])
                prev = previous.get(key)
                previous[key] = entry
                if not entry.get("is_diff") or chain_is_intact(entry, prev):
                    continue
                block = entry.get("block") or ""
                snapshot = entry.get("snapshot") or ""
                if not block or not snapshot or block not in rendered:
                    continue
                rendered = rendered.replace(block, snapshot, 1)
            total += len(_perception_block_text(rendered))
        return total

    def _make_presented(self, rng, count):
        batches = []
        latest = {}
        for index in range(count):
            entries, blocks = [], []
            for room in rng.sample(range(4), rng.randint(0, 2)):
                key = room_key(f"b{room}")
                full = f"# {key} 全文 {index} " + "あ" * rng.randint(20, 120)
                base = latest.get(key)
                entry = {"key": key, "snapshot": full}
                style = rng.random()
                if base is None or style < 0.25:
                    block = full
                    entry["is_diff"] = False
                else:
                    block = f"# {key} 差分 {index}"
                    entry["is_diff"] = True
                    if style < 0.45:
                        pass  # 指紋を持たない旧エントリ (旧規則で扱う)
                    elif style < 0.60:
                        # 別の全文を土台にした = 連なりが最初から切れている
                        entry["base_digest"] = snapshot_digest(full + "!")
                    else:
                        entry["base_digest"] = snapshot_digest(base)
                # 確定文面に現れない block (差し替え不能なので見送られる)
                entry["block"] = block + ("　欠落" if style > 0.95 else "")
                latest[key] = full
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
            key = room_key(f"b{index % self.ROOMS}")
            full = f"# 「{key}」の様子\n" + "あ" * 500 + f"\n{index}"
            base = latest.get(key)
            entry = {"key": key, "snapshot": full}
            if base is None:
                block = full
                entry["is_diff"] = False
            else:
                block = f"# 「{key}」の様子 (前回見たときからの変化)\n{index}"
                entry["is_diff"] = True
                entry["base_digest"] = snapshot_digest(base)
            entry["block"] = block
            latest[key] = full
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
