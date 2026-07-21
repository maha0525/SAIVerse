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

回復 tick の基本は「掃除」で行動を生まない (intent §2.4 の二分)。したがって
完全手動モード (debug_controller) の対象ペルソナに対しても掃除は止めない —
手動検証中こそ記録は正確であるべきで、掃除は自律行動ではない。
「行動を生む」側のうち **#2 (prepared の回収) は W1 Chunk A で実装済み**
(:func:`_collect_prepared_judgments` — kind 別規則は D5 の表。refire は行動を
生むため、手動モード対象ペルソナはスキップする)。**#7 (schedule
reconciliation) は W3 Chunk C で実装済み** (:func:`_reconcile_schedules` →
``ScheduleManager._reconcile_schedules`` — DB 正典と予約の世代照合。登録・
除去のみで LLM を起動しないため、手動モード対象ペルソナも止めない)。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict

from saiverse.execution_ledger import STATUS_PREPARED, ExecutionLedger

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

#: コマ発火 (:func:`saiverse.day_plan._fire_slot`) の台帳 KIND。
SLOT_FIRE_KIND = "slot.fire"

#: slot.fire の精算 settle-close deadline (W2 D5)。
#: 根拠: EventScheduler は単一 dispatch スレッドで全 callback を直列実行する
#: (saiverse/event_scheduler.py の _dispatch_loop / run_due)。コマ発火
#: (_fire_slot) も回復 tick (schedule_periodic) も同じスレッドの callback なので、
#: **同一プロセス内では tick とハンドラが同時に走らない** — tick が見る running
#: slot.fire は「ハンドラは既に return したが精算 tx が転けた / プロセスが跨いだ」
#: もので、稼働中ハンドラを誤 settle するリスクは構造的に低い。それでも
#: 保守側に倒し、長い作業セッションの実行時間 (RUNNING_DEADLINE_SECONDS 未満)
#: を十分に上回らない範囲で 15 分を置く — 万一ハンドラが別スレッドへ処理を逃がす
#: 将来変更が入っても、稼働中ハンドラを settle しないための余裕。
SLOT_SETTLE_DEADLINE_SECONDS = 900.0

# 送信トレイの TARGET 名 (intent §4 スキーマ例)。
TARGET_SAIMEMORY_APPEND = "saimemory.append"
TARGET_PERCEPTION_PUSH = "perception.push"
#: W1 Chunk C (D9-5): 作業セッション digest の配送。saimemory.append の変種で、
#: 冪等 append 後に episode の digest_ref (再訪の鍵) を後段確定する。
TARGET_SAIMEMORY_APPEND_DIGEST = "saimemory.append_digest"
#: W5/B1: 移動 (move.entity) の commit 後処理の配送。位置遷移 + building
#: イベントは移動 tx に同居し、in-process の後処理 (dynamic state / addon
#: hooks / game lifecycle) だけが outbox で at-least-once 実行になる —
#: 旧実装の「commit 後の裸実行 (失敗 = 消失 or 偽の移動失敗)」の置き換え。
TARGET_MOVE_DYNAMIC_STATE = "move.post_dynamic_state"
TARGET_MOVE_ADDON_HOOKS = "move.post_addon_hooks"
TARGET_MOVE_GAME_LIFECYCLE = "move.post_game_lifecycle"

#: 判断点 kind の台帳 KIND 前綴り (autonomy_wiring.fire_judgment_point が刻む)。
JUDGMENT_KIND_PREFIX = "judgment."

#: prepared 回収 (#2、D5 の表): refire する kind と待機秒数。
#: on_event / post_session はイベント/セッションの収穫が判断に依存するため
#: 回収価値が高い。refire は一度 running に入れば terminal に落ちるので
#: 試行回数の管理は不要。
PREPARED_REFIRE_AFTER_SECONDS = 120.0
PREPARED_REFIRE_KINDS = (
    f"{JUDGMENT_KIND_PREFIX}on_event",
    f"{JUDGMENT_KIND_PREFIX}post_session",
)

#: prepared 回収 (#2): 期限切れで failed に落とす kind と期限秒数。
#: day_open / day_close は watchdog が自然再発火する (claim が failed キーを
#: 退避して回る)。post_conversation は会話の瞬間が過ぎており refire しない。
PREPARED_EXPIRE_AFTER_SECONDS = 1800.0
PREPARED_EXPIRE_KINDS = (
    f"{JUDGMENT_KIND_PREFIX}day_open",
    f"{JUDGMENT_KIND_PREFIX}day_close",
    f"{JUDGMENT_KIND_PREFIX}post_conversation",
)

