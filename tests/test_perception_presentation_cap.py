"""知覚を下ろした境界の器と、下ろした跡地の省略の印。

対象は sai_memory/perception_buffer.py の下ろした境界 (`perception_presentation`
の 1 行) と、それを読む sea/runtime_context.py の提示組成
(`list_presented_perception_blocks`)。

**2026-09-09 に「知覚の合計がしきい値を超えたら下ろす」引き金は廃止した**
(docs/intent/presented_context_reduction.md 設計 3)。会話以外の大物は Metabolism の
瞬間の縮み (sai_memory/presented_reduction.py) が減らす側に回り、知覚専用の二水位は
モデル定義・全体設定・保存時検査から消えた。だから提示の組成は境界を**読むだけ**で、
台帳へは一切書かない。

境界そのもの (器) は残っている — 廃止前に下ろされた区間を尊重し、一度下ろした
ものを戻さないため。ここで固定するのはその器の契約:

1. 境界は一方向にだけ進む — 一度下ろしたバッチは提示に戻らない。
2. 下ろした区間には機構名義の省略の印が出る (黙って消さない)。件数はバッチ数では
   なく記録の数。
3. 台帳の行は消えない。下ろされた期間の編纂は従来どおり材料として引き取る。
4. 境界の前進が「部屋の様子」の全文バッチを越えるとき、残った差分へ全文が移管
   される (土台を失わない)。全ての運搬役を越える回は、最新の全文が提示の最古端へ
   置き直される。
5. 提示の組成は行を一切書かない (読み取り専用)。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sai_memory.perception_buffer import (
    advance_presentation_cutoff,
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
    render_room_diff,
    render_room_full,
    room_key,
    snapshot_digest,
)
from sea.eviction_plan import is_injected_perception
from sea.runtime_context import (
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

    def _blocks(self, runtime=_RUNTIME, recent=()):
        return list_presented_perception_blocks(
            runtime, self.persona, list(recent), raise_on_error=True,
        )

    def _drop_through(self, batch_id: int) -> int:
        """境界を ``batch_id`` まで進める (器を直接動かす)。

        引き金 (合計がしきい値を超えたら下ろす) は廃止されたので、下ろされた
        状態は境界を直に進めて作る — 既存の DB に残っている境界と、将来この器を
        使う機構が見る状態はこれと同じ。
        """
        moved = advance_presentation_cutoff(self.conn, batch_id)
        self.conn.commit()
        return moved


class PerceptionCompositionIsReadOnlyTest(PerceptionCapTestBase):
    """提示の組成は台帳へ書かない (2026-09-09 に下ろしの引き金を廃止して以降)。"""

    def setUp(self):
        super().setUp()
        self.ids = [self._batch(chr(ord("A") + i) * 100_000) for i in range(5)]

    def test_a_huge_presentation_never_moves_the_cutoff(self):
        """どれだけ大きくても勝手に下ろさない — 受け皿は縮みの規則と超過の旗。"""
        blocks = self._blocks()
        self.assertEqual(len(blocks), 5)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)
        self.assertFalse(any("省略" in b["content"] for b in blocks))

    def test_repeated_composition_is_stable(self):
        first = self._blocks()
        second = self._blocks()
        self.assertEqual(first, second)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)


class PerceptionCapOneWayTest(PerceptionCapTestBase):
    """境界は一方向にだけ進む (下ろしたものは戻らない)。"""

    def setUp(self):
        super().setUp()
        self.ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]
        self.cutoff = self._drop_through(self.ids[3])
        self.assertEqual(self.cutoff, self.ids[3])

    def test_dropped_batches_do_not_come_back(self):
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
        self.assertEqual(self._drop_through(self.ids[0]), self.cutoff)
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


class PerceptionCutoffGuardBlockedWriteTest(unittest.TestCase):
    """ガードで UPSERT が無効化された回でも、返り値は実境界 (十巡目)。

    A が current を読んだ**後**、UPSERT より**前**に、別接続 B が先にもっと
    大きい境界へ進めて commit した形。UPSERT は「大きい方だけ勝つ」ガードで
    無効になるのに、返り値だけが自分の target を名乗ると、呼び出し側はその値で
    提示を組み、B が既に下ろしたバッチを再提示する (一方向のはずの境界の
    見かけ上の揺り戻し)。

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


class PerceptionOmissionMarkTest(PerceptionCapTestBase):
    """下ろした跡地の省略の印 (機構名義・連続区間を一つに束ねる)。"""

    def setUp(self):
        super().setUp()
        # 1 枚のバッチが 3 件の記録を束ねる形 (Beat 頭に溜まった知覚をまとめて
        # 消費した回)。件数はバッチ数ではなく記録数で出る、をここで固定する。
        self.ids = [
            self._batch(chr(ord("A") + i) * 1_000, records=3) for i in range(5)
        ]
        self._drop_through(self.ids[3])
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
        off = self._blocks(runtime=_RUNTIME_NO_CHRONICLE)
        self.assertIn("[省略された記録]", off[0]["content"])
        self.assertNotIn("あらすじ", off[0]["content"])

    def test_the_mark_disappears_once_the_span_is_annexed(self):
        mark_batches_annexed(self.conn, self.ids[:4], "entry-1")
        self.conn.commit()
        blocks = self._blocks()
        self.assertFalse(any("省略" in b["content"] for b in blocks))


class PerceptionOmissionCountTest(PerceptionCapTestBase):
    """省略の印の件数は「消費時と同じ reduce をかけた後の記録数」。"""

    def test_a_reduced_group_counts_as_the_one_line_it_became(self):
        # 1 枚目 = 同じ reduce_key の 4 件 (文面には 1 件として出た)、
        # 2 枚目 = 2 件、3 枚目 = 1 件 (下ろさない)。
        first = self._batch("X" * 1_000, records=4, reduce_key="c:5")
        second = self._batch("Y" * 1_000, records=2)
        self._batch("Z" * 1_000)
        self._drop_through(second)
        blocks = self._blocks()
        self.assertIn("3 件以上", blocks[0]["content"])
        self.assertEqual(blocks[0]["metadata"]["__perception_omitted__"], 3)
        self.assertGreater(second, first)


class PerceptionCapLedgerTest(PerceptionCapTestBase):
    """下ろすのは提示だけ — 台帳と編纂の一括回収はそのまま。"""

    def setUp(self):
        super().setUp()
        self.ids = [self._batch(chr(ord("A") + i) * 1_000) for i in range(5)]
        self._drop_through(self.ids[3])
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
        # 1 枚目 = 全文 (差分の土台)。2 枚目 = 差分。以降は無関係な知覚。
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

    def test_dropping_the_base_transfers_the_full_text_to_the_survivor(self):
        self._drop_through(self.base_id)
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
        self._drop_through(self.base_id)
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

        self._drop_through(self.diff_id)
        blocks = self._blocks()
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


if __name__ == "__main__":
    unittest.main()
