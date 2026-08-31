"""Tests for memory note extraction."""

import sqlite3
import unittest
from unittest.mock import MagicMock

from sai_memory.memory.note_extractor import (
    _build_extraction_prompt,
    _parse_notes_response,
    extract_and_store_notes,
    extract_memory_notes,
    make_batch_callback,
)
from sai_memory.memory.storage import (
    add_memory_notes,
    count_unresolved_notes,
    get_unresolved_notes,
    init_db,
    Message,
)


def _make_message(role: str, content: str, created_at: int = 1000) -> Message:
    return Message(
        id=f"msg_{created_at}",
        thread_id="t1",
        role=role,
        content=content,
        resource_id=None,
        created_at=created_at,
        metadata=None,
    )


class TestParseNotesResponse(unittest.TestCase):

    def test_clean_json_array(self):
        result = _parse_notes_response('["fact A", "fact B"]')
        self.assertEqual(result, ["fact A", "fact B"])

    def test_markdown_code_block(self):
        result = _parse_notes_response('```json\n["fact A", "fact B"]\n```')
        self.assertEqual(result, ["fact A", "fact B"])

    def test_empty_array(self):
        result = _parse_notes_response("[]")
        self.assertEqual(result, [])

    def test_empty_string(self):
        result = _parse_notes_response("")
        self.assertEqual(result, [])

    def test_fallback_bullet_points(self):
        result = _parse_notes_response("- fact A\n- fact B\n- fact C")
        self.assertEqual(result, ["fact A", "fact B", "fact C"])

    def test_fallback_japanese_bullets(self):
        result = _parse_notes_response("・事実A\n・事実B")
        self.assertEqual(result, ["事実A", "事実B"])

    def test_filters_empty_items(self):
        result = _parse_notes_response('["fact A", "", "fact B"]')
        self.assertEqual(result, ["fact A", "fact B"])


class TestBuildExtractionPrompt(unittest.TestCase):

    def test_basic_prompt(self):
        prompt = _build_extraction_prompt("会話内容")
        self.assertIn("会話内容", prompt)
        self.assertIn("抽出すべき情報のカテゴリ", prompt)

    def test_includes_episode_context(self):
        prompt = _build_extraction_prompt("会話", episode_context="前の出来事の要約")
        self.assertIn("これまでの流れ", prompt)
        self.assertIn("前の出来事の要約", prompt)

    def test_includes_memopedia_context(self):
        prompt = _build_extraction_prompt("会話", memopedia_context="- ページA\n- ページB")
        self.assertIn("既存の知識ベース", prompt)
        self.assertIn("ページA", prompt)
        self.assertIn("重複する情報は抽出しないでください", prompt)

    def test_includes_existing_notes(self):
        prompt = _build_extraction_prompt("会話", existing_notes=["既存メモ1", "既存メモ2"])
        self.assertIn("既にメモ済みの項目", prompt)
        self.assertIn("- 既存メモ1", prompt)
        self.assertIn("- 既存メモ2", prompt)

    def test_all_context_combined(self):
        prompt = _build_extraction_prompt(
            "会話内容",
            episode_context="流れ",
            memopedia_context="知識",
            existing_notes=["メモ"],
        )
        self.assertIn("これまでの流れ", prompt)
        self.assertIn("既存の知識ベース", prompt)
        self.assertIn("既にメモ済みの項目", prompt)
        self.assertIn("会話内容", prompt)

    def test_empty_context_sections_omitted(self):
        prompt = _build_extraction_prompt("会話", episode_context="", memopedia_context="")
        # Section headers should not appear when context is empty
        self.assertNotIn("これまでの流れ（参考）", prompt)
        # The Memopedia section header should not appear
        self.assertNotIn("既存の知識ベース（Memopedia）", prompt)


