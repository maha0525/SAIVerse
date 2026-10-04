"""Invalid provider overrides stay visible and must never choose a lower connection.

All files and active registries are isolated; no persona, LLM, or network runs.
"""
import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import providers
from saiverse import data_paths, model_configs, provider_configs

SHIPPED_DATA = data_paths.BUILTIN_DATA_DIR
VALID = {
    "id": "example",
    "display_name": "Example",
    "protocol": "openai_compat",
    "base_url": "https://example.invalid/v1",
    "api_key_required": False,
}


@pytest.fixture
def layers(tmp_path, monkeypatch):
    roots = {
        "user_data": tmp_path / "user_data",
        "expansion": tmp_path / "expansion_data" / "a_addon",
        "builtin": tmp_path / "builtin_data",
    }
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", roots["user_data"])
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", roots["expansion"].parent)
    monkeypatch.setattr(data_paths, "BUILTIN_DATA_DIR", roots["builtin"])
    # Registry changes use monkeypatch, not reload_configs: a test must never
    # re-decide a live persona as a side effect of checking file resolution.
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", {})
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {})
    monkeypatch.setattr(model_configs, "LEGACY_MODELS_DIR", tmp_path / "no_legacy_models")
    return roots


def write_config(root, filename, config, *, subdir="providers"):
    target = root / subdir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config), encoding="utf-8")
    return target


def load_registries(monkeypatch):
    configs = provider_configs.load_configs()
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", configs)
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", model_configs.load_configs())
    return configs


@pytest.fixture
def client():
    from api.routes import config, info

    app = FastAPI()
    app.include_router(providers.router, prefix="/api/providers")
    app.include_router(info.router, prefix="/api")
    app.include_router(config.router, prefix="/api/config")
    with TestClient(app) as client:
        yield client


def test_candidate_iterator_preserves_layers_and_ordinary_shadowing(layers):
    paths = [write_config(root, "example.json", VALID) for root in layers.values()]
    # The extra addon follows a_addon regardless of filesystem creation order.
    other = layers["expansion"].parent / "z_addon"
    extra = write_config(other, "example.json", VALID)
    write_config(layers["expansion"].parent / ".hidden", "hidden.json", VALID)
    unique = write_config(layers["builtin"], "unique.json", VALID)
    candidates = list(data_paths.iter_file_candidates_with_layer("providers", "*.json"))
    assert candidates[:3] == [
        (paths[0], "user_data"), (paths[1], "expansion"), (extra, "expansion"),
    ]
    assert set(candidates[3:]) == {(paths[2], "builtin"), (unique, "builtin")}
    assert list(data_paths.iter_files_with_layer("providers", "*.json")) == [
        (paths[0], "user_data"), (unique, "builtin"),
    ]
    assert list(data_paths.iter_files("providers", "*.json")) == [paths[0], unique]


@pytest.mark.parametrize("layer", ["user_data", "builtin"])
def test_ordinary_iterator_keeps_recursive_same_root_behavior(layers, layer):
    first = write_config(layers[layer], "first/example.json", VALID)
    second = write_config(layers[layer], "second/example.json", VALID)
    assert set(data_paths.iter_files_with_layer("providers", "**/*.json")) == {
        (first, layer), (second, layer),
    }


