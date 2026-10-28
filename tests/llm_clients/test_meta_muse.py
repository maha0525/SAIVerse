"""Exercise shipped Meta Muse config through loaders, factory, SDK and mocked HTTP."""

import json

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAI

from api.routes import providers as provider_api
from llm_clients import factory
from llm_clients.exceptions import AuthenticationError
from saiverse import data_paths, model_configs, provider_configs

MODELS = {
    # config key -> (API model name, input, cached input, output) in USD per 1M tokens
    "meta-muse-wonder-1.0": ("muse-wonder-1.0", 4.0, 0.2, 20.0),
    "meta-muse-spark-1.3": ("muse-spark-1.3", 1.25, 0.15, 4.25),
    # Contributor mode: Meta may train on the content in exchange for the lower price.
    "meta-muse-spark-1.3-contributor": ("muse-spark-1.3-contributor", 0.10, 0.002, 0.20),
}
KEY = "synthetic-meta-key"
TOOL = {
    "type": "function",
    "function": {
        "name": "synthetic_lookup",
        "description": "Synthetic fixture only",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    },
}
MESSAGES = [
    {"role": "system", "content": "synthetic leading system"},
    {"role": "user", "content": "synthetic input"},
    {"role": "assistant", "content": "synthetic reply"},
    {"role": "system", "content": "synthetic mid-conversation system"},
    {"role": "user", "content": "synthetic follow-up"},
]
SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "detail": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "note": {"type": "string"}},
            "required": ["score"],
        },
    },
    "required": ["answer"],
}
STRUCTURED = {"answer": "ok", "detail": {"score": 1, "note": "n"}}
USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 30,
    "total_tokens": 1030,
    "prompt_tokens_details": {"cached_tokens": 600},
    "completion_tokens_details": {"reasoning_tokens": 20},
}
# Request fields Meta's API documents as rejected with HTTP 400.
REJECTED_FIELDS = (
    "top_p", "stop", "logit_bias", "frequency_penalty", "presence_penalty",
    "logprobs", "max_output_tokens", "verbosity", "modalities",
)


@pytest.fixture
def isolated(tmp_path, monkeypatch, mock_provider_network):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-dummy-key")
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(provider_configs, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "expansion_data")
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", provider_configs.load_configs())
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", model_configs.load_configs())
    mock_provider_network("api.meta.ai")


def completion(api_model, *, tool=False, structured=False):
    msg = {
        "role": "assistant",
        "content": json.dumps(STRUCTURED) if structured else "ok",
        "reasoning_content": "",
    }
    if tool:
        msg.update(
            content=None,
            tool_calls=[
                {
                    "id": "call-fixture",
                    "type": "function",
                    "function": {"name": "synthetic_lookup", "arguments": '{"q":"test"}'},
                }
            ],
        )
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 1,
        "model": api_model,
        "usage": USAGE,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool else "stop"}],
    }


