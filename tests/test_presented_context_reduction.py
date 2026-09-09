"""提示の節約 — 会話以外の内容を Metabolism の瞬間だけ縮める。

正典: docs/intent/presented_context_reduction.md 設計 1 (用の済んだ操作通知を
下ろす / 現在地でない部屋の様子を縮める) と設計 2 (タイミングは Metabolism の
瞬間だけ)。対象は sai_memory/presented_reduction.py と、その適用点である
sea/runtime_context.list_presented_perception_blocks。

契約 (intent の不変条件の文面そのもの):

1. 操作通知 (スペルの増減・コア記憶などの操作のお知らせ) は Metabolism の後に
   提示から下りる。出来事の通知 (入退室・移動・机の押し出し) は下りない。
2. 現在地でない部屋の様子は一行に縮み、その部屋の画像も提示から外れる。
   現在地の部屋は縮まない。
3. 縮めた跡地には機構名義の一行が出る (黙って消さない)。
4. 台帳 (perception_buffer の行) も確定文面 (perception_batches.rendered_text)
   も無傷 — 変わるのは提示だけ。
5. 縮めた部屋へ戻ったら、全文と画像を見せ直す。
6. 提示が変わるのは Metabolism の瞬間だけ — 移動しても新しい知覚が積まれても、
   既に提示に出ているブロックの文面は変わらない。
7. 一方向 — 二度目の Metabolism でも、縮めたものは戻らない。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from sai_memory.perception_buffer import (
    PERCEPTION_OMISSION_HEADER,
    create_consumption_batch,
    format_perception_message,
    init_perception_buffer_table,
    list_pending,
    list_presented_batches,
    push_perception,
    reduce_perceptions,
)
from sai_memory.presented_reduction import (
    advance_notice_cutoff,
    get_notice_cutoff,
    has_pending_reductions,
    is_droppable_notice,
    mark_presentation_reductions,
)
from sai_memory.room_state import (
    LABEL_KIND_META_KEY,
    ROOM_STATE_KIND,
    batch_room_states,
    build_room_state_push,
    collect_batch_room_states,
    reclaim_pending_perceptions,
    render_pending_room_states,
    render_room_full,
)
from sea.runtime_context import list_presented_perception_blocks

#: Chronicle 有効相当 (lifecycle 無し = 判定不能 → 有効側に倒す)。
_RUNTIME = SimpleNamespace(session_lifecycle=None)


def _bundle(building: str, name: str, *, image: str | None = None) -> dict:
    """開いたドキュメント 1 個 (+ 任意の内装画像) を持つ部屋の束。"""
    label = f"[item:{building}] [Document] 覚え書き"
    packages = [{
        "key": f"item:{building}", "family": "item", "label": label,
        "lines": [label, "(Open)", "```", f"{name} のノート", "```"],
        "media": [], "state": "open",
    }]
    if image:
        packages.append({
            "key": "building:image", "family": "interior", "label": "[内装]",
            "lines": ["[内装]"],
            "media": [{"path": image, "mime_type": "image/png"}],
            "state": None,
        })
    return {
        "building_id": building, "building_name": name, "packages": packages,
    }


class PresentedReductionTestBase(unittest.TestCase):
    """生の conn に知覚を積んで消費し、提示の組成を回す土台。"""

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

    # ---- 積む ----

    def _push_room(self, building: str, name: str, *, image: str | None = None):
        payload = build_room_state_push(building, _bundle(building, name, image=image))
        return push_perception(
            self.conn, ROOM_STATE_KIND, payload["content"],
            media=payload["media"], metadata=payload["metadata"],
        )

    def _push_head_mutation(self, section: str, body: str):
        return push_perception(
            self.conn, "head_mutation", body,
            reduce_key=f"head_mutation:{section}",
            metadata=json.dumps({"section": section}, ensure_ascii=False),
        )

    def _push_world_state(self, body: str, label_kind: str | None = None):
        metadata = (
            json.dumps({LABEL_KIND_META_KEY: label_kind}, ensure_ascii=False)
            if label_kind else None
        )
        return push_perception(self.conn, "world_state", body, metadata=metadata)

    # ---- 消費 (adapter.flush_perception_buffer_payload と同じ順序) ----

    def _flush(self):
        items = list_pending(self.conn)
        if not items:
            return None
        reduced = reclaim_pending_perceptions(reduce_perceptions(items))
        reduced = render_pending_room_states(self.conn, reduced)
        text = format_perception_message(reduced)
        room_state_json = collect_batch_room_states(reduced, text)
        media: list = []
        seen: set = set()
        for item in reduced:
            for m in item.media_list():
                path = m.get("path") if isinstance(m, dict) else None
                if path and path in seen:
                    continue
                if path:
                    seen.add(path)
                media.append(m)
        self.clock += 10
        return create_consumption_batch(
            self.conn, [it.id for it in items], consumed_at=self.clock,
            rendered_text=text, media=media or None,
            room_state_json=room_state_json,
        )

    # ---- 節目と提示 ----

    def _metabolism(self):
        """Metabolism の瞬間の縮みの記録 (SessionLifecycle が呼ぶのと同じ一枚)。"""
        result = mark_presentation_reductions(self.conn)
        self.conn.commit()
        return result

    def _blocks(self):
        return list_presented_perception_blocks(
            _RUNTIME, self.persona, [], raise_on_error=True,
        )

    def _text(self):
        return "\n".join(b["content"] for b in self._blocks())

    def _media_paths(self):
        paths = []
        for block in self._blocks():
            for m in (block.get("metadata") or {}).get("media") or []:
                paths.append(m.get("path"))
        return paths


class NoticeDropTest(PresentedReductionTestBase):
    """設計 1 — 用の済んだ操作通知を提示から下ろす。"""

    def setUp(self):
        super().setUp()
        self._push_head_mutation("core_memory", "コア記憶を書き換えました\n\n## コア記憶\n- 甘いものが好き")
        self._push_world_state("スペル foo (foo) が使えるようになりました", "spell_added")
        self._push_world_state("エリスがやって来ました")  # 出来事 (型なし) = 対象外
        self.batch_id = self._flush()

    def test_notices_are_presented_until_metabolism(self):
        text = self._text()
        self.assertIn("コア記憶を書き換えました", text)
        self.assertIn("スペル foo (foo) が使えるようになりました", text)
        self.assertIn("エリスがやって来ました", text)

    def test_operation_notices_drop_after_metabolism(self):
        self._metabolism()
        text = self._text()
        self.assertNotIn("コア記憶を書き換えました", text)
        self.assertNotIn("スペル foo (foo) が使えるようになりました", text)

    def test_event_notices_stay(self):
        self._metabolism()
        self.assertIn("エリスがやって来ました", self._text())

    def test_omission_mark_shows_the_count(self):
        self._metabolism()
        text = self._text()
        self.assertIn(PERCEPTION_OMISSION_HEADER, text)
        self.assertIn("操作のお知らせ 2 件", text)

    def test_ledger_rows_are_untouched(self):
        before = self.conn.execute(
            "SELECT id, kind, content, metadata FROM perception_buffer ORDER BY id"
        ).fetchall()
        self._metabolism()
        after = self.conn.execute(
            "SELECT id, kind, content, metadata FROM perception_buffer ORDER BY id"
        ).fetchall()
        self.assertEqual(before, after)

    def test_batch_rendered_text_is_untouched(self):
        before = self.conn.execute(
            "SELECT rendered_text FROM perception_batches WHERE id = ?",
            (self.batch_id,),
        ).fetchone()[0]
        self._metabolism()
        self._blocks()
        after = self.conn.execute(
            "SELECT rendered_text FROM perception_batches WHERE id = ?",
            (self.batch_id,),
        ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertIn("コア記憶を書き換えました", after)

    def test_new_notices_after_the_metabolism_survive(self):
        self._metabolism()
        self._push_head_mutation("desk", "机を更新しました")
        self._flush()
        self.assertIn("机を更新しました", self._text())
        # 前の節目までのものは下りたまま (一方向)。
        self.assertNotIn("コア記憶を書き換えました", self._text())

    def test_second_metabolism_does_not_bring_them_back(self):
        self._metabolism()
        first = self._text()
        self._metabolism()
        self.assertEqual(first, self._text())


class NoticeTargetSetTest(unittest.TestCase):
    """下ろす通知の集合 — 何を対象にし、何を対象外にしたか。"""

    def _meta(self, label_kind):
        return json.dumps({LABEL_KIND_META_KEY: label_kind}, ensure_ascii=False)

    def test_head_mutation_is_always_droppable(self):
        self.assertTrue(is_droppable_notice("head_mutation", None))

    def test_spell_labels_are_droppable(self):
        for kind in (
            "spell_added", "spell_removed",
            "spell_system_enabled", "spell_system_disabled",
        ):
            self.assertTrue(
                is_droppable_notice("world_state", self._meta(kind)), kind,
            )

    def test_event_labels_are_not_droppable(self):
        for kind in (
            "building_changed", "occupant_entered", "occupant_left",
            "desk_evicted", "playbook_added", "facilities_changed",
        ):
            self.assertFalse(
                is_droppable_notice("world_state", self._meta(kind)), kind,
            )

    def test_untyped_and_other_kinds_are_not_droppable(self):
        self.assertFalse(is_droppable_notice("world_state", None))
        self.assertFalse(is_droppable_notice("world_state", "not json"))
        self.assertFalse(is_droppable_notice(ROOM_STATE_KIND, None))
        self.assertFalse(is_droppable_notice("feed", None))
        self.assertFalse(is_droppable_notice(None, None))

    def test_spell_list_section_types_its_notifications(self):
        """書き手 (spell_list Section) が実際に型を載せているか。"""
        from sea.head_pipeline.sections.spell_list import (
            SpellEntry,
            SpellListSection,
            SpellListSnapshot,
        )

        def _snapshot(names):
            return SpellListSnapshot(
                enabled=True,
                entries=tuple(
                    SpellEntry(
                        name=n, display_name=n, description="d",
                        parameters_json="{}", addon_key=None, visible=True,
                    ) for n in names
                ),
                addon_manifests=(),
                registered_names=frozenset(names),
            )

        labels = SpellListSection().diff_to_notifications(
            _snapshot(["a"]), _snapshot(["a", "b"]),
        )
        self.assertTrue(labels)
        for label in labels:
            self.assertTrue(
                is_droppable_notice(
                    "world_state",
                    json.dumps(label.metadata, ensure_ascii=False),
                ),
                label.kind,
            )


class RoomShrinkTest(PresentedReductionTestBase):
    """設計 1 の「用が乏しい」— 現在地でない部屋の様子を縮める。"""

    def setUp(self):
        super().setUp()
        self._push_room("b1", "工房", image="/img/b1.png")
        self.batch_a = self._flush()
        self._push_room("b2", "書斎", image="/img/b2.png")
        self.batch_b = self._flush()

    def test_both_rooms_are_full_before_metabolism(self):
        text = self._text()
        self.assertIn("# 「工房」の様子", text)
        self.assertIn("工房 のノート", text)
        self.assertIn("書斎 のノート", text)
        self.assertEqual(
            sorted(self._media_paths()), ["/img/b1.png", "/img/b2.png"],
        )

    def test_other_room_shrinks_and_current_room_stays(self):
        self._metabolism()
        text = self._text()
        self.assertNotIn("工房 のノート", text)          # 離れた部屋 = 縮む
        self.assertIn("# 「工房」の様子", text)           # 部屋名は残す
        self.assertIn(PERCEPTION_OMISSION_HEADER, text)  # 省略があったと分かる
        self.assertIn("書斎 のノート", text)             # 現在地 = そのまま

    def test_shrunk_room_media_leaves_the_presentation(self):
        self._metabolism()
        self.assertEqual(self._media_paths(), ["/img/b2.png"])

    def test_ledger_and_rendered_text_are_untouched(self):
        rows_before = self.conn.execute(
            "SELECT id, content, media FROM perception_buffer ORDER BY id"
        ).fetchall()
        text_before = self.conn.execute(
            "SELECT rendered_text FROM perception_batches WHERE id = ?",
            (self.batch_a,),
        ).fetchone()[0]
        self._metabolism()
        self._blocks()
        self.assertEqual(rows_before, self.conn.execute(
            "SELECT id, content, media FROM perception_buffer ORDER BY id"
        ).fetchall())
        self.assertEqual(text_before, self.conn.execute(
            "SELECT rendered_text FROM perception_batches WHERE id = ?",
            (self.batch_a,),
        ).fetchone()[0])
        self.assertIn("工房 のノート", text_before)

    def test_shrunk_entry_leaves_the_room_chain(self):
        """縮めた記帳は束を持たない = 土台にならない (連なりの外)。"""
        self._metabolism()
        entries = batch_room_states(self.conn.execute(
            "SELECT room_state_json FROM perception_batches WHERE id = ?",
            (self.batch_a,),
        ).fetchone()[0])
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["shrunk"])
        self.assertNotIn("snapshot", entries[0])

    def test_returning_shows_the_full_text_and_media_again(self):
        self._metabolism()
        # 工房へ戻る = 入室で部屋の様子が積まれ、消費で描かれる。
        self._push_room("b1", "工房", image="/img/b1.png")
        self._flush()
        text = self._text()
        self.assertIn("工房 のノート", text)              # 全文が戻る
        self.assertNotIn("前回見たときから変わっていません", text)  # 差分ではない
        self.assertIn("/img/b1.png", self._media_paths())  # 画像も戻る

    def test_second_metabolism_does_not_unshrink(self):
        self._metabolism()
        shrunk = self._text()
        # 工房へ戻ってからもう一度 Metabolism — 縮めた古い記帳は戻らない。
        self._push_room("b1", "工房", image="/img/b1.png")
        self._flush()
        self._metabolism()
        text = self._text()
        self.assertIn("# 「工房」の様子", text)
        self.assertEqual(text.count("工房 のノート"), 1)  # 戻った回の一枚だけ
        self.assertIn(PERCEPTION_OMISSION_HEADER, shrunk)


class RoomShrinkGuardTest(PresentedReductionTestBase):
    """現在地が決まらない回・記録が一枚だけの回は縮めない。"""

    def test_single_room_is_never_shrunk(self):
        self._push_room("b1", "工房")
        self._flush()
        self._metabolism()
        self.assertIn("工房 のノート", self._text())

    def test_no_room_record_leaves_everything_alone(self):
        self._push_world_state("エリスがやって来ました")
        self._flush()
        result = self._metabolism()
        self.assertEqual(result["rooms_shrunk"], 0)
        self.assertIn("エリスがやって来ました", self._text())


class RoomShrinkAmbiguityTest(PresentedReductionTestBase):
    """一つのバッチの記帳が同じ文面を指すなら縮めない (曖昧なら触らない)。

    提示の差し替えは確定文面の**文字列一致**で位置を決めるので、残る側 (現在地)
    と縮める側のブロック文面が同じだと、残すべき方の位置を縮めうる
    (ローカルレビュー指摘 2026-09-10)。

    ⚠ この並びは今の消費経路では作れない — ``reclaim_pending_perceptions`` の
    規則 2 (「様子は最後の一枚だけ残す」2026-09-07 まはー裁定) により、1 バッチ
    の部屋の記帳は必ず一つになる。だから記帳を**直接組んで**確かめる: 差し替えが
    文字列一致である以上、記帳が二つ並ぶ形に対しても安全側で止まることを固定
    しておく (供給側の規則が変わった日に静かに壊れないため)。
    """

    def _install_two_room_batch(self, *, identical: bool) -> int:
        """現在地 (書斎) とよその部屋の記帳を一つのバッチへ手で組む。

        ``identical=True`` は二つの記帳が**同じ一つの文面**を指す形 (確定文面に
        その文面は一度しか出てこない) — 縮めると現在地の様子が消える。
        """
        self._push_room("b2", "書斎", image="/img/b2.png")
        batch_id = self._flush()
        row = self.conn.execute(
            "SELECT rendered_text, room_state_json FROM perception_batches "
            "WHERE id = ?", (batch_id,),
        ).fetchone()
        text, entries = row[0], json.loads(row[1])
        current = entries[0]
        other = json.loads(json.dumps(current))  # 深い写し
        other["key"] = "building:b1"
        other["snapshot"]["building_id"] = "b1"
        other["snapshot"]["building_name"] = "工房"
        if not identical:
            other["block"] = current["block"].replace("書斎", "工房")
            text = other["block"] + "\n\n" + text
        self.conn.execute(
            "UPDATE perception_batches SET rendered_text = ?, room_state_json = ? "
            "WHERE id = ?",
            (text, json.dumps([other, current], ensure_ascii=False), batch_id),
        )
        self.conn.commit()
        return batch_id

    def test_identical_block_text_is_left_alone(self):
        self._install_two_room_batch(identical=True)
        result = self._metabolism()
        self.assertEqual(result["rooms_shrunk"], 0)
        # 現在地の様子がそのまま見えている (縮めていたらここが消えていた)。
        self.assertIn("書斎 のノート", self._text())
        self.assertIn("/img/b2.png", self._media_paths())

    def test_distinct_block_text_still_shrinks(self):
        """対照 — 文面が違えば同じ並びでも従来どおり縮む (見送りが効きすぎない)。"""
        self._install_two_room_batch(identical=False)
        result = self._metabolism()
        self.assertEqual(result["rooms_shrunk"], 1)
        text = self._text()
        self.assertNotIn("工房 のノート", text)           # よその部屋は縮んだ
        self.assertIn(PERCEPTION_OMISSION_HEADER, text)   # 省略があったと分かる
        self.assertIn("書斎 のノート", text)              # 現在地は無傷


class RoomShrinkMediaPairingTest(PresentedReductionTestBase):
    """文字と絵ははぐれない — 本文を差し替えられない回は画像も外さない。

    room_state_packages §1 の保証。差し替えに失敗した回に画像だけ外すと、本文に
    部屋の様子が全文で残ったまま絵が消え、省略の表示も出ない。
    """

    def test_media_stays_when_the_block_cannot_be_replaced(self):
        self._push_room("b1", "工房", image="/img/b1.png")
        batch_a = self._flush()
        self._push_room("b2", "書斎", image="/img/b2.png")
        self._flush()
        self._metabolism()
        self.assertEqual(self._media_paths(), ["/img/b2.png"])  # 縮んだ状態

        # 確定文面と記帳の食い違いを作る (台帳の行が読めない・文面が入れ替わった
        # 回に相当)。本文の差し替えは空振りするので、画像も外れてはいけない。
        entries = batch_room_states(self.conn.execute(
            "SELECT room_state_json FROM perception_batches WHERE id = ?",
            (batch_a,),
        ).fetchone()[0])
        for entry in entries:
            if entry.get("shrunk"):
                entry["block"] = "この文面は確定文面のどこにも無い"
        self.conn.execute(
            "UPDATE perception_batches SET room_state_json = ? WHERE id = ?",
            (json.dumps(entries, ensure_ascii=False), batch_a),
        )
        self.conn.commit()

        text = self._text()
        self.assertIn("工房 のノート", text)                  # 本文は全文のまま
        self.assertIn("/img/b1.png", self._media_paths())     # 絵も残る


class ReductionIdempotenceTest(PresentedReductionTestBase):
    """二連続の Metabolism は安全 (冪等・一方向)。

    修正 1 で「会話が畳めない回」からも縮みが走るようになり、同じ提示に対して
    縮みが二度続けて走る並びが現実に出る。
    """

    def test_back_to_back_metabolisms_change_nothing_the_second_time(self):
        self._push_room("b1", "工房", image="/img/b1.png")
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        self._push_room("b2", "書斎")
        self._flush()

        first = self._metabolism()
        text_after_first = self._text()
        media_after_first = self._media_paths()

        second = self._metabolism()
        self.assertEqual(second["rooms_shrunk"], 0)          # 縮める先はもう無い
        self.assertEqual(second["notices_through"], first["notices_through"])
        self.assertEqual(self._text(), text_after_first)     # 提示も動かない
        self.assertEqual(self._media_paths(), media_after_first)


class MetabolismEntryPointsTest(unittest.TestCase):
    """修正 1 — 会話が畳めない回 (知覚だけが上限超え) でも縮みが走る。

    「会話は小さく知覚だけが大きい」は縮みが一番効くべき状態なのに、退場計画が
    空 (``plan.is_empty``) の回は縮みへ到達していなかった。head を描き直して
    **から** 縮みへ入る順序も一緒に固定する — 操作通知は head が今の状態を
    見せるまで唯一の情報源なので、先に下ろすと一拍だけ「通知も head も古い」
    瞬間ができる (intent 設計 1 の合図の定義)。
    """

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from database.models import Base

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.manager = SimpleNamespace(
            SessionLocal=sessionmaker(bind=engine),
            event_scheduler=None,
            meta_layer=SimpleNamespace(
                _load_judgment_config=lambda persona: {
                    "keep_cache_alive": True, "cache_threshold_ratio": 0.3,
                },
            ),
            personas={},
        )

    def _run_with_empty_plan(self):
        from sea.eviction_plan import EvictionPlan, Watermarks
        from sea.session_lifecycle import SessionLifecycle
        from sea.session_window import SessionWindow

        lifecycle = SessionLifecycle(
            SimpleNamespace(run_cache_keepalive=lambda pid, mk=None: None),
            self.manager,
        )
        persona = SimpleNamespace(
            persona_id="p1", model="model-a", current_building_id="room",
        )
        msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(4)]
        window = SessionWindow(
            anchor_id="m0", raw=list(msgs), presented=list(msgs), folds=[],
        )
        order: list = []
        with mock.patch.object(
                    lifecycle, "is_chronicle_enabled_for_persona",
                    return_value=False,
                ), \
                mock.patch.object(
                    lifecycle, "get_presented_window", return_value=window,
                ), \
                mock.patch(
                    "sea.session_lifecycle.plan_eviction",
                    return_value=EvictionPlan(),
                ), \
                mock.patch(
                    "saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                    lambda persona, manager, model_key=None: order.append("head"),
                ), \
                mock.patch.object(
                    lifecycle, "_reduce_presented_perceptions",
                    lambda p: order.append("shrink"),
                ):
            status = lifecycle._run_metabolism_locked(
                persona, "room", window, Watermarks(target=2_000, high=4_000),
                chronicle_force=True,
            )
        return status, order

    def test_empty_plan_still_reaches_the_reduction(self):
        status, order = self._run_with_empty_plan()
        self.assertEqual(status, "nothing")  # 戻り値は従来のまま
        self.assertEqual(order, ["head", "shrink"])


class ReductionTimingTest(PresentedReductionTestBase):
    """設計 2 — 提示が変わるのは Metabolism の瞬間だけ。"""

    def test_moving_and_pushing_do_not_rewrite_the_presentation(self):
        self._push_room("b1", "工房", image="/img/b1.png")
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        before = self._text()

        # 移動して新しい知覚を積む — 節目ではないので既存の提示は動かない。
        self._push_room("b2", "書斎")
        self._push_world_state("スペル foo (foo) が使えるようになりました", "spell_added")
        self._flush()
        after = self._text()

        self.assertTrue(after.startswith(before))
        self.assertIn("工房 のノート", after)
        self.assertIn("コア記憶を書き換えました", after)

    def test_repeated_composition_is_stable(self):
        self._push_room("b1", "工房")
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        self._push_room("b2", "書斎")
        self._flush()
        self._metabolism()
        first = self._text()
        self.assertEqual(first, self._text())
        self.assertEqual(first, self._text())


class NoticeCutoffTest(unittest.TestCase):
    """操作通知の境界は一方向にしか進まない。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)

    def test_starts_at_zero(self):
        self.assertEqual(get_notice_cutoff(self.conn), 0)

    def test_advances_forward_only(self):
        self.assertEqual(advance_notice_cutoff(self.conn, 5), 5)
        self.assertEqual(advance_notice_cutoff(self.conn, 3), 5)
        self.assertEqual(advance_notice_cutoff(self.conn, 9), 9)
        self.assertEqual(get_notice_cutoff(self.conn), 9)

    def test_does_not_disturb_the_drop_cutoff(self):
        from sai_memory.perception_buffer import (
            advance_presentation_cutoff,
            get_presentation_cutoff,
        )
        advance_presentation_cutoff(self.conn, 4)
        advance_notice_cutoff(self.conn, 7)
        self.assertEqual(get_presentation_cutoff(self.conn), 4)
        self.assertEqual(get_notice_cutoff(self.conn), 7)

    def test_missing_table_reads_as_zero(self):
        self.conn.execute("DROP TABLE perception_presentation")
        self.assertEqual(get_notice_cutoff(self.conn), 0)


