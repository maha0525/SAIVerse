"""Tests for provider_configs.py and provider_ref resolution in model_configs."""
import json
import os
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


def _read_builtin_provider(pid: str) -> dict:
    """Load a provider straight from builtin_data.

    Deliberately bypasses load_configs(): a user_data override with the same id
    would otherwise hide the shipped definition, and these tests are about what
    SAIVerse ships.
    """
    from saiverse.data_paths import BUILTIN_DATA_DIR, PROVIDERS_DIR

    cfg = json.loads(
        (BUILTIN_DATA_DIR / PROVIDERS_DIR / f"{pid}.json").read_text(encoding="utf-8")
    )
    cfg["builtin"] = True  # what load_configs() injects for builtin_data files
    return cfg


class TestLoadProviders(unittest.TestCase):
    """Verify builtin providers load with builtin: True flag."""

    def test_seven_builtin_providers_loaded(self):
        configs = provider_configs.load_configs()
        for pid in ("anthropic", "gemini", "openai", "ollama",
                    "nvidia_nim", "xai", "openai_codex"):
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
        # The builtin ollama provider deliberately carries no base_url: an
        # address here would count as "configured" and would disable both
        # OLLAMA_BASE_URL and local discovery. Users set the address by editing
        # the provider (UI -> user_data override), which does get inherited —
        # see tests/test_ollama_endpoint.py.
        self.assertIsNone(out.get("base_url"))