class TestExtractMemoryNotes(unittest.TestCase):

    def test_successful_extraction(self):
        client = MagicMock()
        client.generate.return_value = '["まはーは猫が好き", "新しいモデルが出た"]'

        messages = [
            _make_message("user", "猫かわいいよね", 1000),
            _make_message("assistant", "かわいいですね！", 1001),
        ]

        notes = extract_memory_notes(client, messages)
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0], "まはーは猫が好き")
        client.generate.assert_called_once()

    def test_context_passed_to_prompt(self):
        """Verify that context arguments are included in the LLM prompt."""
        client = MagicMock()
        client.generate.return_value = "[]"

        messages = [_make_message("user", "test", 1000)]
        extract_memory_notes(
            client, messages,
            episode_context="past events",
            memopedia_context="existing pages",
            existing_notes=["already noted"],
        )

        # Check the prompt passed to generate()
        call_args = client.generate.call_args
        prompt = call_args[1]["messages"][0]["content"] if "messages" in call_args[1] else call_args[0][0][0]["content"]
        self.assertIn("past events", prompt)
        self.assertIn("existing pages", prompt)
        self.assertIn("already noted", prompt)

    def test_empty_messages(self):
        client = MagicMock()
        notes = extract_memory_notes(client, [])
        self.assertEqual(notes, [])
        client.generate.assert_not_called()

    def test_llm_failure_returns_empty(self):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API error")

        messages = [_make_message("user", "hello", 1000)]
        notes = extract_memory_notes(client, messages)
        self.assertEqual(notes, [])

    def test_no_notes_found(self):
        client = MagicMock()
        client.generate.return_value = "[]"

        messages = [_make_message("user", "おはよう", 1000)]
        notes = extract_memory_notes(client, messages)
        self.assertEqual(notes, [])


class TestExtractAndStore(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.client = MagicMock()

    def tearDown(self):
        self.conn.close()

    def test_extract_and_store(self):
        self.client.generate.return_value = '["fact A", "fact B"]'
        messages = [_make_message("user", "some content", 1000)]

        stored = extract_and_store_notes(
            self.client,
            self.conn,
            messages,
            thread_id="t1",
            source_pulse_id="pulse_001",
        )
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[0].content, "fact A")
        self.assertEqual(stored[0].source_pulse_id, "pulse_001")

        # Verify in DB
        unresolved = get_unresolved_notes(self.conn)
        self.assertEqual(len(unresolved), 2)

    def test_no_notes_stores_nothing(self):
        self.client.generate.return_value = "[]"
        messages = [_make_message("user", "hello", 1000)]

        stored = extract_and_store_notes(
            self.client, self.conn, messages, thread_id="t1",
        )
        self.assertEqual(len(stored), 0)
        self.assertEqual(count_unresolved_notes(self.conn), 0)

    def test_existing_notes_included_in_prompt(self):
        """Existing unresolved notes should be passed to LLM for dedup."""
        # Pre-populate some notes
        add_memory_notes(self.conn, thread_id="t1", notes=["既存メモA", "既存メモB"])

        self.client.generate.return_value = '["新しい情報"]'
        messages = [_make_message("user", "new topic", 1000)]

        extract_and_store_notes(
            self.client, self.conn, messages, thread_id="t1",
        )

        # Check that existing notes were in the prompt
        call_args = self.client.generate.call_args
        prompt = call_args[1]["messages"][0]["content"] if "messages" in call_args[1] else call_args[0][0][0]["content"]
        self.assertIn("既存メモA", prompt)
        self.assertIn("既存メモB", prompt)


class TestMakeBatchCallback(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.client = MagicMock()

    def tearDown(self):
        self.conn.close()

    def test_callback_stores_notes(self):
        self.client.generate.return_value = '["learned something"]'

        callback = make_batch_callback(
            self.client, self.conn, thread_id="t1",
        )

        messages = [_make_message("user", "interesting topic", 1000)]
        callback(messages)

        self.assertEqual(count_unresolved_notes(self.conn), 1)
        notes = get_unresolved_notes(self.conn)
        self.assertEqual(notes[0].content, "learned something")

    def test_callback_swallows_errors(self):
        self.client.generate.side_effect = RuntimeError("boom")

        callback = make_batch_callback(
            self.client, self.conn, thread_id="t1",
        )

        messages = [_make_message("user", "test", 1000)]
        # Should not raise
        callback(messages)
        self.assertEqual(count_unresolved_notes(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
