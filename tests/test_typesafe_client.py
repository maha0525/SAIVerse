"""TypeSafe System One API クライアント (saiverse/typesafe_client.py) のユニットテスト。

実 API は絶対に呼ばない。httpx.MockTransport でリクエスト/レスポンスを差し替え、
API キーは monkeypatch で偽値を入れる。

固定する契約 (docs.typesafe.ai/api.md と /primitives/noul.md で裏取り済み):
- POST https://api.typesafe.ai/v1/systemone、Authorization: Bearer <key>
- リクエスト: {"state", "model", "questions": {qid: {"type": "noul", "instructions", "criteria"}}}
- レスポンス: {"model", "answers": {qid: {"type": "noul", "noul": 0.92}}, "usage": {...}}
- 成功は「要求した全 qid に妥当な noul (0〜1 の有限数) が返った」ときだけ。
  部分回答・型違い・範囲外は TypeSafeUnavailable。
"""

import json
import threading
import time
from unittest.mock import patch

import httpx
import pytest

from saiverse import typesafe_client
from saiverse.typesafe_client import (
    API_URL,
    TypeSafeUnavailable,
    _mask_secret,
    evaluate_nouls,
)

QUESTIONS = {
    "m0": {
        "instructions": "`memories.m0` は今の話に浮かぶ記憶か。",
        "criteria": {"true": "関連する", "false": "関連しない"},
    },
    "m1": {
        "instructions": "`memories.m1` は今の話に浮かぶ記憶か。",
        "criteria": {"true": "関連する", "false": "関連しない"},
    },
}

STATE = {"conversation": [{"role": "user", "text": "アイフィの話"}], "memories": {}}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-real")


@pytest.fixture(autouse=True)
def _drain_workers():
    """テストが置き去りにしたワーカーを次のテストへ持ち越さない。

    締切超過のテストはワーカーを走らせたまま戻る。ワーカーが終わるまで同時実行
    セマフォの枠を掴んだままなので、テスト間で枠が漏れないようここで回収する。
    """
    yield
    _join_workers()


def _join_workers(timeout=10.0):
    """居残っている typesafe のワーカースレッドが終わるまで待つ。"""
    for thread in threading.enumerate():
        if thread.name == typesafe_client._WORKER_THREAD_NAME:
            thread.join(timeout)


def _transport(handler):
    return httpx.MockTransport(handler)


def _all_answered(*qids, noul=0.5):
    """要求された全 qid に妥当な答えを返す応答ボディ (新契約を満たす最小形)。"""
    return {"model": "jev-latest", "answers": {qid: {"type": "noul", "noul": noul} for qid in qids}}


def test_parses_noul_answers_and_usage():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {
                    "m0": {"type": "noul", "noul": 0.92},
                    "m1": {"type": "noul", "noul": 0.08},
                },
                "usage": {"input_tokens": 312, "output_tokens": 48},
            },
        )

    nouls, usage = evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert nouls == {"m0": 0.92, "m1": 0.08}
    assert usage == {"input_tokens": 312, "output_tokens": 48}


def test_request_shape_and_authorization_header():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert captured["method"] == "POST"
    assert captured["url"] == API_URL
    assert captured["auth"] == "Bearer test-key-not-real"

    body = captured["body"]
    assert body["model"] == "jev-latest"
    assert body["state"] == STATE
    assert set(body["questions"]) == {"m0", "m1"}
    q0 = body["questions"]["m0"]
    assert q0["type"] == "noul"
    assert q0["instructions"] == QUESTIONS["m0"]["instructions"]
    assert q0["criteria"] == {"true": "関連する", "false": "関連しない"}


def test_model_override_is_sent():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate_nouls(STATE, QUESTIONS, timeout=2.5, model="jev-1", transport=_transport(handler))
    assert captured["body"]["model"] == "jev-1"


def test_criteria_omitted_when_absent():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_all_answered("m0"))

    evaluate_nouls(
        STATE, {"m0": {"instructions": "質問だけ"}}, timeout=2.5, transport=_transport(handler),
    )
    assert "criteria" not in captured["body"]["questions"]["m0"]


def test_usage_absent_is_empty_dict_not_an_error():
    def handler(request):
        # 全 qid は答えているが usage キーが無い応答。
        return httpx.Response(200, json=_all_answered("m0", "m1", noul=0.7))

    nouls, usage = evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert nouls == {"m0": 0.7, "m1": 0.7}
    assert usage == {}


