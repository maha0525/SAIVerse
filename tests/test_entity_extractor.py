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


class TestParseExtractionResponse(unittest.TestCase):
    """Test JSON parsing of LLM responses."""

    def test_valid_json(self):
        response = json.dumps({
            "entities": [
                {"name": "エイド", "category": "people", "summary": "ソフィーの一人であるAI", "notes": ["まはーが作ったAI"]},
                {"name": "SAIVerse", "category": "terms", "summary": "AIプラットフォーム", "notes": ["開発中のシステム"]},
            ]
        }, ensure_ascii=False)
        result = _parse_extraction_response(response)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "エイド")
        self.assertEqual(result[0].category, "people")
        self.assertEqual(result[0].summary, "ソフィーの一人であるAI")
        self.assertEqual(result[0].notes, ["まはーが作ったAI"])
        self.assertEqual(result[1].name, "SAIVerse")

    def test_json_in_code_block(self):
        response = '```json\n{"entities": [{"name": "Test", "category": "terms", "notes": ["note1"]}]}\n```'
        result = _parse_extraction_response(response)
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
        self.assertEqual(_parse_extraction_response(response), [])

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
        result = _parse_extraction_response(json.dumps({
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
        result = _parse_extraction_response(json.dumps({
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
        result = _parse_extraction_response(response)
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
        result = _parse_extraction_response(response)
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
        result = _parse_extraction_response(response)
        self.assertEqual(result[0].category, "terms")

    def test_list_format(self):
        """Some LLMs return a list instead of {entities: [...]}."""
        response = json.dumps([
            {"name": "Test", "category": "people", "notes": ["note"]},
        ])
        result = _parse_extraction_response(response)
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
        from sai_memory.memory.entity_extractor import extract_and_reflect

        lock = threading.RLock()
        conn = sqlite3.connect(":memory:")
        with patch("sai_memory.memopedia.Memopedia") as memo_cls, patch(
            "sai_memory.memory.entity_extractor.extract_entities",
            return_value=[],
        ):
            extract_and_reflect(MagicMock(), conn, [self._msg()], db_lock=lock)
        memo_cls.assert_called_once_with(conn, db_lock=lock)


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
