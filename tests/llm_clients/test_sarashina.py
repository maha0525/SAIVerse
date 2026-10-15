"""Exercise shipped Sarashina config through loaders, factory, SDK and mocked HTTP."""

import copy
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAI

from api.routes import providers as provider_api
from llm_clients import factory, openai_runtime
from llm_clients.exceptions import AuthenticationError, RateLimitError
from saiverse import data_paths, model_configs, provider_configs

TOOL = {
    "type": "function",
    "function": {
        "name": "synthetic_lookup",
        "description": "Synthetic fixture only",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    },
}
MESSAGES = [{"role": "user", "name": "fixture", "content": "synthetic input"}]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "prompt_tokens_details": None}
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated(tmp_path, monkeypatch, mock_provider_network):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.delenv("SARASHINA_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-dummy-key")
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(provider_configs, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "expansion_data")
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", provider_configs.load_configs())
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", model_configs.load_configs())
    mock_provider_network("api.sarashina.ai")


def completion(tool=False, structured=False):
    msg = {"role": "assistant", "content": '{"answer":"ok"}' if structured else "ok"}
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
        "model": "sarashina3-mini",
        "usage": USAGE,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool else "stop"}],
    }


def sse(tool=False):
    if tool:
        deltas = [
            {"tool_calls": [{"index": 0, "id": "call-fixture", "type": "function", "function": {"name": "synthetic_lookup", "arguments": '{"q":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"test"}'}}]},
        ]
    else:
        deltas = [{"role": "assistant", "content": ""}, {"content": "o"}, {"content": "k"}]
    chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks += [
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
        {"choices": [], "usage": USAGE},
    ]
    envelope = {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1, "model": "sarashina3-mini"}
    return "".join("data: " + json.dumps(envelope | chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


@pytest.fixture
def make_client(isolated, monkeypatch):
    opened = []
    requests = []

    def make(model="sarashina3-mini", *, tool=False, structured=False, status=200):
        monkeypatch.setenv("SARASHINA_API_KEY", "synthetic-sarashina-key")

        def handle(request):
            body = json.loads(request.content)
            requests.append((request, body))
            assert request.url == "https://api.sarashina.ai/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer synthetic-sarashina-key"
            assert "stream_options" not in body
            assert "max_tokens" not in body
            assert body["max_completion_tokens"] == 4096
            assert all("name" not in msg for msg in body["messages"])
            assert not isinstance(body.get("tool_choice"), dict)
            if status != 200:
                return httpx.Response(status, json={"message": "synthetic error"})
            if body.get("stream"):
                return httpx.Response(200, text=sse(tool), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=completion(tool, structured))

        def sdk(**kwargs):
            client = OpenAI(**kwargs, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
            opened.append(client)
            return client

        monkeypatch.setattr("llm_clients.openai.OpenAI", sdk)
        monkeypatch.setattr("llm_clients.openai.MAX_RETRIES", 1)
        cfg = model_configs.get_model_config(model)
        return factory.get_llm_client(model, cfg["provider"], cfg["context_length"], cfg), requests

    yield make
    for sdk in opened:
        sdk.close()


@pytest.mark.parametrize("model", ["sarashina3-mini", "sarashina3-nano"])
def test_catalog_key_availability_and_factory(make_client, model):
    assert not model_configs.is_model_available(model)
    client, requests = make_client(model)
    assert model_configs.is_model_available(model)
    cfg = model_configs.get_model_config(model)
    assert cfg["provider_ref"] == "sarashina"
    assert cfg["context_length"] == 128000
    assert cfg["supports_images"] is False
    assert cfg["supports_structured_output"] is True
    assert client.generate(MESSAGES, tools=[]) == "ok"
    assert requests[0][1]["model"] == model
    usage = client.consume_usage()
    assert (usage.model, usage.input_tokens, usage.output_tokens) == (model, 7, 3)


def test_missing_sarashina_key_does_not_use_openai_key(isolated):
    cfg = model_configs.get_model_config("sarashina3-mini")
    with pytest.raises(AuthenticationError, match="SARASHINA_API_KEY"):
        factory.get_llm_client("sarashina3-mini", cfg["provider"], cfg["context_length"], cfg)


def test_structured_output(make_client):
    client, requests = make_client(structured=True)
    assert client.generate(MESSAGES, tools=[], response_schema=SCHEMA) == {"answer": "ok"}
    fmt = requests[0][1]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True


def test_structured_output_via_stream_interface(make_client):
    client, requests = make_client(structured=True)
    assert list(client.generate_stream(MESSAGES, tools=[], response_schema=SCHEMA))
    assert not requests[0][1].get("stream")
    assert requests[0][1]["response_format"]["type"] == "json_schema"


def test_text_stream_and_final_usage(make_client):
    client, _ = make_client()
    assert "".join(client.generate_stream(MESSAGES, tools=[])) == "ok"
    usage = client.consume_usage()
    assert (usage.input_tokens, usage.output_tokens) == (7, 3)


def test_tool_detection(make_client):
    client, requests = make_client(tool=True)
    result = client.generate(MESSAGES, tools=[TOOL])
    assert result["tool_name"] == "synthetic_lookup"
    assert result["tool_args"] == {"q": "test"}
    assert requests[0][1]["tool_choice"] == "auto"


@pytest.mark.parametrize("forced", [False, True])
def test_tool_stream_reassembles_arguments_and_usage(make_client, forced):
    client, requests = make_client(tool=True)
    other = copy.deepcopy(TOOL)
    other["function"]["name"] = "other_fixture"
    kwargs = {"force_tool_choice": {"type": "function", "function": {"name": "synthetic_lookup"}}} if forced else {}
    list(client.generate_stream(MESSAGES, tools=[TOOL, other], **kwargs))
    result = client.consume_tool_detection()
    assert result["tool_name"] == "synthetic_lookup"
    assert result["tool_args"] == {"q": "test"}
    assert requests[0][1]["tool_choice"] == ("required" if forced else "auto")
    assert len(requests[0][1]["tools"]) == (1 if forced else 2)
    usage = client.consume_usage()
    assert (usage.input_tokens, usage.output_tokens) == (7, 3)


@pytest.mark.parametrize("status,error", [(401, AuthenticationError), (429, RateLimitError)])
def test_api_error_conversion(make_client, status, error):
    client, _ = make_client(status=status)
    with pytest.raises(error):
        client.generate(MESSAGES, tools=[])


def test_provider_api_reports_key_and_edit_preserves_compatibility(isolated, monkeypatch):
    app = FastAPI()
    app.include_router(provider_api.router, prefix="/api/providers")
    monkeypatch.setattr(provider_configs, "reload_providers_and_models", lambda: type("Result", (), {"notices": lambda self: []})())
    with TestClient(app) as api:
        info = api.get("/api/providers/sarashina").json()
        assert info["api_key_envs"] == [{"name": "SARASHINA_API_KEY", "configured": False}]
        assert info["api_key_configured"] is False
        monkeypatch.setenv("SARASHINA_API_KEY", "synthetic-sarashina-key")
        assert api.get("/api/providers/sarashina").json()["api_key_configured"] is True
        response = api.put("/api/providers/sarashina", json={"display_name": "My Sarashina"})
        assert response.status_code == 200, response.text
    saved = json.loads((data_paths.USER_DATA_DIR / "providers/sarashina.json").read_text(encoding="utf-8"))
    assert saved["openai_compat_options"] == provider_configs.get_provider("sarashina")["openai_compat_options"]


def test_compatibility_applies_to_extra_body_without_mutating_inputs():
    req = {
        "stream_options": {"include_usage": True},
        "messages": MESSAGES,
        "extra_body": {
            "stream_options": {"include_usage": True},
            "messages": MESSAGES,
            "tools": [TOOL],
            "tool_choice": {"type": "function", "function": {"name": "synthetic_lookup"}},
        },
    }
    before = copy.deepcopy(req)
    options = {"supports_stream_options": False, "supports_function_tool_choice": False, "supports_message_name": False}
    adapted = openai_runtime.apply_compat_options(req, options)
    assert req == before
    assert "stream_options" not in adapted and "stream_options" not in adapted["extra_body"]
    assert adapted["tool_choice"] == "required"
    assert adapted["tools"] == [TOOL]
    assert "name" not in adapted["messages"][0]
    assert openai_runtime.apply_compat_options(req, {}) == req
    with pytest.raises(ValueError, match="supplied function"):
        openai_runtime.apply_compat_options({"tools": [], "tool_choice": {"type": "function", "function": {"name": "absent"}}}, options)


def test_parameter_limits():
    for model in ("sarashina3-mini", "sarashina3-nano"):
        config = json.loads((ROOT / f"builtin_data/models/{model}.json").read_text(encoding="utf-8"))
        spec = config["parameters"]["max_completion_tokens"]
        assert (spec["min"], spec["max"]) == (1, 8000)
