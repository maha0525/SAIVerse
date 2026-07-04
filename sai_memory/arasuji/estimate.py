"""Chronicle 一括生成の費用見積もりロジック（単一の真実の源）。

api/routes/people/arasuji.py の GET .../arasuji/cost-estimate (UI 用) と
scripts/arasuji/build_arasuji_core.py の --estimate (CLI 用) の両方が
この関数を呼ぶ。計算式を2箇所で保守すると食い違うため、ロジックはここに一本化する。

未処理メッセージ数・バッチ数・統合コール数・概算コストの計算方法は
api/routes/people/arasuji.py:estimate_chronicle_cost の実装をそのまま踏襲。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class ChronicleCostEstimate:
    """Chronicle 一括生成の費用見積もり結果。"""

    total_messages: int
    processed_messages: int
    unprocessed_messages: int
    estimated_llm_calls: int
    level1_calls: int
    consolidation_calls: int
    estimated_cost_usd: float
    model_name: str
    is_free_tier: bool
    batch_size: int
    consolidation_size: int
    currency: str = "USD"


def estimate_chronicle_generation_cost(
    conn: sqlite3.Connection,
    *,
    batch_size: int,
    consolidation_size: int,
    model_name: str,
) -> ChronicleCostEstimate:
    """未処理メッセージから Chronicle を生成した場合の費用を見積もる。

    LLM は一切呼ばない（見積もりのみ、既存の Chronicle エントリ・メッセージ数
    から算出する）。

    Args:
        conn: persona の memory.db 接続
        batch_size: Lv1 生成のバッチサイズ（メッセージ数/コール）
        consolidation_size: 上位統合のバッチサイズ（エントリ数/コール）
        model_name: 見積もり対象モデル名（pricing 有無で is_free_tier を判定）
    """
    from sai_memory.memory.storage import count_messages
    from sai_memory.arasuji.storage import (
        get_total_message_count,
        count_entries_by_level,
        get_max_level,
    )
    from saiverse.model_configs import get_model_pricing

    total_messages = count_messages(conn)
    processed_messages = get_total_message_count(conn)

    # Calculate qualifying unprocessed messages using the same
    # contiguous-run logic as generate_unprocessed().  Messages in
    # runs shorter than batch_size are skipped during generation,
    # so they should not be counted here either.
    _cur = conn.execute(
        "SELECT DISTINCT json_each.value "
        "FROM arasuji_entries, json_each(source_ids_json) "
        "WHERE level = 1"
    )
    _processed_ids = {row[0] for row in _cur.fetchall()}

    _msg_ids_cur = conn.execute("SELECT id FROM messages ORDER BY created_at ASC")
    _runs_lengths: list[int] = []
    _run_len = 0
    for (msg_id,) in _msg_ids_cur:
        if msg_id in _processed_ids:
            if _run_len > 0:
                _runs_lengths.append(_run_len)
                _run_len = 0
            continue
        _run_len += 1
    if _run_len > 0:
        _runs_lengths.append(_run_len)

    # Count only full batches within qualifying runs (incomplete
    # trailing batches are skipped by generate_from_messages).
    level1_calls = sum(n // batch_size for n in _runs_lengths if n >= batch_size)
    unprocessed = level1_calls * batch_size

    # Consolidation calls: every consolidation_size level-1 entries -> 1 level-2 call, etc.
    consolidation_calls = 0
    entries_at_level = level1_calls
    while entries_at_level >= consolidation_size:
        next_level_calls = math.ceil(entries_at_level / consolidation_size)
        consolidation_calls += next_level_calls
        entries_at_level = next_level_calls

    total_calls = level1_calls + consolidation_calls

    # --- Estimate episode context tokens ---
    # The reverse level promotion algorithm selects context entries from existing
    # Chronicles. Theoretical entry count:
    #   entries_at_max_level + (max_level - 1) * consolidation_size
    # Capped by max_entries (20 for Level 1, 10 for consolidation).
    current_max_level = get_max_level(conn)
    entries_by_level = count_entries_by_level(conn)
    existing_total = sum(entries_by_level.values())

    if current_max_level > 0:
        entries_at_max = entries_by_level.get(current_max_level, 0)
        theoretical_existing = entries_at_max + (current_max_level - 1) * consolidation_size
    else:
        theoretical_existing = 0

    # Project post-generation state: recalculate max_level from total Level 1 count
    existing_lv1 = entries_by_level.get(1, 0)
    total_lv1_after = existing_lv1 + level1_calls
    if total_lv1_after > 0:
        final_max_level = 1
        temp_count = total_lv1_after
        while temp_count >= consolidation_size:
            final_max_level += 1
            temp_count = math.ceil(temp_count / consolidation_size)
        theoretical_after = temp_count + max(0, final_max_level - 1) * consolidation_size
    else:
        theoretical_after = 0

    MAX_ENTRIES_LV1 = 20   # generator.py:167
    MAX_ENTRIES_CONS = 10  # generator.py:326
    ctx_start_lv1 = min(theoretical_existing, MAX_ENTRIES_LV1)
    ctx_end_lv1 = min(theoretical_after, MAX_ENTRIES_LV1)
    avg_context_lv1 = (ctx_start_lv1 + ctx_end_lv1) / 2

    ctx_start_cons = min(theoretical_existing + consolidation_size, MAX_ENTRIES_CONS)
    ctx_end_cons = min(theoretical_after, MAX_ENTRIES_CONS)
    avg_context_cons = (ctx_start_cons + ctx_end_cons) / 2

    # Average tokens per context entry (from existing Chronicle content)
    row = conn.execute("SELECT AVG(LENGTH(content)) FROM arasuji_entries").fetchone()
    avg_content_chars = row[0] if row and row[0] else None
    if avg_content_chars and existing_total > 0:
        avg_entry_tokens = avg_content_chars / 3.5  # Conservative CJK/English estimate
    else:
        avg_entry_tokens = 50  # Default for first-time generation (~3-5 sentences)

    context_tokens_lv1 = avg_context_lv1 * avg_entry_tokens
    context_tokens_cons = avg_context_cons * avg_entry_tokens

    # --- Estimate Memopedia context tokens (Level 1 only) ---
    memopedia_tokens = 0
    try:
        from sai_memory.memopedia import Memopedia, init_memopedia_tables
        init_memopedia_tables(conn)
        memopedia = Memopedia(conn)
        text = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
        if text and text != "(まだページはありません)":
            memopedia_tokens = len(text) / 3.5
    except Exception:
        pass  # Memopedia not initialized -> 0

    # --- Estimate cost ---
    pricing = get_model_pricing(model_name)
    is_free_tier = pricing is None
    estimated_cost = 0.0

    if pricing and total_calls > 0:
        input_rate = pricing.get("input_per_1m_tokens", 0)
        output_rate = pricing.get("output_per_1m_tokens", 0)

        # Level 1: messages + prompt + episode context + Memopedia
        avg_input_lv1 = (
            batch_size * 200  # ~200 tokens/message (mixed CJK/English)
            + 500             # prompt instructions overhead
            + context_tokens_lv1
            + memopedia_tokens
        )
        # Level 2+: arasuji text + prompt + episode context (no Memopedia)
        avg_input_cons = (
            consolidation_size * avg_entry_tokens  # arasuji entries as input
            + 500                                   # prompt instructions overhead
            + context_tokens_cons
        )
        avg_output_per_call = 400  # ~3-5 sentence summary

        total_input = level1_calls * avg_input_lv1 + consolidation_calls * avg_input_cons
        total_output = total_calls * avg_output_per_call
        estimated_cost = (
            (total_input / 1_000_000) * input_rate
            + (total_output / 1_000_000) * output_rate
        )

    return ChronicleCostEstimate(
        total_messages=total_messages,
        processed_messages=processed_messages,
        unprocessed_messages=unprocessed,
        estimated_llm_calls=total_calls,
        level1_calls=level1_calls,
        consolidation_calls=consolidation_calls,
        estimated_cost_usd=round(estimated_cost, 6),
        model_name=model_name,
        is_free_tier=is_free_tier,
        batch_size=batch_size,
        consolidation_size=consolidation_size,
        currency=pricing.get("currency", "USD") if pricing else "USD",
    )
