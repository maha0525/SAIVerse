"""sluice (スルース) — 押し出される記憶が必ず通る採取の関所。

Metabolism の eviction 直前、メインラインの温まった prefix (head + 履歴) に注入
プロンプトを 1 手足し、structured output で「退場前に記録すべきもの」を返させる。
決定は本人 (ペルソナ)、複製・書き込みはシステム側の決定論。

採取する器は三つ (autonomous_behavior_v3.md §13.3 / §13.6):

- コア記憶 (core_adds / core_updates / core_removes) — 恒常知識
- 手帳メモ (want_memos / did_memos) — アクティビティへの日付つき一行
- 約束 (promise_adds / promise_updates) — タスク帳への追加 / 変更

これに加えて、業務日に一回だけ、記憶の手入れ (Memopedia ページの分割・統合の
提案) を本人に見せて page_reviews で approve / skip を返させる (2026-10-09 に
就寝判断から移設 — 自律 OFF のペルソナにも届けるため。v3 §6)。承認分は確定の
後に背景で実行される。後から通す採取には載せない。

旧名 gold_panning (砂金採り) から 2026-08-19 に世代交代した。名前の変化は性質の
変化を運ぶ: 手作業の一掬いから「全ての水が通る構造物」へ — スルースが失敗したら
退場は止まり (あらすじ生成の失敗と同格)、次の Metabolism 機会に再試行される。
「全ての経験が、退場の前に必ず一度、ペルソナ本人の目による構造化出力での解釈・
記録を通る」ことの保証がこのゲートの価値 (§13.3)。

不変条件 (docs/intent/gold_panning.md §5 — 機構の intent は旧名のまま残る):
- キャッシュが熱い瞬間にのみ走る (defer-to-hot は SessionLifecycle 側)
- スルース失敗 = 退場停止 (旧「失敗しても退場が進む」柔らかい格は §13.3 で廃止)
- scene は参照コピーのみ (ペルソナに書き写させない)。照合失敗は明示
- 「採取なし」が正規の応答。採取を促す圧はプロンプトに入れない
- モデルは (persona, default) 固定。lightweight へ切替えない
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, FrozenSet, List, NamedTuple, Optional, Tuple

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# env 設定 (sea/auto_recall.py と同じく毎回 os.getenv を読む)
# ---------------------------------------------------------------------------

#: 旧世代 (gold_panning) 環境変数からの設定移行 (機械写し — コード API の互換
#: シムではない)。優先順は 新キー > 旧キー > 既定。旧キーが効いたら非推奨
#: WARNING をキーごとに一度だけ出す。目的は、旧 SAIVERSE_GOLD_PANNING_ENABLED=0
#: で採取を止めていた環境が、改名後の更新で黙って採取 (課金) を再開する事故の
#: 防止。旧実装が読んでいたキーのうち今も生きているのは ENABLED / PENDING_CAP の
#: 2 つで、いずれも同名置換 (SAIVERSE_GOLD_PANNING_* → SAIVERSE_SLUICE_*)。
_LEGACY_ENV_WARNED: set = set()


def _read_env_with_legacy(name: str) -> Optional[str]:
    """新キーを読み、無ければ旧 SAIVERSE_GOLD_PANNING_* キーへフォールバックする。

    返り値は非空の生文字列か None (未設定・空白のみは None)。
    """
    raw = os.getenv(name)
    if raw is not None and raw.strip():
        return raw
    legacy_name = name.replace("SAIVERSE_SLUICE_", "SAIVERSE_GOLD_PANNING_", 1)
    if legacy_name == name:
        return None
    raw = os.getenv(legacy_name)
    if raw is not None and raw.strip():
        if legacy_name not in _LEGACY_ENV_WARNED:
            _LEGACY_ENV_WARNED.add(legacy_name)
            LOGGER.warning(
                "[sluice] %s は非推奨です — %s へ移行してください "
                "(今回は旧キーの値 %r を使用)",
                legacy_name, name, raw.strip(),
            )
        return raw
    return None


def _env_flag(name: str, default: bool) -> bool:
    raw = _read_env_with_legacy(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    raw = _read_env_with_legacy(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("[sluice] invalid float for %s=%r; using default %s", name, raw, default)
        return default


def is_enabled() -> bool:
    """全体トグル。"0"/"false" で無効化 (defer-to-hot ごと無効になる)。"""
    return _env_flag("SAIVERSE_SLUICE_ENABLED", True)


def get_pending_cap() -> float:
    """defer-to-hot 圧力弁の倍率 (high watermark の何倍で「コールドでも実行」に倒すか)。"""
    return _env_float("SAIVERSE_SLUICE_PENDING_CAP", 1.5)


def get_max_span_chars() -> int:
    """一発のスルースの呼び出しに入れてよい担当範囲の上限 (字数)。

    docs/intent/sluice_coverage_gaps.md 第一段 A: 担当範囲 (パンマーカーから
    窓の末尾まで) がこの量を超えていたら、run_metabolism はスルースを走らせ
    ない (飛ばした範囲を記録して退場は進める)。「入りきらない事態を復旧する」
    のではなく「入りきる回だけ走らせる」 — 旧 §13.5-1 の後退方式の代替。
    """
    raw = _read_env_with_legacy("SAIVERSE_SLUICE_MAX_SPAN_CHARS")
    if raw is None:
        return 100_000
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning(
            "[sluice] invalid int for SAIVERSE_SLUICE_MAX_SPAN_CHARS=%r; "
            "using default 100000", raw,
        )
        return 100_000


# ---------------------------------------------------------------------------
# response_schema (Gemini 制約: additionalProperties 禁止、フラット)
# ---------------------------------------------------------------------------
#
# 型の規律 (2026-08-24、docs/issues/sluice_structured_output_digit_loop.md):
#
# 1. **Gemini に向ける構造化出力の型には数値の欄を置かない。** JSON の数値
#    リテラルは文法で閉じられない (桁をいくら並べても違反にならない) ので、
#    制約付きデコードがその中でループに入ると何も止められない。参照は
#    プロンプトに載せた語の写し (`core:3` / `act:2`) を文字列で受け取り、
#    番号の解決はこちら側で行う。
# 2. **任意の欄を飛ばした先に、飛ばした中身を吐き出せる欄が来る型を作らない。**
#    モデルが書きたいものに対応する必須欄を、その順番で用意する。旧 `ops`
#    (op / content / memory_id、必須は op だけ) は「書き換えなのに本文を
#    飛ばして参照欄へ入る」並びを文法上作れてしまい、参照欄に本文や独り言が
#    流れ込んだ。操作を種類ごとの一覧に分け、必須と順番を文法で縛ることで、
#    その並びが作れなくなる (3 モデル × 10 回で 30/30 正常。旧型は本番 7/7 失敗)。
#
# 欄の並び (dict の挿入順) はそのまま REST の propertyOrdering になる
# (llm_clients/gemini.py の _schema_from_json)。並べ替えると検証した型と
# 別物になるので、順序も含めて実験で通した形のまま維持する。

#: 参照欄の書式。桁数を 9 までに縛るのは、暴走した長大な数字列を int() へ
#: 渡さないため (Python の整数文字列変換上限で例外になり、要素棄却ではなく
#: pan 全体が落ちる)。実物の ID はどちらも小さい。
_CORE_REF_RE = re.compile(r"^core:([0-9]{1,9})$")
_ACTIVITY_REF_RE = re.compile(r"^act:([0-9]{1,9})$")
#: 約束の参照。N は同梱一覧の 1 始まりの位置 (その場の連番 — 永続 ID ではない)。
_PROMISE_REF_RE = re.compile(r"^promise:([0-9]{1,9})$")

#: 参照欄の description (コア記憶。三つの一覧で共用する)。
_CORE_REF_DESCRIPTION = "同梱の「現在のコア記憶」一覧の core:N をそのまま写す (例: core:2)。"


def _parse_ref(raw: Any, pattern: re.Pattern) -> Optional[int]:
    """``core:N`` / ``act:N`` の文字列参照を番号へ解決する。不正は None。

    受け付けるのは**プロンプトに載せた語そのままの写し**だけ (前後の空白は
    落とす)。数字だけ (``"2"``)、後ろに本文が続くもの
    (``"core:2reset core:2 …"``)、文字列ですらないものは None = 要素棄却の
    合図で、呼び出し側がその要素だけ捨てて結果行に残す。
    """
    if not isinstance(raw, str):
        return None
    matched = pattern.match(raw.strip())
    if matched is None:
        return None
    return int(matched.group(1))


_MEMO_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "activity_ref": {
            "type": "string",
            "description": "同梱の「開いているアクティビティ一覧」の act:N をそのまま写す (例: act:1)。一覧に無い活動のときは省略して new_activity_name を使う。",
        },
        "new_activity_name": {
            "type": "string",
            "description": "一覧に無い活動のときだけ。「小説を書く」「絵の練習」のような活動の粒度の名前。具体的な詳細はここではなく text に書く。",
        },
        "text": {
            "type": "string",
            "description": "今日の中身一行 (本人の言葉)。",
        },
    },
    "required": ["text"],
}

_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reflection": {
            "type": "string",
            "description": "採取判断の短い独白。採取なしでも一言。",
        },
        "core_adds": {
            "type": "array",
            "description": "新しく刻むコア記憶。採取なしなら空配列。",
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "新しいコア記憶の本文",
                    },
                },
                "required": ["content"],
            },
        },
        "core_updates": {
            "type": "array",
            "description": (
                "書き換え。memory_ref は同梱の一覧の core:N をそのまま写し、"
                "content は書き換え後の本文 (全文)。採取なしなら空配列。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "memory_ref": {
                        "type": "string",
                        "description": _CORE_REF_DESCRIPTION,
                    },
                    "content": {
                        "type": "string",
                        "description": "書き換え後の本文 (全文)。",
                    },
                },
                "required": ["memory_ref", "content"],
            },
        },
        "core_removes": {
            "type": "array",
            "description": "不要になったコア記憶の削除。採取なしなら空配列。",
            "items": {
                "type": "object",
                "properties": {
                    "memory_ref": {
                        "type": "string",
                        "description": _CORE_REF_DESCRIPTION,
                    },
                },
                "required": ["memory_ref"],
            },
        },
        "want_memos": {
            "type": "array",
            "description": "この範囲でやりたいと思ったこと。無ければ空配列。",
            "items": _MEMO_ITEM_SCHEMA,
        },
        "did_memos": {
            "type": "array",
            "description": "この範囲で実際にやったこと。無ければ空配列。",
            "items": _MEMO_ITEM_SCHEMA,
        },
        # 約束も種類別の二一覧 (2026-09-28、docs/issues/sluice_task_ref_prefix_rejected.md)。
        # 旧 `promises` (op 一本だけ必須) は型の規律 2 の違反形で、変更内容の
        # 無い update・参照欄 (UUID の丸写し) への本文の流れ込み・同じ update の
        # 反復が本番で出た。参照は一覧の位置の写し (promise:N) で受ける —
        # `task:N` は参照文法の正典 (docs/intent/reference_addressing.md) で
        # 目的の木の short_id を指す別の語なので使わない。
        "promise_adds": {
            "type": "array",
            "description": "ユーザーとの新しい約束・依頼のタスク帳への追加。無ければ空配列。",
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "約束の中身。",
                    },
                    "due": {
                        "type": "string",
                        "description": "期限 (YYYY-MM-DD)。期限が明示されていない約束では省略する — 期限を発明しない。",
                    },
                },
                "required": ["content"],
            },
        },
        "promise_updates": {
            "type": "array",
            "description": (
                "既存の約束の変更。"
                "promise_ref は同梱の「手帳の約束の欄」一覧の promise:N をそのまま写す。"
                "無ければ空配列。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "promise_ref": {
                        "type": "string",
                        "description": "同梱の「手帳の約束の欄」一覧の promise:N をそのまま写す (例: promise:2)。",
                    },
                    "content": {
                        "type": "string",
                        "description": "内容が変わったときだけ、変更後の中身 (全文)。期限だけの変更なら省略。",
                    },
                    "due": {
                        "type": "string",
                        "description": "新しい期限 (YYYY-MM-DD)。期限が変わっていなければ省略。",
                    },
                    "clear_due": {
                        "type": "boolean",
                        "description": "期限が撤回されたときだけ true。due と同時には指定しない。",
                    },
                },
                "required": ["promise_ref"],
            },
        },
    },
    # 全欄必須 (Codex 第七巡 修正 1): 「空」は明示的な空配列だけ。欄の省略を
    # 「採取なし」へ丸めると、モデルが欄を出力しなかった回の採取が静かに失われ、
    # ゲート (§13.3) が通ったことにされる。
    "required": [
        "reflection", "core_adds", "core_updates", "core_removes",
        "want_memos", "did_memos", "promise_adds", "promise_updates",
    ],
}

#: 記憶の手入れ (Memopedia ページの分割・統合の提案) への返答の欄。候補を提示
#: した回だけスキーマに足す (:func:`_build_response_schema`)。2026-10-09 に就寝
#: 判断から移設した — 自律 OFF のペルソナにも届けるため
#: (docs/intent/autonomous_behavior_v3.md §6)。提示は業務日に一回だけ
#: (:func:`_offer_page_reviews`)。
_PAGE_REVIEW_FIELD = "page_reviews"
_PAGE_REVIEW_VERDICTS = ("approve", "skip")


def _build_response_schema(page_review_op_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """その回に要求する応答の形。

    候補が無い回は :data:`_RESPONSE_SCHEMA` そのもの (同じオブジェクト) を返す。
    候補がある回だけ、末尾に ``page_reviews`` 欄を足した写しを返し、required にも
    含める — 既存の欄の並び (propertyOrdering) は変えない。``op_id`` は提示した
    候補の enum (空 enum を作らないよう、候補が無ければ欄ごと出さない)。数値の
    欄は置かない (型の規律 1 / v3 §13.6)。
    """
    op_ids = [str(op_id) for op_id in (page_review_op_ids or []) if op_id]
    if not op_ids:
        return _RESPONSE_SCHEMA
    schema = json.loads(json.dumps(_RESPONSE_SCHEMA, ensure_ascii=False))
    schema["properties"][_PAGE_REVIEW_FIELD] = {
        "type": "array",
        "description": (
            "記憶ページの再編の提案への返答。提案一件ごとに op_id をそのまま写し、"
            "verdict に approve (実行する) か skip (見送る) を書く。"
        ),
        "items": {
            "type": "object",
            "properties": {
                "op_id": {
                    "type": "string",
                    "enum": op_ids,
                    "description": "同梱の「記憶ページの再編の提案」の [op_id] をそのまま写す。",
                },
                "verdict": {
                    "type": "string",
                    "enum": list(_PAGE_REVIEW_VERDICTS),
                    "description": "approve=承認 (この後の整理で実行) / skip=見送り",
                },
            },
            "required": ["op_id", "verdict"],
        },
    }
    schema["required"] = [*schema["required"], _PAGE_REVIEW_FIELD]
    return schema


#: スルースの LLM コールの出力上限。実測の応答は 195〜411 トークン
#: (docs/issues/sluice_structured_output_digit_loop.md の再現性実験) なので、
#: 4,096 は正常な応答を切らない。目的は暴走したときの被害の頭打ち — 旧型の
#: 本番失敗は 1 回あたり 79 秒・数万トークンを焼いていた。
_MAX_OUTPUT_TOKENS = 4096


# ---------------------------------------------------------------------------
# 送る中身がモデルに入るかの見積もり
# ---------------------------------------------------------------------------
#
# docs/issues/sluice_skip_ignores_model_context.md: 固定の字数の上限
# (:func:`get_max_span_chars`) は 429 (送信量の制限) を避ける役で、パンマーカー
# から末尾までの字数しか見ていない。スルースが実際に送るのは、そのモデルに
# 普段送っている会話の範囲まるごと (先頭の自己定義や部屋の様子を含む) と
# 指示文なので、モデルを切り替えると「字数の判定は入る、送る中身はモデルに
# 入らない」が起きる。LLM を呼ぶ直前に、組み立て終わった実物を測って比べる。
#
# 換算は文字 1 字 = 1 トークン (:func:`_estimate_input_tokens`)。うるさんの実測
# (GPT-4o で、送った 290,404 字に先頭の自己定義を足して約 311,900 トークン) が
# 1 字 ≒ 1 トークンだった。:func:`saiverse.token_estimator.estimate_messages_tokens`
# (日本語 1 字 = 1.5 トークン) を使わないのは、1.5 で数えると普段の大きさの範囲
# (上限 60,000 字の水位 + 先頭の自己定義) でも、128,000 トークンのモデルで
# スルースが飛ばされうるから。
#
# 画像・音声・動画は、LLM クライアントが実際に送る形で数える (:func:`_media_rules`)。
# クライアントは画像を全部は埋め込まない — 直近の数枚だけを埋め込み、古い画像は
# ``[画像: URI] 要約`` の注記に置き換える (llm_clients/utils.py の
# compute_allowed_attachment_keys と image_summary_note)。埋め込む画像を全部数えると、
# 画像の多いペルソナでは普段の大きさの会話でもスルースが飛ばされ、「上限内の送信
# では従来どおりスルースが走る」が崩れる。逆に注記を数えないと、実物より小さく
# 数えて、判定を通ったスルースがプロバイダに拒まれる。
#
# 見積もりが実物より小さい向きの差は、判定を通ったスルースの失敗 → 畳みの停止
# (本件の症状) に戻る。だから実物より小さく数える系統的な原因 — 応答の枠
# (:func:`_response_token_reserve`)、答えの形の指定 (``response_schema``)、送らない
# メディアの注記 — は見積もりに入れる。境界付近の小さな揺れは 1 割の余白で受ける
# (docs/issues/sluice_skip_ignores_model_context.md「レビューの裁定」第一巡)。

#: メッセージ 1 通ごとに足す分 (役割や区切りの書式)。
_TOKENS_PER_MESSAGE = 4

#: コンテキスト長のうち入力に使ってよい割合。残りの 1 割は見積もりの誤差の枠。
_CONTEXT_USABLE_RATIO = 0.9

#: 後から通す採取の会話以外の部分に足す余白。指示文は「N 通」の件数を文面に
#: 含むので、空のチャンクで組んだ見積もりより件数の桁数ぶん長くなる。
_CAPTURE_COUNT_DIGITS_SLACK = 10

#: 送らないメディア 1 件の注記に載る要約を、保存済みの要約が無いときに数える
#: 字数。保存済みの要約があればその実際の字数で数える (:func:`_media_note_tokens`)
#: — 「300文字以内」は要約を作るときの指示で、保存済みの要約は切り詰めずに
#: 注記に載るので、300 字を超えることがある (レビュー第二巡の 2)。保存済みの
#: 要約が無ければクライアントはその場で作る (saiverse/media_summary.py が画像・
#: 音声・動画とも「300文字以内」と指示して作る)。見積もりでは要約を生成しない
#: ので、その指示の上限で数える。
_UNSAVED_MEDIA_SUMMARY_CHARS = 300

#: 注記の書式の固定部分の字数。いちばん長いのは gemini.py の画像の注記
#: ``[画像参照のみ: `` (9 字) + ``] `` (2 字) で、本文との区切りの改行 (1 字) を
#: 足すと 12 字。OpenAI 互換の画像対応モデルは注記を別の部品として足すので、
#: 部品の区切りの分を見て 16 字にする。要約が取れないときの代わりの文言
#: (``(要約を取得できませんでした)``、15 字) は 300 字に収まる。
_MEDIA_NOTE_FORMAT_CHARS = 16


class SluiceInputTooLargeError(RuntimeError):
    """送る中身の見積もりが、使うモデルに一度に送れる量を超えている。

    LLM を呼ぶ前に送出する (呼べばプロバイダがコンテキスト長の超過で拒む)。
    定常のスルースでは run_metabolism がこれを量による飛ばし (``skipped_cold``)
    と同じ扱いにし、後から通す採取ではジョブの失敗 (``input_too_large``) になる。
    ``response_reserve_tokens`` は上限を出すときにコンテキスト長から引いた応答の
    枠 (:func:`_response_token_reserve`)。
    """

    def __init__(
        self,
        model: str,
        estimated_tokens: int,
        limit_tokens: int,
        response_reserve_tokens: int = _MAX_OUTPUT_TOKENS,
    ) -> None:
        self.model = model
        self.estimated_tokens = estimated_tokens
        self.limit_tokens = limit_tokens
        self.response_reserve_tokens = response_reserve_tokens
        super().__init__(
            f"sluice input does not fit the model context (model={model}, "
            f"estimated={estimated_tokens} tokens > limit={limit_tokens} tokens, "
            f"response_reserve={response_reserve_tokens} tokens)"
        )


#: メッセージの役割の分類 (:func:`_role_class`)。クライアントのメディアの扱いは
#: system / user / それ以外 の三つで分かれる。
_ROLE_SYSTEM = "system"
_ROLE_USER = "user"
_ROLE_OTHER = "other"
_ALL_ROLES: FrozenSet[str] = frozenset({_ROLE_SYSTEM, _ROLE_USER, _ROLE_OTHER})
_NON_SYSTEM_ROLES: FrozenSet[str] = frozenset({_ROLE_USER, _ROLE_OTHER})
_USER_ROLE_ONLY: FrozenSet[str] = frozenset({_ROLE_USER})
_NO_ROLES: FrozenSet[str] = frozenset()


class _MediaRules(NamedTuple):
    """そのモデルの LLM クライアントが、``metadata`` のメディアをどう送るか。

    役割は :func:`_role_class` の三分類で持つ。画像 1 枚の行き先は次の順で決まる:
    枠の内側で、埋め込む役割なら埋め込み (画像 1 枚の見積もり)。そうでなく、注記に
    する役割なら注記 (:func:`_media_note_tokens`)。どちらでもなければ送られない。
    """

    #: 直近の画像を何枚まで埋め込むか (None = 無制限)。枠は新しいメッセージから
    #: 順に埋まる。``__visual_context__`` のメッセージの画像は枠を使わず、常に
    #: 枠の内側 (llm_clients/utils.py の compute_allowed_attachment_keys と、同じ
    #: 規則を持つ gemini.py の _convert_messages)。
    image_limit: Optional[int]
    #: 画像が枠を使う役割。
    image_limit_roles: FrozenSet[str]
    #: 枠の内側の画像を埋め込む役割。
    image_embed_roles: FrozenSet[str]
    #: 埋め込まない画像を注記に置き換える役割。
    image_note_roles: FrozenSet[str]
    #: 音声を注記に置き換える役割 (空 = 注記にしない)。
    audio_note_roles: FrozenSet[str]
    #: 動画を注記に置き換える役割 (空 = 注記にしない)。
    video_note_roles: FrozenSet[str]


def _role_class(role: Any) -> str:
    """メッセージの役割を system / user / other に分ける。

    ``host`` (OpenAI 互換・Anthropic・xAI は system として扱い、Gemini は model
    として扱う) と ``tool`` は other にする。どのクライアントでも、other として
    数えた結果は実物と同じか、実物より多い — 表 (:func:`_media_rules`) で other の
    画像を送らない xAI は host と tool の画像も送らず、other の画像を送る
    クライアントでは、host と tool の画像は同じ扱いか、送られない。
    """
    if role == "system":
        return _ROLE_SYSTEM
    if role == "user":
        return _ROLE_USER
    return _ROLE_OTHER


def _media_rules(model: str) -> _MediaRules:
    """そのモデルの LLM クライアントのメディアの扱い (:class:`_MediaRules`)。

    クライアントは llm_clients/factory.py の get_llm_client が、モデル設定の
    ``protocol`` (無ければ ``provider`` からの読み替え) で選ぶ。その選び方と、
    画像に対応するかの判定 (設定の ``supports_images``、無ければ provider が
    gemini のときだけ対応) は factory の関数をそのまま使う — 見積もりが
    クライアントと別の規則を持たないため。

    枚数の上限は ``SAIVERSE_<名前>_ATTACHMENT_LIMIT`` →
    ``SAIVERSE_ATTACHMENT_LIMIT`` → 既定 4 (llm_clients/utils.py の
    parse_attachment_limit)。各クライアントのコードで確かめた対応
    (2026-09-17。「注記」は ``[画像: URI] 要約`` などの文字に置き換えること、
    「送らない」は画像も注記も載らないこと):

    - ``openai_compat`` (OpenAIClient → openai_message_preparer.py): 上限は
      OPENAI。factory が設定の ``max_image_embeds`` を正の整数のときだけ渡し、
      それが環境変数より優先される。全役割の画像が枠を使う。埋め込むのは画像
      対応のモデルの user の画像だけで、それ以外 (枠の外・user 以外・画像非対応
      のモデル) は全役割で注記。
    - ``openai_codex`` (OpenAICodexClient → openai_message_preparer.py): 上限は
      OPENAI (factory が ``max_image_embeds`` を渡さないので設定は効かない)。
      役割の扱いは openai_compat と同じ。
    - ``nvidia_nim`` (NvidiaNIMClient → openai_message_preparer.py): 上限と
      役割の扱いは openai_compat と同じ (factory が設定の ``max_image_embeds``
      を正の整数のときだけ渡し、それが環境変数より優先される)。構造化出力の
      生 HTTP の経路も、会話と同じ組み立て (OpenAIClient._prepare_messages)
      を通る。
    - ``anthropic_native`` (AnthropicClient → anthropic_request_builder.py):
      上限は ANTHROPIC。設定の ``max_image_embeds`` が整数ならそのまま使う (0 なら
      埋め込まない)。system のメッセージは先に抜かれるので、その画像は枠を使わず
      送らない。埋め込むのは画像対応のモデルの user の画像だけで、それ以外の
      system 以外は注記。
    - ``gemini_native`` (gemini.py の _convert_messages): 上限は GEMINI (設定の
      ``max_image_embeds`` は読まない)。画像非対応のモデルは画像を集めないので、
      画像も注記も送らない。画像対応のモデルでは全役割の画像が枠を使い、system
      の画像は送らず、それ以外は役割によらず枠の内側を埋め込み、枠の外を注記。
      音声・動画は設定の ``supports_audio`` / ``supports_video`` が真なら埋め込み
      (その量はここでは数えない)、偽なら system 以外で注記。
    - ``xai_native`` (xai.py の _build_xai_messages): 上限は XAI (設定の
      ``max_image_embeds`` は読まない)。全役割の画像が枠を使う。画像対応のモデル
      の user の画像だけを、枠の内側なら埋め込み、枠の外なら注記。user 以外の
      画像と、画像非対応のモデルの画像は送らない。
    - ``ollama_compat`` (OllamaClient): 画像は送らない (メタデータの画像を読む
      コードが無い)。
    - それ以外の protocol は factory がクライアントを作れない (ValueError)。
      送られない組み合わせなので、ここでは openai_compat と同じ扱いにしておく。

    音声・動画は、Gemini 以外のクライアントでは基底の
    ``LLMClient._inject_unsupported_media_summaries`` が全役割で注記にする
    (Gemini 以外は音声・動画に対応しない)。
    """
    from llm_clients.factory import _resolve_protocol, _supports_images
    from llm_clients.utils import parse_attachment_limit
    from saiverse.model_configs import get_model_config

    config = get_model_config(model)
    # factory の呼び出し元 (persona/core.py・saiverse/persona_model_selection.py)
    # が渡す provider と同じ読み方 (saiverse.model_configs.get_model_provider の既定)。
    provider = str(config.get("provider", "ollama"))
    protocol = _resolve_protocol(provider, config)
    supports_images = _supports_images(provider, config)
    configured = config.get("max_image_embeds")

    if protocol == "anthropic_native":
        return _MediaRules(
            image_limit=(
                max(configured, 0) if isinstance(configured, int)
                else parse_attachment_limit("ANTHROPIC")
            ),
            image_limit_roles=_NON_SYSTEM_ROLES,
            image_embed_roles=_USER_ROLE_ONLY if supports_images else _NO_ROLES,
            image_note_roles=_NON_SYSTEM_ROLES,
            audio_note_roles=_ALL_ROLES,
            video_note_roles=_ALL_ROLES,
        )
    if protocol == "gemini_native":
        image_roles = _NON_SYSTEM_ROLES if supports_images else _NO_ROLES
        return _MediaRules(
            image_limit=parse_attachment_limit("GEMINI"),
            image_limit_roles=_ALL_ROLES,
            image_embed_roles=image_roles,
            image_note_roles=image_roles,
            audio_note_roles=(
                _NO_ROLES if config.get("supports_audio") else _NON_SYSTEM_ROLES
            ),
            video_note_roles=(
                _NO_ROLES if config.get("supports_video") else _NON_SYSTEM_ROLES
            ),
        )
    if protocol == "xai_native":
        image_roles = _USER_ROLE_ONLY if supports_images else _NO_ROLES
        return _MediaRules(
            image_limit=parse_attachment_limit("XAI"),
            image_limit_roles=_ALL_ROLES,
            image_embed_roles=image_roles,
            image_note_roles=image_roles,
            audio_note_roles=_ALL_ROLES,
            video_note_roles=_ALL_ROLES,
        )
    if protocol == "ollama_compat":
        return _MediaRules(
            image_limit=0,
            image_limit_roles=_ALL_ROLES,
            image_embed_roles=_NO_ROLES,
            image_note_roles=_NO_ROLES,
            audio_note_roles=_ALL_ROLES,
            video_note_roles=_ALL_ROLES,
        )
    return _MediaRules(
        image_limit=(
            configured
            if protocol in ("openai_compat", "nvidia_nim")
            and isinstance(configured, int) and configured > 0
            else parse_attachment_limit("OPENAI")
        ),
        image_limit_roles=_ALL_ROLES,
        image_embed_roles=_USER_ROLE_ONLY if supports_images else _NO_ROLES,
        image_note_roles=_ALL_ROLES,
        audio_note_roles=_ALL_ROLES,
        video_note_roles=_ALL_ROLES,
    )


def _metadata_media_items(metadata: Any, kind: str) -> List[Dict[str, Any]]:
    """メッセージの ``metadata`` に付いたメディアのうち、クライアントが扱う種類
    ``kind`` (``image`` / ``audio`` / ``video``) の項目。

    クライアントがメディアを集める関数 (saiverse/media_utils.py の
    iter_image_media / iter_audio_media / iter_video_media) をそのまま使う。
    返る項目は ``uri`` (注記に載る URI — 元の ``uri``、無ければファイルの場所)・
    ``path`` (ファイルの場所)・``mime_type`` を持つ。

    これらの関数はファイルが実在しない項目を落とす。どのクライアントも同じ
    関数の結果から、埋め込みの枠を数え、埋め込みか注記を作る (openai_message_preparer.py
    の scan_message_metadata、anthropic_request_builder.py の
    _collect_attachment_state、gemini.py の _convert_messages、xai.py の
    _build_xai_messages、基底の LLMClient._inject_unsupported_media_summaries)
    ので、落ちた項目は実物でも埋め込みにも注記にもならず、枠も使わない —
    見積もりと実物で一致する。副作用は、``saiverse://`` の URI の置き場のフォルダ
    を無ければ作ること (resolve_media_uri) で、クライアントが直後に同じことをする。
    """
    from saiverse.media_utils import (
        iter_audio_media,
        iter_image_media,
        iter_video_media,
    )

    if kind == "image":
        return iter_image_media(metadata)
    if kind == "audio":
        return iter_audio_media(metadata)
    return iter_video_media(metadata)


def _media_note_tokens(item: Dict[str, Any]) -> int:
    """送らないメディア 1 件の代わりにクライアントが足す注記の見積もり。

    ``item`` は :func:`_metadata_media_items` の項目。注記は ``[画像: URI] 要約``
    の形 (llm_clients/utils.py の image_summary_note・audio_summary_note・
    video_summary_note と、gemini.py の同じ形の注記)。
    見積もり = 書式の固定部分 (:data:`_MEDIA_NOTE_FORMAT_CHARS`) + URI の字数 +
    要約の字数。URI はクライアントと同じく項目の ``uri``。要約は、クライアントが
    読むのと同じ保存済みの要約 (saiverse/media_utils.py の get_media_summary —
    ensure_*_summary が最初に読むもの) があればその字数、無ければ
    :data:`_UNSAVED_MEDIA_SUMMARY_CHARS`。要約は生成しない。
    """
    from saiverse.media_utils import get_media_summary

    uri = item.get("uri") or item.get("path") or ""
    summary_chars = _UNSAVED_MEDIA_SUMMARY_CHARS
    path = item.get("path")
    if path is not None:
        try:
            summary = get_media_summary(Path(path))
        except (OSError, ValueError):
            summary = None
        if summary:
            summary_chars = len(summary)
    return _MEDIA_NOTE_FORMAT_CHARS + len(str(uri)) + summary_chars


def _response_schema_tokens(response_schema: Optional[Dict[str, Any]]) -> int:
    """答えの形の指定 (``response_schema``) が入力として消費する量の見積もり。

    指定はどのクライアントでも入力として送られる (OpenAI 互換の json_object
    モードは指定の全文を system メッセージとして足し、Anthropic の思考なしは
    ツールの定義として、ほかは構造化出力の指定として送る)。
    ``json.dumps(指定, ensure_ascii=False)`` の字数を 1 字 1 トークンで数える —
    キーや記号の英字は実際には 1 字あたり 1 トークンより少ないので、その差が
    json_object モードの字下げと前置きの文の分を上回り、多めの側に倒れる。
    """
    if response_schema is None:
        return 0
    return len(json.dumps(response_schema, ensure_ascii=False))


def _estimate_input_tokens(
    messages: List[Dict[str, Any]],
    model: str,
    *,
    response_schema: Optional[Dict[str, Any]] = None,
) -> int:
    """モデル ``model`` へ送るメッセージ列 (と答えの形の指定) の入力トークンの見積もり。

    文字 1 字 = 1 トークン (換算の根拠はこの節の冒頭のコメント)。
    文字は ``content`` が文字列ならその長さ、部品の列ならテキスト部品の長さ。
    メッセージ 1 通ごとに 4 トークンを足す。``response_schema`` を渡すと、その
    字数も足す (:func:`_response_schema_tokens`)。

    ``metadata`` のメディアは、クライアントが実際に送る形で数える
    (:func:`_media_rules`)。項目はクライアントと同じ関数で集める
    (:func:`_metadata_media_items` — ファイルが実在しない項目は実物と同じく
    枠も使わず、数えない)。

    - 画像: 埋め込む画像は 1 枚を :func:`saiverse.token_estimator.estimate_image_tokens`
      (モデルの provider ごとの値) で数える。埋め込む枚数は、直近の上限枚数
      (枠は新しいメッセージから順に埋まる) と、枠を使わない ``__visual_context__``
      の画像。埋め込まない画像 (枠の外・画像非対応のモデル・クライアントによっては
      user 以外のメッセージ) は、クライアントが注記に置き換えるなら注記 1 件
      (:func:`_media_note_tokens`) で数え、送らないなら数えない。
    - 音声・動画: 注記に置き換えるクライアントでは 1 件ごとに注記で数える。
      埋め込むクライアント (Gemini で設定が対応のとき) の音声・動画の量は数えない。
    - ``content`` の部品の列にすでに入っている画像部品: 全部数える。これは
      組み立て済みの部品で、OpenAI 互換のクライアントは上限にも画像対応の設定
      にも関係なくそのまま送る (openai_message_preparer.py は ``metadata`` に
      画像の無いメッセージの ``content`` を触らない)。保存された会話から組む
      メッセージはこの形を作らないので、多めの側に倒しておく。
    """
    from saiverse.model_configs import get_model_config
    from saiverse.token_estimator import estimate_image_tokens

    model = str(model)
    image_tokens = estimate_image_tokens(
        str(get_model_config(model).get("provider", "ollama")),
    )
    rules = _media_rules(model)
    remaining_slots = rules.image_limit  # 枠の残り (None = 無制限)
    # 画像を埋め込みにも注記にもしないクライアント (Ollama・画像非対応の Gemini と
    # xAI) では、画像の項目を集めない — 枠がどう埋まっても数える量は 0。
    counts_images = bool(rules.image_embed_roles or rules.image_note_roles)
    total = _response_schema_tokens(response_schema)
    # 枠は新しいメッセージから順に埋まるので、末尾から数える。
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        total += _TOKENS_PER_MESSAGE
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    part_type = part.get("type")
                    if part_type == "text":
                        total += len(str(part.get("text") or ""))
                    elif part_type in ("image_url", "image"):
                        total += image_tokens
                elif isinstance(part, str):
                    total += len(part)
        metadata = msg.get("metadata")
        if not isinstance(metadata, dict):
            continue
        role = _role_class(msg.get("role"))
        uses_slots = (
            role in rules.image_limit_roles
            and not metadata.get("__visual_context__")
        )
        images = _metadata_media_items(metadata, "image") if counts_images else []
        for item in images:
            within_limit = True
            if uses_slots and remaining_slots is not None:
                within_limit = remaining_slots > 0
                remaining_slots = max(remaining_slots - 1, 0)
            if within_limit and role in rules.image_embed_roles:
                total += image_tokens
            elif role in rules.image_note_roles:
                total += _media_note_tokens(item)
        if role in rules.audio_note_roles:
            for item in _metadata_media_items(metadata, "audio"):
                total += _media_note_tokens(item)
        if role in rules.video_note_roles:
            for item in _metadata_media_items(metadata, "video"):
                total += _media_note_tokens(item)
    return total


def _response_token_reserve(llm_client: Any = None) -> int:
    """上限を出すときにコンテキスト長から引く応答の枠 (トークン)。

    = スルースの出力の上限 (:data:`_MAX_OUTPUT_TOKENS`) と、実際に使うクライアントが
    リクエストに載せる応答の上限 (``LLMClient.response_token_limit``) の大きい方。
    プロバイダは入力と応答の上限の合計をコンテキスト長と比べ、per-call の
    ``max_output_tokens`` を守らないクライアント (Anthropic・OpenAI 互換など) は
    自分の持つ上限を送るので、4,096 を引くだけでは実物より入る量を多く見積もる。
    クライアントが無い (None) か、答えを持たない (上限を送らない・per-call を守る)
    ときは :data:`_MAX_OUTPUT_TOKENS`。
    """
    limit_of = getattr(llm_client, "response_token_limit", None)
    client_limit = limit_of() if callable(limit_of) else None
    if (
        isinstance(client_limit, int) and not isinstance(client_limit, bool)
        and client_limit > _MAX_OUTPUT_TOKENS
    ):
        return client_limit
    return _MAX_OUTPUT_TOKENS


def _input_token_budget(
    model: Optional[str],
    *,
    persona_id: Optional[str] = None,
    llm_client: Any = None,
) -> Optional[int]:
    """そのモデルに一度に送ってよい入力の上限 (トークン)。

    上限 = コンテキスト長の 9 割 − 応答の枠 (:func:`_response_token_reserve` —
    ``llm_client`` は LLM を呼ぶのに使うクライアント)。モデルの設定が見つからず
    コンテキスト長が引けないときは None — 判定をせずに従来どおり走らせ、
    WARNING を残す。
    """
    from saiverse.model_configs import get_context_length

    try:
        context_length = get_context_length(str(model))
    except (ValueError, TypeError):
        LOGGER.warning(
            "[sluice] model config for %r is unavailable; the input size check "
            "against the model context is skipped (persona=%s)",
            model, persona_id,
        )
        return None
    return (
        int(context_length * _CONTEXT_USABLE_RATIO)
        - _response_token_reserve(llm_client)
    )


def _ensure_input_fits(
    messages: List[Dict[str, Any]],
    model: Optional[str],
    *,
    persona_id: Optional[str] = None,
    llm_client: Any = None,
    response_schema: Optional[Dict[str, Any]] = None,
) -> None:
    """送る中身の見積もりが上限を超えていたら :class:`SluiceInputTooLargeError`。

    LLM を呼ぶ直前 (実際に使うモデルとクライアントが決まった後) に呼び、
    ``llm_client`` にそのクライアント、``response_schema`` に generate へ渡す
    答えの形の指定を渡す。モデル設定が引けないときは判定しない
    (:func:`_input_token_budget`)。
    """
    limit = _input_token_budget(model, persona_id=persona_id, llm_client=llm_client)
    if limit is None:
        return
    estimated = _estimate_input_tokens(
        messages, str(model), response_schema=response_schema,
    )
    if estimated > limit:
        raise SluiceInputTooLargeError(
            str(model), estimated, limit,
            response_reserve_tokens=_response_token_reserve(llm_client),
        )


# ---------------------------------------------------------------------------
# 注入プロンプト
# ---------------------------------------------------------------------------

_SCENE_PREVIEW_CHARS = 80


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _is_scene_memory(memory: Any) -> bool:
    """その項目が場面の記憶 (実会話の写し) か。

    scene は本人が話した会話をそのまま写したもので、口調のアンカーとして使う
    (このモジュール冒頭の不変条件「scene は参照コピーのみ」)。本人が「直す」
    ことは会話の写しを書き換えること — つまり捏造にあたるので、**長さに関係
    なく** update の対象外にする。remove は対象外にしない (写しを消すことは
    改変ではない)。

    種別の literal は :data:`sai_memory.core_memory.SCENE_KIND` 一箇所が持つ。
    書き込みの最後の保証も同モジュール (`update_core_memory` が既定で拒む) が
    持っていて、ここはその手前で本人向けの説明を返すための判定。

    出自: docs/issues/archive/sluice_truncated_scene_update.md (2026-08-22 裁定
    — 歯止めの条件を「切り詰めて見せたか」から「場面の記憶そのものか」へ移した。
    長さで書くと 80 字以下の scene だけ書き換えられる穴が残る)。
    """
    from sai_memory.core_memory import SCENE_KIND

    return memory is not None and getattr(memory, "kind", None) == SCENE_KIND


def _is_presented_truncated(memory: Any) -> bool:
    """その項目を、プロンプトへ**先頭だけ**載せるか (提示側の切り詰め規則)。

    :func:`_build_sluice_prompt` が「こういう場面がある」と分かる長さへ刻む
    ための判定で、役目は提示だけ。適用側の歯止めは長さではなく種類で決める
    (:func:`_is_scene_memory`) ので、この関数は歯止めには使わない。
    """
    if not _is_scene_memory(memory):
        return False
    body = (getattr(memory, "content", None) or "").replace("\n", " ")
    return len(body) > _SCENE_PREVIEW_CHARS


def _list_open_activities(persona: Any) -> List[Tuple[int, str]]:
    """開いているアクティビティの (id, name) 一覧。

    fail-closed (Codex 第八巡 修正 3): 読み出しの例外は空一覧へ丸めず送出する。
    タスク一覧 (:func:`_list_open_tasks`) とコア記憶 (:func:`_read_core_state`) と
    同じ規律 — 空へ丸めると「開いている活動が一つも無い」とペルソナに見せた
    まま LLM が new_activity_name を書き、既存の活動と重複する新 activity と
    その配下のメモが生まれる (閉語彙の土台が黙って崩れる)。「正常に空」
    (例外なしの空リスト) と「取得不能」(送出) を区別する。
    """
    from sai_memory.memory.pocketbook import list_activities

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or getattr(adapter, "conn", None) is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot list open activities"
        )
    with adapter._db_lock:
        activities = list_activities(adapter.conn)
    return [(a.id, a.name) for a in activities]


#: メモ種類 (memos.kind) の表示名。プロンプトと適用結果の記録で共用する。
_MEMO_KIND_LABEL = {"want": "やりたい", "did": "やった"}


def _list_today_memos(
    persona: Any, activities: List[Tuple[int, str]]
) -> List[Tuple[str, str, str]]:
    """今日の日付のメモを (種類, アクティビティ名, 本文) で横断に取る。

    目的は重複の抑え: 本人が昼に手帳のスペル (``pocketbook_write``) で書いた
    「やりたい」を、夜のスルースが文言違いでまた採る並びを減らす。載せるのは
    **今日の分だけ**なので数行で収まり、プロンプト肥大の管理 (§13.5-6) を
    崩さない。機械側の重複防止 (:func:`find_memo_by_content`) は同じ本文しか
    止められないので、供給源をここで塞ぐ。

    アクティビティ一覧 (:func:`_list_open_activities`) の読みを引数で受け、
    その配下だけを見る (:func:`_read_core_state` と同じく、プロンプトに見せる
    姿と同じ読みから組む)。読み出しの例外は空一覧へ丸めず送出する
    (fail-closed) — 空へ丸めると「今日はまだ何も書いていない」と本人へ見せた
    まま、既に書いたものを再び採らせる。
    """
    from sai_memory.memory.pocketbook import list_memos
    from saiverse import clock

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or getattr(adapter, "conn", None) is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot list today's memos"
        )
    today = clock.now().date().isoformat()
    rows: List[Tuple[str, str, str]] = []
    with adapter._db_lock:
        for activity_id, name in activities:
            for memo in list_memos(adapter.conn, activity_id):
                if memo.date == today:
                    rows.append((memo.kind, name, memo.text))
    return rows


def _list_open_tasks(lifecycle: Any, persona: Any) -> List[Dict[str, Any]]:
    """open なタスク帳の一件一覧 (task_id / content / due_at / revision)。

    アクティビティ一覧と同じ理由 (閉語彙・再提案防止) で同梱する: 一覧が無いと
    promise_updates の promise_ref が書けないだけでなく、会話で言及され続けて
    いる同じ約束を次のスルースが再び add して重複する。

    fail-closed (Codex 第四巡 修正 2): 読み出しの例外は空一覧へ丸めず送出する —
    空へ丸めると、その回のスルースは既存の約束を知らずに再 add し (重複)、
    update の口も失う。「正常に空」(例外なしの空リスト) と「取得不能」(送出) を
    区別する。manager 未構成 (タスク帳の器そのものが無い環境 — テストハーネス
    など) だけは設計上の「タスク帳なし」として空を返す。
    """
    manager = getattr(lifecycle, "manager", None)
    persona_id = getattr(persona, "persona_id", None)
    if manager is None or not hasattr(manager, "SessionLocal") or not persona_id:
        return []
    from saiverse.task_book import list_open
    # task_id の無い行はここで落とす — プロンプトの番号振り
    # (_build_sluice_prompt) と対応表 (_offered_task_map) が同じ列を見る
    # ことを、供給源の一箇所で保証する。片側だけで絞ると「見せた番号が
    # 解決できない」非対称が生まれる (TASK_ID は主キーなので実際には
    # 欠けないが、防御は対称でなければ防御にならない)。
    return [t for t in list_open(manager, persona_id) if t.get("task_id")]


def _format_task_line(position: int, task: Dict[str, Any]) -> str:
    """タスク一件を ``[promise:N] 中身 (期限: …)`` の一行に整形する。

    N は同梱一覧の 1 始まりの位置 (その場の連番)。UUID を見せない — 長い ID の
    丸写しは参照欄へ本文が流れ込む崩れの温床だった
    (docs/issues/sluice_task_ref_prefix_rejected.md)。N → task_id の対応は
    :func:`_offered_task_map` が同じ並びから作り、台帳に凍結する。

    content は切り詰めない — ペルソナ名義のテキストではなく指示書 (確定情報)
    であり、機械的な省略は情報の改変になる。プロンプト肥大が実測で問題に
    なったら、それは同梱量の管理 (autonomous_behavior_v3.md §13.5-6) の管轄。
    """
    due_at = task.get("due_at")
    if due_at is not None:
        try:
            due_label = f"期限: {datetime.fromtimestamp(due_at).strftime('%Y-%m-%d')}"
        except (OverflowError, OSError, ValueError):
            due_label = "期限: 不明"
    else:
        due_label = "期限なし"
    return f"- [promise:{position}] {task.get('content')} ({due_label})"


def _offered_task_map(open_tasks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """同梱一覧の位置 N (文字列) → そのタスクのスナップショットの対応表。

    値は ``{"task_id", "revision", "due_at", "content"}``。``revision`` は CAS
    の照合値 (REVISION 列は NOT NULL なので常に int のはず。None は適用側が
    破損として棄却する — update_entry の None は「省略 = 読み直し CAS」で、
    スナップショット CAS にならないため)、
    ``due_at`` はプロンプトに見せた時点の期限 (「期限の無い約束への
    clear_due」を空振りとして扱う判定に使う — CAS が通る限りスナップショットの
    値が現在値)、``content`` は判断ターン記録でどの約束かを言うための本文
    (``promise:N`` はその場限りの番号なので、後から読む本人には意味を運ばない)。
    番号は :func:`_build_sluice_prompt` と同じく ``open_tasks`` の並びの
    1 始まり — 同じリストから両方を作るので、見せた番号と引く先がずれない。
    鍵を文字列にするのは台帳 (JSON) に凍結しても同じ形で戻すため。
    """
    return {
        str(position): {
            "task_id": str(task.get("task_id")),
            "revision": task.get("revision"),
            "due_at": task.get("due_at"),
            "content": task.get("content"),
        }
        for position, task in enumerate(open_tasks, start=1)
        if task.get("task_id")
    }


def _read_core_state(persona: Any) -> tuple[List[Any], int]:
    """コア記憶の現況 (全項目, 合計字数) を一度の読みで取る。

    プロンプト同梱と CAS スナップショット (Codex 第七巡 修正 2) の両方が
    **同じ読み**を使う — 別々に読むと「LLM が見た姿」と「照合の基準」がずれる。
    読み出しの例外は送出する (fail-closed — タスク一覧と同じ規律)。
    """
    from sai_memory.core_memory import list_core_memories, total_core_memory_chars

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or getattr(adapter, "conn", None) is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot read core memories"
        )
    with adapter._db_lock:
        memories = list_core_memories(adapter.conn)
        total_chars = total_core_memory_chars(adapter.conn)
    return list(memories), total_chars


def _core_content_hash(content: Optional[str]) -> str:
    """コア記憶本文の変更検知ハッシュ (CAS スナップショットの照合値)。

    updated_at (秒粒度 — 同一秒内の連続編集を見分けられない) ではなく本文の
    ハッシュを使う: 変更の検知が決定的で、storage のスキーマに依存しない。
    """
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _scope_sentence(span_new_count: Optional[int]) -> str:
    """「今回どこが対象か」を本人へ伝える一行を組む。

    プロンプトは手帳の節で「この範囲で」と言うのに、その範囲がどこなのかを
    一言も書いていなかった。退場が次回へ繰り越された回 (``unseen_tail``) は
    採取済みの会話が窓に残ったまま再び目に入るので、どこまで採取済みかを
    知らされていない本人は同じメモをもう一度返す (重複の供給源)。

    件数は機械が知る値だけで組む — 本人の申告は使わない (§13.6)。
    ``span_new_count`` が None のときはマーカーが窓に無い (初回 / 押し出されて
    消えた) ので、窓全体が対象。

    出自: docs/issues/sluice_memo_duplicate_across_spans.md (2026-08-22 裁定 —
    機械側の重複防止と対で、供給源をここで塞ぐ)。
    """
    if span_new_count is None:
        return "今回は手元の会話全体が対象です。"
    if span_new_count <= 0:
        return "前回の整理以降、新しいやり取りはありません。"
    return (
        f"今回の対象は直近 {span_new_count} 通の会話です。"
        "それより前は前回の整理で採取済みです。"
    )


def _build_sluice_prompt(
    persona: Any,
    activities: List[Tuple[int, str]],
    open_tasks: List[Dict[str, Any]],
    core_memories: List[Any],
    total_chars: int,
    *,
    span_new_count: Optional[int],
    today_memos: Optional[List[Tuple[str, str, str]]] = None,
    scope_sentence: Optional[str] = None,
    page_review_candidates: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """<system> 包みの注入プロンプトを組む (keepalive の末尾通知と同じ面)。

    ``page_review_candidates`` は記憶の手入れの候補 (:func:`_offer_page_reviews`
    の返り値)。あれば「記憶ページの再編の提案」の節を足し、応答の欄の一覧にも
    page_reviews を含める。無ければ節ごと出さない (定常の回の文面は変わらない)。

    ``core_memories`` / ``total_chars`` は :func:`_read_core_state` の読みを
    呼び出し元から受け取る (CAS スナップショットと同じ姿を見せるため)。
    ``span_new_count`` は今回の担当範囲の通数 (None = 窓全体) — 手帳の節で
    対象範囲を明示するのに使う (:func:`_scope_sentence`)。``today_memos`` は
    :func:`_list_today_memos` の読み (今日すでに書いたメモ) — 無ければその節を
    出さない。``scope_sentence`` は対象範囲の一行の差し替え (後から通す採取が
    「直近の会話」ではなく「読み返した過去の会話」を対象と明示するために使う)。
    None なら従来どおり :func:`_scope_sentence` で組む。
    """
    from builtin_data.tools._core_memory_common import resolve_core_memory_budget

    persona_id = getattr(persona, "persona_id", None)

    # 現在のコア記憶全項目を [c:id] content 形式で列挙する。
    core_lines: List[str] = []
    for mem in core_memories:
        body = mem.content or ""
        if _is_presented_truncated(mem):
            # 切り詰めの規則は _is_presented_truncated が持つ (適用側の歯止めと
            # 同じ判定を使う — 別々に書くと片方だけ変わって歯止めが外れる)。
            body = _truncate(body.replace("\n", " "), _SCENE_PREVIEW_CHARS)
        core_lines.append(f"- [core:{mem.id}] {body}")

    budget = resolve_core_memory_budget(persona_id) if persona_id else 2000

    core_block = "\n".join(core_lines) if core_lines else "（まだコア記憶はありません）"

    if activities:
        activity_block = "\n".join(f"- [act:{aid}] {name}" for aid, name in activities)
    else:
        activity_block = "（まだアクティビティはありません）"

    if open_tasks:
        # 番号は _offered_task_map と同じ並びの 1 始まり (見せた番号 = 引く先)。
        task_block = "\n".join(
            _format_task_line(position, t)
            for position, t in enumerate(open_tasks, start=1)
        )
    else:
        task_block = "（開いている約束はありません）"

    # 今日すでに手帳に書いたもの (本人がスペルで書いた分を含む)。無ければ節ごと
    # 出さない — 空の見出しは「今日は何も書いていない」の主張になる。
    if today_memos:
        today_block = "- 今日すでに手帳に書いたもの:\n" + "\n".join(
            f"  - [{_MEMO_KIND_LABEL.get(kind, kind)}] {name}: {text}"
            for kind, name, text in today_memos
        ) + "\n"
    else:
        today_block = ""

    # 記憶の手入れの提案 (業務日に一回だけ載る)。無い回は節も欄名も出さない。
    if page_review_candidates:
        page_review_block = (
            "4) 記憶ページの再編の提案 (page_reviews):\n"
            "- 記憶ページについて、次の分割・統合を提案します。承認したものだけ、\n"
            "  この後の整理で実行されます。迷うものは skip してかまいません。\n"
            + "\n".join(
                f"  - [{c['op_id']}] {c.get('line') or c['op_id']}"
                for c in page_review_candidates
            )
            + "\n"
            "- 返答は一件ごとに、op_id に [ ] の中身をそのまま写し、verdict に\n"
            "  approve か skip を書きます。\n"
            "\n"
        )
        fields_sentence = (
            "- 応答には全ての欄 (reflection / core_adds / core_updates / core_removes /\n"
            "  want_memos / did_memos / promise_adds / promise_updates / page_reviews)\n"
            "  を含めてください。採るものが無い欄は空配列で。\n"
        )
    else:
        page_review_block = ""
        fields_sentence = (
            "- 応答には全ての欄 (reflection / core_adds / core_updates / core_removes /\n"
            "  want_memos / did_memos / promise_adds / promise_updates) を含めてください。\n"
            "  採るものが無い欄は空配列で。\n"
        )

    prompt = (
        "<system>\n"
        "## 記憶整理の節目 — スルース\n"
        "まもなく古い会話が記憶整理で押し出されます。この機会に、残しておきたい\n"
        "ことがあれば、いま記録できます。押し出された後ではこの会話は手元から\n"
        "消えるため、採るならこのタイミングだけです。\n"
        "\n"
        "1) コア記憶 (core_adds / core_updates / core_removes):\n"
        "- 状態の変化（生活・仕事・健康・関係性など、いま進行中の事実）。状態には\n"
        "  日付を含めてください（例: 2026年6月頃〜 ユーザーは海外赴任中、9月帰国予定）。\n"
        "- 既存のコア記憶と矛盾する新情報（帰国・引っ越しなど。矛盾は core_updates で書き換え）。\n"
        "\n"
        "2) 手帳のメモ欄 (want_memos / did_memos):\n"
        f"- {scope_sentence if scope_sentence is not None else _scope_sentence(span_new_count)}\n"
        "- この範囲で、やりたいと思ったこと・実際にやったことはありますか?\n"
        "  無ければ空で構いません。あれば、活動の名前（「小説を書く」「絵の練習」\n"
        "  のような粒度。下の一覧にあるものは act:N で参照）と、今日の中身\n"
        "  一行 (text) で。\n"
        f"{today_block}"
        "\n"
        "3) 手帳の約束の欄 (promise_adds / promise_updates):\n"
        "- ユーザーとの約束や引き受けた依頼が生まれたり変わったりしていましたか?\n"
        "  無ければ空で構いません。新しい約束は promise_adds へ。期限が明示されて\n"
        "  いないなら due は書かないでください（期限を発明しない）。\n"
        "- 既に下の「手帳の約束の欄」にあるものを再び add しないでください —\n"
        "  内容や期限に変化があれば promise_updates を使い、その一件の promise:N を\n"
        "  promise_ref にそのまま写します (例: promise:2)。内容が変わったときだけ\n"
        "  content に変更後の全文を書き、期限だけの変更なら content は省略します。\n"
        "  期限が撤回されたときは clear_due で期限を外せます。\n"
        "\n"
        f"{page_review_block}"
        "姿勢:\n"
        "- **採取しないのが普通です。** ほとんどの記憶整理では何も採りません\n"
        "  （各欄は空配列）。無理に何かを刻もうとしないでください。\n"
        f"{fields_sentence}"
        "- 既にコア記憶・手帳にあることは再度採らないでください。\n"
        "\n"
        f"### 現在のコア記憶（合計 {total_chars:,} 字 / 目安 {budget:,} 字）\n"
        f"{core_block}\n"
        "\n"
        "### 手帳のメモ欄（開いているアクティビティ）\n"
        f"{activity_block}\n"
        "\n"
        "### 手帳の約束の欄（開いている約束・依頼）\n"
        f"{task_block}\n"
        "</system>"
    )
    return prompt


# ---------------------------------------------------------------------------
# コア記憶の操作の適用 (tool 関数を経由せず直接)
# ---------------------------------------------------------------------------

def _apply_core_ops(
    persona: Any,
    core_adds: List[Dict[str, Any]],
    core_updates: List[Dict[str, Any]],
    core_removes: List[Dict[str, Any]],
    *,
    core_snapshot: Optional[Dict[str, str]],
) -> tuple[int, int, List[str]]:
    """コア記憶の三一覧を順に適用し、(成功数, 失敗数, 結果テキスト行) を返す。

    順番は追加 → 書き換え → 削除 (応答スキーマの欄の並びと同じ)。

    CAS (Codex 第七巡 修正 2 — タスク帳 CAS の同族): ``core_snapshot`` は
    プロンプト作成時に読んだコア記憶現況の {id: 本文ハッシュ}。update / remove は
    適用時に現在値と照合し、不一致 (LLM 実行中のユーザー編集・削除) はその要素
    だけ棄却して記録に明記する — 古い判断でユーザー編集を上書きしない。照合と
    書き込みは同じ adapter._db_lock 保持下で行う (check-then-act の隙間を作らない
    — コア記憶の書き手は全て同じロックを通る)。スナップショットが無い
    (None = 旧形式の記録) / id がスナップショットに無い場合も要素棄却 —
    推定で適用しない (第五巡の裁定と同族)。

    粒度分け (メモ/約束の適用と同じ規律): **入力の不正** (非 dict 要素・空本文・
    ``core:N`` の形でない / 一覧に無い memory_ref) はその要素だけ棄却して結果行に残す。
    **ストレージ例外** (DB 書き込み障害等) は送出してゲート (退場停止 → 台帳
    applied のまま → 次回再適用) に乗せる — 要素失敗へ丸めると台帳が completed
    になり、本人が指定した記憶操作が静かに永遠に失われる。

    冪等性: add は**同一本文の生存コア記憶があればスキップ**する (成功扱い)。
    ゲート化 (v3 §13.3) で「部分適用 → 失敗 → 同じ結果の再適用」が正規の経路に
    なったため。core_memory 側に冪等キーの機構は無い (2026-08-19 確認) ので
    適用側の内容一致ガードで守る — LLM が既存と同じ内容を再提案したときの
    抑止 (プロンプトの「既にあるものは再度採らない」の保険) も兼ねる。
    update は同値上書きで自然に冪等、remove は再適用時「見つからない」の
    失敗行になるだけで二重の害が無い。
    """
    from sai_memory.core_memory import (
        add_core_memory,
        list_core_memories,
        remove_core_memory,
        update_core_memory,
    )

    adapter = getattr(persona, "sai_memory", None)

    applied = 0
    failed = 0
    lines: List[str] = []
    total = len(core_adds) + len(core_updates) + len(core_removes)

    if adapter is None or getattr(adapter, "conn", None) is None:
        return (0, total, ["コア記憶ストレージが利用できず、採取を適用できませんでした。"])

    # 同一本文ガード用の生存コア記憶 (最初の add で遅延ロード)。
    alive_contents: Optional[Dict[str, int]] = None

    def _snapshot_hash(target_id: int) -> Optional[str]:
        """スナップショット時点の本文ハッシュ。無い = 一覧外 or 記録が旧形式。"""
        if core_snapshot is None:
            return None
        return core_snapshot.get(str(target_id))

    # 以下、ストレージ呼び出し (list/add/update/remove_core_memory) は
    # 意図的に例外を握らない — 障害は送出してゲートに乗せる (docstring)。
    for op in core_adds:
        if not isinstance(op, dict):
            failed += 1
            lines.append(f"不正なコア記憶の追加形式のためスキップ: {op!r}")
            continue
        content = (op.get("content") or "").strip()
        if not content:
            failed += 1
            lines.append("add 失敗: 本文が空でした。")
            continue
        if alive_contents is None:
            with adapter._db_lock:
                alive = list_core_memories(adapter.conn)
            alive_contents = {
                (m.content or "").strip(): m.id for m in alive
            }
        dup_id = alive_contents.get(content)
        if dup_id is not None:
            applied += 1
            lines.append(
                f"コア記憶 core:{dup_id} と同一内容のため再採取をスキップしました。"
            )
            continue
        with adapter._db_lock:
            new_id = add_core_memory(
                adapter.conn, content,
                metadata=json.dumps({"source": "sluice"}),
                confirmed=0,  # 自動採取はユーザー確認待ち
            )
        applied += 1
        alive_contents[content] = new_id
        # 記録は <system> 包みのシステム通知として SAIMemory に残る
        # (_persist_record 参照)。省略・切り詰めは採取事実の改変になるため、
        # 本文は全文を書く (不変条件 §5-8 / 2026-07-07 まはー指摘)。
        lines.append(f"コア記憶 core:{new_id} に採取: {content}")

    for op in core_updates:
        if not isinstance(op, dict):
            failed += 1
            lines.append(f"不正なコア記憶の書き換え形式のためスキップ: {op!r}")
            continue
        raw_ref = op.get("memory_ref")
        target_id = _parse_ref(raw_ref, _CORE_REF_RE)
        if target_id is None:
            failed += 1
            lines.append(
                f"update 失敗: memory_ref が core:N の形ではありません ({raw_ref!r})。"
            )
            continue
        content = (op.get("content") or "").strip()
        if not content:
            failed += 1
            lines.append(f"update 失敗: core:{target_id} の新しい本文が空でした。")
            continue
        snap_hash = _snapshot_hash(target_id)
        if snap_hash is None:
            failed += 1
            lines.append(
                f"update 失敗: core:{target_id} のスナップショット情報が"
                "無いため適用しませんでした。"
            )
            continue
        with adapter._db_lock:
            current = next(
                (
                    m for m in list_core_memories(adapter.conn)
                    if int(m.id) == target_id
                ),
                None,
            )
            cas_ok = (
                current is not None
                and _core_content_hash(current.content) == snap_hash
            )
            # 場面の記憶 (scene) は書き換えの対象外 — 長さ不問。
            #
            # scene は実際の会話の写しで、本人が「直す」ことは会話の写しを
            # 書き換えること、つまり捏造にあたる (このモジュール冒頭の
            # 不変条件「scene は参照コピーのみ」)。提示は先頭 80 字に
            # 刻んでいるが、歯止めをその長さで書くと 80 字以下の scene だけ
            # 書き換えられる穴が残る — 目的 (写しを改変させない) から導けば
            # 条件は種類の一行になる (2026-08-22 裁定、
            # docs/issues/sluice_truncated_scene_update.md)。
            #
            # 削除は対象外にしない (写しを消すことは改変ではない)。
            scene_locked = cas_ok and _is_scene_memory(current)
            ok = (
                cas_ok
                and not scene_locked
                and update_core_memory(
                    adapter.conn, target_id, content, confirmed=0,
                )
            )
        if not cas_ok:
            failed += 1
            lines.append(
                f"記憶 core:{target_id} は実行中に変更されたため"
                "適用しませんでした。"
            )
            LOGGER.warning(
                "[sluice] core update rejected: core:%s changed since "
                "snapshot (CAS mismatch)", target_id,
            )
        elif scene_locked:
            failed += 1
            lines.append(
                f"update 失敗: core:{target_id} は場面の記憶 (実会話の写し) "
                "なので書き換えの対象外です。"
            )
            LOGGER.warning(
                "[sluice] core update rejected: core:%s is a scene memory "
                "(verbatim copy — not editable)", target_id,
            )
        elif ok:
            applied += 1
            lines.append(f"コア記憶 core:{target_id} を更新: {content}")
        else:
            failed += 1
            lines.append(f"update 失敗: core:{target_id} が見つかりませんでした。")

    for op in core_removes:
        if not isinstance(op, dict):
            failed += 1
            lines.append(f"不正なコア記憶の削除形式のためスキップ: {op!r}")
            continue
        raw_ref = op.get("memory_ref")
        target_id = _parse_ref(raw_ref, _CORE_REF_RE)
        if target_id is None:
            failed += 1
            lines.append(
                f"remove 失敗: memory_ref が core:N の形ではありません ({raw_ref!r})。"
            )
            continue
        snap_hash = _snapshot_hash(target_id)
        if snap_hash is None:
            failed += 1
            lines.append(
                f"remove 失敗: core:{target_id} のスナップショット情報が"
                "無いため適用しませんでした。"
            )
            continue
        with adapter._db_lock:
            current = next(
                (
                    m for m in list_core_memories(adapter.conn)
                    if int(m.id) == target_id
                ),
                None,
            )
            cas_ok = (
                current is not None
                and _core_content_hash(current.content) == snap_hash
            )
            ok = cas_ok and remove_core_memory(adapter.conn, target_id)
        if not cas_ok:
            failed += 1
            lines.append(
                f"記憶 core:{target_id} は実行中に変更されたため"
                "適用しませんでした。"
            )
            LOGGER.warning(
                "[sluice] core remove rejected: core:%s changed since "
                "snapshot (CAS mismatch)", target_id,
            )
        elif ok:
            applied += 1
            lines.append(f"コア記憶 core:{target_id} を削除しました。")
        else:
            failed += 1
            lines.append(f"remove 失敗: core:{target_id} が見つかりませんでした。")

    return (applied, failed, lines)


# ---------------------------------------------------------------------------
# 手帳メモの適用 (pocketbook)
# ---------------------------------------------------------------------------

def _apply_memos(
    persona: Any,
    want_memos: List[Dict[str, Any]],
    did_memos: List[Dict[str, Any]],
    *,
    idem_prefix: str,
    span_start_id: Optional[str],
    span_end_id: Optional[str],
    offered_activities: Dict[int, str],
    event_date: Optional[str] = None,
    origin: str = "live",
) -> tuple[int, int, List[str]]:
    """want/did メモを手帳 (pocketbook) に書く。(成功数, 失敗数, 結果行) を返す。

    ``event_date`` / ``origin`` は二つの時刻と由来の刻印
    (docs/intent/sluice_coverage_gaps.md B-2)。``event_date`` は採取元の会話の
    メッセージ時刻から呼び出し側が機械で導いた「できごとの日」(None = 導け
    なかった — 読み手は date で代替する)。``origin`` は 'live' (定常のスルース) /
    'readback' (本人の読み返し)。どちらも本人の申告は使わない。

    - ``new_activity_name`` は get_or_create_activity(origin='sluice') で収束させる。
    - ``activity_ref`` は ``act:N`` の形の写しだけを受け取り (:func:`_parse_ref`)、
      プロンプトに同梱した一覧 (``offered_activities``) に無い番号を拒否して、
      その要素だけ捨ててログに残す (LLM の発明 id を書かせない)。
    - 冪等キーは「安定プレフィックス (span 由来) + 操作番号」— 同じ担当範囲の
      再適用 (部分失敗 → 次回 Metabolism の再処理) で重複しない。
    - span_start_id / span_end_id はこのスルースの一手が担当した範囲 (前回の
      パンマーカーから、実際に LLM に渡した末尾まで) の機械刻印。本人の申告は
      使わない (§13.6)。
    - 一連の書き込みは commit=False で束ね、最後に一括 commit する。要素単位の
      不正 (空本文・一覧外 id) はその要素だけ捨てるが、ストレージ例外は送出する
      (スルース失敗 = 退場停止のゲートに乗せる)。
    - 内容ベースの重複防止 (コア記憶 add の内容一致ガードと同じ二段構え): 同じ
      できごとの日・同じアクティビティ・同じ種類・同じ本文の既存メモがあれば
      スキップする (成功扱い)。冪等キーは担当範囲が変わると別キーになるので、
      繰り越された回の再提案を止められない。照合は書き込みと同じロック・同じ
      トランザクションの中で行う
      (docs/issues/sluice_memo_duplicate_across_spans.md)。
    """
    items: List[Tuple[str, Any]] = (
        [("want", m) for m in (want_memos or [])]
        + [("did", m) for m in (did_memos or [])]
    )
    if not items:
        return (0, 0, [])

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or getattr(adapter, "conn", None) is None:
        return (0, len(items), ["手帳ストレージが利用できず、メモを書けませんでした。"])

    from sai_memory.memory.pocketbook import (
        add_memo,
        find_memo_by_content,
        get_or_create_activity,
    )
    from saiverse import clock

    today = clock.now().date().isoformat()
    persona_id = getattr(persona, "persona_id", None)

    applied = 0
    failed = 0
    lines: List[str] = []
    kind_label = _MEMO_KIND_LABEL

    with adapter._db_lock:
        conn = adapter.conn
        try:
            for idx, (kind, memo) in enumerate(items):
                if not isinstance(memo, dict):
                    failed += 1
                    lines.append(f"不正なメモ形式のためスキップ: {memo!r}")
                    continue
                text = (memo.get("text") or "").strip()
                if not text:
                    failed += 1
                    lines.append(f"{kind_label[kind]}メモをスキップ: 本文が空でした。")
                    continue
                new_name = (memo.get("new_activity_name") or "").strip()
                activity_ref = memo.get("activity_ref")
                if new_name:
                    activity = get_or_create_activity(
                        conn, new_name, "sluice", commit=False,
                    )
                    aid = activity.id
                    aname = activity.name
                elif activity_ref is not None:
                    parsed_aid = _parse_ref(activity_ref, _ACTIVITY_REF_RE)
                    if parsed_aid is None:
                        failed += 1
                        lines.append(
                            f"{kind_label[kind]}メモをスキップ: "
                            f"activity_ref={activity_ref!r} は act:N の形ではありません。"
                        )
                        LOGGER.warning(
                            "[sluice] memo rejected: activity_ref %r is not act:N "
                            "(persona=%s)", activity_ref, persona_id,
                        )
                        continue
                    if parsed_aid not in offered_activities:
                        failed += 1
                        lines.append(
                            f"{kind_label[kind]}メモをスキップ: "
                            f"act:{parsed_aid} は一覧にありません。"
                        )
                        LOGGER.warning(
                            "[sluice] memo rejected: act:%s not in offered list "
                            "(persona=%s)", parsed_aid, persona_id,
                        )
                        continue
                    aid = parsed_aid
                    aname = offered_activities[parsed_aid]
                else:
                    failed += 1
                    lines.append(
                        f"{kind_label[kind]}メモをスキップ: activity_ref も "
                        "new_activity_name もありません。"
                    )
                    continue
                # 内容ベースの重複防止 — 同じロック・同じトランザクションの中で
                # 照合してから書く (check-then-act の隙間を作らない)。同じ束の中で
                # 先に書いたメモも同じ接続から見えるので、一回の結果に同じメモが
                # 二つ入っていた場合もここで止まる。照合する日はこれから書く
                # メモの**できごとの日** (event_date、無ければ今日) — 書かれた日で
                # 照合すると、読み返しで拾った別の日のできごとが今日の記録と
                # ぶつかって落ちる (B-2)。
                duplicate = find_memo_by_content(
                    conn, aid, event_date or today, kind, text
                )
                if duplicate is not None:
                    applied += 1
                    lines.append(
                        f"手帳「{aname}」の{kind_label[kind]}メモは既に手帳にある"
                        f"ため採りませんでした: {text}"
                    )
                    continue
                add_memo(
                    conn, aid, today, kind, text,
                    span_start_id=span_start_id,
                    span_end_id=span_end_id,
                    idem_key=f"{idem_prefix}:m{idx}",
                    event_date=event_date,
                    origin=origin,
                    commit=False,
                )
                applied += 1
                lines.append(f"手帳「{aname}」に{kind_label[kind]}メモ: {text}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    return (applied, failed, lines)


# ---------------------------------------------------------------------------
# 約束の適用 (task_book)
# ---------------------------------------------------------------------------

class _RevisionUnknown:
    """「スナップショット時点の revision が分からない」を表す番人 (sentinel)。

    Codex 第八巡 修正 4: ``offered_tasks`` の値 (= CAS の照合値) では、
    ``None`` は「revision 列が NULL の行」という**正当な期待値**であり、
    ``update_entry(expected_revision=None)`` は IS NULL に一致する本物の CAS に
    なる。一方、台帳の旧形式記録 (``offered_task_ids`` しか持たない) から
    復元した場合の「不明」を同じ ``None`` で表すと、``update_entry`` は
    「現在値を読み直して CAS する」経路に落ち、**どんな現在値にも当たる
    = CAS 無効**になる (古い判断が実行中のユーザー編集を黙って上書きする)。
    不明は不明として別の値で持ち、その要素は棄却して判断ターンに残す
    (推定で適用しない — 第五巡・第七巡の裁定と同族)。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 記録・ログ用
        return "<revision unknown>"


