"""ScheduleManager — push 駆動のペルソナスケジューラ (Phase 4-e)。

ペルソナの定期 / 一回 / 恒常スケジュール (PersonaSchedule テーブル) を
EventScheduler 経由で発火する。

旧実装 (60 秒固定ポーリング) は v0.3.0 dev で完全廃止。各スケジュールごとに
次回発火時刻を計算して EventScheduler.schedule() に push する形に置き換えた。

ライフサイクル:
- ``start()``: DB から有効スケジュールを全件読み出し、各々を EventScheduler
  に register する
- ``stop()``: 登録済み全予約を EventScheduler から cancel する
- ``register_schedule(schedule_id)``: 1 件のスケジュールを (再)登録。
  作成・更新・トグル ON 時に API 層から呼ばれる
- ``unregister_schedule(schedule_id)``: 1 件の予約をキャンセル。
  削除・トグル OFF 時に API 層から呼ばれる

register/unregister の失敗は回復 tick (execution_ledger_wiring、60 秒周期) が
呼ぶ ``_reconcile_schedules()`` (W3 Chunk C、handoff D6) が世代照合で自己回復
する — DB が宣言的正典で、予約 (EventScheduler) はそのキャッシュ。

発火後は callback 内で (W3 Chunk B、docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md D3):
  1. 世代照合 (旧世代予約は実行せず最新 DB で再登録)
  2. 実行台帳 ``schedule.dispatch`` の claim + 席取り (二重発火 dedup)
  3. メタプレイブックを PulseController に投げ、型付き outcome を得る (D4)
  4. 精算: executed/accepted/settled_skip は「schedule 状態前進 + mark_applied」を
     単一 commit で行い、次回発火を再 register。failed は backoff 再試行 (D5)。
     unknown は台帳に刻むだけ (LLM 自動再実行禁止、intent §2.5)
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import func

from database.models import PersonaSchedule, AI as AIModel, City as CityModel

if TYPE_CHECKING:
    from .saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)

#: アラーム (PersonaSchedule) が発火したときに動かす既定の Playbook。
#: どの Playbook で動くかはユーザーにもペルソナにも選ばせない (2026-09-01 裁定) —
#: 判断点の Playbook はコードが決定論的に発火させ、生活リズムはライフ設定が
#: 所有するので、アラームの作成者が選ぶ意味のある選択肢はこれ一つしかない。
#: アラームを作る口が三つ (UI の REST 作成、ペルソナの schedule_add スペル、
#: ライフ設定) あるので、既定値はここ一箇所に置いて各入口から参照する。
DEFAULT_META_PLAYBOOK = "track_user_conversation"

# 実行台帳の kind と冪等キー (handoff D1 + W3 Codex 第三陣):
# key = f"{schedule_id}:{instance_token}:{occurrence_token}"
SCHEDULE_DISPATCH_LEDGER_KIND = "schedule.dispatch"

# failed (副作用ゼロ確定) の backoff 再試行 (handoff D5)
SCHEDULE_DISPATCH_RETRY_BACKOFF_SECONDS = 120.0
SCHEDULE_DISPATCH_MAX_ATTEMPTS = 3

#: waiting (勝者の judgment の終端待ち — attempt 非消費) の failed 行に刻む
#: ERROR 接頭辞。mark_failed と揮発 backoff 予約の間で crash した waiting 行を、
#: 回収 (_collect_failed_periodic_schedule_dispatch) が通常の失敗と区別して
#: attempt を据え置いたまま refire するための durable な印 —
#: 印なしだと回収の attempt+1 が待機を失敗として数え、上限到達で当日
#: occurrence を失う (Codex 四巡目 high1)。
SCHEDULE_DISPATCH_WAITING_ERROR_PREFIX = "waiting:"

# reconciliation (handoff D6) で「次回 occurrence の再登録」をブロックする台帳
# status。running = 実行中 (>60s の長 Pulse) への二重登録防止 / applied・
# completed = 既に済んだ occurrence / unknown = 裁定待ちの自動再実行禁止
# (intent §2.5)。prepared (claim 後 crash) と failed (backoff 尽き) は再登録して
# 自己回復する — claim_execution が prepared を再利用し、failed キーを退避する。
_OCCURRENCE_BLOCKING_STATUSES = frozenset(
    {"running", "applied", "completed", "unknown"}
)


def _schedule_key(schedule_id: int) -> str:
    """EventScheduler に渡す key (schedule_id 単位で一意)。"""
    return f"persona_schedule:{schedule_id}"


def _occurrence_token(schedule: PersonaSchedule, next_fire: datetime) -> str:
    """occurrence を識別する安定トークン (Codex W3 指摘 1)。

    通常は発火予定時刻の epoch 文字列。ただし interval の初回
    (``LAST_EXECUTED_AT is None``) は ``_next_interval_fire`` が「現在時刻」を
    返すため、再計算のたびに epoch が変わり別の冪等キーになってしまう —
    unknown による自動再実行禁止 (intent §2.5) を reconciliation / 再起動が
    すり抜ける穴。そこで初回 interval は安定 sentinel ``"first"`` に固定する:
    初回が unknown で終わればキーが同一世代内でブロックされ続け、oneshot の
    unknown と同じ「裁定待ち」挙動になる。初回成功で LAST_EXECUTED_AT が
    入れば以後は epoch ベースに戻る。

    設定世代はここではなく :func:`_occurrence_key` の独立成分 ``g{N}`` が
    持つ (Codex W3 第七陣 — 当初は初回 interval だけ ``first@g{N}`` と
    世代を埋めていたが、「設定変更 = 新しい論理 occurrence」は oneshot /
    periodic / 二回目以降 interval にも等しく適用されるべき軸なので、
    キー構造の独立成分へ昇格した)。
    """
    if schedule.SCHEDULE_TYPE == "interval" and schedule.LAST_EXECUTED_AT is None:
        return "first"
    return str(int(next_fire.timestamp()))


def _instance_token(schedule: PersonaSchedule) -> str:
    """行の一生に固有なトークン (W3 Codex 第三陣)。

    SQLite は AUTOINCREMENT 無しの INTEGER PK で削除済み最大 ID を再利用しうる
    ため、SCHEDULE_ID だけでは「実行済み行を削除 → 新規作成」の新旧行を台帳が
    区別できない (旧行の completed 台帳行が新行の claim を永久ブロックする)。
    INSTANCE_TOKEN は行作成時に書き手が採番し更新では変えない — 世代
    (SYNC_GENERATION=設定の版) とは別概念の「行の同一性」。NULL (migration 前の
    DB / テストの直接 INSERT) は "legacy" として扱う (backfill 後は実質 NULL 無し)。
    """
    return schedule.INSTANCE_TOKEN or "legacy"


def _occurrence_key(
    schedule_id: int, instance_token: str, generation: int, occurrence_token: str
) -> str:
    """台帳の冪等キー (handoff D1 + W3 Codex 第三陣・第七陣)。

    同一性の軸を全て独立成分として持つ: schedule_id + instance_token
    (どの設定**行**か — SCHEDULE_ID 再利用の新旧分離) + ``g{generation}``
    (設定の**何版**か — 「ユーザーの設定変更 = 新しい論理 occurrence」を
    全 schedule 種別で実装する。unknown 封印は同一世代内でのみ効き、設定
    変更で新しいキーになる) + occurrence_token (**いつの分**か)。
    同一世代内の再試行・再発火はここで収束する。
    """
    return f"{schedule_id}:{instance_token}:g{generation}:{occurrence_token}"


class ScheduleManager:
    """ペルソナスケジュールを EventScheduler 経由で発火する管理クラス。"""

    def __init__(self, saiverse_manager: "SAIVerseManager"):
        self.manager = saiverse_manager
        # schedule_id → 登録時の (INSTANCE_TOKEN, SYNC_GENERATION) (W3 D2)。
        # reconciliation が「登録済みの行・世代 ≠ DB の行・世代」の検出にこの
        # map を読む。行トークンも持つのは、削除→SCHEDULE_ID 再利用→新規作成で
        # 新旧行の世代が偶然一致 (ともに 1 等) すると、世代だけの照合では旧予約を
        # 「同期済み」と誤認して新行を登録しないため (Codex W3 第五陣 P1)。
        self._registered: Dict[int, Tuple[str, int]] = {}
        # 互換性のため属性は残すが、未使用 (旧 _schedule_loop で使われていた)
        self._stop_event = None
        # 台帳の無い manager (旧テストスタブ) への degrade WARN を一回に抑える
        self._ledger_missing_warned = False
        LOGGER.info("[ScheduleManager] Initialized (push-driven via EventScheduler)")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """全有効スケジュールを EventScheduler に register する。

        EventScheduler は本メソッド呼び出し時点で start していなくても問題ない
        (schedule() は heap に積むだけで dispatch スレッド起動は別工程)。
        """
        session = self.manager.SessionLocal()
        try:
            schedules = (
                session.query(PersonaSchedule)
                .filter(PersonaSchedule.ENABLED == True)  # noqa: E712
                .all()
            )
            registered_count = 0
            for schedule in schedules:
                try:
                    if self._do_register(schedule, session):
                        registered_count += 1
                except Exception:
                    LOGGER.exception(
                        "[ScheduleManager] Failed to register schedule %d at startup",
                        schedule.SCHEDULE_ID,
                    )
            LOGGER.info(
                "[ScheduleManager] Started: registered %d/%d enabled schedules",
                registered_count, len(schedules),
            )
        finally:
            session.close()

    def stop(self) -> None:
        """登録済み全予約を EventScheduler から cancel する。"""
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            self._registered.clear()
            return

        for schedule_id in list(self._registered):
            try:
                scheduler.cancel(_schedule_key(schedule_id))
            except Exception:
                LOGGER.exception(
                    "[ScheduleManager] Failed to cancel schedule %d on stop",
                    schedule_id,
                )
        self._registered.clear()
        LOGGER.info("[ScheduleManager] Stopped")

    # ------------------------------------------------------------------
    # Public API: 個別スケジュールの register / unregister
    # ------------------------------------------------------------------

    def register_schedule(self, schedule_id: int) -> str:
        """1 件のスケジュールを (再)登録する。既存予約は上書きされる。

        作成・更新・トグル ON 時に API ルートから呼ばれる。スケジュールが
        無効化されていたり、次回発火時刻が計算できない (oneshot 完了済 等)
        場合は EventScheduler から cancel するだけで終わる。

        Returns:
            tri-state の文字列 (Codex W3 指摘 2 — 「予約が無いのが正」と
            「予約すべきなのに作れない」を呼び出し元が区別できるように):

            - ``"registered"``: 予約を作った
            - ``"no_reservation_needed"``: 予約が無いのが正しい状態
              (行なし = 削除済み / disabled / 完了済み oneshot)
            - ``"not_registrable"``: 有効で発火すべきなのに予約を作れない
              (scheduler 不在・設定不備: periodic の TIME_OF_DAY 欠落、
              oneshot の SCHEDULED_DATETIME 欠落、interval の
              INTERVAL_SECONDS 欠落、空の DAYS_OF_WEEK 等)。
              reconciliation でも回復できないため、API 層は
              ``scheduler_synced=False`` として応答に明示する
        """
        session = self.manager.SessionLocal()
        try:
            schedule = session.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id
            ).first()
            if schedule is None:
                # 削除されたケース: cancel のみ
                self._do_cancel(schedule_id)
                return "no_reservation_needed"
            if self._do_register(schedule, session):
                return "registered"
            # _do_register False の内訳分類 (判定ロジックは _do_register と
            # 同じ入力を読むだけで、二重実装はしない)
            if not schedule.ENABLED:
                return "no_reservation_needed"
            if getattr(self.manager, "event_scheduler", None) is None:
                return "not_registrable"
            if schedule.SCHEDULE_TYPE == "oneshot" and schedule.COMPLETED:
                return "no_reservation_needed"
            # 有効なのに next_fire が計算できない = 設定不備
            return "not_registrable"
        finally:
            session.close()

    def refire_occurrence(
        self,
        schedule_id: int,
        instance_token: str,
        occurrence_token: str,
        generation: int,
        attempt: int = 0,
    ) -> None:
        """prepared 残留 occurrence の再発火予約を積む (Codex W3 第六陣 P1)。

        claim → try_mark_running の間でプロセスが死ぬと、`schedule.dispatch` の
        prepared 行だけが残り予約 (in-memory) は消える。reconciliation は「現在
        時刻から計算した次回 occurrence」しか照合しないため、periodic の当日分
        など**過去の occurrence** はここから再発火させる必要がある。呼び出し元
        は回復 tick の prepared 回収
        (:func:`saiverse.execution_ledger_wiring._collect_prepared_schedule_dispatch`)
        — payload に凍結された (行, occurrence, 世代) をそのまま運び、発火時の
        世代照合・行同一性照合・claim の prepared 再利用が安全性を担保する。

        同じ EventScheduler key を使うため、reconciliation が先に積んだ「次回」
        の予約は上書きされるが、この occurrence の精算後の `_do_register` が
        次回を積み直すので収束する。
        """
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            return
        from saiverse import clock

        scheduler.schedule(
            fire_at=clock.now(),
            # attempt を運ぶ (Codex W3 第十陣): 既定 0 で再開すると failed 回収の
            # refire が試行回数をリセットし、crash 窓の繰り返しで backoff 上限が
            # 実質無制限になる。回収側が「失敗した試行 + 1」を渡す。
            callback=lambda sid=schedule_id, tok=instance_token,
                occ=occurrence_token, gen=generation, att=attempt:
                self._handle_fire(sid, tok, occ, gen, att),
            key=_schedule_key(schedule_id),
        )
        self._registered[schedule_id] = (instance_token, generation)
        LOGGER.info(
            "[ScheduleManager] refire scheduled for recovered occurrence "
            "(schedule=%d instance=%s occurrence=%s generation=%d attempt=%d)",
            schedule_id, instance_token, occurrence_token, generation, attempt,
        )

    def unregister_schedule(self, schedule_id: int) -> None:
        """1 件の予約をキャンセル。削除・トグル OFF 時に API 層から呼ばれる。"""
        self._do_cancel(schedule_id)

    # ------------------------------------------------------------------
    # Reconciliation: 回復 tick からの世代照合ループ (W3 Chunk C / handoff D6)
    # ------------------------------------------------------------------

    def _reconcile_schedules(self) -> Dict[str, int]:
        """DB (宣言的正典) と EventScheduler 予約の同期を照合・修復する (A12 の核)。

        回復 tick (:mod:`saiverse.execution_ledger_wiring` の 60 秒周期) から
        呼ばれる。LLM を直接起動しない — やるのは予約の登録・除去だけで、
        register/unregister の失敗 (CRUD 側の握り潰し・callback 例外による
        予約 drop) を再起動なしに自己回復させる。

        - **復元**: ENABLED 全件について「予約が無い」または「登録世代 ≠ DB
          世代」なら再登録候補。ただし計算した次回 occurrence が台帳で
          ブロックされている (:data:`_OCCURRENCE_BLOCKING_STATUSES`) 間は
          登録しない — 実行中への二重登録と unknown 裁定待ち oneshot の
          自動再実行をここで塞ぐ。台帳の無い manager (テストスタブ) では
          確認を飛ばして登録する (発火側の degrade と同じ流儀)。
        - **除去**: 登録 map にあるが DB に無い / disabled の予約は cancel
          (delete / disable 時の unregister 失敗の回復)。
        - 手動モード persona も特別扱いしない (handoff D6-3): 予約の復元は
          宣言的正典の同期であって発火ではない。発火時ゲートは W9 の所掌。

        Returns:
            ``{"registered": int, "cancelled": int}`` (ログ用の集計)。
        """
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            return {"registered": 0, "cancelled": 0}
        ledger = getattr(self.manager, "execution_ledger", None)

        registered = 0
        cancelled = 0
        session = self.manager.SessionLocal()
        try:
            schedules = (
                session.query(PersonaSchedule)
                .filter(PersonaSchedule.ENABLED == True)  # noqa: E712
                .all()
            )
            enabled_ids = set()
            for schedule in schedules:
                schedule_id = schedule.SCHEDULE_ID
                enabled_ids.add(schedule_id)
                try:
                    current_generation = schedule.SYNC_GENERATION or 0
                    if (
                        scheduler.has_key(_schedule_key(schedule_id))
                        and self._registered.get(schedule_id)
                        == (_instance_token(schedule), current_generation)
                    ):
                        continue  # 予約あり + 行・世代一致 = 同期済み

                    next_fire = self._compute_next_fire_at(schedule, session)
                    if next_fire is None:
                        # 完了済み oneshot / 設定不備 — 登録すべき予約が無い。
                        # 更新 (periodic → 日時未指定 oneshot 等) 後に register
                        # 側が失敗すると旧時刻の予約が heap に残る — DB 正典に
                        # 無い予約なのでここで回収する (2026-07-20 Codex W3
                        # 第二陣 P2)。cancel は予約が無ければ False を返すだけ
                        # なので無条件でよい — 有った場合のみ集計する。
                        if scheduler.cancel(_schedule_key(schedule_id)):
                            cancelled += 1
                            LOGGER.info(
                                "[ScheduleManager] reconcile: cancelled stale "
                                "reservation for schedule %d (no next occurrence "
                                "computable)",
                                schedule_id,
                            )
                        self._registered.pop(schedule_id, None)
                        continue

                    if ledger is not None:
                        key = _occurrence_key(
                            schedule_id,
                            _instance_token(schedule),
                            current_generation,
                            _occurrence_token(schedule, next_fire),
                        )
                        row = ledger.find_execution(
                            SCHEDULE_DISPATCH_LEDGER_KIND, key
                        )
                        if (
                            row is not None
                            and row.get("status") in _OCCURRENCE_BLOCKING_STATUSES
                        ):
                            LOGGER.debug(
                                "[ScheduleManager] reconcile: schedule %d occurrence "
                                "%s is blocked in ledger (status=%s); not registering",
                                schedule_id, key, row.get("status"),
                            )
                            continue

                    if self._do_register(schedule, session):
                        registered += 1
                        LOGGER.info(
                            "[ScheduleManager] reconcile: re-registered schedule %d "
                            "(generation=%d)",
                            schedule_id, current_generation,
                        )
                except Exception:
                    LOGGER.exception(
                        "[ScheduleManager] reconcile failed for schedule %d",
                        schedule_id,
                    )

            # 除去: 登録 map にあるが DB に無い / disabled (delete・disable の
            # unregister 失敗の回復)
            for schedule_id in list(self._registered):
                if schedule_id in enabled_ids:
                    continue
                try:
                    scheduler.cancel(_schedule_key(schedule_id))
                    self._registered.pop(schedule_id, None)
                    cancelled += 1
                    LOGGER.info(
                        "[ScheduleManager] reconcile: cancelled stale reservation "
                        "for schedule %d (deleted/disabled)",
                        schedule_id,
                    )
                except Exception:
                    LOGGER.exception(
                        "[ScheduleManager] reconcile cancel failed for schedule %d",
                        schedule_id,
                    )
        finally:
            session.close()
        return {"registered": registered, "cancelled": cancelled}

    # ------------------------------------------------------------------
    # 内部: register / cancel
    # ------------------------------------------------------------------

    def _do_register(self, schedule: PersonaSchedule, session) -> bool:
        """schedule オブジェクトを EventScheduler に push する。

        ENABLED でない / 次回時刻計算不能 の場合は cancel のみ行って False。
        """
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            LOGGER.warning(
                "[ScheduleManager] event_scheduler not available; cannot register schedule %d",
                schedule.SCHEDULE_ID,
            )
            return False

        if not schedule.ENABLED:
            self._do_cancel(schedule.SCHEDULE_ID)
            return False

        next_fire = self._compute_next_fire_at(schedule, session)
        if next_fire is None:
            LOGGER.debug(
                "[ScheduleManager] Schedule %d has no next fire time (completed / invalid); cancelling",
                schedule.SCHEDULE_ID,
            )
            self._do_cancel(schedule.SCHEDULE_ID)
            return False

        schedule_id = schedule.SCHEDULE_ID
        # closure には (schedule_id, instance_token, occurrence_token, generation)
        # を焼き込む (W3 D2 + Codex 第三陣)。schedule オブジェクト自体を閉じ込める
        # と ORM session の寿命とずれるため、発火時は DB から最新状態を読み直す。
        # instance_token はこの登録が狙う「行そのもの」の識別子 (SCHEDULE_ID
        # 再利用との分離)、occurrence_token はこの登録が狙う occurrence の識別子
        # (冪等キーの素材、初回 interval は世代付き sentinel "first@g{N}")、generation は
        # 登録時の SYNC_GENERATION (旧世代予約の発火を無害化する照合トークン)。
        instance_token = _instance_token(schedule)
        occurrence_token = _occurrence_token(schedule, next_fire)
        generation = schedule.SYNC_GENERATION or 0
        scheduler.schedule(
            fire_at=next_fire,
            callback=lambda sid=schedule_id, tok=instance_token,
                occ=occurrence_token, gen=generation:
                self._handle_fire(sid, tok, occ, gen),
            key=_schedule_key(schedule_id),
        )
        self._registered[schedule_id] = (instance_token, generation)
        LOGGER.debug(
            "[ScheduleManager] Registered schedule %d (type=%s, persona=%s, fire_at=%s, "
            "instance=%s, occurrence=%s, generation=%d)",
            schedule_id, schedule.SCHEDULE_TYPE, schedule.PERSONA_ID,
            next_fire.isoformat(), instance_token, occurrence_token, generation,
        )
        return True

    def _do_cancel(self, schedule_id: int) -> None:
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            return
        scheduler.cancel(_schedule_key(schedule_id))
        self._registered.pop(schedule_id, None)

    # ------------------------------------------------------------------
    # 次回発火時刻の計算 (UTC で返す)
    # ------------------------------------------------------------------

    def _compute_next_fire_at(self, schedule: PersonaSchedule, session) -> Optional[datetime]:
        """schedule の次回発火時刻 (naive local datetime) を計算する。

        EventScheduler は naive datetime (ローカルタイム) を受けるので、
        計算は UTC で行うが返り値は ``datetime.now()`` と同じ naive ローカル時刻に
        変換する。
        """
        now_utc = datetime.now(timezone.utc)
        schedule_type = schedule.SCHEDULE_TYPE

        if schedule_type == "periodic":
            tz = self._get_persona_timezone(schedule.PERSONA_ID, session)
            return self._next_periodic_fire(schedule, now_utc, tz)
        elif schedule_type == "oneshot":
            return self._next_oneshot_fire(schedule, now_utc)
        elif schedule_type == "interval":
            return self._next_interval_fire(schedule, now_utc)
        else:
            LOGGER.warning("[ScheduleManager] Unknown schedule type: %s", schedule_type)
            return None

    def _next_periodic_fire(
        self, schedule: PersonaSchedule, now_utc: datetime, tz: ZoneInfo
    ) -> Optional[datetime]:
        """定期スケジュール (曜日 + 時刻指定) の次回発火時刻。

        ペルソナのタイムゾーンで「指定曜日リスト + TIME_OF_DAY」の
        最も近い未来の発火時刻を計算する。
        """
        if not schedule.TIME_OF_DAY:
            return None

        try:
            hour_str, minute_str = schedule.TIME_OF_DAY.split(":")
            target_hour = int(hour_str)
            target_minute = int(minute_str)
        except (ValueError, AttributeError):
            LOGGER.warning(
                "[ScheduleManager] Invalid TIME_OF_DAY for schedule %d: %r",
                schedule.SCHEDULE_ID, schedule.TIME_OF_DAY,
            )
            return None

        if schedule.DAYS_OF_WEEK:
            try:
                allowed_days = set(json.loads(schedule.DAYS_OF_WEEK))
            except Exception:
                LOGGER.warning(
                    "[ScheduleManager] Failed to parse DAYS_OF_WEEK for schedule %d",
                    schedule.SCHEDULE_ID, exc_info=True,
                )
                return None
            if not allowed_days:
                return None
        else:
            allowed_days = set(range(7))  # 曜日指定なし = 毎日

        local_now = now_utc.astimezone(tz)
        # 今日の発火候補
        today_candidate = local_now.replace(
            hour=target_hour, minute=target_minute, second=0, microsecond=0
        )

        # 今日が許可曜日 + 候補時刻が未来 → 今日
        if local_now.weekday() in allowed_days and today_candidate > local_now:
            return self._to_naive_local(today_candidate)

        # それ以外 → 1〜7 日先で最初に見つかる許可曜日
        for delta in range(1, 8):
            candidate = today_candidate + timedelta(days=delta)
            if candidate.weekday() in allowed_days:
                return self._to_naive_local(candidate)

        return None

    def _next_oneshot_fire(
        self, schedule: PersonaSchedule, now_utc: datetime
    ) -> Optional[datetime]:
        if schedule.COMPLETED:
            return None
        if not schedule.SCHEDULED_DATETIME:
            return None

        scheduled = schedule.SCHEDULED_DATETIME
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        # 過去時刻でも push (EventScheduler が即発火する)
        return self._to_naive_local(scheduled)

    def _next_interval_fire(
        self, schedule: PersonaSchedule, now_utc: datetime
    ) -> Optional[datetime]:
        if not schedule.INTERVAL_SECONDS or schedule.INTERVAL_SECONDS <= 0:
            return None
        last = schedule.LAST_EXECUTED_AT
        if last is None:
            return self._to_naive_local(now_utc)  # 初回即実行
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return self._to_naive_local(last + timedelta(seconds=schedule.INTERVAL_SECONDS))

    @staticmethod
    def _to_naive_local(dt: datetime) -> datetime:
        """UTC-aware datetime をシステムローカルの naive datetime に変換する。

        EventScheduler は ``fire_at.timestamp()`` で扱うので、aware/naive どちらでも
        OK だが、内部で扱うフォーマットを naive local に揃える (datetime.now() と
        引き算したい場面用)。
        """
        if dt.tzinfo is None:
            return dt
        return dt.astimezone().replace(tzinfo=None)

    def _get_persona_timezone(self, persona_id: str, session) -> ZoneInfo:
        try:
            persona_model = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
            if not persona_model:
                return ZoneInfo("UTC")
            city_model = session.query(CityModel).filter(
                CityModel.CITYID == persona_model.HOME_CITYID
            ).first()
            if not city_model or not city_model.TIMEZONE:
                return ZoneInfo("UTC")
            return ZoneInfo(city_model.TIMEZONE)
        except Exception:
            LOGGER.warning(
                "[ScheduleManager] Failed to get timezone for persona %s",
                persona_id, exc_info=True,
            )
            return ZoneInfo("UTC")

    # ------------------------------------------------------------------
    # 発火: callback 本体 (EventScheduler から呼ばれる)
    # ------------------------------------------------------------------

    def _handle_fire(
        self,
        schedule_id: int,
        instance_token: str,
        occurrence_token: str,
        generation: int,
        attempt: int = 0,
    ) -> None:
        """EventScheduler から呼ばれる発火 callback (W3 D3: 台帳化)。

        closure が運ぶのは (schedule_id, instance_token, occurrence_token,
        generation, attempt) だけで、発火時に最新の DB 状態を読み直して実行する。
        全体を try/except で包む — EventScheduler の「callback 例外 = WARN + 予約
        drop」に台帳化後の例外を渡すと、schedule の再登録経路ごと消えるため。
        """
        try:
            self._handle_fire_inner(
                schedule_id, instance_token, occurrence_token, generation, attempt,
            )
        except Exception:
            LOGGER.exception(
                "[ScheduleManager] _handle_fire failed (schedule=%d instance=%s "
                "occurrence=%s generation=%d attempt=%d)",
                schedule_id, instance_token, occurrence_token, generation, attempt,
            )

    def _handle_fire_inner(
        self,
        schedule_id: int,
        instance_token: str,
        occurrence_token: str,
        generation: int,
        attempt: int,
    ) -> None:
        session = self.manager.SessionLocal()
        try:
            schedule = session.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id
            ).first()
            if schedule is None:
                LOGGER.warning(
                    "[ScheduleManager] _handle_fire: schedule %d not found (deleted?)",
                    schedule_id,
                )
                self._registered.pop(schedule_id, None)
                return

            if not schedule.ENABLED:
                LOGGER.debug(
                    "[ScheduleManager] _handle_fire: schedule %d is disabled, skipping",
                    schedule_id,
                )
                self._registered.pop(schedule_id, None)
                return

            # --- 行同一性照合 (W3 Codex 第三陣): SCHEDULE_ID 再利用の検出 ---
            # closure が狙った行が削除され、同じ SCHEDULE_ID で別の行が作られた
            # 場合 (SQLite の INTEGER PK 再利用)、この予約は死んだ行のもの。
            # 実行せず、現在の行 (新しいトークン) で再登録する。
            current_token = _instance_token(schedule)
            if current_token != instance_token:
                LOGGER.info(
                    "[ScheduleManager] _handle_fire: reservation targets a dead row "
                    "for schedule id %d (reserved instance=%s, current=%s); "
                    "re-registering from DB without executing",
                    schedule_id, instance_token, current_token,
                )
                self._do_register(schedule, session)
                return

            # --- 世代照合 (W3 D2/A12 回帰②): 旧世代予約は実行しない ---
            current_generation = schedule.SYNC_GENERATION or 0
            if current_generation != generation:
                LOGGER.info(
                    "[ScheduleManager] _handle_fire: stale reservation for schedule %d "
                    "(reserved generation=%d, current=%d); re-registering from DB "
                    "without executing",
                    schedule_id, generation, current_generation,
                )
                self._do_register(schedule, session)
                return

            # --- 台帳 claim (W3 D1/D3): 同一 occurrence の二重発火を dedup ---
            ledger = getattr(self.manager, "execution_ledger", None)
            exec_id: Optional[str] = None
            if ledger is None:
                if not self._ledger_missing_warned:
                    self._ledger_missing_warned = True
                    LOGGER.warning(
                        "[ScheduleManager] manager has no execution_ledger; schedule "
                        "fires run without ledger tracking (dedup/backoff degrade)",
                    )
            else:
                key = _occurrence_key(
                    schedule_id, instance_token, generation, occurrence_token
                )
                exec_id, runnable, existing_status = ledger.claim_execution(
                    SCHEDULE_DISPATCH_LEDGER_KIND,
                    key,
                    persona_id=schedule.PERSONA_ID,
                    payload={
                        "schedule_id": schedule_id,
                        "persona_id": schedule.PERSONA_ID,
                        "schedule_type": schedule.SCHEDULE_TYPE,
                        "instance_token": instance_token,
                        "occurrence": occurrence_token,
                        "generation": generation,
                        "meta_playbook": schedule.META_PLAYBOOK,
                        # 何回目の試行か (Codex W3 第九陣): failed 回収が
                        # 「上限到達の意図的放棄」と「crash で retry を失った」を
                        # 区別するための永続証跡 (予約の有無では再起動後に区別
                        # できない — start() が翌回を先に登録するため)
                        "attempt": attempt,
                    },
                )
                if not runnable:
                    # 二重発火 dedup。hot loop 防止: 次 occurrence がこの blocked
                    # key と同一 (oneshot 未完了 / interval 未前進 / unknown 裁定
                    # 待ち) なら再登録しない — 再登録すると即発火 → claim 却下 →
                    # 再登録の空回りになる。勝者側の settle が次を register する。
                    next_fire = self._compute_next_fire_at(schedule, session)
                    if next_fire is not None and _occurrence_token(schedule, next_fire) != occurrence_token:
                        LOGGER.info(
                            "[ScheduleManager] _handle_fire: occurrence %s already "
                            "claimed (status=%s); registering next occurrence",
                            key, existing_status,
                        )
                        self._do_register(schedule, session)
                    else:
                        LOGGER.info(
                            "[ScheduleManager] _handle_fire: occurrence %s already "
                            "claimed (status=%s); not re-registering (same occurrence "
                            "would hot-loop)",
                            key, existing_status,
                        )
                    return

                # --- 席取り: prepared→running の CAS。敗者は台帳に書かず離脱 ---
                if not ledger.try_mark_running(exec_id):
                    LOGGER.info(
                        "[ScheduleManager] _handle_fire: lost the running seat for "
                        "occurrence %s; leaving without ledger writes", key,
                    )
                    return

            # --- 実行 (型付き outcome、W3 D4) ---
            outcome_class, detail = self._execute_schedule(schedule, session)

            # --- 精算 (W3 D3) ---
            if outcome_class in ("executed", "accepted", "settled_skip"):
                # 単一 tx: schedule 状態前進 + mark_applied を同じ session で行い
                # commit は 1 回だけ (全 or 無 — 前進だけ残って台帳が残らない、
                # あるいはその逆の分裂を作らない)。
                # 状態前進は (行, 世代) 条件付き UPDATE (Codex W3 第七陣 —
                # LLM 実行中にユーザーが行を更新していたら、旧発火の精算が
                # 新世代の COMPLETED / LAST_EXECUTED_AT を書き換えてはならない。
                # 実行そのものは起きたので台帳 applied は記録し、result に
                # superseded を残す)。
                advanced = self._update_schedule_after_execution(
                    schedule, session, instance_token, generation,
                )
                if ledger is not None:
                    ledger.mark_applied(
                        exec_id,
                        session=session,
                        result={
                            "kind": SCHEDULE_DISPATCH_LEDGER_KIND,
                            "action": outcome_class,
                            "reason": detail,
                            "schedule_type": schedule.SCHEDULE_TYPE,
                            "superseded_during_run": not advanced,
                        },
                    )
                session.commit()
                if ledger is not None:
                    # outbox を持たないので即 completed (配送待ちなし)
                    ledger.mark_completed(exec_id)
                if not advanced:
                    LOGGER.info(
                        "[ScheduleManager] schedule %d was reconfigured during "
                        "the run (occurrence %s); state not advanced — "
                        "re-registering per current config",
                        schedule_id, occurrence_token,
                    )
                # 次回 register (oneshot 完了で None → cancel / interval 継続 /
                # periodic 次回)。commit 済みなので ORM の期限切れ再読込が
                # 最新行 (実行中の再設定を含む) を反映する。
                self._do_register(schedule, session)
            elif outcome_class in ("failed", "waiting"):
                # どちらも副作用ゼロ確定 — schedule 状態は前進させず backoff
                # 再試行 (D5)。waiting (勝者の judgment が実行中 / 席が
                # indeterminate) は実処理の失敗ではないので attempt を消費しない
                # — 通常の上限で待機を打ち切ると、勝者が長時間 running のまま
                # attempt が尽き、勝者 failed 後の当日 occurrence を失う
                # (Codex 三巡目 high2)。待機の終端は台帳の running 期限監視
                # (1 時間で unknown 化 → duplicate:unknown → unknown 終端)。
                if ledger is not None:
                    # waiting は ERROR に接頭辞を刻む — crash 後の failed 回収が
                    # attempt を据え置いたまま refire できるように (durable な印)
                    ledger.mark_failed(
                        exec_id,
                        detail if outcome_class == "failed"
                        else f"{SCHEDULE_DISPATCH_WAITING_ERROR_PREFIX}{detail}",
                    )
                self._retry_or_give_up(
                    schedule, session, instance_token, occurrence_token,
                    generation, attempt, detail,
                    consume_attempt=(outcome_class == "failed"),
                )
            else:  # unknown
                # LLM が動いたか不明 — 前進なし・再予約なし。occurrence は
                # unknown claim がブロックする (自動再実行禁止、intent §2.5)。
                if ledger is not None:
                    ledger.mark_unknown(exec_id, detail)
                LOGGER.warning(
                    "[ScheduleManager] schedule %d occurrence %s ended unknown (%s); "
                    "no automatic re-execution (intent §2.5)",
                    schedule_id, occurrence_token, detail,
                )
        finally:
            session.close()

    def _retry_or_give_up(
        self,
        schedule: PersonaSchedule,
        session,
        instance_token: str,
        occurrence_token: str,
        generation: int,
        attempt: int,
        detail: str,
        *,
        consume_attempt: bool = True,
    ) -> None:
        """failed / waiting (いずれも副作用ゼロ) の backoff 再試行 (W3 D5)。

        attempt+1 が上限内なら同一 occurrence closure を backoff 後に再予約する
        (claim が failed キーを退避するので再実行は安全)。尽きたら periodic は
        次 occurrence (翌日) を登録し、oneshot / interval は登録せず
        reconciliation の周期 (Chunk C、60 秒) に委ねる — 恒久故障は cadence が
        落ちて継続する (handoff「引き受ける歪み①」)。

        Args:
            consume_attempt: False = waiting (勝者の judgment の終端待ち)。
                実処理の失敗ではないので attempt を据え置く — 上限は失敗の
                打ち切り用で、待機に適用すると勝者が長い running のあいだに
                occurrence を放棄してしまう。待機ループの終端は台帳の running
                期限監視 (1 時間で unknown 化) が保証する。
        """
        from saiverse import clock

        schedule_id = schedule.SCHEDULE_ID
        scheduler = getattr(self.manager, "event_scheduler", None)
        next_attempt = attempt + 1 if consume_attempt else attempt
        if next_attempt <= SCHEDULE_DISPATCH_MAX_ATTEMPTS and scheduler is not None:
            retry_at = clock.now() + timedelta(
                seconds=SCHEDULE_DISPATCH_RETRY_BACKOFF_SECONDS
            )
            scheduler.schedule(
                fire_at=retry_at,
                callback=lambda sid=schedule_id, tok=instance_token,
                    occ=occurrence_token, gen=generation, att=next_attempt:
                    self._handle_fire(sid, tok, occ, gen, att),
                key=_schedule_key(schedule_id),
            )
            self._registered[schedule_id] = (instance_token, generation)
            LOGGER.warning(
                "[ScheduleManager] schedule %d dispatch failed (%s); retrying "
                "occurrence %s at %s (attempt %d/%d)",
                schedule_id, detail, occurrence_token,
                retry_at.isoformat(timespec="seconds"),
                next_attempt, SCHEDULE_DISPATCH_MAX_ATTEMPTS,
            )
            return

        LOGGER.error(
            "[ScheduleManager] schedule %d dispatch failed (%s); retry attempts "
            "exhausted for occurrence %s (attempt=%d, max=%d)",
            schedule_id, detail, occurrence_token, attempt,
            SCHEDULE_DISPATCH_MAX_ATTEMPTS,
        )
        if schedule.SCHEDULE_TYPE == "periodic":
            # periodic はこの occurrence を諦め、次回 (翌日) を登録する
            self._do_register(schedule, session)
        # oneshot / interval は登録しない — reconciliation (Chunk C) が 60 秒
        # 周期で拾い、cadence を落として再試行を続ける

    # ------------------------------------------------------------------
    # スケジュール実行 (旧実装からほぼそのまま引き継ぎ)
    # ------------------------------------------------------------------

    def _generate_schedule_prompt(self, schedule: PersonaSchedule, session, persona_id: str) -> str:
        """スケジュール実行時のプロンプトを生成

        「現在の日時」はペルソナに見える時刻のため、仮想クロック (一日
        シミュレータ) 有効時は仮想時刻を使う。実モードでは従来どおり
        persona timezone の実時刻 (挙動不変)。
        """
        from saiverse import clock

        persona_tz = self._get_persona_timezone(persona_id, session)
        if clock.is_virtual():
            local_now = clock.now()  # naive ローカル (シナリオの仮想時刻)
        else:
            local_now = datetime.now(timezone.utc).astimezone(persona_tz)

        scheduled_time_str = ""
        if schedule.SCHEDULE_TYPE == "periodic":
            days_str = "毎日"
            if schedule.DAYS_OF_WEEK:
                try:
                    day_list = json.loads(schedule.DAYS_OF_WEEK)
                    day_names = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
                    days_str = ", ".join([day_names[d] for d in day_list if 0 <= d < 7])
                except Exception:
                    LOGGER.warning(
                        "Failed to parse DAYS_OF_WEEK for schedule prompt (schedule %d)",
                        schedule.SCHEDULE_ID, exc_info=True,
                    )
            scheduled_time_str = f"{days_str} {schedule.TIME_OF_DAY or '??:??'}"
        elif schedule.SCHEDULE_TYPE == "oneshot":
            if schedule.SCHEDULED_DATETIME:
                dt_utc = schedule.SCHEDULED_DATETIME
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone(persona_tz)
                scheduled_time_str = dt_local.strftime("%Y年%m月%d日 %H:%M")
        elif schedule.SCHEDULE_TYPE == "interval":
            interval_sec = schedule.INTERVAL_SECONDS or 0
            if interval_sec >= 3600:
                hours = interval_sec // 3600
                scheduled_time_str = f"{hours}時間ごと"
            elif interval_sec >= 60:
                minutes = interval_sec // 60
                scheduled_time_str = f"{minutes}分ごと"
            else:
                scheduled_time_str = f"{interval_sec}秒ごと"

        prompt = f"""<system>
