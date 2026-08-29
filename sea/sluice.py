"""sluice (スルース) — 押し出される記憶が必ず通る採取の関所。

Metabolism の eviction 直前、メインラインの温まった prefix (head + 履歴) に注入
プロンプトを 1 手足し、structured output で「退場前に記録すべきもの」を返させる。
決定は本人 (ペルソナ)、複製・書き込みはシステム側の決定論。

採取する器は三つ (autonomous_behavior_v3.md §13.3 / §13.6):

- コア記憶 (core_adds / core_updates / core_removes) — 恒常知識
- 手帳メモ (want_memos / did_memos) — アクティビティへの日付つき一行
- 約束 (promises) — タスク帳への add / update

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
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

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
        "promises": {
            "type": "array",
            "description": "ユーザーとの約束・依頼のタスク帳への操作列。無ければ空配列。",
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["add", "update"],
                    },
                    "content": {
                        "type": "string",
                        "description": "約束の中身。",
                    },
                    "due": {
                        "type": "string",
                        "description": "期限 (YYYY-MM-DD)。期限が明示されていない約束では省略する — 期限を発明しない。",
                    },
                    "clear_due": {
                        "type": "boolean",
                        "description": "期限が撤回されたときだけ true (update で期限を外す)。省略 = 期限は変更しない。due と同時には指定しない。",
                    },
                    "task_ref": {
                        "type": "string",
                        "description": "update 対象の task_id。同梱の「開いているタスク帳」一覧から選ぶ。",
                    },
                },
                "required": ["op"],
            },
        },
    },
    # 全欄必須 (Codex 第七巡 修正 1): 「空」は明示的な空配列だけ。欄の省略を
    # 「採取なし」へ丸めると、モデルが欄を出力しなかった回の採取が静かに失われ、
    # ゲート (§13.3) が通ったことにされる。
    "required": [
        "reflection", "core_adds", "core_updates", "core_removes",
        "want_memos", "did_memos", "promises",
    ],
}

#: スルースの LLM コールの出力上限。実測の応答は 195〜411 トークン
#: (docs/issues/sluice_structured_output_digit_loop.md の再現性実験) なので、
#: 4,096 は正常な応答を切らない。目的は暴走したときの被害の頭打ち — 旧型の
#: 本番失敗は 1 回あたり 79 秒・数万トークンを焼いていた。
_MAX_OUTPUT_TOKENS = 4096


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
    update の task_ref が書けないだけでなく、会話で言及され続けている同じ約束を
    次のスルースが再び add して重複する。

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
    return list_open(manager, persona_id)


def _format_task_line(task: Dict[str, Any]) -> str:
    """タスク一件を ``[task:ID] 中身 (期限: …)`` の一行に整形する。

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
    return f"- [task:{task.get('task_id')}] {task.get('content')} ({due_label})"


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

    ⚠ コンテキスト超過の後退 (§13.5-1) が起きた回は、実際に見せる通数がここで
    宣言した数より少なくなる (プロンプトは後退前に一度だけ組む)。「それより前は
    採取済み」の部分は後退しても真のままなので、件数だけが概数になる。

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
) -> str:
    """<system> 包みの注入プロンプトを組む (keepalive の末尾通知と同じ面)。

    ``core_memories`` / ``total_chars`` は :func:`_read_core_state` の読みを
    呼び出し元から受け取る (CAS スナップショットと同じ姿を見せるため)。
    ``span_new_count`` は今回の担当範囲の通数 (None = 窓全体) — 手帳の節で
    対象範囲を明示するのに使う (:func:`_scope_sentence`)。``today_memos`` は
    :func:`_list_today_memos` の読み (今日すでに書いたメモ) — 無ければその節を
    出さない。
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
        task_block = "\n".join(_format_task_line(t) for t in open_tasks)
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
        f"- {_scope_sentence(span_new_count)}\n"
        "- この範囲で、やりたいと思ったこと・実際にやったことはありますか?\n"
        "  無ければ空で構いません。あれば、活動の名前（「小説を書く」「絵の練習」\n"
        "  のような粒度。下の一覧にあるものは act:N で参照）と、今日の中身\n"
        "  一行 (text) で。\n"
        f"{today_block}"
        "\n"
        "3) 手帳の約束の欄 (promises):\n"
        "- ユーザーとの約束や引き受けた依頼が生まれたり変わったりしていましたか?\n"
        "  無ければ空で構いません。期限が明示されていないなら due は書かないで\n"
        "  ください（期限を発明しない）。既に下の「手帳の約束の欄」にあるものを再び\n"
        "  add する必要はありません — 内容や期限に変化があれば、その task の ID を\n"
        "  task_ref にして update を使えます。期限が撤回されたときは clear_due で\n"
        "  期限を外せます。\n"
        "\n"
        "姿勢:\n"
        "- **採取しないのが普通です。** ほとんどの記憶整理では何も採りません\n"
        "  （各欄は空配列）。無理に何かを刻もうとしないでください。\n"
        "- 応答には全ての欄 (reflection / core_adds / core_updates / core_removes /\n"
        "  want_memos / did_memos / promises) を含めてください。採るものが無い欄は空配列で。\n"
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
) -> tuple[int, int, List[str]]:
    """want/did メモを手帳 (pocketbook) に書く。(成功数, 失敗数, 結果行) を返す。

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
      日・同じアクティビティ・同じ種類・同じ本文の既存メモがあればスキップする
      (成功扱い)。冪等キーは担当範囲が変わると別キーになるので、繰り越された
      回の再提案を止められない。照合は書き込みと同じロック・同じトランザク
      ションの中で行う (docs/issues/sluice_memo_duplicate_across_spans.md)。
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
                # 二つ入っていた場合もここで止まる。
                duplicate = find_memo_by_content(conn, aid, today, kind, text)
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
    promises: List[Dict[str, Any]],
    *,
    idem_prefix: str,
    span_start_id: Optional[str],
    span_end_id: Optional[str],
    offered_tasks: Dict[str, Any],
) -> tuple[int, int, List[str]]:
    """promises をタスク帳 (task_book) に適用する。(成功数, 失敗数, 結果行) を返す。

    ``offered_tasks`` は ``{task_id: スナップショット時点の revision}``。値の
    ``None`` は「revision 列が NULL の行」という正当な期待値で、
    :data:`_REVISION_UNKNOWN` は「スナップショット不明」(旧形式の台帳記録) —
    後者の update は棄却する。

    - add は origin='sluice'・安定 idem_key (span 由来プレフィックス + 操作番号)
      で冪等化・origin_ref に span 由来の参照。同じ担当範囲の再適用で重複しない。
    - counterpart は 'user' 固定 — 応答スキーマに相手欄が無いため実装上の既定。
      スルースの捕獲対象はユーザーとの会話から生まれる約束で、相手の既定は
      ユーザーが最も嘘が少ない。ペルソナ同士の約束を拾うと相手名義がユーザーに
      化ける限界がある (スキーマに相手欄を足すまでの割り切り)。
    - due は日付文字列を epoch へ変換。解釈不能 (書式不正・範囲外・環境の
      timestamp() 例外) のときも**約束は失わない** (まはー裁定 2026-08-19 —
      タスク帳の芯は「失くすことが許されない」): add は期限なしで保存し、
      update は期限の変更だけを見送る (content 等の変更は適用)。どちらも
      判断ターン記録に明記する (発明しない・黙って落とさない・約束は失わない)。
    - ``clear_due=True`` は update で期限を外す (期限の撤回)。due との同時指定は
      矛盾なので入力不正として要素棄却する。
    - update は task_ref をプロンプトに同梱した一覧 (``offered_tasks``) で検証し、
      一覧に無い id はその要素だけ棄却してログに残す (activity_id と同じ閉語彙の
      規律 — LLM の発明 id を書かせない)。
    - update / clear_due は**スナップショット時点の revision で CAS** する
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
    if not promises:
        return (0, 0, [])

    manager = getattr(lifecycle, "manager", None)
    persona_id = getattr(persona, "persona_id", None)
    if manager is None or not hasattr(manager, "SessionLocal") or not persona_id:
        return (0, len(promises), ["タスク帳ストレージが利用できず、約束を書けませんでした。"])

    from saiverse.task_book import TaskBookError, add_entry, update_entry

    if span_start_id and span_end_id and span_start_id != span_end_id:
        origin_ref = f"{span_start_id}..{span_end_id}"
    else:
        origin_ref = span_end_id or span_start_id

    applied = 0
    failed = 0
    lines: List[str] = []

    for idx, promise in enumerate(promises):
        if not isinstance(promise, dict):
            failed += 1
            lines.append(f"不正な約束形式のためスキップ: {promise!r}")
            continue
        op = promise.get("op")
        content = (promise.get("content") or "").strip()
        due_raw = (promise.get("due") or "").strip()
        due_at, due_error = parse_due(due_raw)
        if due_error is not None:
            # 約束は失わない (まはー裁定): 期限だけを落とし、記録に明記する。
            LOGGER.warning(
                "[sluice] unparsable due; keeping the promise without a "
                "deadline (persona=%s, op=%s): %s",
                persona_id, op, due_error,
            )
        clear_due = promise.get("clear_due")
        if clear_due is not None and not isinstance(clear_due, bool):
            failed += 1
            lines.append(
                f"約束 {op} をスキップ: clear_due が真偽値ではありません ({clear_due!r})。"
            )
            continue
        if clear_due and due_raw:
            # 「期限を外す」と「期限を設定する」の同時指定は矛盾 — 入力不正として
            # 要素棄却 (どちらの意図か発明しない)。
            failed += 1
            lines.append(
                f"約束 {op} をスキップ: due と clear_due は同時に指定できません。"
            )
            continue
        try:
            if op == "add":
                if not content:
                    failed += 1
                    lines.append("約束 add をスキップ: content が空でした。")
                    continue
                add_entry(
                    manager, persona_id, content,
                    origin="sluice",
                    due_at=due_at,
                    counterpart="user",
                    origin_ref=origin_ref,
                    idem_key=f"{idem_prefix}:p{idx}",
                )
                applied += 1
                suffix = f"（期限 {due_raw}）" if due_at is not None else "（期限なし）"
                lines.append(f"タスク帳に約束を追加: {content}{suffix}")
                if due_error is not None:
                    lines.append(
                        f"期限『{due_raw}』を解釈できなかったため期限なしで登録しました。"
                    )
            elif op == "update":
                task_ref = (promise.get("task_ref") or "").strip()
                if not task_ref:
                    failed += 1
                    lines.append("約束 update をスキップ: task_ref がありません。")
                    continue
                if task_ref not in offered_tasks:
                    failed += 1
                    lines.append(
                        f"約束 update をスキップ: task_ref={task_ref!r} は一覧にありません。"
                    )
                    LOGGER.warning(
                        "[sluice] promise update rejected: task_ref %r not in "
                        "offered list (persona=%s)", task_ref, persona_id,
                    )
                    continue
                expected_revision = offered_tasks.get(task_ref)
                if expected_revision is _REVISION_UNKNOWN:
                    # 旧形式の台帳記録から復元した一覧 — スナップショット時点の
                    # revision が分からない (Codex 第八巡 修正 4)。None で渡すと
                    # CAS が無効化されて古い判断が現在値を上書きするので、
                    # コア記憶のスナップショット欠落と同じく要素棄却にする。
                    failed += 1
                    lines.append(
                        f"約束 update をスキップ: タスク {task_ref} の"
                        "スナップショット情報が無いため適用しませんでした。"
                    )
                    LOGGER.warning(
                        "[sluice] promise update rejected: snapshot revision "
                        "unknown for task %s (persona=%s)", task_ref, persona_id,
                    )
                    continue
                kwargs: Dict[str, Any] = {}
                if content:
                    kwargs["content"] = content
                if clear_due:
                    # 期限の撤回 (update_entry の明示 due_at=None)。promises 行は
                    # counterpart='user' なので「期限も相手も無い行」の受け入れ
                    # 不変条件には抵触しない。相手なしの既存行が対象だったときは
                    # update_entry の TaskBookError が要素失敗として拾う。
                    kwargs["due_at"] = None
                elif due_at is not None:
                    kwargs["due_at"] = due_at
                if not kwargs:
                    failed += 1
                    lines.append(
                        f"約束 update をスキップ: 変更内容がありません ({task_ref})。"
                    )
                    if due_error is not None:
                        lines.append(
                            f"期限『{due_raw}』を解釈できなかったため、"
                            f"タスク {task_ref} の期限は変更しませんでした。"
                        )
                    continue
                try:
                    update_entry(
                        manager, persona_id, task_ref,
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
                        lines.append(f"約束 update の適用に失敗: {exc}")
                    else:
                        lines.append(
                            f"タスク {task_ref} は実行中に変更されたため適用しませんでした。"
                        )
                    LOGGER.warning(
                        "[sluice] promise update rejected (persona=%s, task=%s): %s",
                        persona_id, task_ref, exc,
                    )
                    continue
                applied += 1
                if clear_due:
                    lines.append(
                        f"タスク帳の約束 {task_ref} を更新: {content or '(期限のみ)'}"
                        "（期限を撤回）"
                    )
                else:
                    lines.append(
                        f"タスク帳の約束 {task_ref} を更新: {content or '(期限のみ)'}"
                    )
                if due_error is not None:
                    lines.append(
                        f"期限『{due_raw}』を解釈できなかったため、"
                        f"タスク {task_ref} の期限は変更しませんでした。"
                    )
            else:
                failed += 1
                lines.append(f"未知の約束 op '{op}' をスキップしました。")
        except TaskBookError as exc:
            # revision 競合・LLM の発明 task_ref・受け入れ不変条件違反など。
            # 要素単位の失敗としてログと結果行に残し、スルース全体は止めない。
            failed += 1
            lines.append(f"約束 {op} の適用に失敗: {exc}")
            LOGGER.warning(
                "[sluice] promise op failed (persona=%s, op=%s): %s",
                persona_id, op, exc,
            )

    return (applied, failed, lines)


# ---------------------------------------------------------------------------
# 永続化 (判断ターンをペルソナの記憶に残す)
# ---------------------------------------------------------------------------

def _persist_record(
    persona: Any,
    record_text: str,
    prompt_snapshot: str,
    *,
    applied_total: int,
) -> None:
    """判断ターンを main_line / (committed|discardable) で SAIMemory に残す。

    採取ありなら committed (コンテキストに残る来歴)、なしなら discardable
    (DB には残るが context 復元から除外)。生 JSON は保存しない (自然文のみ)。

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
        "scope": "committed" if applied_total > 0 else "discardable",
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
    from sai_memory.memory.storage import get_embed_metadata, set_embed_metadata
    with adapter._db_lock:
        value = get_embed_metadata(conn, _PAN_MARKER_KEY)
        if not value:
            # 旧世代キーからの一回きり移行 (見つかれば新キーへ写す)。
            value = get_embed_metadata(conn, _LEGACY_PAN_MARKER_KEY)
            if value:
                set_embed_metadata(conn, _PAN_MARKER_KEY, value)
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


