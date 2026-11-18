"""GPT-6.2 Astra and GPT-6.2 Sol (API and Codex) model config contracts."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_clients.factory import get_llm_client
from llm_clients.openai_codex import OpenAICodexClient
from saiverse import data_paths, model_configs, provider_configs

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize(
    "config_key,expected_input,expected_cached,expected_output",
    [
        ("gpt-6.2-astra", 8.0, 0.4, 40.0),
        ("gpt-6.2-sol", 2.0, 0.1, 10.0),
    ],
)
def test_gpt_62_api_models_capabilities_and_pricing(
    config_key, expected_input, expected_cached, expected_output, builtin_configs
):
    config = builtin_configs[config_key]
    assert config["model"] == config_key
    assert config["provider_ref"] == config["provider"] == "openai"
    assert config["protocol"] == "openai_compat"
    assert config["context_length"] == 272000
    assert config["supports_images"] is True

    params = config["parameters"]
    assert params["reasoning_effort"]["options"] == ["low", "medium", "high", "xhigh", "max"]
    assert params["reasoning_effort"]["default"] == "medium"
    assert params["max_completion_tokens"]["default"] == params["max_completion_tokens"]["max"] == 8192
    assert params["verbosity"]["client_support"] == ["responses"]

    pricing = config["pricing"]
    assert pricing["input_per_1m_tokens"] == expected_input
    assert pricing["cached_input_per_1m_tokens"] == expected_cached
    assert pricing["output_per_1m_tokens"] == expected_output
    assert pricing["long_context_threshold_tokens"] == 272000
    assert pricing["long_context_input_per_1m_tokens"] == expected_input * 2
    assert pricing["long_context_cached_input_per_1m_tokens"] == expected_cached * 2
    assert pricing["long_context_output_per_1m_tokens"] == expected_output * 1.5

    # Standard tier calculation (<= 272k)
    cost_short = model_configs.calculate_cost(config_key, 100_000, 10_000, cached_tokens=20_000)
    expected_short = (0.08 * expected_input) + (0.02 * expected_cached) + (0.01 * expected_output)
    assert cost_short == pytest.approx(expected_short)

    # Long-context tier calculation (> 272k)
    cost_long = model_configs.calculate_cost(config_key, 300_000, 10_000, cached_tokens=50_000)
    expected_long = (0.25 * expected_input * 2) + (0.05 * expected_cached * 2) + (0.01 * expected_output * 1.5)
    assert cost_long == pytest.approx(expected_long)


@pytest.mark.parametrize(
    "config_key,api_model",
    [
        ("codex-gpt-6.2-astra", "gpt-6.2-astra"),
        ("codex-gpt-6.2-sol", "gpt-6.2-sol"),
    ],
)
def test_gpt_62_codex_models_definition_and_serialization(config_key, api_model, builtin_configs):
    raw = json.loads((ROOT / "builtin_data/models" / f"{config_key}.json").read_text(encoding="utf-8"))
    sol = json.loads((ROOT / "builtin_data/models/codex-gpt-6.1-sol.json").read_text(encoding="utf-8"))
    assert raw["model"] == api_model
    assert raw["provider_ref"] == "openai_codex"
    assert raw["context_length"] == 272000
    assert raw["supports_images"] is True
    assert raw["parameters"] == {}
    assert "pricing" not in raw

    phase_marker = "[応答の phase について]"
    assert raw["system_prompt"].split(phase_marker)[1] == sol["system_prompt"].split(phase_marker)[1]

    config = builtin_configs[config_key]
    assert config["provider"] == "openai_codex"
    assert not config.get("api_key_env")
    assert model_configs.calculate_cost(config_key, 1_000_000, 1_000_000) == 0

    client = get_llm_client(config_key, config["provider"], config["context_length"], config)
    assert isinstance(client, OpenAICodexClient)
    assert client.config_key == config_key
    messages = [{"role": "system", "content": config["system_prompt"]}, {"role": "user", "content": "Synthetic input"}]
    with patch.object(client, "_load_auth", side_effect=AssertionError("auth forbidden")), \
         patch.object(client, "_ensure_session", side_effect=AssertionError("network forbidden")):
        body = client._build_body(messages, None, None, None, None)
    assert body["model"] == api_model
    assert body["stream"] is True
    assert body["store"] is False
