"""Post a tweet to X (Twitter) on behalf of the active persona."""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from pathlib import Path
from typing import Tuple

from tools.core import ToolResult, ToolSchema
from tools.context import (
    get_active_persona_id,
    get_active_persona_path,
    get_active_manager,
    get_auto_mode,
    get_event_callback,
)

LOGGER = logging.getLogger(__name__)

# Ensure x_lib is importable from the same parent directory
_LIB_DIR = str(Path(__file__).parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def x_post_tweet(text: str) -> Tuple[str, ToolResult]:
    """Post a tweet. Shows confirmation dialog unless skip_confirmation is set."""
    from x_lib.credentials import load_credentials
    from x_lib.client import post_tweet, XAPIError

    persona_id = get_active_persona_id()
    persona_path = get_active_persona_path()
    if not persona_path:
        return "[X投稿エラー] ペルソナコンテキストが設定されていません。", ToolResult(history_snippet=None)

    creds = load_credentials(persona_path)
    if not creds:
        return (
            "[X投稿エラー] X連携が設定されていません。ペルソナ設定画面からXアカウントを連携してください。",
            ToolResult(history_snippet=None),
        )

    if len(text) > 280:
        return (
            f"[X投稿エラー] ツイートが280文字を超えています（{len(text)}文字）。短くしてください。",
            ToolResult(history_snippet=None),
        )

    # --- Confirmation flow ---
    auto_mode = get_auto_mode()
    if not creds.skip_confirmation and not auto_mode:
        event_callback = get_event_callback()
        manager = get_active_manager()

        if event_callback and manager:
            request_id = str(uuid.uuid4())
            event = threading.Event()
            manager._pending_tweet_confirmations[request_id] = event

            LOGGER.info("[x_post_tweet] Requesting tweet confirmation (request_id=%s)", request_id)
            event_callback({
                "type": "tweet_confirmation",
                "request_id": request_id,
                "tweet_text": text,
                "persona_id": persona_id,
                "x_username": creds.x_username,
            })

            responded = event.wait(timeout=120)

            # Cleanup
            manager._pending_tweet_confirmations.pop(request_id, None)
            response = manager._tweet_confirmation_responses.pop(request_id, None)

            if not responded or response is None:
                LOGGER.info("[x_post_tweet] Confirmation timed out")
                return "[X投稿] 確認がタイムアウトしました。投稿はキャンセルされました。", ToolResult(history_snippet=None)

            if response == "reject":
                LOGGER.info("[x_post_tweet] User rejected tweet")
                return "[X投稿] ユーザーが投稿を拒否しました。", ToolResult(history_snippet=None)

            if response.startswith("edit:"):
                text = response[5:]
                LOGGER.info("[x_post_tweet] User edited tweet text")

    # --- Post tweet ---
    try:
        result = post_tweet(text, creds, persona_path)
        tweet_id = result.get("data", {}).get("id", "unknown")
        msg = (
            f"ツイートを投稿しました！\n\n"
            f"投稿内容: {text}\n"
            f"ツイートID: {tweet_id}\n"
            f"アカウント: @{creds.x_username}"
        )
        snippet = f"[X投稿] @{creds.x_username}: {text[:80]}{'...' if len(text) > 80 else ''}"
        LOGGER.info("[x_post_tweet] Posted tweet id=%s for %s", tweet_id, creds.x_username)
        return msg, ToolResult(history_snippet=snippet)
    except XAPIError as exc:
        LOGGER.error("[x_post_tweet] Failed to post: %s", exc, exc_info=True)
        return f"[X投稿エラー] ツイートの投稿に失敗しました: {exc}", ToolResult(history_snippet=None)
    except Exception as exc:
        LOGGER.error("[x_post_tweet] Unexpected error: %s", exc, exc_info=True)
        return f"[X投稿エラー] 予期しないエラーが発生しました: {exc}", ToolResult(history_snippet=None)


def schema() -> ToolSchema:
    return ToolSchema(
        name="x_post_tweet",
        description="X（Twitter）にツイートを投稿します。投稿前にユーザーの確認を求めます。",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "ツイート内容（最大280文字）",
                },
            },
            "required": ["text"],
        },
        result_type="string",
    )
