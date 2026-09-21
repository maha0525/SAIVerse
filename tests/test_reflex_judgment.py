"""反射判断の共通層 (saiverse/reflex_judgment.py) のユニットテスト。

実 API は絶対に呼ばない。httpx.MockTransport でリクエスト/レスポンスを差し替え、
API キーは monkeypatch で偽値を入れ、モデル設定は偽の MODEL_CONFIGS を差し込む。

固定する契約:
- 答える側はモデルの役割「反射判断」への割り当てで決まる (env
  ``SAIVERSE_REFLEX_JUDGMENT_MODEL`` → モデル設定 → provider の protocol)。
  protocol が ``jev_compat`` なら System One の形をそのまま送り、それ以外の
  通常の LLM なら質問をプロンプトへ変換して構造化出力で答えさせる。
- 宛先・キーの env 名・応答の欄の名前・対応する型は設定ファイルの宣言から取る
  (コードに提供元別の分岐を置かない)。
- リクエスト: {"state", "model", "questions": {qid: {"type", "instructions", "criteria"}}}
- 成功は「要求した全 qid に妥当な答えが返った」ときだけ。部分回答・型違い・
  範囲外は ReflexJudgmentUnavailable。
- 器 (実経過時間の締切・同時 4 本・キーの伏せ字) は typesafe_client から引き継いだ形。
- 使用量は既存の記帳へ「モデル設定キー名義」で載る。
"""

import json
import threading
import time
from unittest.mock import patch

import httpx
import pytest

from saiverse import reflex_judgment
from saiverse.reflex_judgment import (
    ReflexJudgmentUnavailable,
    _mask_secret,
    evaluate,
    is_available,
    resolve_backend,
)

MODEL_KEY = "test-jev"
ROLE_ENV = "SAIVERSE_REFLEX_JUDGMENT_MODEL"
#: この偽モデル自身の名前空間のキー (saiverse/provider_security.py の
#: ``model_credential_env("test-jev")``)。宛先と組で使ってよいキーの正常形なので、
#: 答える側の解決が通す provider 照合に素通りで掛けられる。
KEY_ENV = "SAIVERSE_MODEL_TEST_JEV_API_KEY"
#: 偽の宛先。照合 (provider_security.validate_provider_url) は名前解決まで行うので、
#: 実在しないホスト名ではなくループバックを使う。
BASE_URL = "http://127.0.0.1:8088"

#: 同梱の TypeSafe 公式と同じ形の偽モデル定義 (provider_ref は解決済みの姿で渡す —
#: model_configs の読み込みが provider の欄をモデルへ畳み込んだ後の形)。
_CANONICAL_DIALECT = {
    "path": "/v1/systemone",
    "answers_key": "answers",
    "usage_key": "usage",
    "answer_fields": {"noul": "noul", "choice": "choice", "score": "score"},
    "usage_fields": {"input_tokens": "input_tokens", "output_tokens": "output_tokens"},
    "supported_types": ["noul", "choice", "score"],
}


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


QUESTIONS = {
    "m0": {
        "type": "noul",
        "instructions": "`memories.m0` は今の話に浮かぶ記憶か。",
        "criteria": {"true": "関連する", "false": "関連しない"},
    },
    "m1": {
        "type": "noul",
        "instructions": "`memories.m1` は今の話に浮かぶ記憶か。",
        "criteria": {"true": "関連する", "false": "関連しない"},
    },
}

STATE = {"conversation": [{"role": "user", "text": "アイフィの話"}], "memories": {}}


@pytest.fixture(autouse=True)
def _role_and_key(monkeypatch):
    """役割に偽モデルを割り当て、その宛先のキーを偽値で埋める。"""
    from saiverse import model_configs

    monkeypatch.setenv(ROLE_ENV, MODEL_KEY)
    monkeypatch.setenv(KEY_ENV, "test-key-not-real")
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {MODEL_KEY: _model_config()})


