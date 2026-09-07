"""部屋の様子のパッケージ (room state packages) の契約テスト。

設計の正典は docs/intent/room_state_packages.md (2026-09-06)。発端は
docs/issues/archive/room_state_diff_built_on_string_parsing.md — 差分を描画済み文字列の
解析で組んでいたため、開いたドキュメントの本文段落が「見当たらなくなったもの」
に化けて v0.3.9 の出荷を止めた。合成の一行アイテムだけを食べたテストがこの欠陥を
6 巡のレビューごと素通ししたので、**本物の描画 (build_room_bundle) をテストに
食わせる** (intent §10) — 開いた複数段落のドキュメント / メディア付きの開いた
画像 / 入れ子の Bag / 設置物 / システムプロンプト付きの建物。

契約 (intent の写し):

- §3: 部屋はパッケージの束のまま運ぶ。同じ部屋は何度読んでも同じ束 (決定論)。
- §4: 差分はキー照合 — 新登場は全文 + メディア / Close→Open は「(開かれた)」+
  開いて初めて見える行 + メディア (説明・作成日時は再掲しない) / 消えたは
  label の一行 / Open→Close は「(閉じられた)」の一行 / open のままの本文変化は
  行単位 diff / 変化なしは一行。
- §5: メディアはパッケージの持ち物 — 新登場に絵が付く / 復元で絵が戻る /
  不変時に再添付しない。
- §6: 供給の四点 — 入室 (末尾) / 滞在中の照合 + 自己回復 (先頭) /
  ブートストラップ (先頭) / 最後の運搬役が下りる回の置き直し (先頭、付記・境界
  前進と同一トランザクション)。
- §9: 旧形式 (文字列 snapshot) は土台なし扱い — 連なりに参加しない。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builtin_data.tools import get_visual_context as gvc
from sai_memory.perception_buffer import (
    advance_presentation_cutoff,
    create_consumption_batch,
    format_perception_message,
    get_presentation_cutoff,
    init_perception_buffer_table,
    list_pending,
    list_presented_batches,
    list_unannexed_batches,
    mark_batches_annexed,
    push_perception,
    reduce_perceptions,
)
from sai_memory.perception_buffer import PerceptionItem
from sai_memory.room_state import (
    ROOM_STATE_KIND,
    build_room_state_push,
    bundle_is_valid,
    bundle_media,
    canonical_bundle_json,
    chain_is_intact,
    collect_batch_room_states,
    find_current_room_key,
    is_legacy_entry,
    latest_visible_snapshot,
    pending_has_room,
    reclaim_pending_perceptions,
    render_pending_room_states,
    render_room_diff,
    render_room_full,
    reseat_current_room,
    restore_room_state_bases,
    room_key,
    snapshot_digest,
)

#: 開いたドキュメントの本文 — 空行 (段落の切れ目) と "## " 見出しを含む。
#: 旧実装 (_split_blocks) は「空行 = アイテムの境目」と推測したため、この本文の
#: 段落が独立した「アイテム」に化けた (v0.3.9 出荷停止の直接原因)。
_DOC_BODY = (
    "このドキュメントは、様々な名画の要素を調和させるためのアイデア・覚え書きです。\n"
    "\n"
    "## 1. 巨匠たちから吸収するエッセンス\n"
    "光の扱いはフェルメールに学ぶ。\n"
    "\n"
    "## 2. 構図\n"
    "対角線を意識する。\n"
)

_DOC_BODY_EDITED = _DOC_BODY + "\n## 3. 色彩\n補色を一組だけ使う。\n"


def _make_item(
    item_id, short_id, item_type, name, description, *,
    is_open=None, file_path=None,
):
    item = {
        "item_id": item_id,
        "short_id": short_id,
        "type": item_type,
        "name": name,
        "description": description,
        "created_at": 1_700_000_000.0,
    }
    if is_open is not None:
        item["state"] = {"is_open": is_open}
    if file_path is not None:
        item["file_path"] = str(file_path)
    return item


class RealWorldEnv:
    """本物の描画 (build_room_bundle) に食わせる世界 (intent §10 の最低限の fixture)。

    - 開いたドキュメント (複数段落・空行・"## " 見出し入りの本文、``` 囲い)
    - メディア付きの開いた画像 (実ファイル)
    - 入れ子の Bag
    - 設置物 (Fixture)
    - システムプロンプト付きの建物 (空行を含む複数行)
    - 他ペルソナ 1 人 + ユーザー 1 人
    """

    def __init__(self, home: Path):
        self.home = home
        (home / "documents").mkdir(parents=True, exist_ok=True)
        (home / "image").mkdir(parents=True, exist_ok=True)
        self.doc_path = home / "documents" / "notes.md"
        self.doc_path.write_text(_DOC_BODY, encoding="utf-8")
        self.pic_path = home / "image" / "pic.png"
        self.pic_path.write_bytes(b"\x89PNG fake")
        self.pic2_path = home / "image" / "pic2.png"
        self.pic2_path.write_bytes(b"\x89PNG fake2")

        self.items = [
            _make_item(
                "uuid-doc", 10, "document", "覚え書き", "創作のアイデア帳。",
                is_open=True, file_path=self.doc_path,
            ),
            _make_item(
                "uuid-pic", 11, "picture", "セピア色の写真", "古い写真。",
                is_open=True, file_path=self.pic_path,
            ),
            _make_item(
                "uuid-bag", 12, "bag", "道具袋", "工具の入った袋。",
                is_open=True,
            ),
            _make_item(
                "uuid-closed", 13, "document", "閉じた手帳", "非公開のメモ。",
                is_open=False, file_path=self.doc_path,
            ),
        ]
        self.bag_contents = [
            {
                "name": "真鍮の定規", "type": "object", "short_id": 21,
                "description": "使い込まれた定規。", "_children": [],
            },
            {
                "name": "小箱", "type": "bag", "short_id": 22,
                "description": "さらに小さな箱。",
                "_children": [{
                    "name": "鍵", "type": "object", "short_id": 23,
                    "description": "何の鍵かは分からない。", "_children": [],
                }],
            },
        ]
        self.fixture = SimpleNamespace(
            NAME="観測儀", TYPE="observer", FIXTURE_ID="fx-1",
            DESCRIPTION="室温を測る装置。",
            STATE_JSON=json.dumps({"temperature": {"value_num": 21.5}}),
        )
        self.other_persona = SimpleNamespace(persona_name="エリス")
        self.persona = SimpleNamespace(
            persona_name="アイフィ",
            current_building_id="b1",
            buildings={
                "b1": SimpleNamespace(
                    name="工房",
                    base_system_instruction=(
                        "ここは創作の工房。\n\n静かに集中できる場所です。"
                    ),
                ),
            },
        )
        self.manager = SimpleNamespace(
            saiverse_home=home,
            occupants={"b1": ["p1", "p2", "42"]},
            all_personas={"p1": self.persona, "p2": self.other_persona},
            get_all_items_in_building=lambda bid: list(self.items),
            get_bag_contents_recursive=lambda item_id: (
                list(self.bag_contents) if item_id == "uuid-bag" else []
            ),
            observer_manager=SimpleNamespace(
                get_building_fixtures=lambda bid: [self.fixture],
            ),
        )

    def bundle(self):
        """本物の組成 (build_room_bundle) で束を組む。"""
        with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
                patch.object(gvc, "get_active_manager", return_value=self.manager), \
                patch.object(gvc, "_get_persona_appearance_path", return_value=None), \
                patch.object(gvc, "_get_building_image_path", return_value=None):
            built = gvc.build_room_bundle("b1")
        assert built is not None
        return built


class _EnvTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = RealWorldEnv(Path(tmp.name))


class RoomBundleCompositionTest(_EnvTestBase):
    """本物の描画がパッケージの束 (intent §3) を組めていること。"""

    def test_the_six_families_have_their_keys(self):
        bundle = self.env.bundle()
        by_key = {p["key"]: p for p in bundle["packages"]}
        self.assertIn("persona:p2", by_key)        # 他ペルソナ = persona:<ID>
        self.assertIn("user:42", by_key)           # ユーザー = user:<ID>
        self.assertIn("building:prompt", by_key)   # システムプロンプト
        self.assertIn("item:10", by_key)           # アイテム = item:N
        self.assertIn("fixture:fx-1", by_key)      # 設置物 = fixture:<ID>
        self.assertEqual(by_key["persona:p2"]["family"], "persona")
        self.assertEqual(by_key["item:10"]["state"], "open")
        self.assertEqual(by_key["item:13"]["state"], "closed")
        self.assertIsNone(by_key["fixture:fx-1"]["state"])

    def test_the_open_document_body_is_inside_its_package(self):
        bundle = self.env.bundle()
        doc = next(p for p in bundle["packages"] if p["key"] == "item:10")
        text = "\n".join(doc["lines"])
        self.assertIn("## 1. 巨匠たちから吸収するエッセンス", text)
        self.assertIn("光の扱いはフェルメールに学ぶ。", text)
        # 空行も構造 (lines) の中にそのまま居る — 文字列解析に戻らない土台。
        self.assertIn("", doc["lines"])

    def test_the_open_picture_carries_its_media(self):
        bundle = self.env.bundle()
        pic = next(p for p in bundle["packages"] if p["key"] == "item:11")
        self.assertEqual(len(pic["media"]), 1)
        self.assertEqual(pic["media"][0]["path"], str(self.env.pic_path))
        self.assertIn(pic["media"][0], bundle_media(bundle))

    def test_the_nested_bag_lists_its_children(self):
        bundle = self.env.bundle()
        bag = next(p for p in bundle["packages"] if p["key"] == "item:12")
        text = "\n".join(bag["lines"])
        self.assertIn("[item:21] [Object] 真鍮の定規", text)
        self.assertIn("[item:23] [Object] 鍵", text)

    def test_the_same_room_composes_the_same_bundle(self):
        """決定論 (intent §3) — 指紋の照合と差分の前提。"""
        first = self.env.bundle()
        second = self.env.bundle()
        self.assertEqual(
            canonical_bundle_json(first), canonical_bundle_json(second),
        )
        self.assertEqual(snapshot_digest(first), snapshot_digest(second))

    def test_the_full_text_is_derived_from_the_bundle(self):
        bundle = self.env.bundle()
        text = render_room_full(bundle)
        self.assertIn("# 「工房」の様子", text)
        # 同席者は名乗りの一行 + 外見 (「がいます」廃止後の唯一の運び手、2026-09-07)
        self.assertIn("- エリス (ID:p2)", text)
        self.assertIn("[エリスの外見]", text)
        self.assertIn("[システムプロンプト]", text)
        self.assertIn("静かに集中できる場所です。", text)
        self.assertIn("## 1. 巨匠たちから吸収するエッセンス", text)
        self.assertIn("観測儀", text)
        self.assertIn("最新観測値: temperature=21.5", text)

    def test_the_tool_view_still_works_without_the_recall(self):
        """get_visual_context はツールとして残る。思い出は退役 (呼び出し側ごと削除)。"""
        self.assertFalse(hasattr(gvc, "_fetch_item_memory_recall"))
        with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
                patch.object(gvc, "get_active_manager", return_value=self.env.manager), \
                patch.object(gvc, "_get_persona_appearance_path", return_value=None), \
                patch.object(gvc, "_get_building_image_path", return_value=None):
            msgs = gvc.get_visual_context(building_id="b1")
        self.assertTrue(msgs)
        self.assertIn("<system>", msgs[0]["content"])
        self.assertNotIn("あの時の思い出", msgs[0]["content"])


class RenderRoomDiffContractTest(_EnvTestBase):
    """§4 の表の行ごとの契約 — 本物の描画同士のキー照合。"""

    def setUp(self):
        super().setUp()
        self.before = self.env.bundle()

    def test_a_new_open_picture_appears_in_full_with_its_media(self):
        """新登場 = 全文 + そのパッケージのメディア (§4 行 1 + §5)。"""
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        diff = render_room_diff(self.before, self.env.bundle())
        self.assertIn("## 増えた・変わったもの", diff["content"])
        self.assertIn("[item:14] [Image] 新しい絵", diff["content"])
        self.assertIn("届いたばかりの絵。", diff["content"])
        # メディアは新登場のパッケージのものだけ (不変の写真は再添付しない)。
        self.assertEqual(
            [m["path"] for m in diff["media"]], [str(self.env.pic2_path)],
        )

    def test_close_to_open_is_an_event_with_only_the_newly_visible_lines(self):
        """Close→Open = 「(開かれた)」+ 開いて初めて見える行 (2026-09-06 実機裁定)。

        「(Open)」は現在の状態でしかなく、新登場のアイテムと見分けが付かない —
        閉じる側の「(閉じられた)」と対の出来事として語る。説明・作成日時は
        閉じている間も全文ビューに出続けていた (§3 の表) ので再掲しない
        (保証 2「同じ内容が二枚並ぶことは構造的に無い」)。開いて初めて見える
        もの (メディアリンクと絵の実体) だけを出す。
        """
        self.env.items.append(_make_item(
            "uuid-shut-pic", 15, "picture", "しまわれた絵", "港の写生。",
            is_open=False, file_path=self.env.pic2_path,
        ))
        before = self.env.bundle()
        self.env.items[-1] = _make_item(
            "uuid-shut-pic", 15, "picture", "しまわれた絵", "港の写生。",
            is_open=True, file_path=self.env.pic2_path,
        )
        diff = render_room_diff(before, self.env.bundle())
        self.assertIn(
            "[item:15] [Image] しまわれた絵\n(開かれた)\n"
            "saiverse://item/15/image",
            diff["content"],
        )
        self.assertNotIn("港の写生。", diff["content"])   # 説明は再掲しない
        self.assertNotIn("作成日時", diff["content"])     # 作成日時も再掲しない
        self.assertNotIn("(Open)", diff["content"])       # 状態ではなく出来事
        # 開いて初めて見える絵の実体は添付される (§5 の「新しく見せる瞬間」)。
        self.assertEqual(
            [m["path"] for m in diff["media"]], [str(self.env.pic2_path)],
        )

    def test_close_to_open_document_reveals_the_body_not_the_description(self):
        """Document も同じ規則 — 開いて初めて見える本文は出し、説明は出さない。"""
        self.env.items[3] = _make_item(
            "uuid-closed", 13, "document", "閉じた手帳", "非公開のメモ。",
            is_open=True, file_path=self.env.doc_path,
        )
        diff = render_room_diff(self.before, self.env.bundle())
        self.assertIn(
            "[item:13] [Document] 閉じた手帳\n(開かれた)", diff["content"],
        )
        # 本文は開いたことで新しく見えるようになったもの — これは出す。
        self.assertIn("光の扱いはフェルメールに学ぶ。", diff["content"])
        # 説明は閉じている間も提示に出ていた — 再掲は保証 2 への自己矛盾。
        self.assertNotIn("非公開のメモ。", diff["content"])

    def test_a_gone_document_is_one_label_line_without_its_body(self):
        """消えた = label の一行だけ (§4 行 2) — v0.3.9 を止めた欠陥の再発防止。

        旧実装は開いたドキュメントの本文段落 (空行区切り) を独立アイテムと誤認
        し、「見当たらなくなったもの」に本文を晒した。
        """
        del self.env.items[0]  # 開いたドキュメントが部屋から消える
        diff = render_room_diff(self.before, self.env.bundle())
        self.assertIn("## 見当たらなくなったもの", diff["content"])
        self.assertIn("- [item:10] [Document] 覚え書き", diff["content"])
        # 本文・説明はもう積まない (絶対に重複にしかならない情報)。
        self.assertNotIn("光の扱いはフェルメールに学ぶ。", diff["content"])
        self.assertNotIn("## 1. 巨匠たちから吸収するエッセンス", diff["content"])
        self.assertNotIn("創作のアイデア帳。", diff["content"])

    def test_open_to_close_is_one_line_without_the_body(self):
        """Open→Close = ユーザーの意思 = 「(閉じられた)」の一行 (§4 行 3)。"""
        self.env.items[0] = _make_item(
            "uuid-doc", 10, "document", "覚え書き", "創作のアイデア帳。",
            is_open=False, file_path=self.env.doc_path,
        )
        diff = render_room_diff(self.before, self.env.bundle())
        self.assertIn("[item:10] [Document] 覚え書き (閉じられた)", diff["content"])
        self.assertNotIn("光の扱いはフェルメールに学ぶ。", diff["content"])
        self.assertNotIn("(Closed)", diff["content"])

    def test_an_edited_open_body_shows_only_the_changed_lines(self):
        """open のままの本文変化 = 行単位の diff (§4 行 4)。"""
        self.env.doc_path.write_text(_DOC_BODY_EDITED, encoding="utf-8")
        diff = render_room_diff(self.before, self.env.bundle())
        self.assertIn("(変わった行だけ)", diff["content"])
        self.assertIn("+ ## 3. 色彩", diff["content"])
        self.assertIn("+ 補色を一組だけ使う。", diff["content"])
        # 変わっていない段落は再掲しない。
        self.assertNotIn("光の扱いはフェルメールに学ぶ。", diff["content"])
        self.assertEqual(diff["media"], [])

    def test_a_rewritten_body_larger_than_the_full_text_falls_back_to_full(self):
        """diff が全文より大きければ全文 (§4 行 4 の但し書き)。"""
        self.env.doc_path.write_text(
            "全部書き直した。\n新しい第一段落。\n\n新しい第二段落。\n",
            encoding="utf-8",
        )
        diff = render_room_diff(self.before, self.env.bundle())
        after_doc = next(
            p for p in self.env.bundle()["packages"] if p["key"] == "item:10"
        )
        self.assertIn("\n".join(after_doc["lines"]), diff["content"])

    def test_no_change_condenses_to_one_line(self):
        """部屋全体で変化なし = 一行 (§4 行 5)。"""
        diff = render_room_diff(self.before, self.env.bundle())
        self.assertEqual(
            diff["content"],
            "# 「工房」の様子\n前回見たときから変わっていません。",
        )
        self.assertEqual(diff["media"], [])

    def test_a_left_persona_is_reported_by_label(self):
        self.env.manager.occupants["b1"] = ["p1", "42"]
        diff = render_room_diff(self.before, self.env.bundle())
        # label は名乗りの形 — 退場の報告が「- [エリスの外見]」にならない
        self.assertIn("- エリス (ID:p2)", diff["content"])


class CrossFamilyKeyTest(_EnvTestBase):
    """2026-09-06 四巡目修正 3: キーは族の接頭辞つき — 生 ID の族またぎ衝突を塞ぐ。

    ペルソナ ID・ユーザー ID・設置物 ID は独立の名前空間。キーが生の文字列の
    ままだと、同じ文字列を持つ別族が差分の辞書 (render_room_diff の key 照合)
    で片方を上書きし、退出・消滅の報告が黙って消える。
    """

    def test_a_gone_fixture_sharing_a_persona_id_is_still_reported(self):
        self.env.fixture.FIXTURE_ID = "p2"  # 他ペルソナ p2 と同じ生 ID
        before = self.env.bundle()
        self.env.manager.observer_manager = SimpleNamespace(
            get_building_fixtures=lambda bid: [],
        )
        diff = render_room_diff(before, self.env.bundle())
        self.assertIn("## 見当たらなくなったもの", diff["content"])
        self.assertIn("観測儀 (ID: p2)", diff["content"])
        # 残っているペルソナが、キー衝突で「変わったもの」に化けない。
        self.assertNotIn("(変わった行だけ)", diff["content"])

    def test_a_duplicate_key_in_the_bundle_warns_and_keeps_the_first(self):
        """同一キーの重複は組成時に検出して WARN (先勝ち — 黙って上書きしない)。"""
        twin = SimpleNamespace(
            NAME="観測儀の複製", TYPE="observer", FIXTURE_ID="fx-1",
            DESCRIPTION="同じ ID の二枚目。", STATE_JSON=None,
        )
        self.env.manager.observer_manager = SimpleNamespace(
            get_building_fixtures=lambda bid: [self.env.fixture, twin],
        )
        with self.assertLogs(
            "builtin_data.tools.get_visual_context", level="WARNING",
        ):
            bundle = self.env.bundle()
        keys = [p["key"] for p in bundle["packages"]]
        self.assertEqual(keys.count("fixture:fx-1"), 1)
        kept = next(p for p in bundle["packages"] if p["key"] == "fixture:fx-1")
        self.assertNotIn("複製", kept["label"])


class RoomStateLedgerTestBase(_EnvTestBase):
    """生の conn で「積む → 消費バッチ確定」を回す土台 (adapter の flush と同形)。"""

    def setUp(self):
        super().setUp()
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)
        self.clock = 1000

    def _push(self, building_id, bundle, *, allow_diff=True):
        payload = build_room_state_push(
            building_id, bundle, allow_diff=allow_diff,
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
        reduced = render_pending_room_states(
            self.conn, reclaim_pending_perceptions(reduce_perceptions(items)),
        )
        text = format_perception_message(reduced)
        media = []
        seen = set()
        for it in reduced:
            for m in it.media_list():
                if m.get("path") in seen:
                    continue
                seen.add(m.get("path"))
                media.append(m)
        self.clock += 10
        return create_consumption_batch(
            self.conn, [it.id for it in items],
            consumed_at=self.clock, rendered_text=text,
            media=media or None,
            room_state_json=collect_batch_room_states(reduced, text),
        )

    def _batch(self, batch_id):
        for b in list_unannexed_batches(self.conn):
            if b.id == batch_id:
                return b
        return None

    def _write_old_generation_batch(self, text, snapshot, *, key=None,
                                    is_diff=False):
        """旧世代の flush が確定させた形のバッチを直接構築する。

        §11-2 の回収以降、新しい消費は旧形式・不正束を文面に載せない — この形は
        既存 DB の遺物 (回収前の世代が書いた記帳や記帳破損) としてだけ現れる。
        §9 の停止契約 (連なりに参加しない・修復されない・材料にならない) は
        その遺物に対して守られ続けるので、テストは遺物を直接書いて検査する。
        """
        room = key or room_key("b1")
        item_id = push_perception(
            self.conn, ROOM_STATE_KIND, text,
            metadata=json.dumps(
                {"room_state": {
                    "key": room, "is_diff": is_diff, "snapshot": snapshot,
                }},
                ensure_ascii=False,
            ),
        )
        self.clock += 10
        return create_consumption_batch(
            self.conn, [item_id], consumed_at=self.clock, rendered_text=text,
            room_state_json=json.dumps([{
                "key": room, "is_diff": is_diff,
                "block": text, "snapshot": snapshot,
            }], ensure_ascii=False),
        )


class RoomStatePushTest(RoomStateLedgerTestBase):
    """積む側は束の記帳だけ — 描画の判定は積む段階では確定しない (§11-2 規則 2)。"""

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()

    def test_the_push_records_the_bundle_without_a_rendering_decision(self):
        payload = self._push("b1", self.bundle_a)
        state = json.loads(payload["metadata"])["room_state"]
        self.assertEqual(state["key"], room_key("b1"))
        self.assertEqual(state["snapshot"], self.bundle_a)
        self.assertIs(state["allow_diff"], True)
        # is_diff / base_digest は積む段階では確定しない (描画は消費時)。
        self.assertNotIn("is_diff", state)
        self.assertNotIn("base_digest", state)

    def test_the_pushed_content_is_the_full_text_for_inspection(self):
        """content 列は劣化時・生の点検用の全文 — 消費の組成はこれを使わない。"""
        payload = self._push("b1", self.bundle_a)
        self.assertEqual(payload["content"], render_room_full(self.bundle_a))
        self.assertEqual(payload["media"], bundle_media(self.bundle_a))

    def test_the_allow_diff_flag_is_frozen_into_the_record(self):
        payload = self._push("b1", self.bundle_a, allow_diff=False)
        state = json.loads(payload["metadata"])["room_state"]
        self.assertIs(state["allow_diff"], False)


class RoomStateComposeTest(RoomStateLedgerTestBase):
    """消費時描画の判定 (全文か差分か) とメディアの契約 (§4 / §5)。

    土台は「提示に見えている同部屋の末尾の束」— 末尾なし / allow_diff=False /
    末尾が旧形式は全文、それ以外は差分 (render_room_diff の既存規則)。
    """

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        self.bundle_b = self.env.bundle()

    def _room_entry(self, batch_id):
        return json.loads(self._batch(batch_id).room_state_json)[0]

    def test_first_visit_composes_the_full_text_with_all_media(self):
        self._push("b1", self.bundle_a)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertEqual(batch.rendered_text, render_room_full(self.bundle_a))
        self.assertEqual(batch.media_list(), bundle_media(self.bundle_a))
        entry = self._room_entry(batch_id)
        self.assertFalse(entry["is_diff"])
        self.assertEqual(entry["key"], room_key("b1"))
        self.assertEqual(entry["snapshot"], self.bundle_a)

    def test_a_revisit_composes_a_diff_against_the_presented_tail(self):
        self._push("b1", self.bundle_a)
        self._flush()
        self._push("b1", self.bundle_b)
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertIn("新しい絵", text)
        self.assertLess(len(text), len(render_room_full(self.bundle_b)))
        entry = self._room_entry(batch_id)
        self.assertTrue(entry["is_diff"])
        self.assertEqual(entry["base_digest"], snapshot_digest(self.bundle_a))

    def test_a_new_picture_in_the_diff_carries_its_media(self):
        """§5 契約 1: 差分で新しく見せるパッケージの絵は差分と一緒に届く。"""
        self._push("b1", self.bundle_a)
        self._flush()
        self._push("b1", self.bundle_b)
        batch_id = self._flush()
        self.assertEqual(
            [m["path"] for m in self._batch(batch_id).media_list()],
            [str(self.env.pic2_path)],
        )

    def test_an_unchanged_room_diff_carries_no_media(self):
        """§5 契約 3: 変わっていないパッケージのメディアは再添付しない。"""
        self._push("b1", self.bundle_a)
        self._flush()
        self._push("b1", self.bundle_a)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertIn("前回見たときから変わっていません。", batch.rendered_text)
        self.assertEqual(batch.media_list(), [])
        self.assertTrue(self._room_entry(batch_id)["is_diff"])

    def test_another_room_is_a_separate_base(self):
        self._push("b1", self.bundle_a)
        self._flush()
        other = dict(self.bundle_a)
        other["building_id"] = "b2"
        other["building_name"] = "書斎"
        self._push("b2", other)
        batch_id = self._flush()
        self.assertEqual(
            self._batch(batch_id).rendered_text, render_room_full(other),
        )

    def test_after_the_base_is_annexed_the_next_visit_is_a_diff_again(self):
        self._push("b1", self.bundle_a)
        batch_id = self._flush()
        mark_batches_annexed(self.conn, [batch_id], "entry-1")
        self.conn.commit()
        # 付記の tx が最後の運搬役の置き直し (§6-4) を伴うので、置き直しの
        # 全文が新しい土台になる — 変化ぶんだけの差分が組める。
        reseated = latest_visible_snapshot(self.conn, room_key("b1"))
        self.assertEqual(reseated, self.bundle_a)
        self._push("b1", self.bundle_b)
        next_id = self._flush()
        entry = self._room_entry(next_id)
        self.assertTrue(entry["is_diff"])
        self.assertEqual(entry["base_digest"], snapshot_digest(self.bundle_a))
        self.assertIn("新しい絵", self._batch(next_id).rendered_text)

    def test_chronicle_disabled_persona_always_gets_the_full_text(self):
        self._push("b1", self.bundle_a, allow_diff=False)
        self._flush()
        self._push("b1", self.bundle_b, allow_diff=False)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertEqual(batch.rendered_text, render_room_full(self.bundle_b))
        self.assertEqual(batch.media_list(), bundle_media(self.bundle_b))
        self.assertFalse(self._room_entry(batch_id)["is_diff"])

    def test_a_legacy_tail_composes_the_full_text(self):
        """末尾が旧形式なら全文 — さらに古い有効束へは遡らない (§9 の止まり方)。"""
        self._push("b1", self.bundle_a)
        self._flush()  # 古い有効束 (遡ってはいけない土台)
        self._write_old_generation_batch("旧世代の全文。", "旧世代の全文。")
        self._push("b1", self.bundle_b)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertEqual(batch.rendered_text, render_room_full(self.bundle_b))
        self.assertFalse(self._room_entry(batch_id)["is_diff"])

    def test_an_old_generation_pending_row_composes_the_full_text(self):
        """allow_diff の旗が無い旧世代 (積む時に描画) の pending 行は全文に倒す。

        積む時に描いた文面 (差分かもしれない) は使わず、束から全文を組み直す —
        余分な全文は無害、間違った土台の差分は復元不能 (実装メモの三原則 3)。
        """
        self._push("b1", self.bundle_a)
        self._flush()
        push_perception(
            self.conn, ROOM_STATE_KIND, "旧世代が積む時に描いた差分の文面",
            metadata=json.dumps({"room_state": {
                "key": room_key("b1"), "is_diff": True,
                "snapshot": self.bundle_b,
                "base_digest": snapshot_digest(self.bundle_a),
            }}, ensure_ascii=False),
        )
        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertEqual(batch.rendered_text, render_room_full(self.bundle_b))
        self.assertFalse(self._room_entry(batch_id)["is_diff"])

    def test_a_close_to_open_diff_still_records_the_full_snapshot(self):
        """描画が出来事 + open のみの行になっても、記帳は全文の束のまま。

        連なり (chain_is_intact) と移管・回復 (restore_room_state_bases /
        reopen_lost_bases) は snapshot の束から全文を導出する — Close→Open の
        描画の変更 (2026-09-06 実機裁定) は消費時描画でも記帳に波及しない、
        の検算。
        """
        pic3_path = self.env.home / "image" / "pic3.png"
        pic3_path.write_bytes(b"\x89PNG fake3")
        self.env.items.append(_make_item(
            "uuid-shut-pic", 15, "picture", "しまわれた絵", "港の写生。",
            is_open=False, file_path=pic3_path,
        ))
        closed_bundle = self.env.bundle()
        self._push("b1", closed_bundle)
        self._flush()
        self.env.items[-1] = _make_item(
            "uuid-shut-pic", 15, "picture", "しまわれた絵", "港の写生。",
            is_open=True, file_path=pic3_path,
        )
        opened_bundle = self.env.bundle()
        self._push("b1", opened_bundle)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertIn("(開かれた)", batch.rendered_text)
        self.assertNotIn("港の写生。", batch.rendered_text)
        entry = self._room_entry(batch_id)
        self.assertTrue(entry["is_diff"])
        self.assertEqual(entry["snapshot"], opened_bundle)
        self.assertEqual(entry["base_digest"], snapshot_digest(closed_bundle))
        self.assertEqual(
            [m["path"] for m in batch.media_list()], [str(pic3_path)],
        )


class RoomStateRestoreTest(RoomStateLedgerTestBase):
    """土台の回復 (付記・境界前進と同一 tx) と、復元でのメディアの戻り (§5 契約 2)。"""

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        self.bundle_b = self.env.bundle()
        self._push("b1", self.bundle_a)
        self.base_id = self._flush()
        self._push("b1", self.bundle_b)
        self.diff_id = self._flush()
        # 付記後も部屋の運搬役 (diff) が生き残る形にする。
        self._push_noise()

    def _push_noise(self):
        push_perception(self.conn, "world_state", "ノイズ通知")
        self.noise_id = self._flush()

    def test_annexing_the_base_reopens_the_survivor_to_the_full_text(self):
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        survivor = self._batch(self.diff_id)
        self.assertEqual(
            survivor.rendered_text, render_room_full(self.bundle_b),
        )
        entry = json.loads(survivor.room_state_json)[0]
        self.assertFalse(entry["is_diff"])
        self.assertTrue(entry["transferred"])

    def test_the_restored_full_text_brings_its_media_back(self):
        """§5 契約 2: 開き直しで全文に戻るなら、束のメディアも戻る。"""
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        survivor = self._batch(self.diff_id)
        paths = {m["path"] for m in survivor.media_list()}
        self.assertIn(str(self.env.pic_path), paths)   # 土台と一緒に下りた絵
        self.assertIn(str(self.env.pic2_path), paths)  # 差分が連れてきた絵

    def test_the_rewrite_is_rolled_back_with_the_stamp(self):
        with patch(
            "sai_memory.room_state.restore_room_state_bases",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        # rollback 済み — 付記も書き換えも残っていない。
        survivor = self._batch(self.diff_id)
        self.assertTrue(json.loads(survivor.room_state_json)[0]["is_diff"])
        self.assertIsNotNone(self._batch(self.base_id))

    def test_a_pending_room_whose_tail_was_annexed_composes_the_full_text(self):
        """末尾が付記で下りた後の消費時描画 — 全文 + メディア (§5 の復元)。"""
        env2 = dict(self.bundle_b)
        self._push("b1", env2)  # pending の束 (描画はまだ確定しない)
        mark_batches_annexed(
            self.conn, [self.base_id, self.diff_id], "entry-1",
        )
        self.conn.commit()
        # 付記の置き直し (§6-4) は pending が運搬役なので発火しない。
        self.assertIsNone(
            next(
                (b for b in list_presented_batches(self.conn)
                 if b.room_state_json), None,
            ),
        )
        items = render_pending_room_states(
            self.conn, reduce_perceptions(list_pending(self.conn)),
        )
        room_items = [
            it for it in items
            if json.loads(it.metadata or "{}").get("room_state")
        ]
        self.assertEqual(len(room_items), 1)
        self.assertEqual(room_items[0].content, render_room_full(env2))
        paths = {m["path"] for m in room_items[0].media_list()}
        self.assertIn(str(self.env.pic2_path), paths)


class RoomStateReseatTest(RoomStateLedgerTestBase):
    """§6-4: 最後の運搬役が下りる回は、最新の全文が提示の最古端へ置き直される。"""

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()
        self._push("b1", self.bundle_a)
        self.base_id = self._flush()
        push_perception(self.conn, "world_state", "ノイズ通知")
        self.noise_id = self._flush()

    def _reseated_batches(self):
        return [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated")
                for e in json.loads(b.room_state_json)
            )
        ]

    def test_annexing_the_last_carrier_reseats_the_room(self):
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        reseated = self._reseated_batches()
        self.assertEqual(len(reseated), 1)
        self.assertEqual(
            reseated[0].rendered_text, render_room_full(self.bundle_a),
        )
        # メディアも一緒に運ばれる (§5 の復元)。
        self.assertIn(
            str(self.env.pic_path),
            {m["path"] for m in reseated[0].media_list()},
        )

    def test_the_reseat_stands_at_the_oldest_end(self):
        """機構の置き直しは先頭 = 背景 (2026-09-06 まはー裁定の置き場所の原則)。"""
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        presented = list_presented_batches(self.conn)
        self.assertTrue(presented)
        reseated = self._reseated_batches()[0]
        self.assertEqual(presented[0].id, reseated.id)
        self.assertLess(reseated.consumed_at, self._batch(self.noise_id).consumed_at)

    def test_a_surviving_carrier_means_no_reseat(self):
        self._push("b1", self.bundle_a)  # 二枚目 (変化なしの一行だが運搬役)
        second_id = self._flush()
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        self.assertEqual(self._reseated_batches(), [])
        self.assertIsNotNone(self._batch(second_id))

    def test_the_cutoff_advance_also_reseats(self):
        advance_presentation_cutoff(self.conn, self.base_id)
        self.conn.commit()
        reseated = self._reseated_batches()
        self.assertEqual(len(reseated), 1)
        # 置き直しは新しい id なので、進んだ境界 (id 基準) に下ろされない。
        self.assertGreater(reseated[0].id, get_presentation_cutoff(self.conn))

    def test_a_dry_run_previews_the_same_content_without_writing(self):
        """下見 (dry_run) = 実 INSERT と同じ判定・同じ内容で、何も書かない。

        測るだけの提示組成が「進めたつもり」の列に幻のブロックを合成する口。
        発火条件・材料の選定・位置決めが実 INSERT と同じ一本を通ることを、
        下見の返り値と実物 (境界前進と同一 tx の置き直し) の突き合わせで固定
        する。
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM perception_batches",
        ).fetchone()[0]
        preview = reseat_current_room(
            self.conn, dry_run=True, assume_cutoff=self.base_id,
        )
        after = self.conn.execute(
            "SELECT COUNT(*) FROM perception_batches",
        ).fetchone()[0]
        self.assertEqual(after, before)  # 下見はバッチを積まない
        self.assertEqual(get_presentation_cutoff(self.conn), 0)  # 境界も不動
        self.assertIsNotNone(preview)
        text, media, consumed_at = preview
        advance_presentation_cutoff(self.conn, self.base_id)
        self.conn.commit()
        reseated = self._reseated_batches()[0]
        self.assertEqual(reseated.rendered_text, text)
        self.assertEqual(reseated.media_list(), media)
        self.assertEqual(reseated.consumed_at, consumed_at)

    def test_a_dry_run_with_a_surviving_carrier_returns_none(self):
        """発火しない形も実物と同じ判定 — 運搬役が残る境界では None。"""
        self.assertIsNone(
            reseat_current_room(self.conn, dry_run=True, assume_cutoff=0),
        )

    def test_a_dry_run_respects_the_pending_gate(self):
        """同部屋の pending が居れば下見も発火しない (実物と同じ門)。"""
        self._push("b1", self.bundle_a)  # pending (未消費) の運搬役
        self.assertIsNone(
            reseat_current_room(
                self.conn, dry_run=True, assume_cutoff=self.base_id,
            ),
        )

    def test_a_failed_reseat_rolls_the_annexation_back(self):
        """置き直しに失敗したら付記ごと見送る (§6-4「置き直してから下ろす」)。

        付記だけが確定すると「今いる部屋の全体像が提示のどこにも無い」状態が
        生まれ、次の検知の自己回復より先に送信が起きる経路では部屋なしで
        送られる。restore_room_state_bases の失敗と同じ扱いで rollback する。
        """
        with patch(
            "sai_memory.room_state.reseat_current_room",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        # rollback 済み — 付記は残らず、部屋の運搬役は提示に残っている。
        self.assertIsNotNone(self._batch(self.base_id))  # 未付記のまま
        self.assertIn(
            self.base_id, [b.id for b in list_presented_batches(self.conn)],
        )

    def test_a_failed_reseat_rolls_the_cutoff_advance_back(self):
        """境界前進も同じ契約 — 置き直しに失敗したら前進ごと見送る。"""
        with patch(
            "sai_memory.room_state.reseat_current_room",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                advance_presentation_cutoff(self.conn, self.base_id)
        # rollback 済み — 境界は動かず、部屋の運搬役は提示に残っている。
        self.assertEqual(get_presentation_cutoff(self.conn), 0)
        self.assertIn(
            self.base_id, [b.id for b in list_presented_batches(self.conn)],
        )

    def test_the_reseat_becomes_the_base_for_the_next_diff(self):
        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        self.assertEqual(
            latest_visible_snapshot(self.conn, room_key("b1")), self.bundle_a,
        )
        self._push("b1", self.bundle_a)
        batch_id = self._flush()
        self.assertIn(
            "前回見たときから変わっていません。",
            self._batch(batch_id).rendered_text,
        )

    def test_the_reseat_is_stamped_but_not_annex_material(self):
        """置き直しは出来事ではない — 付記印は受けるが材料には載らない。"""
        from sai_memory.arasuji.executor import collect_annex_items

        mark_batches_annexed(self.conn, [self.base_id], "entry-1")
        self.conn.commit()
        reseated = self._reseated_batches()[0]
        items, batch_ids = collect_annex_items(self.conn, 0, 10_000_000)
        self.assertIn(reseated.id, batch_ids)
        self.assertNotIn(
            render_room_full(self.bundle_a), [i["text"] for i in items],
        )

    def test_find_current_room_key_reads_the_newest_record(self):
        self.assertEqual(find_current_room_key(self.conn), room_key("b1"))

    def test_reseat_is_a_noop_without_any_room_record(self):
        conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(conn)
        self.addCleanup(conn.close)
        self.assertIsNone(reseat_current_room(conn))


class _FailingConn:
    """特定の SQL だけ OperationalError を出す接続の皮 (読み取り失敗の注入)。

    置き直しの発火判定・材料の読み (find_current_room_key / pending_has_room /
    _latest_room_bundle) だけを狙って落とし、hook 自身の書き込み
    (mark_batches_annexed の UPDATE / 境界の UPSERT) と回復の読み
    (list_presented_batches) は素通しする — 「読み取りが失敗しただけの回」を
    再現するため。
    """

    def __init__(self, conn, fail_when):
        self._conn = conn
        self._fail_when = fail_when

    def execute(self, sql, *args, **kwargs):
        if self._fail_when(sql):
            raise sqlite3.OperationalError("injected read failure")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class ReseatReadFailureTest(RoomStateLedgerTestBase):
    """2026-09-06 四巡目修正 1: 置き直しの読み取り失敗は「対象なし」に化けない。

    find_current_room_key / pending_has_room / _latest_room_bundle が DB の
    失敗を握って None / False を返すと、reseat_current_room は「置き直し不要」
    と読み、hook (mark_batches_annexed / advance_presentation_cutoff) は付記・
    境界前進を commit してしまう — 読み取りが失敗しただけなのに、最後の
    運搬役が置き直しなしで下りる。失敗は例外で伝播し、hook が tx ごと
    rollback して見送る (次の機会にやり直す)。
    """

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()
        self._push("b1", self.bundle_a)
        self.base_id = self._flush()
        push_perception(self.conn, "world_state", "ノイズ通知")
        self.noise_id = self._flush()

    def _conn_failing_room_reads(self):
        """現在地の読み (部屋の記帳の SELECT) だけを落とす接続。"""
        return _FailingConn(
            self.conn,
            lambda sql: (
                ("FROM perception_buffer" in sql
                 and sql.lstrip().startswith("SELECT"))
                or "WHERE room_state_json IS NOT NULL" in sql
            ),
        )

    def test_a_failed_room_key_read_rolls_the_annexation_back(self):
        failing = self._conn_failing_room_reads()
        with self.assertRaises(sqlite3.OperationalError):
            mark_batches_annexed(failing, [self.base_id], "entry-1")
        # rollback 済み — 付記は残らず、部屋の運搬役は提示に残っている。
        self.assertIsNotNone(self._batch(self.base_id))
        self.assertIn(
            self.base_id, [b.id for b in list_presented_batches(self.conn)],
        )

    def test_a_failed_room_key_read_rolls_the_cutoff_advance_back(self):
        failing = self._conn_failing_room_reads()
        with self.assertRaises(sqlite3.OperationalError):
            advance_presentation_cutoff(failing, self.base_id)
        self.assertEqual(get_presentation_cutoff(self.conn), 0)
        self.assertIn(
            self.base_id, [b.id for b in list_presented_batches(self.conn)],
        )

    def test_a_failed_material_read_is_not_no_material(self):
        """材料の読み (_latest_room_bundle) の失敗も「材料なし」に化けない。"""
        failing = _FailingConn(
            self.conn,
            lambda sql: (
                "room_state_json, consumed_at FROM perception_batches" in sql
            ),
        )
        with self.assertRaises(sqlite3.OperationalError):
            mark_batches_annexed(failing, [self.base_id], "entry-1")
        self.assertIsNotNone(self._batch(self.base_id))

    def test_a_failed_pending_read_in_the_self_recovery_raises(self):
        """発火の門 (pending_has_room) の失敗も安全側の False に化けない。"""
        failing = _FailingConn(
            self.conn,
            lambda sql: (
                "FROM perception_buffer" in sql
                and sql.lstrip().startswith("SELECT")
            ),
        )
        with self.assertRaises(sqlite3.OperationalError):
            reseat_current_room(failing, fresh_bundle=self.bundle_a)


class PendingRoomGateLegacyTest(RoomStateLedgerTestBase):
    """発火の門 (pending_has_room) は遺物を数えない (2026-09-06 実機所見)。

    移行したペルソナの最初の会話 Pulse: pending には旧形式の遺物 (文字列
    snapshot) だけが居る。検知は「部屋が提示に見えない」と正しく判定して
    自己回復 (:func:`reseat_current_room` + fresh_bundle) を呼ぶが、門が遺物を
    キー一致だけで「次の消費が部屋を運ぶ」と数えると、その遺物は直後の消費の
    回収 (:func:`reclaim_pending_perceptions` — intent §11-2) が落とす — 門の
    前提が嘘になり、部屋なしで送信される。数えてよいのは回収を生き残る有効な
    エントリだけ。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        self.bundle_a = self.env.bundle()

    def _push_legacy_pending(self):
        """旧世代の push が積んだ形の未消費エントリ (snapshot が文字列)。"""
        text = "# 「工房」の様子\n旧世代の全文。"
        return push_perception(
            self.conn, ROOM_STATE_KIND, text,
            metadata=json.dumps(
                {"room_state":
                     {"key": self.key, "is_diff": False, "snapshot": text}},
                ensure_ascii=False,
            ),
        )

    def test_a_legacy_only_pending_does_not_count_as_a_carrier(self):
        self._push_legacy_pending()
        self.assertFalse(pending_has_room(self.conn, self.key))

    def test_the_self_recovery_fires_over_a_legacy_only_pending(self):
        """実機の形 (2026-09-06): 遺物だけの pending は置き直しを止めない。"""
        self._push_legacy_pending()
        batch_id = reseat_current_room(self.conn, fresh_bundle=self.bundle_a)
        self.assertIsNotNone(batch_id)
        batch = self._batch(batch_id)
        self.assertEqual(batch.rendered_text, render_room_full(self.bundle_a))
        entry = json.loads(batch.room_state_json)[0]
        self.assertEqual(entry["key"], self.key)
        self.assertTrue(entry.get("reseated"))

    def test_the_next_consumption_reclaims_the_legacy_and_the_room_survives(self):
        """門の前提「次の消費が運ぶ」は遺物では成立しない — 回収が落とす。

        置き直しが立っていれば、消費バッチに部屋が載らなくても提示には
        置き直しの全文が残る (実機では置き直しが抑止され、部屋なしで送信
        された)。
        """
        self._push_legacy_pending()
        push_perception(self.conn, "world_state", "ノイズ通知")
        reseat_id = reseat_current_room(self.conn, fresh_bundle=self.bundle_a)
        self.assertIsNotNone(reseat_id)
        self.conn.commit()
        flushed = self._flush()
        # 回収が遺物を落とした — 消費バッチは部屋を運ばない。
        self.assertIsNone(self._batch(flushed).room_state_json)
        presented = {b.id: b for b in list_presented_batches(self.conn)}
        self.assertIn(reseat_id, presented)
        self.assertEqual(
            presented[reseat_id].rendered_text,
            render_room_full(self.bundle_a),
        )

    def test_a_valid_pending_still_suppresses_the_reseat(self):
        """有効な pending (snapshot が束) は従来どおり門になる — 次の消費が運ぶ。"""
        self._push("b1", self.bundle_a)
        self.assertTrue(pending_has_room(self.conn, self.key))
        self.assertIsNone(
            reseat_current_room(self.conn, fresh_bundle=self.bundle_a),
        )

    def test_a_mixed_pending_still_suppresses_the_reseat(self):
        """遺物 + 有効の混在も門になる — 有効な方が運ぶ。"""
        self._push_legacy_pending()
        self._push("b1", self.bundle_a)
        self.assertTrue(pending_has_room(self.conn, self.key))
        self.assertIsNone(
            reseat_current_room(self.conn, fresh_bundle=self.bundle_a),
        )

    def test_the_exclusion_does_not_leak_into_the_current_room_key(self):
        """現在地の解決 (find_current_room_key) は遺物も数えたまま — 波及しない。"""
        self._push_legacy_pending()
        self.assertEqual(find_current_room_key(self.conn), self.key)


class RoomStateSelfRecoveryTest(RoomStateLedgerTestBase):
    """§6-2 / §6-3: 検知の瞬間の自己回復 — 部屋が提示に無ければ全文を最古端へ。"""

    def _sai_mem_stub(self):
        """adapter の表面を持つスタブ (呼ばれた口を記録する)。"""
        conn = self.conn

        class Stub:
            def __init__(self):
                self.pushed = []
                self.reseated = []

            def is_ready(self):
                return True

            def latest_room_snapshot(self, key, *, anchor_id=None,
                                     floor_chars=None):
                return latest_visible_snapshot(conn, key)

            def push_room_state(self, building_id, bundle, *, allow_diff=True):
                self.pushed.append((building_id, bundle, allow_diff))

            def reseat_room_state(self, bundle, *, anchor_id=None,
                                  floor_chars=None):
                self.reseated.append(bundle)
                batch_id = reseat_current_room(conn, fresh_bundle=bundle)
                if batch_id is not None:
                    conn.commit()
                return batch_id

        return Stub()

    def _detect(self, sai_mem, bundle, *, chronicle_on=True):
        from sea.head_pipeline.integration import _detect_room_state_changes

        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None,
            current_building_id="b1", sai_memory=sai_mem,
        )
        with patch(
            "builtin_data.tools.get_visual_context.build_room_bundle",
            return_value=bundle,
        ), patch(
            "sea.head_pipeline.integration._room_chronicle_enabled",
            return_value=chronicle_on,
        ):
            _detect_room_state_changes(persona, SimpleNamespace(), "b1")

    def test_bootstrap_reseats_the_full_text_at_the_front(self):
        """起動直後 (台帳が空) の最初の検知で、部屋の全文が背景として立つ。"""
        bundle = self.env.bundle()
        sai_mem = self._sai_mem_stub()
        self._detect(sai_mem, bundle)
        self.assertEqual(sai_mem.reseated, [bundle])
        self.assertEqual(sai_mem.pushed, [])
        presented = list_presented_batches(self.conn)
        self.assertEqual(len(presented), 1)
        self.assertEqual(presented[0].rendered_text, render_room_full(bundle))

    def test_a_change_during_the_stay_is_pushed_as_a_diff_event(self):
        """滞在中の変化は末尾の出来事 (push) — 置き直しではない。"""
        bundle = self.env.bundle()
        self._push("b1", bundle)
        self._flush()
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        changed = self.env.bundle()
        sai_mem = self._sai_mem_stub()
        self._detect(sai_mem, changed)
        self.assertEqual(sai_mem.pushed, [("b1", changed, True)])
        self.assertEqual(sai_mem.reseated, [])

    def test_an_unchanged_visible_room_does_nothing(self):
        bundle = self.env.bundle()
        self._push("b1", bundle)
        self._flush()
        sai_mem = self._sai_mem_stub()
        self._detect(sai_mem, bundle)
        self.assertEqual(sai_mem.pushed, [])
        self.assertEqual(sai_mem.reseated, [])

    def _detect_with_move_during_composition(self, sai_mem, bundle):
        """組成 (build_room_bundle) の最中にペルソナが b2 へ移動する検知を回す。

        検知の冒頭の「ペルソナが building_id に居るか」は通る (まだ b1 に居る)
        が、束の組成 — world の読みで、開いたドキュメントのファイル読み込みを
        含み時間がかかりうる — の間に別スレッド (ユーザー操作の移動 API 等) が
        現在地を変える形の注入。
        """
        from sea.head_pipeline.integration import _detect_room_state_changes

        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None,
            current_building_id="b1", sai_memory=sai_mem,
        )

        def compose_and_move(building_id):
            persona.current_building_id = "b2"
            return bundle

        with patch(
            "builtin_data.tools.get_visual_context.build_room_bundle",
            side_effect=compose_and_move,
        ), patch(
            "sea.head_pipeline.integration._room_chronicle_enabled",
            return_value=True,
        ):
            _detect_room_state_changes(persona, SimpleNamespace(), "b1")

    def test_a_move_during_composition_skips_the_reseat(self):
        """組成中に移動したら、旧部屋の全文を「現在の知覚」に置き直さない。

        2026-09-06 十一巡目: 冒頭の現在地確認の後、組成と積みの間に再確認が
        無かった — 組成中の移動で旧部屋の全文・メディアが現在の知覚として
        積まれる (次の検知が正すので実害は一拍の混入だが、混入自体を避ける)。
        見送った回は次の検知が現在地でやり直す。
        """
        bundle = self.env.bundle()
        sai_mem = self._sai_mem_stub()
        self._detect_with_move_during_composition(sai_mem, bundle)
        self.assertEqual(
            sai_mem.reseated, [],
            "組成中に移動したのに旧部屋の全文が置き直された — "
            "積む直前の現在地の再確認が無い",
        )
        self.assertEqual(sai_mem.pushed, [])

    def test_a_move_during_composition_skips_the_diff_push(self):
        """組成中に移動したら、旧部屋の差分も末尾の出来事として積まない。"""
        bundle = self.env.bundle()
        self._push("b1", bundle)
        self._flush()
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        changed = self.env.bundle()
        sai_mem = self._sai_mem_stub()
        self._detect_with_move_during_composition(sai_mem, changed)
        self.assertEqual(
            sai_mem.pushed, [],
            "組成中に移動したのに旧部屋の差分が積まれた — "
            "積む直前の現在地の再確認が無い",
        )
        self.assertEqual(sai_mem.reseated, [])

    def test_chronicle_disabled_window_loss_reseats(self):
        """窓絞りで運搬役が見えなくなった Chronicle 無効ペルソナの受け皿。

        形は「運搬役が下ろし境界より新しい (= まだ提示に出る) が、実行 model
        の提示窓 (anchor) より古い」。検知は正しく「窓に見えない」と判定して
        ``reseat_room_state`` を呼ぶ — その中の門 (``reseat_current_room`` の
        「運搬役が生きているか」) が窓の篩を通らず提示境界だけで数えると、
        窓の外の運搬役を生きていると誤認して、自己回復がその主目的の形で
        まさに空振りする (2026-09-06 二巡目修正 1)。ここは本物の adapter
        (``SAIMemoryAdapter``) の門を通し、置き直しの全文バッチが実際に提示へ
        立つところまでを固定する。

        旧版のこのテストは、スタブの ``room_carrier_visible`` を False に固定
        して検知に reseat を呼ばせ、検証も「adapter メソッドが呼ばれた記録」
        止まりだった — 置き直しが実際に積まれたかを見ず、DB にも運搬役が窓の
        外になる形が無かったため、この欠陥を素通しした。
        """
        import threading

        from saiverse_memory.adapter import SAIMemoryAdapter
        from sea.head_pipeline.integration import _detect_room_state_changes

        bundle = self.env.bundle()
        self._push("b1", bundle, allow_diff=False)
        carrier_id = self._flush()
        carrier = self._batch(carrier_id)
        # anchor (実行 model の窓の起点) を運搬役より後に置く — 運搬役は
        # 「下ろし境界より新しいが、窓より古い」になる。
        self.conn.execute("CREATE TABLE messages (id TEXT, created_at INTEGER)")
        self.conn.execute(
            "INSERT INTO messages (id, created_at) VALUES (?, ?)",
            ("anchor-1", carrier.consumed_at + 1_000),
        )
        self.conn.commit()

        adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
        adapter.conn = self.conn
        adapter._db_lock = threading.RLock()

        class Lifecycle:
            def resolve_metabolism_anchor(
                self, persona, model_key=None, persist_advance=True,
            ):
                return ("anchor-1", "self")

        manager = SimpleNamespace(
            sea_runtime=SimpleNamespace(session_lifecycle=Lifecycle()),
        )
        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None, model="standard-model",
            current_building_id="b1", sai_memory=adapter,
        )
        with patch(
            "builtin_data.tools.get_visual_context.build_room_bundle",
            return_value=bundle,
        ), patch(
            "sea.head_pipeline.integration._room_chronicle_enabled",
            return_value=False,
        ):
            _detect_room_state_changes(persona, manager, "b1")

        reseated = [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]
        self.assertEqual(
            len(reseated), 1,
            "運搬役が実行 model の窓の外なのに置き直しが積まれていない — "
            "reseat_current_room の門が提示窓の篩を通っていない",
        )
        self.assertEqual(reseated[0].rendered_text, render_room_full(bundle))
        # 変化は無いので diff の push は起きていない (pending は増えない)。
        self.assertEqual(list_pending(self.conn), [])

    def test_the_window_check_uses_the_execution_models_anchor(self):
        """Chronicle 無効の窓判定は**その回の実行 model** の窓で行う。

        提示窓 (anchor) は (ペルソナ, model) ごと。検知の読みが標準 model の
        窓で「運搬役が見えている」と判定すると、実行 model が標準と違う
        ペルソナでは、提示側では部屋が窓の外なのに自己回復が発火しない。
        ``inject_diff_notifications`` が受けた ``model_key`` が
        ``resolve_metabolism_anchor`` まで届くことを配線ごと確かめる。
        """
        from sea.head_pipeline import integration

        bundle = self.env.bundle()
        self._push("b1", bundle, allow_diff=False)
        self._flush()
        conn = self.conn
        sai_mem = self._sai_mem_stub()
        seen_anchors = []

        def windowed_read(key, *, anchor_id=None, floor_chars=None):
            seen_anchors.append(anchor_id)
            # 標準 model の窓には見えているが、実行 model の窓では外れている。
            if anchor_id == "anchor-exec":
                return None
            return latest_visible_snapshot(conn, key)

        sai_mem.latest_room_snapshot = windowed_read

        class Lifecycle:
            def resolve_metabolism_anchor(
                self, persona, model_key=None, persist_advance=True,
                *, strict=False,
            ):
                if model_key == "exec-model":
                    return ("anchor-exec", "self")
                return ("anchor-standard", "self")  # None = 標準 model の窓

        manager = SimpleNamespace(
            sea_runtime=SimpleNamespace(session_lifecycle=Lifecycle()),
        )
        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None, model="standard-model",
            current_building_id="b1", sai_memory=sai_mem,
        )
        with patch(
            "builtin_data.tools.get_visual_context.build_room_bundle",
            return_value=bundle,
        ), patch(
            "sea.head_pipeline.integration._room_chronicle_enabled",
            return_value=False,
        ), patch.object(
            integration, "build_line_head_input",
            return_value=SimpleNamespace(persona_id="p1", model_key="exec-model"),
        ), patch.object(
            integration, "ensure_snapshot",
        ), patch.object(
            integration, "_push_section_diffs", return_value=False,
        ):
            integration.inject_diff_notifications(
                persona, manager, "b1",
                pipeline=SimpleNamespace(), model_key="exec-model",
            )
        # 実行 model の窓で判定し、外れているので自己回復が発火する。
        self.assertEqual(seen_anchors, ["anchor-exec"])
        self.assertEqual(sai_mem.reseated, [bundle])

    def _anchor_window_env(self):
        """運搬役が anchor の窓の外にいる Chronicle 無効環境 (実 adapter)。

        test_chronicle_disabled_window_loss_reseats と同じ形: anchor の行は
        messages に実在して読める。運搬役 (毎回全文) は下ろし境界より新しいが
        anchor より古い — 検知は「窓に見えない」と判定して自己回復を呼ぶ。
        """
        import threading

        from saiverse_memory.adapter import SAIMemoryAdapter

        bundle = self.env.bundle()
        self._push("b1", bundle, allow_diff=False)
        carrier_id = self._flush()
        carrier = self._batch(carrier_id)
        self.conn.execute("CREATE TABLE messages (id TEXT, created_at INTEGER)")
        self.conn.execute(
            "INSERT INTO messages (id, created_at) VALUES (?, ?)",
            ("anchor-1", carrier.consumed_at + 1_000),
        )
        self.conn.commit()

        adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
        adapter.conn = self.conn
        adapter._db_lock = threading.RLock()

        class Lifecycle:
            def resolve_metabolism_anchor(
                self, persona, model_key=None, persist_advance=True,
            ):
                return ("anchor-1", "self")

        manager = SimpleNamespace(
            sea_runtime=SimpleNamespace(session_lifecycle=Lifecycle()),
        )
        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None, model="standard-model",
            current_building_id="b1", sai_memory=adapter,
        )
        return bundle, adapter, persona, manager

    def test_a_floor_failure_with_a_readable_anchor_still_recovers(self):
        """床の予算の一時失敗は、正当な anchor で判定できる回を巻き込まない。

        2026-09-06 五巡目修正 2: 床 (_presentation_floor_chars) は「anchor の
        行が読めないときの代替」の材料なのに、旧実装は anchor の解決を試す前に
        無条件で床を解決していた — 床の取得が一時失敗しただけで、正当な anchor
        が使える回まで「窓の解決失敗 (判定不能)」へ落とし、自己回復を不要に
        スキップする (部屋の欠落が床の取得成功まで直らない)。床の解決とその
        失敗の見送りは、anchor が読めない回だけでよい。
        """
        bundle, _adapter, persona, manager = self._anchor_window_env()
        with patch(
            "sea.runtime_context._minimal_load_chars",
            side_effect=RuntimeError("boom"),
        ):
            self._run_detection(persona, manager, bundle)
        reseated = self._reseated_batches()
        self.assertEqual(
            len(reseated), 1,
            "anchor が読めるのに、床の予算の失敗だけで自己回復が見送られた — "
            "床が anchor より先に (無条件で) 解決されている",
        )
        self.assertEqual(reseated[0].rendered_text, render_room_full(bundle))
        self.assertEqual(list_pending(self.conn), [])

    def test_a_boundary_read_failure_does_not_duplicate_reseats(self):
        """境界キーの単発失敗が、置き直しの全文バッチの重複を生まない。

        2026-09-06 五巡目修正 3: 境界キーなしで積まれた置き直しバッチは、窓
        判定 (batch_in_window) が consumed_at の epoch 比較へフォールバックし、
        置き直しの consumed_at は意図的に最古なので**窓の外**と判定される。
        一方、積む側の土台探し (latest_visible_snapshot) は窓を見ないので同じ
        束を見つける — 次の検知が「見えない」と判定してまた置き直す。単発の
        読み取り失敗が全文バッチの重複を生むので、境界キーが取れない回は
        INSERT せず見送る (読み取り失敗の契約 = 四巡目修正 1 と同じ向き)。
        """
        bundle, adapter, persona, manager = self._anchor_window_env()
        boundary_sql = "ORDER BY created_at DESC, rowid DESC LIMIT 1"
        adapter.conn = _FailingConn(
            self.conn, lambda sql: boundary_sql in sql,
        )
        self._run_detection(persona, manager, bundle)  # 失敗の回 — 見送り
        adapter.conn = self.conn
        self._run_detection(persona, manager, bundle)  # 直った回 — 置き直し
        self._run_detection(persona, manager, bundle)  # 以後は運搬役が見えている
        reseated = self._reseated_batches()
        self.assertEqual(
            len(reseated), 1,
            "境界キーの単発失敗の後、置き直しの全文バッチが重複している — "
            "キーなしの置き直しが窓の外に立って次の検知がまた置き直した",
        )
        self.assertEqual(reseated[0].rendered_text, render_room_full(bundle))
        self.assertEqual(list_pending(self.conn), [])

    def _degraded_window_env(self):
        """anchor の行が読めない劣化窓 (2026-09-06 三巡目 #1 の形)。

        - messages に anchor_id の行が無い (削除・修復などで消えた劣化)。
        - 運搬役は「下ろし境界より新しい (提示候補) が、床の窓より古い」。
        - 生ログの提示窓 (recent) は新しい会話 3 行 (合計 60 字 = 床の予算
          ちょうど) — 提示側の床 (recent の最古 epoch=5000) と検知側の床
          (messages 末尾 60 字ぶんの最古行 = m1) が同じ位置に立つ。
        """
        import threading

        from saiverse_memory.adapter import SAIMemoryAdapter

        bundle = self.env.bundle()
        self.conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, "
            "role TEXT, content TEXT, resource_id TEXT, created_at INTEGER, "
            "metadata TEXT)"
        )
        # 運搬役の時代の古い行 — 床の走査では予算の外に落ちる。
        self.conn.execute(
            "INSERT INTO messages VALUES ('m-old', 't1', 'user', ?, 'p1', "
            "900, NULL)",
            ("昔の話" * 10,),
        )
        self._push("b1", bundle, allow_diff=False)
        carrier_id = self._flush()  # consumed_at=1010、境界キーなし
        # いまの会話 (提示の生ログ窓)。20 字 × 3 行 = 予算 60 字を使い切る。
        for mid, at in (("m1", 5000), ("m2", 5010), ("m3", 5020)):
            self.conn.execute(
                "INSERT INTO messages VALUES (?, 't1', 'user', ?, 'p1', ?, "
                "NULL)",
                (mid, "あ" * 20, at),
            )
        self.conn.commit()

        adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
        adapter.conn = self.conn
        adapter._db_lock = threading.RLock()

        class Lifecycle:
            def resolve_metabolism_anchor(
                self, persona, model_key=None, persist_advance=True,
            ):
                return ("anchor-gone", "self")  # messages に行が無い anchor

            def get_metabolism_watermarks(self, persona, model_key=None):
                return None  # 床の予算は persona.context_length へ落ちる

            def is_chronicle_enabled_for_persona(self, persona):
                return False

        lifecycle = Lifecycle()
        manager = SimpleNamespace(
            sea_runtime=SimpleNamespace(session_lifecycle=lifecycle),
        )
        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None, model="standard-model",
            context_length=60, current_building_id="b1", sai_memory=adapter,
        )
        runtime = SimpleNamespace(session_lifecycle=lifecycle)
        recent = [
            {
                "id": mid, "role": "user", "content": "あ" * 20,
                "created_at": at, "metadata": {"tags": []},
            }
            for mid, at in (("m1", 5000), ("m2", 5010), ("m3", 5020))
        ]
        return bundle, carrier_id, persona, manager, runtime, recent

    def _run_detection(self, persona, manager, bundle):
        from sea.head_pipeline.integration import _detect_room_state_changes

        with patch(
            "builtin_data.tools.get_visual_context.build_room_bundle",
            return_value=bundle,
        ), patch(
            "sea.head_pipeline.integration._room_chronicle_enabled",
            return_value=False,
        ):
            _detect_room_state_changes(persona, manager, "b1")

    def _presented_blocks(self, runtime, persona, recent):
        from sea.runtime_context import list_presented_perception_blocks

        return list_presented_perception_blocks(
            runtime, persona, recent, anchor_id="anchor-gone",
            raise_on_error=True,
        )

    def _reseated_batches(self):
        return [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]

    def test_a_dead_anchor_agrees_with_the_presentation_and_reseats(self):
        """anchor が読めない劣化時も、提示と検知は同じ解決で窓を判定する。

        提示は anchor が引けないと recent の最古 epoch を床にして古い運搬役を
        窓の外に置く。検知が同じ失敗で「窓なし = 全部見える」へ落ちると、
        提示では部屋が見えないのに検知は見えている扱いになり、自己回復が
        抑止される (2026-09-06 三巡目 #1 — 窓の規則の二枚目が劣化経路に
        残っていた)。両側とも resolve_window_key の一枚を通ることを、
        「提示に出ない運搬役 → 置き直しが実際に積まれる」の形で固定する。
        """
        bundle, carrier_id, persona, manager, runtime, recent = (
            self._degraded_window_env()
        )
        # 提示側: 運搬役は床の窓の外 — 提示ブロックに出ない。
        shown = {
            b["metadata"].get("__perception_batch_id__")
            for b in self._presented_blocks(runtime, persona, recent)
        }
        self.assertNotIn(carrier_id, shown)
        # 検知側: 同じ解決を通れば「見えない」→ 置き直しが提示に立つ。
        self._run_detection(persona, manager, bundle)
        reseated = self._reseated_batches()
        self.assertEqual(
            len(reseated), 1,
            "anchor が読めない劣化時、提示は運搬役を窓の外に置くのに検知が"
            "「全部見える」へ落ちて自己回復が抑止されている — 窓の解決が二枚",
        )
        self.assertEqual(reseated[0].rendered_text, render_room_full(bundle))
        # 置き直しは提示側の床の窓でも見える側に立つ (境界キー = 最新の
        # message を優先する包含が床にも効く)。consumed_at の素比較だと
        # 最古端に置いた瞬間から床の外に落ち、提示と検知が逆向きに割れる。
        contents = [
            b["content"]
            for b in self._presented_blocks(runtime, persona, recent)
        ]
        self.assertTrue(
            any(render_room_full(bundle) in c for c in contents),
            "置き直しの全文が提示の床の窓から落ちている",
        )

    def test_a_dead_anchor_does_not_reseat_on_every_detection(self):
        """劣化が続いても、置き直しは検知のたびに繰り返されない。

        置き直しバッチの境界キーは最新の message なので、床 (messages 末尾の
        予算ぶん) が会話少々で追い越すことはない — 次の検知は生きている
        運搬役を数えて何もしない。ここが崩れると、劣化中の毎 Pulse に全文
        一枚が積まれ続ける。
        """
        bundle, _carrier_id, persona, manager, _runtime, _recent = (
            self._degraded_window_env()
        )
        self._run_detection(persona, manager, bundle)
        self.assertEqual(len(self._reseated_batches()), 1)
        # 会話が少し進む (床が置き直しの境界キーを追い越さない範囲)。
        self.conn.execute(
            "INSERT INTO messages VALUES ('m4', 't1', 'user', ?, 'p1', "
            "5030, NULL)",
            ("え" * 5,),
        )
        self.conn.commit()
        self._run_detection(persona, manager, bundle)
        self.assertEqual(
            len(self._reseated_batches()), 1,
            "劣化が続くと検知のたびに置き直しが積まれる — ループ",
        )
        # 変化は無いので diff の push も起きていない。
        self.assertEqual(list_pending(self.conn), [])

    def test_a_failed_floor_budget_defers_the_judgment_with_a_warning(self):
        """床予算の解決失敗は「全部見える」に倒さない — 見送り + WARN。

        2026-09-06 四巡目修正 2: 旧実装は _presentation_floor_chars の例外を
        None (= 床なし) に握り、anchor も読めない劣化の回に resolve_window_key
        が None (= 窓なし・全件可視) になった — 提示は床で運搬役を隠している
        のに、検知は「見えている」と誤判定して自己回復を抑止する (「検知は
        見えない側にしか倒れない」契約の破れ)。単純に「見えない」へ倒すと
        毎検知の置き直しループになるので、解決失敗の回は判定そのものを
        見送って WARN を出す (抑止でもループでもない第三の形)。
        """
        bundle, _carrier_id, persona, manager, _runtime, _recent = (
            self._degraded_window_env()
        )
        with patch(
            "sea.runtime_context._minimal_load_chars",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs(
                "sea.head_pipeline.integration", level="WARNING",
            ) as logs:
                self._run_detection(persona, manager, bundle)
        self.assertTrue(
            any("window" in line for line in logs.output),
            f"見送りの WARN が出ていない: {logs.output}",
        )
        # 見送り = 判定しない — 置き直しも push も起こさない。
        self.assertEqual(self._reseated_batches(), [])
        self.assertEqual(list_pending(self.conn), [])

    def test_a_failed_floor_budget_does_not_loop_reseats(self):
        """解決失敗が続いても、置き直しは一枚も積まれない (ループしない)。

        「解決失敗 = 全部見えない」へ倒すと、置き直しで作った新しいバッチも
        見えない扱いになり、失敗が続く限り毎検知で全文一枚が積もる。見送りは
        そのループを作らず、失敗が直った次の検知が通常どおり自己回復する。
        """
        bundle, _carrier_id, persona, manager, _runtime, _recent = (
            self._degraded_window_env()
        )
        with patch(
            "sea.runtime_context._minimal_load_chars",
            side_effect=RuntimeError("boom"),
        ):
            for _ in range(3):
                self._run_detection(persona, manager, bundle)
        self.assertEqual(self._reseated_batches(), [])
        # 失敗が直った回は、見送っていた判定が通常どおり働く。
        self._run_detection(persona, manager, bundle)
        self.assertEqual(len(self._reseated_batches()), 1)

    def test_an_unreadable_floor_makes_the_room_read_unjudgeable(self):
        """床の読みの失敗も三値の「判定不能」— 「窓なし = 全部見える」に倒さない。

        窓の三値は「窓なし (None)」「窓キー (tuple)」「解決失敗 (例外)」。
        検知の読み (latest_room_snapshot) は解決失敗を例外で伝え、検知は
        その回の部屋の判定を見送る。
        """
        import threading

        from sai_memory.perception_buffer import WindowResolutionError
        from saiverse_memory.adapter import SAIMemoryAdapter

        bundle = self.env.bundle()
        self._push("b1", bundle, allow_diff=False)
        self._flush()  # 運搬役は提示に居る (窓が読めれば見えるかもしれない)
        # messages テーブルが無い = anchor も床も読めない劣化。
        adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
        adapter.conn = self.conn
        adapter._db_lock = threading.RLock()
        with self.assertRaises(WindowResolutionError):
            adapter.latest_room_snapshot(
                room_key("b1"), anchor_id="anchor-gone", floor_chars=60,
            )

    def _oversize_newest_message(self):
        """床の予算 (60 字) を一行で超える最新メッセージ (100 字) を積む。"""
        self.conn.execute(
            "INSERT INTO messages VALUES ('m-big', 't1', 'user', ?, 'p1', "
            "5030, NULL)",
            ("あ" * 100,),
        )
        self.conn.commit()

    def test_an_oversized_newest_message_does_not_unbound_the_window(self):
        """最新の一通が床の予算を超えても、窓は「無制限」に反転しない。

        2026-09-06 十二巡目: 旧 _window_floor_key は予算に収まる行が一つも
        無いと床キーを立てず None を返し、resolve_window_key の None は
        「窓なし = 全件可視」— 巨大な最新メッセージがあるだけで、検知が床の
        外の古い運搬役を「見えている」と誤認して自己回復を抑止する
        (「検知は見えない側にしか倒れない」契約の破れ — 四巡目修正 2 と同じ
        契約)。予算超過の一行は「最新一行だけの窓」(最新行そのものが床) へ
        倒す — 履歴空 (正当な窓なし) だけが None。
        """
        bundle, carrier_id, persona, manager, _runtime, _recent = (
            self._degraded_window_env()
        )
        self._oversize_newest_message()
        self._run_detection(persona, manager, bundle)
        reseated = self._reseated_batches()
        self.assertEqual(
            len(reseated), 1,
            "最新一通が予算を超えると床キーが立たず「全部見える」へ落ち、"
            "床の外の運搬役 (batch %s) への自己回復が抑止されている"
            % carrier_id,
        )
        self.assertEqual(reseated[0].rendered_text, render_room_full(bundle))

    def test_the_one_line_window_does_not_loop_reseats(self):
        """予算超過の一行の保守的な窓でも、置き直しは繰り返されない。

        置き直しバッチの境界キーは最新の message — 床が最新行そのものでも
        ``>=`` の包含で窓の内に立つので、次の検知は置き直しを運搬役として
        数えて何もしない。
        """
        bundle, _carrier_id, persona, manager, _runtime, _recent = (
            self._degraded_window_env()
        )
        self._oversize_newest_message()
        self._run_detection(persona, manager, bundle)
        self.assertEqual(len(self._reseated_batches()), 1)
        self._run_detection(persona, manager, bundle)
        self.assertEqual(
            len(self._reseated_batches()), 1,
            "最新一行だけの窓で置き直しがループしている — 置き直しの境界"
            "キー (最新 message) が窓の外に立っている",
        )
        # 変化は無いので diff の push も起きていない。
        self.assertEqual(list_pending(self.conn), [])


class WindowFloorFourStateTest(unittest.TestCase):
    """床キーの四状態 — 通常 / 一行窓 / 窓なし / 判定不能 (2026-09-06 十三巡目)。

    :func:`sai_memory.perception_buffer.resolve_window_key` の床フォール
    バックの契約: 予算内に収まる最古行があればそれが床 (**通常**)。予算に
    収まる行が一つも無い — 予算超過の一行・予算 0 以下・空内容の並び、
    すべて — なら**最新行そのもの**が床 (**一行窓** = 最新一行だけの保守的な
    窓 — 「検知は見えない側にしか倒れない」)。履歴が空のときだけ None
    (**窓なし** — 正当)。履歴は有るのに正典キー (created_at, rowid) を
    作れる行が一つも無ければ :class:`WindowResolutionError` (**判定不能** —
    None の全件可視にも最古の広い窓にも倒さない)。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, "
            "role TEXT, content TEXT, resource_id TEXT, created_at INTEGER, "
            "metadata TEXT)"
        )

    def _resolve(self, floor_chars=60):
        from sai_memory.perception_buffer import resolve_window_key

        # anchor 行は無い — 床フォールバックの枝に必ず入る。
        return resolve_window_key(
            self.conn, "anchor-gone", floor_chars=floor_chars,
        )

    def _key_of(self, message_id):
        return self.conn.execute(
            "SELECT created_at, rowid FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    def _insert(self, message_id, created_at, content):
        self.conn.execute(
            "INSERT INTO messages VALUES (?, 't1', 'user', ?, 'p1', ?, NULL)",
            (message_id, content, created_at),
        )

    def test_an_empty_history_is_a_legitimate_no_window(self):
        self.assertIsNone(self._resolve())

    def test_the_normal_floor_is_the_oldest_row_within_the_budget(self):
        for mid, at in (("m1", 5000), ("m2", 5010), ("m3", 5020)):
            self._insert(mid, at, "あ" * 20)  # 20 字 × 3 行 = 予算 60 字ちょうど
        created_at, rowid = self._key_of("m1")
        self.assertEqual(self._resolve(), (created_at, rowid))

    def test_an_oversized_newest_row_becomes_its_own_floor(self):
        for mid, at in (("m1", 5000), ("m2", 5010), ("m3", 5020)):
            self._insert(mid, at, "あ" * 20)
        self._insert("m-big", 5030, "あ" * 100)  # 一行で予算 60 字を超える
        created_at, rowid = self._key_of("m-big")
        self.assertEqual(
            self._resolve(), (created_at, rowid),
            "予算に収まる行が一つも無いのに床が None (= 窓なし・全件可視) へ"
            "落ちている — 履歴空と同じ状態に畳まれている",
        )

    def test_a_zero_budget_over_empty_rows_stays_a_one_line_window(self):
        """予算 0 以下では、空内容の行が続いても床は最新行 — 最古へ反転しない。

        2026-09-06 十三巡目: 旧実装は「予算を超えた行の手前で止まる」勘定
        しか持たず、空内容の行は consumed を増やさないので走査が全行を舐め、
        床が**最古行**に立った — 「予算に収まる行が無ければ最新行の一行窓」
        (見えない側に倒れる) と逆向きの、最も広い窓への反転。
        """
        for mid, at in (("m1", 5000), ("m2", 5010), ("m3", 5020)):
            self._insert(mid, at, "")  # 空内容 — 文字勘定を増やさない
        created_at, rowid = self._key_of("m3")
        self.assertEqual(
            self._resolve(floor_chars=0), (created_at, rowid),
            "予算 0 以下で床が最新行でなく最古側に立っている — 一行窓の"
            "契約と逆向きの、実質全件可視の広い窓",
        )

    def test_a_history_without_canonical_keys_is_unjudgeable(self):
        """NULL 時刻だけの非空履歴は「窓なし (全件可視)」でなく判定不能。

        2026-09-06 十三巡目: スキーマ上 created_at は NULL を許す
        (sai_memory/memory/storage.py の messages DDL)。正典キー
        (created_at, rowid) を作れる行が一つも無い履歴では床の近似そのものが
        成立しない — None に畳むと「履歴が有るのに全件可視」へ反転する
        (「検知は見えない側にしか倒れない」契約の破れ — 四巡目修正 2 と同じ
        三値の向きで、受け手の検知は WindowResolutionError でその回の判定を
        見送る)。
        """
        from sai_memory.perception_buffer import WindowResolutionError

        self._insert("m-null", None, "あ" * 20)
        with self.assertRaises(WindowResolutionError):
            self._resolve()

    def test_unkeyed_rows_do_not_poison_a_keyed_history(self):
        """正典キーを作れる行が一つでもあれば、NULL 時刻の行は素通しで通常判定。"""
        self._insert("m-null", None, "あ" * 20)
        for mid, at in (("m1", 5000), ("m2", 5010), ("m3", 5020)):
            self._insert(mid, at, "あ" * 20)
        created_at, rowid = self._key_of("m1")
        self.assertEqual(self._resolve(), (created_at, rowid))


class ChronicleWindowReopenTest(RoomStateLedgerTestBase):
    """可視性が変わる瞬間 4 (Chronicle 無効の窓絞り) — 提示時の開き直し + 絵の復元。

    トグルを有効から無効へ切り替えた後は、有効な間に積んだ差分が台帳に残る。
    窓絞りは台帳へ書ける事実ではないので、提示の組成が文面を全文へ開き直し、
    束のメディアをブロックへ添え直す (§5 の復元 — 台帳は書き換えない)。
    """

    def setUp(self):
        super().setUp()
        import threading

        self.bundle_a = self.env.bundle()
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        self.bundle_b = self.env.bundle()
        self._push("b1", self.bundle_a)
        self.base_id = self._flush()
        self._push("b1", self.bundle_b)
        self.diff_id = self._flush()
        self.persona = SimpleNamespace(
            persona_id="p1", model="test-model",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(),
                is_ready=lambda: True,
            ),
        )
        self.enabled = SimpleNamespace(session_lifecycle=None)
        self.disabled = SimpleNamespace(
            session_lifecycle=SimpleNamespace(
                is_chronicle_enabled_for_persona=lambda persona: False,
            ),
        )

    def _blocks(self, runtime, recent=()):
        from sea.runtime_context import list_presented_perception_blocks

        return list_presented_perception_blocks(
            runtime, self.persona, list(recent), raise_on_error=True,
        )

    def _window_after_base(self):
        """土台のバッチだけが窓の外になる提示行。"""
        base = next(
            b for b in list_unannexed_batches(self.conn) if b.id == self.base_id
        )
        return [{
            "id": "m1", "role": "user", "content": "こんにちは",
            "created_at": base.consumed_at + 1, "metadata": {"tags": []},
        }]

    def test_while_chronicle_is_on_the_diff_stays_a_diff(self):
        blocks = self._blocks(self.enabled)
        diff_block = next(
            b for b in blocks
            if b["metadata"]["__perception_batch_id__"] == self.diff_id
        )
        self.assertIn("新しい絵", diff_block["content"])
        self.assertNotIn("光の扱いはフェルメールに学ぶ。", diff_block["content"])

    def test_the_window_loss_reopens_the_diff_with_its_media(self):
        blocks = self._blocks(self.disabled, self._window_after_base())
        self.assertEqual(len(blocks), 1)
        self.assertIn(render_room_full(self.bundle_b), blocks[0]["content"])
        # 開き直した全文には束のメディアが添え直される (§5: 復元で絵が戻る)。
        paths = {m["path"] for m in blocks[0]["metadata"]["media"]}
        self.assertIn(str(self.env.pic_path), paths)
        self.assertIn(str(self.env.pic2_path), paths)

    def test_the_ledger_is_not_rewritten_and_the_reopening_is_deterministic(self):
        first = self._blocks(self.disabled, self._window_after_base())
        second = self._blocks(self.disabled, self._window_after_base())
        self.assertEqual(
            [b["content"] for b in first], [b["content"] for b in second],
        )
        stored = next(
            b for b in list_unannexed_batches(self.conn) if b.id == self.diff_id
        )
        self.assertNotIn(
            render_room_full(self.bundle_b), stored.rendered_text,
        )


class RoomStateLegacyRecordTest(RoomStateLedgerTestBase):
    """§9: 旧形式 (文字列 snapshot) は土台なし扱い — 連なりに参加しない。"""

    def setUp(self):
        super().setUp()
        self.bundle = self.env.bundle()
        self.legacy_full = "# 「工房」の様子\n古い世代の全文。"
        # 旧世代の flush が確定させたバッチをそのまま構築する — §11-2 の回収
        # 以降、新しい消費は旧形式を文面に載せないので、この形は既存 DB の
        # 遺物としてだけ現れる。§9 の契約 (連なりに参加しない・修復されない)
        # はその遺物に対して守られ続ける。
        self.legacy_id = self._write_old_generation_batch(
            self.legacy_full, self.legacy_full,
        )

    def test_a_legacy_record_is_detected_as_legacy(self):
        entry = json.loads(self._batch(self.legacy_id).room_state_json)[0]
        self.assertTrue(is_legacy_entry(entry))

    def test_a_legacy_record_is_not_a_base(self):
        self.assertIsNone(latest_visible_snapshot(self.conn, room_key("b1")))
        self._push("b1", self.bundle)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        self.assertEqual(batch.rendered_text, render_room_full(self.bundle))
        self.assertFalse(json.loads(batch.room_state_json)[0]["is_diff"])

    def test_a_legacy_record_is_never_repaired(self):
        """旧データの読者を書かない — 提示は積んだときの文面のまま。"""
        self._push("b1", self.bundle)
        new_id = self._flush()
        mark_batches_annexed(self.conn, [new_id], "entry-1")
        self.conn.commit()
        self.assertEqual(
            self._batch(self.legacy_id).rendered_text, self.legacy_full,
        )

    def test_a_new_diff_chains_over_the_legacy_record(self):
        """旧形式より新しい有効束が提示に立てば、次の消費はそこへ連なる。"""
        self._push("b1", self.bundle)
        self._flush()
        self._push("b1", self.bundle)
        batch_id = self._flush()
        entry = json.loads(self._batch(batch_id).room_state_json)[0]
        self.assertTrue(entry["is_diff"])
        self.assertEqual(entry["base_digest"], snapshot_digest(self.bundle))


class BundleValidationTest(_EnvTestBase):
    """2026-09-06 七巡目修正 1: 束の検証は利用側が読むフィールドの型まで行う。

    浅い検査 (dict + packages が list) だけだと、building_id / building_name が
    欠けた束や、key / family / label / lines / media / state の型が壊れた
    パッケージを持つ束が「有効」扱いになり、first_room_bundle の停止規則
    (旧形式・不正束 = 材料なし・土台なし) を素通りする — 壊れた土台への差分や
    壊れた束の置き直しが静かに確定する。検査は読まれる実フィールドだけで、
    読まれないフィールドの有無では落とさない (空の packages は正当な空室)。
    """

    @staticmethod
    def _bundle_with_package(**overrides):
        package = {
            "key": "item:1", "family": "item", "label": "[item:1] 覚え書き",
            "lines": ["[item:1] 覚え書き"], "media": [], "state": "open",
        }
        package.update(overrides)
        return {
            "building_id": "b1", "building_name": "工房",
            "packages": [package],
        }

    def _broken_variants(self):
        """浅い検査は通るが、利用側が読める形ではない束たち。"""
        base = self._bundle_with_package()
        return {
            "building_id が無い": {
                k: v for k, v in base.items() if k != "building_id"
            },
            "building_id が文字列でない": dict(base, building_id=1),
            "building_name が無い": {
                k: v for k, v in base.items() if k != "building_name"
            },
            "building_name が文字列でない": dict(base, building_name=None),
            "パッケージが dict でない": dict(base, packages=["文字列"]),
            "key が空": self._bundle_with_package(key=""),
            "key が文字列でない": self._bundle_with_package(key=1),
            "family が文字列でない": self._bundle_with_package(family=None),
            "label が文字列でない": self._bundle_with_package(label=["リスト"]),
            "lines が list でない": self._bundle_with_package(
                lines="一枚の文字列",
            ),
            "lines の中身が文字列でない": self._bundle_with_package(
                lines=["正当な行", None],
            ),
            "media が list でない": self._bundle_with_package(
                media={"path": "x.png"},
            ),
            "media の中身が dict でない": self._bundle_with_package(
                media=["x.png"],
            ),
            "media の dict に path が無い": self._bundle_with_package(
                media=[{"mime_type": "image/png"}],
            ),
            "state が未知の値": self._bundle_with_package(state="opened"),
        }

    def test_a_real_bundle_and_an_empty_room_are_valid(self):
        self.assertTrue(bundle_is_valid(self.env.bundle()))
        self.assertTrue(bundle_is_valid({
            "building_id": "b1", "building_name": "空き部屋", "packages": [],
        }))

    def test_a_legacy_string_snapshot_is_invalid(self):
        self.assertFalse(bundle_is_valid("# 「工房」の様子\n旧世代の全文。"))
        self.assertFalse(bundle_is_valid(None))

    def test_broken_bundles_are_invalid(self):
        for name, bundle in self._broken_variants().items():
            with self.subTest(name):
                self.assertFalse(bundle_is_valid(bundle), name)

    def test_media_field_types_match_the_consumer_contract(self):
        """2026-09-06 八巡目修正 2: media は path / mime_type の型まで検める。

        消費契約は「path = 非空文字列 (set への in 照合・ファイルパスとして
        使用)、mime_type = 存在するなら文字列 (LLM クライアントがそのまま
        API へ渡す)」。truthiness だけの浅い検査だと ``{"path": ["x"],
        "mime_type": []}`` が有効束を名乗り、:func:`bundle_media` の
        ``path in seen`` (set への in) が TypeError で落ちる — 検証済みの束が
        後段で例外化する。
        """
        variants = {
            "path が文字列でない": [{"path": ["x.png"], "mime_type": []}],
            "mime_type が文字列でない": [{"path": "x.png", "mime_type": 5}],
        }
        for name, media in variants.items():
            with self.subTest(name):
                self.assertFalse(
                    bundle_is_valid(self._bundle_with_package(media=media)),
                    name,
                )
        # 検証を通った束は bundle_media が例外なく読める (消費契約の裏面)。
        ok = self._bundle_with_package(
            media=[{"path": "x.png", "mime_type": "image/png"}],
        )
        self.assertTrue(bundle_is_valid(ok))
        self.assertEqual(bundle_media(ok), [
            {"path": "x.png", "mime_type": "image/png"},
        ])

    def test_duplicate_package_keys_are_invalid(self):
        """2026-09-06 九巡目修正 2: 束の中のキー重複は不正束。

        差分の組成 (render_room_diff) はパッケージをキーで辞書化するので、
        重複キーを持つ記帳破損束が検証を通ると片方が静かに上書きされ、
        全文 (走査順) と差分 (辞書) の整合が崩れる。組成側 (build_room_bundle)
        は先勝ち + WARN で弾くが、それは組成時の弾き — 検証の存在理由は
        「保存済みの束が読み手の前提を満たすか」なので、一意性もここで検める。
        パッケージの型は全部正当なので、型検査だけでは通ってしまう。
        """
        first = {
            "key": "item:1", "family": "item", "label": "[item:1] 一枚目",
            "lines": ["[item:1] 一枚目"], "media": [], "state": "open",
        }
        second = dict(first, label="[item:1] 二枚目", lines=["[item:1] 二枚目"])
        self.assertFalse(bundle_is_valid({
            "building_id": "b1", "building_name": "工房",
            "packages": [first, second],
        }))


class BrokenBundleRouteStopTest(RoomStateLedgerTestBase):
    """七巡目修正 1 の経路検査: 壊れた記帳は「不正束 = 停止」に落ちる。

    材料探し (_latest_room_bundle) と土台探し (latest_visible_snapshot) は
    first_room_bundle の一枚を通るので、bundle_is_valid が浅いと両経路とも
    壊れた束を素通しする (見積もり _room_reseat_projection の同型は
    tests/test_perception_presentation_cap.py の
    PerceptionCapLegacyNewestGateTest 側)。
    """

    @staticmethod
    def _broken_bundle():
        """浅い検査 (packages が list) は通るが、lines の型が壊れた束。"""
        return {
            "building_id": "b1", "building_name": "工房",
            "packages": [{
                "key": "item:1", "family": "item",
                "label": "[item:1] 壊れた記帳",
                "lines": "一枚の文字列 (list でない)", "media": [],
                "state": "open",
            }],
        }

    @staticmethod
    def _media_broken_bundle():
        """media の型が壊れた束 (2026-09-06 八巡目修正 2)。

        lines は正当なので render_room_full は通ってしまうが、path が list
        なので :func:`bundle_media` の ``path in seen`` (set への in) が
        TypeError になる — truthiness だけの検査だと有効束を名乗ったまま
        後段 (置き直しのメディア復元) で例外化する。
        """
        return {
            "building_id": "b1", "building_name": "工房",
            "packages": [{
                "key": "item:1", "family": "item",
                "label": "[item:1] 壊れた絵の記帳",
                "lines": ["[item:1] 壊れた絵の記帳"],
                "media": [{"path": ["x.png"], "mime_type": []}],
                "state": "open",
            }],
        }

    def _push_broken(self, bundle=None):
        # 記帳破損は既存 DB の遺物としてだけ現れる (§11-2 の回収が新しい消費
        # から旧形式・不正束を外すため) — 遺物を直接構築して停止契約を検査。
        return self._write_old_generation_batch(
            "# 「工房」の様子\n壊れた記帳の文面。",
            bundle or self._broken_bundle(),
        )

    def test_a_broken_bundle_is_not_a_diff_base(self):
        """土台探し (latest_visible_snapshot) — 壊れた束は土台にならない。"""
        self._push_broken()
        self.assertIsNone(
            latest_visible_snapshot(self.conn, room_key("b1")),
            "型の壊れた束が差分の土台に立った — 壊れた土台への差分は復元不能",
        )
        bundle = self.env.bundle()
        payload = self._push("b1", bundle)
        self.assertEqual(payload["content"], render_room_full(bundle))

    def test_the_reseat_material_search_stops_at_a_broken_bundle(self):
        """材料探し (_latest_room_bundle) — 壊れた最新記録で止まり、遡らない。"""
        bundle_a = self.env.bundle()
        self._push("b1", bundle_a)
        valid_id = self._flush()
        broken_id = self._push_broken()
        push_perception(self.conn, "world_state", "ノイズ通知")
        self._flush()
        mark_batches_annexed(self.conn, [valid_id, broken_id], "entry-1")
        self.conn.commit()
        reseated = [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]
        self.assertEqual(
            reseated, [],
            "最新の同部屋記録が壊れた束なのに置き直しが立った — "
            "bundle_is_valid が型の壊れた束を有効と数えている",
        )
        # より古い valid の束へも遡らない (五巡目修正 1 と同じ止まり方)。
        texts = [b.rendered_text for b in list_presented_batches(self.conn)]
        self.assertNotIn(render_room_full(bundle_a), texts)

    def test_a_media_broken_bundle_is_not_a_diff_base(self):
        """土台探し — media の型が壊れた束も不正束 (八巡目修正 2)。"""
        self._push_broken(self._media_broken_bundle())
        self.assertIsNone(
            latest_visible_snapshot(self.conn, room_key("b1")),
            "media の型が壊れた束が差分の土台に立った",
        )
        bundle = self.env.bundle()
        payload = self._push("b1", bundle)
        self.assertEqual(payload["content"], render_room_full(bundle))

    @staticmethod
    def _duplicate_key_bundle():
        """キーの重複した束 (記帳破損) — 型は全部正当なので型検査だけでは通る。

        差分の組成 (render_room_diff) はパッケージをキーで辞書化するので、
        この束を有効と数えると片方が静かに上書きされる (九巡目修正 2)。
        """
        package = {
            "key": "item:1", "family": "item", "label": "[item:1] 一枚目",
            "lines": ["[item:1] 一枚目"], "media": [], "state": "open",
        }
        return {
            "building_id": "b1", "building_name": "工房",
            "packages": [
                package,
                dict(package, label="[item:1] 二枚目",
                     lines=["[item:1] 二枚目"]),
            ],
        }

    def test_a_duplicate_key_bundle_is_not_a_diff_base(self):
        """土台探し — キーの重複した束は土台にならない (九巡目修正 2)。"""
        self._push_broken(self._duplicate_key_bundle())
        self.assertIsNone(
            latest_visible_snapshot(self.conn, room_key("b1")),
            "キーの重複した束が差分の土台に立った — 差分の辞書化で片方が"
            "静かに上書きされる",
        )
        bundle = self.env.bundle()
        payload = self._push("b1", bundle)
        self.assertEqual(payload["content"], render_room_full(bundle))

    def test_the_reseat_material_search_stops_at_a_duplicate_key_bundle(self):
        """材料探し — キーの重複した最新記録で止まり、置き直しを立てない。"""
        bundle_a = self.env.bundle()
        self._push("b1", bundle_a)
        valid_id = self._flush()
        broken_id = self._push_broken(self._duplicate_key_bundle())
        push_perception(self.conn, "world_state", "ノイズ通知")
        self._flush()
        mark_batches_annexed(self.conn, [valid_id, broken_id], "entry-1")
        self.conn.commit()
        reseated = [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]
        self.assertEqual(
            reseated, [],
            "キーの重複した最新記録なのに置き直しが立った — bundle_is_valid "
            "が重複キーの束を有効と数えている",
        )
        # より古い valid の束へも遡らない (五巡目修正 1 と同じ止まり方)。
        texts = [b.rendered_text for b in list_presented_batches(self.conn)]
        self.assertNotIn(render_room_full(bundle_a), texts)

    def test_the_reseat_material_search_stops_at_a_media_broken_bundle(self):
        """材料探し — 修正前は bundle_media の TypeError が付記ごと落とす形。

        壊れた最新記録を材料に置き直しへ進むと、render_room_full は通るのに
        :func:`bundle_media` (メディアの復元) が ``path in seen`` の TypeError
        で落ち、付記のトランザクションごと巻き添えになる。検証が消費契約まで
        検めれば、不正束 = 材料なしの停止に落ちて付記は普通に進む。
        """
        bundle_a = self.env.bundle()
        self._push("b1", bundle_a)
        valid_id = self._flush()
        broken_id = self._push_broken(self._media_broken_bundle())
        push_perception(self.conn, "world_state", "ノイズ通知")
        self._flush()
        mark_batches_annexed(self.conn, [valid_id, broken_id], "entry-1")
        self.conn.commit()
        reseated = [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]
        self.assertEqual(
            reseated, [],
            "media の型が壊れた最新記録なのに置き直しが立った",
        )
        texts = [b.rendered_text for b in list_presented_batches(self.conn)]
        self.assertNotIn(render_room_full(bundle_a), texts)


class LegacyNewestReseatTest(RoomStateLedgerTestBase):
    """2026-09-06 五巡目修正 1: 最新の同部屋記録が旧形式なら、置き直しは材料なし。

    旧形式は連なりに参加しない (§9) — だが材料探し (_latest_room_bundle) が
    旧形式を飛ばしてさらに古い構造化束を返すと、fresh_bundle の無い hook 経路
    (付記・境界前進の置き直し) がその古い束を「今の部屋」として最古端に立てる。
    「旧形式の読者を書かない・次の入室が全文を積み直す」という移行契約を材料
    探しが裏口から破り、古い部屋の様子を提示する。同部屋の走査で最初に見つかる
    のが旧形式なら、その時点で材料なし — 受け皿は旧形式の次の入室 push (全文)
    と検知の自己回復。
    """

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()
        self._push("b1", self.bundle_a)
        self.structured_id = self._flush()
        # 構造化束の後に旧形式 (文字列 snapshot) が記帳された台帳 (旧世代の遺物)。
        legacy_text = "# 「工房」の様子\n旧世代の全文。"
        self.legacy_id = self._write_old_generation_batch(
            legacy_text, legacy_text,
        )
        push_perception(self.conn, "world_state", "ノイズ通知")
        self.noise_id = self._flush()

    def _reseated_batches(self):
        return [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]

    def test_the_annexation_reseat_does_not_resurrect_an_older_bundle(self):
        mark_batches_annexed(self.conn, [self.structured_id], "entry-1")
        self.conn.commit()
        self.assertEqual(
            self._reseated_batches(), [],
            "最新の同部屋記録が旧形式なのに、より古い構造化束が置き直しに"
            "立った — 材料探しが旧形式で止まっていない",
        )
        # 古い束の全文が提示に再登場していない。
        texts = [b.rendered_text for b in list_presented_batches(self.conn)]
        self.assertNotIn(render_room_full(self.bundle_a), texts)

    def test_the_cutoff_advance_reseat_stops_at_the_legacy_record_too(self):
        advance_presentation_cutoff(self.conn, self.structured_id)
        self.conn.commit()
        self.assertEqual(self._reseated_batches(), [])
        texts = [b.rendered_text for b in list_presented_batches(self.conn)]
        self.assertNotIn(render_room_full(self.bundle_a), texts)


class StaleCarrierGateTest(RoomStateLedgerTestBase):
    """2026-09-06 七巡目修正 2: 運搬役の存在も「同部屋の最新の一致」で判定する。

    「最新の同部屋記録が旧形式・不正束、より古い valid 記録が提示に残っている」
    並びでは、検知 (latest_room_snapshot = None) が fresh_bundle つきの自己回復
    を呼ぶ — そこで運搬役の走査だけが「古い順に valid を一つでも見つけたら
    生きている」だと、六巡目で一枚化した first_room_bundle の停止規則をこの門
    だけが迂回し、置き直しが抑止される。実際の移行の時系列ではほぼ作れない
    並びだが (構造化形式は昇格後にしか書かれない)、規則の一枚化をここで
    完成させる。
    """

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()
        self._push("b1", self.bundle_a)
        self.valid_id = self._flush()
        # 古い valid 記録より**新しい**同部屋の旧形式 (旧世代の遺物の記帳)。
        legacy_text = "# 「工房」の様子\n旧世代の全文。"
        self.legacy_id = self._write_old_generation_batch(
            legacy_text, legacy_text,
        )
        push_perception(self.conn, "world_state", "ノイズ通知")
        self.noise_id = self._flush()

    def _reseated_batches(self):
        return [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]

    def test_the_self_recovery_reseats_over_an_older_valid_carrier(self):
        """検知が呼んだ fresh_bundle つきの自己回復は、置き直しを積む。"""
        # 検知と同じ判定: 最新の同部屋記録が旧形式なので土台なし。
        self.assertIsNone(latest_visible_snapshot(self.conn, room_key("b1")))
        batch_id = reseat_current_room(self.conn, fresh_bundle=self.bundle_a)
        self.assertIsNotNone(
            batch_id,
            "最新の同部屋記録が旧形式なのに、より古い valid 記録が運搬役扱い"
            "されて置き直しが抑止された — 運搬役の判定が first_room_bundle "
            "の一枚 (最新の一致で確定) を通っていない",
        )
        self.conn.commit()
        reseated = self._reseated_batches()
        self.assertEqual(len(reseated), 1)
        self.assertEqual(
            reseated[0].rendered_text, render_room_full(self.bundle_a),
        )
        # 機構の置き直しは先頭 = 背景 (置き場所の原則はそのまま)。
        self.assertEqual(list_presented_batches(self.conn)[0].id, reseated[0].id)

    def test_the_hook_path_still_stops_with_no_material(self):
        """fresh_bundle の無い経路は従来どおり材料なしで停止 (五巡目修正 1)。"""
        self.assertIsNone(reseat_current_room(self.conn))
        self.assertEqual(self._reseated_batches(), [])


class ChainIntegrityTest(RoomStateLedgerTestBase):
    """連なりの照合 (指紋) — 中間の一枚だけが下りた形も捕まえる。"""

    def setUp(self):
        super().setUp()
        self.bundle_a = self.env.bundle()
        self.env.items.append(_make_item(
            "uuid-n1", 14, "object", "置物その一", "ひとつめ。",
        ))
        self.bundle_b = self.env.bundle()
        self.env.items.append(_make_item(
            "uuid-n2", 15, "object", "置物その二", "ふたつめ。",
        ))
        self.bundle_c = self.env.bundle()
        self._push("b1", self.bundle_a)
        self.a_id = self._flush()
        self._push("b1", self.bundle_b)
        self.b_id = self._flush()
        self._push("b1", self.bundle_c)
        self.c_id = self._flush()

    def _entry(self, batch_id):
        return json.loads(self._batch(batch_id).room_state_json)[0]

    def test_a_diff_records_the_fingerprint_of_its_base(self):
        self.assertEqual(
            self._entry(self.b_id)["base_digest"], snapshot_digest(self.bundle_a),
        )
        self.assertEqual(
            self._entry(self.c_id)["base_digest"], snapshot_digest(self.bundle_b),
        )

    def test_an_intact_chain_is_left_alone(self):
        self.assertEqual(restore_room_state_bases(self.conn), 0)

    def test_annexing_the_middle_reopens_the_orphaned_diff(self):
        mark_batches_annexed(self.conn, [self.b_id], "entry-1")
        self.conn.commit()
        survivor = self._batch(self.c_id)
        self.assertEqual(survivor.rendered_text, render_room_full(self.bundle_c))
        # 最古の全文 (A) はそのまま。
        self.assertEqual(
            self._batch(self.a_id).rendered_text, render_room_full(self.bundle_a),
        )

    def test_chain_check_uses_the_fingerprint(self):
        b_entry = self._entry(self.b_id)
        a_entry = self._entry(self.a_id)
        self.assertTrue(chain_is_intact(b_entry, a_entry))
        self.assertFalse(chain_is_intact(b_entry, self._entry(self.c_id)))
        self.assertFalse(chain_is_intact(b_entry, None))


class EntryPushWiringTest(_EnvTestBase):
    """§6-1: 入室の push は束を組んで adapter へ渡す (末尾 = 出来事)。"""

    def test_on_building_entered_pushes_the_bundle(self):
        from saiverse.dynamic_state import DynamicStateManager

        pushed = []
        sai_mem = SimpleNamespace(
            is_ready=lambda: True,
            push_room_state=lambda bid, bundle, allow_diff=True: pushed.append(
                (bid, bundle, allow_diff),
            ),
        )
        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None, sai_memory=sai_mem,
            current_building_id="b1", buildings=self.env.persona.buildings,
            persona_name="アイフィ",
        )
        self.env.manager.all_personas["p1"] = persona
        self.env.manager.personas = {}
        self.env.manager.feed_manager = None
        bundle = self.env.bundle()
        with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
                patch.object(gvc, "get_active_manager", return_value=self.env.manager), \
                patch.object(gvc, "_get_persona_appearance_path", return_value=None), \
                patch.object(gvc, "_get_building_image_path", return_value=None), \
                patch(
                    "sea.head_pipeline.inject_diff_notifications",
                ) as inject:
            DynamicStateManager.on_building_entered(
                persona, "b1", self.env.manager,
            )
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][0], "b1")
        self.assertEqual(
            canonical_bundle_json(pushed[0][1]), canonical_bundle_json(bundle),
        )
        # 本人向けの検知は detect_room=False (入室を二重に語らない)。
        self.assertTrue(inject.call_args_list)
        self.assertEqual(
            inject.call_args_list[0].kwargs.get("detect_room"), False,
        )
        # 本人へ届けるのは移動の事実だけ (2026-09-07)。スペル等の状態の差分を
        # ここで積むと、次の Pulse で読まれる頃には別の部屋の話になる。
        # building_occupants は文を出さない (部屋替えの分岐は deliver=False) —
        # 基準を新しい部屋の顔ぶれへ合わせるために対象へ含める。
        self.assertEqual(
            inject.call_args_list[0].kwargs.get("only_sections"),
            {"building", "building_occupants"},
        )


class EntryPushDegradeReadinessTest(_EnvTestBase):
    """台帳なし degrade は SAIMemory 未 ready を成功扱いしない。

    push_room_state は未 ready を黙って return するので、degrade 分岐が
    ready を検めずに呼ぶと ok=True のまま知覚が静かに失われる。台帳あり側
    (perception.room_state handler) の「未 ready は例外で pending に残す」と
    対称に、こちらは WARN + ok=False (全段成功の意味は変えない)。
    """

    def test_not_ready_memory_fails_the_entry_push_stage(self):
        from saiverse.dynamic_state import DynamicStateManager

        pushed = []
        sai_mem = SimpleNamespace(
            is_ready=lambda: False,
            push_room_state=lambda bid, bundle, allow_diff=True: pushed.append(
                bid,
            ),
        )
        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None, sai_memory=sai_mem,
            current_building_id="b1", buildings=self.env.persona.buildings,
            persona_name="アイフィ",
        )
        self.env.manager.all_personas["p1"] = persona
        self.env.manager.personas = {}
        self.env.manager.feed_manager = None
        with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
                patch.object(
                    gvc, "get_active_manager", return_value=self.env.manager,
                ), \
                patch.object(
                    gvc, "_get_persona_appearance_path", return_value=None,
                ), \
                patch.object(gvc, "_get_building_image_path", return_value=None), \
                patch("sea.head_pipeline.inject_diff_notifications"), \
                patch(
                    "saiverse.dynamic_state._dispatch_head_event",
                    return_value=True,
                ):
            ok = DynamicStateManager.on_building_entered(
                persona, "b1", self.env.manager,
            )
        self.assertFalse(ok)
        self.assertEqual(pushed, [])  # 未 ready なら push 自体を呼ばない


class DetectionEntryModelKeyTest(unittest.TestCase):
    """検知の呼び出し口が実行 model を配線していること (2026-09-06 二巡目修正 2)。

    中の配管 (``inject_diff_notifications`` の ``model_key`` →
    ``resolve_metabolism_anchor``) は
    RoomStateSelfRecoveryTest.test_the_window_check_uses_the_execution_models_anchor
    が固定済み。ここは入口 — Pulse の通常経路の facade
    (``DynamicStateManager.maybe_inject_event_messages``) が model_key を受けて
    検知まで渡すこと。入口が省略のままだと、配管が通っていても実運用では常に
    標準 model の窓で判定される。
    """

    def test_the_pulse_facade_forwards_the_execution_model(self):
        from saiverse.dynamic_state import DynamicStateManager

        persona = SimpleNamespace(persona_id="p1", current_building_id="b1")
        with patch(
            "sea.head_pipeline.inject_diff_notifications", return_value=True,
        ) as inject:
            ok = DynamicStateManager.maybe_inject_event_messages(
                persona, SimpleNamespace(), model_key="exec-model",
            )
        self.assertTrue(ok)
        self.assertEqual(
            inject.call_args.kwargs.get("model_key"), "exec-model",
        )
        # Pulse 開始の検知は全 Section (絞らない) — 移動時に積まなくなった状態の
        # 差分は、ここが「最後に知らせた状態 vs 今」で拾う (2026-09-07)。
        self.assertIsNone(inject.call_args.kwargs.get("only_sections"))


class EntryOccupantNotifyScopeTest(unittest.TestCase):
    """居合わせる既存者への入室通知は絞らない (2026-09-07 の隣の検算)。

    移動した本人への積み込みだけが移動の事実の 2 セクションに絞られる。既存者に
    とって「誰かが入ってきた」は自分の部屋で起きた出来事なので、従来どおり全
    Section の検知で拾う。
    """

    def test_existing_occupants_are_notified_with_every_section(self):
        from saiverse.dynamic_state import DynamicStateManager

        newcomer = SimpleNamespace(
            persona_id="p1", persona_dir=None, sai_memory=None,
            current_building_id="b1",
        )
        resident = SimpleNamespace(persona_id="p2", current_building_id="b1")
        manager = SimpleNamespace(
            personas={"p1": newcomer, "p2": resident},
            occupants={"b1": ["p1", "p2"]},
            feed_manager=None,
        )
        with patch(
            "sea.head_pipeline.inject_diff_notifications", return_value=True,
        ) as inject, patch(
            "saiverse.dynamic_state._dispatch_head_event", return_value=True,
        ):
            DynamicStateManager.on_building_entered(newcomer, "b1", manager)

        by_persona = {
            call.args[0].persona_id: call for call in inject.call_args_list
        }
        self.assertEqual(
            by_persona["p1"].kwargs.get("only_sections"),
            {"building", "building_occupants"},
        )
        self.assertIsNone(by_persona["p2"].kwargs.get("only_sections"))


class WindowedDetectionReadTest(RoomStateLedgerTestBase):
    """2026-09-06 八巡目修正 1: 検知の読み (digest 比較の「前回」) も提示と同じ窓を通る。

    並びは「古い束 A が窓の内、最新の束 C が窓の外、今の部屋は C のまま」:

    - A は置き直しバッチ (consumed_at は最古端、境界キーは最新の message) —
      窓の包含は境界キー優先 (batch_in_window) なので窓の内。
    - C は境界キーの無いバッチ (旧バッチ / 境界の読みが失敗した flush) —
      epoch フォールバック (consumed_at 比較) で anchor より古く、窓の外。

    検知の読み (latest_room_snapshot) が窓を通らないと、C を「前回」に拾って
    「fresh と同じ = 変化なし」と誤判定し、運搬役判定は窓の中の A を発見して
    置き直しも起きない — ペルソナに見えているのは古い A のままなのに、差分も
    置き直しも来ない。読みが窓を通れば「前回」は窓の中の A になり、fresh (C)
    との変化が積まれて (Chronicle 無効は全文) 見え方が現在に追いつく。
    """

    def setUp(self):
        super().setUp()
        from sai_memory.perception_buffer import (
            batch_in_window,
            resolve_window_key,
        )

        self.bundle_a = self.env.bundle()
        # 部屋が A だった時代の全文 — 境界キーなし (epoch 比較で窓の外)。
        self._push("b1", self.bundle_a, allow_diff=False)
        self._flush()
        # anchor (提示窓の起点)。既存バッチは epoch 比較で全部窓の外になる。
        self.conn.execute("CREATE TABLE messages (id TEXT, created_at INTEGER)")
        self.conn.execute(
            "INSERT INTO messages (id, created_at) VALUES (?, ?)",
            ("anchor-1", 5000),
        )
        self.conn.commit()
        # 検知の自己回復に相当する置き直し — A の全文が窓の内に立つ
        # (境界キー = 最新の message ≥ anchor)。
        window_key = resolve_window_key(self.conn, "anchor-1")
        self.reseat_id = reseat_current_room(
            self.conn, fresh_bundle=self.bundle_a,
            in_window=lambda b: batch_in_window(b, window_key),
        )
        self.assertIsNotNone(self.reseat_id)
        self.conn.commit()
        # 部屋が C に変わり全文 C が積まれる — が、このバッチは境界キーを
        # 持たない (旧バッチ / 境界の読みが失敗した flush) ので窓の外。
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        self.bundle_c = self.env.bundle()
        self._push("b1", self.bundle_c, allow_diff=False)
        self.newest_id = self._flush()

    def test_the_change_detection_uses_the_windowed_previous(self):
        import threading

        from saiverse_memory.adapter import SAIMemoryAdapter
        from sea.head_pipeline.integration import _detect_room_state_changes

        adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
        adapter.conn = self.conn
        adapter._db_lock = threading.RLock()

        class Lifecycle:
            def resolve_metabolism_anchor(
                self, persona, model_key=None, persist_advance=True,
            ):
                return ("anchor-1", "self")

        manager = SimpleNamespace(
            sea_runtime=SimpleNamespace(session_lifecycle=Lifecycle()),
        )
        persona = SimpleNamespace(
            persona_id="p1", persona_dir=None, model="standard-model",
            current_building_id="b1", sai_memory=adapter,
        )
        with patch(
            "builtin_data.tools.get_visual_context.build_room_bundle",
            return_value=self.bundle_c,
        ), patch(
            "sea.head_pipeline.integration._room_chronicle_enabled",
            return_value=False,
        ):
            _detect_room_state_changes(persona, manager, "b1")

        room_pending = [
            it for it in list_pending(self.conn) if it.kind == ROOM_STATE_KIND
        ]
        self.assertEqual(
            len(room_pending), 1,
            "窓の中の提示は古い A のままなのに、検知が窓の外の最新束 C を"
            "「前回」に拾って「変化なし」と誤判定した — 検知の読みが提示の"
            "窓を通っていない",
        )
        self.assertEqual(
            room_pending[0].content, render_room_full(self.bundle_c),
        )
        # 追いつきは変化の push (末尾 = 出来事) — 置き直しは増えない。
        reseated = [
            b for b in list_presented_batches(self.conn)
            if b.room_state_json and any(
                e.get("reseated") for e in json.loads(b.room_state_json)
            )
        ]
        self.assertEqual([b.id for b in reseated], [self.reseat_id])


# ---------------------------------------------------------------------------
# §11: 未消費バッファの回収 (2026-09-07 実機所見の第一弾)
# ---------------------------------------------------------------------------


_NO_CHANGE_LINE_TEST = "前回見たときから変わっていません。"


def _typed_move_meta(from_id, from_name, to_id, to_name):
    """型付きの移動通知 metadata (書き手 = sea/head_pipeline/sections/building.py)。"""
    return json.dumps({
        "label_kind": "building_changed",
        "from_id": from_id, "from_name": from_name,
        "to_id": to_id, "to_name": to_name,
    }, ensure_ascii=False)


def _typed_instruction_meta(building_id, building_name):
    """型付きの役割・指示 metadata — 書き手は 2026-09-07 に退役 (§11-3 改訂)。

    旧コードが積んだ遺物の再現材料。回収 (§11-2 規則 3) が無条件で破棄する
    (指示は束の building:prompt パッケージが運ぶので、独立エントリは様子との
    重複)。
    """
    return json.dumps({
        "label_kind": "building_instruction",
        "building_id": building_id, "building_name": building_name,
    }, ensure_ascii=False)


class _RoundTripMixin:
    """往復 (b1 → b2 → b1) の未消費バッファを積む共通手順。

    実機所見 ④ (2026-09-07、アイフィ) の再現: 部屋の全文が往復のたびに積み重なり、
    移動通知・指示 (指示の書き手は同日中に退役 — 旧コードの遺物) が間に挟まる。
    回収 (§11-2) 後は「経路一行 + 最終の様子 (全文、組成の末尾)」だけが残る
    はずの並び — 指示エントリは遺物として消える (§11-3 改訂)。
    """

    ROUTE_LINE = "この間に現在地が移動しました: 「工房」 → 「書斎」 → 「工房」"

    def _stack_round_trip(self):
        self.bundle_a = self.env.bundle()
        b2 = dict(self.bundle_a)
        b2["building_id"] = "b2"
        b2["building_name"] = "書斎"
        self.bundle_b2 = b2
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        self.bundle_a2 = self.env.bundle()

        self._push("b1", self.bundle_a)
        push_perception(
            self.conn, "world_state",
            "現在地が「工房」から「書斎」に変わりました",
            metadata=_typed_move_meta("b1", "工房", "b2", "書斎"),
        )
        push_perception(
            self.conn, "world_state",
            "# 「書斎」の役割・指示\n書斎では静かに。",
            metadata=_typed_instruction_meta("b2", "書斎"),
        )
        self._push("b2", self.bundle_b2)
        push_perception(
            self.conn, "world_state",
            "現在地が「書斎」から「工房」に変わりました",
            metadata=_typed_move_meta("b2", "書斎", "b1", "工房"),
        )
        push_perception(
            self.conn, "world_state",
            "# 「工房」の役割・指示\n工房の指示。",
            metadata=_typed_instruction_meta("b1", "工房"),
        )
        # 最終の様子も束の記帳のみ — 描画 (提示に末尾が無いので全文) は消費の
        # 組成の一回だけ (§11-2 規則 2)。
        payload = self._push("b1", self.bundle_a2)
        assert "is_diff" not in json.loads(payload["metadata"])["room_state"]


class PendingReclaimTest(_RoundTripMixin, RoomStateLedgerTestBase):
    """§11-2: 回収の一枚 — 往復の堆積・旧形式の遺物・配達重複の自己修復。"""

    def test_a_round_trip_collapses_to_route_and_last_room(self):
        self._stack_round_trip()
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text

        # 経路一行が最後の移動通知の位置に立つ。
        self.assertIn(self.ROUTE_LINE, text)
        # 途中の部屋グループ (通知・指示・様子) は落ちる。
        self.assertNotIn("現在地が「工房」から「書斎」に変わりました", text)
        self.assertNotIn("現在地が「書斎」から「工房」に変わりました", text)
        self.assertNotIn("書斎では静かに。", text)
        self.assertNotIn("# 「書斎」の様子", text)
        # 指示エントリは遺物として消える — 最終の部屋のものも残らない
        # (§11-3 改訂。指示は束の building:prompt パッケージが運ぶ)。
        self.assertNotIn("工房の指示。", text)
        # 最終の様子は消費時描画 — 提示に同部屋の末尾が無いので全文になり、
        # 建物の指示は全文の ## Building 節 (building:prompt) に出る。
        self.assertIn(render_room_full(self.bundle_a2), text)
        self.assertEqual(text.count("# 「工房」の様子"), 1)
        self.assertIn("ここは創作の工房。", text)
        # 読み順: 経路 (出来事) → 様子 (組成の末尾)。
        self.assertLess(
            text.index(self.ROUTE_LINE), text.index("# 「工房」の様子"),
        )
        # 開き直しの全文にはそのメディアも付く (§5 の復元)。
        self.assertEqual(
            [m["path"] for m in self._batch(batch_id).media_list()],
            [str(self.env.pic_path), str(self.env.pic2_path)],
        )
        # 外した行にも消費済みの印が付く (相殺は未消費の間だけ、の既存規則)。
        self.assertEqual(list_pending(self.conn), [])

    def test_legacy_junk_is_dropped_from_the_text_but_consumed(self):
        legacy_text = (
            "# 「工房」の様子\n古い世代の壊れた差分。\n"
            "## 見当たらなくなったもの\n- 本文の段落"
        )
        push_perception(
            self.conn, ROOM_STATE_KIND, legacy_text,
            metadata=json.dumps({"room_state": {
                "key": room_key("b1"), "is_diff": True,
                "snapshot": legacy_text,
            }}, ensure_ascii=False),
        )
        push_perception(self.conn, "world_state", "生きている通知")
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertNotIn("古い世代の壊れた差分", text)
        self.assertIn("生きている通知", text)
        # 旧形式にも消費済みの印だけは付く (台帳の行は消さない)。
        self.assertEqual(list_pending(self.conn), [])
        consumed = self.conn.execute(
            "SELECT consumed_at FROM perception_buffer WHERE kind = ?",
            (ROOM_STATE_KIND,),
        ).fetchall()
        self.assertTrue(all(row[0] is not None for row in consumed))
        # バッチの記帳にも旧形式は載らない。
        self.assertIsNone(self._batch(batch_id).room_state_json)

    def test_a_metadata_less_surroundings_row_is_also_legacy(self):
        push_perception(self.conn, ROOM_STATE_KIND, "metadata の無い旧世代の様子")
        push_perception(self.conn, "world_state", "生きている通知")
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertNotIn("metadata の無い旧世代の様子", text)
        self.assertIn("生きている通知", text)

    def test_a_batch_emptied_by_reclaim_is_not_presented(self):
        """旧形式の遺物だけの消費 = 空バッチ。提示に <system></system> を出さない。"""
        import threading

        from sea.runtime_context import list_presented_perception_blocks

        push_perception(self.conn, ROOM_STATE_KIND, "metadata の無い旧世代の様子")
        batch_id = self._flush()
        self.assertEqual(self._batch(batch_id).rendered_text, "")
        persona = SimpleNamespace(
            persona_id="p1", model="test-model",
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(),
                is_ready=lambda: True,
            ),
        )
        runtime = SimpleNamespace(session_lifecycle=None)
        blocks = list_presented_perception_blocks(
            runtime, persona, [], raise_on_error=True,
        )
        self.assertEqual(blocks, [])

    def test_duplicated_entry_delivery_self_repairs_to_one_full_text(self):
        """入室配送の再試行による様子の二重積み (既知の形) は回収が自己修復する。

        issue: entry_delivery_retry_duplicates_room_perception — 同じ束の push が
        二重に走っても、回収が最後の一枚だけ残し、消費時描画が一枚の全文に組む。
        """
        bundle = self.env.bundle()
        self._push("b1", bundle)
        self._push("b1", bundle)
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertEqual(text, render_room_full(bundle))
        self.assertEqual(text.count("# 「工房」の様子"), 1)
        self.assertNotIn(_NO_CHANGE_LINE_TEST, text)
        self.assertEqual(
            [m["path"] for m in self._batch(batch_id).media_list()],
            [m["path"] for m in bundle_media(bundle)],
        )

    def test_a_single_move_is_left_alone(self):
        push_perception(
            self.conn, "world_state",
            "現在地が「工房」から「書斎」に変わりました",
            metadata=_typed_move_meta("b1", "工房", "b2", "書斎"),
        )
        push_perception(
            self.conn, "world_state",
            "# 「書斎」の役割・指示\n書斎では静かに。",
            metadata=_typed_instruction_meta("b2", "書斎"),
        )
        b2 = dict(self.env.bundle())
        b2["building_id"] = "b2"
        b2["building_name"] = "書斎"
        self._push("b2", b2)
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertIn("現在地が「工房」から「書斎」に変わりました", text)
        self.assertNotIn("この間に現在地が移動しました", text)
        # 指示エントリは移動 1 件でも遺物として破棄される (§11-2 規則 3)。
        self.assertNotIn("書斎では静かに。", text)

    def test_an_instruction_entry_is_dropped_even_without_moves(self):
        """型付き指示エントリは移動 0 件でも破棄される (§11-2 規則 3 の遺物)。"""
        push_perception(
            self.conn, "world_state",
            "# 「工房」の役割・指示\n工房の指示。",
            metadata=_typed_instruction_meta("b1", "工房"),
        )
        push_perception(self.conn, "world_state", "生きている通知")
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertNotIn("工房の指示。", text)
        self.assertIn("生きている通知", text)
        # 破棄した行にも消費済みの印は付く (台帳の行は消さない)。
        self.assertEqual(list_pending(self.conn), [])

    def test_the_room_state_moves_to_the_tail_after_events(self):
        """様子は状態であって出来事ではない — 出来事より後 (組成の末尾) に出る。

        実機 (2026-09-07): エリスの入室通知より先に部屋の差分 ([エリスの外見])
        が出て、因果が逆に読めた。pending が [様子, 出来事] の順でも、回収が
        様子一枚だけを末尾へ動かす (§11-2 規則 2 / §11-3 改訂)。
        """
        self._push("b1", self.env.bundle())
        push_perception(self.conn, "world_state", "エリスがやって来ました")
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertIn("エリスがやって来ました", text)
        self.assertIn("# 「工房」の様子", text)
        self.assertLess(
            text.index("エリスがやって来ました"),
            text.index("# 「工房」の様子"),
        )

    def test_untyped_move_notifications_are_not_collapsed(self):
        """metadata の無い旧ラベルの通知は畳まない (§11-3-2 — 小さいので実害なし)。"""
        push_perception(
            self.conn, "world_state", "現在地が「工房」から「書斎」に変わりました",
        )
        push_perception(
            self.conn, "world_state", "現在地が「書斎」から「工房」に変わりました",
        )
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertIn("現在地が「工房」から「書斎」に変わりました", text)
        self.assertIn("現在地が「書斎」から「工房」に変わりました", text)
        self.assertNotIn("この間に現在地が移動しました", text)
        # 一出来事一ラベル (issues/archive/perception_event_boundaries_unclear.md 裁定):
        # 連続する 2 件でも見出しは合流しない。
        self.assertEqual(text.count("[システム通知]"), 2)

    def test_other_perceptions_keep_their_positions(self):
        """移動・指示・様子以外 (フィード・コア記憶等) は位置ごと一切触らない。"""
        bundle_a = self.env.bundle()
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        bundle_a2 = self.env.bundle()
        push_perception(self.conn, "feed", "フィード記事 その一")
        self._push("b1", bundle_a)
        push_perception(
            self.conn, "world_state",
            "現在地が「工房」から「書斎」に変わりました",
            metadata=_typed_move_meta("b1", "工房", "b2", "書斎"),
        )
        push_perception(self.conn, "core_memory_correction", "コア記憶の修正")
        push_perception(
            self.conn, "world_state",
            "現在地が「書斎」から「工房」に変わりました",
            metadata=_typed_move_meta("b2", "書斎", "b1", "工房"),
        )
        self._push("b1", bundle_a2)
        batch_id = self._flush()
        text = self._batch(batch_id).rendered_text
        self.assertIn("フィード記事 その一", text)
        self.assertIn("コア記憶の修正", text)
        # 経路一行は最後の移動通知の位置 = コア記憶の修正より後。
        self.assertLess(
            text.index("フィード記事 その一"), text.index("コア記憶の修正"),
        )
        self.assertLess(
            text.index("コア記憶の修正"),
            text.index("この間に現在地が移動しました"),
        )


class PendingPreviewParityTest(_RoundTripMixin, RoomStateLedgerTestBase):
    """§11-2: プレビューは実 flush と同じ回収後の文面を出し、DB の行を書き換えない。"""

    def test_preview_matches_flush_and_writes_nothing(self):
        import threading

        from sea.runtime_context import _compose_pending_preview

        self._stack_round_trip()
        sai_mem = SimpleNamespace(conn=self.conn, _db_lock=threading.RLock())
        select = (
            "SELECT id, kind, content, media, metadata, consumed_at "
            "FROM perception_buffer ORDER BY id"
        )
        before = self.conn.execute(select).fetchall()
        preview_items = _compose_pending_preview(sai_mem)
        preview_text = format_perception_message(preview_items)
        after = self.conn.execute(select).fetchall()
        self.assertEqual(before, after)  # 読むだけ — 行は触らない
        self.assertIn(self.ROUTE_LINE, preview_text)
        batch_id = self._flush()
        self.assertEqual(preview_text, self._batch(batch_id).rendered_text)


class ReclaimReturnListTest(unittest.TestCase):
    """§11-2: 回収 (純関数) の返却列の同一性と並びを直接ピン留めする。

    文面レベルのテスト (PendingReclaimTest) と独立に、「エントリの重複・欠落・
    順序破壊が起きない」を item の同一性 (id) で検証する — happy path の期待
    文面だけでは、将来の実装変更でこの契約が崩れても検出できない
    (2026-09-07 Codex 指摘の採用)。
    """

    def _item(self, item_id, kind="world_state", content="x", metadata=None):
        return PerceptionItem(
            id=item_id, kind=kind, content=content, reduce_key=None,
            salient=0, media=None, metadata=metadata, created_at=item_id,
        )

    def _room(self, item_id, building_id="b1"):
        state = {
            "key": f"building:{building_id}",
            "snapshot": {
                "building_id": building_id, "building_name": "工房",
                "packages": [],
            },
            "allow_diff": True,
        }
        return self._item(
            item_id, kind=ROOM_STATE_KIND, content="(束)",
            metadata=json.dumps({"room_state": state}, ensure_ascii=False),
        )

    def test_room_already_at_tail_is_untouched(self):
        items = [self._item(1), self._room(2)]
        out = reclaim_pending_perceptions(items)
        self.assertEqual([i.id for i in out], [1, 2])
        self.assertIs(out[0], items[0])
        self.assertIs(out[1], items[1])

    def test_no_room_state_keeps_order(self):
        items = [self._item(1), self._item(2)]
        out = reclaim_pending_perceptions(items)
        self.assertEqual([i.id for i in out], [1, 2])

    def test_all_entries_can_be_reclaimed_to_empty(self):
        items = [
            self._item(1, metadata=_typed_instruction_meta("b1", "工房")),
        ]
        self.assertEqual(reclaim_pending_perceptions(items), [])

    def test_trail_replacement_and_tail_move_together(self):
        """[様子, 移動1, 移動2] → [経路一行 (移動2 の行), 様子] — 重複も欠落もない。

        畳みの差し替え (経路一行は最後の移動通知の位置) と末尾寄せ (様子は
        組成の末尾) が同時に起きる合流ケース。
        """
        items = [
            self._room(1),
            self._item(2, metadata=_typed_move_meta("b1", "工房", "b2", "書斎")),
            self._item(3, metadata=_typed_move_meta("b2", "書斎", "b1", "工房")),
        ]
        out = reclaim_pending_perceptions(items)
        self.assertEqual([i.id for i in out], [3, 1])
        self.assertTrue(out[0].content.startswith("この間に現在地が移動しました"))
        self.assertIn("「工房」 → 「書斎」 → 「工房」", out[0].content)
        self.assertIs(out[1], items[0])  # 様子は束の記帳のまま (描画は消費時)


class ConsumptionTimeRenderingTest(RoomStateLedgerTestBase):
    """描画は消費の組成の一回だけ (2026-09-06 まはー裁定 — intent §11-2 規則 2)。

    実機の再現 (red): 提示済みの置き直しバッチ (全文、束 A) が生きているのに、
    未消費に [途中の様子 + 型付き移動通知 2 組 + 最後の様子] が積まれた形。
    旧実装は積む時に描画して土台を pending から選ぶ (最後の様子の base_digest =
    捨てられる途中の pending の指紋) が、回収 (§11-2) は途中の pending を必ず
    捨てるので連なりが切れ、消費時の開き直しがもう一枚の全文を立てて保証 2
    (同じ内容が二枚並ばない) を破った。

    green: 描画が消費の組成の一回になると、土台は常に「提示に見えている同部屋の
    末尾の束」(束 A) — 出力は A→最終の差分 (数行。同内容なら一行) になり、
    全文は二枚並ばない。
    """

    def setUp(self):
        super().setUp()
        self.key = room_key("b1")
        self.bundle_a = self.env.bundle()
        self.env.items.append(_make_item(
            "uuid-new", 14, "picture", "新しい絵", "届いたばかりの絵。",
            is_open=True, file_path=self.env.pic2_path,
        ))
        self.bundle_b = self.env.bundle()
        # 提示済みの置き直しバッチ (全文、束 A): 入室の全文を付記で下ろすと
        # §6-4 の置き直しが立つ — 実機と同じ作られ方。
        self._push("b1", self.bundle_a)
        entry_id = self._flush()
        mark_batches_annexed(self.conn, [entry_id], "entry-1")
        self.conn.commit()

    def _stack_pendings(self, mid_bundle, final_bundle):
        """未消費: 途中の様子 → 移動通知 2 組 → 最後の様子 (実機の並び)。"""
        self._push("b1", mid_bundle)  # 滞在中の検知が積んだ途中の様子
        push_perception(
            self.conn, "world_state",
            "現在地が「工房」から「書斎」に変わりました",
            metadata=_typed_move_meta("b1", "工房", "b2", "書斎"),
        )
        push_perception(
            self.conn, "world_state",
            "# 「書斎」の役割・指示\n書斎では静かに。",
            metadata=_typed_instruction_meta("b2", "書斎"),
        )
        push_perception(
            self.conn, "world_state",
            "現在地が「書斎」から「工房」に変わりました",
            metadata=_typed_move_meta("b2", "書斎", "b1", "工房"),
        )
        push_perception(
            self.conn, "world_state",
            "# 「工房」の役割・指示\n工房の指示。",
            metadata=_typed_instruction_meta("b1", "工房"),
        )
        self._push("b1", final_bundle)  # 最後の様子

    def test_the_last_room_state_composes_a_diff_against_the_presented_tail(self):
        self._stack_pendings(self.bundle_b, self.bundle_b)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        text = batch.rendered_text
        # 提示済みの置き直し (束 A の全文) が生きているのに、もう一枚の全文が
        # 立ってはならない (保証 2)。
        self.assertNotIn(render_room_full(self.bundle_b), text)
        self.assertNotIn("覚え書き", text)  # 変わっていないパッケージは再掲しない
        # 出力は A→B の差分 — 新しく現れたパッケージだけ + そのメディア。
        self.assertIn("(前回見たときからの変化)", text)
        self.assertIn("新しい絵", text)
        self.assertEqual(
            [m["path"] for m in batch.media_list()], [str(self.env.pic2_path)],
        )

    def test_an_unchanged_return_condenses_to_the_no_change_line(self):
        self._stack_pendings(self.bundle_b, self.bundle_a)
        batch_id = self._flush()
        batch = self._batch(batch_id)
        text = batch.rendered_text
        self.assertNotIn(render_room_full(self.bundle_a), text)
        self.assertIn(_NO_CHANGE_LINE_TEST, text)
        self.assertEqual(batch.media_list(), [])

    def test_the_batch_records_the_tail_as_the_base(self):
        """記帳の base_digest は末尾 (束 A) の指紋 — 次の消費の連なりが繋がる。"""
        self._stack_pendings(self.bundle_b, self.bundle_b)
        batch_id = self._flush()
        entry = json.loads(self._batch(batch_id).room_state_json)[0]
        self.assertTrue(entry["is_diff"])
        self.assertEqual(entry["base_digest"], snapshot_digest(self.bundle_a))
        self.assertEqual(entry["snapshot"], self.bundle_b)
        # 連なりは切れていない — 提示側の回復は何も直さない。
        self.assertEqual(restore_room_state_bases(self.conn), 0)

    def test_the_preview_composes_the_same_diff_without_writing(self):
        import threading

        from sea.runtime_context import _compose_pending_preview

        self._stack_pendings(self.bundle_b, self.bundle_b)
        sai_mem = SimpleNamespace(conn=self.conn, _db_lock=threading.RLock())
        select = (
            "SELECT id, kind, content, media, metadata, consumed_at "
            "FROM perception_buffer ORDER BY id"
        )
        before = self.conn.execute(select).fetchall()
        preview_text = format_perception_message(
            _compose_pending_preview(sai_mem),
        )
        after = self.conn.execute(select).fetchall()
        self.assertEqual(before, after)  # 読むだけ — 行は触らない
        self.assertNotIn(render_room_full(self.bundle_b), preview_text)
        batch_id = self._flush()
        self.assertEqual(preview_text, self._batch(batch_id).rendered_text)

    def test_the_next_consumption_chains_on_the_recorded_base(self):
        """検算: 消費時描画の記帳を土台に、次の消費の差分が正しく繋がる。"""
        self._stack_pendings(self.bundle_b, self.bundle_b)
        first_id = self._flush()
        self.env.items.append(_make_item(
            "uuid-next", 15, "object", "置物", "あとから増えた。",
        ))
        bundle_c = self.env.bundle()
        self._push("b1", bundle_c)
        second_id = self._flush()
        entry = json.loads(self._batch(second_id).room_state_json)[0]
        self.assertTrue(entry["is_diff"])
        self.assertEqual(entry["base_digest"], snapshot_digest(self.bundle_b))
        self.assertIn("置物", self._batch(second_id).rendered_text)
        self.assertEqual(restore_room_state_bases(self.conn), 0)
        self.assertIsNotNone(self._batch(first_id))


class EntryDeliveryOrderTest(_EnvTestBase):
    """§11-3: 入室配送は一回の flush で 通知 → 様子 の順に揃って着地する。

    逆順の正体 (2026-09-07 調査): 通知は outbox 経由・様子は直接 push という
    配送機構の非対称 + 再入検知による通知の見送り + _flush_queue が配送中に
    積まれた項目を配らないこと。修正後は様子も outbox に乗り、同一 FIFO の
    配り直しで順序が構造的に決まる。役割・指示の独立ラベルは退役 (§11-3 改訂)
    — 指示は様子の全文の ## Building 節 (束の building:prompt) が運ぶ。
    """

    PID = "p1"

    def setUp(self):
        super().setUp()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from database.models import Base
        from saiverse import execution_ledger_wiring as wiring

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.SessionLocal = sessionmaker(bind=engine)

        tmp = tempfile.TemporaryDirectory()
        persona_dir = Path(tmp.name) / "personas" / self.PID
        persona_dir.mkdir(parents=True)

        class _DummyEmbedder:
            def __init__(self, model=None, **kwargs):
                self.model_name = model

            def embed(self, texts, **kwargs):
                return [[0.0] * 3 for _ in texts]

        with patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
            from saiverse_memory import SAIMemoryAdapter
            self.adapter = SAIMemoryAdapter(
                self.PID, persona_dir=persona_dir, resource_id=self.PID,
            )
        self.addCleanup(self.adapter.conn.close)
        self.addCleanup(lambda: self._cleanup_tmp(tmp))

        # 移動先 b2 を実世界 (RealWorldEnv) に足す — 実組成で束が組める部屋。
        self.env.persona.buildings["b2"] = SimpleNamespace(
            name="書斎",
            base_system_instruction="ここは書斎。\n\n静かに使うこと。",
        )
        self.env.manager.occupants["b2"] = ["p1", "p2"]

        self.persona = SimpleNamespace(
            persona_id=self.PID, persona_dir=persona_dir,
            sai_memory=self.adapter, current_building_id="b2",
            buildings=self.env.persona.buildings, persona_name="アイフィ",
        )
        self.env.manager.all_personas[self.PID] = self.persona
        mgr = self.env.manager
        mgr.SessionLocal = self.SessionLocal
        mgr.personas = {self.PID: self.persona}
        mgr.feed_manager = None
        mgr.execution_ledger = wiring.build_execution_ledger(mgr)
        self.ledger = mgr.execution_ledger

    @staticmethod
    def _cleanup_tmp(tmp):
        try:
            tmp.cleanup()
        except PermissionError:
            pass

    def _fake_inject(self, persona, manager, building_id, **kwargs):
        """検知器の代役 — 実 BuildingSection のラベルを実 _push_section_diffs で積む。

        head の snapshot 機構 (capture / ensure) だけを飛ばし、ラベルの組成と
        台帳への積み方は本物を通す。
        """
        from sea.head_pipeline import integration as hp
        from sea.head_pipeline.sections.building import (
            BuildingSection,
            BuildingSnapshot,
        )
        if getattr(persona, "persona_id", None) != self.PID:
            return False
        old = BuildingSnapshot(
            building_id="b1", name="工房",
            base_system_instruction="", physical_vessel_id=None,
        )
        new = BuildingSnapshot(
            building_id="b2", name="書斎",
            base_system_instruction="ここは書斎。\n\n静かに使うこと。",
            physical_vessel_id=None,
        )
        labels = BuildingSection().diff_to_notifications(old, new)
        pipeline = SimpleNamespace(
            flush_diffs=lambda ctx, **kw: (labels, {}),
            advance_last_notified=lambda *a, **k: None,
        )
        ctx = SimpleNamespace(persona_id=persona.persona_id)
        return hp._push_section_diffs(persona, manager, pipeline, ctx, building_id)

    def test_one_flush_lands_notification_then_room(self):
        eid, created = self.ledger.begin_execution(
            "move.entity", persona_id=self.PID,
        )
        self.assertTrue(created)
        self.ledger.mark_running(eid)
        self.ledger.mark_applied(eid, outbox_items=[{
            "target": "move.post_dynamic_state", "persona_id": self.PID,
            "payload": {
                "entity_id": self.PID, "entity_type": "ai", "to_id": "b2",
            },
        }], deliver=False)

        with patch.object(gvc, "get_active_persona_id", return_value=self.PID), \
                patch.object(
                    gvc, "get_active_manager", return_value=self.env.manager,
                ), \
                patch.object(
                    gvc, "_get_persona_appearance_path", return_value=None,
                ), \
                patch.object(gvc, "_get_building_image_path", return_value=None), \
                patch(
                    "sea.head_pipeline.inject_diff_notifications",
                    side_effect=self._fake_inject,
                ), \
                patch(
                    "saiverse.dynamic_state._dispatch_head_event",
                    return_value=True,
                ):
            done = self.ledger.flush_pending_for_persona(self.PID)
            expected_bundle = gvc.build_room_bundle("b2")

        # 一回の flush で全量配送 (通知・様子が pending に残らない)。
        self.assertTrue(done)
        with self.adapter._db_lock:
            rows = self.adapter.conn.execute(
                "SELECT kind, content, metadata FROM perception_buffer "
                "ORDER BY id ASC"
            ).fetchall()
        kinds = [r[0] for r in rows]
        self.assertEqual(kinds, ["world_state", ROOM_STATE_KIND])
        # (1) 移動通知 — 一行のみ (役割・指示の独立ラベルは退役 — §11-3 改訂)。
        self.assertIn("現在地が「工房」から「書斎」に変わりました", rows[0][1])
        self.assertNotIn("役割・指示", rows[0][1])
        meta0 = json.loads(rows[0][2])
        self.assertEqual(meta0.get("label_kind"), "building_changed")
        self.assertEqual(meta0.get("from_id"), "b1")
        self.assertEqual(meta0.get("to_id"), "b2")
        self.assertEqual(meta0.get("to_name"), "書斎")
        # (2) 部屋の様子 — 実組成の束の全文。指示 (システムプロンプト) は
        # 全文の ## Building 節 (束の building:prompt パッケージ) が運ぶ。
        self.assertIsNotNone(expected_bundle)
        self.assertEqual(rows[1][1], render_room_full(expected_bundle))
        self.assertIn("## Building", rows[1][1])
        self.assertIn("ここは書斎。", rows[1][1])
        self.assertIn("静かに使うこと。", rows[1][1])


if __name__ == "__main__":
    unittest.main()