class ReductionWiringTest(PresentedReductionTestBase):
    """配線の契約 — 縮みの失敗は「縮めない」へ倒れ、知覚の喪失に化けない。"""

    def test_reduce_failure_degrades_to_full_size_not_empty(self):
        # 縮みの適用が失敗しても、知覚は縮まずに全文で提示される (空にならない)。
        # ローカルレビュー指摘 2026-09-10: ここで送出すると外側の受けが知覚を
        # 丸ごと空にする — その向きを禁じる契約。
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        self._metabolism()
        with mock.patch(
            "sai_memory.presented_reduction.reduce_presented_batches",
            side_effect=sqlite3.DatabaseError("boom"),
        ):
            blocks = list_presented_perception_blocks(
                _RUNTIME, self.persona, [], raise_on_error=False,
            )
        self.assertTrue(blocks)
        self.assertIn(
            "コア記憶を書き換えました",
            "\n".join(b["content"] for b in blocks),
        )

    def test_lifecycle_wiring_not_ready_and_rollback(self):
        # SessionLifecycle._reduce_presented_perceptions の配線:
        # 未 ready なら何も書かず、失敗したら rollback して送出しない。
        from sea.session_lifecycle import SessionLifecycle

        events: list = []
        persona = SimpleNamespace(
            persona_id="p1",
            sai_memory=SimpleNamespace(
                is_ready=lambda: False, _db_lock=threading.RLock(),
                conn=SimpleNamespace(commit=lambda: events.append("commit")),
            ),
        )
        SessionLifecycle._reduce_presented_perceptions(SimpleNamespace(), persona)
        self.assertEqual(events, [])

        class _FailingConn:
            def commit(self):
                events.append("commit")

            def rollback(self):
                events.append("rollback")

            def execute(self, *args, **kwargs):
                raise sqlite3.DatabaseError("boom")

        persona2 = SimpleNamespace(
            persona_id="p2",
            sai_memory=SimpleNamespace(
                is_ready=lambda: True, _db_lock=threading.RLock(),
                conn=_FailingConn(),
            ),
        )
        SessionLifecycle._reduce_presented_perceptions(SimpleNamespace(), persona2)
        self.assertEqual(events, ["rollback"])