class TestKeylessProviders(unittest.TestCase):
    """Local OpenAI-compatible servers that accept any API key.

    LM Studio and llama.cpp server speak the OpenAI protocol but perform no
    authentication. Without `api_key_required: false` the OpenAI client demands
    a real OPENAI_API_KEY and refuses to construct, so a user with no OpenAI
    account could not run a local model at all — and docs/custom_providers.md
    told them to leave the key field empty.
    """

    NO_KEYS = ("OPENAI_API_KEY", "LMSTUDIO_API_KEY",
               "SAIVERSE_PROVIDER_LMSTUDIO_API_KEY")

    def _env_without_keys(self):
        return patch.dict(
            os.environ,
            {k: v for k, v in os.environ.items() if k not in self.NO_KEYS},
            clear=True,
        )

    def test_lmstudio_is_shipped_and_needs_no_key(self):
        cfg = _read_builtin_provider("lmstudio")
        self.assertEqual(cfg["protocol"], "openai_compat")
        # base_url is mandatory here, unlike ollama: an empty one would send
        # requests to api.openai.com instead of the local server.
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:1234/v1")
        self.assertIs(cfg["api_key_required"], False)
        # No api_key_env: a custom-named variable would fail the credential
        # binding check the moment the user edits this provider in the UI
        # (the override is no longer builtin, so validate_provider_config
        # enforces SAIVERSE_PROVIDER_<ID>_API_KEY).
        self.assertIsNone(cfg.get("api_key_env"))

    def test_llama_cpp_server_needs_no_key(self):
        cfg = _read_builtin_provider("llama_cpp_server")
        self.assertIs(cfg["api_key_required"], False)

    def test_edited_copy_passes_credential_validation(self):
        """A UI edit drops the builtin flag; the result must still validate."""
        from saiverse.provider_security import validate_provider_config

        cfg = _read_builtin_provider("lmstudio")
        cfg.pop("builtin")  # what save_provider() strips before writing
        validate_provider_config("lmstudio", cfg)  # must not raise

    def _use_builtin_provider(self, pid="lmstudio", **overrides):
        """Pin get_provider() to the shipped definition for the whole block.

        The patch must stay open across get_llm_client() too: the factory
        re-reads the provider for its credential check, and on a machine that
        has a user_data override of this id it would otherwise validate against
        that instead of what SAIVerse ships.
        """
        cfg = _read_builtin_provider(pid)
        cfg.update(overrides)
        return patch("saiverse.provider_configs.get_provider", return_value=cfg)

    def test_flag_is_inherited_by_models(self):
        with self._use_builtin_provider():
            resolved = model_configs._resolve_provider_ref(
                {"model": "local-model", "provider_ref": "lmstudio"},
            )
        self.assertIs(resolved.get("api_key_required"), False)
        self.assertEqual(resolved.get("base_url"), "http://127.0.0.1:1234/v1")

    def test_model_stays_available_without_any_key(self):
        with self._use_builtin_provider():
            resolved = model_configs._resolve_provider_ref(
                {"model": "local-model", "provider_ref": "lmstudio"},
            )
        saved = model_configs.MODEL_CONFIGS
        try:
            model_configs.MODEL_CONFIGS = {"probe": resolved}
            with self._env_without_keys():
                self.assertEqual(model_configs._get_required_env_vars("probe"), [])
                self.assertTrue(model_configs.is_model_available("probe"))
        finally:
            model_configs.MODEL_CONFIGS = saved

    def test_client_builds_with_placeholder_key(self):
        from llm_clients.factory import (
            get_llm_client, _LOCAL_SERVER_PLACEHOLDER_KEY,
        )

        with self._use_builtin_provider():
            resolved = model_configs._resolve_provider_ref(
                {"model": "local-model", "provider_ref": "lmstudio"},
            )
            with self._env_without_keys():
                client = get_llm_client("probe", resolved["provider"], 4096, resolved)
        self.assertEqual(client.client.api_key, _LOCAL_SERVER_PLACEHOLDER_KEY)
        self.assertIn("127.0.0.1:1234", str(client.client.base_url))

    def test_real_key_wins_over_placeholder(self):
        """Local servers can be put behind auth; don't override a set key."""
        from llm_clients.factory import (
            get_llm_client, _LOCAL_SERVER_PLACEHOLDER_KEY,
        )

        key_env = "SAIVERSE_PROVIDER_LMSTUDIO_API_KEY"
        with self._use_builtin_provider(api_key_env=key_env):
            resolved = model_configs._resolve_provider_ref(
                {"model": "local-model", "provider_ref": "lmstudio"},
            )
            with patch.dict(os.environ, {key_env: "real-secret"}):
                client = get_llm_client("probe", resolved["provider"], 4096, resolved)
        self.assertEqual(client.client.api_key, "real-secret")
        self.assertNotEqual(client.client.api_key, _LOCAL_SERVER_PLACEHOLDER_KEY)

    def test_api_exposes_and_round_trips_the_flag(self):
        """The editor UI reads and writes this field, so it must survive both ways."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes import providers as prov

        cfg = _read_builtin_provider("lmstudio")
        app = FastAPI()
        app.include_router(prov.router, prefix="/api/providers")
        client = TestClient(app)

        with patch.object(prov.provider_configs, "PROVIDER_CONFIGS", {"lmstudio": cfg}), \
                patch.object(prov.provider_configs, "get_provider", return_value=cfg):
            body = client.get("/api/providers/lmstudio").json()
        self.assertIs(body["api_key_required"], False)

        # False must not be dropped as "unset" on the way back in, otherwise
        # unchecking the box in the UI would silently do nothing.
        payload = prov.ProviderUpdateRequest(
            display_name="LM Studio (local)", api_key_required=False,
        ).model_dump(exclude_none=True)
        self.assertIs(payload["api_key_required"], False)

    def test_normal_provider_still_requires_its_key(self):
        """The flag must not leak into providers that do authenticate."""
        saved = model_configs.MODEL_CONFIGS
        try:
            model_configs.MODEL_CONFIGS = {
                "probe": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
            }
            self.assertEqual(
                model_configs._get_required_env_vars("probe"), ["OPENAI_API_KEY"],
            )
            with self._env_without_keys():
                self.assertFalse(model_configs.is_model_available("probe"))
        finally:
            model_configs.MODEL_CONFIGS = saved


class TestListModelsUsingProvider(unittest.TestCase):
    """Reverse lookup: which models reference a given provider?"""

    def test_only_lists_models_pointing_at_that_provider(self):
        # Builtin models were migrated to provider_ref (2026-07-23), so this
        # lookup now returns real results rather than an empty list — every
        # entry must actually reference the queried provider.
        using = provider_configs.list_models_using_provider("openai")
        self.assertTrue(using, "expected builtin models to reference 'openai'")
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
            "gemini_native", "xai_native",
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
