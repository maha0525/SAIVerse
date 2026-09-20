"""反射判断 — 状況と型付きの質問を渡すと、確率・選択・数値だけが返る判断の層。

設計の正典は docs/intent/reflex_judgment.md。考えて文章を組み立てる判断 (通常の
LLM 呼び出し) より速く・安く・形が保証される代わりに、自由記述はできない。

**答える側の決め方**: モデルの役割「反射判断」(``saiverse/model_defaults.py`` の
``MODEL_ROLES["reflex_judgment_model"]``、世界の既定は env
``SAIVERSE_REFLEX_JUDGMENT_MODEL``) に割り当てられたモデル設定を、既存の 3 層読み込み
(``saiverse/model_configs.py``) で引く。そのモデルの provider の protocol が
``jev_compat`` なら、System One の形をそのまま送る。宛先・キーの env 名・応答の欄の
方言・対応する質問の型は、すべて provider / モデル設定ファイルの ``reflex_judgment``
欄の宣言から取る (提供元ごとの分岐をこのコードには置かない)。

第 1 段では通常の LLM プロトコル (openai_compat / gemini_native など) への変換層は
無い。役割に jev 互換でないモデルが割り当てられていたら「使えなかった」として
``ReflexJudgmentUnavailable`` を投げ、理由を WARNING に出す。

**リクエストの形** (TypeSafe 公式 https://docs.typesafe.ai/api.md が正典。互換勢も
中身は同じで、違うのは宛先の path・認証・応答の欄の名前・対応する型):

    POST <base_url><path>
    Authorization: Bearer <api_key_env の値>

    {"state": ..., "model": "<モデル設定の model 欄>",
     "questions": {"<qid>": {"type": "noul", "instructions": "...",
                             "criteria": {"true": "...", "false": "..."}}}}

    -> {"answers": {"<qid>": {"type": "noul", "noul": 0.92}},
        "usage": {"input_tokens": 312, "output_tokens": 48}}

失敗 (役割が未割り当て・モデル設定が無い・provider が jev 互換でない・キー欠落・
リクエスト準備の失敗・接続失敗・タイムアウト・絶対締切超過・同時実行の上限超過・
非 200・不正応答・部分回答・通信中に出た予期しない例外) はすべて
``ReflexJudgmentUnavailable`` に正規化する。呼び出し側はこれ 1 つを捕まえて、
外部 API が落ちていてもペルソナの返事が止まらない経路へフォールバックすること
(そのターンをどう凌ぐかは各機能の設計が持つ — intent §4)。

``saiverse/typesafe_client.py`` を置き換えたモジュール (2026-09-20)。会話の返事を
待たせる場所で使う前提の器 — 同時本数の上限 (プロセス全体で 4 本)・実際の経過時間で
打ち切る締切・キーの伏せ字・全 qid の揃わない応答の不成立 — はそちらで実証済みの
形をそのまま引き継いでいる。
"""

from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

LOGGER = logging.getLogger("saiverse.reflex_judgment")

#: 反射判断が話せる provider の protocol。
JEV_COMPAT_PROTOCOL = "jev_compat"

#: 質問の型 (System One の 3 型)。第 1 段の利用者 (自動想起の選別) は noul だけを
#: 使うが、器は 3 型とも通す。
QUESTION_TYPES = ("noul", "choice", "score")

#: 第 1 段でこの層に仕事を頼む唯一の機能 (自動想起の選別) が投げる質問の型。
#:
#: 「この宛先は使えるか」を答えるとき、protocol と宛先の URL が揃っているだけでは
#: 足りない — choice にしか答えない宛先を「使える」と答えると、自動想起は候補の
#: 集め方だけを広げたまま毎ターン判定に失敗する。自動想起 (sea/auto_recall.py) と
#: 画面の警告 (saiverse/model_defaults.py) は、この型に答えられることまでを
#: 「噛み合っている」の条件にする。
FIRST_STAGE_QUESTION_TYPE = "noul"

#: provider / モデル設定で方言を宣言する欄の名前。
DIALECT_FIELD = "reflex_judgment"

#: 方言の宣言が無い欄の既定 (TypeSafe 公式 = 正典の形)。
_DEFAULT_PATH = "/v1/systemone"
_DEFAULT_ANSWERS_KEY = "answers"
_DEFAULT_USAGE_KEY = "usage"
_DEFAULT_ANSWER_FIELDS = {"noul": "noul", "choice": "choice", "score": "score"}
_DEFAULT_USAGE_FIELDS = {"input_tokens": "input_tokens", "output_tokens": "output_tokens"}