@pytest.mark.parametrize("filename", ["example.json", "renamed.json"])
@pytest.mark.parametrize("field,value", [
    ("display_name", None), ("display_name", 5), ("display_name", []),
    ("protocol", None), ("protocol", False), ("protocol", {}),
    ("protocol", "DO_NOT_LOG"), ("protocol", ""),
    ("base_url", 0), ("base_url", []),
    ("api_key_env", 0), ("api_key_env", ["DO_NOT_LOG"]),
    ("api_key_required", "false"), ("api_key_required", 0),
    ("default_request_kwargs", []), ("default_convert_system_to_user", "false"),
    ("default_supports_images", "true"),
    ("default_max_image_bytes", "1024"), ("default_max_image_bytes", True),
    ("api_key_env_alternates", {}), ("api_key_env_alternates", None),
    ("api_key_env_alternates", "OTHER_KEY"), ("api_key_env_alternates", [None]),
    ("api_key_env_alternates", [3]), ("api_key_env_alternates", [""]),
    ("api_key_env_alternates", [" "]),
])
def test_invalid_override_blocks_all_consumers(
    layers, monkeypatch, client, caplog, filename, field, value,
):
    from llm_clients import factory
    from saiverse.provider_security import validate_model_config_connection

    write_config(layers["builtin"], "example.json", VALID)
    write_config(layers["builtin"], "unrelated.json", {**VALID, "id": "unrelated"})
    invalid = write_config(layers["user_data"], filename, {
        **VALID, "base_url": "https://DO_NOT_LOG.invalid/v1", field: value,
    })
    before = invalid.read_bytes()
    write_config(layers["builtin"], "model.json", {
        "model": "synthetic", "provider_ref": "example", "display_name": "Synthetic",
    }, subdir="models")
    with caplog.at_level(logging.WARNING):
        configs = load_registries(monkeypatch)
    error = configs["example"]["config_error"]
    assert error == {"path": str(invalid.absolute()), "source": "user_data", "reason": error["reason"]}
    assert field in error["reason"]
    assert "DO_NOT_LOG" not in caplog.text
    assert "base_url" not in configs["example"]
    assert configs["unrelated"]["source"] == "builtin"
    response = client.get("/api/providers")
    assert response.status_code == 200
    row = next(row for row in response.json() if row["id"] == "example")
    assert row["config_error"] == error
    assert row["available"] is False
    assert row["base_url"] is None
    assert row["api_key_envs"] == []
    assert "DO_NOT_LOG" not in response.text
    assert client.get("/api/providers/example/models").json() == ["model"]
    model = model_configs.MODEL_CONFIGS["model"]
    assert "base_url" not in model
    assert not model_configs.is_model_available("model")
    for route in ("/api/models", "/api/config/models"):
        response = client.get(route)
        assert response.status_code == 200
        row = next(row for row in response.json() if row["id"] == "model")
        assert row["available"] is False
        assert row["config_error"] == error
    # Neither validation nor the real factory may get as far as DNS or SDK creation.
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid provider reached a connection boundary")
    monkeypatch.setattr("saiverse.provider_security.validate_provider_url", forbidden)
    for name in ("OpenAIClient", "OllamaClient", "GeminiClient", "AnthropicClient"):
        monkeypatch.setattr(factory, name, forbidden)
    with pytest.raises(ValueError, match="Provider configuration is invalid"):
        validate_model_config_connection("model", model)
    with pytest.raises(ValueError, match="Provider configuration is invalid"):
        factory.get_llm_client("model", "openai", 4096, model)
    probe = client.post("/api/providers/example/test").json()
    assert probe["success"] is False
    assert "Provider configuration is invalid" in probe["error"]
    assert invalid.read_bytes() == before


@pytest.mark.parametrize("contents", [
    "{", "null", "[]", '\"text\"', "12", "true", "[{}]",
    '{"id": null}', '{"id": []}', '{"id": {}}', '{"id": 42}', '{"id": ""}',
    '{"id": " "}', '{"id": "DO_NOT_LOG/token"}',
])
def test_malformed_json_and_identity_remain_visible(layers, monkeypatch, client, contents):
    write_config(layers["builtin"], "example.json", VALID)
    path = write_config(layers["user_data"], "example.json", {})
    path.write_text(contents, encoding="utf-8")
    configs = load_registries(monkeypatch)
    assert configs["example"]["source"] == "user_data"
    assert configs["example"]["config_error"]["path"] == str(path.absolute())
    assert client.get("/api/providers").status_code == 200


@pytest.mark.parametrize("failure", ["utf8", "read_error"])
def test_unreadable_override_stops_lower_layer(layers, monkeypatch, failure):
    write_config(layers["builtin"], "example.json", VALID)
    path = write_config(layers["user_data"], "example.json", VALID)
    if failure == "utf8":
        path.write_bytes(b"\xff\xfe")
    else:
        original = Path.read_text
        def read_text(self, *args, **kwargs):
            if self == path:
                raise PermissionError("DO_NOT_LOG")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read_text)
    assert provider_configs.load_configs()["example"]["config_error"]["source"] == "user_data"


@pytest.mark.parametrize("filename", ["example.json", "renamed.json"])
def test_three_layers_keep_broken_highest_until_repaired(layers, filename):
    write_config(layers["builtin"], "example.json", VALID)
    write_config(layers["expansion"], "example.json", {**VALID, "display_name": "Addon"})
    write_config(layers["expansion"].parent / "z_addon", "example.json", VALID)
    high = write_config(layers["user_data"], filename, {**VALID, "protocol": None})
    assert provider_configs.load_configs()["example"]["source"] == "user_data"
    high.write_text(json.dumps({**VALID, "display_name": "User"}), encoding="utf-8")
    configs = provider_configs.load_configs()
    assert list(configs) == ["example"]
    assert configs["example"]["display_name"] == "User"
    assert "config_error" not in configs["example"]


