"""反射判断 — 状況と型付きの質問を渡すと、確率・選択・数値だけが返る判断の層。

設計の正典は docs/intent/reflex_judgment.md。考えて文章を組み立てる判断 (通常の
LLM 呼び出し) より速く・安く・形が保証される代わりに、自由記述はできない。

**答える側の決め方**: モデルの役割「反射判断」(``saiverse/model_defaults.py`` の
``MODEL_ROLES["reflex_judgment_model"]``、世界の既定は env
``SAIVERSE_REFLEX_JUDGMENT_MODEL``) に割り当てられたモデル設定を、既存の 3 層読み込み
(``saiverse/model_configs.py``) で引く。答える側は 2 種類ある:

- **jev 互換の宛先** (provider の protocol が ``jev_compat``) — System One の形を
  そのまま送る。宛先・キーの env 名・応答の欄の方言・対応する質問の型は、すべて
  provider / モデル設定ファイルの ``reflex_judgment`` 欄の宣言から取る (提供元ごとの
  分岐をこのコードには置かない)。
- **通常の LLM** (openai_compat / gemini_native など) — この層が質問をプロンプトへ
  変換し、構造化出力で答えさせる。3 型すべてに答えられる扱いにする。

どちらの種別かは :class:`ReflexBackend` の ``kind`` が持ち、解決の入口
(:func:`resolve_backend`) と呼び出しの入口 (:func:`evaluate`) は 1 つずつのまま。

**リクエストの形 (jev 互換)** (TypeSafe 公式 https://docs.typesafe.ai/api.md が正典。
互換勢も中身は同じで、違うのは宛先の path・認証・応答の欄の名前・対応する型):

    POST <base_url><path>
    Authorization: Bearer <api_key_env の値>

    {"state": ..., "model": "<モデル設定の model 欄>",
     "questions": {"<qid>": {"type": "noul", "instructions": "...",
                             "criteria": {"true": "...", "false": "..."}}}}

    -> {"answers": {"<qid>": {"type": "noul", "noul": 0.92}},
        "usage": {"input_tokens": 312, "output_tokens": 48}}

方言の宣言で ``request_shape: "openai_decisions"`` を書いた宛先 (OpenAI の Decisions
API) だけは、組み立て方そのものが違うので送る直前に変換する — 質問は ``name`` 付きの
配列 (型名 ``predicate`` / ``choice`` / ``score``)、状況は文字列の ``input`` で、
criteria の欄が無い。choice の選択肢の説明は ``choices`` へ、score の段階の説明は
``levels`` へ写し、構造の中に居場所の無い基準 (noul の基準と、choices に入らなかった
choice の基準) を ``input`` の最後に書き足す。答えも ``name`` 付きの配列で返るので、
qid の辞書へ戻してから同じ検算に通す。System One の形が本流で、呼び出し側が組む質問の
形はどちらの宛先でも変わらない。

**リクエストの形 (通常の LLM)**: 状況と質問一覧を 1 通のプロンプトに畳み、答えは
``{"answers": [{"qid": ..., "noul"/"score"/"choice": ...}, ...]}`` の構造化出力で
受ける。**辞書ではなく配列**なのは、qid が実行時に決まるうえ Gemini が
``additionalProperties`` を使えないため (intent §3)。

失敗 (役割が未割り当て・モデル設定が無い・キーと宛先の組が照合に通らない・キー欠落・
リクエスト準備の失敗・接続失敗・タイムアウト・絶対締切超過・同時実行の上限超過・
非 200・不正応答・部分回答・通信中に出た予期しない例外) はすべて
``ReflexJudgmentUnavailable`` に正規化する。呼び出し側はこれ 1 つを捕まえて、
外部 API が落ちていてもペルソナの返事が止まらない経路へフォールバックすること
(そのターンをどう凌ぐかは各機能の設計が持つ — intent §4)。例外の型は 1 つのまま
だが、「時間内に答えなかった」だけは ``kind`` 属性 (``UNAVAILABLE_DEADLINE``) で
見分けられる — 待ち時間や割り当てたモデルを変えれば直る失敗で、しかも課金は発生
しているので、呼び出し側が利用者へ知らせられるようにしてある。呼び出し 1 回ごとの
結果 (成立 / 締切超過 / その他の失敗) は直近 ``OUTCOME_HISTORY_SIZE`` 件だけ
プロセス内に積み (:func:`recent_outcomes`)、設定画面の警告がそこから読む。数えるのは
``OUTCOME_WINDOW_SECONDS`` 秒以内の記録だけ — 設定を直した人が、そのペルソナと
しばらく喋らないだけで古い警告を見続けることがないようにするため。

``saiverse/typesafe_client.py`` を置き換えたモジュール (2026-09-20)。会話の返事を
待たせる場所で使う前提の器 — 同時本数の上限 (プロセス全体で 4 本)・実際の経過時間で
打ち切る締切・キーの伏せ字・全 qid の揃わない応答の不成立 — はそちらで実証済みの
形をそのまま引き継いでいる。器は 2 種類の答える側で共有する。
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Mapping, Optional, Sequence, Tuple, Union

import httpx

from saiverse.provider_protocols import JEV_COMPAT_PROTOCOL

LOGGER = logging.getLogger("saiverse.reflex_judgment")

#: 答える側の種別。``resolve_backend`` が決め、``evaluate`` がどちらの道を通すかを選ぶ。
#: 提供元の名前ではなくこの 2 値だけで分岐する。
REFLEX_KIND_JEV = "jev_compat"  # System One の形をそのまま送る
REFLEX_KIND_LLM = "llm"         # 質問をプロンプトへ変換し、構造化出力で答えさせる

#: 質問の型 (System One の 3 型)。第 1 段の利用者 (自動想起の選別) は noul だけを
#: 使うが、器は 3 型とも通す。
QUESTION_TYPES = ("noul", "choice", "score")

#: 第 1 段でこの層に仕事を頼む唯一の機能 (自動想起の選別) が投げる質問の型。
#:
#: 「この宛先は使えるか」を答えるとき、宛先が解決できるだけでは足りない — choice に
#: しか答えない宛先を「使える」と答えると、自動想起は候補の集め方だけを広げたまま
#: 毎ターン判定に失敗する。自動想起 (sea/auto_recall.py) と画面の警告
#: (saiverse/model_defaults.py) は、この型に答えられることまでを「噛み合っている」の
#: 条件にする。
FIRST_STAGE_QUESTION_TYPE = "noul"

#: provider / モデル設定で方言を宣言する欄の名前 (jev 互換の宛先だけが使う)。
DIALECT_FIELD = "reflex_judgment"

#: 方言の宣言が無い欄の既定 (TypeSafe 公式 = 正典の形)。
_DEFAULT_PATH = "/v1/systemone"
_DEFAULT_ANSWERS_KEY = "answers"
_DEFAULT_USAGE_KEY = "usage"
_DEFAULT_ANSWER_FIELDS = {"noul": "noul", "choice": "choice", "score": "score"}
_DEFAULT_USAGE_FIELDS = {"input_tokens": "input_tokens", "output_tokens": "output_tokens"}

#: リクエストの組み立て方 (方言の宣言の ``request_shape``)。jev 互換の宛先だけが使う。
#:
#: - ``system_one`` (既定、宣言が無ければこれ): System One の形 (``state`` + qid の辞書の
#:   ``questions``) をそのまま送る。本流の形で、呼び出し側が組む質問もこの形。
#: - ``openai_decisions``: OpenAI の Decisions API の形へ、送る直前に変換する。質問は
#:   ``name`` 付きの配列、型名は ``predicate`` / ``choice`` / ``score``、状況は文字列の
#:   ``input``。Decisions には criteria の欄が無い。まはーの裁定 (2026-10-07) は
#:   「System One 側を本流のままにし、OpenAI では criteria を input の最後に押し込む」と
#:   「OpenAI だけ対応できない型を残さない」の二つで、それを受けて、構造の中に居場所が
#:   ある基準はそこへ写す形にした (score の段階の説明 → ``levels``、choice の選択肢の
#:   説明 → ``choices`` の ``description``)。居場所の無い基準 (noul のはい/いいえの
#:   基準と、choices に入らなかった choice の基準) は ``input`` の最後の節に書き足す。
#:   答えも ``name`` 付きの配列で返るので、qid の辞書へ戻してから既存の検算に渡す。
#:
#: 宣言に ``request_shape`` のキーが無いときだけ ``system_one``。キーがあって値が
#: 上の二つ以外 (null を含む) なら設定ミスとして解決を断る。この「キーが無ければ既定、
#: キーがあって null・型違いなら断る」は方言の宣言の全欄に共通の規則
#: (:func:`_dialect_field`)。分岐はこの宣言値だけで行う (提供元の名前や URL では
#: 分岐しない)。
REQUEST_SHAPE_SYSTEM_ONE = "system_one"
REQUEST_SHAPE_OPENAI_DECISIONS = "openai_decisions"
REQUEST_SHAPES = (REQUEST_SHAPE_SYSTEM_ONE, REQUEST_SHAPE_OPENAI_DECISIONS)

#: ``openai_decisions`` の形での質問の型名 (SAIVerse 側 → Decisions 側)。
_DECISIONS_QUESTION_TYPES = {"noul": "predicate", "choice": "choice", "score": "score"}
#: 答えの型名を SAIVerse 側へ戻す表 (Decisions 側 → SAIVerse 側)。
_DECISIONS_ANSWER_TYPES = {value: key for key, value in _DECISIONS_QUESTION_TYPES.items()}
#: 答えられなかった質問に Decisions が返す要素の型名 (``name`` だけが載る)。
_DECISIONS_REFUSAL = "refusal"
#: ``openai_decisions`` の形での answer_fields の既定。noul の答えは ``probability`` の
#: 欄に載る。user_data で provider を上書きして answer_fields を書き忘れても、noul の
#: 欄名を System One の既定 (``noul``) と取り違えないよう、形ごとに既定を持つ。
_DECISIONS_DEFAULT_ANSWER_FIELDS = {"noul": "probability", "choice": "choice", "score": "score"}
#: 構造に居場所の無い基準を ``input`` の最後に書き足すときの見出し (通常 LLM 向けの
#: 前置きと揃えて英語)。OpenAI がモデルに ``name`` を見せるかは確かめられていないので、
#: 各ブロックは名前だけでなく質問文 (instructions) でも質問を指す。「above」とは
#: 書かない — Decisions の質問は input とは別の欄にあり、モデルから見て上にあるとは
#: 限らない。
_DECISIONS_CRITERIA_HEADER = (
    "Criteria for the questions (each block starts with the question name "
    "and its instructions):"
)
#: score の段階の数の下限 (System One の正典も 2〜10 個)。1 個では段階にならない。
_DECISIONS_MIN_LEVELS = 2

#: 通常の LLM に組ませる構造化出力の、答えの配列が載る欄。
_LLM_ANSWERS_KEY = "answers"

# ログ/例外メッセージに載せる外部由来テキストの最大文字数 (非 200 本文・HTTP 例外文言)。
_ERROR_BODY_PREVIEW = 200

# 壁時計の絶対締切に足す余裕 (秒)。httpx の timeout は接続・書き込み・読み取りの
# 各 I/O 単位なので、少量ずつ断続的に返す応答では合計が timeout を超えうる。
# 呼び出し全体を timeout + この余裕で打ち切る。公開しているのは、呼び出し側が
# 「利用者の設定値 = 見切りの時刻そのもの」にしたいとき、設定値からこの余裕を
# 引いた値を timeout に渡せるようにするため (sea/auto_recall.py)。
DEADLINE_MARGIN = 0.5

# 締切超過で置き去りにしたワーカーが積み上がらないための同時実行の上限。
# 締切を超えた呼び出しはメインスレッドから通信を切るのでワーカーは速やかに終わるが、
# それでも一度に走る本数はここで頭打ちにする (上限に達している間の呼び出しは待たずに
# ReflexJudgmentUnavailable にする — 会話の同期経路にいるので、キュー待ちで返事を
# 遅らせない)。
# 通信を切れない答える側 (通常の LLM の ``generate``) では、ワーカーは応答が返るまで
# 枠を 1 本占有し続ける。その場合の症状は毎回の「too many in-flight calls」INFO で
# 観測でき、会話は従来方式へ戻る (壊れ方は費用ゼロ側)。
_MAX_IN_FLIGHT = 4
_IN_FLIGHT = threading.BoundedSemaphore(_MAX_IN_FLIGHT)

# ワーカースレッド名 (テストが居残りワーカーを join するための目印)。
_WORKER_THREAD_NAME = "reflex-judgment-post"

#: 使用量の記帳に使う区分 (saiverse/usage_tracker.py の category / node_type)。
USAGE_CATEGORY = "reflex_judgment"


#: 使えなかった理由の種別 (:class:`ReflexJudgmentUnavailable` の ``kind``)。
#:
#: **例外の型は 1 つのまま**にする (呼び出し側が 1 種類を捕まえて畳めるという契約を
#: 壊さないため)。種別は属性で運ぶ。区別が要るのは「時間内に答えなかった」だけ —
#: それは設定 (待ち時間・割り当てたモデルの速さ) で直せる失敗で、しかも**課金は
#: 発生している**ので、利用者に知らせる価値がある。キー欠落や接続失敗は別の話。
UNAVAILABLE_DEADLINE = "deadline"
UNAVAILABLE_OTHER = "other"


class ReflexJudgmentUnavailable(Exception):
    """反射判断が使えなかった。

    役割の未割り当て・モデル設定の欠落・キーと宛先の組の照合失敗・API キー欠落・
    接続失敗・タイムアウト・非 200・不正応答のすべてをこれに正規化する
    (呼び出し側が「今回は判定なし」として同じ扱いで畳めるようにするため)。

    Attributes:
        kind: 理由の種別。締切超過の raise だけが :data:`UNAVAILABLE_DEADLINE` で、
            それ以外はすべて :data:`UNAVAILABLE_OTHER`。呼び出し側が「待てば済んだ
            失敗」だけを利用者へ知らせられるようにするための印で、捕まえ方は
            従来どおり 1 種類のまま。
    """

    def __init__(self, *args: Any, kind: str = UNAVAILABLE_OTHER) -> None:
        super().__init__(*args)
        self.kind = kind


@dataclass(frozen=True)
class ReflexBackend:
    """答える側 1 つ分の解決結果 (モデル設定 + provider の宣言から組む)。

    Attributes:
        model_key: モデル設定のキー (ファイル名の stem)。**使用量と費用はこの名義で
            記帳する** — 答える側を替えても費用が同じ場所に見える (intent §2)。
        api_model: API 上のモデル名 (jev 互換ではリクエストの ``model`` 欄に載る)。
        kind: ``REFLEX_KIND_JEV`` か ``REFLEX_KIND_LLM``。どちらの道を通すかはこの欄
            だけで決まる (提供元の名前による分岐はコードに置かない)。
        api_key_env: 資格情報を読む環境変数名 (無ければ None)。
        api_key_required: False なら認証しない宛先 (localjev など)。**jev 互換の道の
            キー検査だけがこれを見る** — 通常の LLM のキー検査は ``is_model_available``
            (代替キー名や provider の既定キー名まで知っている既存の述語) に任せる
            (:func:`_has_credentials`)。
        supported_types: この宛先が答えられる質問の型。
        url: 宛先 (provider の ``base_url`` + 宣言された ``path``)。jev 互換のみ。
        answers_key / usage_key: 応答のどの欄に答え / 使用量が載るか。jev 互換のみ。
        answer_fields: 質問の型 → 答えの数値が載る欄の名前。jev 互換の宛先では、
            宣言の無いキーの既定が ``request_shape`` ごとに違う (``openai_decisions``
            の noul は ``probability``)。
        usage_fields: 記帳する使用量の名前 → 応答の欄の名前。jev 互換のみ。
        request_shape: リクエストの組み立て方 (:data:`REQUEST_SHAPES` のどれか)。
            jev 互換のみ。既定は System One の形のまま送る ``system_one``。
    """

    model_key: str
    api_model: str
    kind: str
    api_key_env: Optional[str]
    api_key_required: bool
    supported_types: frozenset
    url: str = ""
    answers_key: str = _DEFAULT_ANSWERS_KEY
    usage_key: str = _DEFAULT_USAGE_KEY
    answer_fields: Mapping[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_ANSWER_FIELDS)
    )
    usage_fields: Mapping[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_USAGE_FIELDS)
    )
    request_shape: str = REQUEST_SHAPE_SYSTEM_ONE


# ---------------------------------------------------------------------------
# 答える側の解決
# ---------------------------------------------------------------------------


def reflex_model_setting() -> Optional[str]:
    """モデルの役割「反射判断」に割り当てられたモデル設定キー。未設定なら None。"""
    from saiverse.model_defaults import MODEL_ROLES

    value = os.getenv(MODEL_ROLES["reflex_judgment_model"]) or ""
    return value.strip() or None


def _refuse_dialect(key: str, problem: str) -> ReflexJudgmentUnavailable:
    """方言の宣言の設定ミスを WARNING に出し、投げる例外を返す。"""
    LOGGER.warning(
        "[reflex] model %r: the %r declaration %s; reflex judgment is unavailable",
        key, DIALECT_FIELD, problem,
    )
    return ReflexJudgmentUnavailable(f"model {key!r}: the {DIALECT_FIELD!r} declaration {problem}")


def _dialect(key: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    """方言の宣言を読む。欄が無い (None) ときだけ「宣言なし」= 空の辞書。

    辞書でない宣言 (``"reflex_judgment": "typo"`` など) を空の辞書に読み替えると、
    書いた人の宣言が全部消えて TypeSafe 正典の既定へ黙って落ち、違う宛先・違う形で
    毎回失敗する。だから設定ミスとして解決を断る。
    """
    if DIALECT_FIELD not in config:
        return {}
    raw = config[DIALECT_FIELD]
    if raw is None:
        # 明示の null は「宣言なし」ではない。provider の宣言を null で消した形で、
        # 既定へ落とすと Decisions の宛先へ System One の形を送り続ける。
        raise _refuse_dialect(key, "is null")
    if not isinstance(raw, Mapping):
        raise _refuse_dialect(key, f"is not an object: {raw!r}")
    return dict(raw)


def _dialect_field(
    key: str, dialect: Mapping[str, Any], name: str, expected: Any, default: Any,
) -> Any:
    """方言の宣言の 1 欄を読む。全欄に共通の規則はここ 1 箇所に置く。

    - キーが無ければ既定。
    - キーがあって値が null・型違い (``expected`` でない)・中身の無い文字列なら、
      設定ミスとして解決を断る。既定へ黙って落とさない — モデル側に null を書くと
      キー単位の合成 (``model_configs._merge_reflex_judgment``) で provider の宣言を
      消しているので、既定へ落とすと違う宛先・違う欄名で毎回失敗し、誰も気づかない。
    """
    if name not in dialect:
        return default
    value = dialect[name]
    if value is None or isinstance(value, bool) or not isinstance(value, expected):
        raise _refuse_dialect(key, f"has an unusable {name!r}: {value!r}")
    if isinstance(value, str):
        # 前後の空白は落とす (" answers" のまま使うと、毎回その欄が見つからない)。
        value = value.strip()
        if not value:
            raise _refuse_dialect(key, f"has an empty {name!r}")
    return value


def _string_map(
    key: str, dialect: Mapping[str, Any], name: str, default: Mapping[str, str],
) -> Dict[str, str]:
    """``{名前: 欄名}`` の宣言を読む。書いたキーだけが既定を上書きする。

    欄そのものの null・型違いは :func:`_dialect_field` の規則で断る。中の 1 項目が
    文字列でない・空の文字列のときも同じ理由で断る (黙って既定の欄名へ落とすと、
    答えや使用量を違う欄から読み続ける)。
    """
    raw = _dialect_field(key, dialect, name, Mapping, None)
    merged = dict(default)
    if raw is None:
        return merged
    for field_key, value in raw.items():
        if not isinstance(field_key, str) or not isinstance(value, str) or not value.strip():
            raise _refuse_dialect(
                key, f"has an unusable entry in {name!r}: {field_key!r}: {value!r}",
            )
        merged[field_key] = value.strip()
    return merged


def _api_key_env_of(config: Mapping[str, Any]) -> Optional[str]:
    """モデル設定が宣言する資格情報の env 名 (宣言が無い / 空なら None)。"""
    value = config.get("api_key_env")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resolve_jev_backend(
    key: str, config: Mapping[str, Any], api_model: str, api_key_env: Optional[str],
) -> ReflexBackend:
    """jev 互換の宛先を、provider / モデル設定の方言の宣言から組む。"""
    base_url = str(config.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        LOGGER.warning("[reflex] model %r declares no base_url; reflex judgment is unavailable", key)
        raise ReflexJudgmentUnavailable(f"model {key!r} declares no base_url")

    # 方言の宣言の全欄に同じ規則を掛ける (_dialect_field): キーが無ければ既定、
    # キーがあって null・型違いなら設定ミスとして解決を断る。
    dialect = _dialect(key, config)
    path = _dialect_field(key, dialect, "path", str, _DEFAULT_PATH).strip()
    if not path.startswith("/"):
        path = "/" + path

    raw_types = _dialect_field(key, dialect, "supported_types", (list, tuple), None)
    if raw_types is None:
        supported = frozenset(QUESTION_TYPES)
    else:
        if not all(isinstance(t, str) for t in raw_types):
            raise _refuse_dialect(key, f"has a non-string entry in 'supported_types': {raw_types!r}")
        supported = frozenset(t for t in raw_types if t in QUESTION_TYPES)
    if not supported:
        LOGGER.warning(
            "[reflex] model %r declares no usable question type; reflex judgment is unavailable", key,
        )
        raise ReflexJudgmentUnavailable(f"model {key!r} declares no usable question type")

    # リクエストの組み立て方。キーが無ければ本流 (System One の形)。null・型違いは
    # 他の欄と同じ規則で断り (null を既定へ落とすと、Decisions の宛先へ System One の
    # 形を送り続ける)、文字列でも知らない値なら同じく設定ミスとして断る。
    request_shape = _dialect_field(
        key, dialect, "request_shape", str, REQUEST_SHAPE_SYSTEM_ONE,
    )
    if request_shape not in REQUEST_SHAPES:
        LOGGER.warning(
            "[reflex] model %r declares an unknown request_shape %r (known: %s); "
            "reflex judgment is unavailable", key, request_shape, list(REQUEST_SHAPES),
        )
        raise ReflexJudgmentUnavailable(
            f"model {key!r} declares an unknown request_shape {request_shape!r}"
        )

    # 答えの欄の名前の既定は形ごとに違う (Decisions の noul は probability の欄)。
    default_answer_fields = (
        _DECISIONS_DEFAULT_ANSWER_FIELDS
        if request_shape == REQUEST_SHAPE_OPENAI_DECISIONS
        else _DEFAULT_ANSWER_FIELDS
    )

    return ReflexBackend(
        model_key=key,
        api_model=api_model,
        kind=REFLEX_KIND_JEV,
        api_key_env=api_key_env,
        api_key_required=config.get("api_key_required") is not False,
        supported_types=supported,
        url=f"{base_url}{path}",
        answers_key=_dialect_field(key, dialect, "answers_key", str, _DEFAULT_ANSWERS_KEY),
        usage_key=_dialect_field(key, dialect, "usage_key", str, _DEFAULT_USAGE_KEY),
        answer_fields=_string_map(key, dialect, "answer_fields", default_answer_fields),
        usage_fields=_string_map(key, dialect, "usage_fields", _DEFAULT_USAGE_FIELDS),
        request_shape=request_shape,
    )


def _resolve_llm_backend(
    key: str, config: Mapping[str, Any], api_model: str, api_key_env: Optional[str],
) -> ReflexBackend:
    """通常の LLM を答える側として組む (質問はこの層がプロンプトへ変換する)。

    宛先の URL はここでは見ない — native 系の provider (gemini_native など) は
    ``base_url`` を持たないことがあり、宛先を知っているのは LLM クライアント側。
    答えられる質問の型は 3 型すべて (変換はこの層が全型分を持っている)。

    ``api_key_env`` / ``api_key_required`` はモデル設定の宣言をそのまま写すだけで、
    この道のキー検査はそれを見ない — 通常の LLM が実際に読むキーは宣言以外にもある
    (代替キー名、provider の既定キー名) ので、検査は ``is_model_available`` に任せる
    (:func:`_has_credentials`)。

    Raises:
        ReflexJudgmentUnavailable: LLM クライアントの工場が話せない protocol。
            照合は工場側の解決規則そのもの (``llm_clients.factory`` の
            ``resolve_protocol`` + ``SUPPORTED_PROTOCOLS``) で行う — ここで素通しに
            すると、未知の protocol を書いた壊れた設定が「解決できた」顔で保存と
            画面警告を通り抜け、実行時に毎ターン黙って失敗し続ける
            (2026-09-20 の敵対レビュー 1 巡目)。
    """
    from llm_clients.factory import SUPPORTED_PROTOCOLS, resolve_protocol

    # provider 欄の既定は実行時の引き方 (saiverse/model_configs.get_model_provider の
    # 既定 "ollama") と同じにする — ここだけ違う既定を使うと、工場が受ける設定を
    # この照合が断る逆ずれが生まれる。
    factory_protocol = resolve_protocol(str(config.get("provider") or "ollama"), dict(config))
    if factory_protocol not in SUPPORTED_PROTOCOLS:
        LOGGER.warning(
            "[reflex] model %r speaks protocol %r, which no LLM client understands; "
            "reflex judgment is unavailable", key, factory_protocol,
        )
        raise ReflexJudgmentUnavailable(
            f"model {key!r} speaks protocol {factory_protocol!r}, which no LLM client understands"
        )

    return ReflexBackend(
        model_key=key,
        api_model=api_model,
        kind=REFLEX_KIND_LLM,
        api_key_env=api_key_env,
        api_key_required=bool(api_key_env) and config.get("api_key_required") is not False,
        supported_types=frozenset(QUESTION_TYPES),
    )


def resolve_backend(
    model_key: Optional[str] = None, *, required_type: Optional[str] = None,
) -> ReflexBackend:
    """答える側を解決する。使えない理由はすべて例外 1 種類で返す。

    Args:
        model_key: モデル設定キー。省略すると役割 (env) に割り当てられた値を使う。
            ペルソナ個別の上書き (``AI.REFLEX_JUDGMENT_MODEL``) はここへ渡ってくる。
        required_type: 「この型の質問に答えられること」を解決の条件に足す。
            省略すると型は問わない (器としての解決)。呼び出し側が投げる型が決まって
            いるときは渡すこと — 答えられない宛先を「使える」と答えると、呼び出し側は
            その前提で動き出したまま毎ターン判定に失敗する。

    Raises:
        ReflexJudgmentUnavailable: 役割が未割り当て / 設定が無い / キーと宛先の組が
            照合に通らない / jev 互換なのに宛先が宣言されていない /
            ``required_type`` に答えられない。理由は WARNING に出す (設定ミスは
            黙って「効かないだけ」にしない — intent §2)。
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

    api_model = str(config.get("model") or key)
    api_key_env = _api_key_env_of(config)
    protocol = config.get("protocol") or config.get("provider")

    if protocol == JEV_COMPAT_PROTOCOL:
        backend = _resolve_jev_backend(key, config, api_model, api_key_env)
    else:
        backend = _resolve_llm_backend(key, config, api_model, api_key_env)

    if required_type is not None and required_type not in backend.supported_types:
        LOGGER.warning(
            "[reflex] model %r answers %s but the caller asks %r questions; "
            "reflex judgment is unavailable for that work",
            key, sorted(backend.supported_types), required_type,
        )
        raise ReflexJudgmentUnavailable(
            f"model {key!r} does not answer {required_type!r} questions"
        )

    return backend


