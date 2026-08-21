"""sluice (スルース) — 押し出される記憶が必ず通る採取の関所。

Metabolism の eviction 直前、メインラインの温まった prefix (head + 履歴) に注入
プロンプトを 1 手足し、structured output で「退場前に記録すべきもの」を返させる。
決定は本人 (ペルソナ)、複製・書き込みはシステム側の決定論。

採取する器は三つ (autonomous_behavior_v3.md §13.3 / §13.6):

- コア記憶 ops (add / update / remove) — 恒常知識
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
#: 防止。旧実装が読んでいたキーは ENABLED / PENDING_CAP / CLOSE_MIN_MESSAGES の
#: 3 つで、いずれも同名置換 (SAIVERSE_GOLD_PANNING_* → SAIVERSE_SLUICE_*)。
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


def _env_int(name: str, default: int) -> int:
    raw = _read_env_with_legacy(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("[sluice] invalid int for %s=%r; using default %s", name, raw, default)
        return default


def is_enabled() -> bool:
    """全体トグル。"0"/"false" で無効化 (defer-to-hot ごと無効になる)。"""
    return _env_flag("SAIVERSE_SLUICE_ENABLED", True)


def get_pending_cap() -> float:
    """defer-to-hot 圧力弁の倍率 (high watermark の何倍で「コールドでも実行」に倒すか)。"""
    return _env_float("SAIVERSE_SLUICE_PENDING_CAP", 1.5)


def get_close_min_messages() -> int:
    """セッションクローズ (Phase 3) 時のスキップ下限。"""
    return _env_int("SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES", 10)


# ---------------------------------------------------------------------------
# response_schema (Gemini 制約: additionalProperties 禁止、フラット)
# ---------------------------------------------------------------------------

_MEMO_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "activity_id": {
            "type": "integer",
            "description": "同梱の「開いているアクティビティ一覧」から選んだ ID。一覧に無い活動のときは省略して new_activity_name を使う。",
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
        "ops": {
            "type": "array",
            "description": "コア記憶への操作列。採取なしなら空配列。",
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["add", "update", "remove"],
                    },
                    "content": {
                        "type": "string",
                        "description": "add / update の本文。",
                    },
                    "memory_id": {
                        "type": "integer",
                        "description": "update / remove 対象の core:N の N。",
                    },
                },
                "required": ["op"],
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
    "required": ["reflection", "ops", "want_memos", "did_memos", "promises"],
}


# ---------------------------------------------------------------------------
# 注入プロンプト
# ---------------------------------------------------------------------------

_SCENE_PREVIEW_CHARS = 80


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


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


def _list_open_tasks(lifecycle: Any, persona: Any) -> List[Dict[str, Any]]:
    """open なタスク帳の一件一覧 (task_id / content / due_at / revision)。

    アクティビティ一覧と同じ理由 (閉語彙・再提案防止) で同梱する: 一覧が無いと
    update の task_ref が書けないだけでなく、会話で言及され続けている同じ約束を
    次のスルースが再び add して重複する。

    fail-closed (Codex 第四巡 修正 2): 読み出しの例外は空一覧へ丸めず送出する —
    空へ丸めると、その回のスルースは既存の約束を知らずに再 add し (重複)、
    update の口も失う。「正常に空」(例外なしの空リスト) と「取得不能」(送出) を
    区別する。manager 未構成 (タスク帳の器そのものが無い環境 — テストハーネス・
    session close の縮退) だけは設計上の「タスク帳なし」として空を返す。
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


