"""Tests for entity_extractor module."""

import json
import unittest
from unittest.mock import MagicMock, patch

from sai_memory.memory.entity_extractor import (
    ExtractedEntity,
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

    def test_empty_response(self):
        self.assertEqual(_parse_extraction_response(""), [])
        self.assertEqual(_parse_extraction_response(None), [])

    def test_empty_entities(self):
        response = json.dumps({"entities": []})
        self.assertEqual(_parse_extraction_response(response), [])

    def test_invalid_json(self):
        result = _parse_extraction_response("this is not json")
        self.assertEqual(result, [])

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

    def test_llm_error(self):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM error")

        messages = [
            Message(id="1", thread_id="t", role="user", content="test",
                    resource_id="r", created_at=1000, metadata={}),
        ]

        result = extract_entities(client, messages)
        self.assertEqual(result, [])


class TestReflectToMemopedia(unittest.TestCase):
    """Test Memopedia reflection logic."""

    def test_append_to_existing_page(self):
        memopedia = MagicMock()
        page = MagicMock()
        page.id = "page_123"
        memopedia.find_by_title.return_value = page

        entities = [
            ExtractedEntity(name="まはー", category="people", notes=["新しい情報"]),
        ]

        results = reflect_to_memopedia(entities, memopedia, source_time=1711900000)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].is_new_page)
        self.assertEqual(results[0].page_id, "page_123")
        memopedia.append_to_content.assert_called_once()

    def test_create_new_page_with_summary(self):
        memopedia = MagicMock()
        memopedia.find_by_title.return_value = None
        new_page = MagicMock()
        new_page.id = "new_page_456"
        memopedia.create_page.return_value = new_page

        entities = [
            ExtractedEntity(name="エイド", category="people", summary="ソフィーの一人であるAI", notes=["AIアシスタント"]),
        ]

        results = reflect_to_memopedia(entities, memopedia, source_time=1711900000)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_new_page)
        self.assertEqual(results[0].page_id, "new_page_456")
        memopedia.create_page.assert_called_once()
        call_kwargs = memopedia.create_page.call_args
        self.assertEqual(call_kwargs.kwargs["parent_id"], "root_people")
        self.assertEqual(call_kwargs.kwargs["title"], "エイド")
        self.assertEqual(call_kwargs.kwargs["summary"], "ソフィーの一人であるAI")

    @patch("sai_memory.memopedia.storage.update_page")
    def test_existing_page_gets_summary_if_empty(self, mock_update):
        memopedia = MagicMock()
        page = MagicMock()
        page.id = "page_123"
        page.summary = ""  # Empty summary
        memopedia.find_by_title.return_value = page

        entities = [
            ExtractedEntity(name="まはー", category="people", summary="ユーザー", notes=["新情報"]),
        ]

        reflect_to_memopedia(entities, memopedia, source_time=1711900000)
        mock_update.assert_called_once_with(memopedia.conn, "page_123", summary="ユーザー")

    def test_existing_page_keeps_summary_if_present(self):
        memopedia = MagicMock()
        page = MagicMock()
        page.id = "page_123"
        page.summary = "既存の概要"
        memopedia.find_by_title.return_value = page

        entities = [
            ExtractedEntity(name="まはー", category="people", summary="新しい概要", notes=["新情報"]),
        ]

        # Should not attempt to update summary since page already has one
        reflect_to_memopedia(entities, memopedia, source_time=1711900000)
        # append_to_content should be called, but no summary update
        memopedia.append_to_content.assert_called_once()

    def test_empty_notes_skipped(self):
        memopedia = MagicMock()
        entities = [
            ExtractedEntity(name="Empty", category="terms", notes=[]),
        ]
        results = reflect_to_memopedia(entities, memopedia)
        self.assertEqual(results, [])

    def test_category_to_root_mapping(self):
        memopedia = MagicMock()
        memopedia.find_by_title.return_value = None
        new_page = MagicMock()
        new_page.id = "p"
        memopedia.create_page.return_value = new_page

        for category, expected_root in [
            ("people", "root_people"),
            ("terms", "root_terms"),
            ("events", "root_events"),
            ("plans", "root_plans"),
        ]:
            memopedia.reset_mock()
            memopedia.find_by_title.return_value = None
            memopedia.create_page.return_value = new_page

            entities = [ExtractedEntity(name="Test", category=category, notes=["note"])]
            reflect_to_memopedia(entities, memopedia)
            self.assertEqual(
                memopedia.create_page.call_args.kwargs["parent_id"],
                expected_root,
                f"Category '{category}' should map to '{expected_root}'",
            )


if __name__ == "__main__":
    unittest.main()