def _read_api_key(backend: ReflexBackend) -> str:
    """宛先が宣言した env から資格情報を読む (宣言が無ければ空文字)。"""
    if not backend.api_key_env:
        return ""
    return (os.getenv(backend.api_key_env) or "").strip()


def _has_credentials(backend: ReflexBackend) -> bool:
    """この宛先を呼ぶのに要る資格情報が揃っているか。

    答える側の種別で判定の出どころが違う:

    - **通常の LLM**: ``saiverse/model_configs.py`` の :func:`is_model_available` に
      任せる。この述語は代替キー名 (``api_key_env_alternates``、例: Gemini の無料枠の
      キー)・宣言の無い旧形式の provider 既定キー名・``api_key_required: false``・
      キーの要らないローカル provider (ollama / llama_cpp) を全部知っている。ここで
      「api_key_env の env が空か」を手組みすると、無料枠のキーだけを設定している人に
      「使えない」と答えてしまう — そのとき呼び出し側 (sea/auto_recall.py) は候補の
      集め方だけを広げたまま毎ターン判定に失敗する。実際に呼ぶ LLM クライアントと
      同じ述語を見ることで、この食い違いを構造から消す。
    - **jev 互換**: 宣言ベースのまま (``api_key_env`` + ``api_key_required``)。この
      protocol は ``llm_clients`` の工場を通らないので、:func:`is_model_available` の
      provider 既定キー名の表には載っていない。
    """
    if backend.kind == REFLEX_KIND_LLM:
        from saiverse.model_configs import is_model_available

        return is_model_available(backend.model_key)
    return bool(_read_api_key(backend)) or not backend.api_key_required