def test_missing_answer_qid_raises_unavailable():
    """部分回答は成功にしない (欠けた qid が静かに不採用になるのを防ぐ)。"""
    def handler(request):
        # m1 の answer が欠けている応答。
        return httpx.Response(
            200,
            json={"model": "jev-latest", "answers": {"m0": {"type": "noul", "noul": 0.7}}},
        )

    with pytest.raises(TypeSafeUnavailable) as exc:
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
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
        '{"model": "jev-latest", "answers": {'
        f'"m0": {bad_answer_json}, '
        '"m1": {"type": "noul", "noul": 0.3}}}'
    )

    def handler(request):
        return httpx.Response(200, text=body, headers={"Content-Type": "application/json"})

    with pytest.raises(TypeSafeUnavailable):
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_missing_api_key_raises_unavailable(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")

    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("API must not be called without a key")

    with pytest.raises(TypeSafeUnavailable):
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_non_200_raises_unavailable():
    def handler(request):
        return httpx.Response(500, text="internal error")

    with pytest.raises(TypeSafeUnavailable) as exc:
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert "500" in str(exc.value)


def test_timeout_raises_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TypeSafeUnavailable) as exc:
        evaluate_nouls(STATE, QUESTIONS, timeout=0.1, transport=_transport(handler))
    assert "ReadTimeout" in str(exc.value)


def test_connection_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TypeSafeUnavailable):
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_invalid_json_raises_unavailable():
    def handler(request):
        return httpx.Response(200, text="not json at all")

    with pytest.raises(TypeSafeUnavailable):
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_response_without_answers_object_raises_unavailable():
    def handler(request):
        return httpx.Response(200, json={"model": "jev-latest"})

    with pytest.raises(TypeSafeUnavailable):
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))


def test_empty_questions_short_circuits_without_request():
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("API must not be called with no questions")

    nouls, usage = evaluate_nouls(STATE, {}, timeout=2.5, transport=_transport(handler))
    assert nouls == {}
    assert usage == {}


def test_question_without_instructions_is_a_caller_error():
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("API must not be called with a malformed question")

    with pytest.raises(ValueError):
        evaluate_nouls(STATE, {"m0": {"criteria": {}}}, timeout=2.5, transport=_transport(handler))


def test_api_key_value_never_logged(caplog):
    caplog.set_level("DEBUG", logger="saiverse.typesafe")

    def handler(request):
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert "test-key-not-real" not in caplog.text


def test_api_key_in_error_body_is_masked(caplog):
    """エラー本文にキーがそのまま載っていても、ログにも例外にも出さない。"""
    caplog.set_level("DEBUG", logger="saiverse.typesafe")

    def handler(request):
        return httpx.Response(
            401,
            text='{"error": "invalid api key: test-key-not-real (Bearer test-key-not-real)"}',
        )

    with pytest.raises(TypeSafeUnavailable) as exc:
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert "test-key-not-real" not in str(exc.value)
    assert "test-key-not-real" not in caplog.text
    # 本文プレビュー自体は残す (デバッグ価値があるので全面秘匿はしない)。
    assert "invalid api key" in str(exc.value)
    assert "***" in str(exc.value)


def test_http_error_text_containing_the_key_is_masked(caplog):
    """HTTP 例外の文言にキーが載っていても、ログにも例外にも出さない。

    httpx/h11 の例外はヘッダ値 (= Authorization のキー) を文言に含む型がある。
    """
    caplog.set_level("DEBUG", logger="saiverse.typesafe")

    def handler(request):
        raise httpx.ConnectError(
            "illegal header value b'Bearer test-key-not-real'", request=request,
        )

    with pytest.raises(TypeSafeUnavailable) as exc:
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert "test-key-not-real" not in str(exc.value)
    assert "test-key-not-real" not in caplog.text
    # 何が起きたかは残す (デバッグ価値)。
    assert "ConnectError" in str(exc.value)
    assert "***" in str(exc.value)


def test_non_http_exception_is_normalized_and_masked(caplog):
    """httpx 以外の例外も TypeSafeUnavailable に正規化し、キーは伏せる。

    素通しすると「失敗はすべて TypeSafeUnavailable」という契約が破れ、呼び出し側が
    1 種類の例外を捕まえるだけでは畳めなくなる (会話の同期経路に例外が抜ける)。
    """
    caplog.set_level("DEBUG", logger="saiverse.typesafe")

    def handler(request):
        raise ValueError("unexpected failure while sending test-key-not-real")

    with pytest.raises(TypeSafeUnavailable) as exc:
        evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))

    assert "test-key-not-real" not in str(exc.value)
    assert "test-key-not-real" not in caplog.text
    # 何が起きたかは残す (デバッグ価値)。
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


