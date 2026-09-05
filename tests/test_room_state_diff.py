"""部屋の様子 (room state) の差分提示と移管のテスト (2026-09-04 まはー裁定)。

対象は sai_memory/room_state.py と、その三つの結び目:

- 積む側 (saiverse/dynamic_state.py → SAIMemoryAdapter.push_room_state):
  同じ部屋の前回エントリがまだ提示に見えているなら差分だけを積む。
- 下ろす側 (sai_memory/perception_buffer.mark_batches_annexed): 全文が編纂の
  退場付記で提示から下りるとき、残った最古の同部屋エントリへ全文を移管する。
  書き換えは付記と同一トランザクションでだけ起きる。
- head 側 (sea/head_pipeline の visual_context、2026-09-05 追加): 台帳に土台が
  無くても head が同じ部屋を見せていれば、その姿を土台にする。head は
  (ペルソナ, model) ごとに別々の時点で capture されるので、全文へ開き直すか
  どうかの判定は提示時にだけ置く (issue room_state_duplicates_head_inventory)。

契約 (裁定の文面そのもの):

1. 再訪で全文が提示に見えている間は差分だけが積まれる。
2. 前回全文が付記で下りるとき、最古の残存同部屋エントリへ内容が移管される
   (移管後の提示で差分の土台が読める)。
3. 移管が付記と同一 tx で行われる (付記無しの単独書き換えが起きない)。
4. 初訪問・久しぶり (同部屋バッチが提示に無い) は従来どおり全文。
5. 移管の読み取りが落ちた回は「移管対象なし」に化かさず、付記も境界前進も
   tx ごと rollback して見送る (付記だけが確定して差分が宙に浮く形を作らない)。
6. head が同じ部屋を見せている間、その部屋の全文は知覚に二枚目として出ない。
   head が別の部屋を見せたら、その回の提示だけが全文へ開き直される。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sai_memory.perception_buffer import (
    advance_presentation_cutoff,
    create_consumption_batch,
    format_perception_message,
    get_presentation_cutoff,
    init_perception_buffer_table,
    list_pending,
    list_unannexed_batches,
    mark_batches_annexed,
    push_perception,
    reduce_perceptions,
)
from sai_memory.room_state import (
    ROOM_STATE_KIND,
    build_room_state_push,
    collect_batch_room_states,
    ensure_room_state_base,
    latest_visible_snapshot,
    render_room_diff,
    restore_room_state_bases,
    room_key,
    snapshot_digest,
)
from sea.runtime_context import (
    _merge_consumed_perceptions,
    list_presented_perception_blocks,
)

#: Chronicle 有効相当 (lifecycle 無し = 判定不能 → バッチを隠さない側)。
_RUNTIME = SimpleNamespace(session_lifecycle=None)
#: Chronicle 無効のペルソナ (「編纂なしで忘れる」を選んだ = 提示窓で絞る)。
_RUNTIME_NO_CHRONICLE = SimpleNamespace(
    session_lifecycle=SimpleNamespace(
        is_chronicle_enabled_for_persona=lambda persona: False,
    ),
)


def _room_text(name: str, items) -> str:
    """``get_visual_context(for_perception=True)`` と同じ形の全文を組む。"""
    parts = [
        f"# 「{name}」の様子", "",
        "## 一緒にいるペルソナ", "他のペルソナはいません。", "",
        "---", "",
        "## Building", "",
        "---", "",
        "## Item", "",
    ]
    for ref, label, desc in items:
        parts.append(f"[item:{ref}] [Object] {label}")
        parts.append(desc)
        parts.append("")
    return "\n".join(parts)


class RenderRoomDiffTest(unittest.TestCase):
    """差分本文の組み立て (決定論・かたまり単位)。"""

    def setUp(self):
        self.before = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("6", "古い写真", "セピア色の写真。"),
        ])

    def test_added_item_is_shown_in_full(self):
        after = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("6", "古い写真", "セピア色の写真。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        diff = render_room_diff(self.before, after)
        self.assertIn("「工房」の様子", diff)
        self.assertIn("増えた・変わったもの", diff)
        self.assertIn("[item:7] [Object] 銅のノギス", diff)
        self.assertIn("目盛りが細かいノギス。", diff)
        # 変わっていないアイテムの説明は積み直さない。
        self.assertNotIn("使い込まれた定規。", diff)

    def test_removed_item_shows_only_its_heading(self):
        after = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        diff = render_room_diff(self.before, after)
        self.assertIn("見当たらなくなったもの", diff)
        self.assertIn("- [item:6] [Object] 古い写真", diff)
        self.assertNotIn("セピア色の写真。", diff)

    def test_modified_item_appears_only_as_changed(self):
        after = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("6", "古い写真", "色が褪せてきた写真。"),
        ])
        diff = render_room_diff(self.before, after)
        self.assertIn("色が褪せてきた写真。", diff)
        # 同じ見出しが「消えた」側にも出ると、同じ物が消えて増えたように読める。
        self.assertNotIn("見当たらなくなったもの", diff)

    def test_no_change_condenses_to_one_line(self):
        diff = render_room_diff(self.before, self.before)
        self.assertEqual(diff, "# 「工房」の様子\n前回見たときから変わっていません。")


class RoomStateLedgerTestBase(unittest.TestCase):
    """生の conn で「積む → 消費バッチ確定」を回す土台。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)
        self.clock = 1000

    def _push(
        self, building_id, full_text, *,
        allow_diff=True, media=None, head_full_text=None,
    ):
        payload = build_room_state_push(
            self.conn, building_id, full_text,
            media=media, allow_diff=allow_diff, head_full_text=head_full_text,
        )
        push_perception(
            self.conn, ROOM_STATE_KIND, payload["content"],
            media=payload["media"], metadata=payload["metadata"],
        )
        return payload

    def _flush(self):
        """未消費分を 1 バッチに確定する (adapter の flush と同じ組み立て)。"""
        items = list_pending(self.conn)
        if not items:
            return None
        reduced = ensure_room_state_base(self.conn, reduce_perceptions(items))
        text = format_perception_message(reduced)
        self.clock += 10
        return create_consumption_batch(
            self.conn, [it.id for it in items],
            consumed_at=self.clock, rendered_text=text,
            room_state_json=collect_batch_room_states(reduced, text),
        )

    def _batch(self, batch_id):
        for b in list_unannexed_batches(self.conn):
            if b.id == batch_id:
                return b
        return None


