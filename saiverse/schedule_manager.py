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

発火後は callback 内で:
  1. メタプレイブックを PulseController に投げる
  2. 完了状態 (oneshot の COMPLETED, interval の LAST_EXECUTED_AT) を更新
  3. 次回発火時刻を計算して EventScheduler に再 register
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from database.models import PersonaSchedule, AI as AIModel, City as CityModel

if TYPE_CHECKING:
    from .saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)


def _schedule_key(schedule_id: int) -> str:
    """EventScheduler に渡す key (schedule_id 単位で一意)。"""
    return f"persona_schedule:{schedule_id}"


class ScheduleManager:
    """ペルソナスケジュールを EventScheduler 経由で発火する管理クラス。"""

    def __init__(self, saiverse_manager: "SAIVerseManager"):
        self.manager = saiverse_manager
        self._registered_ids: Set[int] = set()
        # 互換性のため属性は残すが、未使用 (旧 _schedule_loop で使われていた)
        self._stop_event = None
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
            self._registered_ids.clear()
            return

        for schedule_id in list(self._registered_ids):
            try:
                scheduler.cancel(_schedule_key(schedule_id))
            except Exception:
                LOGGER.exception(
                    "[ScheduleManager] Failed to cancel schedule %d on stop",
                    schedule_id,
                )
        self._registered_ids.clear()
        LOGGER.info("[ScheduleManager] Stopped")

    # ------------------------------------------------------------------
    # Public API: 個別スケジュールの register / unregister
    # ------------------------------------------------------------------

    def register_schedule(self, schedule_id: int) -> bool:
        """1 件のスケジュールを (再)登録する。既存予約は上書きされる。

        作成・更新・トグル ON 時に API ルートから呼ばれる。スケジュールが
        無効化されていたり、次回発火時刻が計算できない (oneshot 完了済 等)
        場合は EventScheduler から cancel するだけで終わる。

        Returns:
            登録できたら True、cancel のみだった or 失敗なら False。
        """
        session = self.manager.SessionLocal()
        try:
            schedule = session.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id
            ).first()
            if schedule is None:
                # 削除されたケース: cancel のみ
                self._do_cancel(schedule_id)
                return False
            return self._do_register(schedule, session)
        finally:
            session.close()

    def unregister_schedule(self, schedule_id: int) -> None:
        """1 件の予約をキャンセル。削除・トグル OFF 時に API 層から呼ばれる。"""
        self._do_cancel(schedule_id)

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
        # callback は schedule_id だけを captureして、発火時に DB から最新状態を読む。
        # schedule オブジェクト自体を closure に閉じ込めると ORM session の寿命とずれる。
        scheduler.schedule(
            fire_at=next_fire,
            callback=lambda sid=schedule_id: self._handle_fire(sid),
            key=_schedule_key(schedule_id),
        )
        self._registered_ids.add(schedule_id)
        LOGGER.debug(
            "[ScheduleManager] Registered schedule %d (type=%s, persona=%s, fire_at=%s)",
            schedule_id, schedule.SCHEDULE_TYPE, schedule.PERSONA_ID, next_fire.isoformat(),
        )
        return True

    def _do_cancel(self, schedule_id: int) -> None:
        scheduler = getattr(self.manager, "event_scheduler", None)
        if scheduler is None:
            return
        scheduler.cancel(_schedule_key(schedule_id))
        self._registered_ids.discard(schedule_id)

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

    def _handle_fire(self, schedule_id: int) -> None:
        """EventScheduler から呼ばれる発火 callback。

        schedule_id だけを captureしているので、発火時に最新の DB 状態を
        読み直して実行する。実行後は次回発火時刻を計算して再 register する。
        """
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
                self._registered_ids.discard(schedule_id)
                return

            if not schedule.ENABLED:
                LOGGER.debug(
                    "[ScheduleManager] _handle_fire: schedule %d is disabled, skipping",
                    schedule_id,
                )
                self._registered_ids.discard(schedule_id)
                return

            self._execute_schedule(schedule, session)
            self._update_schedule_after_execution(schedule, session)

            # 次回 register (oneshot 完了 / interval 継続 / periodic 次回)
            self._do_register(schedule, session)

        finally:
            session.close()

    # ------------------------------------------------------------------
    # スケジュール実行 (旧実装からほぼそのまま引き継ぎ)
    # ------------------------------------------------------------------

    def _generate_schedule_prompt(self, schedule: PersonaSchedule, session, persona_id: str) -> str:
        """スケジュール実行時のプロンプトを生成"""
        now = datetime.now(timezone.utc)
        persona_tz = self._get_persona_timezone(persona_id, session)
        local_now = now.astimezone(persona_tz)

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

    def _execute_schedule(self, schedule: PersonaSchedule, session) -> None:
        """スケジュールを実行 (PulseController.submit_schedule)。"""
        persona_id = schedule.PERSONA_ID
        meta_playbook = schedule.META_PLAYBOOK

        schedule_args: Optional[Dict[str, Any]] = None
        pre_spells: Optional[List[str]] = None
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

        LOGGER.info(
            "[ScheduleManager] Executing schedule %d for persona %s (type=%s, playbook=%s)",
            schedule.SCHEDULE_ID, persona_id, schedule.SCHEDULE_TYPE, meta_playbook,
        )

        persona = self.manager.all_personas.get(persona_id)
        if not persona:
            LOGGER.warning("[ScheduleManager] Persona %s not found in all_personas", persona_id)
            return

        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            LOGGER.warning("[ScheduleManager] Persona %s has no current_building_id", persona_id)
            return

        user_input = self._generate_schedule_prompt(schedule, session, persona_id)

        try:
            # pulse_dispatch.md §7: PulseDispatcher 経由で起動
            self.manager.pulse_dispatcher.dispatch_schedule_fire(
                persona_id=persona_id,
                building_id=building_id,
                user_input=user_input,
                metadata={"schedule_id": schedule.SCHEDULE_ID, "schedule_type": schedule.SCHEDULE_TYPE},
                meta_playbook=meta_playbook,
                args=schedule_args,
                pre_spells=pre_spells,
            )
            LOGGER.info("[ScheduleManager] Schedule %d submitted via PulseDispatcher", schedule.SCHEDULE_ID)

            self.manager._save_modified_buildings()
            persona._save_session_metadata()

        except Exception:
            LOGGER.exception(
                "[ScheduleManager] Failed to execute schedule %d", schedule.SCHEDULE_ID,
            )

    def _update_schedule_after_execution(self, schedule: PersonaSchedule, session) -> None:
        """スケジュール実行後の状態を更新。"""
        now = datetime.now(timezone.utc)

        if schedule.SCHEDULE_TYPE == "oneshot":
            schedule.COMPLETED = True
            session.commit()
            LOGGER.info("[ScheduleManager] Oneshot schedule %d marked as completed", schedule.SCHEDULE_ID)
        elif schedule.SCHEDULE_TYPE == "interval":
            schedule.LAST_EXECUTED_AT = now
            session.commit()
            LOGGER.info(
                "[ScheduleManager] Interval schedule %d updated LAST_EXECUTED_AT",
                schedule.SCHEDULE_ID,
            )
        # periodic は LAST_EXECUTED_AT を持たない (次回発火は曜日+時刻で機械的に計算)