def test_different_ids_same_filename_block_hidden_provider_too(layers):
    write_config(layers["builtin"], "shared.json", VALID)
    high = write_config(layers["user_data"], "shared.json", {**VALID, "id": "custom", "protocol": None})
    configs = provider_configs.load_configs()
    for pid in ("custom", "example"):
        assert configs[pid]["config_error"]["path"] == str(high.absolute())
        assert "base_url" not in configs[pid]


def test_unparseable_filename_alias_blocks_lower_id_and_alternate_name(layers):
    write_config(layers["builtin"], "other.json", VALID)
    write_config(layers["expansion"], "shared.json", VALID)
    high = write_config(layers["user_data"], "shared.json", None)
    configs = provider_configs.load_configs()
    assert configs["example"]["config_error"]["path"] == str(high.absolute())
    assert configs["shared"]["config_error"]["path"] == str(high.absolute())


def test_broken_lower_definition_cannot_disable_valid_higher(layers):
    write_config(layers["user_data"], "renamed.json", VALID)
    write_config(layers["builtin"], "example.json", {**VALID, "protocol": None})
    assert provider_configs.load_configs()["example"] == {**VALID, "source": "user_data"}


def test_optional_nulls_and_empty_alternates_are_accepted(layers, monkeypatch, client):
    write_config(layers["user_data"], "example.json", {
        **VALID, "base_url": None, "api_key_env": None, "api_key_env_alternates": [],
        "api_key_required": None, "default_request_kwargs": None,
        "default_convert_system_to_user": None, "default_supports_images": None,
        "default_max_image_bytes": None,
    })
    configs = load_registries(monkeypatch)
    assert "config_error" not in configs["example"]
    assert client.get("/api/providers/example").json()["base_url"] is None


def test_missing_protocol_is_visible_as_invalid(layers):
    write_config(layers["user_data"], "legacy.json", {})
    assert "protocol" in provider_configs.load_configs()["legacy"]["config_error"]["reason"]


def test_invalid_only_provider_remains_visible(layers, monkeypatch, client):
    write_config(layers["user_data"], "broken.json", {"protocol": None})
    write_config(layers["builtin"], "example.json", VALID)
    load_registries(monkeypatch)
    response = client.get("/api/providers")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {"broken", "example"}


def test_walked_layer_and_diagnostics_cannot_be_forged(layers):
    builtin = write_config(layers["builtin"], "example.json", {**VALID, "config_error": {"reason": "fake"}})
    addon_dir = layers["expansion"] / "providers"
    addon_dir.mkdir(parents=True)
    try:
        (addon_dir / "example.json").symlink_to(builtin)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    config = provider_configs.load_configs()["example"]
    assert config["source"] == "expansion"
    assert "config_error" not in config


def test_shipped_openrouter_models_visible_unavailable_then_repaired(layers, monkeypatch, client):
    monkeypatch.setattr(data_paths, "BUILTIN_DATA_DIR", SHIPPED_DATA)
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key-for-isolated-test")
    baseline = load_registries(monkeypatch)
    expected = {
        key: config for key, config in model_configs.MODEL_CONFIGS.items()
        if config.get("provider_ref") == "openrouter"
    }
    assert expected
    path = write_config(layers["user_data"], "openrouter.json", {})
    path.write_text('{"protocol":', encoding="utf-8")
    configs = load_registries(monkeypatch)
    assert configs["openrouter"]["config_error"]
    assert set(client.get("/api/providers/openrouter/models").json()) == set(expected)
    for route in ("/api/models", "/api/config/models"):
        response = client.get(route)
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()}
        for key in expected:
            assert rows[key]["available"] is False
            assert rows[key]["config_error"]["path"] == str(path.absolute())
            assert not model_configs.is_model_available(key)
    path.unlink()  # explicit user repair/removal allows lower-layer settings again
    assert load_registries(monkeypatch) == baseline
    for key, config in expected.items():
        assert model_configs.MODEL_CONFIGS[key] == config
        assert model_configs.is_model_available(key)


def test_alias_discovered_after_lower_id_still_blocks_it(layers):
    # Enumeration order within one root must not decide whether an override is safe.
    write_config(layers["user_data"], "shared.json", None)
    write_config(layers["builtin"], "first.json", VALID)
    write_config(layers["builtin"], "shared.json", VALID)
    assert provider_configs.load_configs()["example"]["config_error"]["source"] == "user_data"