class RoomStatePushTest(RoomStateLedgerTestBase):
    """積む側の判定 (全文か差分か)。"""

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])

    def test_first_visit_pushes_the_full_text(self):
        payload = self._push("b1", self.full_a)
        self.assertEqual(payload["content"], self.full_a)
        state = json.loads(payload["metadata"])["room_state"]
        self.assertFalse(state["is_diff"])
        self.assertEqual(state["key"], room_key("b1"))
        self.assertEqual(state["snapshot"], self.full_a)

    def test_revisit_while_base_is_still_pending_pushes_a_diff(self):
        self._push("b1", self.full_a)
        payload = self._push("b1", self.full_a2)
        self.assertNotEqual(payload["content"], self.full_a2)
        self.assertIn("銅のノギス", payload["content"])
        self.assertTrue(json.loads(payload["metadata"])["room_state"]["is_diff"])

    def test_revisit_while_base_batch_is_presented_pushes_a_diff(self):
        self._push("b1", self.full_a)
        self._flush()
        payload = self._push("b1", self.full_a2)
        self.assertIn("増えた・変わったもの", payload["content"])
        self.assertLess(len(payload["content"]), len(self.full_a2))

    def test_diff_carries_no_media(self):
        media = [{"path": "/tmp/room.png", "mime_type": "image/png"}]
        first = self._push("b1", self.full_a, media=media)
        self.assertEqual(first["media"], media)
        self._flush()
        second = self._push("b1", self.full_a2, media=media)
        self.assertIsNone(second["media"])

    def test_another_room_is_a_separate_base(self):
        self._push("b1", self.full_a)
        self._flush()
        full_b = _room_text("書斎", [("9", "背の高い本棚", "本が詰まっている。")])
        payload = self._push("b2", full_b)
        self.assertEqual(payload["content"], full_b)

    def test_after_the_base_is_annexed_the_next_visit_is_full_again(self):
        base = self._push("b1", self.full_a)
        self.assertEqual(base["content"], self.full_a)
        batch_id = self._flush()
        mark_batches_annexed(self.conn, [batch_id], "entry-1")
        self.conn.commit()
        self.assertIsNone(latest_visible_snapshot(self.conn, room_key("b1")))
        payload = self._push("b1", self.full_a2)
        self.assertEqual(payload["content"], self.full_a2)

    def test_chronicle_disabled_persona_always_gets_the_full_text(self):
        self._push("b1", self.full_a, allow_diff=False)
        self._flush()
        payload = self._push("b1", self.full_a2, allow_diff=False)
        self.assertEqual(payload["content"], self.full_a2)

    def test_no_change_revisit_still_records_a_transfer_target(self):
        self._push("b1", self.full_a)
        self._flush()
        payload = self._push("b1", self.full_a)
        self.assertIn("前回見たときから変わっていません。", payload["content"])
        self.assertTrue(json.loads(payload["metadata"])["room_state"]["is_diff"])


class RoomStateTransferTest(RoomStateLedgerTestBase):
    """下ろす側 (付記) に相乗りする移管。"""

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        self.full_a3 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
            ("8", "使い古した鍵", "何を開けるか分からない鍵。"),
        ])
        self._push("b1", self.full_a)
        self.base_id = self._flush()
        self._push("b1", self.full_a2)
        self.diff_id = self._flush()

    def test_annexing_the_base_moves_the_full_text_into_the_oldest_survivor(self):
        before = self._batch(self.diff_id).rendered_text
        self.assertNotIn("使い込まれた定規。", before)

        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()

        survivor = self._batch(self.diff_id)
        self.assertIsNotNone(survivor)
        # 移管後は「その時点の部屋の全文」が読める = 差分の土台を失っていない。
        self.assertEqual(survivor.rendered_text, self.full_a2)
        entry = json.loads(survivor.room_state_json)[0]
        self.assertFalse(entry["is_diff"])
        self.assertTrue(entry["transferred"])

    def test_transferred_text_is_what_the_presentation_shows(self):
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        persona = SimpleNamespace(
            persona_id="p1",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(), is_ready=lambda: True,
            ),
        )
        merged = _merge_consumed_perceptions(_RUNTIME, persona, [])
        self.assertEqual(len(merged), 1)
        self.assertIn("使い込まれた定規。", merged[0]["content"])
        self.assertIn("目盛りが細かいノギス。", merged[0]["content"])

    def test_only_the_oldest_survivor_becomes_full(self):
        self._push("b1", self.full_a3)
        newest_id = self._flush()

        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()

        self.assertEqual(self._batch(self.diff_id).rendered_text, self.full_a2)
        newest = self._batch(newest_id)
        self.assertIn("使い古した鍵", newest.rendered_text)
        self.assertNotIn("使い込まれた定規。", newest.rendered_text)
        self.assertTrue(json.loads(newest.room_state_json)[0]["is_diff"])

    def test_the_rewrite_is_rolled_back_with_the_stamp(self):
        self.conn.execute("BEGIN IMMEDIATE")
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.rollback()

        self.assertIsNotNone(self._batch(self.base_id))  # 付記は戻った
        survivor = self._batch(self.diff_id)
        self.assertNotIn("使い込まれた定規。", survivor.rendered_text)
        self.assertTrue(json.loads(survivor.room_state_json)[0]["is_diff"])

    def _break_the_invariant(self):
        """最古の可視エントリが差分、という壊れた形を移管を通さずに作る。

        付記を直接 SQL で打つ (= mark_batches_annexed を通らない) ので、移管は
        走っていない。残るのは差分だけになった提示。
        """
        self._push("b1", self.full_a3)
        stale_id = self._flush()
        self.conn.execute(
            "UPDATE perception_batches SET annexed_entry_id = 'seed' "
            "WHERE id IN (?, ?)",
            (self.base_id, self.diff_id),
        )
        self.conn.commit()
        self.assertTrue(json.loads(self._batch(stale_id).room_state_json)[0]["is_diff"])
        return stale_id

    def test_no_stamp_means_no_rewrite(self):
        stale_id = self._break_the_invariant()
        before = self._batch(stale_id).rendered_text

        # 既に付記済みの id をもう一度渡す = 印は 1 行も立たない。
        stamped = mark_batches_annexed(self.conn, [self.base_id], "entry-2")
        self.conn.commit()

        self.assertEqual(stamped, 0)
        self.assertEqual(self._batch(stale_id).rendered_text, before)
        self.assertTrue(json.loads(self._batch(stale_id).room_state_json)[0]["is_diff"])

    def test_a_real_stamp_carries_the_transfer(self):
        stale_id = self._break_the_invariant()
        # 別の部屋のバッチを 1 件付記する = 印が 1 行立つ回。
        self._push("b2", _room_text("書斎", [("9", "本棚", "本が詰まっている。")]))
        other_id = self._flush()

        stamped = mark_batches_annexed(self.conn, [other_id], "entry-2")
        self.conn.commit()

        self.assertEqual(stamped, 1)
        self.assertEqual(self._batch(stale_id).rendered_text, self.full_a3)

    def test_transfer_walks_forward_as_each_base_is_annexed(self):
        self._push("b1", self.full_a3)
        newest_id = self._flush()

        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        mark_batches_annexed(self.conn, [self.diff_id], "entry-2")
        self.conn.commit()

        self.assertEqual(self._batch(newest_id).rendered_text, self.full_a3)