スケジュールの実行時刻です。

現在の日時: {local_now.strftime("%Y年%m月%d日 %H:%M")} ({persona_tz})
スケジュールタイプ: {schedule.SCHEDULE_TYPE}
スケジュール設定: {scheduled_time_str}
スケジュールの説明: {schedule.DESCRIPTION or "（説明なし）"}
</system>"""
        return prompt

    # 判断点経路の submitted=False reason のうち「意図して実行しない」と裁定済みの
    # ゲート系 (handoff D4)。settled_skip = occurrence を前進させ、再試行しない。
    # 判断点の実体回復は W1 の judgment.* 台帳 + watchdog が持つ。
    _JUDGMENT_GATE_REASONS = frozenset({
        "precondition not met",
        "persona autonomy disabled",
        "playbook not imported",
        "not a judgment playbook",
        "kind not schedulable",
    })

    @classmethod
    def _classify_judgment_outcome(cls, result: Any) -> Tuple[str, str]:
        """判断点経路 (handle_scheduled_judgment) の戻り dict → 型付き outcome。"""
        if not isinstance(result, dict):
            return "failed", f"judgment returned non-dict: {result!r}"
        if result.get("submitted"):
            return "executed", "submitted"
        reason = str(result.get("reason") or "")
        if reason.startswith("duplicate:"):
            # 同じ判断キーの既存実行の状態ごとに精算を分ける
            # (docs/issues/judgment_seat_contention_and_event_loss.md ①②)。
            # backoff 再試行の claim 自体が「勝者の終端の照合」になっている:
            # applied/completed → 判断は済んだ = 前進 / failed → claim がキーを
            # 退避して再実行 (duplicate としてここへは来ない) / running →
            # まだ終わっていない — settled_skip で前進させると、勝者が後で
            # failed になったとき当営業日分が回収不能になるため、waiting
            # (attempt を消費しない backoff 再試行) で勝者の終端を待つ。
            # 待機の終端は台帳の running 期限監視 (回復 tick が 1 時間で
            # unknown 化) が保証する — running は永続しない。
            status = reason.split(":", 1)[1]
            if status == "running":
                return "waiting", reason
            if status == "unknown":
                # LLM が動いたか不明 — 自動再実行禁止 (intent §2.5)
                return "unknown", reason
            return "settled_skip", reason
        # indeterminate = 判断の席を放棄できなかった (別 claimant が同じ判断を
        # LLM 実行中 / 台帳が応答しない)。この dispatch 自身は副作用ゼロだが、
        # 実処理の失敗ではないので waiting — 再試行の claim が上の duplicate
        # 分類で勝者の終端を照合する。settled_skip (前進) にしてはならない。
        from saiverse.judgment_points import OUTCOME_INDETERMINATE, OUTCOME_RAN

        if result.get("outcome") == OUTCOME_INDETERMINATE:
            return "waiting", reason or "judgment seat indeterminate"
        if result.get("outcome") == OUTCOME_RAN:
            # メタレーンが走った後、成功の証跡なく戻った = LLM が動いたか不明。
            # 自動再実行禁止 (intent §2.5) — failed (再試行安全) に落とさない。
            return "unknown", reason or "judgment ran without success evidence"
        if (
            reason in cls._JUDGMENT_GATE_REASONS
            or reason.startswith("conversation had no exchange")
        ):
            return "settled_skip", reason
        # "precondition raised" / "resume execution not found" /
        # "resume target not prepared" と未知の reason は保守的に failed
        # (副作用ゼロで戻る経路のみ — 再試行安全)
        return "failed", reason or "judgment not submitted (no reason)"

    @staticmethod
    def _classify_dispatch_outcome(result: Any) -> Tuple[str, str]:
        """汎用経路 (dispatch_schedule_fire) の型付き戻り dict → 型付き outcome。

        schedule は on_blocked="wait" — queued / cancelled は queue (復帰 queue)
        に残っていて消えないため accepted (前進) とする (handoff D4)。
        """
        if not isinstance(result, dict):
            return "unknown", f"dispatch returned non-dict: {result!r}"
        action = result.get("action")
        runtime_outcome = result.get("runtime_outcome")
        error = result.get("error")
        detail = f"action={action} outcome={runtime_outcome}"
        if error:
            detail += f" error={error}"
        if action == "execute" and runtime_outcome == "completed":
            return "executed", detail
        if action == "queued" or runtime_outcome == "cancelled":
            return "accepted", detail
        if runtime_outcome == "gate_closed":
            # Beat 関所閉鎖 = 副作用ゼロ確定 → 再試行安全
            return "failed", "beat gate closed"
        if runtime_outcome == "floor_unmet":
            # 最終防衛ライン未達 (arasuji_levels.md §15-5) = Playbook 未起動・
            # 副作用ゼロ確定 → 再試行安全。occurrence は消費しない。
            return "failed", "window floor unmet"
        if action in ("unavailable", "error_before_submit", "skipped"):
            # 受付裁定前 / 受付されず — LLM は動いていない → 再試行安全
            return "failed", detail
        if runtime_outcome == "error":
            # 実行中の例外 (submit 例外含む) — LLM が動いたか不明
            return "unknown", detail
        return "unknown", detail

    def _execute_schedule(self, schedule: PersonaSchedule, session) -> Tuple[str, str]:
        """スケジュールを実行し、型付き outcome を返す (W3 D4)。

        META_PLAYBOOK が自律行動 v2 の判断点 Playbook (judgment_day_open /
        judgment_day_close) の場合は専用経路 —
        ``saiverse.autonomy_wiring.handle_scheduled_judgment`` — を通す。
        判断点は発火時に judgment_points が組む動的 args (situation_text /
        response_schema) が必須で、通常の「<system> プロンプト + Playbook」の
        submit_schedule では起動できないため。起床・就寝時刻の出所はこの
        PersonaSchedule 行そのもの (スケジュール未設定のペルソナは発火しない)。

        Returns:
            ``(outcome_class, detail)``。outcome_class は
            "executed" (実行完走) / "accepted" (queue 受付済みで消えない) /
            "settled_skip" (実行しないと裁定済み) /
            "failed" (副作用ゼロ確定 — 再試行安全) /
            "waiting" (副作用ゼロだが勝者の judgment の終端待ち — attempt を
            消費しない backoff 再試行) /
            "unknown" (LLM が動いたか不明 — 自動再実行禁止)。
        """
        persona_id = schedule.PERSONA_ID
        meta_playbook = (schedule.META_PLAYBOOK or "").strip()
        if not meta_playbook:
            # 発火の場所で一枚に守る。作る側の入口 (REST / schedule_add スペル /
            # ライフ設定) は既定値へ正規化するよう揃えたが、それ以前に作られた行と、
            # 将来増える入口の取りこぼしがここへ届く。空のまま進むと Playbook を
            # 引けずに落ちる = 鳴らないアラームになるので、既定の Playbook で鳴らす。
            # WARNING を出すのは、空値を書いている入口が残っているなら見つけたいため。
            LOGGER.warning(
                "[ScheduleManager] schedule %d (persona=%s) has an empty "
                "META_PLAYBOOK; falling back to %s. Some creation path is "
                "writing blank playbook names — find it.",
                schedule.SCHEDULE_ID, persona_id, DEFAULT_META_PLAYBOOK,
            )
            meta_playbook = DEFAULT_META_PLAYBOOK

        schedule_args: Optional[Dict[str, Any]] = None
        pre_spells: Optional[List[str]] = None
        parsed_params: Optional[Dict[str, Any]] = None
        if schedule.PLAYBOOK_PARAMS:
            try:
                parsed_params = json.loads(schedule.PLAYBOOK_PARAMS)
            except Exception as e:
                LOGGER.warning(
                    "[ScheduleManager] Failed to parse PLAYBOOK_PARAMS for schedule %d: %s",
                    schedule.SCHEDULE_ID, e,
                )
                parsed_params = None
            if isinstance(parsed_params, dict):
                raw_pre_spells = parsed_params.get("pre_spells")
                if isinstance(raw_pre_spells, list):
                    pre_spells = [s for s in raw_pre_spells if isinstance(s, str) and s.strip()]
                schedule_args = {k: v for k, v in parsed_params.items() if k != "pre_spells"} or None

        # 自律行動 v2: 判断点スケジュール (起床 / 就寝) の専用経路
        from saiverse.autonomy_wiring import (
            JUDGMENT_PLAYBOOK_NAMES,
            handle_scheduled_judgment,
        )
        if meta_playbook in JUDGMENT_PLAYBOOK_NAMES:
            LOGGER.info(
                "[ScheduleManager] Executing judgment schedule %d for persona %s "
                "(playbook=%s)",
                schedule.SCHEDULE_ID, persona_id, meta_playbook,
            )
            try:
                result = handle_scheduled_judgment(
                    self.manager, persona_id, meta_playbook,
                    params=parsed_params if isinstance(parsed_params, dict) else None,
                )
            except Exception as e:
                # fire_judgment_point は例外を戻り dict に落とすので、ここに届く
                # 例外は submit 前の経路 (変換・ゲート) — 副作用ゼロで再試行安全
                LOGGER.exception(
                    "[ScheduleManager] Judgment schedule %d raised", schedule.SCHEDULE_ID,
                )
                return "failed", f"judgment raised: {e or type(e).__name__}"
            return self._classify_judgment_outcome(result)

        LOGGER.info(
            "[ScheduleManager] Executing schedule %d for persona %s (type=%s, playbook=%s)",
            schedule.SCHEDULE_ID, persona_id, schedule.SCHEDULE_TYPE, meta_playbook,
        )

        persona = self.manager.all_personas.get(persona_id)
        if not persona:
            LOGGER.warning("[ScheduleManager] Persona %s not found in all_personas", persona_id)
            return "failed", "persona not found"

        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            LOGGER.warning("[ScheduleManager] Persona %s has no current_building_id", persona_id)
            return "failed", "persona has no current_building_id"

        user_input = self._generate_schedule_prompt(schedule, session, persona_id)

        try:
            # pulse_dispatch.md §7: PulseDispatcher 経由で起動。
            # dispatch_schedule_fire は例外を再送出しない型付き dict を返す
            # (W3 Chunk A)。ここで例外が出るのは submit 前の自前処理のみ
            # (副作用ゼロ) → failed。
            result = self.manager.pulse_dispatcher.dispatch_schedule_fire(
                persona_id=persona_id,
                building_id=building_id,
                user_input=user_input,
                metadata={"schedule_id": schedule.SCHEDULE_ID, "schedule_type": schedule.SCHEDULE_TYPE},
                meta_playbook=meta_playbook,
                args=schedule_args,
                pre_spells=pre_spells,
            )
        except Exception as e:
            LOGGER.exception(
                "[ScheduleManager] Failed to execute schedule %d", schedule.SCHEDULE_ID,
            )
            return "failed", f"dispatch raised before submit: {e or type(e).__name__}"

        outcome_class, detail = self._classify_dispatch_outcome(result)
        LOGGER.info(
            "[ScheduleManager] Schedule %d dispatched via PulseDispatcher (%s: %s)",
            schedule.SCHEDULE_ID, outcome_class, detail,
        )

        if outcome_class == "executed":
            # 実行完走時のみの後処理 (従来挙動の維持)。失敗しても outcome の
            # 分類は覆さない (dispatch の顛末が真実)。
            try:
                self.manager._save_modified_buildings()
                persona._save_session_metadata()
            except Exception:
                LOGGER.warning(
                    "[ScheduleManager] post-execution save failed for schedule %d",
                    schedule.SCHEDULE_ID, exc_info=True,
                )
        return outcome_class, detail

    def _update_schedule_after_execution(
        self,
        schedule: PersonaSchedule,
        session,
        instance_token: str,
        generation: int,
    ) -> bool:
        """スケジュール実行後の状態前進を session に書く (commit しない)。

        commit は呼び出し元 (_handle_fire の精算 tx) が mark_applied と同梱で
        1 回だけ行う (W3 D3: 状態前進と台帳 applied の全 or 無)。

        前進は **(行, 世代) 条件付き UPDATE** (Codex W3 第七陣): dispatch 前の
        世代照合から LLM 実行完了までの間にユーザーが同じ行を更新 (世代 bump)
        していた場合、旧発火の精算が新設定の COMPLETED / LAST_EXECUTED_AT を
        書き換えてはならない — 更新件数 0 = 「実行中に設定が置き換わった」で
        False を返し、呼び出し元は状態を進めず最新行で再登録する。

        Returns:
            True = 前進を書いた (periodic は前進すべき状態が無いため、no-op
            UPDATE によるフェンス照合の成立)。False = 行・世代が閉包の値と
            一致せず、前進 (照合) を見送った。
        """
        schedule_id = schedule.SCHEDULE_ID
        schedule_type = schedule.SCHEDULE_TYPE

        if schedule_type == "oneshot":
            values = {PersonaSchedule.COMPLETED: True}
        elif schedule_type == "interval":
            values = {PersonaSchedule.LAST_EXECUTED_AT: datetime.now(timezone.utc)}
        else:
            # periodic は前進すべき状態を持たない (次回発火は曜日+時刻で計算) が、
            # 行・世代フェンスの照合だけは行う (Codex W3 第八陣) — 実行中に行が
            # 更新・置換されていたら superseded_during_run を正しく記録するため。
            # 照合は SELECT でなく **no-op 条件付き UPDATE** (第九陣): SELECT は
            # 精算 commit と原子でなく、count 後・commit 前の世代 bump を見逃す。
            # 無変更 UPDATE でも書き込みロックを取るため、並行する bump と精算が
            # 直列化され「照合した世代のまま commit される」ことが保証される。
            values = {PersonaSchedule.SYNC_GENERATION: PersonaSchedule.SYNC_GENERATION}

        updated = (
            session.query(PersonaSchedule)
            .filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id,
                func.coalesce(PersonaSchedule.INSTANCE_TOKEN, "legacy")
                == instance_token,
                func.coalesce(PersonaSchedule.SYNC_GENERATION, 0) == generation,
            )
            .update(values, synchronize_session=False)
        )
        if updated:
            LOGGER.info(
                "[ScheduleManager] %s schedule %d %s (instance=%s generation=%d)",
                schedule_type, schedule_id,
                "fence verified" if schedule_type == "periodic"
                else "state advanced",
                instance_token, generation,
            )
        return updated > 0
