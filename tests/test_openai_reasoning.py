import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from llm_clients.openai import OpenAIClient
from llm_clients.openai_reasoning import (
    extract_reasoning_from_delta,
    extract_reasoning_from_message,
)


class _Delta(SimpleNamespace):
    def model_dump(self):
        return {
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "reasoning_details": self.reasoning_details,
        }


class TestOpenAIReasoningHelpers(unittest.TestCase):
    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "test_openai_key"

    def test_extract_reasoning_from_delta_priority_over_reasoning_details(self):
        delta = {
            "reasoning": [{"text": "r1"}],
            "reasoning_content": "r2",
            "reasoning_details": [{"text": "dup-from-details"}],
        }

        self.assertEqual(extract_reasoning_from_delta(delta), ["r1", "r2"])

    def test_extract_reasoning_from_delta_uses_reasoning_details_only_when_empty(self):
        delta = {"reasoning_details": [{"text": "rd1"}, {"summary": "rd2"}]}

        self.assertEqual(extract_reasoning_from_delta(delta), ["rd1", "rd2"])

    def test_extract_reasoning_from_message_keeps_text_and_reasoning_details(self):
        text, reasoning_entries, reasoning_details = extract_reasoning_from_message(
            {
                "content": "answer",
                "reasoning_content": "think",
                "reasoning_details": [{"index": 0, "text": "detail"}],
            }
        )

        self.assertEqual(text, "answer")
        self.assertEqual(reasoning_entries, [{"title": "", "text": "think"}])
        self.assertEqual(reasoning_details, [{"index": 0, "text": "detail"}])

    @patch("llm_clients.openai.OpenAI")
    def test_generate_and_generate_stream_store_same_reasoning_payload(self, mock_openai):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        resp = MagicMock()
        resp.usage = None
        resp.model_dump_json.return_value = "{}"
        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.message = {
            "content": "ok",
            "reasoning_content": "R",
            "reasoning_details": [{"index": 0, "text": "D"}],
        }
        resp.choices = [choice]

        chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    delta=_Delta(
                        content="ok",
                        reasoning_content="R",
                        reasoning_details=[{"index": 0, "text": "D"}],
                    ),
                )
            ],
            usage=None,
        )

        mock_client.chat.completions.create.side_effect = [resp, [chunk]]

        client = OpenAIClient("gpt-4.1-nano")
        _ = client.generate([{"role": "user", "content": "hello"}], tools=[])
        reasoning_generate = client.consume_reasoning()
        details_generate = client.consume_reasoning_details()

        list(client.generate_stream([{"role": "user", "content": "hello"}], tools=[]))
        reasoning_stream = client.consume_reasoning()
        details_stream = client.consume_reasoning_details()

        self.assertEqual(reasoning_generate, reasoning_stream)
        self.assertEqual(details_generate, details_stream)


if __name__ == "__main__":
    unittest.main()