#: 「スナップショット不明」の唯一の実体 (``is`` で判定する)。
_REVISION_UNKNOWN = _RevisionUnknown()

#: 復元した対応表で「期限のスナップショットが分からない」を表す番兵。
#: None (= 一覧の時点で期限なし、clear_due の空振り成功の根拠になる) と
#: 区別するために置く。台帳へ再直列化されることはない (凍結されるのは
#: 新規コール側の :func:`_offered_task_map` の生の値だけ)。
_DUE_AT_UNKNOWN = object()


#: due の日付のみ形式 (YYYY-MM-DD)。\d でなく [0-9] 明記 (全角数字を通さない)。
_DUE_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: サポートする期限の年範囲 (Codex 第四巡 修正 3)。下限 1970 = epoch の始まり
#: (負の epoch を持ち込まない)、上限 9999 = ISO 表記の上限。範囲内でも環境の
#: timestamp() が受けない日付 (Windows の遠未来など) は変換段の except が拾う。
_DUE_MIN_YEAR = 1970
_DUE_MAX_YEAR = 9999


def parse_due(raw: str) -> tuple[Optional[int], Optional[str]]:
    """LLM 出力の期限文字列を epoch 秒へ変換する。(epoch, 解釈不能の理由) を返す。

    「期限の文字列をどう読むか」の規則はこの関数**一箇所**が持つ — スルースだけ
    でなく、本人が唱える手帳のスペル (``pocketbook_write``) も同じ入力形
    ('YYYY-MM-DD') を同じ意味で読む必要があるため公開している。二箇所に同じ
    規則を書くと、同じ日付文字列がタスク帳の中で二つの epoch を持つ。

    理由が非 None のとき、呼び出し側は**約束を失わない方向**で処理する (まはー
    裁定 2026-08-19 — タスク帳の芯は「失くすことが許されない」): add は期限なしで
    保存、update は期限の変更だけを見送る。どちらも判断ターン記録に明記する
    (発明しない・黙って落とさない・約束は失わない、の三立)。日付のみ
    (YYYY-MM-DD) はその日の終わり (23:59:59 ローカル) と解釈する (「〜までに」の
    意味論)。「解釈不能」の判定は ValueError (書式不正) に加えて年範囲検証と
    OSError / OverflowError (環境の timestamp() が受けない日付 — Windows では
    0001-01-01 が OSError) まで広く畳む: 台帳に凍結された結果の再適用でこの例外が
    送出されると、同じ例外で退場が永久に止まり続けるため。
    """
    if not raw:
        return (None, None)
    try:
        if _DUE_DATE_RE.match(raw):
            dt = datetime.fromisoformat(raw).replace(hour=23, minute=59, second=59)
        else:
            dt = datetime.fromisoformat(raw)
    except ValueError:
        return (None, f"期限 {raw!r} を日付として解釈できません。")
    if not (_DUE_MIN_YEAR <= dt.year <= _DUE_MAX_YEAR):
        return (
            None,
            f"期限 {raw!r} はサポート範囲 ({_DUE_MIN_YEAR}〜{_DUE_MAX_YEAR} 年) の外です。",
        )
    try:
        return (int(dt.timestamp()), None)
    except (ValueError, OSError, OverflowError):
        return (None, f"期限 {raw!r} を epoch へ変換できません (環境のサポート範囲外)。")


