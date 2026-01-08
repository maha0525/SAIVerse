"""
phenomena.defs.example_log ― サンプルフェノメノン

ログ出力を行うサンプルフェノメノン。
テストや動作確認に使用できる。
"""
import logging
from phenomena.defs import PhenomenonSchema

LOGGER = logging.getLogger(__name__)


def log_event(message: str, level: str = "info") -> str:
    """指定されたメッセージをログに出力する

    Args:
        message: ログに出力するメッセージ
        level: ログレベル (debug, info, warning, error)

    Returns:
        実行結果のメッセージ
    """
    log_func = getattr(LOGGER, level, LOGGER.info)
    log_func("[Phenomenon] %s", message)
    return f"Logged: {message}"


def schema() -> PhenomenonSchema:
    return PhenomenonSchema(
        name="log_event",
        description="指定されたメッセージをシステムログに出力します。テストや動作確認に使用できます。",
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "ログに出力するメッセージ",
                },
                "level": {
                    "type": "string",
                    "enum": ["debug", "info", "warning", "error"],
                    "description": "ログレベル（デフォルト: info）",
                },
            },
            "required": ["message"],
        },
        is_async=True,
    )
