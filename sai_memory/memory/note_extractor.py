"""Extract memory notes from conversation messages.

Runs at the same timing as Chronicle Lv1 generation (per batch of ~20 messages).
Uses a lightweight LLM to extract noteworthy knowledge as short bullet points.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sai_memory.memory.storage import (
    Message,
    MemoryNote,
    add_memory_notes,
    get_unresolved_notes,
)

LOGGER = logging.getLogger(__name__)


def _build_extraction_prompt(
    conversation: str,
    *,
    episode_context: str = "",
    memopedia_context: str = "",
    existing_notes: List[str] = (),
) -> str:
    """Build the full extraction prompt with all context sections."""
    parts = [
        "あなたは記憶の整理係です。以下の会話から、覚えておくべき新しい情報を箇条書きで抽出してください。",
        "",
        "## 抽出すべき情報のカテゴリ",
        "",
        "1. **未知の実世界情報**: ニュース、新製品、流行、技術の動向など、会話の中で言及された外部世界の新しい情報",
        "2. **ユーザーに関する新規情報**: 好み、興味、最近の出来事、知り合いに関する話、生活の変化など",
        "3. **AI自身に関する新たな情報**: 自分が表明した意見、やってみたいこと、自己認識に関する発言など",
        "4. **他AIの情報**: 同居AIの特技、エピソード、印象的な発言、性格の一面など",
        "",
    ]

    if episode_context:
        parts.extend([
            "## これまでの流れ（参考）",
            episode_context,
            "",
        ])

    if memopedia_context:
        parts.extend([
            "## 既存の知識ベース（Memopedia）",
            "以下のトピックは既に知識として記録済みです。これらと重複する情報は抽出しないでください。",
            memopedia_context,
            "",
        ])

    if existing_notes:
        parts.extend([
            "## 既にメモ済みの項目",
            "以下の項目は既にメモされています。同じ内容や類似する内容は抽出しないでください。",
        ])
        for note in existing_notes:
            parts.append(f"- {note}")
        parts.append("")

    parts.extend([
        "## 今回記録する会話",
        conversation,
        "",
        "## 指示",
        "",
        "- 各項目は1行の短い文にしてください（「〜ということ」「〜だと判明」のような形）",
        "- 上記の「既存の知識ベース」や「既にメモ済みの項目」と重複する情報は除外してください",
        "- 既に常識として知っているはずの情報は除外してください",
        "- 会話の中で明確に述べられた事実のみを抽出してください（推測や解釈は含めない）",
        "- 抽出すべき情報がない場合は空のリストを返してください",
        "- 日本語で出力してください",
        "",
        "## 出力形式",
        "",
        "以下のJSON形式で出力してください。",
        "```json",
        '["項目1", "項目2", "項目3"]',
        "```",
        "",
        "JSONのみを出力してください。",
    ])

    return "\n".join(parts)


def _format_messages(messages: List[Message]) -> str:
    """Format messages for the extraction prompt."""
    lines: List[str] = []
    for msg in messages:
        role = msg.role
        if role == "model":
            role = "assistant"
        content = (msg.content or "").strip()
        if not content:
            continue
        ts_str = datetime.fromtimestamp(msg.created_at).strftime("%Y-%m-%d %H:%M") if msg.created_at else "?"
        lines.append(f"[{ts_str}] [{role}]: {content}")
    return "\n\n".join(lines)


def _parse_notes_response(response: str) -> List[str]:
    """Parse LLM response into a list of note strings.

    Handles both clean JSON arrays and responses wrapped in markdown code blocks.
    """
    if not response:
        return []

    text = response.strip()

    # Strip markdown code block if present
    md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if item]
        return []
    except json.JSONDecodeError:
        LOGGER.warning("Failed to parse note extraction response as JSON: %s", text[:200])
        # Fallback: try to extract bullet points
        notes = []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith(("- ", "・", "* ")):
                notes.append(line.lstrip("-・* ").strip())
            elif line and not line.startswith(("{", "[", "]", "}", "`", "#")):
                notes.append(line)
        return notes


def extract_memory_notes(
    client,
    messages: List[Message],
    *,
    episode_context: str = "",
    memopedia_context: str = "",
    existing_notes: List[str] = (),
    persona_id: Optional[str] = None,
) -> List[str]:
    """Extract noteworthy knowledge from a batch of messages.

    Args:
        client: LLM client with generate() method (lightweight model preferred).
        messages: Batch of messages to extract from.
        episode_context: Chronicle context for surrounding events.
        memopedia_context: Existing Memopedia page tree (to avoid duplicates).
        existing_notes: List of already-captured note contents (to avoid duplicates).
        persona_id: Optional persona ID for usage tracking.

    Returns:
        List of extracted note strings.
    """
    if not messages:
        return []

    conversation = _format_messages(messages)
    if not conversation.strip():
        return []

    prompt = _build_extraction_prompt(
        conversation,
        episode_context=episode_context,
        memopedia_context=memopedia_context,
        existing_notes=existing_notes,
    )

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
    except Exception as e:
        LOGGER.warning("LLM call failed for memory note extraction: %s", e)
        return []

    # Track usage if the client supports it
    if persona_id and hasattr(client, "last_usage"):
        try:
            from sai_memory.arasuji.generator import _record_llm_usage
            _record_llm_usage(client, persona_id, "memory_note_extraction")
        except Exception:
            pass

    notes = _parse_notes_response(response or "")
    LOGGER.info("Extracted %d memory notes from %d messages", len(notes), len(messages))
    return notes


def extract_and_store_notes(
    client,
    conn: sqlite3.Connection,
    messages: List[Message],
    *,
    thread_id: str,
    source_pulse_id: Optional[str] = None,
    episode_context: str = "",
    memopedia_context: str = "",
    persona_id: Optional[str] = None,
) -> List[MemoryNote]:
    """Extract and store memory notes in one step.

    Convenience function that combines extraction and storage.
    Automatically fetches existing unresolved notes from the DB to avoid duplicates.

    Args:
        client: LLM client with generate() method.
        conn: Database connection for storage.
        messages: Batch of messages to extract from.
        thread_id: Active thread ID.
        source_pulse_id: Optional pulse ID these messages belong to.
        episode_context: Chronicle context for surrounding events.
        memopedia_context: Existing Memopedia page tree.
        persona_id: Optional persona ID for usage tracking.

    Returns:
        List of created MemoryNote objects.
    """
    # Fetch existing unresolved notes to pass as dedup context
    existing = get_unresolved_notes(conn, limit=200)
    existing_contents = [n.content for n in existing]

    notes = extract_memory_notes(
        client,
        messages,
        episode_context=episode_context,
        memopedia_context=memopedia_context,
        existing_notes=existing_contents,
        persona_id=persona_id,
    )
    if not notes:
        return []

    source_time = max(m.created_at for m in messages) if messages else None

    stored = add_memory_notes(
        conn,
        thread_id=thread_id,
        notes=notes,
        source_pulse_id=source_pulse_id,
        source_time=source_time,
    )
    LOGGER.info(
        "Stored %d memory notes (thread=%s, pulse=%s)",
        len(stored), thread_id, source_pulse_id,
    )
    return stored


def make_batch_callback(
    client,
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    memopedia_context: str = "",
    persona_id: Optional[str] = None,
) -> Callable[[List[Message]], None]:
    """Create a batch_callback for ArasujiGenerator that extracts memory notes.

    Episode context is computed per-batch from the batch's time range.
    Existing unresolved notes are fetched automatically per-batch for dedup.

    Usage:
        generator = ArasujiGenerator(client, conn, ...)
        callback = make_batch_callback(note_client, conn, thread_id="main",
                                       memopedia_context=memopedia_ctx)
        generator.generate_from_messages(messages, batch_callback=callback)

    Args:
        client: LLM client for note extraction (can be different from Chronicle client).
        conn: Database connection.
        thread_id: Active thread ID.
        memopedia_context: Existing Memopedia page tree (passed once at creation).
        persona_id: Optional persona ID for usage tracking.

    Returns:
        A callback function compatible with ArasujiGenerator.generate_from_messages().
    """
    def callback(batch_messages: List[Message]) -> None:
        try:
            # Compute episode context for this batch's time range
            ep_ctx = ""
            if batch_messages:
                start_time = min(m.created_at for m in batch_messages)
                end_time = max(m.created_at for m in batch_messages)
                try:
                    from sai_memory.arasuji.context import get_episode_context_for_timerange
                    ep_ctx = get_episode_context_for_timerange(
                        conn, start_time=start_time, end_time=end_time, max_entries=10,
                    )
                except Exception as exc:
                    LOGGER.debug("Could not get episode context for notes: %s", exc)

            extract_and_store_notes(
                client,
                conn,
                batch_messages,
                thread_id=thread_id,
                episode_context=ep_ctx,
                memopedia_context=memopedia_context,
                persona_id=persona_id,
            )
        except Exception as exc:
            LOGGER.warning("Memory note extraction failed for batch: %s", exc)

    return callback