def _build_sluice_prompt(
    persona: Any,
    activities: List[Tuple[int, str]],
    open_tasks: List[Dict[str, Any]],
    core_memories: List[Any],
    total_chars: int,
) -> str:
    """<system> 包みの注入プロンプトを組む (keepalive の末尾通知と同じ面)。

    ``core_memories`` / ``total_chars`` は :func:`_read_core_state` の読みを
    呼び出し元から受け取る (CAS スナップショットと同じ姿を見せるため)。
    """
    from builtin_data.tools._core_memory_common import resolve_core_memory_budget

    persona_id = getattr(persona, "persona_id", None)

    # 現在のコア記憶全項目を [c:id] content 形式で列挙する。
    core_lines: List[str] = []
    for mem in core_memories:
        body = mem.content or ""
        if mem.kind == "scene":
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
        task_block = "（開いているタスクはありません）"

    prompt = (
        "<system>\n"
        "## 記憶整理の節目 — スルース\n"
        "まもなく古い会話が記憶整理で押し出されます。この機会に、残しておきたい\n"
        "ことがあれば、いま記録できます。押し出された後ではこの会話は手元から\n"
        "消えるため、採るならこのタイミングだけです。\n"
        "\n"
        "1) コア記憶 (ops):\n"
        "- 状態の変化（生活・仕事・健康・関係性など、いま進行中の事実）。状態には\n"
        "  日付を含めてください（例: 2026年6月頃〜 ユーザーは海外赴任中、9月帰国予定）。\n"
        "- 既存のコア記憶と矛盾する新情報（帰国・引っ越しなど。矛盾は update で解消）。\n"
        "\n"
        "2) 手帳 (want_memos / did_memos):\n"
        "- この範囲で、やりたいと思ったこと・実際にやったことはありますか?\n"
        "  無ければ空で構いません。あれば、活動の名前（「小説を書く」「絵の練習」\n"
        "  のような粒度。下の一覧にあるものは activity_id で参照）と、今日の中身\n"
        "  一行 (text) で。\n"
        "\n"
        "3) 約束 (promises):\n"
        "- ユーザーとの約束や引き受けた依頼が生まれたり変わったりしていましたか?\n"
        "  無ければ空で構いません。期限が明示されていないなら due は書かないで\n"
        "  ください（期限を発明しない）。既に下のタスク帳一覧にあるものを再び\n"
        "  add する必要はありません — 内容や期限に変化があれば、その task の ID を\n"
        "  task_ref にして update を使えます。期限が撤回されたときは clear_due で\n"
        "  期限を外せます。\n"
        "\n"
        "姿勢:\n"
        "- **採取しないのが普通です。** ほとんどの記憶整理では何も採りません\n"
        "  （各欄は空配列）。無理に何かを刻もうとしないでください。\n"
        "- 応答には全ての欄 (reflection / ops / want_memos / did_memos /\n"
        "  promises) を含めてください。採るものが無い欄は空配列で。\n"
        "- 既にコア記憶・手帳にあることは再度採らないでください。\n"
        "\n"
        f"### 現在のコア記憶（合計 {total_chars:,} 字 / 目安 {budget:,} 字）\n"
        f"{core_block}\n"
        "\n"
        "### 開いているアクティビティ（手帳）\n"
        f"{activity_block}\n"
        "\n"
        "### 開いているタスク帳（約束・依頼）\n"
        f"{task_block}\n"
        "</system>"
    )
    return prompt


# ---------------------------------------------------------------------------
# コア記憶 ops の適用 (tool 関数を経由せず直接)
# ---------------------------------------------------------------------------

