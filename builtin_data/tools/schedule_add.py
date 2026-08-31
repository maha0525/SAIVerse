"""
アラーム追加ツール

ペルソナが自分のアラームを追加できる。

ファイル名・関数名の ``schedule`` は互換のため残している (機能名としては
「アラーム」に改名済み)。ユーザーと LLM に見える文言だけを「アラーム」に揃える。
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from database.models import PersonaSchedule, AI as AIModel, City as CityModel
# 既定 Playbook の正典は ScheduleManager 側に一本化してある。UI からの REST 作成と
# このスペルで別々の既定値を持たないよう、両方ここから引く。
from saiverse.schedule_manager import DEFAULT_META_PLAYBOOK
from tools.context import get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def schedule_add(
    schedule_type: str,
    meta_playbook: str = DEFAULT_META_PLAYBOOK,
    description: str = "",
    priority: int = 0,
    enabled: bool = True,
    # periodic用
    days_of_week: Optional[List[int]] = None,
    time_of_day: Optional[str] = None,
    # oneshot用
    scheduled_datetime: Optional[str] = None,
    # interval用
    interval_seconds: Optional[int] = None,
    # playbook args
    args: Optional[Dict[str, Any]] = None,
) -> str:
    """
    新しいアラームを追加する。

    Args:
        schedule_type: アラームの種別 ("periodic", "oneshot", "interval")
        meta_playbook: 実行する Playbook 名。通常は省略してよい
            （会話用の標準 Playbook が使われる）。
        description: アラームの説明
        priority: 優先度（大きいほど優先）
        enabled: 有効にするかどうか
        days_of_week: 曜日リスト (periodic用、0=月曜日, 6=日曜日)。未指定なら毎日。
        time_of_day: 実行時刻 (periodic用、"HH:MM"形式、例: "09:00")
        scheduled_datetime: 実行日時 (oneshot用、"YYYY-MM-DD HH:MM"形式、ペルソナのタイムゾーンで指定)
        interval_seconds: 実行間隔（秒）(interval用)
        args: Playbook引数

    Returns:
        str: 実行結果メッセージ
    """
    manager = get_active_manager()
    if not manager:
        return "エラー: SAIVerseManagerが利用できません。"

    # 現在のペルソナIDを取得
    from tools.context import get_active_persona_id
    persona_id = get_active_persona_id()
    if not persona_id:
        return "エラー: 現在のペルソナを取得できませんでした。"

    # enabled の正規化（LLMが空文字やNoneを返す場合の防御）
    if enabled is None or enabled == "":
        enabled = True
    elif isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "1", "yes")

    # バリデーション
    if schedule_type not in ["periodic", "oneshot", "interval"]:
        return f"エラー: 不正なアラームの種別です: {schedule_type}"

    # LLM が None・空文字・空白のみを渡してきたときも既定の Playbook で
    # 成立させる (Playbook 名の指定はもうペルソナに求めていない)。空白を
    # 残したまま保存すると発火時に Playbook を引けず、鳴らないアラームになる。
    meta_playbook = (meta_playbook or "").strip() or DEFAULT_META_PLAYBOOK

    session = manager.SessionLocal()
    try:
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

        new_schedule = PersonaSchedule(
            PERSONA_ID=persona_id,
            SCHEDULE_TYPE=schedule_type,
            META_PLAYBOOK=meta_playbook,
            DESCRIPTION=description,
            PRIORITY=priority,
            ENABLED=enabled,
            PLAYBOOK_PARAMS=json.dumps(args) if args else None,
            # W3 A12 (D2): 新規行は世代 1 から始める (設定変更ごとに +1)
            SYNC_GENERATION=1,
            # W3 Codex 第三陣: 行一生トークン (SCHEDULE_ID 再利用との分離)。
            # 作成時に一度だけ採番し、更新では変えない。
            INSTANCE_TOKEN=uuid.uuid4().hex[:12],
        )

        # スケジュールタイプごとの設定
        if schedule_type == "periodic":
            if not time_of_day:
                return "エラー: 定期アラームには時刻 (time_of_day) を指定してください。"

            new_schedule.TIME_OF_DAY = time_of_day

            if days_of_week:
                # 曜日リストをJSONに変換
                new_schedule.DAYS_OF_WEEK = json.dumps(days_of_week)
            # 未指定なら毎日（DAYS_OF_WEEKをNullのままにする）

        elif schedule_type == "oneshot":
            if not scheduled_datetime:
                return "エラー: 単発アラームには実行日時 (scheduled_datetime) を指定してください。"

            try:
                # ペルソナのタイムゾーンで入力された日時を解釈
                dt_naive = datetime.strptime(scheduled_datetime, "%Y-%m-%d %H:%M")
                dt_local = dt_naive.replace(tzinfo=persona_tz)
                dt_utc = dt_local.astimezone(timezone.utc)
                new_schedule.SCHEDULED_DATETIME = dt_utc

                LOGGER.info(
                    "[schedule_add] Oneshot schedule: input=%s, local=%s (%s), utc=%s",
                    scheduled_datetime,
                    dt_local.isoformat(),
                    persona_tz,
                    dt_utc.isoformat(),
                )
            except ValueError as e:
                return f"エラー: 日時の形式が正しくありません (YYYY-MM-DD HH:MM): {e}"

        elif schedule_type == "interval":
            if not interval_seconds or interval_seconds <= 0:
                return "エラー: 繰り返しアラームには正の実行間隔 (interval_seconds) を指定してください。"

            new_schedule.INTERVAL_SECONDS = interval_seconds

        # DBに保存
        session.add(new_schedule)
        session.commit()

        schedule_id = new_schedule.SCHEDULE_ID

        LOGGER.info(
            "[schedule_add] Added schedule %d for persona %s (type=%s, playbook=%s)",
            schedule_id,
            persona_id,
            schedule_type,
            meta_playbook,
        )

        # 成功メッセージを生成
        status = "有効" if enabled else "無効"
        return f"✓ アラームを追加しました (ID: {schedule_id}, 種別: {schedule_type}, 状態: {status})"

    except Exception as e:
        LOGGER.error("Failed to add schedule: %s", e, exc_info=True)
        return f"エラー: アラームの追加に失敗しました。{e}"
    finally:
        session.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="schedule_add",
        description="新しいアラームを追加する。定期実行、単発実行、一定間隔での実行ができる。",
        parameters={
            "type": "object",
            "properties": {
                "schedule_type": {
                    "type": "string",
                    "enum": ["periodic", "oneshot", "interval"],
                    "description": "アラームの種別。periodic=定期実行、oneshot=単発実行、interval=一定間隔実行",
                },
                "meta_playbook": {
                    "type": "string",
                    "description": (
                        "実行する Playbook 名。通常は省略してよい"
                        "（会話用の標準 Playbook が使われる）。"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "アラームの説明。実行時にAIに渡される。",
                },
                "priority": {
                    "type": "integer",
                    "description": "優先度（0～100、大きいほど優先）。デフォルト: 0",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "有効にするかどうか。デフォルト: true",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "曜日リスト（periodic用）。0=月曜日、6=日曜日。未指定なら毎日。例: [0,2,4]で月水金",
                },
                "time_of_day": {
                    "type": "string",
                    "description": "実行時刻（periodic用）。HH:MM形式。例: '09:00'",
                },
                "scheduled_datetime": {
                    "type": "string",
                    "description": "実行日時（oneshot用）。YYYY-MM-DD HH:MM形式。ペルソナのタイムゾーンで指定。例: '2025-12-07 09:00'",
                },
                "interval_seconds": {
                    "type": "integer",
                    "description": "実行間隔（interval用、秒単位）。例: 600で10分ごと",
                },
                "args": {
                    "type": "object",
                    "description": "Playbook引数。通常は省略してよい。",
                },
            },
            "required": ["schedule_type"],
        },
        result_type="string",
    )