# ログ/例外メッセージに載せる外部由来テキストの最大文字数 (非 200 本文・HTTP 例外文言)。
_ERROR_BODY_PREVIEW = 200

# 壁時計の絶対締切に足す余裕 (秒)。httpx の timeout は接続・書き込み・読み取りの
# 各 I/O 単位なので、少量ずつ断続的に返す応答では合計が timeout を超えうる。
# 呼び出し全体を timeout + この余裕で打ち切る。
_DEADLINE_MARGIN = 0.5

# 締切超過で置き去りにしたワーカーが積み上がらないための同時実行の上限。
# 締切を超えた呼び出しはメインスレッドから client.close() で通信を切るので
# ワーカーは速やかに終わるが、それでも一度に走る本数はここで頭打ちにする
# (上限に達している間の呼び出しは待たずに ReflexJudgmentUnavailable にする —
# 会話の同期経路にいるので、キュー待ちで返事を遅らせない)。
# close に応じない transport ではワーカーが枠を持ち続けうるが、その場合の症状は
# 毎回の「too many in-flight calls」INFO で観測でき、会話は従来方式へ戻る
# (壊れ方は費用ゼロ側)。
_MAX_IN_FLIGHT = 4
_IN_FLIGHT = threading.BoundedSemaphore(_MAX_IN_FLIGHT)

# ワーカースレッド名 (テストが居残りワーカーを join するための目印)。
_WORKER_THREAD_NAME = "reflex-judgment-post"

#: 使用量の記帳に使う区分 (saiverse/usage_tracker.py の category / node_type)。
USAGE_CATEGORY = "reflex_judgment"


class ReflexJudgmentUnavailable(Exception):
    """反射判断が使えなかった。

    役割の未割り当て・モデル設定の欠落・jev 互換でない provider・API キー欠落・
    接続失敗・タイムアウト・非 200・不正応答のすべてをこれに正規化する
    (呼び出し側が「今回は判定なし」として同じ扱いで畳めるようにするため)。
    """


@dataclass(frozen=True)
class ReflexBackend:
    """答える側 1 つ分の解決結果 (モデル設定 + provider の宣言から組む)。

    Attributes:
        model_key: モデル設定のキー (ファイル名の stem)。**使用量と費用はこの名義で
            記帳する** — 答える側を替えても費用が同じ場所に見える (intent §2)。
        api_model: リクエストの ``model`` 欄に載せる API 上のモデル名。
        url: 宛先 (provider の ``base_url`` + 宣言された ``path``)。
        api_key_env: 資格情報を読む環境変数名 (無ければ None)。
        api_key_required: False なら認証しない宛先 (localjev など)。
        answers_key / usage_key: 応答のどの欄に答え / 使用量が載るか。
        answer_fields: 質問の型 → 答えの数値が載る欄の名前。
        usage_fields: 記帳する使用量の名前 → 応答の欄の名前。
        supported_types: この宛先が答えられる質問の型。
    """

    model_key: str
    api_model: str
    url: str
    api_key_env: Optional[str]
    api_key_required: bool
    answers_key: str
    usage_key: str
    answer_fields: Mapping[str, str]
    usage_fields: Mapping[str, str]
    supported_types: frozenset


# ---------------------------------------------------------------------------
# 答える側の解決
# ---------------------------------------------------------------------------


def reflex_model_setting() -> Optional[str]:
    """モデルの役割「反射判断」に割り当てられたモデル設定キー。未設定なら None。"""
    from saiverse.model_defaults import MODEL_ROLES

    value = os.getenv(MODEL_ROLES["reflex_judgment_model"]) or ""
    return value.strip() or None


def _dialect(config: Mapping[str, Any]) -> Dict[str, Any]:
    raw = config.get(DIALECT_FIELD)
    return dict(raw) if isinstance(raw, dict) else {}


def _string_map(raw: Any, default: Mapping[str, str]) -> Dict[str, str]:
    """``{名前: 欄名}`` の宣言を読む。宣言が無い / 形が違うキーは既定のまま。"""
    merged = dict(default)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                merged[key] = value
    return merged


