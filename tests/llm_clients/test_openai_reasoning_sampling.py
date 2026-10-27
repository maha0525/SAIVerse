"""OpenAI rejects sampling parameters while reasoning is on; other compatible APIs keep them.

The rule comes from the GPT-6 migration guide ("When reasoning effort is not
none, remove temperature, top_p, and top_logprobs. For Chat Completions, also
remove logprobs"). Callers such as the Stelis Chronicle pass temperature=0.3
to whatever lightweight model the persona uses, so the drop has to happen at
the send boundary, from what the provider declares.
"""
import copy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm_clients import openai_runtime
from llm_clients.factory import get_llm_client
from saiverse import data_paths, model_configs, provider_configs

ROOT = Path(__file__).resolve().parents[2]
OPTION = {"supports_sampling_with_reasoning": False}
SAMPLING = {"temperature": 0.3, "top_p": 0.9, "top_logprobs": 2, "logprobs": True}


@pytest.fixture
def builtin_configs(monkeypatch, tmp_path):
    """Load the bundled files only, never a developer's overrides."""
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "expansion_data")
    monkeypatch.setattr(data_paths, "BUILTIN_DATA_DIR", ROOT / "builtin_data")
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", provider_configs.load_configs())
    configs = model_configs.load_configs()
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", configs)
    return configs


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "max"])
def test_drops_sampling_from_body_and_extra_body_while_reasoning(effort):
    req = {"model": "m", "reasoning_effort": effort, **SAMPLING, "extra_body": dict(SAMPLING)}
    before = copy.deepcopy(req)
    adapted = openai_runtime.apply_compat_options(req, OPTION)
    assert req == before
    for key in SAMPLING:
        assert key not in adapted and key not in adapted["extra_body"]
    assert adapted["reasoning_effort"] == effort


def test_effort_in_extra_body_counts():
    adapted = openai_runtime.apply_compat_options(
        {"temperature": 0.3, "extra_body": {"reasoning_effort": "high"}}, OPTION,
    )
    assert "temperature" not in adapted


@pytest.mark.parametrize("effort", [None, "none"])
def test_keeps_sampling_without_reasoning(effort):
    req = {"model": "m", **SAMPLING}
    if effort is not None:
        req["reasoning_effort"] = effort
    assert openai_runtime.apply_compat_options(req, OPTION) == req


def test_keeps_sampling_when_provider_does_not_declare_the_option():
    req = {"model": "m", "reasoning_effort": "high", **SAMPLING}
    assert openai_runtime.apply_compat_options(req, {}) == req
    assert openai_runtime.apply_compat_options(req, {"supports_sampling_with_reasoning": True}) == req


def _sent_kwargs(config_key, configs, monkeypatch, *, override=None):
    monkeypatch.setenv(configs[config_key].get("api_key_env") or "OPENAI_API_KEY", "synthetic-offline-key")
    config = configs[config_key]
    sdk = MagicMock()
    resp = MagicMock()
    resp.usage = None
    resp.model_dump_json.return_value = "{}"
    choice = MagicMock()
    choice.message.content = "ok"
    choice.finish_reason = "stop"
    resp.choices = [choice]
    sdk.chat.completions.create.return_value = resp
    with patch("saiverse.provider_security.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         patch("llm_clients.openai.OpenAI", return_value=sdk):
        client = get_llm_client(config_key, config["provider"], config["context_length"], config)
        if override:
            client.configure_parameters(override)
        client.generate([{"role": "user", "content": "Synthetic input"}], tools=[], temperature=0.3)
    _, kwargs = sdk.chat.completions.create.call_args
    return kwargs


@pytest.mark.parametrize("config_key", ["gpt-6.1-sol", "gpt-6-astra", "gpt-5"])
def test_builtin_openai_reasoning_models_send_no_temperature(builtin_configs, monkeypatch, config_key):
    kwargs = _sent_kwargs(config_key, builtin_configs, monkeypatch)
    assert kwargs["reasoning_effort"] == builtin_configs[config_key]["parameters"]["reasoning_effort"]["default"]
    assert "temperature" not in kwargs


def test_builtin_openai_model_keeps_temperature_when_reasoning_is_none(builtin_configs, monkeypatch):
    kwargs = _sent_kwargs("gpt-6-luna", builtin_configs, monkeypatch, override={"reasoning_effort": "none"})
    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["temperature"] == 0.3


@pytest.mark.parametrize("config_key", ["meta-muse-spark-1.3", "openrouter-qwen3.8-max"])
def test_compatible_provider_with_reasoning_keeps_temperature(builtin_configs, monkeypatch, config_key):
    assert builtin_configs[config_key]["protocol"] == "openai_compat"
    kwargs = _sent_kwargs(config_key, builtin_configs, monkeypatch)
    assert kwargs["reasoning_effort"]
    assert kwargs["temperature"] == 0.3