class RoomStateMiddleBaseTest(RoomStateLedgerTestBase):
    """中間の一枚だけが提示から下りた形 (2026-09-05 Codex 三巡 #1)。

    差分の土台は「同部屋の**直前**のエントリ」であって、提示に残っている最古の
    全文ではない。「A の全文 → B 追加 → C 追加」を積んで**中間の B だけ**を
    付記すると、最古 (A) は全文のまま残るのに C の土台 (B 時点の全文) が消える。
    最古だけを見る規則ではこの欠落を検出できず、C は土台のない差分のまま
    プロンプトへ乗る (編纂の付記は期間指定なので中間区間だけを対象にできる)。
    """

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_b = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        self.full_c = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
            ("8", "使い古した鍵", "何を開けるか分からない鍵。"),
        ])
        self._push("b1", self.full_a)
        self.a_id = self._flush()
        self._push("b1", self.full_b)
        self.b_id = self._flush()
        self._push("b1", self.full_c)
        self.c_id = self._flush()

    def _entry(self, batch_id):
        return json.loads(self._batch(batch_id).room_state_json)[0]

    def test_a_diff_records_the_fingerprint_of_its_base(self):
        self.assertEqual(self._entry(self.b_id)["base_digest"], snapshot_digest(self.full_a))
        self.assertEqual(self._entry(self.c_id)["base_digest"], snapshot_digest(self.full_b))
        self.assertNotIn("base_digest", self._entry(self.a_id))

    def test_annexing_the_middle_reopens_the_orphaned_diff(self):
        mark_batches_annexed(self.conn, [self.b_id], "entry-1")
        self.conn.commit()

        self.assertEqual(self._batch(self.c_id).rendered_text, self.full_c)
        entry = self._entry(self.c_id)
        self.assertFalse(entry["is_diff"])
        self.assertTrue(entry["transferred"])

    def test_the_oldest_full_text_is_left_where_it_is(self):
        """最古 (A) は既に全文なので触らない — 開き直すのは切れた位置だけ。"""
        mark_batches_annexed(self.conn, [self.b_id], "entry-1")
        self.conn.commit()
        self.assertEqual(self._batch(self.a_id).rendered_text, self.full_a)

    def test_the_presentation_still_shows_the_whole_room(self):
        mark_batches_annexed(self.conn, [self.b_id], "entry-1")
        self.conn.commit()
        persona = SimpleNamespace(
            persona_id="p1",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(), is_ready=lambda: True,
            ),
        )
        merged = _merge_consumed_perceptions(_RUNTIME, persona, [])
        text = "\n".join(m["content"] for m in merged)
        # B で増えたノギスも、C で増えた鍵も読める (C が全文へ戻ったため)。
        self.assertIn("目盛りが細かいノギス。", text)
        self.assertIn("何を開けるか分からない鍵。", text)

    def test_an_intact_chain_is_left_as_a_diff(self):
        """土台が直前に見えている差分は差分のまま (無駄に全文へ戻さない)。"""
        self._push("b2", _room_text("書斎", [("9", "本棚", "本が詰まっている。")]))
        other_id = self._flush()

        self.assertEqual(mark_batches_annexed(self.conn, [other_id], "entry-2"), 1)
        self.conn.commit()

        self.assertTrue(self._entry(self.b_id)["is_diff"])
        self.assertTrue(self._entry(self.c_id)["is_diff"])

    def test_a_pending_diff_whose_middle_base_was_annexed_is_reopened(self):
        """まだ台帳で待っている差分にも同じ検査を通す。

        キーが見えているかどうかだけを見ていた頃は、最古の A がキーを覆って
        いるので「土台あり」と読み、土台の無い差分がそのまま確定していた。
        """
        self._push("b1", _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
            ("8", "使い古した鍵", "何を開けるか分からない鍵。"),
            ("9", "麻の前掛け", "道具を挿すポケットが並ぶ。"),
        ]))
        mark_batches_annexed(self.conn, [self.b_id, self.c_id], "entry-1")
        self.conn.commit()

        batch = self._batch(self._flush())
        entry = json.loads(batch.room_state_json)[0]
        self.assertFalse(entry["is_diff"])
        self.assertTrue(entry["reopened"])
        self.assertIn("道具を挿すポケットが並ぶ。", batch.rendered_text)
        self.assertIn("目盛りが細かいノギス。", batch.rendered_text)


class RoomStateLegacyRecordTest(RoomStateLedgerTestBase):
    """``base_digest`` を持たない旧バッチは旧規則のまま扱う (退行させない)。

    指紋は 2026-09-05 に足した記帳なので、それ以前に確定したバッチには無い。
    照合できないぶん中間欠落は捕まえられないが、旧規則 (同部屋のエントリが
    手前に見えていれば土台ありとみなす) をそのまま適用するので、旧データの
    挙動が今日より悪くなることはない。
    """

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_b = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        self.full_c = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
            ("8", "使い古した鍵", "何を開けるか分からない鍵。"),
        ])
        self._push("b1", self.full_a)
        self.a_id = self._flush()
        self._push("b1", self.full_b)
        self.b_id = self._flush()
        self._push("b1", self.full_c)
        self.c_id = self._flush()
        self._strip_digests()

    def _strip_digests(self):
        """指紋を持たない世代の記帳へ落とす (旧 DB の再現)。"""
        for batch in list_unannexed_batches(self.conn):
            entries = json.loads(batch.room_state_json)
            for entry in entries:
                entry.pop("base_digest", None)
            self.conn.execute(
                "UPDATE perception_batches SET room_state_json = ? WHERE id = ?",
                (json.dumps(entries, ensure_ascii=False), batch.id),
            )
        self.conn.commit()

    def _entry(self, batch_id):
        return json.loads(self._batch(batch_id).room_state_json)[0]

    def test_the_oldest_rule_still_repairs_a_dropped_prefix(self):
        mark_batches_annexed(self.conn, [self.a_id], "entry-1")
        self.conn.commit()
        self.assertEqual(self._batch(self.b_id).rendered_text, self.full_b)
        self.assertFalse(self._entry(self.b_id)["is_diff"])

    def test_a_middle_drop_stays_undetected_in_old_records(self):
        """既知の境界: 指紋が無いので中間欠落は照合できない。"""
        mark_batches_annexed(self.conn, [self.b_id], "entry-1")
        self.conn.commit()
        self.assertTrue(self._entry(self.c_id)["is_diff"])

    def test_a_new_diff_on_top_of_an_old_record_carries_a_fingerprint(self):
        """旧エントリを土台にした新しい差分には指紋が入る (以後は照合できる)。"""
        payload = self._push("b1", _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
            ("8", "使い古した鍵", "何を開けるか分からない鍵。"),
            ("9", "麻の前掛け", "道具を挿すポケットが並ぶ。"),
        ]))
        state = json.loads(payload["metadata"])["room_state"]
        self.assertEqual(state["base_digest"], snapshot_digest(self.full_c))