@pytest.fixture(autouse=True)
def usage_calls(monkeypatch):
    """記帳の入口 (UsageTracker) を差し替えて、渡った引数を集める。

    本番の UsageTracker を素通りさせないためでもある (設定されていなければ捨てる
    実装だが、テストが本番の singleton を触ること自体を避ける)。
    """
    calls = []

    class _Tracker:
        def record_usage(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("saiverse.usage_tracker.get_usage_tracker", lambda: _Tracker())
    return calls


@pytest.fixture(autouse=True)
def _clear_outcome_history():
    """直近の呼び出しの記録 (プロセス内) をテスト間で持ち越さない。

    設定画面の警告はこの記録から作られるので、ここで積んだ結果がそのまま他の
    テストファイルの「警告は空のはず」を壊す (同じワーカーで走ると実際に起きる)。
    """
    reflex_judgment.reset_recent_outcomes()
    yield
    reflex_judgment.reset_recent_outcomes()


@pytest.fixture(autouse=True)
def _drain_workers():
    """テストが置き去りにしたワーカーを次のテストへ持ち越さない。

    締切超過のテストはワーカーを走らせたまま戻る。ワーカーが終わるまで同時実行
    セマフォの枠を掴んだままなので、テスト間で枠が漏れないようここで回収する。
    """
    yield
    _join_workers()


def _join_workers(timeout=10.0):
    """居残っている反射判断のワーカースレッドが終わるまで待つ。"""
    for thread in threading.enumerate():
        if thread.name == reflex_judgment._WORKER_THREAD_NAME:
            thread.join(timeout)


def _transport(handler):
    return httpx.MockTransport(handler)


def _all_answered(*qids, noul=0.5):
    """要求された全 qid に妥当な答えを返す応答ボディ (契約を満たす最小形)。"""
    return {"answers": {qid: {"type": "noul", "noul": noul} for qid in qids}}


def _set_configs(monkeypatch, configs):
    from saiverse import model_configs

    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", configs)


#: 同梱の 3 モデルと同じ「provider_ref で宛先とキーを受け継ぐ」形を作るための偽 provider。
PROVIDER_ID = "test-jev-provider"


def _provider(**overrides):
    from saiverse.provider_configs import SOURCE_BUILTIN

    provider = {
        "id": PROVIDER_ID,
        "protocol": "jev_compat",
        "base_url": BASE_URL,
        "api_key_env": KEY_ENV,
        "source": SOURCE_BUILTIN,
        "reflex_judgment": dict(_CANONICAL_DIALECT),
    }
    provider.update(overrides)
    return provider


def _set_providers(monkeypatch, providers):
    from saiverse import provider_configs

    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", providers)


# ---------------------------------------------------------------------------
# 答える側の解決 (役割 → モデル設定 → provider の宣言)
# ---------------------------------------------------------------------------


def test_backend_is_built_from_the_role_assignment_and_the_declaration():
    backend = resolve_backend()
    assert backend.model_key == MODEL_KEY          # 記帳はこの名義
    assert backend.api_model == "jev-latest"       # リクエストに載るのは API 側の名前
    assert backend.url == f"{BASE_URL}/v1/systemone"
    assert backend.api_key_env == KEY_ENV
    assert backend.supported_types == frozenset({"noul", "choice", "score"})
    assert is_available() is True


def test_unassigned_role_is_unavailable(monkeypatch):
    monkeypatch.delenv(ROLE_ENV, raising=False)
    with pytest.raises(ReflexJudgmentUnavailable):
        resolve_backend()
    assert is_available() is False


def test_missing_model_config_is_unavailable(monkeypatch):
    _set_configs(monkeypatch, {})
    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        resolve_backend()
    assert MODEL_KEY in str(exc.value)
    assert is_available() is False


def test_an_ordinary_llm_resolves_as_the_answering_side(monkeypatch):
    """通常の LLM プロトコルのモデルも答える側として解決できる (第 2 段の変換層)。

    宛先の URL はここでは見ない — native 系の provider は ``base_url`` を持たない
    ことがあり、宛先を知っているのは LLM クライアント側。答えられる型は 3 型すべて。
    """
    _set_configs(monkeypatch, {
        MODEL_KEY: {"model": "gemini-x", "protocol": "gemini_native", "provider": "gemini"},
    })
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")

    backend = resolve_backend()
    assert backend.kind == reflex_judgment.REFLEX_KIND_LLM
    assert backend.api_model == "gemini-x"
    assert backend.supported_types == frozenset({"noul", "choice", "score"})
    # この欄は設定ファイルの宣言を写すだけ。通常の LLM のキー検査はこれを見ず、
    # is_model_available (provider の既定キー名まで知っている) に任せる。
    assert backend.api_key_required is False
    assert is_available() is True


def test_dialect_is_taken_from_the_declaration(monkeypatch):
    """宛先の path・応答の欄の名前は宣言から取る (提供元別の分岐を作らない)。"""
    _set_configs(monkeypatch, {
        MODEL_KEY: _model_config(
            base_url="http://127.0.0.1:9099",
            reflex_judgment={
                "path": "/api/alpha/decisions",
                "answers_key": "decisions",
                "usage_key": "meta",
                "answer_fields": {"noul": "probability"},
                "usage_fields": {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"},
                "supported_types": ["noul"],
            },
        ),
    })
    backend = resolve_backend()
    assert backend.url == "http://127.0.0.1:9099/api/alpha/decisions"
    assert backend.answers_key == "decisions"
    assert backend.answer_fields["noul"] == "probability"
    assert backend.supported_types == frozenset({"noul"})


def test_declared_dialect_is_honored_end_to_end(monkeypatch):
    _set_configs(monkeypatch, {
        MODEL_KEY: _model_config(
            reflex_judgment={
                "path": "/api/alpha/decisions",
                "answers_key": "decisions",
                "usage_key": "meta",
                "answer_fields": {"noul": "probability"},
                "usage_fields": {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"},
                "supported_types": ["noul"],
            },
        ),
    })
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "decisions": {"m0": {"type": "noul", "probability": 0.9},
                          "m1": {"type": "noul", "probability": 0.1}},
            "meta": {"prompt_tokens": 20, "completion_tokens": 3},
        })

    answers, usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert captured["url"] == f"{BASE_URL}/api/alpha/decisions"
    assert answers == {"m0": 0.9, "m1": 0.1}
    assert usage == {"input_tokens": 20, "output_tokens": 3}


def test_unsupported_question_type_is_unavailable(monkeypatch):
    """答える側が対応しない型が混ざったら、質問ひとまとまりごと不成立にする。"""
    _set_configs(monkeypatch, {
        MODEL_KEY: _model_config(
            reflex_judgment={**_CANONICAL_DIALECT, "supported_types": ["choice"]},
        ),
    })

    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the destination must not be called for an unsupported type")

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


# ---------------------------------------------------------------------------
# キーと宛先の組の照合 (通常の会話クライアントと同じ関所を通る)
# ---------------------------------------------------------------------------


def test_the_shipped_provider_ref_shape_resolves(monkeypatch):
    """同梱の 3 モデルと同じ「provider_ref で宛先とキーを受け継ぐ」形は通る。"""
    _set_providers(monkeypatch, {PROVIDER_ID: _provider()})
    _set_configs(monkeypatch, {MODEL_KEY: _model_config(provider_ref=PROVIDER_ID)})

    backend = resolve_backend()
    assert backend.url == f"{BASE_URL}/v1/systemone"
    assert backend.api_key_env == KEY_ENV
    assert is_available() is True


def test_a_model_may_not_pair_another_credential_with_its_provider(monkeypatch, caplog):
    """provider が宣言したのと違うキーの env 名をモデルが名乗ったら、使えない。

    これを素通りさせると、拡張データや user_data のモデル定義が ``api_key_env`` に
    他所のキー (例: 公式の OPENAI_API_KEY) の名前を書くだけで、その値を自分の宛先へ
    送れてしまう。通常の会話クライアントは接続を作る前にこの照合を必ず通る
    (llm_clients/factory.py) ので、反射判断も同じ関所を通す。
    """
    caplog.set_level("WARNING", logger="saiverse.reflex_judgment")
    _set_providers(monkeypatch, {PROVIDER_ID: _provider()})
    _set_configs(monkeypatch, {
        MODEL_KEY: _model_config(provider_ref=PROVIDER_ID, api_key_env="OPENAI_API_KEY"),
    })

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        resolve_backend()
    assert "OPENAI_API_KEY" in str(exc.value)
    assert is_available() is False
    assert "credential/destination" in caplog.text


def test_a_direct_destination_may_not_borrow_a_shipped_credential(monkeypatch):
    """自分で宛先を名乗るモデルが、どの provider とも結ばれていないキーを使えない。"""
    _set_providers(monkeypatch, {PROVIDER_ID: _provider()})
    _set_configs(monkeypatch, {MODEL_KEY: _model_config(api_key_env="OPENAI_API_KEY")})

    with pytest.raises(ReflexJudgmentUnavailable):
        resolve_backend()
    assert is_available() is False


# ---------------------------------------------------------------------------
# 答えられる質問の型 (呼び出し側が投げる型に答えられるか)
# ---------------------------------------------------------------------------


def test_the_first_stage_type_is_answered_by_the_canonical_shape():
    assert is_available(required_type=reflex_judgment.FIRST_STAGE_QUESTION_TYPE) is True


def test_a_destination_that_cannot_answer_the_asked_type_is_unavailable(monkeypatch, caplog):
    """choice にしか答えない宛先は、noul を投げる仕事にとっては「使えない」。

    ここで True を返すと、呼び出し側 (自動想起) は候補の集め方だけを広げたまま
    毎ターン判定に失敗する — 劣化した状態が恒常化する。
    """
    caplog.set_level("WARNING", logger="saiverse.reflex_judgment")
    _set_configs(monkeypatch, {
        MODEL_KEY: _model_config(
            reflex_judgment={**_CANONICAL_DIALECT, "supported_types": ["choice"]},
        ),
    })

    # 型を問わなければ宛先としては解決できる (器としては生きている)。
    assert is_available() is True
    assert is_available(required_type="noul") is False
    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        resolve_backend(required_type="noul")
    assert "noul" in str(exc.value)
    assert "noul" in caplog.text


# ---------------------------------------------------------------------------
# リクエストと応答の契約
# ---------------------------------------------------------------------------


def test_parses_noul_answers_and_usage():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "m0": {"type": "noul", "noul": 0.92},
                    "m1": {"type": "noul", "noul": 0.08},
                },
                "usage": {"input_tokens": 312, "output_tokens": 48},
            },
        )

    answers, usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert answers == {"m0": 0.92, "m1": 0.08}
    assert usage == {"input_tokens": 312, "output_tokens": 48}


def test_request_shape_and_authorization_header():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert captured["method"] == "POST"
    assert captured["url"] == f"{BASE_URL}/v1/systemone"
    assert captured["auth"] == "Bearer test-key-not-real"

    body = captured["body"]
    assert body["model"] == "jev-latest"
    assert body["state"] == STATE
    assert set(body["questions"]) == {"m0", "m1"}
    q0 = body["questions"]["m0"]
    assert q0["type"] == "noul"
    assert q0["instructions"] == QUESTIONS["m0"]["instructions"]
    assert q0["criteria"] == {"true": "関連する", "false": "関連しない"}


def test_question_type_defaults_to_noul():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_all_answered("m0"))

    evaluate(STATE, {"m0": {"instructions": "型を省いた質問"}}, timeout=2.5,
             transport=_transport(handler))
    assert captured["body"]["questions"]["m0"]["type"] == "noul"


def test_criteria_omitted_when_absent():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_all_answered("m0"))

    evaluate(STATE, {"m0": {"instructions": "質問だけ"}}, timeout=2.5,
             transport=_transport(handler))
    assert "criteria" not in captured["body"]["questions"]["m0"]


def test_choice_and_score_pass_through_the_same_vessel():
    """第 1 段の利用者は noul だけだが、器は 3 型とも通す。"""
    def handler(request):
        return httpx.Response(200, json={"answers": {
            "c0": {"type": "choice", "choice": "walk"},
            "s0": {"type": "score", "score": 7.5},
        }})

    answers, _usage = evaluate(
        STATE,
        {
            "c0": {"type": "choice", "instructions": "どれを選ぶ", "options": ["walk", "stay"]},
            "s0": {"type": "score", "instructions": "何点か"},
        },
        timeout=2.5, transport=_transport(handler),
    )
    assert answers == {"c0": "walk", "s0": 7.5}


def test_a_choice_outside_the_offered_options_is_unavailable():
    """選択肢を渡した質問の答えが、その中のどれでもなければ不成立。

    呼び出し側は選択肢で場合分けするので、外の答えは「判定できた」にできない。
    規則は jev 互換の道と変換層の道で共通 (検算が 1 箇所にある)。
    """
    def handler(request):
        return httpx.Response(200, json={"answers": {"c0": {"type": "choice", "choice": "fly"}}})

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(
            STATE,
            {"c0": {"type": "choice", "instructions": "どれを選ぶ", "options": ["walk", "stay"]}},
            timeout=2.5, transport=_transport(handler),
        )
    assert "options" in str(exc.value)


def test_a_choice_without_offered_options_is_free_text():
    """選択肢を渡していない質問は、空でない文字列ならそのまま通す (従来どおり)。"""
    def handler(request):
        return httpx.Response(200, json={"answers": {"c0": {"type": "choice", "choice": "fly"}}})

    answers, _usage = evaluate(
        STATE, {"c0": {"type": "choice", "instructions": "どれを選ぶ"}},
        timeout=2.5, transport=_transport(handler),
    )
    assert answers == {"c0": "fly"}


