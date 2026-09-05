"""部屋の様子 (room state) の差分提示と移管のテスト (2026-09-04 まはー裁定)。

対象は sai_memory/room_state.py と、その二つの結び目:

- 積む側 (saiverse/dynamic_state.py → SAIMemoryAdapter.push_room_state):
  同じ部屋の前回エントリがまだ提示に見えているなら差分だけを積む。
- 下ろす側 (sai_memory/perception_buffer.mark_batches_annexed): 全文が編纂の
  退場付記で提示から下りるとき、残った最古の同部屋エントリへ全文を移管する。
  書き換えは付記と同一トランザクションでだけ起きる。

契約 (裁定の文面そのもの):

1. 再訪で全文が提示に見えている間は差分だけが積まれる。
2. 前回全文が付記で下りるとき、最古の残存同部屋エントリへ内容が移管される
   (移管後の提示で差分の土台が読める)。
3. 移管が付記と同一 tx で行われる (付記無しの単独書き換えが起きない)。
4. 初訪問・久しぶり (同部屋バッチが提示に無い) は従来どおり全文。
5. 移管の読み取りが落ちた回は「移管対象なし」に化かさず、付記も境界前進も
   tx ごと rollback して見送る (付記だけが確定して差分が宙に浮く形を作らない)。
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

    def _push(self, building_id, full_text, *, allow_diff=True, media=None):
        payload = build_room_state_push(
            self.conn, building_id, full_text,
            media=media, allow_diff=allow_diff,
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