def _warn_missing_api_key(backend: ReflexBackend) -> None:
    """資格情報が揃っていないので呼べないことを WARNING に出す。

    設定ミスが続いている間は毎ターン出る。それでよい — 黙って「効かないだけ」に
    しないのがこの層の方針 (intent §2)。どちらの文面もモデル設定キーを載せる
    (env の値そのものは載せない)。
    """
    if backend.kind == REFLEX_KIND_LLM:
        # 通常の LLM はキーの env 名が複数ありうる (代替キー名・provider の既定名) ので、
        # 一つの名前を名指ししない。
        LOGGER.warning(
            "[reflex] no API key is configured for model %r; it cannot answer",
            backend.model_key,
        )
        return
    LOGGER.warning(
        "[reflex] %s is not set; %r cannot answer",
        backend.api_key_env or "(no api_key_env declared)", backend.model_key,
    )


def is_available(
    *, model_key: Optional[str] = None, required_type: Optional[str] = None,
) -> bool:
    """いま反射判断を呼べるか (割り当てがあり、宛先が解決でき、キーも揃っているか)。

    呼び出し側のスイッチ (ペルソナごとの「自動想起を強化する」など) と組で使う。
    解決に失敗した理由は :func:`resolve_backend` が WARNING に出す。

    ``model_key`` を渡すとその設定を答える側として見る (ペルソナ個別の上書き)。
    省略すると役割 (env) の割り当てを見る。``required_type`` を渡すと「その型の
    質問に答えられること」まで条件に入る。判定そのものは :func:`resolve_backend` の
    中だけで行い、ここでは結果を見るだけ (条件をこの関数にも書くと、画面の警告と
    実行の判定が二枚に分かれて食い違う)。

    **キーの有無までここで見る**のは、呼び出し側がこの関数の True を「反射判断で
    選ぶ前提」として、候補の集め方そのものを広げるため (sea/auto_recall.py の
    キーワード・除外・枠の上書き)。キー欠落を :func:`evaluate` まで持ち越すと、
    候補の母集団を広げたあとで判定だけが落ち、「従来方式へ戻った」というログと
    実際の挙動が食い違う。キーが要るかどうかの判定は :func:`_has_credentials` —
    通常の LLM は ``is_model_available`` (代替キー名や provider の既定キー名まで知って
    いる既存の述語)、jev 互換は設定ファイルの宣言。
    """
    try:
        backend = resolve_backend(model_key=model_key, required_type=required_type)
    except ReflexJudgmentUnavailable:
        return False
    except Exception:
        LOGGER.warning("[reflex] could not resolve the backend", exc_info=True)
        return False
    if not _has_credentials(backend):
        _warn_missing_api_key(backend)
        return False
    return True


# ---------------------------------------------------------------------------
# 伏せ字と応答の検査
# ---------------------------------------------------------------------------


#: 伏せる値の渡し方 — 1 本の文字列でも、候補の一覧でもよい (下の _mask_targets)。
_Secrets = Union[str, Sequence[str]]


