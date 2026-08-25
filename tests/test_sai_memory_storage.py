import unittest

from sai_memory.memory.recall import semantic_recall, semantic_recall_groups
from sai_memory.memory.storage import (
    add_message,
    get_messages_for_chronicle,
    get_messages_from_id,
    get_messages_paginated,
    get_messages_with_persona_in_audience,
    get_or_create_thread,
    init_db,
    replace_message_embeddings,
    spell_group_spans,
)


class TestSpellGroupSpans(unittest.TestCase):
    """spell_group_spans — スペルの群が占める添字の区間 (規則の一点管理)。

    退場の境目 (保護境界・fold の切れ目・編纂チャンクの切れ目) を群の内側に
    落とさないための材料。ここを間違えると、ペルソナの窓が結果の行から始まり
    「唱えた記憶が無いのに結果だけある」= 記憶の捏造になる。
    """

    def test_origin_row_has_no_marker_of_its_own(self):
        """起点行 (最初の唱え) の spell_origin_id は NULL — それでも群に入る。

        起点は「自分の id が他の行の spell_origin_id として現れる」ことでしか
        識別できない。この非対称を落とすと区間が起点を含まず、境目が唱えと
        結果の間に落ちる。
        """
        rows = [
            ("a", None),
            ("s0", None),        # 唱え = 起点 (印は NULL)
            ("s1", "s0"),        # 結果を読んだ発話
        ]
        self.assertEqual(spell_group_spans(rows), [(1, 2)])

    def test_span_covers_interlopers_inside_the_group(self):
        """群の内側に割り込んだ行 (event_message / メタ判断) も区間に入る。

        区間は「最初のメンバーから最後のメンバーまで」であってメンバーの集合
        ではない — 割り込みごと 1 つの単位として扱わないと、そこが境目に
        なってしまう。
        """
        rows = [
            ("s0", None),        # 唱え
            ("ev", None),        # [システム通知] — 群のメンバーではない
            ("mj", None),        # committed なメタ判断
            ("s1", "s0"),        # 結果を読んだ発話
        ]
        self.assertEqual(spell_group_spans(rows), [(0, 3)])

    def test_single_member_group_is_not_returned(self):
        """1 件しか無い群 (幅ゼロ) は境目を割りようがないので返さない。"""
        self.assertEqual(spell_group_spans([("s0", None), ("a", None)]), [])
        # 印だけがあって起点行が列に居ない場合も、1 件なら幅ゼロ。
        self.assertEqual(spell_group_spans([("s1", "gone")]), [])

    def test_multiple_groups_are_returned_in_start_order(self):
        rows = [
            ("p0", None),
            ("p1", "p0"),
            ("x", None),
            ("q0", None),
            ("q1", "q0"),
            ("q2", "q0"),
        ]
        self.assertEqual(spell_group_spans(rows), [(0, 1), (3, 5)])

    def test_group_whose_origin_row_is_absent_still_spans_its_members(self):
        """起点行が列の外 (既に退場済み) でも、残ったメンバーは 1 つの群。"""
        rows = [("s1", "gone"), ("mid", None), ("s2", "gone")]
        self.assertEqual(spell_group_spans(rows), [(0, 2)])

    def test_empty_input(self):
        self.assertEqual(spell_group_spans([]), [])


class TestChronicleSelectCarriesLineMetadata(unittest.TestCase):
    """get_messages_for_chronicle が専用列 (spell_origin_id ほか) を運ぶこと。

    編纂チャンクの切れ目を群の内側に落とさない判定は spell_origin_id が要る。
    SELECT が 7 列だと列は常に None になり、判定が黙って無効化される。
    """

    def setUp(self):
        self.conn = init_db(":memory:")
        get_or_create_thread(self.conn, "thread-1", resource_id="resource-1")

    def tearDown(self):
        self.conn.close()

    def test_spell_origin_id_is_populated(self):
        origin = add_message(
            self.conn, thread_id="thread-1", role="model",
            content="唱える", pulse_id="p1",
        )
        add_message(
            self.conn, thread_id="thread-1", role="model",
            content="結果を読んだ発話", pulse_id="p2",
            spell_origin_id=origin, spell_seq=1,
        )
        rows = get_messages_for_chronicle(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0].spell_origin_id)
        self.assertEqual(rows[1].spell_origin_id, origin)
        # 同じ SELECT で運ばれる他の専用列も埋まる (origin_episode の
        # metadata フォールバックが主経路ではなくなった)。
        self.assertEqual(rows[0].pulse_id, "p1")
        self.assertEqual(rows[1].pulse_id, "p2")


