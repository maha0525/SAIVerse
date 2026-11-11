"""Claude 5 系を中心とした組み込みモデル定義の契約 (Fable 5.2、サンプリング非対応)。"""

import pytest

from saiverse import model_configs

# 公式仕様で temperature / top_p / top_k の非既定値が 400 になるモデル。
SAMPLING_REJECTING_MODELS = [
    "claude-fable-5.2", "claude-fable-5.1", "claude-fable-5",
    "claude-opus-5.5", "claude-opus-5", "claude-opus-4.8", "claude-opus-4.7",
    "claude-sonnet-5.5", "claude-sonnet-5", "claude-haiku-5.5",
]


def test_fable_5_2_pricing_and_limits():
    config = model_configs.get_model_config("claude-fable-5.2")
    assert config["model"] == "claude-fable-5-2"
    assert config["max_output_tokens"] == 128_000
    assert model_configs.calculate_cost("claude-fable-5.2", 1_000_000, 1_000_000) == pytest.approx(48.0)


@pytest.mark.parametrize("key", SAMPLING_REJECTING_MODELS)
def test_models_rejecting_sampling_parameters_opt_out(key):
    assert model_configs.get_model_config(key)["supports_sampling_parameters"] is False
