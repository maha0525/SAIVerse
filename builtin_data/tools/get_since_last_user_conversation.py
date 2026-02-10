"""Get summary of events since last user conversation."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Maximum messages to include in raw log
MAX_RAW_MESSAGES = 20
# Maximum characters for summary generation
MAX_SUMMARY_INPUT_CHARS = 30000


def get_since_last_user_conversation(
    include_raw_log: bool = True,
    max_raw_messages: int = MAX_RAW_MESSAGES,
) -> str:
    """Get summary and recent log of events since the last user conversation.

    This tool:
    1. Finds the last message with user interaction (with: ["user"])
    2. Collects all messages since then
    3. Generates a summary using LLM
    4. Saves the summary with a UUID for later detailed recall
    5. Returns the summary + recent raw messages

    Args:
        include_raw_log: Include recent raw messages in the output
        max_raw_messages: Maximum number of raw messages to include

    Returns:
        Summary text with UUID reference, optionally followed by raw log
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    adapter = getattr(persona, "sai_memory", None)
    if not adapter or not adapter.is_ready():
        return "メモリが利用できません"

    # Get all recent messages
    try:
        all_messages = adapter.recent_persona_messages(
            max_chars=MAX_SUMMARY_INPUT_CHARS,
            required_tags=None,  # Get all messages
        )
    except Exception as exc:
        LOGGER.warning("Failed to get messages: %s", exc)
        return "メッセージの取得に失敗しました"

    if not all_messages:
        return "履歴がありません"

    # Find the last user conversation
    last_user_idx = -1
    for idx, msg in enumerate(all_messages):
        metadata = msg.get("metadata", {})
        with_list = metadata.get("with", [])
        if "user" in with_list:
            last_user_idx = idx

    if last_user_idx == -1:
        # No user conversation found - summarize all
        messages_since = all_messages
        context_note = "ユーザーとの会話履歴が見つかりませんでした。全体の状況を要約します。"
    elif last_user_idx == len(all_messages) - 1:
        # Last message was with user - nothing new since then
        return "ユーザーとの会話後、特に新しい出来事はありません。"
    else:
        # Get messages after the last user conversation
        messages_since = all_messages[last_user_idx + 1:]
        context_note = f"最後にユーザーと話してから{len(messages_since)}件のメッセージがあります。"

    if not messages_since:
        return "ユーザーとの会話後、特に新しい出来事はありません。"

    # Generate summary using LLM
    summary_uuid = str(uuid.uuid4())
    summary_text = _generate_summary(persona, messages_since, summary_uuid)

    # Save summary to SAIMemory with UUID for later recall
    _save_summary_to_memory(adapter, summary_text, summary_uuid, messages_since)

    # Build output
    output_parts = [
        context_note,
        "",
        f"【要約】(参照ID: {summary_uuid[:8]})",
        summary_text,
    ]

    # Add raw log if requested
    if include_raw_log and messages_since:
        raw_messages = messages_since[-max_raw_messages:]
        output_parts.append("")
        output_parts.append(f"【直近のログ】(最新{len(raw_messages)}件)")
        for msg in raw_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")[:200]  # Truncate long messages
            if len(msg.get("content", "")) > 200:
                content += "..."
            output_parts.append(f"- [{role}] {content}")

    return "\n".join(output_parts)


def _generate_summary(persona: Any, messages: List[Dict], summary_uuid: str) -> str:
    """Generate a summary of messages using LLM."""
    try:
        from llm_clients import get_llm_client
        from saiverse.model_configs import get_model_config

        # Use lightweight model for summary
        model_name = getattr(persona, "lightweight_model", None)
        if not model_name:
            import os
            model_name = os.getenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gemini-2.5-flash-lite-preview-09-2025")

        config = get_model_config(model_name)
        client = get_llm_client(model_name, config)

        # Build prompt
        messages_text = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            metadata = msg.get("metadata", {})
            tags = metadata.get("tags", [])

            # Skip internal wait messages in summary
            if "wait" in tags:
                continue

            messages_text.append(f"[{role}] {content}")

        if not messages_text:
            return "特に記録すべき出来事はありませんでした（待機のみ）。"

        prompt = f"""以下は、ユーザーと最後に話してから現在までの出来事の記録です。
これを簡潔に要約してください（3-5文程度）。重要な出来事や会話があれば強調してください。

---
{chr(10).join(messages_text)}
---

要約:"""

        llm_messages = [{"role": "user", "content": prompt}]
        response = client.chat(llm_messages, model=model_name)
        return response.strip()

    except Exception as exc:
        LOGGER.warning("Failed to generate summary: %s", exc)
        # Fallback: simple count-based summary
        wait_count = sum(1 for m in messages if "wait" in m.get("metadata", {}).get("tags", []))
        other_count = len(messages) - wait_count
        return f"待機{wait_count}回、その他のアクティビティ{other_count}件がありました。"


def _save_summary_to_memory(
    adapter: Any,
    summary_text: str,
    summary_uuid: str,
    source_messages: List[Dict],
) -> None:
    """Save summary to SAIMemory with reference metadata."""
    try:
        # Calculate time range of source messages
        timestamps = []
        for msg in source_messages:
            ts = msg.get("timestamp")
            if ts:
                timestamps.append(ts)

        metadata = {
            "tags": ["summary", "since_user_conversation"],
            "summary_uuid": summary_uuid,
            "source_message_count": len(source_messages),
        }
        if timestamps:
            metadata["source_time_start"] = min(timestamps)
            metadata["source_time_end"] = max(timestamps)

        summary_msg = {
            "role": "system",
            "content": f"[要約 {summary_uuid[:8]}] {summary_text}",
            "metadata": metadata,
            "timestamp": datetime.now(dt_timezone.utc).isoformat(),
        }

        adapter.append_persona_message(summary_msg)
        LOGGER.debug("Saved summary with UUID %s", summary_uuid[:8])

    except Exception as exc:
        LOGGER.warning("Failed to save summary: %s", exc)


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_since_last_user_conversation",
        description="Get summary and recent log of events since the last user conversation. Returns a summary with a UUID that can be used for detailed recall.",
        parameters={
            "type": "object",
            "properties": {
                "include_raw_log": {
                    "type": "boolean",
                    "description": "Include recent raw messages in output. Default: true.",
                    "default": True
                },
                "max_raw_messages": {
                    "type": "integer",
                    "description": "Maximum number of raw messages to include. Default: 20.",
                    "default": 20
                }
            },
            "required": [],
        },
        result_type="string",
    )