def test_usage_absent_is_empty_dict_not_an_error():
    def handler(request):
        return httpx.Response(200, json=_all_answered("m0", "m1", noul=0.7))

    answers, usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert answers == {"m0": 0.7, "m1": 0.7}
    assert usage == {}


def test_missing_answer_qid_raises_unavailable():
    """部分回答は成功にしない (欠けた qid が静かに不採用になるのを防ぐ)。"""
    def handler(request):
        return httpx.Response(200, json={"answers": {"m0": {"type": "noul", "noul": 0.7}}})

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert "m1" in str(exc.value)


@pytest.mark.parametrize(
    "bad_answer_json",
    [
        '{"type": "noul", "noul": "たぶん"}',  # 数値でない
        '{"type": "noul", "noul": true}',      # bool は数値として認めない
        '{"type": "noul", "noul": 1.5}',       # 0〜1 の外
        '{"type": "noul", "noul": -0.1}',      # 0〜1 の外
        '{"type": "noul", "noul": NaN}',       # 非有限 (json.loads は NaN を受ける)
        '{"type": "choice", "choice": "yes"}',  # 型が noul でない
        '{"type": "noul"}',                    # noul キー自体が無い
    ],
    ids=["string", "bool", "above_one", "negative", "nan", "wrong_type", "no_noul_key"],
)
def test_invalid_noul_raises_unavailable(bad_answer_json):
    # NaN は httpx の json= が拒否する (allow_nan=False) ので、本文は生テキストで組む。
    body = (
        '{"answers": {'
        f'"m0": {bad_answer_json}, '
        '"m1": {"type": "noul", "noul": 0.3}}}'
    )

    def handler(request):
        return httpx.Response(200, text=body, headers={"Content-Type": "application/json"})

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_missing_api_key_raises_unavailable(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "   ")

    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the destination must not be called without a key")

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_missing_api_key_makes_it_unavailable(monkeypatch, caplog):
    """キーが空なら「いま呼べる」とは答えない (理由も 1 件 WARNING に出す)。

    呼び出し側はこの True を「反射判断で選ぶ前提」として候補の集め方まで広げる
    (sea/auto_recall.py)。キー欠落を evaluate まで持ち越すと、母集団を広げた後で
    判定だけが落ち、「従来方式へ戻った」というログと実際の挙動が食い違う。
    """
    caplog.set_level("WARNING", logger="saiverse.reflex_judgment")
    monkeypatch.setenv(KEY_ENV, "   ")

    assert is_available() is False
    assert KEY_ENV in caplog.text


def test_keyless_destination_is_available_without_a_key(monkeypatch):
    """認証しない宛先 (localjev など) は、キーが無くても使える側のまま。"""
    _set_configs(monkeypatch, {
        MODEL_KEY: _model_config(
            base_url="http://127.0.0.1:8088", api_key_env=None, api_key_required=False,
        ),
    })
    monkeypatch.delenv(KEY_ENV, raising=False)

    assert is_available() is True


def test_keyless_destination_sends_no_authorization(monkeypatch):
    """認証しない宛先 (localjev) はキーが無くても使える。ヘッダも付けない。"""
    _set_configs(monkeypatch, {
        MODEL_KEY: _model_config(
            base_url="http://127.0.0.1:8088", api_key_env=None, api_key_required=False,
        ),
    })
    monkeypatch.delenv(KEY_ENV, raising=False)
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    answers, _usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert captured["auth"] is None
    assert set(answers) == {"m0", "m1"}


def test_non_200_raises_unavailable():
    def handler(request):
        return httpx.Response(500, text="internal error")

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert "500" in str(exc.value)


def test_timeout_raises_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=0.1, transport=_transport(handler))
    assert "ReadTimeout" in str(exc.value)


def test_connection_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_invalid_json_raises_unavailable():
    def handler(request):
        return httpx.Response(200, text="not json at all")

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_response_without_answers_object_raises_unavailable():
    def handler(request):
        return httpx.Response(200, json={"model": "jev-latest"})

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_empty_questions_short_circuits_without_request():
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the destination must not be called with no questions")

    answers, usage = evaluate(STATE, {}, timeout=2.5, transport=_transport(handler))
    assert answers == {}
    assert usage == {}


def test_question_without_instructions_is_a_caller_error():
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the destination must not be called with a malformed question")

    with pytest.raises(ValueError):
        evaluate(STATE, {"m0": {"criteria": {}}}, timeout=2.5, transport=_transport(handler))


def test_question_that_is_not_a_mapping_is_a_caller_error():
    """辞書でない質問も呼び出し側のバグ (ValueError) として返す。

    素通しすると ``.get`` の AttributeError が文書化された契約 (呼び出し側のバグ =
    ValueError / 実行時障害 = ReflexJudgmentUnavailable) の外から漏れる。
    ``ReflexJudgmentUnavailable`` へ正規化もしない — 呼び出し側のバグを「外部 API が
    落ちていた」に化かすと、フォールバックに隠れて永久に気づかれない。
    """
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the destination must not be called with a malformed question")

    with pytest.raises(ValueError) as exc:
        evaluate(STATE, {"m0": "関連しているか"}, timeout=2.5, transport=_transport(handler))
    assert "m0" in str(exc.value)

    with pytest.raises(ValueError):
        evaluate(STATE, {"m0": None}, timeout=2.5, transport=_transport(handler))


def test_unknown_question_type_is_a_caller_error():
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the destination must not be called with an unknown type")

    with pytest.raises(ValueError):
        evaluate(STATE, {"m0": {"type": "vibes", "instructions": "?"}}, timeout=2.5,
                 transport=_transport(handler))


# ---------------------------------------------------------------------------
# キーの伏せ字
# ---------------------------------------------------------------------------


def test_api_key_value_never_logged(caplog):
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")

    def handler(request):
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert "test-key-not-real" not in caplog.text


def test_api_key_in_error_body_is_masked(caplog):
    """エラー本文にキーがそのまま載っていても、ログにも例外にも出さない。"""
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")

    def handler(request):
        return httpx.Response(
            401,
            text='{"error": "invalid api key: test-key-not-real (Bearer test-key-not-real)"}',
        )

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert "test-key-not-real" not in str(exc.value)
    assert "test-key-not-real" not in caplog.text
    # 本文プレビュー自体は残す (デバッグ価値があるので全面秘匿はしない)。
    assert "invalid api key" in str(exc.value)
    assert "***" in str(exc.value)


def test_http_error_text_containing_the_key_is_masked(caplog):
    """HTTP 例外の文言にキーが載っていても、ログにも例外にも出さない。

    httpx/h11 の例外はヘッダ値 (= Authorization のキー) を文言に含む型がある。
    """
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")

    def handler(request):
        raise httpx.ConnectError(
            "illegal header value b'Bearer test-key-not-real'", request=request,
        )

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert "test-key-not-real" not in str(exc.value)
    assert "test-key-not-real" not in caplog.text
    assert "ConnectError" in str(exc.value)
    assert "***" in str(exc.value)


def test_non_http_exception_is_normalized_and_masked(caplog):
    """httpx 以外の例外も ReflexJudgmentUnavailable に正規化し、キーは伏せる。

    素通しすると「失敗はすべて 1 種類の例外」という契約が破れ、呼び出し側が
    1 種類を捕まえるだけでは畳めなくなる (会話の同期経路に例外が抜ける)。
    """
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")

    def handler(request):
        raise ValueError("unexpected failure while sending test-key-not-real")

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert "test-key-not-real" not in str(exc.value)
    assert "test-key-not-real" not in caplog.text
    assert "ValueError" in str(exc.value)
    assert "***" in str(exc.value)


def test_mask_secret_also_masks_the_escaped_repr_form():
    """改行を含むキーは ``\\n`` のエスケープ表現で記録に出る (生文字列置換をすり抜ける)。"""
    secret = "line1\nline2"
    text = f"Illegal header value b'Bearer {repr(secret)[1:-1]}'"

    assert secret not in text  # 生の値では出ていない (= 一段目の置換では落ちない)
    masked = _mask_secret(text, secret)
    assert "line1" not in masked
    assert "line2" not in masked
    assert "***" in masked


# ---------------------------------------------------------------------------
# usage の許可リストと記帳
# ---------------------------------------------------------------------------


