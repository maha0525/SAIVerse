"""Model definition -> client -> request contract, entirely without paid inference."""
from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from anthropic import Anthropic

from llm_clients.anthropic import AnthropicClient
from llm_clients.exceptions import InvalidRequestError
from llm_clients.factory import get_llm_client
from saiverse import model_configs


@pytest.fixture
def haiku(monkeypatch):
    monkeypatch.setenv("CLAUDE_API_KEY", "offline-test-key")
    for name in ("TYPE", "BUDGET", "EFFORT", "DISPLAY"):
        monkeypatch.delenv(f"ANTHROPIC_THINKING_{name}", raising=False)
    monkeypatch.delenv("ANTHROPIC_MAX_OUTPUT_TOKENS", raising=False)
    config = model_configs.get_model_config("claude-haiku-5.5")
    assert config["supports_sampling_parameters"] is False
    assert config["supports_assistant_prefill"] is False
    with patch("llm_clients.anthropic.Anthropic"):
        client = get_llm_client("claude-haiku-5.5", config["provider"], config["context_length"], config)
    assert isinstance(client, AnthropicClient)
    return client


def _capture(client, streaming, messages, **kwargs):
    if streaming:
        with patch.object(client, "_iter_stream", return_value=iter(["ok"])) as stream:
            assert list(client.generate_stream(messages, **kwargs)) == ["ok"]
            return stream.call_args.args[0]
    response = MagicMock(usage=None, content=[MagicMock(type="text", text="ok")])
    client.client.messages.create.return_value = response
    assert client.generate(messages, **kwargs) == "ok"
    return client.client.messages.create.call_args.kwargs


@pytest.mark.parametrize("streaming", [False, True])
def test_sampling_overrides_are_removed_and_adaptive_defaults_survive(haiku, streaming):
    haiku.configure_parameters({"temperature": 0.2, "top_p": 0.8, "top_k": 10, "thinking_budget": 999999})
    params = _capture(haiku, streaming, [{"role": "user", "content": "hello"}], temperature=0.3)
    assert params["model"] == "claude-haiku-5-5"
    assert params["max_tokens"] == 128000
    assert params["thinking"]["type"] == "adaptive"
    assert "budget_tokens" not in params["thinking"]
    assert params["output_config"]["effort"] == "medium"
    assert "extra_body" not in params
    assert not {"temperature", "top_p", "top_k"}.intersection(params)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("trailing_empty", [False, True])
def test_prefill_is_rejected_before_transport_without_mutating_history(haiku, streaming, trailing_empty):
    messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "partial"}]
    if trailing_empty:
        messages.append({"role": "user", "content": ""})
    original = copy.deepcopy(messages)
    with pytest.raises(InvalidRequestError) as caught:
        if streaming:
            list(haiku.generate_stream(messages))
        else:
            haiku.generate(messages)
    assert caught.value.error_code == "invalid_request"
    assert messages == original
    haiku.client.messages.create.assert_not_called()
    haiku.client.messages.stream.assert_not_called()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_ui_effort_reaches_request(haiku, effort):
    haiku.configure_parameters({"thinking_effort": effort})
    params = _capture(haiku, False, [{"role": "user", "content": "hello"}])
    assert params["output_config"]["effort"] == effort


def test_configured_xhigh_and_legacy_environment_budget(haiku, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_THINKING_TYPE", "enabled")
    monkeypatch.setenv("ANTHROPIC_THINKING_BUDGET", "999999")
    config = dict(model_configs.get_model_config("claude-haiku-5.5"), thinking_effort="xhigh")
    with patch("llm_clients.anthropic.Anthropic"):
        client = AnthropicClient(config["model"], config)
    params = _capture(client, False, [{"role": "user", "content": "hello"}])
    assert params["thinking"]["type"] == "adaptive"
    assert "budget_tokens" not in params["thinking"]
    assert params["output_config"]["effort"] == "xhigh"
    assert params["max_tokens"] == 128000


def test_legacy_models_preserve_sampling_prefill_and_manual_budget(haiku):
    with patch("llm_clients.anthropic.Anthropic"):
        client = AnthropicClient("legacy", {"thinking_type": "enabled", "thinking_budget": 8192})
    params = _capture(client, False, [{"role": "assistant", "content": "partial"}], temperature=0.2)
    assert params["extra_body"] == {"temperature": 0.2}
    assert params["messages"][-1]["role"] == "assistant"
    assert params["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert params["max_tokens"] == 12288


def test_haiku_request_crosses_sdk_to_offline_wire(haiku):
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as http_client:
        haiku.client = Anthropic(api_key="offline-test-key", max_retries=0, http_client=http_client,
                                 timeout=httpx2.Timeout(30))
        assert haiku.generate([{"role": "user", "content": "hello"}], temperature=0.1) == "ok"
    assert captured["model"] == "claude-haiku-5-5"
    assert captured["max_tokens"] == 128000
    assert captured["thinking"]["type"] == "adaptive"
    assert not {"temperature", "top_p", "top_k", "extra_body"}.intersection(captured)