def _mask_secret(text: str, secret: _Secrets) -> str:
    """``text`` 中の資格情報の値の出現をすべて ``***`` に置き換える。

    外部由来の文字列 (応答本文・HTTP 例外の文言・応答内の不正値) をログや例外
    メッセージに載せる前に通す。切り詰めより先に置換すること (先に切り詰めると
    キーの断片が残りうる)。``secret`` は 1 本の値でも候補の一覧でもよい。

    生の値に加えて ``repr(値)`` のエスケープ表現も落とす: httpx/h11 の例外や
    ``%r`` 経由の記録では、キーに含まれる改行やタブが ``\\n`` のような表現へ
    化けて生文字列の置換をすり抜けるため。
    """
    values = (secret,) if isinstance(secret, str) else tuple(secret)
    for value in values:
        if not value:
            continue
        text = text.replace(value, "***")
        escaped = repr(value)[1:-1]
        if escaped and escaped != value:
            text = text.replace(escaped, "***")
    return text


def _masked_repr(value: Any, secret: _Secrets) -> str:
    """``repr(value)`` をキーの伏せ字化を通して返す (応答内の不正値を記録するとき用)。"""
    return _mask_secret(repr(value), secret)


def _mask_targets(backend: ReflexBackend) -> Tuple[str, ...]:
    """ログ・例外メッセージから伏せるべき資格情報の値の一覧。

    jev 互換は宣言された env 1 本の値。通常の LLM は、クライアントが代替キー名
    (Gemini の無料枠のキーなど) や宣言の無い旧形式の provider 既定キー名で認証する
    ことがある (saiverse/model_configs.py の ``required_api_key_env_names``) ので、
    その候補すべての値を伏せる — 宣言の 1 本だけを伏せると、代替キーで認証した
    呼び出しの例外文言に実際のキーが残る (2026-09-20 のローカルレビューの指摘)。
    """
    if backend.kind == REFLEX_KIND_LLM:
        from saiverse.model_configs import required_api_key_env_names

        values: list = []
        for name in required_api_key_env_names(backend.model_key):
            value = (os.getenv(name) or "").strip()
            if value and value not in values:
                values.append(value)
        return tuple(values)
    return (_read_api_key(backend),)


def _clean_usage(raw: Any, usage_fields: Mapping[str, str]) -> Dict[str, Any]:
    """使用量から、記録・返却してよい数値だけを写した dict を作る。

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
    for name, field_name in usage_fields.items():
        value = raw.get(field_name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or value < 0:
            LOGGER.warning(
                "[reflex] usage field %r is not a usable token count: %r (dropping that field)",
                field_name, value,
            )
            continue
        cleaned[name] = value
    return cleaned


#: LLM クライアントの ``consume_usage()`` から写す欄 (llm_clients/base.py の UsageInfo)。
_CLIENT_USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")


def _usage_from_client(info: Any) -> Dict[str, Any]:
    """LLM クライアントの ``consume_usage()`` の戻りを、記帳できる dict に写す。

    クライアントは自分では記帳しない (記帳は呼び出し側の仕事) ので、ここで受け取って
    この層の入口 (:func:`_record_usage`) へ渡す。数値として成り立たない欄は
    :func:`_clean_usage` と同じ規則で落とす。
    """
    if info is None:
        return {}
    try:
        raw = {name: getattr(info, name) for name in _CLIENT_USAGE_FIELDS}
        ttl = info.cache_ttl
    except AttributeError:
        LOGGER.warning(
            "[reflex] the LLM client returned an unexpected usage object (%s); "
            "recording nothing for this call", type(info).__name__,
        )
        return {}
    cleaned = _clean_usage(raw, {name: name for name in _CLIENT_USAGE_FIELDS})
    if isinstance(ttl, str) and ttl:
        cleaned["cache_ttl"] = ttl
    return cleaned


def _request_question(qid: str, question: Mapping[str, Any], backend: ReflexBackend) -> Dict[str, Any]:
    """呼び出し側の質問定義を、答える側へ渡す形へ整える。

    Raises:
        ValueError: 質問が辞書 (Mapping) でない / ``instructions`` が無い / 型が
            3 型のどれでもない (呼び出し側の契約違反。API の障害ではないので
            ``ReflexJudgmentUnavailable`` へは正規化しない — 呼び出し側のバグを
            「外部 API が落ちていた」に化かすと、フォールバックに隠れて永久に
            気づかれない)。
        ReflexJudgmentUnavailable: 答える側が対応していない型 (部分回答を採らない
            既存の厳格さのまま、質問ひとまとまりごと不成立にする)。
    """
    if not isinstance(qid, str) or not qid:
        # qid は答えとの突き合わせの鍵 (Decisions では name)。文字列でないと JSON に
        # した時点で形が変わり、返ってきた答えと対応しなくなる。
        raise ValueError(f"question id {qid!r} must be a non-empty string")
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


def _usable_options(question: Mapping[str, Any]) -> Optional[Tuple[str, ...]]:
    """選択肢の宣言を、答えの検算に使える形 (文字列の並び) で返す。

    宣言が無い / 文字列以外が混ざっている質問では None (= 検算しない)。選択肢が
    文字列でない形で使われているときに、正しい答えを取りこぼさないため。
    """
    options = question.get("options")
    if (
        isinstance(options, (list, tuple))
        and options
        and all(isinstance(option, str) for option in options)
    ):
        return tuple(options)
    return None


def _read_answer(
    qid: str,
    qtype: str,
    answer: Mapping[str, Any],
    backend: ReflexBackend,
    secret: _Secrets,
    options: Optional[Sequence[str]] = None,
) -> Any:
    """答え 1 件を型ごとに検算して返す (jev 互換の道と変換層の道で共用する)。

    ``noul`` (0〜1 の確率) だけは実運用で使われている型なので、範囲まで固定する。
    ``score`` は有限の数値であること、``choice`` は空でない文字列であることを
    確かめる。``options`` が渡っていれば、``choice`` の答えがその中のどれかで
    あることまで要求する — 選択肢の外の答えは、呼び出し側が場合分けできない値
    なので「判定できた」にしない。
    """
    answer_type = answer.get("type")
    if answer_type is not None and answer_type != qtype:
        shown = _masked_repr(answer_type, secret)
        LOGGER.warning("[reflex] answer for qid=%r has type=%s (expected %r)", qid, shown, qtype)
        raise ReflexJudgmentUnavailable(
            f"answer for question {qid!r} has type {shown}, expected {qtype!r}"
        )

    field_name = backend.answer_fields.get(qtype, qtype)
    value = answer.get(field_name)

    if qtype == "choice":
        if not isinstance(value, str) or not value.strip():
            LOGGER.warning(
                "[reflex] answer for qid=%r has no %r string: %s",
                qid, field_name, _masked_repr(value, secret),
            )
            raise ReflexJudgmentUnavailable(
                f"answer for question {qid!r} has no {field_name!r} string"
            )
        if options is not None and value not in options:
            shown = _masked_repr(value, secret)
            LOGGER.warning(
                "[reflex] answer for qid=%r is not one of the offered options: %s", qid, shown,
            )
            raise ReflexJudgmentUnavailable(
                f"answer for question {qid!r} is not one of the offered options: {shown}"
            )
        return value

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        LOGGER.warning(
            "[reflex] answer for qid=%r has non-numeric %r: %s",
            qid, field_name, _masked_repr(value, secret),
        )
        raise ReflexJudgmentUnavailable(
            f"answer for question {qid!r} has non-numeric {field_name!r}"
        )
    number = float(value)
    if not math.isfinite(number):
        shown = _masked_repr(number, secret)
        LOGGER.warning("[reflex] answer for qid=%r is not a finite number: %s", qid, shown)
        raise ReflexJudgmentUnavailable(f"answer for question {qid!r} is not a finite number: {shown}")
    if qtype == "noul" and not (0.0 <= number <= 1.0):
        shown = _masked_repr(number, secret)
        LOGGER.warning("[reflex] answer for qid=%r has out-of-range %r: %s", qid, field_name, shown)
        raise ReflexJudgmentUnavailable(
            f"answer for question {qid!r} has out-of-range {field_name!r}: {shown}"
        )
    return number


def _read_all_answers(
    answers: Any,
    types_by_qid: Mapping[str, str],
    options_by_qid: Mapping[str, Optional[Tuple[str, ...]]],
    backend: ReflexBackend,
    secret: _Secrets,
) -> Dict[str, Any]:
    """要求した全 qid の答えを検算して ``{qid: 答え}`` にする (部分回答は不成立)。"""
    resolved: Dict[str, Any] = {}
    for qid, qtype in types_by_qid.items():
        answer = answers.get(qid) if isinstance(answers, dict) else None
        if not isinstance(answer, dict):
            LOGGER.warning("[reflex] no answer object for qid=%r", qid)
            raise ReflexJudgmentUnavailable(f"no answer for question {qid!r}")
        resolved[qid] = _read_answer(
            qid, qtype, answer, backend, secret, options_by_qid.get(qid),
        )
    return resolved


def _record_usage(backend: ReflexBackend, usage: Mapping[str, Any], persona_id: Optional[str]) -> None:
    """使用量を既存の記帳 (saiverse/usage_tracker.py) へモデル設定キー名義で載せる。

    LLM クライアントが通る道と同じ入口を使うので、モデル設定ファイルに ``pricing``
    を書けば費用もそのまま出る (intent §2)。記帳の失敗で会話を止めない。

    キャッシュの欄は、値が入っているときだけ渡す — jev 互換の道は input/output しか
    持たないので、そちらの記帳は今までどおりの形のまま。
    """
    if not usage:
        return
    try:
        from saiverse.usage_tracker import get_usage_tracker

        extra: Dict[str, Any] = {}
        for name in ("cached_tokens", "cache_write_tokens"):
            if name in usage:
                extra[name] = int(usage.get(name) or 0)
        ttl = usage.get("cache_ttl")
        if isinstance(ttl, str) and ttl:
            extra["cache_ttl"] = ttl

        get_usage_tracker().record_usage(
            model_id=backend.model_key,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            persona_id=persona_id,
            node_type=USAGE_CATEGORY,
            category=USAGE_CATEGORY,
            **extra,
        )
    except Exception:
        LOGGER.warning("[reflex] failed to record usage for %r", backend.model_key, exc_info=True)


# ---------------------------------------------------------------------------
# リクエストの形の変換 (request_shape が openai_decisions の宛先だけ)
#
# 呼び出し側が組む質問と状況は System One の形のまま (本流)。OpenAI の Decisions
# API へは、送る直前にここで組み替え、受け取った答えを qid の辞書へ戻してから
# 既存の検算 (_read_all_answers) に渡す。
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    """文字列はそのまま、それ以外は JSON の 1 行にする (input・説明欄へ書くとき用)。"""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _has_information(value: Any) -> bool:
    """中身があるか。Decisions への変換で「中身が無い」の判定はこの 1 箇所だけを使う。

    中身が無いのは、null・空白だけの文字列・空の辞書・空のリスト/タプル。choice の
    説明、末尾の節の行、score の段階、choice の選択肢の値のすべてに同じ判定を掛ける
    (場所ごとに判定が違うと、同じ値が一方では落ち、他方では ``{}`` のような文字列に
    なって送られる)。0 や False は中身として扱う。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple)):
        return len(value) > 0
    return True