def _apply_ops(
    persona: Any,
    ops: List[Dict[str, Any]],
    *,
    core_snapshot: Optional[Dict[str, str]],
) -> tuple[int, int, List[str]]:
    """ops を順に適用し、(成功数, 失敗数, 結果テキスト行) を返す。

    CAS (Codex 第七巡 修正 2 — タスク帳 CAS の同族): ``core_snapshot`` は
    プロンプト作成時に読んだコア記憶現況の {id: 本文ハッシュ}。update / remove は
    適用時に現在値と照合し、不一致 (LLM 実行中のユーザー編集・削除) はその要素
    だけ棄却して記録に明記する — 古い判断でユーザー編集を上書きしない。照合と
    書き込みは同じ adapter._db_lock 保持下で行う (check-then-act の隙間を作らない
    — コア記憶の書き手は全て同じロックを通る)。スナップショットが無い
    (None = 旧形式の記録) / id がスナップショットに無い場合も要素棄却 —
    推定で適用しない (第五巡の裁定と同族)。

    粒度分け (メモ/約束の適用と同じ規律): **入力の不正** (非 dict 要素・未知の
    op・空本文・不正/不在の memory_id 参照) はその要素だけ棄却して結果行に残す。
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

    if adapter is None or getattr(adapter, "conn", None) is None:
        return (0, len(ops), ["コア記憶ストレージが利用できず、採取を適用できませんでした。"])

    # 同一本文ガード用の生存コア記憶 (最初の add で遅延ロード)。
    alive_contents: Optional[Dict[str, int]] = None

    def _coerce_memory_id(raw: Any) -> Optional[int]:
        """memory_id の入力検査。不正は None (要素棄却の合図)。"""
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    for op in ops:
        if not isinstance(op, dict):
            failed += 1
            lines.append(f"不正な op 形式のためスキップ: {op!r}")
            continue
        kind = op.get("op")
        # 以下、ストレージ呼び出し (list/add/update/remove_core_memory) は
        # 意図的に例外を握らない — 障害は送出してゲートに乗せる (docstring)。
        if kind == "add":
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

        elif kind == "update":
            memory_id = op.get("memory_id")
            content = (op.get("content") or "").strip()
            if memory_id is None:
                failed += 1
                lines.append("update 失敗: memory_id が指定されていません。")
                continue
            if not content:
                failed += 1
                lines.append(f"update 失敗: core:{memory_id} の新しい本文が空でした。")
                continue
            target_id = _coerce_memory_id(memory_id)
            if target_id is None:
                failed += 1
                lines.append(f"update 失敗: memory_id が不正です ({memory_id!r})。")
                continue
            snap_hash = (
                None if core_snapshot is None
                else core_snapshot.get(str(target_id))
            )
            if snap_hash is None:
                failed += 1
                lines.append(
                    f"update 失敗: core:{memory_id} のスナップショット情報が"
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
                ok = cas_ok and update_core_memory(
                    adapter.conn, target_id, content, confirmed=0,
                )
            if not cas_ok:
                failed += 1
                lines.append(
                    f"記憶 core:{memory_id} は実行中に変更されたため"
                    "適用しませんでした。"
                )
                LOGGER.warning(
                    "[sluice] core update rejected: core:%s changed since "
                    "snapshot (CAS mismatch)", memory_id,
                )
            elif ok:
                applied += 1
                lines.append(f"コア記憶 core:{memory_id} を更新: {content}")
            else:
                failed += 1
                lines.append(f"update 失敗: core:{memory_id} が見つかりませんでした。")

        elif kind == "remove":
            memory_id = op.get("memory_id")
            if memory_id is None:
                failed += 1
                lines.append("remove 失敗: memory_id が指定されていません。")
                continue
            target_id = _coerce_memory_id(memory_id)
            if target_id is None:
                failed += 1
                lines.append(f"remove 失敗: memory_id が不正です ({memory_id!r})。")
                continue
            snap_hash = (
                None if core_snapshot is None
                else core_snapshot.get(str(target_id))
            )
            if snap_hash is None:
                failed += 1
                lines.append(
                    f"remove 失敗: core:{memory_id} のスナップショット情報が"
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
                    f"記憶 core:{memory_id} は実行中に変更されたため"
                    "適用しませんでした。"
                )
                LOGGER.warning(
                    "[sluice] core remove rejected: core:%s changed since "
                    "snapshot (CAS mismatch)", memory_id,
                )
            elif ok:
                applied += 1
                lines.append(f"コア記憶 core:{memory_id} を削除しました。")
            else:
                failed += 1
                lines.append(f"remove 失敗: core:{memory_id} が見つかりませんでした。")

        else:
            failed += 1
            lines.append(f"未知の op '{kind}' をスキップしました。")

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
    - ``activity_id`` はプロンプトに同梱した一覧 (``offered_activities``) に無い id
      を拒否し、その要素だけ捨ててログに残す (LLM の発明 id を書かせない)。
    - 冪等キーは「安定プレフィックス (span 由来) + 操作番号」— 同じ担当範囲の
      再適用 (部分失敗 → 次回 Metabolism の再処理) で重複しない。
    - span_start_id / span_end_id はこのスルースの一手が担当した範囲 (前回の
      パンマーカーから、実際に LLM に渡した末尾まで) の機械刻印。本人の申告は
      使わない (§13.6)。
    - 一連の書き込みは commit=False で束ね、最後に一括 commit する。要素単位の
      不正 (空本文・一覧外 id) はその要素だけ捨てるが、ストレージ例外は送出する
      (スルース失敗 = 退場停止のゲートに乗せる)。
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

    from sai_memory.memory.pocketbook import add_memo, get_or_create_activity
    from saiverse import clock

    today = clock.now().date().isoformat()
    persona_id = getattr(persona, "persona_id", None)

    applied = 0
    failed = 0
    lines: List[str] = []
    kind_label = {"want": "やりたい", "did": "やった"}

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
                activity_id = memo.get("activity_id")
                if new_name:
                    activity = get_or_create_activity(
                        conn, new_name, "sluice", commit=False,
                    )
                    aid = activity.id
                    aname = activity.name
                elif activity_id is not None:
                    if (
                        isinstance(activity_id, bool)
                        or not isinstance(activity_id, int)
                        or activity_id not in offered_activities
                    ):
                        failed += 1
                        lines.append(
                            f"{kind_label[kind]}メモをスキップ: activity_id={activity_id!r} "
                            "は一覧にありません。"
                        )
                        LOGGER.warning(
                            "[sluice] memo rejected: activity_id %r not in offered list "
                            "(persona=%s)", activity_id, persona_id,
                        )
                        continue
                    aid = activity_id
                    aname = offered_activities[activity_id]
                else:
                    failed += 1
                    lines.append(
                        f"{kind_label[kind]}メモをスキップ: activity_id も "
                        "new_activity_name もありません。"
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


def _parse_due(raw: str) -> tuple[Optional[int], Optional[str]]:
    """LLM 出力の期限文字列を epoch 秒へ変換する。(epoch, 解釈不能の理由) を返す。

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
        due_at, due_error = _parse_due(due_raw)
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
        # internal/event_message タグで General Chronicle / scene 切り出し /
        # キーワード検索から自動除外させる (storage.CHRONICLE_EXCLUDED_TAGS 他)。
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
# マーカーは run_session_close の「新規メッセージ数」ガードの基準であり、メモの
# span (担当範囲) の起点でもある。消えると窓全体が新規扱いになり採取 LLM コールが
# 1 回余分に走る。message id と同じ memory.db (embed_metadata KV) に置くことで、
# memory.db のリストア/差し替えでも id の指す先とマーカーがずれない。
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


