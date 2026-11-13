"""反射判断の Gemini Decisions と画像 (docs/intent/reflex_judgment.md §7・§8) のユニットテスト。

実 API は絶対に呼ばない。httpx.MockTransport でリクエストを捕まえ、通常の LLM の道は
偽のクライアントを差し込み、モデル設定は偽の MODEL_CONFIGS / PROVIDER_CONFIGS を
差し込む (同梱の設定は builtin_data から直接読んで、読み込み後の解決済みの姿にする)。

固定する契約:
- ``request_shape: gemini_decisions`` は、キーを ``x-goog-api-key`` に接頭辞なしで載せ、
  状況を文字列の ``input`` にし、noul の基準を ``true_description`` /
  ``false_description`` へ写し、choice を ``options`` の欄なしで ``criteria`` の辞書に
  して送る。答えと使用量 (``total_input_tokens``) は既存の読み戻しと記帳を通る。
- 認証ヘッダの宣言 (``auth_header`` / ``auth_prefix``) の既定は従来のヘッダのまま。
- 画像を受け付けない答える側には、画像を渡しても渡さないときと同じ要求を送る。
  受け付ける答える側には ``request_shape`` ごとの送り方で足す。読めない画像はその
  1 枚だけ落として続ける。
- 自動想起は、このターンの新しいユーザー発言の添付画像だけを判断に渡す。
"""

import base64
import io
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from PIL import Image

from saiverse import reflex_judgment
from saiverse.reflex_judgment import (
    SYSTEM_ONE_IMAGES_STATE_KEY,
    ReflexJudgmentUnavailable,
    evaluate,
    is_available,
    resolve_backend,
)

ROLE_ENV = "SAIVERSE_REFLEX_JUDGMENT_MODEL"
MODEL_KEY = "test-jev"
KEY_ENV = "SAIVERSE_MODEL_TEST_JEV_API_KEY"
BASE_URL = "http://127.0.0.1:8088"
API_KEY = "test-key-not-real"

GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_PAID = "gemini-decision-1-paid"
GEMINI_FREE = "gemini-decision-1"
GEMINI_URL = f"https://{GEMINI_HOST}/v1beta/decisions"

_CANONICAL_DIALECT = {
    "path": "/v1/systemone",
    "answers_key": "answers",
    "usage_key": "usage",
    "answer_fields": {"noul": "noul", "choice": "choice", "score": "score"},
    "usage_fields": {"input_tokens": "input_tokens", "output_tokens": "output_tokens"},
    "supported_types": ["noul", "choice", "score"],
}

NOUL_QUESTIONS = {
    "m0": {
        "type": "noul",
        "instructions": "`memories.m0` は今の話に浮かぶ記憶か。",
        "criteria": {"true": "関連する", "false": "関連しない"},
    },
}
STATE = {"conversation": [{"role": "user", "text": "これ見て"}], "memories": {}}


def _model_config(**overrides):
    config = {
        "model": "jev-latest",
        "protocol": "jev_compat",
        "provider": "jev_compat",
        "base_url": BASE_URL,
        "api_key_env": KEY_ENV,
        "reflex_judgment": dict(_CANONICAL_DIALECT),
    }
    config.update(overrides)
    return config


def _set_configs(monkeypatch, configs):
    from saiverse import model_configs

    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", configs)


def _set_providers(monkeypatch, providers):
    from saiverse import provider_configs

    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", providers)


@pytest.fixture(autouse=True)
def _role_and_key(monkeypatch):
    monkeypatch.setenv(ROLE_ENV, MODEL_KEY)
    monkeypatch.setenv(KEY_ENV, API_KEY)
    _set_configs(monkeypatch, {MODEL_KEY: _model_config()})


@pytest.fixture(autouse=True)
def usage_calls(monkeypatch):
    calls = []

    class _Tracker:
        def record_usage(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("saiverse.usage_tracker.get_usage_tracker", lambda: _Tracker())
    return calls


@pytest.fixture(autouse=True)
def _clean_outcomes_and_workers():
    reflex_judgment.reset_recent_outcomes()
    yield
    for thread in threading.enumerate():
        if thread.name == reflex_judgment._WORKER_THREAD_NAME:
            thread.join(10.0)
    reflex_judgment.reset_recent_outcomes()


def _capture(captured, response_json, status=200):
    """1 往復を捕まえる transport (呼ばれた回数も数える)。"""
    def handler(request):
        captured.setdefault("count", 0)
        captured["count"] += 1
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status, json=response_json)
    return httpx.MockTransport(handler)


def _image_file(tmp_path, name, size=(400, 300), fmt="PNG", mode="RGB"):
    path = tmp_path / name
    Image.new(mode, size, color=(10, 120, 200) if mode == "RGB" else 1).save(path, format=fmt)
    return path


def _descriptor(path, mime_type="image/png"):
    """saiverse.media_utils.iter_image_media が返す形。"""
    return {"uri": str(path), "path": path, "mime_type": mime_type}


def _decoded_size(b64):
    with Image.open(io.BytesIO(base64.b64decode(b64))) as img:
        return img.format, img.size


# ---------------------------------------------------------------------------
# 同梱の Gemini Decisions の設定
# ---------------------------------------------------------------------------


