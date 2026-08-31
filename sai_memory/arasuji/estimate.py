"""Chronicle 一括生成の費用見積もりロジック（単一の真実の源）。

api/routes/people/arasuji.py の GET .../arasuji/cost-estimate (UI 用) と
scripts/arasuji/build_arasuji_core.py の --estimate (CLI 用) の両方が
この関数を呼ぶ。計算式を2箇所で保守すると食い違うため、ロジックはここに一本化する。

見積もりは生成経路と同じ計画 (sai_memory/arasuji/alignment.plan_alignment /
sai_memory/arasuji/bands.plan_band_overflow) を dry で呼び、**同じ計画**から
LLM コール数を数える (一点管理)。
"""

from __future__ import annotations

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
    currency: str = "USD"


def estimate_chronicle_generation_cost(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    excluded_entry_ids: Optional[frozenset] = frozenset(),
    db_lock=None,
    compile_before: Optional[tuple] = None,
) -> ChronicleCostEstimate:
    """未処理メッセージから Chronicle を生成した場合の費用を見積もる。

    LLM は一切呼ばない（計画と既存エントリ統計から算出する）。

    Args:
        conn: persona の memory.db 接続
        model_name: 見積もり対象モデル名（pricing 有無で is_free_tier を判定）
        excluded_entry_ids: 圧縮区間として提示中の digest entry id 集合
            (生成経路と同じ集合を渡さないと束ねコールの見積もりが乖離する)。
            **None = 照会失敗 (fold の有無が不明)** — 生成経路が束ねを見送る
            のと同形に、束ねコールを 0 と見積もる。既定の空集合は
            「fold という概念ごと無い環境」(CLI / テスト) 用
        db_lock: 同じ DB を書く adapter がいる場合、その ``_db_lock``。見積もり
            自体は読むだけだが、Memopedia の初期化はテーブル作成の書き込みを伴う
            (docs/issues/memopedia_writers_bypass_adapter_lock.md)
        compile_before: 被覆補修の止め線 (arasuji_levels.md §16-2)。正典順序キー
            ``(created_at, rowid)`` で、これより新しいメッセージは計画に入れない
            — 温かい提示窓の下は編纂しない。絞りは生成経路
            (sea/session_lifecycle.generate_chronicle) と同じ
            ``clip_messages_before_position`` を通す (表示と実走が違う数を
            言ってはならない)。None = 上端なし (全域。CLI / 修復スクリプト用)
    """
    from sai_memory.arasuji.alignment import (
        chronicle_band_budget,
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
    if compile_before is not None:
        # 止め線 (§16-2): 生成経路と同じ関数で同じ範囲に絞る。
        from sai_memory.memory.storage import clip_messages_before_position
        all_messages = clip_messages_before_position(
            conn, all_messages, int(compile_before[0]), int(compile_before[1]),
        )
    cur = conn.execute(
        "SELECT DISTINCT json_each.value "
        "FROM arasuji_entries, json_each(source_ids_json) "
        "WHERE level = 1"
    )
    processed_ids = {row[0] for row in cur.fetchall()}
    plan = plan_alignment(
        all_messages,
        processed_ids,
        target_chars=chronicle_band_budget(),
    )

    level1_calls = plan.llm_calls
    unprocessed = plan.total_unprocessed

    # 束ね (統合 LLM) の予測: 実行 (bands.run_band_overflow) と同じ計画の
    # dry 実行 — 既存のレベル別の並び + 新規チャンク (レベル1 到着) で判定する。
    from sai_memory.arasuji.bands import EST_PARENT_CHARS, plan_band_overflow
    if excluded_entry_ids is None:
        # fold の有無が不明 — 生成経路は束ねを見送るので、見積もりも 0 が同形。
        consolidation_calls = 0
    else:
        try:
            consolidation_calls = plan_band_overflow(
                conn,
                extra_leaves=[
                    (
                        c.coverage_chars,
                        min((m.created_at for m in c.messages), default=None),
                        max((m.created_at for m in c.messages), default=None),
                        EST_PARENT_CHARS,
                    )
                    for c in plan.chunks
                ],
                excluded_entry_ids=set(excluded_entry_ids) or None,
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
        from sai_memory.memopedia import Memopedia
        # テーブルの用意は Memopedia のコンストラクタが**ロックの内側で**行う
        # (ここで先に呼ぶと同じ commit がロック外で走る)
        memopedia = Memopedia(conn, db_lock=db_lock)
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

        # チャンクの入力 = 被覆生ログ + プロンプト + 文脈 + Memopedia。
        llm_chunk_chars = sum(c.coverage_chars for c in plan.chunks)
        avg_input_lv1_total = (
            llm_chunk_chars / 3.5
            + level1_calls * (500 + context_tokens_lv1 + memopedia_tokens)
        )
        avg_input_cons = (
            10 * avg_entry_tokens    # 束ねる子 digest 群 (束は 10 個前後)
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
        currency=pricing.get("currency", "USD") if pricing else "USD",
    )
