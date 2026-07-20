"""Arasuji (Chronicle) 級 1 生成の共有部品。

W4 (体験の構造 工程(2)) で旧 20 件固定バッチ経路 (ArasujiGenerator /
maybe_consolidate / gap-fill / dismantle) は撤去された — 生成経路の後継は
sai_memory/arasuji/alignment.py (整列計画) + executor.py (チャンク実行) +
bands.py (帯あふれ束ね)。

本モジュールに残るのは:

- :func:`generate_level1_arasuji` — 単一エントリの再生成
  (UI の regenerate → scripts/arasuji/build_arasuji_core.regenerate_entry_from_messages)
  が使う級 1 生成の一回分。
- プロンプト整形・usage 記録のユーティリティ (_format_* / _record_llm_usage)
  — executor / bands / note_extractor / note_organizer と共有。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sai_memory.memory.storage import Message
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    create_entry,
    get_leaf_entries_by_level,
    get_max_level,
)

LOGGER = logging.getLogger(__name__)


def _record_llm_usage(client, persona_id: Optional[str], node_type: str) -> None:
    """Record LLM usage from the client to usage tracker."""
    try:
        usage = client.consume_usage()
        if usage:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                node_type=node_type,
                category="memory_weave_generate",
            )
    except Exception as e:
        LOGGER.warning(f"Failed to record chronicle usage: {e}")

# Default settings
DEFAULT_BATCH_SIZE = 20  # messages per level-1 arasuji
DEFAULT_CONSOLIDATION_SIZE = 10  # entries per higher-level arasuji


def _format_timestamp(ts: Optional[int]) -> str:
    """Format Unix timestamp to readable string."""
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _format_messages_for_prompt(messages: List[Message], *, include_timestamp: bool = True) -> str:
    """Format messages for the arasuji prompt.

    Args:
        messages: Messages to format
        include_timestamp: If False, omit timestamps from output
    """
    lines: List[str] = []
    for msg in messages:
        role = msg.role
        if role == "model":
            role = "assistant"
        content = (msg.content or "").strip()
        if not content:
            continue
        if include_timestamp:
            ts_str = _format_timestamp(msg.created_at)
            lines.append(f"[{ts_str}] [{role}]: {content}")
        else:
            lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


def _format_entries_for_prompt(entries: List[ArasujiEntry], *, include_timestamp: bool = True) -> str:
    """Format arasuji entries for consolidation prompt."""
    lines: List[str] = []
    for i, entry in enumerate(entries, 1):
        if include_timestamp:
            start = _format_timestamp(entry.start_time)
            end = _format_timestamp(entry.end_time)
            lines.append(f"### あらすじ {i} ({start} ~ {end})")
        else:
            lines.append(f"### あらすじ {i}")
        lines.append(entry.content)
        lines.append("")
    return "\n".join(lines)


def _get_context_summaries(conn: sqlite3.Connection, current_level: int, *, include_timestamp: bool = True) -> str:
    """Get context summaries from higher levels for generation context.

    Retrieves unconsolidated entries from levels above the current level
    to provide context about what happened before.
    """
    context_parts: List[str] = []
    max_level = get_max_level(conn)

    # Start from highest level down to current_level + 1
    for level in range(max_level, current_level, -1):
        entries = get_leaf_entries_by_level(conn, level)
        if entries:
            # Calculate messages per entry at this level
            # Level 1 = batch_size, Level 2 = batch_size * consolidation_size, etc.
            context_parts.append(f"## レベル{level}のあらすじ（より大きな流れ）")
            for entry in entries:
                if include_timestamp:
                    start = _format_timestamp(entry.start_time)
                    end = _format_timestamp(entry.end_time)
                    context_parts.append(f"【{start} ~ {end}】")
                context_parts.append(entry.content)
                context_parts.append("")

    return "\n".join(context_parts) if context_parts else ""


def generate_level1_arasuji(
    client,
    conn: sqlite3.Connection,
    messages: List[Message],
    *,
    dry_run: bool = False,
    include_timestamp: bool = True,
    memopedia_context: Optional[str] = None,
    debug_log_path: Optional[Path] = None,
    persona_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    origin_track_id: Optional[str] = None,
    is_incomplete: bool = False,
    track_title: Optional[str] = None,
    track_intent: Optional[str] = None,
) -> Optional[ArasujiEntry]:
    """Generate a level-1 arasuji from messages.

    Args:
        client: LLM client with generate() method
        conn: Database connection
        messages: Messages to summarize
        dry_run: If True, don't save to database
        include_timestamp: If False, omit timestamps from prompt (useful when dates are unreliable)
        memopedia_context: Optional semantic memory context (page titles, summaries, keywords)
        origin_track_id: Track Chronicle 用 (v0.32)。set すると Track 紐付き entry として保存される。
                         set のとき抽出プロンプトを Track 目的駆動に切り替える
        is_incomplete: バッチサイズ未満で作る一時 Lv1 のフラグ (Track Chronicle 用)
        track_title / track_intent: Track Chronicle 抽出時にプロンプトに入れる Track 識別情報

    Returns:
        Created ArasujiEntry or None on failure
    """
    if not messages:
        return None

    # Extract time range from messages first (needed for temporal isolation)
    start_time = min(msg.created_at for msg in messages) if messages else None
    end_time = max(msg.created_at for msg in messages) if messages else None

    # Get episode context BEFORE this time range (temporal isolation)
    # This ensures we only see past Chronicles, not future ones during regeneration
    if start_time and end_time:
        from sai_memory.arasuji.context import get_episode_context_for_timerange
        context = get_episode_context_for_timerange(
            conn,
            start_time=start_time,
            end_time=end_time,
            max_entries=20
        )
    else:
        context = ""

    # Format messages
    conversation = _format_messages_for_prompt(messages, include_timestamp=include_timestamp)
    if not conversation.strip():
        return None

    # Build prompt — Track Chronicle と General Chronicle で抽出視点が異なる (v0.32)。
    # General: 出来事のあらすじ。Track: Track 目的駆動の作業遂行情報抽出。
    is_track_chronicle = origin_track_id is not None

    if is_track_chronicle:
        title_str = track_title or "(無題)"
        intent_str = (track_intent or "").strip()
        prompt_parts = [
            "あなたはペルソナ自身の記憶整理の頭脳です。"
            f"以下は、トラック「{title_str}」での作業履歴の一部です。",
            "このトラックの目的に沿って、後で再開した時に作業を続けるために必要な情報を抽出してください。",
            "",
        ]
        if intent_str:
            prompt_parts.extend([
                "## このトラックの意図",
                intent_str,
                "",
            ])
    else:
        prompt_parts = [
            "あなたは記憶の記録者です。以下の会話から、出来事のあらすじを書いてください。",
            "",
        ]

    if context:
        prompt_parts.extend([
            "## これまでの流れ（参考）",
            context,
            "",
        ])

    if memopedia_context:
        prompt_parts.extend([
            "## 意味記憶（人物・用語の背景情報）",
            memopedia_context,
            "",
        ])

    if is_track_chronicle:
        prompt_parts.extend([
            "## 今回まとめる範囲のメッセージ",
            conversation,
            "",
            "## 指示",
            "- このトラックでの「作業遂行情報」を簡潔に抽出する",
            "- 含めるべき要素: 計画 / 完了済みの作業 / 進行中の課題 / 待ち事項・未解決の問い / 結論・決定",
            "- このトラックの目的と無関係な雑談・脱線は省略する",
            "- 固有名詞・参照中のリソース・重要な数値は保持する",
            "- 段落構成は自由 (箇条書きでもよい)。長くなりすぎず、5〜10 文程度を目安",
            "- **日時情報（【2025-01-07 23:56 ~】など）は書かないでください**（自動で付与されます）",
            "- **「あらすじ」などの見出しは書かないでください**（本文のみ出力）",
            "",
            "作業遂行情報を日本語で書いてください。",
        ])
    else:
        prompt_parts.extend([
            "## 今回記録する会話",
            conversation,
            "",
            "## 指示",
            "- 3〜5文程度で、何が起きたか、誰と何を話したかを要約",
            "- 時系列の流れがわかるように書く",
            "- 固有名詞や重要な詳細は保持する",
            "- 感情や雰囲気も含める",
            "- 「〜について話した」のような抽象的な記述は避け、具体的に書く",
            "- **日時情報（【2025-01-07 23:56 ~】など）は書かないでください**（自動で付与されます）",
            "- **「あらすじ」などの見出しは書かないでください**（本文のみ出力）",
            "",
            "あらすじを日本語で書いてください。",
        ])

    prompt = "\n".join(prompt_parts)

    # Debug log: write prompt
    if debug_log_path:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"[CHRONICLE Lv1] {datetime.now().isoformat()}\n")
            f.write("=" * 80 + "\n")
            f.write("--- PROMPT ---\n")
            f.write(prompt)
            f.write("\n")

    # --- LLM call (no retry here; provider handles retry internally) ---
    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        _record_llm_usage(client, persona_id, "chronicle_level1")
    except Exception as e:
        LOGGER.error(f"LLM call failed for level-1 arasuji: {e}")
        from llm_clients.exceptions import LLMError
        if isinstance(e, LLMError):
            raise  # Propagate all LLM errors (empty, safety, timeout, etc.)
        return None

    # Debug log: write response
    if debug_log_path:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write("--- RESPONSE ---\n")
            f.write(response or "(empty)")
            f.write("\n")

    if not response or not response.strip():
        LOGGER.warning("Empty response from LLM for level-1 arasuji")
        return None

    content = response.strip()

    # Extract message IDs (time range already calculated at the beginning)
    source_ids = [msg.id for msg in messages]

    if dry_run:
        LOGGER.info(f"[DRY RUN] Would create level-1 arasuji: {content}")
        return ArasujiEntry(
            id="dry-run",
            level=1,
            content=content,
            source_ids=source_ids,
            start_time=start_time,
            end_time=end_time,
            source_count=len(messages),
            message_count=len(messages),
            parent_id=None,
            is_consolidated=False,
            created_at=0,
            origin_track_id=origin_track_id,
            is_incomplete=is_incomplete,
        )

    # --- DB save with retry (LLM result is already obtained, no re-call) ---
    max_db_retries = 3
    for attempt in range(max_db_retries):
        try:
            entry = create_entry(
                conn,
                level=1,
                content=content,
                source_ids=source_ids,
                start_time=start_time,
                end_time=end_time,
                source_count=len(messages),
                message_count=len(messages),
                thread_id=thread_id,
                origin_track_id=origin_track_id,
                is_incomplete=is_incomplete,
            )
            LOGGER.info(
                "Created level-1 arasuji: track=%s incomplete=%s content=%s",
                origin_track_id or "(general)", is_incomplete, content[:60],
            )
            return entry
        except Exception as e:
            LOGGER.warning(
                "DB save failed for level-1 arasuji (attempt %d/%d): %s",
                attempt + 1, max_db_retries, e,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt < max_db_retries - 1:
                time.sleep(2 ** attempt)

    LOGGER.error("DB save failed after %d attempts for level-1 arasuji", max_db_retries)
    return None