# ---------------------------------------------------------------------------
# コンテキスト超過の後退 (§13.5-1)
# ---------------------------------------------------------------------------

#: 超過エラー判定の文字列マーカー (プロバイダ横断のヒューリスティック)。
_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "context_length_exceeded",
    "too many tokens",
    "token limit",
    "input token count",
    "prompt is too long",
    "request too large",
    "exceeds the maximum",
)


def _is_context_overflow(exc: BaseException) -> bool:
    """例外が「プロンプトがコンテキストに入りきらない」超過エラーかを判定する。

    プロバイダ共通の超過例外型が無いため、例外文字列 (original_error 含む) の
    マーカー照合で判定する。判定に漏れた超過エラーは通常の失敗として退場停止 →
    次回再試行の経路に乗る (取りこぼしても fail-closed)。
    """
    texts = [str(exc)]
    original = getattr(exc, "original_error", None)
    if original is not None:
        texts.append(str(original))
    blob = " ".join(texts).lower()
    return any(marker in blob for marker in _CONTEXT_OVERFLOW_MARKERS)


def _drop_last_exchange(
    context_messages: List[Dict[str, Any]],
) -> Optional[tuple[List[Dict[str, Any]], int]]:
    """担当範囲の直近のプロンプト+応答の組を一つ外す (§13.5-1 の後退方式)。

    末尾から最後の user メッセージを探し、そこから末尾まで (user プロンプトと
    それに続く応答) をまとめて外す。外せる組が無ければ None。
    """
    last_user = None
    for i in range(len(context_messages) - 1, -1, -1):
        msg = context_messages[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = i
            break
    if last_user is None:
        return None
    return (context_messages[:last_user], len(context_messages) - last_user)


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

    Codex 第八巡 修正 2: 全交換がコンテキスト超過の後退で外れる等で「何も見て
    いない」結果になったとき、それを適用・確定してしまうと、マーカーは進まない
    のに台帳が completed になり、以降は**同じ安定キーの記録が永久に再利用**
    されて新しい LLM コールが起きない (退場側は空の見た集合を拒むので、末尾の
    退場が止まったままになる)。完了の条件は「最低 1 通は本人の目を通った」—
    満たさない結果は適用前に送出し、mark_failed → 次回の新しい LLM コールで
    やり直す (副作用ゼロの段なので再試行が安全)。
    """


_LIST_FIELDS = (
    "core_adds", "core_updates", "core_removes",
    "want_memos", "did_memos", "promises",
)

#: コア記憶の三一覧 (棄却の件数を「コア記憶の操作」へ束ねるときに使う)。
_CORE_FIELDS = ("core_adds", "core_updates", "core_removes")

#: 判断ターン記録・ログ用の欄の呼び名 (まはーが読む面には実装名を出さない)。
_FIELD_LABELS: Dict[str, str] = {
    "core_adds": "コア記憶の追加",
    "core_updates": "コア記憶の書き換え",
    "core_removes": "コア記憶の削除",
    "want_memos": "やりたいメモ",
    "did_memos": "やったメモ",
    "promises": "約束",
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
    "promises": {
        "op": "string", "content": "string", "due": "string",
        "clear_due": "boolean", "task_ref": "string",
    },
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
    """要素の中で最初に見つかった型不正の説明。問題が無ければ None。

    値の **省略と null は未指定**として通す (適用側が既定へ落とし、必要な欄が
    無ければそこで要素棄却になる)。値があるのに型が違うものだけを拾う。
    """
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
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """LLM の構造化出力を検証済み dict へ正規化する。不適合は SluiceOutputError。

    Returns:
        ``(検証済みの応答, 棄却した要素の記録)``。棄却の記録は
        ``{"field": 欄名, "text": 判断ターンに残す一行}`` の列。

    fail-closed の粒度: **全体の型** (dict でない / 必須欄 — reflection と
    4 つの操作列全部 — の欠落・null / 各欄が配列でない / 配列要素が object で
    ない / reflection が文字列でない) は送出。
    **要素内フィールドの型不正** (content や memory_ref が文字列でない等) は
    その要素だけ落として棄却の記録に残す (Codex 第八巡 修正 6)。
    **中身の参照・値の不正** (空本文、``core:N`` / ``act:N`` の形でない参照、
    一覧に無い参照、未知の promise op、解釈不能な due) は従来どおり適用側の
    要素単位棄却に委ねる。
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
    for field in _LIST_FIELDS:
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
# ①退場が止まっている間に窓へ新着が積まれる ②コンテキスト超過の後退で縮む、
# の二通りで試行ごとに動き、キーに含めると「同じ仕事の再試行」が別キーになって
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


#: 応答形式の世代印。旧世代 (``ops`` 一本) の記録が同じ担当範囲に残っている
#: 環境で、新しい実行を**別キー**に立てるために使う。台帳は applied → failed の
#: 遷移を許さない (状態機械の規約) ので、読めない旧行は退避も上書きもせず
#: そのまま残し、こちらが別キーで走り直す。
_RESPONSE_FORMAT_TAG = "core3"


def _get_ledger(lifecycle: Any) -> Optional[Any]:
    """manager 所有の ExecutionLedger を引く。無ければ None (台帳なしで動く)。"""
    manager = getattr(lifecycle, "manager", None)
    if manager is None:
        return None
    return getattr(manager, "execution_ledger", None)


def _is_legacy_response(response: Any) -> bool:
    """記録済み結果が旧世代 (``ops`` 一本) の応答形式か。

    新形式はコア記憶の三一覧 (``core_adds`` / ``core_updates`` /
    ``core_removes``) を必ず全部持つ — :func:`_parse_structured_result` が
    凍結より前に全欄必須で検証しているため。旧形式をそのまま適用側へ渡すと
    三一覧が空として読まれ、「コア記憶の採取ゼロ」で completed になる
    (本人が指定した記憶操作が静かに消える)。だから再利用せず、新しい LLM
    コールでやり直す (fail-closed)。
    """
    if not isinstance(response, dict):
        return True
    if "ops" in response:
        return True
    return not all(field in response for field in _CORE_FIELDS)


def _find_recorded_result(ledger: Any, ledger_key: str) -> Optional[Dict[str, Any]]:
    """同じ担当範囲の記録済み結果 (applied / completed) を探す。

    見つかれば ``{execution_id, status, response, span_start_id, span_end_id,
    seen_ids, rejections, offered_activities, offered_tasks, core_snapshot,
    prompt}`` を返す。**記録が無ければ** None。

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
        raw_offered_tasks = result.get("offered_tasks")
        if isinstance(raw_offered_tasks, dict):
            # 現行形式。値は revision (int) か None (REVISION 列が NULL の行)。
            # それ以外の型 (記録の破損・別実装) は「不明」へ倒す — int でない値を
            # update_entry へ渡すと TaskBookError の文面が競合と混ざる。
            offered_tasks = {
                str(task_id): (
                    revision
                    if revision is None
                    or (isinstance(revision, int) and not isinstance(revision, bool))
                    else _REVISION_UNKNOWN
                )
                for task_id, revision in raw_offered_tasks.items()
            }
        else:
            # 旧形式 (offered_task_ids のみ — スナップショット時点の revision を
            # 持たない)。Codex 第八巡 修正 4: 不明を None で表すと update_entry の
            # 「現在値を読み直して CAS」に落ちて CAS が無効化されるので、
            # _REVISION_UNKNOWN として持ち、update 要素は棄却させる。
            offered_tasks = {
                str(task_id): _REVISION_UNKNOWN
                for task_id in (result.get("offered_task_ids") or [])
            }
        return {
            "execution_id": existing.get("execution_id"),
            "status": status,
            "response": result.get("response"),
            "span_start_id": result.get("span_start_id"),
            "span_end_id": result.get("span_end_id"),
            "seen_ids": result.get("seen_ids"),
            "rejections": result.get("rejections") or [],
            "offered_activities": result.get("offered_activities") or {},
            "offered_tasks": offered_tasks,
            "core_snapshot": result.get("core_snapshot"),
            "prompt": result.get("prompt"),
        }
    return None


def _seen_span_end(
    span_ids: List[str], span_end_full: Optional[str], dropped_total: int,
) -> Optional[str]:
    """実際に LLM に渡した範囲の末尾メッセージ ID を求める。

    コンテキスト超過の後退 (§13.5-1) で外した組は「見ていない」— パンマーカーと
    span をそこまで進めると、外した会話が未見のまま処理済みになりゲートの
    不変条件 (全経験が退場前に一度本人の目を通る) が破れる。後退で外したのは
    提示 context の末尾ブロックなので、窓の ID 列の末尾から同数を引いた位置を
    見た範囲の末尾とする (提示 context の履歴部は窓のメッセージと 1:1 で並ぶ
    前提の末尾勘定。ずれても安全側 = 進めなさすぎ、で二重見にしかならない)。
    全部外れた (勘定が窓を使い切った) ときは None = マーカーを進めない。
    """
    if dropped_total <= 0:
        return span_end_full
    seen_count = len(span_ids) - dropped_total
    if seen_count <= 0:
        return None
    return span_ids[seen_count - 1]


def _call_sluice_llm(
    lifecycle: Any,
    persona: Any,
    building_id: str,
    span_ids: List[str],
    span_end_full: Optional[str],
    span_new_count: Optional[int],
    window_anchor_id: Optional[str] = None,
    model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """LLM 呼び出しフェーズ (context 組み → 後退つき generate → usage → 検証)。

    Returns:
        ``{"response": 検証済み dict, "rejections": 型不正で落とした要素の記録,
        "span_end_id": 実際に見た範囲の末尾 (None =
        特定不能), "seen_ids": 実際に LLM 入力に含めたメッセージ ID の列
        (**必ず 1 件以上** — 空なら SluiceEmptySeenSetError),
        "offered_activities": {id: name},
        "offered_tasks": {task_id: スナップショット時点の revision},
        "core_snapshot": {core_id: 本文ハッシュ (スナップショット時点)},
        "prompt": 注入プロンプト}``

    例外 (LLM エラー・出力不適合) はそのまま送出する — 呼び出し元が台帳の
    mark_failed とゲート失敗 (退場停止) に写像する。
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
    # タスクは id → スナップショット時点の revision (CAS 用 — 実行中のユーザー
    # 編集へ黙って上書きしないための照合値)。
    offered_tasks: Dict[str, Optional[int]] = {
        str(t.get("task_id")): t.get("revision")
        for t in open_tasks if t.get("task_id")
    }
    # コア記憶の現況を一度読み、プロンプト同梱と CAS スナップショット
    # (id → 本文ハッシュ。Codex 第七巡 修正 2 — タスク帳 CAS の同族) の両方に使う。
    core_memories, core_total_chars = _read_core_state(persona)
    core_snapshot: Dict[str, str] = {
        str(mem.id): _core_content_hash(mem.content) for mem in core_memories
    }
    # 今日すでに手帳に書いたもの (本人がスペルで書いた分を含む) — 同じ日の
    # 再採取を減らすため、アクティビティ一覧と同じ読みの配下から一度で取る。
    today_memos = _list_today_memos(persona, activities)
    prompt = _build_sluice_prompt(
        persona, activities, open_tasks, core_memories, core_total_chars,
        span_new_count=span_new_count, today_memos=today_memos,
    )

    node_def = SimpleNamespace(id="sluice", memorize=None, speak=False)
    llm_client, _sluice_model = runtime.select_llm_client(
        node_def, persona, execution_context=execution_context,
        needs_structured_output=True,
    )
    if _sluice_model != execution_context.model_key:
        # structured-output fallback で実 model が変わった場合の差し替え
        execution_context = execution_context.with_model(_sluice_model)

    # コンテキスト超過の後退方式 (autonomous_behavior_v3.md §13.5-1): 超過エラー
    # のときは担当範囲の直近のプロンプト+応答の組を一つ外して再試行し、まだ
    # 入らなければもう一組前を外す。外した組は「見ていない」のでパンマーカーを
    # そこまで進めず、次回の担当範囲に残す (_seen_span_end)。
    # TODO(§13.5-2): 再試行し続けても失敗しキャッシュが切れた場合の扱いは未設計
    # (autonomous_behavior_v3.md §13.5-2 — 存在だけ確定)。現状は失敗として送出し、
    # 呼び出し元の退場停止 → 次回再試行に乗る。
    dropped_total = 0
    while True:
        messages = context_messages + [{"role": "user", "content": prompt}]
        try:
            result = llm_client.generate(
                messages,
                tools=[],
                response_schema=_RESPONSE_SCHEMA,
                temperature=runtime._default_temperature(persona),
                # このコールだけの出力上限 (per-call)。対応していない
                # プロバイダのクライアントは generate の **kwargs が
                # 黙って落とす — 上限が効かないだけで、例外にはしない。
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                **runtime._get_cache_kwargs(persona_id),
            )
            break
        except Exception as exc:
            if not _is_context_overflow(exc):
                raise
            reduced = _drop_last_exchange(context_messages)
            if reduced is None:
                LOGGER.error(
                    "[sluice] context overflow persists with nothing left to drop "
                    "(persona=%s dropped=%d)", persona_id, dropped_total,
                )
                raise
            context_messages, dropped = reduced
            dropped_total += dropped
            LOGGER.warning(
                "[sluice] context overflow; dropped the most recent prompt+response "
                "pair (%d messages, total dropped=%d) and retrying — "
                "外した組は次回の担当範囲に残る (persona=%s)",
                dropped, dropped_total, persona_id,
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
    parsed, rejections = _parse_structured_result(result, persona_id)

    # 見た集合: 実入力の履歴 ID 列から、後退で外した末尾ぶんを件数で引く
    # (外した組は context の末尾ブロック = 履歴の末尾)。退場側の包含検算
    # (_eviction_within_seen) の一次データ。
    seen_count = max(0, len(presented_ids) - dropped_total)
    seen_ids = presented_ids[:seen_count]
    if not seen_ids:
        # 1 通も見ていない結果は完了させない (Codex 第八巡 修正 2)。凍結すると
        # マーカー据え置きのまま completed になり、以後は同じ記録が再利用されて
        # LLM が二度と走らず、末尾の退場が永久に止まる。
        raise SluiceEmptySeenSetError(
            f"the sluice saw no messages (persona={persona_id}, "
            f"presented={len(presented_ids)}, dropped={dropped_total}); "
            "refusing to freeze a result that saw nothing"
        )

    return {
        "response": parsed,
        "rejections": rejections,
        "span_end_id": _seen_span_end(span_ids, span_end_full, dropped_total),
        "seen_ids": seen_ids,
        "offered_activities": offered_activities,
        "offered_tasks": offered_tasks,
        "core_snapshot": core_snapshot,
        "prompt": prompt,
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
         "skipped": bool, "reason": str|None,
         "seen_span_end": str|None, "seen_ids": list[str]|None}

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
    """
    def _skipped(reason: str) -> Dict[str, Any]:
        return {
            "ops_applied": 0, "ops_failed": 0,
            "memos_applied": 0, "memos_failed": 0,
            "promises_applied": 0, "promises_failed": 0,
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
        if recorded is not None and _is_legacy_response(recorded.get("response")):
            # 旧世代の応答形式は適用側が読めない — 再利用せず、別キーで新しい
            # LLM コールを立てる (_RESPONSE_FORMAT_TAG の説明を参照)。
            LOGGER.warning(
                "[sluice] 記録の形式が古いため再利用しません "
                "(execution=%s key=%s persona=%s) — 新しい実行として採り直します",
                recorded.get("execution_id"), ledger_key, persona_id,
            )
            ledger_key = f"{ledger_key}#format-{_RESPONSE_FORMAT_TAG}"
            recorded = _find_recorded_result(ledger, ledger_key)

    execution_id: Optional[str] = None
    ledger_status: Optional[str] = None
    if recorded is not None:
        execution_id = recorded["execution_id"]
        ledger_status = recorded["status"]
        parsed_result = recorded["response"]
        span_start_id = recorded.get("span_start_id")
        span_end_id = recorded.get("span_end_id")
        offered_activities = {
            int(k): v
            for k, v in dict(recorded.get("offered_activities") or {}).items()
        }
        offered_tasks = dict(recorded.get("offered_tasks") or {})
        core_snapshot = recorded.get("core_snapshot")
        if not isinstance(core_snapshot, dict):
            # 旧形式の記録 (スナップショット無し): 再構成しない (第五巡の裁定と
            # 同族)。None のまま渡し、update / remove は要素棄却になる。
            core_snapshot = None
        seen_ids = recorded.get("seen_ids")
        if not isinstance(seen_ids, list):
            # fail-closed (Codex 第五巡 修正 1): seen_ids の無い記録を span から
            # 再構成するのは「別読みの近似」の同族 — 推定せず送出して退場を
            # 止める。空リストは「何も見ていない正当な集合」として通す。
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
                lifecycle, persona, building_id, span_ids, span_end_full,
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
        if span_end_id is None:
            # 実際に見た範囲の末尾が特定できない (後退で窓勘定を使い切った等)。
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
    promises = _as_list("promises")

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
    )
    promises_applied, promises_failed, promise_lines = _apply_promises(
        lifecycle, persona, promises,
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
        elif field == "promises":
            promises_failed += 1
    applied_total = ops_applied + memos_applied + promises_applied
    result_lines = rejection_lines + ops_lines + memo_lines + promise_lines

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
    #    ④パンマーカー前進 (実際に見た範囲の末尾まで — 超過後退で外した組は
    #    次回の担当範囲に残る) は「確定」として一塊にする。呼び出し元
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
        _persist_record(
            persona, record_text, prompt_snapshot, applied_total=applied_total,
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

    if finalize:
        _finalize()

    LOGGER.info(
        "[sluice] done: persona=%s core=%d/%d memos=%d/%d promises=%d/%d "
        "(evict=%d, finalized=%s)",
        persona_id, ops_applied, ops_failed, memos_applied, memos_failed,
        promises_applied, promises_failed, evict_count, finalize,
    )
    return {
        "ops_applied": ops_applied, "ops_failed": ops_failed,
        "memos_applied": memos_applied, "memos_failed": memos_failed,
        "promises_applied": promises_applied, "promises_failed": promises_failed,
        "skipped": False, "reason": None,
        "seen_span_end": span_end_id,
        "seen_ids": seen_ids,
        "finalize": _finalize,
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