def sse(api_model, *, tool=False):
    if tool:
        deltas = [
            {"role": "assistant", "reasoning_content": ""},
            {"tool_calls": [{"index": 0, "id": "call-fixture", "type": "function", "function": {"name": "synthetic_lookup", "arguments": '{"q":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"test"}'}}]},
        ]
    else:
        deltas = [
            {"role": "assistant", "content": "", "reasoning_content": ""},
            {"content": "o", "reasoning_content": ""},
            {"content": "k"},
        ]
    chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks += [
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
        {"choices": [], "usage": USAGE},
    ]
    envelope = {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1, "model": api_model}
    return "".join("data: " + json.dumps(envelope | chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


@pytest.fixture
def make_client(isolated, monkeypatch):
    opened = []
    requests = []

    def make(config_key="meta-muse-wonder-1.0", *, tool=False, structured=False):
        api_model = MODELS[config_key][0]
        monkeypatch.setenv("META_API_KEY", KEY)

        def handle(request):
            body = json.loads(request.content)
            requests.append((request, body))
            if body.get("stream"):
                return httpx.Response(200, text=sse(api_model, tool=tool), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=completion(api_model, tool=tool, structured=structured))

        def sdk(**kwargs):
            client = OpenAI(**kwargs, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
            opened.append(client)
            return client

        monkeypatch.setattr("llm_clients.openai.OpenAI", sdk)
        monkeypatch.setattr("llm_clients.openai.MAX_RETRIES", 1)
        cfg = model_configs.get_model_config(config_key)
        return factory.get_llm_client(config_key, cfg["provider"], cfg["context_length"], cfg), requests

    yield make
    for sdk in opened:
        sdk.close()


def assert_meta_request(request, body, api_model):
    """The wire contract every Meta request must hold, whatever the call path."""
    assert request.method == "POST"
    assert request.url == "https://api.meta.ai/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == api_model
    assert body["reasoning_effort"] == "medium"
    assert body["temperature"] == 1
    assert body["max_completion_tokens"] == 32768
    assert "max_tokens" not in body
    for field in REJECTED_FIELDS:
        assert field not in body, f"{field} must not be sent to Meta"
    assert body.get("n", 1) == 1
    # The leading system message stays a system message; a later one becomes user.
    roles = [msg["role"] for msg in body["messages"]]
    assert roles == ["system", "user", "assistant", "user", "user"]
    assert body["messages"][0]["content"] == "synthetic leading system"
    assert body["messages"][3]["content"] == "<system>\nsynthetic mid-conversation system\n</system>"


def assert_usage(usage, config_key):
    assert usage.model == config_key
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (1000, 30, 600)


@pytest.mark.parametrize("config_key", sorted(MODELS))
def test_catalog_resolves_meta_provider_and_pricing(isolated, config_key):
    api_model, input_rate, cached_rate, output_rate = MODELS[config_key]
    cfg = model_configs.get_model_config(config_key)
    assert cfg["model"] == api_model
    assert cfg["provider_ref"] == "meta"
    assert cfg["protocol"] == "openai_compat"
    assert cfg["base_url"] == "https://api.meta.ai/v1"
    assert cfg["api_key_env"] == "META_API_KEY"
    assert cfg["context_length"] == 1048576
    assert cfg["supports_images"] is True
    assert cfg["convert_system_to_user"] is True
    assert cfg["supports_structured_output"] is True
    # Meta rejects a request with more than 50 images (HTTP 400).
    assert cfg["max_image_embeds"] == 50
    assert model_configs.get_model_parameter_defaults(config_key) == {
        "reasoning_effort": "medium",
        "temperature": 1,
        "max_completion_tokens": 32768,
    }
    assert "none" not in cfg["parameters"]["reasoning_effort"]["options"]
    # Meta documents that "max" is not available on the Contributor variant.
    assert ("max" in cfg["parameters"]["reasoning_effort"]["options"]) == (not api_model.endswith("-contributor"))
    pricing = model_configs.get_model_pricing(config_key)
    assert pricing["input_per_1m_tokens"] == input_rate
    assert pricing["cached_input_per_1m_tokens"] == cached_rate
    assert pricing["output_per_1m_tokens"] == output_rate
    assert pricing["currency"] == "USD"


@pytest.mark.parametrize("config_key", sorted(MODELS))
def test_availability_follows_meta_key_only(isolated, monkeypatch, config_key):
    assert model_configs.required_api_key_env_names(config_key) == ["META_API_KEY"]
    assert not model_configs.is_model_available(config_key)
    monkeypatch.setenv("META_API_KEY", KEY)
    assert model_configs.is_model_available(config_key)


def test_missing_meta_key_does_not_use_openai_key(isolated):
    cfg = model_configs.get_model_config("meta-muse-wonder-1.0")
    with pytest.raises(AuthenticationError, match="META_API_KEY"):
        factory.get_llm_client("meta-muse-wonder-1.0", cfg["provider"], cfg["context_length"], cfg)


@pytest.mark.parametrize("config_key", sorted(MODELS))
def test_generate_sends_meta_contract_and_records_cached_usage(make_client, config_key):
    client, requests = make_client(config_key)
    assert client.generate(MESSAGES, tools=[]) == "ok"
    assert len(requests) == 1
    request, body = requests[0]
    assert_meta_request(request, body, MODELS[config_key][0])
    assert "stream" not in body
    assert "response_format" not in body
    assert_usage(client.consume_usage(), config_key)


@pytest.mark.parametrize("config_key", sorted(MODELS))
def test_cached_usage_is_priced_at_cached_rate(make_client, config_key):
    _, input_rate, cached_rate, output_rate = MODELS[config_key]
    client, _ = make_client(config_key)
    client.generate(MESSAGES, tools=[])
    usage = client.consume_usage()
    cost = model_configs.calculate_cost(
        usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, log_details=False,
    )
    expected = (400 * input_rate + 600 * cached_rate + 30 * output_rate) / 1_000_000
    assert cost == pytest.approx(expected)


def test_structured_output(make_client):
    client, requests = make_client(structured=True)
    assert client.generate(MESSAGES, tools=[], response_schema=SCHEMA) == STRUCTURED
    request, body = requests[0]
    assert_meta_request(request, body, "muse-wonder-1.0")
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    for node in (schema, schema["properties"]["detail"]):
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])
    assert_usage(client.consume_usage(), "meta-muse-wonder-1.0")


def test_text_stream_requests_usage_and_records_final_chunk(make_client):
    client, requests = make_client()
    assert "".join(client.generate_stream(MESSAGES, tools=[])) == "ok"
    request, body = requests[0]
    assert_meta_request(request, body, "muse-wonder-1.0")
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert_usage(client.consume_usage(), "meta-muse-wonder-1.0")


def test_tool_detection(make_client):
    client, requests = make_client(tool=True)
    result = client.generate(MESSAGES, tools=[TOOL])
    assert result["tool_name"] == "synthetic_lookup"
    assert result["tool_args"] == {"q": "test"}
    request, body = requests[0]
    assert_meta_request(request, body, "muse-wonder-1.0")
    assert body["tools"] == [TOOL]
    assert body["tool_choice"] == "auto"
    assert_usage(client.consume_usage(), "meta-muse-wonder-1.0")


def test_tool_stream_reassembles_arguments_and_usage(make_client):
    client, requests = make_client(tool=True)
    list(client.generate_stream(MESSAGES, tools=[TOOL]))
    result = client.consume_tool_detection()
    assert result["tool_name"] == "synthetic_lookup"
    assert result["tool_args"] == {"q": "test"}
    request, body = requests[0]
    assert_meta_request(request, body, "muse-wonder-1.0")
    assert body["stream_options"] == {"include_usage": True}
    assert_usage(client.consume_usage(), "meta-muse-wonder-1.0")


def test_provider_api_reports_meta_key(isolated, monkeypatch):
    app = FastAPI()
    app.include_router(provider_api.router, prefix="/api/providers")
    with TestClient(app) as api:
        info = api.get("/api/providers/meta").json()
        assert info["api_key_envs"] == [{"name": "META_API_KEY", "configured": False}]
        assert info["api_key_configured"] is False
        monkeypatch.setenv("META_API_KEY", KEY)
        assert api.get("/api/providers/meta").json()["api_key_configured"] is True