class RoomStateTransferReadFailureTest(RoomStateLedgerTestBase):
    """移管の読み取りが落ちた回は、付記も境界前進も確定させない (Codex #1)。

    読み取り失敗を「移管対象なし (0 件)」に化かすと、呼び出し側は移管が済んだ
    回と区別できないまま commit する — 土台の全文バッチだけが提示から下り、
    残った差分が土台の無いまま宙に浮く (付記済みバッチは土台にできないので
    復元不能)。だから失敗は例外で伝え、両方の呼び出し点が tx ごと rollback して
    「何もしなかった」へ倒す。次の機会に全体をやり直せばよい。
    """

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        self._push("b1", self.full_a)
        self.base_id = self._flush()
        self._push("b1", self.full_a2)
        self.diff_id = self._flush()

    def _read_failure(self):
        """移管が提示バッチを読む一点を落とす (locked / 表欠けの再現)。"""
        return patch(
            "sai_memory.perception_buffer.list_presented_batches",
            side_effect=sqlite3.OperationalError("database is locked"),
        )

    def _assert_nothing_moved(self):
        survivor = self._batch(self.diff_id)
        self.assertNotIn("使い込まれた定規。", survivor.rendered_text)
        self.assertTrue(json.loads(survivor.room_state_json)[0]["is_diff"])

    def test_the_failure_is_not_reported_as_nothing_to_transfer(self):
        with self._read_failure():
            with self.assertRaises(sqlite3.OperationalError):
                restore_room_state_bases(self.conn)

    def test_the_annexation_is_rolled_back_with_it(self):
        with self._read_failure():
            with self.assertRaises(sqlite3.OperationalError):
                mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        # 付記印は残らない = 土台は提示に残り、次の編纂でチャンクごとやり直せる。
        self.assertIsNotNone(self._batch(self.base_id))
        self._assert_nothing_moved()

    def test_the_presentation_cutoff_does_not_advance_with_it(self):
        with self._read_failure():
            with self.assertRaises(sqlite3.OperationalError):
                advance_presentation_cutoff(self.conn, self.base_id)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)
        self._assert_nothing_moved()

    def test_the_presentation_falls_open_to_showing_every_batch(self):
        """境界前進の入口 (提示の組成) は失敗を飲んで従来どおり全部出す。"""
        persona = SimpleNamespace(
            persona_id="p1", model="test-model",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(), is_ready=lambda: True,
            ),
        )
        with self._read_failure(), patch(
            "sea.runtime_context.resolve_perception_watermarks",
            return_value=(1, 1),  # 必ず下ろしたくなる水位
        ):
            blocks = list_presented_perception_blocks(_RUNTIME, persona, [])
        self.assertEqual(get_presentation_cutoff(self.conn), 0)
        self.assertEqual(
            [b["metadata"]["__perception_batch_id__"] for b in blocks],
            [self.base_id, self.diff_id],
        )
        self._assert_nothing_moved()


class RoomStateBaseLostBeforeConsumptionTest(RoomStateLedgerTestBase):
    """積んでから消費するまでの間に土台が付記で下りたとき (移管の受け皿が無い)。"""

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])

    def test_pending_diff_is_reopened_to_the_full_text(self):
        self._push("b1", self.full_a)
        base_id = self._flush()
        self._push("b1", self.full_a2)  # まだ台帳で待っている差分

        mark_batches_annexed(self.conn, [base_id], "entry-1")
        self.conn.commit()

        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertEqual(batch.rendered_text, self.full_a2)
        entry = json.loads(batch.room_state_json)[0]
        self.assertFalse(entry["is_diff"])
        self.assertTrue(entry["reopened"])

    def test_two_pending_diffs_reopen_only_the_first(self):
        self._push("b1", self.full_a)
        base_id = self._flush()
        self._push("b1", self.full_a2)
        self._push("b1", self.full_a)  # 同じ部屋へ戻って中身も戻った
        mark_batches_annexed(self.conn, [base_id], "entry-1")
        self.conn.commit()

        batch = self._batch(self._flush())
        entries = json.loads(batch.room_state_json)
        self.assertEqual([e["is_diff"] for e in entries], [False, True])
        self.assertIn(self.full_a2, batch.rendered_text)

    def test_pending_diff_keeps_its_shape_while_the_base_is_visible(self):
        self._push("b1", self.full_a)
        self._flush()
        self._push("b1", self.full_a2)
        batch = self._batch(self._flush())
        self.assertNotEqual(batch.rendered_text, self.full_a2)
        self.assertTrue(json.loads(batch.room_state_json)[0]["is_diff"])
        self.assertNotIn("reopened", json.loads(batch.room_state_json)[0])

    def test_non_room_items_pass_through_untouched(self):
        push_perception(self.conn, "world_state", "誰かが入室した")
        items = list_pending(self.conn)
        self.assertEqual(ensure_room_state_base(self.conn, items), items)


class RoomStateChronicleGateTest(unittest.TestCase):
    """積む側の門 (``saiverse.dynamic_state._chronicle_enabled``) の runtime 引き。

    兄弟三箇所 (head_pipeline/integration.py, sections/memory_weave.py,
    day_plan.py) は manager から runtime を ``sea_runtime`` → ``runtime`` の
    二段で引く。ここだけ ``sea_runtime`` しか見ていないと、``runtime`` の名前
    しか持たない manager で lifecycle が引けず、Chronicle 無効のペルソナにも
    差分を積んでしまう (無効のペルソナは窓で土台を忘れる = 差分が宙に浮く)。
    """

    @staticmethod
    def _lifecycle(enabled):
        return SimpleNamespace(
            session_lifecycle=SimpleNamespace(
                is_chronicle_enabled_for_persona=lambda persona: enabled,
            ),
        )

    def _gate(self, manager):
        from saiverse.dynamic_state import _chronicle_enabled
        return _chronicle_enabled(SimpleNamespace(persona_id="p1"), manager)

    def test_it_reads_the_lifecycle_through_sea_runtime(self):
        self.assertFalse(self._gate(
            SimpleNamespace(sea_runtime=self._lifecycle(False)),
        ))

    def test_it_also_reads_the_runtime_alias(self):
        self.assertFalse(self._gate(
            SimpleNamespace(runtime=self._lifecycle(False)),
        ))

    def test_sea_runtime_wins_when_both_are_present(self):
        self.assertTrue(self._gate(SimpleNamespace(
            sea_runtime=self._lifecycle(True), runtime=self._lifecycle(False),
        )))

    def test_no_runtime_at_all_falls_to_enabled(self):
        self.assertTrue(self._gate(SimpleNamespace()))