def test_stale_resolved_model_and_reflex_cannot_use_newly_broken_provider(layers, monkeypatch):
    from llm_clients import factory
    from saiverse import reflex_judgment

    write_config(layers["builtin"], "example.json", VALID)
    write_config(layers["builtin"], "model.json", {
        "model": "synthetic", "provider_ref": "example",
    }, subdir="models")
    load_registries(monkeypatch)
    stale = dict(model_configs.MODEL_CONFIGS["model"])
    write_config(layers["user_data"], "example.json", {**VALID, "protocol": "typo"})
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", provider_configs.load_configs())
    assert not model_configs.is_model_available("model")
    monkeypatch.setattr(factory, "OpenAIClient", lambda *a, **k: pytest.fail("SDK constructed"))
    with pytest.raises(ValueError, match="Provider configuration is invalid"):
        factory.get_llm_client("model", "openai", 4096, stale)
    with pytest.raises(reflex_judgment.ReflexJudgmentUnavailable, match="Provider configuration is invalid"):
        reflex_judgment.resolve_backend("model")


def test_null_endpoint_and_key_keep_factory_semantics(layers, monkeypatch):
    from types import SimpleNamespace
    from llm_clients import factory

    write_config(layers["user_data"], "example.json", {
        **VALID, "base_url": None, "api_key_env": None, "api_key_required": False,
    })
    write_config(layers["builtin"], "model.json", {
        "model": "synthetic", "provider_ref": "example",
    }, subdir="models")
    load_registries(monkeypatch)
    seen = []
    def capture(*args, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(factory, "OpenAIClient", capture)
    factory.get_llm_client("model", "openai", 4096)
    assert seen[0]["api_key"] == factory._LOCAL_SERVER_PLACEHOLDER_KEY
    assert "api_key_env" not in seen[0]
    assert "base_url" not in seen[0]  # null leaves endpoint choice to this protocol's existing client


def test_protocol_validation_matches_factory_dispatch_and_reflex_implementation():
    import ast
    import inspect
    import textwrap
    from llm_clients import factory
    from saiverse import reflex_judgment
    from saiverse.provider_protocols import SUPPORTED_PROVIDER_PROTOCOLS

    code = ast.parse(textwrap.dedent(inspect.getsource(factory.get_llm_client)))
    dispatched = {
        node.comparators[0].value for node in ast.walk(code)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
        and node.left.id == "protocol" and isinstance(node.ops[0], ast.Eq)
        and isinstance(node.comparators[0], ast.Constant)
    }
    assert dispatched == factory.SUPPORTED_PROTOCOLS
    assert SUPPORTED_PROVIDER_PROTOCOLS == dispatched | {reflex_judgment.JEV_COMPAT_PROTOCOL}
    for protocol in SUPPORTED_PROVIDER_PROTOCOLS:
        assert provider_configs._provider_shape_error({**VALID, "protocol": protocol}, "example") is None
    for path in (SHIPPED_DATA / "providers").glob("*.json"):
        assert provider_configs._provider_shape_error(json.loads(path.read_text()), path.stem) is None


def test_reply_binding_stops_selected_model_instead_of_switching(layers, monkeypatch):
    from llm_clients import factory
    from llm_clients.exceptions import ModelUnavailableError
    from saiverse.persona_model_selection import ReplyModelBinding, TIER_STANDARD

    write_config(layers["user_data"], "example.json", {**VALID, "protocol": None})
    write_config(layers["builtin"], "model.json", {
        "model": "synthetic", "provider_ref": "example",
    }, subdir="models")
    load_registries(monkeypatch)
    created = []
    def forbidden(*args, **kwargs):
        created.append(args)
        pytest.fail("A replacement client was created")
    monkeypatch.setattr(factory, "OpenAIClient", forbidden)
    monkeypatch.setattr(factory, "GeminiClient", forbidden)

    class SyntheticPersona:
        persona_id = "isolated-fixture"
        persona_name = "Fixture"
        model = "model"
        lightweight_model = None
        @property
        def llm_client(self):
            return factory.get_llm_client(self.model, "openai", 4096)

    persona = SyntheticPersona()
    binding = ReplyModelBinding.capture(persona)
    with pytest.raises(ModelUnavailableError) as exc_info:
        binding.client_for(TIER_STANDARD)
    assert exc_info.value.model == "model"
    assert exc_info.value.reason == "unreachable"
    assert persona.model == "model"
    assert created == []


def test_invalid_id_diagnostic_never_returns_raw_value(layers, monkeypatch, client, caplog):
    write_config(layers["user_data"], "broken.json", {
        **VALID, "id": "DO_NOT_LOG/token", "display_name": "DO_NOT_LOG", "protocol": None,
    })
    with caplog.at_level(logging.WARNING):
        configs = load_registries(monkeypatch)
    assert list(configs) == ["broken"]
    response = client.get("/api/providers")
    assert response.status_code == 200
    assert "DO_NOT_LOG" not in response.text + caplog.text