class DummyEmbedder:
    def __init__(self, vector):
        self._vector = vector

    def embed(self, texts, **kwargs):
        return [self._vector for _ in texts]


class TestSAIMemoryStorage(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        get_or_create_thread(self.conn, "thread-1", resource_id="resource-1")
        get_or_create_thread(self.conn, "thread-2", resource_id="resource-2")

    def test_replace_message_embeddings_multiple_chunks(self):
        mid = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="chunked message",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid, ([1.0, 0.0], [0.0, 1.0]))

        results = semantic_recall(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=1,
            range_before=0,
            range_after=0,
            scope="thread",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, mid)

    def test_semantic_recall_groups_deduplicates_chunks(self):
        mid1 = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="first",
            resource_id="resource-1",
        )
        mid2 = add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="second",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid1, ([1.0, 0.0], [0.9, 0.1]))
        replace_message_embeddings(self.conn, mid2, ([0.0, 1.0],))

        groups = semantic_recall_groups(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=2,
            range_before=0,
            range_after=0,
            scope="thread",
        )
        self.assertEqual(len(groups), 2)
        seed_ids = [seed.id for seed, _, _ in groups]
        self.assertEqual(len(seed_ids), len(set(seed_ids)))

    def test_semantic_recall_groups_respects_exclude_ids(self):
        mid1 = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="first",
            resource_id="resource-1",
        )
        mid2 = add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="second",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid1, ([1.0, 0.0],))
        replace_message_embeddings(self.conn, mid2, ([0.0, 1.0],))

        groups = semantic_recall_groups(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=2,
            range_before=0,
            range_after=0,
            scope="thread",
            exclude_message_ids={mid1},
        )
        self.assertTrue(all(seed.id != mid1 for seed, _, _ in groups))

    def test_semantic_recall_groups_includes_context_messages(self):
        mid_user = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="first context",
            resource_id="resource-1",
        )
        mid_target = add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="target",
            resource_id="resource-1",
        )
        mid_after = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="after context",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid_user, ([0.5, 0.5],))
        replace_message_embeddings(self.conn, mid_target, ([1.0, 0.0],))
        replace_message_embeddings(self.conn, mid_after, ([0.4, 0.6],))

        groups = semantic_recall_groups(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=1,
            range_before=1,
            range_after=1,
            scope="thread",
        )
        self.assertEqual(len(groups), 1)
        _, bundle, _ = groups[0]
        bundle_ids = [msg.id for msg in bundle]
        self.assertIn(mid_user, bundle_ids)
        self.assertIn(mid_target, bundle_ids)
        self.assertIn(mid_after, bundle_ids)

    def test_get_messages_with_persona_in_audience_respects_thread_filter(self):
        add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="current thread",
            resource_id="resource-1",
            metadata={
                "audience": {"personas": ["friend"], "users": []},
                "tags": ["conversation"],
            },
        )
        add_message(
            self.conn,
            thread_id="thread-2",
            role="assistant",
            content="other thread",
            resource_id="resource-2",
            metadata={
                "audience": {"personas": ["friend"], "users": []},
                "tags": ["conversation"],
            },
        )

        messages = get_messages_with_persona_in_audience(
            self.conn,
            "friend",
            thread_id="thread-1",
            required_tags=["conversation"],
            limit=10,
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].thread_id, "thread-1")
        self.assertEqual(messages[0].content, "current thread")

    # ------------------------------------------------------------------
    # Phase 1 / Phase 3 段階 4-A・4-B: line metadata カラムの往復検証
    # ------------------------------------------------------------------

    def test_add_message_persists_line_metadata_and_paginated_round_trip(self):
        """add_message で line_role / line_id / scope / pulse_id を渡すと
        get_messages_paginated 経由で Message オブジェクトに正しく載る。

        - 4-A で Message データクラスに line_role / line_id / scope / pulse_id を追加し、
          context 経路の SELECT (`get_messages_paginated`) を 11 列拡張済み。
        - 4-B で sub_play の report_to_parent 書き込み経路が
          `_store_memory(line_role="main_line", scope="committed", pulse_id=...)` に変更。
        この round-trip テストは「DB の書き込み層 → 読み出し層」が line metadata を
        欠落させずに往復することを担保する。
        """
        mid = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="report to parent payload",
            resource_id="resource-1",
            line_role="main_line",
            line_id="line-uuid-aaa",
            scope="committed",
            pulse_id="pulse-uuid-bbb",
        )

        rows = get_messages_paginated(self.conn, "thread-1", page=0, page_size=10)
        target = next((m for m in rows if m.id == mid), None)
        self.assertIsNotNone(target, "inserted message must be returned by get_messages_paginated")
        self.assertEqual(target.line_role, "main_line")
        self.assertEqual(target.line_id, "line-uuid-aaa")
        self.assertEqual(target.scope, "committed")
        self.assertEqual(target.pulse_id, "pulse-uuid-bbb")

    def test_add_message_with_discardable_scope_excluded_from_paginated(self):
        """scope='discardable' のメッセージは get_messages_paginated から除外される。

        Phase 1.3 (Intent A v0.14): メタ判断分岐ターンが continue で消える経路。
        `get_messages_paginated` の `scope IS NULL OR scope != 'discardable'` 条件を
        4-A で 11 列 SELECT に拡張した際にも維持されていることを担保する。
        """
        mid_committed = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="committed message",
            resource_id="resource-1",
            line_role="main_line",
            scope="committed",
        )
        mid_discardable = add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="discardable meta-judgment turn",
            resource_id="resource-1",
            line_role="meta_judgment",
            scope="discardable",
        )

        rows = get_messages_paginated(self.conn, "thread-1", page=0, page_size=10)
        ids = {m.id for m in rows}
        self.assertIn(mid_committed, ids, "committed message must be returned")
        self.assertNotIn(mid_discardable, ids, "discardable message must be excluded")

    def test_add_message_legacy_row_has_null_line_metadata(self):
        """line metadata 引数なしで add_message を呼んだ legacy 行は line_role/line_id/pulse_id が None。

        scope は schema レベルで NOT NULL DEFAULT 'committed' なので
        DB 上 'committed' になる (Message.scope に正しく載る)。
        4-A の legacy 互換ロジック (line_role IS NULL → 'main_line' 扱い) はこの形を前提とする。
        """
        mid = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="legacy message without line metadata",
            resource_id="resource-1",
        )
        rows = get_messages_paginated(self.conn, "thread-1", page=0, page_size=10)
        target = next((m for m in rows if m.id == mid), None)
        self.assertIsNotNone(target)
        self.assertIsNone(target.line_role)
        self.assertIsNone(target.line_id)
        self.assertIsNone(target.pulse_id)
        self.assertEqual(target.scope, "committed")

    def test_get_messages_from_id_carries_line_metadata(self):
        """anchor 経由 (get_messages_from_id) でも line metadata が読み出せる。

        4-A で `get_messages_from_id` の SELECT も 11 列に拡張済み。
        metabolism anchor 経路 (sea/runtime.py:1559) はここを通る。
        """
        mid_anchor = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="anchor",
            resource_id="resource-1",
            line_role="main_line",
            scope="committed",
            pulse_id="pulse-anchor",
        )
        add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="after anchor",
            resource_id="resource-1",
            line_role="main_line",
            scope="committed",
            pulse_id="pulse-after",
        )
        rows = get_messages_from_id(self.conn, "thread-1", mid_anchor)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0].id, mid_anchor)
        self.assertEqual(rows[0].line_role, "main_line")
        self.assertEqual(rows[0].pulse_id, "pulse-anchor")
        self.assertEqual(rows[1].pulse_id, "pulse-after")


if __name__ == "__main__":
    unittest.main()
