"""Tests for entity_extractor module."""

import json
import unittest
from unittest.mock import MagicMock, patch

from sai_memory.memory.entity_extractor import (
    ExtractedEntity,
    ExtractionFailed,
    _build_extraction_prompt,
    _parse_extraction_response,
    extract_entities,
    reflect_to_memopedia,
)
from sai_memory.memory.storage import Message


def _parse_entities(response):
    """抽出部分だけを見るテストのための細口 (B2 欄は別クラスで見る)。"""
    return _parse_extraction_response(response).entities


class TestParseExtractionResponse(unittest.TestCase):
    """Test JSON parsing of LLM responses."""

    def test_valid_json(self):
        response = json.dumps({
            "entities": [
                {"name": "エイド", "category": "people", "summary": "ソフィーの一人であるAI", "notes": ["まはーが作ったAI"]},
                {"name": "SAIVerse", "category": "terms", "summary": "AIプラットフォーム", "notes": ["開発中のシステム"]},
            ]
        }, ensure_ascii=False)
        result = _parse_entities(response)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "エイド")
        self.assertEqual(result[0].category, "people")
        self.assertEqual(result[0].summary, "ソフィーの一人であるAI")
        self.assertEqual(result[0].notes, ["まはーが作ったAI"])
        self.assertEqual(result[1].name, "SAIVerse")

    def test_json_in_code_block(self):
        response = '```json\n{"entities": [{"name": "Test", "category": "terms", "notes": ["note1"]}]}\n```'
        result = _parse_entities(response)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Test")

    def test_empty_response_is_a_failure(self):
        """⭐ 空応答は「抽出ゼロ」ではなく失敗 — 空リストで返すと成功扱いになる。"""
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response("")
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(None)

    def test_empty_entities(self):
        """entities キーがあって空 = 正常な抽出ゼロ (これだけが空リスト)。"""
        response = json.dumps({"entities": []})
        self.assertEqual(_parse_entities(response), [])

    def test_invalid_json_is_a_failure(self):
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response("this is not json")

    def test_json_without_entities_field_is_a_failure(self):
        """指示した形をしていない応答 = 抽出が成立していない。"""
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(json.dumps({"result": "ok"}))
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(json.dumps({"entities": {"name": "x"}}))

    def test_null_entities_is_a_failure(self):
        """⭐ entities が null は「抽出ゼロ」ではない (空リストへ読み替えない)。"""
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(json.dumps({"entities": None}))

    def test_broken_entity_item_is_a_failure(self):
        """要素が辞書でない = 応答の形が壊れている。"""
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(json.dumps({"entities": ["ただの文字列"]}))

    def test_non_string_fields_are_a_failure(self):
        """name / summary が文字列でない応答を str() で押し通さない。"""
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(json.dumps({
                "entities": [{"name": {"a": 1}, "notes": ["n"]}],
            }))
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(json.dumps({
                "entities": [{"name": "x", "summary": ["a"], "notes": ["n"]}],
            }))

    def test_notes_as_a_bare_string_becomes_one_note(self):
        """⭐ notes を素の文字列で返す LLM がいる。一文字ずつ Fragment にしない。"""
        result = _parse_entities(json.dumps({
            "entities": [{"name": "x", "category": "terms", "notes": "ひとつの事実"}],
        }, ensure_ascii=False))
        self.assertEqual(result[0].notes, ["ひとつの事実"])

    def test_notes_of_a_wrong_type_is_a_failure(self):
        with self.assertRaises(ExtractionFailed):
            _parse_extraction_response(json.dumps({
                "entities": [{"name": "x", "notes": {"a": 1}}],
            }))

    def test_entity_without_a_name_is_skipped_not_fatal(self):
        """名前の無い項目だけを飛ばす (良い項目まで巻き添えにしない)。"""
        result = _parse_entities(json.dumps({
            "entities": [
                {"name": "", "notes": ["捨てる"]},
                {"name": "Valid", "category": "terms", "notes": ["残る"]},
            ],
        }, ensure_ascii=False))
        self.assertEqual([e.name for e in result], ["Valid"])

    def test_missing_name_skipped(self):
        response = json.dumps({
            "entities": [
                {"name": "", "category": "people", "notes": ["note"]},
                {"name": "Valid", "category": "terms", "notes": ["note"]},
            ]
        })
        result = _parse_entities(response)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Valid")

    def test_empty_notes_with_summary_kept(self):
        response = json.dumps({
            "entities": [
                {"name": "OnlySummary", "category": "people", "summary": "概要あり", "notes": []},
                {"name": "NoNotesNoSummary", "category": "terms", "notes": []},
                {"name": "HasNotes", "category": "terms", "notes": ["note"]},
            ]
        })
        result = _parse_entities(response)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "OnlySummary")
        self.assertEqual(result[0].summary, "概要あり")
        self.assertEqual(result[1].name, "HasNotes")

    def test_invalid_category_defaults_to_terms(self):
        response = json.dumps({
            "entities": [
                {"name": "Test", "category": "invalid_cat", "notes": ["note"]},
            ]
        })
        result = _parse_entities(response)
        self.assertEqual(result[0].category, "terms")

    def test_list_format(self):
        """Some LLMs return a list instead of {entities: [...]}."""
        response = json.dumps([
            {"name": "Test", "category": "people", "notes": ["note"]},
        ])
        result = _parse_entities(response)
        self.assertEqual(len(result), 1)


class TestBuildExtractionPrompt(unittest.TestCase):
    """Test prompt construction."""

    def test_basic_prompt(self):
        prompt = _build_extraction_prompt("会話内容")
        self.assertIn("エンティティ", prompt)
        self.assertIn("会話内容", prompt)
        self.assertIn("JSON", prompt)

    def test_with_episode_context(self):
        prompt = _build_extraction_prompt("会話", episode_context="前回の要約")
        self.assertIn("前回の要約", prompt)

    def test_with_existing_pages(self):
        prompt = _build_extraction_prompt("会話", existing_pages="[people]\n  - まはー")
        self.assertIn("まはー", prompt)
        self.assertIn("既存のMemopedia", prompt)

    def test_asks_for_involved_entities(self):
        """B2 欄は入力を変えず、出力に一欄増やすだけ (recall_tags §9.3)。"""
        prompt = _build_extraction_prompt("会話")
        self.assertIn("involved_entities", prompt)
        self.assertIn("新しく判明した情報が無くても列挙してください", prompt)


