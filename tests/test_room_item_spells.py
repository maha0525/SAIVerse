"""部屋の片付けの道具のスペル 2 本のテスト。

正典: docs/intent/room_item_display_cap.md 設計 3・5。

- ``buried_items_view``（埋もれたアイテムを見る）: 並びの軸・ページ切り出し・
  説明を切らないこと・部屋が空のとき・範囲外のページ・5 ページ上限。
- ``bag_create``（入れ物を作る）: bag が今いる部屋に**閉じた状態**で置かれる
  こと・名前が空のときの断り。

LLM 呼び出しは無い（どちらも読み書きだけ）。``bag_create`` は一時ファイルの
SQLite を使い、本番データ (~/.saiverse/) には触れない。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from builtin_data.tools.bag_create import bag_create
from builtin_data.tools.buried_items_view import ITEMS_PER_PAGE, buried_items_view
from tools.context import persona_context

BUILDING_ID = "air_city_a_room"
BUILDING_NAME = "エアの部屋"
PERSONA_ID = "tester"

BASE_TIME = datetime(2026, 9, 1, 12, 0, 0)


def _item(short_id, name, *, updated_at, location_updated_at=None,
          item_type="object", description="説明"):
    return {
        "item_id": f"uuid-{short_id}",
        "short_id": short_id,
        "name": name,
        "type": item_type,
        "description": description,
        "file_path": None,
        "state": {},
        "creator_id": PERSONA_ID,
        "source_context": None,
        "created_at": BASE_TIME,
        "updated_at": updated_at,
        "slot_number": short_id,
        "location_updated_at": location_updated_at or updated_at,
    }


class _FakeManager:
    """アイテムのキャッシュだけを持つ manager（部屋の様子の読み口と同じ形）。

    ``bag_contents`` は ``{bag の item_id: [中身のアイテム...]}``。
    """

    def __init__(self, items, bag_contents=None):
        self._items = list(items)
        self._bag_contents = dict(bag_contents or {})
        self.item_locations = {
            item["item_id"]: {
                "owner_kind": "building",
                "owner_id": BUILDING_ID,
                "updated_at": item["location_updated_at"],
                "slot_number": item["slot_number"],
            }
            for item in self._items
        }
        for bag_id, children in self._bag_contents.items():
            for child in children:
                self.item_locations[child["item_id"]] = {
                    "owner_kind": "bag",
                    "owner_id": bag_id,
                    "updated_at": child["location_updated_at"],
                    "slot_number": child["slot_number"],
                }
        persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            persona_name="エア",
            current_building_id=BUILDING_ID,
        )
        self.all_personas = {PERSONA_ID: persona}
        self.personas = {PERSONA_ID: persona}
        self.building_map = {BUILDING_ID: SimpleNamespace(name=BUILDING_NAME)}

    def get_all_items_in_building(self, building_id):
        if building_id != BUILDING_ID:
            return []
        return [dict(item) for item in self._items]

    def get_items_in_bag(self, bag_item_id):
        return [dict(item) for item in self._bag_contents.get(bag_item_id, [])]

    def get_bag_contents_recursive(self, bag_item_id, max_depth=10):
        """部屋の様子の側の入れ子描画（このスペルでは外れているはずの経路）。"""
        result = []
        for child in self._bag_contents.get(bag_item_id, []):
            entry = dict(child)
            entry["_children"] = []
            result.append(entry)
        return result


class BuriedItemsViewSpellTest(unittest.TestCase):
    def _view(self, items, bag_contents=None, **kwargs):
        manager = _FakeManager(items, bag_contents)
        with tempfile.TemporaryDirectory() as tmp:
            with persona_context(PERSONA_ID, Path(tmp), manager=manager):
                return buried_items_view(**kwargs)

    def _refs(self, text):
        """出力に現れた item:N を出た順に拾う。"""
        import re

        return re.findall(r"\[item:(\d+)\]", text)

    # --- 並びの軸 --------------------------------------------------------

    def test_order_is_the_newer_of_the_two_timestamps(self):
        """⭐ 軸は「アイテム自身の更新」と「置き場所の更新」の**新しい方**。

        片方だけで並べると、Bag から出して部屋に置いた物（場所だけ動いた物）が
        埋もれたままになる（intent 設計 1）。
        """
        items = [
            # 置き場所だけが新しい = さっき Bag から出して置いた物。
            _item(1, "取り出した定規",
                  updated_at=BASE_TIME,
                  location_updated_at=BASE_TIME + timedelta(hours=3)),
            # アイテム自身だけが新しい = さっき説明を書き換えた物。
            _item(2, "書き足した手記",
                  updated_at=BASE_TIME + timedelta(hours=2),
                  location_updated_at=BASE_TIME),
            _item(3, "ずっと置きっぱなしの箱",
                  updated_at=BASE_TIME,
                  location_updated_at=BASE_TIME),
        ]
        text = self._view(items)
        self.assertEqual(self._refs(text), ["1", "2", "3"])

    def test_same_timestamp_falls_back_to_short_id_descending(self):
        items = [
            _item(5, "五番", updated_at=BASE_TIME),
            _item(7, "七番", updated_at=BASE_TIME),
            _item(6, "六番", updated_at=BASE_TIME),
        ]
        text = self._view(items)
        self.assertEqual(self._refs(text), ["7", "6", "5"])

    def test_missing_timestamps_sink_to_the_bottom(self):
        """時刻が読めない物は「一番古い」に倒す（新着を押し出さない）。"""
        broken = _item(9, "時刻不明", updated_at=None, location_updated_at=None)
        items = [broken, _item(1, "普通の物", updated_at=BASE_TIME)]
        text = self._view(items)
        self.assertEqual(self._refs(text), ["1", "9"])

    def test_iso_string_timestamps_order_like_real_datetimes(self):
        """⭐ 時刻が ISO 形式の文字列で来ても並びは同じ。

        DB の行をそのまま渡す道では datetime が来るが、JSON を経由した
        キャッシュや API の返りでは文字列になる。文字列を一律で「一番古い」に
        倒すと、さっき触った物が黙って埋もれる（部屋の様子の側と同じ一枚の
        物差し :func:`builtin_data.tools.get_visual_context._touched_epoch`
        を通っていることの確認でもある）。
        """
        items = [
            _item(1, "文字列で新しい物",
                  updated_at=(BASE_TIME + timedelta(hours=3)).isoformat()),
            _item(2, "文字列で真ん中の物",
                  updated_at=(BASE_TIME + timedelta(hours=1)).isoformat()),
            _item(3, "datetime のまま一番古い物", updated_at=BASE_TIME),
            _item(4, "読めない文字列の物",
                  updated_at="いつだったか", location_updated_at="いつだったか"),
        ]
        text = self._view(items)
        self.assertEqual(self._refs(text), ["1", "2", "3", "4"])

    # --- ページ ----------------------------------------------------------

    def _many(self, count):
        return [
            _item(n, f"物 {n}", updated_at=BASE_TIME + timedelta(minutes=n))
            for n in range(1, count + 1)
        ]

    def test_first_page_is_the_default(self):
        text = self._view(self._many(25))
        refs = self._refs(text)
        self.assertEqual(len(refs), ITEMS_PER_PAGE)
        self.assertEqual(refs[0], "25")
        self.assertEqual(refs[-1], "16")
        self.assertIn(
            "全 25 件・3 ページ。いま 1 ページ目 (1〜10 件目)。"
            "page='N' で他のページを開けます。",
            text,
        )
        self.assertIn(f"【この部屋のアイテム】{BUILDING_NAME}", text)

    def test_second_page(self):
        text = self._view(self._many(25), page="2")
        refs = self._refs(text)
        self.assertEqual(refs[0], "15")
        self.assertEqual(refs[-1], "6")
        self.assertIn(
            "全 25 件・3 ページ。いま 2 ページ目 (11〜20 件目)。", text,
        )

    def test_range_opens_several_pages_at_once(self):
        text = self._view(self._many(25), page="1-3")
        self.assertEqual(len(self._refs(text)), 25)
        self.assertIn(
            "全 25 件・3 ページ。いま 1〜3 ページ目 (1〜25 件目)。", text,
        )

    def test_range_over_five_pages_is_refused(self):
        text = self._view(self._many(25), page="1-6")
        self.assertIn("一度に開けるのは 5 ページまでです", text)
        self.assertEqual(self._refs(text), [])

    def test_page_past_the_end_says_so(self):
        text = self._view(self._many(25), page="9")
        self.assertIn("全 3 ページです。9 ページ目はありません。", text)
        self.assertEqual(self._refs(text), [])

    def test_bad_page_argument_is_refused(self):
        text = self._view(self._many(3), page="二")
        self.assertIn("page は '2' のような番号か '1-5' のような範囲", text)

    # --- 本文を切らない・空の部屋 ----------------------------------------

    def test_long_description_is_not_truncated(self):
        """⭐ 不変条件 2・4: 省略の単位は件数だけで、説明は途中で切らない。

        部屋の様子の Bag の中身の描画は 160 字で切り詰めるので、そちらの
        ヘルパを流用して**建物直下の物まで切る**実装に倒れていないかを見る。
        """
        long_text = "真鍮のノギスで測った寸法の控えが刻んである。" * 20
        text = self._view([_item(1, "古い定規", updated_at=BASE_TIME,
                                 description=long_text)])
        self.assertIn(long_text, text)
        self.assertNotIn("...", text)

    def test_empty_room(self):
        text = self._view([])
        self.assertIn("この部屋にはアイテムがありません。", text)

    # --- 入れ物の中身 (2026-09-11 第三回裁定) ------------------------------

    def test_bag_contents_are_listed_with_where_they_are(self):
        """⭐ 入れ物の中の物も一覧に出て、各行に置き場所が添う。

        開いた入れ物の中身の描画にも上限が入ったので、入れ物の中で埋もれた物への
        道はこのスペル一本が担う (intent 設計 3)。
        """
        bag = _item(1, "資料箱", updated_at=BASE_TIME + timedelta(hours=1),
                    item_type="bag", description="紙の束を入れる")
        inside = _item(2, "星の地図", updated_at=BASE_TIME + timedelta(hours=2),
                       description="古い星図の写し")
        text = self._view([bag], bag_contents={bag["item_id"]: [inside]})

        self.assertEqual(self._refs(text), ["2", "1"])
        self.assertIn("[item:2] [Object] 星の地図（入れ物「資料箱」の中）", text)
        # 部屋に直接置かれた物には添え書きが付かない。
        self.assertIn("[item:1] [Bag] 資料箱", text)
        self.assertNotIn("資料箱（入れ物", text)
        # 件数は中身も数える。
        self.assertIn("全 2 件・1 ページ。", text)

    def test_nested_bag_contents_are_labeled_with_the_immediate_container(self):
        outer = _item(1, "大きな箱", updated_at=BASE_TIME, item_type="bag")
        inner = _item(2, "小箱", updated_at=BASE_TIME + timedelta(hours=1),
                      item_type="bag")
        deep = _item(3, "真鍮のノギス", updated_at=BASE_TIME + timedelta(hours=2))
        text = self._view(
            [outer],
            bag_contents={outer["item_id"]: [inner], inner["item_id"]: [deep]},
        )
        self.assertEqual(self._refs(text), ["3", "2", "1"])
        self.assertIn("[item:3] [Object] 真鍮のノギス（入れ物「小箱」の中）", text)
        self.assertIn("[item:2] [Bag] 小箱（入れ物「大きな箱」の中）", text)

    def test_bag_contents_appear_once_even_when_the_bag_is_open(self):
        """開いた入れ物でも、中身が入れ子描画と一覧で二度出ない。"""
        bag = _item(1, "資料箱", updated_at=BASE_TIME, item_type="bag")
        bag["state"] = {"is_open": True}
        inside = _item(2, "星の地図", updated_at=BASE_TIME + timedelta(hours=1))
        text = self._view([bag], bag_contents={bag["item_id"]: [inside]})
        self.assertEqual(text.count("星の地図"), 1)

    def test_a_cycle_in_the_containment_record_does_not_hang(self):
        """置き場所の記録が輪になっていても、一度通った物は二度積まない。"""
        bag_a = _item(1, "箱A", updated_at=BASE_TIME, item_type="bag")
        bag_b = _item(2, "箱B", updated_at=BASE_TIME, item_type="bag")
        text = self._view(
            [bag_a],
            bag_contents={bag_a["item_id"]: [bag_b], bag_b["item_id"]: [bag_a]},
        )
        self.assertEqual(sorted(self._refs(text)), ["1", "2"])

    def test_item_is_drawn_like_the_room_view(self):
        """名前・種類・説明が部屋の様子と同じ見た目で出る。"""
        text = self._view([_item(4, "真鍮のノギス", updated_at=BASE_TIME,
                                 description="使い込まれている")])
        self.assertIn("[item:4] [Object] 真鍮のノギス", text)
        self.assertIn("使い込まれている", text)


class BagCreateSpellTest(unittest.TestCase):
    """実物の ItemService を一時 SQLite で動かして、入れ物が本当に置かれるか見る。"""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from database.models import Base
        from manager.items import ItemService

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        root = Path(self._tmp.name)
        self.persona_path = root / "personas" / PERSONA_ID
        self.persona_path.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(f"sqlite:///{root / 'central.db'}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        # 置き場所に書く Building は DB にも実在させる — 作成は書く前に
        # 「その部屋が今も在るか」を確かめるので、居ない部屋では断られる
        # (どの部屋にも属さないアイテムを作らないため)。
        self._add_building(BUILDING_ID, BUILDING_NAME)

        self.events = []
        self.notes = []
        persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            persona_name="エア",
            current_building_id=BUILDING_ID,
            is_proxy=False,
        )
        self.manager = SimpleNamespace(
            personas={PERSONA_ID: persona},
            all_personas={PERSONA_ID: persona},
            SessionLocal=self.SessionLocal,
            saiverse_home=root,
            buildings=[],
            building_map={BUILDING_ID: SimpleNamespace(
                name=BUILDING_NAME,
                base_system_instruction="",
                system_instruction="",
            )},
            record_persona_event=lambda pid, msg: self.events.append((pid, msg)),
            _append_building_history_note=lambda bid, note: self.notes.append((bid, note)),
        )
        self.service = ItemService(self.manager, SimpleNamespace())
        self.manager.item_service = self.service
        self.manager.items = self.service.items
        self.manager.item_locations = self.service.item_locations
        self.manager.create_bag_item = (
            lambda pid, name, desc, source_context=None:
            self.service.create_bag_item(pid, name, desc, source_context=source_context)
        )
        self.manager.get_all_items_in_building = self.service.get_all_items_in_building

    def _add_building(self, building_id, name):
        from database.models import Building as BuildingModel

        db = self.SessionLocal()
        try:
            db.add(BuildingModel(
                CITYID=1, BUILDINGID=building_id, BUILDINGNAME=name,
                CAPACITY=4, SYSTEM_INSTRUCTION="", ENTRY_PROMPT="",
                AUTO_PROMPT="", DESCRIPTION="", AUTO_INTERVAL_SEC=10,
            ))
            db.commit()
        finally:
            db.close()

    def _cleanup_temp(self):
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            # Windows では sqlite のハンドル解放が rmtree に間に合わないことがある。
            pass

    def _create(self, **kwargs):
        with persona_context(PERSONA_ID, self.persona_path, manager=self.manager):
            return bag_create(**kwargs)

    def _rows(self):
        from database.models import Item as ItemModel, ItemLocation as ItemLocationModel

        db = self.SessionLocal()
        try:
            return (
                db.query(ItemModel).all(),
                db.query(ItemLocationModel).all(),
            )
        finally:
            db.close()

    def test_bag_is_created_closed_in_the_current_room(self):
        message = self._create(name="布の道具袋", description="細かい道具をまとめる")

        items, locations = self._rows()
        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row.NAME, "布の道具袋")
        self.assertEqual(row.TYPE, "bag")
        self.assertEqual(row.DESCRIPTION, "細かい道具をまとめる")
        self.assertEqual(row.CREATOR_ID, PERSONA_ID)
        # ⭐ 閉じた状態で置く — 開いたままだと中身が全部部屋の様子に出て、
        # 片付けにならない（ペルソナには閉じる口がまだ無い）。
        self.assertEqual(json.loads(row.STATE_JSON), {"is_open": False})

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0].OWNER_KIND, "building")
        self.assertEqual(locations[0].OWNER_ID, BUILDING_ID)

        # 返す参照は表示語彙 item:N（生 UUID を返さない）。
        self.assertIn(f"item:{row.SHORT_ID}", message)
        self.assertIn("布の道具袋", message)

    def test_created_bag_is_visible_to_the_room_and_the_paging_spell(self):
        self._create(name="資料箱", description="紙の束を入れる")
        with persona_context(PERSONA_ID, self.persona_path, manager=self.manager):
            text = buried_items_view()
        self.assertIn("[Bag] 資料箱", text)
        self.assertIn("紙の束を入れる", text)

    def test_persona_event_and_building_note_are_recorded(self):
        self._create(name="布の道具袋", description="細かい道具をまとめる")
        self.assertEqual(len(self.events), 1)
        self.assertIn("「布の道具袋」という入れ物を作り", self.events[0][1])
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][0], BUILDING_ID)

    def test_blank_name_is_refused_without_creating_anything(self):
        message = self._create(name="   ", description="説明だけある")
        self.assertIn("名前 (name) が空です", message)
        items, locations = self._rows()
        self.assertEqual(items, [])
        self.assertEqual(locations, [])

    def test_description_may_be_omitted(self):
        self._create(name="空の箱")
        items, _ = self._rows()
        self.assertEqual(items[0].DESCRIPTION, "")

    def test_creation_is_refused_when_the_room_is_not_in_the_db(self):
        """⭐ 現在地の部屋が DB に無ければ、書く前に断る。

        ペルソナの現在地は部屋が消された後もインメモリに残ることがある。その
        ID をそのまま置き場所に書くと、部屋の様子にも「埋もれたアイテムを見る」
        にも出ず、ユーザー画面からも辿れないのに DB には在る、どの部屋にも
        属さないアイテムが静かに生まれる。
        """
        self.manager.personas[PERSONA_ID].current_building_id = "消えた部屋"

        with self.assertRaises(RuntimeError) as ctx:
            self._create(name="布の道具袋", description="細かい道具をまとめる")
        message = str(ctx.exception)
        self.assertIn("消えた部屋", message)
        self.assertIn("見つからない", message)
        # 「データベース登録に失敗しました」で包むと、何が起きたのか読めない。
        self.assertNotIn("データベース登録に失敗しました", message)

        items, locations = self._rows()
        self.assertEqual(items, [])
        self.assertEqual(locations, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.notes, [])


if __name__ == "__main__":
    unittest.main()
