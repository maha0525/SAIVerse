"""Tests for provider_configs.py and provider_ref resolution in model_configs."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from saiverse import data_paths, model_configs, provider_configs
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
    # what load_configs() stamps for builtin_data files
    cfg["source"] = provider_configs.SOURCE_BUILTIN
    return cfg


class TestLoadProviders(unittest.TestCase):
    """Verify builtin providers load stamped with source: builtin."""

    def test_seven_builtin_providers_loaded(self):
        configs = provider_configs.load_configs()
        for pid in ("anthropic", "gemini", "openai", "ollama",
                    "nvidia_nim", "xai", "openai_codex"):
            self.assertIn(pid, configs, f"Missing builtin provider: {pid}")
            self.assertEqual(
                configs[pid].get("source"), provider_configs.SOURCE_BUILTIN,
                f"Builtin provider {pid} should be stamped source: builtin",
            )

    def test_builtin_provider_has_protocol(self):
        configs = provider_configs.load_configs()
        # Every builtin must declare a protocol
        for pid, cfg in configs.items():
            if cfg.get("source") == provider_configs.SOURCE_BUILTIN:
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
        # derived layer markers should be stripped
        self.assertNotIn("source", loaded)
        self.assertNotIn("builtin", loaded)

    def test_save_strips_derived_layer_markers(self):
        provider_configs.save_provider("foo_test", {
            "display_name": "Foo",
            "protocol": "openai_compat",
            "base_url": "http://x",
            "builtin": True,   # legacy marker, should be stripped on save
            "source": "builtin",  # should be stripped on save
        })
        target = self.tmp_path / "providers" / "foo_test.json"
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertNotIn("builtin", loaded)
        self.assertNotIn("source", loaded)

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
        # user_data files are stamped as such, never as builtin
        self.assertEqual(cfg.get("source"), provider_configs.SOURCE_USER_DATA)

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
        # user_data override is stamped user_data, not builtin
        self.assertEqual(cfg.get("source"), provider_configs.SOURCE_USER_DATA)


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

    def test_default_headers_are_inherited_by_models(self):
        """Every OpenRouter model must inherit the app-attribution headers.

        The headers live on the provider so one file covers the whole model
        catalog; a model that has to opt in individually would silently drop
        out of the ranking the day someone adds a new OpenRouter model JSON.
        """
        out = model_configs._resolve_provider_ref(
            {"model": "z-ai/glm-5", "provider_ref": "openrouter"}
        )
        self.assertEqual(
            out["default_headers"]["X-OpenRouter-Title"], "SAIVerse",
        )

    def test_default_headers_survive_a_model_that_sets_request_kwargs(self):
        """Inheritance is per-field, so request_kwargs must not shadow headers.

        Provider defaults are only inherited where the model leaves the field
        unset. Several OpenRouter models ship their own request_kwargs (GLM-5
        enables reasoning that way); putting the headers inside that same field
        would have excluded exactly those models from the attribution.
        """
        out = model_configs._resolve_provider_ref(
            {
                "model": "z-ai/glm-5",
                "provider_ref": "openrouter",
                "request_kwargs": {"extra_body": {"reasoning": {"enabled": True}}},
            }
        )
        self.assertEqual(
            out["request_kwargs"], {"extra_body": {"reasoning": {"enabled": True}}},
        )
        self.assertIn("HTTP-Referer", out["default_headers"])


class TestOpenRouterAppAttribution(unittest.TestCase):
    """What SAIVerse ships as its identity on the OpenRouter app ranking.

    These headers are how SAIVerse appears on openrouter.ai/apps. The values
    are constrained by OpenRouter's documented rules, and a silent typo means
    the app simply never shows up — the API call succeeds either way.
    """

    # https://openrouter.ai/docs/app-attribution — unrecognized categories are
    # silently ignored, so a typo here fails without any error to notice.
    RECOGNIZED_CATEGORIES = {
        "cli-agent", "ide-extension", "cloud-agent", "programming-app",
        "native-app-builder", "creative-writing", "video-gen", "image-gen",
        "audio-gen", "writing-assistant", "general-chat", "personal-agent",
        "legal", "roleplay", "game",
    }
    MAX_CATEGORIES = 2

    def setUp(self):
        self.headers = _read_builtin_provider("openrouter")["default_headers"]

    def test_referer_is_the_public_site(self):
        # HTTP-Referer is the primary identifier; without it no app page is
        # created at all, and the title alone does not produce a ranking entry.
        self.assertEqual(self.headers["HTTP-Referer"], "https://saiverse.net")

    def test_title_is_the_product_name(self):
        self.assertEqual(self.headers["X-OpenRouter-Title"], "SAIVerse")

    def test_every_shipped_openrouter_model_inherits_the_headers(self):
        """Check the real catalog, not one synthetic model.

        Inheritance is per-field, so a model that sets a field the headers
        travel with would drop out silently. Also guards request_kwargs:
        `extra_headers` there is applied per request and outranks the client's
        default headers, which would replace the attribution for that model.
        """
        from saiverse.data_paths import BUILTIN_DATA_DIR, MODELS_DIR

        provider = _read_builtin_provider("openrouter")
        checked = 0
        for path in sorted((BUILTIN_DATA_DIR / MODELS_DIR).glob("*.json")):
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if cfg.get("provider_ref") != "openrouter":
                continue
            checked += 1
            with patch.dict(
                provider_configs.PROVIDER_CONFIGS, {"openrouter": provider}
            ):
                resolved = model_configs._resolve_provider_ref(cfg)
            self.assertEqual(
                resolved.get("default_headers"),
                provider["default_headers"],
                f"{path.name} does not inherit the attribution headers",
            )
            self.assertNotIn(
                "extra_headers",
                cfg.get("request_kwargs") or {},
                f"{path.name} would override the attribution headers per request",
            )
        self.assertGreater(checked, 0, "no OpenRouter models found to check")

    def test_categories_are_recognized_and_within_the_limit(self):
        categories = self.headers["X-OpenRouter-Categories"].split(",")
        self.assertLessEqual(len(categories), self.MAX_CATEGORIES)
        for category in categories:
            # No surrounding whitespace: the value is sent as a raw header.
            self.assertEqual(category, category.strip())
            self.assertIn(category, self.RECOGNIZED_CATEGORIES)


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
        # No api_key_env at all: the local server authenticates nobody, so
        # there is no variable to name.
        self.assertIsNone(cfg.get("api_key_env"))

    def test_llama_cpp_server_needs_no_key(self):
        cfg = _read_builtin_provider("llama_cpp_server")
        self.assertIs(cfg["api_key_required"], False)

    def test_edited_copy_passes_credential_validation(self):
        """A UI edit lands in user_data; the result must still validate."""
        from saiverse.provider_security import validate_provider_config

        cfg = _read_builtin_provider("lmstudio")
        cfg["source"] = provider_configs.SOURCE_USER_DATA  # where the edit lands
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


class TestCredentialLayerBinding(unittest.TestCase):
    """Who declared a credential pairing decides whether it is allowed.

    user_data is reachable only by the person running SAIVerse (add-ons install
    into expansion_data/, and no tool writes arbitrary files), so pairings found
    there are their own decision. Definitions that arrived any other way may
    only use the provider's own namespaced variable.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp_path = Path(self.tmp_dir.name)
        self.patches = _patch_user_data(self.tmp_path)
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.expansion = self.tmp_path / "expansion"
        exp_patch = patch("saiverse.data_paths.EXPANSION_DATA_DIR", self.expansion)
        exp_patch.start()
        self.addCleanup(exp_patch.stop)
        provider_configs.reload_configs()
        self.addCleanup(provider_configs.reload_configs)

    def _write_addon_provider(self, pid: str, cfg: dict) -> None:
        target = self.expansion / "some_addon" / "providers"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{pid}.json").write_text(
            json.dumps({"id": pid, **cfg}), encoding="utf-8",
        )

    def _link_dir(self, link: Path, target: Path) -> None:
        """Link a directory, or skip if this machine allows neither form."""
        try:
            link.symlink_to(target, target_is_directory=True)
            return
        except (OSError, NotImplementedError):
            pass
        if os.name != "nt":
            self.skipTest("directory links unavailable on this machine")
        # Junctions need no privilege on Windows, unlike symlinks
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if completed.returncode != 0:
            self.skipTest(f"could not create a junction: {completed.stderr.strip()}")

    def test_layer_comes_from_the_root_that_was_walked(self):
        """Not re-derived from the path: a link must not relabel its layer.

        An add-on can ship its providers/ directory as a symlink or a Windows
        junction whose target sits under user_data. A layer decided by
        resolve() would hand that definition owner trust; a layer taken from
        the root actually walked does not.
        """
        target = self.tmp_path / "linked_elsewhere"
        target.mkdir(parents=True, exist_ok=True)
        (target / "openrouter.json").write_text(json.dumps({
            "id": "openrouter", "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "OPENROUTER_API_KEY",
        }), encoding="utf-8")

        addon = self.expansion / "some_addon"
        addon.mkdir(parents=True, exist_ok=True)
        self._link_dir(addon / "providers", target)

        provider_configs.reload_configs()
        cfg = provider_configs.get_provider("openrouter")
        # It does shadow the builtin, and it is stamped by where it was walked
        self.assertEqual(cfg["base_url"], "https://example.com/v1")
        self.assertEqual(cfg["source"], provider_configs.SOURCE_EXPANSION)

        # ...and the stamp actually denies it the shipped key
        from saiverse.provider_security import validate_provider_config
        with self.assertRaises(ValueError) as ctx:
            validate_provider_config("openrouter", cfg)
        self.assertIn("SAIVERSE_PROVIDER_OPENROUTER_API_KEY", str(ctx.exception))

    def test_untrusted_layer_must_declare_its_credential(self):
        """Silence is not neutral: clients fall back to a shipped key name."""
        from saiverse.provider_security import validate_provider_config

        # No api_key_env at all -> OpenAIClient would use OPENAI_API_KEY
        with self.assertRaises(ValueError) as ctx:
            validate_provider_config("addonproxy", {
                "protocol": "openai_compat",
                "base_url": "https://example.com/v1",
                "source": provider_configs.SOURCE_EXPANSION,
            })
        self.assertIn("api_key_required", str(ctx.exception))

        # Declaring it needs no key is fine — nothing is sent
        validate_provider_config("addonproxy", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_required": False,
            "source": provider_configs.SOURCE_EXPANSION,
        })

    def test_untrusted_empty_or_non_string_key_name_is_refused(self):
        """An empty name is the fallback state, not an absent declaration."""
        from saiverse.provider_security import validate_provider_config

        for bad in ("", "   ", 0, False, []):
            with self.subTest(api_key_env=bad):
                with self.assertRaises(ValueError):
                    validate_provider_config("addonproxy", {
                        "protocol": "openai_compat",
                        "base_url": "https://example.com/v1",
                        "api_key_env": bad,
                        "api_key_required": False,  # must not rescue it
                        "source": provider_configs.SOURCE_EXPANSION,
                    })

    def test_untrusted_provider_cannot_share_another_ids_credential(self):
        """'addon-bar' and 'addon_bar' normalize to the same variable name."""
        from saiverse.provider_security import validate_provider_config

        self._write_addon_provider("addon-bar", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDON_BAR_API_KEY",
        })
        self._write_addon_provider("addon_bar", {
            "protocol": "openai_compat",
            "base_url": "https://example.org/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDON_BAR_API_KEY",
        })
        provider_configs.reload_configs()
        for pid in ("addon-bar", "addon_bar"):
            with self.subTest(provider=pid):
                with self.assertRaises(ValueError) as ctx:
                    validate_provider_config(pid, provider_configs.get_provider(pid))
                self.assertIn("already read by", str(ctx.exception))

    def test_model_cannot_erase_its_provider_credential_name(self):
        """An empty api_key_env blocks inheritance and reopens the fallback."""
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_provider("addonproxy", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDONPROXY_API_KEY",
        })
        provider_configs.reload_configs()
        # Repeating the provider's own name is fine
        validate_model_config_connection("addon-probe", {
            "model": "x", "provider_ref": "addonproxy",
            "api_key_env": "SAIVERSE_PROVIDER_ADDONPROXY_API_KEY",
        })
        # Erasing it is not
        with self.assertRaises(ValueError) as ctx:
            validate_model_config_connection("addon-probe", {
                "model": "x", "provider_ref": "addonproxy", "api_key_env": "",
            })
        self.assertIn("provider_ref credential", str(ctx.exception))

    def test_model_cannot_cancel_an_untrusted_providers_keyless_promise(self):
        """api_key_required: false is why that provider may omit a key name."""
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_provider("addonproxy", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_required": False,
        })
        provider_configs.reload_configs()
        # Leaving the promise alone is fine
        validate_model_config_connection("addon-probe", {
            "model": "x", "provider_ref": "addonproxy",
            "base_url": "https://example.com/v1", "api_key_required": False,
        })
        # Omitting it inherits the provider's false, which is the normal form
        validate_model_config_connection("addon-probe", {
            "model": "x", "provider_ref": "addonproxy",
            "base_url": "https://example.com/v1",
        })
        # Revoking it would send the shipped key to the add-on's endpoint
        with self.assertRaises(ValueError) as ctx:
            validate_model_config_connection("addon-probe", {
                "model": "x", "provider_ref": "addonproxy",
                "base_url": "https://example.com/v1", "api_key_required": True,
            })
        self.assertIn("keyless declaration", str(ctx.exception))

    def test_model_may_omit_the_key_name_of_a_keyed_untrusted_provider(self):
        """Omitting api_key_env is the normal form: it inherits the provider's."""
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_provider("addonproxy", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDONPROXY_API_KEY",
        })
        provider_configs.reload_configs()
        # Unresolved config, as the model save route sees it
        validate_model_config_connection("addon-probe", {
            "model": "x", "provider_ref": "addonproxy",
        })  # must not raise
        # Resolved config, as the factory sees it
        validate_model_config_connection("addon-probe", {
            "model": "x", "provider_ref": "addonproxy",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDONPROXY_API_KEY",
        })  # must not raise

    def test_model_cannot_blank_out_its_provider_destination(self):
        """A falsy base_url survives inheritance and lands on the SDK default."""
        from saiverse.provider_security import validate_model_config_connection

        for bad in ("", "   ", 0, False, []):
            with self.subTest(base_url=bad):
                with self.assertRaises(ValueError) as ctx:
                    validate_model_config_connection("probe", {
                        "model": "x", "provider_ref": "openrouter", "base_url": bad,
                    })
                self.assertIn("provider_ref destination", str(ctx.exception))
        # Omitting it inherits, which is the normal form
        validate_model_config_connection(
            "probe", {"model": "x", "provider_ref": "openrouter"},
        )

    def test_keyless_promise_survives_non_boolean_revocations(self):
        """1 / "true" / [] revoke it just as effectively as a real true."""
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_provider("addonproxy", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_required": False,
        })
        provider_configs.reload_configs()
        for bad in (True, 1, "true", "false", []):
            with self.subTest(api_key_required=bad):
                with self.assertRaises(ValueError) as ctx:
                    validate_model_config_connection("addon-probe", {
                        "model": "x", "provider_ref": "addonproxy",
                        "base_url": "https://example.com/v1",
                        "api_key_required": bad,
                    })
                self.assertIn("keyless declaration", str(ctx.exception))

    def test_case_differing_variable_counts_as_shared(self):
        """Windows resolves environment variables without regard to case."""
        from saiverse.provider_security import validate_provider_config

        provider_configs.save_provider("owner_side", {
            "protocol": "openai_compat",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "saiverse_provider_addon_bar_api_key",
        })
        self._write_addon_provider("addon-bar", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDON_BAR_API_KEY",
        })
        provider_configs.reload_configs()
        with self.assertRaises(ValueError) as ctx:
            validate_provider_config(
                "addon-bar", provider_configs.get_provider("addon-bar"),
            )
        self.assertIn("already read by", str(ctx.exception))

    def test_editing_a_provider_re_resolves_the_models_on_it(self):
        """Models inline the endpoint at load, so a save must refresh them."""
        from saiverse import model_configs
        from saiverse.provider_security import validate_model_config_connection

        probe = "openrouter-deepseek-v3.2"
        self.assertIn(probe, model_configs.MODEL_CONFIGS, "fixture model missing")

        provider_configs.save_provider("openrouter", {
            "display_name": "OpenRouter (proxied)",
            "protocol": "openai_compat",
            "base_url": "https://api.openai.com/v1",  # a different destination
            "api_key_env": "OPENROUTER_API_KEY",
        })
        resolved = model_configs.MODEL_CONFIGS[probe]
        self.assertEqual(resolved.get("base_url"), "https://api.openai.com/v1")
        validate_model_config_connection(probe, resolved)  # must not raise

    def _write_addon_model(self, key: str, cfg: dict) -> None:
        target = self.expansion / "some_addon" / "models"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{key}.json").write_text(json.dumps(cfg), encoding="utf-8")

    def test_addon_model_naming_its_own_destination_must_declare_a_credential(self):
        """The direct path is subject to the same rule as a provider."""
        from saiverse import model_configs
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_model("addon_direct", {
            "model": "x", "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
        })
        model_configs.reload_configs()
        cfg = model_configs.MODEL_CONFIGS["addon_direct"]
        self.assertEqual(cfg["source"], provider_configs.SOURCE_EXPANSION)
        with self.assertRaises(ValueError) as ctx:
            validate_model_config_connection("addon_direct", cfg)
        self.assertIn("SAIVERSE_MODEL_ADDON_DIRECT_API_KEY", str(ctx.exception))

    def test_addon_model_may_declare_keyless_or_name_its_own_variable(self):
        from saiverse import model_configs
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_model("addon_keyless", {
            "model": "x", "protocol": "openai_compat",
            "base_url": "https://example.com/v1", "api_key_required": False,
        })
        self._write_addon_model("addon_named", {
            "model": "x", "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_MODEL_ADDON_NAMED_API_KEY",
        })
        model_configs.reload_configs()
        for key in ("addon_keyless", "addon_named"):
            with self.subTest(model=key):
                validate_model_config_connection(
                    key, model_configs.MODEL_CONFIGS[key],
                )  # must not raise

    def test_addon_model_cannot_fake_keyless_with_a_non_boolean(self):
        from saiverse import model_configs
        from saiverse.provider_security import validate_model_config_connection

        for bad in ("false", 0, [], None):
            with self.subTest(api_key_required=bad):
                self._write_addon_model("addon_fake", {
                    "model": "x", "protocol": "openai_compat",
                    "base_url": "https://example.com/v1",
                    "api_key_required": bad,
                })
                model_configs.reload_configs()
                with self.assertRaises(ValueError):
                    validate_model_config_connection(
                        "addon_fake", model_configs.MODEL_CONFIGS["addon_fake"],
                    )

    def test_owner_model_may_leave_its_credential_unnamed(self):
        """user_data is the owner's own territory here too."""
        from saiverse import model_configs
        from saiverse.provider_security import validate_model_config_connection

        models_dir = self.tmp_path / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / "my_local.json").write_text(json.dumps({
            "model": "x", "protocol": "openai_compat",
            "base_url": "http://127.0.0.1:1234/v1",
        }), encoding="utf-8")
        model_configs.reload_configs()
        cfg = model_configs.MODEL_CONFIGS["my_local"]
        self.assertEqual(cfg["source"], provider_configs.SOURCE_USER_DATA)
        validate_model_config_connection("my_local", cfg)  # must not raise

    def test_saving_an_owner_model_is_judged_as_the_layer_it_lands_in(self):
        """The save route has no stamp yet; it must supply its destination."""
        from api.routes import config as config_route

        payload = config_route._strip_runtime_fields({
            "model": "x", "protocol": "openai_compat",
            "base_url": "http://127.0.0.1:1234/v1",
            "source": "builtin",  # must never reach the file
        })
        self.assertNotIn("source", payload)
        self.assertNotIn("builtin", payload)
        # Must not raise: a hand-written local model may omit the key name
        config_route._validate_model_connection("my_local", payload)

    def test_addon_model_cannot_read_a_variable_another_definition_uses(self):
        """Model keys collapse the same way provider ids do: foo-bar / foo_bar."""
        from saiverse import model_configs
        from saiverse.provider_security import validate_model_config_connection

        owner_models = self.tmp_path / "models"
        owner_models.mkdir(parents=True, exist_ok=True)
        (owner_models / "foo_bar.json").write_text(json.dumps({
            "model": "x", "protocol": "openai_compat",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key_env": "SAIVERSE_MODEL_FOO_BAR_API_KEY",
        }), encoding="utf-8")
        self._write_addon_model("foo-bar", {
            "model": "x", "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_MODEL_FOO_BAR_API_KEY",
        })
        model_configs.reload_configs()
        cfg = model_configs.MODEL_CONFIGS["foo-bar"]
        self.assertEqual(cfg["source"], provider_configs.SOURCE_EXPANSION)
        with self.assertRaises(ValueError) as ctx:
            validate_model_config_connection("foo-bar", cfg)
        self.assertIn("already read by", str(ctx.exception))

    def test_model_cannot_claim_its_own_layer(self):
        from saiverse import model_configs

        self._write_addon_model("addon_liar", {
            "model": "x", "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "source": "user_data",  # must be discarded
        })
        model_configs.reload_configs()
        self.assertEqual(
            model_configs.MODEL_CONFIGS["addon_liar"]["source"],
            provider_configs.SOURCE_EXPANSION,
        )

    def test_direct_model_rejects_present_but_empty_key_name(self):
        """Written-but-blank lands in the same fallback as writing nothing."""
        from saiverse.provider_security import validate_model_config_connection

        for bad in ("", "   ", 0, False, []):
            with self.subTest(api_key_env=bad):
                with self.assertRaises(ValueError) as ctx:
                    validate_model_config_connection("legacy-probe", {
                        "model": "x",
                        "base_url": "https://example.com/v1",
                        "api_key_env": bad,
                    })
                self.assertIn("non-empty string", str(ctx.exception))

    def test_untrusted_provider_cannot_read_a_variable_another_provider_uses(self):
        """The grant is only safe while nobody else reads the same variable."""
        from saiverse.provider_security import validate_provider_config

        # The owner's own provider happens to read the add-on's namespaced name
        provider_configs.save_provider("owner_side", {
            "protocol": "openai_compat",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDON_BAR_API_KEY",
        })
        self._write_addon_provider("addon-bar", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDON_BAR_API_KEY",
        })
        provider_configs.reload_configs()
        with self.assertRaises(ValueError) as ctx:
            validate_provider_config(
                "addon-bar", provider_configs.get_provider("addon-bar"),
            )
        self.assertIn("already read by", str(ctx.exception))

    def test_lookalike_id_that_reads_another_variable_is_not_refused(self):
        """Only an actual shared variable disqualifies, not a lookalike id."""
        from saiverse.provider_security import validate_provider_config

        provider_configs.save_provider("addon_bar", {
            "protocol": "openai_compat",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "SOMETHING_ELSE_API_KEY",
        })
        self._write_addon_provider("addon-bar", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_ADDON_BAR_API_KEY",
        })
        provider_configs.reload_configs()
        validate_provider_config(
            "addon-bar", provider_configs.get_provider("addon-bar"),
        )  # must not raise

    def test_untrusted_provider_without_key_is_refused_at_call_time(self):
        """End to end: the fallback must not reach a model build."""
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_provider("addonproxy", {
            "display_name": "Addon Proxy",
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
        })
        provider_configs.reload_configs()
        with self.assertRaises(ValueError):
            validate_model_config_connection(
                "addon-probe", {"model": "x", "provider_ref": "addonproxy"},
            )

    def test_owner_may_override_a_builtin_and_keep_its_shipped_key(self):
        """The reported bug: saving an unchanged builtin returned 400."""
        from saiverse.provider_security import validate_provider_config

        for pid in ("openai", "gemini", "anthropic", "openrouter",
                    "nvidia_nim", "plamo", "sakana", "xai"):
            cfg = dict(_read_builtin_provider(pid))
            cfg["source"] = provider_configs.SOURCE_USER_DATA
            validate_provider_config(pid, cfg)  # must not raise

    def test_owner_may_point_a_shipped_key_somewhere_else(self):
        """user_data is the owner's own territory; their file, their choice."""
        from saiverse.provider_security import validate_provider_config

        validate_provider_config("openrouter", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "source": provider_configs.SOURCE_USER_DATA,
        })

    def test_addon_may_not_borrow_a_shipped_key(self):
        from saiverse.provider_security import validate_provider_config

        with self.assertRaises(ValueError) as ctx:
            validate_provider_config("openrouter", {
                "protocol": "openai_compat",
                "base_url": "https://example.com/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "source": provider_configs.SOURCE_EXPANSION,
            })
        self.assertIn("SAIVERSE_PROVIDER_OPENROUTER_API_KEY", str(ctx.exception))

    def test_addon_may_use_its_own_namespaced_variable(self):
        from saiverse.provider_security import validate_provider_config

        validate_provider_config("myaddon", {
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "SAIVERSE_PROVIDER_MYADDON_API_KEY",
            "source": provider_configs.SOURCE_EXPANSION,
        })

    def test_unstamped_config_is_treated_as_untrusted(self):
        """Fail closed: a config with no layer must not be taken on faith."""
        from saiverse.provider_security import validate_provider_config

        with self.assertRaises(ValueError):
            validate_provider_config("openrouter", {
                "base_url": "https://example.com/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            })

    def test_addon_cannot_forge_its_layer(self):
        """A file declaring itself builtin must not be believed."""
        self._write_addon_provider("openrouter", {
            "display_name": "OpenRouter",
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "builtin": True,
            "source": "builtin",
        })
        configs = provider_configs.load_configs()
        cfg = configs["openrouter"]
        # The addon file does shadow the builtin (3-layer priority)...
        self.assertEqual(cfg["base_url"], "https://example.com/v1")
        # ...but it cannot claim to be one.
        self.assertEqual(cfg["source"], provider_configs.SOURCE_EXPANSION)
        self.assertNotIn("builtin", cfg)

    def test_forged_layer_is_rejected_at_call_time(self):
        """The forgery must not survive into the path that builds a client."""
        from saiverse.provider_security import validate_model_config_connection

        self._write_addon_provider("openrouter", {
            "display_name": "OpenRouter",
            "protocol": "openai_compat",
            "base_url": "https://example.com/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "builtin": True,
        })
        provider_configs.reload_configs()
        with self.assertRaises(ValueError):
            validate_model_config_connection(
                "openrouter-probe", {"model": "x", "provider_ref": "openrouter"},
            )

    def test_ui_can_save_every_builtin_unchanged(self):
        """The reported symptom: opening a builtin and pressing 保存 returned 400.

        Sends exactly what ProviderEditorModal.handleSave builds after the form
        was populated from the provider, i.e. an edit that changes nothing.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes import providers as prov

        app = FastAPI()
        app.include_router(prov.router, prefix="/api/providers")
        client = TestClient(app)

        failures = []
        for info in client.get("/api/providers").json():
            resp = client.put(f"/api/providers/{info['id']}", json={
                "display_name": info["display_name"],
                "protocol": info["protocol"],
                "base_url": info.get("base_url") or None,
                "api_key_env": info.get("api_key_env") or None,
                "api_key_required": info.get("api_key_required") is not False,
            })
            if resp.status_code != 200:
                failures.append((info["id"], resp.status_code, resp.text[:120]))
        self.assertEqual(failures, [], f"builtin providers rejected on save: {failures}")

    def test_documented_custom_provider_walkthrough_is_accepted(self):
        """docs/custom_providers.md walks the user through adding Kimi.

        Its example names the key KIMI_API_KEY. Under the previous rule that
        was rejected (a non-builtin had to use SAIVERSE_PROVIDER_KIMI_API_KEY),
        so the documented happy path for the feature could not be followed.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes import providers as prov

        app = FastAPI()
        app.include_router(prov.router, prefix="/api/providers")
        client = TestClient(app)
        resp = client.post("/api/providers", json={
            "id": "kimi",
            "display_name": "Kimi (Moonshot AI)",
            "protocol": "openai_compat",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key_env": "KIMI_API_KEY",
        })
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_inline_test_of_an_unknown_provider_cannot_name_any_variable(self):
        """This route sends the named secret to the requested host.

        Saving is the owner's call, but the probe actually transmits, and a
        loopback start has no owner authentication in front of it — so an id
        that is not saved yet may only be probed with its own namespaced
        variable. Validation happens before any request is made.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes import providers as prov

        app = FastAPI()
        app.include_router(prov.router, prefix="/api/providers")
        client = TestClient(app)

        body = client.post("/api/providers/test", json={
            "protocol": "openai_compat",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key_env": "OPENAI_API_KEY",
            "provider_id": "not_saved_yet",
        }).json()
        self.assertFalse(body["success"])
        self.assertIn("SAIVERSE_PROVIDER_NOT_SAVED_YET_API_KEY", body["error"])

    def test_saved_override_does_not_persist_the_layer_marker(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes import providers as prov

        app = FastAPI()
        app.include_router(prov.router, prefix="/api/providers")
        client = TestClient(app)
        info = client.get("/api/providers/openrouter").json()
        client.put("/api/providers/openrouter", json={
            "display_name": info["display_name"],
            "protocol": info["protocol"],
            "base_url": info["base_url"],
            "api_key_env": info["api_key_env"],
        })
        written = json.loads(
            (self.tmp_path / "providers" / "openrouter.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("source", written)
        self.assertNotIn("builtin", written)

    def test_owner_override_still_serves_its_models(self):
        """Overriding a builtin must not break the models that reference it."""
        from saiverse.provider_security import validate_model_config_connection

        provider_configs.save_provider("openrouter", {
            "display_name": "OpenRouter",
            "protocol": "openai_compat",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
        })
        self.assertEqual(
            provider_configs.get_provider("openrouter")["source"],
            provider_configs.SOURCE_USER_DATA,
        )
        validate_model_config_connection(
            "openrouter-probe", {"model": "x", "provider_ref": "openrouter"},
        )  # must not raise


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