class RoomStateChronicleToggleTest(RoomStateLedgerTestBase):
    """Chronicle を有効から無効へ切り替えた後の窓絞り (2026-09-05 四巡目 #1)。

    有効な間は差分が積まれる。その後トグルを無効にすると、提示は窓 (anchor)
    より古いバッチを**付記なしで**落とすようになる — 台帳側の回復
    (``restore_room_state_bases``) は絞られていない ``list_presented_batches``
    を見るので「土台はまだ見えている」と読み、走らない。だから土台の全文だけが
    消えて、差分が宙に浮いたままプロンプトへ乗っていた。

    直しは提示時の開き直し (``reopen_lost_bases``): 絞った後の並びで連なりが
    切れていたら、その位置をそのエントリ自身の snapshot で全文へ開く。台帳は
    書き換えない (絞りはペルソナと model ごとに動くので、DB に書ける事実では
    ない)。
    """

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        # Chronicle 有効の間に「全文 → 差分」を積む。
        self._push("b1", self.full_a)
        self.base_id = self._flush()
        self._push("b1", self.full_a2)
        self.diff_id = self._flush()
        self.assertTrue(json.loads(self._batch(self.diff_id).room_state_json)[0]["is_diff"])
        self.persona = SimpleNamespace(
            persona_id="p1", model="test-model",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(), is_ready=lambda: True,
            ),
        )
        # 窓 = 「土台のバッチより後」— 生ログの最古行の時刻で絞らせる。
        base_at = self._batch(self.base_id).consumed_at
        self.window_after_base = [{"created_at": base_at + 1}]

    def _blocks(self, runtime, recent=()):
        return list_presented_perception_blocks(
            runtime, self.persona, list(recent), raise_on_error=True,
        )

    def test_while_chronicle_is_on_the_diff_stays_a_diff(self):
        blocks = self._blocks(_RUNTIME, self.window_after_base)
        self.assertEqual(len(blocks), 2)
        self.assertIn("使い込まれた定規。", blocks[0]["content"])
        self.assertNotIn("使い込まれた定規。", blocks[1]["content"])

    def test_after_the_toggle_the_window_drops_the_base(self):
        blocks = self._blocks(_RUNTIME_NO_CHRONICLE, self.window_after_base)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(
            blocks[0]["metadata"]["__perception_batch_id__"], self.diff_id,
        )

    def test_the_orphaned_diff_is_presented_as_the_full_room(self):
        blocks = self._blocks(_RUNTIME_NO_CHRONICLE, self.window_after_base)
        content = blocks[0]["content"]
        # 土台にしか無かった説明も、差分で増えたものも、両方読める。
        self.assertIn("使い込まれた定規。", content)
        self.assertIn("目盛りが細かいノギス。", content)
        self.assertNotIn("前回見たときからの変化", content)

    def test_the_ledger_and_the_settled_text_are_not_rewritten(self):
        self._blocks(_RUNTIME_NO_CHRONICLE, self.window_after_base)
        survivor = self._batch(self.diff_id)
        self.assertNotIn("使い込まれた定規。", survivor.rendered_text)
        self.assertTrue(json.loads(survivor.room_state_json)[0]["is_diff"])

    def test_the_reopening_is_deterministic(self):
        first = self._blocks(_RUNTIME_NO_CHRONICLE, self.window_after_base)
        second = self._blocks(_RUNTIME_NO_CHRONICLE, self.window_after_base)
        self.assertEqual(
            [b["content"] for b in first], [b["content"] for b in second],
        )

    def test_a_base_still_inside_the_window_leaves_the_diff_alone(self):
        blocks = self._blocks(_RUNTIME_NO_CHRONICLE, [{"created_at": 0}])
        self.assertEqual(len(blocks), 2)
        self.assertNotIn("使い込まれた定規。", blocks[1]["content"])


class RoomStateHeadBasePushTest(RoomStateLedgerTestBase):
    """head が同じ部屋を見せているときの積み方 (issue room_state_duplicates_head_inventory)。

    まはーの再現経路: 部屋 A (アイテム 41 件) → 部屋 B → 部屋 A。戻ったとき、
    台帳には A のエントリが一枚も見えていない (行きの一枚は B のもの) ので
    「初訪問」として全文が積まれ、head の一覧と一字も違わない二重になっていた。
    head の visual_context は移動では撮り直されないので、往復の間ずっと A を
    見せている — その姿を土台にすれば変化だけで足りる。
    """

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        self.full_b = _room_text("書斎", [("9", "背の高い本棚", "本が詰まっている。")])

    def _state(self, payload):
        return json.loads(payload["metadata"])["room_state"]

    def test_returning_to_an_unchanged_room_costs_one_line(self):
        # 行き (B) → 帰り (A)。台帳に A のエントリは無く、head だけが見せている。
        self._push("b2", self.full_b)
        self._flush()
        payload = self._push("b1", self.full_a, head_full_text=self.full_a)
        self.assertEqual(
            payload["content"], "# 「工房」の様子\n前回見たときから変わっていません。",
        )
        state = self._state(payload)
        self.assertTrue(state["is_diff"])
        self.assertEqual(state["base_source"], "head")
        # 全体像は開き直しの受け皿として記帳に残る。
        self.assertEqual(state["snapshot"], self.full_a)

    def test_changes_since_the_head_capture_are_the_only_thing_pushed(self):
        payload = self._push("b1", self.full_a2, head_full_text=self.full_a)
        self.assertIn("増えた・変わったもの", payload["content"])
        self.assertIn("目盛りが細かいノギス。", payload["content"])
        self.assertNotIn("使い込まれた定規。", payload["content"])
        self.assertEqual(self._state(payload)["base_source"], "head")

    def test_no_head_view_of_this_room_still_pushes_the_full_text(self):
        # head が別の部屋を見せている回は、呼び出し側が None を渡す。
        payload = self._push("b1", self.full_a, head_full_text=None)
        self.assertEqual(payload["content"], self.full_a)
        self.assertFalse(self._state(payload)["is_diff"])

    def test_the_ledger_base_wins_over_the_head(self):
        """台帳に土台が見えているなら従来どおりそれを使う (連なりを保つ)。"""
        self._push("b1", self.full_a)
        self._flush()
        payload = self._push("b1", self.full_a2, head_full_text=self.full_b)
        self.assertIn("目盛りが細かいノギス。", payload["content"])
        self.assertNotIn("base_source", self._state(payload))

    def test_a_head_based_diff_carries_no_media(self):
        media = [{"path": "/tmp/room.png", "mime_type": "image/png"}]
        payload = self._push(
            "b1", self.full_a2, media=media, head_full_text=self.full_a,
        )
        self.assertIsNone(payload["media"])

    def test_the_chronicle_gate_does_not_stop_the_head_base(self):
        """``allow_diff`` の理由 (窓絞りで台帳の土台が消える) は head には無い。"""
        payload = self._push(
            "b1", self.full_a, allow_diff=False, head_full_text=self.full_a,
        )
        self.assertIn("変わっていません", payload["content"])
        self.assertEqual(self._state(payload)["base_source"], "head")

    def test_a_pending_head_based_entry_is_not_a_base_for_the_next_push(self):
        self._push("b1", self.full_a2, head_full_text=self.full_a)
        self.assertIsNone(latest_visible_snapshot(self.conn, room_key("b1")))
        payload = self._push("b1", self.full_a2)
        self.assertEqual(payload["content"], self.full_a2)

    def test_a_settled_head_based_entry_is_not_a_base_either(self):
        self._push("b1", self.full_a2, head_full_text=self.full_a)
        self._flush()
        self.assertIsNone(latest_visible_snapshot(self.conn, room_key("b1")))
        payload = self._push("b1", self.full_a2)
        self.assertEqual(payload["content"], self.full_a2)

    def test_a_pending_head_based_diff_is_not_reopened_on_consumption(self):
        """土台が台帳の外なので、付記で土台が下りるという壊れ方が起きない。"""
        self._push("b1", self.full_a2, head_full_text=self.full_a)
        items = ensure_room_state_base(self.conn, reduce_perceptions(list_pending(self.conn)))
        self.assertEqual(len(items), 1)
        self.assertIn("増えた・変わったもの", items[0].content)
        self.assertNotIn("使い込まれた定規。", items[0].content)


