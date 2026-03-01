import unittest
from unittest.mock import patch, MagicMock
import os
import json
import httpx
from typing import List, Dict, Iterator
from google.genai import types as genai_types

os.environ.setdefault('SAIVERSE_SKIP_TOOL_IMPORTS', '1')

# テスト対象のモジュールをインポート
import llm_clients
from llm_clients import openai_errors
from llm_clients.openai import _prepare_openai_messages
from llm_clients import anthropic as anthropic_module
from llm_clients import openai_runtime
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
from llm_clients.exceptions import InvalidRequestError

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
        os.environ['CLAUDE_API_KEY'] = 'test_anthropic_key'
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
        # AnthropicClient uses _thinking_config (not _request_kwargs)
        if client._thinking_config:
            self.assertEqual(client._thinking_config.get("type"), "enabled")

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

        config = {
            "model": "stockmark/stockmark-2-100b-instruct",
            "provider": "openai",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key_env": "NVIDIA_API_KEY",
        }
        client = get_llm_client("stockmark-stockmark-2-100b-instruct", "openai", 32768, config=config)

        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.model, "stockmark/stockmark-2-100b-instruct")
        mock_openai.assert_called_once_with(
            api_key='test_nim_key',
            base_url='https://integrate.api.nvidia.com/v1'
        )

    @patch('llm_clients.factory.OpenAIClient')
    def test_get_llm_client_forwards_openai_extra_kwargs(self, mock_openai_client):
        config = {
            "model": "gpt-4.1",
            "provider": "openai",
            "structured_output_mode": " json_object ",
            "reasoning_passback_field": " reasoning_details ",
        }

        get_llm_client("gpt-4.1", "openai", 8192, config=config)

        _, kwargs = mock_openai_client.call_args
        self.assertEqual(kwargs["structured_output_mode"], "json_object")
        self.assertEqual(kwargs["reasoning_passback_field"], "reasoning_details")

    @patch('llm_clients.openai.OpenAI')
    @patch('llm_clients.openai_message_preparer.prepare_openai_messages')
    def test_nvidia_nim_generate_uses_openai_message_preparer_contract(self, mock_prepare, mock_openai):
        mock_prepare.return_value = [{"role": "user", "content": "prepared"}]
        mock_openai.return_value = MagicMock()

        from llm_clients.nvidia_nim import NvidiaNIMClient

        client = NvidiaNIMClient(
            "nvidia/model",
            supports_images=True,
            max_image_bytes=2048,
            convert_system_to_user=True,
            reasoning_passback_field="reasoning_details",
        )
        client._create_nim_structured_output_via_tool = MagicMock(return_value='{"ok": true}')

        messages = [{"role": "user", "content": "hello"}]
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

        result = client.generate(messages=messages, tools=[], response_schema=schema)

        self.assertEqual(result, '{"ok": true}')
        mock_prepare.assert_called_once_with(
            messages,
            True,
            2048,
            True,
            "reasoning_details",
        )

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        # Build a proper mock response for non-tool mode
        mock_resp = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.usage.prompt_tokens_details = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = "Test OpenAI response"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        messages = [{"role": "user", "content": "Hello"}]
        # tools=[] triggers non-tool path, returns str
        response = client.generate(messages, tools=[])

        self.assertEqual(response, "Test OpenAI response")
        mock_client_instance.chat.completions.create.assert_called_once()
        _, kwargs = mock_client_instance.chat.completions.create.call_args
        self.assertEqual(kwargs["model"], "gpt-4.1-nano")
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_with_schema(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        mock_resp = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.usage.prompt_tokens_details = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = '{"answer": "yes"}'
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        messages = [{"role": "user", "content": "Hello"}]
        schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
        response = client.generate(messages, tools=[], response_schema=schema)

        self.assertEqual(response, {"answer": "yes"})
        mock_client_instance.chat.completions.create.assert_called_once()
        _, kwargs = mock_client_instance.chat.completions.create.call_args
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)
        self.assertIn("response_format", kwargs)
        rf = kwargs["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["name"], "Decision")
        self.assertTrue(rf["json_schema"]["strict"])
        self.assertIsNone(kwargs.get("temperature"))

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_with_schema_invalid_json_raises(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = 'not-json'
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

        with self.assertRaises(InvalidRequestError):
            client.generate([{"role": "user", "content": "Hello"}], tools=[], response_schema=schema)

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_with_schema_json_fence_is_parsed(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = '```json\n{"answer": "yes"}\n```'
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

        response = client.generate([{"role": "user", "content": "Hello"}], tools=[], response_schema=schema)
        self.assertEqual(response, {"answer": "yes"})

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_with_schema_preface_text_is_parsed(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = '了解です。\n{"answer": "yes"}\n以上です。'
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

        response = client.generate([{"role": "user", "content": "Hello"}], tools=[], response_schema=schema)
        self.assertEqual(response, {"answer": "yes"})

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_tool_detection_with_and_without_tool_call(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        tool_call_resp = MagicMock()
        tool_call_resp.usage = None
        tool_call_resp.model_dump_json.return_value = '{}'
        tool_call_choice = MagicMock()
        tool_call_choice.finish_reason = "tool_calls"
        tool_call_choice.message.content = None
        tool_call = MagicMock()
        tool_call.function.name = "search"
        tool_call.function.arguments = '{"query":"x"}'
        tool_call_choice.message.tool_calls = [tool_call]
        tool_call_resp.choices = [tool_call_choice]

        text_resp = MagicMock()
        text_resp.usage = None
        text_resp.model_dump_json.return_value = '{}'
        text_choice = MagicMock()
        text_choice.finish_reason = "stop"
        text_choice.message.content = "no tool"
        text_choice.message.tool_calls = []
        text_resp.choices = [text_choice]

        mock_client_instance.chat.completions.create.side_effect = [tool_call_resp, text_resp]
        client = OpenAIClient("gpt-4.1-nano")
        schema = {"title": "Ignored", "type": "object", "properties": {}}
        tools = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object", "properties": {}}}}]

        tool_result = client.generate([{"role": "user", "content": "Hello"}], tools=tools, response_schema=schema)
        text_result = client.generate([{"role": "user", "content": "Hello"}], tools=tools, response_schema=schema)

        self.assertEqual(tool_result["type"], "tool_call")
        self.assertEqual(tool_result["tool_name"], "search")
        self.assertEqual(tool_result["tool_args"], {"query": "x"})
        self.assertEqual(text_result, {"type": "text", "content": "no tool"})

        for call in mock_client_instance.chat.completions.create.call_args_list:
            kwargs = call.kwargs
            self.assertIn("tools", kwargs)
            self.assertNotIn("response_format", kwargs)

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_host_role_is_system(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        mock_resp = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.usage.prompt_tokens_details = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = "ok"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

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
        mock_resp = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.usage.prompt_tokens_details = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = "ok"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

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

    @patch('llm_clients.openai.OpenAI')
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
        # Pass tools=[] to avoid tool routing
        response_generator = client.generate_stream(messages, tools=[])

        self.assertEqual(list(response_generator), ["Stream ", "test"])

    @patch('llm_clients.openai.OpenAI')
    def test_openai_stream_tool_call_fragments_are_reconstructed(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        chunk1 = MagicMock()
        delta1 = MagicMock()
        delta1.content = None
        call1 = MagicMock()
        call1.id = "call_1"
        call1.function.name = "search"
        call1.function.arguments = '{"query":'
        delta1.tool_calls = [call1]
        chunk1.choices = [MagicMock(delta=delta1)]

        chunk2 = MagicMock()
        delta2 = MagicMock()
        delta2.content = None
        call2 = MagicMock()
        call2.id = None
        call2.function.name = None
        call2.function.arguments = ' "tokyo"}'
        delta2.tool_calls = [call2]
        chunk2.choices = [MagicMock(delta=delta2)]

        mock_client_instance.chat.completions.create.return_value = [chunk1, chunk2]

        client = OpenAIClient("gpt-4.1-nano")
        list(client.generate_stream([{"role": "user", "content": "find"}], tools=[{"type": "function", "function": {"name": "search", "parameters": {"type": "object", "properties": {}}}}]))

        detection = client.consume_tool_detection()
        self.assertEqual(detection["type"], "tool_call")
        self.assertEqual(detection["tool_name"], "search")
        self.assertEqual(detection["tool_args"], {"query": "tokyo"})

    @patch('llm_clients.openai.OpenAI')
    def test_openai_stream_emits_thinking_event(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        chunk = MagicMock()
        delta = MagicMock()
        delta.tool_calls = None
        delta.reasoning = "step by step"
        delta.content = None
        delta.model_dump.return_value = {"reasoning": "step by step"}
        chunk.choices = [MagicMock(delta=delta)]
        mock_client_instance.chat.completions.create.return_value = [chunk]

        client = OpenAIClient("gpt-4.1-nano")
        out = list(client.generate_stream([{"role": "user", "content": "hi"}], tools=[]))
        self.assertEqual(out, [{"type": "thinking", "content": "step by step"}])

    @patch('llm_clients.openai.OpenAI')
    def test_openai_stream_content_filter_raises(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        chunk = MagicMock()
        choice = MagicMock()
        choice.finish_reason = "content_filter"
        delta = MagicMock()
        delta.tool_calls = None
        delta.content = None
        choice.delta = delta
        chunk.choices = [choice]
        mock_client_instance.chat.completions.create.return_value = [chunk]

        client = OpenAIClient("gpt-4.1-nano")
        with self.assertRaisesRegex(Exception, "OpenAI output blocked by content filter"):
            list(client.generate_stream([{"role": "user", "content": "hi"}], tools=[]))

    @patch('llm_clients.openai.OpenAI')
    def test_openai_stream_history_prefix_emitted_only_on_first_text(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        chunk1 = MagicMock()
        delta1 = MagicMock()
        delta1.tool_calls = None
        delta1.content = [{"type": "reasoning", "text": "internal"}]
        chunk1.choices = [MagicMock(delta=delta1)]

        chunk2 = MagicMock()
        delta2 = MagicMock()
        delta2.tool_calls = None
        delta2.content = "hello"
        chunk2.choices = [MagicMock(delta=delta2)]

        mock_client_instance.chat.completions.create.return_value = [chunk1, chunk2]

        client = OpenAIClient("gpt-4.1-nano")
        out = list(client.generate_stream(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "search", "parameters": {"type": "object", "properties": {}}}}],
            history_snippets=["h1", "h2"],
        ))
        self.assertEqual(out, [{"type": "thinking", "content": "internal"}, "h1\nh2\n", "hello"])

    def test_prepare_openai_messages_regression_host_and_empty_and_reasoning(self):
        messages = [
            {"role": "host", "content": "Host instruction"},
            {"role": "user", "content": ""},
            {
                "role": "assistant",
                "content": "ok",
                "metadata": {"reasoning_details": [{"type": "reasoning.text", "text": "r"}]},
            },
            {"role": "assistant", "content": "", "tool_calls": []},
        ]

        prepared = _prepare_openai_messages(
            messages,
            supports_images=False,
            reasoning_passback_field="reasoning_details",
        )

        self.assertEqual(prepared[0]["role"], "system")
        self.assertEqual(len(prepared), 2)
        self.assertEqual(prepared[1]["reasoning_details"], [{"type": "reasoning.text", "text": "r"}])

    @patch("llm_clients.openai_message_preparer.image_summary_note", return_value="[image summary]")
    @patch("llm_clients.openai_message_preparer.load_image_bytes_for_llm", return_value=(b"img-bytes", "image/png"))
    @patch(
        "llm_clients.openai_message_preparer.iter_image_media",
        return_value=[{"path": "dummy.png", "mime_type": "image/png", "uri": "saiverse://image/dummy.png"}],
    )
    def test_prepare_openai_messages_regression_supports_images_toggle(
        self,
        _mock_iter_image_media,
        _mock_load_image,
        _mock_summary,
    ):
        messages = [{"role": "user", "content": "hello", "metadata": {"media": [{"uri": "dummy"}]}}]

        prepared_with_images = _prepare_openai_messages(messages, supports_images=True)
        self.assertIsInstance(prepared_with_images[0]["content"], list)
        self.assertEqual(prepared_with_images[0]["content"][0]["type"], "text")
        self.assertEqual(prepared_with_images[0]["content"][1]["type"], "image_url")

        prepared_without_images = _prepare_openai_messages(messages, supports_images=False)
        self.assertEqual(prepared_without_images[0]["content"], "hello\n[image summary]")

    def test_anthropic_request_builder_helpers_are_covered_in_dedicated_tests(self):
        self.assertTrue(True)

    @patch('llm_clients.openai.OpenAI')
    def test_openai_content_filter_message_is_unified(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.finish_reason = "content_filter"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        with self.assertRaisesRegex(Exception, "OpenAI output blocked by content filter"):
            client.generate([{"role": "user", "content": "Hello"}], tools=[])

    @patch('llm_clients.openai.OpenAI')
    def test_openai_tool_mode_content_filter_message_is_unified(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.finish_reason = "content_filter"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        tools = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object", "properties": {}}}}]
        with self.assertRaisesRegex(Exception, "OpenAI output blocked by content filter"):
            client.generate([{"role": "user", "content": "Hello"}], tools=tools)

    def test_openai_runtime_call_with_retry_returns_on_retry(self):
        calls = {"count": 0}

        def _create():
            calls["count"] += 1
            if calls["count"] < 2:
                raise RuntimeError("429")
            return "ok"

        with patch('llm_clients.openai_runtime.time.sleep'):
            result = openai_runtime.call_with_retry(
                _create,
                context="test",
                max_retries=3,
                initial_backoff=0.01,
                should_retry=lambda e: "429" in str(e),
            )
        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)


    def test_openai_error_helpers_should_retry(self):
        self.assertTrue(openai_errors.should_retry(Exception("429 rate limit")))
        self.assertTrue(openai_errors.should_retry(Exception("503 unavailable")))
        self.assertFalse(openai_errors.should_retry(Exception("402 payment required")))

    def test_openai_error_helpers_convert_to_llm_error(self):
        err = openai_errors.convert_to_llm_error(Exception("insufficient_quota"), "streaming")
        self.assertEqual(err.error_code, "payment")

        err = openai_errors.convert_to_llm_error(Exception("content_policy blocked"), "streaming")
        self.assertEqual(err.error_code, "safety_filter")

    @patch('llm_clients.gemini.genai')
    def test_gemini_client_generate(self, mock_genai):
        mock_client_instance = MagicMock()
        mock_genai.Client.return_value = mock_client_instance

        # Non-tool mode uses streaming, so mock generate_content_stream
        mock_chunk = MagicMock()
        mock_chunk.prompt_feedback = None
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [
            MagicMock(text="Test Gemini response", function_call=None, thought=False),
        ]
        mock_chunk.candidates = [mock_candidate]
        mock_chunk.usage_metadata = MagicMock(
            prompt_token_count=10,
            candidates_token_count=5,
            cached_content_token_count=0,
        )
        mock_client_instance.models.generate_content_stream.return_value = [mock_chunk]

        client = GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hello"}]
        # No tools → non-tool path → streaming
        response = client.generate(messages, tools=[])

        self.assertEqual(response, "Test Gemini response")

    @patch('llm_clients.gemini.GeminiClient._start_stream')
    @patch('llm_clients.gemini.genai')
    def test_gemini_client_generate_stream(self, mock_genai, mock_start_stream):
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
        """Test manual thinking mode (legacy, for Sonnet 4.5 / Opus 4.5)."""
        client = AnthropicClient(
            "claude-sonnet-4-5",
            config={"thinking_budget": 2048, "thinking_type": "enabled"}
        )
        self.assertIsNotNone(client._thinking_config)
        self.assertEqual(client._thinking_config.get("budget_tokens"), 2048)
        self.assertEqual(client._thinking_config.get("type"), "enabled")
        self.assertIsNone(client._thinking_effort)

    def test_anthropic_adaptive_thinking(self):
        """Test adaptive thinking mode (Opus 4.6+)."""
        client = AnthropicClient(
            "claude-opus-4-6",
            config={"thinking_type": "adaptive", "thinking_effort": "high"}
        )
        self.assertIsNotNone(client._thinking_config)
        self.assertEqual(client._thinking_config.get("type"), "adaptive")
        # Adaptive mode should NOT have budget_tokens
        self.assertNotIn("budget_tokens", client._thinking_config)
        self.assertEqual(client._thinking_effort, "high")
        # Adaptive thinking should set higher default max_tokens
        self.assertEqual(client._max_tokens, 16000)

    def test_anthropic_adaptive_thinking_with_effort(self):
        """Test adaptive thinking with different effort levels."""
        for effort in ("low", "medium", "high", "max"):
            client = AnthropicClient(
                "claude-opus-4-6",
                config={"thinking_type": "adaptive", "thinking_effort": effort}
            )
            self.assertEqual(client._thinking_effort, effort)

        # Invalid effort should be ignored
        client = AnthropicClient(
            "claude-opus-4-6",
            config={"thinking_type": "adaptive", "thinking_effort": "invalid"}
        )
        self.assertIsNone(client._thinking_effort)

    def test_anthropic_configure_thinking_effort(self):
        """Test that thinking_effort can be changed via configure_parameters."""
        client = AnthropicClient(
            "claude-opus-4-6",
            config={"thinking_type": "adaptive"}
        )
        self.assertIsNone(client._thinking_effort)

        # Set effort via configure_parameters
        client.configure_parameters({"thinking_effort": "medium"})
        self.assertEqual(client._thinking_effort, "medium")

        # Change effort
        client.configure_parameters({"thinking_effort": "max"})
        self.assertEqual(client._thinking_effort, "max")

        # Clear effort
        client.configure_parameters({"thinking_effort": None})
        self.assertIsNone(client._thinking_effort)

        # Invalid effort should be ignored
        client.configure_parameters({"thinking_effort": "invalid"})
        self.assertIsNone(client._thinking_effort)


    @patch('llm_clients.anthropic.time.sleep')
    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_execute_with_retry_retries_rate_limit(self, mock_anthropic, mock_sleep):
        mock_client_instance = MagicMock()
        mock_anthropic.return_value = mock_client_instance
        client = AnthropicClient("claude-sonnet-4-5")

        request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx.Response(429, request=request)
        rate_limit_error = anthropic_module.anthropic.RateLimitError(
            "rate limit", response=response, body=None
        )
        calls = {"count": 0}

        def flaky_call():
            calls["count"] += 1
            if calls["count"] < 3:
                raise rate_limit_error
            return "ok"

        result = client._execute_with_retry(flaky_call, "API call")

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('llm_clients.anthropic.time.sleep')
    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_execute_with_retry_raises_server_error_after_max_retries(self, mock_anthropic, mock_sleep):
        mock_client_instance = MagicMock()
        mock_anthropic.return_value = mock_client_instance
        client = AnthropicClient("claude-sonnet-4-5")

        request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx.Response(503, request=request)
        server_error = anthropic_module.anthropic.APIStatusError(
            "server unavailable", response=response, body=None
        )

        with self.assertRaises(anthropic_module.ServerError):
            client._execute_with_retry(lambda: (_ for _ in ()).throw(server_error), "API call")

        self.assertEqual(mock_sleep.call_count, anthropic_module.MAX_RETRIES - 1)

    @patch('llm_clients.anthropic.time.sleep')
    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_execute_with_retry_raises_timeout_after_max_retries(self, mock_anthropic, mock_sleep):
        mock_client_instance = MagicMock()
        mock_anthropic.return_value = mock_client_instance
        client = AnthropicClient("claude-sonnet-4-5")

        request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
        timeout_error = anthropic_module.anthropic.APITimeoutError(request)

        with self.assertRaises(anthropic_module.LLMTimeoutError):
            client._execute_with_retry(lambda: (_ for _ in ()).throw(timeout_error), "API call")

        self.assertEqual(mock_sleep.call_count, anthropic_module.MAX_RETRIES - 1)

    @patch('llm_clients.anthropic.time.sleep')
    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_execute_with_retry_bad_request_content_policy_maps_to_safety_filter(self, mock_anthropic, mock_sleep):
        mock_client_instance = MagicMock()
        mock_anthropic.return_value = mock_client_instance
        client = AnthropicClient("claude-sonnet-4-5")

        request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx.Response(400, request=request)
        error = anthropic_module.anthropic.BadRequestError(
            "content policy violation", response=response, body=None
        )

        with self.assertRaises(anthropic_module.SafetyFilterError):
            client._execute_with_retry(lambda: (_ for _ in ()).throw(error), "API call")

        mock_sleep.assert_not_called()

    @patch('llm_clients.anthropic.time.sleep')
    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_execute_with_retry_bad_request_non_policy_maps_to_invalid_request(self, mock_anthropic, mock_sleep):
        mock_client_instance = MagicMock()
        mock_anthropic.return_value = mock_client_instance
        client = AnthropicClient("claude-sonnet-4-5")

        request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx.Response(400, request=request)
        error = anthropic_module.anthropic.BadRequestError(
            "invalid request payload", response=response, body=None
        )

        with self.assertRaises(anthropic_module.InvalidRequestError):
            client._execute_with_retry(lambda: (_ for _ in ()).throw(error), "API call")

        mock_sleep.assert_not_called()

    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_build_request_params_consistent_between_generate_and_stream(self, mock_anthropic):
        mock_client_instance = MagicMock()
        mock_anthropic.return_value = mock_client_instance

        mock_response = MagicMock()
        mock_response.usage = None
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "ok"
        mock_response.content = [mock_text_block]
        mock_response.model_dump_json.return_value = '{}'
        mock_client_instance.messages.create.return_value = mock_response

        mock_stream = MagicMock()
        mock_stream.__iter__.return_value = iter(())
        stream_cm = MagicMock()
        stream_cm.__enter__.return_value = mock_stream
        stream_cm.__exit__.return_value = None
        mock_client_instance.messages.stream.return_value = stream_cm

        client = AnthropicClient(
            "claude-sonnet-4-5",
            config={"thinking_type": "adaptive", "thinking_effort": "high"},
        )
        client.configure_parameters({"top_p": 0.9, "top_k": 10})

        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ]
        tools = [{
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }]

        build_result = anthropic_module.build_request_params(
            messages=messages,
            tools=tools,
            response_schema=None,
            temperature=0.3,
            enable_cache=True,
            cache_ttl="5m",
            model=client.model,
            max_tokens=client._max_tokens,
            extra_params=client._extra_params,
            thinking_config=client._thinking_config,
            thinking_effort=client._thinking_effort,
            supports_images=client.supports_images,
            max_image_bytes=client.max_image_bytes,
        )

        client.generate(messages, tools=tools, temperature=0.3, enable_cache=True, cache_ttl="5m")
        list(client.generate_stream(messages, tools=tools, temperature=0.3, enable_cache=True, cache_ttl="5m"))

        _, generate_kwargs = mock_client_instance.messages.create.call_args
        _, stream_kwargs = mock_client_instance.messages.stream.call_args

        self.assertEqual(generate_kwargs, build_result["request_params"])
        self.assertEqual(stream_kwargs, build_result["request_params"])
        self.assertTrue(build_result["use_tools"])
        self.assertFalse(build_result["use_native_structured_output"])

    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_generate_tool_mode_empty_response_raises(self, mock_anthropic):
        mock_anthropic.return_value = MagicMock()
        client = AnthropicClient("claude-sonnet-4-5")

        mock_response = MagicMock()
        mock_response.usage = None
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = ""
        mock_response.content = [mock_text_block]
        mock_response.model_dump_json.return_value = '{}'
        client._execute_with_retry = MagicMock(return_value=mock_response)

        messages = [{"role": "user", "content": "hello"}]
        tools = [{
            "type": "function",
            "function": {"name": "test_tool", "parameters": {"type": "object", "properties": {}}},
        }]

        with self.assertRaises(anthropic_module.LLMEmptyResponseError):
            client.generate(messages, tools=tools)

    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_generate_native_schema_parse_failure_returns_raw_text(self, mock_anthropic):
        mock_anthropic.return_value = MagicMock()
        client = AnthropicClient("claude-opus-4-6", config={"thinking_type": "adaptive"})

        mock_response = MagicMock()
        mock_response.usage = None
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "not-json"
        mock_response.content = [mock_text_block]
        mock_response.model_dump_json.return_value = '{}'
        client._execute_with_retry = MagicMock(return_value=mock_response)

        messages = [{"role": "user", "content": "hello"}]
        schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}}

        result = client.generate(messages, tools=[], response_schema=schema)

        self.assertEqual(result, "not-json")

    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_parse_structured_response_legacy_tool_choice_compatibility(self, mock_anthropic):
        mock_anthropic.return_value = MagicMock()
        client = AnthropicClient("claude-sonnet-4-5")

        mock_tool_response = MagicMock()
        mock_tool_block = MagicMock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.id = "tool_1"
        mock_tool_block.name = "Decision"
        mock_tool_block.input = {"answer": "ok"}
        mock_tool_response.content = [mock_tool_block]

        tool_result = client.parse_structured_response(
            mock_tool_response,
            use_native_structured_output=False,
        )

        self.assertEqual(tool_result, {"answer": "ok"})

        mock_text_response = MagicMock()
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "plain text fallback"
        mock_text_response.content = [mock_text_block]

        text_result = client.parse_structured_response(
            mock_text_response,
            use_native_structured_output=False,
        )

        self.assertEqual(text_result, "plain text fallback")

    @patch('llm_clients.gemini.genai')
    def test_gemini_client_free_key_fallback(self, mock_genai):
        mock_free = MagicMock()
        mock_paid = MagicMock()
        mock_genai.Client.side_effect = [mock_free, mock_paid]

        # Free client streaming fails
        mock_free.models.generate_content_stream.side_effect = Exception("429")

        # Paid client streaming succeeds
        mock_chunk = MagicMock()
        mock_chunk.prompt_feedback = None
        cand = MagicMock()
        cand.content.parts = [MagicMock(text="OK", function_call=None, thought=False)]
        mock_chunk.candidates = [cand]
        mock_chunk.usage_metadata = MagicMock(
            prompt_token_count=10,
            candidates_token_count=5,
            cached_content_token_count=0,
        )
        mock_paid.models.generate_content_stream.return_value = [mock_chunk]

        client = GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hi"}]
        response = client.generate(messages, tools=[])

        self.assertEqual(response, "OK")
        mock_paid.models.generate_content_stream.assert_called_once()

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
    def test_gemini_client_generate_with_schema(self, mock_genai, mock_schema_conv, mock_config_cls):
        mock_client_instance = MagicMock()
        mock_genai.Client.return_value = mock_client_instance

        # Schema mode with tools=[] goes through non-tool streaming path
        mock_chunk = MagicMock()
        mock_chunk.prompt_feedback = None
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [MagicMock(text='{"key":"value"}', function_call=None, thought=False)]
        mock_chunk.candidates = [mock_candidate]
        mock_chunk.usage_metadata = MagicMock(
            prompt_token_count=10,
            candidates_token_count=5,
            cached_content_token_count=0,
        )
        mock_client_instance.models.generate_content_stream.return_value = [mock_chunk]

        client = llm_clients.GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hello"}]
        schema = {"title": "Decision", "type": "object", "properties": {}, "required": []}

        client.generate(messages, tools=[], response_schema=schema)

        mock_schema_conv.assert_called_once_with(schema)
        mock_config_cls.assert_called()
        config_kwargs = mock_config_cls.call_args.kwargs
        self.assertEqual(config_kwargs.get("response_mime_type"), "application/json")
        self.assertIn("response_schema", config_kwargs)

if __name__ == '__main__':
    unittest.main()
