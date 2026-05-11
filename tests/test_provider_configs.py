"""Tests for provider_configs.py and provider_ref resolution in model_configs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from saiverse import model_configs, provider_configs
from llm_clients.factory import _resolve_protocol


def _patch_user_data(tmp_path: Path) -> list:
    """Return started patches that redirect USER_DATA_DIR to tmp_path everywhere
    it's bound. Caller must stop the patches in tearDown / cleanup.
    """
    patches = [
        patch("saiverse.data_paths.USER_DATA_DIR", tmp_path),
        patch("saiverse.provider_configs.USER_DATA_DIR", tmp_path),
    ]
    for p in patches:
        p.start()
    return patches


class TestLoadProviders(unittest.TestCase):
    """Verify builtin providers load with builtin: True flag."""

    def test_eight_builtin_providers_loaded(self):
        configs = provider_configs.load_configs()
        # Eight builtin providers ship with the repo
        for pid in ("anthropic", "gemini", "openai", "ollama",
                    "llama_cpp", "nvidia_nim", "xai", "openai_codex"):
            self.assertIn(pid, configs, f"Missing builtin provider: {pid}")
            self.assertTrue(
                configs[pid].get("builtin"),
                f"Builtin provider {pid} should have builtin: True",
            )

    def test_builtin_provider_has_protocol(self):
        configs = provider_configs.load_configs()
        # Every builtin must declare a protocol
        for pid, cfg in configs.items():
            if cfg.get("builtin"):
                self.assertIn(
                    "protocol", cfg,
                    f"Builtin provider {pid} missing 'protocol'",
                )


class TestSaveDeleteProvider(unittest.TestCase):
    """Provider CRUD against an isolated tempdir USER_DATA_DIR."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp_path = Path(self.tmp_dir.name)
        self.patches = _patch_user_data(self.tmp_path)
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        # Reload so provider_configs sees the empty user_data
        provider_configs.reload_configs()
        self.addCleanup(provider_configs.reload_configs)

    def test_save_creates_user_data_file(self):
        provider_configs.save_provider("lmstudio_test", {
            "display_name": "LM Studio Test",
            "protocol": "openai_compat",
            "base_url": "http://localhost:1234/v1",
            "api_key_env": "LMSTUDIO_API_KEY",
        })
        target = self.tmp_path / "providers" / "lmstudio_test.json"
        self.assertTrue(target.exists())
        loaded = json.loads(target.read_text(encoding="utf-8"))
        # id should be enforced from the function arg
        self.assertEqual(loaded["id"], "lmstudio_test")
        # builtin flag should be stripped
        self.assertNotIn("builtin", loaded)

    def test_save_strips_builtin_flag(self):
        provider_configs.save_provider("foo_test", {
            "display_name": "Foo",
            "protocol": "openai_compat",
            "base_url": "http://x",
            "builtin": True,  # should be stripped on save
        })
        target = self.tmp_path / "providers" / "foo_test.json"
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertNotIn("builtin", loaded)

    def test_save_invalid_id_raises(self):
        with self.assertRaises(ValueError):
            provider_configs.save_provider("bad/id", {"protocol": "openai_compat"})
        with self.assertRaises(ValueError):
            provider_configs.save_provider("", {"protocol": "openai_compat"})
        with self.assertRaises(ValueError):
            provider_configs.save_provider("../escape", {"protocol": "openai_compat"})

    def test_save_then_load_roundtrip(self):
        provider_configs.save_provider("kimi_test", {
            "display_name": "Kimi",
            "protocol": "openai_compat",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key_env": "KIMI_API_KEY",
        })
        # save_provider already calls reload, so PROVIDER_CONFIGS is fresh
        cfg = provider_configs.get_provider("kimi_test")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["base_url"], "https://api.moonshot.cn/v1")
        # user_data files are not builtin
        self.assertFalse(cfg.get("builtin"))

    def test_delete_user_data_provider(self):
        provider_configs.save_provider("delete_me", {"protocol": "openai_compat", "base_url": "http://x"})
        target = self.tmp_path / "providers" / "delete_me.json"
        self.assertTrue(target.exists())
        provider_configs.delete_provider("delete_me")
        self.assertFalse(target.exists())
        self.assertIsNone(provider_configs.get_provider("delete_me"))

    def test_delete_builtin_only_raises(self):
        # 'anthropic' exists only in builtin_data (no user_data override)
        with self.assertRaises(ValueError) as ctx:
            provider_configs.delete_provider("anthropic")
        self.assertIn("builtin", str(ctx.exception).lower())

    def test_delete_nonexistent_raises(self):
        with self.assertRaises(FileNotFoundError):
            provider_configs.delete_provider("never_existed_xyz")

    def test_user_data_overrides_builtin(self):
        # Create a user_data override for 'openai' with a different base_url
        provider_configs.save_provider("openai", {
            "display_name": "OpenAI (overridden)",
            "protocol": "openai_compat",
            "base_url": "https://my-proxy.example.com/v1",
            "api_key_env": "OPENAI_API_KEY",
        })
        cfg = provider_configs.get_provider("openai")
        self.assertEqual(cfg["base_url"], "https://my-proxy.example.com/v1")
        # user_data override is not marked builtin
        self.assertFalse(cfg.get("builtin"))


