from __future__ import annotations

from unittest.mock import patch

from llm_clients.anthropic_request_builder import (
    _prepare_anthropic_messages,
    build_request_params,
)


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