#: schedule 発火の台帳 KIND (W3。ScheduleManager 側の定数と同値 — wiring は
#: schedule_manager を遅延 import するため文字列をここにも持つ)。
SCHEDULE_DISPATCH_KIND = "schedule.dispatch"

#: prepared 回収 (#2、W3 Codex 第六陣 P1): claim → try_mark_running の間で
#: プロセスが死んだ schedule.dispatch の prepared 残留を再発火させるまでの
#: 待機秒数。予約は in-memory なので crash で必ず消えており、reconciliation は
#: 「現在時刻から計算した次回 occurrence」しか照合しない — periodic の当日分
#: など過去の occurrence はこの回収が唯一の再発火経路。
SCHEDULE_PREPARED_REFIRE_AFTER_SECONDS = 120.0

#: failed periodic 回収 (W3 Codex 第八陣): mark_failed (durable) と backoff 予約
#: (揮発) の間で死んだ periodic occurrence を放棄するまでの上限。periodic の
#: 「当日中に retry する」意味論に合わせ、occurrence がこれより古い failed は
#: 回収しない (翌 occurrence が正)。
SCHEDULE_FAILED_OCCURRENCE_MAX_AGE_SECONDS = 86400.0

#: failed periodic 回収の待機猶予 (W3 Codex 第九陣)。揮発 backoff 予約が
#: 生きていれば backoff (120 秒) + 定期 tick (60 秒) 以内に必ず発火して claim が
#: キーを退避する — 正準キーの failed 行がこれより古い = backoff 連鎖は死んで
#: いる (crash / schedule() 失敗)、と行齢だけで判定できる。予約の有無は使わない
#: (再起動後は start() が翌回を先に登録するため区別にならない)。
SCHEDULE_FAILED_RETRY_GRACE_SECONDS = 300.0


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
    ledger.register_outbox_handler(
        TARGET_SAIMEMORY_APPEND_DIGEST,
        _make_saimemory_append_digest_handler(manager),
    )
    ledger.register_outbox_handler(
        TARGET_MOVE_DYNAMIC_STATE, _make_move_dynamic_state_handler(manager)
    )
    ledger.register_outbox_handler(
        TARGET_MOVE_ADDON_HOOKS, _make_move_addon_hooks_handler(manager)
    )
    ledger.register_outbox_handler(
        TARGET_MOVE_GAME_LIFECYCLE, _make_move_game_lifecycle_handler(manager)
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
        # slot.fire は汎用 sweep から除外する (unknown 化すると episode が永久
        # open のまま照合待ちになる) — 代わりに下の settle-close で拾う。
        recovered = ledger.recover_stale_running(
            all_running=True, exclude_kinds=(SLOT_FIRE_KIND,),
        )
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
    # 前世代の running slot.fire を settle-close する。起動直後の running slot.fire は
    # 定義上すべて前世代 (単一 dispatch スレッドのハンドラは起動前に途絶している)
    # なので、deadline を課さず (older_than_seconds=None) 全件を掃除する — 稼働中
    # ハンドラ誤 settle の懸念は起動時には存在しない (前世代確定)。
    try:
        _collect_stale_slot_executions(manager, older_than_seconds=None)
    except Exception:
        LOGGER.exception("[ledger-wiring] startup slot.fire settle-close failed")
    try:
        _close_orphaned_unknown_slot_episodes(manager)
    except Exception:
        LOGGER.exception("[ledger-wiring] startup orphaned-episode close failed")
    try:
        # applied 残留の照合掃除 (W3 Codex 第七陣 — tick と同じ)。
        ledger.sweep_applied()
    except Exception:
        LOGGER.exception("[ledger-wiring] startup applied sweep failed")
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
    """定期掃除の 1 周: running 期限監視 (#3) + prepared 回収 (#2) +
    schedule reconciliation (#7) + pending 配送 (#1、dead 化 #6 は配送器内)。

    EventScheduler.schedule_periodic は callback 例外で周期を止める契約なので、
    一度の DB エラーで掃除が永久停止しないよう、ここで例外を吸収してログに残す。
    """
    ledger = manager.execution_ledger
    # slot.fire の settle-close を汎用 sweep より前に行う (slot.fire は汎用 sweep
    # から除外するので順序に依存はしないが、コマ回復を先に置く方が安全)。
    try:
        _collect_stale_slot_executions(manager)
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: slot.fire settle-close failed")
    try:
        _close_orphaned_unknown_slot_episodes(manager)
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: orphaned-episode close failed")
    try:
        ledger.recover_stale_running(
            max_age_seconds=RUNNING_DEADLINE_SECONDS,
            exclude_kinds=(SLOT_FIRE_KIND,),
        )
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: running-deadline sweep failed")
    try:
        # applied 残留の照合掃除 (W3 Codex 第七陣): mark_applied commit と
        # mark_completed の間の crash で outbox の無い applied が非終端のまま
        # 隠れるのを終端へ収束させる。
        swept = ledger.sweep_applied()
        if swept:
            LOGGER.info(
                "[ledger-wiring] recovery tick: %d applied execution(s) swept "
                "to completed: %s", len(swept), swept,
            )
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: applied sweep failed")
    try:
        _collect_prepared_judgments(manager)
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: prepared collection failed")
    try:
        _collect_prepared_schedule_dispatch(manager)
    except Exception:
        LOGGER.exception(
            "[ledger-wiring] recovery tick: schedule.dispatch prepared "
            "collection failed"
        )
    try:
        _collect_failed_periodic_schedule_dispatch(manager)
    except Exception:
        LOGGER.exception(
            "[ledger-wiring] recovery tick: schedule.dispatch failed-periodic "
            "collection failed"
        )
    try:
        _reconcile_schedules(manager)
    except Exception:
        LOGGER.exception(
            "[ledger-wiring] recovery tick: schedule reconciliation failed"
        )
    try:
        _flush_all_pending(manager)
    except Exception:
        LOGGER.exception("[ledger-wiring] recovery tick: pending flush failed")


def _collect_stale_slot_executions(
    manager: "SAIVerseManager", *, older_than_seconds: Any = SLOT_SETTLE_DEADLINE_SECONDS
) -> None:
    """回復: 精算が転けて running のまま残ったコマ発火を settle-close する (W2 D5)。

    slot.fire は「行動を生む」判断点ではなく「実行した記録の締め」(episode close +
    slot done + 台帳 applied) なので、LLM 再実行を伴わない掃除である — したがって
    完全手動モードのペルソナに対しても止めない (掃除は自律行動ではない)。

    Args:
        older_than_seconds: :meth:`ExecutionLedger.list_running` に渡す deadline。
            None (起動時) は前世代確定の全 running slot.fire を対象にする。
            個別の例外は tick を殺さない (:func:`_collect_prepared_judgments` の
            防御に倣う)。
    """
    ledger = manager.execution_ledger
    try:
        rows = ledger.list_running(SLOT_FIRE_KIND, older_than_seconds=older_than_seconds)
    except Exception:
        LOGGER.exception("[ledger-wiring] failed to list running slot.fire executions")
        return
    if not rows:
        return
    for row in rows:
        try:
            _settle_one_stale_slot(manager, row)
        except Exception:
            LOGGER.exception(
                "[ledger-wiring] slot.fire settle-close failed (execution=%s)",
                row.get("execution_id"),
            )


def _settle_one_stale_slot(manager: "SAIVerseManager", row: Dict[str, Any]) -> None:
    """running な slot.fire 1 行を settle-close する (D5)。

    台帳 payload の slot 座標 (persona/plan_date/index) から origin_ref を
    再構成して開いている出来事を逆引きし、:func:`saiverse.day_plan.settle_stale_slot`
    に委ねる (episode close + slot done + 台帳 applied/completed、予算は予約額のまま)。
    """
    # 遅延 import (wiring は day_plan / episodes から独立に import され得る)。
    from saiverse import day_plan, episodes

    ledger = manager.execution_ledger
    execution_id = row.get("execution_id")
    payload = row.get("payload")
    if not execution_id or not isinstance(payload, dict):
        LOGGER.warning(
            "[ledger-wiring] stale slot.fire has no usable payload; skipping "
            "(execution=%s)", execution_id,
        )
        return
    persona_id = payload.get("persona_id")
    plan_date = payload.get("plan_date")
    index = payload.get("index")
    if (
        not persona_id
        or not plan_date
        or not isinstance(index, int)
        or isinstance(index, bool)
    ):
        LOGGER.warning(
            "[ledger-wiring] stale slot.fire payload incomplete "
            "(execution=%s persona=%s date=%s index=%r); skipping",
            execution_id, persona_id, plan_date, index,
        )
        return

    # slot_id (不変 ID) は精算対象コマの特定に使う。この修正より前の payload には
    # 無い (None) — その場合 settle 側は done 書き込みを省いて episode/台帳だけ締める。
    slot_id = payload.get("slot_id")
    origin_ref = day_plan._slot_origin_ref(persona_id, plan_date, index)
    open_ep = episodes.get_open_episode_by_origin(manager, persona_id, origin_ref)
    episode_ref = open_ep.get("episode_ref") if open_ep else None
    day_plan.settle_stale_slot(
        manager, ledger, execution_id, persona_id, plan_date, index, slot_id,
        episode_ref,
    )


def _close_orphaned_unknown_slot_episodes(manager: "SAIVerseManager") -> None:
    """回復: handler 例外 + episode close 失敗が重なって unknown へ進んだ slot.fire の
    孤児 open episode を閉じる (A6 の handler 例外経路、Codex 指摘)。

    handler 例外経路は best-effort で episode を閉じてから ``mark_unknown`` する。
    close が失敗すると episode が open のまま unknown へ進むが、settle-close
    (:func:`_collect_stale_slot_executions`) は **running のみ** 対象なので拾えない。
    ここは episode を閉じるだけ — slot は fired のまま (handler は失敗しており done
    ではない)、台帳も unknown のまま (LLM が動いたか不明の照合対象)。孤児 episode を
    解消して後続記録の誤帰属を止めるのが目的。

    open episode が既に無ければ no-op なので冪等 (二度目以降の tick は空振り)。
    個別の例外は tick を殺さない。
    """
    ledger = manager.execution_ledger
    try:
        rows = ledger.list_unknown()
    except Exception:
        LOGGER.exception("[ledger-wiring] failed to list unknown executions")
        return
    from saiverse import day_plan, episodes

    for row in rows:
        if row.get("kind") != SLOT_FIRE_KIND:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        persona_id = payload.get("persona_id")
        plan_date = payload.get("plan_date")
        index = payload.get("index")
        if (
            not persona_id
            or not plan_date
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            continue
        try:
            origin_ref = day_plan._slot_origin_ref(persona_id, plan_date, index)
            open_ep = episodes.get_open_episode_by_origin(manager, persona_id, origin_ref)
            if not open_ep:
                continue
            episodes.close_episode(manager, persona_id, open_ep["episode_ref"])
            LOGGER.info(
                "[ledger-wiring] closed orphaned episode %s for unknown slot.fire "
                "(execution=%s persona=%s)",
                open_ep.get("episode_ref"), row.get("execution_id"), persona_id,
            )
        except Exception:
            LOGGER.exception(
                "[ledger-wiring] failed to close orphaned episode for unknown "
                "slot.fire (execution=%s)", row.get("execution_id"),
            )


def _collect_prepared_judgments(manager: "SAIVerseManager") -> None:
    """回復 #2: 走り出せなかった prepared 判断の回収 (intent §2.4 #2、D5)。

    refire 対象は **prepared のみ** — fallback 済みの実行は failed/unknown
    終端なので refire されない (二重応対なし)。refire は「行動を生む」ため、
    完全手動モード (``manager._debug_manual_mode_personas``) の対象ペルソナは
    スキップする。個別の例外は tick を殺さない。
    """
    ledger = manager.execution_ledger
    try:
        rows = ledger.list_prepared(JUDGMENT_KIND_PREFIX)
    except Exception:
        LOGGER.exception("[ledger-wiring] failed to list prepared judgments")
        return
    if not rows:
        return
    from saiverse import clock

    now = int(clock.now().timestamp())
    manual_personas = getattr(manager, "_debug_manual_mode_personas", None) or set()
    for row in rows:
        try:
            _collect_one_prepared(manager, row, now, manual_personas)
        except Exception:
            LOGGER.exception(
                "[ledger-wiring] prepared collection failed (execution=%s kind=%s)",
                row.get("execution_id"), row.get("kind"),
            )


def _collect_one_prepared(
    manager: "SAIVerseManager",
    row: Dict[str, Any],
    now: int,
    manual_personas: Any,
) -> None:
    """prepared 1 行に kind 別回収規則 (D5 の表) を適用する。"""
    ledger = manager.execution_ledger
    kind = row.get("kind") or ""
    execution_id = row.get("execution_id")
    persona_id = row.get("persona_id")
    created_at = row.get("created_at")
    if not execution_id or not isinstance(created_at, int):
        return
    age = now - created_at

    if kind in PREPARED_REFIRE_KINDS:
        if age < PREPARED_REFIRE_AFTER_SECONDS:
            return
        if persona_id in manual_personas:
            LOGGER.debug(
                "[ledger-wiring] prepared %s refire skipped: persona %s is in "
                "manual mode (execution=%s)", kind, persona_id, execution_id,
            )
            return
        judgment_kind = kind[len(JUDGMENT_KIND_PREFIX):]
        payload = row.get("payload")
        context = payload if isinstance(payload, dict) else None
        LOGGER.info(
            "[ledger-wiring] re-firing prepared %s (execution=%s persona=%s age=%ds)",
            kind, execution_id, persona_id, age,
        )
        # 遅延 import (wiring は autonomy_wiring から独立に import され得る)
        from saiverse import autonomy_wiring

        result = autonomy_wiring.fire_judgment_point(
            manager, persona_id, judgment_kind,
            context=context, resume_execution_id=execution_id,
        )
        if not result.get("submitted"):
            # ゲートで止まった refire (自律 OFF / Playbook 欠如等) が prepared の
            # まま毎 tick 回り続けないよう terminal に落とす (D5: refire 失敗は
            # terminal に落ちて終わり)。run 側が既に failed/unknown へ遷移させた
            # 場合はもう prepared ではないので触らない。
            try:
                current = ledger.get_execution(execution_id).get("status")
            except Exception:
                LOGGER.warning(
                    "[ledger-wiring] could not re-read refired execution %s",
                    execution_id, exc_info=True,
                )
                return
            if current == STATUS_PREPARED:
                ledger.mark_failed(
                    execution_id, f"refire failed: {result.get('reason')}"
                )
        return

    if kind in PREPARED_EXPIRE_KINDS:
        if age < PREPARED_EXPIRE_AFTER_SECONDS:
            return
        LOGGER.info(
            "[ledger-wiring] expiring prepared %s (execution=%s persona=%s age=%ds)",
            kind, execution_id, persona_id, age,
        )
        ledger.mark_failed(execution_id, "expired: prepared not executed")
        return

    LOGGER.debug(
        "[ledger-wiring] prepared %s has no collection rule; leaving as-is "
        "(execution=%s)", kind, execution_id,
    )


def _collect_prepared_schedule_dispatch(manager: "SAIVerseManager") -> None:
    """回復 #2 (W3 Codex 第六陣 P1): schedule.dispatch の prepared 残留を回収する。

    claim → try_mark_running の間のプロセス死で残った prepared は、予約
    (in-memory) が消えているため誰も発火させない。reconciliation は「次回
    occurrence」しか見ない — periodic なら翌日分を登録してしまい、**当日分の
    occurrence が永久に取りこぼされる**。ここが過去 occurrence の唯一の再発火
    経路。規則:

    - schedule 行が消えた / disabled / 世代・行トークンが payload と不一致
      → prepared を failed に落とす (副作用ゼロ保証の終端化。設定変更 = 新しい
      論理 occurrence なので旧 occurrence は放棄が正)
    - 一致 → :meth:`ScheduleManager.refire_occurrence` で同一 occurrence の
      予約を積み直す。発火側の claim が prepared を再利用し、二重実行は
      try_mark_running の席取りが防ぐ
    - refire は「行動を生む」ため、完全手動モードのペルソナはスキップ
      (:func:`_collect_prepared_judgments` と同じ規律)
    """
    schedule_manager = getattr(manager, "schedule_manager", None)
    if schedule_manager is None:
        return
    ledger = manager.execution_ledger
    try:
        rows = ledger.list_prepared(SCHEDULE_DISPATCH_KIND)
    except Exception:
        LOGGER.exception("[ledger-wiring] failed to list prepared schedule.dispatch")
        return
    if not rows:
        return
    from database.models import PersonaSchedule
    from saiverse import clock

    now = int(clock.now().timestamp())
    manual_personas = getattr(manager, "_debug_manual_mode_personas", None) or set()
    for row in rows:
        execution_id = row.get("execution_id")
        created_at = row.get("created_at")
        payload = row.get("payload")
        if not execution_id or not isinstance(created_at, int) \
                or not isinstance(payload, dict):
            continue
        if now - created_at < SCHEDULE_PREPARED_REFIRE_AFTER_SECONDS:
            continue
        if row.get("persona_id") in manual_personas:
            LOGGER.debug(
                "[ledger-wiring] prepared schedule.dispatch refire skipped: "
                "persona %s is in manual mode (execution=%s)",
                row.get("persona_id"), execution_id,
            )
            continue
        schedule_id = payload.get("schedule_id")
        occurrence = payload.get("occurrence")
        instance_token = payload.get("instance_token")
        generation = payload.get("generation")
        if not isinstance(schedule_id, int) or not occurrence \
                or not instance_token or not isinstance(generation, int):
            LOGGER.warning(
                "[ledger-wiring] prepared schedule.dispatch payload incomplete; "
                "marking failed (execution=%s payload=%r)", execution_id, payload,
            )
            try:
                ledger.mark_failed(execution_id, "prepared payload incomplete")
            except Exception:
                LOGGER.exception(
                    "[ledger-wiring] failed to fail incomplete prepared %s",
                    execution_id,
                )
            continue
        try:
            db = manager.SessionLocal()
            try:
                schedule = db.query(PersonaSchedule).filter(
                    PersonaSchedule.SCHEDULE_ID == schedule_id
                ).first()
                if schedule is None or not schedule.ENABLED:
                    reason = "schedule removed or disabled"
                elif (schedule.SYNC_GENERATION or 0) != generation:
                    reason = "superseded by config change (generation)"
                elif (schedule.INSTANCE_TOKEN or "legacy") != instance_token:
                    reason = "schedule row replaced (instance token)"
                else:
                    reason = None
            finally:
                db.close()
            if reason is not None:
                LOGGER.info(
                    "[ledger-wiring] abandoning prepared schedule.dispatch %s: %s",
                    execution_id, reason,
                )
                ledger.mark_failed(execution_id, reason)
                continue
            LOGGER.info(
                "[ledger-wiring] re-firing prepared schedule.dispatch "
                "(execution=%s schedule=%d occurrence=%s)",
                execution_id, schedule_id, occurrence,
            )
            payload_attempt = payload.get("attempt")
            schedule_manager.refire_occurrence(
                schedule_id, instance_token, occurrence, generation,
                # prepared は「まだ実行していない」試行なので attempt はそのまま
                # 運ぶ (第十陣: 既定 0 リセットは backoff 上限を迂回させる)
                attempt=(
                    payload_attempt
                    if isinstance(payload_attempt, int)
                    and not isinstance(payload_attempt, bool)
                    and payload_attempt >= 0
                    else 0
                ),
            )
        except Exception:
            LOGGER.exception(
                "[ledger-wiring] prepared schedule.dispatch collection failed "
                "(execution=%s)", execution_id,
            )


def _collect_failed_periodic_schedule_dispatch(manager: "SAIVerseManager") -> None:
    """回復 (W3 Codex 第八陣): failed だけ残して retry 予約を失った periodic の
    occurrence を回収する。

    `_retry_or_give_up` は mark_failed (durable) の後に backoff 予約 (揮発) を
    積む — 間でプロセスが死ぬと failed 行だけが残る。oneshot / interval は
    reconciliation が「同一 occurrence」を再登録して自然回復するが、**periodic
    は時刻通過後の再計算が翌回になる**ため、当日分はここが唯一の回復経路
    (第六陣 prepared 回収の failed 版)。規則:

    - 対象 = kind `schedule.dispatch`・payload の schedule_type が periodic・
      冪等キーが正準形 (claim がキーを ``#failed-`` へ退避した行は後継 claim が
      既に居るので対象外)
    - **予約の有無は判定に使わない** (Codex W3 第九陣 — 再起動後は
      ``schedule_manager.start()`` が翌回を先に登録するため区別にならない)。
      代わりに行齢 :data:`SCHEDULE_FAILED_RETRY_GRACE_SECONDS` で「backoff
      連鎖が死んでいる」ことを判定し、payload の ``attempt`` で「上限到達の
      意図的放棄」(= 回収しない) を識別する。回収の refire は同一 key の
      翌回予約を上書きするが、精算後の `_do_register` が翌回を積み直す
    - occurrence が :data:`SCHEDULE_FAILED_OCCURRENCE_MAX_AGE_SECONDS` より
      古いものは放棄 (「当日中の retry」の意味論 — 長時間停止後に古い判断を
      発火させない)
    - 行消滅 / disabled / 世代・行トークン不一致は何もしない (failed は既に
      副作用ゼロの終端)
    - refire は「行動を生む」ため手動モードのペルソナはスキップ
    """
    schedule_manager = getattr(manager, "schedule_manager", None)
    scheduler = getattr(manager, "event_scheduler", None)
    if schedule_manager is None or scheduler is None:
        return
    ledger = manager.execution_ledger
    try:
        rows = ledger.list_failed(
            SCHEDULE_DISPATCH_KIND,
            newer_than_seconds=SCHEDULE_FAILED_OCCURRENCE_MAX_AGE_SECONDS,
        )
    except Exception:
        LOGGER.exception("[ledger-wiring] failed to list failed schedule.dispatch")
        return
    if not rows:
        return
    from database.models import PersonaSchedule
    from saiverse import clock
    from saiverse.schedule_manager import SCHEDULE_DISPATCH_MAX_ATTEMPTS

    now = int(clock.now().timestamp())
    manual_personas = getattr(manager, "_debug_manual_mode_personas", None) or set()
    for row in rows:
        payload = row.get("payload")
        key = row.get("idempotency_key") or ""
        if not isinstance(payload, dict) or "#failed-" in key:
            continue
        if payload.get("schedule_type") != "periodic":
            continue
        # 猶予の起点は UPDATED_AT (= mark_failed が刻む失敗時刻、Codex W3
        # 第十陣)。CREATED_AT は claim 時刻なので、dispatch が長引いた失敗では
        # 「失敗直後なのに行齢が猶予超過」となり、生存中の backoff 予約を
        # 奪ってしまう。
        failed_at = row.get("updated_at")
        if not isinstance(failed_at, int) \
                or now - failed_at < SCHEDULE_FAILED_RETRY_GRACE_SECONDS:
            continue
        attempt = payload.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) \
                or attempt < 0 or attempt >= SCHEDULE_DISPATCH_MAX_ATTEMPTS:
            # 上限到達 = 意図的放棄 (翌回が正)、欠落・不正値 = 出所不明の行 —
            # どちらも自動回収しない (第十陣: 緩い判定は放棄済み行まで再発火
            # させる)
            continue
        occurrence = payload.get("occurrence")
        try:
            occurrence_epoch = int(occurrence)
        except (TypeError, ValueError):
            continue
        if now - occurrence_epoch > SCHEDULE_FAILED_OCCURRENCE_MAX_AGE_SECONDS:
            continue
        if row.get("persona_id") in manual_personas:
            continue
        schedule_id = payload.get("schedule_id")
        instance_token = payload.get("instance_token")
        generation = payload.get("generation")
        if not isinstance(schedule_id, int) or not instance_token \
                or not isinstance(generation, int):
            continue
        try:
            db = manager.SessionLocal()
            try:
                schedule = db.query(PersonaSchedule).filter(
                    PersonaSchedule.SCHEDULE_ID == schedule_id
                ).first()
                fence_ok = (
                    schedule is not None
                    and schedule.ENABLED
                    and (schedule.SYNC_GENERATION or 0) == generation
                    and (schedule.INSTANCE_TOKEN or "legacy") == instance_token
                )
            finally:
                db.close()
            if not fence_ok:
                continue
            LOGGER.info(
                "[ledger-wiring] re-firing failed periodic schedule.dispatch "
                "(execution=%s schedule=%d occurrence=%s attempt=%d)",
                row.get("execution_id"), schedule_id, occurrence, attempt + 1,
            )
            # 失敗した試行の「次」として再開する (attempt リセット禁止 —
            # 第十陣。attempt+1 が上限なら _retry_or_give_up が正しく打ち切る)
            schedule_manager.refire_occurrence(
                schedule_id, instance_token, str(occurrence), generation,
                attempt=attempt + 1,
            )
        except Exception:
            LOGGER.exception(
                "[ledger-wiring] failed-periodic collection failed (execution=%s)",
                row.get("execution_id"),
            )


def _reconcile_schedules(manager: "SAIVerseManager") -> None:
    """回復 #7: schedule 予約の世代照合 (W3 Chunk C / handoff D6)。

    実体は ``ScheduleManager._reconcile_schedules`` — DB (宣言的正典) と
    EventScheduler 予約の同期のみで、LLM を直接起動しない (登録・除去だけ)。
    したがって完全手動モードのペルソナに対しても止めない (掃除は自律行動では
    ない — 発火時ゲートの一貫化は W9 の所掌)。schedule_manager を持たない
    manager (テストスタブ等) は no-op。
    """
    schedule_manager = getattr(manager, "schedule_manager", None)
    if schedule_manager is None:
        return
    result = schedule_manager._reconcile_schedules()
    if result.get("registered") or result.get("cancelled"):
        LOGGER.info(
            "[ledger-wiring] schedule reconciliation: registered=%d cancelled=%d",
            result.get("registered", 0), result.get("cancelled", 0),
        )


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


def _make_saimemory_append_digest_handler(
    manager: "SAIVerseManager",
) -> Callable[[Dict[str, Any]], None]:
    """target='saimemory.append_digest' — 作業セッション digest の配送 (D9-5)。

    payload 契約 (積む側 = judgment_finalize の post_session):
        {"message": {...DIGEST_TAG / main_line / committed のダイジェスト行...},
         "episode_ref": str | None}
    冪等 append (adapter.append_ledger_message — 再配送は既存 message id を
    返す) の後、``episodes.set_digest_ref`` で出来事の再訪の鍵
    (``message:{id}``) を確定する。set_digest_ref も冪等 (同値 no-op / 別値は
    WARN + 非上書き) なので、append 済み→digest_ref 前のクラッシュ再配送でも
    二重にならない。set_digest_ref の失敗は例外 = 配送失敗 (pending 残存 →
    次 tick / 関所が引き継ぐ)。
    """
    def handler(item: Dict[str, Any]) -> None:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("saimemory.append_digest payload must be a dict")
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ValueError(
                "saimemory.append_digest payload requires 'message' dict"
            )
        persona_id = item.get("persona_id")
        adapter = _resolve_adapter(manager, persona_id)
        mid = adapter.append_ledger_message(
            message,
            execution_id=item["execution_id"],
            outbox_id=item["outbox_id"],
            building_id=None,
            thread_suffix=None,
        )
        episode_ref = payload.get("episode_ref")
        if episode_ref:
            from saiverse import episodes

            episodes.set_digest_ref(
                manager, persona_id, str(episode_ref), f"message:{mid}"
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


def _move_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    """move.post_* 共通の payload 検証 (W5/B1)。"""
    payload = item.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("move.post payload must be a dict")
    if not payload.get("entity_id") or not payload.get("to_id"):
        raise ValueError("move.post payload requires 'entity_id' and 'to_id'")
    return payload


def _make_move_dynamic_state_handler(
    manager: "SAIVerseManager",
) -> Callable[[Dict[str, Any]], None]:
    """target='move.post_dynamic_state' — 入室時の Dynamic State 同期。

    diff 通知の注入 + BUILDING_ENTERED の Section snapshot 再構築 (いずれも
    現在状態からの再構築 = 再配送安全)。ペルソナ未ロードは恒久 no-op (再試行
    しても届かない — 位置は DB 正典が持っており、次回ロード時に再構築される)。
    例外は配送失敗 = pending 残存 → 関所 / 回復 tick が再試行。

    ``on_building_entered`` 自身は内部の各段 (diff 通知 / 他ペルソナ通知 /
    visual_context push / head event dispatch) を独立した best-effort として
    例外を吸収する — 1 段の失敗が他段を止めない設計は変えない。ただし戻り値
    (全段成功したか) を見て、1 段でも失敗したら例外を投げて配送失敗にする
    (2026-07-21 Codex レビュー P2: 内部失敗を握ったまま delivered 記帳される
    と、失敗した段が二度と再試行されなかった)。
    """
    def handler(item: Dict[str, Any]) -> None:
        payload = _move_payload(item)
        entity_id = str(payload["entity_id"])
        if payload.get("entity_type") != "ai":
            return
        persona = (getattr(manager, "personas", {}) or {}).get(entity_id)
        if persona is None:
            LOGGER.info(
                "[ledger] move.post_dynamic_state: persona %s not loaded; "
                "skipping (state rebuilds on next load)", entity_id,
            )
            return
        from saiverse.dynamic_state import DynamicStateManager
        to_id = str(payload["to_id"])
        if not DynamicStateManager.on_building_entered(persona, to_id, manager):
            raise RuntimeError(
                f"dynamic state sync incomplete for persona={entity_id} "
                f"building={to_id} (see prior WARNING logs for the failing "
                f"stage)"
            )
    return handler


def _make_move_addon_hooks_handler(
    manager: "SAIVerseManager",
) -> Callable[[Dict[str, Any]], None]:
    """target='move.post_addon_hooks' — 建物移動の addon hook 発火。

    退室 → 入室の順序は 1 配送内で保持 (旧実装と同じ)。dispatch_hook は
    addon ごとの例外を内部で吸収する設計のため、ここまで届いた例外だけが
    配送失敗として再試行される。
    """
    def handler(item: Dict[str, Any]) -> None:
        payload = _move_payload(item)
        if payload.get("entity_type") != "ai":
            return
        from saiverse.addon_hooks import dispatch_hook
        entity_id = str(payload["entity_id"])
        from_id = payload.get("from_id")
        to_id = str(payload["to_id"])
        dispatch_hook(
            "persona_exited_building",
            persona_id=entity_id,
            building_id=from_id,
            from_building_id=from_id,
            to_building_id=to_id,
        )
        dispatch_hook(
            "persona_entered_building",
            persona_id=entity_id,
            building_id=to_id,
            from_building_id=from_id,
        )
    return handler


def _make_move_game_lifecycle_handler(
    manager: "SAIVerseManager",
) -> Callable[[Dict[str, Any]], None]:
    """target='move.post_game_lifecycle' — game Region の自動ポーズ/再開。

    on_entity_moved は現在状態を読んで判定する (再配送安全)。game_lifecycle の
    無い manager は no-op。戻り値 (成功したか) を見て、失敗なら例外を投げて
    配送失敗にする (2026-07-21 Codex レビュー P2: on_entity_moved は移動本体を
    守るため内部例外を吸収する設計だが、それを outbox が「配送成功」と誤記帳
    しないよう、握った失敗を戻り値経由でここまで引き上げる)。
    """
    def handler(item: Dict[str, Any]) -> None:
        payload = _move_payload(item)
        lifecycle = getattr(manager, "game_lifecycle", None)
        if lifecycle is None:
            return
        entity_id = str(payload["entity_id"])
        to_id = str(payload["to_id"])
        if not lifecycle.on_entity_moved(entity_id, payload.get("from_id"), to_id):
            raise RuntimeError(
                f"game lifecycle sync failed for entity={entity_id} -> {to_id} "
                f"(see prior exception log for the cause)"
            )
    return handler
