"""モデルを選ぶ欄の並べ方 (saiverse/model_picker.py、docs/intent/model_picker.md)。

標準のプロバイダ定義の current_models が実在のモデルを指していることと、
並べ方の決め方を確かめる。ファイルと読み込み済みの定義は全部テストの中で差し替え、
ペルソナ・LLM・ネットワークには触れない。
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saiverse import data_paths, model_configs, model_picker, provider_configs

SHIPPED_DATA = data_paths.BUILTIN_DATA_DIR


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 標準のプロバイダ定義に書いた current_models の検査
# ---------------------------------------------------------------------------

def _shipped_providers() -> dict[str, dict]:
    return {
        path.stem: _read_json(path)
        for path in sorted((SHIPPED_DATA / "providers").glob("*.json"))
    }


def _shipped_models() -> dict[str, dict]:
    return {
        path.stem: _read_json(path)
        for path in sorted((SHIPPED_DATA / "models").glob("*.json"))
    }


def test_標準のプロバイダ定義の形は読み込みの検査を通る():
    for pid, config in _shipped_providers().items():
        assert provider_configs._provider_shape_error(config, pid) is None, pid


def test_current_models_は実在する自分のプロバイダのモデルだけを重複なく並べる():
    models = _shipped_models()
    checked = 0
    for pid, provider in _shipped_providers().items():
        listed = provider.get("current_models")
        if listed is None:
            continue
        checked += 1
        assert listed, f"{pid}: current_models が空"
        assert len(listed) == len(set(listed)), f"{pid}: 重複がある"
        for key in listed:
            assert key in models, f"{pid}: {key} は builtin_data/models/ に無い"
            model = models[key]
            owner = model.get("provider_ref") or model.get("provider")
            assert owner == pid, f"{pid}: {key} のプロバイダは {owner}"
    assert checked >= 6


def test_gemini_の_current_models_は有料6つと無料5つ():
    provider = _shipped_providers()["gemini"]
    models = _shipped_models()
    listed = provider["current_models"]
    paid = [key for key in listed if models[key].get("prefer_paid")]
    free = [key for key in listed if not models[key].get("prefer_paid")]
    assert len(paid) == 6
    assert len(free) == 5


def test_手元のサーバー用の標準プロバイダは_custom_にまとめる():
    providers = _shipped_providers()
    for pid in ("ollama", "lmstudio", "llama_cpp_server"):
        assert providers[pid]["picker_group"] == "custom"


# ---------------------------------------------------------------------------
# 形の検査
# ---------------------------------------------------------------------------

BASE_PROVIDER = {"id": "example", "display_name": "Example", "protocol": "openai_compat"}


@pytest.mark.parametrize("value", [
    "a", [1], [""], ["  "], ["ok", None], {"a": 1},
])
def test_current_models_の形が違うと定義は壊れている扱い(value):
    reason = provider_configs._provider_shape_error(
        {**BASE_PROVIDER, "current_models": value}, "example")
    assert reason is not None and "current_models" in reason


@pytest.mark.parametrize("value", ["", "has space", "a/b", 1, ["custom"]])
def test_picker_group_の形が違うと定義は壊れている扱い(value):
    reason = provider_configs._provider_shape_error(
        {**BASE_PROVIDER, "picker_group": value}, "example")
    assert reason is not None and "picker_group" in reason


@pytest.mark.parametrize("extra", [
    {}, {"current_models": None}, {"current_models": []}, {"current_models": ["a", "b"]},
    {"picker_group": None}, {"picker_group": "custom"},
])
def test_省略と正しい形は通る(extra):
    assert provider_configs._provider_shape_error({**BASE_PROVIDER, **extra}, "example") is None


# ---------------------------------------------------------------------------
# 並べ方の決め方 (定義はテスト用の層に書いて読み込む)
# ---------------------------------------------------------------------------

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
    # 読み直しは reload_configs を使わず差し替えで行う (ペルソナの決め直しを起こさない)。
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", {})
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {})
    monkeypatch.setattr(model_configs, "LEGACY_MODELS_DIR", tmp_path / "no_legacy_models")
    return roots


def write(root: Path, subdir: str, name: str, config: dict) -> Path:
    target = root / subdir / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config), encoding="utf-8")
    return target


def provider(root, pid, display_name, **extra):
    return write(root, "providers", pid, {
        "id": pid, "display_name": display_name, "protocol": "openai_compat",
        "api_key_required": False, **extra,
    })


def model(root, key, display_name, provider_ref, **extra):
    return write(root, "models", key, {
        "model": key, "display_name": display_name, "provider_ref": provider_ref, **extra,
    })


def load(monkeypatch):
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", provider_configs.load_configs())
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", model_configs.load_configs())
    return model_picker.compute_placements()


def test_gemini_は_prefer_paid_で有料と無料のボタンに分かれる(layers, monkeypatch):
    b = layers["builtin"]
    provider(b, "gemini", "Google Gemini", current_models=["g-paid", "g-free"])
    model(b, "g-paid", "G Paid", "gemini", prefer_paid=True)
    model(b, "g-free", "G Free", "gemini", prefer_paid=False)
    model(b, "g-unset", "G Unset", "gemini")
    placements = load(monkeypatch)
    assert placements["g-paid"].picker_group == "gemini_paid"
    assert placements["g-paid"].picker_group_label == "Gemini (有料)"
    assert placements["g-free"].picker_group == "gemini_free"
    assert placements["g-free"].picker_group_label == "Gemini (無料)"
    assert placements["g-unset"].picker_group == "gemini_free"
    assert placements["g-paid"].is_current and placements["g-free"].is_current
    assert placements["g-unset"].is_current is False


def test_利用者が自分で作ったプロバイダは_custom_に入りプロバイダ名が見出しになる(layers, monkeypatch):
    provider(layers["user_data"], "mine", "My Server")
    model(layers["user_data"], "mine-1", "Mine 1", "mine")
    placements = load(monkeypatch)
    assert placements["mine-1"].picker_group == "custom"
    assert placements["mine-1"].picker_group_label == "カスタム"
    assert placements["mine-1"].picker_heading == "My Server"
    # リストを持たないプロバイダは全部を最初から出す。
    assert placements["mine-1"].is_current is True


def test_標準のプロバイダを上書きしても置き場所は配られている定義のとおり(layers, monkeypatch):
    """画面から標準のプロバイダを編集すると user_data の層の上書きになる。

    それでモデルが custom へ移ったり、上書きの中の古い current_models が効いたり
    しないこと。
    """
    provider(layers["builtin"], "openai", "OpenAI", current_models=["gpt-b"])
    provider(layers["user_data"], "openai", "My OpenAI", current_models=["gpt-a"])
    provider(layers["builtin"], "ollama", "Ollama (local)", picker_group="custom")
    provider(layers["user_data"], "ollama", "My Ollama")
    model(layers["builtin"], "gpt-a", "GPT A", "openai")
    model(layers["builtin"], "gpt-b", "GPT B", "openai")
    model(layers["builtin"], "ol-1", "Ol 1", "ollama")
    placements = load(monkeypatch)
    for key in ("gpt-a", "gpt-b"):
        assert placements[key].picker_group == "openai"
        assert placements[key].picker_heading is None
    assert placements["gpt-a"].is_current is False
    assert placements["gpt-b"].is_current is True
    assert placements["ol-1"].picker_group == "custom"
    assert placements["ol-1"].picker_heading == "My Ollama"


def test_picker_group_を書いたプロバイダはその名前のボタンに入る(layers, monkeypatch):
    provider(layers["builtin"], "ollama", "Ollama (local)", picker_group="custom")
    model(layers["builtin"], "ol-1", "Ol 1", "ollama")
    placements = load(monkeypatch)
    assert placements["ol-1"].picker_group == "custom"
    assert placements["ol-1"].picker_heading == "Ollama (local)"
    assert placements["ol-1"].is_current is True


def test_反射判断専用のプロバイダは_reflex_のボタンにまとまり先頭に並ぶ(layers, monkeypatch):
    b = layers["builtin"]
    provider(b, "typesafe", "TypeSafe System One", picker_group="reflex")
    provider(b, "localjev", "localjev (local)", picker_group="reflex")
    provider(b, "anthropic", "Anthropic")
    model(b, "jev-ts", "Jev (TypeSafe)", "typesafe")
    model(b, "jev-local", "Jev (localjev)", "localjev")
    model(b, "a-1", "A 1", "anthropic")
    placements = load(monkeypatch)
    for key, heading in (("jev-ts", "TypeSafe System One"), ("jev-local", "localjev (local)")):
        assert placements[key].picker_group == "reflex"
        assert placements[key].picker_group_label == "反射判断専用"
        assert placements[key].picker_heading == heading
    assert placements["jev-ts"].picker_group_order < placements["a-1"].picker_group_order


def test_利用者が追加したモデルはリストに無くても最初から出す(layers, monkeypatch):
    provider(layers["builtin"], "openai", "OpenAI", current_models=["gpt-a"])
    model(layers["builtin"], "gpt-a", "GPT A", "openai")
    model(layers["builtin"], "gpt-old", "GPT Old", "openai")
    model(layers["user_data"], "gpt-mine", "GPT Mine", "openai")
    model(layers["expansion"], "gpt-addon", "GPT Addon", "openai")
    placements = load(monkeypatch)
    assert placements["gpt-a"].is_current is True
    assert placements["gpt-old"].is_current is False
    assert placements["gpt-mine"].is_current is True
    assert placements["gpt-addon"].is_current is True
    for key in ("gpt-a", "gpt-old", "gpt-mine", "gpt-addon"):
        assert placements[key].picker_group == "openai"
        assert placements[key].picker_group_label == "OpenAI"
        assert placements[key].picker_heading is None


def test_リストの無いプロバイダは全部を最初から出す(layers, monkeypatch):
    provider(layers["builtin"], "sakana", "Sakana AI")
    model(layers["builtin"], "s-1", "S 1", "sakana")
    model(layers["builtin"], "s-2", "S 2", "sakana")
    placements = load(monkeypatch)
    assert placements["s-1"].is_current and placements["s-2"].is_current
    assert placements["s-1"].picker_group == "sakana"


def test_provider_ref_の無い古い形の定義は_provider_の値で分ける(layers, monkeypatch):
    write(layers["user_data"], "models", "legacy", {
        "model": "legacy", "display_name": "Legacy", "provider": "anthropic",
    })
    provider(layers["builtin"], "anthropic", "Anthropic")
    placements = load(monkeypatch)
    assert placements["legacy"].picker_group == "anthropic"
    assert placements["legacy"].is_current is True


def _scenario_for_order(layers):
    b = layers["builtin"]
    provider(b, "openai", "OpenAI", current_models=["o-second", "o-first"])
    model(b, "o-first", "Zeta", "openai")
    model(b, "o-second", "Alpha", "openai")
    model(b, "o-rest-b", "Bravo", "openai")
    model(b, "o-rest-a", "Able", "openai")
    provider(b, "anthropic", "Anthropic")
    model(b, "a-1", "Claude", "anthropic")
    provider(b, "gemini", "Google Gemini")
    model(b, "g-free", "G Free", "gemini")
    model(b, "g-paid", "G Paid", "gemini", prefer_paid=True)
    provider(b, "zeta_vendor", "Zeta Vendor")
    model(b, "z-1", "Z", "zeta_vendor")
    provider(b, "beta_vendor", "Beta Vendor")
    model(b, "bv-1", "B", "beta_vendor")
    provider(b, "ollama", "Ollama (local)", picker_group="custom")
    model(b, "ol-1", "Ol", "ollama")
    provider(layers["user_data"], "aaa_server", "AAA Server")
    model(layers["user_data"], "aaa-1", "AAA", "aaa_server")


EXPECTED_ORDER = [
    "a-1",                                   # anthropic
    "o-second", "o-first",                   # openai: リストの順
    "o-rest-a", "o-rest-b",                  # openai: 残りは表示名の順
    "g-paid",                                # gemini_paid
    "g-free",                                # gemini_free
    "bv-1",                                  # 固定の順に無いグループは名前の順
    "z-1",
    "aaa-1",                                 # custom: 見出し (プロバイダ名) の順
    "ol-1",
]


def test_並び順はボタンの順_グループの中はリストの順のあと表示名の順(layers, monkeypatch):
    _scenario_for_order(layers)
    placements = load(monkeypatch)
    ordered = model_picker.sort_model_keys(list(placements), placements)
    assert ordered == EXPECTED_ORDER
    orders = [placements[key].picker_group_order for key in ordered]
    assert orders == sorted(orders)
    assert placements["ol-1"].picker_group_order == max(orders)
    assert placements["a-1"].picker_group_order < placements["o-first"].picker_group_order
    assert placements["o-first"].picker_group_order < placements["g-paid"].picker_group_order
    assert placements["g-paid"].picker_group_order < placements["g-free"].picker_group_order


def test_読み直したあとは新しい定義で決める(layers, monkeypatch):
    path = provider(layers["builtin"], "openai", "OpenAI", current_models=["gpt-a"])
    model(layers["builtin"], "gpt-a", "GPT A", "openai")
    model(layers["builtin"], "gpt-b", "GPT B", "openai")
    placements = load(monkeypatch)
    assert placements["gpt-b"].is_current is False
    path.write_text(json.dumps({
        "id": "openai", "display_name": "OpenAI", "protocol": "openai_compat",
        "api_key_required": False, "current_models": ["gpt-b"],
    }), encoding="utf-8")
    placements = load(monkeypatch)
    assert placements["gpt-a"].is_current is False
    assert placements["gpt-b"].is_current is True


# ---------------------------------------------------------------------------
# GET /api/config/models
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from api.routes import config

    app = FastAPI()
    app.include_router(config.router, prefix="/api/config")
    with TestClient(app) as test_client:
        yield test_client


def test_モデル一覧の応答に置き場所が載り決めた順に並ぶ(layers, monkeypatch, client):
    _scenario_for_order(layers)
    load(monkeypatch)
    response = client.get("/api/config/models")
    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == EXPECTED_ORDER
    by_id = {row["id"]: row for row in rows}
    assert by_id["g-paid"]["picker_group"] == "gemini_paid"
    assert by_id["g-paid"]["picker_group_label"] == "Gemini (有料)"
    assert by_id["o-rest-a"]["is_current"] is False
    assert by_id["o-first"]["is_current"] is True
    assert by_id["ol-1"]["picker_heading"] == "Ollama (local)"
    assert by_id["a-1"]["picker_heading"] is None
    for row in rows:
        for field in ("picker_group", "picker_group_label", "picker_group_order",
                      "picker_heading", "is_current"):
            assert field in row


def test_キーの無いモデルは一覧に出ないまま並び順は崩れない(layers, monkeypatch, client):
    b = layers["builtin"]
    write(b, "providers", "openai", {
        "id": "openai", "display_name": "OpenAI", "protocol": "openai_compat",
        "base_url": "https://api.openai.com/v1", "api_key_env": "PICKER_TEST_OPENAI_KEY",
    })
    model(b, "o-1", "O 1", "openai")
    provider(b, "anthropic", "Anthropic")
    model(b, "a-1", "A 1", "anthropic")
    monkeypatch.delenv("PICKER_TEST_OPENAI_KEY", raising=False)
    load(monkeypatch)
    assert [row["id"] for row in client.get("/api/config/models").json()] == ["a-1"]
    monkeypatch.setenv("PICKER_TEST_OPENAI_KEY", "dummy-key-for-isolated-test")
    assert [row["id"] for row in client.get("/api/config/models").json()] == ["a-1", "o-1"]


def test_標準の定義で一覧を作っても全行に置き場所が付く(layers, monkeypatch, client):
    monkeypatch.setattr(data_paths, "BUILTIN_DATA_DIR", SHIPPED_DATA)
    for env in ("GEMINI_API_KEY", "CLAUDE_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
                "OPENROUTER_API_KEY"):
        monkeypatch.setenv(env, "dummy-key-for-isolated-test")
    placements = load(monkeypatch)
    shipped = _shipped_providers()
    for pid in ("anthropic", "openai", "openai_codex", "xai", "openrouter"):
        listed = shipped[pid]["current_models"]
        current = [
            key for key in model_picker.sort_model_keys(list(placements), placements)
            if placements[key].picker_group == pid and placements[key].is_current
        ]
        assert current == listed, pid
    gemini_listed = shipped["gemini"]["current_models"]
    for group in ("gemini_paid", "gemini_free"):
        current = [
            key for key in model_picker.sort_model_keys(list(placements), placements)
            if placements[key].picker_group == group and placements[key].is_current
        ]
        assert current == [
            key for key in gemini_listed if placements[key].picker_group == group
        ], group
    rows = client.get("/api/config/models").json()
    orders = [row["picker_group_order"] for row in rows]
    assert orders == sorted(orders)