class RoomStateHeadPresentationTest(RoomStateLedgerTestBase):
    """head 土台の差分をいつ全文へ開き直すか (提示時・model ごと)。

    head は (ペルソナ, model) ごとに別々の時点で capture されるので、「その差分の
    部屋の全体像が今この Session に見えているか」は台帳へ書けない。Chronicle
    無効の窓絞りと同じ扱いで、その回の提示文面だけを差し替える。
    """

    def setUp(self):
        super().setUp()
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])
        self.batch_id = None
        self._push("b1", self.full_a2, head_full_text=self.full_a)
        self.batch_id = self._flush()
        self.persona = SimpleNamespace(
            persona_id="p1", model="test-model",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(), is_ready=lambda: True,
            ),
        )

    def _blocks(self, head_building_id, head_text=""):
        with patch(
            "sea.head_pipeline.current_head_room",
            return_value=(head_building_id, head_text),
        ):
            return list_presented_perception_blocks(
                _RUNTIME, self.persona, [], raise_on_error=True,
            )

    def test_while_the_head_shows_the_room_the_diff_stays_a_diff(self):
        content = self._blocks("b1", self.full_a)[0]["content"]
        self.assertIn("目盛りが細かいノギス。", content)
        self.assertNotIn("使い込まれた定規。", content)

    def test_a_recaptured_head_of_the_same_room_does_not_reopen_it(self):
        """head が撮り直されて中身が変わっても、同じ部屋なら差分のまま。

        全体像は撮り直した head が最新の姿で見せている。ここで開き直すと、
        同じ部屋の全文が head と知覚に二枚並ぶ (消そうとしている重複そのもの)。
        """
        content = self._blocks("b1", self.full_a2)[0]["content"]
        self.assertNotIn("使い込まれた定規。", content)

    def test_when_the_head_moves_to_another_room_the_full_text_comes_back(self):
        content = self._blocks("b2", "…書斎の様子…")[0]["content"]
        self.assertIn("使い込まれた定規。", content)
        self.assertIn("目盛りが細かいノギス。", content)
        self.assertNotIn("増えた・変わったもの", content)

    def test_an_unreadable_head_falls_to_the_full_text(self):
        content = self._blocks(None)[0]["content"]
        self.assertIn("使い込まれた定規。", content)

    def test_the_ledger_and_the_settled_text_are_not_rewritten(self):
        self._blocks("b2")
        row = self._batch(self.batch_id)
        self.assertNotIn("使い込まれた定規。", row.rendered_text)
        entry = json.loads(row.room_state_json)[0]
        self.assertTrue(entry["is_diff"])
        self.assertEqual(entry["base_source"], "head")

    def test_the_ledger_side_recovery_never_touches_a_head_based_diff(self):
        self.assertEqual(restore_room_state_bases(self.conn), 0)
        self.assertNotIn(
            "使い込まれた定規。", self._batch(self.batch_id).rendered_text,
        )

    def test_the_accounting_matches_what_is_actually_sent(self):
        """下ろし量の見積もりと提示が同じ head の値を見る。"""
        from sea.runtime_context import _presented_chars_after_transfer

        presented = list_unannexed_batches(self.conn)
        for building_id, head_text in (("b1", self.full_a), ("b2", "")):
            with self.subTest(head=building_id):
                blocks = self._blocks(building_id, head_text)
                sent = sum(len(b["content"]) for b in blocks)
                predicted = _presented_chars_after_transfer(
                    presented, head_room_key=room_key(building_id),
                )
                self.assertEqual(predicted, sent)

    def test_a_supplied_head_room_wins_over_reading_the_head_again(self):
        """呼び出し側が渡した部屋が使われ、head は読み直されない。

        prepare_context は head を先に描画して固定する。その後で head を読み
        直すと、間に走った Metabolism / TTL の撮り直しで別の部屋を見てしまい、
        「送った head には無い部屋」の差分をそのまま出す (逆に、送った head に
        は載っている部屋を全文へ開き直して二枚並べる) — 2026-09-05 Codex 指摘。
        """
        from sea.runtime_context import _presented_chars_after_transfer

        presented = list_unannexed_batches(self.conn)
        # 読み直し側は「別の部屋 (b2)」を返す = 渡した値を無視すれば開き直る。
        with patch(
            "sea.head_pipeline.current_head_room", return_value=("b2", "…"),
        ) as reread:
            blocks = list_presented_perception_blocks(
                _RUNTIME, self.persona, [], raise_on_error=True,
                head_room_key=room_key("b1"),
            )
        reread.assert_not_called()
        content = blocks[0]["content"]
        self.assertNotIn("使い込まれた定規。", content)
        self.assertIn("目盛りが細かいノギス。", content)
        # 勘定も同じ値で走る (提示と一致する)。
        self.assertEqual(
            _presented_chars_after_transfer(presented, head_room_key=room_key("b1")),
            sum(len(b["content"]) for b in blocks),
        )

    def test_a_supplied_none_means_the_head_shows_no_room(self):
        """``None`` は「渡されていない」ではなく「どの部屋も見せていない」。"""
        with patch(
            "sea.head_pipeline.current_head_room", return_value=("b1", self.full_a),
        ) as reread:
            blocks = list_presented_perception_blocks(
                _RUNTIME, self.persona, [], raise_on_error=True,
                head_room_key=None,
            )
        reread.assert_not_called()
        self.assertIn("使い込まれた定規。", blocks[0]["content"])


