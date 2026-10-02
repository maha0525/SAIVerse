"""The provider API reports every environment variable a provider's key can live in.

Gemini reads its key from two variables: ``GEMINI_API_KEY`` (paid tier, the
primary ``api_key_env``) and ``GEMINI_FREE_API_KEY`` (free tier, declared in
``api_key_env_alternates``). The settings screen builds one key input per
variable from ``api_key_envs``, so a variable missing from that list is a key
the user cannot enter from the screen at all.

Environment variables are only ever set to dummy values here; real keys are
neither read nor printed.
"""
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import providers as prov
from saiverse import model_configs, provider_configs

# Every key variable the shipped providers under test can read.
KEY_VARS = ("GEMINI_API_KEY", "GEMINI_FREE_API_KEY", "NVIDIA_API_KEY")
DUMMY = "dummy-value-for-tests"


@pytest.fixture
def user_data(tmp_path, monkeypatch):
    """Only the shipped (builtin) layer is visible, loaded by the real loader.

    user_data and expansion_data are pointed at empty directories so that an
    override on the machine running the tests cannot stand in for the shipped
    definition. Yields the (empty) user_data directory.
    """
    user_dir = tmp_path / "user_data"
    expansion_dir = tmp_path / "expansion_data"
    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    with patch("saiverse.data_paths.USER_DATA_DIR", user_dir), \
            patch("saiverse.provider_configs.USER_DATA_DIR", user_dir), \
            patch("saiverse.data_paths.EXPANSION_DATA_DIR", expansion_dir):
        provider_configs.reload_providers_and_models()
        yield user_dir
    # Back on the real directories: put the module-level caches back.
    provider_configs.reload_providers_and_models()


@pytest.fixture
def client(user_data):
    app = FastAPI()
    app.include_router(prov.router, prefix="/api/providers")
    return TestClient(app)


def _names(info: dict) -> list[str]:
    return [env["name"] for env in info["api_key_envs"]]


def _flags(info: dict) -> list[bool]:
    return [env["configured"] for env in info["api_key_envs"]]


def test_loader_keeps_the_alternates_of_the_shipped_gemini_definition(user_data):
    cfg = provider_configs.get_provider("gemini")
    assert cfg["source"] == provider_configs.SOURCE_BUILTIN
    assert cfg["api_key_env"] == "GEMINI_API_KEY"
    assert cfg["api_key_env_alternates"] == ["GEMINI_FREE_API_KEY"]


def test_gemini_lists_the_paid_variable_first_then_the_free_one(client):
    info = client.get("/api/providers/gemini").json()
    assert _names(info) == ["GEMINI_API_KEY", "GEMINI_FREE_API_KEY"]
    # The primary name is still reported on its own, for older screens.
    assert info["api_key_env"] == "GEMINI_API_KEY"


def test_only_the_free_key_set_counts_as_configured(client, monkeypatch):
    monkeypatch.setenv("GEMINI_FREE_API_KEY", DUMMY)
    info = client.get("/api/providers/gemini").json()
    assert _flags(info) == [False, True]
    assert info["api_key_configured"] is True