class TestExtractEntities(unittest.TestCase):
    """Test the extraction function with mocked LLM."""

    def test_basic_extraction(self):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "entities": [
                {"name": "エイド", "category": "people", "notes": ["AIアシスタント"]},
            ]
        }, ensure_ascii=False)

        messages = [
            Message(id="1", thread_id="t", role="user", content="エイドについて話そう",
                    resource_id="r", created_at=1000, metadata={}),
        ]

        result = extract_entities(client, messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "エイド")
        client.generate.assert_called_once()

    def test_empty_messages(self):
        client = MagicMock()
        result = extract_entities(client, [])
        self.assertEqual(result, [])
        client.generate.assert_not_called()

    def test_llm_error_raises(self):
        """⭐ LLM が落ちた回を空リストで返すと「成功」として付箋が剥がれる。"""
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM error")

        messages = [
            Message(id="1", thread_id="t", role="user", content="test",
                    resource_id="r", created_at=1000, metadata={}),
        ]

        with self.assertRaises(ExtractionFailed):
            extract_entities(client, messages)

    def test_empty_llm_response_raises(self):
        client = MagicMock()
        client.generate.return_value = ""

        messages = [
            Message(id="1", thread_id="t", role="user", content="test",
                    resource_id="r", created_at=1000, metadata={}),
        ]

        with self.assertRaises(ExtractionFailed):
            extract_entities(client, messages)


class TestReflectToMemopedia(unittest.TestCase):
    """Memopedia への反映 (実物の Memopedia + in-memory DB で契約を見る)。"""

    def setUp(self):
        import sqlite3

        from sai_memory.memopedia.core import Memopedia

        self.conn = sqlite3.connect(":memory:")
        self.memopedia = Memopedia(self.conn)

    def tearDown(self):
        self.conn.close()

    def _fragments(self, page_id):
        return [f.content for f in self.memopedia.get_fragments(page_id)]

    def test_existing_page_creates_fragments(self):
        page = self.memopedia.create_page(parent_id="root_people", title="まはー")

        entities = [
            ExtractedEntity(name="まはー", category="people", notes=["新しい情報"]),
        ]
        results = reflect_to_memopedia(entities, self.memopedia, source_time=1711900000)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].is_new_page)
        self.assertEqual(results[0].page_id, page.id)
        # Notes are stored as Fragments (not appended to page content).
        self.assertEqual(self._fragments(page.id), ["新しい情報"])
        self.assertEqual(self.memopedia.get_page(page.id).content, "")

    def test_create_new_page_with_summary(self):
        entities = [
            ExtractedEntity(name="エイド", category="people", summary="ソフィーの一人であるAI", notes=["AIアシスタント"]),
        ]
        results = reflect_to_memopedia(entities, self.memopedia, source_time=1711900000)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_new_page)
        page = self.memopedia.get_page(results[0].page_id)
        self.assertEqual(page.title, "エイド")
        self.assertEqual(page.summary, "ソフィーの一人であるAI")
        self.assertEqual(page.parent_id, "root_people")
        self.assertEqual(self._fragments(page.id), ["AIアシスタント"])

    def test_existing_page_gets_summary_if_empty(self):
        page = self.memopedia.create_page(parent_id="root_people", title="まはー", summary="")

        entities = [
            ExtractedEntity(name="まはー", category="people", summary="ユーザー", notes=["新情報"]),
        ]
        reflect_to_memopedia(entities, self.memopedia, source_time=1711900000)

        self.assertEqual(self.memopedia.get_page(page.id).summary, "ユーザー")

    def test_summary_update_leaves_no_extractor_edit_history(self):
        """⭐ summary の書き換えは編集来歴に残さない。

        `entity_extractor` 名義の来歴は本文 → Fragment 変換が「機械が足した行」の
        確証に使う。本文でない文 (summary) を混ぜると、同じ文字列の手書き行まで
        自動変換されうる。
        """
        page = self.memopedia.create_page(parent_id="root_people", title="まはー", summary="古い")

        entities = [
            ExtractedEntity(name="まはー", category="people", summary="新しい", notes=["新情報"]),
        ]
        reflect_to_memopedia(entities, self.memopedia, source_time=1711900000)

        sources = [
            h.edit_source for h in self.memopedia.get_page_edit_history(page.id)
        ]
        self.assertNotIn("entity_extractor", sources)

    def test_new_page_records_extractor_edit_history(self):
        """新規ページの作成来歴は entity_extractor 名義 (変換の確証が使う)。"""
        entities = [
            ExtractedEntity(name="エイド", category="people", summary="AI", notes=["note"]),
        ]
        results = reflect_to_memopedia(entities, self.memopedia)

        sources = [
            h.edit_source for h in self.memopedia.get_page_edit_history(results[0].page_id)
        ]
        self.assertIn("entity_extractor", sources)

    def test_empty_notes_skipped(self):
        entities = [
            ExtractedEntity(name="Empty", category="terms", notes=[]),
        ]
        results = reflect_to_memopedia(entities, self.memopedia)
        self.assertEqual(results, [])
        self.assertIsNone(self.memopedia.find_by_title("Empty"))

    def test_category_to_root_mapping(self):
        for category, expected_root in [
            ("people", "root_people"),
            ("terms", "root_terms"),
            ("events", "root_events"),
            ("plans", "root_plans"),
        ]:
            entities = [
                ExtractedEntity(name=f"Test-{category}", category=category, notes=["note"]),
            ]
            results = reflect_to_memopedia(entities, self.memopedia)
            page = self.memopedia.get_page(results[0].page_id)
            self.assertEqual(
                page.parent_id, expected_root,
                f"Category '{category}' should map to '{expected_root}'",
            )

    def test_same_entry_reapplied_does_not_duplicate_fragments(self):
        """⭐ 拾い直しの二度目で同じ知識が二重に入らない (F3)。"""
        entities = [
            ExtractedEntity(name="エイド", category="people", summary="AI", notes=["note-1"]),
        ]
        first = reflect_to_memopedia(
            entities, self.memopedia, chronicle_entry_id="entry-1",
        )
        second = reflect_to_memopedia(
            entities, self.memopedia, chronicle_entry_id="entry-1",
        )

        self.assertEqual(self._fragments(first[0].page_id), ["note-1"])
        self.assertEqual(second[0].notes_appended, 0)

    def test_partial_failure_leaves_nothing_behind(self):
        """⭐ 途中で落ちたら、先行のページも Fragment も残らない (F3)。

        部分適用が残ると、拾い直しがチャンク全体を再抽出して同じ知識を
        新しい UUID で挿し直す。
        """
        entities = [
            ExtractedEntity(name="先", category="people", summary="s", notes=["note-1"]),
            ExtractedEntity(name="後", category="people", summary="s", notes=["note-2"]),
        ]
        with patch(
            "sai_memory.memopedia.core.storage_create_fragment",
            side_effect=[RuntimeError("disk full")],
        ):
            with self.assertRaises(RuntimeError):
                reflect_to_memopedia(entities, self.memopedia, chronicle_entry_id="e1")

        self.assertIsNone(self.memopedia.find_by_title("先"))
        self.assertIsNone(self.memopedia.find_by_title("後"))


