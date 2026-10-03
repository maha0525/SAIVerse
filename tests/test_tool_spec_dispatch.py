"""ToolSchema → SEA selection → provider serializer, without inference or personas."""
import socket
from unittest.mock import Mock

import pytest
from google.genai import types
from openai.types.chat import ChatCompletion, ChatCompletionChunk

import tools as tool_registry
from llm_clients.anthropic import AnthropicClient
from llm_clients.anthropic_request_builder import _prepare_anthropic_tools
from llm_clients.base import LLMClient
from llm_clients.gemini import GeminiClient
from llm_clients.llama_cache import LlamaCachedClient
from llm_clients.nvidia_nim import NvidiaNIMClient
from llm_clients.ollama import OllamaClient
from llm_clients.openai import OpenAIClient
from llm_clients.openai_codex import OpenAICodexClient
from llm_clients.xai import XAIClient, _convert_tools
from sea.runtime import SEARuntime
from tools.adapters.gemini import to_gemini
from tools.adapters.openai import to_openai
from tools.core import ToolSchema


@pytest.fixture(autouse=True)
def synthetic_tool_catalog(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Tool format tests must not contact a backend")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    schemas = [
        ToolSchema(
            name=name,
            description=f"Synthetic {name}",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            result_type="string",
        )
        for name in ("first", "second", "excluded")
    ]
    monkeypatch.setattr(tool_registry, "OPENAI_TOOLS_SPEC", [to_openai(s) for s in schemas])
    monkeypatch.setattr(tool_registry, "GEMINI_TOOLS_SPEC", [to_gemini(s) for s in schemas])


def build_spec(client, names=("first", "second")):
    # No runtime/persona initialization; this method only filters the catalog.
    return SEARuntime._build_tools_spec(None, list(names), client)


def bare_client(client_class):
    # Capability declarations need no SDK, credentials, local-server probes or DB.
    client = client_class.__new__(client_class)
    LLMClient.__init__(client)
    return client


@pytest.mark.parametrize("client_class", [
    OpenAIClient, AnthropicClient, OllamaClient, NvidiaNIMClient, OpenAICodexClient, XAIClient,
])
def test_all_dict_consumers_receive_openai_specs(client_class):
    client = bare_client(client_class)
    assert client.tool_spec_format() == "openai"
    assert build_spec(client) == tool_registry.OPENAI_TOOLS_SPEC[:2]


@pytest.mark.parametrize("depth", [0, 1, 2])
@pytest.mark.parametrize("client_class,expected_format", [
    (OpenAIClient, "openai"), (OpenAICodexClient, "openai"), (GeminiClient, "gemini"),
])
def test_cache_wrappers_delegate_format_without_changing_it(client_class, expected_format, depth):
    inner = bare_client(client_class)
    client = inner
    cache = Mock()
    for _ in range(depth):
        client = LlamaCachedClient(client, cache)
    assert client.tool_spec_format() == expected_format
    assert build_spec(client) == build_spec(inner)
    assert not cache.mock_calls


def test_new_subclass_inherits_format_without_a_runtime_allowlist():
    class CustomClient(OpenAIClient):
        pass

    assert build_spec(bare_client(CustomClient)) == tool_registry.OPENAI_TOOLS_SPEC[:2]


def test_capability_wins_even_with_a_misleading_class_name():
    class OpenAIClient(LLMClient):
        def tool_spec_format(self):
            return "gemini"

    assert isinstance(build_spec(OpenAIClient())[0], types.Tool)


def test_gemini_combines_only_selected_declarations():
    spec = build_spec(bare_client(GeminiClient))
    assert len(spec) == 1
    assert isinstance(spec[0], types.Tool)
    assert [decl.name for decl in spec[0].function_declarations] == ["first", "second"]
    request = types.GenerateContentConfig(tools=spec)
    assert len(request.model_dump(mode="json")["tools"][0]["function_declarations"]) == 2


@pytest.mark.parametrize("client_class", [OpenAICodexClient, GeminiClient])
@pytest.mark.parametrize("names", [(), ("missing",)])
def test_empty_selection_does_not_add_tools(client_class, names):
    assert build_spec(bare_client(client_class), names) == []


def test_codex_body_keeps_tools_selected_by_runtime():
    client = OpenAICodexClient("synthetic-codex")
    body = client._build_body(
        [{"role": "user", "content": "Synthetic serializer input"}],
        temperature=None,
        tools=build_spec(client),
        response_schema=None,
        reasoning_effort=None,
    )
    assert [tool["name"] for tool in body["tools"]] == ["first", "second"]
    assert body["tools"][0]["parameters"]["properties"]["value"]["type"] == "string"
    assert body["tool_choice"] == "auto"
    assert client._session is None


@pytest.mark.parametrize("stream", [False, True])
def test_cache_wrapper_delivers_selected_tools_to_openai_sdk(monkeypatch, stream):
    sdk = Mock()
    monkeypatch.setattr("llm_clients.openai.OpenAI", Mock(return_value=sdk))
    inner = OpenAIClient("synthetic-model", api_key="synthetic-key")
    cache = Mock()
    cache.acquire_slot.return_value = 0
    wrapper = LlamaCachedClient(inner, cache)
    messages = [{"role": "user", "content": "Synthetic serializer input"}]
    spec = build_spec(wrapper)
    create = sdk.chat.completions.create
    if stream:
        create.return_value = [ChatCompletionChunk(
            id="synthetic-chunk", created=0, model="synthetic-model", object="chat.completion.chunk",
            choices=[{"index": 0, "delta": {"content": "Synthetic response"}, "finish_reason": "stop"}],
        )]
        assert list(wrapper.generate_stream(messages, tools=spec)) == ["Synthetic response"]
    else:
        create.return_value = ChatCompletion(
            id="synthetic-response", created=0, model="synthetic-model", object="chat.completion",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Synthetic response"},
                      "finish_reason": "stop"}],
        )
        assert wrapper.generate(messages, tools=spec) == {"type": "text", "content": "Synthetic response"}
    assert create.call_count == 1
    assert create.call_args.kwargs["tools"] == tool_registry.OPENAI_TOOLS_SPEC[:2]
    cache.restore.assert_called_once()
    cache.save.assert_called_once()
    cache.release_slot.assert_called_once_with(0)


def test_anthropic_serializer_accepts_selected_format():
    spec = build_spec(bare_client(AnthropicClient))
    converted = _prepare_anthropic_tools(spec, enable_cache=False)
    assert [tool["name"] for tool in converted] == ["first", "second"]
    assert converted[0]["input_schema"] == spec[0]["function"]["parameters"]


def test_xai_serializer_accepts_selected_format():
    converted = _convert_tools(build_spec(bare_client(XAIClient)))
    assert [tool.function.name for tool in converted] == ["first", "second"]


def test_missing_capability_fails_instead_of_guessing_gemini():
    with pytest.raises(NotImplementedError, match="tool_spec_format"):
        build_spec(LLMClient())


def test_unknown_format_fails_before_sending_tools():
    class UnknownClient(LLMClient):
        def tool_spec_format(self):
            return "unknown"

    with pytest.raises(ValueError, match="Unsupported tool spec format"):
        build_spec(UnknownClient())
