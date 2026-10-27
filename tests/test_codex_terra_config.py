"""Codex Terra configuration reaches the existing Responses serializer offline."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_clients.factory import get_llm_client
from llm_clients.openai_codex import OpenAICodexClient
from saiverse import data_paths, model_configs, provider_configs

ROOT = Path(__file__).resolve().parents[1]
MODEL_KEY = "codex-gpt-6.1-terra"


@pytest.fixture
def builtin_configs(monkeypatch, tmp_path):
    """Load actual bundled files, never a developer's overrides or cached providers."""
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "expansion_data")
    monkeypatch.setattr(data_paths, "BUILTIN_DATA_DIR", ROOT / "builtin_data")
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", provider_configs.load_configs())
    configs = model_configs.load_configs()
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", configs)
    return configs


def test_builtin_terra_definition_and_phase_contract():
    config = json.loads((ROOT / "builtin_data/models" / f"{MODEL_KEY}.json").read_text(encoding="utf-8"))
    sol = json.loads((ROOT / "builtin_data/models/codex-gpt-6.1-sol.json").read_text(encoding="utf-8"))
    assert config["model"] == "gpt-6.1-terra"
    assert config["provider_ref"] == "openai_codex"
    assert config["supports_images"] is True
    assert config["parameters"] == {}
    assert config["context_length"] == 272000
    assert "pricing" not in config
    assert "公式最大コンテキストではない" in config["system_prompt"]
    phase_marker = "[応答の phase について]"
    assert config["system_prompt"].split(phase_marker)[1] == sol["system_prompt"].split(phase_marker)[1]


def test_terra_is_loaded_as_subscription_provider_without_api_pricing(builtin_configs):
    config = builtin_configs[MODEL_KEY]
    assert config["provider"] == "openai_codex"
    assert config["model"] == "gpt-6.1-terra"
    assert not config.get("api_key_env")
    with patch.dict(model_configs.MODEL_CONFIGS, {MODEL_KEY: config}):
        assert model_configs.calculate_cost(MODEL_KEY, 1_000_000, 1_000_000) == 0


@pytest.mark.parametrize("with_tool", [False, True])
def test_terra_responses_body_preserves_image_and_supported_output_mode(with_tool, builtin_configs):
    config = builtin_configs[MODEL_KEY]
    client = get_llm_client(MODEL_KEY, config["provider"], config["context_length"], config)
    assert isinstance(client, OpenAICodexClient)
    assert client.config_key == MODEL_KEY
    image = "data:image/png;base64,iVBORw0KGgo="
    messages = [{"role": "system", "content": config["system_prompt"]}, {
        "role": "user", "content": [
            {"type": "text", "text": "Synthetic input"},
            {"type": "image_url", "image_url": {"url": image}},
        ],
    }]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    tools = [{"type": "function", "function": {"name": "synthetic_tool", "parameters": schema}}] if with_tool else None
    # Construction/serialization must never read credentials or initialize HTTP.
    with patch.object(client, "_load_auth", side_effect=AssertionError("auth forbidden")), \
         patch.object(client, "_ensure_session", side_effect=AssertionError("network forbidden")):
        body = client._build_body(messages, None, tools, None if with_tool else schema, None)
    assert body["model"] == "gpt-6.1-terra"
    assert body["stream"] is True
    assert body["store"] is False
    assert "temperature" not in body and "max_output_tokens" not in body
    assert body["input"][0]["content"][1] == {"type": "input_image", "image_url": image}
    assert body["instructions"] == config["system_prompt"]
    if with_tool:
        assert body["tools"][0]["name"] == "synthetic_tool"
        assert body["tool_choice"] == "auto"
    else:
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