def test_only_the_paid_key_set_counts_as_configured(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", DUMMY)
    info = client.get("/api/providers/gemini").json()
    assert _flags(info) == [True, False]
    assert info["api_key_configured"] is True


def test_neither_key_set_is_not_configured(client):
    info = client.get("/api/providers/gemini").json()
    assert _flags(info) == [False, False]
    assert info["api_key_configured"] is False


def test_empty_value_does_not_count_as_set(client, monkeypatch):
    monkeypatch.setenv("GEMINI_FREE_API_KEY", "")
    info = client.get("/api/providers/gemini").json()
    assert _flags(info) == [False, False]
    assert info["api_key_configured"] is False


def test_provider_with_one_key_variable_lists_one(client, monkeypatch):
    info = client.get("/api/providers/nvidia_nim").json()
    assert info["api_key_envs"] == [{"name": "NVIDIA_API_KEY", "configured": False}]
    assert info["api_key_configured"] is False

    monkeypatch.setenv("NVIDIA_API_KEY", DUMMY)
    info = client.get("/api/providers/nvidia_nim").json()
    assert info["api_key_envs"] == [{"name": "NVIDIA_API_KEY", "configured": True}]
    assert info["api_key_configured"] is True


def test_provider_without_a_key_variable_lists_none(client):
    info = client.get("/api/providers/ollama").json()
    assert info["api_key_envs"] == []
    assert info["api_key_configured"] is None


@pytest.mark.parametrize(
    "alternates, expected",
    [
        # not a list: ignored as a whole
        ("ALT_ONE_KEY", ["PRIMARY_KEY"]),
        ({"ALT_ONE_KEY": True}, ["PRIMARY_KEY"]),
        (None, ["PRIMARY_KEY"]),
        # non-string and empty entries are dropped; so are repeats, including
        # a repeat of the primary name
        (
            ["ALT_ONE_KEY", "", None, 5, ["x"], "PRIMARY_KEY", "ALT_TWO_KEY", "ALT_ONE_KEY"],
            ["PRIMARY_KEY", "ALT_ONE_KEY", "ALT_TWO_KEY"],
        ),
        ([], ["PRIMARY_KEY"]),
    ],
)
def test_malformed_alternates_are_ignored(alternates, expected, monkeypatch):
    for name in ("PRIMARY_KEY", "ALT_ONE_KEY", "ALT_TWO_KEY"):
        monkeypatch.delenv(name, raising=False)
    cfg = {
        "protocol": "openai_compat",
        "api_key_env": "PRIMARY_KEY",
        "api_key_env_alternates": alternates,
    }
    info = prov._to_provider_info("probe", cfg)
    assert [env.name for env in info.api_key_envs] == expected

    # Same rule as the one that decides whether a model is usable: a variable
    # the availability check accepts must be one the screen can fill in, and
    # the other way round.
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {"probe": cfg})
    assert model_configs._get_required_env_vars("probe") == expected


def test_alternates_without_a_primary_variable_list_nothing():
    info = prov._to_provider_info(
        "probe",
        {"protocol": "openai_compat", "api_key_env_alternates": ["ALT_ONE_KEY"]},
    )
    assert info.api_key_envs == []
    assert info.api_key_configured is None


def test_every_route_that_returns_a_provider_reports_the_same_variables(
    client, user_data, monkeypatch, mock_provider_network,
):
    """List, single get, update, reload and create all build the info one way."""
    mock_provider_network("example.com")
    monkeypatch.setenv("GEMINI_FREE_API_KEY", DUMMY)
    expected = [
        {"name": "GEMINI_API_KEY", "configured": False},
        {"name": "GEMINI_FREE_API_KEY", "configured": True},
    ]

    def from_list(body: list) -> dict:
        return next(item for item in body if item["id"] == "gemini")

    listed = from_list(client.get("/api/providers").json())
    single = client.get("/api/providers/gemini").json()
    assert listed["api_key_envs"] == expected
    assert single["api_key_envs"] == expected
    assert listed["api_key_configured"] is True
    assert single["api_key_configured"] is True

    # What ProviderEditorModal.handleSave sends for an edit that changes
    # nothing: it creates a user_data override of the shipped definition.
    resp = client.put("/api/providers/gemini", json={
        "display_name": single["display_name"],
        "protocol": single["protocol"],
        "base_url": single.get("base_url") or None,
        "api_key_env": single.get("api_key_env") or None,
        "api_key_required": single.get("api_key_required") is not False,
    })
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["builtin"] is False
    assert updated["api_key_envs"] == expected
    assert updated["api_key_configured"] is True

    # The override keeps the alternates, so the free-tier input does not
    # disappear once the shipped definition has been edited from the screen.
    written = json.loads(
        (user_data / "providers" / "gemini.json").read_text(encoding="utf-8")
    )
    assert written["api_key_env_alternates"] == ["GEMINI_FREE_API_KEY"]
    assert client.get("/api/providers/gemini").json()["api_key_envs"] == expected
    assert from_list(client.get("/api/providers").json())["api_key_envs"] == expected

    reloaded = from_list(client.post("/api/providers/reload").json())
    assert reloaded["api_key_envs"] == expected

    # Create: a new provider names a single variable.
    monkeypatch.delenv("KEY_ENVS_PROBE_API_KEY", raising=False)
    resp = client.post("/api/providers", json={
        "id": "key_envs_probe",
        "display_name": "Key envs probe",
        "protocol": "openai_compat",
        "base_url": "https://example.com/v1",
        "api_key_env": "KEY_ENVS_PROBE_API_KEY",
    })
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["api_key_envs"] == [
        {"name": "KEY_ENVS_PROBE_API_KEY", "configured": False},
    ]
    assert created["api_key_configured"] is False
    assert (
        client.get("/api/providers/key_envs_probe").json()["api_key_envs"]
        == created["api_key_envs"]
    )