class HeadRoomViewTest(unittest.TestCase):
    """head 側の供給 — 「head が見せている部屋」をどこから取るか。

    VisualContextSection は同じ capture の瞬間に、head 用の姿と**知覚記法の姿**
    (``room_text``) の両方を焼く。書式が同じでないと差分の土台にできないため。
    二つは **一度の世界の読み**から作る (`build_visual_contexts`) — 別々に読むと
    間に世界が動いて、head の姿と差分の土台が別時点になる。
    """

    def _ctx(self, building_id="b1"):
        from sea.head_pipeline.types import LineHeadInput
        return LineHeadInput(
            persona_id="p1", model_key="m1", current_building_id=building_id,
            persona=SimpleNamespace(persona_id="p1", persona_dir="/tmp/p1"),
            manager=SimpleNamespace(),
        )

    def _capture(self, head_text, room_text, *, reads=None):
        from sea.head_pipeline.sections.visual_context import VisualContextSection

        def _fake(building_id=None, views=()):
            if reads is not None:
                reads.append(building_id)
            out = []
            for view in views:
                text = room_text if view.for_perception else head_text
                out.append(
                    [{"content": text, "metadata": {"media": []}}] if text else []
                )
            return out

        with patch(
            "builtin_data.tools.get_visual_context.build_visual_contexts", _fake,
        ):
            return VisualContextSection().capture(self._ctx())

    def test_capture_keeps_both_shapes_of_the_same_moment(self):
        reads = []
        snapshot = self._capture(
            "<system>…head…</system>", "# 「工房」の様子\n…", reads=reads,
        )
        self.assertEqual(snapshot.building_id, "b1")
        self.assertEqual(snapshot.room_text, "# 「工房」の様子\n…")
        self.assertIn("head", snapshot.text)
        # 世界を読むのは一度だけ (二度読むと二つの姿が別時点になる)。
        self.assertEqual(reads, ["b1"])

    def test_the_room_text_is_not_rendered_into_the_head(self):
        from sea.head_pipeline.sections.visual_context import VisualContextSection
        snapshot = self._capture("<system>…head…</system>", "# 「工房」の様子\n…")
        rendered = VisualContextSection().render(snapshot)
        self.assertEqual(rendered.text, snapshot.text)

    def test_an_empty_perception_shape_still_yields_a_head(self):
        snapshot = self._capture("<system>…head…</system>", "")
        self.assertIn("head", snapshot.text)
        self.assertEqual(snapshot.room_text, "")

    def test_a_failing_world_read_yields_no_head_at_all(self):
        """読みが落ちたら head も空 (人格に属さない部屋を描かない、従来どおり)。"""
        from sea.head_pipeline.sections.visual_context import VisualContextSection

        def _boom(building_id=None, views=()):
            raise RuntimeError("boom")

        with patch(
            "builtin_data.tools.get_visual_context.build_visual_contexts", _boom,
        ):
            snapshot = VisualContextSection().capture(self._ctx())
        self.assertEqual(snapshot.text, "")
        self.assertEqual(snapshot.room_text, "")
        self.assertIsNone(snapshot.building_id)

    def test_a_head_that_renders_nothing_is_not_showing_a_room(self):
        """head 本文が空なら「見せている」に数えない (全体像がどこにも無くなる)。"""
        from sea.head_pipeline import current_head_room
        from sea.head_pipeline.sections.visual_context import VisualContextSnapshot
        for snapshot in (
            VisualContextSnapshot(
                text="", media=(), building_id="b1", room_text="# 「工房」の様子",
            ),
            VisualContextSnapshot(
                text="x", media=(), building_id="b1", room_text="",
            ),
        ):
            with self.subTest(text=snapshot.text, room=snapshot.room_text):
                pipeline = SimpleNamespace(
                    get_snapshot=lambda p, m, s=snapshot: SimpleNamespace(
                        sections={"visual_context": s},
                    ),
                )
                self.assertEqual(
                    current_head_room(
                        SimpleNamespace(persona_id="p1", model="m1"),
                        pipeline=pipeline,
                    ),
                    (None, ""),
                )

    def test_serialization_round_trips_and_old_rows_stay_readable(self):
        from sea.head_pipeline.sections.visual_context import VisualContextSection
        section = VisualContextSection()
        snapshot = self._capture("<system>…head…</system>", "# 「工房」の様子\n…")
        restored = section.deserialize_snapshot(section.serialize_snapshot(snapshot))
        self.assertEqual(restored, snapshot)
        legacy = section.deserialize_snapshot(json.dumps({"text": "x", "media": []}))
        self.assertIsNone(legacy.building_id)
        self.assertEqual(legacy.room_text, "")

    def test_current_head_room_does_not_capture_when_there_is_no_snapshot(self):
        from sea.head_pipeline import current_head_room
        pipeline = SimpleNamespace(get_snapshot=lambda persona_id, model_key: None)
        persona = SimpleNamespace(persona_id="p1", model="m1")
        self.assertEqual(
            current_head_room(persona, pipeline=pipeline), (None, ""),
        )

    def test_the_rendered_head_hands_back_the_room_it_actually_showed(self):
        """描画で固定した head の部屋を out-param が持ち帰る (後の撮り直しに動かない)。

        prepare_context は head を先に描画して固定し、知覚の提示はその後で
        組む。判定用に head を読み直すと、間に走った Metabolism / TTL の
        撮り直しで**送った head とは別の部屋**を見てしまう (2026-09-05 Codex
        指摘)。描画の中で確定させた値を渡す形にして、二つを同じにする。
        """
        from sea.head_pipeline import (
            HeadPipeline,
            HeadSectionRegistry,
            build_line_head_input,
            current_head_room,
        )
        from sea.head_pipeline.integration import render_head_messages
        from sea.head_pipeline.sections.visual_context import VisualContextSection

        registry = HeadSectionRegistry()
        registry.register(VisualContextSection())
        pipeline = HeadPipeline(registry=registry)
        persona = SimpleNamespace(persona_id="p1", persona_dir="/tmp/p1", model="m1")
        manager = SimpleNamespace()

        def _fake(building_id=None, views=()):
            return [
                [{
                    "content": (
                        f"# 「{building_id}」の様子 "
                        f"({'room' if view.for_perception else 'head'})"
                    ),
                    "metadata": {"media": []},
                }]
                for view in views
            ]

        head_room_out = {}
        with patch(
            "builtin_data.tools.get_visual_context.build_visual_contexts", _fake,
        ):
            render_head_messages(
                persona, manager, "b1",
                enabled_sections={"visual_context"},
                pipeline=pipeline, head_room_out=head_room_out,
            )
            # 描画のあとで別の部屋の capture が走る (Metabolism / TTL の撮り直し)。
            pipeline.capture_all(
                build_line_head_input(persona, manager, "b2", model_key="m1"),
            )
            reread = current_head_room(persona, model_key="m1", pipeline=pipeline)

        # 読み直しは既に b2 を指している — が、送った head は b1 のまま。
        self.assertEqual(reread[0], "b2")
        self.assertEqual(head_room_out["building_id"], "b1")
        self.assertEqual(head_room_out["room_text"], "# 「b1」の様子 (room)")

    def test_a_head_without_the_visual_section_shows_no_room(self):
        """visual_context を描画しない呼び出しは「どの部屋も見せていない」を返す。

        enabled_sections が visual_context を外した prompt には部屋の全体像が
        載らない。それでも pin した snapshot の部屋を書き戻すと、提示側が
        「head が見せている」と判定して差分を圧縮したまま送る — 全体像なしの
        差分という契約破れになる (2026-09-05 Codex 二巡)。書き戻しは
        (None, "") = 差分は全文へ開き直される側に倒す。
        """
        from sea.head_pipeline import HeadPipeline, HeadSectionRegistry
        from sea.head_pipeline.integration import render_head_messages
        from sea.head_pipeline.sections.visual_context import VisualContextSection

        registry = HeadSectionRegistry()
        registry.register(VisualContextSection())
        pipeline = HeadPipeline(registry=registry)
        persona = SimpleNamespace(persona_id="p1", persona_dir="/tmp/p1", model="m1")
        manager = SimpleNamespace()

        def _fake(building_id=None, views=()):
            return [
                [{"content": f"# 「{building_id}」の様子", "metadata": {"media": []}}]
                for _ in views
            ]

        head_room_out = {}
        with patch(
            "builtin_data.tools.get_visual_context.build_visual_contexts", _fake,
        ):
            render_head_messages(
                persona, manager, "b1",
                enabled_sections=set(),
                pipeline=pipeline, head_room_out=head_room_out,
            )
        self.assertIsNone(head_room_out["building_id"])
        self.assertEqual(head_room_out["room_text"], "")

    def test_current_head_room_reads_the_visual_context_section(self):
        from sea.head_pipeline import current_head_room
        from sea.head_pipeline.sections.visual_context import VisualContextSnapshot
        snapshot = SimpleNamespace(sections={
            "visual_context": VisualContextSnapshot(
                text="x", media=(), building_id="b1", room_text="# 「工房」の様子",
            ),
        })
        pipeline = SimpleNamespace(get_snapshot=lambda persona_id, model_key: snapshot)
        persona = SimpleNamespace(persona_id="p1", model="m1")
        self.assertEqual(
            current_head_room(persona, pipeline=pipeline),
            ("b1", "# 「工房」の様子"),
        )


