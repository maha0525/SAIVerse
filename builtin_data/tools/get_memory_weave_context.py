"""Build Memory Weave context for LLM with Chronicle (Arasuji) and Memopedia."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

# Marker to identify Memory Weave context messages
MEMORY_WEAVE_CONTEXT_MARKER = "__memory_weave_context__"


def get_memory_weave_context(
    *,
    persona_id: Optional[str] = None,
    persona_dir: Optional[str] = None,
    max_chronicle_entries: int = 50,
    exclude_chronicle_entry_ids: Optional[List[str]] = None,
    raise_on_error: bool = False,
) -> List[Dict[str, Any]]:
    """Build Memory Weave context messages containing the Chronicle.

    This provides the persona with:
    - Chronicle: Recent events in detail, older events in summary (hierarchical)

    記憶アーキv2 §7.1 (2026-07-04): Memopedia 索引の head 常時掲示は**廃止**し、
    知識への接触はゾーン C の自動想起 (sea/auto_recall.py) + 深掘りスペルに一本化した。
    Memopedia を能動的なメモ帳として使うユーザー向けの後方互換 (per-persona トグル
    ``MEMOPEDIA_INDEX_ENABLED``、database/models.py) は、2026-07-14 に
    ``sea/head_pipeline/sections/memopedia_index.py`` の ``MemopediaIndexSection``
    へ一本化された（旧実装 ``_get_memopedia_context`` / ``include_memopedia`` 引数は
    本番から呼ばれない死にコードだったため削除済み）。
    ペルソナが明示的に開いたページ (memory_open → get_open_pages_content) は
    別機構なので、このトグルの影響を受けない。

    The context is inserted after the system prompt but before visual context
    and conversation history.

    Args:
        persona_id: Persona ID (auto-detected if not provided)
        persona_dir: Persona directory path (auto-detected if not provided)
        max_chronicle_entries: Max Chronicle entries. Chronicle は §6.2 の文字数
            予算制に移行したためこの値は安全弁の下限として扱われる (予算が主制御)。
        exclude_chronicle_entry_ids: head の Chronicle 枠から外すエントリ id。
            提示コンテキストの中で元の時系列位置に digest を差し込んでいる範囲を渡す
            (docs/intent/chronicle_eviction.md §6) — 同じあらすじが提示コンテキストと head の
            両方に出ると体験が二重化して時系列の錯覚を招くため。
        raise_on_error: True なら組み立て失敗を例外で伝える (既定は [] へ変換)。
            「成功した空」と「読取失敗」の区別が要る呼び出し側 (§15 preview の
            weave 差し替え) 用。

    Returns:
        List of messages to insert into context.
        Returns empty list if Memory Weave is not available.
    """
    # Get persona context
    from tools.context import get_active_persona_id, get_active_persona_path

    if persona_id is None:
        persona_id = get_active_persona_id()
    if not persona_id:
        LOGGER.debug("get_memory_weave_context: No active persona")
        return []

    # Try to get persona_dir from context if not provided
    if persona_dir is None:
        try:
            path_obj = get_active_persona_path()
            persona_dir = str(path_obj) if path_obj else None
        except Exception:
            LOGGER.warning("Failed to get active persona path", exc_info=True)
    
    if not persona_dir:
        LOGGER.debug("get_memory_weave_context: No persona dir")
        return []

    # Find memory.db
    memory_db_path = Path(persona_dir) / "memory.db"
    if not memory_db_path.exists():
        LOGGER.debug("get_memory_weave_context: memory.db not found at %s", memory_db_path)
        return []

    try:
        conn = sqlite3.connect(str(memory_db_path))

        # 1. Get Chronicle context (hierarchical episode memory)
        chronicle_text = _get_chronicle_context(
            conn, max_entries=max_chronicle_entries,
            exclude_entry_ids=set(exclude_chronicle_entry_ids or ()),
            raise_on_error=raise_on_error,
        )

        # 記憶アーキv2 §7.1: Memopedia 索引の head 常時掲示は既定で廃止 (自動想起 +
        # 深掘りスペルに一本化)。MEMOPEDIA_INDEX_ENABLED トグル (後方互換) の描画は
        # MemopediaIndexSection に一本化されているため、本関数はもう関与しない。
        conn.close()

        # Chronicle は独立した user message として流す (context preview が
        # __memory_weave_type__ で section ラベルを切り替えるため)
        messages: List[Dict[str, Any]] = []
        if chronicle_text:
            messages.append({
                "role": "user",
                "content": f"以下は、あなたの長期記憶（Chronicle: これまでの出来事）です。\n\n{chronicle_text}",
                "metadata": {
                    MEMORY_WEAVE_CONTEXT_MARKER: True,
                    "__memory_weave_type__": "chronicle",
                },
            })
        total_chars = sum(len(m["content"]) for m in messages)
        LOGGER.info(
            "get_memory_weave_context: Generated %d messages (%d chars total)",
            len(messages), total_chars,
        )
        return messages

    except Exception as exc:
        if raise_on_error:
            # 「成功した空」と「読取失敗」を呼び出し側が区別したい経路
            # (§15 preview の weave 差し替え等)。既定の [] 変換は、失敗を
            # 空 weave としてコミットさせてしまう (Codex 指摘 2026-07-30)。
            raise
        LOGGER.warning("get_memory_weave_context: Failed to build context: %s", exc)
        return []


def _get_chronicle_context(
    conn: sqlite3.Connection,
    max_entries: int = 50,
    exclude_entry_ids: Optional[set] = None,
    raise_on_error: bool = False,
) -> str:
    """Get Chronicle (Arasuji) context using hierarchical algorithm.

    Track Chronicle 時代の行 (origin_track_id 付き) は :func:`get_episode_context`
    の側で並びから外れる — 書き手は退役したが既存 DB には残っているため
    (track_retirement.md 住人 5)。

    記憶アーキv2 §6.2 (Phase 3, 2026-07-04): Chronicle の読み込みは件数上限から
    文字数予算制へ移行した。件数上限だと超過時に最古が黙って落ちる (不変条件
    §10-4) ため、``char_budget=USE_DEFAULT_BUDGET`` を渡して予算制を有効化する。既定
    予算は 20,000 字 / env ``SAIVERSE_CHRONICLE_CHAR_BUDGET`` で調整可。``max_entries``
    は安全弁として残す (予算制側が主制御)。
    """
    # import 文だけを ModuleNotFoundError で受ける — モジュール不在は
    # 「本当に無い」= 正当な空 (strict でも空のまま)。export 欠落や依存の
    # version skew は同じ import 文から**素の ImportError** で飛んでくるが、
    # それは不在ではなく壊れた状態なので汎用ハンドラへ流し、strict 時に
    # 再送出する。本体の実行中に飛ぶ ImportError も同様 (Codex 指摘
    # 2026-07-30 ×2巡)。
    try:
        from sai_memory.arasuji.context import (
            USE_DEFAULT_BUDGET,
            format_episode_context,
            get_episode_context,
        )
    except ModuleNotFoundError:
        LOGGER.debug("Chronicle module not available")
        return ""
    except ImportError as exc:
        if raise_on_error:
            raise
        LOGGER.warning("Chronicle module import is broken: %s", exc)
        return ""
    try:
        # 予算制が主制御なので max_entries は「暴走防止の安全弁」に格下げ。既定 50 の
        # ままだと 20万メッセージ級ユーザーで最古到達前に打ち切られ不変条件 §10-4 を
        # 破るため、予算制では十分大きい上限に引き上げる (件数ではなく予算で絞る)。
        context = get_episode_context(
            conn,
            max_entries=max(max_entries, 10_000),
            char_budget=USE_DEFAULT_BUDGET,
            exclude_entry_ids=exclude_entry_ids or None,
        )
        if not context:
            return ""

        return format_episode_context(context, include_level_info=True)
    except Exception as exc:
        if raise_on_error:
            raise  # 読取失敗を「成功した空」に変換しない (strict 経路)
        LOGGER.warning("Failed to get Chronicle context: %s", exc)
        return ""


# Tool definition for registry (optional, mainly used via runtime.py)
TOOL_DEF = {
    "name": "get_memory_weave_context",
    "description": "Build Memory Weave context containing Chronicle and Memopedia for LLM context.",
    "parameters": {
        "type": "object",
        "properties": {
            "max_chronicle_entries": {
                "type": "integer",
                "description": "Maximum number of Chronicle entries to include. Default: 50.",
            },
        },
    },
}
