import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from database.models import PersonaSchedule, AI as AIModel, City as CityModel

if TYPE_CHECKING:
    from saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)


class ScheduleManager:
    """
    ペルソナのスケジュールを管理し、定期的にチェックして実行するクラス。
    """

    def __init__(self, saiverse_manager: "SAIVerseManager", check_interval: int = 60):
        """
        :param saiverse_manager: SAIVerseManagerインスタンス
        :param check_interval: スケジュールチェック間隔（秒）
        """
        self.manager = saiverse_manager
        self.check_interval = check_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_check_times: Dict[int, datetime] = {}  # schedule_id -> last check time
        LOGGER.info("[ScheduleManager] Initialized with check interval: %d seconds", check_interval)

    def start(self):
        """スケジュールチェックループをバックグラウンドで開始"""
        if self._thread and self._thread.is_alive():
            LOGGER.warning("[ScheduleManager] Thread is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self._thread.start()
        LOGGER.info("[ScheduleManager] Started background schedule checker thread (check_interval=%ds).", self.check_interval)
        # スレッドが実際に起動したか少し待って確認
        import time
        time.sleep(0.1)
        if self._thread.is_alive():
            LOGGER.info("[ScheduleManager] Thread is confirmed alive.")
        else:
            LOGGER.error("[ScheduleManager] Thread failed to start!")

    def stop(self):
        """スケジュールチェックループを停止"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        LOGGER.info("[ScheduleManager] Stopped schedule checker thread.")

    def _schedule_loop(self):
        """定期的にスケジュールをチェックして実行するメインループ"""
        LOGGER.info("[ScheduleManager] Schedule loop started")
        while not self._stop_event.is_set():
            self._stop_event.wait(self.check_interval)
            if self._stop_event.is_set():
                break

            try:
                LOGGER.debug("[ScheduleManager] Checking schedules...")
                self._check_and_execute_schedules()
            except Exception as e:
                LOGGER.error("[ScheduleManager] Error in schedule loop: %s", e, exc_info=True)
        LOGGER.info("[ScheduleManager] Schedule loop ended")

    def _check_and_execute_schedules(self):
        """全スケジュールをチェックし、発火条件を満たすものを実行"""
        session = self.manager.SessionLocal()
        try:
            # 有効なスケジュールを優先度順に取得
            schedules = (
                session.query(PersonaSchedule)
                .filter(PersonaSchedule.ENABLED == True)
                .order_by(PersonaSchedule.PRIORITY.desc(), PersonaSchedule.SCHEDULE_ID.desc())
                .all()
            )

            LOGGER.debug("[ScheduleManager] Found %d enabled schedules", len(schedules))

            for schedule in schedules:
                try:
                    should_run = self._should_execute(schedule, session)
                    LOGGER.debug(
                        "[ScheduleManager] Schedule %d (type=%s, persona=%s): should_execute=%s",
                        schedule.SCHEDULE_ID,
                        schedule.SCHEDULE_TYPE,
                        schedule.PERSONA_ID,
                        should_run,
                    )
                    if should_run:
                        self._execute_schedule(schedule, session)
                except Exception as e:
                    LOGGER.error(
                        "[ScheduleManager] Error checking schedule %d: %s",
                        schedule.SCHEDULE_ID,
                        e,
                        exc_info=True,
                    )
        finally:
            session.close()

    def _should_execute(self, schedule: PersonaSchedule, session) -> bool:
        """スケジュールを実行すべきかを判定"""
        schedule_id = schedule.SCHEDULE_ID
        schedule_type = schedule.SCHEDULE_TYPE
        now = datetime.now(timezone.utc)

        # ペルソナのタイムゾーンを取得
        persona_tz = self._get_persona_timezone(schedule.PERSONA_ID, session)
        local_now = now.astimezone(persona_tz)

        if schedule_type == "periodic":
            return self._should_execute_periodic(schedule, local_now, schedule_id)
        elif schedule_type == "oneshot":
            return self._should_execute_oneshot(schedule, now)
        elif schedule_type == "interval":
            return self._should_execute_interval(schedule, now)
        else:
            LOGGER.warning("[ScheduleManager] Unknown schedule type: %s", schedule_type)
            return False

    def _should_execute_periodic(self, schedule: PersonaSchedule, local_now: datetime, schedule_id: int) -> bool:
        """定期スケジュールの発火判定"""
        # 曜日チェック
        if schedule.DAYS_OF_WEEK:
            try:
                days = json.loads(schedule.DAYS_OF_WEEK)
                if local_now.weekday() not in days:
                    return False
            except Exception:
                LOGGER.warning("[ScheduleManager] Failed to parse DAYS_OF_WEEK for schedule %d", schedule_id)
                return False

        # 時刻チェック
        if not schedule.TIME_OF_DAY:
            return False

        target_time = schedule.TIME_OF_DAY
        current_time = local_now.strftime("%H:%M")

        # 前回のチェック時刻を取得
        last_check = self._last_check_times.get(schedule_id)
        self._last_check_times[schedule_id] = local_now

        # 現在時刻が目標時刻と一致し、かつ前回チェックから一定時間経過している場合に実行
        if current_time == target_time:
            if last_check is None or (local_now - last_check).total_seconds() > 60:
                return True

        return False

    def _should_execute_oneshot(self, schedule: PersonaSchedule, now: datetime) -> bool:
        """単発スケジュールの発火判定"""
        if schedule.COMPLETED:
            LOGGER.debug("[ScheduleManager] Schedule %d already completed", schedule.SCHEDULE_ID)
            return False

        if not schedule.SCHEDULED_DATETIME:
            LOGGER.warning("[ScheduleManager] Schedule %d has no SCHEDULED_DATETIME", schedule.SCHEDULE_ID)
            return False

        # スケジュール時刻を過ぎているかチェック
        scheduled_time = schedule.SCHEDULED_DATETIME
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)

        should_execute = now >= scheduled_time
        LOGGER.debug(
            "[ScheduleManager] Oneshot schedule %d: now=%s, scheduled=%s, should_execute=%s",
            schedule.SCHEDULE_ID,
            now.isoformat(),
            scheduled_time.isoformat(),
            should_execute,
        )
        return should_execute

    def _should_execute_interval(self, schedule: PersonaSchedule, now: datetime) -> bool:
        """恒常スケジュールの発火判定"""
        if not schedule.INTERVAL_SECONDS:
            return False

        last_executed = schedule.LAST_EXECUTED_AT
        if last_executed is None:
            # 初回実行
            return True

        if last_executed.tzinfo is None:
            last_executed = last_executed.replace(tzinfo=timezone.utc)

        elapsed = (now - last_executed).total_seconds()
        return elapsed >= schedule.INTERVAL_SECONDS

    def _generate_schedule_prompt(self, schedule: PersonaSchedule, session, persona_id: str) -> str:
        """スケジュール実行時のプロンプトを生成"""
        now = datetime.now(timezone.utc)
        persona_tz = self._get_persona_timezone(persona_id, session)
        local_now = now.astimezone(persona_tz)

        # スケジュールタイプに応じた実行日時の取得
        scheduled_time_str = ""
        if schedule.SCHEDULE_TYPE == "periodic":
            # 定期スケジュールの場合、曜日と時刻を表示
            days_str = "毎日"
            if schedule.DAYS_OF_WEEK:
                try:
                    day_list = json.loads(schedule.DAYS_OF_WEEK)
                    day_names = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
                    days_str = ", ".join([day_names[d] for d in day_list if 0 <= d < 7])
                except Exception:
                    pass
            scheduled_time_str = f"{days_str} {schedule.TIME_OF_DAY or '??:??'}"

        elif schedule.SCHEDULE_TYPE == "oneshot":
            # 単発スケジュールの場合、設定された日時を表示
            if schedule.SCHEDULED_DATETIME:
                dt_utc = schedule.SCHEDULED_DATETIME
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone(persona_tz)
                scheduled_time_str = dt_local.strftime("%Y年%m月%d日 %H:%M")

        elif schedule.SCHEDULE_TYPE == "interval":
            # 恒常スケジュールの場合、インターバルを表示
            interval_sec = schedule.INTERVAL_SECONDS or 0
            if interval_sec >= 3600:
                hours = interval_sec // 3600
                scheduled_time_str = f"{hours}時間ごと"
            elif interval_sec >= 60:
                minutes = interval_sec // 60
                scheduled_time_str = f"{minutes}分ごと"
            else:
                scheduled_time_str = f"{interval_sec}秒ごと"

        # プロンプトを生成
        prompt = f"""<system>
スケジュールが実行されました。

現在の日時: {local_now.strftime("%Y年%m月%d日 %H:%M")} ({persona_tz})
スケジュールタイプ: {schedule.SCHEDULE_TYPE}
スケジュール設定: {scheduled_time_str}
スケジュールの説明: {schedule.DESCRIPTION or "（説明なし）"}
</system>"""

        LOGGER.debug("[ScheduleManager] Generated prompt: %s", prompt)
        return prompt

    def _execute_schedule(self, schedule: PersonaSchedule, session):
        """スケジュールを実行"""
        persona_id = schedule.PERSONA_ID
        meta_playbook = schedule.META_PLAYBOOK

        LOGGER.info(
            "[ScheduleManager] Executing schedule %d for persona %s (type=%s, playbook=%s)",
            schedule.SCHEDULE_ID,
            persona_id,
            schedule.SCHEDULE_TYPE,
            meta_playbook,
        )

        # ペルソナを取得
        persona = self.manager.all_personas.get(persona_id)
        if not persona:
            LOGGER.warning("[ScheduleManager] Persona %s not found in all_personas", persona_id)
            return

        # ペルソナの現在地を取得
        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            LOGGER.warning("[ScheduleManager] Persona %s has no current_building_id", persona_id)
            return

        # スケジュール実行用のプロンプトを生成
        user_input = self._generate_schedule_prompt(schedule, session, persona_id)

        # メタプレイブックを実行
        try:
            sea_enabled = hasattr(self.manager, "sea_enabled") and self.manager.sea_enabled
            LOGGER.debug(
                "[ScheduleManager] SEA framework check: sea_enabled=%s, has_sea_runtime=%s",
                sea_enabled,
                hasattr(self.manager, "sea_runtime"),
            )

            if sea_enabled:
                # SEA有効時はrun_meta_userを使用
                occupants = self.manager.occupants.get(building_id, [])
                LOGGER.info(
                    "[ScheduleManager] Calling sea_runtime.run_meta_user with playbook=%s, building=%s, prompt_length=%d",
                    meta_playbook,
                    building_id,
                    len(user_input),
                )
                self.manager.sea_runtime.run_meta_user(
                    persona=persona,
                    user_input=user_input,
                    building_id=building_id,
                    metadata={"schedule_id": schedule.SCHEDULE_ID, "schedule_type": schedule.SCHEDULE_TYPE},
                    meta_playbook=meta_playbook,
                )
                LOGGER.info("[ScheduleManager] sea_runtime.run_meta_user completed")
            else:
                LOGGER.warning("[ScheduleManager] SEA framework not enabled, schedule execution skipped")

            # 実行後の状態更新
            self._update_schedule_after_execution(schedule, session)

        except Exception as e:
            LOGGER.error(
                "[ScheduleManager] Failed to execute schedule %d: %s",
                schedule.SCHEDULE_ID,
                e,
                exc_info=True,
            )

    def _update_schedule_after_execution(self, schedule: PersonaSchedule, session):
        """スケジュール実行後の状態を更新"""
        now = datetime.now(timezone.utc)

        if schedule.SCHEDULE_TYPE == "oneshot":
            # 単発スケジュールは完了フラグを立てる
            schedule.COMPLETED = True
            session.commit()
            LOGGER.info("[ScheduleManager] Oneshot schedule %d marked as completed", schedule.SCHEDULE_ID)

        elif schedule.SCHEDULE_TYPE == "interval":
            # 恒常スケジュールは最終実行時刻を更新
            schedule.LAST_EXECUTED_AT = now
            session.commit()
            LOGGER.info("[ScheduleManager] Interval schedule %d updated LAST_EXECUTED_AT", schedule.SCHEDULE_ID)

    def _get_persona_timezone(self, persona_id: str, session) -> ZoneInfo:
        """ペルソナのホームCityのタイムゾーンを取得"""
        try:
            persona_model = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
            if not persona_model:
                LOGGER.warning("[ScheduleManager] Persona %s not found in database", persona_id)
                return ZoneInfo("UTC")

            city_model = session.query(CityModel).filter(CityModel.CITYID == persona_model.HOME_CITYID).first()
            if not city_model or not city_model.TIMEZONE:
                LOGGER.warning("[ScheduleManager] City timezone not found for persona %s", persona_id)
                return ZoneInfo("UTC")

            return ZoneInfo(city_model.TIMEZONE)
        except Exception as e:
            LOGGER.warning("[ScheduleManager] Failed to get timezone for persona %s: %s", persona_id, e)
            return ZoneInfo("UTC")
