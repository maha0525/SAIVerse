import unittest
from unittest.mock import patch, MagicMock
import os
import json
from typing import List, Dict, Iterator
from google.genai import types as genai_types

os.environ.setdefault('SAIVERSE_SKIP_TOOL_IMPORTS', '1')

# テスト対象のモジュールをインポート
import llm_clients
import tools as saiverse_tools
from llm_clients import (
    LLMClient,
    OpenAIClient,
    AnthropicClient,
    GeminiClient,
    OllamaClient,
    get_llm_client,
    OPENAI_TOOLS_SPEC,
)

if not saiverse_tools.OPENAI_TOOLS_SPEC:
    saiverse_tools._autodiscover_tools()
if not saiverse_tools.OPENAI_TOOLS_SPEC:
    saiverse_tools.OPENAI_TOOLS_SPEC.append({
        "type": "function",
        "function": {
            "name": "test_tool",
            "parameters": {"type": "object", "properties": {}}
        }
    })
if not saiverse_tools.GEMINI_TOOLS_SPEC:
    saiverse_tools.GEMINI_TOOLS_SPEC.append(genai_types.Tool(function_declarations=[]))

class TestLLMClients(unittest.TestCase):

    def setUp(self):
        os.environ['OPENAI_API_KEY'] = 'test_openai_key'
        os.environ['GEMINI_API_KEY'] = 'test_gemini_key'
        os.environ['GEMINI_FREE_API_KEY'] = 'test_free_key'
        os.environ['ANTHROPIC_API_KEY'] = 'test_anthropic_key'
        os.environ.pop('SAIVERSE_DISABLE_GEMINI_STREAMING', None)

    def test_get_llm_client(self):
        # OpenAIClientのテスト
        client = get_llm_client("gpt-4.1-nano", "openai", 1000)
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.model, "gpt-4.1-nano")

        # AnthropicClientのテスト
        client = get_llm_client("claude-sonnet-4-5", "anthropic", 1000)
        self.assertIsInstance(client, AnthropicClient)
        self.assertEqual(client.model, "claude-sonnet-4-5")
        thinking_cfg = client._request_kwargs.get("extra_body", {}).get("thinking")
        self.assertIsNotNone(thinking_cfg)
        self.assertEqual(thinking_cfg.get("type"), "enabled")

        # GeminiClientのテスト
        client = get_llm_client("gemini-1.5-flash", "gemini", 1000)
        self.assertIsInstance(client, GeminiClient)
        self.assertEqual(client.model, "gemini-1.5-flash")

        # OllamaClientのテスト
        client = get_llm_client("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", "ollama", 1000)
        self.assertIsInstance(client, OllamaClient)
        self.assertEqual(client.model, "hf.co/unsloth/gemma-3-1b-it-GGUF:BF16")
        self.assertEqual(client.context_length, 1000)

    @patch('llm_clients.openai.OpenAI')
    def test_get_llm_client_custom_openai_base(self, mock_openai):
        os.environ['NVIDIA_API_KEY'] = 'test_nim_key'
        self.addCleanup(lambda: os.environ.pop('NVIDIA_API_KEY', None))

        client = get_llm_client("stockmark/stockmark-2-100b-instruct", "openai", 32768)

        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.model, "stockmark/stockmark-2-100b-instruct")
        mock_openai.assert_called_once_with(
            api_key='test_nim_key',
            base_url='https://integrate.api.nvidia.com/v1'
        )

    @patch('llm_router.client')
    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate(self, mock_openai, mock_router_client):
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

    @patch('llm_router.client')
    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_with_schema(self, mock_openai, mock_router_client):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        choice = MagicMock()
        choice.message.content = "Structured response"
        mock_client_instance.chat.completions.create.return_value.choices = [choice]

        client = OpenAIClient("gpt-4.1-nano")
        messages = [{"role": "user", "content": "Hello"}]
        schema = {"title": "Decision", "type": "object", "properties": {}, "required": []}
        response = client.generate(messages, tools=[], response_schema=schema)

        self.assertEqual(response, "Structured response")
        mock_client_instance.chat.completions.create.assert_called_once()
        _, kwargs = mock_client_instance.chat.completions.create.call_args
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)
        self.assertIn("response_format", kwargs)
        rf = kwargs["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["schema"], schema)
        self.assertEqual(rf["json_schema"]["name"], "Decision")
        self.assertTrue(rf["json_schema"]["strict"])
        self.assertIsNone(kwargs.get("temperature"))

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_host_role_is_system(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        mock_choice = MagicMock()
        mock_choice.message.content = "ok"
        mock_client_instance.chat.completions.create.return_value.choices = [mock_choice]

        client = OpenAIClient("gpt-4.1-nano")
        messages = [
            {"role": "host", "content": "Entrance notice"},
            {"role": "user", "content": "Hello"},
        ]

        client.generate(messages, tools=[])

        _, kwargs = mock_client_instance.chat.completions.create.call_args
        sent_messages = kwargs["messages"]
        self.assertEqual(sent_messages[0]["role"], "system")
        self.assertEqual(sent_messages[1]["role"], "user")

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_configure_parameters(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        mock_choice = MagicMock()
        mock_choice.message.content = "ok"
        mock_client_instance.chat.completions.create.return_value.choices = [mock_choice]

        client = OpenAIClient("gpt-4.1-nano")
        client.configure_parameters({"temperature": 0.2, "reasoning_effort": "low", "verbosity": "high"})
        self.assertEqual(client._request_kwargs["temperature"], 0.2)
        self.assertEqual(client._request_kwargs["reasoning_effort"], "low")
        self.assertNotIn("verbosity", client._request_kwargs)

        messages = [{"role": "user", "content": "Hi"}]
        client.generate(messages, tools=[])

        _, kwargs = mock_client_instance.chat.completions.create.call_args
        self.assertEqual(kwargs["temperature"], 0.2)

        client.configure_parameters({"temperature": None})
        self.assertNotIn("temperature", client._request_kwargs)

    @patch('llm_router.client')
    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_stream(self, mock_openai, mock_router_client):
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

    @patch('llm_router.client')
    @patch('llm_clients.gemini.genai')
    def test_gemini_client_generate(self, mock_genai, mock_router_client):
        mock_client_instance = MagicMock()
        mock_genai.Client.return_value = mock_client_instance
        mock_resp = MagicMock()
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [
            MagicMock(text="Test ", function_call=None, thought=False),
            MagicMock(text="Gemini response", function_call=None, thought=False),
        ]
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

    @patch('llm_clients.gemini.GeminiClient._start_stream')
    @patch('llm_router.client')
    @patch('llm_clients.gemini.genai')
    def test_gemini_client_generate_stream(self, mock_genai, mock_router_client, mock_start_stream):
        mock_client_instance = MagicMock()
        mock_genai.Client.return_value = mock_client_instance

        # ストリーム応答のモック
        mock_chunk1 = MagicMock()
        cand1 = MagicMock()
        cand1.content = MagicMock()
        cand1.content.parts = [
            MagicMock(text="Stream ", function_call=None, thought=False),
            MagicMock(text="test", function_call=None, thought=False),
        ]
        cand1.index = 0
        mock_chunk1.candidates = [cand1]

        mock_chunk2 = MagicMock()
        cand2 = MagicMock()
        cand2.content = MagicMock()
        cand2.content.parts = [MagicMock(text="Stream test!", function_call=None, thought=False)]
        cand2.index = 0
        cand2.finish_reason = "STOP"
        mock_chunk2.candidates = [cand2]

        mock_start_stream.return_value = [mock_chunk1, mock_chunk2]

        client = GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hello"}]
        response_generator = client.generate_stream(messages)

        outputs = list(response_generator)
        mock_start_stream.assert_called_once()
        self.assertEqual(outputs, ["Stream test", "!"])

    def test_anthropic_thinking_override(self):
        client = AnthropicClient(
            "claude-sonnet-4-5",
            config={"thinking_budget": 2048, "thinking_type": "enabled", "thinking_effort": "medium"}
        )
        thinking_cfg = client._request_kwargs.get("extra_body", {}).get("thinking")
        self.assertEqual(thinking_cfg.get("budget_tokens"), 2048)
        self.assertEqual(thinking_cfg.get("type"), "enabled")
        self.assertEqual(thinking_cfg.get("effort"), "medium")

    @patch('llm_router.client')
    @patch('llm_clients.gemini.genai')
    def test_gemini_client_free_key_fallback(self, mock_genai, mock_router_client):
        mock_free = MagicMock()
        mock_paid = MagicMock()
        mock_genai.Client.side_effect = [mock_free, mock_paid]
        mock_free.models.generate_content.side_effect = Exception("429")
        mock_paid_resp = MagicMock()
        cand = MagicMock()
        cand.content.parts = [MagicMock(text="OK", function_call=None, thought=False)]
        mock_paid_resp.candidates = [cand]
        mock_paid.models.generate_content.return_value = mock_paid_resp

        client = GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hi"}]
        response = client.generate(messages)

        self.assertEqual(response, "OK")
        mock_paid.models.generate_content.assert_called_once()

    @patch('llm_clients.ollama.OllamaClient._probe_base', return_value='http://ollama.test')
    @patch('llm_clients.ollama.requests.post')
    def test_ollama_client_generate(self, mock_post, mock_probe):
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
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], client.url)
        payload = kwargs["json"]
        self.assertEqual(payload["model"], client.model)
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["stream"], False)
        self.assertEqual(payload["options"], {"num_ctx": client.context_length})
        self.assertNotIn("response_format", payload)
        self.assertEqual(kwargs["timeout"], (3, 300))

    @patch('llm_clients.ollama.OllamaClient._probe_base', return_value='http://ollama.test')
    @patch('llm_clients.ollama.requests.post')
    def test_ollama_client_generate_with_schema(self, mock_post, mock_probe):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}]
        }
        mock_post.return_value = mock_response

        client = OllamaClient("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", 1000)
        messages = [{"role": "user", "content": "Hello"}]
        schema = {"title": "Decision", "type": "object", "properties": {}, "required": []}
        client.generate(messages, response_schema=schema)

        http_calls = [c for c in mock_post.call_args_list if c.args and isinstance(c.args[0], str)]
        self.assertGreaterEqual(len(http_calls), 1)
        first_url = http_calls[0].args[0]
        last_url = http_calls[-1].args[0]
        self.assertEqual(first_url, client.chat_url)
        self.assertEqual(last_url, client.url)
        payload = http_calls[-1].kwargs["json"]
        self.assertIn("format", payload)
        self.assertEqual(payload["format"]["json_schema"]["schema"], schema)
        self.assertIsNone(payload["options"].get("temperature"))

    @patch('llm_clients.ollama.OllamaClient._probe_base', return_value='http://ollama.test')
    @patch('llm_clients.ollama.requests.post')
    def test_ollama_client_generate_stream(self, mock_post, mock_probe):
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
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], client.url)
        payload = kwargs["json"]
        self.assertEqual(payload["model"], client.model)
        self.assertEqual(payload["messages"], messages)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["options"], {"num_ctx": client.context_length})
        self.assertNotIn("response_format", payload)
        self.assertEqual(kwargs["timeout"], (3, 300))
        self.assertTrue(kwargs["stream"])

    @patch('llm_clients.gemini.types.GenerateContentConfig')
    @patch.object(llm_clients.GeminiClient, "_schema_from_json", return_value=MagicMock())
    @patch('llm_clients.gemini.genai')
    @patch('llm_router.client')
    def test_gemini_client_generate_with_schema(self, mock_router_client, mock_genai, mock_schema_conv, mock_config_cls):
        mock_client_instance = MagicMock()
        mock_genai.Client.return_value = mock_client_instance
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [MagicMock(text="Structured", function_call=None, thought=False)]
        mock_resp = MagicMock()
        mock_resp.candidates = [mock_candidate]
        mock_client_instance.models.generate_content.return_value = mock_resp

        client = llm_clients.GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hello"}]
        schema = {"title": "Decision", "type": "object", "properties": {}, "required": []}

        response = client.generate(messages, tools=[], response_schema=schema)

        self.assertEqual(response, "Structured")
        mock_schema_conv.assert_called_once_with(schema)
        mock_config_cls.assert_called()
        config_kwargs = mock_config_cls.call_args.kwargs
        self.assertEqual(config_kwargs.get("response_mime_type"), "application/json")
        self.assertIn("response_schema", config_kwargs)

if __name__ == '__main__':
    unittest.main()