def _shipped_configs(*provider_ids, model_keys=()):
    """同梱の provider / モデル定義を builtin_data から読み、解決済みの姿で返す。"""
    from saiverse import model_configs
    from saiverse.data_paths import BUILTIN_DATA_DIR, MODELS_DIR, PROVIDERS_DIR
    from saiverse.provider_configs import SOURCE_BUILTIN

    def read(directory, stem):
        return json.loads((BUILTIN_DATA_DIR / directory / f"{stem}.json").read_text(encoding="utf-8"))

    providers = {}
    for pid in provider_ids:
        provider = read(PROVIDERS_DIR, pid)
        provider["source"] = SOURCE_BUILTIN
        providers[pid] = provider
    with patch.dict("saiverse.provider_configs.PROVIDER_CONFIGS", providers, clear=True):
        configs = {
            key: model_configs._resolve_provider_ref(read(MODELS_DIR, key)) for key in model_keys
        }
    return providers, configs


@pytest.fixture
def shipped_gemini(monkeypatch, mock_provider_network):
    providers, configs = _shipped_configs(
        "gemini_decisions", "gemini_decisions_free", model_keys=(GEMINI_PAID, GEMINI_FREE),
    )
    _set_providers(monkeypatch, providers)
    _set_configs(monkeypatch, configs)
    mock_provider_network(GEMINI_HOST)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-paid-key")
    monkeypatch.setenv("GEMINI_FREE_API_KEY", "test-gemini-free-key")
    return configs


def test_the_shipped_gemini_models_resolve_and_pass_the_credential_check(shipped_gemini):
    from saiverse.provider_security import validate_model_config_connection

    for key, env in ((GEMINI_PAID, "GEMINI_API_KEY"), (GEMINI_FREE, "GEMINI_FREE_API_KEY")):
        validate_model_config_connection(key, shipped_gemini[key])  # must not raise
        backend = resolve_backend(key, required_type="noul")
        assert backend.url == GEMINI_URL
        assert backend.api_model == "gemini-decision-1"
        assert backend.api_key_env == env
        assert backend.request_shape == reflex_judgment.REQUEST_SHAPE_GEMINI_DECISIONS
        assert backend.auth_header == "x-goog-api-key"
        assert backend.auth_prefix == ""
        assert backend.supports_images is True
        assert dict(backend.answer_fields) == {"noul": "noul", "choice": "choice", "score": "score"}
        assert dict(backend.usage_fields) == {
            "input_tokens": "total_input_tokens", "output_tokens": "total_output_tokens",
        }
        assert is_available(model_key=key, required_type="noul") is True
    assert shipped_gemini[GEMINI_FREE].get("api_key_env_alternates") is None


def test_gemini_usage_fields_default_by_shape_when_not_declared(shipped_gemini, monkeypatch):
    # user_data で provider を上書きして usage_fields を書き忘れても、Gemini の使用量の
    # 欄 (total_input_tokens) を読む。既定が System One の欄名のままだと、使用量が黙って
    # 記帳されなくなる。
    config = dict(shipped_gemini[GEMINI_PAID])
    dialect = dict(config["reflex_judgment"])
    dialect.pop("usage_fields")
    config["reflex_judgment"] = dialect
    _set_configs(monkeypatch, {GEMINI_PAID: config})
    backend = resolve_backend(GEMINI_PAID, required_type="noul")
    assert dict(backend.usage_fields) == {
        "input_tokens": "total_input_tokens", "output_tokens": "total_output_tokens",
    }
    assert "pricing" not in shipped_gemini[GEMINI_FREE]
    assert shipped_gemini[GEMINI_PAID]["pricing"]["input_per_1m_tokens"] == 0.1
    assert shipped_gemini[GEMINI_PAID]["context_length"] == 262144


def test_the_shipped_gemini_providers_pass_the_provider_shape_check():
    from saiverse import provider_configs
    from saiverse.data_paths import BUILTIN_DATA_DIR, PROVIDERS_DIR

    for pid in ("gemini_decisions", "gemini_decisions_free", "typesafe"):
        path = BUILTIN_DATA_DIR / PROVIDERS_DIR / f"{pid}.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        assert provider_configs._provider_shape_error(config, pid) is None
        assert config["id"] == pid


def test_the_gemini_models_are_kept_out_of_the_conversation_pickers(shipped_gemini):
    from saiverse import model_defaults

    assert model_defaults.is_reflex_only_model(GEMINI_PAID) is True
    assert model_defaults.is_reflex_only_model(GEMINI_FREE) is True


def test_the_free_gemini_model_never_falls_back_to_the_paid_key(shipped_gemini, monkeypatch):
    """無料のキーが無ければ、有料のキーがあってもキーの欠落として「使えなかった」。"""
    monkeypatch.delenv("GEMINI_FREE_API_KEY")
    captured = {}
    assert is_available(model_key=GEMINI_FREE, required_type="noul") is False
    with pytest.raises(ReflexJudgmentUnavailable, match="GEMINI_FREE_API_KEY"):
        evaluate(
            STATE, NOUL_QUESTIONS, timeout=2.0, backend=resolve_backend(GEMINI_FREE),
            transport=_capture(captured, {}),
        )
    assert "count" not in captured  # 何も送っていない


MIXED = {
    "n0": {
        "type": "noul",
        "instructions": "浮かぶ記憶か。",
        "criteria": {"true": "関連する", "false": "関連しない"},
    },
    "c0": {
        "type": "choice",
        "instructions": "次に何をする。",
        "options": ["walk", "stay"],
        "criteria": {"walk": "外へ出る", "later": "あとで決める"},
    },
    "s0": {
        "type": "score",
        "instructions": "どのくらい関係するか。",
        "criteria": ["無関係", "少し", "強く"],
    },
}

