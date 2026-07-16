from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests


def test_anthropic_stream_does_not_retry_after_first_yield() -> None:
    from llm_clients.anthropic import AnthropicClient

    client = AnthropicClient.__new__(AnthropicClient)
    client.model = "test-model"
    client._max_tokens = 100
    client._extra_params = {}
    client._thinking_config = None
    client._thinking_effort = None
    client.supports_images = False
    client.max_image_bytes = 1024
    client.max_image_embeds = None
    client._store_reasoning = lambda value: None
    client._inject_unsupported_media_summaries = lambda messages: messages
    calls = 0

    def interrupted_stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        yield "committed-prefix"
        raise RuntimeError("connection lost")

    client._iter_stream = interrupted_stream
    with patch(
        "llm_clients.anthropic.build_request_params",
        return_value={
            "request_params": {"messages": [{"role": "user", "content": "x"}]},
            "use_tools": False,
        },
    ):
        stream = client.generate_stream([{"role": "user", "content": "x"}])
        assert next(stream) == "committed-prefix"
        with pytest.raises(Exception, match="connection lost"):
            next(stream)

    assert calls == 1


def test_ollama_stream_does_not_retry_or_fallback_after_first_yield() -> None:
    from llm_clients.ollama import OllamaClient

    client = OllamaClient.__new__(OllamaClient)
    client.model = "test-model"
    client.context_length = 4096
    client._request_kwargs = {}
    client.chat_url = "http://ollama/api/chat"
    client.url = "http://ollama/v1/chat/completions"
    client._inject_unsupported_media_summaries = lambda messages: messages

    class InterruptedResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield json.dumps(
                {"message": {"content": "committed-prefix"}, "done": False}
            ).encode()
            raise requests.ConnectionError("connection lost")

    post = MagicMock(return_value=InterruptedResponse())
    with patch("llm_clients.ollama.requests.post", post):
        stream = client.generate_stream([{"role": "user", "content": "x"}])
        assert next(stream) == "committed-prefix"
        with pytest.raises(RuntimeError, match="connection lost"):
            next(stream)

    assert post.call_count == 1


def test_mcp_tool_exception_is_not_replayed_after_uncertain_dispatch() -> None:
    from tools.mcp_client import MCPServerConnection

    connection = MCPServerConnection("server", {})
    connection._connected = True
    connection.session = SimpleSession = MagicMock()
    SimpleSession.call_tool = AsyncMock(side_effect=RuntimeError("response lost"))
    connection.disconnect = AsyncMock()
    connection.connect = AsyncMock()

    async def run() -> None:
        with pytest.raises(RuntimeError, match="response lost"):
            await connection.call_tool("mutating_tool", {"value": 1})

    asyncio.run(run())
    SimpleSession.call_tool.assert_awaited_once()
    connection.disconnect.assert_awaited_once()
    connection.connect.assert_not_awaited()