def _criteria_lines(criteria: Any) -> list:
    """構造に居場所の無い基準 1 件を、input の最後に書き足す行の並びにする。

    辞書なら ``key: value`` を 1 行ずつ (値が文字列でなければ JSON で書く。値に中身の
    無いキー — :func:`_has_information` — は情報が無いので載せない)。辞書でなければ
    全体を 1 行 (文字列ならそのまま、それ以外は JSON)。中身が無ければ空の並び。
    """
    if isinstance(criteria, Mapping):
        return [
            f"{_as_text(name)}: {_as_text(value)}"
            for name, value in criteria.items()
            if _has_information(value)
        ]
    if not _has_information(criteria):
        return []
    return [_as_text(criteria)]


def _decisions_input(state: Any, criteria_blocks: Sequence[Sequence[str]]) -> str:
    """Decisions の ``input`` (1 本の文字列) を組む。

    状況が文字列ならそのまま、それ以外は JSON。Decisions の質問の構造に居場所の無い
    基準 (noul の基準と、choices に入らなかった choice の基準) を持つ質問が 1 つでも
    あれば、その節を input の**最後**に書き足す (まはーの裁定「criteria は input の
    最後に押し込む」)。各ブロックは ``[<qid>] <instructions>`` の見出し行で始まる —
    OpenAI がモデルに ``name`` を見せるかは確かめられていないので、質問文でも結び
    付ける。載せる質問が 1 つも無ければ節ごと付けない。
    """
    text = _as_text(state)
    if not criteria_blocks:
        return text
    lines = [text, "", _DECISIONS_CRITERIA_HEADER]
    for block in criteria_blocks:
        lines.extend(block)
    return "\n".join(lines)


def _decisions_choices(
    qid: str, question: Mapping[str, Any], backend: ReflexBackend,
) -> Tuple[list, list]:
    """choice 質問の選択肢を Decisions の ``choices`` に写す。

    値は、``options`` が空でないリスト/タプルならその順 (要素が文字列でなければ
    :func:`_as_text` で文字列にする — 文字列でない options を黙って捨てると、OpenAI の
    宛先だけが使えない形が残る)、無ければ ``criteria`` が辞書のときそのキーの順
    (System One の choice の criteria は「選択肢 → 説明」の辞書。キーも文字列にする)。

    説明は、``criteria`` の辞書のキーを文字列にしたものが値の文字列と一致すれば
    (``{1: "one"}`` と options ``["1"]`` は対応する)、その値に中身があるとき
    (:func:`_has_information`) それを文字列化したもの、それ以外は値と同じ文字列
    (description を省略できるかは公式資料で確かめられていないので、空にはしない)。

    Returns:
        ``(choices, 末尾の節に回す行)``。``criteria`` が辞書でなければ全体を、辞書なら
        文字列にしたキーが choices の値のどれとも一致しないキー (説明として choices に
        入らなかったキー) だけを末尾へ回す。choices に入った説明は構造の側だけに入り、
        節には重ねない。

    Raises:
        ReflexJudgmentUnavailable: この形で表せない choice — 選択肢を 1 つも作れない、
            options がリスト/タプルでない、中身の無い選択肢の値がある、文字列にした
            後で選択肢の値が重複する (答えがどれを指すか決められない)、文字列にした
            後で同じになる criteria のキーが複数あってその文字列が選択肢の値になって
            いる (どの説明を使うか決められない)。
    """
    def refuse(problem: str) -> ReflexJudgmentUnavailable:
        return ReflexJudgmentUnavailable(
            f"the destination for {backend.model_key!r} cannot express the choice "
            f"question {qid!r}: {problem}"
        )

    criteria = question.get("criteria")
    descriptions = criteria if isinstance(criteria, Mapping) else {}
    options = question.get("options")
    if options is not None and not isinstance(options, (list, tuple)):
        # _request_question は空の options を落とすので、ここへ来るのは中身のある
        # 辞書や文字列。criteria のキーへ黙って切り替えると options が消える。
        raise refuse(f"options must be a list, got {type(options).__name__}")
    raw_values: Sequence[Any] = options if options else list(descriptions.keys())
    if not raw_values:
        raise ReflexJudgmentUnavailable(
            f"the destination for {backend.model_key!r} needs options (or a criteria "
            f"mapping) for the choice question {qid!r}"
        )

    values: list = []
    for raw in raw_values:
        if not _has_information(raw):
            raise refuse("an option has no content")
        text = _as_text(raw)
        if text in values:
            raise refuse(f"the option {text!r} appears more than once")
        values.append(text)

    # criteria のキーは文字列にしてから選択肢の値と突き合わせる。
    described_by_text: Dict[str, Any] = {}
    leftover_criteria: Dict[Any, Any] = {}
    for name, description in descriptions.items():
        text = _as_text(name)
        if text not in values:
            leftover_criteria[name] = description
            continue
        if text in described_by_text:
            raise refuse(f"more than one criteria key describes the option {text!r}")
        described_by_text[text] = description

    choices = []
    for text in values:
        described = described_by_text.get(text)
        choices.append({
            "value": text,
            "description": _as_text(described) if _has_information(described) else text,
        })
    if isinstance(criteria, Mapping):
        leftover = _criteria_lines(leftover_criteria)
    else:
        leftover = _criteria_lines(criteria)
    return choices, leftover


def _decisions_levels(qid: str, question: Mapping[str, Any], backend: ReflexBackend) -> list:
    """score 質問の段階の説明 (System One の criteria の配列) を ``levels`` に写す。

    System One の score の criteria は順序付きの段階の説明の配列で、答えは段階の番号の
    確率加重平均 — Decisions の score と同じ意味。2 個未満・配列でない criteria は
    段階にならないので不成立 (Jev でも受け付けない形)。

    中身の無い段階 (:func:`_has_information` — null・空白だけの文字列・空の入れ物) が
    1 つでもあれば不成立。黙って落とすと後ろの段階の番号が 1 つずつずれ、答えの
    score の意味が変わる (段階 2 の答えが、呼び出し側の段階 3 を指してしまう)。
    """
    criteria = question.get("criteria")
    if not isinstance(criteria, (list, tuple)) or len(criteria) < _DECISIONS_MIN_LEVELS:
        raise ReflexJudgmentUnavailable(
            f"the destination for {backend.model_key!r} needs at least "
            f"{_DECISIONS_MIN_LEVELS} ordered levels in the criteria of the score "
            f"question {qid!r}"
        )
    levels = []
    for index, level in enumerate(criteria):
        if not _has_information(level):
            raise ReflexJudgmentUnavailable(
                f"the destination for {backend.model_key!r} cannot express the score "
                f"question {qid!r}: level {index} has no content"
            )
        label = _as_text(level)
        levels.append({"label": label, "description": label})
    return levels


def _decisions_request(
    state: Any,
    request_questions: Mapping[str, Mapping[str, Any]],
    backend: ReflexBackend,
) -> Tuple[Dict[str, Any], Dict[str, Tuple[str, ...]]]:
    """System One の形の状況と質問を、Decisions のリクエストへ一度に組み替える。

    ``questions`` (name 付きの配列) は呼び出し側の順序のまま組む。criteria は
    ``criteria`` の欄としては入れない (欄が無い)。choice の説明は ``choices``、score の
    段階は ``levels`` へ写し、構造に居場所の無い基準 (noul の基準、choices に入らなかった
    choice の基準) は :func:`_decisions_input` が input の最後の節へ書く。score の
    criteria は段階としてしか使わない (2 個以上の配列でなければ不成立) ので末尾には
    載せない。

    Returns:
        ``(payload, qid → 実際に送った choices の value の並び)``。後者は choice の答えが
        送った選択肢のどれかであることの検算に使う (criteria の辞書だけから組んだ
        choice でも検算が効くように)。

    Raises:
        ReflexJudgmentUnavailable: この形で表せない質問 — Decisions に型が無い、選択肢を
            作れない・選択肢の値が空か重複する choice、段階を作れない・中身の無い段階が
            ある score。答えられない型を含む質問と同じ扱いで、ひとまとまりごと不成立に
            する。
        Exception: 状況や基準を JSON にできない (TypeError / ValueError など)。型を問わず
            呼び出し側の :func:`_evaluate` が ``ReflexJudgmentUnavailable`` へ正規化する。
    """
    converted = []
    criteria_blocks: list = []
    offered: Dict[str, Tuple[str, ...]] = {}
    for qid, question in request_questions.items():
        qtype = question["type"]
        decisions_type = _DECISIONS_QUESTION_TYPES.get(qtype)
        if decisions_type is None:
            raise ReflexJudgmentUnavailable(
                f"the destination for {backend.model_key!r} cannot express {qtype!r} questions"
            )
        instructions = question["instructions"]
        entry: Dict[str, Any] = {
            "type": decisions_type,
            "name": qid,
            "instructions": instructions,
        }
        tail: list = []
        if qtype == "choice":
            choices, tail = _decisions_choices(qid, question, backend)
            entry["choices"] = choices
            offered[qid] = tuple(choice["value"] for choice in choices)
        elif qtype == "score":
            entry["levels"] = _decisions_levels(qid, question, backend)
        else:
            tail = _criteria_lines(question.get("criteria"))
        converted.append(entry)
        if tail:
            criteria_blocks.append([f"[{qid}] {instructions}", *tail])
    payload = {
        "model": backend.api_model,
        "input": _decisions_input(state, criteria_blocks),
        "questions": converted,
    }
    return payload, offered