_GEMINI_ANSWERS = {
    "answers": {
        "n0": {"type": "noul", "noul": 0.8, "confidence": 0.6},
        "c0": {"type": "choice", "choice": "walk", "probabilities": {"walk": 0.7, "stay": 0.3},
               "confidence": 0.4},
        "s0": {"type": "score", "score": 1.2, "legend": {}, "probabilities": {}, "confidence": 0.3},
    },
    "usage": {
        "total_input_tokens": 321,
        "input_tokens_by_modality": [{"modality": "TEXT", "tokens": 321}],
        "total_output_tokens": 0,
        "total_tokens": 321,
    },
}


def test_the_gemini_request_is_converted_and_the_answers_read_back(shipped_gemini, usage_calls):
    captured = {}
    answers, usage = evaluate(
        STATE, MIXED, timeout=2.0, backend=resolve_backend(GEMINI_PAID),
        persona_id="p1", transport=_capture(captured, _GEMINI_ANSWERS),
    )

    assert captured["url"] == GEMINI_URL
    headers = captured["headers"]
    assert headers["x-goog-api-key"] == "test-gemini-paid-key"  # 接頭辞なし
    assert "authorization" not in headers
    body = captured["body"]
    assert body["model"] == "gemini-decision-1"
    assert isinstance(body["input"], str)
    assert body["input"].startswith(json.dumps(STATE, ensure_ascii=False))
    questions = body["questions"]
    assert questions["n0"] == {
        "type": "noul", "instructions": "浮かぶ記憶か。",
        "criteria": {"true_description": "関連する", "false_description": "関連しない"},
    }
    assert questions["c0"] == {
        "type": "choice", "instructions": "次に何をする。",
        "criteria": {"walk": "外へ出る", "stay": ""},
    }
    assert "options" not in questions["c0"]
    assert questions["s0"] == {
        "type": "score", "instructions": "どのくらい関係するか。",
        "criteria": ["無関係", "少し", "強く"],
    }
    # 選択肢に入らなかった choice の基準は input の最後の節へ。noul の基準は構造に入るので節に無い。
    assert "[c0] 次に何をする。\nlater: あとで決める" in body["input"]
    assert "[n0]" not in body["input"]

    assert answers == {"n0": 0.8, "c0": "walk", "s0": 1.2}
    assert usage == {"input_tokens": 321, "output_tokens": 0}
    assert len(usage_calls) == 1
    assert usage_calls[0]["model_id"] == GEMINI_PAID
    assert usage_calls[0]["input_tokens"] == 321
    assert usage_calls[0]["output_tokens"] == 0


def test_a_gemini_400_is_unavailable(shipped_gemini, usage_calls):
    captured = {}
    with pytest.raises(ReflexJudgmentUnavailable, match="HTTP 400"):
        evaluate(
            STATE, NOUL_QUESTIONS, timeout=2.0, backend=resolve_backend(GEMINI_PAID),
            transport=_capture(captured, {"error": {"code": 400}}, status=400),
        )
    assert usage_calls == []


def test_a_choice_answer_outside_the_sent_criteria_is_unavailable(shipped_gemini):
    questions = {"c0": {"type": "choice", "instructions": "どれ。", "criteria": {"a": "", "b": ""}}}
    with pytest.raises(ReflexJudgmentUnavailable, match="not one of the offered options"):
        evaluate(
            STATE, questions, timeout=2.0, backend=resolve_backend(GEMINI_PAID),
            transport=_capture({}, {"answers": {"c0": {"type": "choice", "choice": "z"}}}),
        )


@pytest.mark.parametrize("option", [
    " leading", "trailing ", "tab\there", "x" * 129, "全角　空白", "あ" * 171,
])
def test_choice_names_gemini_does_not_accept_are_refused_before_sending(shipped_gemini, option):
    captured = {}
    questions = {"c0": {"type": "choice", "instructions": "どれ。", "options": [option, "ok"]}}
    with pytest.raises(ReflexJudgmentUnavailable, match="is not accepted"):
        evaluate(
            STATE, questions, timeout=2.0, backend=resolve_backend(GEMINI_PAID),
            transport=_capture(captured, {}),
        )
    assert "count" not in captured


def test_a_choice_name_with_inner_ascii_spaces_is_accepted(shipped_gemini):
    captured = {}
    questions = {"c0": {"type": "choice", "instructions": "どれ。", "options": ["go out", "x" * 128]}}
    evaluate(
        STATE, questions, timeout=2.0, backend=resolve_backend(GEMINI_PAID),
        transport=_capture(captured, {"answers": {"c0": {"type": "choice", "choice": "go out"}}}),
    )
    assert list(captured["body"]["questions"]["c0"]["criteria"]) == ["go out", "x" * 128]


@pytest.mark.parametrize("criteria, expected, tail", [
    ({"true": "はい"}, {"true_description": "はい"}, None),
    ({"true": "はい", "hint": "補足"}, {"true_description": "はい"}, "hint: 補足"),
    ("文字列の基準", None, "文字列の基準"),
    ({"true": "", "false": None}, None, None),
])
def test_noul_criteria_that_cannot_be_mapped_go_to_the_end_of_input(
    shipped_gemini, criteria, expected, tail,
):
    captured = {}
    questions = {"n0": {"type": "noul", "instructions": "浮かぶか。", "criteria": criteria}}
    evaluate(
        STATE, questions, timeout=2.0, backend=resolve_backend(GEMINI_PAID),
        transport=_capture(captured, {"answers": {"n0": {"type": "noul", "noul": 0.5}}}),
    )
    sent = captured["body"]["questions"]["n0"]
    assert sent.get("criteria") == expected
    if tail is None:
        assert "[n0]" not in captured["body"]["input"]
    else:
        assert f"[n0] 浮かぶか。\n{tail}" in captured["body"]["input"]


