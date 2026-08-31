"""提示コンテキストの中で畳まれた範囲の提示 (sea/session_window.py) のテスト。

docs/intent/chronicle_eviction.md §6 (見せ方):

- 畳んだ範囲は**元の時系列位置**で digest に置き換える (別枠に寄せない)。
- 置き換えには圧縮マークの注釈を添える (過去が新規情報に化けないため)。
- あらすじが引けないときは**生ログのまま残す** (体験を黙って消さない)。
"""
from __future__ import annotations

import unittest

from sea.session_window import (
    FOLDED_MARKER,
    FoldedRange,
    apply_folds,
    deserialize_folds,
    prune_folds,
    serialize_folds,
)


def msg(mid, at=100, content="本文"):
    return {"id": mid, "content": content, "created_at": at}


class ApplyFoldsTest(unittest.TestCase):
    def setUp(self):
        self.messages = [msg(f"m{i}", 100 + i) for i in range(5)]

    def test_fold_is_replaced_in_place(self):
        """畳んだ範囲は元の位置で 1 通の置き換えになり、前後の並びは動かない。"""
        fold = FoldedRange(
            message_ids=["m1", "m2"], start_at=101, end_at=102,
            chronicle_entry_ids=["e1"], chronicle_short_ids=[7],
        )
        out = apply_folds(self.messages, [fold], lambda f: "作業の要約")
        self.assertEqual([m["id"] for m in out], ["m0", "folded:m1", "m3", "m4"])
        placeholder = out[1]
        self.assertEqual(placeholder["created_at"], 101)
        self.assertTrue(placeholder["metadata"][FOLDED_MARKER])

    def test_placeholder_carries_compression_notice_and_spell_hint(self):
        """圧縮マーク: ①圧縮されている ②スペルで中身に戻れる、が伝わる。"""
        fold = FoldedRange(
            message_ids=["m1", "m2"], start_at=101, end_at=102,
            chronicle_short_ids=[7],
        )
        out = apply_folds(self.messages, [fold], lambda f: "作業の要約")
        content = out[1]["content"]
        self.assertIn("圧縮されています", content)
        self.assertIn("消えたのではなく", content)
        self.assertIn("ch:7", content)
        self.assertIn("作業の要約", content)
        # 機構の声は system 包み — ペルソナの発話と混ざらない
        self.assertTrue(content.startswith("<system>"))
        self.assertEqual(out[1]["role"], "user")

    def test_unresolvable_digest_keeps_raw_log(self):
        """あらすじが引けない範囲は生のまま通す (記憶の連続性 > 軽量化)。"""
        fold = FoldedRange(message_ids=["m1", "m2"], start_at=101, end_at=102)
        out = apply_folds(self.messages, [fold], lambda f: None)
        self.assertEqual([m["id"] for m in out], ["m0", "m1", "m2", "m3", "m4"])

    def test_resolver_exception_keeps_raw_log(self):
        fold = FoldedRange(message_ids=["m1"], start_at=101, end_at=101)

        def _boom(_fold):
            raise RuntimeError("db gone")

        out = apply_folds(self.messages, [fold], _boom)
        self.assertEqual([m["id"] for m in out], ["m0", "m1", "m2", "m3", "m4"])

    def test_multiple_folds_keep_order(self):
        folds = [
            FoldedRange(message_ids=["m0"], start_at=100, end_at=100),
            FoldedRange(message_ids=["m3", "m4"], start_at=103, end_at=104),
        ]
        out = apply_folds(self.messages, folds, lambda f: "要約")
        self.assertEqual(
            [m["id"] for m in out], ["folded:m0", "m1", "m2", "folded:m3"],
        )

    def test_fold_head_outside_window_still_shows_placeholder(self):
        """範囲の先頭が提示コンテキストの外に出ても、提示コンテキストに残る分は置き換えとして見える。

        anchor が範囲の途中まで前進した圧縮区間で先頭固定にすると、置き換えも生ログも
        出ず**体験が黙って消える**（提示コンテキストにも head にも出ない）。位置は「提示コンテキストに残って
        いる最初のメッセージ」に取る。
        """
        fold = FoldedRange(
            message_ids=["gone0", "m0", "m1"], start_at=90, end_at=101,
            chronicle_short_ids=[7],
        )
        out = apply_folds(self.messages, [fold], lambda f: "要約")
        self.assertEqual([m["id"] for m in out], ["folded:m0", "m2", "m3", "m4"])
        self.assertEqual(out[0]["created_at"], 100)

    def test_no_folds_is_identity(self):
        out = apply_folds(self.messages, [], lambda f: "要約")
        self.assertEqual(out, self.messages)


class PruneFoldsTest(unittest.TestCase):
    def test_fold_outside_window_is_dropped(self):
        """anchor が前進して提示コンテキストから出た圧縮区間は落とす (head の Chronicle 枠が担当)。"""
        folds = [FoldedRange(message_ids=["old0", "old1"])]
        self.assertEqual(prune_folds(folds, ["m0", "m1"]), [])

    def test_partially_live_fold_is_kept(self):
        """一部でも提示コンテキストに残っている圧縮区間は残す (消すと生ログが復活して提示コンテキストが膨らむ)。"""
        folds = [FoldedRange(message_ids=["old0", "m0"])]
        self.assertEqual(len(prune_folds(folds, ["m0", "m1"])), 1)


class SerializationTest(unittest.TestCase):
    def test_roundtrip(self):
        folds = [
            FoldedRange(
                message_ids=["m1", "m2"], start_at=101, end_at=102,
                chronicle_entry_ids=["e1"], chronicle_short_ids=[7],
                episode_ref="episode:3",
            )
        ]
        restored = deserialize_folds(serialize_folds(folds))
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].message_ids, ["m1", "m2"])
        self.assertEqual(restored[0].chronicle_short_ids, [7])
        self.assertEqual(restored[0].episode_ref, "episode:3")

    def test_empty_serializes_to_none(self):
        self.assertIsNone(serialize_folds([]))

    def test_broken_json_degrades_to_no_holes(self):
        """壊れた記録は「圧縮区間なし」に縮退する = 生ログ提示 (体験を失わない側)。"""
        self.assertEqual(deserialize_folds("{not json"), [])
        self.assertEqual(deserialize_folds(None), [])


if __name__ == "__main__":
    unittest.main()