def _apply_promises(
    lifecycle: Any,
    persona: Any,
    promise_adds: List[Dict[str, Any]],
    promise_updates: List[Dict[str, Any]],
    *,
    idem_prefix: str,
    span_start_id: Optional[str],
    span_end_id: Optional[str],
    offered_tasks: Dict[str, Any],
) -> tuple[int, int, List[str]]:
    """約束の二一覧をタスク帳 (task_book) に適用する。(成功数, 失敗数, 結果行) を返す。

    順番は追加 → 変更 (応答スキーマの欄の並びと同じ)。

    ``offered_tasks`` は :func:`_offered_task_map` の対応表
    ``{"N": {"task_id", "revision", "due_at", "content"}}`` (N は同梱一覧の
    1 始まりの位置)。``revision`` が :data:`_REVISION_UNKNOWN` (スナップ
    ショット不明 = 記録の破損) と ``None`` (update_entry では「省略 = 読み直し
    CAS」の意味になり、スナップショット CAS が外れる) の変更はどちらも
    棄却する。

    - 追加は content 必須 (空は要素棄却)。同じ本文の約束が既に生きていれば
      追加しない (一覧にあるものの再 add の歯止め)。origin='sluice'・安定
      idem_key (span 由来プレフィックス + 操作番号) で冪等化・origin_ref に
      span 由来の参照。同じ担当範囲の再適用で重複しない。
    - counterpart は 'user' 固定 — 応答スキーマに相手欄が無いため実装上の既定。
      スルースの捕獲対象はユーザーとの会話から生まれる約束で、相手の既定は
      ユーザーが最も嘘が少ない。ペルソナ同士の約束を拾うと相手名義がユーザーに
      化ける限界がある (スキーマに相手欄を足すまでの割り切り)。
    - due は日付文字列を epoch へ変換。解釈不能 (書式不正・範囲外・環境の
      timestamp() 例外) のときも**約束は失わない** (まはー裁定 2026-08-19 —
      タスク帳の芯は「失くすことが許されない」): add は期限なしで保存し、
      update は期限の変更だけを見送る (content 等の変更は適用)。どちらも
      判断ターン記録に明記する (発明しない・黙って落とさない・約束は失わない)。
    - ``clear_due=True`` は変更で期限を外す (期限の撤回)。due との同時指定は
      矛盾なので入力不正として要素棄却する。期限の無い約束への clear_due は
      無害な空振りとして「期限は変更しなかった」を記録する (棄却しない)。
    - 変更は promise_ref を ``promise:N`` の完全一致だけ受け (:func:`_parse_ref`
      の流儀)、N を同梱一覧 (``offered_tasks``) で引く。形の崩れた参照・一覧に
      無い N はその要素だけ棄却してログに残す (activity_ref と同じ閉語彙の
      規律 — LLM の発明参照を書かせない)。content / due / clear_due が全部
      無い変更は「変更内容がありません」で棄却する。
    - 変更は**スナップショット時点の revision で CAS** する
      (``offered_tasks`` の値を ``update_entry(expected_revision=...)`` へ渡す)。
      LLM 実行中にユーザーが同じ行を編集していたら CAS が外れ、その要素だけ
      棄却して「実行中に変更されたため適用しなかった」を判断ターン記録に明記
      する — 黙って上書きしない + 黙って消さない、を棄却の記録で両立する。
      ゲート失敗にはしない (稀な競合のたびに退場を止めて LLM を焼き直すのは
      重すぎ、ユーザー編集が続くと膠着する)。
    - TaskBookError は握りつぶさずログしてその要素だけ失敗扱い — スルース全体は
      成功扱いのまま (情報は失われていない)。ストレージ自体の例外 (DB 接続断等)
      は送出してゲートに乗せる。
    """
    promise_adds = list(promise_adds or [])
    promise_updates = list(promise_updates or [])
    total = len(promise_adds) + len(promise_updates)
    if not total:
        return (0, 0, [])

    manager = getattr(lifecycle, "manager", None)
    persona_id = getattr(persona, "persona_id", None)
    if manager is None or not hasattr(manager, "SessionLocal") or not persona_id:
        return (0, total, ["タスク帳ストレージが利用できず、約束を書けませんでした。"])

    from saiverse.task_book import TaskBookError, add_entry, list_open, update_entry

    if span_start_id and span_end_id and span_start_id != span_end_id:
        origin_ref = f"{span_start_id}..{span_end_id}"
    else:
        origin_ref = span_end_id or span_start_id

    applied = 0
    failed = 0
    lines: List[str] = []

    # --- 追加 (promise_adds) ---
    open_by_content: Optional[Dict[str, Optional[str]]] = None
    for idx, item in enumerate(promise_adds):
        if not isinstance(item, dict):
            failed += 1
            lines.append(f"不正な約束の追加形式のためスキップ: {item!r}")
            continue
        content = (item.get("content") or "").strip()
        if not content:
            failed += 1
            lines.append("約束の追加をスキップ: content が空でした。")
            continue
        idem_key = f"{idem_prefix}:pa{idx}"
        if open_by_content is None:
            # 生きている約束の本文 → idem_key。読み出しの例外は送出する
            # (fail-closed — 空へ丸めると重複の歯止めが黙って外れる)。
            open_by_content = {
                str(entry.get("content") or "").strip(): entry.get("idem_key")
                for entry in list_open(manager, persona_id)
            }
        if content in open_by_content and open_by_content[content] != idem_key:
            # 同じ本文の約束が既に生きている (一覧にあるものの再 add、または
            # 別の担当範囲・別の形式世代で追加済み)。同じ idem_key の行は
            # この要素自身の再適用なので、下の add_entry (get-or-create) に通す。
            # 数え方は手帳メモ・コア記憶の同一内容スキップと揃えて成功扱い
            # (約束は既に器にある = 情報は失われていない)。
            applied += 1
            lines.append(f"約束は既にタスク帳にあるため追加しませんでした: {content}")
            continue
        due_raw = (item.get("due") or "").strip()
        due_at, due_error = parse_due(due_raw)
        if due_error is not None:
            # 約束は失わない (まはー裁定): 期限だけを落とし、記録に明記する。
            LOGGER.warning(
                "[sluice] unparsable due; keeping the promise without a "
                "deadline (persona=%s, add): %s", persona_id, due_error,
            )
        try:
            add_entry(
                manager, persona_id, content,
                origin="sluice",
                due_at=due_at,
                counterpart="user",
                origin_ref=origin_ref,
                idem_key=idem_key,
            )
        except TaskBookError as exc:
            # 受け入れ不変条件違反など。要素単位の失敗としてログと結果行に
            # 残し、スルース全体は止めない。
            failed += 1
            lines.append(f"約束の追加に失敗: {exc}")
            LOGGER.warning(
                "[sluice] promise add failed (persona=%s): %s", persona_id, exc,
            )
            continue
        open_by_content[content] = idem_key
        applied += 1
        suffix = f"（期限 {due_raw}）" if due_at is not None else "（期限なし）"
        lines.append(f"タスク帳に約束を追加: {content}{suffix}")
        if due_error is not None:
            lines.append(
                f"期限『{due_raw}』を解釈できなかったため期限なしで登録しました。"
            )

    # --- 変更 (promise_updates) ---
    for item in promise_updates:
        if not isinstance(item, dict):
            failed += 1
            lines.append(f"不正な約束の変更形式のためスキップ: {item!r}")
            continue
        raw_ref = item.get("promise_ref")
        position = _parse_ref(raw_ref, _PROMISE_REF_RE)
        if position is None:
            failed += 1
            lines.append(
                f"約束の変更をスキップ: promise_ref={raw_ref!r} は promise:N の形ではありません。"
            )
            LOGGER.warning(
                "[sluice] promise update rejected: malformed promise_ref %r "
                "(persona=%s)", raw_ref, persona_id,
            )
            continue
        ref = f"promise:{position}"
        snapshot = offered_tasks.get(str(position))
        if not isinstance(snapshot, dict) or not snapshot.get("task_id"):
            failed += 1
            lines.append(f"約束の変更をスキップ: {ref} は一覧にありません。")
            LOGGER.warning(
                "[sluice] promise update rejected: %s not in offered list "
                "(persona=%s)", ref, persona_id,
            )
            continue
        task_id = str(snapshot["task_id"])
        snapshot_content = snapshot.get("content")
        label = (
            f"約束「{snapshot_content}」" if isinstance(snapshot_content, str)
            and snapshot_content else f"約束 {ref}"
        )
        expected_revision = snapshot.get("revision", _REVISION_UNKNOWN)
        if expected_revision is _REVISION_UNKNOWN or expected_revision is None:
            # スナップショット時点の revision が分からない (記録の破損など —
            # Codex 第八巡 修正 4)。None も同じ扱い — update_entry は
            # expected_revision=None を「引数省略 = 現在値を読み直して CAS」と
            # 解釈するので (saiverse/task_book.py)、None を渡すとスナップ
            # ショット CAS が無効化され、古い判断が実行中のユーザー編集を
            # 上書きできてしまう。REVISION 列は NOT NULL なので None の
            # スナップショットは実質「破損」の一形。要素棄却にする。
            failed += 1
            lines.append(
                f"{label}の変更をスキップ: スナップショット情報が無いため"
                "適用しませんでした。"
            )
            LOGGER.warning(
                "[sluice] promise update rejected: snapshot revision "
                "unknown for task %s (persona=%s)", task_id, persona_id,
            )
            continue
        content = (item.get("content") or "").strip()
        due_raw = (item.get("due") or "").strip()
        clear_due = item.get("clear_due")
        if clear_due is not None and not isinstance(clear_due, bool):
            failed += 1
            lines.append(
                f"{label}の変更をスキップ: clear_due が真偽値ではありません ({clear_due!r})。"
            )
            continue
        if clear_due and due_raw:
            # 「期限を外す」と「期限を設定する」の同時指定は矛盾 — 入力不正として
            # 要素棄却 (どちらの意図か発明しない)。
            failed += 1
            lines.append(
                f"{label}の変更をスキップ: due と clear_due は同時に指定できません。"
            )
            continue
        due_at, due_error = parse_due(due_raw)
        if due_error is not None:
            # 約束は失わない (まはー裁定): 期限の変更だけを見送り、記録に明記する。
            LOGGER.warning(
                "[sluice] unparsable due; keeping the deadline unchanged "
                "(persona=%s, task=%s): %s", persona_id, task_id, due_error,
            )
        # 期限の無い約束への clear_due は無害な空振り (実験で軽量モデルが稀に
        # 余分に付けた)。要素棄却にはせず「期限は変更しなかった」と記録し、
        # 併記された content は適用する。期限の有無は**一覧に見せた時点の
        # スナップショット**で見る — 空振りだけの要素は update_entry を呼ばず
        # CAS を通らないので、実行中にユーザーが期限を付けていた場合は
        # その期限が残る (ユーザーの編集が勝つ。約束は失われない)。記録の
        # 文言も「一覧の時点で」と現在形を避ける。
        clear_due_noop = bool(clear_due) and snapshot.get("due_at") is None
        kwargs: Dict[str, Any] = {}
        if content:
            kwargs["content"] = content
        if clear_due and not clear_due_noop:
            # 期限の撤回 (update_entry の明示 due_at=None)。スルースが足す行は
            # counterpart='user' なので「期限も相手も無い行」の受け入れ
            # 不変条件には抵触しない。相手なしの既存行が対象だったときは
            # update_entry の TaskBookError が要素失敗として拾う。
            kwargs["due_at"] = None
        elif due_at is not None:
            kwargs["due_at"] = due_at
        if not kwargs:
            if clear_due_noop:
                # 空振りの成功 (望まれた状態 = 期限なし は一覧の時点で既に
                # 成り立っている)。下の「変更内容がありません」(failed) と
                # 扱いが違うのは意図的 — こちらは意味のある意図 (期限を外す)
                # が既に満たされている形、あちらは意図そのものが空の形で、
                # 後者は失敗として見えることが応答の欠陥の観測点になる。
                applied += 1
                lines.append(
                    f"{label}は一覧の時点で期限が無いため、期限は変更しませんでした。"
                )
                continue
            failed += 1
            lines.append(f"{label}の変更をスキップ: 変更内容がありません ({ref})。")
            if due_error is not None:
                lines.append(
                    f"期限『{due_raw}』を解釈できなかったため、"
                    f"{label}の期限は変更しませんでした。"
                )
            continue
        try:
            update_entry(
                manager, persona_id, task_id,
                expected_revision=expected_revision,
                **kwargs,
            )
        except TaskBookError as exc:
            # スナップショット時点の revision での CAS が外れた
            # (実行中のユーザー編集・クローズ・消失)。発明 ref は上の
            # 一覧検証で弾かれているため、ここへ来る TaskBookError は
            # ほぼ「実行中に行が変わった」— 古い判断で上書きせず、
            # 棄却を記録に明記する。受け入れ不変条件の拒否 (期限も
            # 相手も無い行になる更新) だけは文面をそのまま出す。
            failed += 1
            if "期限も相手もない" in str(exc):
                lines.append(f"{label}の変更の適用に失敗: {exc}")
            else:
                lines.append(
                    f"{label}は実行中に変更されたため適用しませんでした。"
                )
            LOGGER.warning(
                "[sluice] promise update rejected (persona=%s, task=%s): %s",
                persona_id, task_id, exc,
            )
            continue
        applied += 1
        suffix = "（期限を撤回）" if "due_at" in kwargs and kwargs["due_at"] is None else ""
        lines.append(f"タスク帳の{label}を更新: {content or '(期限のみ)'}{suffix}")
        if clear_due_noop:
            lines.append(f"{label}は一覧の時点で期限が無いため、期限は変更しませんでした。")
        if due_error is not None:
            lines.append(
                f"期限『{due_raw}』を解釈できなかったため、"
                f"{label}の期限は変更しませんでした。"
            )

    return (applied, failed, lines)


# ---------------------------------------------------------------------------
# 記憶の手入れ (Memopedia ページの分割・統合の提案) — 2026-10-09 に就寝判断から移設
# ---------------------------------------------------------------------------
#
# 流れ: 定常のスルースの回に、業務日一回だけ候補 (saiverse/curation.py の決定論
# 検知) を本人に見せ、``page_reviews`` で approve / skip を返させる。approve は
# curation_plans に予約として積み、スルースの確定 (``_finalize``) の後に背景
# スレッドで実行する。後から通す採取 (run_sluice_capture) には載せない。
#
# 手入れは採取のゲート (§13.3) の成立条件ではない — 検知・提示の記録・予約・
# 起動のどこで失敗しても、スルース本体 (と退場) は止めない (WARNING を残す)。
# 例外は応答の形の検査だけで、候補を見せた回に page_reviews 欄が無い応答は
# 他の欄の欠落と同じく fail-closed になる (再試行の回は同じ業務日なので候補を
# 見せない)。

#: 背景で記憶の手入れを実行中のペルソナ。同じペルソナのバッチを並走させない
#: — 二本が同じ pending のプランを同時に読むと、同じ分割・統合が二度走る。
_CURATION_BATCH_RUNNING: set = set()
_CURATION_BATCH_LOCK = threading.Lock()