class ReductionAccountingTest(PresentedReductionTestBase):
    """縮んだ提示は、測る側と送る側で同じ一枚 (組成規則の二枚目を作らない)。"""

    def test_presented_chars_follow_the_reduction(self):
        self._push_room("b1", "工房", image="/img/b1.png")
        self._flush()
        self._push_room("b2", "書斎")
        self._flush()
        before = sum(len(b["content"]) for b in self._blocks())
        self._metabolism()
        after = sum(len(b["content"]) for b in self._blocks())
        self.assertLess(after, before)
        # 縮んだ本文がそのまま勘定の対象 (提示ブロックの content が正)。
        full = render_room_full(_bundle("b1", "工房", image="/img/b1.png"))
        self.assertNotIn(full, "\n".join(b["content"] for b in self._blocks()))

    def test_batches_stay_presented(self):
        """縮みは提示から**下ろす**のではない — バッチは提示に残る。"""
        self._push_room("b1", "工房")
        self._flush()
        self._push_room("b2", "書斎")
        self._flush()
        self._metabolism()
        self.assertEqual(len(list_presented_batches(self.conn)), 2)


class HasPendingReductionsTest(PresentedReductionTestBase):
    """修正 2 の前提条件 — 「いま呼んだら何か縮むか」を安い読みだけで答える。

    入口の門はこれが True のときだけ縮みへ入る。False の回に head を描き直すと、
    合計が上限を超えている限り毎ターン前置きが書き変わって、プロンプトキャッシュ
    の前方一致が無駄に割れ続ける。
    """

    def _pending(self):
        return has_pending_reductions(self.conn)

    def test_empty_ledger_has_nothing_to_reduce(self):
        self.assertFalse(self._pending())

    def test_droppable_notice_makes_it_true_until_the_metabolism(self):
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        self.assertTrue(self._pending())
        self._metabolism()
        self.assertFalse(self._pending())

    def test_event_notice_alone_is_not_enough(self):
        # 出来事の通知 (型なし) は下ろす対象ではない。
        self._push_world_state("エリスがやって来ました")
        self._flush()
        self.assertFalse(self._pending())

    def test_room_away_makes_it_true_until_the_metabolism(self):
        self._push_room("b1", "工房", image="/img/b1.png")
        self._flush()
        self._push_room("b2", "書斎")   # 現在地は b2 になる
        self._flush()
        self.assertTrue(self._pending())
        self._metabolism()
        self.assertFalse(self._pending())

    def test_current_room_alone_is_not_enough(self):
        self._push_room("b1", "工房", image="/img/b1.png")
        self._flush()
        self.assertFalse(self._pending())

    def test_new_notice_after_the_metabolism_makes_it_true_again(self):
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        self._metabolism()
        self.assertFalse(self._pending())
        self._push_head_mutation("desk", "机を更新しました")
        self._flush()
        self.assertTrue(self._pending())

    def test_read_failure_is_raised_not_swallowed(self):
        # 「縮めるものが無い」の False に化かさない (門が見送りとして記録する)。
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        with mock.patch(
            "sai_memory.perception_buffer.list_presented_batches",
            side_effect=sqlite3.DatabaseError("boom"),
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                self._pending()


class MetabolismGateReductionTest(PresentedReductionTestBase):
    """修正 2 — 入口の門 (自動発火) から縮みが走る。

    4 経路 (自動 / 手動 / 冷えた先回り / 非常畳み) はどれも「会話の行が残す量
    以下」だと Metabolism の本体へ入らずに引き返す。会話がずっと小さく知覚だけが
    大きいペルソナは本体の縮み (``plan.is_empty`` の回) に一度も届かないので、
    門が縮みの唯一の機会になる。
    """

    def setUp(self):
        super().setUp()
        from sea.eviction_plan import Watermarks
        from sea.session_lifecycle import SessionLifecycle
        from sea.session_window import SessionWindow

        self.lifecycle = SessionLifecycle(
            SimpleNamespace(run_cache_keepalive=lambda pid, mk=None: None),
            SimpleNamespace(personas={}),
        )
        self.persona.history_manager = SimpleNamespace()
        self.persona.current_building_id = "b2"
        # 会話は小さい (残す量以下) が、合計は上限超え。
        msgs = [{"id": "m0", "content": "x" * 100}]
        self.window = SessionWindow(
            anchor_id="m0", raw=list(msgs), presented=list(msgs), folds=[],
        )
        self.watermarks = Watermarks(target=2_000, high=4_000)
        self.order: list = []

    def _run_gate(self):
        """自動経路 (maybe_run_metabolism) の門を、実データの上で一度回す。"""
        with mock.patch.object(
                    self.lifecycle, "load_anchor_entry",
                    return_value={"anchor_id": "m0"},
                ), \
                mock.patch.object(
                    self.lifecycle, "get_metabolism_watermarks",
                    return_value=self.watermarks,
                ), \
                mock.patch.object(
                    self.lifecycle, "get_presented_window",
                    return_value=self.window,
                ), \
                mock.patch.object(
                    self.lifecycle, "presented_chars", return_value=9_000,
                ), \
                mock.patch.object(
                    self.lifecycle, "run_metabolism",
                    side_effect=AssertionError("the gate must not run metabolism"),
                ), \
                mock.patch(
                    "saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                    lambda persona, manager, model_key=None: self.order.append(
                        ("head", model_key),
                    ),
                ):
            self.lifecycle.maybe_run_metabolism(
                self.persona, "b2", model_key="test-model",
            )

    def test_gate_reduces_when_the_conversation_cannot_be_folded(self):
        self._push_room("b1", "工房", image="/img/b1.png")
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        self._push_room("b2", "書斎")
        self._flush()

        before = self._text()
        self.assertIn("工房 のノート", before)
        self.assertIn("コア記憶を書き換えました", before)

        self._run_gate()

        after = self._text()
        self.assertNotIn("工房 のノート", after)              # いない部屋は縮んだ
        self.assertNotIn("コア記憶を書き換えました", after)   # 操作通知は下りた
        self.assertIn(PERCEPTION_OMISSION_HEADER, after)      # 黙って消していない
        self.assertIn("書斎 のノート", after)                 # 現在地は全文のまま
        self.assertNotIn("/img/b1.png", self._media_paths())
        # head を描き直して**から** 縮んだ (順序は入れ替えられない)。
        self.assertEqual(self.order, [("head", "test-model")])

    def test_second_pass_skips_the_head_rebuild(self):
        self._push_room("b1", "工房")
        self._push_head_mutation("core_memory", "コア記憶を書き換えました")
        self._flush()
        self._push_room("b2", "書斎")
        self._flush()

        self._run_gate()
        text_after_first = self._text()
        self.order.clear()

        # まだ超過は続いている (presented_chars は 9,000 のまま) が、縮める
        # ものはもう無い — head は描き直さず、提示も動かない。
        self._run_gate()
        self.assertEqual(self.order, [])
        self.assertEqual(self._text(), text_after_first)

    def test_nothing_to_reduce_writes_nothing(self):
        # 現在地の部屋だけ・出来事の通知だけ = 縮める先が無い超過。
        self._push_room("b2", "書斎")
        self._push_world_state("エリスがやって来ました")
        self._flush()
        before_batches = self.conn.execute(
            "SELECT id, rendered_text, room_state_json FROM perception_batches "
            "ORDER BY id"
        ).fetchall()

        self._run_gate()

        # 門そのものには届いている (超過の旗が立っている) — その上で何も書かない。
        self.assertIn("p1", self.lifecycle._perception_over_budget_warned)
        self.assertEqual(self.order, [])                 # head も描き直さない
        self.assertEqual(get_notice_cutoff(self.conn), 0)  # 境界も進まない
        self.assertEqual(
            self.conn.execute(
                "SELECT id, rendered_text, room_state_json FROM "
                "perception_batches ORDER BY id"
            ).fetchall(),
            before_batches,
        )

    def test_gate_never_calls_an_llm(self):
        """門は縮めても本体 (run_metabolism = 編纂・スルース) へ入らない。"""
        self._push_room("b1", "工房")
        self._flush()
        self._push_room("b2", "書斎")
        self._flush()
        # run_metabolism は _run_gate の中で AssertionError を投げるように
        # 差し替えてある — 呼ばれれば落ちる。
        self._run_gate()
        self.assertTrue(get_notice_cutoff(self.conn) > 0)


if __name__ == "__main__":
    unittest.main()
