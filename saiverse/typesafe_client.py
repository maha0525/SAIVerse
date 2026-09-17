"""TypeSafe System One API (判断専用モデル Jev) の薄い HTTP クライアント。

Jev は文章を生成しない。状況 (state) と型付きの質問 (questions) を 1 リクエストで
渡すと、質問ごとに型付きの答えが返る。ここで使うのは Noul (はい/いいえの確率
0〜1) だけ。

API 契約 (https://docs.typesafe.ai/api.md, https://docs.typesafe.ai/primitives/noul.md):

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <TYPESAFE_API_KEY>

    {"state": ..., "model": "jev-latest",
     "questions": {"<qid>": {"type": "noul", "instructions": "...",
                             "criteria": {"true": "...", "false": "..."}}}}

    -> {"model": "...", "answers": {"<qid>": {"type": "noul", "noul": 0.92}},
        "usage": {"input_tokens": 312, "output_tokens": 48}}

最初の利用者は自動想起の Jev 選別層 (docs/intent/auto_recall_jev_rerank.md)。
判断口は今後ツール選択・自律判断の実験でも使う想定があるため、auto_recall へ
埋め込まず独立モジュールにしてある。

失敗 (キー欠落・リクエスト準備の失敗・接続失敗・タイムアウト・絶対締切超過・
同時実行の上限超過・非 200・不正応答・部分回答・通信中に出た予期しない例外) は
すべて ``TypeSafeUnavailable`` に正規化する。呼び出し側はこれ 1 つを捕まえて、外部 API が
落ちていてもペルソナの返事が止まらない経路へフォールバックすること。
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Dict, Optional, Tuple

import httpx

LOGGER = logging.getLogger("saiverse.typesafe")

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

# ログ/例外メッセージに載せる外部由来テキストの最大文字数 (非 200 本文・HTTP 例外文言)。
_ERROR_BODY_PREVIEW = 200

# 壁時計の絶対締切に足す余裕 (秒)。httpx の timeout は接続・書き込み・読み取りの
# 各 I/O 単位なので、少量ずつ断続的に返す応答では合計が timeout を超えうる。
# 呼び出し全体を timeout + この余裕で打ち切る。
_DEADLINE_MARGIN = 0.5

# 締切超過で置き去りにしたワーカーが積み上がらないための同時実行の上限。
# 締切を超えた呼び出しはメインスレッドから client.close() で通信を切るので
# ワーカーは速やかに終わるが、それでも一度に走る本数はここで頭打ちにする
# (上限に達している間の呼び出しは待たずに TypeSafeUnavailable にする —
# 会話の同期経路にいるので、キュー待ちで返事を遅らせない)。
_MAX_IN_FLIGHT = 4
_IN_FLIGHT = threading.BoundedSemaphore(_MAX_IN_FLIGHT)

# ワーカースレッド名 (テストが居残りワーカーを join するための目印)。
_WORKER_THREAD_NAME = "typesafe-post"

# 返却・記録してよい usage のキー (許可リスト)。API が将来足すかもしれない
# 未知のキー (デバッグ用のエコーなど) を素通ししないため、明示した数値キーだけ写す。
_USAGE_KEYS = ("input_tokens", "output_tokens")


class TypeSafeUnavailable(Exception):
    """TypeSafe API が使えなかった。

    API キー欠落・接続失敗・タイムアウト・非 200・不正応答のすべてをこれに正規化する
    (呼び出し側が「今回は判定なし」として同じ扱いで畳めるようにするため)。
    """


def _mask_secret(text: str, secret: str) -> str:
    """``text`` 中の ``secret`` の出現をすべて ``***`` に置き換える。

    外部由来の文字列 (応答本文・HTTP 例外の文言・応答内の不正値) をログや例外
    メッセージに載せる前に通す。切り詰めより先に置換すること (先に切り詰めると
    キーの断片が残りうる)。

    生の値に加えて ``repr(secret)`` のエスケープ表現も落とす: httpx/h11 の例外や
    ``%r`` 経由の記録では、キーに含まれる改行やタブが ``\\n`` のような表現へ
    化けて生文字列の置換をすり抜けるため。
    """
    if not secret:
        return text
    masked = text.replace(secret, "***")
    escaped = repr(secret)[1:-1]
    if escaped and escaped != secret:
        masked = masked.replace(escaped, "***")
    return masked


def _masked_repr(value: Any, secret: str) -> str:
    """``repr(value)`` をキーの伏せ字化を通して返す (応答内の不正値を記録するとき用)。"""
    return _mask_secret(repr(value), secret)


def _clean_usage(raw: Any) -> Dict[str, Any]:
    """応答の ``usage`` から、記録・返却してよい数値キーだけを写した dict を作る。

    ``_USAGE_KEYS`` 以外のキーは捨てる (呼び出し側はこの dict を INFO ログへ
    そのまま載せるので、API が返した未知の値を記録に流さない)。bool は数値として
    扱わない。
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key in _USAGE_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        cleaned[key] = value
    return cleaned


