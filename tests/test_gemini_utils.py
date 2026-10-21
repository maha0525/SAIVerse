from types import SimpleNamespace

import pytest

import llm_clients.gemini_utils as gemini_utils


def _make_dummy_genai(created_clients):
    def client_factory(*, api_key=None, http_options=None):
        client = SimpleNamespace(api_key=api_key)
        created_clients.append(client)
        return client

    # Provide types.HttpOptions and types.HttpRetryOptions used by _http_options()
    types_ns = SimpleNamespace(
        HttpOptions=lambda **kw: SimpleNamespace(**kw),
        HttpRetryOptions=lambda **kw: SimpleNamespace(**kw),
    )

    return SimpleNamespace(Client=client_factory, types=types_ns)


def test_build_gemini_clients_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("GEMINI_FREE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    free_client, paid_client, active_client = gemini_utils.build_gemini_clients()
    assert free_client is None
    assert paid_client is None
    assert active_client is None


def test_build_gemini_clients_free_only(monkeypatch):
    created = []
    dummy_genai = _make_dummy_genai(created)
    monkeypatch.setattr(gemini_utils, "_get_genai_module", lambda: dummy_genai)
    monkeypatch.setenv("GEMINI_FREE_API_KEY", "free-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    free_client, paid_client, active_client = gemini_utils.build_gemini_clients()

    assert free_client is not None
    assert getattr(free_client, "api_key", None) == "free-key"
    assert paid_client is None
    assert active_client is free_client
    assert [client.api_key for client in created] == ["free-key"]


def test_build_gemini_clients_prefer_paid(monkeypatch):
    created = []
    dummy_genai = _make_dummy_genai(created)
    monkeypatch.setattr(gemini_utils, "_get_genai_module", lambda: dummy_genai)
    monkeypatch.setenv("GEMINI_FREE_API_KEY", "free-key")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-key")

    free_client, paid_client, active_client = gemini_utils.build_gemini_clients(prefer_paid=True)

    assert getattr(free_client, "api_key", None) == "free-key"
    assert getattr(paid_client, "api_key", None) == "paid-key"
    assert active_client is paid_client
    assert [client.api_key for client in created] == ["free-key", "paid-key"]


def _capture_http_options(monkeypatch):
    """作られた SDK クライアントに渡った HttpOptions を記録する。"""
    captured = []

    def client_factory(*, api_key=None, http_options=None):
        captured.append(http_options)
        return SimpleNamespace(api_key=api_key)

    types_ns = SimpleNamespace(
        HttpOptions=lambda **kw: SimpleNamespace(**kw),
        HttpRetryOptions=lambda **kw: SimpleNamespace(**kw),
    )
    dummy_genai = SimpleNamespace(Client=client_factory, types=types_ns)
    monkeypatch.setattr(gemini_utils, "_get_genai_module", lambda: dummy_genai)
    monkeypatch.delenv("GEMINI_FREE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "paid-key")
    return captured


def test_build_gemini_clients_timeout_defaults_to_env(monkeypatch):
    captured = _capture_http_options(monkeypatch)
    monkeypatch.delenv("GEMINI_TIMEOUT_SECONDS", raising=False)

    gemini_utils.build_gemini_clients(prefer_paid=True)

    # HttpOptions.timeout はミリ秒。既定は 300 秒。
    assert [opts.timeout for opts in captured] == [300_000]


def test_build_gemini_clients_timeout_reads_env(monkeypatch):
    captured = _capture_http_options(monkeypatch)
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "120")

    gemini_utils.build_gemini_clients(prefer_paid=True)

    assert [opts.timeout for opts in captured] == [120_000]


def test_build_gemini_clients_model_timeout_overrides_env(monkeypatch):
    captured = _capture_http_options(monkeypatch)
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "120")

    gemini_utils.build_gemini_clients(prefer_paid=True, timeout_seconds=900)

    assert [opts.timeout for opts in captured] == [900_000]


def test_build_gemini_clients_env_zero_means_no_timeout(monkeypatch):
    captured = _capture_http_options(monkeypatch)
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "0")

    gemini_utils.build_gemini_clients(prefer_paid=True)

    assert [hasattr(opts, "timeout") for opts in captured] == [False]