def test_unknown_usage_keys_are_dropped(caplog):
    """usage は許可リスト方式。API が返した未知のキーは返さないし記録もしない。"""
    caplog.set_level("DEBUG", logger="saiverse.typesafe")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {
                    "m0": {"type": "noul", "noul": 0.4},
                    "m1": {"type": "noul", "noul": 0.6},
                },
                "usage": {"input_tokens": 10, "output_tokens": 2, "debug": "test-key-not-real"},
            },
        )

    nouls, usage = evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert nouls == {"m0": 0.4, "m1": 0.6}
    assert usage == {"input_tokens": 10, "output_tokens": 2}

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "debug" not in logged
    assert "test-key-not-real" not in logged


def test_non_numeric_usage_values_are_dropped():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {qid: {"type": "noul", "noul": 0.5} for qid in ("m0", "m1")},
                "usage": {"input_tokens": "312", "output_tokens": 48},
            },
        )

    _, usage = evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert usage == {"output_tokens": 48}


def test_deadline_exceeded_raises_unavailable():
    """各 I/O が短くても、呼び出し全体が締切を超えたら打ち切る。"""
    def handler(request):
        time.sleep(1.0)
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    started = time.monotonic()
    with pytest.raises(TypeSafeUnavailable) as exc:
        evaluate_nouls(STATE, QUESTIONS, timeout=0.1, transport=_transport(handler))
    elapsed = time.monotonic() - started

    assert "deadline exceeded" in str(exc.value)
    # ワーカーの完了 (1.0 秒) を待たずに戻る。
    assert elapsed < 0.95


def test_worker_thread_is_a_daemon():
    """置き去りにしたワーカーがプロセス終了を止めないこと (非デーモンだと join される)。"""
    seen = {}

    def handler(request):
        current = threading.current_thread()
        seen["daemon"] = current.daemon
        seen["name"] = current.name
        return httpx.Response(200, json=_all_answered("m0", "m1"))

    evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert seen["daemon"] is True
    assert seen["name"] == typesafe_client._WORKER_THREAD_NAME


def test_too_many_in_flight_calls_is_unavailable():
    """同時実行の枠が埋まっていたら、待たずに TypeSafeUnavailable にする。"""
    def handler(request):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("API must not be called while the limit is reached")

    acquired = 0
    try:
        for _ in range(typesafe_client._MAX_IN_FLIGHT):
            assert typesafe_client._IN_FLIGHT.acquire(blocking=False) is True
            acquired += 1

        started = time.monotonic()
        with pytest.raises(TypeSafeUnavailable) as exc:
            evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
        elapsed = time.monotonic() - started
    finally:
        for _ in range(acquired):
            typesafe_client._IN_FLIGHT.release()

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
        with pytest.raises(TypeSafeUnavailable) as exc:
            evaluate_nouls(STATE, QUESTIONS, timeout=0.1, transport=_transport(slow_handler))
        assert "deadline exceeded" in str(exc.value)

    # ワーカーが終われば枠は返っている (二重 release で壊れてもいない)。
    _join_workers()
    for _ in range(typesafe_client._MAX_IN_FLIGHT):
        assert typesafe_client._IN_FLIGHT.acquire(blocking=False) is True
    for _ in range(typesafe_client._MAX_IN_FLIGHT):
        typesafe_client._IN_FLIGHT.release()

    def fast_handler(request):
        return httpx.Response(200, json=_all_answered("m0", "m1", noul=0.42))

    nouls, _usage = evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(fast_handler))
    assert nouls == {"m0": 0.42, "m1": 0.42}


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

    attempts = typesafe_client._MAX_IN_FLIGHT + 1
    with patch.object(typesafe_client.httpx, "Client", _explode):
        for _ in range(attempts):
            with pytest.raises(TypeSafeUnavailable) as exc:
                evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
            # 枠が漏れていれば、途中から失敗の理由が「枠が無い」に変わる。
            assert "too many in-flight calls" not in str(exc.value)
            assert "RuntimeError" in str(exc.value)

    # 枠は全部空いている (失敗のたびに返っていた)。
    for _ in range(typesafe_client._MAX_IN_FLIGHT):
        assert typesafe_client._IN_FLIGHT.acquire(blocking=False) is True
    for _ in range(typesafe_client._MAX_IN_FLIGHT):
        typesafe_client._IN_FLIGHT.release()

    # 原因が直れば (= Client が作れれば) そのまま使える。
    nouls, _usage = evaluate_nouls(STATE, QUESTIONS, timeout=2.5, transport=_transport(handler))
    assert nouls == {"m0": 0.55, "m1": 0.55}
