"""Tests for model_configs.py — provider resolution, cost calculation, config lookup."""
import unittest

from saiverse import model_configs


class TestGetModelProvider(unittest.TestCase):
    def test_known_model_returns_provider(self):
        # claude-sonnet-4-5 is configured as anthropic
        provider = model_configs.get_model_provider("claude-sonnet-4-5")
        self.assertEqual(provider, "anthropic")

    def test_unknown_model_raises_error(self):
        with self.assertRaises(ValueError) as ctx:
            model_configs.get_model_provider("nonexistent-model-xyz")
        self.assertIn("nonexistent-model-xyz", str(ctx.exception))


class TestGetContextLength(unittest.TestCase):
    def test_known_model_returns_configured_length(self):
        length = model_configs.get_context_length("claude-sonnet-4-5")
        self.assertEqual(length, 64000)

    def test_unknown_model_raises_error(self):
        with self.assertRaises(ValueError) as ctx:
            model_configs.get_context_length("nonexistent-model-xyz")
        self.assertIn("nonexistent-model-xyz", str(ctx.exception))


class TestCalculateCost(unittest.TestCase):
    def test_model_with_pricing(self):
        # claude-sonnet-4-5: input $3/1M, output $15/1M
        cost = model_configs.calculate_cost("claude-sonnet-4-5", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 18.0)

    def test_cached_tokens_discount(self):
        # 1M input with 500K cached (cached rate: $0.3/1M), no output
        cost = model_configs.calculate_cost("claude-sonnet-4-5", 1_000_000, 0, cached_tokens=500_000)
        # non-cached: 500K * $3/1M = $1.5, cached: 500K * $0.3/1M = $0.15
        self.assertAlmostEqual(cost, 1.65)

    def test_cache_write_tokens_premium(self):
        # 1M input with 500K cache_write (write rate: $3.75/1M), no output
        cost = model_configs.calculate_cost(
            "claude-sonnet-4-5", 1_000_000, 0, cache_write_tokens=500_000
        )
        # non-cached: 500K * $3/1M = $1.5, cache_write: 500K * $3.75/1M = $1.875
        self.assertAlmostEqual(cost, 3.375)

    def test_cache_write_1h_tokens_premium(self):
        # 1M input with 500K cache_write at 1h TTL (write rate: $6/1M), no output
        cost = model_configs.calculate_cost(
            "claude-sonnet-4-5", 1_000_000, 0,
            cache_write_tokens=500_000, cache_ttl="1h",
        )
        # non-cached: 500K * $3/1M = $1.5, cache_write: 500K * $6/1M = $3.0
        self.assertAlmostEqual(cost, 4.5)

    def test_cache_write_1h_fallback_to_5m_rate(self):
        # Model without 1h-specific pricing should use default cache_write rate
        cost_5m = model_configs.calculate_cost(
            "claude-sonnet-4-5", 1_000_000, 0,
            cache_write_tokens=500_000, cache_ttl="5m",
        )
        # non-cached: 500K * $3/1M = $1.5, cache_write: 500K * $3.75/1M = $1.875
        self.assertAlmostEqual(cost_5m, 3.375)

    def test_no_pricing_returns_zero(self):
        cost = model_configs.calculate_cost("nonexistent-model-xyz", 1_000_000, 1_000_000)
        self.assertEqual(cost, 0.0)


class TestModelSupportsImages(unittest.TestCase):
    def test_vision_capable_model(self):
        self.assertTrue(model_configs.model_supports_images("claude-sonnet-4-5"))

    def test_non_vision_model(self):
        self.assertFalse(model_configs.model_supports_images("stockmark-stockmark-2-100b-instruct"))


class TestFindModelConfig(unittest.TestCase):
    def test_find_by_config_key(self):
        key, config = model_configs.find_model_config("claude-sonnet-4-5")
        self.assertEqual(key, "claude-sonnet-4-5")
        self.assertEqual(config.get("provider"), "anthropic")

    def test_find_by_api_model_name(self):
        key, config = model_configs.find_model_config("stockmark/stockmark-2-100b-instruct")
        self.assertTrue(key)
        self.assertEqual(config.get("model"), "stockmark/stockmark-2-100b-instruct")

    def test_not_found(self):
        key, config = model_configs.find_model_config("nonexistent-model-xyz-abc")
        self.assertEqual(key, "")
        self.assertEqual(config, {})


class TestIsLocalModel(unittest.TestCase):
    def test_unknown_model_is_not_local(self):
        # Unknown models return False (no config = no fallback to ollama)
        self.assertFalse(model_configs.is_local_model("nonexistent-model-xyz"))

    def test_anthropic_is_not_local(self):
        self.assertFalse(model_configs.is_local_model("claude-sonnet-4-5"))


class TestSupportsStructuredOutput(unittest.TestCase):
    def test_default_true(self):
        # Models without explicit config default to True
        self.assertTrue(model_configs.supports_structured_output("claude-sonnet-4-5"))

    def test_explicit_false(self):
        self.assertFalse(model_configs.supports_structured_output("stockmark-stockmark-2-100b-instruct"))


if __name__ == "__main__":
    unittest.main()
