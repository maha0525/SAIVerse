"""SelfViewSection (自分の外見とインベントリ) のテスト。

発端: 2026-09-06 に head の旧 VisualContextSection を退役したとき、それだけが
運んでいた「インベントリの一覧」と「自分の外見の画像」がどこからも届かなく
なった。インベントリの増減の差分通知 (旧 BuildingItemsSection) も同時に消えた。
各部品のテストは緑のまま、組み上がった文脈から持ち物が消えても何も落ちなかった
(docs/issues/inventory_and_appearance_dropped_from_context.md)。

固定する契約:

1. Section 単体 — capture / render / diff / serialize。アイテムの描き方は部屋と
   同じ ``_render_item_entry`` の出力そのもの (二つ目の描画ロジックを作らない)。
2. 置き場 — ``_compose_messages`` がシステムプロンプトと Memory Weave の後ろに
   独立した ``role: "user"`` のメッセージとして置き、画像を ``metadata.media`` で
   添付する。システムプロンプトには入れない。
3. 既存 head への欠損補完 — 新 Section が ``recapture_missing`` で足された回に、
   全アイテムを「加わりました」と通知しない (cached_head_architecture.md C8)。
4. 差分通知の画像 — 加わったアイテムの画像が知覚まで運ばれる (台帳経路・
   台帳なしの degrade 経路の両方)。
5. 境界 — 実際の ``prepare_context`` の出力 (LLM に渡るメッセージ列) に、
   インベントリのアイテム名と外見の行・画像が載る。コア記憶とスペル一覧も同じ
   テストで見る (登録されているのに描画されない章を捕まえる)。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from builtin_data.tools import get_visual_context as gvc
from sea.head_pipeline.integration import (
    SELF_VIEW_SECTION_NAME,
    _compose_messages,
    ensure_snapshot,
    inject_diff_notifications,
)
from sea.head_pipeline.pipeline import HeadPipeline
from sea.head_pipeline.registry import HeadSectionRegistry
from sea.head_pipeline.sections.self_view import (
    EMPTY_INVENTORY_TEXT,
    SelfViewSection,
    SelfViewSnapshot,
)
from sea.head_pipeline.types import (
    LineHeadInput,
    LineHeadSnapshot,
    MediaRef,
    RenderedSection,
)

PERSONA_ID = "aifi_test"
PERSONA_NAME = "アイフィ"
MODEL = "model-standard"
BUILDING = "b1"


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
        "created_at": 1_778_000_000.0,
    }
    if is_open is not None:
        item["state"] = {"is_open": is_open}
    if file_path is not None:
        item["file_path"] = str(file_path)
    return item


class SelfViewWorld:
    """合成ペルソナの持ち物と外見 (実ファイル付き)。"""

    def __init__(self, home: Path):
        self.home = home
        (home / "image").mkdir(parents=True, exist_ok=True)
        (home / "documents").mkdir(parents=True, exist_ok=True)
        self.appearance_path = home / "image" / "aifi_face.png"
        self.appearance_path.write_bytes(b"\x89PNG face")
        self.appearance2_path = home / "image" / "aifi_face_v2.png"
        self.appearance2_path.write_bytes(b"\x89PNG face2")
        self.pendant_path = home / "image" / "pendant.png"
        self.pendant_path.write_bytes(b"\x89PNG pendant")
        self.letter_path = home / "image" / "letter.png"
        self.letter_path.write_bytes(b"\x89PNG letter")
        self.diary_path = home / "documents" / "diary.md"
        self.diary_path.write_text("今日のこと。", encoding="utf-8")

        self.appearance: str | None = str(self.appearance_path)
        self.inventory = [
            _make_item(
                "uuid-pendant", 291, "picture",
                "ラピスラズリとイタリアガラスのペンダント（まはーからの贈り物）",
                "深い青のペンダント。", is_open=True, file_path=self.pendant_path,
            ),
            _make_item("uuid-stone", 292, "object", "川原の丸い石", "すべすべした石。"),
            _make_item(
                "uuid-diary", 293, "document", "日記帳", "毎日の記録。",
                is_open=False, file_path=self.diary_path,
            ),
        ]
        self.persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name=PERSONA_NAME, model=MODEL,
            current_building_id=BUILDING,
        )
        self.manager = SimpleNamespace(
            saiverse_home=home,
            occupants={BUILDING: [PERSONA_ID]},
            all_personas={PERSONA_ID: self.persona},
            personas={PERSONA_ID: self.persona},
            get_all_items_for_persona=self._items_for,
            get_bag_contents_recursive=lambda item_id: [],
        )

    def _items_for(self, persona_id):
        if persona_id != PERSONA_ID:
            return []
        return [dict(item) for item in self.inventory]

    def appearance_patch(self):
        """外見の DB 読み (_get_persona_appearance_path) をこの世界の値に差し替える。"""
        return patch.object(
            gvc, "_get_persona_appearance_path",
            side_effect=lambda manager, pid, **_kw: self.appearance if pid == PERSONA_ID else None,
        )

    def ctx(self, *, manager: Any = "default") -> LineHeadInput:
        return LineHeadInput(
            persona_id=PERSONA_ID, model_key=MODEL,
            current_building_id=BUILDING,
            persona=self.persona,
            manager=self.manager if manager == "default" else manager,
        )


class _WorldTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.world = SelfViewWorld(Path(tmp.name))
        patcher = self.world.appearance_patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        self._appearance_patcher = patcher
        self.section = SelfViewSection()

    def capture(self) -> SelfViewSnapshot:
        return self.section.capture(self.world.ctx())


# ---------------------------------------------------------------------------
# 1. Section 単体
# ---------------------------------------------------------------------------


class SelfViewRenderTest(_WorldTestBase):
    def test_renders_appearance_and_inventory_in_one_system_block(self):
        rendered = self.section.render(self.capture())
        self.assertIsNotNone(rendered)
        text = rendered.text
        self.assertTrue(text.startswith("<system>\n# あなた自身\n"))
        self.assertTrue(text.endswith("</system>"))
        self.assertIn(
            "以下は、あなた自身の外見と、あなたが持っているアイテムです。"
            "持ち物は移動してもあなたと一緒にあります。", text,
        )
        self.assertIn(
            "## 外見\n[あなた自身（アイフィ）の外見]\n"
            f"saiverse://persona/{PERSONA_ID}/image", text,
        )
        self.assertIn("## インベントリ", text)
        self.assertIn(
            "[item:291] [Image] ラピスラズリとイタリアガラスのペンダント（まはーからの贈り物）\n"
            "(Open)", text,
        )
        self.assertIn("saiverse://item/291/image", text)
        self.assertIn("[item:292] [Object] 川原の丸い石", text)
        self.assertIn("[item:293] [Document] 日記帳\n(Closed)", text)
        # head は凍結される — 「現在」「リアルタイム」と書かない
        self.assertNotIn("現在", text)
        self.assertNotIn("リアルタイム", text)
        # 外見 → 開いた写真の順で添付される
        self.assertEqual(
            [m.path for m in rendered.media],
            [str(self.world.appearance_path), str(self.world.pendant_path)],
        )
        self.assertTrue(all(m.role == "image" for m in rendered.media))
        self.assertEqual(rendered.media[0].mime_type, "image/png")

    def test_item_lines_are_the_room_renderer_output(self):
        """アイテム 1 件は部屋と同じ _render_item_entry の出力そのもの。"""
        rendered = self.section.render(self.capture())
        for item in self.world.inventory:
            entry = gvc._render_item_entry(dict(item), self.world.manager)
            self.assertIn("\n".join(entry.lines), rendered.text)

    def test_items_are_in_short_id_order(self):
        self.world.inventory.reverse()
        text = self.section.render(self.capture()).text
        self.assertLess(text.index("[item:291]"), text.index("[item:292]"))
        self.assertLess(text.index("[item:292]"), text.index("[item:293]"))

    def test_empty_inventory_says_so(self):
        self.world.inventory.clear()
        rendered = self.section.render(self.capture())
        self.assertIn(f"## インベントリ\n{EMPTY_INVENTORY_TEXT}\n</system>", rendered.text)
        self.assertEqual([m.path for m in rendered.media], [str(self.world.appearance_path)])

    def test_no_appearance_image_drops_the_appearance_part(self):
        self.world.appearance = None
        rendered = self.section.render(self.capture())
        self.assertNotIn("## 外見", rendered.text)
        self.assertNotIn("の外見]", rendered.text)
        self.assertNotIn("あなた自身の外見と", rendered.text)
        self.assertIn("以下は、あなたが持っているアイテムです。", rendered.text)
        self.assertEqual([m.path for m in rendered.media], [str(self.world.pendant_path)])

    def test_missing_appearance_file_drops_the_appearance_part(self):
        """DB に path があってもファイルが無ければ外見の節ごと省く。"""
        self.world.appearance = str(self.world.home / "image" / "gone.png")
        rendered = self.section.render(self.capture())
        self.assertNotIn("## 外見", rendered.text)

    def test_no_manager_renders_nothing(self):
        """読み口が無い回は head に何も出さない (「持ち物なし」と嘘を書かない)。"""
        snapshot = self.section.capture(self.world.ctx(manager=None))
        self.assertFalse(snapshot.available)
        self.assertIsNone(self.section.render(snapshot))

    def test_read_failure_is_not_turned_into_an_empty_inventory(self):
        def _broken(persona_id):
            raise RuntimeError("item store unavailable")

        self.world.manager.get_all_items_for_persona = _broken
        with self.assertRaises(RuntimeError):
            self.capture()

    def test_appearance_read_failure_is_not_turned_into_no_appearance(self):
        # 外見の DB 読みが失敗した回は、head の読みでは上げる (凍結される head に
        # 「外見なし」を焼かない)。ツールの読みは従来どおり None に倒す。
        def _broken_session():
            raise RuntimeError("database unavailable")

        # 世界の差し替え (外見の読みの偽物) を外して、本物の読みを通す。
        self._appearance_patcher.stop()
        try:
            with patch("database.session.SessionLocal", side_effect=_broken_session):
                with self.assertRaises(RuntimeError):
                    self.capture()
                self.assertIsNone(
                    gvc._get_persona_appearance_path(self.world.manager, PERSONA_ID),
                )
        finally:
            self._appearance_patcher.start()

    def test_snapshot_roundtrip(self):
        snapshot = self.capture()
        restored = self.section.deserialize_snapshot(
            self.section.serialize_snapshot(snapshot),
        )
        self.assertEqual(restored, snapshot)
        self.assertEqual(
            self.section.render(restored).text, self.section.render(snapshot).text,
        )

    def test_not_required_and_not_refreshed_by_events(self):
        self.assertFalse(getattr(self.section, "required", False))
        self.assertEqual(self.section.refresh_on_events, frozenset())


class SelfViewDiffTest(_WorldTestBase):
    def setUp(self):
        super().setUp()
        self.before = self.capture()

    def diff(self):
        return self.section.diff_to_notifications(self.before, self.capture())

    def test_no_change_no_labels(self):
        self.assertEqual(self.diff(), [])

    def test_no_baseline_no_labels(self):
        """基準 (B) が無い回は何も言わない — 全アイテムの「加わりました」を出さない。"""
        self.assertEqual(self.section.diff_to_notifications(None, self.before), [])

    def test_added_item_with_its_image(self):
        self.world.inventory.append(_make_item(
            "uuid-letter", 300, "picture", "手紙の写真", "青い便箋。",
            is_open=True, file_path=self.world.letter_path,
        ))
        labels = self.diff()
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].kind, "inventory_item_added")
        self.assertEqual(
            labels[0].label, "インベントリにアイテム「手紙の写真」(item:300) が加わりました",
        )
        self.assertEqual(
            labels[0].media,
            [MediaRef(path=str(self.world.letter_path), mime_type="image/png", role="image")],
        )

    def test_added_closed_item_has_no_media(self):
        self.world.inventory.append(_make_item("uuid-key", 301, "object", "鍵", "古い鍵。"))
        labels = self.diff()
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].media, [])

    def test_removed_item(self):
        self.world.inventory = [i for i in self.world.inventory if i["short_id"] != 292]
        labels = self.diff()
        self.assertEqual([lb.kind for lb in labels], ["inventory_item_removed"])
        self.assertEqual(
            labels[0].label, "インベントリからアイテム「川原の丸い石」(item:292) がなくなりました",
        )

    def test_renamed_item(self):
        self.world.inventory[1]["name"] = "願いの石"
        labels = self.diff()
        self.assertEqual([lb.kind for lb in labels], ["inventory_item_renamed"])
        self.assertEqual(
            labels[0].label,
            "インベントリのアイテム「川原の丸い石」(item:292) の名前が「願いの石」に変わりました",
        )

    def test_appearance_changed_with_the_new_image(self):
        self.world.appearance = str(self.world.appearance2_path)
        labels = self.diff()
        self.assertEqual([lb.kind for lb in labels], ["self_appearance_changed"])
        self.assertEqual(labels[0].label, "あなたの外見の画像が変わりました")
        self.assertEqual([m.path for m in labels[0].media], [str(self.world.appearance2_path)])

    def test_appearance_removed_is_reported(self):
        """外見の設定が外された回は知らせる。DB の読み失敗は capture が上げて
        前の値が据え置かれるので、ここに空が届くのは本当に外されたときだけ。"""
        self.world.appearance = None
        labels = self.diff()
        self.assertEqual([lb.kind for lb in labels], ["self_appearance_removed"])
        self.assertEqual(labels[0].label, "あなたの外見の画像が外されました")
        self.assertEqual(labels[0].media, [])

    def test_unavailable_side_is_silent(self):
        unavailable = SelfViewSnapshot(available=False)
        self.assertEqual(self.section.diff_to_notifications(unavailable, self.before), [])
        self.assertEqual(self.section.diff_to_notifications(self.before, unavailable), [])


# ---------------------------------------------------------------------------
# 2. 置き場 (_compose_messages)
# ---------------------------------------------------------------------------


class ComposeSelfViewTest(_WorldTestBase):
    def test_self_view_is_a_separate_user_message_after_system_and_weave(self):
        weave_entry = SimpleNamespace(kind="chronicle", content="これまでのあらすじ")
        snapshot = LineHeadSnapshot(
            persona_id=PERSONA_ID, model_key=MODEL, line_role="main_line",
            captured_at=0.0, snapshot_version=1,
            sections={"memory_weave": SimpleNamespace(entries=(weave_entry,))},
        )
        self_view = self.section.render(self.capture())
        rendered_by_name = {
            "persona_self": RenderedSection(text="## あなたについて\nアイフィです。"),
            "memory_weave": RenderedSection(text="(weave)"),
            SELF_VIEW_SECTION_NAME: self_view,
        }
        messages = _compose_messages(snapshot, rendered_by_name)

        self.assertEqual([m["role"] for m in messages], ["system", "user", "user"])
        self.assertNotIn("# あなた自身", messages[0]["content"])
        self.assertTrue(messages[1]["metadata"].get("__memory_weave_context__"))
        last = messages[2]
        self.assertEqual(last["content"], self_view.text)
        self.assertTrue(last["metadata"]["__self_view__"])
        # head の視覚メッセージの共通の印 — 画像の枠を使わない / 自動想起の
        # クエリから外れる (既存の読み手が居る印)
        self.assertTrue(last["metadata"]["__visual_context__"])
        self.assertEqual(
            last["metadata"]["media"],
            [
                {"path": str(self.world.appearance_path), "mime_type": "image/png", "type": "image"},
                {"path": str(self.world.pendant_path), "mime_type": "image/png", "type": "image"},
            ],
        )

    def test_nothing_is_added_when_the_section_renders_nothing(self):
        messages = _compose_messages(None, {
            "persona_self": RenderedSection(text="## あなたについて"),
        })
        self.assertEqual([m["role"] for m in messages], ["system"])


# ---------------------------------------------------------------------------
# 3. 既存 head への欠損補完 (C8) と 4. 差分通知の配送
# ---------------------------------------------------------------------------


class _ConstSection:
    """既存 head を表す最小の Section (self_view より前から居た章)。"""

    name = "persona_self"
    order = 200
    refresh_on_events = frozenset()

    def capture(self, ctx):
        return {"text": "アイフィ"}

    def render(self, snapshot):
        return RenderedSection(text=snapshot["text"])

    def diff_to_notifications(self, old, new):
        return []

    def serialize_snapshot(self, snapshot):
        return json.dumps(snapshot)

    def deserialize_snapshot(self, data):
        return json.loads(data)


class _RecordingSaiMemory:
    def __init__(self):
        self.pushed: List[tuple] = []

    def is_ready(self):
        return True

    def push_perception(self, kind, content, **kwargs):
        self.pushed.append((kind, content, kwargs))


class MissingSectionBackfillTest(_WorldTestBase):
    """新 Section が既存 head へ欠損補完で足される回の既読基準 (C8)。"""

    def setUp(self):
        super().setUp()
        self.registry = HeadSectionRegistry()
        self.registry.register(_ConstSection())
        self.pipeline = HeadPipeline(registry=self.registry)
        # self_view が無かった頃の head を作ってから、Section を足す
        self.pipeline.capture_all(self.world.ctx())
        self.registry.register(SelfViewSection())
        self.world.persona.sai_memory = _RecordingSaiMemory()

    def test_backfill_does_not_announce_every_item(self):
        ensure_snapshot(self.pipeline, self.world.ctx())
        snapshot = self.pipeline.get_snapshot(PERSONA_ID, MODEL)
        self.assertIsNotNone(snapshot.sections.get(SELF_VIEW_SECTION_NAME))

        # 実際の Pulse 頭の検知 (台帳なしの degrade 経路) を通す
        pushed = inject_diff_notifications(
            self.world.persona, self.world.manager, BUILDING,
            pipeline=self.pipeline, model_key=MODEL, detect_room=False,
        )
        self.assertFalse(pushed)
        self.assertEqual(self.world.persona.sai_memory.pushed, [])

    def test_after_backfill_a_new_item_is_announced_with_its_image(self):
        ensure_snapshot(self.pipeline, self.world.ctx())
        self.world.inventory.append(_make_item(
            "uuid-letter", 300, "picture", "手紙の写真", "青い便箋。",
            is_open=True, file_path=self.world.letter_path,
        ))
        pushed = inject_diff_notifications(
            self.world.persona, self.world.manager, BUILDING,
            pipeline=self.pipeline, model_key=MODEL, detect_room=False,
        )
        self.assertTrue(pushed)
        records = self.world.persona.sai_memory.pushed
        self.assertEqual(len(records), 1)
        kind, content, kwargs = records[0]
        self.assertEqual(kind, "world_state")
        self.assertEqual(content, "インベントリにアイテム「手紙の写真」(item:300) が加わりました")
        self.assertEqual(
            kwargs["media"],
            [{"path": str(self.world.letter_path), "mime_type": "image/png", "type": "image"}],
        )
        # 一度届けたら二度は言わない
        self.assertFalse(inject_diff_notifications(
            self.world.persona, self.world.manager, BUILDING,
            pipeline=self.pipeline, model_key=MODEL, detect_room=False,
        ))

    def test_labels_without_images_do_not_pass_media(self):
        """画像の無いラベルは従来どおり media を渡さない (既存の push 口の互換)。"""
        ensure_snapshot(self.pipeline, self.world.ctx())
        self.world.inventory.pop(1)
        inject_diff_notifications(
            self.world.persona, self.world.manager, BUILDING,
            pipeline=self.pipeline, model_key=MODEL, detect_room=False,
        )
        records = self.world.persona.sai_memory.pushed
        self.assertEqual(len(records), 1)
        self.assertNotIn("media", records[0][2])


class LedgerDeliveryMediaTest(_WorldTestBase):
    """台帳経路 (outbox) でも、加わったアイテムの画像が payload に載る。"""

    def test_outbox_payload_carries_the_label_media(self):
        from database.models import Base, ExecutionOutboxItem
        from saiverse.execution_ledger import ExecutionLedger

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.world.manager.execution_ledger = ExecutionLedger(session_factory=Session)

        registry = HeadSectionRegistry()
        registry.register(SelfViewSection())
        pipeline = HeadPipeline(registry=registry)
        pipeline.capture_all(self.world.ctx())

        self.world.inventory.append(_make_item(
            "uuid-letter", 300, "picture", "手紙の写真", "青い便箋。",
            is_open=True, file_path=self.world.letter_path,
        ))
        self.assertTrue(inject_diff_notifications(
            self.world.persona, self.world.manager, BUILDING,
            pipeline=pipeline, model_key=MODEL, detect_room=False,
        ))

        db = Session()
        try:
            rows = list(db.query(ExecutionOutboxItem).all())
        finally:
            db.close()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0].PAYLOAD_JSON)
        self.assertEqual(
            payload["content"], "インベントリにアイテム「手紙の写真」(item:300) が加わりました",
        )
        self.assertEqual(
            payload["media"],
            [{"path": str(self.world.letter_path), "mime_type": "image/png", "type": "image"}],
        )


# ---------------------------------------------------------------------------
# 5. 境界: 実際の prepare_context の出力
# ---------------------------------------------------------------------------


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class PrepareContextBoundaryTest(unittest.TestCase):
    """LLM に渡るメッセージ列に、ペルソナが自分について知っているべき事実が載る。

    部品ごとのテストが緑でも、組み上がった文脈から持ち物が消えていた
    (2026-09-06〜09-25)。ここは実際の ``prepare_context`` を通し、default の
    Section 群 (register_default_sections) で head を組み上げた出力を見る。
    世界の DB は一時ファイルの SQLite で、本番の ~/.saiverse には触らない。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self._tmp = tmp
        self.addCleanup(self._cleanup_tmp)
        home = Path(tmp.name)
        self.world = SelfViewWorld(home)

        # 世界の DB (AI 行: スペル有効 + 外見の画像)
        from database.models import AI, Base

        self.engine = create_engine(
            f"sqlite:///{home / 'world.db'}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        try:
            db.add(AI(
                AIID=PERSONA_ID, HOME_CITYID=1, AINAME=PERSONA_NAME,
                SYSTEMPROMPT="あなたはアイフィです。",
                APPEARANCE_IMAGE_PATH=str(self.world.appearance_path),
                SPELL_ENABLED=True,
            ))
            db.commit()
        finally:
            db.close()
        self.world.manager.SessionLocal = self.Session

        # 外見の読み (_get_persona_appearance_path) は database.session の
        # グローバルな SessionLocal を引くので、同じ一時 DB へ向ける。
        for target, value in (
            ("database.session.SessionLocal", self.Session),
            ("saiverse.persona_language.get_persona_language", lambda *a, **k: "ja"),
            ("saiverse_memory.adapter.Embedder", _DummyEmbedder),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # コア記憶を持つ本物の SAIMemory (一時ディレクトリ)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)
        from sai_memory.core_memory import add_core_memory
        from saiverse_memory import SAIMemoryAdapter

        persona_dir = home / "personas" / PERSONA_ID
        persona_dir.mkdir(parents=True, exist_ok=True)
        adapter = SAIMemoryAdapter(PERSONA_ID, persona_dir=persona_dir, resource_id=PERSONA_ID)
        self.addCleanup(adapter.close)
        with adapter._db_lock:
            add_core_memory(adapter.conn, "まはーの誕生日は1月14日")
        persona = self.world.persona
        persona.sai_memory = adapter
        persona.persona_system_instruction = "あなたはアイフィです。"
        persona.language = "ja"
        persona.buildings = {}

        # default の Section 群で組んだ pipeline を prepare_context に使わせる
        from sea.head_pipeline.sections import register_default_sections

        registry = HeadSectionRegistry()
        register_default_sections(registry)
        self.pipeline = HeadPipeline(registry=registry, store=None)
        patcher = patch(
            "sea.head_pipeline.integration.get_default_pipeline",
            return_value=self.pipeline,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup_tmp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _prepare(self):
        from sea.playbook_models import ContextRequirements
        from sea.runtime_context import prepare_context

        runtime = SimpleNamespace(manager=self.world.manager, session_lifecycle=None)
        return prepare_context(
            runtime, self.world.persona, BUILDING, user_input=None,
            requirements=ContextRequirements(history_depth=0, realtime_context=False),
            preview_only=True, model_key=MODEL,
        )

    def test_llm_messages_carry_what_the_persona_should_know_about_itself(self):
        messages = self._prepare()

        self_view = [
            m for m in messages
            if isinstance(m.get("metadata"), dict) and m["metadata"].get("__self_view__")
        ]
        self.assertEqual(len(self_view), 1, "自分の外見とインベントリのメッセージが一通")
        msg = self_view[0]
        self.assertEqual(msg["role"], "user")
        content = msg["content"]
        # インベントリのアイテム名 (全件)
        for item in self.world.inventory:
            self.assertIn(item["name"], content)
        # 外見の行と画像
        self.assertIn(f"[あなた自身（{PERSONA_NAME}）の外見]", content)
        media_paths = [m["path"] for m in msg["metadata"]["media"]]
        self.assertIn(str(self.world.appearance_path), media_paths)
        self.assertIn(str(self.world.pendant_path), media_paths)

        # 置き場: システムプロンプトの後ろ、独立したメッセージ
        system_indices = [i for i, m in enumerate(messages) if m.get("role") == "system"]
        self.assertTrue(system_indices)
        self.assertGreater(messages.index(msg), max(system_indices))
        system_text = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        self.assertNotIn("# あなた自身", system_text)

        # 既存の必須の事実も同じ組み上がりに載っている
        self.assertIn("あなたはアイフィです。", system_text)      # 人格
        self.assertIn("まはーの誕生日は1月14日", system_text)      # コア記憶
        self.assertIn("### 利用可能なスペル", system_text)         # スペル一覧


if __name__ == "__main__":
    unittest.main()
