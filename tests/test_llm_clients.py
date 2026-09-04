import unittest
from unittest.mock import patch, MagicMock
import os
import json
import httpx2  # anthropic 1.x / openai 3.x run on httpx2: their http_client, Timeout and exception Request/Response are httpx2 types
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
    def test_default_headers_reach_the_openai_sdk(self, mock_openai):
        """default_headers must land on the SDK client, not on request kwargs.

        OpenRouter identifies the calling app by these headers, so they have to
        ride every request to the backend rather than a single call.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": {"HTTP-Referer": "https://saiverse.net"},
            # Shaped after the shipped OpenRouter models (GLM-5 turns reasoning
            # on this way): headers must survive next to request_kwargs, which
            # is why they are not merged into that field.
            "request_kwargs": {"extra_body": {"reasoning": {"enabled": True}}},
        }

        client = get_llm_client("openrouter-test-model", "openai", 8192, config=config)

        _, kwargs = mock_openai.call_args
        self.assertEqual(
            kwargs["default_headers"], {"HTTP-Referer": "https://saiverse.net"},
        )
        self.assertEqual(
            client._request_kwargs, {"extra_body": {"reasoning": {"enabled": True}}},
        )

    @patch('llm_clients.openai.OpenAI')
    def test_malformed_default_headers_do_not_break_the_call(self, mock_openai):
        """A broken header entry is dropped; the LLM call still goes through.

        Attribution is advertising, not function. A user_data override with a
        bad value must not take every conversation down with it.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": {"HTTP-Referer": ["not", "a", "string"], "X-OpenRouter-Title": "SAIVerse"},
        }

        get_llm_client("openrouter-test-model", "openai", 8192, config=config)

        _, kwargs = mock_openai.call_args
        self.assertEqual(kwargs["default_headers"], {"X-OpenRouter-Title": "SAIVerse"})

    @patch('llm_clients.openai.OpenAI')
    def test_default_headers_of_wrong_type_are_ignored(self, mock_openai):
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": "HTTP-Referer: https://saiverse.net",
        }

        get_llm_client("openrouter-test-model", "openai", 8192, config=config)

        _, kwargs = mock_openai.call_args
        self.assertNotIn("default_headers", kwargs)

    def _set_env(self, name, value):
        """Set an env var for one test, restoring any pre-existing value after.

        Plain assignment plus a pop() in cleanup would delete a real key the
        developer had exported, leaking that loss into later tests in the run.
        """
        patcher = patch.dict(os.environ, {name: value})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wire_headers_for(self, mock_openai, config, key="openrouter-test-model"):
        """Build the client through factory, then run OpenAIClient.generate for real.

        Two reasons this cannot be shortened to inspecting constructor
        arguments. The SDK merges its own headers with the configured ones and
        which side wins is the whole question; and extra_headers does not ride
        the client at all — it travels through _request_kwargs into each call
        site, so only the real path shows whether it still arrives.
        """
        from openai import OpenAI as _RealOpenAI

        client = get_llm_client(key, "openai", 8192, config=config)

        captured = []

        def handler(request):
            captured.append(dict(request.headers))
            return httpx2.Response(200, json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }],
            })

        # Swap the mocked SDK for a real one built from the very kwargs factory
        # produced, so everything downstream of construction runs for real.
        # openai 3.x runs on httpx2 (http_client is typed httpx2.Client), so
        # the injected client is httpx2's. 3.7.0 happens not to reject an old
        # httpx.Client (measured: a MockTransport round trip even succeeds),
        # but that is duck-typing luck, not the contract -- stay on the SDK's
        # own stack so the test exercises what production runs on.
        client.client = _RealOpenAI(
            **mock_openai.call_args.kwargs,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        client.generate([{"role": "user", "content": "hi"}], tools=[])
        return captured[-1]

    @patch('llm_clients.openai.OpenAI')
    def test_attribution_headers_reach_the_wire(self, mock_openai):
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": {
                "HTTP-Referer": "https://saiverse.net",
                "X-OpenRouter-Title": "SAIVerse",
                "X-OpenRouter-Categories": "roleplay,general-chat",
            },
        }

        sent = self._wire_headers_for(mock_openai, config)

        self.assertEqual(sent.get("http-referer"), "https://saiverse.net")
        self.assertEqual(sent.get("x-openrouter-title"), "SAIVerse")
        self.assertEqual(sent.get("x-openrouter-categories"), "roleplay,general-chat")

    @patch('llm_clients.openai.OpenAI')
    def test_default_headers_cannot_replace_the_credential(self, mock_openai):
        """A config file must not be able to swap the API key for another value.

        The SDK merges custom default headers *after* the ones it derives from
        api_key, so an Authorization entry would otherwise be the value that
        actually ships — pairing an endpoint vetted by provider_security with a
        credential it never saw.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": {
                "Authorization": "Bearer HIJACKED",
                "Host": "evil.example",
                # Decides which tenant gets billed — same class of hole.
                "OpenAI-Organization": "org-someone-else",
                "HTTP-Referer": "https://saiverse.net",
            },
        }

        sent = self._wire_headers_for(mock_openai, config)

        self.assertEqual(sent.get("authorization"), "Bearer test_or_key")
        self.assertEqual(sent.get("host"), "openrouter.ai")
        self.assertIsNone(sent.get("openai-organization"))
        # The legitimate entry in the same object still goes through.
        self.assertEqual(sent.get("http-referer"), "https://saiverse.net")

    @patch('llm_clients.openai.OpenAI')
    def test_extra_headers_cannot_replace_the_credential(self, mock_openai):
        """The per-request door onto the credential passes the same gate.

        extra_headers outranks both default_headers and the SDK's own auth
        header, so guarding only default_headers would leave the invariant
        ("credentials belong to the client") true on one path and false on
        the other — which is not an invariant.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "request_kwargs": {"extra_headers": {
                "Authorization": "Bearer HIJACKED",
                "X-Trace": "keep-me",
            }},
        }

        sent = self._wire_headers_for(mock_openai, config)

        self.assertEqual(sent.get("authorization"), "Bearer test_or_key")
        # A non-reserved entry in the same object is untouched.
        self.assertEqual(sent.get("x-trace"), "keep-me")

    @patch('llm_clients.openai.OpenAI')
    def test_override_works_across_header_name_spellings(self, mock_openai):
        """Overriding must not depend on matching the shipped capitalisation.

        Header names are case-insensitive, but the SDK merges default_headers
        and extra_headers by exact key — so a differently-spelled override
        would put *both* values on the wire instead of replacing.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": {"HTTP-Referer": "https://saiverse.net"},
            "request_kwargs": {"extra_headers": {"http-referer": "https://other.example"}},
        }

        sent = self._wire_headers_for(mock_openai, config)

        self.assertEqual(sent.get("http-referer"), "https://other.example")
        self.assertNotIn("saiverse.net", sent.get("http-referer", ""))

    @patch('llm_clients.openai.OpenAI')
    def test_non_ascii_header_value_is_dropped_not_raised(self, mock_openai):
        """httpx encodes header values as ASCII, so a Japanese value would
        raise while building the request and stop the conversation — the exact
        outcome "attribution is advertising, not function" rules out.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": {
                "X-OpenRouter-Title": "サイヴァース",
                "X-Broken": "line\nbreak",
                "HTTP-Referer": "https://saiverse.net",
            },
        }

        sent = self._wire_headers_for(mock_openai, config)

        self.assertIsNone(sent.get("x-openrouter-title"))
        self.assertIsNone(sent.get("x-broken"))
        self.assertEqual(sent.get("http-referer"), "https://saiverse.net")

    @patch('llm_clients.openai.OpenAI')
    def test_header_shapes_h11_rejects_are_dropped(self, mock_openai):
        """Values that only fail on the way out still have to fail open.

        h11 validates names and values when it serializes the request — not
        when httpx builds it — so these forms would surface to the user as a
        failed conversation rather than a missing header. Measured against
        h11 0.16.0; inner spaces and tabs are legal and must survive.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "default_headers": {
                "X Bad Name": "v",          # space in the name
                "X-Trailing-LF\n": "v",     # regex anchored with $ would pass this
                "X-Nul": "a\x00b",          # NUL in the value
                "X-Vt": "a\x0bb",           # vertical tab
                "X-Ff": "a\x0cb",           # form feed
                "X-Blank": "   ",           # whitespace-only value
                "X-Leading": " v",          # leading whitespace
                "X-Inner": "a b\tc",        # legal: inner space and tab
                "HTTP-Referer": "https://saiverse.net",
            },
        }

        sent = self._wire_headers_for(mock_openai, config)

        for dropped in ("x bad name", "x-trailing-lf\n", "x-nul", "x-vt", "x-ff",
                        "x-blank", "x-leading"):
            self.assertIsNone(sent.get(dropped), f"{dropped} should have been dropped")
        self.assertEqual(sent.get("x-inner"), "a b\tc")
        self.assertEqual(sent.get("http-referer"), "https://saiverse.net")

    @patch('llm_clients.openai.OpenAI')
    def test_extra_headers_of_wrong_shape_does_not_break_the_call(self, mock_openai):
        """A string where an object belongs must not reach the SDK.

        The SDK raises on it, which would turn a config typo into a failed
        conversation — and the raw value would already have been written to
        the DEBUG log by then.
        """
        self._set_env('OPENROUTER_API_KEY', 'test_or_key')

        config = {
            "model": "test/model",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "request_kwargs": {"extra_headers": "Authorization: Bearer SECRET"},
        }

        sent = self._wire_headers_for(mock_openai, config)

        self.assertEqual(sent.get("authorization"), "Bearer test_or_key")

    def test_debug_log_never_carries_header_values(self):
        """The user runs at DEBUG, so this line lands in a persisted log."""
        from llm_clients.factory import _loggable_kwargs

        for shape in ({"Authorization": "Bearer SECRET"}, "Authorization: Bearer SECRET"):
            rendered = repr(_loggable_kwargs({
                "default_headers": {"Authorization": "Bearer SECRET"},
                "request_kwargs": {"extra_headers": shape},
            }))
            self.assertNotIn("SECRET", rendered)

    @patch('llm_clients.openai.OpenAI')
    @patch('httpx.Client')
    def test_nim_structured_output_path_carries_default_headers(self, mock_httpx, mock_openai):
        """NIM's structured output bypasses the SDK and builds headers by hand.

        Without this the same backend would see different headers depending on
        whether the caller asked for structured output.
        """
        self._set_env('NVIDIA_API_KEY', 'test_nim_key')
        from llm_clients.nvidia_nim import NvidiaNIMClient

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"tool_calls": [
                {"function": {"arguments": '{"a": 1}'}},
            ]}}],
        }
        mock_httpx.return_value.__enter__.return_value.post.return_value = response

        client = NvidiaNIMClient(
            "m",
            api_key_env="NVIDIA_API_KEY",
            base_url="https://integrate.api.nvidia.com/v1",
            default_headers={"HTTP-Referer": "https://saiverse.net"},
        )
        client._create_nim_structured_output_via_tool(
            [{"role": "user", "content": "x"}],
            {"type": "object", "properties": {"a": {"type": "integer"}}},
            None,
        )

        _, post_kwargs = mock_httpx.return_value.__enter__.return_value.post.call_args
        self.assertEqual(post_kwargs["headers"].get("HTTP-Referer"), "https://saiverse.net")
        # Credentials stay owned by the client even on this hand-built path.
        self.assertEqual(post_kwargs["headers"].get("Authorization"), "Bearer test_nim_key")

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
    def test_nvidia_nim_structured_output_empty_raises_empty_response_error(self, mock_openai):
        """NIM の構造化出力が空文字で返ったら EmptyResponseError (RuntimeError ではない)。"""
        from llm_clients.exceptions import EmptyResponseError
        from llm_clients.nvidia_nim import NvidiaNIMClient

        mock_openai.return_value = MagicMock()
        client = NvidiaNIMClient("nvidia/model")
        client._create_nim_structured_output_via_tool = MagicMock(return_value="   ")

        with self.assertRaises(EmptyResponseError) as ctx:
            client.generate(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                response_schema={"type": "object", "properties": {}},
            )
        self.assertEqual(ctx.exception.error_code, "empty_response")

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
    def test_openai_client_gpt5_nano_drops_non_default_temperature(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance
        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = "ok"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-5-nano")
        client.configure_parameters({"temperature": 0.2})
        client.generate([{"role": "user", "content": "hi"}], tools=[], temperature=0.3)

        _, kwargs = mock_client_instance.chat.completions.create.call_args
        self.assertNotIn("temperature", kwargs)

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_with_schema_empty_candidate_raises(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = ""
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-4.1-nano")
        schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

        with self.assertRaises(InvalidRequestError):
            client.generate([{"role": "user", "content": "Hello"}], tools=[], response_schema=schema)

    @patch('llm_clients.openai.OpenAI')
    def test_openai_client_generate_with_schema_uses_parsed_payload_when_content_empty(self, mock_openai):
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_resp.model_dump_json.return_value = '{}'
        mock_choice = MagicMock()
        mock_choice.message.content = ""
        mock_choice.message.parsed = {"answer": "ok"}
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]
        mock_client_instance.chat.completions.create.return_value = mock_resp

        client = OpenAIClient("gpt-5-nano")
        schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

        result = client.generate([{"role": "user", "content": "Hello"}], tools=[], response_schema=schema)
        self.assertEqual(result, {"answer": "ok"})

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
        # An explicit empty list selects no-tool mode and must not invoke the
        # process-global Gemini tool router (or a real external API).
        response_generator = client.generate_stream(messages, tools=[])

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

    def test_anthropic_configure_thinking_budget(self):
        """thinking_budget updates manual thinking config (enabled models only)."""
        client = AnthropicClient(
            "claude-opus-4-5",
            config={"thinking_type": "enabled", "thinking_budget": 8192},
        )
        self.assertEqual(client._thinking_config.get("budget_tokens"), 8192)

        # Raising the budget updates it and keeps max_tokens above the budget
        client.configure_parameters({"thinking_budget": 20000})
        self.assertEqual(client._thinking_config["budget_tokens"], 20000)
        self.assertGreater(client._max_tokens, 20000)

        # Non-numeric / non-positive values are ignored
        client.configure_parameters({"thinking_budget": "oops"})
        self.assertEqual(client._thinking_config["budget_tokens"], 20000)

        # Adaptive models must ignore budget entirely
        adaptive = AnthropicClient(
            "claude-opus-4-6", config={"thinking_type": "adaptive"}
        )
        adaptive.configure_parameters({"thinking_budget": 12000})
        self.assertEqual(adaptive._thinking_config.get("type"), "adaptive")
        self.assertNotIn("budget_tokens", adaptive._thinking_config)


    def test_openai_client_http_stack_is_httpx2(self):
        """openai 3.x runs on httpx2. SAIVerse hands the SDK only a plain float
        timeout (factory: config["timeout"] -> OpenAIClient(timeout=float)),
        which the SDK wraps itself, so unlike the anthropic client there is no
        httpx-typed object of ours to pin. What this fixes instead, on the real
        construction path (no SDK mock): the client the factory builds sits on
        an httpx2.Client with our timeout applied, so a later "helpful"
        http_client= or httpx.Timeout(...) addition on this path cannot land
        on the old stack unnoticed."""
        client = get_llm_client(
            "gpt-4.1", "openai", 8192, config={"model": "gpt-4.1", "timeout": 123},
        )

        self.assertIsInstance(client, OpenAIClient)
        self.assertIsInstance(client.client._client, httpx2.Client)
        self.assertEqual(client.client.timeout, 123.0)
        self.assertEqual(client.client._client.timeout, httpx2.Timeout(123.0))

    def test_openai_client_default_timeout_is_httpx2_too(self):
        client = OpenAIClient("gpt-4.1")
        self.assertIsInstance(client.client._client, httpx2.Client)
        self.assertIsInstance(client.client._client.timeout, httpx2.Timeout)

    def test_anthropic_client_timeout_is_httpx2(self):
        """anthropic 1.x runs on httpx2. The SDK does NOT reject an old
        httpx.Timeout at construction (verified against 1.3.0): it is accepted
        and each per-phase timeout handed to the transport becomes the whole
        Timeout object instead of a number. So the type is pinned here, on the
        real construction path (no SDK mock), because nothing else would fail
        loudly if someone switched the import back to httpx."""
        client = AnthropicClient("claude-sonnet-4-5")

        self.assertIsInstance(client.client.timeout, httpx2.Timeout)
        self.assertEqual(client.client.timeout.connect, 5.0)
        self.assertEqual(client.client.timeout.read, anthropic_module.DEFAULT_TIMEOUT_SECONDS)

    def test_anthropic_client_timeout_env_override(self):
        with patch.dict(os.environ, {"ANTHROPIC_TIMEOUT_SECONDS": "120"}):
            client = AnthropicClient("claude-sonnet-4-5")
        self.assertIsInstance(client.client.timeout, httpx2.Timeout)
        self.assertEqual(client.client.timeout.read, 120.0)
        self.assertEqual(client.client.timeout.connect, 5.0)

    @patch('llm_clients.anthropic.time.sleep')
    @patch('llm_clients.anthropic.Anthropic')
    def test_anthropic_execute_with_retry_retries_rate_limit(self, mock_anthropic, mock_sleep):
        mock_client_instance = MagicMock()
        mock_anthropic.return_value = mock_client_instance
        client = AnthropicClient("claude-sonnet-4-5")

        request = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx2.Response(429, request=request)
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

        request = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx2.Response(503, request=request)
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

        request = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")
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

        request = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx2.Response(400, request=request)
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

        request = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx2.Response(400, request=request)
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
    def test_ollama_client_generate_empty_content_raises_empty_response_error(self, mock_post, mock_probe):
        """空 content は RuntimeError ではなく EmptyResponseError (error_code=empty_response)。

        呼び出し側の空応答再試行 (generator.generate_text_with_empty_retry) は
        EmptyResponseError か空文字だけを再試行の対象にするので、Ollama だけ
        RuntimeError だと再試行が効かない。/api/chat の後段フォールバックが無い
        構成 (chat_url なし) で、v1 の空応答がそのまま種別を保って上がることを見る。
        """
        from llm_clients.exceptions import EmptyResponseError

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = '{"choices": [{"message": {"content": ""}}]}'
        mock_response.json.return_value = {
            "choices": [{"message": {"content": ""}}]
        }
        mock_post.return_value = mock_response

        client = OllamaClient("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", 1000)
        client.chat_url = None
        with self.assertRaises(EmptyResponseError) as ctx:
            client.generate([{"role": "user", "content": "Hello"}])
        self.assertEqual(ctx.exception.error_code, "empty_response")
        self.assertEqual(str(ctx.exception), "Ollama returned empty response")
        # 空応答は再試行対象 (_should_retry) ではないので v1 は 1 回だけ叩かれる。
        self.assertEqual(mock_post.call_count, 1)

    @patch('llm_clients.ollama.OllamaClient._probe_base', return_value='http://ollama.test')
    @patch('llm_clients.ollama.requests.post')
    def test_ollama_client_tool_mode_empty_content_raises_empty_response_error(self, mock_post, mock_probe):
        """tool モードで tool_calls も本文も無い応答も EmptyResponseError (OpenAI クライアントと同型)。"""
        from llm_clients.exceptions import EmptyResponseError

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = '{"choices": [{"message": {"content": ""}}]}'
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "", "tool_calls": []}}]
        }
        mock_post.return_value = mock_response

        client = OllamaClient("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", 1000)
        tools = [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}]
        with self.assertRaises(EmptyResponseError) as ctx:
            client.generate([{"role": "user", "content": "Hello"}], tools=tools)
        self.assertEqual(ctx.exception.error_code, "empty_response")

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
        # Ollama /api/chat はネイティブ JSON-line 形式 (SSE ではない)。
        # 各行は {"message": {"content": "..."}, "done": false} で、最後に {"done": true}。
        def iter_lines_mock():
            yield b'{"message":{"content":"Stream ","thinking":""},"done":false}'
            yield b'{"message":{"content":"test","thinking":""},"done":false}'
            yield b'{"done":true,"done_reason":"stop"}'
        mock_response.iter_lines.return_value = iter_lines_mock()
        mock_post.return_value = mock_response

        client = OllamaClient("hf.co/unsloth/gemma-3-1b-it-GGUF:BF16", 1000)
        messages = [{"role": "user", "content": "Hello"}]
        response_generator = client.generate_stream(messages)

        self.assertEqual(list(response_generator), ["Stream ", "test"])
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        # 通常のストリーミングは /api/chat (native) を優先利用する
        self.assertEqual(args[0], client.chat_url)
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

        # Schema mode with tools=[] goes through the non-streaming path
        # (response_schema is buffered server-side; streaming offers no benefit)
        mock_resp = MagicMock()
        mock_resp.prompt_feedback = None
        mock_candidate = MagicMock()
        mock_candidate.finish_reason = None
        mock_candidate.content.parts = [MagicMock(text='{"key":"value"}', function_call=None, thought=False)]
        mock_resp.candidates = [mock_candidate]
        mock_resp.usage_metadata = MagicMock(
            prompt_token_count=10,
            candidates_token_count=5,
            cached_content_token_count=0,
        )
        mock_client_instance.models.generate_content.return_value = mock_resp

        client = llm_clients.GeminiClient("gemini-1.5-flash")
        messages = [{"role": "user", "content": "Hello"}]
        schema = {"title": "Decision", "type": "object", "properties": {}, "required": []}

        client.generate(messages, tools=[], response_schema=schema)

        mock_schema_conv.assert_called_once_with(schema)
        mock_config_cls.assert_called()
        config_kwargs = mock_config_cls.call_args.kwargs
        self.assertEqual(config_kwargs.get("response_mime_type"), "application/json")
        self.assertIn("response_schema", config_kwargs)

class TestOpenAICodexUsageAttribution(unittest.TestCase):
    """Codex はサブスクで賄われるので、使用量は設定キーで記録されなければならない。

    Codex 設定の API モデル名 (例 "gpt-5.6-terra") は従量課金の API 版モデル設定の
    キーと衝突する。usage をその名前で記録すると API 版の単価が引き当てられ、
    課金されていない呼び出しに金額が付く。

    client は factory 経由で作る。config_key を手で代入して検証すると、実運用で
    唯一 config_key を立てている factory の代入が消えてもテストが通ってしまう。
    """

    CONFIG_KEY = "codex-gpt-5.6-terra"
    API_MODEL = "gpt-5.6-terra"

    def _codex_client(self):
        return get_llm_client(self.CONFIG_KEY, "openai_codex", 372000)

    def _finalized_usage(self, client):
        client._finalize(
            {
                "usage_input": 1_000_000,
                "usage_output": 1_000_000,
                "usage_cached": 0,
                "reasoning_summary_text": "",
                "reasoning_full_text": "",
                "text": "",
                "function_calls": [],
            },
            None,
        )
        return client.consume_usage()

    def test_factory_sets_config_key(self):
        client = self._codex_client()
        self.assertEqual(client.config_key, self.CONFIG_KEY)
        # API 名は設定キーと別物。同名の従量課金版設定が builtin に存在する。
        self.assertEqual(client.model, self.API_MODEL)

    def test_usage_is_attributed_to_config_key(self):
        usage = self._finalized_usage(self._codex_client())
        self.assertEqual(usage.model, self.CONFIG_KEY)

    def test_subscription_call_costs_nothing(self):
        from saiverse import model_configs

        usage = self._finalized_usage(self._codex_client())
        cost = model_configs.calculate_cost(
            usage.model, usage.input_tokens, usage.output_tokens
        )
        self.assertEqual(cost, 0.0)


class TestLlamaCachedClientUsageAttribution(unittest.TestCase):
    """wrapper 越しでも使用量が設定キーへ帰属すること。

    factory は inner を LlamaCachedClient で包んでから config_key を代入する。
    wrapper がその値を inner へ通さないと、usage を実際に記録する inner 側は
    self.model (API 名) へフォールバックし、API 名と同名の設定があればその単価が
    引き当てられる。
    """

    CONFIG_KEY = "llama-cache-config-key"
    API_MODEL = "llama-cache-api-name"

    def _cached_client(self, slot_save_path):
        config = {
            "model": self.API_MODEL,
            "provider": "openai",
            "base_url": "http://localhost:18099/v1",
            "api_key_required": False,
            "llama_slot_save_path": slot_save_path,
        }
        return get_llm_client(self.CONFIG_KEY, "openai", 4096, config=config)

    def test_config_key_reaches_inner(self):
        import tempfile

        from llm_clients.llama_cache import LlamaCachedClient

        with tempfile.TemporaryDirectory() as tmp:
            client = self._cached_client(tmp)
            self.assertIsInstance(client, LlamaCachedClient)
            self.assertEqual(client._inner.config_key, self.CONFIG_KEY)

    def test_usage_is_attributed_to_config_key(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = self._cached_client(tmp)
            # usage を記録するのは inner。wrapper の consume_usage は委譲するだけ。
            client._inner._store_usage(input_tokens=100, output_tokens=50)
            usage = client.consume_usage()
            self.assertEqual(usage.model, self.CONFIG_KEY)

    def test_wrapping_preserves_existing_config_key(self):
        """設定済みの client を包んでも config_key が消えないこと。

        config_key は inner へ委譲する property なので、基底 __init__ の
        self.config_key = "" も setter 経由で inner へ届く。退避・復元しないと
        wrap した瞬間に既存の帰属が失われ、以後の usage が API 名へ戻る。
        """
        import tempfile

        from llm_clients.llama_cache import LlamaCacheManager, LlamaCachedClient

        class _FakeInner(LLMClient):
            def __init__(self):
                super().__init__()
                self.model = "api-name"

            def generate(self, *args, **kwargs):
                return ""

            def generate_stream(self, *args, **kwargs):
                yield ""

        inner = _FakeInner()
        inner.config_key = "preset-config-key"
        with tempfile.TemporaryDirectory() as tmp:
            cache = LlamaCacheManager("http://localhost:18099/v1", tmp, 1)
            wrapper = LlamaCachedClient(inner, cache)
            self.assertEqual(inner.config_key, "preset-config-key")
            self.assertEqual(wrapper.config_key, "preset-config-key")


class TestStructuredOutputRecordsUsage(unittest.TestCase):
    """構造化出力の経路でも使用量が記録されること。

    generate_stream は response_schema があると stream=False で投げるので、構造化
    出力は _stream_text_mode の非ストリーム分岐を通る。そこが usage を保存しないと、
    judgment / router など構造化出力を使う呼び出しが使用量と費用から丸ごと落ちる。
    """

    def _fake_response(self):
        message = MagicMock()
        message.content = '{"ok": true}'
        message.tool_calls = None
        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.message = message
        usage = MagicMock()
        usage.prompt_tokens = 1234
        usage.completion_tokens = 56
        usage.prompt_tokens_details = None
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = usage
        return resp

    def test_non_stream_structured_branch_stores_usage(self):
        client = OpenAIClient("gpt-4.1-nano")
        client.config_key = "gpt-4.1-nano"
        consumed = list(client._stream_text_mode(
            resp=self._fake_response(),
            history_snippets=[],
            req_kwargs={"stream": False},
            response_schema={"type": "object"},
            reasoning_chunks=[],
        ))
        self.assertTrue(consumed)
        usage = client.consume_usage()
        self.assertIsNotNone(usage, "structured output path recorded no usage")
        self.assertEqual(usage.input_tokens, 1234)
        self.assertEqual(usage.output_tokens, 56)
        # 帰属は API 名でなく設定キーへ
        self.assertEqual(usage.model, "gpt-4.1-nano")

    def test_usage_survives_content_filter(self):
        """content_filter で例外になっても使用量が残ること。

        応答が来た時点で課金は成立している。解釈より前に保存しないと、弾かれた
        呼び出しのトークンが使用量から消える。
        """
        from llm_clients.exceptions import LLMError

        resp = self._fake_response()
        resp.choices[0].finish_reason = "content_filter"

        client = OpenAIClient("gpt-4.1-nano")
        client.config_key = "gpt-4.1-nano"
        with self.assertRaises(LLMError):
            list(client._stream_text_mode(
                resp=resp,
                history_snippets=[],
                req_kwargs={"stream": False},
                response_schema={"type": "object"},
                reasoning_chunks=[],
            ))
        usage = client.consume_usage()
        self.assertIsNotNone(usage, "usage was lost when the response was filtered")
        self.assertEqual(usage.input_tokens, 1234)


class TestScriptsPassConfigKeyToFactory(unittest.TestCase):
    """CLI が factory へ API 名でなく設定キーを渡すこと。

    get_llm_client の第一引数はそのまま client.config_key になる。API 名を渡すと、
    同じ API 名を持つ従量課金版の設定があるモデル (Codex 等) で、使用量がその単価で
    記録される。2026-08-01 時点で scripts/ の 6 箇所がこの形だった。

    変数名での検査なので万能ではない (別名で同じ誤りを書けば素通りする)。実際に
    起きた書き方の再発を止めるための歯止め。
    """

    def test_no_script_passes_api_model_name_to_factory(self):
        import re
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        # git 管理下のファイルだけを見る。rglob だと gitignore された仮想環境まで
        # 舐めてしまい (2026-08-01 実測: 管理下 59 に対し 2144 ファイル)、結果も
        # 実行時間もローカル環境に依存する。
        # git そのものが無い / checkout でない環境だけ skip。git があるのにエラーで
        # 終わった場合は fail — 検査不能を成功扱いにすると、一つも読まずに green に
        # なり「違反が無い」ことの証拠にならない。
        def _git(*args):
            try:
                return subprocess.run(
                    ["git", *args], cwd=repo_root,
                    capture_output=True, text=True, timeout=30,
                )
            except FileNotFoundError:
                self.skipTest("git executable not available")
            except subprocess.SubprocessError as exc:
                self.fail(f"git {' '.join(args)} did not complete: {exc}")

        inside = _git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            self.skipTest("not inside a git work tree; scan requires git ls-files")

        listed = _git("ls-files", "scripts/*.py")
        self.assertEqual(
            listed.returncode, 0,
            f"git ls-files failed, scan could not run: {listed.stderr.strip()}",
        )
        rels = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        self.assertTrue(
            rels, "git ls-files returned no scripts/*.py — the scan would vacuously pass"
        )

        pattern = re.compile(r"get_llm_client\(\s*actual_model_id\b")
        offenders = []
        missing = []
        for rel in rels:
            path = repo_root / rel
            if not path.exists():
                # sparse checkout などで index にはあるが実体が無い。黙って飛ばすと
                # 一件も読まないまま green になるので、読めなかった事実を持ち帰る。
                missing.append(rel)
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(rel)
        self.assertEqual(
            missing, [],
            "listed by git but missing on disk (sparse checkout?); scan was incomplete: "
            + ", ".join(missing),
        )
        self.assertEqual(
            offenders, [],
            "factory の第一引数は設定キー。API 名 (actual_model_id) を渡すと使用量が "
            "従量課金版の単価で記録される: " + ", ".join(offenders),
        )


class TestFactoryFlagsApiModelName(unittest.TestCase):
    """factory が「設定キーでなく API 名を渡された」ことを検出すること。

    scripts/ の走査検査は既に起きた書き方しか止められない (別名変数や他ディレクトリは
    素通りする)。呼び出し側の変数名にもディレクトリにも依存せずに検出できるのは
    factory の境界だけなので、そこにも置く。
    """

    BASE_CONFIG = {
        "model": "vendor/guard-api-name",
        "provider": "openai",
        "base_url": "http://localhost:18099/v1",
        "api_key_required": False,
    }
    # 設定キー "guard-config-key" が API 名 "vendor/guard-api-name" を持つ registry。
    # API 名を factory へ渡すと、この設定の単価が使用量に付く状況を再現する。
    FAKE_CONFIGS = {"guard-config-key": dict(BASE_CONFIG)}

    def _patched_configs(self, configs):
        from saiverse import model_configs as model_configs_module

        return patch.object(model_configs_module, "MODEL_CONFIGS", configs)

    def test_warns_when_api_name_collides_with_another_config(self):
        with self._patched_configs(dict(self.FAKE_CONFIGS)):
            with self.assertLogs(level="WARNING") as captured:
                get_llm_client(
                    "vendor/guard-api-name", "openai", 4096, config=dict(self.BASE_CONFIG)
                )
        self.assertTrue(
            any("does not look like the right config key" in line for line in captured.output),
            captured.output,
        )

    def test_warns_when_passed_config_disagrees_with_registered_key(self):
        """実在する設定キーでも、渡した config がその設定と食い違えば警告すること。

        2026-08-01 に実害が出た scripts/ の呼び出しがこの形だった。gpt-5.6-terra は
        従量課金版のキーとして実在するので「未知のキー」では捕まらないが、渡していた
        config は Codex 設定 (provider_ref が別) だった。
        """
        registry = {
            "shared-api-name": {"model": "shared-api-name", "provider_ref": "openai"},
            "codex-shared": {"model": "shared-api-name", "provider_ref": "openai_codex"},
        }
        # provider_ref を書くと base_url は provider 定義と一致していなければならない
        # (provider_security.validate_model_config_connection)。ここでは書かない。
        codex_config = {
            "model": "shared-api-name",
            "provider_ref": "openai_codex",
        }
        with self._patched_configs(registry):
            with self.assertLogs(level="WARNING") as captured:
                get_llm_client("shared-api-name", "openai", 4096, config=codex_config)
        self.assertTrue(
            any("provider_ref" in line for line in captured.output),
            captured.output,
        )

    def test_does_not_warn_for_config_key(self):
        import logging as _logging

        with self._patched_configs(dict(self.FAKE_CONFIGS)):
            with self.assertLogs(level="WARNING") as captured:
                # assertLogs は 1 件も出ないと失敗するので番兵を入れる
                _logging.getLogger("test.sentinel").warning("sentinel")
                get_llm_client(
                    "guard-config-key", "openai", 4096, config=dict(self.BASE_CONFIG)
                )
        self.assertFalse(
            any("does not look like the right config key" in line for line in captured.output),
            captured.output,
        )

    def test_does_not_warn_for_unknown_key_without_collision(self):
        """設定キーとして未知でも、その名前を API 名に持つ設定が無ければ騒がない。

        動的に組んだ設定やテスト用の架空キーで誤検出しないための境界。
        """
        import logging as _logging

        with self._patched_configs(dict(self.FAKE_CONFIGS)):
            with self.assertLogs(level="WARNING") as captured:
                _logging.getLogger("test.sentinel").warning("sentinel")
                get_llm_client(
                    "totally-unknown-key", "openai", 4096, config=dict(self.BASE_CONFIG)
                )
        self.assertFalse(
            any("does not look like the right config key" in line for line in captured.output),
            captured.output,
        )

    def test_detection_follows_reloaded_registry(self):
        """reload_configs() 後の registry を見ること。

        model_configs.reload_configs() は MODEL_CONFIGS を新しい辞書へ再束縛する。
        factory が import 時の辞書を掴んでいると、再読込で追加された設定を見落として
        検出が古いままになる。
        """
        with self._patched_configs({}):
            # 空 registry では衝突相手が居ないので警告は出ない
            with self.assertLogs(level="WARNING") as before:
                import logging as _logging

                _logging.getLogger("test.sentinel").warning("sentinel")
                get_llm_client(
                    "vendor/guard-api-name", "openai", 4096, config=dict(self.BASE_CONFIG)
                )
            self.assertFalse(
                any("does not look like the right config key" in line for line in before.output),
                before.output,
            )

        # 別の辞書へ差し替えた後は、その内容で判定されること
        with self._patched_configs(dict(self.FAKE_CONFIGS)):
            with self.assertLogs(level="WARNING") as after:
                get_llm_client(
                    "vendor/guard-api-name", "openai", 4096, config=dict(self.BASE_CONFIG)
                )
            self.assertTrue(
                any("does not look like the right config key" in line for line in after.output), after.output
            )


if __name__ == '__main__':
    unittest.main()
