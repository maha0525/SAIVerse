import unittest
from unittest.mock import patch, MagicMock
import os
import json
from typing import List, Dict, Iterator

# テスト対象のモジュールをインポート
from llm_clients import (
    LLMClient,
    OpenAIClient,
    GeminiClient,
    OllamaClient,
    get_llm_client,
    OPENAI_TOOLS_SPEC,
)

class TestLLMClients(unittest.TestCase):

    def setUp(self):
        os.environ['OPENAI_API_KEY'] = 'test_openai_key'
        os.environ['GEMINI_API_KEY'] = 'test_gemini_key'

    def test_get_llm_client(self):
        # OpenAIClientのテスト
        client = get_llm_client("gpt-4.1-nano", "openai", 1000)
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.model, "gpt-4.1-nano")

        # GeminiClientのテスト
        client = get_llm_client("gemini-1.5-flash", "gemini", 1000)
        self.assertIsInstance(client, GeminiClient)
        self.assertEqual(client.model, "gemini-1.5-flash")

        # OllamaClientのテスト
        client = get_llm_client("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", "ollama", 1000)
        self.assertIsInstance(client, OllamaClient)
        self.assertEqual(client.model, "hf.co/unsloth/gemma-3-1b-it-GGUF:BF16")
        self.assertEqual(client.context_length, 1000)

    @patch('llm_clients.OpenAI')
    def test_openai_client_generate(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        mock_client_instance.chat.completions.create.return_value.choices[0].message.content = "Test OpenAI response"

        client = OpenAIClient("gpt-4.1-nano")
        messages = [{"role": "user", "content": "Hello"}]
        response = client.generate(messages)

        self.assertEqual(response, "Test OpenAI response")
        mock_client_instance.chat.completions.create.assert_called_once_with(
            model="gpt-4.1-nano",
            messages=messages,
            tools=OPENAI_TOOLS_SPEC,
            tool_choice="auto",
            n=1,
        )

    @patch('llm_clients.OpenAI')
    def test_openai_client_generate_stream(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        # ストリーム応答のモック
        mock_chunk1 = MagicMock()
        delta1 = MagicMock()
        delta1.content = "Stream "
        delta1.tool_calls = None
        mock_choice1 = MagicMock(delta=delta1)
        mock_chunk1.choices = [mock_choice1]

        mock_chunk2 = MagicMock()
        delta2 = MagicMock()
        delta2.content = "test"
        delta2.tool_calls = None
        mock_choice2 = MagicMock(delta=delta2)
        mock_chunk2.choices = [mock_choice2]

        mock_client_instance.chat.completions.create.return_value = [mock_chunk1, mock_chunk2]

        client = OpenAIClient("gpt-4.1-nano")
        messages = [{"role": "user", "content": "Hello"}]
        response_generator = client.generate_stream(messages)

        self.assertEqual(list(response_generator), ["Stream ", "test"])
        mock_client_instance.chat.completions.create.assert_called_once_with(
            model="gpt-4.1-nano",
            messages=messages,
            tools=OPENAI_TOOLS_SPEC,
            tool_choice="auto",
            stream=True
        )

    @patch('llm_clients.genai')
    def test_gemini_client_generate(self, mock_genai):
        mock_client_instance = MagicMock()
        mock_genai.Client.return_value = mock_client_instance
        mock_resp = MagicMock()
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [MagicMock(text="Test Gemini response", function_call=None)]
        mock_resp.candidates = [mock_candidate]
        mock_client_instance.models.generate_content.return_value = mock_resp

        client = GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hello"}]
        response = client.generate(messages)

        self.assertEqual(response, "Test Gemini response")
        # Geminiのメッセージ変換ロジックも考慮してアサーション
        mock_genai.Client.return_value.models.generate_content.assert_called_once()
        args, kwargs = mock_genai.Client.return_value.models.generate_content.call_args
        self.assertEqual(kwargs['model'], "gemini-1.5-flash")
        self.assertTrue(kwargs['config'].tools)
        # contentsの構造を考慮して検証
        self.assertEqual(len(kwargs['contents']), 1)
        self.assertEqual(kwargs['contents'][0].role, "user")
        self.assertEqual(kwargs['contents'][0].parts[0].text, "Hello")

    @patch('llm_clients.genai')
    def test_gemini_client_generate_stream(self, mock_genai):
        mock_client_instance = MagicMock()
        mock_genai.Client.return_value = mock_client_instance

        # ストリーム応答のモック
        mock_chunk1 = MagicMock()
        cand1 = MagicMock()
        cand1.content = MagicMock()
        cand1.content.parts = [MagicMock(text="Stream ", function_call=None)]
        mock_chunk1.candidates = [cand1]
        mock_chunk2 = MagicMock()
        cand2 = MagicMock()
        cand2.content = MagicMock()
        cand2.content.parts = [MagicMock(text="test", function_call=None)]
        mock_chunk2.candidates = [cand2]
        mock_client_instance.models.generate_content_stream.return_value = [mock_chunk1, mock_chunk2]

        client = GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hello"}]
        response_generator = client.generate_stream(messages)

        self.assertEqual(list(response_generator), ["Stream ", "test"])
        mock_genai.Client.return_value.models.generate_content_stream.assert_called_once()
        args, kwargs = mock_genai.Client.return_value.models.generate_content_stream.call_args
        self.assertEqual(kwargs['model'], "gemini-1.5-flash")
        self.assertTrue(kwargs['config'].tools)
        # contentsの構造を考慮して検証
        self.assertEqual(len(kwargs['contents']), 1)
        self.assertEqual(kwargs['contents'][0].role, "user")
        self.assertEqual(kwargs['contents'][0].parts[0].text, "Hello")

    @patch('llm_clients.requests.post')
    def test_ollama_client_generate(self, mock_post):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [{
                "message": {"content": "Test Ollama response"}
            }]
        }
        mock_post.return_value = mock_response

        client = OllamaClient("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", 1000)
        messages = [{"role": "user", "content": "Hello"}]
        response = client.generate(messages)

        self.assertEqual(response, "Test Ollama response")
        mock_post.assert_called_once_with(
            client.url,
            json={
                "model": client.model,
                "messages": messages,
                "stream": False,
                "options": {"num_ctx": client.context_length}
            },
            timeout=300,
        )

    @patch('llm_clients.requests.post')
    def test_ollama_client_generate_stream(self, mock_post):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        # ストリーム応答のモック
        def iter_lines_mock():
            yield b'data: {"choices":[{"delta":{"content":"Stream "}}]}' + b'\n'
            yield b'data: {"choices":[{"delta":{"content":"test"}}]}' + b'\n'
            yield b'data: [DONE]' + b'\n'
        mock_response.iter_lines.return_value = iter_lines_mock()
        mock_post.return_value = mock_response

        client = OllamaClient("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", 1000)
        messages = [{"role": "user", "content": "Hello"}]
        response_generator = client.generate_stream(messages)

        self.assertEqual(list(response_generator), ["Stream ", "test"])
        mock_post.assert_called_once_with(
            client.url,
            json={
                "model": client.model,
                "messages": messages,
                "stream": True,
                "options": {"num_ctx": client.context_length}
            },
            timeout=300,
            stream=True,
        )

if __name__ == '__main__':
    unittest.main()
