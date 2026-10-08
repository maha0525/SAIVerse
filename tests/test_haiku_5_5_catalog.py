"""Haiku 5.5 catalog contract and prompt-length pricing boundaries."""

import pytest

from saiverse import model_configs


def test_haiku_5_5_config():
    config = model_configs.get_model_config('claude-haiku-5.5')
    assert config['model'] == 'claude-haiku-5-5'
    assert config['provider_ref'] == 'anthropic'
    assert config['context_length'] == 1_000_000
    assert config['max_output_tokens'] == 128_000
    assert config['supports_images'] is True
    assert config['supports_sampling_parameters'] is False
    assert config['supports_assistant_prefill'] is False
    assert config['thinking_type'] == 'adaptive'
    assert config['thinking_effort'] == 'medium'
    assert 'thinking_budget' not in config
    assert config['parameters']['thinking_effort']['default'] == 'medium'
    assert config['parameters']['thinking_effort']['options'] == [
        'low', 'medium', 'high', 'xhigh', 'max',
    ]
    assert config['cache']['min_tokens'] == 512
    assert config['cache']['ttl_options'] == ['5m', '1h']


@pytest.mark.parametrize('input_tokens,input_rate,output_rate,cached_rate,write_5m,write_1h', [
    (100_000, .1, .5, .01, .125, .2),
    (100_001, .5, 2.5, .05, .625, 1),
])
@pytest.mark.parametrize('ttl', ['5m', '1h'])
def test_haiku_5_5_all_rates_at_prompt_boundary(
    input_tokens, input_rate, output_rate, cached_rate, write_5m, write_1h, ttl,
):
    # Total input includes cache reads and writes for the threshold decision.
    cached, written, output = 20_000, 30_000, 10_000
    expected = (
        (input_tokens - cached - written) * input_rate
        + cached * cached_rate
        + written * (write_1h if ttl == '1h' else write_5m)
        + output * output_rate
    ) / 1_000_000
    assert model_configs.calculate_cost(
        'claude-haiku-5.5', input_tokens, output,
        cached_tokens=cached, cache_write_tokens=written, cache_ttl=ttl,
    ) == pytest.approx(expected)


def test_long_context_1h_cache_write_falls_back_to_standard_1h(monkeypatch):
    monkeypatch.setattr(model_configs, 'MODEL_CONFIGS', {'tiered-test': {
        'model': 'tiered-test',
        'pricing': {
            'input_per_1m_tokens': 1,
            'cache_write_per_1m_tokens': 1.25,
            'cache_write_1h_per_1m_tokens': 2,
            'long_context_threshold_tokens': 100,
            'long_context_cache_write_per_1m_tokens': 2.5,
        },
    }})
    assert model_configs.calculate_cost(
        'tiered-test', 1000, 0, cache_write_tokens=1000, cache_ttl='1h',
    ) == pytest.approx(.002)