def test_unknown_usage_keys_are_dropped(caplog):
    """usage は宣言された欄だけ。API が返した未知のキーは返さないし記録もしない。"""
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "m0": {"type": "noul", "noul": 0.4},
                    "m1": {"type": "noul", "noul": 0.6},
                },
                "usage": {"input_tokens": 10, "output_tokens": 2, "debug": "test-key-not-real"},
            },
        )

    answers, usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert answers == {"m0": 0.4, "m1": 0.6}
    assert usage == {"input_tokens": 10, "output_tokens": 2}

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "debug" not in logged
    assert "test-key-not-real" not in logged


@pytest.mark.parametrize(
    "bad_input_tokens",
    ["-5", "NaN", "Infinity", "-Infinity"],
    ids=["negative", "nan", "inf", "neg_inf"],
)
def test_unusable_usage_numbers_are_dropped_field_by_field(bad_input_tokens, usage_calls):
    """トークン数として成り立たない値は、その欄だけ捨てる。

    負数をそのまま記帳すると累計の使用量と費用が目減りし、NaN/inf は記帳側の
    int() で落ちて WARNING になるだけ。どちらも黙って会計を歪める。一方で、
    **答えが正しいのに会計の欄が壊れているだけの応答を「判定できなかった」には
    しない** — 判定は成立のまま返す。
    """
    # NaN / Infinity は httpx の json= が拒否する (allow_nan=False) ので生テキストで組む。
    body = (
        '{"answers": {"m0": {"type": "noul", "noul": 0.5}, '
        '"m1": {"type": "noul", "noul": 0.5}}, '
        f'"usage": {{"input_tokens": {bad_input_tokens}, "output_tokens": 48}}}}'
    )

    def handler(request):
        return httpx.Response(200, text=body, headers={"Content-Type": "application/json"})

    answers, usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert answers == {"m0": 0.5, "m1": 0.5}      # 判定そのものは成立のまま
    assert usage == {"output_tokens": 48}         # 壊れた欄だけ落ちる
    assert len(usage_calls) == 1
    assert usage_calls[0]["input_tokens"] == 0
    assert usage_calls[0]["output_tokens"] == 48


def test_non_numeric_usage_values_are_dropped():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "answers": {qid: {"type": "noul", "noul": 0.5} for qid in ("m0", "m1")},
                "usage": {"input_tokens": "312", "output_tokens": 48},
            },
        )

    _answers, usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert usage == {"output_tokens": 48}


def test_usage_is_recorded_under_the_model_config_key(usage_calls):
    """費用はモデル設定キー名義で、LLM クライアントと同じ入口 (UsageTracker) へ載る。

    答える側を替えても費用が同じ場所に見える形 (intent §2)。モデル設定ファイルに
    pricing を書けば、この記帳がそのまま費用に変わる。
    """
    def handler(request):
        return httpx.Response(200, json={
            "answers": {qid: {"type": "noul", "noul": 0.5} for qid in ("m0", "m1")},
            "usage": {"input_tokens": 100, "output_tokens": 4},
        })

    evaluate(STATE, QUESTIONS, timeout=2.5, persona_id="air_city_a",
             transport=_transport(handler))

    assert len(usage_calls) == 1
    assert usage_calls[0]["model_id"] == MODEL_KEY
    assert usage_calls[0]["input_tokens"] == 100
    assert usage_calls[0]["output_tokens"] == 4
    assert usage_calls[0]["persona_id"] == "air_city_a"
    assert usage_calls[0]["category"] == reflex_judgment.USAGE_CATEGORY


def test_no_usage_in_the_response_records_nothing(usage_calls):
    def handler(request):
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert usage_calls == []


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param({"m0": {"type": "noul", "noul": 0.5}}, id="missing_qid"),
        pytest.param(
            {
                "m0": {"type": "noul", "noul": 0.5},
                "m1": {"type": "noul", "noul": 1.4},
            },
            id="out_of_range",
        ),
        pytest.param(
            {
                "m0": {"type": "noul", "noul": 0.5},
                "m1": {"type": "score", "noul": 0.5},
            },
            id="wrong_type",
        ),
    ],
)
def test_usage_is_recorded_even_when_the_answers_are_unusable(answers, usage_calls):
    """答えの側が不成立でも、応答が届いている以上 API 側の課金は発生している。

    判定は ReflexJudgmentUnavailable のまま (不備のある答えを成立させない) だが、
    使用量は台帳に載る — 締切で見切った呼び出しが答えを見ずに使用量だけ記帳するのと
    同じ扱いで、「支払ったのに台帳に無い」費用を作らない。
    """
    def handler(request):
        return httpx.Response(200, json={
            "answers": answers,
            "usage": {"input_tokens": 120, "output_tokens": 6},
        })

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, persona_id="air_city_a",
                 transport=_transport(handler))

    assert len(usage_calls) == 1
    assert usage_calls[0]["model_id"] == MODEL_KEY
    assert usage_calls[0]["input_tokens"] == 120
    assert usage_calls[0]["output_tokens"] == 6
    assert usage_calls[0]["persona_id"] == "air_city_a"
    assert usage_calls[0]["category"] == reflex_judgment.USAGE_CATEGORY


def test_a_response_without_an_answers_object_still_records_its_usage(usage_calls):
    """``answers`` の欄ごと無い応答も、届いた以上は使用量を記帳する。"""
    def handler(request):
        return httpx.Response(200, json={"usage": {"input_tokens": 9, "output_tokens": 1}})

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert len(usage_calls) == 1
    assert usage_calls[0]["input_tokens"] == 9


def test_non_200_records_nothing_even_with_a_usage_body(usage_calls):
    """非 200 は記帳しない (応答は届いていないものとして扱う既存の線引き)。"""
    def handler(request):
        return httpx.Response(500, json={
            "answers": _all_answered("m0", "m1")["answers"],
            "usage": {"input_tokens": 9, "output_tokens": 1},
        })

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert usage_calls == []


def test_invalid_json_records_nothing(usage_calls):
    """JSON として読めない応答は、使用量の載せどころが無いので記帳しない。"""
    def handler(request):
        return httpx.Response(200, text="not json", headers={"Content-Type": "application/json"})

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert usage_calls == []


# ---------------------------------------------------------------------------
# 器 (締切・ワーカー・同時実行の上限)
# ---------------------------------------------------------------------------


def test_deadline_exceeded_raises_unavailable():
    """各 I/O が短くても、呼び出し全体が締切を超えたら打ち切る。"""
    def handler(request):
        time.sleep(1.0)
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    started = time.monotonic()
    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=0.1, transport=_transport(handler))
    elapsed = time.monotonic() - started

    assert "deadline exceeded" in str(exc.value)
    # ワーカーの完了 (1.0 秒) を待たずに戻る。
    assert elapsed < 0.95


# ---------------------------------------------------------------------------
# 使えなかった理由の種別 (例外の型は 1 つのまま、印は属性で運ぶ)
# ---------------------------------------------------------------------------


def test_only_the_deadline_raise_is_marked_as_a_deadline():
    """締切超過だけが "deadline"。呼び出し側はこの印で注記を出すかを決める。"""
    def handler(request):
        time.sleep(1.0)
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=0.1, transport=_transport(handler))

    assert exc.value.kind == reflex_judgment.UNAVAILABLE_DEADLINE


@pytest.mark.parametrize("make_failure", [
    pytest.param(lambda: httpx.Response(500, text="boom"), id="non-200"),
    pytest.param(
        lambda: httpx.Response(200, json={"answers": {"m0": {"type": "noul", "noul": 0.5}}}),
        id="partial-answer",
    ),
    pytest.param(
        lambda: httpx.Response(200, text="not json", headers={"Content-Type": "application/json"}),
        id="invalid-json",
    ),
])
def test_other_failures_are_not_marked_as_a_deadline(make_failure):
    """待ち時間を延ばしても直らない失敗には印を立てない (直し方を誤って案内しない)。"""
    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5,
                 transport=_transport(lambda request: make_failure()))

    assert exc.value.kind == reflex_judgment.UNAVAILABLE_OTHER


def test_a_missing_api_key_is_not_marked_as_a_deadline(monkeypatch):
    """キー欠落も「その他の失敗」。宛先が固まっているのとは別の話。"""
    monkeypatch.delenv(KEY_ENV, raising=False)

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(
            lambda request: httpx.Response(200, json=_all_answered("m0", "m1")),
        ))

    assert exc.value.kind == reflex_judgment.UNAVAILABLE_OTHER


def test_the_recent_outcomes_count_each_call_once():
    """成立 / 締切超過 / その他の失敗が、直近の記録に 1 件ずつ積まれる。"""
    assert reflex_judgment.recent_outcomes() == (0, 0, 0)

    evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(
        lambda request: httpx.Response(200, json=_all_answered("m0", "m1")),
    ))
    assert reflex_judgment.recent_outcomes() == (1, 0, 0)

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(
            lambda request: httpx.Response(500, text="boom"),
        ))
    assert reflex_judgment.recent_outcomes() == (2, 0, 1)

    def _slow(request):
        time.sleep(1.0)
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=0.1, transport=_transport(_slow))
    assert reflex_judgment.recent_outcomes() == (3, 1, 1)