class RoomStateEntryHandoffTest(unittest.TestCase):
    """入室の結び目 — head の姿を土台として渡すのは同じ部屋のときだけ。"""

    def _run_entry(self, head_building_id):
        from saiverse.dynamic_state import DynamicStateManager
        calls = {}

        def _push_room_state(building_id, content, **kwargs):
            calls["building_id"] = building_id
            calls["kwargs"] = kwargs

        persona = SimpleNamespace(
            persona_id="p1", persona_dir="/tmp/p1",
            sai_memory=SimpleNamespace(push_room_state=_push_room_state),
        )
        manager = SimpleNamespace(personas={}, occupants={}, feed_manager=None)
        with patch(
            "builtin_data.tools.get_visual_context.get_visual_context",
            lambda **kwargs: [{"content": "# 「工房」の様子", "metadata": {"media": []}}],
        ), patch(
            "sea.head_pipeline.current_head_room",
            return_value=(head_building_id, "# 「工房」の様子 (head)"),
        ):
            DynamicStateManager.on_building_entered(persona, "b1", manager)
        return calls

    def test_the_head_view_is_handed_over_for_the_same_room(self):
        calls = self._run_entry("b1")
        self.assertEqual(calls["building_id"], "b1")
        self.assertEqual(
            calls["kwargs"]["head_full_text"], "# 「工房」の様子 (head)",
        )

    def test_a_head_showing_another_room_is_not_handed_over(self):
        calls = self._run_entry("b2")
        self.assertIsNone(calls["kwargs"]["head_full_text"])

    def test_a_head_built_by_this_very_entry_is_not_a_base(self):
        """入室処理が作った head を土台にしない (初訪問は従来どおり全文)。

        ``inject_diff_notifications`` は ensure_snapshot を通るので、snapshot が
        未構築のとき・anchor TTL が切れているときは移動先の姿で head を撮り直す。
        その head を土台にすると、ペルソナが一度も見ていない部屋に「前回見た
        ときから変わっていません」が付く (2026-09-05 Codex 指摘)。
        """
        from saiverse.dynamic_state import DynamicStateManager
        calls = {}
        head = {"building_id": None}

        def _inject(persona, manager, building_id, **kwargs):
            # ensure_snapshot が移動先で capture_all した状態を模す。
            head["building_id"] = building_id
            return False

        def _push_room_state(building_id, content, **kwargs):
            calls["kwargs"] = kwargs

        persona = SimpleNamespace(
            persona_id="p1", persona_dir="/tmp/p1",
            sai_memory=SimpleNamespace(push_room_state=_push_room_state),
        )
        manager = SimpleNamespace(personas={}, occupants={}, feed_manager=None)
        with patch(
            "builtin_data.tools.get_visual_context.get_visual_context",
            lambda **kwargs: [{"content": "# 「工房」の様子", "metadata": {"media": []}}],
        ), patch(
            "sea.head_pipeline.inject_diff_notifications", _inject,
        ), patch(
            "sea.head_pipeline.current_head_room",
            side_effect=lambda *a, **kw: (head["building_id"], "# 「工房」の様子 (head)"),
        ):
            DynamicStateManager.on_building_entered(persona, "b1", manager)

        # 入室が head を作った後でも、土台は「入室前に見えていた head」= 無し。
        self.assertEqual(head["building_id"], "b1")
        self.assertIsNone(calls["kwargs"]["head_full_text"])


class RoomStateAdapterFlushTest(unittest.TestCase):
    """本物の adapter で「積む → 消費 → 付記 → 移管」を一周する。"""

    PERSONA_ID = "room-state-tester"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        persona_path = Path(self._tmp.name) / "personas" / self.PERSONA_ID
        persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup)

        class _DummyEmbedder:
            def __init__(self, model=None, **kwargs):
                self.model_name = model

            def embed(self, texts, **kwargs):
                return [[0.0] * 3 for _ in texts]

        patcher = patch("saiverse_memory.adapter.Embedder", _DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter(
            self.PERSONA_ID, persona_dir=persona_path, resource_id=self.PERSONA_ID,
        )
        self.full_a = _room_text("工房", [("5", "真鍮の定規", "使い込まれた定規。")])
        self.full_a2 = _room_text("工房", [
            ("5", "真鍮の定規", "使い込まれた定規。"),
            ("7", "銅のノギス", "目盛りが細かいノギス。"),
        ])

    def _cleanup(self):
        import gc
        try:
            self.adapter.close()
        except Exception:
            pass
        gc.collect()
        os.environ.pop("SAIMEMORY_MEMORY", None)
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_full_cycle_through_the_adapter(self):
        self.adapter.push_room_state("b1", self.full_a)
        first = self.adapter.flush_perception_buffer_payload()
        self.assertIsNotNone(first)
        self.assertIn("使い込まれた定規。", first["content"])

        self.adapter.push_room_state("b1", self.full_a2)
        second = self.adapter.flush_perception_buffer_payload()
        self.assertIsNotNone(second)
        self.assertIn("銅のノギス", second["content"])
        self.assertNotIn("使い込まれた定規。", second["content"])

        with self.adapter._db_lock:
            batches = list_unannexed_batches(self.adapter.conn)
            self.assertEqual(len(batches), 2)
            self.assertTrue(all(b.room_state_json for b in batches))
            mark_batches_annexed(self.adapter.conn, [batches[0].id], "entry-1")
            self.adapter.conn.commit()
            survivors = list_unannexed_batches(self.adapter.conn)

        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0].rendered_text, self.full_a2)


if __name__ == "__main__":
    unittest.main()
