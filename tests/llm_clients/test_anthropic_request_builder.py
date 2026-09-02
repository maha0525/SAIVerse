from __future__ import annotations

import json
from unittest.mock import patch

import anthropic
import httpx2

from llm_clients.anthropic_request_builder import (
    _prepare_anthropic_messages,
    build_request_params,
)

# Sampling parameters that anthropic 1.x removed from messages.create() /
# messages.stream() (passing them as kwargs raises TypeError). They must travel
# in extra_body, which the SDK merges into the request JSON.
_REMOVED_SAMPLING_KWARGS = ("temperature", "top_p", "top_k")


def _build(**overrides):
    kwargs = dict(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        response_schema=None,
        temperature=None,
        enable_cache=False,
        cache_ttl="5m",
        model="claude-sonnet-4-6",
        max_tokens=256,
        extra_params={},
        thinking_config=None,
        thinking_effort=None,
        supports_images=False,
        max_image_bytes=None,
    )
    kwargs.update(overrides)
    return build_request_params(**kwargs)["request_params"]


@patch("llm_clients.anthropic_request_builder.image_summary_note", return_value="[image summary]")
@patch("llm_clients.anthropic_request_builder.load_image_bytes_for_llm", return_value=(b"img-bytes", "image/png"))
@patch(
    "llm_clients.anthropic_request_builder.iter_image_media",
    return_value=[{"path": "dummy.png", "mime_type": "image/png", "uri": "saiverse://image/dummy.png"}],
)
def test_prepare_anthropic_messages_supports_images_toggle(
    _mock_iter_image_media,
    _mock_load_image,
    _mock_summary,
):
    messages = [{"role": "user", "content": "hello", "metadata": {"media": [{"uri": "dummy"}]}}]

    prepared_with_images = _prepare_anthropic_messages(messages, supports_images=True)
    assert prepared_with_images[0]["content"][0]["type"] == "text"
    assert prepared_with_images[0]["content"][1]["type"] == "image"

    prepared_without_images = _prepare_anthropic_messages(messages, supports_images=False)
    assert prepared_without_images[0]["content"][1]["text"] == "[image summary]"


def test_prepare_anthropic_messages_realtime_cache_breakpoint() -> None:
    messages = [
        {"role": "user", "content": "static"},
        {"role": "user", "content": "dynamic", "metadata": {"__realtime_context__": True}},
        {"role": "user", "content": "latest"},
    ]

    prepared = _prepare_anthropic_messages(messages, enable_cache=True)

    assert "cache_control" in prepared[0]["content"][-1]
    assert "cache_control" not in prepared[1]["content"][-1]


def test_build_request_params_native_schema_uses_output_config() -> None:
    schema = {"title": "Decision", "type": "object", "properties": {"answer": {"type": "string"}}}

    build_result = build_request_params(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        response_schema=schema,
        temperature=0.1,
        enable_cache=True,
        cache_ttl="5m",
        model="claude-opus-4-6",
        max_tokens=4096,
        extra_params={},
        thinking_config={"type": "adaptive"},
        thinking_effort="high",
        supports_images=True,
        max_image_bytes=5 * 1024 * 1024,
    )

    request_params = build_result["request_params"]
    assert request_params["output_config"]["effort"] == "high"
    assert request_params["output_config"]["format"]["type"] == "json_schema"
    assert build_result["use_native_structured_output"] is True


def test_sampling_params_go_to_extra_body_never_top_level() -> None:
    # explicit temperature override wins over the configured one
    params = _build(temperature=0.3, extra_params={"temperature": 0.9, "top_p": 0.8, "top_k": 7})

    assert params["extra_body"] == {"temperature": 0.3, "top_p": 0.8, "top_k": 7}
    for key in _REMOVED_SAMPLING_KWARGS:
        assert key not in params, f"{key} must not be a messages.create() kwarg on anthropic 1.x"
    # max_tokens is still a real SDK parameter and stays top-level
    assert params["max_tokens"] == 256


def test_configured_temperature_used_when_no_override() -> None:
    params = _build(extra_params={"temperature": 0.5})
    assert params["extra_body"] == {"temperature": 0.5}


def test_no_extra_body_without_sampling_params() -> None:
    params = _build()
    assert "extra_body" not in params
    for key in _REMOVED_SAMPLING_KWARGS:
        assert key not in params


def test_request_params_are_accepted_by_sdk_and_sampling_reaches_the_wire() -> None:
    """Cross the SDK boundary offline: the dict build_request_params emits must
    bind to anthropic 1.x messages.create() without TypeError, and extra_body
    must land as top-level keys in the JSON the transport sends."""
    captured: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "m",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    client = anthropic.Anthropic(
        api_key="dummy", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    params = _build(temperature=0.2, extra_params={"top_k": 5})

    message = client.messages.create(**params)

    assert message.content[0].text == "ok"
    body = captured["body"]
    assert body["temperature"] == 0.2
    assert body["top_k"] == 5
    assert body["max_tokens"] == 256
    assert "extra_body" not in body