def test_an_empty_question_set_is_not_counted_as_a_call():
    """質問ゼロの呼び出しは外部へ何も送っていないので数えない。"""
    assert evaluate(STATE, {}, timeout=2.5) == ({}, {})
    assert reflex_judgment.recent_outcomes() == (0, 0, 0)


def test_the_history_only_keeps_the_most_recent_calls():
    """記録は直近 OUTCOME_HISTORY_SIZE 件で頭打ちになる。"""
    for _ in range(reflex_judgment.OUTCOME_HISTORY_SIZE + 5):
        evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(
            lambda request: httpx.Response(200, json=_all_answered("m0", "m1")),
        ))

    assert reflex_judgment.recent_outcomes() == (reflex_judgment.OUTCOME_HISTORY_SIZE, 0, 0)


def test_records_older_than_the_window_are_not_counted(monkeypatch):
    """窓 (OUTCOME_WINDOW_SECONDS) の外まで古くなった記録は数えない。

    モデルを速いものへ替えて直した人が、そのペルソナとしばらく喋らないだけで
    「最近◯回中◯回…」の警告を見続けることがないようにするための窓。
    """
    clock = {"now": 1000.0}

    class _FakeTime:
        """反射判断のモジュールだけに差し込む時計 (本物の time を書き換えない)。"""

        @staticmethod
        def monotonic() -> float:
            return clock["now"]

    monkeypatch.setattr(reflex_judgment, "time", _FakeTime)

    reflex_judgment._record_outcome(reflex_judgment.OUTCOME_DEADLINE)
    reflex_judgment._record_outcome(reflex_judgment.OUTCOME_FAILED)
    assert reflex_judgment.recent_outcomes() == (2, 1, 1)

    # 窓のちょうど端はまだ数える
    clock["now"] += reflex_judgment.OUTCOME_WINDOW_SECONDS
    assert reflex_judgment.recent_outcomes() == (2, 1, 1)

    # 端を越えた記録は落ちる
    clock["now"] += 1.0
    assert reflex_judgment.recent_outcomes() == (0, 0, 0)

    # 新しい記録だけがまた数え上げられる (古いのは戻ってこない)
    reflex_judgment._record_outcome(reflex_judgment.OUTCOME_OK)
    assert reflex_judgment.recent_outcomes() == (1, 0, 0)


def test_abandoned_call_still_records_its_usage(usage_calls):
    """締切で見切った呼び出しでも、応答が後から届けば使用量は記帳される。

    メインは締切の時点で ReflexJudgmentUnavailable を投げて会話を先へ進めるが、
    API 側の課金はもう発生している。落とすと「支払ったのに台帳に無い」費用ができる。
    答えは読まない (使わない判定を後から成立させない)。
    """
    def slow_handler(request):
        time.sleep(0.8)
        return httpx.Response(200, json={
            "answers": {qid: {"type": "noul", "noul": 0.5} for qid in ("m0", "m1")},
            "usage": {"input_tokens": 77, "output_tokens": 5},
        })

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=0.1, persona_id="air_city_a",
                 transport=_transport(slow_handler))
    assert "deadline exceeded" in str(exc.value)

    # メインは記帳しない (成功で返った経路だけが記帳する)。
    assert usage_calls == []

    _join_workers()
    # 置き去りにされたワーカーが、自分の見た応答の使用量だけを 1 回記帳する。
    assert len(usage_calls) == 1
    assert usage_calls[0]["model_id"] == MODEL_KEY
    assert usage_calls[0]["input_tokens"] == 77
    assert usage_calls[0]["output_tokens"] == 5
    assert usage_calls[0]["persona_id"] == "air_city_a"
    assert usage_calls[0]["category"] == reflex_judgment.USAGE_CATEGORY


def test_deadline_racing_with_completion_records_the_usage_once(usage_calls):
    """締切とワーカーの完了がぶつかっても、課金済みの使用量はちょうど 1 回記帳される。

    ワーカーが「完了した」と宣言し終えたあと、メインが見切りを宣言する — という並びを
    固定する。錠が無いと、ワーカーは「まだ見切られていない」、メインは「まだ完了して
    いない」と見て、**どちらも記帳しないまま**抜けてしまう窓がここにあった。
    """
    # ワーカーは「完了の宣言」を済ませてから枠を返す。その返却を関所で止めておくと、
    # done.set() も止まるのでメインの待ちは必ず締切で切れ、見切りの宣言はワーカーの
    # 宣言より確実に後になる。
    release_gate = threading.Event()
    worker_reached_release = threading.Event()

    class _GatedSemaphore:
        """枠の返却を関所で止めるだけのセマフォ (本物へ委譲する)。"""

        def __init__(self, inner):
            self._inner = inner

        def acquire(self, blocking=True):
            return self._inner.acquire(blocking)

        def release(self):
            if threading.current_thread().name == reflex_judgment._WORKER_THREAD_NAME:
                worker_reached_release.set()
                release_gate.wait(10)
            self._inner.release()

    def handler(request):
        return httpx.Response(200, json={
            "answers": {qid: {"type": "noul", "noul": 0.5} for qid in ("m0", "m1")},
            "usage": {"input_tokens": 77, "output_tokens": 5},
        })

    gated = _GatedSemaphore(reflex_judgment._IN_FLIGHT)
    try:
        with patch.object(reflex_judgment, "_IN_FLIGHT", gated):
            with pytest.raises(ReflexJudgmentUnavailable) as exc:
                evaluate(STATE, QUESTIONS, timeout=0.2, persona_id="air_city_a",
                         transport=_transport(handler))
    finally:
        release_gate.set()

    assert "deadline exceeded" in str(exc.value)
    # 狙った並びを実際に通ったこと (ワーカーは完了の宣言を済ませて関所で待っていた)。
    assert worker_reached_release.is_set()

    # ワーカーは見切りの印を見る前に通り過ぎているので記帳していない。落とさずに
    # メインが引き受ける。
    assert len(usage_calls) == 1
    assert usage_calls[0]["model_id"] == MODEL_KEY
    assert usage_calls[0]["input_tokens"] == 77
    assert usage_calls[0]["output_tokens"] == 5
    assert usage_calls[0]["persona_id"] == "air_city_a"
    assert usage_calls[0]["category"] == reflex_judgment.USAGE_CATEGORY

    # ワーカーが最後まで走り切っても二重記帳にならない。
    _join_workers()
    assert len(usage_calls) == 1


def test_abandoned_call_that_fails_records_nothing(usage_calls):
    """締切を過ぎたうえに通信が失敗した呼び出しは、記帳するものが無い。"""
    def slow_failing_handler(request):
        time.sleep(0.8)
        raise httpx.ConnectError("connection reset", request=request)

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=0.1, transport=_transport(slow_failing_handler))

    _join_workers()
    assert usage_calls == []


def test_abandoned_non_200_records_nothing(usage_calls):
    """締切超過で届いたのが非 200 なら、使用量として読まない。"""
    def slow_error_handler(request):
        time.sleep(0.8)
        return httpx.Response(500, json={"usage": {"input_tokens": 9, "output_tokens": 1}})

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=0.1, transport=_transport(slow_error_handler))

    _join_workers()
    assert usage_calls == []


def test_worker_thread_is_a_daemon():
    """置き去りにしたワーカーがプロセス終了を止めないこと (非デーモンだと join される)。"""
    seen = {}

    def handler(request):
        current = threading.current_thread()
        seen["daemon"] = current.daemon
        seen["name"] = current.name
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert seen["daemon"] is True
    assert seen["name"] == reflex_judgment._WORKER_THREAD_NAME


def test_too_many_in_flight_calls_is_unavailable():
    """同時実行の枠が埋まっていたら、待たずに ReflexJudgmentUnavailable にする。"""
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the destination must not be called while the limit is reached")

    acquired = 0
    try:
        for _ in range(reflex_judgment._MAX_IN_FLIGHT):
            assert reflex_judgment._IN_FLIGHT.acquire(blocking=False) is True
            acquired += 1

        started = time.monotonic()
        with pytest.raises(ReflexJudgmentUnavailable) as exc:
            evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
        elapsed = time.monotonic() - started
    finally:
        for _ in range(acquired):
            reflex_judgment._IN_FLIGHT.release()

    assert "too many in-flight calls" in str(exc.value)
    assert elapsed < 0.5  # 待たずに即戻る


