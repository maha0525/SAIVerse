"""How Ollama decides which endpoint to talk to.

Ollama is the one provider whose address is discovered rather than fixed, so
the rules matter: a configured address must be honoured exactly (it is an
instruction), while an unconfigured one may be searched for on localhost.

Configuration reaches the client from three places, all of which end up as the
``base_url`` argument or an env var:
  - the ollama provider JSON (editable from the UI, which writes a user_data
    override) -> inherited by models via provider_ref
  - a model JSON's own base_url
  - OLLAMA_BASE_URL / OLLAMA_HOST
"""
import os
import unittest
from unittest.mock import patch

from llm_clients.ollama import OllamaClient, _normalize_ollama_url

DISCOVERY_ADDRESSES = {
    "http://127.0.0.1:11434",
    "http://localhost:11434",
    "http://host.docker.internal:11434",
    "http://172.17.0.1:11434",
}


class _Probe:
    """Records probed URLs; treats the given set as reachable."""

    def __init__(self, reachable=()):
        self.reachable = set(reachable)
        self.tried = []

    def __call__(self, url, timeout=None):
        base = url.rsplit("/", 2)[0] if url.endswith("/v1/models") else url
        for suffix in ("/v1/models", "/api/version"):
            if url.endswith(suffix):
                base = url[: -len(suffix)]
                break
        if base not in self.tried:
            self.tried.append(base)

        class _Resp:
            ok = base in self.reachable

        return _Resp()


class OllamaEndpointResolutionTests(unittest.TestCase):
    def setUp(self):
        OllamaClient.reset_probe_cache()
        self._env = patch.dict(
            os.environ,
            {k: v for k, v in os.environ.items()
             if k not in ("OLLAMA_BASE_URL", "OLLAMA_HOST")},
            clear=True,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        OllamaClient.reset_probe_cache()

    def _build(self, **kwargs):
        return OllamaClient("test-model", 1000, **kwargs)

    # --- nothing configured: discovery is allowed -------------------------

    def test_discovery_searches_local_candidates(self):
        probe = _Probe(reachable={"http://localhost:11434"})
        with patch("llm_clients.ollama.requests.get", probe):
            client = self._build()
        self.assertEqual(client.base, "http://localhost:11434")
        self.assertTrue(DISCOVERY_ADDRESSES.issuperset(probe.tried))
        self.assertIn("http://127.0.0.1:11434", probe.tried)

    def test_discovery_result_is_cached_across_instances(self):
        probe = _Probe(reachable={"http://127.0.0.1:11434"})
        with patch("llm_clients.ollama.requests.get", probe):
            self._build()
            first_round = list(probe.tried)
            self._build()
            self.assertEqual(probe.tried, first_round)

    # --- configured address: honoured exactly ----------------------------

    def test_configured_base_url_is_not_searched_beyond(self):
        probe = _Probe(reachable={"http://192.168.1.50:11434"})
        with patch("llm_clients.ollama.requests.get", probe):
            client = self._build(base_url="http://192.168.1.50:11434")
        self.assertEqual(client.base, "http://192.168.1.50:11434")
        self.assertEqual(probe.tried, ["http://192.168.1.50:11434"])

    def test_unreachable_configured_base_url_is_kept_not_localhost(self):
        """The whole point: a typo'd or down host must stay visible."""
        probe = _Probe(reachable={"http://127.0.0.1:11434"})
        with patch("llm_clients.ollama.requests.get", probe):
            client = self._build(base_url="http://192.168.1.50:11434")
        self.assertEqual(client.base, "http://192.168.1.50:11434")
        self.assertNotIn("http://127.0.0.1:11434", probe.tried)

    def test_env_var_is_treated_as_configured(self):
        probe = _Probe(reachable=set())
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://box.local:11434"}):
            with patch("llm_clients.ollama.requests.get", probe):
                client = self._build()
        self.assertEqual(client.base, "http://box.local:11434")
        self.assertEqual(probe.tried, ["http://box.local:11434"])

    def test_base_url_argument_wins_over_env_var(self):
        probe = _Probe(reachable={"http://from-config:11434"})
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://from-env:11434"}):
            with patch("llm_clients.ollama.requests.get", probe):
                client = self._build(base_url="http://from-config:11434")
        self.assertEqual(client.base, "http://from-config:11434")

    def test_configured_address_does_not_populate_shared_cache(self):
        probe = _Probe(reachable={"http://192.168.1.50:11434", "http://127.0.0.1:11434"})
        with patch("llm_clients.ollama.requests.get", probe):
            self._build(base_url="http://192.168.1.50:11434")
            fallback_client = self._build()
        self.assertEqual(fallback_client.base, "http://127.0.0.1:11434")

    def test_comma_separated_list_tries_each_in_order(self):
        probe = _Probe(reachable={"http://second:11434"})
        with patch("llm_clients.ollama.requests.get", probe):
            client = self._build(base_url="http://first:11434,http://second:11434")
        self.assertEqual(client.base, "http://second:11434")
        self.assertEqual(probe.tried, ["http://first:11434", "http://second:11434"])

    # --- address normalization -------------------------------------------

    def test_missing_scheme_is_added(self):
        self.assertEqual(_normalize_ollama_url("box:11434"), "http://box:11434")

    def test_wildcard_listen_address_becomes_loopback(self):
        self.assertEqual(
            _normalize_ollama_url("http://0.0.0.0:11434"), "http://127.0.0.1:11434",
        )

    def test_https_scheme_is_preserved(self):
        self.assertEqual(
            _normalize_ollama_url("https://ollama.example:443"),
            "https://ollama.example:443",
        )


class OllamaProviderWiringTests(unittest.TestCase):
    """The provider JSON must be the thing models read their address from."""

    def test_builtin_provider_leaves_address_unset(self):
        """Empty base_url is what keeps discovery and env vars working.

        Pinning 127.0.0.1 here would make every ollama model 'configured',
        silently disabling OLLAMA_BASE_URL.
        """
        from saiverse.provider_configs import get_provider

        provider = get_provider("ollama")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.get("protocol"), "ollama_compat")
        self.assertFalse(provider.get("base_url"))

    def test_builtin_ollama_models_reference_the_provider(self):
        """Without provider_ref, editing the provider in the UI has no effect."""
        import json
        from saiverse.data_paths import BUILTIN_DATA_DIR, MODELS_DIR

        checked = 0
        for path in sorted((BUILTIN_DATA_DIR / MODELS_DIR).glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("provider") != "ollama" and raw.get("provider_ref") != "ollama":
                continue
            checked += 1
            self.assertEqual(
                raw.get("provider_ref"), "ollama",
                f"{path.stem} still uses the legacy provider field",
            )
        self.assertGreater(checked, 0)

    def test_provider_base_url_reaches_the_model(self):
        """A UI edit writes base_url on the provider; models must inherit it."""
        from saiverse import model_configs

        with patch("saiverse.provider_configs.get_provider", return_value={
            "id": "ollama",
            "protocol": "ollama_compat",
            "base_url": "http://192.168.1.50:11434",
        }):
            resolved = model_configs._resolve_provider_ref({
                "model": "probe", "provider_ref": "ollama",
            })
        self.assertEqual(resolved.get("base_url"), "http://192.168.1.50:11434")
        self.assertEqual(resolved.get("provider"), "ollama")


if __name__ == "__main__":
    unittest.main()
