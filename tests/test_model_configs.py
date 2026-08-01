"""Tests for model_configs.py — provider resolution, cost calculation, config lookup."""
import os
import unittest
from unittest.mock import patch

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
        self.assertEqual(length, 200000)

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

    @staticmethod
    def _codex_config_keys(configs):
        """Codex 設定を列挙する。

        provider_ref だけを見ると、legacy の provider フィールドしか持たない設定
        (factory は今もこれを有効な protocol として扱う) と、provider_ref を落とした
        user_data override を取りこぼす。
        """
        return [
            key for key, config in configs.items()
            if "openai_codex" in (config.get("provider_ref"), config.get("provider"))
        ]

    def test_subscription_backed_configs_are_unpriced(self):
        """Codex はサブスク課金なので、単価を書くと使用画面に架空の金額が出る。

        Codex 設定の API モデル名は従量課金版の設定キーと衝突するため、価格は
        必ず設定キー側 (codex-*) で引かれ、そこが無価格である必要がある。
        """
        codex_keys = self._codex_config_keys(model_configs.MODEL_CONFIGS)
        self.assertTrue(codex_keys, "no openai_codex models found — check the fixture set")
        for key in codex_keys:
            with self.subTest(model=key):
                self.assertIsNone(model_configs.MODEL_CONFIGS[key].get("pricing"))
                self.assertEqual(
                    model_configs.calculate_cost(key, 1_000_000, 1_000_000), 0.0
                )

    def test_legacy_provider_form_is_enumerated(self):
        """provider_ref を持たない Codex 設定も上の検査に入ること。

        legacy 形式 (provider フィールドのみ) と provider_ref を落とした user_data
        override はどちらも実在しうる。列挙が provider_ref だけを見ていると、
        そこに価格が書かれていても検査をすり抜ける。
        """
        configs = {
            "codex-legacy-form": {"model": "gpt-5.6-terra", "provider": "openai_codex"},
            "codex-ref-form": {"model": "gpt-5.6-terra", "provider_ref": "openai_codex"},
            "unrelated": {"model": "gpt-4.1", "provider": "openai"},
        }
        self.assertEqual(
            sorted(self._codex_config_keys(configs)),
            ["codex-legacy-form", "codex-ref-form"],
        )


class TestModelSupportsImages(unittest.TestCase):
    def test_vision_capable_model(self):
        self.assertTrue(model_configs.model_supports_images("claude-sonnet-4-5"))

    def test_non_vision_model(self):
        self.assertFalse(model_configs.model_supports_images("nim-deepseek-v4-pro"))


class TestFindModelConfig(unittest.TestCase):
    def test_find_by_config_key(self):
        key, config = model_configs.find_model_config("claude-sonnet-4-5")
        self.assertEqual(key, "claude-sonnet-4-5")
        self.assertEqual(config.get("provider"), "anthropic")

    def test_find_by_api_model_name(self):
        key, config = model_configs.find_model_config("mistralai/mistral-large-3-675b-instruct-2512")
        self.assertTrue(key)
        self.assertEqual(config.get("model"), "mistralai/mistral-large-3-675b-instruct-2512")

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
        self.assertFalse(model_configs.supports_structured_output("nim-step-3.5-flash"))


class TestRequiredEnvVars(unittest.TestCase):
    """The availability gate behind GET /models.

    A model with several accepted key names must stay available when ANY of
    them is set. Gemini is the live case: the provider declares
    api_key_env=GEMINI_API_KEY plus api_key_env_alternates=[GEMINI_FREE_API_KEY],
    and a free-tier-only setup must still see Gemini models.
    """

    def _gate(self, config):
        saved = model_configs.MODEL_CONFIGS
        try:
            model_configs.MODEL_CONFIGS = {"probe-model": config}
            return model_configs._get_required_env_vars("probe-model")
        finally:
            model_configs.MODEL_CONFIGS = saved

    def test_alternates_are_returned_alongside_primary(self):
        names = self._gate({
            "provider": "gemini",
            "api_key_env": "GEMINI_API_KEY",
            "api_key_env_alternates": ["GEMINI_FREE_API_KEY"],
        })
        self.assertEqual(names, ["GEMINI_API_KEY", "GEMINI_FREE_API_KEY"])

    def test_primary_alone_when_no_alternates(self):
        names = self._gate({"provider": "openai", "api_key_env": "OPENAI_API_KEY"})
        self.assertEqual(names, ["OPENAI_API_KEY"])

    def test_alternates_deduplicated_and_type_checked(self):
        names = self._gate({
            "provider": "gemini",
            "api_key_env": "GEMINI_API_KEY",
            "api_key_env_alternates": ["GEMINI_API_KEY", "", None, "OTHER_KEY"],
        })
        self.assertEqual(names, ["GEMINI_API_KEY", "OTHER_KEY"])

    def test_local_models_need_no_key(self):
        self.assertEqual(self._gate({"provider": "ollama"}), [])

    def test_builtin_gemini_model_accepts_either_key(self):
        # Regression guard: migrating gemini models to provider_ref must not
        # collapse the gate down to the paid key alone.
        names = model_configs._get_required_env_vars("gemini-3.6-flash")
        self.assertIn("GEMINI_API_KEY", names)
        self.assertIn("GEMINI_FREE_API_KEY", names)

    def test_free_key_alone_keeps_gemini_available(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GEMINI_API_KEY", "GEMINI_FREE_API_KEY")}
        env["GEMINI_FREE_API_KEY"] = "test-free-key"
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(model_configs.is_model_available("gemini-3.6-flash"))


class TestProviderRefInheritance(unittest.TestCase):
    def test_alternates_inherited_from_provider(self):
        resolved = model_configs._resolve_provider_ref({
            "model": "probe", "provider_ref": "gemini",
        })
        self.assertEqual(resolved.get("api_key_env"), "GEMINI_API_KEY")
        self.assertEqual(
            resolved.get("api_key_env_alternates"), ["GEMINI_FREE_API_KEY"],
        )

    def test_model_fields_win_over_provider(self):
        resolved = model_configs._resolve_provider_ref({
            "model": "probe",
            "provider_ref": "gemini",
            "api_key_env": "CUSTOM_KEY",
        })
        self.assertEqual(resolved.get("api_key_env"), "CUSTOM_KEY")

    def test_builtin_models_carry_no_connection_info(self):
        """Connection details belong to the provider, not the model.

        builtin models must not pin base_url/api_key_env themselves — that is
        what lets an external catalog be refused those fields later.
        """
        from saiverse.data_paths import BUILTIN_DATA_DIR, MODELS_DIR
        import json

        offenders = []
        for path in sorted((BUILTIN_DATA_DIR / MODELS_DIR).glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("llama_server") or raw.get("llama_slot_save_path"):
                continue  # local llama.cpp templates legitimately pin a port
            if raw.get("base_url") or raw.get("api_key_env"):
                offenders.append(path.stem)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
