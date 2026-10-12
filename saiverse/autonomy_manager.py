"""ペルソナごとの自律の運転 — ティックの予約と、見張り (watchdog) の予約。

v0.4 計画 段 3 (docs/intent/autonomous_behavior_v04_plan.md、設計の中身は
autonomous_behavior_v3.md §5) で、旧「50 分間隔の watchdog タイマー」を二本立てに
作り直した。どちらも EventScheduler の予約で、発火はワーカースレッドで走る
(§8.2 決定 A — callback は LLM を呼んでブロックしてよい)。

1. **ティックの予約** (key ``tick:{persona_id}``、1 ペルソナ 1 枚 — §8.3 の
   「ティック × ティック」行)。ティック = ライフ中に間隔で打たれる本人の 1 Pulse。
   予約は「最後の標準呼び出しから T 分」(決定 10):

   - ライフ開始の節目が now + T に張り (``day_plan.apply_life_boundary``)、終了の
     節目が消す
   - 本人の標準モデルが呼ばれるたびに now + T へ押し直す
     (:meth:`AutonomyManager.notify_standard_call` — 呼び口は
     ``sea.session_lifecycle.SessionLifecycle.touch_anchor_after_llm_call``)。
     予約が無いとき (ライフ外) は生やさない
   - 発火したら、先に次回を now + T で張り直し、ゲート (全停止・自律 OFF・
     ライフ外・会話中) を見て、通ればタスク帳から割り当てを引き当てて
     ``SAIVerseManager.fire_tick`` を呼ぶ

2. **見張り** (key ``autonomy-watchdog:{persona_id}``、固定間隔
   :data:`WATCHDOG_INTERVAL_MINUTES`)。中身は ``PulseDispatcher.dispatch_autonomy_tick``
   → ``autonomy_wiring.watchdog_tick`` — ライフの張り直しと、走っているライフに
   ティックの予約が無いときの張り直し (再起動からの回復、段 3-7)。正常時は
   何もしない (LLM も呼ばない)。

T (ティックの間隔) は予約を張るたびに現在の設定から読む
(``day_plan.resolve_tick_interval_minutes`` — 既定 30 分・床 10 分・均等モードは
50 分上限、決定 11)。ライフに焼き込まないので、ユーザーが日中に変えたら次の
張り直しから効く。

start / stop は自律ゲート (``autonomy_wiring.is_autonomy_on``) への同期
(``SAIVerseManager.ensure_autonomy_for``) が握る。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

from saiverse import clock
from saiverse.day_plan import (  # noqa: F401 (再公開: ティックの間隔の定数の在処)
    DEFAULT_TICK_INTERVAL_MINUTES,
    TICK_INTERVAL_FLOOR_MINUTES,
)

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)

#: 見張り (watchdog) の固定間隔 (分)。ライフの張り直しとティックの予約の回復の
#: 遅れの上限。中身は正常時 no-op で LLM を呼ばないので、短くても課金は無い。
WATCHDOG_INTERVAL_MINUTES = 10


def watchdog_reservation_key(persona_id: str) -> str:
    """見張りの EventScheduler 予約 key。"""
    return f"autonomy-watchdog:{persona_id}"


def tick_reservation_key(persona_id: str) -> str:
    """ティックの EventScheduler 予約 key (1 ペルソナ 1 枚)。"""
    return f"tick:{persona_id}"


class AutonomyState(str, Enum):
    """AutonomyManager の状態。"""
    STOPPED = "stopped"
    RUNNING = "running"


@dataclass
class CycleReport:
    """発火 1 回の顛末 (観察・テスト用)。

    status: "completed" (ティックが走りきった) / "skipped" (ゲートで見送った —
    reason に理由) / "not_completed" (fire_tick は呼んだが走りきらなかった) /
    "error" (例外)。
    """
    cycle_id: str
    kind: str = "tick"  # "tick" | "watchdog"
    started_at: float = 0.0
    completed_at: float = 0.0
    status: str = "pending"
    reason: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# 割り当ての引き当て (v3 §5「割り付け」、段 3-6)
# ---------------------------------------------------------------------------


def _format_due(due_at: int) -> str:
    return datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M")


def _is_missing_table(exc: BaseException) -> bool:
    """タスク帳のテーブルがまだ無い DB (= 一件も無い) を、他の失敗と分ける。"""
    return "no such table" in str(exc).lower()


class TaskBookUnreadable(Exception):
    """タスク帳が読めなかった (引き当ての結果が分からない)。"""


def build_tick_assignment(
    manager: Any, persona_id: str, *, now: datetime, horizon: datetime,
) -> Optional[str]:
    """このティックの割り当て文を、タスク帳から機械的に引き当てる。

    順序 (決定論、v3 §5): **締め切り** → **システムタスク** → 無ければ None
    (自由時間 — Playbook の既定文)。

    - 締め切り: 期限のある open な一件のうち、期限が既に過ぎているか、次の
      ティックまで (``horizon`` = now + T) に期限が来るもの。最も期限の早い一件
    - システムタスク: 機械からの急ぎでない依頼 (ORIGIN='system') の先頭 (作成順)

    割り当て文は確定情報の文 (「この時間は〜に使います」) で、問いは渡さない
    (v3 §5 — v1 の病因 2 を踏まない)。

    Raises:
        TaskBookUnreadable: タスク帳を読めなかった (テーブルが無いのは空として
            扱う)。読めないまま自由時間にすると、締め切り優先の保証が黙って
            外れる。
    """
    from saiverse import task_book

    def _read(fn):
        try:
            return fn(manager, persona_id)
        except Exception as exc:
            if _is_missing_table(exc):
                return []
            raise TaskBookUnreadable(str(exc)) from exc

    horizon_epoch = int(horizon.timestamp())
    now_epoch = int(now.timestamp())
    with_due = _read(task_book.list_open_with_due)
    if with_due:
        nearest = with_due[0]  # DUE_AT 昇順 — 先頭が最も期限の早い一件
        due_at = nearest.get("due_at")
        if isinstance(due_at, int) and due_at <= horizon_epoch:
            content = str(nearest.get("content") or "").strip()
            if due_at <= now_epoch:
                return (
                    f"この時間は、タスク帳の一件「{content}」に使います。"
                    f"期限の {_format_due(due_at)} はもう過ぎています。"
                )
            return (
                f"この時間は、タスク帳の一件「{content}」に使います。"
                f"期限は {_format_due(due_at)} です。"
            )
    system_tasks = _read(task_book.list_open_system_tasks)
    if system_tasks:
        content = str(system_tasks[0].get("content") or "").strip()
        return f"この時間は、システムからの依頼「{content}」に使います。"
    return None


# ---------------------------------------------------------------------------
# AutonomyManager
# ---------------------------------------------------------------------------


class AutonomyManager:
    """ペルソナ 1 人ぶんの自律の運転 (ティックの予約と見張りの予約)。"""

    def __init__(self, persona_id: str, manager: "SAIVerseManager"):
        self.persona_id = persona_id
        self.manager = manager
        self._state = AutonomyState.STOPPED
        self._current_cycle_id: Optional[str] = None
        self._last_report: Optional[CycleReport] = None
        # 状態の遷移と、予約の張り直し (状態を見てから張る) を同じ区間に置く —
        # stop() と発火中の張り直しが交差して、止めた後に予約が生き返らないように。
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> AutonomyState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state != AutonomyState.STOPPED

    @property
    def last_report(self) -> Optional[CycleReport]:
        return self._last_report

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """見張りを起動する。起動済みなら False。

        最初の見張りは即時に走る — 自律 ON になった時点 (起動・切り替え・全停止
        の解除) で、今日のライフが確定していなければ起床の帳簿処理を火入れし直し、
        走っているライフにティックの予約が無ければ張る。ティックの予約そのものは
        ここでは張らない (ライフの節目と見張りが張る)。
        """
        with self._lock:
            if self._state != AutonomyState.STOPPED:
                LOGGER.warning(
                    "[Autonomy:%s] Cannot start: already in state %s",
                    self.persona_id, self._state,
                )
                return False
            self._state = AutonomyState.RUNNING
            # 期限は EventScheduler と同じ clock.now() で刻む (issue
            # autonomy_manager_tick_uses_wall_clock)
            self._schedule_locked(
                watchdog_reservation_key(self.persona_id), clock.now(),
                self._handle_watchdog,
            )
        LOGGER.info(
            "[Autonomy:%s] Started (watchdog every %d min, tick by the life)",
            self.persona_id, WATCHDOG_INTERVAL_MINUTES,
        )
        return True

    def stop(self) -> bool:
        """見張りとティックの予約を両方消す。走っていなければ False。"""
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return False
            self._state = AutonomyState.STOPPED
            scheduler = self._scheduler()
            if scheduler is not None:
                scheduler.cancel(watchdog_reservation_key(self.persona_id))
                scheduler.cancel(tick_reservation_key(self.persona_id))
        LOGGER.info("[Autonomy:%s] Stopped", self.persona_id)
        return True

    # ------------------------------------------------------------------
    # ティックの予約
    # ------------------------------------------------------------------

    def resolve_interval_minutes(self, *, life_mode: Optional[str] = None) -> int:
        """いまの T (分) — 予約を張るたびに設定から読む (決定 11)。"""
        from saiverse.day_plan import resolve_tick_interval_minutes

        return resolve_tick_interval_minutes(
            self.manager, self.persona_id, life_mode=life_mode,
        )

    def arm_tick(self, *, life_mode: Optional[str] = None) -> bool:
        """ティックの予約を now + T に張る (既存の予約は上書き)。

        呼び手はライフ開始の節目 (``day_plan._arm_tick_reservation``)。止まって
        いれば張らない (False)。
        """
        minutes = self.resolve_interval_minutes(life_mode=life_mode)
        fire_at = clock.now() + timedelta(minutes=minutes)
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return False
            self._schedule_locked(
                tick_reservation_key(self.persona_id), fire_at, self._handle_tick,
            )
        LOGGER.info(
            "[Autonomy:%s] tick armed at %s (T=%d min)",
            self.persona_id, fire_at.isoformat(timespec="seconds"), minutes,
        )
        return True

    def arm_tick_if_absent(self, *, life_mode: Optional[str] = None) -> bool:
        """ティックの予約が無いときだけ now + T に張る (生きている予約は触らない)。

        呼び手は見張り (再起動からの回復)。Returns: 今回張ったか。
        """
        scheduler = self._scheduler()
        if scheduler is None:
            return False
        minutes = self.resolve_interval_minutes(life_mode=life_mode)
        fire_at = clock.now() + timedelta(minutes=minutes)
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return False
            return scheduler.schedule_if_absent(
                fire_at=fire_at, callback=self._handle_tick,
                key=tick_reservation_key(self.persona_id),
            )

    def cancel_tick(self) -> bool:
        """ティックの予約を消す。Returns: 予約があって消したか。"""
        scheduler = self._scheduler()
        if scheduler is None:
            return False
        cancelled = scheduler.cancel(tick_reservation_key(self.persona_id))
        if cancelled:
            LOGGER.info("[Autonomy:%s] tick reservation cancelled", self.persona_id)
        return cancelled

    def notify_standard_call(self) -> bool:
        """本人の標準モデルが呼ばれた — ティックの予約を now + T へ押し直す (決定 10)。

        **予約が現に有るときだけ**動かす (``schedule_if_present``) — 予約が無い
        (ライフ外・自律 OFF) なら何もしない。ライフ外の会話でティックを生やさ
        ないため。呼び手は ``SessionLifecycle.touch_anchor_after_llm_call``
        (keep-alive の保温は呼ばない — 保温がティックを永遠に先送りするのを防ぐ)。

        Returns:
            押し直したか。
        """
        scheduler = self._scheduler()
        if scheduler is None:
            return False
        key = tick_reservation_key(self.persona_id)
        # T の解決は DB を読むので、予約の無い (ライフ外の) 呼び出しでは読まない。
        if not scheduler.has_key(key):
            return False
        minutes = self.resolve_interval_minutes()
        fire_at = clock.now() + timedelta(minutes=minutes)
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return False
            moved = scheduler.schedule_if_present(
                fire_at=fire_at, callback=self._handle_tick, key=key,
            )
        if moved:
            LOGGER.debug(
                "[Autonomy:%s] tick pushed back to %s by a standard call (T=%d min)",
                self.persona_id, fire_at.isoformat(timespec="seconds"), minutes,
            )
        return moved

    def tick_fire_at(self) -> Optional[datetime]:
        """ティックの予約の発火時刻 (無ければ None)。keep-alive の抑制が読む。"""
        scheduler = self._scheduler()
        if scheduler is None:
            return None
        return scheduler.fire_time_of(tick_reservation_key(self.persona_id))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """状態の dict (API・観察用)。``interval_minutes`` は解決済みの T。"""
        try:
            interval = self.resolve_interval_minutes()
        except Exception:
            LOGGER.warning(
                "[Autonomy:%s] failed to resolve T for status", self.persona_id,
                exc_info=True,
            )
            interval = None
        report = self._last_report
        return {
            "persona_id": self.persona_id,
            "state": self._state.value,
            "interval_minutes": interval,
            "tick_armed": self.tick_fire_at() is not None,
            "next_tick_eta_seconds": self.get_next_tick_eta_seconds(),
            "current_cycle_id": self._current_cycle_id,
            "last_report": {
                "cycle_id": report.cycle_id,
                "kind": report.kind,
                "status": report.status,
                "reason": report.reason,
                "error": report.error,
            } if report else None,
        }

    def get_next_tick_eta_seconds(self) -> Optional[int]:
        """次のティックまでの残り秒数。予約が無ければ None。"""
        fire_at = self.tick_fire_at()
        if fire_at is None:
            return None
        remaining = fire_at.timestamp() - clock.now().timestamp()
        return max(0, int(remaining))

    # ------------------------------------------------------------------
    # 発火
    # ------------------------------------------------------------------

    def _handle_watchdog(self) -> None:
        """見張りの発火: watchdog_tick を回し、次回を固定間隔で張り直す。"""
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return
        report = CycleReport(
            cycle_id=str(uuid.uuid4())[:8], kind="watchdog", started_at=time.time(),
        )
        try:
            dispatcher = getattr(self.manager, "pulse_dispatcher", None)
            if dispatcher is None:
                report.status = "error"
                report.error = "pulse_dispatcher unavailable"
                LOGGER.warning(
                    "[Autonomy:%s] No pulse_dispatcher on manager; skipping watchdog",
                    self.persona_id,
                )
            else:
                dispatcher.dispatch_autonomy_tick(
                    self.persona_id,
                    context={
                        "cycle_id": report.cycle_id,
                        "interval_seconds": WATCHDOG_INTERVAL_MINUTES * 60,
                    },
                )
                report.status = "completed"
        except Exception as exc:
            LOGGER.exception("[Autonomy:%s] watchdog error: %s", self.persona_id, exc)
            report.status = "error"
            report.error = str(exc)
        report.completed_at = time.time()
        self._last_report = report
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return
            self._schedule_locked(
                watchdog_reservation_key(self.persona_id),
                clock.now() + timedelta(minutes=WATCHDOG_INTERVAL_MINUTES),
                self._handle_watchdog,
            )

    def _handle_tick(self) -> None:
        """ティックの予約の発火 (ワーカースレッド上で走る — LLM を呼んでよい)。"""
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return
        report = CycleReport(
            cycle_id=str(uuid.uuid4())[:8], kind="tick", started_at=time.time(),
        )
        self._current_cycle_id = report.cycle_id
        try:
            report.status, report.reason = self._run_tick()
        except Exception as exc:
            LOGGER.exception("[Autonomy:%s] tick error: %s", self.persona_id, exc)
            report.status = "error"
            report.error = str(exc)
        report.completed_at = time.time()
        self._last_report = report
        self._current_cycle_id = None
        LOGGER.info(
            "[Autonomy:%s] tick %s: %s%s (%.1fs)",
            self.persona_id, report.cycle_id, report.status,
            f" ({report.reason})" if report.reason else "",
            report.completed_at - report.started_at,
        )

    def _run_tick(self) -> Tuple[str, Optional[str]]:
        """ティック 1 回の本体。``(status, reason)`` を返す。"""
        from saiverse import day_plan
        from saiverse.autonomy_wiring import is_autonomy_all_stopped, is_autonomy_on

        manager = self.manager
        persona_id = self.persona_id
        fired_at = clock.now()

        # 1. 先に次回を now + T で張り直す — この発火の途中 (LLM の失敗・例外) で
        #    連鎖が切れないため。中で標準呼び出しが成功すれば notify_standard_call
        #    がもう少し後ろへ押し直す。
        minutes = self.resolve_interval_minutes()
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return ("skipped", "stopped")
            self._schedule_locked(
                tick_reservation_key(persona_id),
                fired_at + timedelta(minutes=minutes),
                self._handle_tick,
            )

        # 2. ゲート (通らない回は何もしない)
        if is_autonomy_all_stopped(manager):
            # 解除時の復帰は ensure_autonomy_for の一斉再同期と見張りの張り直し。
            self.cancel_tick()
            return ("skipped", "autonomy all-stop")
        if not is_autonomy_on(manager, persona_id):
            self.cancel_tick()
            return ("skipped", "autonomy off")
        basis = day_plan.resolve_business_day(manager, persona_id, now=fired_at)
        if basis is None:
            # ライフを読めない — どの日を生きているか分からない。予約は残して
            # 次の回に委ねる (一時的な読み取り失敗で連鎖を切らない)。
            return ("skipped", "lives unreadable")
        first = basis.lives[0] if basis.lives else None
        if (
            basis.source != "life"
            or not isinstance(first, dict)
            or not first.get("started")
            or first.get("ended")
        ):
            # ライフが現に走っていない — 次のライフ開始の節目が張り直す。
            self.cancel_tick()
            return ("skipped", "outside life")
        conversation = day_plan.get_user_conversation_state(manager, persona_id)
        if conversation is not False:
            # 会話が開いている (または分からない) 間は空のティックを打たない
            # (v3 §5 — 保温は会話自身がしている)。予約は 1 の張り直しのまま残る。
            return ("skipped", "in conversation" if conversation else "conversation unknown")

        # 3. 引き当て: 締め切り → システムタスク → 自由
        try:
            assignment = build_tick_assignment(
                manager, persona_id, now=fired_at,
                horizon=fired_at + timedelta(minutes=minutes),
            )
        except TaskBookUnreadable as exc:
            LOGGER.warning(
                "[Autonomy:%s] task book unreadable; skipping this tick so a "
                "deadline is not silently replaced by free time: %s",
                persona_id, exc,
            )
            return ("skipped", "task book unreadable")

        # 4. ティックを打つ
        outcome = manager.fire_tick(persona_id, assignment)
        if (
            outcome.get("action") == "execute"
            and outcome.get("runtime_outcome") == "completed"
        ):
            try:
                day_plan.consume_life_pulse(
                    manager, persona_id, basis.plan_date,
                    at_time=fired_at.strftime("%H:%M"),
                )
            except Exception:
                LOGGER.warning(
                    "[Autonomy:%s] failed to record the tick in the life ledger",
                    persona_id, exc_info=True,
                )
            return ("completed", "assigned" if assignment else "free")
        return (
            "not_completed",
            f"action={outcome.get('action')} outcome={outcome.get('runtime_outcome')}",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _scheduler(self) -> Any:
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            LOGGER.warning(
                "[Autonomy:%s] event_scheduler not available", self.persona_id,
            )
        return scheduler

    def _schedule_locked(self, key: str, fire_at: datetime, callback) -> None:
        """``self._lock`` を持った状態で予約を張る (上書き)。"""
        scheduler = self._scheduler()
        if scheduler is None:
            return
        scheduler.schedule(fire_at=fire_at, callback=callback, key=key)
        LOGGER.debug(
            "[Autonomy:%s] scheduled %s at %s",
            self.persona_id, key, fire_at.isoformat(timespec="seconds"),
        )