def test_abandoned_workers_release_their_slot():
    """締切超過で置き去りにしたワーカーは、終わるときに自分で枠を返す。

    枠を返さないと、締切超過が数回起きただけで以後の全呼び出しが
    "too many in-flight calls" になってしまう。
    """
    def slow_handler(request):
        time.sleep(0.8)
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    for _ in range(2):
        with pytest.raises(ReflexJudgmentUnavailable) as exc:
            evaluate(STATE, QUESTIONS, timeout=0.1, transport=_transport(slow_handler))
        assert "deadline exceeded" in str(exc.value)

    # ワーカーが終われば枠は返っている (二重 release で壊れてもいない)。
    _join_workers()
    for _ in range(reflex_judgment._MAX_IN_FLIGHT):
        assert reflex_judgment._IN_FLIGHT.acquire(blocking=False) is True
    for _ in range(reflex_judgment._MAX_IN_FLIGHT):
        reflex_judgment._IN_FLIGHT.release()

    def fast_handler(request):
        return httpx.Response(200, json=_all_answered("m0", "m1", noul=0.42))

    answers, _usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(fast_handler))
    assert answers == {"m0": 0.42, "m1": 0.42}


def test_failure_before_the_worker_starts_releases_the_slot():
    """ワーカーが走り出す前に落ちても、掴んだ枠はその場で返す。

    枠を返さずに戻ると、繰り返し失敗する環境 (壊れた SSL 関連の環境変数で
    httpx.Client の生成が毎回失敗する等) では _MAX_IN_FLIGHT 回で枠が尽き、
    原因を直してもプロセスを再起動するまで判定が復旧しなくなる。
    """
    def handler(request):
        return httpx.Response(200, json=_all_answered("m0", "m1", noul=0.55))

    def _explode(*args, **kwargs):
        raise RuntimeError("could not load the SSL certificate file")

    attempts = reflex_judgment._MAX_IN_FLIGHT + 1
    with patch.object(reflex_judgment.httpx, "Client", _explode):
        for _ in range(attempts):
            with pytest.raises(ReflexJudgmentUnavailable) as exc:
                evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
            # 枠が漏れていれば、途中から失敗の理由が「枠が無い」に変わる。
            assert "too many in-flight calls" not in str(exc.value)
            assert "RuntimeError" in str(exc.value)

    # 枠は全部空いている (失敗のたびに返っていた)。
    for _ in range(reflex_judgment._MAX_IN_FLIGHT):
        assert reflex_judgment._IN_FLIGHT.acquire(blocking=False) is True
    for _ in range(reflex_judgment._MAX_IN_FLIGHT):
        reflex_judgment._IN_FLIGHT.release()

    # 原因が直れば (= Client が作れれば) そのまま使える。
    answers, _usage = evaluate(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert answers == {"m0": 0.55, "m1": 0.55}


# ---------------------------------------------------------------------------
# 通常の LLM への変換層 (第 2 段)
#
# 実 LLM は絶対に呼ばない。クライアントの工場 (llm_clients.get_llm_client) を
# 差し替えて、プロンプト・スキーマ・答えの読み戻し・使用量の記帳だけを確かめる。
# ---------------------------------------------------------------------------

LLM_MODEL_KEY = "test-llm-judge"
#: この偽モデル自身の名前空間のキー (照合に通る正常形)。
LLM_KEY_ENV = "SAIVERSE_MODEL_TEST_LLM_JUDGE_API_KEY"


def _llm_model_config(**overrides):
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


MIXED_QUESTIONS = {
    "n0": {
        "type": "noul",
        "instructions": "`memories.n0` は今の話に浮かぶ記憶か。",
        "criteria": {"true": "関連する", "false": "関連しない"},
    },
    "c0": {"type": "choice", "instructions": "次に何をする。", "options": ["walk", "stay"]},
    "s0": {"type": "score", "instructions": "何点か。"},
}


class _FakeLLMClient:
    """llm_clients が返すクライアントの偽物 (generate と consume_usage だけ)。"""

    def __init__(self, answer, usage=None, *, raises=None, delay=0.0):
        self.answer = answer
        self.usage = usage
        self.raises = raises
        self.delay = delay
        self.calls = []
        # 思考を最小にしてくれという申し入れが、何回・どの順で届いたか。
        # ("minimal_reasoning" と "generate" を 1 本の並びに記録して、
        #  申し入れが generate より前に来たことを順序で確かめる)
        self.events = []

    def prefer_minimal_reasoning(self):
        self.events.append("minimal_reasoning")

    def generate(self, messages, tools=None, response_schema=None, **kwargs):
        self.events.append("generate")
        self.calls.append({
            "messages": messages, "response_schema": response_schema, "kwargs": kwargs,
        })
        if self.delay:
            time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.answer

    def consume_usage(self):
        usage, self.usage = self.usage, None
        return usage


def _usage_info(input_tokens, output_tokens, **overrides):
    from llm_clients.base import UsageInfo

    return UsageInfo(
        model="vendor/judge-lite",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        timestamp=time.time(),
        **overrides,
    )


@pytest.fixture
def llm_role(monkeypatch):
    """役割に通常の LLM の偽モデルを割り当てる。"""
    monkeypatch.setenv(ROLE_ENV, LLM_MODEL_KEY)
    monkeypatch.setenv(LLM_KEY_ENV, "test-key-not-real")
    _set_configs(monkeypatch, {LLM_MODEL_KEY: _llm_model_config()})


def _install_llm_client(monkeypatch, client):
    """クライアントの工場を差し替え、渡った引数を集めて返す。"""
    created = []

    def _factory(model, provider, context_length, config=None):
        created.append((model, provider, context_length))
        return client

    monkeypatch.setattr("llm_clients.get_llm_client", _factory)
    return created


def test_llm_backend_answers_all_three_types(llm_role, monkeypatch):
    """3 型混在の質問を、配列で受けた答えから {qid: 答え} に戻す。"""
    client = _FakeLLMClient({"answers": [
        {"qid": "n0", "noul": 0.9},
        {"qid": "c0", "choice": "walk"},
        {"qid": "s0", "score": 7.5},
    ]})
    created = _install_llm_client(monkeypatch, client)

    answers, usage = evaluate(STATE, MIXED_QUESTIONS, timeout=2.5)

    assert answers == {"n0": 0.9, "c0": "walk", "s0": 7.5}
    assert usage == {}  # このクライアントは使用量を持っていない
    # 設定キー・provider・context_length は既存の呼び出し元と同じ引き方で渡す。
    assert created == [(LLM_MODEL_KEY, "openai", 32000)]


def test_llm_call_asks_for_minimal_reasoning_before_generating(llm_role, monkeypatch):
    """一撃の判定なので、送る前に「思考は最小でよい」を伝える。

    伝える先はクライアント (つまみの実名は各クライアントが持つ)。ここで見るのは
    「generate より前に 1 回だけ届いた」ことだけ — 実際に何を最小にするかは
    モデル次第で、明示の設定があればクライアント側が何もしない。
    """
    client = _FakeLLMClient({"answers": [{"qid": "m0", "noul": 0.25},
                                         {"qid": "m1", "noul": 0.75}]})
    _install_llm_client(monkeypatch, client)

    evaluate(STATE, QUESTIONS, timeout=2.5)

    assert client.events == ["minimal_reasoning", "generate"]


def test_llm_answer_may_arrive_as_a_json_string(llm_role, monkeypatch):
    """generate は dict を返すことも JSON 文字列を返すこともある (両方受ける)。"""
    client = _FakeLLMClient(json.dumps({"answers": [{"qid": "m0", "noul": 0.25},
                                                    {"qid": "m1", "noul": 0.75}]}))
    _install_llm_client(monkeypatch, client)

    answers, _usage = evaluate(STATE, QUESTIONS, timeout=2.5)
    assert answers == {"m0": 0.25, "m1": 0.75}


def test_llm_answer_that_is_not_json_is_unavailable(llm_role, monkeypatch):
    _install_llm_client(monkeypatch, _FakeLLMClient("ごめん、判定できませんでした"))

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5)


def test_llm_partial_answer_is_unavailable(llm_role, monkeypatch):
    """qid が欠けた答えは不成立 (部分回答を採らない規則は変換層でも同じ)。"""
    _install_llm_client(monkeypatch, _FakeLLMClient({"answers": [{"qid": "m0", "noul": 0.5}]}))

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5)
    assert "m1" in str(exc.value)


def test_llm_choice_outside_the_options_is_unavailable(llm_role, monkeypatch):
    _install_llm_client(monkeypatch, _FakeLLMClient({"answers": [{"qid": "c0", "choice": "fly"}]}))

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(
            STATE,
            {"c0": {"type": "choice", "instructions": "どれを選ぶ", "options": ["walk", "stay"]}},
            timeout=2.5,
        )
    assert "options" in str(exc.value)