def _curation_business_day(lifecycle: Any, persona_id: Optional[str]) -> str:
    """提示の回数を数える単位の業務日 ("YYYY-MM-DD")。

    :func:`saiverse.day_plan.resolve_business_day` (予約・watchdog と同じ
    解決器) を使う。manager が無い・ライフが読めない・解決が例外のときは
    ローカルの暦日 (``clock.now()``) に倒す。
    """
    from saiverse import clock

    manager = getattr(lifecycle, "manager", None)
    if manager is not None and persona_id:
        try:
            from saiverse.day_plan import resolve_business_day
            basis = resolve_business_day(manager, persona_id)
            if basis is not None and basis.plan_date:
                return str(basis.plan_date)
        except Exception:
            LOGGER.warning(
                "[sluice] business day resolution failed for the page review "
                "offer (persona=%s); using the local date", persona_id,
                exc_info=True,
            )
    return clock.now().date().isoformat()


def _offer_page_reviews(
    lifecycle: Any, persona: Any,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """この回に本人へ見せる記憶の手入れの候補と、その業務日。無ければ ``([], None)``。

    業務日に一回だけ: 最後に提示した業務日が今日なら検知もしない。提示の
    記録はここでは書かない — 呼び出し側が、**応答が読めた後** (本人が候補を
    見て答えた後) に :func:`_mark_page_reviews_offered` で書く。入力に入らず
    外した回・LLM が失敗した回・応答が読めなかった回は記録されず、同じ業務日の
    後のスルースでもう一度提示される。読めた回は、全部見送りでも記録され、
    翌業務日の検知まで再提示されない。

    返す要素は ``{"op_id", "kind", "refs", "line"}`` だけに絞る (台帳に凍結
    して、記録の再適用で同じ候補を引くため)。どこで失敗しても空を返す。
    """
    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None or not persona_id:
        return ([], None)
    try:
        from sai_memory.curation_ops import (
            VALID_PLAN_KINDS,
            get_last_presented_day,
        )
        from saiverse.curation import detect_curation_candidates

        business_day = _curation_business_day(lifecycle, persona_id)
        with adapter._db_lock:
            last_day = get_last_presented_day(conn)
        if last_day == business_day:
            return ([], None)
        with adapter._db_lock:
            detected = detect_curation_candidates(conn, persona_id)
        candidates: List[Dict[str, Any]] = []
        for cand in detected or []:
            if not isinstance(cand, dict):
                continue
            op_id = cand.get("op_id")
            kind = cand.get("kind")
            refs = cand.get("refs")
            if not (isinstance(op_id, str) and op_id and kind in VALID_PLAN_KINDS
                    and isinstance(refs, list)):
                continue
            candidates.append({
                "op_id": op_id,
                "kind": kind,
                "refs": [str(r) for r in refs],
                "line": str(cand.get("line") or op_id),
            })
        if not candidates:
            return ([], None)
        return (candidates, business_day)
    except Exception:
        LOGGER.warning(
            "[sluice] page review offer failed (persona=%s); this sluice runs "
            "without the offer", persona_id, exc_info=True,
        )
        return ([], None)


def _mark_page_reviews_offered(persona: Any, business_day: str) -> bool:
    """候補を見せた業務日を記録する。書けなければ False。

    呼び出し側は応答が読めた後に呼ぶので、False でも応答は捨てない (WARNING を
    出して進む)。その場合「業務日に一回だけ」は破れて同じ日にもう一度見せうるが、
    承認の二重は予約の冪等で無害 — 見せ損ないより軽い側に倒す。
    """
    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None:
        return False
    try:
        from sai_memory.curation_ops import record_presented_day
        with adapter._db_lock:
            record_presented_day(conn, business_day)
    except Exception:
        LOGGER.warning(
            "[sluice] recording the page review offer failed (persona=%s); "
            "this sluice runs without the offer", persona_id, exc_info=True,
        )
        return False
    return True


def _restore_page_review_candidates(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """台帳に凍結した記憶の手入れの候補を読み戻す。破損は None。

    呼び出し側は**欄が無い**旧記録 (2026-10-09 より前) に ``[]`` を渡す —
    「提案ゼロの回」として再生する。欄があるのに読めない (配列でない、要素の
    形が違う) ものは破損 = 記録ごと採り直し (約束の対応表と同じ扱い。破損の
    まま再適用すると、本人の承認が黙って失われたまま completed になる)。
    """
    if not isinstance(raw, list):
        return None
    from sai_memory.curation_ops import VALID_PLAN_KINDS

    restored: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        op_id = item.get("op_id")
        kind = item.get("kind")
        refs = item.get("refs")
        line = item.get("line")
        if not (isinstance(op_id, str) and op_id):
            return None
        if kind not in VALID_PLAN_KINDS:
            return None
        if not (isinstance(refs, list) and all(isinstance(r, str) for r in refs)):
            return None
        restored.append({
            "op_id": op_id,
            "kind": kind,
            "refs": list(refs),
            "line": line if isinstance(line, str) else op_id,
        })
    return restored


def _page_review_op_ids(candidates: Optional[List[Dict[str, Any]]]) -> List[str]:
    return [str(c["op_id"]) for c in (candidates or []) if c.get("op_id")]


def _apply_page_reviews(
    persona: Any,
    page_reviews: List[Any],
    candidates: List[Dict[str, Any]],
) -> Tuple[int, int, int, List[str]]:
    """``page_reviews`` を適用する: approve は curation_plans に予約を積む。

    承認と予約は同一回の応答でだけ結ばれる — この回の確定が検算で破棄されても
    予約は残り、次の確定でまとめて実行される (取り消しの経路は無い)。

    Returns:
        ``(承認して予約した数, 見送った数, 予約に失敗した数, 記録行)``

    - 候補に無い op_id・未知の verdict は WARNING を出して無視する (記録行にも
      出さない — 返答の誤記で本人の記録を汚さない)。同じ op_id の二件目も無視。
    - approve の予約は冪等 (``enqueue_plan`` は同じ op_id の pending があれば
      積み直さない) — 記録の再適用で二重に積まれない。
    - skip は何もしない (翌業務日の検知で、条件が続けば再提示される)。
    - 実行は呼び出し元の確定 (``_finalize``) の後 (:func:`_maybe_launch_curation_batch`)。
    """
    persona_id = getattr(persona, "persona_id", None)
    valid_ops = {c["op_id"]: c for c in candidates if c.get("op_id")}
    approved = skipped = failed = 0
    lines: List[str] = []
    if not valid_ops:
        if page_reviews:
            LOGGER.warning(
                "[sluice] page_reviews present without offered candidates; "
                "ignored (persona=%s)", persona_id,
            )
        return (0, 0, 0, lines)

    handled: set = set()
    for index, review in enumerate(page_reviews):
        if not isinstance(review, dict):
            LOGGER.warning(
                "[sluice] page_reviews[%d] is not an object; ignored (persona=%s)",
                index, persona_id,
            )
            continue
        op_id = str(review.get("op_id") or "").strip()
        verdict = str(review.get("verdict") or "").strip()
        if op_id not in valid_ops:
            LOGGER.warning(
                "[sluice] page_reviews[%d] op_id=%r is not an offered candidate; "
                "ignored (persona=%s)", index, op_id, persona_id,
            )
            continue
        if verdict not in _PAGE_REVIEW_VERDICTS:
            LOGGER.warning(
                "[sluice] page_reviews[%d] verdict=%r is neither approve nor "
                "skip; ignored (persona=%s)", index, verdict, persona_id,
            )
            continue
        if op_id in handled:
            LOGGER.warning(
                "[sluice] page_reviews[%d] repeats op_id=%r; ignored (persona=%s)",
                index, op_id, persona_id,
            )
            continue
        handled.add(op_id)
        cand = valid_ops[op_id]
        line = cand.get("line") or op_id
        if verdict == "skip":
            skipped += 1
            lines.append(f"記憶ページの再編を見送り: {line}")
            continue
        adapter = getattr(persona, "sai_memory", None)
        conn = getattr(adapter, "conn", None) if adapter is not None else None
        try:
            if conn is None:
                raise SluiceStorageUnavailableError(
                    "memory.db connection is missing; cannot reserve the plan"
                )
            from sai_memory.curation_ops import enqueue_plan
            with adapter._db_lock:
                plan_id = enqueue_plan(
                    conn, cand["kind"], op_id, list(cand.get("refs") or []),
                )
        except Exception as exc:
            failed += 1
            LOGGER.warning(
                "[sluice] reserving page review op_id=%r failed (persona=%s)",
                op_id, persona_id, exc_info=True,
            )
            lines.append(
                f"記憶ページの再編を承認しましたが、予定に入れられませんでした"
                f"（{exc}）: {line}"
            )
            continue
        approved += 1
        LOGGER.info(
            "[sluice] page review op_id=%r approved and reserved as plan %s "
            "(persona=%s)", op_id, plan_id, persona_id,
        )
        lines.append(f"記憶ページの再編を承認（この後の整理で実行）: {line}")
    return (approved, skipped, failed, lines)


def _maybe_launch_curation_batch(lifecycle: Any, persona: Any) -> None:
    """pending の予約があれば、背景の daemon スレッドで記憶の手入れを実行する。

    スルースの確定 (``_finalize``) が成功した後にだけ呼ぶ — 確定が破棄された回
    (run_metabolism の検算で見送り) の予約は pending のまま残り、次の確定で
    起動される。同じペルソナのバッチが走っている間は起動しない
    (:data:`_CURATION_BATCH_RUNNING`)。失敗はスルースへ伝えない (WARNING)。
    """
    persona_id = getattr(persona, "persona_id", None)
    try:
        manager = getattr(lifecycle, "manager", None)
        adapter = getattr(persona, "sai_memory", None)
        conn = getattr(adapter, "conn", None) if adapter is not None else None
        if manager is None or conn is None or not persona_id:
            return
        from sai_memory.curation_ops import list_pending, run_pending_plans

        with adapter._db_lock:
            pending = list_pending(conn)
        if not pending:
            return
        with _CURATION_BATCH_LOCK:
            if persona_id in _CURATION_BATCH_RUNNING:
                LOGGER.info(
                    "[sluice] curation batch already running (persona=%s); "
                    "the pending plans wait for the next launch", persona_id,
                )
                return
            _CURATION_BATCH_RUNNING.add(persona_id)

        def _run() -> None:
            try:
                run_pending_plans(manager, persona_id)
            except Exception:
                LOGGER.warning(
                    "[sluice] curation batch raised (persona=%s)", persona_id,
                    exc_info=True,
                )
            finally:
                with _CURATION_BATCH_LOCK:
                    _CURATION_BATCH_RUNNING.discard(persona_id)

        LOGGER.info(
            "[sluice] launching the curation batch (persona=%s pending=%d)",
            persona_id, len(pending),
        )
        try:
            threading.Thread(
                target=_run, name=f"CurationBatch-{persona_id[:8]}", daemon=True,
            ).start()
        except Exception:
            with _CURATION_BATCH_LOCK:
                _CURATION_BATCH_RUNNING.discard(persona_id)
            raise
    except Exception:
        LOGGER.warning(
            "[sluice] launching the curation batch failed (persona=%s)",
            persona_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# 永続化 (判断ターンをペルソナの記憶に残す)
# ---------------------------------------------------------------------------

def _persist_record(
    persona: Any,
    record_text: str,
    prompt_snapshot: str,
    *,
    applied_total: int,
    scope_override: Optional[str] = None,
) -> None:
    """判断ターンを main_line / (committed|discardable) で SAIMemory に残す。

    採取ありなら committed (コンテキストに残る来歴)、なしなら discardable
    (DB には残るが context 復元から除外)。生 JSON は保存しない (自然文のみ)。
    ``scope_override`` はこの規則の差し替え — 後から通す採取 (本人の読み返し)
    は過程を本線の context に載せない (入口は一本 — ダイジェスト一行だけが
    committed で立つ) ため、採取ありでも 'discardable' を渡す。

    role は "user"、``record_text`` は呼び出し側で ``<system>…</system>`` に
    包んだシステム通知形式で渡る (event_message の確立形式)。プロンプト無しの
    ``role="assistant"`` メッセージは「自分は普段こう喋る」という few-shot 汚染源に
    なるため、ペルソナ発話ではなくナレーションとして残す (2026-07-07 まはー指摘)。

    書き込み失敗は握り潰さず送出する (Codex 第六巡 修正 2) — finalize 失敗として
    台帳が applied のまま残り、次回の再適用 → 再 finalize で回収される。
    """
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None:
        raise SluiceStorageUnavailableError(
            "sai_memory adapter is missing; cannot persist the judgment record"
        )

    pulse_id = None
    try:
        from tools.context import get_active_pulse_context
        pulse_ctx = get_active_pulse_context()
        pulse_id = getattr(pulse_ctx, "pulse_id", None) if pulse_ctx else None
    except Exception:
        pulse_id = None  # pulse 文脈は任意メタ — 取得失敗は記録を止めない

    adapter.append_persona_message({
        "role": "user",
        "content": record_text,
        # tz-aware UTC ISO 文字列必須 (naive だと adapter が system TZ 解釈で
        # created_at が ±9h ずれる)。
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # internal/event_message タグは機構名義の印 (storage.MECHANISM_TAGS)。
        # scene 切り出し・会話キーワード検索からは自動除外される。Chronicle
        # 編纂には 2026-08-29 裁定から材料として入る (長文は決定論の一行に縮む)。
        "metadata": {"tags": ["internal", "event_message", "sluice"]},
        "line_role": "main_line",
        "scope": scope_override or (
            "committed" if applied_total > 0 else "discardable"
        ),
        "pulse_id": pulse_id,
        "paired_action_text": prompt_snapshot,
    })


# ---------------------------------------------------------------------------
# pan マーカー永続化 (再起動を跨ぐ「前回採取した末尾 id」)
# ---------------------------------------------------------------------------
#
# マーカーはメモの span (担当範囲) の起点であり、同じ範囲を採り直したときに
# 適用を重複させないための冪等キーの土台でもある。消えると窓全体が新規扱いに
# なり採取 LLM コールが 1 回余分に走る。message id と同じ memory.db
# (embed_metadata KV) に置くことで、memory.db のリストア/差し替えでも id の
# 指す先とマーカーがずれない。
# read-through: 取得は persona 属性→無ければ永続ストア→属性にキャッシュ。
# 保存は属性と永続ストアの両方 (write-through)。

_PAN_MARKER_KEY = "sluice_last_pan_id"
#: 旧世代 (gold_panning) の永続キー。読み出しで新キーが無いときに一度だけ参照して
#: 新キーへ写す (永続データの移行であって、コード API の互換シムではない)。
_LEGACY_PAN_MARKER_KEY = "gold_panning_last_pan_id"

#: 本人モードの読み返しダイジェスト (本線の一行) の材料を、走行を跨いで貯める
#: 永続キー。値は JSON: ``{"period_start", "period_end", "messages", "memos",
#: "core", "promises", "last_chunk_id", "flush_nonce"}``
#: (:data:`_PENDING_DIGEST_COUNTS` / :data:`_PENDING_DIGEST_STRINGS`)。
#: in-memory のカウンタだけで組んでいた頃は、
#: (i) 中断 (cancelled / cooldown / 例外) した走行の採取分がどの走行の
#: ダイジェストにも入らず、(ii) 記録の縮めの後に本線への追記が落ちると、範囲の
#: 記録は消えているので再実行が noop になりダイジェストが永久に立たなかった
#: (2026-09-09 Codex 指摘)。手帳・コア記憶への書き込みには本線の痕跡が必ず残る、
#: という透明性の約束を守るために、材料を memory.db へ耐久化する。
_CAPTURE_PENDING_DIGEST_KEY = "sluice_capture_pending_digest"


def _load_pan_marker(persona: Any) -> Optional[str]:
    """pan マーカー (前回採取した末尾 message id) を取得する (read-through)。

    persona 属性にキャッシュがあればそれを返す。無ければ永続ストア (memory.db の
    embed_metadata KV) からロードし、属性にキャッシュしてから返す。

    fail-closed (Codex 第八巡 修正 5): **未存在と読み取り失敗を区別する**。
    「キーが無い」(初回 pan) は None を返すが、ストア読み出しの例外は
    :class:`SluiceStorageUnavailableError` として送出し、pan・確定・退場を
    止める — 例外を None へ丸めると一時的な読み取り障害が「初回 pan」に化け、
    ①担当範囲が窓全体に広がって処理済みの履歴を採り直し、②確定時に
    マーカーを**現在値より後ろへ書き戻す**縁ができる (マーカーは進む一方で
    なければならない)。
    """
    cached = getattr(persona, "_sluice_last_pan_id", None)
    if cached is not None:
        return cached
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot read the pan marker"
        )
    # 読みは strict 版 (Codex 第二巡 修正 A)。通常の get_embed_metadata は
    # **あらゆる** OperationalError を「テーブルがまだ無い旧 DB」とみなして None を
    # 返すので、DB ロックや I/O 障害が入口で「マーカー不在」に化け、下の
    # 包み直し (fail-closed) がそもそも発火しなかった。
    from sai_memory.memory.storage import (
        get_embed_metadata_strict,
        set_embed_metadata,
    )
    try:
        with adapter._db_lock:
            value = get_embed_metadata_strict(conn, _PAN_MARKER_KEY)
            if not value:
                # 旧世代キーからの一回きり移行 (見つかれば新キーへ写す)。
                value = get_embed_metadata_strict(conn, _LEGACY_PAN_MARKER_KEY)
                if value:
                    set_embed_metadata(conn, _PAN_MARKER_KEY, value)
    except SluiceStorageUnavailableError:
        raise
    except Exception as exc:
        # 読み取り障害はこの型に揃える (2026-09-09 Codex 指摘) — 呼び出し側は
        # 「マーカーが読めない」を一つの型で捕まえて前進・飛ばしを止められる。
        # 生の例外のままだと、捕まえる側が provider 依存の型を列挙することに
        # なり、取りこぼした型が呼び出し元まで素通りする。
        raise SluiceStorageUnavailableError(
            f"pan marker read failed: {exc}"
        ) from exc
    if value:
        persona._sluice_last_pan_id = value
    return value


def _save_pan_marker(persona: Any, last_id: str) -> None:
    """pan マーカーを永続ストアと persona 属性の両方に書く。

    **永続が先、属性は成功後** (Codex 第六巡 修正 2)。永続化の失敗は握り潰さず
    送出する — finalize 失敗として台帳が applied のまま残り、次回の再適用 →
    再 finalize で回収される。属性だけ先に進めると、確定していないのに次回の
    span 起点 (= 台帳の identity キー) が動き、記録済み結果に合流できなくなる。
    """
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot persist the pan marker"
        )
    from sai_memory.memory.storage import set_embed_metadata
    with adapter._db_lock:
        set_embed_metadata(conn, _PAN_MARKER_KEY, last_id)
    persona._sluice_last_pan_id = last_id


# ---------------------------------------------------------------------------
# 読み返しダイジェストの材料の耐久化 (走行を跨いで貯める)
# ---------------------------------------------------------------------------

#: 貯まっている材料の数の欄 (整数)。
_PENDING_DIGEST_COUNTS = ("messages", "memos", "core", "promises")
#: 貯まっている材料の文字列の欄 (無ければ None)。
#: - ``period_start`` / ``period_end``: 読み返した期間 ('YYYY-MM-DD')。
#: - ``last_chunk_id``: 最後に足したチャンクの先頭 message id。同じチャンクを
#:   やり直した回に二重加算しないための冪等キー (修正 B)。
#: - ``flush_nonce``: 本線へ立てる一行に刻む識別子。追記の前に採番して永続する
#:   ことで、「追記成功・消し込み失敗」の後の再実行が二度目の一行を立てない
#:   (修正 C)。
_PENDING_DIGEST_STRINGS = (
    "period_start", "period_end", "last_chunk_id", "flush_nonce",
)


def _empty_pending_digest() -> Dict[str, Any]:
    empty: Dict[str, Any] = {key: None for key in _PENDING_DIGEST_STRINGS}
    empty.update({key: 0 for key in _PENDING_DIGEST_COUNTS})
    return empty


def _pending_digest_conn(persona: Any):
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot access the pending "
            "capture digest"
        )
    return adapter, conn


def _load_pending_digest(persona: Any) -> Dict[str, Any]:
    """貯まっているダイジェストの材料を読む (無ければ空の器)。

    壊れた値 (JSON にならない / 辞書でない) は空として扱う — ダイジェスト一行の
    材料であって、採取そのものの成立条件ではないので、ここで走行を止めない。

    一方、**ストアが読めない**例外は送出する (Codex 第二巡 修正 A)。読めないのを
    「空」と誤認したまま :func:`_merge_pending_digest` が書き戻すと、貯まっていた
    件数がその一回で消える。呼び出し側で新たに捕まえる必要は無い — チャンク処理の
    失敗として伝播し、ジョブが failed になって再実行で回収される。
    """
    adapter, conn = _pending_digest_conn(persona)
    from sai_memory.memory.storage import get_embed_metadata_strict
    with adapter._db_lock:
        raw = get_embed_metadata_strict(conn, _CAPTURE_PENDING_DIGEST_KEY)
    if not raw:
        return _empty_pending_digest()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        LOGGER.warning(
            "[sluice-capture] pending digest value is not valid JSON; "
            "starting from an empty tally",
        )
        return _empty_pending_digest()
    if not isinstance(parsed, dict):
        return _empty_pending_digest()
    merged = _empty_pending_digest()
    for key in _PENDING_DIGEST_STRINGS:
        value = parsed.get(key)
        merged[key] = value if isinstance(value, str) and value else None
    for key in _PENDING_DIGEST_COUNTS:
        try:
            merged[key] = int(parsed.get(key) or 0)
        except (TypeError, ValueError):
            merged[key] = 0
    return merged


def _write_pending_digest(persona: Any, pending: Dict[str, Any]) -> None:
    """貯まっている材料を丸ごと書き戻す (材料の永続はこの一点に集める)。"""
    adapter, conn = _pending_digest_conn(persona)
    from sai_memory.memory.storage import set_embed_metadata
    with adapter._db_lock:
        set_embed_metadata(
            conn, _CAPTURE_PENDING_DIGEST_KEY,
            json.dumps(pending, ensure_ascii=False),
        )


def _merge_pending_digest(
    persona: Any, *, chunk_start_id: Optional[str],
    messages: int, memos: int, core: int, promises: int,
    period_start: Optional[str], period_end: Optional[str],
) -> None:
    """チャンク 1 個ぶんの適用数を、貯まっている材料へ足して書き戻す。

    ``chunk_start_id`` (そのチャンクの先頭 message id = 実行台帳の identity と
    同じ値) で**冪等**にする (Codex 第二巡 修正 B)。貯まっている材料の
    ``last_chunk_id`` と一致したら何も足さずに返る — 台帳の記録済み結果で同じ
    チャンクをやり直した回 (LLM は呼ばず適用だけ再実行する経路) が、同じ件数を
    二度足さないため。足すときは ``last_chunk_id`` を更新して書く。

    適用ゼロのチャンクは呼び出し側がそもそも呼ばない (``last_chunk_id`` は適用の
    あったチャンクだけ進む) — ゼロのチャンクのやり直しは足すものが無いので、
    冪等キーが進んでいなくても二重にはならない。
    """
    current = _load_pending_digest(persona)
    if chunk_start_id and current.get("last_chunk_id") == chunk_start_id:
        LOGGER.info(
            "[sluice-capture] chunk %s is already merged into the pending "
            "digest; skipping the tally", chunk_start_id,
        )
        return
    current["last_chunk_id"] = chunk_start_id or current.get("last_chunk_id")
    current["messages"] += int(messages or 0)
    current["memos"] += int(memos or 0)
    current["core"] += int(core or 0)
    current["promises"] += int(promises or 0)
    if period_start and (
        current["period_start"] is None or period_start < current["period_start"]
    ):
        current["period_start"] = period_start
    if period_end and (
        current["period_end"] is None or period_end > current["period_end"]
    ):
        current["period_end"] = period_end
    _write_pending_digest(persona, current)


def _clear_pending_digest(persona: Any) -> None:
    """貯まっている材料を消す (ダイジェスト一行を本線に立て終えた後だけ)。"""
    _write_pending_digest(persona, _empty_pending_digest())


def _pending_digest_total(pending: Dict[str, Any]) -> int:
    """貯まっている適用数の合計 (0 ならダイジェストを立てる材料が無い)。"""
    return (
        int(pending.get("memos") or 0) + int(pending.get("core") or 0)
        + int(pending.get("promises") or 0)
    )


# ---------------------------------------------------------------------------
# span (担当範囲) の機械刻印
# ---------------------------------------------------------------------------