def test_a_score_with_fewer_than_two_levels_is_unavailable(shipped_gemini):
    questions = {"s0": {"type": "score", "instructions": "何点。", "criteria": ["一つだけ"]}}
    with pytest.raises(ReflexJudgmentUnavailable, match="at least 2"):
        evaluate(
            STATE, questions, timeout=2.0, backend=resolve_backend(GEMINI_PAID),
            transport=_capture({}, {}),
        )


# ---------------------------------------------------------------------------
# 認証ヘッダの宣言
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_id, model_key, host, env", [
    ("typesafe", "jev-latest", "api.typesafe.ai", "TYPESAFE_API_KEY"),
    ("openrouter_systemone", "openrouter-jev-latest", "openrouter.ai", "OPENROUTER_API_KEY"),
    ("openai_decisions", "openai-decisions-gpt-6-luna", "api.openai.com", "OPENAI_API_KEY"),
])
def test_the_existing_destinations_still_send_a_bearer_authorization(
    monkeypatch, mock_provider_network, provider_id, model_key, host, env,
):
    providers, configs = _shipped_configs(provider_id, model_keys=(model_key,))
    _set_providers(monkeypatch, providers)
    _set_configs(monkeypatch, configs)
    mock_provider_network(host)
    monkeypatch.setenv(env, "test-existing-key")
    backend = resolve_backend(model_key)
    assert (backend.auth_header, backend.auth_prefix) == ("Authorization", "Bearer ")
    assert backend.supports_images is (provider_id == "openai_decisions")

    captured = {}
    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, NOUL_QUESTIONS, timeout=2.0, backend=backend,
                 transport=_capture(captured, {}))
    assert captured["headers"]["authorization"] == "Bearer test-existing-key"
    assert set(captured["headers"]) & {"x-goog-api-key", "typesafe-danger-zone-images"} == set()


def test_localjev_still_sends_no_credential(monkeypatch):
    providers, configs = _shipped_configs("localjev", model_keys=("localjev",))
    _set_providers(monkeypatch, providers)
    _set_configs(monkeypatch, configs)
    backend = resolve_backend("localjev")
    assert backend.supports_images is False
    captured = {}
    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, NOUL_QUESTIONS, timeout=2.0, backend=backend,
                 transport=_capture(captured, {}))
    assert "authorization" not in captured["headers"]


def test_a_declared_auth_header_and_prefix_are_used(monkeypatch):
    dialect = {**_CANONICAL_DIALECT, "auth_header": "X-Api-Key", "auth_prefix": "Token "}
    _set_configs(monkeypatch, {MODEL_KEY: _model_config(reflex_judgment=dialect)})
    captured = {}
    evaluate(STATE, NOUL_QUESTIONS, timeout=2.0,
             transport=_capture(captured, {"answers": {"m0": {"type": "noul", "noul": 0.1}}}))
    assert captured["headers"]["x-api-key"] == f"Token {API_KEY}"
    assert "authorization" not in captured["headers"]


@pytest.mark.parametrize("field, value", [
    ("auth_header", None), ("auth_header", 3), ("auth_header", ""), ("auth_header", "content-TYPE"),
    ("auth_prefix", None), ("auth_prefix", 0),
    ("image_request_headers", None), ("image_request_headers", "x"),
    ("image_request_headers", {"Content-Type": "image/png"}),
    ("image_request_headers", {"authorization": "x"}),
    ("image_request_headers", {"X-Flag": 1}),
    ("image_request_headers", {"X-Flag": ""}),
])
def test_unusable_header_declarations_are_refused(monkeypatch, caplog, field, value):
    dialect = {**_CANONICAL_DIALECT, field: value}
    _set_configs(monkeypatch, {MODEL_KEY: _model_config(reflex_judgment=dialect)})
    with pytest.raises(ReflexJudgmentUnavailable):
        resolve_backend()
    assert is_available() is False


def test_image_headers_may_not_override_a_custom_auth_header(monkeypatch):
    dialect = {
        **_CANONICAL_DIALECT, "auth_header": "x-goog-api-key", "auth_prefix": "",
        "image_request_headers": {"X-GOOG-API-KEY": "x"},
    }
    _set_configs(monkeypatch, {MODEL_KEY: _model_config(reflex_judgment=dialect)})
    with pytest.raises(ReflexJudgmentUnavailable, match="reserved"):
        resolve_backend()


# ---------------------------------------------------------------------------
# 画像を受け付けるかの判定
# ---------------------------------------------------------------------------


def test_a_gemini_conversation_model_without_the_field_takes_images(monkeypatch):
    """欄が無いときは会話用クライアントと同じ規則 (provider が gemini なら true)。"""
    from llm_clients.factory import supports_images

    gemini = {"model": "gemini-x", "provider": "gemini", "protocol": "gemini_native",
              "context_length": 1000}
    openai_compat = {"model": "x", "provider": "openai", "protocol": "openai_compat",
                     "context_length": 1000, "base_url": BASE_URL,
                     "api_key_env": "SAIVERSE_MODEL_COMPAT_X_API_KEY"}
    _set_configs(monkeypatch, {
        "gemini-x": gemini, "compat-x": openai_compat, MODEL_KEY: _model_config(),
    })
    assert resolve_backend("gemini-x").supports_images is True
    assert resolve_backend("compat-x").supports_images is False
    assert supports_images("gemini", gemini) is True
    assert supports_images("openai", openai_compat) is False
    assert resolve_backend(MODEL_KEY).supports_images is False  # jev 互換で欄なし


# ---------------------------------------------------------------------------
# 画像 — jev 互換の 3 形
# ---------------------------------------------------------------------------

