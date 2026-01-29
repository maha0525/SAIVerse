"""
スケジュール一覧取得ツール

ペルソナが自分のスケジュール一覧を取得できる。
"""

import json
import logging
from datetime import timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from database.models import PersonaSchedule, AI as AIModel, City as CityModel
from tools.context import get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def schedule_list() -> str:
    """
    自分のスケジュール一覧を取得する。

    Returns:
        str: スケジュール一覧を整形した文字列
    """
    manager = get_active_manager()
    if not manager:
        return "エラー: SAIVerseManagerが利用できません。"

    # 現在のペルソナIDを取得
    from tools.context import get_active_persona_id
    persona_id = get_active_persona_id()
    if not persona_id:
        return "エラー: 現在のペルソナを取得できませんでした。"

    session = manager.SessionLocal()
    try:
        # ペルソナのスケジュールを取得
        schedules = (
            session.query(PersonaSchedule)
            .filter(PersonaSchedule.PERSONA_ID == persona_id)
            .order_by(PersonaSchedule.PRIORITY.desc(), PersonaSchedule.SCHEDULE_ID.desc())
            .all()
        )

        if not schedules:
            return "現在、登録されているスケジュールはありません。"

        # ペルソナのタイムゾーンを取得
        persona_model = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
        if not persona_model:
            persona_tz = ZoneInfo("UTC")
        else:
            city_model = session.query(CityModel).filter(CityModel.CITYID == persona_model.HOME_CITYID).first()
            if city_model and city_model.TIMEZONE:
                persona_tz = ZoneInfo(city_model.TIMEZONE)
            else:
                persona_tz = ZoneInfo("UTC")

        # スケジュール情報を整形
        result_lines = [f"【スケジュール一覧】 (全{len(schedules)}件)\n"]

        for i, s in enumerate(schedules, 1):
            status = "✓有効" if s.ENABLED else "✗無効"
            completed = " (完了)" if s.COMPLETED else ""

            # タイプごとの詳細情報
            detail = ""
            if s.SCHEDULE_TYPE == "periodic":
                days = "毎日"
                if s.DAYS_OF_WEEK:
                    try:
                        day_list = json.loads(s.DAYS_OF_WEEK)
                        day_names = ["月", "火", "水", "木", "金", "土", "日"]
                        days = ", ".join([day_names[d] for d in day_list if 0 <= d < 7])
                    except Exception:
                        pass
                detail = f"{days} {s.TIME_OF_DAY or '??:??'}"

            elif s.SCHEDULE_TYPE == "oneshot":
                if s.SCHEDULED_DATETIME:
                    dt_utc = s.SCHEDULED_DATETIME
                    if dt_utc.tzinfo is None:
                        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                    dt_local = dt_utc.astimezone(persona_tz)
                    detail = dt_local.strftime("%Y年%m月%d日 %H:%M")
                else:
                    detail = "未設定"

            elif s.SCHEDULE_TYPE == "interval":
                interval_sec = s.INTERVAL_SECONDS or 0
                if interval_sec >= 3600:
                    hours = interval_sec // 3600
                    detail = f"{hours}時間ごと"
                elif interval_sec >= 60:
                    minutes = interval_sec // 60
                    detail = f"{minutes}分ごと"
                else:
                    detail = f"{interval_sec}秒ごと"

                if s.LAST_EXECUTED_AT:
                    last_exec_utc = s.LAST_EXECUTED_AT
                    if last_exec_utc.tzinfo is None:
                        last_exec_utc = last_exec_utc.replace(tzinfo=timezone.utc)
                    last_exec_local = last_exec_utc.astimezone(persona_tz)
                    detail += f" (最終実行: {last_exec_local.strftime('%Y-%m-%d %H:%M')})"

            # Parse playbook_params
            params_str = "(なし)"
            if s.PLAYBOOK_PARAMS:
                try:
                    params = json.loads(s.PLAYBOOK_PARAMS)
                    if params:
                        params_str = ", ".join([f"{k}={v}" for k, v in params.items()])
                except Exception:
                    pass

            result_lines.append(
                f"{i}. [ID: {s.SCHEDULE_ID}] {status}{completed}\n"
                f"   タイプ: {s.SCHEDULE_TYPE}\n"
                f"   実行: {detail}\n"
                f"   プレイブック: {s.META_PLAYBOOK}\n"
                f"   パラメータ: {params_str}\n"
                f"   優先度: {s.PRIORITY}\n"
                f"   説明: {s.DESCRIPTION or '(なし)'}\n"
            )

        return "\n".join(result_lines)

    except Exception as e:
        LOGGER.error("Failed to list schedules: %s", e, exc_info=True)
        return f"エラー: スケジュール一覧の取得に失敗しました。{e}"
    finally:
        session.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="schedule_list",
        description="自分のスケジュール一覧を取得する。スケジュールIDや設定内容を確認したいときに使う。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        result_type="string",
    )
