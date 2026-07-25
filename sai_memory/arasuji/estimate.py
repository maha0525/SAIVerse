"""Chronicle 一括生成の費用見積もりロジック（単一の真実の源）。

api/routes/people/arasuji.py の GET .../arasuji/cost-estimate (UI 用) と
scripts/arasuji/build_arasuji_core.py の --estimate (CLI 用) の両方が
この関数を呼ぶ。計算式を2箇所で保守すると食い違うため、ロジックはここに一本化する。

W4 (体験の構造 工程(2)) で生成経路が episode 整列チャンクに世代交代した:
見積もりも同じ整列計画 (sai_memory/arasuji/alignment.plan_alignment) を dry で
呼び、生成経路と**同じ計画**から LLM コール数を数える (一点管理)。旧 20 件
固定バッチ (batch_size / consolidation_size) の数え方は廃止。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple


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
    # W4: チャンク内訳 (LLM を使わないチャンクは費用ゼロだが件数は見せる)
    chunks_identity: int = 0
    chunks_episode: int = 0
    currency: str = "USD"


def estimate_chronicle_generation_cost(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    episode_digests: Optional[Mapping[str, Tuple[str, str]]] = None,
) -> ChronicleCostEstimate:
    """未処理メッセージから Chronicle を生成した場合の費用を見積もる。

    LLM は一切呼ばない（整列計画と既存エントリ統計から算出する）。

    Args:
        conn: persona の memory.db 接続
        model_name: 見積もり対象モデル名（pricing 有無で is_free_tier を判定）
        episode_digests: digest 確定済み episode の index
            (session_lifecycle._collect_episode_digests と同形)。渡されない
            場合は空 — digest 済み範囲も LLM チャンクとして数えるため、
            見積もりは過大側 (安全側) に倒れる。
    """
    from sai_memory.arasuji.alignment import (
        chronicle_band_base,
        chronicle_band_budget,
        chronicle_min_digest_chars,
        plan_alignment,
    )
    from sai_memory.arasuji.storage import (
        count_entries_by_level,
        get_total_message_count,
    )
    from sai_memory.memory.storage import count_messages, get_messages_for_chronicle
    from saiverse.model_configs import get_model_pricing

    total_messages = count_messages(conn)
    processed_messages = get_total_message_count(conn)

    # 生成経路 (generate_chronicle) と同じ計画を作る。
    all_messages = get_messages_for_chronicle(conn)
    cur = conn.execute(
        "SELECT DISTINCT json_each.value "
        "FROM arasuji_entries, json_each(source_ids_json) "
        "WHERE level = 1"
    )
    processed_ids = {row[0] for row in cur.fetchall()}
    plan = plan_alignment(
        all_messages,
        processed_ids,
        episode_digests or {},
        target_chars=chronicle_band_budget(),
        min_llm_chars=chronicle_min_digest_chars(),
    )

    level1_calls = plan.llm_calls
    summary = plan.summary
    unprocessed = plan.total_unprocessed

    # 列のあふれ束ね (統合 LLM) の予測: 実行 (bands.run_band_overflow) と同じ
    # 選定ロジックの dry 実行 — 既存の未統合の列 + 新規チャンクの実 coverage で
    # 判定する (Codex W4 #9。新規数だけの近似は既存 backlog を見落とす)。
    from sai_memory.arasuji.bands import plan_band_overflow
    base = chronicle_band_base()
    try:
        consolidation_calls = plan_band_overflow(
            conn,
            extra_leaves=[
                (
                    c.coverage_chars,
                    min((m.created_at for m in c.messages), default=None),
                    max((m.created_at for m in c.messages), default=None),
                )
                for c in plan.chunks
            ],
        )
    except Exception:
        consolidation_calls = 0

    total_calls = level1_calls + consolidation_calls

    # --- 文脈トークンの概算 (プロンプトに入る既存 Chronicle 文脈) ---
    entries_by_level = count_entries_by_level(conn)
    existing_total = sum(entries_by_level.values())
    row = conn.execute("SELECT AVG(LENGTH(content)) FROM arasuji_entries").fetchone()
    avg_content_chars = row[0] if row and row[0] else None
    if avg_content_chars and existing_total > 0:
        avg_entry_tokens = avg_content_chars / 3.5  # Conservative CJK/English estimate
    else:
        avg_entry_tokens = 50  # Default for first-time generation (~3-5 sentences)

    # get_episode_context_for_timerange の max_entries (executor: 20 / bands: 10)
    context_tokens_lv1 = min(existing_total, 20) * avg_entry_tokens
    context_tokens_cons = min(existing_total, 10) * avg_entry_tokens

    # --- Memopedia 文脈トークン (Level 1 のみ) ---
    memopedia_tokens = 0.0
    try:
        from sai_memory.memopedia import Memopedia, init_memopedia_tables
        init_memopedia_tables(conn)
        memopedia = Memopedia(conn)
        text = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
        if text and text != "(まだページはありません)":
            memopedia_tokens = len(text) / 3.5
    except Exception:
        pass  # Memopedia not initialized -> 0

    # --- 費用 ---
    pricing = get_model_pricing(model_name)
    is_free_tier = pricing is None
    estimated_cost = 0.0

    if pricing and total_calls > 0:
        input_rate = pricing.get("input_per_1m_tokens", 0)
        output_rate = pricing.get("output_per_1m_tokens", 0)

        # llm_batch チャンクの入力 = 被覆生ログ + プロンプト + 文脈 + Memopedia。
        # チャンクは標準被覆 U 字 (端数もあるため plan から実測合計を使う)。
        llm_chunk_chars = sum(
            c.coverage_chars for c in plan.chunks if c.kind == "batch"
        )
        avg_input_lv1_total = (
            llm_chunk_chars / 3.5
            + level1_calls * (500 + context_tokens_lv1 + memopedia_tokens)
        )
        avg_input_cons = (
            base * avg_entry_tokens  # 束ねる子 digest 群
            + 500                    # prompt instructions overhead
            + context_tokens_cons
        )
        avg_output_per_call = 400  # ~3-5 sentence summary

        total_input = avg_input_lv1_total + consolidation_calls * avg_input_cons
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
        chunks_identity=summary["chunks_identity"],
        chunks_episode=summary["chunks_episode"],
        currency=pricing.get("currency", "USD") if pricing else "USD",
    )