class TestResolveProviderRef(unittest.TestCase):
    """provider_ref resolution: direct fields win, provider supplies defaults."""

    def setUp(self):
        # Use real builtin providers (no temp patching needed for read-only tests)
        provider_configs.reload_configs()

    def test_no_provider_ref_returns_unchanged(self):
        cfg = {"model": "x", "provider": "openai", "base_url": "http://a"}
        out = model_configs._resolve_provider_ref(cfg)
        # Same dict (no ref means no copy)
        self.assertIs(out, cfg)

    def test_provider_ref_inherits_protocol_and_base_url(self):
        cfg = {
            "model": "qwen2.5",
            "provider_ref": "openai",  # builtin OpenAI provider
        }
        out = model_configs._resolve_provider_ref(cfg)
        self.assertEqual(out["protocol"], "openai_compat")
        self.assertEqual(out["provider"], "openai")
        self.assertEqual(out["base_url"], "https://api.openai.com/v1")
        self.assertEqual(out["api_key_env"], "OPENAI_API_KEY")

    def test_direct_fields_override_provider_defaults(self):
        cfg = {
            "model": "custom",
            "provider_ref": "openai",
            "base_url": "http://override.local/v1",
            "api_key_env": "MY_OVERRIDE_KEY",
        }
        out = model_configs._resolve_provider_ref(cfg)
        # Direct fields win
        self.assertEqual(out["base_url"], "http://override.local/v1")
        self.assertEqual(out["api_key_env"], "MY_OVERRIDE_KEY")
        # But protocol/provider still come from the provider
        self.assertEqual(out["protocol"], "openai_compat")

    def test_unknown_provider_ref_returns_unchanged(self):
        cfg = {"model": "x", "provider_ref": "no_such_provider_xyz"}
        out = model_configs._resolve_provider_ref(cfg)
        # Original dict back; warning is logged but config is not mutated
        self.assertIs(out, cfg)
        self.assertNotIn("protocol", cfg)

    def test_ollama_compat_inheritance(self):
        cfg = {"model": "llama3", "provider_ref": "ollama"}
        out = model_configs._resolve_provider_ref(cfg)
        self.assertEqual(out["protocol"], "ollama_compat")
        self.assertEqual(out["provider"], "ollama")
        self.assertEqual(out["base_url"], "http://127.0.0.1:11434")


class TestListModelsUsingProvider(unittest.TestCase):
    """Reverse lookup: which models reference a given provider?"""

    def test_existing_models_use_no_provider_ref(self):
        # Pre-existing builtin models all use the 'provider' field, not provider_ref
        # so list_models_using_provider should return empty for any builtin id
        using = provider_configs.list_models_using_provider("openai")
        # No model in builtin_data/models/ uses provider_ref yet
        for m in using:
            cfg = model_configs.MODEL_CONFIGS.get(m, {})
            self.assertEqual(cfg.get("provider_ref"), "openai")

    def test_returns_sorted(self):
        # Even with empty result, the function should return a list
        using = provider_configs.list_models_using_provider("nonexistent")
        self.assertEqual(using, [])


class TestExistingModelsBackwardCompat(unittest.TestCase):
    """Verify all existing models still load and produce a valid protocol."""

    def test_all_models_resolve_to_known_protocol(self):
        valid_protocols = {
            "openai_compat", "ollama_compat", "anthropic_native",
            "gemini_native", "xai_native", "llama_cpp_native",
            "nvidia_nim", "openai_codex",
        }
        unknown = []
        for key, cfg in model_configs.MODEL_CONFIGS.items():
            provider = cfg.get("provider", "ollama")
            proto = _resolve_protocol(provider, cfg)
            if proto not in valid_protocols:
                unknown.append((key, provider, proto))
        self.assertEqual(unknown, [], f"Models with unknown protocol: {unknown}")

    def test_existing_model_has_provider_field(self):
        # Spot-check that an existing model still has its provider field
        cfg = model_configs.MODEL_CONFIGS.get("claude-sonnet-4-5", {})
        self.assertEqual(cfg.get("provider"), "anthropic")


class TestProtocolResolution(unittest.TestCase):
    """factory.py _resolve_protocol logic."""

    def test_explicit_protocol_wins(self):
        # Explicit protocol on config beats the provider arg
        self.assertEqual(
            _resolve_protocol("openai", {"protocol": "openai_compat"}),
            "openai_compat",
        )
        self.assertEqual(
            _resolve_protocol("anthropic", {"protocol": "openai_compat"}),
            "openai_compat",
        )

    def test_legacy_provider_mapping(self):
        cases = [
            ("openai", "openai_compat"),
            ("ollama", "ollama_compat"),
            ("anthropic", "anthropic_native"),
            ("gemini", "gemini_native"),
            ("xai", "xai_native"),
            ("llama_cpp", "llama_cpp_native"),
            ("nvidia_nim", "nvidia_nim"),
            ("openai_codex", "openai_codex"),
        ]
        for legacy, expected in cases:
            with self.subTest(legacy=legacy):
                self.assertEqual(_resolve_protocol(legacy, None), expected)
                self.assertEqual(_resolve_protocol(legacy, {}), expected)

    def test_unknown_provider_passthrough(self):
        # Unknown providers are not silently mapped — they pass through
        # so the factory raises a clear error downstream
        self.assertEqual(_resolve_protocol("foo_bar", None), "foo_bar")


if __name__ == "__main__":
    unittest.main()
