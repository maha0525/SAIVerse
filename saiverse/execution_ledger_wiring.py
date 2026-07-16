"""実行台帳の結線 (Execution Ledger wiring) — 器を世界に繋ぐ。

docs/intent/execution_ledger.md §2.4 (回復処理) / §2.2 (送信トレイ) の配線と、
実ハンドラ 2 種 (saimemory.append / perception.push) の定義。器 (状態機械・
配送器・関所) は saiverse/execution_ledger.py に閉じており、本モジュールは
「manager の世界にどう住まわせるか」だけを受け持つ。

SAIVerseManager の構築 / 起動分離の不変条件に従う:

- :func:`build_execution_ledger` — ``__init__`` 段。インスタンス化 + ハンドラ登録
  のみ (スレッドも DB アクセスも発生しない)。
- :func:`run_startup_recovery` — ``start()`` 段。前世代 running の unknown 化
  (intent §2.4 #4) + 滞留 pending の全量配送 (#1)。pulse を生む背景ループが
  1 本も動き出す前に同期実行する。
- :func:`schedule_recovery_tick` — ``start()`` 段。60 秒周期の掃除 tick を
  EventScheduler に予約する (#1/#3/#6)。

回復 tick は「掃除」のみで行動を生まない (intent §2.4 の二分)。したがって
完全手動モード (debug_controller) の対象ペルソナに対しても止めない —
手動検証中こそ記録は正確であるべきで、掃除は自律行動ではない。
「行動を生む」側 (#2 prepared の回収 / #7 schedule reconciliation) は
kind ごとの回収規則が要るため Phase 1 以降。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict

from saiverse.execution_ledger import ExecutionLedger

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)

#: EventScheduler 上の掃除 tick の予約キー。
RECOVERY_TICK_KEY = "execution_ledger_recovery"

#: 掃除 tick の間隔 (intent §2.4「既定 60 秒仮置き」)。
RECOVERY_TICK_INTERVAL_SECONDS = 60.0

#: running の実行期限。超過で unknown へ落とす (intent §2.4 #3)。
#: kind ごとの期限は Phase 1 で定義する — それまでの全 kind 共通の保守的な
#: 仮置き。長い作業セッションでも 1 時間更新が無い running は観測途絶とみなす。
RUNNING_DEADLINE_SECONDS = 3600.0

# 送信トレイの TARGET 名 (intent §4 スキーマ例)。
TARGET_SAIMEMORY_APPEND = "saimemory.append"
TARGET_PERCEPTION_PUSH = "perception.push"


def build_execution_ledger(manager: "SAIVerseManager") -> ExecutionLedger:
    """台帳を構築し、実ハンドラ 2 種を登録して返す (``__init__`` 段)。

    ハンドラは manager を閉じ込めるが、persona の解決は配送時まで遅延する —
    __init__ のこの時点でペルソナが未ロードでも問題ない。
    """
    ledger = ExecutionLedger(session_factory=manager.SessionLocal)
    ledger.register_outbox_handler(
        TARGET_SAIMEMORY_APPEND, _make_saimemory_append_handler(manager)
    )
    ledger.register_outbox_handler(
        TARGET_PERCEPTION_PUSH, _make_perception_push_handler(manager)
    )
    return ledger


def run_startup_recovery(manager: "SAIVerseManager") -> None:
    """起動時回復 (intent §2.4 #1/#4)。``start()`` 冒頭から同期で呼ばれる。

    1. 前世代 running の一括 unknown 化。前提: main.py が runtime_marker を
       取得済み (同一 DB を共有する他 City プロセスの不在確認) なので、
       「起動直後の running は定義上すべて前世代」の一括 sweep が成立する。
    2. 滞留 pending の全量配送。pulse を生む背景ループの起動前に流すことで、
       「pending より新しい記憶が先に書かれる」並びを起動直後から作らない
       (記憶の順序一貫性、不変条件 8)。
    """
    ledger = manager.execution_ledger
    try:
        recovered = ledger.recover_stale_running(all_running=True)
        if recovered:
            LOGGER.warning(
                "[ledger-wiring] startup sweep: %d 件の前世代 running を "
                "unknown 化しました (自動再実行はしません): %s",
                len(recovered), recovered,
            )
    except Exception:
        # sweep 失敗でも起動は止めない (unknown 化は次の tick でも再試行される
        # 掃除)。ただし黙らせない。
        LOGGER.exception("[ledger-wiring] startup running-sweep failed")
    _flush_all_pending(manager)


def schedule_recovery_tick(manager: "SAIVerseManager") -> None:
    """掃除 tick (intent §2.4 #1/#3/#6) を EventScheduler に周期予約する。"""
    manager.event_scheduler.schedule_periodic(
        RECOVERY_TICK_INTERVAL_SECONDS,
        lambda: _recovery_tick(manager),
        key=RECOVERY_TICK_KEY,
    )
    LOGGER.info(
        "[ledger-wiring] recovery tick scheduled (key=%s interval=%.0fs)",
        RECOVERY_TICK_KEY, RECOVERY_TICK_INTERVAL_SECONDS,
    )


def _recovery_tick(manager: "SAIVerseManager") -> None:
    """定期掃除の 1 周: running 期限監視 (#3) + pending 配送 (#1、dead 化 #6 は配送器内)。

    EventScheduler.schedule_periodic は callback 例外で周期を止める契約なので、
    一度の DB エラーで掃除が永久停止しないよう、ここで例外を吸収してログに残す。
    """
    ledger = manager.execution_ledger
    try:
        ledger.recover_stale_running(max_age_seconds=RUNNING_DEADLINE_SECONDS)
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: running-deadline sweep failed")
    try:
        _flush_all_pending(manager)
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: pending flush failed")


def _flush_all_pending(manager: "SAIVerseManager") -> None:
    """pending を持つ全キュー (世界横断 None 含む) を FIFO 配送する。

    対象はメモリ上の personas ではなく DB の実態 (list_pending_personas) から
    列挙する — 削除済み persona 宛の pending も配送試行 → 再試行上限 → dead
    (人裁定) の正規経路に乗せる。個々の失敗は pending / dead に残るだけで、
    次の tick / Pulse 前関所が引き継ぐ。
    """
    ledger = manager.execution_ledger
    try:
        targets = ledger.list_pending_personas()
    except Exception:
        LOGGER.exception("[ledger-wiring] failed to enumerate pending personas")
        return
    for persona_id in targets:
        try:
            ledger.flush_pending_for_persona(persona_id)
        except Exception:
            LOGGER.exception(
                "[ledger-wiring] flush failed for persona=%s (left pending)",
                persona_id,
            )


# ----------------------------------------------------------------------
# 実ハンドラ (intent §5: 基盤は payload を運ぶだけで内容に触れない)
# ----------------------------------------------------------------------


def _resolve_adapter(manager: "SAIVerseManager", persona_id: Any):
    """配送先 persona の SAIMemory adapter を解決する。失敗は例外 (= 配送失敗)。

    persona 不在 (削除等) は再試行上限を経て dead に落ち、人裁定に回る
    (intent §4「dead は配送先消滅で人裁定に回す終端。黙って捨てない」)。
    """
    if not persona_id:
        raise ValueError("memory delivery requires persona_id")
    persona = manager.personas.get(persona_id)
    if persona is None:
        raise LookupError(f"persona not found: {persona_id}")
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None:
        raise RuntimeError(f"persona {persona_id} has no SAIMemory adapter")
    return adapter


def _make_saimemory_append_handler(
    manager: "SAIVerseManager",
) -> Callable[[Dict[str, Any]], None]:
    """target='saimemory.append' — 本人の記憶へのメッセージ追記の配送。

    payload 契約 (積む側 = 各処理が守る):
        {"message": {...adapter.append_persona_message と同形の dict...},
         "building_id": str | None,     # 省略時 persona スレッド
         "thread_suffix": str | None}
    本文・名義・実行時刻は payload で凍結済みのものをそのまま書く (不変条件 6)。
    冪等キー (execution_id / outbox_id) の刻印と重複抑止は adapter 側
    (append_ledger_message) が行う。
    """
    def handler(item: Dict[str, Any]) -> None:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("saimemory.append payload must be a dict")
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ValueError("saimemory.append payload requires 'message' dict")
        adapter = _resolve_adapter(manager, item.get("persona_id"))
        adapter.append_ledger_message(
            message,
            execution_id=item["execution_id"],
            outbox_id=item["outbox_id"],
            building_id=payload.get("building_id"),
            thread_suffix=payload.get("thread_suffix"),
        )
    return handler


def _make_perception_push_handler(
    manager: "SAIVerseManager",
) -> Callable[[Dict[str, Any]], None]:
    """target='perception.push' — 知覚バッファへの配送 (消費は次 Pulse)。

    payload 契約:
        {"kind": str, "content": str,
         "reduce_key": str | None, "salient": bool,
         "media": list | None, "metadata": str | None}
    冪等キーの刻印と重複抑止は adapter 側 (push_ledger_perception) が行う。
    """
    def handler(item: Dict[str, Any]) -> None:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("perception.push payload must be a dict")
        kind = payload.get("kind")
        content = payload.get("content")
        if not kind or content is None:
            raise ValueError("perception.push payload requires 'kind' and 'content'")
        adapter = _resolve_adapter(manager, item.get("persona_id"))
        adapter.push_ledger_perception(
            execution_id=item["execution_id"],
            outbox_id=item["outbox_id"],
            kind=kind,
            content=content,
            reduce_key=payload.get("reduce_key"),
            salient=bool(payload.get("salient", False)),
            media=payload.get("media"),
            metadata=payload.get("metadata"),
        )
    return handler