def test_llm_out_of_range_noul_is_unavailable(llm_role, monkeypatch):
    """答えの検算は jev 互換の道と同じ規則を通る (検算は 1 箇所にある)。"""
    _install_llm_client(monkeypatch, _FakeLLMClient({"answers": [
        {"qid": "m0", "noul": 1.4}, {"qid": "m1", "noul": 0.5},
    ]}))

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5)


def test_llm_usage_is_recorded_once_under_the_model_config_key(llm_role, monkeypatch, usage_calls):
    """クライアントは自分では記帳しない — この層が設定キー名義で 1 回だけ載せる。"""
    client = _FakeLLMClient(
        {"answers": [{"qid": "m0", "noul": 0.5}, {"qid": "m1", "noul": 0.5}]},
        usage=_usage_info(200, 12, cached_tokens=30),
    )
    _install_llm_client(monkeypatch, client)

    evaluate(STATE, QUESTIONS, timeout=2.5, persona_id="air_city_a")

    assert len(usage_calls) == 1
    assert usage_calls[0]["model_id"] == LLM_MODEL_KEY
    assert usage_calls[0]["input_tokens"] == 200
    assert usage_calls[0]["output_tokens"] == 12
    assert usage_calls[0]["cached_tokens"] == 30
    assert usage_calls[0]["persona_id"] == "air_city_a"
    assert usage_calls[0]["category"] == reflex_judgment.USAGE_CATEGORY


def test_llm_usage_is_recorded_even_when_the_answers_are_unusable(llm_role, monkeypatch, usage_calls):
    """答えが不成立でも、応答が返った以上 API 側の課金は発生している。"""
    client = _FakeLLMClient({"answers": [{"qid": "m0", "noul": 0.5}]}, usage=_usage_info(50, 3))
    _install_llm_client(monkeypatch, client)

    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5)

    assert len(usage_calls) == 1
    assert usage_calls[0]["input_tokens"] == 50


def test_llm_call_abandoned_at_the_deadline_records_only_its_usage(llm_role, monkeypatch, usage_calls):
    """締切で見切っても ``generate`` は途中で切れない。完走したワーカーが記帳する。

    答えは読まない (メインはもう会話を先へ進めている)。記帳は 1 回だけ。
    """
    client = _FakeLLMClient(
        {"answers": [{"qid": "m0", "noul": 0.5}, {"qid": "m1", "noul": 0.5}]},
        usage=_usage_info(77, 5), delay=0.8,
    )
    _install_llm_client(monkeypatch, client)

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=0.1, persona_id="air_city_a")
    assert "deadline exceeded" in str(exc.value)
    assert usage_calls == []

    _join_workers()
    assert len(usage_calls) == 1
    assert usage_calls[0]["model_id"] == LLM_MODEL_KEY
    assert usage_calls[0]["input_tokens"] == 77
    assert usage_calls[0]["output_tokens"] == 5
    assert usage_calls[0]["category"] == reflex_judgment.USAGE_CATEGORY


def test_llm_failure_is_normalized_and_masked(llm_role, monkeypatch, caplog):
    """クライアントが投げた例外も 1 種類に正規化し、キーの値は伏せる。"""
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")
    _install_llm_client(
        monkeypatch,
        _FakeLLMClient(None, raises=RuntimeError("bad request with key test-key-not-real")),
    )

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5)

    assert "test-key-not-real" not in str(exc.value)
    assert "test-key-not-real" not in caplog.text
    assert "RuntimeError" in str(exc.value)
    assert "***" in str(exc.value)


def _refuse_llm_client(monkeypatch):
    """クライアントの工場を「呼ばれたら失敗」に差し替える。"""
    def _factory(*args, **kwargs):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("the client must not be created without a key")

    monkeypatch.setattr("llm_clients.get_llm_client", _factory)


@pytest.mark.parametrize("set_env", [True, False], ids=["empty", "unset"])
def test_llm_missing_api_key_is_unavailable(llm_role, monkeypatch, set_env):
    """キーの env 名を宣言している設定で、その env が無い / 空なら呼ばない。"""
    if set_env:
        monkeypatch.setenv(LLM_KEY_ENV, "")
    else:
        monkeypatch.delenv(LLM_KEY_ENV, raising=False)
    _refuse_llm_client(monkeypatch)

    assert is_available() is False
    with pytest.raises(ReflexJudgmentUnavailable):
        evaluate(STATE, QUESTIONS, timeout=2.5)


# --- 通常の LLM のキーの有無は is_model_available に聞く --------------------------
#
# 手組みで「api_key_env の env が空か」を見ると、代替キー名 (Gemini の無料枠の
# キー)・宣言の無い旧形式の provider 既定キー名・キーの要らないローカル provider を
# 取りこぼす。取りこぼしの症状は「候補の集め方だけ広がって毎ターン判定が落ちる」で、
# この設計が一番避けたい食い違い。

GEMINI_MODEL_KEY = "test-gemini-judge"
OLLAMA_MODEL_KEY = "test-local-judge"
OPENAI_LEGACY_MODEL_KEY = "test-legacy-openai-judge"


def _gemini_model_config():
    """同梱の Gemini モデルと同じ、読み込みで provider の欄を畳み込んだ後の形。"""
    return {
        "model": "gemini-x",
        "protocol": "gemini_native",
        "provider": "gemini",
        "context_length": 32000,
        "api_key_env": "GEMINI_API_KEY",
        "api_key_env_alternates": ["GEMINI_FREE_API_KEY"],
    }


def test_an_alternate_key_name_alone_makes_the_llm_available(monkeypatch):
    """代替キー名 (Gemini の無料枠のキー) だけが設定されていても呼べる。

    ``api_key_env`` の env だけを手組みで見ると、無料枠のキーしか持っていない人に
    「使えない」と答えてしまう。判定は既存の述語 (saiverse/model_configs.py の
    is_model_available) に寄せてあるので、代替キー名もそのまま効く。
    """
    monkeypatch.setenv(ROLE_ENV, GEMINI_MODEL_KEY)
    _set_configs(monkeypatch, {GEMINI_MODEL_KEY: _gemini_model_config()})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_FREE_API_KEY", "test-key-not-real")

    assert is_available() is True

    # 呼び出しの入口も同じ判定を引く (「使える」と答えた宛先を evaluate が断らない)。
    _install_llm_client(monkeypatch, _FakeLLMClient(
        {"answers": [{"qid": "m0", "noul": 0.5}, {"qid": "m1", "noul": 0.5}]},
    ))
    answers, _usage = evaluate(STATE, QUESTIONS, timeout=2.5)
    assert answers == {"m0": 0.5, "m1": 0.5}


def test_an_alternate_key_value_is_masked_in_llm_errors(monkeypatch, caplog):
    """代替キー名で認証した呼び出しの例外文言でも、そのキーの値を伏せる。

    伏せ字の対象を「宣言された 1 本の env の値」だけにすると、無料枠のキーで
    認証したときの例外にそのキーが素通りする (2026-09-20 のローカルレビューの指摘)。
    伏せる値の一覧は、キー有無の判定と同じ表 (required_api_key_env_names) から作る。
    """
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")
    monkeypatch.setenv(ROLE_ENV, GEMINI_MODEL_KEY)
    _set_configs(monkeypatch, {GEMINI_MODEL_KEY: _gemini_model_config()})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_FREE_API_KEY", "free-key-not-real")
    _install_llm_client(monkeypatch, _FakeLLMClient(
        None, raises=RuntimeError("auth failed for bearer free-key-not-real"),
    ))

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5)

    assert "free-key-not-real" not in str(exc.value)
    assert "free-key-not-real" not in caplog.text
    assert "***" in str(exc.value)


def test_an_unknown_protocol_does_not_resolve_as_an_llm_backend(monkeypatch, caplog):
    """LLM クライアントの工場が話せない protocol は「解決できない」。

    素通しにすると、壊れた設定が「解決できた」顔で保存と画面警告を通り抜け、
    強化 ON のペルソナだけが実行時に毎ターン黙って失敗し続ける (2026-09-20 の
    敵対レビュー 1 巡目)。照合は工場の解決規則そのもの (llm_clients.factory の
    resolve_protocol + SUPPORTED_PROTOCOLS) を使い、二枚目の表を作らない。
    """
    caplog.set_level("WARNING", logger="saiverse.reflex_judgment")
    monkeypatch.setenv(ROLE_ENV, "test-broken-protocol-judge")
    _set_configs(monkeypatch, {"test-broken-protocol-judge": {
        "model": "vendor/unknown",
        "protocol": "banana_compat",
        "provider": "banana",
        "context_length": 1000,
    }})

    assert is_available() is False
    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        resolve_backend()
    assert "banana_compat" in str(exc.value)
    assert "banana_compat" in caplog.text