class TestBatchCallbackContract(unittest.TestCase):
    """docs/issues/memopedia_writers_bypass_adapter_lock.md の契約。"""

    def _msg(self):
        return Message(id="m1", thread_id="t", role="user", content="hi",
                       resource_id="r", created_at=1000, metadata={})

    def test_extraction_failure_propagates_to_caller(self):
        """⭐ callback は失敗を握り潰さない。

        記録するのは呼び出し元 (executor.ExecutionResult.extraction_failures)。
        ここで warning に畳むと、ペルソナの記憶追記が黙って落ちる。
        """
        import sqlite3
        from sai_memory.memory.entity_extractor import make_batch_callback

        conn = sqlite3.connect(":memory:")
        callback = make_batch_callback(MagicMock(), conn)
        with patch(
            "sai_memory.memory.entity_extractor.extract_and_reflect",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                callback([self._msg()], "entry-1")

    def test_extract_and_reflect_passes_the_shared_lock(self):
        """⭐ adapter の _db_lock が Memopedia まで届くこと。

        錠前は DB ファイルに紐づくので渡し忘れても同じものになるが、抽出器は
        付箋の書き込みなど Memopedia を通らない経路にも同じ錠前を使う。
        """
        import sqlite3
        import threading
        from sai_memory.memory.entity_extractor import (
            ExtractionOutput,
            extract_and_reflect,
        )

        lock = threading.RLock()
        conn = sqlite3.connect(":memory:")
        with patch("sai_memory.memopedia.Memopedia") as memo_cls, patch(
            "sai_memory.memory.entity_extractor.extract_entities_and_involvement",
            return_value=ExtractionOutput(),
        ):
            extract_and_reflect(MagicMock(), conn, [self._msg()], db_lock=lock)
        memo_cls.assert_called_once_with(conn, db_lock=lock)


class TestInvolvedEntitiesParsing(unittest.TestCase):
    """B2 欄 (involved_entities) の読み取り — 要素単位で棄却し、抽出は壊さない。"""

    def _involved(self, payload):
        return _parse_extraction_response(
            json.dumps(payload, ensure_ascii=False)
        ).involved_titles

    def test_titles_are_read_and_stripped(self):
        self.assertEqual(
            self._involved({
                "entities": [],
                "involved_entities": ["  まはー ", "スタックチャン"],
            }),
            ["まはー", "スタックチャン"],
        )

    def test_missing_field_is_not_a_failure(self):
        """⭐ 欄の無い応答 (欄を無視したモデル) でも抽出は成立する。"""
        output = _parse_extraction_response(json.dumps({
            "entities": [{"name": "エイド", "category": "people", "notes": ["n"]}],
        }, ensure_ascii=False))
        self.assertEqual([e.name for e in output.entities], ["エイド"])
        self.assertEqual(output.involved_titles, [])

    def test_wrong_type_drops_only_the_tags(self):
        """⭐ 欄の型が違っても抽出まで巻き添えにしない。

        タグの取りこぼしは後から全記憶を走査して遡及できるが、知識は拾い直しに
        失敗すれば永久に落ちる。だから非対称に扱う。
        """
        output = _parse_extraction_response(json.dumps({
            "entities": [{"name": "エイド", "category": "people", "notes": ["n"]}],
            "involved_entities": {"a": 1},
        }, ensure_ascii=False))
        self.assertEqual([e.name for e in output.entities], ["エイド"])
        self.assertEqual(output.involved_titles, [])

    def test_bare_string_becomes_one_title(self):
        self.assertEqual(
            self._involved({"entities": [], "involved_entities": "まはー"}),
            ["まはー"],
        )

    def test_non_string_element_is_dropped_not_fatal(self):
        self.assertEqual(
            self._involved({
                "entities": [],
                "involved_entities": ["まはー", {"name": "壊れた"}, "エイド"],
            }),
            ["まはー", "エイド"],
        )

    def test_duplicates_collapse(self):
        self.assertEqual(
            self._involved({
                "entities": [],
                "involved_entities": ["まはー", "まはー"],
            }),
            ["まはー"],
        )

    def test_legacy_list_response_has_no_tags(self):
        """entities を包まない旧形式には B2 欄を載せる場所が無い。"""
        output = _parse_extraction_response(json.dumps([
            {"name": "Test", "category": "people", "notes": ["note"]},
        ]))
        self.assertEqual(output.involved_titles, [])


class TestInvolvementEdges(unittest.TestCase):
    """B2 欄 → タイトル照合 → chunk_page_edges の辺 (recall_tags §9.3)。"""

    def setUp(self):
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.memopedia import Memopedia
        from sai_memory.memory.storage import init_db

        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)
        self.memopedia = Memopedia(self.conn)
        self.maha = self.memopedia.create_page(
            parent_id="root_people", title="まはー",
        )
        self.stackchan = self.memopedia.create_page(
            parent_id="root_terms", title="スタックチャン",
        )

    def tearDown(self):
        self.conn.close()

    def _client(self, *, entities=None, involved=None, omit_involved=False):
        payload = {"entities": entities if entities is not None else []}
        if not omit_involved:
            payload["involved_entities"] = involved if involved is not None else []
        client = MagicMock()
        client.generate.return_value = json.dumps(payload, ensure_ascii=False)
        return client

    def _msgs(self):
        return [
            Message(id="m1", thread_id="t", role="user", content="調べ物をした",
                    resource_id="r", created_at=1000, metadata={}),
        ]

    def _edges(self, chronicle_id="entry-1"):
        from sai_memory.memory.recall_edges import list_entity_pages_for_chronicle

        return list_entity_pages_for_chronicle(self.conn, chronicle_id)

    def test_involved_titles_become_edges(self):
        """⭐ 新しく判明した知識がゼロでも、関与した対象には辺が張られる。

        「新情報が無くても、その対象のための調べ物・作業を含む」(§9.3) —— ここで
        打ち切ると B2 欄を足した意味がなくなる。
        """
        from sai_memory.memory.entity_extractor import extract_and_reflect

        results = extract_and_reflect(
            self._client(involved=["まはー", "スタックチャン"]),
            self.conn, self._msgs(), chronicle_entry_id="entry-1",
        )

        self.assertEqual(results, [])
        self.assertEqual(
            sorted(self._edges()), sorted([self.maha.id, self.stackchan.id]),
        )

    def test_edges_are_written_alongside_extraction(self):
        """抽出とタグ付けは同じコールに乗る別の仕事 — 両方が着地する。"""
        from sai_memory.memory.entity_extractor import extract_and_reflect

        results = extract_and_reflect(
            self._client(
                entities=[{
                    "name": "エイド", "category": "people",
                    "summary": "AI", "notes": ["新情報"],
                }],
                involved=["まはー"],
            ),
            self.conn, self._msgs(), chronicle_entry_id="entry-1",
        )

        self.assertEqual([r.entity_name for r in results], ["エイド"])
        self.assertEqual(self._edges(), [self.maha.id])

    def test_unresolved_title_is_dropped_and_extraction_still_succeeds(self):
        """⭐ 照合できないタイトルはその 1 件だけ捨てる (指し先は実体ページに限る)。

        自由語を辺にすると、表記ゆれのぶんだけ誰も辿れないノードが増える。
        """
        from sai_memory.memory.entity_extractor import extract_and_reflect

        results = extract_and_reflect(
            self._client(
                entities=[{
                    "name": "エイド", "category": "people",
                    "summary": "AI", "notes": ["新情報"],
                }],
                involved=["まはーさん", "そのプロジェクトの調べ物"],
            ),
            self.conn, self._msgs(), chronicle_entry_id="entry-1",
        )

        self.assertEqual([r.entity_name for r in results], ["エイド"])
        self.assertEqual(self._edges(), [])

    def test_category_shelves_are_not_edge_targets(self):
        """カテゴリの棚 (「人物」等) を指し先にしない — 遡りの材料にならない。"""
        from sai_memory.memory.entity_extractor import extract_and_reflect

        extract_and_reflect(
            self._client(involved=["人物", "まはー"]),
            self.conn, self._msgs(), chronicle_entry_id="entry-1",
        )
        self.assertEqual(self._edges(), [self.maha.id])

    def test_a_page_created_by_this_extraction_can_be_tagged(self):
        """この回に作られたページも関与の指し先になれる (関与は事実で新旧は無関係)。"""
        from sai_memory.memory.entity_extractor import extract_and_reflect

        results = extract_and_reflect(
            self._client(
                entities=[{
                    "name": "エイド", "category": "people",
                    "summary": "AI", "notes": ["新情報"],
                }],
                involved=["エイド"],
            ),
            self.conn, self._msgs(), chronicle_entry_id="entry-1",
        )
        self.assertEqual(self._edges(), [results[0].page_id])

    def test_rerunning_the_same_extraction_does_not_duplicate_edges(self):
        """⭐ 冪等 — 拾い直しで同じ抽出をもう一度走らせても辺は重ならない。"""
        from sai_memory.memory.entity_extractor import extract_and_reflect

        for _ in range(2):
            extract_and_reflect(
                self._client(involved=["まはー", "スタックチャン"]),
                self.conn, self._msgs(), chronicle_entry_id="entry-1",
            )

        rows = self.conn.execute(
            "SELECT COUNT(*) FROM chunk_page_edges WHERE chronicle_page_id = ?",
            ("entry-1",),
        ).fetchone()[0]
        self.assertEqual(rows, 2)

    def test_missing_involved_field_leaves_extraction_intact(self):
        """⭐ 欄そのものが無い応答でも既存の抽出は壊れない (辺はゼロ)。"""
        from sai_memory.memory.entity_extractor import extract_and_reflect

        results = extract_and_reflect(
            self._client(
                entities=[{
                    "name": "エイド", "category": "people",
                    "summary": "AI", "notes": ["新情報"],
                }],
                omit_involved=True,
            ),
            self.conn, self._msgs(), chronicle_entry_id="entry-1",
        )
        self.assertEqual([r.entity_name for r in results], ["エイド"])
        self.assertEqual(self._edges(), [])

    def test_without_a_chronicle_id_no_edge_is_invented(self):
        """辺を張る先が無い回は、辺を作らずに抽出だけ通す。"""
        from sai_memory.memory.entity_extractor import extract_and_reflect

        extract_and_reflect(
            self._client(involved=["まはー"]),
            self.conn, self._msgs(), chronicle_entry_id=None,
        )
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM chunk_page_edges"
        ).fetchone()[0]
        self.assertEqual(rows, 0)

    def test_edge_write_failure_is_not_swallowed(self):
        """⭐ 辺の書き込みの失敗を空成功にしない。

        呼び出し元 (executor / bands) が付箋に貼り、次の Metabolism が抽出ごと
        拾い直す —— ここで握り潰すと、その回の関与は誰にも回収されない。
        """
        from sai_memory.memory.entity_extractor import extract_and_reflect

        with patch(
            "sai_memory.memory.entity_extractor.add_chunk_page_edge",
            side_effect=RuntimeError("disk full"),
        ):
            with self.assertRaises(RuntimeError):
                extract_and_reflect(
                    self._client(involved=["まはー"]),
                    self.conn, self._msgs(), chronicle_entry_id="entry-1",
                )

    def test_backlog_retry_writes_the_edges(self):
        """⭐ 付箋の拾い直し経路でも辺が張られる。

        辺は抽出と同じコールの産物なので、拾い直しは抽出ごとやり直す = 辺も
        書き直される。冪等なので、一度書けていた辺が重複することもない。
        """
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.memory.entity_extractor import (
            make_batch_callback,
            record_extraction_failure,
            retry_extraction_backlog,
        )
        from sai_memory.memory.storage import add_message

        mid = add_message(self.conn, "t", "user", "調べ物をした", created_at=1000)
        create_entry(
            self.conn, level=1, content="調べ物をした",
            source_ids=[mid], source_count=1, message_count=1,
            entry_id="entry-1",
        )
        record_extraction_failure(self.conn, "entry-1")

        client = self._client(
            entities=[{
                "name": "エイド", "category": "people",
                "summary": "AI", "notes": ["新情報"],
            }],
            involved=["まはー"],
        )
        stats = retry_extraction_backlog(
            self.conn, make_batch_callback(client, self.conn),
        )

        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(self._edges(), [self.maha.id])

    def test_a_taken_over_claim_writes_no_edges(self):
        """⭐ 取り置きを取り戻された実行は、辺も書かない。

        新知識ゼロの回は Memopedia のトランザクションが走らないため、書き込み
        直前の検査 (precondition) がどこも通らなくなる。辺の前で通す。
        """
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.memory.entity_extractor import (
            make_batch_callback,
            record_extraction_failure,
            retry_extraction_backlog,
        )
        from sai_memory.memory.storage import add_message

        mid = add_message(self.conn, "t", "user", "調べ物をした", created_at=1000)
        create_entry(
            self.conn, level=1, content="調べ物をした",
            source_ids=[mid], source_count=1, message_count=1,
            entry_id="entry-1",
        )
        record_extraction_failure(self.conn, "entry-1")

        inner = make_batch_callback(self._client(involved=["まはー"]), self.conn)

        def steal_then_extract(messages, eid, precondition=None):
            # 拾い直しに時間が掛かっている間に、別の実行が取り置きを取り戻した
            self.conn.execute(
                "UPDATE entity_extraction_backlog SET version = version + 1 "
                "WHERE entry_id = ?", (eid,),
            )
            self.conn.commit()
            inner(messages, eid, precondition=precondition)

        stats = retry_extraction_backlog(self.conn, steal_then_extract)

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self._edges(), [], "取り下げた実行が辺を書いている")

    # ----- 取り置きの検査と辺の書き込みの競合 (Codex 2026-08-21 #1) -----

    def _backlogged_entry(self):
        """付箋 1 枚 + 元メッセージ + Chronicle エントリを用意して entry_id を返す。"""
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.memory.entity_extractor import record_extraction_failure
        from sai_memory.memory.storage import add_message

        mid = add_message(self.conn, "t", "user", "調べ物をした", created_at=1000)
        create_entry(
            self.conn, level=1, content="調べ物をした",
            source_ids=[mid], source_count=1, message_count=1,
            entry_id="entry-1",
        )
        record_extraction_failure(self.conn, "entry-1")
        return "entry-1"

    def _steal_claim(self, entry_id="entry-1"):
        """別の実行が取り置きを取り戻した状況を作る (版を進める)。"""
        self.conn.execute(
            "UPDATE entity_extraction_backlog SET version = version + 1 "
            "WHERE entry_id = ?", (entry_id,),
        )
        self.conn.commit()

    def _run_retry_stealing_before_the_edges(self, client):
        """タイトル解決の直前で取り置きを奪ってから拾い直しを走らせる。

        奪う位置は「Memopedia への反映が終わった後・辺の INSERT の前」——
        検査が辺の書き込みの外にあった頃に開いていた窓そのもの。
        """
        from sai_memory.memory import entity_extractor as ee

        real_resolve = ee._resolve_involved_page_ids

        def steal_then_resolve(memopedia, titles):
            self._steal_claim()
            return real_resolve(memopedia, titles)

        with patch.object(
            ee, "_resolve_involved_page_ids", side_effect=steal_then_resolve,
        ):
            return ee.retry_extraction_backlog(
                self.conn, ee.make_batch_callback(client, self.conn),
            )

    def test_a_claim_stolen_after_reflect_writes_no_edges(self):
        """⭐ 反映が済んだ後に取り置きを奪われた実行は、辺を書かない。

        新知識があった回は Memopedia 側の検査を通ってしまう。そこから辺の
        INSERT が届くまでの間に取り置きが動くと、失効した実行の辺が確定して
        いた —— 検査は辺の書き込みと同じトランザクションの中で行う。
        """
        self._backlogged_entry()
        client = self._client(
            entities=[{
                "name": "エイド", "category": "people",
                "summary": "AI", "notes": ["新情報"],
            }],
            involved=["まはー"],
        )

        stats = self._run_retry_stealing_before_the_edges(client)

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self._edges(), [], "失効した実行が辺を書いている")

    def test_a_claim_stolen_before_the_edges_writes_none_with_zero_entities(self):
        """⭐ 新知識ゼロの拾い直しでも、辺の直前で奪われたら 1 本も書かない。

        この経路は Memopedia のトランザクションが走らないので、辺の書き込みの
        中の検査だけが最後の歯止めになる。
        """
        self._backlogged_entry()

        stats = self._run_retry_stealing_before_the_edges(
            self._client(involved=["まはー", "スタックチャン"]),
        )

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self._edges(), [], "失効した実行が辺を書いている")

    def test_an_intact_claim_writes_the_edges(self):
        """取り置きが自分のままなら、同じ経路で辺は普通に書かれる。

        上二つの「書かない」が、検査ではなく別の理由で起きていないことの対。
        """
        from sai_memory.memory.entity_extractor import (
            make_batch_callback,
            retry_extraction_backlog,
        )

        self._backlogged_entry()
        stats = retry_extraction_backlog(
            self.conn,
            make_batch_callback(
                self._client(involved=["まはー", "スタックチャン"]), self.conn,
            ),
        )

        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(
            sorted(self._edges()), sorted([self.maha.id, self.stackchan.id]),
        )

    def test_the_claim_version_does_not_move_between_check_and_commit(self):
        """⭐ 検査を通してから辺が確定するまで、取り置きの版が動かない。

        版は :func:`_still_ours` が照合する値そのもの。検査と INSERT が同じ
        トランザクションに入っていれば、その間に別の実行が版を進めることは
        できない (SQLite の書き込みロックが待たせる)。ここでは INSERT の
        まっただ中で版を読み、検査時と同じ値であることを確かめる。
        """
        from sai_memory.memory import entity_extractor as ee

        self._backlogged_entry()
        seen_versions = []

        def read_version():
            row = self.conn.execute(
                "SELECT version FROM entity_extraction_backlog WHERE entry_id = ?",
                ("entry-1",),
            ).fetchone()
            return row[0] if row else None

        real_still_ours = ee._still_ours

        def watching_still_ours(conn, entry_id, version):
            check = real_still_ours(conn, entry_id, version)

            def wrapped():
                check()
                seen_versions.append(("check", read_version()))
            return wrapped

        real_add = ee.add_chunk_page_edge

        def watching_add(*args, **kwargs):
            seen_versions.append(("insert", read_version()))
            return real_add(*args, **kwargs)

        with patch.object(ee, "_still_ours", side_effect=watching_still_ours), \
                patch.object(ee, "add_chunk_page_edge", side_effect=watching_add):
            stats = ee.retry_extraction_backlog(
                self.conn,
                ee.make_batch_callback(
                    self._client(involved=["まはー", "スタックチャン"]), self.conn,
                ),
            )

        self.assertEqual(stats["recovered"], 1)
        self.assertEqual([w for w, _ in seen_versions], ["check", "insert", "insert"])
        versions = {v for _, v in seen_versions}
        self.assertEqual(
            len(versions), 1,
            f"検査から辺の確定までに取り置きの版が動いた: {seen_versions}",
        )

    def test_a_failed_edge_rolls_back_the_ones_already_written(self):
        """⭐ 辺は 1 回の抽出ぶんがまとめて入るか、1 本も入らないか。

        1 本ずつ確定していると、途中で落ちた回に「検査を通った証拠のない辺」が
        残る。検査と INSERT を同じトランザクションに収めた副産物として、
        部分的な記帳も無くなる。
        """
        from sai_memory.memory import entity_extractor as ee

        real_add = ee.add_chunk_page_edge
        calls = []

        def failing_add(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real_add(*args, **kwargs)

        with patch.object(ee, "add_chunk_page_edge", side_effect=failing_add):
            with self.assertRaises(RuntimeError):
                ee.extract_and_reflect(
                    self._client(involved=["まはー", "スタックチャン"]),
                    self.conn, self._msgs(), chronicle_entry_id="entry-1",
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(self._edges(), [], "先に書けた辺が残っている")


class TestInvolvementEdgeCleanup(unittest.TestCase):
    """チャンク／実体ページの物理削除で、辺が孤児にならない (Codex 2026-08-21 #2)。

    ``chunk_page_edges`` には外部キーも削除連鎖も無いので、削除を発行する側が
    同じトランザクションで辺を落とす。ここで数え上げているのが、非テストコードに
    ある ``DELETE FROM memopedia_pages`` の全部。
    """

    def setUp(self):
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.memopedia import Memopedia
        from sai_memory.memory.recall_edges import init_chunk_page_edge_tables
        from sai_memory.memory.storage import init_db

        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)
        self.memopedia = Memopedia(self.conn)
        init_chunk_page_edge_tables(self.conn)
        self.maha = self.memopedia.create_page(
            parent_id="root_people", title="まはー",
        )

    def tearDown(self):
        self.conn.close()

    def _chunk(self, *, content="調べ物をした", **kw):
        from sai_memory.arasuji.storage import create_entry

        return create_entry(
            self.conn, level=1, content=content,
            source_ids=[], source_count=0, message_count=0, **kw,
        )

    def _edge(self, chronicle_id, entity_id=None):
        from sai_memory.memory.recall_edges import add_chunk_page_edge

        add_chunk_page_edge(
            self.conn, chronicle_id, entity_id or self.maha.id,
        )

    def _edge_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM chunk_page_edges"
        ).fetchone()[0]

    def test_deleting_a_chunk_removes_its_edges(self):
        from sai_memory.arasuji.storage import delete_entry

        entry = self._chunk()
        self._edge(entry.id)
        self.assertTrue(delete_entry(self.conn, entry.id))
        self.assertEqual(self._edge_count(), 0)

    def test_deleting_a_chunk_with_its_parent_removes_its_edges(self):
        from sai_memory.arasuji.storage import delete_entry_and_update_parent

        entry = self._chunk()
        self._edge(entry.id)
        success, _ = delete_entry_and_update_parent(self.conn, entry.id)
        self.assertTrue(success)
        self.assertEqual(self._edge_count(), 0)

    def test_dismantling_a_chunk_removes_its_edges(self):
        from sai_memory.arasuji.storage import (
            add_to_parent_source_ids,
            dismantle_entry,
        )

        child = self._chunk(content="子")
        parent = self._chunk(content="親")
        add_to_parent_source_ids(self.conn, child.id, parent.id)
        self._edge(parent.id)
        self._edge(child.id)

        success, _ = dismantle_entry(self.conn, parent.id)
        self.assertTrue(success)
        # 親だけが消える (子は未束ねへ戻るだけ) ので、子の辺は残る
        self.assertEqual(self._edge_count(), 1)
        from sai_memory.memory.recall_edges import list_chronicle_pages_for_entity
        self.assertEqual(
            list_chronicle_pages_for_entity(self.conn, self.maha.id), [child.id],
        )

    def test_deleting_incomplete_chunks_removes_their_edges(self):
        from sai_memory.arasuji.storage import delete_incomplete_entries

        entry = self._chunk(origin_track_id="track-1", is_incomplete=True)
        self._edge(entry.id)
        self.assertEqual(delete_incomplete_entries(self.conn, "track-1"), 1)
        self.assertEqual(self._edge_count(), 0)

    def test_clearing_all_chunks_removes_all_edges(self):
        from sai_memory.arasuji.storage import clear_all_entries

        for i in range(3):
            self._edge(self._chunk(content=f"c{i}").id)
        self.assertEqual(self._edge_count(), 3)
        clear_all_entries(self.conn)
        self.assertEqual(self._edge_count(), 0)

    def test_regenerating_a_chunk_leaves_no_edge_for_the_replaced_id(self):
        """⭐ 再生成は別 id の新エントリへ差し替える — 旧 id の辺を残さない。

        旧辺は新エントリの再抽出でも消えない (指し先の id が違う) ため、
        置換のたびに「存在しないチャンク」を指す辺が積み上がっていた。
        """
        from sai_memory.arasuji.storage import get_entry, regenerate_entry
        from sai_memory.memory.storage import add_message

        mid = add_message(self.conn, "t", "user", "調べ物をした", created_at=1000)
        old = self._chunk(content="旧本文")
        self.conn.execute(
            "UPDATE memopedia_pages SET metadata = json_set(metadata, "
            "'$.source_ids', json_array(?)) WHERE id = ?",
            (mid, old.id),
        )
        self.conn.commit()
        self._edge(old.id)

        replacement = self._chunk(content="新本文")

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            return_value=replacement,
        ):
            new_entry = regenerate_entry(self.conn, old.id)

        self.assertIsNotNone(new_entry, "再生成が成立していない")
        self.assertIsNone(get_entry(self.conn, old.id))
        self.assertEqual(
            self._edge_count(), 0,
            "置換されたチャンクを指す辺が孤児として残っている",
        )

    def test_physically_deleting_an_entity_page_removes_its_edges(self):
        """⭐ 逆側 — 実体ページの物理削除 (clear_all_pages / import の入れ替え)。

        ``Memopedia.delete_page`` は soft-delete なのでここには来ない (行が
        残る = 辺の指し先も残る)。物理削除は storage 側の同名関数だけ。
        """
        from sai_memory.memopedia.storage import delete_page

        entry = self._chunk()
        self._edge(entry.id)
        self.assertTrue(delete_page(self.conn, self.maha.id))
        self.assertEqual(self._edge_count(), 0)

    def test_clear_all_pages_removes_entity_edges(self):
        entry = self._chunk()
        self._edge(entry.id)
        self.memopedia.clear_all_pages()
        self.assertEqual(self._edge_count(), 0)

    def test_soft_deleting_an_entity_page_keeps_its_edges(self):
        """soft-delete では辺を落とさない — ページ行は残り、復元もされうる。"""
        entry = self._chunk()
        self._edge(entry.id)
        self.assertTrue(self.memopedia.delete_page(self.maha.id))
        self.assertEqual(self._edge_count(), 1)

    def test_cleanup_is_a_noop_without_the_edge_table(self):
        """辺のテーブルがまだ無い DB (抽出が一度も走っていない) でも削除は通る。"""
        from sai_memory.arasuji.storage import delete_entry

        entry = self._chunk()
        self.conn.execute("DROP TABLE chunk_page_edges")
        self.conn.commit()
        self.assertTrue(delete_entry(self.conn, entry.id))