_DECISIONS_DIALECT = {
    **_CANONICAL_DIALECT, "request_shape": "openai_decisions", "path": "/v1/decisions",
    "answer_fields": {"noul": "probability", "choice": "choice", "score": "score"},
}
_GEMINI_DIALECT = {
    **_CANONICAL_DIALECT, "request_shape": "gemini_decisions", "path": "/v1beta/decisions",
    "auth_header": "x-goog-api-key", "auth_prefix": "",
}
_SYSTEM_ONE_IMAGE_DIALECT = {
    **_CANONICAL_DIALECT, "image_request_headers": {"TypeSafe-Danger-Zone-Images": "true"},
}

_ANSWERS_BY_SHAPE = {
    "system_one": {"answers": {"m0": {"type": "noul", "noul": 0.5}}},
    "gemini_decisions": {"answers": {"m0": {"type": "noul", "noul": 0.5}}},
    "openai_decisions": {"answers": [{"type": "predicate", "name": "m0", "probability": 0.5}]},
}
_DIALECT_BY_SHAPE = {
    "system_one": _SYSTEM_ONE_IMAGE_DIALECT,
    "gemini_decisions": _GEMINI_DIALECT,
    "openai_decisions": _DECISIONS_DIALECT,
}


def _use_shape(monkeypatch, shape, *, supports_images):
    _set_configs(monkeypatch, {MODEL_KEY: _model_config(
        reflex_judgment=dict(_DIALECT_BY_SHAPE[shape]), supports_images=supports_images,
    )})


def _send(shape, images=None, state=STATE):
    captured = {}
    kwargs = {} if images is None else {"images": images}
    evaluate(state, NOUL_QUESTIONS, timeout=5.0,
             transport=_capture(captured, _ANSWERS_BY_SHAPE[shape]), **kwargs)
    return captured


@pytest.mark.parametrize("shape", ["system_one", "gemini_decisions", "openai_decisions"])
def test_a_model_that_does_not_take_images_sends_the_same_request(monkeypatch, tmp_path, shape):
    _use_shape(monkeypatch, shape, supports_images=False)
    image = _descriptor(_image_file(tmp_path, "a.png"))
    without = _send(shape)
    with_images = _send(shape, [image])
    assert with_images["body"] == without["body"]
    assert with_images["headers"] == without["headers"]