def resolve_backend(
    model_key: Optional[str] = None, *, required_type: Optional[str] = None,
) -> ReflexBackend:
    """答える側を解決する。使えない理由はすべて例外 1 種類で返す。

    Args:
        model_key: モデル設定キー。省略すると役割 (env) に割り当てられた値を使う。
        required_type: 「この型の質問に答えられること」を解決の条件に足す。
            省略すると型は問わない (器としての解決)。呼び出し側が投げる型が決まって
            いるときは渡すこと — 答えられない宛先を「使える」と答えると、呼び出し側は
            その前提で動き出したまま毎ターン判定に失敗する。

    Raises:
        ReflexJudgmentUnavailable: 役割が未割り当て / 設定が無い / provider が
            jev 互換でない / キーと宛先の組が照合に通らない / 宛先が宣言されて
            いない / ``required_type`` に答えられない。理由は WARNING に出す
            (設定ミスは黙って「効かないだけ」にしない — intent §2)。
    """
    from saiverse import model_configs

    key = (model_key or reflex_model_setting() or "").strip()
    if not key:
        # 未割り当ては平常の状態 (既定は役割なし = 反射判断は動かない)。呼び出し側の
        # スイッチが ON のときだけここへ来るので DEBUG ではなく INFO にしておく。
        LOGGER.info(
            "[reflex] no model is assigned to the reflex_judgment role; "
            "reflex judgment is unavailable"
        )
        raise ReflexJudgmentUnavailable("no model is assigned to the reflex_judgment role")

    config = model_configs.MODEL_CONFIGS.get(key)
    if not config:
        LOGGER.warning(
            "[reflex] model config %r was not found; reflex judgment is unavailable", key,
        )
        raise ReflexJudgmentUnavailable(f"model config {key!r} was not found")

    protocol = config.get("protocol") or config.get("provider")
    if protocol != JEV_COMPAT_PROTOCOL:
        # 第 1 段には通常 LLM への変換層が無い (intent §6-4)。「効かない」を黙って
        # 続けないよう、割り当てが噛み合っていないことをそのまま言う。
        LOGGER.warning(
            "[reflex] model %r speaks protocol %r, not %r; reflex judgment needs a "
            "jev-compatible destination (converting typed questions into a prompt for "
            "an ordinary LLM comes in the second stage)",
            key, protocol, JEV_COMPAT_PROTOCOL,
        )
        raise ReflexJudgmentUnavailable(
            f"model {key!r} is not on a {JEV_COMPAT_PROTOCOL} provider (protocol={protocol!r})"
        )

    # キーと宛先の組の照合。通常の会話クライアントは接続を作る前にこれを必ず通る
    # (llm_clients/factory.py)。反射判断だけ素通りさせると、拡張データや user_data の
    # provider が ``api_key_env`` に他所のキーの env 名を書き、宛先を自分の好きな所に
    # 向けるだけで、そのキーが第三者へ送られる。intent §2 が「既存の provider 照合が
    # 守る」と書いている約束は、この呼び出しがあって初めて成立する。
    try:
        from saiverse.provider_security import validate_model_config_connection

        validate_model_config_connection(key, config)
    except Exception as exc:
        LOGGER.warning(
            "[reflex] model %r did not pass the credential/destination check: %s: %s",
            key, type(exc).__name__, exc,
        )
        raise ReflexJudgmentUnavailable(
            f"model {key!r} did not pass the credential/destination check: {exc}"
        ) from exc

    base_url = str(config.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        LOGGER.warning("[reflex] model %r declares no base_url; reflex judgment is unavailable", key)
        raise ReflexJudgmentUnavailable(f"model {key!r} declares no base_url")

    dialect = _dialect(config)
    path = str(dialect.get("path") or _DEFAULT_PATH)
    if not path.startswith("/"):
        path = "/" + path

    raw_types = dialect.get("supported_types")
    if isinstance(raw_types, (list, tuple)):
        supported = frozenset(t for t in raw_types if t in QUESTION_TYPES)
    else:
        supported = frozenset(QUESTION_TYPES)
    if not supported:
        LOGGER.warning(
            "[reflex] model %r declares no usable question type; reflex judgment is unavailable", key,
        )
        raise ReflexJudgmentUnavailable(f"model {key!r} declares no usable question type")

    if required_type is not None and required_type not in supported:
        LOGGER.warning(
            "[reflex] model %r answers %s but the caller asks %r questions; "
            "reflex judgment is unavailable for that work",
            key, sorted(supported), required_type,
        )
        raise ReflexJudgmentUnavailable(
            f"model {key!r} does not answer {required_type!r} questions"
        )

    api_model = str(config.get("model") or key)
    api_key_env = config.get("api_key_env")
    api_key_env = api_key_env.strip() if isinstance(api_key_env, str) and api_key_env.strip() else None

    return ReflexBackend(
        model_key=key,
        api_model=api_model,
        url=f"{base_url}{path}",
        api_key_env=api_key_env,
        api_key_required=config.get("api_key_required") is not False,
        answers_key=str(dialect.get("answers_key") or _DEFAULT_ANSWERS_KEY),
        usage_key=str(dialect.get("usage_key") or _DEFAULT_USAGE_KEY),
        answer_fields=_string_map(dialect.get("answer_fields"), _DEFAULT_ANSWER_FIELDS),
        usage_fields=_string_map(dialect.get("usage_fields"), _DEFAULT_USAGE_FIELDS),
        supported_types=supported,
    )


def _read_api_key(backend: ReflexBackend) -> str:
    """宛先が宣言した env から資格情報を読む (宣言が無ければ空文字)。"""
    if not backend.api_key_env:
        return ""
    return (os.getenv(backend.api_key_env) or "").strip()


def _warn_missing_api_key(backend: ReflexBackend) -> None:
    """キーが要る宛先なのに env が空だったことを WARNING に出す。

    設定ミスが続いている間は毎ターン出る。それでよい — 黙って「効かないだけ」に
    しないのがこの層の方針 (intent §2)。
    """
    LOGGER.warning(
        "[reflex] %s is not set; %r cannot answer",
        backend.api_key_env or "(no api_key_env declared)", backend.model_key,
    )


def is_available(*, required_type: Optional[str] = None) -> bool:
    """いま反射判断を呼べるか (役割にモデルがあり、jev 互換で、キーも揃っているか)。

    呼び出し側のスイッチ (ペルソナごとの「自動想起を強化する」など) と組で使う。
    解決に失敗した理由は :func:`resolve_backend` が WARNING に出す。

    ``required_type`` を渡すと「その型の質問に答えられること」まで条件に入る。
    判定そのものは :func:`resolve_backend` の中だけで行い、ここでは結果を見るだけ
    (条件をこの関数にも書くと、画面の警告と実行の判定が二枚に分かれて食い違う)。

    **キーの有無までここで見る**のは、呼び出し側がこの関数の True を「反射判断で
    選ぶ前提」として、候補の集め方そのものを広げるため (sea/auto_recall.py の
    キーワード・除外・枠の上書き)。キー欠落を :func:`evaluate` まで持ち越すと、
    候補の母集団を広げたあとで判定だけが落ち、「従来方式へ戻った」というログと
    実際の挙動が食い違う。``api_key_required`` が False の宛先 (localjev など) は
    従来どおりキー不要。
    """
    try:
        backend = resolve_backend(required_type=required_type)
    except ReflexJudgmentUnavailable:
        return False
    except Exception:
        LOGGER.warning("[reflex] could not resolve the backend", exc_info=True)
        return False
    if backend.api_key_required and not _read_api_key(backend):
        _warn_missing_api_key(backend)
        return False
    return True


# ---------------------------------------------------------------------------
# 伏せ字と応答の検査
# ---------------------------------------------------------------------------


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


def _clean_usage(raw: Any, usage_fields: Mapping[str, str]) -> Dict[str, Any]:
    """応答の使用量から、記録・返却してよい数値だけを写した dict を作る。

    宣言された欄 (``usage_fields``) 以外のキーは捨てる (呼び出し側はこの dict を
    INFO ログへそのまま載せるので、API が返した未知の値を記録に流さない)。
    bool は数値として扱わない。

    トークン数として成り立たない値 (負数・NaN・inf) も欄ごと捨てる。負数をそのまま
    記帳すると累計の使用量と費用が目減りし、非有限値は記帳側の ``int()`` で落ちて
    WARNING になるだけで、どちらも黙って会計を歪める。**捨てるのはその欄だけで、
    判定そのものは成立のまま**にする — 答えが正しいのに会計の欄が壊れているだけの
    応答を「判定できなかった」に化かさない。
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for name, field in usage_fields.items():
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or value < 0:
            LOGGER.warning(
                "[reflex] usage field %r is not a usable token count: %r (dropping that field)",
                field, value,
            )
            continue
        cleaned[name] = value
    return cleaned


def _request_question(qid: str, question: Mapping[str, Any], backend: ReflexBackend) -> Dict[str, Any]:
    """呼び出し側の質問定義を API のリクエスト形へ整える。

    Raises:
        ValueError: 質問が辞書 (Mapping) でない / ``instructions`` が無い / 型が
            3 型のどれでもない (呼び出し側の契約違反。API の障害ではないので
            ``ReflexJudgmentUnavailable`` へは正規化しない — 呼び出し側のバグを
            「外部 API が落ちていた」に化かすと、フォールバックに隠れて永久に
            気づかれない)。
        ReflexJudgmentUnavailable: 答える側が対応していない型 (部分回答を採らない
            既存の厳格さのまま、質問ひとまとまりごと不成立にする)。
    """
    if not isinstance(question, Mapping):
        raise ValueError(
            f"question {qid!r} must be a mapping, got {type(question).__name__}"
        )
    qtype = question.get("type", "noul")
    if qtype not in QUESTION_TYPES:
        raise ValueError(f"question {qid!r} has unknown type {qtype!r}")
    if qtype not in backend.supported_types:
        raise ReflexJudgmentUnavailable(
            f"the destination for {backend.model_key!r} does not answer {qtype!r} questions"
        )
    instructions = question.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError(f"question {qid!r} has no 'instructions' string")
    payload: Dict[str, Any] = {"type": qtype, "instructions": instructions}
    criteria = question.get("criteria")
    if criteria:
        payload["criteria"] = criteria
    options = question.get("options")
    if options:
        payload["options"] = options
    return payload


def _read_answer(
    qid: str, qtype: str, answer: Mapping[str, Any], backend: ReflexBackend, secret: str,
) -> Any:
    """答え 1 件を型ごとに検算して返す。

    ``noul`` (0〜1 の確率) だけは実運用で使われている型なので、範囲まで固定する。
    ``choice`` / ``score`` は器として通すところまで — 値が「空でない文字列」
    「有限の数値」であることだけを確かめる。
    """
    answer_type = answer.get("type")
    if answer_type is not None and answer_type != qtype:
        shown = _masked_repr(answer_type, secret)
        LOGGER.warning("[reflex] answer for qid=%r has type=%s (expected %r)", qid, shown, qtype)
        raise ReflexJudgmentUnavailable(
            f"answer for question {qid!r} has type {shown}, expected {qtype!r}"
        )

    field = backend.answer_fields.get(qtype, qtype)
    value = answer.get(field)

    if qtype == "choice":
        if not isinstance(value, str) or not value.strip():
            LOGGER.warning(
                "[reflex] answer for qid=%r has no %r string: %s",
                qid, field, _masked_repr(value, secret),
            )
            raise ReflexJudgmentUnavailable(f"answer for question {qid!r} has no {field!r} string")
        return value

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        LOGGER.warning(
            "[reflex] answer for qid=%r has non-numeric %r: %s",
            qid, field, _masked_repr(value, secret),
        )
        raise ReflexJudgmentUnavailable(f"answer for question {qid!r} has non-numeric {field!r}")
    number = float(value)
    if not math.isfinite(number):
        shown = _masked_repr(number, secret)
        LOGGER.warning("[reflex] answer for qid=%r is not a finite number: %s", qid, shown)
        raise ReflexJudgmentUnavailable(f"answer for question {qid!r} is not a finite number: {shown}")
    if qtype == "noul" and not (0.0 <= number <= 1.0):
        shown = _masked_repr(number, secret)
        LOGGER.warning("[reflex] answer for qid=%r has out-of-range %r: %s", qid, field, shown)
        raise ReflexJudgmentUnavailable(
            f"answer for question {qid!r} has out-of-range {field!r}: {shown}"
        )
    return number


def _record_usage(backend: ReflexBackend, usage: Mapping[str, Any], persona_id: Optional[str]) -> None:
    """使用量を既存の記帳 (saiverse/usage_tracker.py) へモデル設定キー名義で載せる。

    LLM クライアントが通る道と同じ入口を使うので、モデル設定ファイルに ``pricing``
    を書けば費用もそのまま出る (intent §2)。記帳の失敗で会話を止めない。
    """
    if not usage:
        return
    try:
        from saiverse.usage_tracker import get_usage_tracker

        get_usage_tracker().record_usage(
            model_id=backend.model_key,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            persona_id=persona_id,
            node_type=USAGE_CATEGORY,
            category=USAGE_CATEGORY,
        )
    except Exception:
        LOGGER.warning("[reflex] failed to record usage for %r", backend.model_key, exc_info=True)


def _record_abandoned_usage(
    response: Any, backend: ReflexBackend, persona_id: Optional[str],
) -> None:
    """締切で見切った呼び出しの応答から、**使用量だけ**を best-effort で記帳する。

    メインスレッドは締切を超えた時点で ``ReflexJudgmentUnavailable`` を投げて会話を
    先へ進めているので、ここで読んだ答えは誰も使わない (答えを読まないのは、使わない
    判定を後から成立させないため)。一方で API 側の課金はもう発生しているので、
    使用量を落とすと「支払ったのに台帳に無い」費用ができてしまう。

    呼ぶのは、置き去りにされたワーカーか、締切の時点で「ワーカーはもう応答を持って
    いる」と分かったメインのどちらか一方。どちらが呼ぶかは ``evaluate`` 内の錠の下で
    1 回だけ決まるので、二重記帳も記帳漏れも起きない (錠の置き方はそちらのコメント)。
    """
    if getattr(response, "status_code", None) != 200:
        return
    try:
        data = response.json()
    except Exception:
        return
    if not isinstance(data, dict):
        return
    usage = _clean_usage(data.get(backend.usage_key), backend.usage_fields)
    if not usage:
        return
    LOGGER.info(
        "[reflex] the call to %r was abandoned at the deadline but its answer did "
        "arrive; recording its usage only: %s",
        backend.model_key, usage,
    )
    _record_usage(backend, usage, persona_id)


# ---------------------------------------------------------------------------
# 呼び出し
# ---------------------------------------------------------------------------


def evaluate(
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    timeout: float,
    backend: Optional[ReflexBackend] = None,
    persona_id: Optional[str] = None,
    transport: Optional[Any] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """全質問を 1 リクエストで評価し ``({qid: 答え}, usage情報)`` を返す。

    Args:
        state: 判断の材料になる状況。文字列でも dict/list でもよい (API がそのまま受ける)。
        questions: ``{qid: {"type": "noul"|"choice"|"score", "instructions": str,
            "criteria": {...}}}``。``type`` を省くと ``noul``。``criteria`` /
            ``options`` は省略可。
        timeout: HTTP タイムアウト (秒)。
        backend: 答える側。省略すると役割の割り当てから解決する。
        persona_id: 使用量の記帳に載せるペルソナ (分からなければ省略)。
        transport: テストからトランスポート (``httpx.MockTransport``) を差し込むための口。
            本番経路では常に None。

    Returns:
        (qid -> 答え, usage dict)。**要求した全 qid が揃っている**ことを保証する —
        一部だけ答えが返った応答は成功にせず ``ReflexJudgmentUnavailable`` を投げる
        (部分的な判定を「判定した」として扱うと、答えの無い質問が静かに既定へ倒れ、
        外部 API の劣化が判定結果に化けてしまうため)。答えは ``noul`` なら 0〜1 の
        float、``score`` なら有限の float、``choice`` なら文字列。
        usage は宣言された欄のうち数値だったものだけを写した dict。

    Raises:
        ReflexJudgmentUnavailable: 答える側を解決できない・API キー欠落・
            リクエスト準備の失敗・接続失敗・タイムアウト・絶対締切超過・
            同時実行の上限超過・非 200・不正応答・部分回答・通信中に出た
            予期しない例外 (httpx 以外の型も含む)。
        ValueError: ``questions`` の形が契約を満たしていない (呼び出し側のバグ) —
            質問が辞書でない・``instructions`` が無い・型が 3 型のどれでもない。

    Note:
        使用量は**答えを読む前**に記帳する。応答が 200 で JSON の辞書として読めた
        時点で API 側の課金はもう発生しているので、答えの側の不備 (qid の欠落・
        型違い・範囲外) で判定が不成立になっても台帳からは落とさない。締切超過で
        見切った呼び出しも、応答が後から届けば**使用量だけ**は記帳される
        (答えは読まない)。非 200 と JSON として読めない応答は記帳しない。
    """
    if backend is None:
        backend = resolve_backend()

    if not questions:
        return {}, {}

    # 呼び出し側が is_available() を通っていても、ここでもう一度見る (二重の網)。
    # 判定と呼び出しの間に env が空になることも、backend を直接渡す呼び出しもある。
    api_key = _read_api_key(backend)
    if not api_key and backend.api_key_required:
        _warn_missing_api_key(backend)
        raise ReflexJudgmentUnavailable(
            f"{backend.api_key_env or 'the API key'} is not set for {backend.model_key!r}"
        )

    # 質問の検査は _request_question 1 箇所に集める。型の一覧も検査済みの
    # リクエスト形から作る (検査前の生の質問から `.get` で拾うと、辞書でない質問が
    # 検査へ届く前に AttributeError で漏れる)。
    request_questions = {
        qid: _request_question(qid, q, backend) for qid, q in questions.items()
    }
    types_by_qid = {qid: q["type"] for qid, q in request_questions.items()}
    payload = {
        "state": state,
        "model": backend.api_model,
        "questions": request_questions,
    }
    # API キーの値そのものはログに出さない (ヘッダごとダンプしない)。
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    LOGGER.debug(
        "[reflex] POST %s model=%s (config=%s) questions=%d timeout=%.1fs",
        backend.url, backend.api_model, backend.model_key, len(payload["questions"]), timeout,
    )

    # 同時実行の上限。待たずに弾く (会話の同期経路なのでキュー待ちさせない)。
    if not _IN_FLIGHT.acquire(blocking=False):
        # INFO であって WARNING ではない: 上限到達は障害ではなく平常動作
        # (会話の同期経路でキュー待ちさせないための設計。intent の失敗時の節)。
        # 賑やかな時間帯に毎ターン WARNING が並ぶと、効果測定でログを読む人が
        # 「壊れている」と誤読する。件数自体は効果測定で見たいので DEBUG にもしない。
        LOGGER.info("[reflex] too many in-flight calls (limit=%d); skipping", _MAX_IN_FLIGHT)
        raise ReflexJudgmentUnavailable(f"too many in-flight calls (limit={_MAX_IN_FLIGHT})")

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
        # 「見切りの宣言」と「完了の判定」を同じ錠の下で行い、置き去りにされた応答の
        # 使用量を**どちらが記帳するか**を 1 回だけ決める。
        #
        # 錠が無いと、メインの done.wait が False を返してから見切りの印を立てるまでの
        # 隙にワーカーの後始末が通り抜け、ワーカーは「まだ見切られていない」と見て
        # 記帳せず、メインは「まだ完了していない」と見て記帳しないまま例外で戻る —
        # 課金済みの使用量が両者の手から漏れる。
        #
        # 錠の下でやるのは印の読み書きだけで、記帳そのものは錠の外で行う (記帳は
        # UsageTracker への I/O なので、錠を長く持たない)。担当の決定は錠の中で
        # 終えているので、外に出た時点で競合は残らない。
        outcome_lock = threading.Lock()
        outcome = {"worker_done": False, "abandoned": False}

        def _post() -> None:
            response = None
            try:
                response = client.post(backend.url, json=payload, headers=headers)
                holder["response"] = response
            except BaseException as exc:  # noqa: BLE001 - 例外はメインスレッドへ運んで再送出する
                holder["error"] = exc
            finally:
                # 完了を宣言し、同じ錠の下で「自分が記帳するか」を決める。既に見切りの
                # 印が立っていれば、その応答を受け取る者はもういないので記帳はワーカーの役。
                with outcome_lock:
                    outcome["worker_done"] = True
                    record_here = response is not None and outcome["abandoned"]
                # 置き去りにされた呼び出しの後始末。記帳の失敗でこの下の
                # close / release / done.set を妨げないよう、全体を包む。
                try:
                    if record_here:
                        _record_abandoned_usage(response, backend, persona_id)
                except Exception:
                    LOGGER.warning(
                        "[reflex] failed to record the abandoned call's usage",
                        exc_info=True,
                    )
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
        LOGGER.warning("[reflex] could not start the request: %s", detail)
        raise ReflexJudgmentUnavailable(f"could not start the request: {detail}") from exc

    # 絶対締切 (壁時計)。httpx の timeout は I/O 単位なので、少量ずつ返し続ける
    # 応答は合計時間が青天井になりうる。会話の同期経路にいるので全体を打ち切る。
    deadline = timeout + _DEADLINE_MARGIN
    if not done.wait(deadline):
        # 通信を切る前に「見切った」印を立てる。close の後に応答が届いたワーカーは、
        # この印を見て使用量だけを記帳する (答えは読まない — メインはもう会話を
        # 先へ進めている)。印を見るのが間に合わなかった応答の使用量は取れないが、
        # それは通信を切った以上どうしようもない残余。
        #
        # 締切と完了がぶつかったとき (ワーカーが既に完了を宣言していて、その時点では
        # 見切りの印がまだ立っていなかったとき) は、ワーカーは記帳せずに通り抜けて
        # いる。その応答の記帳はメインが引き受ける — 課金は発生済みなので、どちらの
        # 手からも落とさない。
        with outcome_lock:
            outcome["abandoned"] = True
            abandoned_response = (
                holder.get("response") if outcome["worker_done"] else None
            )
        if abandoned_response is not None:
            try:
                _record_abandoned_usage(abandoned_response, backend, persona_id)
            except Exception:
                LOGGER.warning(
                    "[reflex] failed to record the abandoned call's usage", exc_info=True,
                )
        # 通信を切ってワーカーを終わらせる (ワーカー側の post が例外で戻り、
        # その finally でセマフォが返る)。会話を待たせないことが優先。
        try:
            client.close()
        except Exception:
            pass
        LOGGER.warning("[reflex] deadline exceeded after %.1fs; abandoning the call", deadline)
        raise ReflexJudgmentUnavailable(f"deadline exceeded after {deadline:.1f}s")

    error = holder.get("error")
    if error is not None:
        # 例外の型を問わず ReflexJudgmentUnavailable に正規化する。httpx 以外の例外
        # (トランスポート実装や差し込まれたフックが投げるもの) を素通しすると、
        # 呼び出し側が 1 種類の例外を捕まえるだけで畳めるという契約が破れる。
        # 文言にはヘッダ値 (= API キー) が混ざる型がある (h11 の Illegal header
        # value など) ので、ログにも例外メッセージにも伏せ字で載せる。
        detail = _mask_secret(f"{type(error).__name__}: {error}", api_key)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning("[reflex] request failed: %s", detail)
        raise ReflexJudgmentUnavailable(f"request failed: {detail}") from error

    response = holder["response"]

    if response.status_code != 200:
        # 応答本文に API キーがそのまま載っている実装もありうるので、プレビューを
        # 作る前にキーの値を伏せる (デバッグ価値のある本文自体は残す)。
        body = _mask_secret(response.text or "", api_key)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning("[reflex] non-200 response: status=%s body=%r", response.status_code, body)
        raise ReflexJudgmentUnavailable(f"HTTP {response.status_code}: {body}")

    try:
        data = response.json()
    except ValueError as exc:
        LOGGER.warning("[reflex] response is not valid JSON: %s", exc)
        raise ReflexJudgmentUnavailable(f"invalid JSON response: {exc}") from exc

    if not isinstance(data, dict):
        LOGGER.warning("[reflex] response is not a JSON object: %s", type(data).__name__)
        raise ReflexJudgmentUnavailable("response is not a JSON object")

    # 使用量の記帳は、答えを読む**前**にここで済ませる。答えの側の不備 (qid の欠落・
    # 型違い・範囲外) で下の検査が例外になっても、API 側の処理と課金はもう発生して
    # いるので、記帳に届かないと「支払ったのに台帳に無い」費用ができてしまう。
    # 締切で見切った呼び出し (_record_abandoned_usage) が答えを見ずに使用量だけを
    # 記帳するのと同じ扱いで、通常経路との非対称も無くなる。
    # 記帳はこの 1 箇所だけで、成功時に返す usage も同じ抽出結果を使う (二重記帳しない)。
    usage = _clean_usage(data.get(backend.usage_key), backend.usage_fields)
    _record_usage(backend, usage, persona_id)

    answers = data.get(backend.answers_key)
    if not isinstance(answers, dict):
        LOGGER.warning("[reflex] response has no %r object", backend.answers_key)
        raise ReflexJudgmentUnavailable(f"response has no {backend.answers_key!r} object")

    # 全 qid が揃っていることを要求する (部分回答は成功にしない)。
    resolved: Dict[str, Any] = {}
    for qid, qtype in types_by_qid.items():
        answer = answers.get(qid)
        if not isinstance(answer, dict):
            LOGGER.warning("[reflex] no answer object for qid=%r", qid)
            raise ReflexJudgmentUnavailable(f"no answer for question {qid!r}")
        resolved[qid] = _read_answer(qid, qtype, answer, backend, api_key)

    LOGGER.debug(
        "[reflex] %s answered %d/%d question(s) usage=%s",
        backend.model_key, len(resolved), len(questions), usage,
    )
    return resolved, usage


__all__ = [
    "DIALECT_FIELD",
    "FIRST_STAGE_QUESTION_TYPE",
    "JEV_COMPAT_PROTOCOL",
    "QUESTION_TYPES",
    "USAGE_CATEGORY",
    "ReflexBackend",
    "ReflexJudgmentUnavailable",
    "evaluate",
    "is_available",
    "reflex_model_setting",
    "resolve_backend",
]
