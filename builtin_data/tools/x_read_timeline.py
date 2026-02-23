"""Read X (Twitter) home timeline for the active persona."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Tuple

from tools.core import ToolResult, ToolSchema
from tools.context import get_active_persona_id, get_active_persona_path

LOGGER = logging.getLogger(__name__)

_LIB_DIR = str(Path(__file__).parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def x_read_timeline(max_results: int = 10) -> Tuple[str, ToolResult]:
    """Fetch the home timeline for the active persona's X account."""
    from x_lib.credentials import load_credentials
    from x_lib.client import read_timeline, XAPIError

    persona_path = get_active_persona_path()
    if not persona_path:
        return "[Xタイムラインエラー] ペルソナコンテキストが設定されていません。", ToolResult(history_snippet=None)

    creds = load_credentials(persona_path)
    if not creds:
        return (
            "[Xタイムラインエラー] X連携が設定されていません。ペルソナ設定画面からXアカウントを連携してください。",
            ToolResult(history_snippet=None),
        )

    try:
        tweets = read_timeline(creds, persona_path, max_results=max_results)
    except XAPIError as exc:
        LOGGER.error("[x_read_timeline] Failed: %s", exc, exc_info=True)
        return f"[Xタイムラインエラー] タイムラインの取得に失敗しました: {exc}", ToolResult(history_snippet=None)
    except Exception as exc:
        LOGGER.error("[x_read_timeline] Unexpected error: %s", exc, exc_info=True)
        return f"[Xタイムラインエラー] 予期しないエラー: {exc}", ToolResult(history_snippet=None)

    if not tweets:
        return "タイムラインにツイートがありませんでした。", ToolResult(history_snippet="[Xタイムライン] 0件")

    lines = [f"ホームタイムライン（{len(tweets)}件）:"]
    for t in tweets:
        name = t.get("author_name", "")
        username = t.get("author_username", "")
        text = t.get("text", "")
        created = t.get("created_at", "")
        line = f"- @{username} ({name}) [{created}]: {text}"
        for m in t.get("media", []):
            line += f"\n  [{m.get('type', 'media')}] {m['url']}"
        lines.append(line)

    msg = "\n".join(lines)
    snippet = f"[Xタイムライン] {len(tweets)}件取得"
    LOGGER.info("[x_read_timeline] Retrieved %d tweets for %s", len(tweets), creds.x_username)
    return msg, ToolResult(history_snippet=snippet)


def schema() -> ToolSchema:
    return ToolSchema(
        name="x_read_timeline",
        description="X（Twitter）のホームタイムラインを取得します。",
        parameters={
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "取得するツイート数（1-100、デフォルト10）",
                    "default": 10,
                },
            },
            "required": [],
        },
        result_type="string",
    )