def test_gemini_images_go_after_the_text_block(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    text_only = _send("gemini_decisions")["body"]["input"]
    captured = _send("gemini_decisions", [
        _descriptor(_image_file(tmp_path, "a.png")),
        _descriptor(_image_file(tmp_path, "b.jpg", fmt="JPEG"), "image/jpeg"),
    ])
    blocks = captured["body"]["input"]
    assert blocks[0] == {"type": "text", "text": text_only}
    assert [(b["type"], b["mime_type"]) for b in blocks[1:]] == [
        ("image", "image/png"), ("image", "image/jpeg"),
    ]
    assert _decoded_size(blocks[1]["data"]) == ("PNG", (400, 300))
    assert "typesafe-danger-zone-images" not in captured["headers"]


def test_gemini_images_keep_the_criteria_section_in_the_text_block(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    questions = {"m0": {"type": "noul", "instructions": "浮かぶか。", "criteria": "文字列の基準"}}
    captured = {}
    evaluate(STATE, questions, timeout=5.0, images=[_descriptor(_image_file(tmp_path, "a.png"))],
             transport=_capture(captured, _ANSWERS_BY_SHAPE["gemini_decisions"]))
    text = captured["body"]["input"][0]["text"]
    assert text.endswith("[m0] 浮かぶか。\n文字列の基準")


def test_openai_decisions_images_go_after_input_text(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "openai_decisions", supports_images=True)
    text_only = _send("openai_decisions")["body"]["input"]
    captured = _send("openai_decisions", [_descriptor(_image_file(tmp_path, "a.png"))])
    message = captured["body"]["input"]
    assert len(message) == 1 and message[0]["role"] == "user"
    parts = message[0]["content"]
    assert parts[0] == {"type": "input_text", "text": text_only}
    assert parts[1]["type"] == "input_image"
    prefix = "data:image/png;base64,"
    assert parts[1]["image_url"].startswith(prefix)
    assert _decoded_size(parts[1]["image_url"][len(prefix):]) == ("PNG", (400, 300))


def test_system_one_images_are_registered_as_assets(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "system_one", supports_images=True)
    without = _send("system_one")
    state = dict(STATE)
    captured = _send("system_one", [
        _descriptor(_image_file(tmp_path, "a.png")),
        _descriptor(_image_file(tmp_path, "b.jpg", fmt="JPEG"), "image/jpeg"),
    ], state=state)
    body = captured["body"]
    refs = body["state"][SYSTEM_ONE_IMAGES_STATE_KEY]
    assert len(refs) == 2 and len(set(refs)) == 2
    assert set(body["assets"]) == set(refs)
    assert [body["assets"][ref]["format"] for ref in refs] == ["png", "jpeg"]
    assert all(body["assets"][ref]["type"] == "image" for ref in refs)
    # 呼び出し側の state は書き換えない
    assert state == STATE
    assert {k: v for k, v in body["state"].items() if k != SYSTEM_ONE_IMAGES_STATE_KEY} == STATE
    # opt-in のヘッダは画像を含む要求にだけ
    assert captured["headers"]["typesafe-danger-zone-images"] == "true"
    assert "typesafe-danger-zone-images" not in without["headers"]
    assert "assets" not in without["body"]


@pytest.mark.parametrize("size, expected", [
    ((100, 150), (280, 420)),        # 小さい画像は短辺を 280 まで拡大
    ((3000, 2000), (1568, 1045)),    # 大きい画像は長辺を 1568 まで縮小 (長辺 2048 の縮小のあと)
    ((500, 500), (500, 500)),        # 範囲内はそのまま
])
def test_system_one_images_are_fitted_to_the_edge_range(monkeypatch, tmp_path, size, expected):
    _use_shape(monkeypatch, "system_one", supports_images=True)
    captured = _send("system_one", [_descriptor(_image_file(tmp_path, "a.png", size=size))])
    (asset,) = captured["body"]["assets"].values()
    fmt, actual = _decoded_size(asset["data_base64"])
    # 長辺 2048 を超える画像は会話用と同じ縮小 (load_image_bytes_for_llm) で JPEG になりうる。
    # 宣言した形式と中身が一致していればよい。
    assert asset["format"] == fmt.lower()
    assert abs(actual[0] - expected[0]) <= 1 and abs(actual[1] - expected[1]) <= 1
    assert all(280 <= edge <= 1568 for edge in actual)


def test_a_system_one_image_that_cannot_fit_is_dropped(monkeypatch, tmp_path, caplog):
    _use_shape(monkeypatch, "system_one", supports_images=True)
    without = _send("system_one")
    with caplog.at_level("WARNING", logger="saiverse.reflex_judgment"):
        captured = _send("system_one", [_descriptor(_image_file(tmp_path, "wide.png", size=(2000, 100)))])
    assert captured["body"] == without["body"]
    assert captured["headers"] == without["headers"]
    assert "dropping attached image" in caplog.text


def test_a_system_one_state_without_room_skips_the_images(monkeypatch, tmp_path, caplog):
    _use_shape(monkeypatch, "system_one", supports_images=True)
    state = {**STATE, SYSTEM_ONE_IMAGES_STATE_KEY: "caller's own"}
    captured = _send("system_one", [_descriptor(_image_file(tmp_path, "a.png"))], state=state)
    assert captured["body"]["state"] == state
    assert "assets" not in captured["body"]


# ---------------------------------------------------------------------------
# 画像の整え
# ---------------------------------------------------------------------------


def test_unreadable_images_are_dropped_and_the_rest_is_sent(monkeypatch, tmp_path, caplog):
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"this is not an image")
    missing = tmp_path / "missing.png"
    with caplog.at_level("WARNING", logger="saiverse.reflex_judgment"):
        captured = _send("gemini_decisions", [
            _descriptor(broken), _descriptor(missing), _descriptor(_image_file(tmp_path, "ok.png")),
        ])
    blocks = captured["body"]["input"]
    assert [b["type"] for b in blocks] == ["text", "image"]
    assert "broken.png" in caplog.text and "missing.png" in caplog.text


def test_when_every_image_is_dropped_the_request_is_the_text_only_one(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "openai_decisions", supports_images=True)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG not really")
    without = _send("openai_decisions")
    captured = _send("openai_decisions", [_descriptor(broken)])
    assert captured["body"] == without["body"]
    assert captured["headers"] == without["headers"]


def test_a_gif_is_redrawn_as_png(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    gif = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (64, 48), color) for color in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:])
    # 宣言の MIME が実物と食い違っていても、Pillow で開いて形式を決める
    for mime in ("image/gif", "image/png"):
        captured = _send("gemini_decisions", [_descriptor(gif, mime)])
        image = captured["body"]["input"][1]
        assert image["mime_type"] == "image/png"
        assert _decoded_size(image["data"]) == ("PNG", (64, 48))