def _answers_from_decisions(
    raw_answers: Any, backend: ReflexBackend, secret: _Secrets,
) -> Dict[str, Any]:
    """Decisions の ``answers`` (name 付きの配列) を ``{qid: 答え}`` の辞書へ戻す。

    戻すときに型名を SAIVerse 側へ写す (predicate → noul)。要求していない名前は
    辞書に載っても使われない (既存の辞書形式と同じく無視)。要求した名前の欠落は
    呼び出し側の :func:`_read_all_answers` が部分回答として不成立にする。

    - ``type: "refusal"`` の要素は、その質問の答えが無いものとして扱う (辞書に
      載せない → 要求した質問なら不成立)。拒否されたことは WARNING に出す。
    - 同じ ``name`` が 2 回出たら不正応答 (どちらを採るかを決められない)。
    - 辞書でない要素・``name`` が文字列でない要素も不正応答 (黙って捨てると、壊れた
      応答が「その質問の答えが無い」に化けて原因が見えなくなる)。
    - ``type`` が文字列で ``predicate`` / ``choice`` / ``score`` / ``refusal`` のどれかで
      ない要素 (型の欄が無い、``noul`` のような System One の語彙、未知の語) も不正応答。
      写した後の型と質問の型の食い違い (predicate を choice の質問へ返す等) は、既存の
      :func:`_read_answer` が不成立にする。

    Raises:
        ReflexJudgmentUnavailable: ``answers`` が配列でない・形の壊れた要素・名前の重複・
            読めない型名。
    """
    if not isinstance(raw_answers, list):
        LOGGER.warning("[reflex] response has no %r array", backend.answers_key)
        raise ReflexJudgmentUnavailable(f"response has no {backend.answers_key!r} array")

    answers: Dict[str, Any] = {}
    seen: set = set()
    for entry in raw_answers:
        if not isinstance(entry, dict):
            shown = _masked_repr(entry, secret)[:_ERROR_BODY_PREVIEW]
            LOGGER.warning("[reflex] an entry in %r is not an object: %s", backend.answers_key, shown)
            raise ReflexJudgmentUnavailable(
                f"an entry in {backend.answers_key!r} is not an object: {shown}"
            )
        name = entry.get("name")
        if not isinstance(name, str):
            shown = _masked_repr(name, secret)[:_ERROR_BODY_PREVIEW]
            LOGGER.warning("[reflex] an entry in %r has no name string: %s", backend.answers_key, shown)
            raise ReflexJudgmentUnavailable(
                f"an entry in {backend.answers_key!r} has no name string: {shown}"
            )
        if name in seen:
            shown = _masked_repr(name, secret)[:_ERROR_BODY_PREVIEW]
            LOGGER.warning("[reflex] the answer for %s appears more than once", shown)
            raise ReflexJudgmentUnavailable(f"the answer for {shown} appears more than once")
        seen.add(name)

        answer_type = entry.get("type")
        if not isinstance(answer_type, str) or (
            answer_type not in _DECISIONS_ANSWER_TYPES and answer_type != _DECISIONS_REFUSAL
        ):
            # 型の欄が無い・System One の語彙 (noul)・未知の語は、Decisions の応答として
            # 読めない。型の欄が無い要素を素通しすると、既存の検算 (_read_answer) は
            # 型の欄の無い答えを受け付けるので、壊れた応答が「判定できた」になりうる。
            shown_name = _masked_repr(name, secret)[:_ERROR_BODY_PREVIEW]
            shown = _masked_repr(answer_type, secret)[:_ERROR_BODY_PREVIEW]
            LOGGER.warning(
                "[reflex] the answer for %s has an unexpected type %s", shown_name, shown,
            )
            raise ReflexJudgmentUnavailable(
                f"the answer for {shown_name} has an unexpected type {shown}"
            )
        if answer_type == _DECISIONS_REFUSAL:
            LOGGER.warning(
                "[reflex] %r refused to answer qid=%s",
                backend.model_key, _masked_repr(name, secret)[:_ERROR_BODY_PREVIEW],
            )
            continue
        answer = dict(entry)
        answer["type"] = _DECISIONS_ANSWER_TYPES[answer_type]
        answers[name] = answer
    return answers


# ---------------------------------------------------------------------------
# 答える側ごとの往復 (器は evaluate が共有する)
# ---------------------------------------------------------------------------


class _JevCall:
    """jev 互換の宛先へ、方言の宣言 (``request_shape``) どおりの形で 1 往復する道。

    ``system_one`` なら System One の形をそのまま、``openai_decisions`` なら
    :func:`_evaluate` が組み替えた Decisions の形を送り、答えは qid の辞書へ戻して
    から既存の検算に渡す。

    ``evaluate`` の器 (同時実行の枠・ワーカースレッド・絶対締切・使用量の記帳の
    取り決め) はこのクラスの外にある。ここが持つのは「1 往復の中身」だけ。
    """

    def __init__(
        self,
        backend: ReflexBackend,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
        transport: Optional[Any],
    ) -> None:
        self.backend = backend
        self.payload = payload
        self.headers = headers
        self.timeout = timeout
        self.transport = transport
        self._client: Optional[httpx.Client] = None

    def describe(self) -> str:
        return f"POST {self.backend.url}"

    def start(self) -> None:
        # client はメインスレッド側で作る。締切を超えたときに、ここから close() して
        # 置き去りのワーカーの通信を切れるようにするため。
        self._client = httpx.Client(timeout=self.timeout, transport=self.transport)

    def perform(self) -> Any:
        return self._client.post(self.backend.url, json=self.payload, headers=self.headers)

    def abort(self) -> None:
        """締切で見切ったときに通信を切る (ワーカーの post が例外で戻る)。"""
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()  # 冪等

    def record_abandoned_usage(self, result: Any, persona_id: Optional[str]) -> None:
        """締切で見切った呼び出しの応答から、**使用量だけ**を best-effort で記帳する。"""
        if getattr(result, "status_code", None) != 200:
            return
        try:
            data = result.json()
        except Exception:
            return
        if not isinstance(data, dict):
            return
        usage = _clean_usage(data.get(self.backend.usage_key), self.backend.usage_fields)
        if not usage:
            return
        LOGGER.info(
            "[reflex] the call to %r was abandoned at the deadline but its answer did "
            "arrive; recording its usage only: %s",
            self.backend.model_key, usage,
        )
        _record_usage(self.backend, usage, persona_id)

    def read(
        self,
        result: Any,
        types_by_qid: Mapping[str, str],
        options_by_qid: Mapping[str, Optional[Tuple[str, ...]]],
        persona_id: Optional[str],
        secret: _Secrets,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        backend = self.backend
        if result.status_code != 200:
            # 応答本文に API キーがそのまま載っている実装もありうるので、プレビューを
            # 作る前にキーの値を伏せる (デバッグ価値のある本文自体は残す)。
            body = _mask_secret(result.text or "", secret)[:_ERROR_BODY_PREVIEW]
            LOGGER.warning("[reflex] non-200 response: status=%s body=%r", result.status_code, body)
            raise ReflexJudgmentUnavailable(f"HTTP {result.status_code}: {body}")

        try:
            data = result.json()
        except ValueError as exc:
            LOGGER.warning("[reflex] response is not valid JSON: %s", exc)
            raise ReflexJudgmentUnavailable(f"invalid JSON response: {exc}") from exc

        if not isinstance(data, dict):
            LOGGER.warning("[reflex] response is not a JSON object: %s", type(data).__name__)
            raise ReflexJudgmentUnavailable("response is not a JSON object")

        # 使用量の記帳は、答えを読む**前**に済ませる (理由は evaluate の Note)。
        usage = _clean_usage(data.get(backend.usage_key), backend.usage_fields)
        _record_usage(backend, usage, persona_id)

        answers = data.get(backend.answers_key)
        if backend.request_shape == REQUEST_SHAPE_OPENAI_DECISIONS:
            # name 付きの配列で返る形。qid の辞書へ戻してから同じ検算に渡す。
            answers = _answers_from_decisions(answers, backend, secret)
        elif not isinstance(answers, dict):
            LOGGER.warning("[reflex] response has no %r object", backend.answers_key)
            raise ReflexJudgmentUnavailable(f"response has no {backend.answers_key!r} object")

        resolved = _read_all_answers(answers, types_by_qid, options_by_qid, backend, secret)
        return resolved, usage


#: 通常の LLM に渡す前置き。会話ではなく判定であることを最初に言う。
_LLM_PROMPT_HEADER = (
    "You are a judgment component inside a running system, not a conversational "
    "assistant. Read the situation below and answer every question with a value "
    "only. Do not greet, explain, apologise or write prose."
)

#: 答え方の指示 (構造化出力のスキーマと対になる文)。
_LLM_PROMPT_FOOTER = (
    "Answer EVERY question listed above. Return exactly one entry per qid in "
    "`answers`, and fill only the field that matches that question's type: "
    "`noul` for a probability between 0 and 1, `score` for a number, `choice` "
    "for one of that question's options copied exactly as written. Do not add "
    "entries for qids that were not asked."
)


def _build_llm_prompt(state: Any, request_questions: Mapping[str, Mapping[str, Any]]) -> str:
    """状況と質問一覧を 1 通のプロンプトに畳む。"""
    lines = [
        _LLM_PROMPT_HEADER,
        "",
        "SITUATION (JSON):",
        json.dumps(state, ensure_ascii=False, default=str),
        "",
        "QUESTIONS:",
    ]
    for qid, question in request_questions.items():
        lines.append(f"- qid: {qid}")
        lines.append(f"  type: {question['type']}")
        lines.append(f"  instructions: {question['instructions']}")
        criteria = question.get("criteria")
        if criteria:
            lines.append(f"  criteria: {json.dumps(criteria, ensure_ascii=False, default=str)}")
        options = question.get("options")
        if options:
            lines.append(f"  options: {json.dumps(options, ensure_ascii=False, default=str)}")
    lines.extend(["", _LLM_PROMPT_FOOTER])
    return "\n".join(lines)


def _llm_response_schema(
    request_questions: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """構造化出力のスキーマを組む。

    **辞書ではなく配列で受ける**: qid は実行時に決まるので辞書スキーマにすると
    キーを列挙できず、Gemini は ``additionalProperties`` を受け付けない (intent §3)。
    要素は固定のプロパティだけを持ち、union 型も使わない — 型ごとの欄を並べ、
    どれを埋めるかはプロンプトの指示と受け取り側の検算で決める。

    全部の choice 質問が選択肢を宣言しているときだけ、``choice`` の欄に enum を
    付ける (宣言の無い質問が 1 つでもあると、その答えまで縛ってしまう)。
    """
    choice_options: list = []
    every_choice_declares_options = True
    for question in request_questions.values():
        if question.get("type") != "choice":
            continue
        options = _usable_options(question)
        if options is None:
            every_choice_declares_options = False
            continue
        for option in options:
            if option not in choice_options:
                choice_options.append(option)

    choice_schema: Dict[str, Any] = {"type": "string"}
    if choice_options and every_choice_declares_options:
        choice_schema["enum"] = list(choice_options)

    return {
        "type": "object",
        "properties": {
            _LLM_ANSWERS_KEY: {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "qid": {"type": "string"},
                        "noul": {"type": "number"},
                        "score": {"type": "number"},
                        "choice": choice_schema,
                    },
                    "required": ["qid"],
                },
            },
        },
        "required": [_LLM_ANSWERS_KEY],
    }