def _noul_question(qid: str, question: Dict[str, Any]) -> Dict[str, Any]:
    """呼び出し側の質問定義を API のリクエスト形 (type=noul) へ整える。"""
    instructions = question.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        # 呼び出し側の契約違反 (API の障害ではない) なので正規化せずそのまま投げる。
        raise ValueError(f"question {qid!r} has no 'instructions' string")
    payload: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    criteria = question.get("criteria")
    if criteria:
        payload["criteria"] = criteria
    return payload


def evaluate_nouls(
    state: Any,
    questions: Dict[str, Dict[str, Any]],
    *,
    timeout: float,
    model: str = DEFAULT_MODEL,
    transport: Optional[Any] = None,
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """全質問を 1 リクエストで評価し ``({qid: noul確率}, usage情報)`` を返す。

    Args:
        state: 判断の材料になる状況。文字列でも dict/list でもよい (API がそのまま受ける)。
        questions: ``{qid: {"instructions": str, "criteria": {"true": ..., "false": ...}}}``。
            ``criteria`` は省略可。``type`` はこの関数が付ける。
        timeout: HTTP タイムアウト (秒)。
        model: 使用モデル。既定 ``jev-latest``。
        transport: テストからトランスポート (``httpx.MockTransport``) を差し込むための口。
            本番経路では常に None。

    Returns:
        (qid -> noul 確率 (0〜1), usage dict)。**要求した全 qid が揃っている**ことを
        保証する — 一部だけ答えが返った応答は成功にせず ``TypeSafeUnavailable`` を
        投げる (部分的な判定を「判定した」として扱うと、答えの無い候補が静かに
        不採用になり、外部 API の劣化が判定結果に化けてしまうため)。
        usage は ``input_tokens`` / ``output_tokens`` のうち数値だったものだけを
        写した dict (許可リスト方式。応答の他のキーは返さないし記録もしない)。

    Raises:
        TypeSafeUnavailable: API キー欠落・リクエスト準備の失敗 (HTTP クライアント
            生成・ワーカースレッド起動)・接続失敗・タイムアウト・絶対締切超過・
            同時実行の上限超過・非 200・不正応答 (JSON でない / ``answers`` が無い /
            qid の欠落 / ``type`` が ``noul`` でない / ``noul`` が数値でない・
            非有限・0〜1 の外)・通信中に出た予期しない例外 (httpx 以外の型も含む)。
        ValueError: ``questions`` の形が契約を満たしていない (呼び出し側のバグ)。
    """
    api_key = (os.getenv("TYPESAFE_API_KEY") or "").strip()
    if not api_key:
        raise TypeSafeUnavailable("TYPESAFE_API_KEY is not set")

    if not questions:
        return {}, {}

    payload = {
        "state": state,
        "model": model,
        "questions": {qid: _noul_question(qid, q) for qid, q in questions.items()},
    }
    # API キーの値そのものはログに出さない (ヘッダごとダンプしない)。
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    LOGGER.debug(
        "[typesafe] POST %s model=%s questions=%d timeout=%.1fs",
        API_URL, model, len(payload["questions"]), timeout,
    )

    # 同時実行の上限。待たずに弾く (会話の同期経路なのでキュー待ちさせない)。
    if not _IN_FLIGHT.acquire(blocking=False):
        # INFO であって WARNING ではない: 上限到達は障害ではなく平常動作
        # (会話の同期経路でキュー待ちさせないための設計。intent の失敗時の節)。
        # 賑やかな時間帯に毎ターン WARNING が並ぶと、効果測定でログを読む人が
        # 「壊れている」と誤読する。件数自体は効果測定で見たいので DEBUG にもしない。
        LOGGER.info("[typesafe] too many in-flight calls (limit=%d); skipping", _MAX_IN_FLIGHT)
        raise TypeSafeUnavailable(f"too many in-flight calls (limit={_MAX_IN_FLIGHT})")

    # acquire に成功してから「ワーカーの start() が成功する」までは、枠の所有権が
    # メインスレッド側にある。この区間で例外が飛んで枠を返さないまま抜けると、枠は
    # 永久に失われる (例: SSL 関連の環境変数が壊れていて httpx.Client の生成が毎回
    # 失敗すると、4 回で枠が尽き、環境変数を直してもプロセスを再起動するまで全
    # ペルソナの判定が復旧しない)。だから区間全体を try で包み、どの例外でも枠を返す。
    client: Optional[httpx.Client] = None
    try:
        # client はメインスレッド側で作る。締切を超えたときに、ここから close() して
        # 置き去りのワーカーの通信を切れるようにするため。
        client = httpx.Client(timeout=timeout, transport=transport)
        holder: Dict[str, Any] = {}
        done = threading.Event()

        def _post() -> None:
            try:
                holder["response"] = client.post(API_URL, json=payload, headers=headers)
            except BaseException as exc:  # noqa: BLE001 - 例外はメインスレッドへ運んで再送出する
                holder["error"] = exc
            finally:
                # close は冪等 (メイン側が締切超過で先に閉じていてもよい)。
                try:
                    client.close()
                except Exception:
                    pass
                # start() 成功後のセマフォの release はここ 1 箇所だけ。締切超過で
                # メインが先に戻ってもメイン側は release しないので、二重 release
                # (BoundedSemaphore の ValueError) は構造上起きない。
                _IN_FLIGHT.release()
                done.set()

        # ワーカーは daemon。締切超過で置き去りにした通信が、プロセス終了時の join を
        # 妨げないようにする (非デーモンだと Python の終了そのものが止まりうる)。
        worker = threading.Thread(target=_post, name=_WORKER_THREAD_NAME, daemon=True)
        worker.start()
    except BaseException as exc:
        # start() から戻る前の失敗では、ワーカーの finally はまず走らない。
        # ここでの release は 1 回だけで、ワーカー側の release と重なることはない。
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        _IN_FLIGHT.release()
        detail = _mask_secret(f"{type(exc).__name__}: {exc}", api_key)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning("[typesafe] could not start the request: %s", detail)
        raise TypeSafeUnavailable(f"could not start the request: {detail}") from exc

    # 絶対締切 (壁時計)。httpx の timeout は I/O 単位なので、少量ずつ返し続ける
    # 応答は合計時間が青天井になりうる。会話の同期経路にいるので全体を打ち切る。
    deadline = timeout + _DEADLINE_MARGIN
    if not done.wait(deadline):
        # 通信を切ってワーカーを終わらせる (ワーカー側の post が例外で戻り、
        # その finally でセマフォが返る)。会話を待たせないことが優先。
        try:
            client.close()
        except Exception:
            pass
        LOGGER.warning("[typesafe] deadline exceeded after %.1fs; abandoning the call", deadline)
        raise TypeSafeUnavailable(f"deadline exceeded after {deadline:.1f}s")

    error = holder.get("error")
    if error is not None:
        # 例外の型を問わず TypeSafeUnavailable に正規化する。httpx 以外の例外
        # (トランスポート実装や差し込まれたフックが投げるもの) を素通しすると、
        # 呼び出し側が 1 種類の例外を捕まえるだけで畳めるという契約が破れる。
        # 文言にはヘッダ値 (= API キー) が混ざる型がある (h11 の Illegal header
        # value など) ので、ログにも例外メッセージにも伏せ字で載せる。
        detail = _mask_secret(f"{type(error).__name__}: {error}", api_key)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning("[typesafe] request failed: %s", detail)
        raise TypeSafeUnavailable(f"request failed: {detail}") from error

    response = holder["response"]

    if response.status_code != 200:
        # 応答本文に API キーがそのまま載っている実装もありうるので、プレビューを
        # 作る前にキーの値を伏せる (デバッグ価値のある本文自体は残す)。
        body = _mask_secret(response.text or "", api_key)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning("[typesafe] non-200 response: status=%s body=%r", response.status_code, body)
        raise TypeSafeUnavailable(f"HTTP {response.status_code}: {body}")

    try:
        data = response.json()
    except ValueError as exc:
        LOGGER.warning("[typesafe] response is not valid JSON: %s", exc)
        raise TypeSafeUnavailable(f"invalid JSON response: {exc}") from exc

    if not isinstance(data, dict):
        LOGGER.warning("[typesafe] response is not a JSON object: %s", type(data).__name__)
        raise TypeSafeUnavailable("response is not a JSON object")

    answers = data.get("answers")
    if not isinstance(answers, dict):
        LOGGER.warning("[typesafe] response has no 'answers' object")
        raise TypeSafeUnavailable("response has no 'answers' object")

    # 全 qid が揃っていることを要求する (部分回答は成功にしない)。
    nouls: Dict[str, float] = {}
    for qid in questions:
        answer = answers.get(qid)
        if not isinstance(answer, dict):
            LOGGER.warning("[typesafe] no answer object for qid=%r", qid)
            raise TypeSafeUnavailable(f"no answer for question {qid!r}")
        answer_type = answer.get("type")
        if answer_type != "noul":
            # 応答由来の値はすべて伏せ字化を通してから記録する (キーがそのまま
            # エコーされている応答でも、記録に残さないため)。
            shown = _masked_repr(answer_type, api_key)
            LOGGER.warning("[typesafe] answer for qid=%r has type=%s (expected 'noul')", qid, shown)
            raise TypeSafeUnavailable(f"answer for question {qid!r} has type {shown}, expected 'noul'")
        value = answer.get("noul")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            LOGGER.warning(
                "[typesafe] answer for qid=%r has non-numeric 'noul': %s",
                qid, _masked_repr(value, api_key),
            )
            raise TypeSafeUnavailable(f"answer for question {qid!r} has non-numeric 'noul'")
        value = float(value)
        if not math.isfinite(value) or not (0.0 <= value <= 1.0):
            shown = _masked_repr(value, api_key)
            LOGGER.warning("[typesafe] answer for qid=%r has out-of-range 'noul': %s", qid, shown)
            raise TypeSafeUnavailable(f"answer for question {qid!r} has out-of-range 'noul': {shown}")
        nouls[qid] = value

    usage = _clean_usage(data.get("usage"))

    LOGGER.debug("[typesafe] answered %d/%d question(s) usage=%s", len(nouls), len(questions), usage)
    return nouls, usage