def test_a_webp_is_redrawn_as_png(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    path = _image_file(tmp_path, "a.webp", fmt="WEBP")
    captured = _send("gemini_decisions", [_descriptor(path, "image/webp")])
    assert captured["body"]["input"][1]["mime_type"] == "image/png"


def test_an_image_rejected_by_the_api_is_unavailable_without_a_text_retry(monkeypatch, tmp_path):
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    captured = {}
    with pytest.raises(ReflexJudgmentUnavailable, match="HTTP 400"):
        evaluate(STATE, NOUL_QUESTIONS, timeout=5.0,
                 images=[_descriptor(_image_file(tmp_path, "a.png"))],
                 transport=_capture(captured, {"error": "bad image"}, status=400))
    assert captured["count"] == 1


def test_images_are_prepared_inside_the_deadline(monkeypatch, tmp_path):
    """画像の整えはワーカーの中 = 締切の計測の内側 (遅い整えは締切超過になる)。"""
    import time as _time

    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    real = reflex_judgment._prepare_images

    def slow(*args, **kwargs):
        _time.sleep(1.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(reflex_judgment, "_prepare_images", slow)
    with pytest.raises(ReflexJudgmentUnavailable) as excinfo:
        evaluate(STATE, NOUL_QUESTIONS, timeout=0.5,
                 images=[_descriptor(_image_file(tmp_path, "a.png"))],
                 transport=_capture({}, _ANSWERS_BY_SHAPE["gemini_decisions"]))
    assert excinfo.value.kind == reflex_judgment.UNAVAILABLE_DEADLINE


# ---------------------------------------------------------------------------
# 画像 — 通常の LLM の道
# ---------------------------------------------------------------------------

LLM_MODEL_KEY = "test-llm-judge"
LLM_KEY_ENV = "SAIVERSE_MODEL_TEST_LLM_JUDGE_API_KEY"


def _llm_config(**overrides):
    config = {
        "model": "vendor/judge-lite",
        "protocol": "openai_compat",
        "provider": "openai",
        "context_length": 32000,
        "base_url": BASE_URL,
        "api_key_env": LLM_KEY_ENV,
    }
    config.update(overrides)
    return config


class _RecordingClient:
    """偽の LLM クライアント。generate の中で ``on_generate`` を呼べる (一時ファイルが
    まだある間にメッセージを調べるため)。"""

    def __init__(self, on_generate=None):
        self.calls = []
        self.on_generate = on_generate

    def prefer_minimal_reasoning(self):
        pass

    def generate(self, messages, tools=None, response_schema=None, **kwargs):
        self.calls.append(json.loads(json.dumps(messages, default=str)))
        if self.on_generate is not None:
            self.on_generate(messages)
        return {"answers": [{"qid": "m0", "noul": 0.5}]}

    def consume_usage(self):
        return None


def _use_llm(monkeypatch, client, **config):
    monkeypatch.setenv(ROLE_ENV, LLM_MODEL_KEY)
    monkeypatch.setenv(LLM_KEY_ENV, API_KEY)
    _set_configs(monkeypatch, {LLM_MODEL_KEY: _llm_config(**config)})
    monkeypatch.setattr("llm_clients.get_llm_client", lambda *a, **k: client)


def test_an_llm_that_does_not_take_images_gets_no_image_metadata(monkeypatch, tmp_path):
    client = _RecordingClient()
    _use_llm(monkeypatch, client, supports_images=False)
    evaluate(STATE, NOUL_QUESTIONS, timeout=5.0)
    evaluate(STATE, NOUL_QUESTIONS, timeout=5.0, images=[_descriptor(_image_file(tmp_path, "a.png"))])
    assert client.calls[0] == client.calls[1]
    assert "metadata" not in client.calls[1][0]


def test_an_llm_that_takes_images_gets_them_in_metadata(monkeypatch, tmp_path):
    from saiverse.media_utils import iter_image_media

    seen = {}

    def inspect(messages):
        items = iter_image_media(messages[0]["metadata"])
        seen["items"] = items
        seen["bytes"] = [item["path"].read_bytes() for item in items]

    client = _RecordingClient(inspect)
    _use_llm(monkeypatch, client, supports_images=True)
    gif = tmp_path / "a.gif"
    Image.new("RGB", (32, 32)).save(gif, format="GIF")
    evaluate(STATE, NOUL_QUESTIONS, timeout=5.0, images=[
        _descriptor(_image_file(tmp_path, "a.png")), _descriptor(gif, "image/gif"),
    ])
    message = client.calls[0][0]
    assert message["role"] == "user"
    assert message["metadata"]["__skip_image_summary__"] is True
    # 整えた画像 (GIF は PNG に描き直したもの) の一時ファイルがパスで渡り、元の URI は載らない
    assert [item["mime_type"] for item in seen["items"]] == ["image/png", "image/png"]
    assert all(data.startswith(b"\x89PNG") for data in seen["bytes"])
    assert all("uri" not in entry for entry in message["metadata"]["images"])
    # generate が終わったら一時ファイルは消える
    assert not any(item["path"].exists() for item in seen["items"])


def test_the_skip_flag_keeps_the_clients_from_generating_summaries(monkeypatch, tmp_path):
    """印があれば、画像を埋め込めないクライアントの組み立てでも要約の生成は呼ばれない。

    対照として、印を外した同じメッセージでは要約の生成が呼ばれることも確かめる
    (このテストが空振りしていないことの確認)。
    """
    from llm_clients.openai_message_preparer import prepare_openai_messages

    summary_calls = []

    def fake_summary(path, mime_type):
        summary_calls.append(path)
        return "要約"

    monkeypatch.setattr("saiverse.media_summary.ensure_image_summary", fake_summary)
    outcome = {}

    def inspect(messages):
        prepare_openai_messages(messages, supports_images=False)
        outcome["with_flag"] = len(summary_calls)
        unflagged = [dict(messages[0], metadata={
            key: value for key, value in messages[0]["metadata"].items()
            if key != "__skip_image_summary__"
        })]
        prepare_openai_messages(unflagged, supports_images=False)
        outcome["without_flag"] = len(summary_calls) - outcome["with_flag"]

    _use_llm(monkeypatch, _RecordingClient(inspect), supports_images=True)
    evaluate(STATE, NOUL_QUESTIONS, timeout=5.0, images=[_descriptor(_image_file(tmp_path, "a.png"))])
    assert outcome == {"with_flag": 0, "without_flag": 1}


def test_all_images_dropped_on_the_llm_path_sends_the_text_only_message(monkeypatch, tmp_path):
    client = _RecordingClient()
    _use_llm(monkeypatch, client, supports_images=True)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"nope")
    evaluate(STATE, NOUL_QUESTIONS, timeout=5.0)
    evaluate(STATE, NOUL_QUESTIONS, timeout=5.0, images=[_descriptor(broken)])
    assert client.calls[0] == client.calls[1]


# ---------------------------------------------------------------------------
# 自動想起 — どの画像を判断に渡すか
# ---------------------------------------------------------------------------


def _image_meta(path):
    return {"images": [{"uri": str(path), "path": str(path), "mime_type": "image/png"}]}


def _perception_with_image(path):
    from sea.eviction_plan import CONSUMED_PERCEPTION_KEY

    return {
        "role": "system", "content": "部屋の様子",
        "metadata": {
            "__system_notice__": "perception", CONSUMED_PERCEPTION_KEY: True,
            **_image_meta(path),
        },
    }


@pytest.mark.parametrize("case", [
    "latest_user", "image_only_user", "no_new_user", "perception_only", "past_user",
])
def test_auto_recall_passes_only_the_new_user_utterances_images(tmp_path, case):
    from sea.auto_recall import _reflex_images

    now = _image_file(tmp_path, "now.png")
    old = _image_file(tmp_path, "old.png")
    room = _image_file(tmp_path, "room.png")
    if case == "latest_user":
        messages = [
            {"role": "user", "content": "前の", "metadata": _image_meta(old)},
            {"role": "assistant", "content": "うん"},
            _perception_with_image(room),
            {"role": "user", "content": "これ見て", "metadata": _image_meta(now)},
        ]
        expected = [now]
    elif case == "image_only_user":
        messages = [
            {"role": "assistant", "content": "どうぞ"},
            {"role": "user", "content": "", "metadata": _image_meta(now)},
        ]
        expected = [now]
    elif case == "no_new_user":
        messages = [
            {"role": "user", "content": "これ見て", "metadata": _image_meta(old)},
            {"role": "assistant", "content": "かわいいね"},
        ]
        expected = []
    elif case == "perception_only":
        messages = [
            {"role": "assistant", "content": "ふむ"},
            _perception_with_image(room),
        ]
        expected = []
    else:  # past_user: 新しいユーザー発言はあるが画像は無い
        messages = [
            {"role": "user", "content": "前の", "metadata": _image_meta(old)},
            {"role": "assistant", "content": "うん"},
            {"role": "user", "content": "続きだけど"},
        ]
        expected = []
    assert [item["path"] for item in _reflex_images(messages)] == expected


def test_auto_recall_hands_the_images_to_evaluate(monkeypatch, tmp_path):
    """_run_reflex_rerank は画像を evaluate に渡し、画像の無いターンは従来の呼び出しのまま。"""
    from sai_memory.unified_recall import RecallHit
    from sea import auto_recall

    monkeypatch.setenv(ROLE_ENV, MODEL_KEY)
    calls = []

    def fake_evaluate(state, questions, **kwargs):
        calls.append(kwargs)
        return {qid: 0.9 for qid in questions}, {}

    hit = RecallHit(
        source_type="fragment", source_id="f1", title="記憶", content="内容", score=0.01,
        uri="saiverse://self/fragment/f1", embed_score=0.9, chronicle_entry_id=None,
    )
    now = _image_file(tmp_path, "now.png")
    with patch("saiverse.reflex_judgment.evaluate", fake_evaluate):
        auto_recall._run_reflex_rerank(
            [hit], [{"role": "user", "content": "これ見て", "metadata": _image_meta(now)}],
            accepted_keys=set(), context_ids=set(), persona_id="p1",
        )
        auto_recall._run_reflex_rerank(
            [hit], [{"role": "user", "content": "文字だけ"}],
            accepted_keys=set(), context_ids=set(), persona_id="p1",
        )
    assert [item["path"] for item in calls[0]["images"]] == [now]
    assert "images" not in calls[1]


def test_at_most_four_images_are_sent_to_one_judgment(monkeypatch, tmp_path):
    # 1 回の発言に画像が何枚付いていても、判断 1 回に送るのは渡された順の先頭 4 枚まで
    # (intent §8。全 request_shape と通常の LLM に同じ上限)。
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    descriptors = [
        _descriptor(_image_file(tmp_path, f"i{n}.png", size=(300 + n, 300)))
        for n in range(6)
    ]
    blocks = _send("gemini_decisions", descriptors)["body"]["input"]
    sizes = [_decoded_size(b["data"])[1] for b in blocks[1:]]
    assert sizes == [(300, 300), (301, 300), (302, 300), (303, 300)]


def test_images_beyond_the_total_byte_cap_are_dropped(monkeypatch, tmp_path):
    # 整えた後の合計が上限を超える画像はその 1 枚だけ落とし、後ろの小さい画像は試す。
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    small = _descriptor(_image_file(tmp_path, "small.png", size=(300, 300)))
    big_path = tmp_path / "big.png"
    # 単色だと PNG が小さく圧縮されるので、乱数の画素で圧縮の効かない画像にする
    Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3)).save(big_path, format="PNG")
    big = _descriptor(big_path)
    small_len = len(base64.b64decode(
        _send("gemini_decisions", [small])["body"]["input"][1]["data"]
    ))
    monkeypatch.setattr(reflex_judgment, "_MAX_REFLEX_IMAGE_BYTES", small_len * 2 + 10)
    blocks = _send("gemini_decisions", [small, big, small])["body"]["input"]
    sizes = [_decoded_size(b["data"])[1] for b in blocks[1:]]
    assert sizes == [(300, 300), (300, 300)]


def test_an_image_with_too_many_pixels_is_dropped_before_decoding(monkeypatch, tmp_path):
    # 寸法の巨大な画像は、ヘッダだけを読んで落とし、全デコード (load_image_bytes_for_llm)
    # に進まない。1 ビットの画像なら作るのも保存するのも軽い。
    _use_shape(monkeypatch, "gemini_decisions", supports_images=True)
    huge = tmp_path / "huge.png"
    Image.new("1", (9000, 9000), 0).save(huge, format="PNG")
    small = _descriptor(_image_file(tmp_path, "small.png", size=(300, 300)))

    import saiverse.media_utils as media_utils

    real_load = media_utils.load_image_bytes_for_llm
    loaded = []

    def spy(path, mime_type, max_bytes=None):
        loaded.append(Path(path).name)
        return real_load(path, mime_type, max_bytes)

    monkeypatch.setattr(media_utils, "load_image_bytes_for_llm", spy)
    blocks = _send("gemini_decisions", [_descriptor(huge), small])["body"]["input"]
    assert [_decoded_size(b["data"])[1] for b in blocks[1:]] == [(300, 300)]
    assert loaded == ["small.png"]
