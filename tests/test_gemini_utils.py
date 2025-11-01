import types
from types import SimpleNamespace

import pytest

import llm_clients.gemini_utils as gemini_utils


def _make_dummy_genai(created_clients):
    def client_factory(*, api_key=None):
        client = SimpleNamespace(api_key=api_key)
        created_clients.append(client)
        return client

    return SimpleNamespace(Client=client_factory)


def test_build_gemini_clients_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_FREE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        gemini_utils.build_gemini_clients()


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