_LIST_FIELDS = ("ops", "want_memos", "did_memos", "promises")

#: 判断ターン記録・ログ用の欄の呼び名 (まはーが読む面には実装名を出さない)。
_FIELD_LABELS: Dict[str, str] = {
    "ops": "コア記憶の操作",
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
    "ops": {"op": "string", "content": "string", "memory_id": "integer"},
    "want_memos": {
        "activity_id": "integer", "new_activity_name": "string", "text": "string",
    },
    "did_memos": {
        "activity_id": "integer", "new_activity_name": "string", "text": "string",
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
    **要素内フィールドの型不正** (content が文字列でない、memory_id が整数で
    ない等) はその要素だけ落として棄却の記録に残す (Codex 第八巡 修正 6)。
    **中身の参照・値の不正** (未知の op、空本文、一覧外の activity_id / task_ref、
    解釈不能な due) は従来どおり適用側の要素単位棄却に委ねる。
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


def _get_ledger(lifecycle: Any) -> Optional[Any]:
    """manager 所有の ExecutionLedger を引く。無ければ None (台帳なしで動く)。"""
    manager = getattr(lifecycle, "manager", None)
    if manager is None:
        return None
    return getattr(manager, "execution_ledger", None)


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

    # standard tier (default モデル固定)。lightweight への分岐は書かない (intent §5-7)。
    # Beat 相当の開始点 — Pulse 外なので pulse_context=None (beat_execution_context §2.1)。
    # _prepare_context より先に解決するのは、head を同じ model の Session
    # (persona, model) に向けて render するため (§3.1)。
    from sea.pulse_context import resolve_execution_context
    execution_context = resolve_execution_context(persona, None)

    # メインラインと同じ context を組み、末尾に注入プロンプトを 1 つ足す。
    # 直前の応答コールで prefix が温まっている前提 (defer-to-hot が保証)。
    # context_meta: 今回の prefix の anchor を call-local で受け取る (§3.2)。
    context_meta: Dict[str, Any] = {}
    context_messages = list(runtime._prepare_context(
        persona, building_id, None, model_key=execution_context.model_key,
        context_meta=context_meta,
        # コア記憶・手帳の採取はメインラインへの一手 = ペルソナ本人の判断として残る。
        persona_voiced=True,
    ) or [])
    # 見た集合の一次情報 (Codex 第四巡 修正 1): _prepare_context が**実際に
    # プロンプトへ組み込んだ**履歴メッセージの ID 列を out-param で受け取る。
    # コールド実行の anchor 前進も、この列が組成の実体から来るので自然に映る。
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
    prompt = _build_sluice_prompt(
        persona, activities, open_tasks, core_memories, core_total_chars,
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
) -> Dict[str, Any]:
    """スルース本体。押し出し直前のメインライン prefix に 1 手足して採取を判断させる。

    例外は送出する — 隔離しない。呼び出し元 (run_metabolism) はスルースの失敗で
    退場を止め、次の Metabolism 機会に再試行する (§13.3 の「確実に通るゲート」)。

    確定の二段階化 (Codex 第五巡 修正 2): 適用 (コア記憶・メモ・約束) までは
    本体が行い、**確定 (台帳の mark_completed + 判断ターン記録の永続 + 完了通知 +
    パンマーカー前進)** は返り値の ``finalize`` クロージャに割ってある。
    ``finalize=True`` (既定) は従来どおり本体内で即確定する (直接呼び・
    セッションクローズ経路は挙動不変)。``finalize=False`` の呼び出し元
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

    # 実行台帳: identity は (persona, span_start_id)。同じ担当範囲の記録済み
    # 構造化結果があれば LLM を呼ばず再利用する (設計は _LEDGER_KIND 上のコメント)。
    ledger = _get_ledger(lifecycle)
    ledger_key = (
        f"{persona_id}:{span_start_id}" if (persona_id and span_start_id) else None
    )
    recorded = (
        _find_recorded_result(ledger, ledger_key)
        if (ledger is not None and ledger_key) else None
    )

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

    ops = _as_list("ops")
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

    # 4. 適用: コア記憶 ops → 手帳メモ → 約束 (全て冪等 — 再適用で重複しない)。
    ops_applied, ops_failed, ops_lines = _apply_ops(
        persona, ops, core_snapshot=core_snapshot,
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
        if field == "ops":
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
        # pan マーカー: 次回の担当範囲の起点・セッションクローズ (Phase 3) の
        # 「新規メッセージ」判定用に、**実際に LLM に渡した範囲の末尾 id**
        # (span_end_id) を記録する。永続 (memory.db) が先、属性は成功後
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
# セッションクローズ (Phase 3): TTL 発火時にペルソナが Active でない = セッションが
# 閉じた瞬間のスルース。docs/intent/gold_panning.md §3.6 (機構の intent は旧名のまま)。
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


def run_session_close(lifecycle: Any, persona: Any) -> Dict[str, Any]:
    """セッションクローズ時のスルース。

    run_cache_keepalive の not-Active 分岐 (セッションが閉じ、anchor がまだ温かい
    可能性が高い唯一の停止点) から別スレッド経由で呼ばれる。

    旧実装はここで Chronicle の前倒し全量生成も行っていたが、arasuji_levels.md
    §13 (2026-07-29) で撤去した — 編纂の自動発火は予算超過 (応答後 Metabolism)
    の一本で、クローズは編纂しない。採取 (pan) はマーカーガードを通過し、かつ
    キャッシュが熱いときだけ走る (不変条件 §5-1: クローズはコールド例外を作らない)。
    戻り値の "chronicle" キーは互換のため常に False で残す。

    クローズ採取は退場を伴わないため「確実に通るゲート」(§13.3) の対象外 —
    失敗は best-effort のまま (呼び出しスレッドの例外ログで終わる)。ゲートが
    効くのは Metabolism 内 (run_metabolism → run_sluice) の経路。

    Beat ロック (beat_execution_context.md §3.4): この経路は
    SEARuntime._spawn_session_close の**別スレッド**で走り、persona の記憶
    (採取判断の記録) に書くため、最外周をここで
    beat_gate.hold(purpose="sluice") に入れる。Pulse 内 (run_meta_user →
    Metabolism → run_sluice) の実行は呼び出し元の hold の同一スレッド
    再入で自動カバーされる (このエントリは通らない)。関所 fail-closed は
    skipped_reason='gate_closed' として静かに見送る (クローズ採取は best-effort。
    pending の配送は回復 tick が引き継ぐ)。

    Returns:
        {"panned": bool, "chronicle": bool, "skipped_reason": str|None}
    """
    persona_id = getattr(persona, "persona_id", None)

    if not is_enabled():
        return {"panned": False, "chronicle": False, "skipped_reason": "disabled"}

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not getattr(adapter, "is_ready", lambda: False)():
        return {"panned": False, "chronicle": False, "skipped_reason": "no_memory"}

    # in-flight ガード: 同一ペルソナのクローズが二重に走らないようにする。
    if getattr(persona, "_sluice_close_inflight", False):
        LOGGER.debug(
            "[sluice] session close already in-flight; skipping (persona=%s)", persona_id,
        )
        return {"panned": False, "chronicle": False, "skipped_reason": "inflight"}

    persona._sluice_close_inflight = True
    try:
        from sea.beat_gate import BeatGateClosedError, hold_beat
        try:
            with hold_beat(
                getattr(lifecycle, "manager", None), persona_id,
                purpose="sluice",
            ):
                return _run_session_close_locked(lifecycle, persona, persona_id)
        except BeatGateClosedError as exc:
            LOGGER.warning(
                "[sluice] session close skipped: Beat gate closed "
                "(persona=%s): %s", persona_id, exc,
            )
            return {"panned": False, "chronicle": False, "skipped_reason": "gate_closed"}
    finally:
        persona._sluice_close_inflight = False


def _run_session_close_locked(
    lifecycle: Any, persona: Any, persona_id: Optional[str],
) -> Dict[str, Any]:
    """:func:`run_session_close` の本体 (Beat ロック保持下で実行される)。"""
    panned = False
    chronicle_done = False
    skipped_reason: Optional[str] = None

    # window の起点はスルース自身の pan マーカー (前回処理した末尾)。
    # metabolism anchor は退場 (畳み) で前進する点なので採取範囲の起点には
    # 使わない — 畳みが走っただけで未採取メッセージが範囲外へ落ちる (旧 TTL
    # リセット時代の実害: 2026-07-08 sophie 実機で anchor が当日リセットされ
    # window が main_line/committed/現スレッド絞りで 4 件に縮んでいた。TTL
    # リセット自体は arasuji_levels.md §13 で廃止)。初回 (マーカー無し) は
    # 「今コンテキストに載っている生履歴の先頭」として metabolism anchor を起点に使う
    # (この時 anchor は読んでいる範囲の先頭を表す)。
    # 性質フィルタ (main_line/committed) は付けない: 読んでいる生履歴全体を範囲に取る
    # (main_line 限定は metabolism のカウント用で、スルースの範囲とは別軸)。
    history_mgr = getattr(persona, "history_manager", None)
    if not history_mgr:
        return {"panned": False, "chronicle": False, "skipped_reason": "no_history"}

    pan_marker = _load_pan_marker(persona)
    window_start = pan_marker
    if not window_start:
        # 初回 (マーカー無し) のみ metabolism anchor を起点に使う。正は
        # session_anchor 行 (persona, model) — スルースは standard tier
        # (persona.model) 固定 (intent §5-7) なのでその行を読む。旧
        # history_manager.metabolism_anchor_message_id (persona 単一可変属性)
        # は廃止 (beat_execution_context.md §3.2)。
        _load_entry = getattr(lifecycle, "load_anchor_entry", None)
        _model = getattr(persona, "model", None)
        if callable(_load_entry) and _model:
            try:
                _entry = _load_entry(persona_id, str(_model))
                window_start = _entry.get("anchor_id") if _entry else None
            except Exception:
                LOGGER.warning(
                    "[sluice] session close: anchor row read failed (persona=%s)",
                    persona_id, exc_info=True,
                )
    if not window_start:
        LOGGER.info(
            "[sluice] session close: no window start; skipping (persona=%s)", persona_id,
        )
        return {"panned": False, "chronicle": False, "skipped_reason": "no_anchor"}

    current_messages = history_mgr.get_history_from_anchor(window_start) or []

    # マーカーガード: 新規件数 (マーカー以降。初回は全件) が close_min 未満なら採取
    # スキップ。
    new_count = _count_new_since_marker(current_messages, pan_marker)
    close_min = get_close_min_messages()
    pan_allowed = new_count >= close_min
    if not pan_allowed:
        skipped_reason = "below_min"
        LOGGER.info(
            "[sluice] session close: new messages below close_min "
            "(persona=%s new=%d min=%d); skipping pan", persona_id, new_count, close_min,
        )

    # (旧: ここに Chronicle 前倒し全量生成があった — arasuji_levels.md §13 で
    #  撤去。編纂の自動発火は予算超過の一本。)
    # ensure_recall_embeddings はクローズで必ず実行 (run_metabolism と同じ思想:
    # ローカル・無料で、Chronicle 生成の成否・トグルに相乗りさせない)。
    try:
        lifecycle.ensure_recall_embeddings(persona)
    except Exception:
        LOGGER.exception(
            "[sluice] session close: ensure_recall_embeddings failed (persona=%s)", persona_id,
        )

    # 採取 (マーカーガード通過時のみ、かつ hot のときだけ)。
    if pan_allowed:
        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            skipped_reason = "no_building"
            LOGGER.info(
                "[sluice] session close: no current_building_id (persona=%s); skipping pan",
                persona_id,
            )
        elif not lifecycle._is_cache_hot(persona):
            skipped_reason = "cold"
            LOGGER.info(
                "[sluice] session close: cache cold (persona=%s); skipping pan "
                "(invariant §5-1: no cold exception)", persona_id,
            )
        else:
            summary = run_sluice(
                lifecycle, persona, building_id, current_messages,
                evict_count=0, event_callback=None, finalize=False,
            )
            panned = True
            # マーカー安全ゲート (Codex 第六巡 修正 1): クローズ側の窓は無フィルタ
            # の生履歴 (sub-line / discardable 込み) で、_prepare_context が実際に
            # 組む提示対象 (main_line/committed) より広い。マーカー候補
            # (seen_span_end) は窓の末尾勘定なので、LLM が見ていない ID を指しうる。
            # 判定は seen と同じ ID 空間 (提示対象) で行う — seen は提示列の先頭
            # 切片なので、「新マーカー位置以前の提示対象メッセージ全件 ⊆ seen」は
            # 「マーカーが seen に含まれる」ことと同値。含まれなければ確定を
            # 保留する (マーカー据え置き — 次回クローズが同じ範囲を採り直す。
            # 適用は冪等キーで重複しない)。
            seen_end = summary.get("seen_span_end")
            seen_set = {str(i) for i in (summary.get("seen_ids") or [])}
            finalize_fn = summary.get("finalize")
            if seen_end is None or str(seen_end) in seen_set:
                if finalize_fn is not None:
                    finalize_fn()
            else:
                skipped_reason = "finalize_withheld"
                LOGGER.info(
                    "[sluice] session close: finalize withheld — marker "
                    "candidate %s is not in the seen set (persona=%s); the "
                    "next close will re-cover the range (idempotent)",
                    seen_end, persona_id,
                )

    LOGGER.info(
        "[sluice] session close done: persona=%s panned=%s chronicle=%s skipped_reason=%s",
        persona_id, panned, chronicle_done, skipped_reason,
    )
    return {"panned": panned, "chronicle": chronicle_done, "skipped_reason": skipped_reason}
