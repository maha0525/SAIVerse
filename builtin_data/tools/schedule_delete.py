"""
アラーム削除ツール

ペルソナが自分のアラームを削除できる。

ファイル名・関数名の ``schedule`` は互換のため残している (機能名としては
「アラーム」に改名済み)。ユーザーと LLM に見える文言だけを「アラーム」に揃える。
"""

import logging
from typing import Any, Dict

from database.models import PersonaSchedule
from tools.context import get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def schedule_delete(schedule_id: int) -> str:
    """
    指定されたIDのアラームを削除する。
    自分のアラームのみ削除可能。

    Args:
        schedule_id: 削除するアラームのID

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

    session = manager.SessionLocal()
    try:
        # スケジュールを取得
        schedule = (
            session.query(PersonaSchedule)
            .filter(
                PersonaSchedule.SCHEDULE_ID == schedule_id,
                PersonaSchedule.PERSONA_ID == persona_id,  # 自分のスケジュールのみ
            )
            .first()
        )

        if not schedule:
            return f"エラー: アラームID {schedule_id} が見つかりません。または、他のペルソナのアラームです。"

        # アラーム情報を保存（削除前に）
        schedule_type = schedule.SCHEDULE_TYPE
        description = schedule.DESCRIPTION or "(説明なし)"

        # 削除実行
        session.delete(schedule)
        session.commit()

        LOGGER.info(
            "[schedule_delete] Deleted schedule %d for persona %s (type=%s)",
            schedule_id,
            persona_id,
            schedule_type,
        )

        return f"✓ アラームを削除しました (ID: {schedule_id}, 種別: {schedule_type}, 説明: {description})"

    except Exception as e:
        LOGGER.error("Failed to delete schedule: %s", e, exc_info=True)
        return f"エラー: アラームの削除に失敗しました。{e}"
    finally:
        session.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="schedule_delete",
        description="指定されたIDのアラームを削除する。自分のアラームのみ削除できる。",
        parameters={
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "integer",
                    "description": "削除するアラームのID。schedule_listで確認できる。",
                },
            },
            "required": ["schedule_id"],
        },
        result_type="string",
    )