def _compute_span(
    current_messages: List[Dict[str, Any]],
    prev_marker: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """このスルースの一手が担当した範囲 (span_start_id, span_end_id) を計算する。

    範囲 = 前回のパンマーカーの次のメッセージ〜今回の窓の末尾。機械が知っている
    値だけで組む — 本人の申告は使わない (§13.6)。マーカーが窓に無い (押し出されて
    消えた / 初回) ときは窓の先頭が起点。マーカーが窓の末尾と一致する (新規なし)
    ときは末尾 1 点に縮退する。
    """
    ids = [
        m.get("id") for m in current_messages
        if isinstance(m, dict) and m.get("id")
    ]
    if not ids:
        return (None, None)
    end = ids[-1]
    start = ids[0]
    if prev_marker and prev_marker in ids:
        # 複数一致は後勝ち (最新) — _count_new_since_marker と同じ規約。
        idx = len(ids) - 1 - ids[::-1].index(prev_marker)
        start = ids[idx + 1] if idx + 1 < len(ids) else end
    return (start, end)


def _message_event_date(persona: Any, message_id: Optional[str]) -> Optional[str]:
    """メッセージ id から「できごとの日」('YYYY-MM-DD') を機械で導く。

    二つの時刻の刻印 (docs/intent/sluice_coverage_gaps.md B-2) の供給源。
    採取元の範囲の**末尾メッセージ**の保存時刻 (messages.created_at) を日粒度に
    落とす — 本人・LLM に申告させない。読めない (行が無い・時刻が欠落・変換
    不能) ときは None を返し、記録は event_date NULL で書かれる (読み手は
    作成日で代替する)。刻印できない事情で採取を止めない — 時刻の欄は提示の
    並びのためのもので、採取の成立条件ではない。
    """
    if not message_id:
        return None
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None:
        return None
    try:
        with adapter._db_lock:
            row = conn.execute(
                "SELECT created_at FROM messages WHERE id = ?",
                (str(message_id),),
            ).fetchone()
    except Exception:
        LOGGER.warning(
            "[sluice] event_date lookup failed for message %s", message_id,
            exc_info=True,
        )
        return None
    if not row or row[0] is None:
        return None
    try:
        ts = int(row[0])
        if ts <= 0:
            # created_at 欠落を 0 に写した行 (native import) — 1970-01-01 の
            # 嘘を刻印しない。
            return None
        return datetime.fromtimestamp(ts).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# (旧: コンテキスト超過の後退 §13.5-1 — 2026-09-08 廃止。
#  docs/intent/sluice_coverage_gaps.md 第一段 A: 「直近の組を外して再試行」は
#  1 組はみ出したための設計で、巨大な冷たい窓には無力なまま 429 の連打だけを
#  生んだ。しかも文字列マーカー "request too large" が OpenAI の 429 TPM 超過に
#  一致してレート制限をコンテキスト超過に誤分類していた。現行は「入らない量の
#  ときは走らせない」(run_metabolism 側の量判定) に一本化し、本物の超過は
#  通常の失敗として送出 → 退場停止 → 次回再試行に乗る。)


# ---------------------------------------------------------------------------
# 構造化出力の検証 (fail-closed)
# ---------------------------------------------------------------------------

class SluiceOutputError(RuntimeError):
    """構造化出力がスキーマに適合しない。

    壊れた応答を「採取なし」へ丸めると、ゲート (§13.3) が通ったことにされて
    未採取のまま退場が進む — だから fail-closed: 送出して退場を止め、次回
    再試行に乗せる。「採取なし」と認めるのはスキーマに適合した明示的な空
    (各欄が空配列) だけ。
    """


class SluiceExecutionBlockedError(RuntimeError):
    """実行台帳が同じ担当範囲の実行をブロックしている (running / unknown 等)。"""


class SluiceStorageUnavailableError(RuntimeError):
    """記憶ストレージ (SAIMemory adapter) が未準備でスルースを実行できない。

    fail-closed: 成功扱いのスキップは**明示的な disabled だけ**。ストレージ
    未準備を skip=成功へ丸めると、採取ゼロのまま退場が進み「全経験は退場前に
    一度本人の目を通る」(v3 §13.3) が静かに破れる — 送出して退場を止める。
    """


class SluiceContextUnavailableError(RuntimeError):
    """実入力の履歴 ID 列 (presented_message_ids) が取得できない。

    見た集合 (退場の包含検算の一次データ) は _prepare_context が実際に組み込んだ
    履歴の ID 列だけを正とする (Codex 第四巡 修正 1)。取得できない = 何を見たか
    証明できないので fail-closed — 別読みの近似や「渡された窓で代用」の
    fail-open はしない。送出して退場を止め、次回再試行に乗せる。
    """


class SluiceEmptySeenSetError(RuntimeError):
    """このスルースが 1 通も見ていない (見た集合が空)。

    Codex 第八巡 修正 2: 履歴の実入力が空等で「何も見ていない」結果に
    なったとき、それを適用・確定してしまうと、マーカーは進まない
    のに台帳が completed になり、以降は**同じ安定キーの記録が永久に再利用**
    されて新しい LLM コールが起きない (退場側は空の見た集合を拒むので、末尾の
    退場が止まったままになる)。完了の条件は「最低 1 通は本人の目を通った」—
    満たさない結果は適用前に送出し、mark_failed → 次回の新しい LLM コールで
    やり直す (副作用ゼロの段なので再試行が安全)。
    """


_LIST_FIELDS = (
    "core_adds", "core_updates", "core_removes",
    "want_memos", "did_memos", "promise_adds", "promise_updates",
)

#: コア記憶の三一覧 (棄却の件数を「コア記憶の操作」へ束ねるときに使う)。
_CORE_FIELDS = ("core_adds", "core_updates", "core_removes")

#: 約束の二一覧 (棄却の件数を「約束」へ束ねるときと、旧世代判定に使う)。
_PROMISE_FIELDS = ("promise_adds", "promise_updates")

#: 判断ターン記録・ログ用の欄の呼び名 (まはーが読む面には実装名を出さない)。
_FIELD_LABELS: Dict[str, str] = {
    "core_adds": "コア記憶の追加",
    "core_updates": "コア記憶の書き換え",
    "core_removes": "コア記憶の削除",
    "want_memos": "やりたいメモ",
    "did_memos": "やったメモ",
    "promise_adds": "約束の追加",
    "promise_updates": "約束の変更",
    _PAGE_REVIEW_FIELD: "記憶ページの再編への返答",
}

#: 各欄の要素が持ちうるフィールドの実行時型 (Codex 第八巡 修正 6)。
#: 応答スキーマは型を宣言しているが、**保証はされない** (プロバイダによっては
#: 型が崩れる)。ここを通さないと ``.strip()`` などが要素単位の棄却より外側で
#: 例外になり、pan 全体が落ちる。しかも落ちるのが台帳への凍結より後だと、
#: 壊れた記録が再利用され続けて同じ例外を繰り返す — だから**凍結より前**に
#: 検査し、型の壊れた要素だけを落とす。
_ELEMENT_FIELD_TYPES: Dict[str, Dict[str, str]] = {
    "core_adds": {"content": "string"},
    "core_updates": {"memory_ref": "string", "content": "string"},
    "core_removes": {"memory_ref": "string"},
    "want_memos": {
        "activity_ref": "string", "new_activity_name": "string", "text": "string",
    },
    "did_memos": {
        "activity_ref": "string", "new_activity_name": "string", "text": "string",
    },
    "promise_adds": {"content": "string", "due": "string"},
    "promise_updates": {
        "promise_ref": "string", "content": "string", "due": "string",
        "clear_due": "boolean",
    },
    _PAGE_REVIEW_FIELD: {"op_id": "string", "verdict": "string"},
}

#: 各欄の要素の必須フィールド — :data:`_RESPONSE_SCHEMA` の items.required と
#: 同じ集合 (ずれると required の検査が嘘になる。形は schema の側が正)。
#: 欠落・null は :func:`_first_type_error` が要素棄却にする (Codex 四巡目)。
_ELEMENT_REQUIRED_FIELDS: Dict[str, tuple] = {
    "core_adds": ("content",),
    "core_updates": ("memory_ref", "content"),
    "core_removes": ("memory_ref",),
    "want_memos": ("text",),
    "did_memos": ("text",),
    "promise_adds": ("content",),
    "promise_updates": ("promise_ref",),
    _PAGE_REVIEW_FIELD: ("op_id", "verdict"),
}

_TYPE_LABELS: Dict[str, str] = {
    "string": "文字列", "integer": "整数", "boolean": "真偽値",
}


def _type_matches(value: Any, expected: str) -> bool:
    """``value`` が応答スキーマの宣言型 ``expected`` に合うか。

    JSON の boolean は Python では int の派生なので、integer 判定では明示的に
    除外する (True が 1 として通ると activity_id=True のような値が生き残る)。
    """
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def _first_type_error(field: str, item: Dict[str, Any]) -> Optional[str]:
    """要素の中で最初に見つかった形の不正の説明。問題が無ければ None。

    **必須欄** (応答スキーマの required と同じ集合) の欠落・null は要素の
    形の不正として拾う (Codex 四巡目) — 「未指定」へ丸めて適用側の要素棄却に
    落とすと、凍結後の破損検出 (:func:`_recorded_response_unusable` の
    「再読で棄却が出る = 破損」) がこの形だけ素通りし、壊れた記録が採り直され
    ないまま completed になる。**任意欄**の省略と null は従来どおり未指定として
    通し、値があるのに型が違うものだけを拾う。
    """
    for name in _ELEMENT_REQUIRED_FIELDS.get(field, ()):
        if item.get(name) is None:
            return f"必須欄 {name} がありません"
    for name, expected in _ELEMENT_FIELD_TYPES.get(field, {}).items():
        if name not in item:
            continue
        value = item[name]
        if value is None:
            continue
        if not _type_matches(value, expected):
            return (
                f"{name} が{_TYPE_LABELS.get(expected, expected)}ではありません "
                f"({value!r})"
            )
    return None


def _parse_structured_result(
    result: Any, persona_id: Optional[str],
    *,
    page_review_op_ids: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """LLM の構造化出力を検証済み dict へ正規化する。不適合は SluiceOutputError。

    Returns:
        ``(検証済みの応答, 棄却した要素の記録)``。棄却の記録は
        ``{"field": 欄名, "text": 判断ターンに残す一行}`` の列。

    検査の基準は**その回に要求した欄の集合**: 常に要求する 8 欄に加えて、
    記憶の手入れの候補を見せた回 (``page_review_op_ids`` が非空) は
    ``page_reviews`` も必須の配列として同じ規則で検査する。要求していない回に
    ``page_reviews`` が来たら WARNING を出して欄ごと落とす (応答全体は棄却
    しない — 見せていない提案への返答には適用先が無いだけで、他の欄は正しい)。
    op_id の値が候補にあるかは適用側 (:func:`_apply_page_reviews`) が見る。

    fail-closed の粒度: **全体の型** (dict でない / 必須欄 — reflection と
    7 つの操作列全部 — の欠落・null / 各欄が配列でない / 配列要素が object で
    ない / reflection が文字列でない) は送出。
    **要素内フィールドの型不正** (content や memory_ref が文字列でない等) は
    その要素だけ落として棄却の記録に残す (Codex 第八巡 修正 6)。
    **中身の参照・値の不正** (空本文、``core:N`` / ``act:N`` / ``promise:N``
    の形でない参照、一覧に無い参照、変更内容の無い約束の変更、解釈不能な
    due) は従来どおり適用側の要素単位棄却に委ねる。
    """
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError) as exc:
            raise SluiceOutputError(
                f"structured output is not JSON (persona={persona_id}): {result[:200]!r}"
            ) from exc
        result = parsed
    if not isinstance(result, dict):
        raise SluiceOutputError(
            f"structured output is not an object (persona={persona_id}): "
            f"{type(result).__name__}"
        )
    # 旧世代の欄が混ざった応答は fail-closed で全体棄却する — 適用側は現行の
    # 欄しか読まないので、旧欄に入った操作は黙って失われる (schema は
    # 旧欄を含まないが、制約の緩いプロバイダは未知キーを出力しうる)。
    # additionalProperties での禁止は Gemini が受け付けないため、関所は
    # ここに置く。台帳の旧記録は :func:`_is_legacy_response` が別途弾く。
    for legacy_field in ("ops", "promises"):
        if legacy_field in result:
            raise SluiceOutputError(
                f"legacy field {legacy_field!r} present in structured output "
                f"(persona={persona_id}); rejecting to avoid silent loss"
            )
    # 全欄必須 (Codex 第七巡 修正 1): 欄の省略・null を「採取なし」へ丸めない。
    # 「空」と認めるのは明示的な空配列だけ。
    reflection = result.get("reflection")
    if not isinstance(reflection, str):
        raise SluiceOutputError(
            f"required field 'reflection' is missing or not a string "
            f"(persona={persona_id}): {reflection!r}"
        )
    sanitized: Dict[str, Any] = dict(result)
    rejections: List[Dict[str, str]] = []
    requested_fields: Tuple[str, ...] = _LIST_FIELDS
    if page_review_op_ids:
        requested_fields = (*_LIST_FIELDS, _PAGE_REVIEW_FIELD)
    elif _PAGE_REVIEW_FIELD in result:
        LOGGER.warning(
            "[sluice] %s present although no page review was offered "
            "(persona=%s); ignoring the field", _PAGE_REVIEW_FIELD, persona_id,
        )
        sanitized.pop(_PAGE_REVIEW_FIELD, None)
    for field in requested_fields:
        value = result.get(field)
        if not isinstance(value, list):
            raise SluiceOutputError(
                f"required field {field!r} is missing or not an array "
                f"(persona={persona_id}): {type(value).__name__}"
            )
        kept: List[Any] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise SluiceOutputError(
                    f"element {field}[{index}] must be an object "
                    f"(persona={persona_id}): {type(item).__name__}"
                )
            type_error = _first_type_error(field, item)
            if type_error is not None:
                label = _FIELD_LABELS.get(field, field)
                rejections.append({
                    "field": field,
                    "text": f"{label}の{index + 1}件目を棄却: {type_error}。",
                })
                LOGGER.warning(
                    "[sluice] dropped %s[%d]: %s (persona=%s)",
                    field, index, type_error, persona_id,
                )
                continue
            kept.append(item)
        sanitized[field] = kept
    return sanitized, rejections


# ---------------------------------------------------------------------------
# 実行台帳 (execution ledger) — 再試行の LLM 重複と適用重複を塞ぐ
# ---------------------------------------------------------------------------
#
# ゲート化 (§13.3) で「部分適用 → 失敗 → 次回 Metabolism で再処理」が正規の
# 経路になった。再処理が新しい LLM コールをすると、結果が揺れて前回の部分適用と
# 重複する — だから **LLM の構造化結果そのものを台帳 (RESULT_JSON) に記録し、
# 同じ担当範囲の再処理は記録済み結果を再利用して適用だけやり直す**。
#
# 実行の identity は (persona, span_start_id)。担当範囲の起点はパンマーカーで
# 決まり、マーカーは成功時にしか進まないので、同じ論理単位の再試行は必ず同じ
# 起点を持つ。終端 (span_end) を identity に含めないのは意図的 — 終端は
# 退場が止まっている間に窓へ新着が積まれることで試行ごとに動き、
# キーに含めると「同じ仕事の再試行」が別キーになって
# 記録済み結果に合流できない (= 重複の穴が戻る)。実際に見た終端は identity
# ではなく記録の中身 (result.span_end_id) が持つ。
#
# 状態の使い方 (saiverse/execution_ledger.py の状態機械に素直に乗せる):
# - claim → try_mark_running → LLM 呼び出し
# - LLM 成功 + 出力検証通過 → mark_applied(result=構造化結果 + span + 同梱一覧)
#   (適用ステップは冪等 (span 由来 idem_key・内容一致ガード) なので、ここが
#   「不可逆な実行 = LLM コール」の確定点)
# - 適用まで完走 → mark_completed / LLM・検証の失敗 → mark_failed (適用前の
#   検証棄却 — 副作用ゼロなので claim がキーを退避して次回の新実行を許す)
# - 適用の途中で失敗 → applied のまま残す → 次回が結果を再利用
# - claim が running / unknown を返したら SluiceExecutionBlockedError (unknown は
#   台帳の大原則どおり自動再実行しない — list_unknown での裁定待ち)

_LEDGER_KIND = "sluice.pan"


#: 応答形式の世代印。旧世代の記録 (``ops`` 一本 = 2026-08-24 まで、
#: ``promises`` 一本 = 2026-09-28 まで) が同じ担当範囲に残っている環境で、
#: 新しい実行を**別キー**に立てるために使う。台帳は applied → failed の
#: 遷移を許さない (状態機械の規約) ので、読めない旧行は退避も上書きもせず
#: そのまま残し、こちらが別キーで走り直す。
_RESPONSE_FORMAT_TAG = "promise2"


def _get_ledger(lifecycle: Any) -> Optional[Any]:
    """manager 所有の ExecutionLedger を引く。無ければ None (台帳なしで動く)。"""
    manager = getattr(lifecycle, "manager", None)
    if manager is None:
        return None
    return getattr(manager, "execution_ledger", None)


def _is_legacy_response(response: Any) -> bool:
    """記録済み結果が旧世代 (``ops`` 一本 / ``promises`` 一本) の応答形式か。

    現行形式はコア記憶の三一覧 (``core_adds`` / ``core_updates`` /
    ``core_removes``) と約束の二一覧 (``promise_adds`` / ``promise_updates``)
    を必ず全部持つ — :func:`_parse_structured_result` が凍結より前に全欄必須で
    検証しているため。旧形式をそのまま適用側へ渡すと欠けた一覧が空として
    読まれ、「採取ゼロ」で completed になる (本人が指定したコア記憶や約束の
    操作が静かに消える)。だから再利用せず、新しい LLM コールでやり直す
    (fail-closed)。
    """
    if not isinstance(response, dict):
        return True
    if "ops" in response or "promises" in response:
        return True
    return not all(
        field in response for field in (*_CORE_FIELDS, *_PROMISE_FIELDS)
    )


def _recorded_response_unusable(
    response: Any, persona_id: Optional[str],
    *,
    page_review_op_ids: Optional[List[str]] = None,
) -> bool:
    """記録済み応答が再適用に使えない形か (旧世代、または凍結後の破損)。

    旧世代 (:func:`_is_legacy_response`) に加えて、現行の欄は揃っているのに
    形が壊れている記録 (欄が null・配列でない等) も「使えない」と判定する —
    凍結の前には :func:`_parse_structured_result` の検証があるので正規の書き手
    からは生まれないが、凍結後の破損・版ずれの記録を再適用側の寛容な読み
    (:code:`_as_list` は非配列を空へ丸める) に通すと、**採取ゼロのまま
    completed になり、本人が指定した操作が静かに失われる** (Codex 二巡目)。
    使えない記録は旧世代と同じ扱い — 再利用せず、別キーの新しい LLM コールで
    採り直す。

    要素単位の型不正 (content が int 等) も「使えない」と判定する (Codex
    三巡目): 凍結されるのは検証済み (sanitized) の応答なので、再読で棄却が
    一件でも出る = 凍結後の破損。生の記録をそのまま適用側へ流すと、適用側の
    ``.strip()`` 等が非文字列で例外化し、applied の行が残ったまま毎回同じ
    クラッシュを繰り返す (LLM 呼び出しの失敗処理の外なので mark_failed も
    走らない)。

    ``page_review_op_ids`` はその記録の回に見せた記憶の手入れの候補の op_id
    (記録の ``page_review_candidates``)。凍結時と同じ「要求した欄の集合」で
    検査する — 候補を見せた回の記録は page_reviews 欄を持つはずで、無ければ
    破損。候補の欄が無い旧記録 (2026-10-09 より前) は空 = 提案ゼロの回。
    """
    if _is_legacy_response(response):
        return True
    try:
        _sanitized, rejections = _parse_structured_result(
            response, persona_id, page_review_op_ids=page_review_op_ids,
        )
    except SluiceOutputError:
        return True
    return bool(rejections)


def _recorded_result_unusable(
    recorded: Dict[str, Any], persona_id: Optional[str],
) -> bool:
    """記録済み結果 (:func:`_find_recorded_result` の返り値) が再適用に使えないか。

    応答本体の検査 (:func:`_recorded_response_unusable`) に加えて、約束の
    対応表の破損 (:func:`_restore_offered_tasks` が None を返した = 記録の
    ``offered_tasks`` が読めない) も「使えない」に含める — 応答が正常でも
    対応表が壊れていれば、凍結済みの約束の変更は要素棄却で失われたまま
    completed になる (Codex 三巡目)。アクティビティ一覧 (三巡目の同族) と、
    見た集合 ``seen_ids`` の欠落・空 (Codex 六巡目 — 定常・読み返しとも
    書き手は非空を凍結する。再適用の分岐で送出すると applied の行が残った
    まま毎回同じ例外になり自動回復しないので、ここで「使えない」に含めて
    別キーの採り直しに乗せる) も同じ。記憶の手入れの候補 (欄があるのに
    読めない ``page_review_candidates``) も同じ扱い。
    """
    page_review_candidates = recorded.get("page_review_candidates")
    if page_review_candidates is None:
        return True
    if _recorded_response_unusable(
        recorded.get("response"), persona_id,
        page_review_op_ids=_page_review_op_ids(page_review_candidates),
    ):
        return True
    if recorded.get("offered_tasks") is None:
        return True
    if recorded.get("offered_activities") is None:
        return True
    seen_ids = recorded.get("seen_ids")
    return not isinstance(seen_ids, list) or not seen_ids


def _restore_offered_tasks(raw: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """台帳に凍結した約束の対応表 (:func:`_offered_task_map` の形) を読み戻す。

    読めるのは ``{"N": {"task_id": str, "revision": int, ...}}`` の形だけ。
    それ以外は **None (= 対応表の破損)** を返す (Codex 三巡目): 破損した
    対応表で再適用すると、応答に凍結済みの約束の変更が「一覧にありません」
    「スナップショット情報が無い」の要素棄却で失われたまま completed になり、
    採り直しの機会が二度と来ない。None を受けた呼び出し側は記録ごと
    「使えない」として別キーの採り直しへ回す。破損と判定するもの:
    dict でない / 要素が dict でない / ``task_id`` が非空文字列でない /
    ``revision`` が int でない (欠落・None を含む — 正規の書き手は常に int を
    凍結する。None は update_entry で「省略 = 読み直し CAS」に化けるため
    受けない)。

    ``due_at`` の欠落・不正だけは破損ではなく :data:`_DUE_AT_UNKNOWN` へ倒す —
    None は「一覧の時点で期限なし」の主張で clear_due の空振り成功の根拠に
    なるため丸めないが、「不明」でも変更自体は実際の update_entry + CAS へ
    進めて完全に適用できる (失われる操作が無い) ので、記録ごと捨てる理由に
    ならない。``content`` も同じ理由で不正は None (表示ラベルにしか使わない)。
    """
    if not isinstance(raw, dict):
        return None
    restored: Dict[str, Dict[str, Any]] = {}
    for position, snapshot in raw.items():
        # 鍵は正典の書き手 (enumerate 1 始まり) が作る十進表記そのもの。
        # "01" のような別表記は、応答の promise:1 が引く "1" と一致せず
        # 「一覧にありません」の静かな喪失になるので破損扱い (Codex 四巡目)。
        # 桁数上限は参照 (_PROMISE_REF_RE) と同じ 9 桁。
        if not isinstance(position, str) or not re.fullmatch(
            r"[1-9][0-9]{0,8}", position,
        ):
            return None
        if not isinstance(snapshot, dict):
            return None
        task_id = snapshot.get("task_id")
        # 前後に空白の付いた ID はタスク帳の完全一致検索に当たらず、検査を
        # 通したのに適用で失敗する — 正準 (strip 済みと同一) だけ受ける。
        if (
            not isinstance(task_id, str)
            or not task_id
            or task_id != task_id.strip()
        ):
            return None
        revision = snapshot.get("revision")
        # 保存側の不変条件は非負の int (REVISION は NOT NULL default 0)。
        if not (
            isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
        ):
            return None
        due_at = snapshot.get("due_at", _DUE_AT_UNKNOWN)
        if due_at is not None and not (
            isinstance(due_at, int) and not isinstance(due_at, bool)
        ):
            due_at = _DUE_AT_UNKNOWN
        content = snapshot.get("content")
        restored[str(position)] = {
            "task_id": task_id,
            "revision": revision,
            "due_at": due_at,
            "content": content if isinstance(content, str) else None,
        }
    return restored


def _restore_offered_activities(raw: Any) -> Optional[Dict[int, str]]:
    """台帳に凍結したアクティビティ一覧 ``{id: name}`` を読み戻す。

    鍵は凍結時に ``str(id)`` で書かれるので、十進の非負整数表記だけを int へ
    戻す。それ以外の鍵・str でない名前は **None (= 記録の破損)** — 約束の
    対応表 (:func:`_restore_offered_tasks`) と同じ理由で、破損した一覧で
    再適用するとメモの採取が要素棄却で失われたまま completed になるか、
    鍵の ``int()`` が例外化して applied の行が残ったまま毎回クラッシュする
    (Codex 三巡目・四巡目の同族を自前で掃いた分)。
    """
    if not isinstance(raw, dict):
        return None
    restored: Dict[int, str] = {}
    for key, name in raw.items():
        # 凍結形式は str(id) の正準十進表記だけ。"01" のような別表記を int へ
        # 正規化すると "1" と衝突して別の活動の名前を上書きできるため、
        # 正準でない鍵は破損として記録ごと採り直す (Codex 五巡目)。正準性は
        # パターンで判定する (先頭ゼロ無し + 桁数上限) — str(int(key)) の
        # 比較は 4300 桁超の鍵で int() 自体が ValueError になり、None を
        # 返せずクラッシュループに化ける (Codex 六巡目)。
        if not (isinstance(key, str) and re.fullmatch(r"0|[1-9][0-9]{0,8}", key)):
            return None
        if not isinstance(name, str):
            return None
        restored[int(key)] = name
    return restored


def _find_recorded_result(ledger: Any, ledger_key: str) -> Optional[Dict[str, Any]]:
    """同じ担当範囲の記録済み結果 (applied / completed) を探す。

    見つかれば ``{execution_id, status, response, span_start_id, span_end_id,
    seen_ids, rejections, offered_activities, offered_tasks, core_snapshot,
    prompt}`` を返す。**記録が無ければ** None。``offered_tasks`` は
    :func:`_restore_offered_tasks` の読み戻しで、**破損なら None** — 呼び出し
    側は :func:`_recorded_result_unusable` で記録ごと採り直しへ回す。

    fail-closed (第八巡 修正 5 の同族): 台帳の読み出し例外は「記録なし」へ
    丸めず送出する。丸めると、記録があるのに無いものとして扱って新しい LLM
    コールへ進み (課金と結果の揺れ)、その先の claim が既存行に当たって
    ブロック例外になる — 失敗の顔が本当の原因 (台帳が読めない) から遠ざかる。
    """
    existing = ledger.find_execution(_LEDGER_KIND, ledger_key)
    if not existing:
        return None
    status = existing.get("status")
    result = existing.get("result")
    if status in ("applied", "completed") and isinstance(result, dict) \
            and isinstance(result.get("response"), dict):
        offered_tasks = _restore_offered_tasks(result.get("offered_tasks"))
        return {
            "execution_id": existing.get("execution_id"),
            "status": status,
            "response": result.get("response"),
            "span_start_id": result.get("span_start_id"),
            "span_end_id": result.get("span_end_id"),
            "seen_ids": result.get("seen_ids"),
            # list でない棄却記録 (破損) は空へ — 落ちるのは情報の行だけで
            # 操作は失われないため、記録ごと捨てる理由にならない。int 等を
            # そのまま返すと再適用側の内包表記が TypeError でクラッシュ
            # ループになる (Codex 五巡目)。
            "rejections": (
                result["rejections"]
                if isinstance(result.get("rejections"), list) else []
            ),
            "offered_activities": _restore_offered_activities(
                result.get("offered_activities")
            ),
            "offered_tasks": offered_tasks,
            "core_snapshot": result.get("core_snapshot"),
            "prompt": result.get("prompt"),
            # 欄の無い旧記録 (2026-10-09 より前・後から通す採取の記録) は
            # 提案ゼロの回として再生する。欄があって読めなければ None (破損)。
            "page_review_candidates": _restore_page_review_candidates(
                result.get("page_review_candidates", [])
            ),
        }
    return None


def _call_sluice_llm(
    lifecycle: Any,
    persona: Any,
    building_id: str,
    span_end_full: Optional[str],
    span_new_count: Optional[int],
    window_anchor_id: Optional[str] = None,
    model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """LLM 呼び出しフェーズ (context 組み → generate → usage → 検証)。

    Returns:
        ``{"response": 検証済み dict, "rejections": 型不正で落とした要素の記録,
        "span_end_id": 実際に見た範囲の末尾 (None =
        特定不能), "seen_ids": 実際に LLM 入力に含めたメッセージ ID の列
        (**必ず 1 件以上** — 空なら SluiceEmptySeenSetError),
        "offered_activities": {id: name},
        "offered_tasks": {"N": {task_id, revision, due_at, content}}
        (:func:`_offered_task_map` — N は約束一覧の位置),
        "core_snapshot": {core_id: 本文ハッシュ (スナップショット時点)},
        "prompt": 注入プロンプト,
        "page_review_candidates": この回に見せた記憶の手入れの候補 (無ければ空)}``

    例外 (LLM エラー・出力不適合) はそのまま送出する — 呼び出し元が台帳の
    mark_failed とゲート失敗 (退場停止) に写像する。送る中身が実際に使う
    モデルに入らないときは LLM を呼ばずに :class:`SluiceInputTooLargeError` を
    送出する (run_metabolism はこれを量による飛ばしと同じ扱いにする)。
    """
    runtime = lifecycle.runtime
    persona_id = getattr(persona, "persona_id", None)

    # model は呼び出し元 (run_metabolism) が窓を撮ったのと同じ model_key に
    # 揃える (Codex 2026-08-24 #2): 窓と畳み (folds) と anchor 行は
    # (persona, model) ごとなので、ここで別 model に解決すると退場計画の窓と
    # スルース入力が別物になり、prefix の凍結 (pinned_anchor_id) も別 Session
    # の起点を凍結する誤りになる。窓取得・畳み適用・hot 判定・prefix 組成・
    # LLM 呼び出しの全部がこの一つの model_key で揃う。
    # intent gold_panning §5-7「lightweight への分岐は書かない」は tier 分岐の
    # 禁止 (判断の質を軽量 tier へ落とさない) であって、セッションの model へ
    # の統一とは別問題 (§5-7 の注記参照)。model_key が来ない互換経路
    # (直接呼び) だけ従来どおり standard tier を解決する。
    # Beat 相当の開始点 — Pulse 外なので pulse_context=None (beat_execution_context §2.1)。
    # _prepare_context より先に解決するのは、head を同じ model の Session
    # (persona, model) に向けて render するため (§3.1)。
    from sea.pulse_context import resolve_execution_context
    execution_context = resolve_execution_context(persona, None)
    if model_key and execution_context.model_key != model_key:
        execution_context = execution_context.with_model(model_key)

    # メインラインと同じ context を組み、末尾に注入プロンプトを 1 つ足す。
    # 直前の応答コールで prefix が温まっている前提 (defer-to-hot が保証)。
    # 起点の凍結 (window_anchor_id → pinned_anchor_id): 呼び出し元
    # (run_metabolism) が実行頭に撮った窓の起点をそのまま使い、組成中の起点
    # 前進 (§14-2 機構1) を判定ごと走らせない — 「一回の整理は一つの一貫した
    # 窓で最後まで走る」(2026-08-24 まはー裁定)。前回の会話 prefix はこの
    # 起点で組まれているので、凍結は温まった prefix を守る方向でもある
    # (実行中の前進はむしろ prefix を変えてキャッシュを壊していた)。
    # context_meta: 今回の prefix の anchor を call-local で受け取る (§3.2)。
    context_meta: Dict[str, Any] = {}
    context_messages = list(runtime._prepare_context(
        persona, building_id, None, model_key=execution_context.model_key,
        context_meta=context_meta,
        # コア記憶・手帳の採取はメインラインへの一手 = ペルソナ本人の判断として残る。
        persona_voiced=True,
        pinned_anchor_id=window_anchor_id,
    ) or [])
    # 見た集合の一次情報 (Codex 第四巡 修正 1): _prepare_context が**実際に
    # プロンプトへ組み込んだ**履歴メッセージの ID 列を out-param で受け取る。
    # 起点を凍結しない呼び出し (window_anchor_id=None の互換経路) では anchor
    # 前進も、この列が組成の実体から来るので自然に映る。
    # 取得できない (履歴構築の失敗・契約を満たさない代替実装) は fail-closed —
    # 別読みの近似や「渡された窓で代用」の fail-open はしない。
    presented_ids_raw = context_meta.get("presented_message_ids")
    if not isinstance(presented_ids_raw, list):
        raise SluiceContextUnavailableError(
            f"presented_message_ids missing from context preparation "
            f"(persona={persona_id}); cannot establish the seen set"
        )
    presented_ids = [str(message_id) for message_id in presented_ids_raw]
    activities = _list_open_activities(persona)
    offered_activities = dict(activities)
    open_tasks = _list_open_tasks(lifecycle, persona)
    # 約束は一覧の位置 N → スナップショット (task_id と、CAS 用の revision —
    # 実行中のユーザー編集へ黙って上書きしないための照合値)。
    offered_tasks = _offered_task_map(open_tasks)
    # コア記憶の現況を一度読み、プロンプト同梱と CAS スナップショット
    # (id → 本文ハッシュ。Codex 第七巡 修正 2 — タスク帳 CAS の同族) の両方に使う。
    core_memories, core_total_chars = _read_core_state(persona)
    core_snapshot: Dict[str, str] = {
        str(mem.id): _core_content_hash(mem.content) for mem in core_memories
    }
    # 今日すでに手帳に書いたもの (本人がスペルで書いた分を含む) — 同じ日の
    # 再採取を減らすため、アクティビティ一覧と同じ読みの配下から一度で取る。
    today_memos = _list_today_memos(persona, activities)
    # 記憶の手入れの候補 (業務日に一回だけ)。最後に提示した業務日の確認と
    # 検知はここで行い、提示の記録は**応答が読めた後** (下の
    # _parse_structured_result の後) に書く — 入らずに飛ばされた回も、LLM が
    # 失敗した回も、本人は候補を見ていないので「提示した」に数えない
    # (2026-10-09 ローカルレビュー指摘 3: 見る前に記録が進むと、失敗した日の
    # 候補が誰にも見られないまま翌業務日まで闇に落ちる)。
    page_review_candidates, page_review_day = _offer_page_reviews(lifecycle, persona)

    def _compose(candidates: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        return (
            _build_sluice_prompt(
                persona, activities, open_tasks, core_memories, core_total_chars,
                span_new_count=span_new_count, today_memos=today_memos,
                page_review_candidates=candidates or None,
            ),
            _build_response_schema(_page_review_op_ids(candidates)),
        )

    prompt, response_schema = _compose(page_review_candidates)

    node_def = SimpleNamespace(id="sluice", memorize=None, speak=False)
    llm_client, _sluice_model = runtime.select_llm_client(
        node_def, persona, execution_context=execution_context,
        needs_structured_output=True,
    )
    if _sluice_model != execution_context.model_key:
        # structured-output fallback で実 model が変わった場合の差し替え
        execution_context = execution_context.with_model(_sluice_model)

    # LLM 呼び出しは一発 (2026-09-08、docs/intent/sluice_coverage_gaps.md
    # 第一段 A)。旧 §13.5-1 の後退方式 (超過エラーで直近の組を外して再試行) は
    # 廃止した — 入らない量の窓はそもそも呼び出し元 (run_metabolism) の量判定が
    # 走らせない。本物のコンテキスト超過を含むあらゆる失敗は送出し、呼び出し元の
    # 退場停止 → 次回再試行に乗る。
    messages = context_messages + [{"role": "user", "content": prompt}]
    # 送る中身がそのモデルに入るか (docs/issues/sluice_skip_ignores_model_context.md)。
    # 比べるのは実際に使うモデル (構造化出力の都合で差し替わった後) の上限で、
    # 応答の枠はそのクライアントが送る上限、答えの形の指定も入力に数える。
    # 入らなければ LLM を呼ばずに送出する — run_metabolism は量による飛ばしと
    # 同じ扱いにする (範囲を記録して畳みを進める)。
    try:
        _ensure_input_fits(
            messages, execution_context.model_key, persona_id=persona_id,
            llm_client=llm_client, response_schema=response_schema,
        )
    except SluiceInputTooLargeError:
        if not page_review_candidates:
            raise
        # 手入れの提案を足したせいで入らない回は、提案を外して採取を通す —
        # 提案の同梱で採取の飛ばし (SluiceInputTooLargeError) を増やさない。
        # 提示の記録は書かないので、提案は同じ業務日の後の回で見せられる。
        LOGGER.info(
            "[sluice] the page review offer does not fit the model context; "
            "running without it (persona=%s)", persona_id,
        )
        page_review_candidates, page_review_day = [], None
        prompt, response_schema = _compose(page_review_candidates)
        messages = context_messages + [{"role": "user", "content": prompt}]
        _ensure_input_fits(
            messages, execution_context.model_key, persona_id=persona_id,
            llm_client=llm_client, response_schema=response_schema,
        )
    result = llm_client.generate(
        messages,
        tools=[],
        response_schema=response_schema,
        temperature=runtime._default_temperature(persona),
        # このコールだけの出力上限 (per-call)。対応していない
        # プロバイダのクライアントは generate の **kwargs が
        # 黙って落とす — 上限が効かないだけで、例外にはしない。
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        **runtime._get_cache_kwargs(persona_id),
    )

    # usage 記録 + anchor touch (keepalive の後処理と同じ)。
    usage = llm_client.consume_usage() if hasattr(llm_client, "consume_usage") else None
    if usage is not None:
        try:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                building_id=building_id,
                node_type="sluice",
                playbook_name="sluice",
                category="sluice",
            )
        except Exception:
            LOGGER.warning("[sluice] usage tracking failed (persona=%s)", persona_id, exc_info=True)
        try:
            lifecycle.touch_anchor_after_llm_call(
                persona, usage, anchor_id=context_meta.get("prefix_anchor_id"),
            )
        except Exception:
            LOGGER.warning("[sluice] anchor touch failed (persona=%s)", persona_id, exc_info=True)

    # 構造化出力の検証 (fail-closed — 壊れた応答を「採取なし」へ丸めない)。
    # 要素内フィールドの型検査もここ = **台帳への凍結より前**で行う
    # (Codex 第八巡 修正 6: 壊れた要素を凍結しないので、再適用が同じ例外を
    # 繰り返す縁ができない)。
    parsed, rejections = _parse_structured_result(
        result, persona_id,
        page_review_op_ids=_page_review_op_ids(page_review_candidates),
    )
    # 応答が読めた = 本人が候補を見て答えた。ここで初めて「提示した」を記録する
    # (台帳への凍結より前 — 凍結後の再適用の回は、最初の回が既に記録している)。
    # 書けなかったときは WARNING だけ出して進む: 同じ業務日にもう一度見せて
    # しまうことは許す (承認の二重は予約の冪等で無害。見せ損ないより軽い)。
    if page_review_candidates and page_review_day is not None:
        if not _mark_page_reviews_offered(persona, str(page_review_day)):
            LOGGER.warning(
                "[sluice] page review offer succeeded but the day could not be "
                "recorded; the offer may repeat within the same business day "
                "(persona=%s)", persona_id,
            )

    # 見た集合 = 実入力の履歴 ID 列そのもの (後退方式の廃止で件数の引き算は
    # 無くなった)。退場側の包含検算 (_eviction_within_seen) の一次データ。
    seen_ids = list(presented_ids)
    if not seen_ids:
        # 1 通も見ていない結果は完了させない (Codex 第八巡 修正 2)。凍結すると
        # マーカー据え置きのまま completed になり、以後は同じ記録が再利用されて
        # LLM が二度と走らず、末尾の退場が永久に止まる。
        raise SluiceEmptySeenSetError(
            f"the sluice saw no messages (persona={persona_id}, "
            f"presented={len(presented_ids)}); "
            "refusing to freeze a result that saw nothing"
        )

    return {
        "response": parsed,
        "rejections": rejections,
        "span_end_id": span_end_full,
        "seen_ids": seen_ids,
        "offered_activities": offered_activities,
        "offered_tasks": offered_tasks,
        "core_snapshot": core_snapshot,
        "prompt": prompt,
        "page_review_candidates": list(page_review_candidates),
    }


# ---------------------------------------------------------------------------
# エントリ関数
# ---------------------------------------------------------------------------

def run_sluice(
    lifecycle: Any,
    persona: Any,
    building_id: str,
    current_messages: List[Dict[str, Any]],
    evict_count: int,
    event_callback: Optional[Any] = None,
    *,
    run_id: Optional[str] = None,
    finalize: bool = True,
    window_anchor_id: Optional[str] = None,
    model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """スルース本体。押し出し直前のメインライン prefix に 1 手足して採取を判断させる。

    例外は送出する — 隔離しない。呼び出し元 (run_metabolism) はスルースの失敗で
    退場を止め、次の Metabolism 機会に再試行する (§13.3 の「確実に通るゲート」)。

    確定の二段階化 (Codex 第五巡 修正 2): 適用 (コア記憶・メモ・約束) までは
    本体が行い、**確定 (台帳の mark_completed + 判断ターン記録の永続 + 完了通知 +
    パンマーカー前進)** は返り値の ``finalize`` クロージャに割ってある。
    ``finalize=True`` (既定) は本体内で即確定する (Memory 窓からの手動生成など
    直接呼びの経路)。``finalize=False`` の呼び出し元
    (run_metabolism) は、マーカー前進が未提示メッセージを跨がないことを検算して
    からクロージャを呼ぶ — 確定を保留した回の記録は台帳に applied のまま残り、
    次回は記録の再適用から入り直す (適用は冪等なので重複しない)。

    Args:
        lifecycle: SessionLifecycle インスタンス (lifecycle.runtime で SEARuntime)。
        current_messages: run_metabolism の手元の提示窓。
        evict_count: 今回押し出される件数 (ログ用。退場は episode 単位になったので
            窓の先頭からの連続とは限らない — chronicle_eviction.md §5)。
        run_id: スルース実行 ID (冪等キーの種)。省略時は乱数採番。テストと
            再適用検証用に注入可能にしてある。
        window_anchor_id: ``current_messages`` を撮った窓の起点。渡されると
            スルースのプロンプト組成はこの起点に**凍結**され、組成中の起点
            前進 (§14-2 機構1) は判定ごと走らない — 退場計画の土台とスルース
            入力が同じ窓になる (2026-08-24 まはー裁定「一回の整理は一つの
            一貫した窓で最後まで走る」)。凍結起点で組めなければ
            :class:`~sea.runtime_context.PinnedAnchorUnavailableError` が
            送出され、退場停止に乗る (通常解決へのフォールバック禁止)。
            None は互換経路 (窓が起点を持たないブートストラップ等) で、
            従来どおり組成側が起点を解決する。**契約: 非空の
            ``current_messages`` を渡す呼び出し元は必ず起点も渡すこと** —
            渡さないと組成側の解決 (§14-2 前進つき) が復活し、退場計画の
            土台とスルース入力が別々の窓になる (run_metabolism は関所で
            この形を "failed" に落とす)。
        model_key: 呼び出し元 (run_metabolism) が窓を撮った model。プロンプト
            組成 (head / 畳み / prefix) と LLM 呼び出しをこの model の
            Session に揃える。None は互換経路で standard tier を解決する。

    Returns:
        {"ops_applied": int, "ops_failed": int,
         "memos_applied": int, "memos_failed": int,
         "promises_applied": int, "promises_failed": int,
         "pages_approved": int, "pages_skipped": int, "pages_failed": int,
         "skipped": bool, "reason": str|None,
         "seen_span_end": str|None, "seen_ids": list[str]|None}

        ``pages_*`` は記憶の手入れの返答 (承認して予約した数 / 見送った数 /
        予約に失敗した数)。候補を見せていない回は全て 0。

        ``seen_ids`` は**このスルースが実際に LLM 入力に含めたメッセージ ID の
        集合** (記録済み結果の再適用ではその記録の集合)。呼び出し元
        (run_metabolism) は退場計画の対象 ID 全件がここに含まれるかを検算する —
        欠けがあれば退場は見送り (§13.3 の不変条件: 全経験は退場前に一度本人の
        目を通る)。None は skipped (disabled) だけで、そのとき退場は「採取なしで
        忘れる」設計どおり進む。``seen_span_end`` は見た範囲の末尾 (パンマーカー
        と同じ値 — 観測・テスト用)。

        skipped=True で返るのは**明示的な disabled だけ**。ストレージ未準備は
        :class:`SluiceStorageUnavailableError` を送出する (fail-closed)。
        1 通も見ないまま終わった回は :class:`SluiceEmptySeenSetError` を送出する
        (Codex 第八巡 修正 2 — 「最低 1 通は本人の目を通った」が完了の条件)。
        送る中身が使うモデルに入らない回は LLM を呼ばずに
        :class:`SluiceInputTooLargeError` を送出する (台帳は failed。呼び出し元の
        run_metabolism は量による飛ばしと同じ扱いにする)。
    """
    def _skipped(reason: str) -> Dict[str, Any]:
        return {
            "ops_applied": 0, "ops_failed": 0,
            "memos_applied": 0, "memos_failed": 0,
            "promises_applied": 0, "promises_failed": 0,
            "pages_approved": 0, "pages_skipped": 0, "pages_failed": 0,
            "skipped": True, "reason": reason,
            "seen_span_end": None,
            "seen_ids": None,
            "finalize": (lambda: None),  # 確定する仕事が無い (no-op)
        }

    if not is_enabled():
        return _skipped("disabled")

    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not getattr(adapter, "is_ready", lambda: False)():
        # fail-closed (Codex 第三巡 修正 3): 成功扱いのスキップは disabled だけ。
        # ストレージ未準備は送出してゲート失敗 (退場停止) に写像する。
        raise SluiceStorageUnavailableError(
            f"persona memory storage is not ready (persona={persona_id}); "
            "eviction must not proceed without capture"
        )

    if run_id is None:
        run_id = uuid.uuid4().hex

    if event_callback:
        try:
            event_callback({
                "type": "metabolism",
                "status": "sluice",
                "content": "覚えておくことを探しています……",
            })
        except Exception:
            LOGGER.debug("[sluice] start event_callback raised", exc_info=True)

    # 担当範囲 (span) は前回のパンマーカーから始まる。マーカーのロードは保存
    # (末尾の _save_pan_marker) より前に行う。
    prev_marker = _load_pan_marker(persona)
    span_ids = [
        m.get("id") for m in current_messages
        if isinstance(m, dict) and m.get("id")
    ]
    span_start_id, span_end_full = _compute_span(current_messages, prev_marker)
    # 対象範囲の通数 — プロンプトで本人へ明示する材料 (:func:`_scope_sentence`)。
    # マーカーが窓に無い (初回 / 押し出されて消えた) ときは None = 窓全体が対象。
    span_new_count = (
        _count_new_since_marker(current_messages, prev_marker)
        if prev_marker and prev_marker in span_ids
        else None
    )

    # 実行台帳: identity は (persona, span_start_id)。同じ担当範囲の記録済み
    # 構造化結果があれば LLM を呼ばず再利用する (設計は _LEDGER_KIND 上のコメント)。
    ledger = _get_ledger(lifecycle)
    ledger_key = (
        f"{persona_id}:{span_start_id}" if (persona_id and span_start_id) else None
    )
    recorded = None
    if ledger is not None and ledger_key:
        recorded = _find_recorded_result(ledger, ledger_key)
        if recorded is not None and _recorded_result_unusable(recorded, persona_id):
            # 旧世代・破損の記録は適用側が読めない — 再利用せず、別キーで
            # 新しい LLM コールを立てる (_RESPONSE_FORMAT_TAG の説明を参照)。
            LOGGER.warning(
                "[sluice] 記録の形式が古いか壊れているため再利用しません "
                "(execution=%s key=%s persona=%s) — 新しい実行として採り直します",
                recorded.get("execution_id"), ledger_key, persona_id,
            )
            ledger_key = f"{ledger_key}#format-{_RESPONSE_FORMAT_TAG}"
            recorded = _find_recorded_result(ledger, ledger_key)
            if recorded is not None and _recorded_result_unusable(recorded, persona_id):
                # 採り直し用の別キーの記録まで使えない — さらに別のキーへ
                # 逃がすとキーが無限に育つので、ここは fail-closed で送出する
                # (退場停止 → 人の裁定)。静かに completed へ進めない。
                raise SluiceOutputError(
                    f"recorded result at {ledger_key} is unusable "
                    f"(persona={persona_id}); refusing silent completion"
                )

    execution_id: Optional[str] = None
    ledger_status: Optional[str] = None
    if recorded is not None:
        execution_id = recorded["execution_id"]
        ledger_status = recorded["status"]
        parsed_result = recorded["response"]
        span_start_id = recorded.get("span_start_id")
        span_end_id = recorded.get("span_end_id")
        # 対応表は _find_recorded_result が読み戻し済み (破損 = None は上の
        # _recorded_result_unusable が採り直しへ回すので、ここでは dict)。
        offered_activities = dict(recorded.get("offered_activities") or {})
        offered_tasks = dict(recorded.get("offered_tasks") or {})
        core_snapshot = recorded.get("core_snapshot")
        if not isinstance(core_snapshot, dict):
            # 旧形式の記録 (スナップショット無し): 再構成しない (第五巡の裁定と
            # 同族)。None のまま渡し、update / remove は要素棄却になる。
            core_snapshot = None
        seen_ids = recorded.get("seen_ids")
        if not isinstance(seen_ids, list) or not seen_ids:
            # fail-closed (Codex 第五巡 修正 1): seen_ids の無い記録を span から
            # 再構成するのは「別読みの近似」の同族 — 推定せず送出して退場を
            # 止める。空リストも同じ (Codex 五巡目): 書き手は空の見た集合を
            # 凍結しない (SluiceEmptySeenSetError で failed にする) ので、空の
            # 記録は破損 — 通すと、見ていない会話の上をマーカーが進んで
            # 畳まれる。
            raise SluiceContextUnavailableError(
                f"recorded result for {ledger_key} lacks seen_ids; refusing to "
                "reconstruct the seen set from the span (fail-closed)"
            )
        seen_ids = [str(message_id) for message_id in seen_ids]
        rejections = [
            item for item in (recorded.get("rejections") or [])
            if isinstance(item, dict)
        ]
        prompt_snapshot = str(
            recorded.get("prompt") or "(実行台帳の記録済み結果の再適用)"
        )
        # 欄の無い旧記録は [] (提案ゼロの回)。破損 (None) は上の
        # _recorded_result_unusable が採り直しへ回すので、ここでは list。
        page_review_candidates = list(recorded.get("page_review_candidates") or [])
        LOGGER.info(
            "[sluice] reusing recorded result (execution=%s span=%s..%s "
            "persona=%s); no new LLM call — re-applying idempotently",
            execution_id, span_start_id, span_end_id, persona_id,
        )
    else:
        if ledger is not None and ledger_key:
            execution_id, runnable, existing_status = ledger.claim_execution(
                _LEDGER_KIND, ledger_key, persona_id,
                payload={
                    "span_start_id": span_start_id,
                    "span_end_id": span_end_full,
                },
            )
            if not runnable:
                # running = 走行中 (通常は Beat ロックで起きない)、unknown =
                # 観測途絶 — どちらも自動の LLM 再実行はしない
                # (execution_ledger intent §2.5。unknown は list_unknown で裁定)。
                raise SluiceExecutionBlockedError(
                    f"sluice execution blocked by ledger (key={ledger_key}, "
                    f"status={existing_status})"
                )
            if not ledger.try_mark_running(execution_id):
                raise SluiceExecutionBlockedError(
                    f"sluice running seat lost (key={ledger_key})"
                )
        try:
            call = _call_sluice_llm(
                lifecycle, persona, building_id, span_end_full,
                span_new_count, window_anchor_id=window_anchor_id,
                model_key=model_key,
            )
        except Exception as exc:
            # LLM 失敗・出力不適合 = 適用前の検証棄却 (副作用ゼロ) → failed。
            # claim がキーを退避するので次回は新しい LLM コールで再試行される。
            if execution_id is not None:
                try:
                    ledger.mark_failed(execution_id, str(exc) or type(exc).__name__)
                except Exception:
                    LOGGER.exception(
                        "[sluice] mark_failed itself failed (execution=%s)",
                        execution_id,
                    )
            raise
        parsed_result = call["response"]
        rejections = call["rejections"]
        span_end_id = call["span_end_id"]
        seen_ids = call["seen_ids"]
        offered_activities = call["offered_activities"]
        offered_tasks = call["offered_tasks"]
        core_snapshot = call["core_snapshot"]
        prompt_snapshot = call["prompt"]
        page_review_candidates = list(call.get("page_review_candidates") or [])
        if span_end_id is None:
            # 実際に見た範囲の末尾が特定できない (窓に id 付きの行が無い等)。
            # span 刻印もマーカー前進も行わず、適用だけ実施する。
            span_start_id = None
        if execution_id is not None:
            # LLM の構造化結果を凍結 (running → applied)。以降の適用ステップは
            # 冪等なので、途中で失敗しても次回はこの記録を再利用する。
            try:
                ledger.mark_applied(execution_id, result={
                    "response": parsed_result,
                    "rejections": rejections,
                    "span_start_id": span_start_id,
                    "span_end_id": span_end_id,
                    "seen_ids": seen_ids,
                    "offered_activities": {
                        str(k): v for k, v in offered_activities.items()
                    },
                    "offered_tasks": offered_tasks,
                    "core_snapshot": core_snapshot,
                    "prompt": prompt_snapshot,
                    # 見せた記憶の手入れの候補 (再適用で同じ候補から承認を
                    # 引くため。提案ゼロの回も空配列で書く)。
                    "page_review_candidates": page_review_candidates,
                })
            except Exception as exc:
                # 凍結そのものの失敗 (DB 障害・コミット失敗)。ここを素通しすると
                # 台帳が running のまま残り、次回の claim が拒否して
                # SluiceExecutionBlockedError になる — 起動時回収で unknown に
                # 入っても人裁定までブロックし続ける (Codex 第八巡 修正 1)。
                # この時点で世界側の適用はまだ 1 件も走っていない (適用は下の
                # _apply_ops から) ので、running → failed は台帳の規約どおりの
                # 「適用前の検証棄却」— 次回は claim がキーを退避して新しい
                # LLM コールでやり直せる。mark_failed も失敗したら送出して
                # pan ごと失敗させる (fail-closed。commit は成功していたのに
                # 応答が失われた並びでは applied → failed が拒否されるが、
                # そのときは記録が残っているので次回が再利用で回収する)。
                LOGGER.error(
                    "[sluice] freezing the result failed (execution=%s "
                    "persona=%s); marking the execution failed so the next "
                    "metabolism can retry", execution_id, persona_id,
                    exc_info=True,
                )
                ledger.mark_failed(execution_id, str(exc) or type(exc).__name__)
                raise
            ledger_status = "applied"

    reflection = str(parsed_result.get("reflection", "") or "")

    def _as_list(key: str) -> List[Any]:
        raw = parsed_result.get(key, [])
        return list(raw) if isinstance(raw, list) else []

    core_adds = _as_list("core_adds")
    core_updates = _as_list("core_updates")
    core_removes = _as_list("core_removes")
    want_memos = _as_list("want_memos")
    did_memos = _as_list("did_memos")
    promise_adds = _as_list("promise_adds")
    promise_updates = _as_list("promise_updates")

    # 冪等キーの安定プレフィックス: span 由来 (再適用で不変 — §13.3 のゲート化で
    # 「部分適用 → 失敗 → 再適用」が正規経路のため)。span が無いときは
    # execution_id (台帳あり = 再利用でも不変)、それも無ければ run_id (台帳なし・
    # span なしの縮退 — このときだけ再試行間の重複防止は効かない)。
    if span_start_id and span_end_id:
        idem_prefix = f"sluice:{span_start_id}..{span_end_id}"
    elif execution_id is not None:
        idem_prefix = f"sluice:exec:{execution_id}"
    else:
        idem_prefix = f"sluice:{run_id}"

    # 4. 適用: コア記憶 → 手帳メモ → 約束 (全て冪等 — 再適用で重複しない)。
    ops_applied, ops_failed, ops_lines = _apply_core_ops(
        persona, core_adds, core_updates, core_removes,
        core_snapshot=core_snapshot,
    )
    memos_applied, memos_failed, memo_lines = _apply_memos(
        persona, want_memos, did_memos,
        idem_prefix=idem_prefix, span_start_id=span_start_id, span_end_id=span_end_id,
        offered_activities=offered_activities,
        # できごとの日 = 担当範囲の末尾メッセージの時刻 (機械刻印、B-2)。
        event_date=_message_event_date(persona, span_end_id),
        origin="live",
    )
    promises_applied, promises_failed, promise_lines = _apply_promises(
        lifecycle, persona, promise_adds, promise_updates,
        idem_prefix=idem_prefix, span_start_id=span_start_id, span_end_id=span_end_id,
        offered_tasks=offered_tasks,
    )
    # 検証段で型不正として落とした要素 (Codex 第八巡 修正 6) も、その欄の失敗と
    # して数え、判断ターン記録の先頭に残す — 黙って捨てない。記録は台帳に凍結
    # されているので、再適用でも同じ行が出る。
    rejection_lines: List[str] = []
    for item in rejections:
        text = str(item.get("text") or "").strip()
        if text:
            rejection_lines.append(text)
        field = item.get("field")
        if field in _CORE_FIELDS:
            ops_failed += 1
        elif field in ("want_memos", "did_memos"):
            memos_failed += 1
        elif field in _PROMISE_FIELDS:
            promises_failed += 1
    # 記憶の手入れの返答: approve は予約を積む (冪等)。実行は確定の後
    # (_finalize の末尾)。候補を見せていない回は何もしない。
    pages_approved, pages_skipped, pages_failed, page_lines = _apply_page_reviews(
        persona, _as_list(_PAGE_REVIEW_FIELD), page_review_candidates,
    )
    applied_total = ops_applied + memos_applied + promises_applied
    result_lines = (
        rejection_lines + ops_lines + memo_lines + promise_lines + page_lines
    )

    # 5. 記録テキスト。event_message 形式のシステム通知として <system> に包む
    #    (ペルソナ発話ではなくナレーション。few-shot 汚染回避、2026-07-07 まはー指摘)。
    #    判断 (reflection) と適用結果行を全文載せる (省略・切り詰め禁止、不変条件 §5-8)。
    persona_name = getattr(persona, "persona_name", None) or persona_id or "assistant"
    body_lines: List[str] = ["記憶整理の節目 — スルースの採取判断:"]
    if reflection.strip():
        body_lines.append(f"{persona_name}の判断: {reflection.strip()}")
    if result_lines:
        body_lines.extend(result_lines)
    if applied_total == 0 and not result_lines:
        body_lines.append("今回は採取しませんでした。")
    record_text = "<system>" + "\n".join(body_lines) + "\n</system>"

    # 6. 確定クロージャ (Codex 第五巡 修正 2)。適用は済んでいるが、
    #    ①台帳を閉じる (applied → completed) ②判断ターン記録の永続 ③完了通知
    #    ④パンマーカー前進 (実際に見た範囲の末尾まで) は「確定」として
    #    一塊にする。呼び出し元
    #    (run_metabolism) はマーカー前進が未提示メッセージを跨がないことを
    #    検算してから呼ぶ — 確定しなかった回の記録は applied のまま残り、
    #    次回は再適用 (冪等) から入り直す。二重呼びは no-op。
    finalize_state = {"done": False}

    def _finalize() -> None:
        if finalize_state["done"]:
            return
        # 順序 (Codex 第六巡 修正 2): **永続化が先、completed が最後**。
        # ①ナレーション永続 → ②パンマーカー保存 → ③mark_completed → ④完了通知。
        # ①②の失敗は握り潰さず送出する — 台帳は applied のまま残り、呼び出し元は
        # 退場を見送って、次回の再適用 (冪等) → 再 finalize で回収する。これで
        # 不変条件「completed ⇒ ナレーションとマーカーが永続済み」が成立する
        # (旧順序は completed 後の永続失敗が握り潰され、再起動後に旧マーカーで
        # 重複解釈する縁があった)。done は③まで成功した後にだけ立てる —
        # 途中失敗後の再呼びは頭から再実行される (①の再実行はナレーション重複の
        # 縁 — 大きさは run_sluice docstring ではなく報告に記す)。
        # 記憶の手入れの承認も、本人の決定が永続の予約を生んだ記録なので
        # 採取ありと同じく committed で残す (本人が承認したことを文脈に残す)。
        _persist_record(
            persona, record_text, prompt_snapshot,
            applied_total=applied_total + pages_approved,
        )
        # pan マーカー: 次回の担当範囲の起点として、**実際に LLM に渡した範囲の
        # 末尾 id** (span_end_id) を記録する。永続 (memory.db) が先、属性は成功後
        # (_save_pan_marker — 失敗は送出)。
        if span_end_id:
            _save_pan_marker(persona, span_end_id)
        if execution_id is not None and ledger_status == "applied":
            # ここで失敗したらスルース全体が失敗し (退場停止)、次回が記録済み
            # 結果を再利用して再適用する。
            ledger.mark_completed(execution_id)
        finalize_state["done"] = True
        if event_callback and applied_total > 0:
            try:
                event_callback({
                    "type": "metabolism",
                    "status": "sluice",
                    "content": f"覚えておくことを {applied_total} 件、記録しました。",
                })
            except Exception:
                LOGGER.debug("[sluice] completion event_callback raised", exc_info=True)
        LOGGER.info(
            "[sluice] finalized: persona=%s marker=%s", persona_id, span_end_id,
        )
        # 記憶の手入れの実行 (確定の後だけ — 確定が検算で見送られた回は
        # ここに来ないので、承認分は pending のまま次の確定を待つ)。この回に
        # 候補が無くても、前の回の pending が残っていれば起動する。
        _maybe_launch_curation_batch(lifecycle, persona)

    if finalize:
        _finalize()

    LOGGER.info(
        "[sluice] done: persona=%s core=%d/%d memos=%d/%d promises=%d/%d "
        "pages=%d approved/%d skipped/%d failed (evict=%d, finalized=%s)",
        persona_id, ops_applied, ops_failed, memos_applied, memos_failed,
        promises_applied, promises_failed,
        pages_approved, pages_skipped, pages_failed, evict_count, finalize,
    )
    return {
        "ops_applied": ops_applied, "ops_failed": ops_failed,
        "memos_applied": memos_applied, "memos_failed": memos_failed,
        "promises_applied": promises_applied, "promises_failed": promises_failed,
        "pages_approved": pages_approved, "pages_skipped": pages_skipped,
        "pages_failed": pages_failed,
        "skipped": False, "reason": None,
        "seen_span_end": span_end_id,
        "seen_ids": seen_ids,
        "finalize": _finalize,
    }


# ---------------------------------------------------------------------------
# 後から通す採取 (capture) — docs/intent/sluice_coverage_gaps.md 第一段 B
# ---------------------------------------------------------------------------
#
# ``sluice_skipped_spans`` に記録された「スルースを通っていない範囲」を、
# チャンクに刻んで順に通すジョブの実行部。入口 (UI) は第二段で、
# 第一段では API (api/routes/people/sluice.py) までを作る。
#
# 判断の主体は二本立て (B 節、2026-09-08 再設計): **機構** (候補を拾って
# ``sluice_candidate_memos`` に置くだけ — 既定) と **現在の本人** (読み返しだと
# 明示して読み、定常のスルースと同じ器を操作する)。「本人のシステムプロンプト
# だけ着せた器」は当時の本人でも今の本人でもない第三の主体だった (棄却)。
#
# 定常のスルース (run_sluice) との違い (本人モード):
# - 文脈は提示窓ではなく、記録された範囲のメッセージを DB から読み直して組む。
#   前置き (本人のシステムプロンプト + 短い自己認識) は毎チャンク同一に固定し、
#   プロバイダのプロンプトキャッシュに乗せる (帯の全量は載せない — intent 決定 3)。
# - パンマーカーは動かさない。このジョブは過去の範囲の採取で、定常のスルースの
#   担当範囲 (マーカーから窓の末尾) とは独立。
# - 進みの記録は ``sluice_skipped_spans`` の行そのもの: チャンクを処理し終える
#   たびに行の start_message_id を前進させ、全部済んだら行を消す — 中断しても
#   続きから再開できる。
# - 実行台帳は定常のスルースと同じ kind (:data:`_LEDGER_KIND`)・同じ identity
#   (persona, チャンクの起点 message id) に乗る。チャンクの起点は記録の縮めで
#   一意に決まるので、再試行は同じキーへ合流する。定常のスルースが同じ起点で
#   残した記録 (例: 記録範囲の末尾 1 通 = 新しい窓の頭を、後の温かい回が採取
#   済み) があれば再利用され、二重の LLM コールと二重適用が自然に防がれる。

#: 前置きの「短い自己認識」— 毎チャンク同一の固定文 (intent 決定 3 / 追加の
#: 決定 4: 文面はコード中の定数に置き、実装の検収でまはーが読む)。本人の
#: システムプロンプトの直後に置かれ、いま読んでいるものが進行中の会話ではなく
#: 過去の読み返しであることを本人に伝える。
_CAPTURE_SELF_RECOGNITION = (
    "<system>\n"
    "## 過去の会話の読み返し\n"
    "これは、いま進行中の会話ではありません。あなたの過去の会話のうち、\n"
    "記憶整理の採取（スルース）をまだ通っていない期間を、あなた自身の目で\n"
    "読み返すための時間です。\n"
    "この後に、その期間の会話の写しが当時のまま続きます。読み終えたところで、\n"
    "残しておきたいことがあれば記録できます。\n"
    "</system>"
)


class SluiceCaptureSpanUnreadableError(RuntimeError):
    """記録された範囲のメッセージが読み出せない (端の行の欠落・thread 不一致)。

    fail-closed: 読めない範囲を黙って消したり飛ばしたりせず、送出してジョブを
    失敗させる — 記録は残るので、原因 (行の削除等) を直せば再実行できる。
    """


def _build_capture_preamble(persona: Any) -> str:
    """毎チャンク同一の前置き (system 面)。本人のシステムプロンプト + 短い自己認識。

    「## あなたについて」の見出しは head の persona_self セクション
    (sea/head_pipeline/sections/persona_self.py) と同じ形 — 本人が普段の会話で
    受け取っている自己定義と同じ姿で渡す。帯 (Memory Weave) の全量は載せない
    (intent 決定 3) — 前置きが毎チャンク同一であることがプロンプトキャッシュの
    前提なので、変化する材料を混ぜない。
    """
    instruction = (
        getattr(persona, "persona_system_instruction", "") or ""
    ).strip()
    parts: List[str] = []
    if instruction:
        parts.append(f"## あなたについて\n{instruction}")
    parts.append(_CAPTURE_SELF_RECOGNITION)
    return "\n\n".join(parts)


def _capture_scope_sentence(count: int) -> str:
    """後から通す採取での「今回どこが対象か」の一行 (:func:`_scope_sentence` の差し替え)。"""
    return (
        f"今回の対象は、上に写した過去の会話 {count} 通です。"
        "いま進行中の会話とは独立した、後からの読み返しです。"
    )


def _chunk_period_dates(
    chunk_messages: List[Any],
) -> tuple[Optional[str], Optional[str]]:
    """チャンクの期間の両端の日付 ('YYYY-MM-DD')。読めない端は None。"""
    def _fmt(ts: Any) -> Optional[str]:
        try:
            value = int(ts)
            if value <= 0:
                return None  # created_at 欠落 (0 写し) — 1970 の嘘を出さない
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    return (
        _fmt(getattr(chunk_messages[0], "created_at", None)),
        _fmt(getattr(chunk_messages[-1], "created_at", None)),
    )


def _capture_period_label(chunk_messages: List[Any]) -> Optional[str]:
    """チャンクの期間 (日付) の表示。created_at が読めなければ None (載せない)。"""
    start, end = _chunk_period_dates(chunk_messages)
    if start and end:
        return start if start == end else f"{start}〜{end}"
    return None


def _plan_capture_chunks(
    messages: List[Any],
    max_chars: int,
    fit: Optional[Tuple[int, Callable[[Any, int], int]]] = None,
) -> List[List[Any]]:
    """メッセージ列を、A と同じ閾値以下のチャンク列に刻む (各チャンク最低 1 通)。

    貪欲法: 先頭から字数を積み、超える直前で切る。**1 通だけで閾値を超える
    メッセージは、その 1 通だけのチャンクにする** — メッセージより細かい単位は
    無く、飛ばすと範囲の縮めが進まなくなる (採取は冪等なので過大な 1 チャンクを
    許す方が安全)。1 通だけでモデルに入らないチャンクは、LLM を呼ぶ直前の比較
    (:func:`_ensure_input_fits`) が止める。

    ``fit`` は :func:`_capture_input_fit` の ``(会話に使える量, 1 通の数え方)``。
    渡されると、会話の字数の閾値 (``max_chars``、429 を避ける固定の上限) とは
    別に、写しの書式を含めた量がそのモデルに入る量を超える直前でも切る。
    数え方はチャンク内の位置 (1 始まり) を受け取る — 機構モードの行番号
    ``msg:N`` の桁が位置で変わるため。
    """
    fit_limit = fit[0] if fit is not None else None
    cost_of = fit[1] if fit is not None else None
    chunks: List[List[Any]] = []
    current: List[Any] = []
    current_chars = 0
    current_cost = 0
    for msg in messages:
        chars = len(getattr(msg, "content", None) or "")
        cost = cost_of(msg, len(current) + 1) if cost_of is not None else 0
        over_chars = current_chars + chars > max_chars
        over_fit = fit_limit is not None and current_cost + cost > fit_limit
        if current and (over_chars or over_fit):
            chunks.append(current)
            current = []
            current_chars = 0
            current_cost = 0
            cost = cost_of(msg, 1) if cost_of is not None else 0
        current.append(msg)
        current_chars += chars
        current_cost += cost
    if current:
        chunks.append(current)
    return chunks


def _capture_execution_context(persona: Any, model_key: Optional[str]) -> Any:
    """後から通す採取で使う実行の身分証 (構造化出力の都合で差し替わる前)。

    使用モデルの既定は本人のモデル。明示指定 (第二段 UI の選択肢) が来たとき
    だけ差し替える (intent 決定 3 — 後から通す操作は明示の操作なので、モデルが
    通常の会話と違ってよい)。実際に使うモデルは :func:`_select_capture_llm` が
    ここから決める。
    """
    from sea.pulse_context import resolve_execution_context

    execution_context = resolve_execution_context(persona, None)
    if model_key and execution_context.model_key != model_key:
        execution_context = execution_context.with_model(model_key)
    return execution_context


def _select_capture_llm(
    lifecycle: Any, persona: Any, model_key: Optional[str],
) -> Tuple[Any, Any]:
    """後から通す採取で実際に使うクライアントと実行の身分証 (決め方の一本化)。

    LLM 呼び出し (:func:`_call_capture_llm` / :func:`_call_mechanism_llm`) と
    刻みの見積もり (:func:`_capture_input_fit`) が同じモデルとクライアントを見る
    ために、決め方をここに揃える: 呼び出しと同じ ``runtime.select_llm_client``
    (``needs_structured_output=True``) を通し、構造化出力に対応しないモデルが
    軽量モデルへ差し替わったら、身分証も差し替わった後のモデルにする
    (docs/issues/sluice_skip_ignores_model_context.md「レビューの裁定」第一巡の 4)。

    接続を作る (``select_llm_client`` は接続を作り、llama.cpp のサーバーを必要なら
    起動する) ので、LLM を呼ぶ実行の側だけが使う。見積もり (dry) は接続を作らない
    :func:`_resolve_capture_model` を使う。

    Returns:
        ``(llm_client, execution_context)``。``lifecycle`` か runtime が無い呼び出し
        は ``(None, 差し替え前の身分証)``。
    """
    execution_context = _capture_execution_context(persona, model_key)
    runtime = getattr(lifecycle, "runtime", None)
    if runtime is None:
        return None, execution_context
    node_def = SimpleNamespace(id="sluice_capture", memorize=None, speak=False)
    llm_client, actual_model = runtime.select_llm_client(
        node_def, persona, execution_context=execution_context,
        needs_structured_output=True,
    )
    if actual_model != execution_context.model_key:
        execution_context = execution_context.with_model(actual_model)
    return llm_client, execution_context


def _resolve_capture_model(
    lifecycle: Any, persona: Any, model_key: Optional[str],
) -> str:
    """後から通す採取で実際に使うモデルを、接続を作らずに決める (見積もり用)。

    規則は :func:`_select_capture_llm` と同じ — runtime の
    ``resolve_llm_model`` (``select_llm_client`` と同じ規則で、構造化出力に対応
    しないモデルを軽量モデルへ差し替える) を ``needs_structured_output=True`` で
    通す。接続の作成・サーバーの起動・疎通の確認はしない (dry の「LLM ゼロ・
    書き込みゼロ」— docs/issues/sluice_skip_ignores_model_context.md
    「レビューの裁定」第二巡の 1)。``lifecycle`` か runtime が無い呼び出しは
    差し替え前のモデル。
    """
    execution_context = _capture_execution_context(persona, model_key)
    runtime = getattr(lifecycle, "runtime", None)
    if runtime is None:
        return str(execution_context.model_key)
    return str(runtime.resolve_llm_model(
        persona, execution_context=execution_context,
        needs_structured_output=True,
    ))


def _capture_input_fit(
    lifecycle: Any,
    persona: Any,
    *,
    mode: str,
    model_key: Optional[str],
    dry: bool = False,
) -> Optional[Tuple[int, Callable[[Any, int], int]]]:
    """後から通す採取で、会話の写しに使える量 (トークン) と 1 通の数え方。

    実行 (``dry=False``) では、モデルとクライアントを呼び出しと同じ決め方
    (:func:`_select_capture_llm` — 接続を作る) で決める。そのチャンクの LLM
    呼び出しが直後に同じ接続を使う。見積もり (``dry=True``) では、同じ規則で
    モデルだけを接続を作らずに決め (:func:`_resolve_capture_model`)、接続が無い
    ので応答の枠は :data:`_MAX_OUTPUT_TOKENS` で数える — 応答の枠が大きい
    クライアントでは、実行の刻みが見積もりより細かくなりうる。
    会話に使える量 = そのモデルの上限 (:func:`_input_token_budget` — 応答の
    枠はそのクライアントが送る上限) − 会話以外の部分の見積もり。会話以外の部分は、
    本人モードなら前置き (:func:`_build_capture_preamble`) と指示文と答えの形の
    指定 (:data:`_RESPONSE_SCHEMA`)、機構モードなら空のチャンクで組んだ指示文
    (:func:`_build_mechanism_prompt`) と候補の形の指定 (:data:`_CANDIDATE_SCHEMA`)。
    どちらも件数の桁の余白 (:data:`_CAPTURE_COUNT_DIGITS_SLACK`) を足す。
    ``lifecycle`` が無いときは、差し替え前のモデルと既定の応答の枠で見積もる。

    1 通の数え方は、LLM を呼ぶ直前の見積もり (:func:`_estimate_input_tokens`) が
    その 1 通に数える量以上になるようにする — 刻んだチャンク (2 通以上) が
    呼び出し直前の比較を必ず通るため。本人モードは本文の字数 + 1 通ごとの 4、
    機構モードは写しの一行 (``[msg:N] 日付 名前: 本文``) と改行の字数。

    モデル設定が引けないときは None (刻みは固定の字数の上限だけで決まる)。
    """
    if dry:
        llm_client = None
        model = _resolve_capture_model(lifecycle, persona, model_key)
    else:
        llm_client, execution_context = _select_capture_llm(
            lifecycle, persona, model_key,
        )
        model = str(execution_context.model_key)
    persona_id = getattr(persona, "persona_id", None)
    limit = _input_token_budget(
        model, persona_id=persona_id, llm_client=llm_client,
    )
    if limit is None:
        return None

    if mode == "persona":
        instruction = _build_capture_instruction(lifecycle, persona, 0)
        fixed = _estimate_input_tokens(
            [
                {"role": "system", "content": _build_capture_preamble(persona)},
                {"role": "user", "content": instruction["prompt"]},
            ],
            model,
            response_schema=_RESPONSE_SCHEMA,
        )

        def cost_of(msg: Any, index: int) -> int:
            return len(getattr(msg, "content", None) or "") + _TOKENS_PER_MESSAGE
    else:
        persona_name = _mechanism_persona_name(persona)
        fixed = _estimate_input_tokens(
            [{"role": "user", "content": _build_mechanism_prompt(persona_name, [])}],
            model,
            response_schema=_CANDIDATE_SCHEMA,
        )

        def cost_of(msg: Any, index: int) -> int:
            return len(_mechanism_transcript_line(persona_name, msg, index)) + 1

    return limit - fixed - _CAPTURE_COUNT_DIGITS_SLACK, cost_of


def _read_span_messages(persona: Any, span: Dict[str, Any]) -> Optional[List[Any]]:
    """記録された範囲 [start, end] の実会話メッセージを正典順で読む。

    読みは :func:`sai_memory.memory.storage.get_conversation_messages_between`
    — 機構名義の行 (event_message / handy_tool / spell) と ``<system>`` 頭の
    通知は対象に入れない。本人の目で読み返して採取する材料は発話であって、
    機構の定型文ではない。端の行が無い・thread が食い違うときは None (呼び出し
    側が fail-closed で止める)。
    """
    from sai_memory.memory.storage import get_conversation_messages_between

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or getattr(adapter, "conn", None) is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot read the skipped span"
        )
    with adapter._db_lock:
        return get_conversation_messages_between(
            adapter.conn,
            str(span["start_message_id"]),
            str(span["end_message_id"]),
        )


def plan_sluice_capture(
    persona: Any,
    *,
    mode: str = "mechanism",
    model_key: Optional[str] = None,
    lifecycle: Any = None,
) -> Dict[str, Any]:
    """後から通す採取の見積もり (dry)。LLM ゼロ・書き込みゼロ。

    接続を作らない: LLM の接続の作成・llama.cpp のサーバーの起動・疎通の確認・
    ファイルやディレクトリの作成をしない (docs/issues/sluice_skip_ignores_model_context.md
    「レビューの裁定」第二巡の 1)。

    ``mode`` と ``model_key`` は実行 (:func:`run_sluice_capture`) に渡すのと同じ
    値を渡す — 刻みの大きさは使うモデルと判断の主体で変わる
    (:func:`_capture_input_fit`)。既定は実行の既定 (機構モード・本人のモデル)。
    ``lifecycle`` は、実行の ``runtime.select_llm_client`` と同じ規則で実際に使う
    モデル (構造化出力の都合で軽量モデルへ差し替わった後) を接続を作らずに決める
    のと (runtime の ``resolve_llm_model``)、本人モードの指示文に載る約束の一覧を
    読むのに使う。None (runtime の無い呼び出し) なら、差し替え前のモデルで、
    約束の一覧なしで見積もる — 差し替わるモデルでは実行側の刻みと違う数になりうる。

    Returns:
        ``{"spans": [{id, start_message_id, end_message_id, created_at,
        message_count, estimated_chunks, readable}], "target_messages": int,
        "estimated_chunks": int, "unreadable_spans": int,
        "max_span_chars": int}``。件数・チャンク数は実行部と同じ読み
        (:func:`_read_span_messages`) と同じ刻み (:func:`_plan_capture_chunks`)
        から数える。チャンク数は**見積もった時点の状態での数**で、実行の刻みの方が
        細かくなりうる理由が二つある。一つは、本人モードでは採取でコア記憶や手帳が
        増えると指示文が伸び、実行はチャンクごとにその時点の状態で刻み直すこと
        (状態が変わらなければ一致する)。もう一つは、見積もりは接続を作らないので
        応答の枠を 4,096 (:data:`_MAX_OUTPUT_TOKENS`) で数え、実行はそのクライアントが
        送る応答の上限で数えること — 応答の枠が大きいクライアント (Anthropic、
        ``max_tokens`` を送る OpenAI 互換) では実行の刻みが見積もりより細かくなりうる。
        ``max_span_chars`` は見積もりで使った刻みの大きさ (固定の字数の上限と、
        そのモデルに入る量の小さい方)。
    """
    from sai_memory.memory.storage import list_sluice_skipped_spans

    if mode not in CAPTURE_MODES:
        raise ValueError(
            f"unknown capture mode: {mode!r} (expected one of {CAPTURE_MODES})"
        )
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not getattr(adapter, "is_ready", lambda: False)():
        raise SluiceStorageUnavailableError(
            "persona memory storage is not ready; cannot plan the capture"
        )
    max_chars = get_max_span_chars()
    fit = _capture_input_fit(
        lifecycle, persona, mode=mode, model_key=model_key, dry=True,
    )
    with adapter._db_lock:
        spans = list_sluice_skipped_spans(adapter.conn)

    spans_out: List[Dict[str, Any]] = []
    target_messages = 0
    estimated_chunks = 0
    unreadable = 0
    for span in spans:
        messages = _read_span_messages(persona, span)
        if messages is None:
            unreadable += 1
            spans_out.append({
                **span, "message_count": None, "estimated_chunks": None,
                "readable": False,
            })
            continue
        chunks = _plan_capture_chunks(messages, max_chars, fit)
        target_messages += len(messages)
        estimated_chunks += len(chunks)
        spans_out.append({
            **span, "message_count": len(messages),
            "estimated_chunks": len(chunks), "readable": True,
        })
    return {
        "spans": spans_out,
        "target_messages": target_messages,
        "estimated_chunks": estimated_chunks,
        "unreadable_spans": unreadable,
        "max_span_chars": (
            max_chars if fit is None else max(0, min(max_chars, fit[0]))
        ),
    }


def _build_capture_instruction(
    lifecycle: Any, persona: Any, message_count: int,
) -> Dict[str, Any]:
    """本人モードの指示文 (末尾の注入プロンプト) と、その時点の照合値を組む。

    LLM 呼び出し (:func:`_call_capture_llm`) と刻みの見積もり
    (:func:`_capture_input_fit`) が同じ組み方を使う — 別々に組むと、見積もりが
    実物より短くなって刻んだチャンクが呼び出し直前の比較で止まる。

    Returns:
        ``{"prompt": str, "offered_activities": {id: name},
        "offered_tasks": {"N": {task_id, revision, due_at, content}},
        "core_snapshot": {core_id: hash}}``
    """
    activities = _list_open_activities(persona)
    open_tasks = _list_open_tasks(lifecycle, persona)
    core_memories, core_total_chars = _read_core_state(persona)
    today_memos = _list_today_memos(persona, activities)
    prompt = _build_sluice_prompt(
        persona, activities, open_tasks, core_memories, core_total_chars,
        span_new_count=None, today_memos=today_memos,
        scope_sentence=_capture_scope_sentence(message_count),
    )
    return {
        "prompt": prompt,
        "offered_activities": dict(activities),
        "offered_tasks": _offered_task_map(open_tasks),
        "core_snapshot": {
            str(mem.id): _core_content_hash(mem.content) for mem in core_memories
        },
    }


def _call_capture_llm(
    lifecycle: Any,
    persona: Any,
    chunk_messages: List[Any],
    *,
    model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """チャンク 1 つの LLM 呼び出し (前置き + 会話の写し + 注入プロンプト)。

    定常の :func:`_call_sluice_llm` と違い、文脈は提示窓ではなくチャンクの
    メッセージから直接組む。anchor には触らない — この呼び出しは会話の
    Session prefix を温めるものではないので、touch すると温かさの偽装になる。
    例外 (LLM エラー・出力不適合) はそのまま送出する。送る中身が実際に使う
    モデルに入らないときは LLM を呼ばずに :class:`SluiceInputTooLargeError` を
    送出する (刻みは :func:`_capture_input_fit` で入る量に合わせてあるので、
    ここで止まるのは 1 通だけで入らないメッセージを含むチャンク)。
    """
    runtime = lifecycle.runtime
    persona_id = getattr(persona, "persona_id", None)

    # 刻みの見積もり (_capture_input_fit) と同じ決め方でモデルとクライアントを決める。
    llm_client, execution_context = _select_capture_llm(
        lifecycle, persona, model_key,
    )

    instruction = _build_capture_instruction(
        lifecycle, persona, len(chunk_messages),
    )
    prompt = instruction["prompt"]
    offered_activities = instruction["offered_activities"]
    offered_tasks = instruction["offered_tasks"]
    core_snapshot = instruction["core_snapshot"]

    # 会話の写し。役割は保存値のまま ('model' だけ通称 'assistant' へ —
    # saiverse_memory/adapter.py の提示時と同じ写像)。実会話フィルタ
    # (_read_span_messages) が user/model/assistant 以外を通さない。
    history = [
        {
            "role": (
                "assistant" if getattr(m, "role", None) == "model"
                else getattr(m, "role", "user")
            ),
            "content": getattr(m, "content", None) or "",
        }
        for m in chunk_messages
    ]
    messages = (
        [{"role": "system", "content": _build_capture_preamble(persona)}]
        + history
        + [{"role": "user", "content": prompt}]
    )

    _ensure_input_fits(
        messages, execution_context.model_key, persona_id=persona_id,
        llm_client=llm_client, response_schema=_RESPONSE_SCHEMA,
    )
    result = llm_client.generate(
        messages,
        tools=[],
        response_schema=_RESPONSE_SCHEMA,
        temperature=runtime._default_temperature(persona),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        **runtime._get_cache_kwargs(persona_id),
    )

    usage = llm_client.consume_usage() if hasattr(llm_client, "consume_usage") else None
    if usage is not None:
        try:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                building_id=getattr(persona, "current_building_id", None),
                node_type="sluice",
                playbook_name="sluice_capture",
                category="sluice",
            )
        except Exception:
            LOGGER.warning(
                "[sluice-capture] usage tracking failed (persona=%s)",
                persona_id, exc_info=True,
            )

    parsed, rejections = _parse_structured_result(result, persona_id)
    return {
        "response": parsed,
        "rejections": rejections,
        "offered_activities": offered_activities,
        "offered_tasks": offered_tasks,
        "core_snapshot": core_snapshot,
        "prompt": prompt,
    }


def _run_capture_chunk(
    lifecycle: Any,
    persona: Any,
    chunk_messages: List[Any],
    *,
    model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """チャンク 1 つを台帳 → LLM → 適用 → 記録 → 完了まで通す。

    振り付けは :func:`run_sluice` と同じ状態機械 (claim → running → 凍結
    (applied) → 冪等適用 → 永続 → completed) の縮約形。パンマーカーの前進と
    finalize の保留 (退場との検算) はこのジョブには無い — 進みの記録は
    呼び出し側 (:func:`run_sluice_capture`) が範囲の行の縮めで持つ。

    途中失敗の再実行はチャンクの起点 (= 記録の縮めで一意) が同じ台帳キーへ
    合流する: applied/completed の記録があれば LLM を呼ばず再適用する (適用は
    冪等)。completed 済みの記録を再適用した回は判断ターンの永続が二重になる
    縁があるが、これは run_sluice の finalize 再実行と同じ大きさの既知の縁。
    """
    persona_id = getattr(persona, "persona_id", None)
    chunk_start_id = str(getattr(chunk_messages[0], "id"))
    chunk_end_id = str(getattr(chunk_messages[-1], "id"))

    ledger = _get_ledger(lifecycle)
    ledger_key = f"{persona_id}:{chunk_start_id}" if persona_id else None
    recorded = None
    if ledger is not None and ledger_key:
        recorded = _find_recorded_result(ledger, ledger_key)
        if recorded is not None and _recorded_result_unusable(recorded, persona_id):
            LOGGER.warning(
                "[sluice-capture] 記録の形式が古いか壊れているため再利用しません "
                "(execution=%s key=%s persona=%s) — 新しい実行として採り直します",
                recorded.get("execution_id"), ledger_key, persona_id,
            )
            ledger_key = f"{ledger_key}#format-{_RESPONSE_FORMAT_TAG}"
            recorded = _find_recorded_result(ledger, ledger_key)
            if recorded is not None and _recorded_result_unusable(recorded, persona_id):
                # 定常側と同じ fail-closed (キーを無限に育てない)。
                raise SluiceOutputError(
                    f"recorded capture result at {ledger_key} is unusable "
                    f"(persona={persona_id}); refusing silent completion"
                )

    execution_id: Optional[str] = None
    ledger_status: Optional[str] = None
    # 適用の冪等キーと span 刻印の素材。再利用の回は**記録側の span** を使う —
    # 定常のスルースが同じ起点で残した記録 (終端が違う) を別キーで適用し直すと
    # 冪等キーが揃わず、同じメモが二重に入る (run_sluice の再利用と同じ規律)。
    apply_span_start = chunk_start_id
    apply_span_end = chunk_end_id
    if recorded is not None:
        execution_id = recorded["execution_id"]
        ledger_status = recorded["status"]
        parsed_result = recorded["response"]
        apply_span_start = str(recorded.get("span_start_id") or chunk_start_id)
        apply_span_end = str(recorded.get("span_end_id") or chunk_end_id)
        rejections = [
            item for item in (recorded.get("rejections") or [])
            if isinstance(item, dict)
        ]
        # 対応表は _find_recorded_result が読み戻し済み (破損 = None は
        # _recorded_result_unusable が採り直しへ回すので、ここでは dict)。
        offered_activities = dict(recorded.get("offered_activities") or {})
        offered_tasks = dict(recorded.get("offered_tasks") or {})
        core_snapshot = recorded.get("core_snapshot")
        if not isinstance(core_snapshot, dict):
            core_snapshot = None
        prompt_snapshot = str(
            recorded.get("prompt") or "(実行台帳の記録済み結果の再適用)"
        )
        LOGGER.info(
            "[sluice-capture] reusing recorded result (execution=%s span=%s..%s "
            "persona=%s); no new LLM call — re-applying idempotently",
            execution_id, chunk_start_id, chunk_end_id, persona_id,
        )
    else:
        if ledger is not None and ledger_key:
            execution_id, runnable, existing_status = ledger.claim_execution(
                _LEDGER_KIND, ledger_key, persona_id,
                payload={
                    "span_start_id": chunk_start_id,
                    "span_end_id": chunk_end_id,
                    "origin": "capture",
                },
            )
            if not runnable:
                raise SluiceExecutionBlockedError(
                    f"sluice capture blocked by ledger (key={ledger_key}, "
                    f"status={existing_status})"
                )
            if not ledger.try_mark_running(execution_id):
                raise SluiceExecutionBlockedError(
                    f"sluice capture running seat lost (key={ledger_key})"
                )
        try:
            call = _call_capture_llm(
                lifecycle, persona, chunk_messages, model_key=model_key,
            )
        except Exception as exc:
            # LLM 失敗・出力不適合 = 適用前の検証棄却 (副作用ゼロ) → failed。
            # レート制限起因なら persona 単位の小休止 (C-1) を置く — 呼び出し
            # 側のループはこの小休止を見て走行を閉じる。
            noter = getattr(lifecycle, "_note_metabolism_rate_limit", None)
            if callable(noter):
                noter(persona_id, exc)
            if execution_id is not None:
                try:
                    ledger.mark_failed(execution_id, str(exc) or type(exc).__name__)
                except Exception:
                    LOGGER.exception(
                        "[sluice-capture] mark_failed itself failed (execution=%s)",
                        execution_id,
                    )
            raise
        parsed_result = call["response"]
        rejections = call["rejections"]
        offered_activities = call["offered_activities"]
        offered_tasks = call["offered_tasks"]
        core_snapshot = call["core_snapshot"]
        prompt_snapshot = call["prompt"]
        if execution_id is not None:
            try:
                ledger.mark_applied(execution_id, result={
                    "response": parsed_result,
                    "rejections": rejections,
                    "span_start_id": chunk_start_id,
                    "span_end_id": chunk_end_id,
                    "seen_ids": [
                        str(getattr(m, "id")) for m in chunk_messages
                    ],
                    "offered_activities": {
                        str(k): v for k, v in offered_activities.items()
                    },
                    "offered_tasks": offered_tasks,
                    "core_snapshot": core_snapshot,
                    "prompt": prompt_snapshot,
                })
            except Exception as exc:
                # 凍結の失敗を running のまま残さない (run_sluice と同じ理由)。
                LOGGER.error(
                    "[sluice-capture] freezing the result failed (execution=%s "
                    "persona=%s); marking failed so the next run can retry",
                    execution_id, persona_id, exc_info=True,
                )
                ledger.mark_failed(execution_id, str(exc) or type(exc).__name__)
                raise
            ledger_status = "applied"

    reflection = str(parsed_result.get("reflection", "") or "")

    def _as_list(key: str) -> List[Any]:
        raw = parsed_result.get(key, [])
        return list(raw) if isinstance(raw, list) else []

    idem_prefix = f"sluice:{apply_span_start}..{apply_span_end}"

    ops_applied, ops_failed, ops_lines = _apply_core_ops(
        persona, _as_list("core_adds"), _as_list("core_updates"),
        _as_list("core_removes"), core_snapshot=core_snapshot,
    )
    memos_applied, memos_failed, memo_lines = _apply_memos(
        persona, _as_list("want_memos"), _as_list("did_memos"),
        idem_prefix=idem_prefix,
        span_start_id=apply_span_start, span_end_id=apply_span_end,
        offered_activities=offered_activities,
        # できごとの日 = チャンクの末尾メッセージの時刻 (機械刻印、B-2)。
        # 由来の印 = 本人の読み返し。
        event_date=_message_event_date(persona, apply_span_end),
        origin="readback",
    )
    promises_applied, promises_failed, promise_lines = _apply_promises(
        lifecycle, persona, _as_list("promise_adds"), _as_list("promise_updates"),
        idem_prefix=idem_prefix,
        span_start_id=apply_span_start, span_end_id=apply_span_end,
        offered_tasks=offered_tasks,
    )
    rejection_lines: List[str] = []
    for item in rejections:
        text = str(item.get("text") or "").strip()
        if text:
            rejection_lines.append(text)
        field = item.get("field")
        if field in _CORE_FIELDS:
            ops_failed += 1
        elif field in ("want_memos", "did_memos"):
            memos_failed += 1
        elif field in _PROMISE_FIELDS:
            promises_failed += 1
    applied_total = ops_applied + memos_applied + promises_applied
    result_lines = rejection_lines + ops_lines + memo_lines + promise_lines

    # 判断ターンの記録 (run_sluice と同じ event_message 形式・同じ永続経路)。
    # 見出しで「過去の読み返し」であることを明示する — 定常のスルースの記録と
    # 混ざっても、いつの何を読んだ判断かが本人にも読める。
    persona_name = getattr(persona, "persona_name", None) or persona_id or "assistant"
    period = _capture_period_label(chunk_messages)
    header = "過去の会話の読み返し — スルースの採取判断"
    if period:
        header += f"（{period}、{len(chunk_messages)} 通）"
    else:
        header += f"（{len(chunk_messages)} 通）"
    body_lines: List[str] = [header + ":"]
    if reflection.strip():
        body_lines.append(f"{persona_name}の判断: {reflection.strip()}")
    if result_lines:
        body_lines.extend(result_lines)
    if applied_total == 0 and not result_lines:
        body_lines.append("今回は採取しませんでした。")
    record_text = "<system>" + "\n".join(body_lines) + "\n</system>"

    # 永続が先、completed が最後 (run_sluice の finalize と同じ順序)。
    # 読み返しの過程は本線の context に載せない (入口は一本 — 走行の締めに
    # ダイジェスト一行だけが committed で立つ) ので、判断ターン記録は採取の
    # 有無に関わらず discardable (DB には残る = 監査は可能)。
    _persist_record(
        persona, record_text, prompt_snapshot, applied_total=applied_total,
        scope_override="discardable",
    )
    if execution_id is not None and ledger_status == "applied":
        ledger.mark_completed(execution_id)

    LOGGER.info(
        "[sluice-capture] chunk done: persona=%s span=%s..%s (%d messages) "
        "core=%d/%d memos=%d/%d promises=%d/%d",
        persona_id, chunk_start_id, chunk_end_id, len(chunk_messages),
        ops_applied, ops_failed, memos_applied, memos_failed,
        promises_applied, promises_failed,
    )
    return {
        "ops_applied": ops_applied, "ops_failed": ops_failed,
        "memos_applied": memos_applied, "memos_failed": memos_failed,
        "promises_applied": promises_applied, "promises_failed": promises_failed,
        "span_start_id": chunk_start_id, "span_end_id": chunk_end_id,
        "messages": len(chunk_messages),
        # 走行の締めのダイジェスト一行が期間を言うための材料 (日付のみ)。
        "period_start": _chunk_period_dates(chunk_messages)[0],
        "period_end": _chunk_period_dates(chunk_messages)[1],
    }


# ---------------------------------------------------------------------------
# 機構モード (candidate 抽出) — docs/intent/sluice_coverage_gaps.md B 節 候補 1
# ---------------------------------------------------------------------------
#
# 誰でもない機構がその範囲だけを見て、手帳のメモの**候補**を拾う。Chronicle の
# 生成と同じ型 (本人のシステムプロンプトを着せない・単発の user プロンプト)。
# 機構はコア記憶・手帳へ代筆できない (本人の言葉の器) ので、出力は
# ``sluice_candidate_memos`` に候補として置くだけ — 本人の器 (コア記憶・手帳・
# 約束・会話ログ) には何も書かない。採用・却下は第二段の UI。
# コア記憶・約束の候補は出さない (第一段の範囲外)。

#: 候補の参照欄の書式 (会話の写しに振る行番号の写し)。桁数の縛りは
#: _CORE_REF_RE と同じ理由 (暴走した数字列を int() へ渡さない)。
_MSG_REF_RE = re.compile(r"^msg:([0-9]{1,9})$")

_CANDIDATE_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "activity_name": {
            "type": "string",
            "description": (
                "「小説を書く」「絵の練習」のような活動の粒度の名前の提案。"
                "具体的な詳細はここではなく text に書く。"
            ),
        },
        "text": {
            "type": "string",
            "description": (
                "候補の一行。会話の中の本人の言い方に沿わせる（発明しない）。"
            ),
        },
        "source_refs": {
            "type": "array",
            "description": (
                "根拠になったメッセージの msg:N（会話の写しの行頭の番号）を"
                "そのまま写す（例: msg:3）。"
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["activity_name", "text", "source_refs"],
}

#: 機構モードの構造化出力 (want / did の二欄のみ)。型の規律は _RESPONSE_SCHEMA
#: と同じ: 数値の欄を置かない (参照は msg:N の文字列写し)、全欄必須 (欄の省略を
#: 「候補なし」へ丸めない)。
_CANDIDATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "want_memos": {
            "type": "array",
            "description": "本人がやりたいと言っていたことの候補。無ければ空配列。",
            "items": _CANDIDATE_ITEM_SCHEMA,
        },
        "did_memos": {
            "type": "array",
            "description": "本人が実際にやったことの候補。無ければ空配列。",
            "items": _CANDIDATE_ITEM_SCHEMA,
        },
    },
    "required": ["want_memos", "did_memos"],
}

_CANDIDATE_LIST_FIELDS = ("want_memos", "did_memos")


def _mechanism_persona_name(persona: Any) -> str:
    """機構モードの写しと指示文に出すペルソナの名前。"""
    return (
        getattr(persona, "persona_name", None)
        or getattr(persona, "persona_id", None)
        or "ペルソナ"
    )


def _mechanism_transcript_line(persona_name: str, msg: Any, index: int) -> str:
    """機構モードの会話の写しの一行 (``[msg:N] YYYY-MM-DD 名前: 本文``)。

    指示文の組み立て (:func:`_build_mechanism_prompt`) と刻みの見積もり
    (:func:`_capture_input_fit`) が同じ一行を使う。
    """
    role = getattr(msg, "role", None)
    speaker = persona_name if role in ("model", "assistant") else "ユーザー"
    date_label = ""
    try:
        ts = int(getattr(msg, "created_at", 0) or 0)
        if ts > 0:
            date_label = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") + " "
    except (TypeError, ValueError, OverflowError, OSError):
        date_label = ""
    content = (getattr(msg, "content", None) or "").strip()
    return f"[msg:{index}] {date_label}{speaker}: {content}"


def _build_mechanism_prompt(
    persona_name: str, chunk_messages: List[Any],
) -> str:
    """機構モードの単発プロンプト (機構の声 — Chronicle 生成と同じ型)。

    本人のシステムプロンプトは着せない。会話の写しに ``msg:N`` の行番号を
    振り、候補の根拠 (source_refs) はその写しで受け取る — できごとの日時
    (event_date) は根拠のメッセージの保存時刻から**機械が刻印**するので、
    日付を LLM に申告させる欄は無い。
    """
    transcript = "\n".join(
        _mechanism_transcript_line(persona_name, msg, index)
        for index, msg in enumerate(chunk_messages, start=1)
    )
    return (
        "これは、過去の会話の記録から、手帳のメモの候補を拾う整理の作業です。\n"
        "あなたはこの会話の当事者ではありません。拾った候補はそのまま記録には"
        "ならず、後で本人とユーザーが見て採用・却下を決めます。\n"
        "\n"
        f"以下は、ペルソナ「{persona_name}」とユーザーの過去の会話の写しです"
        f"（{len(chunk_messages)} 通。行頭の msg:N は参照用の番号）:\n"
        "\n"
        "【会話の写し】\n"
        f"{transcript}\n"
        "\n"
        "この写しの中から、次の二種類の候補を拾ってください:\n"
        f"- want_memos: {persona_name} がやりたいと言っていたこと\n"
        f"- did_memos: {persona_name} が実際にやったこと\n"
        "\n"
        "各候補の書き方:\n"
        "- activity_name: 「小説を書く」「絵の練習」のような活動の粒度の名前の提案\n"
        f"- text: 候補の一行。会話の中の {persona_name} の言い方に沿わせて"
        "ください（書かれていないことを発明しない）\n"
        "- source_refs: 根拠になったメッセージの msg:N をそのまま写す\n"
        "\n"
        "拾わないのが普通です。写しに確かな根拠のある候補だけを拾い、無ければ"
        "両方とも空配列で構いません。コア記憶や約束はこの作業では扱いません。"
    )


def _parse_candidate_result(
    result: Any, persona_id: Optional[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """機構モードの構造化出力を検証済み dict へ正規化する。

    fail-closed の粒度は :func:`_parse_structured_result` と同じ思想:
    **全体の型** (dict でない / 必須欄の欠落・非配列 / 要素が object でない)
    は :class:`SluiceOutputError` を送出。**要素の中身の不正** (text が空・
    文字列でない等) はその要素だけ落として WARNING に残す — 候補は本人の器に
    触れないので、要素の棄却を本人向けの記録に書く先は無い。
    """
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError) as exc:
            raise SluiceOutputError(
                f"candidate output is not JSON (persona={persona_id}): "
                f"{result[:200]!r}"
            ) from exc
        result = parsed
    if not isinstance(result, dict):
        raise SluiceOutputError(
            f"candidate output is not an object (persona={persona_id}): "
            f"{type(result).__name__}"
        )
    out: Dict[str, List[Dict[str, Any]]] = {}
    for field in _CANDIDATE_LIST_FIELDS:
        value = result.get(field)
        if not isinstance(value, list):
            raise SluiceOutputError(
                f"required field {field!r} is missing or not an array "
                f"(persona={persona_id}): {type(value).__name__}"
            )
        kept: List[Dict[str, Any]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise SluiceOutputError(
                    f"element {field}[{index}] must be an object "
                    f"(persona={persona_id}): {type(item).__name__}"
                )
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                LOGGER.warning(
                    "[sluice-capture] dropped candidate %s[%d]: empty or "
                    "non-string text (persona=%s)", field, index, persona_id,
                )
                continue
            kept.append(item)
        out[field] = kept
    return out


def _candidate_event_date(
    persona: Any, item: Dict[str, Any], chunk_messages: List[Any],
) -> Optional[str]:
    """候補一件の「できごとの日」を機械で導く (B-2 の刻印)。

    根拠 (source_refs の msg:N) が解決できれば、その中で最も新しい
    メッセージの日付。解決できなければチャンクの末尾メッセージの日付で代替。
    どの経路でも LLM の申告は使わない。
    """
    best_ts = 0
    refs = item.get("source_refs")
    if isinstance(refs, list):
        for ref in refs:
            parsed = _parse_ref(ref, _MSG_REF_RE)
            if parsed is None or not (1 <= parsed <= len(chunk_messages)):
                continue
            try:
                ts = int(getattr(chunk_messages[parsed - 1], "created_at", 0) or 0)
            except (TypeError, ValueError):
                continue
            best_ts = max(best_ts, ts)
    if best_ts > 0:
        try:
            return datetime.fromtimestamp(best_ts).date().isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return _message_event_date(
        persona, str(getattr(chunk_messages[-1], "id", "") or "") or None,
    )


def _call_mechanism_llm(
    lifecycle: Any,
    persona: Any,
    chunk_messages: List[Any],
    *,
    model_key: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """チャンク 1 つの機構モード LLM 呼び出し (単発 user プロンプト)。

    Chronicle 生成と同じ型: 本人のシステムプロンプトも現在のコア記憶・手帳の
    一覧も載せない — 判断の主体は機構で、現在の本人の知識を混ぜない。例外
    (LLM エラー・出力不適合) はそのまま送出する。送る中身が実際に使うモデルに
    入らないときは LLM を呼ばずに :class:`SluiceInputTooLargeError` を送出する。
    """
    runtime = lifecycle.runtime
    persona_id = getattr(persona, "persona_id", None)
    persona_name = _mechanism_persona_name(persona)

    # 刻みの見積もり (_capture_input_fit) と同じ決め方でモデルとクライアントを決める。
    llm_client, execution_context = _select_capture_llm(
        lifecycle, persona, model_key,
    )

    prompt = _build_mechanism_prompt(persona_name, chunk_messages)
    messages = [{"role": "user", "content": prompt}]
    _ensure_input_fits(
        messages, execution_context.model_key, persona_id=persona_id,
        llm_client=llm_client, response_schema=_CANDIDATE_SCHEMA,
    )
    result = llm_client.generate(
        messages,
        tools=[],
        response_schema=_CANDIDATE_SCHEMA,
        temperature=runtime._default_temperature(persona),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        **runtime._get_cache_kwargs(persona_id),
    )

    usage = llm_client.consume_usage() if hasattr(llm_client, "consume_usage") else None
    if usage is not None:
        try:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                building_id=getattr(persona, "current_building_id", None),
                node_type="sluice",
                playbook_name="sluice_capture",
                category="sluice",
            )
        except Exception:
            LOGGER.warning(
                "[sluice-capture] usage tracking failed (persona=%s)",
                persona_id, exc_info=True,
            )

    return _parse_candidate_result(result, persona_id)


def _run_mechanism_chunk(
    lifecycle: Any,
    persona: Any,
    chunk_messages: List[Any],
    *,
    model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """チャンク 1 つを機構モードで処理する (候補テーブルへ置くだけ)。

    振り付けは :func:`_run_capture_chunk` の縮約形 (claim → running → 凍結
    (applied) → 冪等適用 → completed)。本人の器 (コア記憶・手帳・約束・会話
    ログ) には一切書かない — 判断ターンの永続 (_persist_record) も無い。
    台帳キーは本人モードと**別** (``#mechanism`` 接尾) — 同じチャンクでも
    主体が違えば別の実行で、互いの記録を再利用しない。
    """
    persona_id = getattr(persona, "persona_id", None)
    chunk_start_id = str(getattr(chunk_messages[0], "id"))
    chunk_end_id = str(getattr(chunk_messages[-1], "id"))

    ledger = _get_ledger(lifecycle)
    ledger_key = (
        f"{persona_id}:{chunk_start_id}#mechanism" if persona_id else None
    )
    recorded = None
    if ledger is not None and ledger_key:
        recorded = _find_recorded_result(ledger, ledger_key)
        if recorded is not None and not all(
            isinstance(recorded.get("response", {}).get(field), list)
            for field in _CANDIDATE_LIST_FIELDS
        ):
            # 形式の読めない記録は再利用しない (別キーで採り直す —
            # _is_legacy_response と同じ fail-closed)。
            LOGGER.warning(
                "[sluice-capture] 機構モードの記録の形式が読めないため再利用"
                "しません (execution=%s key=%s persona=%s)",
                recorded.get("execution_id"), ledger_key, persona_id,
            )
            ledger_key = f"{ledger_key}#format-candidate1"
            recorded = _find_recorded_result(ledger, ledger_key)

    execution_id: Optional[str] = None
    ledger_status: Optional[str] = None
    apply_span_start = chunk_start_id
    apply_span_end = chunk_end_id
    if recorded is not None:
        execution_id = recorded["execution_id"]
        ledger_status = recorded["status"]
        parsed = {
            field: [
                item for item in (recorded["response"].get(field) or [])
                if isinstance(item, dict)
            ]
            for field in _CANDIDATE_LIST_FIELDS
        }
        apply_span_start = str(recorded.get("span_start_id") or chunk_start_id)
        apply_span_end = str(recorded.get("span_end_id") or chunk_end_id)
        LOGGER.info(
            "[sluice-capture] reusing recorded candidates (execution=%s "
            "span=%s..%s persona=%s); no new LLM call",
            execution_id, chunk_start_id, chunk_end_id, persona_id,
        )
    else:
        if ledger is not None and ledger_key:
            execution_id, runnable, existing_status = ledger.claim_execution(
                _LEDGER_KIND, ledger_key, persona_id,
                payload={
                    "span_start_id": chunk_start_id,
                    "span_end_id": chunk_end_id,
                    "origin": "capture_mechanism",
                },
            )
            if not runnable:
                raise SluiceExecutionBlockedError(
                    f"sluice mechanism capture blocked by ledger "
                    f"(key={ledger_key}, status={existing_status})"
                )
            if not ledger.try_mark_running(execution_id):
                raise SluiceExecutionBlockedError(
                    f"sluice mechanism capture running seat lost (key={ledger_key})"
                )
        try:
            parsed = _call_mechanism_llm(
                lifecycle, persona, chunk_messages, model_key=model_key,
            )
        except Exception as exc:
            noter = getattr(lifecycle, "_note_metabolism_rate_limit", None)
            if callable(noter):
                noter(persona_id, exc)
            if execution_id is not None:
                try:
                    ledger.mark_failed(execution_id, str(exc) or type(exc).__name__)
                except Exception:
                    LOGGER.exception(
                        "[sluice-capture] mark_failed itself failed (execution=%s)",
                        execution_id,
                    )
            raise
        if execution_id is not None:
            try:
                ledger.mark_applied(execution_id, result={
                    "response": parsed,
                    "span_start_id": chunk_start_id,
                    "span_end_id": chunk_end_id,
                    "seen_ids": [
                        str(getattr(m, "id")) for m in chunk_messages
                    ],
                })
            except Exception as exc:
                LOGGER.error(
                    "[sluice-capture] freezing candidates failed (execution=%s "
                    "persona=%s); marking failed so the next run can retry",
                    execution_id, persona_id, exc_info=True,
                )
                ledger.mark_failed(execution_id, str(exc) or type(exc).__name__)
                raise
            ledger_status = "applied"

    # 適用: 候補テーブルへ置くだけ (内容一致で冪等 — 再適用で二重に並ばない)。
    from sai_memory.memory.storage import add_sluice_candidate_memo

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or getattr(adapter, "conn", None) is None:
        raise SluiceStorageUnavailableError(
            "memory.db connection is missing; cannot store candidate memos"
        )
    candidates_created = 0
    candidates_total = 0
    for field, kind in (("want_memos", "want"), ("did_memos", "did")):
        for item in parsed.get(field, []):
            candidates_total += 1
            activity_name = item.get("activity_name")
            if not isinstance(activity_name, str) or not activity_name.strip():
                activity_name = None
            else:
                activity_name = activity_name.strip()
            event_date = _candidate_event_date(persona, item, chunk_messages)
            with adapter._db_lock:
                new_id = add_sluice_candidate_memo(
                    adapter.conn,
                    span_start_id=apply_span_start,
                    span_end_id=apply_span_end,
                    kind=kind,
                    activity_name=activity_name,
                    text=str(item.get("text")).strip(),
                    event_date=event_date,
                )
            if new_id is not None:
                candidates_created += 1

    if execution_id is not None and ledger_status == "applied":
        ledger.mark_completed(execution_id)

    LOGGER.info(
        "[sluice-capture] mechanism chunk done: persona=%s span=%s..%s "
        "(%d messages) candidates=%d (new=%d)",
        persona_id, chunk_start_id, chunk_end_id, len(chunk_messages),
        candidates_total, candidates_created,
    )
    return {
        "ops_applied": 0, "ops_failed": 0,
        "memos_applied": candidates_total, "memos_failed": 0,
        "promises_applied": 0, "promises_failed": 0,
        "span_start_id": chunk_start_id, "span_end_id": chunk_end_id,
        "messages": len(chunk_messages),
        "candidates_created": candidates_created,
        "period_start": _chunk_period_dates(chunk_messages)[0],
        "period_end": _chunk_period_dates(chunk_messages)[1],
    }


#: 後から通す採取の判断の主体 (docs/intent/sluice_coverage_gaps.md B 節)。
#: 'mechanism' = 機構が候補を拾う (安い方 — 既定) / 'persona' = 現在の本人が
#: 読み返す。
CAPTURE_MODES = ("mechanism", "persona")


#: 読み返しのダイジェスト一行のタグ。旧 ``sea.work_session.DIGEST_TAG`` と同値
#: (作業セッションは段 1-4 で撤去)。Chronicle の編纂 (sai_memory/arasuji/
#: generator.py の ``_message_kind_label``) がこのリテラルで「既に要約された
#: まとめ」を見分けるので、値を変えないこと。
_CAPTURE_DIGEST_TAG = "session_digest"


def _capture_digest_exists(persona: Any, nonce: str) -> bool:
    """この nonce のダイジェスト一行が、既に本線に立っているか。

    「追記は成功したが消し込み (:func:`_clear_pending_digest`) が落ちた」あとの
    再実行が、同じ一行をもう一度立てないための照会 (Codex 第二巡 修正 C)。
    本人の目には「同じ読み返しが二度あった」ように見えるのを止める。

    索引の無い全走査だが、走るのは走行の締めの flush のときだけ (キャンペーンに
    一度) — 索引を足してまで速くする対象ではない。
    """
    adapter, conn = _pending_digest_conn(persona)
    with adapter._db_lock:
        row = conn.execute(
            "SELECT 1 FROM messages "
            "WHERE json_extract(metadata, '$.capture_digest_nonce') = ? "
            "LIMIT 1",
            (nonce,),
        ).fetchone()
    return row is not None


def _append_capture_digest(
    persona: Any, digest_text: str, *, nonce: Optional[str] = None,
) -> None:
    """読み返しのダイジェスト一行を本線へ立てる (長期記憶への入口は一本)。

    器は旧作業セッションのダイジェスト行と同じ形 (:data:`_CAPTURE_DIGEST_TAG`
    / main_line / committed) を使う — Chronicle の編纂 (sai_memory/arasuji/
    generator.py) がこのタグを「既に要約されたまとめ」として扱うので、生の会話
    として再展開されない。旧来の読み手だった一日新聞 (day_report) と就寝判断
    (day_close) は段 1-4 で撤去された。
    role は旧作業セッションの digest (assistant = 本人の言葉) と違って
    user + ``<system>`` 包み — この一行は件数から機械が組んだ文で、機構の
    代筆を本人名義 (assistant) にしない (発話の尊厳の規律)。
    書き込みの失敗は送出する — 採取は適用済みなので、呼び出し元のジョブが
    失敗として報告し、記録の欠けを黙って飲まない。

    ``nonce`` は :func:`_capture_digest_exists` が照会する識別子で、metadata の
    ``capture_digest_nonce`` として一行に刻まれる (修正 C)。
    """
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None:
        raise SluiceStorageUnavailableError(
            "sai_memory adapter is missing; cannot append the capture digest"
        )
    metadata: Dict[str, Any] = {"tags": [_CAPTURE_DIGEST_TAG, "sluice"]}
    if nonce:
        metadata["capture_digest_nonce"] = nonce
    message_id = adapter.append_persona_message({
        "role": "user",
        "content": f"<system>{digest_text}</system>",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "line_role": "main_line",
        "scope": "committed",
    })
    if not message_id:
        # adapter は行が入らなかったとき (未準備・INSERT 例外) に例外でなく
        # None を返す — docstring の「書き込みの失敗は送出する」の契約は
        # ここで実装する (2026-09-09 Codex 第三巡)。送出しないと呼び出し元が
        # 材料 (pending) を消し込み、一行が立たないまま回収不能になる。
        raise SluiceStorageUnavailableError(
            "capture digest append returned no message id; keeping the "
            "pending tally for the next run"
        )


def run_sluice_capture(
    lifecycle: Any,
    persona: Any,
    *,
    mode: str = "mechanism",
    model_key: Optional[str] = None,
    event_callback: Optional[Any] = None,
    cancellation_token: Optional[Any] = None,
) -> Dict[str, Any]:
    """記録された「通っていない範囲」を、チャンクごとに順に通す。

    docs/intent/sluice_coverage_gaps.md 第一段 B の実行部。範囲の行を古い順に
    取り、A と同じ閾値 (:func:`get_max_span_chars`) 以下で、かつ使うモデルに
    入る量 (:func:`_capture_input_fit` — docs/issues/sluice_skip_ignores_model_context.md)
    以下のチャンクへ刻んで処理する。1 通だけでモデルに入らないメッセージが
    あれば、LLM を呼ぶ直前に :class:`SluiceInputTooLargeError` で止まる。
    チャンクを終えるたびに範囲の行を縮める (行の start_message_id を
    前進、全部済んだら行を削除) — 中断しても続きから。パンマーカーは動かさない。

    判断の主体 (``mode`` — B 節の再設計):

    - ``"mechanism"`` (既定 — 安い方): 誰でもない機構が範囲を読み、手帳のメモの
      **候補**を ``sluice_candidate_memos`` に置く (:func:`_run_mechanism_chunk`)。
      本人の器 (コア記憶・手帳・約束・会話ログ) には何も書かない。
    - ``"persona"``: 現在の本人が「読み返し」だと明示されて読み、コア記憶・
      手帳・約束を定常のスルースと同じ経路で操作する
      (:func:`_run_capture_chunk`)。拾われたメモは origin='readback' と
      できごとの日 (event_date) の機械刻印を持つ。読み返しの過程は本線の
      context に載せず (判断ターン記録は discardable)、範囲を全部通し終えた
      走行で、貯まっている採取が 1 件以上あれば本線にダイジェスト一行だけを
      立てる (:func:`_append_capture_digest` — 入口は一本)。件数の材料は
      チャンクごとに memory.db へ貯める (:func:`_merge_pending_digest`) ので、
      中断した走行の採取分も次の完走のダイジェストに合流する。採取ゼロなら
      立てない。

    直列化: チャンクごとに Beat ロック (purpose="sluice_capture") を取る —
    ロックはチャンク間で手放すので、走行中も会話 (Pulse) が間に挟まれる
    (「普通の会話を妨げない」が本設計の芯)。

    小休止 (C-1): チャンクの頭ごとに persona 単位のレート制限小休止を見て、
    小休止中なら走行を閉じる (status="cooldown" — 進んだ分は確定済み)。
    チャンクの LLM がレート制限で落ちた場合も同じ小休止が置かれる
    (:func:`_run_capture_chunk`)。

    Returns:
        ``{"status": "ok"|"noop"|"disabled"|"cooldown"|"cancelled",
        "chunks_processed": int, "messages_processed": int,
        "captures_applied": int, "captures_failed": int,
        "spans_remaining": int}``。例外 (LLM 失敗・台帳ブロック・範囲が
        読めない) は送出する — 進んだ分の縮めは確定済みなので、再実行は
        続きから。
    """
    from sai_memory.memory.storage import (
        advance_sluice_skipped_span,
        delete_sluice_skipped_span,
        list_sluice_skipped_spans,
    )

    if mode not in CAPTURE_MODES:
        raise ValueError(
            f"unknown capture mode: {mode!r} (expected one of {CAPTURE_MODES})"
        )

    if not is_enabled():
        # スルースを env で切っている環境では、後から通す採取も動かさない —
        # 「採取 (課金) を止めている」意思の尊重 (定常のスルースと同じ判定)。
        return {
            "status": "disabled", "chunks_processed": 0,
            "messages_processed": 0, "captures_applied": 0,
            "captures_failed": 0, "spans_remaining": 0,
        }

    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not getattr(adapter, "is_ready", lambda: False)():
        raise SluiceStorageUnavailableError(
            f"persona memory storage is not ready (persona={persona_id}); "
            "cannot capture skipped spans"
        )

    from sea.beat_gate import hold_beat
    manager = getattr(lifecycle, "manager", None)
    max_chars = get_max_span_chars()

    chunks_processed = 0
    messages_processed = 0
    captures_applied = 0
    captures_failed = 0
    status = "ok"
    processed_any = False
    # 本人モードのダイジェスト一行の材料 (期間と器ごとの件数) は memory.db 側に
    # 貯める (_merge_pending_digest) — in-memory の累計だと、中断した走行の
    # 採取分がどの走行のダイジェストにも入らない。

    def _spans() -> List[Dict[str, Any]]:
        with adapter._db_lock:
            return list_sluice_skipped_spans(adapter.conn)

    def _emit(content: str) -> None:
        if event_callback is None:
            return
        try:
            event_callback({
                "type": "metabolism",
                "status": "sluice_capture",
                "content": content,
                "messages_processed": messages_processed,
            })
        except Exception:
            LOGGER.debug("[sluice-capture] event_callback raised", exc_info=True)

    while True:
        if cancellation_token is not None and cancellation_token.is_cancelled():
            status = "cancelled"
            break
        rate_check = getattr(lifecycle, "_metabolism_rate_limit_active", None)
        if callable(rate_check) and rate_check(persona_id):
            LOGGER.info(
                "[sluice-capture] stopping during rate-limit cooldown "
                "(persona=%s)", persona_id,
            )
            status = "cooldown"
            break
        spans = _spans()
        if not spans:
            status = "ok" if processed_any else "noop"
            break
        span = spans[0]
        span_messages = _read_span_messages(persona, span)
        if span_messages is None:
            raise SluiceCaptureSpanUnreadableError(
                f"recorded span {span['start_message_id']}.."
                f"{span['end_message_id']} (row {span['id']}) cannot be read "
                f"(missing endpoint or cross-thread; persona={persona_id})"
            )
        if not span_messages:
            # 範囲に実会話が 1 通も無い (機構の記録だけ等) — 本人の目で読む
            # 材料が無いので、記録を閉じて次へ。
            with adapter._db_lock:
                delete_sluice_skipped_span(adapter.conn, span["id"])
            LOGGER.info(
                "[sluice-capture] span %s..%s had no conversation messages; "
                "record closed (persona=%s)",
                span["start_message_id"], span["end_message_id"], persona_id,
            )
            processed_any = True
            continue
        with hold_beat(
            manager, persona_id, purpose="sluice_capture", check_gate=False,
        ):
            # 刻みはチャンクごとに、ロックの内側で見積もり直す — 本人モードでは
            # 採取でコア記憶や手帳が増えると指示文が伸びるので、走行の頭の
            # 見積もりのままだと後半のチャンクがモデルに入らなくなりうる。
            fit = _capture_input_fit(
                lifecycle, persona, mode=mode, model_key=model_key,
            )
            chunk = _plan_capture_chunks(span_messages, max_chars, fit)[0]
            if mode == "persona":
                chunk_summary = _run_capture_chunk(
                    lifecycle, persona, chunk, model_key=model_key,
                )
            else:
                chunk_summary = _run_mechanism_chunk(
                    lifecycle, persona, chunk, model_key=model_key,
                )
        chunk_applied = (
            chunk_summary["ops_applied"] + chunk_summary["memos_applied"]
            + chunk_summary["promises_applied"]
        )
        if mode == "persona" and chunk_applied:
            # ダイジェストの材料は memory.db に貯める — この走行が中断しても
            # (cancelled / cooldown / 例外)、次に完走した走行が本線の一行を
            # 立てて拾う。
            #
            # **縮めより先**に足す (Codex 第二巡 修正 B)。逆順だと「縮めは確定 →
            # マージが落ちる」の窓で、チャンクは台帳上完了済みなのに件数が
            # どこにも残らず、そのチャンクの適用分がダイジェストから永久に
            # 欠けた。マージを先にしても二重にはならない — 失敗の各窓の帰結:
            #   - マージ後・縮め前に落ちる → 再実行は台帳の記録済み結果で同じ
            #     チャンクをやり直し、マージは last_chunk_id で二重加算を止め、
            #     縮めだけが進む。
            #   - マージ前に落ちる → 縮めも進んでいないので、再実行がマージから
            #     やり直す。
            # どちらも欠けも二重も無い。残る縁が一つ (2026-09-09 Codex 第三巡、
            # 記録して受け入れ): マージ前に落ちた回の再適用は、冪等スキップを
            # applied に数えない操作 (コア記憶の remove 等) の件数だけ少なく
            # 数える。実体は器に残っており、欠けるのは通知の件数が控えめに
            # 言うことだけ — 件数を台帳に凍結する架構はこの縁には過大。
            _merge_pending_digest(
                persona,
                chunk_start_id=chunk_summary.get("span_start_id"),
                messages=chunk_summary["messages"],
                memos=chunk_summary["memos_applied"],
                core=chunk_summary["ops_applied"],
                promises=chunk_summary["promises_applied"],
                period_start=chunk_summary.get("period_start"),
                period_end=chunk_summary.get("period_end"),
            )
        # 処理し終えたチャンクぶんだけ記録を縮める。縮めの失敗は送出 —
        # 縮めずに次へ進むと同じチャンクを永久に回る。再実行は台帳の記録済み
        # 結果に合流するので、縮め直前の失敗でも二重適用にはならない。
        if len(chunk) == len(span_messages):
            with adapter._db_lock:
                delete_sluice_skipped_span(adapter.conn, span["id"])
        else:
            next_start = str(getattr(span_messages[len(chunk)], "id"))
            with adapter._db_lock:
                advance_sluice_skipped_span(adapter.conn, span["id"], next_start)
        processed_any = True
        chunks_processed += 1
        messages_processed += chunk_summary["messages"]
        captures_applied += chunk_applied
        captures_failed += (
            chunk_summary["ops_failed"] + chunk_summary["memos_failed"]
            + chunk_summary["promises_failed"]
        )
        if mode == "persona":
            _emit(
                f"過去の会話を読み返しています…… ({messages_processed} 通まで採取済み)"
            )
        else:
            _emit(
                f"過去の会話から候補を探しています…… ({messages_processed} 通まで処理済み)"
            )

    # 本人モードのダイジェスト一行 (入口は一本): 記録された範囲を全部通し終えた
    # 走行 (status "ok"、または貯まった材料だけが残っていて今回は範囲ゼロだった
    # "noop") で、貯まっている適用数が 1 件以上あるときに本線へ一行を立てる。
    # 件数は in-memory のカウンタではなく耐久化した累計から組む — 前の走行が
    # 中断していたら、その採取分もここで一緒に報告される。追記が失敗したら
    # 材料は残したまま送出する (ジョブは失敗になるが、次の完走が拾う = 復旧経路)。
    if mode == "persona" and status in ("ok", "noop"):
        pending = _load_pending_digest(persona)
        if _pending_digest_total(pending) > 0:
            # 追記の識別子は**追記より先に**永続する (Codex 第二巡 修正 C)。
            # 「追記は成功・消し込みは失敗」で落ちると材料が残ったままになり、
            # 次の完走が同じ一行をもう一度本線へ立てていた (本人の目には同じ
            # 読み返しが二度あったように見える)。先に採番して残しておけば、
            # 再実行はその nonce の一行が既にあるかを見て追記を飛ばせる。
            flush_nonce = pending.get("flush_nonce")
            if not flush_nonce:
                flush_nonce = str(uuid.uuid4())
                pending["flush_nonce"] = flush_nonce
                _write_pending_digest(persona, pending)
            pending_start = pending["period_start"]
            pending_end = pending["period_end"]
            pending_messages = int(pending["messages"] or 0)
            if pending_start and pending_end:
                period_label = (
                    pending_start if pending_start == pending_end
                    else f"{pending_start}〜{pending_end}"
                )
                period_part = f"（期間 {period_label}、{pending_messages} 通）"
            else:
                period_part = f"（{pending_messages} 通）"
            parts: List[str] = []
            if pending["memos"]:
                parts.append(f"手帳のメモ {pending['memos']} 件")
            if pending["core"]:
                parts.append(f"コア記憶の操作 {pending['core']} 件")
            if pending["promises"]:
                parts.append(f"約束の操作 {pending['promises']} 件")
            digest_text = (
                f"過去の会話{period_part}を読み返し、{'・'.join(parts)}を記録した。"
            )
            if not _capture_digest_exists(persona, flush_nonce):
                _append_capture_digest(
                    persona, digest_text, nonce=flush_nonce,
                )
            else:
                LOGGER.info(
                    "[sluice-capture] the digest line for %s is already on the "
                    "main line; skipping the append and clearing the tally",
                    flush_nonce,
                )
            _clear_pending_digest(persona)

    LOGGER.info(
        "[sluice-capture] run closed: persona=%s mode=%s status=%s chunks=%d "
        "messages=%d applied=%d failed=%d",
        persona_id, mode, status, chunks_processed, messages_processed,
        captures_applied, captures_failed,
    )
    return {
        "status": status,
        "mode": mode,
        "chunks_processed": chunks_processed,
        "messages_processed": messages_processed,
        "captures_applied": captures_applied,
        "captures_failed": captures_failed,
        "spans_remaining": len(_spans()),
    }


# ---------------------------------------------------------------------------
# 担当範囲の通数勘定 (:func:`run_sluice` がプロンプトへ載せる材料)
# ---------------------------------------------------------------------------

def _count_new_since_marker(
    current_messages: List[Dict[str, Any]], last_pan_id: Optional[str],
) -> int:
    """マーカー (前回採取した末尾 id) 以降の「新規」件数を数える。

    - マーカー無し (None) → 全件が新規。
    - マーカーが窓内にあれば、その次以降の件数。
    - マーカーが窓外 (押し出されて消えた) → 全件が新規。

    マーカーが複数一致する場合は後勝ち (最新) を採用する。
    """
    if not current_messages:
        return 0
    if not last_pan_id:
        return len(current_messages)
    idx: Optional[int] = None
    for i, msg in enumerate(current_messages):
        mid = msg.get("id") if isinstance(msg, dict) else None
        if mid == last_pan_id:
            idx = i
    if idx is None:
        return len(current_messages)
    return len(current_messages) - idx - 1
