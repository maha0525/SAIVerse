"""Invalid provider overrides must not hide valid lower-layer definitions.

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
    from api.routes import info

    app = FastAPI()
    app.include_router(providers.router, prefix="/api/providers")
    app.include_router(info.router, prefix="/api")
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
    ("base_url", 0), ("base_url", []),
    ("api_key_env", 0), ("api_key_env", 7), ("api_key_env", ["DO_NOT_LOG"]),
    ("api_key_required", "false"), ("api_key_required", 0),
    ("default_request_kwargs", []),
    ("default_convert_system_to_user", "false"),
    ("default_supports_images", "true"),
    ("default_max_image_bytes", "1024"), ("default_max_image_bytes", True),
])
def test_invalid_fields_fall_back_before_filename_or_id_shadowing(
    layers, monkeypatch, client, filename, field, value,
):
    write_config(layers["builtin"], "example.json", VALID)
    write_config(layers["builtin"], "unrelated.json", {**VALID, "id": "unrelated"})
    invalid = write_config(layers["user_data"], filename, {**VALID, field: value})
    before = invalid.read_bytes()
    write_config(layers["builtin"], "model.json", {
        "model": "synthetic", "provider_ref": "example", "display_name": "Synthetic",
    }, subdir="models")

    configs = load_registries(monkeypatch)
    assert configs["example"] == {**VALID, "source": "builtin"}
    response = client.get("/api/providers")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {"example", "unrelated"}
    assert client.get("/api/providers/example").json()["builtin"] is True
    assert client.get("/api/providers/example/models").json() == ["model"]
    model = model_configs.MODEL_CONFIGS["model"]
    assert model["protocol"] == VALID["protocol"]
    assert model["base_url"] == VALID["base_url"]
    assert model["api_key_required"] is False
    assert [row["id"] for row in client.get("/api/models").json()] == ["model"]
    assert invalid.read_bytes() == before


@pytest.mark.parametrize("contents", [
    "{", "null", "[]", '"text"', "12", "true", "[{}]",
    '{"id": null}', '{"id": []}', '{"id": {}}', '{"id": 42}', '{"id": ""}',
])
def test_malformed_json_and_identity_do_not_escape_loader(layers, monkeypatch, client, contents):
    write_config(layers["builtin"], "example.json", VALID)
    path = write_config(layers["user_data"], "example.json", {})
    path.write_text(contents, encoding="utf-8")
    configs = load_registries(monkeypatch)
    assert configs["example"]["source"] == "builtin"
    assert client.get("/api/providers").status_code == 200


@pytest.mark.parametrize("failure", ["utf8", "read_error"])
def test_unreadable_override_uses_lower_layer(layers, monkeypatch, failure):
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
    assert provider_configs.load_configs()["example"]["source"] == "builtin"


@pytest.mark.parametrize("same_filename", [True, False])
def test_all_three_layers_and_later_addon_are_considered(layers, same_filename):
    write_config(layers["builtin"], "example.json", VALID)
    write_config(layers["user_data"], "example.json", {**VALID, "protocol": None})
    exp_name = "example.json" if same_filename else "addon-provider.json"
    exp_path = write_config(layers["expansion"], exp_name, {**VALID, "display_name": None})
    assert provider_configs.load_configs()["example"]["source"] == "builtin"
    later = layers["expansion"].parent / "z_addon"
    write_config(later, exp_name, {**VALID, "display_name": "Later addon"})
    assert provider_configs.load_configs()["example"]["display_name"] == "Later addon"
    exp_path.write_text(json.dumps({**VALID, "display_name": "First addon"}), encoding="utf-8")
    assert provider_configs.load_configs()["example"]["display_name"] == "First addon"


@pytest.mark.parametrize("filename", ["example.json", "renamed.json"])
def test_valid_override_retains_priority(layers, filename):
    write_config(layers["builtin"], "example.json", VALID)
    write_config(layers["expansion"], "example.json", {**VALID, "display_name": "Addon"})
    write_config(layers["user_data"], filename, {**VALID, "display_name": "User"})
    configs = provider_configs.load_configs()
    assert list(configs) == ["example"]
    assert configs["example"]["display_name"] == "User"
    assert configs["example"]["source"] == "user_data"


def test_different_ids_same_filename_shadow_only_when_valid(layers):
    write_config(layers["builtin"], "shared.json", VALID)
    path = write_config(layers["user_data"], "shared.json", {**VALID, "id": "custom", "protocol": None})
    assert list(provider_configs.load_configs()) == ["example"]
    path.write_text(json.dumps({**VALID, "id": "custom"}), encoding="utf-8")
    assert list(provider_configs.load_configs()) == ["custom"]


def test_optional_nulls_and_legacy_omissions_remain_accepted(layers, monkeypatch, client):
    write_config(layers["user_data"], "legacy.json", {
        "base_url": None, "api_key_env": None, "api_key_required": None,
        "default_request_kwargs": None, "default_convert_system_to_user": None,
        "default_supports_images": None, "default_max_image_bytes": None,
    })
    load_registries(monkeypatch)
    response = client.get("/api/providers/legacy")
    assert response.status_code == 200
    assert response.json()["id"] == "legacy"
    assert response.json()["display_name"] == "legacy"
    assert response.json()["protocol"] == "unknown"
    assert response.json()["api_key_env"] is None


@pytest.mark.parametrize("filename,declared_id", [
    ("example.json", "example"), ("renamed.json", "example"), ("example.json", "different"),
])
def test_warnings_identify_source_and_actual_fallback_without_values(
    layers, caplog, filename, declared_id,
):
    low = write_config(layers["builtin"], "example.json", VALID)
    high = write_config(layers["user_data"], filename, {
        **VALID, "id": declared_id, "api_key_env": {"secret": "DO_NOT_LOG"},
        "base_url": "https://DO_NOT_LOG.invalid/", "default_headers": {"Authorization": "DO_NOT_LOG"},
    })
    with caplog.at_level(logging.WARNING, logger="saiverse.provider_configs"):
        provider_configs.load_configs()
    message = caplog.messages[0]
    assert f"path={str(high.absolute())!r} source=user_data provider_id={declared_id!r}" in message
    assert f"fallback=path={str(low.absolute())!r} source=builtin provider_id='example'" in message
    assert "api_key_env" in message
    assert "DO_NOT_LOG" not in caplog.text


def test_missing_fallback_is_explicit_and_unrelated_provider_survives(layers, monkeypatch, client, caplog):
    write_config(layers["user_data"], "broken.json", {**VALID, "id": "broken", "protocol": None})
    write_config(layers["builtin"], "example.json", VALID)
    with caplog.at_level(logging.WARNING, logger="saiverse.provider_configs"):
        configs = load_registries(monkeypatch)
    assert list(configs) == ["example"]
    assert "provider_id='broken'" in caplog.text
    assert "fallback=none" in caplog.text
    assert client.get("/api/providers").status_code == 200


def test_fallback_keeps_walked_layer_for_symlink(layers):
    # A valid lower candidate through an addon link must not inherit builtin trust.
    builtin = write_config(layers["builtin"], "example.json", VALID)
    addon_dir = layers["expansion"] / "providers"
    addon_dir.mkdir(parents=True)
    try:
        (addon_dir / "example.json").symlink_to(builtin)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    write_config(layers["user_data"], "example.json", {**VALID, "protocol": None})
    assert provider_configs.load_configs()["example"]["source"] == "expansion"


def test_shipped_openrouter_models_survive_broken_override(layers, monkeypatch, client):
    monkeypatch.setattr(data_paths, "BUILTIN_DATA_DIR", SHIPPED_DATA)
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key-for-isolated-test")
    baseline = load_registries(monkeypatch)
    expected = {
        key: config for key, config in model_configs.MODEL_CONFIGS.items()
        if config.get("provider_ref") == "openrouter"
    }
    assert expected, "The shipped models must exercise provider_ref resolution"
    path = write_config(layers["user_data"], "openrouter.json", {})
    path.write_text('{"protocol":', encoding="utf-8")
    configs = load_registries(monkeypatch)
    assert configs == baseline
    for key, config in expected.items():
        assert model_configs.MODEL_CONFIGS[key] == config
    response = client.get("/api/providers")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == set(baseline)
    assert set(client.get("/api/providers/openrouter/models").json()) == set(expected)
    response = client.get("/api/models")
    assert response.status_code == 200
    assert set(expected) <= {row["id"] for row in response.json()}