def test_a_legacy_provider_name_resolves_with_the_factory_mapping(monkeypatch):
    """protocol 欄の無い旧形式 (provider 名だけ) は、工場と同じ写像で通す。

    照合を自前の表で行うと、工場が受ける旧形式の設定 (provider: gemini など) を
    ここだけが断る逆ずれが生まれる。
    """
    monkeypatch.setenv(ROLE_ENV, "test-legacy-gemini-judge")
    _set_configs(monkeypatch, {"test-legacy-gemini-judge": {
        "model": "gemini-x",
        "provider": "gemini",
        "context_length": 1000,
    }})

    backend = resolve_backend()
    assert backend.kind == reflex_judgment.REFLEX_KIND_LLM


def test_a_protocol_only_config_requires_the_clients_default_key(monkeypatch):
    """provider 欄なしで protocol だけ書いた設定でも、クライアントの既定キーで見る。

    この形 (手書きの user_data 定義) は、実行時のクライアントが自分の既定の env
    (openai_compat なら OPENAI_API_KEY) を読む。キー候補の表がそれを知らないと、
    「使える」と答えたのに実行時だけ毎ターン失敗する食い違いが残る (2026-09-20 の
    敵対レビュー 2 巡目)。
    """
    monkeypatch.setenv(ROLE_ENV, "test-protocol-only-judge")
    _set_configs(monkeypatch, {"test-protocol-only-judge": {
        "model": "vendor/protocol-only",
        "protocol": "openai_compat",
        "context_length": 1000,
    }})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _refuse_llm_client(monkeypatch)

    assert is_available() is False

    monkeypatch.setenv("OPENAI_API_KEY", "default-key-not-real")
    assert is_available() is True


def test_the_clients_default_key_is_masked_in_llm_errors(monkeypatch, caplog):
    """provider 欄なしの設定で、クライアントが読んだ既定キーの値も伏せる。

    伏せる値の一覧はキー有無の判定と同じ表から作るので、表が既定キーを知った時点で
    伏せ字も同じ名前を見る (2026-09-20 の敵対レビュー 2 巡目 — 判定と伏せ字が同じ
    形の設定で同時に漏れていた)。
    """
    caplog.set_level("DEBUG", logger="saiverse.reflex_judgment")
    monkeypatch.setenv(ROLE_ENV, "test-protocol-only-judge")
    _set_configs(monkeypatch, {"test-protocol-only-judge": {
        "model": "vendor/protocol-only",
        "protocol": "openai_compat",
        "context_length": 1000,
    }})
    monkeypatch.setenv("OPENAI_API_KEY", "default-key-not-real")
    _install_llm_client(monkeypatch, _FakeLLMClient(
        None, raises=RuntimeError("401 for bearer default-key-not-real"),
    ))

    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5)

    assert "default-key-not-real" not in str(exc.value)
    assert "default-key-not-real" not in caplog.text
    assert "***" in str(exc.value)


def test_a_provider_that_needs_no_key_is_available(monkeypatch):
    """ローカルの provider (ollama / llama_cpp) はキーを宣言しなくても呼べる。"""
    monkeypatch.setenv(ROLE_ENV, OLLAMA_MODEL_KEY)
    _set_configs(monkeypatch, {OLLAMA_MODEL_KEY: {
        "model": "qwen3:8b",
        "protocol": "ollama_compat",
        "provider": "ollama",
        "context_length": 32000,
    }})

    assert is_available() is True


def test_a_provider_default_key_that_is_not_set_makes_it_unavailable(monkeypatch, caplog):
    """キーの env 名を宣言していない旧形式の設定でも、provider の既定キー名で見る。

    宣言が無いことを「キー不要」と読むと、キーの無い OpenAI 系のモデルを「使える」と
    答えてしまう。WARNING にはモデル設定キーを載せる (env の値そのものは載せない)。
    """
    monkeypatch.setenv(ROLE_ENV, OPENAI_LEGACY_MODEL_KEY)
    _set_configs(monkeypatch, {OPENAI_LEGACY_MODEL_KEY: {
        "model": "vendor/legacy",
        "protocol": "openai_compat",
        "provider": "openai",
        "context_length": 32000,
    }})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _refuse_llm_client(monkeypatch)
    caplog.set_level("WARNING", logger="saiverse.reflex_judgment")

    assert is_available() is False
    with pytest.raises(ReflexJudgmentUnavailable) as exc:
        evaluate(STATE, QUESTIONS, timeout=2.5)

    assert OPENAI_LEGACY_MODEL_KEY in str(exc.value)
    assert OPENAI_LEGACY_MODEL_KEY in caplog.text


def test_is_available_can_be_asked_about_a_specific_model(monkeypatch):
    """ペルソナ個別の上書きは model_key で渡る (役割の env には落ちない)。"""
    monkeypatch.delenv(ROLE_ENV, raising=False)
    monkeypatch.setenv(LLM_KEY_ENV, "test-key-not-real")
    _set_configs(monkeypatch, {LLM_MODEL_KEY: _llm_model_config()})

    # 役割は未割り当てなので、引数なしでは使えない。
    assert is_available() is False
    assert is_available(model_key=LLM_MODEL_KEY) is True
    assert resolve_backend(LLM_MODEL_KEY).model_key == LLM_MODEL_KEY
    assert is_available(model_key="not-a-model") is False


def test_the_prompt_carries_the_state_and_every_question(llm_role, monkeypatch):
    client = _FakeLLMClient({"answers": [
        {"qid": "n0", "noul": 0.9},
        {"qid": "c0", "choice": "walk"},
        {"qid": "s0", "score": 1.0},
    ]})
    _install_llm_client(monkeypatch, client)

    evaluate(STATE, MIXED_QUESTIONS, timeout=2.5)

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user"]
    prompt = messages[0]["content"]
    assert "アイフィの話" in prompt                     # state が JSON で載る
    for qid, question in MIXED_QUESTIONS.items():
        assert f"qid: {qid}" in prompt
        assert question["instructions"] in prompt
    assert "walk" in prompt and "stay" in prompt       # 選択肢も渡る
    # 会話ではなく判定であることを最初に言う。
    assert "not a conversational" in prompt
    # temperature は指定しない (モデル設定の既定に従う)。
    assert "temperature" not in client.calls[0]["kwargs"]


def _walk_schema(node, seen):
    """スキーマを再帰的に歩いて、辞書のキーを集める。"""
    if isinstance(node, dict):
        seen.update(node.keys())
        for value in node.values():
            _walk_schema(value, seen)
    elif isinstance(node, list):
        for value in node:
            _walk_schema(value, seen)


def test_the_response_schema_is_shaped_for_gemini(llm_role, monkeypatch):
    """Gemini でも通る形 — additionalProperties を使わず、qid は配列で受ける。

    qid は実行時に決まるので辞書スキーマではキーを列挙できず、Gemini は
    additionalProperties を受け付けない (docs/intent/reflex_judgment.md §3)。
    """
    client = _FakeLLMClient({"answers": [
        {"qid": "n0", "noul": 0.9},
        {"qid": "c0", "choice": "walk"},
        {"qid": "s0", "score": 1.0},
    ]})
    _install_llm_client(monkeypatch, client)

    evaluate(STATE, MIXED_QUESTIONS, timeout=2.5)

    schema = client.calls[0]["response_schema"]
    keys = set()
    _walk_schema(schema, keys)
    assert "additionalProperties" not in keys

    answers = schema["properties"]["answers"]
    assert answers["type"] == "array"
    item = answers["items"]
    assert item["type"] == "object"
    assert set(item["properties"]) == {"qid", "noul", "score", "choice"}
    assert item["required"] == ["qid"]
    # 選択肢が全部宣言されていれば enum でも縛る。
    assert item["properties"]["choice"]["enum"] == ["walk", "stay"]


def test_the_choice_field_is_not_bound_when_options_are_missing(llm_role, monkeypatch):
    """選択肢を宣言しない choice 質問が混ざったら enum で縛らない。

    縛ると、宣言の無い質問の正しい答えまで弾いてしまう。
    """
    client = _FakeLLMClient({"answers": [{"qid": "c0", "choice": "walk"},
                                         {"qid": "c1", "choice": "なんでも"}]})
    _install_llm_client(monkeypatch, client)

    evaluate(STATE, {
        "c0": {"type": "choice", "instructions": "どれを選ぶ", "options": ["walk", "stay"]},
        "c1": {"type": "choice", "instructions": "自由に答えて"},
    }, timeout=2.5)

    item = client.calls[0]["response_schema"]["properties"]["answers"]["items"]
    assert "enum" not in item["properties"]["choice"]