class TestExtractionBacklog(unittest.TestCase):
    """抽出失敗の付箋 — 貼る・拾い直す・上限で止まる (まはー裁定 2026-08-06)。"""

    def setUp(self):
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.memory.storage import init_db

        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def _entry_with_messages(self, entry_id="entry-1"):
        """メッセージ 2 件と、それを source に持つ Chronicle entry を作る。"""
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.memory.storage import add_message

        m1 = add_message(self.conn, "t", "user", "こんにちは", created_at=1000)
        m2 = add_message(self.conn, "t", "assistant", "やあ", created_at=1001)
        create_entry(
            self.conn, level=1, content="挨拶した",
            source_ids=[m1, m2], source_count=2, message_count=2,
            entry_id=entry_id,
        )
        return entry_id

    def test_retry_recovers_and_clears_the_note(self):
        """⭐ 拾い直しに成功したら付箋が剥がれる。callback には元メッセージが届く。"""
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)

        seen = []
        stats = retry_extraction_backlog(
            self.conn, lambda messages, eid, **_: seen.append((len(messages), eid))
        )
        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(seen, [(2, entry_id)])
        left = self.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 0)

    def test_retry_failure_keeps_the_note_and_counts_attempts(self):
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)

        def bad_callback(messages, eid, **_):
            raise RuntimeError("still down")

        stats = retry_extraction_backlog(self.conn, bad_callback)
        self.assertEqual(stats["failed"], 1)
        attempts = self.conn.execute(
            "SELECT attempts FROM entity_extraction_backlog WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()[0]
        self.assertEqual(attempts, 2)

    def test_an_abandoned_claim_still_counts_toward_the_limit(self):
        """⭐ 途中で死んだ拾い直しも回数に数える (上限をすり抜けさせない)。

        取り置きの成立ではなく失敗のときだけ数えていると、処理の途中で落ちた
        付箋は回数が増えないまま毎時間取り戻され、LLM を呼び続ける。
        """
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)

        calls = []

        def callback_that_dies(messages, eid, **_):
            calls.append(eid)
            raise KeyboardInterrupt("プロセスが落ちた相当")

        # 落ちた回のあと、放置された取り置きを毎回取り戻す状況を作る
        for _ in range(5):
            self.conn.execute(
                "UPDATE entity_extraction_backlog SET claimed_at = 0 "
                "WHERE entry_id = ?", (entry_id,),
            )
            self.conn.commit()
            try:
                retry_extraction_backlog(self.conn, callback_that_dies)
            except KeyboardInterrupt:
                pass

        self.assertLessEqual(
            len(calls), 3, "上限を超えて LLM を呼び続けている",
        )

    def test_exhausted_note_is_skipped_but_not_deleted(self):
        """⭐ 上限を超えた付箋は LLM を呼ばずスキップ。ただし剥がさない (黙って諦めない)。"""
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        for _ in range(4):  # 上限 (3) を超える失敗回数
            record_extraction_failure(self.conn, entry_id)

        calls = []
        stats = retry_extraction_backlog(
            self.conn, lambda messages, eid, **_: calls.append(eid)
        )
        self.assertEqual(stats["exhausted"], 1)
        self.assertEqual(calls, [])
        left = self.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 1)

    def test_a_new_failure_during_retry_is_not_erased(self):
        """⭐ 拾い直しの最中に貼り直された失敗を、成功側が消さない (F2)。

        版 (version) が一致したときだけ剥がす。並走する別の Metabolism が
        新しい失敗を貼った直後に、こちらの「成功したから剥がす」が通ると、
        その失敗は誰にも拾われなくなる。
        """
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)

        def callback_that_races(messages, eid, **_):
            # 抽出をやり直している間に、別経路が同じ entry の新しい失敗を貼る
            record_extraction_failure(self.conn, eid)

        stats = retry_extraction_backlog(self.conn, callback_that_races)
        self.assertEqual(stats["recovered"], 1)
        row = self.conn.execute(
            "SELECT attempts, state FROM entity_extraction_backlog WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        self.assertIsNotNone(row, "新しい失敗の付箋が消えている")
        # 1 (最初の失敗) + 1 (取り置き) + 1 (割り込んだ新しい失敗)
        self.assertEqual(row[0], 3)
        self.assertEqual(row[1], "pending")

    def test_claimed_note_is_skipped_by_a_parallel_retry(self):
        """⭐ 取り置き中の付箋を別の拾い直しが二重に抽出しない (F2)。"""
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)

        seen = []
        inner_stats = {}

        def callback(messages, eid, **_):
            seen.append(eid)
            # 取り置きの最中に走った「もう一方の Metabolism」
            inner_stats.update(
                retry_extraction_backlog(self.conn, lambda m, e, **_: seen.append(e))
            )

        retry_extraction_backlog(self.conn, callback)
        self.assertEqual(seen, [entry_id], "同じ付箋が二度抽出されている")
        self.assertEqual(inner_stats.get("skipped"), 1)

    def test_a_taken_over_claim_cannot_write(self):
        """⭐ 取り戻された後に戻ってきた実行は、書き込みの直前で止まる。

        後片付けの版検査だけでは、確定してしまった書き込みは戻せない。検査は
        書き込みと同じトランザクションの中で行う。
        """
        from sai_memory.memory.entity_extractor import (
            ClaimLost,
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)

        blocked = []

        def slow_callback(messages, eid, precondition=None):
            # 時間が掛かっているあいだに、別の実行が取り置きを取り戻した
            self.conn.execute(
                "UPDATE entity_extraction_backlog SET version = version + 1 "
                "WHERE entry_id = ?", (eid,),
            )
            self.conn.commit()
            # 書き込みの直前の検査 (Memopedia のトランザクションの中で呼ばれる)
            try:
                precondition()
            except ClaimLost:
                blocked.append(eid)
                raise

        stats = retry_extraction_backlog(self.conn, slow_callback)
        self.assertEqual(blocked, [entry_id], "書き込みが止められていない")
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["failed"], 0, "取り下げを失敗として数えている")
        left = self.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 1, "付箋は残る (別の実行の担当)")

    def test_legacy_backlog_rows_are_migrated(self):
        """先行版 (state / version を持たない付箋) も拾い直せる。"""
        from sai_memory.memory.entity_extractor import retry_extraction_backlog

        entry_id = self._entry_with_messages()
        self.conn.execute("DROP TABLE IF EXISTS entity_extraction_backlog")
        self.conn.execute(
            "CREATE TABLE entity_extraction_backlog ("
            "  entry_id TEXT PRIMARY KEY, failed_at INTEGER NOT NULL, "
            "  attempts INTEGER NOT NULL DEFAULT 1)"
        )
        self.conn.execute(
            "INSERT INTO entity_extraction_backlog (entry_id, failed_at, attempts) "
            "VALUES (?, ?, 1)", (entry_id, 1000),
        )
        self.conn.commit()

        seen = []
        stats = retry_extraction_backlog(self.conn, lambda m, e, **_: seen.append(e))
        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(seen, [entry_id])

    def test_count_only_sees_retriable_notes(self):
        """上限を超えた付箋は「拾い直せる枚数」に数えない (LLM を用意しない)。"""
        from sai_memory.memory.entity_extractor import (
            count_extraction_backlog,
            record_extraction_failure,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)
        self.assertEqual(count_extraction_backlog(self.conn)["claimable"], 1)

        for _ in range(3):
            record_extraction_failure(self.conn, entry_id)
        counts = count_extraction_backlog(self.conn)
        self.assertEqual(counts["claimable"], 0)
        # 上限で止まっている付箋も「残っている」ことは見えていないといけない
        self.assertEqual(counts["total"], 1)

    def test_partially_lost_sources_are_not_recovered(self):
        """⭐ 元メッセージが一部だけ消えていたら、残りで抽出して成功にしない。

        残った分だけ拾って付箋を剥がすと、消えた分の知識が「拾い直した」顔で
        永久に落ちる。LLM も呼ばない (不完全と分かっている抽出に課金しない)。
        """
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)
        # source の片方だけ消す
        row = self.conn.execute(
            "SELECT source_ids_json FROM arasuji_entries WHERE id = ?", (entry_id,),
        ).fetchone()
        import json as _json
        gone = _json.loads(row[0])[0]
        self.conn.execute("DELETE FROM messages WHERE id = ?", (gone,))
        self.conn.commit()

        calls = []
        stats = retry_extraction_backlog(
            self.conn, lambda m, e, **_: calls.append(e),
        )
        self.assertEqual(calls, [], "欠けたまま抽出を走らせている")
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["recovered"], 0)
        left = self.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 1, "付箋が剥がれている (欠けた分が見えなくなる)")

    def test_broken_source_metadata_keeps_the_note(self):
        """⭐ 出所の記録が壊れているだけの付箋を「辿れない」と同じ扱いにしない。

        元メッセージは在るかもしれないのに、辿る鍵だけが読めない状態。剥がすと
        その範囲の知識が永久に失われる。
        """
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        entry_id = self._entry_with_messages()
        record_extraction_failure(self.conn, entry_id)
        # arasuji_entries はビュー。実体 (memopedia_pages.metadata) の
        # source_ids を JSON として読めない値に差し替える
        self.conn.execute(
            "UPDATE memopedia_pages SET metadata = json_set(metadata, "
            "'$.source_ids', '壊れた記録') WHERE id = ?",
            (entry_id,),
        )
        self.conn.commit()

        calls = []
        stats = retry_extraction_backlog(
            self.conn, lambda m, e, **_: calls.append(e),
        )
        self.assertEqual(calls, [])
        self.assertEqual(stats["dropped"], 0, "壊れた記録を「辿れない」で剥がしている")
        self.assertEqual(stats["failed"], 1)
        left = self.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 1)

    def test_note_for_a_vanished_entry_is_dropped(self):
        """entry や元メッセージが消えていたら拾いようがない — 剥がして進む。"""
        from sai_memory.memory.entity_extractor import (
            record_extraction_failure,
            retry_extraction_backlog,
        )

        record_extraction_failure(self.conn, "no-such-entry")
        stats = retry_extraction_backlog(self.conn, lambda m, e, **_: None)
        self.assertEqual(stats["dropped"], 1)
        left = self.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