class _LLMCall:
    """質問をプロンプトへ変換し、構造化出力で答えさせる道。

    ``generate`` は途中で切れないので、締切で見切ってもワーカーは走り続ける
    (daemon なのでプロセスの終了は妨げない)。**遅いモデルは応答が返るまで同時実行の
    枠を 1 本占有し続ける** — 枠が尽きている間の呼び出しは待たずに
    ``ReflexJudgmentUnavailable`` になり、会話は従来方式へ戻る。
    """

    def __init__(
        self,
        backend: ReflexBackend,
        state: Any,
        request_questions: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.backend = backend
        self.messages = [{"role": "user", "content": _build_llm_prompt(state, request_questions)}]
        self.schema = _llm_response_schema(request_questions)

    def describe(self) -> str:
        return f"structured-output call to {self.backend.api_model}"

    def start(self) -> None:
        """事前に用意するものは無い (クライアントはワーカーの中で作る)。"""

    def perform(self) -> Any:
        from llm_clients import get_llm_client
        from saiverse.model_configs import get_context_length, get_model_provider

        key = self.backend.model_key
        client = get_llm_client(key, get_model_provider(key), get_context_length(key))
        # 一撃の判定に長考は要らない (待ち時間と費用だけが増える)。モデル設定や画面で
        # 思考の深さが決まっているときは、クライアント側がそれを尊重して何もしない
        # (llm_clients/base.py の prefer_minimal_reasoning の契約)。
        client.prefer_minimal_reasoning()
        # temperature は指定しない (モデル設定の既定に従う)。
        raw = client.generate(self.messages, response_schema=self.schema)
        # クライアントは自分では記帳しない (記帳は呼び出し側の仕事) ので、ここで
        # 受け取って運ぶ。記帳そのものは read / record_abandoned_usage のどちらか
        # 一方だけが行う (evaluate の錠の取り決め)。
        return (raw, client.consume_usage())

    def abort(self) -> None:
        """``generate`` は途中で切れない。走らせたまま置き去りにする (枠は占有のまま)。"""

    def close(self) -> None:
        """閉じるものは無い (クライアントはワーカーの中で使い切る)。"""

    def record_abandoned_usage(self, result: Any, persona_id: Optional[str]) -> None:
        _raw, info = result
        usage = _usage_from_client(info)
        if not usage:
            return
        LOGGER.info(
            "[reflex] the call to %r was abandoned at the deadline but its answer did "
            "arrive; recording its usage only: %s",
            self.backend.model_key, usage,
        )
        _record_usage(self.backend, usage, persona_id)

    def read(
        self,
        result: Any,
        types_by_qid: Mapping[str, str],
        options_by_qid: Mapping[str, Optional[Tuple[str, ...]]],
        persona_id: Optional[str],
        secret: _Secrets,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        raw, info = result
        # 答えを読む**前**に記帳する (理由は evaluate の Note)。
        usage = _usage_from_client(info)
        _record_usage(self.backend, usage, persona_id)

        data: Any = raw
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError) as exc:
                LOGGER.warning("[reflex] the model's answer is not valid JSON: %s", exc)
                raise ReflexJudgmentUnavailable(f"invalid JSON answer: {exc}") from exc
        if not isinstance(data, dict):
            LOGGER.warning(
                "[reflex] the model's answer is not a JSON object: %s", type(data).__name__,
            )
            raise ReflexJudgmentUnavailable("the answer is not a JSON object")

        entries = data.get(_LLM_ANSWERS_KEY)
        if not isinstance(entries, list):
            LOGGER.warning("[reflex] the model's answer has no %r array", _LLM_ANSWERS_KEY)
            raise ReflexJudgmentUnavailable(f"the answer has no {_LLM_ANSWERS_KEY!r} array")

        # 配列で受けた答えを {qid: 答え} へ戻す (辞書スキーマを組めない分の埋め合わせ)。
        answers: Dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            qid = entry.get("qid")
            if not isinstance(qid, str) or not qid:
                continue
            answers[qid] = {key: value for key, value in entry.items() if key != "qid"}

        resolved = _read_all_answers(answers, types_by_qid, options_by_qid, self.backend, secret)
        return resolved, usage


# ---------------------------------------------------------------------------
# 直近の呼び出しの結果 (画面の設定の警告が読む、プロセス内の小さな記録)
#
# チャットの注記は「いま起きたターン」しか知らせない。設定画面で「最近どのくらい
# 起きているか」を見せるために、呼び出し 1 回ごとの結果をここに積む。プロセス内
# だけの記録で、再起動で消えて構わない (これは効果の観測であって、世界の状態では
# ないため — 永続化すると「どのペルソナの・いつの」まで背負うことになる)。
# ---------------------------------------------------------------------------

#: 呼び出し 1 回の結果。
OUTCOME_OK = "ok"              # 判定が成立した
OUTCOME_DEADLINE = "deadline"  # 時間内に答えなかった (課金は発生している)
OUTCOME_FAILED = "failed"      # それ以外の失敗 (キー欠落・接続失敗・不正応答など)

#: 何回分さかのぼって数えるか。
OUTCOME_HISTORY_SIZE = 20

#: どのくらい昔までの記録を数えるか (秒)。窓の外の記録は数から落とす。
#: これは**効果の観測**なので、古い証拠で警告を出し続けない — モデルを速いものへ
#: 替えて直したあと、そのペルソナとしばらく喋らなければ、件数だけを頼りにした
#: 警告は永遠に「最近 3 回中 3 回…」と言い続ける。時間の窓を付けると、直した人が
#: 何もしなくても警告は消える。
OUTCOME_WINDOW_SECONDS = 1800.0

#: ``(結果, 記録した時刻)``。時刻は単調時計 (システム時計の調整で過去へ飛ばない)。
_OUTCOMES: Deque[Tuple[str, float]] = deque(maxlen=OUTCOME_HISTORY_SIZE)
_OUTCOMES_LOCK = threading.Lock()


def _record_outcome(outcome: str) -> None:
    """呼び出し 1 回分の結果を記録する (複数のペルソナが同時に呼ぶのでロックする)。"""
    with _OUTCOMES_LOCK:
        _OUTCOMES.append((outcome, time.monotonic()))


def recent_outcomes() -> Tuple[int, int, int]:
    """``(直近の呼び出し回数, 締切超過だった回数, それ以外の失敗の回数)``。

    数えるのは直近 :data:`OUTCOME_HISTORY_SIZE` 件のうち、記録から
    :data:`OUTCOME_WINDOW_SECONDS` 秒以内のものだけ。まだ 1 回も呼んでいない
    プロセスと、窓の中に記録が無いプロセスでは ``(0, 0, 0)``。

    締切超過と「それ以外の失敗」を分けて返すのは、警告の文面が対処を書き分けられる
    ようにするため — 分母に他の理由の失敗が混ざっていると、キー欠落で落ち続けている
    家にも「待ち時間を延ばせ」と読める文面が出る。
    """
    cutoff = time.monotonic() - OUTCOME_WINDOW_SECONDS
    with _OUTCOMES_LOCK:
        snapshot = [item for item, at in _OUTCOMES if at >= cutoff]
    return (
        len(snapshot),
        sum(1 for item in snapshot if item == OUTCOME_DEADLINE),
        sum(1 for item in snapshot if item == OUTCOME_FAILED),
    )


def reset_recent_outcomes() -> None:
    """記録を捨てる (テストの相互汚染を断つための口)。"""
    with _OUTCOMES_LOCK:
        _OUTCOMES.clear()


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
    """:func:`_evaluate` の入口。結果を直近の記録へ 1 件積んでから返す / 投げ直す。

    引数・戻り値・例外は :func:`_evaluate` のまま (記録以外は何もしない)。記録を
    ここに集めるのは、``_evaluate`` の中に散らばる出口ごとに書き足すと必ずどれかを
    書き忘れるため — 入口は 1 つしかないので、ここで包めば数え漏れが起きない。

    質問が空の呼び出しは「呼んでいない」ので数えない (外部へは何も送っていない)。

    ``ValueError`` (呼び出し側の契約違反) は記録しない — それは反射判断の成否では
    なく呼び出し側のバグで、混ぜると「宛先が不調」の顔をしてしまう。
    """
    try:
        result = _evaluate(
            state, questions,
            timeout=timeout, backend=backend, persona_id=persona_id, transport=transport,
        )
    except ReflexJudgmentUnavailable as exc:
        if questions:
            _record_outcome(
                OUTCOME_DEADLINE
                if getattr(exc, "kind", UNAVAILABLE_OTHER) == UNAVAILABLE_DEADLINE
                else OUTCOME_FAILED
            )
        raise
    if questions:
        _record_outcome(OUTCOME_OK)
    return result


def _evaluate(
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    timeout: float,
    backend: Optional[ReflexBackend] = None,
    persona_id: Optional[str] = None,
    transport: Optional[Any] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """全質問を 1 往復で評価し ``({qid: 答え}, usage情報)`` を返す。

    Args:
        state: 判断の材料になる状況。文字列でも dict/list でもよい。
        questions: ``{qid: {"type": "noul"|"choice"|"score", "instructions": str,
            "criteria": {...}, "options": [...]}}``。``type`` を省くと ``noul``。
            ``criteria`` / ``options`` は省略可。``options`` を渡した ``choice`` の
            答えは、その中のどれかであることまで要求する。
        timeout: タイムアウト (秒)。
        backend: 答える側。省略すると役割の割り当てから解決する。
        persona_id: 使用量の記帳に載せるペルソナ (分からなければ省略)。
        transport: テストからトランスポート (``httpx.MockTransport``) を差し込むための口
            (jev 互換の道だけが使う)。本番経路では常に None。

    Returns:
        (qid -> 答え, usage dict)。**要求した全 qid が揃っている**ことを保証する —
        一部だけ答えが返った応答は成功にせず ``ReflexJudgmentUnavailable`` を投げる
        (部分的な判定を「判定した」として扱うと、答えの無い質問が静かに既定へ倒れ、
        外部 API の劣化が判定結果に化けてしまうため)。答えは ``noul`` なら 0〜1 の
        float、``score`` なら有限の float、``choice`` なら文字列。

    Raises:
        ReflexJudgmentUnavailable: 答える側を解決できない・API キー欠落・
            送るものの組み立ての失敗 (Decisions への変換と通常の LLM のプロンプトの
            組み立てで出た例外は型を問わず)・リクエスト準備の失敗・
            接続失敗・タイムアウト・絶対締切超過・
            同時実行の上限超過・非 200・不正応答・部分回答・通信中に出た
            予期しない例外 (httpx 以外の型も含む)。
        ValueError: ``questions`` の形が契約を満たしていない (呼び出し側のバグ) —
            質問が辞書でない・``instructions`` が無い・型が 3 型のどれでもない。

    Note:
        使用量は**答えを読む前**に記帳する。応答が届いた時点で API 側の課金はもう
        発生しているので、答えの側の不備 (qid の欠落・型違い・範囲外) で判定が
        不成立になっても台帳からは落とさない。締切超過で見切った呼び出しも、応答が
        後から届けば**使用量だけ**は記帳される (答えは読まない)。非 200 と JSON と
        して読めない応答は記帳しない。
    """
    if backend is None:
        backend = resolve_backend()

    if not questions:
        return {}, {}

    # 呼び出し側が is_available() を通っていても、ここでもう一度見る (二重の網)。
    # 判定と呼び出しの間に env が空になることも、backend を直接渡す呼び出しもある。
    # 判定は is_available と同じ一本 (_has_credentials) から引く — 二枚に分けると、
    # 「使える」と答えた宛先を呼び出しの入口が断る食い違いが生まれる。
    # api_key は jev 互換の Authorization ヘッダに使う。伏せ字は secrets の側 —
    # 通常の LLM は代替キー名で認証することがあるので、宣言の 1 本 (api_key) だけを
    # 伏せると実際に使われたキーが例外文言に残る (_mask_targets)。
    api_key = _read_api_key(backend)
    secrets = _mask_targets(backend)
    if not _has_credentials(backend):
        _warn_missing_api_key(backend)
        raise ReflexJudgmentUnavailable(
            f"no API key is configured for {backend.model_key!r}"
            if backend.kind == REFLEX_KIND_LLM
            else f"{backend.api_key_env or 'the API key'} is not set for {backend.model_key!r}"
        )

    # 質問の検査は _request_question 1 箇所に集める。型と選択肢の一覧も検査済みの
    # 形から作る (検査前の生の質問から `.get` で拾うと、辞書でない質問が検査へ
    # 届く前に AttributeError で漏れる)。
    request_questions = {
        qid: _request_question(qid, q, backend) for qid, q in questions.items()
    }
    types_by_qid = {qid: q["type"] for qid, q in request_questions.items()}
    options_by_qid = {qid: _usable_options(q) for qid, q in request_questions.items()}

    # 送るものの組み立て。ここは同時実行の枠を取る前の区間で、例外は型を問わず
    # ReflexJudgmentUnavailable に正規化する — 状況や基準を JSON にできない
    # (文字列でないキーの辞書・循環参照など) ときの TypeError / ValueError のほか、
    # 呼び出し側の値の __str__ などが何を投げても、呼び出し側が 1 種類の例外で
    # 「今回は判定なし」と畳めるようにするため。対象は Decisions への変換と、通常の
    # LLM の道のプロンプトとスキーマの組み立て (_LLMCall の構築)。System One の道は
    # 辞書を組むだけで、JSON にするのは送信時 (ワーカーの中の httpx) なので、そこで
    # 出た例外はワーカーの例外の正規化 (下の "request failed") が受ける。
    # 呼び出し側のバグの ValueError (_request_question) はこの区間の前で出るので、
    # ここには巻き込まれない。
    call: Optional[Any] = None
    if backend.kind != REFLEX_KIND_JEV:
        build_label = "structured-output prompt"
    elif backend.request_shape == REQUEST_SHAPE_OPENAI_DECISIONS:
        build_label = "Decisions request"
    else:
        build_label = "request"
    try:
        if backend.kind == REFLEX_KIND_JEV:
            if backend.request_shape == REQUEST_SHAPE_OPENAI_DECISIONS:
                # 送る直前に OpenAI の Decisions の形へ組み替える (呼び出し側の質問と
                # 状況は System One の形のまま)。表せない質問はここで不成立になり、
                # 同時実行の枠を取る前に戻る。
                payload, offered = _decisions_request(state, request_questions, backend)
                # choice の答えの検算は、実際に送った choices の値で行う (criteria の
                # 辞書だけから組んだ choice や、文字列でない options を文字列にして
                # 送った choice でも、送っていない値を「判定できた」にしない)。
                options_by_qid = {
                    qid: offered.get(qid) if qtype == "choice" else options_by_qid[qid]
                    for qid, qtype in types_by_qid.items()
                }
            else:
                payload = {
                    "state": state,
                    "model": backend.api_model,
                    "questions": request_questions,
                }
            # API キーの値そのものはログに出さない (ヘッダごとダンプしない)。
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            call = _JevCall(backend, payload, headers, timeout, transport)
        else:
            call = _LLMCall(backend, state, request_questions)
    except ReflexJudgmentUnavailable as exc:
        # この宛先の形で表せない質問 (空の選択肢・段階が足りない score など) を、
        # 送る前に断った。設定や呼び出し方を直せば直る失敗なので、層のログにも残す。
        # 文言には呼び出し側の質問の中身 (選択肢の値など) が入るので、他の経路と
        # 同じく伏せ字を通してからログと例外に載せる。
        detail = _mask_secret(str(exc), secrets)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning(
            "[reflex] the %s for %r was refused before sending: %s",
            build_label, backend.model_key, detail,
        )
        raise ReflexJudgmentUnavailable(detail) from exc
    except Exception as exc:
        # 例外文言は外部由来の値を含みうるので伏せ字を通す。
        detail = _mask_secret(f"{type(exc).__name__}: {exc}", secrets)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning(
            "[reflex] could not build the %s for %r: %s", build_label, backend.model_key, detail,
        )
        raise ReflexJudgmentUnavailable(
            f"could not build the {build_label} for {backend.model_key!r}: {detail}"
        ) from exc

    LOGGER.debug(
        "[reflex] %s (config=%s) questions=%d timeout=%.1fs",
        call.describe(), backend.model_key, len(request_questions), timeout,
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
    try:
        call.start()
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
            result = None
            try:
                result = call.perform()
                holder["result"] = result
            except BaseException as exc:  # noqa: BLE001 - 例外はメインスレッドへ運んで再送出する
                holder["error"] = exc
            finally:
                # 完了を宣言し、同じ錠の下で「自分が記帳するか」を決める。既に見切りの
                # 印が立っていれば、その応答を受け取る者はもういないので記帳はワーカーの役。
                with outcome_lock:
                    outcome["worker_done"] = True
                    record_here = result is not None and outcome["abandoned"]
                # 置き去りにされた呼び出しの後始末。記帳の失敗でこの下の
                # close / release / done.set を妨げないよう、全体を包む。
                try:
                    if record_here:
                        call.record_abandoned_usage(result, persona_id)
                except Exception:
                    LOGGER.warning(
                        "[reflex] failed to record the abandoned call's usage",
                        exc_info=True,
                    )
                try:
                    call.close()
                except Exception:
                    pass
                # start() 成功後のセマフォの release はここ 1 箇所だけ。締切超過で
                # メインが先に戻ってもメイン側は release しないので、二重 release
                # (BoundedSemaphore の ValueError) は構造上起きない。
                _IN_FLIGHT.release()
                done.set()

        # ワーカーは daemon。締切超過で置き去りにした呼び出しが、プロセス終了時の join を
        # 妨げないようにする (非デーモンだと Python の終了そのものが止まりうる)。
        worker = threading.Thread(target=_post, name=_WORKER_THREAD_NAME, daemon=True)
        worker.start()
    except BaseException as exc:
        # start() から戻る前の失敗では、ワーカーの finally はまず走らない。
        # ここでの release は 1 回だけで、ワーカー側の release と重なることはない。
        try:
            call.close()
        except Exception:
            pass
        _IN_FLIGHT.release()
        detail = _mask_secret(f"{type(exc).__name__}: {exc}", secrets)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning("[reflex] could not start the request: %s", detail)
        raise ReflexJudgmentUnavailable(f"could not start the request: {detail}") from exc

    # 絶対締切 (壁時計)。httpx の timeout は I/O 単位なので、少量ずつ返し続ける
    # 応答は合計時間が青天井になりうる。会話の同期経路にいるので全体を打ち切る。
    deadline = timeout + DEADLINE_MARGIN
    if not done.wait(deadline):
        # 見切る前に「見切った」印を立てる。後から応答を得たワーカーは、この印を見て
        # 使用量だけを記帳する (答えは読まない — メインはもう会話を先へ進めている)。
        #
        # 締切と完了がぶつかったとき (ワーカーが既に完了を宣言していて、その時点では
        # 見切りの印がまだ立っていなかったとき) は、ワーカーは記帳せずに通り抜けて
        # いる。その応答の記帳はメインが引き受ける — 課金は発生済みなので、どちらの
        # 手からも落とさない。
        with outcome_lock:
            outcome["abandoned"] = True
            abandoned_result = holder.get("result") if outcome["worker_done"] else None
        if abandoned_result is not None:
            try:
                call.record_abandoned_usage(abandoned_result, persona_id)
            except Exception:
                LOGGER.warning(
                    "[reflex] failed to record the abandoned call's usage", exc_info=True,
                )
        # 切れる答える側なら通信を切ってワーカーを終わらせる (ワーカー側が例外で戻り、
        # その finally でセマフォが返る)。切れない側 (通常の LLM) はそのまま走らせる。
        # 会話を待たせないことが優先。
        try:
            call.abort()
        except Exception:
            pass
        LOGGER.warning("[reflex] deadline exceeded after %.1fs; abandoning the call", deadline)
        # 種別を立てるのはここだけ。「時間内に答えなかった」は待ち時間を延ばすか
        # 速い宛先へ替えれば直る失敗で、しかも課金は発生しているので、呼び出し側が
        # 利用者へ知らせられるように他の失敗と区別する。
        raise ReflexJudgmentUnavailable(
            f"deadline exceeded after {deadline:.1f}s", kind=UNAVAILABLE_DEADLINE,
        )

    error = holder.get("error")
    if error is not None:
        # 例外の型を問わず ReflexJudgmentUnavailable に正規化する。httpx 以外の例外
        # (トランスポート実装や差し込まれたフック、LLM クライアントが投げるもの) を
        # 素通しすると、呼び出し側が 1 種類の例外を捕まえるだけで畳めるという契約が
        # 破れる。文言にはヘッダ値 (= API キー) が混ざる型がある (h11 の Illegal
        # header value など) ので、ログにも例外メッセージにも伏せ字で載せる。
        detail = _mask_secret(f"{type(error).__name__}: {error}", secrets)[:_ERROR_BODY_PREVIEW]
        LOGGER.warning("[reflex] request failed: %s", detail)
        raise ReflexJudgmentUnavailable(f"request failed: {detail}") from error

    resolved, usage = call.read(
        holder["result"], types_by_qid, options_by_qid, persona_id, secrets,
    )

    LOGGER.debug(
        "[reflex] %s answered %d/%d question(s) usage=%s",
        backend.model_key, len(resolved), len(questions), usage,
    )
    return resolved, usage


__all__ = [
    "DEADLINE_MARGIN",
    "DIALECT_FIELD",
    "FIRST_STAGE_QUESTION_TYPE",
    "JEV_COMPAT_PROTOCOL",
    "OUTCOME_DEADLINE",
    "OUTCOME_FAILED",
    "OUTCOME_HISTORY_SIZE",
    "OUTCOME_OK",
    "OUTCOME_WINDOW_SECONDS",
    "QUESTION_TYPES",
    "REQUEST_SHAPES",
    "REQUEST_SHAPE_OPENAI_DECISIONS",
    "REQUEST_SHAPE_SYSTEM_ONE",
    "REFLEX_KIND_JEV",
    "REFLEX_KIND_LLM",
    "UNAVAILABLE_DEADLINE",
    "UNAVAILABLE_OTHER",
    "USAGE_CATEGORY",
    "ReflexBackend",
    "ReflexJudgmentUnavailable",
    "evaluate",
    "is_available",
    "recent_outcomes",
    "reflex_model_setting",
    "reset_recent_outcomes",
    "resolve_backend",
]
