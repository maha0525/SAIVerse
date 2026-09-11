"""部屋の様子のアイテム表示個数の上限の契約テスト。

設計の正典: docs/intent/room_item_display_cap.md (設計 1・2・4・6 と不変条件)。
束と差分の正典: docs/intent/room_state_packages.md §4・§7。

散らかった部屋では物が見えなくなる、という世界の物理を、部屋の様子の組成
(``build_room_bundle``) と描画 (``render_room_full`` / ``render_room_diff``) で
成立させる。このテストが固定する契約:

- 載せる物の選別は「最近触られた順」= アイテム自身の更新時刻と置き場所の
  更新時刻の**新しい方**の降順、同時刻は ``SHORT_ID`` の降順 (設計 1)。
- 束の中の並びは選別で変わらない (不変条件 6 — 差分の照合と指紋の前提)。
- 上限は既定 10 個、Building の設定で上書きでき、0 も有効な値 (設計 4)。
- 上限で埋もれた物は「見当たらなくなったもの」に出ない (不変条件 3)。
  本当に部屋から消えた物は今までどおり出る。
- 埋もれていた物が触られて表面に戻った回は「増えた・変わったもの」で出る (設計 2)。
- 埋もれた数が 1 以上の回は「ほかに N 個」の一行が全文にも差分にも添う (設計 2)。
- ``capped_keys`` が無い束 (上限を入れる前の記帳) は「外した物なし」として読む。
- Building の設定は API の全ホップを通り、送ってこない画面の保存で消えない (設計 4)。
- DB の列追加は additive — 既存の DB に ``ITEM_DISPLAY_LIMIT`` が足される。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from builtin_data.tools import get_visual_context as gvc
from sai_memory.room_state import (
    BUNDLE_CAPPED_KEYS,
    DEFAULT_ROOM_ITEM_DISPLAY_LIMIT,
    bundle_capped_keys,
    bundle_is_valid,
    canonical_bundle_json,
    capped_notice_line,
    render_room_diff,
    render_room_full,
)

_BASE_TIME = datetime(2026, 9, 11, 12, 0, 0)


def _make_item(short_id, name, *, touched_min=0, location_touched_min=None):
    """建物直下のアイテム 1 件 (``get_all_items_in_building`` の返りと同じ形)。

    ``touched_min`` / ``location_touched_min`` は基準時刻からの分数。
    ``location_touched_min`` が None なら置き場所の時刻は持たない
    (置き場所の行が無い / 読めない世界の再現)。
    """
    item = {
        "item_id": f"uuid-{short_id}",
        "short_id": short_id,
        "type": "object",
        "name": name,
        "description": f"{name}の説明。",
        "created_at": _BASE_TIME,
        "updated_at": _BASE_TIME + timedelta(minutes=touched_min),
    }
    if location_touched_min is not None:
        item["location_updated_at"] = _BASE_TIME + timedelta(
            minutes=location_touched_min,
        )
    return item


def _build_bundle(items, *, item_display_limit=None, building_name="工房",
                  building=None):
    """本物の組成 (build_room_bundle) に食わせて束を取る。

    ``building`` を渡すとその Building オブジェクトをそのまま使う (DB から
    読み込んだ本物の Building を組成に通すため)。渡さなければ、指定された
    上限だけを持つ最小の代役を立てる。
    """
    if building is None:
        building = SimpleNamespace(
            name=building_name,
            base_system_instruction="",
            item_display_limit=item_display_limit,
        )
    persona = SimpleNamespace(
        persona_name="アイフィ",
        current_building_id="b1",
        buildings={"b1": building},
    )
    manager = SimpleNamespace(
        saiverse_home=None,
        occupants={"b1": ["p1"]},
        all_personas={"p1": persona},
        get_all_items_in_building=lambda bid: list(items),
        get_bag_contents_recursive=lambda item_id: [],
        observer_manager=None,
    )
    with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
            patch.object(gvc, "get_active_manager", return_value=manager), \
            patch.object(gvc, "_get_building_image_path", return_value=None):
        return gvc.build_room_bundle("b1")


def _item_keys(bundle):
    return [
        p["key"] for p in bundle["packages"] if p.get("family") == "item"
    ]


class SelectionAxisTest(unittest.TestCase):
    """設計 1: 何を「最近触られた」と数えるか。"""

    def test_newer_of_the_two_timestamps_decides(self):
        """置き場所だけ動いた物 (Bag から出して置いた物) も表面に出る。

        アイテム自身の更新時刻だけで測ると、Bag から出して部屋に置いた物は
        埋もれたままになる — 二つの時刻の新しい方を採る理由そのもの。
        """
        items = [
            # 自身は古いが、置き場所が今さっき動いた (Bag から出して置いた)
            _make_item(1, "定規", touched_min=0, location_touched_min=100),
            # 自身がそこそこ新しいが、置き場所は古い
            _make_item(2, "帳面", touched_min=50, location_touched_min=0),
            _make_item(3, "小石", touched_min=10, location_touched_min=10),
        ]
        bundle = _build_bundle(items, item_display_limit=2)
        self.assertEqual(_item_keys(bundle), ["item:1", "item:2"])
        self.assertEqual(bundle_capped_keys(bundle), ["item:3"])

    def test_missing_location_timestamp_falls_back_to_item_timestamp(self):
        """置き場所の時刻が無い物は、アイテム自身の時刻だけで測る。"""
        items = [
            _make_item(1, "古い物", touched_min=0),
            _make_item(2, "新しい物", touched_min=90),
        ]
        bundle = _build_bundle(items, item_display_limit=1)
        self.assertEqual(_item_keys(bundle), ["item:2"])

    def test_ties_break_on_short_id_descending(self):
        """同時刻は SHORT_ID の降順 — 後から生まれた物が残る。"""
        items = [_make_item(n, f"物{n}", touched_min=5) for n in (1, 2, 3, 4)]
        bundle = _build_bundle(items, item_display_limit=2)
        self.assertEqual(_item_keys(bundle), ["item:3", "item:4"])
        self.assertEqual(bundle_capped_keys(bundle), ["item:1", "item:2"])

    def test_bundle_order_is_unchanged_by_the_cap(self):
        """不変条件 6: 束の並びはキーの昇順のまま (選別だけが上限の仕事)。

        選ばれる二つがキー順で**飛び飛び**になり (item:1 と item:3 が残り、
        間の item:2 が埋もれる)、かつ新しい順とキー順で並びが逆になる材料を
        使う — 連続した先頭 2 件が選ばれる材料では、束が「選んだ順」で並んで
        いても「キー順」で並んでいても同じ答えになり、契約を検分できない。
        """
        items = [
            _make_item(1, "真ん中に触った物", touched_min=50),
            _make_item(2, "ずっと触っていない物", touched_min=10),
            _make_item(3, "さっき触った物", touched_min=90),
        ]
        bundle = _build_bundle(items, item_display_limit=2)
        # 新しい順に選ばれたのは item:3 → item:1 の順だが、束の並びはキーの
        # 昇順のまま。選んだ順で並べる実装だと ["item:3", "item:1"] になる。
        self.assertEqual(_item_keys(bundle), ["item:1", "item:3"])
        self.assertEqual(bundle_capped_keys(bundle), ["item:2"])


class LimitValueTest(unittest.TestCase):
    """設計 4: 既定の 10 個と、Building ごとの上書き。"""

    def test_default_limit_is_ten(self):
        items = [_make_item(n, f"物{n}", touched_min=n) for n in range(1, 16)]
        bundle = _build_bundle(items, item_display_limit=None)
        self.assertEqual(DEFAULT_ROOM_ITEM_DISPLAY_LIMIT, 10)
        self.assertEqual(len(_item_keys(bundle)), 10)
        # 残った 10 個は「新しい順」の上位 = short_id 6..15。
        self.assertEqual(
            _item_keys(bundle), [f"item:{n}" for n in range(6, 16)],
        )
        self.assertEqual(
            bundle_capped_keys(bundle), [f"item:{n}" for n in range(1, 6)],
        )

    def test_building_override_widens_the_room(self):
        items = [_make_item(n, f"物{n}", touched_min=n) for n in range(1, 16)]
        bundle = _build_bundle(items, item_display_limit=14)
        self.assertEqual(len(_item_keys(bundle)), 14)
        self.assertEqual(bundle_capped_keys(bundle), ["item:1"])

    def test_zero_is_a_valid_limit(self):
        """0 = 様子にアイテムを出さない部屋 (真偽値で潰してはいけない値)。"""
        items = [_make_item(n, f"物{n}", touched_min=n) for n in (1, 2, 3)]
        bundle = _build_bundle(items, item_display_limit=0)
        self.assertEqual(_item_keys(bundle), [])
        self.assertEqual(
            bundle_capped_keys(bundle), ["item:1", "item:2", "item:3"],
        )

    def test_under_the_limit_carries_no_capped_keys_field(self):
        """上限に掛からない部屋の束は上限を入れる前と同じ (指紋も変わらない)。"""
        items = [_make_item(n, f"物{n}", touched_min=n) for n in (1, 2)]
        bundle = _build_bundle(items, item_display_limit=10)
        self.assertNotIn(BUNDLE_CAPPED_KEYS, bundle)
        self.assertIsNone(capped_notice_line(bundle))

    def test_broken_limit_value_falls_back_to_the_default(self):
        """手で DB を書いた世界で負数や数でない値が入っても様子は組める。"""
        items = [_make_item(n, f"物{n}", touched_min=n) for n in range(1, 13)]
        for bad in (-1, "たくさん"):
            with self.subTest(bad=bad):
                bundle = _build_bundle(items, item_display_limit=bad)
                self.assertEqual(len(_item_keys(bundle)), 10)


class CappedNoticeLineTest(unittest.TestCase):
    """設計 2: 「ほかに N 個」の一行 (文言は intent の出力例と一字一句同じ)。"""

    EXPECTED = (
        "（ほかに 48 個のアイテムがありますが、埋もれていて見えません。"
        "スペル「埋もれたアイテムを見る」でめくって見られます）"
    )

    def _bundle_with_capped(self, count):
        return {
            "building_id": "b1",
            "building_name": "工房",
            "packages": [],
            BUNDLE_CAPPED_KEYS: [f"item:{n}" for n in range(count)],
        }

    def test_wording_matches_the_intent(self):
        self.assertEqual(
            capped_notice_line(self._bundle_with_capped(48)), self.EXPECTED,
        )

    def test_full_text_carries_the_line(self):
        items = [_make_item(n, f"物{n}", touched_min=n) for n in range(1, 15)]
        bundle = _build_bundle(items, item_display_limit=10)
        text = render_room_full(bundle)
        self.assertIn(
            "（ほかに 4 個のアイテムがありますが、埋もれていて見えません。"
            "スペル「埋もれたアイテムを見る」でめくって見られます）",
            text,
        )

    def test_diff_carries_the_line(self):
        old = _build_bundle(
            [_make_item(n, f"物{n}", touched_min=n) for n in range(1, 12)],
            item_display_limit=10,
        )
        new = _build_bundle(
            [_make_item(n, f"物{n}", touched_min=n) for n in range(1, 15)],
            item_display_limit=10,
        )
        content = render_room_diff(old, new)["content"]
        self.assertIn("ほかに 4 個のアイテムがありますが", content)

    def test_no_change_diff_still_carries_the_line(self):
        bundle = _build_bundle(
            [_make_item(n, f"物{n}", touched_min=n) for n in range(1, 15)],
            item_display_limit=10,
        )
        content = render_room_diff(bundle, bundle)["content"]
        self.assertIn("前回見たときから変わっていません。", content)
        self.assertIn("ほかに 4 個のアイテムがありますが", content)

    def test_zero_limit_room_says_buried_not_empty(self):
        """⭐ 上限 0 の部屋は「ほかに N 個」だけを言い、「ありません」と言わない。

        アイテムの節が空になるのは「部屋に何も無い」ときと「全部埋もれた」とき
        の二つで、意味は正反対。埋もれている回に「アイテムはありません。」を
        出すと、直後の「ほかに 3 個のアイテムがありますが」と真正面から矛盾した
        一枚をペルソナに読ませることになる (全文の分岐を外すと赤になる形)。
        """
        items = [_make_item(n, f"物{n}", touched_min=n) for n in (1, 2, 3)]
        bundle = _build_bundle(items, item_display_limit=0)
        text = render_room_full(bundle)
        self.assertIn(
            "（ほかに 3 個のアイテムがありますが、埋もれていて見えません。"
            "スペル「埋もれたアイテムを見る」でめくって見られます）",
            text,
        )
        self.assertNotIn("アイテムはありません。", text)

    def test_truly_empty_room_still_says_it_is_empty(self):
        """物が一つも無い部屋では、今までどおり「ありません」と言う。"""
        bundle = _build_bundle([], item_display_limit=0)
        text = render_room_full(bundle)
        self.assertIn("アイテムはありません。", text)
        self.assertNotIn("埋もれていて見えません", text)

    def test_no_line_when_nothing_is_buried(self):
        bundle = _build_bundle(
            [_make_item(1, "定規", touched_min=1)], item_display_limit=10,
        )
        self.assertNotIn("埋もれていて見えません", render_room_full(bundle))


class GoneSuppressionTest(unittest.TestCase):
    """不変条件 3: 埋もれただけの物は「見当たらなくなったもの」に出ない。"""

    def test_capped_item_is_not_reported_as_gone(self):
        before = [
            _make_item(1, "定規", touched_min=10),
            _make_item(2, "帳面", touched_min=20),
        ]
        after = [
            # 定規は触られないまま、帳面と新入りに押し出される
            _make_item(1, "定規", touched_min=10),
            _make_item(2, "帳面", touched_min=20),
            _make_item(3, "新入り", touched_min=30),
        ]
        old = _build_bundle(before, item_display_limit=2)
        new = _build_bundle(after, item_display_limit=2)
        self.assertEqual(bundle_capped_keys(new), ["item:1"])
        content = render_room_diff(old, new)["content"]
        self.assertNotIn("## 見当たらなくなったもの", content)
        self.assertNotIn("定規", content)
        self.assertIn("新入り", content)

    def test_really_removed_item_is_still_reported_as_gone(self):
        before = [
            _make_item(1, "定規", touched_min=10),
            _make_item(2, "帳面", touched_min=20),
        ]
        after = [_make_item(2, "帳面", touched_min=20)]
        old = _build_bundle(before, item_display_limit=10)
        new = _build_bundle(after, item_display_limit=10)
        content = render_room_diff(old, new)["content"]
        self.assertIn("## 見当たらなくなったもの", content)
        self.assertIn("定規", content)

    def test_removed_while_visible_is_gone_even_when_others_are_capped(self):
        """埋もれた物がいる回でも、見えていた物が消えたら今までどおり報告する。"""
        before = [_make_item(n, f"物{n}", touched_min=n) for n in (1, 2, 3)]
        after = [_make_item(n, f"物{n}", touched_min=n) for n in (1, 2)]
        old = _build_bundle(before, item_display_limit=2)   # 物1 が埋もれる
        new = _build_bundle(after, item_display_limit=1)    # 物1 が埋もれ、物3 は消えた
        self.assertEqual(bundle_capped_keys(new), ["item:1"])
        content = render_room_diff(old, new)["content"]
        self.assertIn("## 見当たらなくなったもの", content)
        self.assertIn("物3", content)
        self.assertNotIn("- 物1", content)

    def test_buried_item_that_gets_touched_comes_back_as_changed(self):
        """設計 2: 久しぶりに取り出した物は「増えた・変わったもの」で全文が出る。"""
        before = [
            _make_item(1, "古い箱", touched_min=0),
            _make_item(2, "帳面", touched_min=20),
        ]
        old = _build_bundle(before, item_display_limit=1)
        self.assertEqual(bundle_capped_keys(old), ["item:1"])

        after = [
            _make_item(1, "古い箱", touched_min=99),   # いま触られた
            _make_item(2, "帳面", touched_min=20),
        ]
        new = _build_bundle(after, item_display_limit=1)
        self.assertEqual(_item_keys(new), ["item:1"])
        content = render_room_diff(old, new)["content"]
        self.assertIn("## 増えた・変わったもの", content)
        self.assertIn("古い箱", content)
        # 押し出された帳面は「消えた」ではなく、埋もれただけ。
        self.assertNotIn("## 見当たらなくなったもの", content)


class OpenBagContentCapTest(unittest.TestCase):
    """設計 1 (2026-09-11 第三回裁定): 開いた入れ物の中身にも上限がある。

    部屋の上限の勘定には入らないまま、入れ物の中身の**描画**を一階層 10 件に
    絞る — これが無いと、開いた入れ物が部屋の上限の抜け道になる。
    """

    def _bag_child(self, short_id, name, *, touched_min=0, children=None):
        entry = {
            "item_id": f"uuid-{short_id}",
            "short_id": short_id,
            "type": "bag" if children is not None else "object",
            "name": name,
            "description": f"{name}の説明。",
            "created_at": _BASE_TIME,
            "updated_at": _BASE_TIME + timedelta(minutes=touched_min),
            "location_updated_at": _BASE_TIME + timedelta(minutes=touched_min),
            "_children": children or [],
        }
        return entry

    def _render_bag(self, contents):
        bag = {
            "item_id": "uuid-bag",
            "short_id": 100,
            "type": "bag",
            "name": "道具袋",
            "description": "工具の入った袋。",
            "state": {"is_open": True},
            "created_at": _BASE_TIME,
            "updated_at": _BASE_TIME,
        }
        building = SimpleNamespace(
            name="工房", base_system_instruction="", item_display_limit=None,
        )
        persona = SimpleNamespace(
            persona_name="アイフィ", current_building_id="b1",
            buildings={"b1": building},
        )
        manager = SimpleNamespace(
            saiverse_home=None,
            occupants={"b1": ["p1"]},
            all_personas={"p1": persona},
            get_all_items_in_building=lambda bid: [bag],
            get_bag_contents_recursive=lambda item_id: (
                list(contents) if item_id == "uuid-bag" else []
            ),
            observer_manager=None,
        )
        with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
                patch.object(gvc, "get_active_manager", return_value=manager), \
                patch.object(gvc, "_get_building_image_path", return_value=None):
            bundle = gvc.build_room_bundle("b1")
        return render_room_full(bundle)

    def test_twelve_children_render_ten_plus_one_line(self):
        contents = [
            self._bag_child(n, f"中身{n}", touched_min=n)
            for n in range(1, 13)
        ]
        text = self._render_bag(contents)
        self.assertEqual(gvc.BAG_CONTENT_DISPLAY_LIMIT, 10)
        shown = [n for n in range(1, 13) if f"[item:{n}]" in text]
        self.assertEqual(shown, list(range(3, 13)))  # 新しい順の上位 10 件
        self.assertIn(
            "（この入れ物にはほかに 2 個のアイテムがありますが、"
            "埋もれていて見えません）",
            text,
        )

    def test_no_line_when_the_bag_is_under_the_limit(self):
        contents = [
            self._bag_child(n, f"中身{n}", touched_min=n) for n in range(1, 5)
        ]
        text = self._render_bag(contents)
        self.assertNotIn("この入れ物にはほかに", text)

    def test_nested_bag_gets_its_own_cap(self):
        """入れ子の入れ物も各階層で同じ上限が掛かる。"""
        inner = [
            self._bag_child(200 + n, f"奥{n}", touched_min=n)
            for n in range(1, 14)
        ]
        contents = [
            self._bag_child(1, "小箱", touched_min=50, children=inner),
            self._bag_child(2, "定規", touched_min=40),
        ]
        text = self._render_bag(contents)
        # 外側は 2 件なので一行は出ず、内側 (13 件) だけが 3 個を埋める。
        self.assertIn(
            "（この入れ物にはほかに 3 個のアイテムがありますが、"
            "埋もれていて見えません）",
            text,
        )
        self.assertEqual(text.count("この入れ物にはほかに"), 1)
        self.assertNotIn("[item:201]", text)   # 一番古い 3 件は埋もれた
        self.assertNotIn("[item:203]", text)
        self.assertIn("[item:204]", text)
        self.assertIn("[item:213]", text)

    def test_every_level_is_capped_and_each_item_is_drawn_once(self):
        """⭐ 実描画の経路 (build_room_bundle → render_room_full) で、開いた
        入れ物の中身が**各階層 10 件 + 内側の一行**になり、どのアイテムも
        一度だけ出ること。

        入れ子の両方の階層をあふれさせる材料を使う — 片方だけの材料では、
        階層ごとに上限が掛かっているのか一番外だけなのかを区別できない。
        重複の検分も同じ一枚で行う: 中身を親の描画と一覧の両方に書く実装に
        戻ると、同じアイテムが二度出て送る量が黙って倍になる。
        """
        import re

        inner = [
            self._bag_child(200 + n, f"奥{n}", touched_min=n)
            for n in range(1, 13)
        ]
        contents = [self._bag_child(1, "小箱", touched_min=50, children=inner)]
        contents += [
            self._bag_child(n, f"中身{n}", touched_min=n - 1)
            for n in range(2, 13)
        ]
        text = self._render_bag(contents)

        refs = re.findall(r"\[item:(\d+)\]", text)
        self.assertEqual(
            len(refs), len(set(refs)),
            f"同じアイテムが二度描かれている: {refs}",
        )

        # 外側 (道具袋の中身) は 12 件中 10 件 — 小箱と、新しい方の中身 9 件。
        shown_outer = sorted(
            int(r) for r in refs if 1 <= int(r) <= 12
        )
        self.assertEqual(shown_outer, [1] + list(range(4, 13)))
        # 内側 (小箱の中身) も 12 件中 10 件。
        shown_inner = sorted(int(r) for r in refs if 200 < int(r) < 300)
        self.assertEqual(shown_inner, list(range(203, 213)))

        # 「ほかに 2 個」の一行が、あふれた階層ごとに一本ずつ。
        self.assertEqual(
            text.count(
                "（この入れ物にはほかに 2 個のアイテムがありますが、"
                "埋もれていて見えません）"
            ),
            2,
        )
        # 部屋直下は入れ物 1 個なので、部屋の上限の一行は出ない。
        self.assertNotIn("スペル「埋もれたアイテムを見る」", text)

    def test_bag_contents_do_not_count_toward_the_room_limit(self):
        """開いた入れ物の中身は部屋の上限の勘定に入らない (裁定の維持)。"""
        contents = [
            self._bag_child(n, f"中身{n}", touched_min=n) for n in range(1, 30)
        ]
        text = self._render_bag(contents)
        # 部屋直下は入れ物 1 個だけ = 部屋の「ほかに N 個」の一行は出ない。
        self.assertNotIn("スペル「埋もれたアイテムを見る」", text)


class BundleShapeTest(unittest.TestCase):
    """束の検査と指紋 (intent 設計 2 の「実装時に確定」の写し)。"""

    def _bundle(self, capped=None):
        bundle = {
            "building_id": "b1",
            "building_name": "工房",
            "packages": [{
                "key": "item:9", "family": "item", "label": "[Object] 定規",
                "lines": ["[Object] 定規"], "media": [], "state": None,
            }],
        }
        if capped is not None:
            bundle[BUNDLE_CAPPED_KEYS] = capped
        return bundle

    def test_legacy_bundle_without_the_field_is_valid_and_reads_as_empty(self):
        bundle = self._bundle()
        self.assertTrue(bundle_is_valid(bundle))
        self.assertEqual(bundle_capped_keys(bundle), [])
        self.assertIsNone(capped_notice_line(bundle))

    def test_legacy_bundle_does_not_suppress_real_gone_reports(self):
        """capped_keys が無い旧い束を土台にしても、消えた報告はそのまま出る。"""
        old = self._bundle()
        new = {"building_id": "b1", "building_name": "工房", "packages": []}
        content = render_room_diff(old, new)["content"]
        self.assertIn("## 見当たらなくなったもの", content)
        self.assertIn("定規", content)

    def test_capped_keys_are_in_the_fingerprint_material(self):
        with_capped = canonical_bundle_json(self._bundle(["item:1"]))
        without = canonical_bundle_json(self._bundle())
        self.assertIn("capped_keys", with_capped)
        self.assertNotEqual(with_capped, without)

    def test_broken_capped_keys_make_the_bundle_invalid(self):
        for bad in ("item:1", [1, 2], [""], [None]):
            with self.subTest(bad=bad):
                self.assertFalse(bundle_is_valid(self._bundle(bad)))

    def test_duplicate_capped_keys_make_the_bundle_invalid(self):
        """同じ物を二度「外した」と記帳した束は組成の欠陥の印なので通さない。"""
        self.assertFalse(bundle_is_valid(self._bundle(["item:1", "item:1"])))

    def test_non_item_keys_in_the_capped_list_make_the_bundle_invalid(self):
        """⭐ 「埋もれた」と言えるのは建物直下のアイテムだけ (item: の名前空間)。

        上限が絞るのはアイテムだけなので、ペルソナや設置物のキーが一覧に載った
        束は組成の欠陥の印。通すと差分の「消えたと言わない」照合がその族にも
        働き、退室したペルソナの「見当たらなくなったもの」まで黙って落ちる。
        """
        for bad in (["persona:air"], ["fixture:f1"], ["user:u1"],
                    ["building:image"], ["item:1", "persona:air"]):
            with self.subTest(bad=bad):
                self.assertFalse(bundle_is_valid(self._bundle(bad)))

    def test_capped_key_that_is_also_displayed_makes_the_bundle_invalid(self):
        """表で見えている物が「埋もれている」一覧にも立つ束は通さない。

        通すと、その物が本当に部屋から消えた回にも差分の gone 抑止が働き、
        「見当たらなくなったもの」の報告が黙って落ちる (実削除が隠れる)。
        """
        # _bundle が積むパッケージのキーは item:9。
        self.assertFalse(bundle_is_valid(self._bundle(["item:9"])))
        self.assertFalse(bundle_is_valid(self._bundle(["item:1", "item:9"])))


class BuildingSettingPlumbingTest(unittest.TestCase):
    """設計 4: 設定が API の全ホップを通り、送ってこない画面で消えないこと。"""

    def setUp(self):
        from database.models import Base, Building as BuildingModel
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        db = self.SessionLocal()
        db.add(BuildingModel(
            CITYID=1, BUILDINGID="b1", BUILDINGNAME="工房", CAPACITY=4,
            SYSTEM_INSTRUCTION="", ENTRY_PROMPT="", AUTO_PROMPT="",
            DESCRIPTION="", AUTO_INTERVAL_SEC=10,
        ))
        db.commit()
        db.close()

    def tearDown(self):
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _admin(self):
        from manager.admin import AdminService

        service = SimpleNamespace(SessionLocal=self.SessionLocal)
        return lambda **kw: AdminService.update_building(
            service, "b1", "工房", 4, "", "", 1, [], 10, **kw,
        )

    def _stored(self):
        from database.models import Building as BuildingModel

        db = self.SessionLocal()
        try:
            return db.query(BuildingModel).filter_by(
                BUILDINGID="b1",
            ).first().ITEM_DISPLAY_LIMIT
        finally:
            db.close()

    def test_admin_writes_zero_and_clears_with_none(self):
        update = self._admin()
        self.assertFalse(update(item_display_limit=0).startswith("Error"))
        self.assertEqual(self._stored(), 0)
        self.assertFalse(update(item_display_limit=None).startswith("Error"))
        self.assertIsNone(self._stored())

    def test_admin_rejects_negative(self):
        update = self._admin()
        update(item_display_limit=5)
        self.assertTrue(update(item_display_limit=-1).startswith("Error"))
        self.assertEqual(self._stored(), 5)  # 拒否された保存は値を壊さない

    def test_admin_leaves_the_value_alone_when_not_sent(self):
        """このフィールドを送らない画面 (Building 設定モーダル) の保存で消えない。"""
        update = self._admin()
        update(item_display_limit=3)
        self.assertFalse(update().startswith("Error"))
        self.assertEqual(self._stored(), 3)

    def test_manager_hop_passes_the_value_through(self):
        """SAIVerseManager → AdminService の委譲で値が落ちない (過去の実害の型)。"""
        from saiverse.saiverse_manager import SAIVerseManager
        from manager.admin import UNSET

        seen = {}

        def _fake_update(*args):
            seen["args"] = args
            return "Building '工房' updated successfully."

        building = SimpleNamespace(
            name="", capacity=0, description="", base_system_instruction="",
            system_instruction="", auto_interval_sec=0, extra_prompt_files=[],
            item_display_limit=None,
        )
        fake_self = SimpleNamespace(
            admin=SimpleNamespace(update_building=_fake_update),
            building_map={"b1": building},
        )
        SAIVerseManager.update_building(
            fake_self, "b1", "工房", 4, "", "", 1, [], 10, None, None, 7,
        )
        self.assertEqual(seen["args"][-1], 7)
        self.assertEqual(building.item_display_limit, 7)

        SAIVerseManager.update_building(
            fake_self, "b1", "工房", 4, "", "", 1, [], 10,
        )
        self.assertIs(seen["args"][-1], UNSET)
        self.assertEqual(building.item_display_limit, 7)  # 触られていない

    def test_route_rejects_negative_and_distinguishes_absent_from_null(self):
        from fastapi import HTTPException
        from api.routes import world as world_routes
        from manager.admin import UNSET

        seen = {}

        def _fake_update(*args):
            seen["args"] = args
            return "ok"

        manager = SimpleNamespace(update_building=_fake_update)

        def _payload(**extra):
            return world_routes.BuildingUpdate(
                name="工房", description="", capacity=4, system_instruction="",
                city_id=1, tool_ids=[], auto_interval=10, **extra,
            )

        world_routes.update_building("b1", _payload(), manager)
        self.assertIs(seen["args"][-1], UNSET)

        world_routes.update_building("b1", _payload(item_display_limit=None), manager)
        self.assertIsNone(seen["args"][-1])

        world_routes.update_building("b1", _payload(item_display_limit=0), manager)
        self.assertEqual(seen["args"][-1], 0)

        with self.assertRaises(HTTPException) as ctx:
            world_routes.update_building(
                "b1", _payload(item_display_limit=-1), manager,
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def _load_buildings(self):
        """起動時と同じ読み込み処理で、DB から in-memory Building を作る。"""
        from saiverse.saiverse_manager import SAIVerseManager

        fake_self = SimpleNamespace(SessionLocal=self.SessionLocal, city_id=1)
        loaded = SAIVerseManager._load_and_create_buildings_from_db(fake_self)
        return {b.building_id: b for b in loaded}

    def _set_limit_in_db(self, building_id, value):
        from database.models import Building as BuildingModel

        db = self.SessionLocal()
        try:
            row = db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
            row.ITEM_DISPLAY_LIMIT = value
            db.commit()
        finally:
            db.close()

    def test_building_loaded_from_db_carries_the_limit(self):
        """⭐ DB の列が、**起動時の読み込み処理を通って**組成の読む値まで届く。

        コンストラクタに直に渡して確かめても、読み込み処理がその列を読み忘れて
        いる形 (部屋の設定が保存できるのに一つも効かない) は素通りする。0 は
        真偽値で潰れやすい値なので、0 で確かめる。
        """
        from database.models import Building as BuildingModel

        db = self.SessionLocal()
        try:
            db.add(BuildingModel(
                CITYID=1, BUILDINGID="b2", BUILDINGNAME="蔵", CAPACITY=4,
                SYSTEM_INSTRUCTION="", ENTRY_PROMPT="", AUTO_PROMPT="",
                DESCRIPTION="", AUTO_INTERVAL_SEC=10,
            ))
            db.commit()
        finally:
            db.close()
        self._set_limit_in_db("b1", 0)

        buildings = self._load_buildings()
        self.assertEqual(buildings["b1"].item_display_limit, 0)
        self.assertIsNone(buildings["b2"].item_display_limit)  # 未設定は既定のまま

    def test_limit_loaded_from_db_actually_caps_the_room(self):
        """読み込んだ Building をそのまま組成に通すと、部屋の様子が縮む。

        値が in-memory に載るだけで組成に届いていない形を捕まえるため、読み
        込んだ本物の Building オブジェクトで束を組む。
        """
        self._set_limit_in_db("b1", 0)
        building = self._load_buildings()["b1"]

        items = [_make_item(n, f"物{n}", touched_min=n) for n in (1, 2, 3)]
        bundle = _build_bundle(items, building=building)
        self.assertEqual(
            [p["key"] for p in bundle["packages"] if p.get("family") == "item"],
            [],
        )
        self.assertEqual(
            bundle_capped_keys(bundle), ["item:1", "item:2", "item:3"],
        )


class MigrationTest(unittest.TestCase):
    """設計 4: DB の列追加は additive — 既存 DB に列が足されるだけ。"""

    def test_additive_migration_adds_the_column(self):
        from sqlalchemy import create_engine
        from database.models import Base
        from database.migrate import needs_migration, try_additive_migration

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            engine.dispose()
            # 列が無かった頃の DB を作り直す
            conn = sqlite3.connect(db_path)
            conn.execute('ALTER TABLE building DROP COLUMN "ITEM_DISPLAY_LIMIT"')
            conn.commit()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(building)")}
            conn.close()
            self.assertNotIn("ITEM_DISPLAY_LIMIT", cols)
            self.assertTrue(needs_migration(db_path))

            self.assertTrue(try_additive_migration(db_path))

            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(building)")}
            conn.close()
            self.assertIn("ITEM_DISPLAY_LIMIT", cols)
            self.assertFalse(needs_migration(db_path))
        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
