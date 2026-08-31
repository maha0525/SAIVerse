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
    # 吸収に伴う上位あらすじ再生成の見込み (Codex レビュー 2026-08-31 採用 4)。
    # consolidation_calls (band 束ね) と**混ぜない** — CLI 実行は
    # consolidation_calls を run_band_overflow の max_folds (承認済み束ね回数の
    # 上限) にそのまま渡すため、ここに再生成を混ぜると束ねの上限が膨らむ。
    upper_regen_calls: int = 0


def estimate_chronicle_generation_cost(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    excluded_entry_ids: Optional[frozenset] = frozenset(),
    db_lock=None,
    compile_before: Optional[tuple] = None,
    tail_fold_estimator=None,
    messages_override=None,
    truncate_limit: int = 0,
    skip_absorption: bool = False,
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
        tail_fold_estimator: 末尾の未被覆帯 (anchor 引き戻しの対象 —
            arasuji_tiny_run_absorption 裁定 5 改訂) に伴う即時畳みの見込みを
            返す callback ``(first_message_id, run_ids) -> (calls, material)``。
            引き戻し自体は 0 LLM コールだが、戻した窓が上限を超えると補修
            ジョブ内で畳みが走るため、その分を見積もりに含める。API 経路が
            sea/coverage_repair.estimate_tail_rewind_fold を包んで渡す
            (実行と同じ判定関数)。None = 数えない (CLI / オフライン —
            スクリプトは引き戻しを実行しないので 0 が実態と一致する)
        messages_override: 編纂候補列を呼び出し側の実物で差し替える (CLI の
            --thread 絞り込み等 — Codex 三巡 F4: 見積もりと実行が同じ入力から
            同じ計画を作る)。None = 従来どおりここで全量取得。
        truncate_limit: CLI の --limit (>0 で truncate_plan と同じ切り詰めを
            見積もり側でも適用する)。0 = 切り詰めなし。
        skip_absorption: CLI の --limit>0 実行と同形 — 吸収を数えない (上位
            再生成は前回の未完了の flush 分だけ数える)。
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
    all_messages = (
        list(messages_override) if messages_override is not None
        else get_messages_for_chronicle(conn)
    )
    if compile_before is not None:
        # 止め線 (§16-2): 生成経路と同じ関数で同じ範囲に絞る。created_at は
        # None (NULL = 全ての実時刻より前) がありうるので写像せずそのまま渡す。
        from sai_memory.memory.storage import clip_messages_before_position
        all_messages = clip_messages_before_position(
            conn, all_messages, compile_before[0], int(compile_before[1]),
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
    if truncate_limit > 0:
        # CLI の --limit と同じ切り詰め (実行側 truncate_plan と同一関数)。
        from sai_memory.arasuji.alignment import truncate_plan
        plan = truncate_plan(plan, truncate_limit)
    unprocessed = plan.total_unprocessed

    # 極小 run の隣人吸収 (arasuji_tiny_run_absorption): 生成経路 (generate_
    # chronicle の全量計画 / build_arasuji) と同じ分割・同じ吸収計画で数える —
    # 表示と実走が違う数を言ってはならない (§16-2 と同じ裁定)。
    # excluded_entry_ids=None (fold 不明) の回は生成側が吸収を見送るので、
    # 見積もりも吸収 0 (前回の未完了の flush だけ数える) が同形。
    from sai_memory.arasuji.absorption import (
        list_stale_upper_ids,
        list_sweep_regen_ids,
        plan_absorption,
        split_plan_for_absorption,
        uncovered_tail_zone,
    )
    plan, tiny_chunks = split_plan_for_absorption(
        plan, target_chars=chronicle_band_budget(),
    )

    # 末尾の未被覆帯 = anchor 引き戻しの対象 (0 LLM コール)。ただし戻した窓が
    # 上限を超えると補修ジョブ内で畳みが走る — その分を callback で数える。
    tail_fold_calls = 0
    tail_fold_material = 0
    zone_run_ids, zone_first_id, _zone_idx = uncovered_tail_zone(
        tiny_chunks, all_messages,
    )
    if zone_first_id is not None and tail_fold_estimator is not None:
        # 見込みの計算失敗は伝播 (Codex 四巡 G2 — 「表示 ≥ 実走」。0 で
        # ごまかすと引き戻し後の即時畳みが表示なしで課金される)。
        tail_fold_calls, tail_fold_material = tail_fold_estimator(
            zone_first_id, zone_run_ids,
        )
    absorption_calls = 0
    absorption_material_chars = 0
    counted_upper_ids: list = []
    if skip_absorption:
        # CLI の --limit>0 実行と同形: 吸収は見送り。前回の未完了 (content_
        # stale) の flush だけは実行側 (run_absorption) が無条件に行うので数える。
        counted_upper_ids = list(list_stale_upper_ids(conn))
    elif excluded_entry_ids is None or not tiny_chunks:
        counted_upper_ids = list(list_stale_upper_ids(conn))
    else:
        # 計画の例外は**伝播させる** (Codex 四巡 G1 — 「表示 ≥ 実走」)。実行側
        # (generate_chronicle) は計画例外で吸収を見送る = 承認より少なく走る
        # 方向で無害だが、見積もり側が 0 でごまかすと「表示 < 実走」になりうる。
        # cost-estimate エンドポイントはこの例外で 500 に止まる (止め線の
        # CeilingResolutionError fail-closed と同じ前例)。テーブル不在だけは
        # 従来の縮退 (吸収 0 + 未完了 flush の計上) を許す。
        try:
            absorption_plan = plan_absorption(
                conn, tiny_chunks, all_messages, processed_ids,
                target_chars=chronicle_band_budget(),
                excluded_entry_ids=frozenset(excluded_entry_ids),
            )
        except sqlite3.OperationalError as exc:
            from sai_memory.arasuji.storage import is_missing_table_error
            if not is_missing_table_error(exc):
                raise
            counted_upper_ids = list(list_stale_upper_ids(conn))
        else:
            absorption_calls = len(absorption_plan.items)
            absorption_material_chars = absorption_plan.material_chars
            counted_upper_ids = list(absorption_plan.stale_upper_ids)

    # sweep (壊れた親の救済) が実行時に**新しく見つける**分を足す。実行側
    # (run_absorption / generate_chronicle) は走る前に _sweep_broken_parents を
    # 無条件に回し、壊れた親と先祖へ content_stale を付ける — その全部が後段の
    # flush で語り直される = LLM コール。検知は読み取り専用のクエリなので、
    # 見積もりも**同じ関数** (list_sweep_regen_ids → list_broken_parent_ids) で
    # 数える (検知クエリを二枚にしない)。既に stale / 吸収で汚れる分として
    # 数えた id は除く。照会の例外は伝播させる (G1・G2 と同じ「表示 ≥ 実走」。
    # 実行側も J4 で sweep の例外を握り潰さず failed にする)。
    _already_counted = set(counted_upper_ids)
    sweep_regen_ids = [
        eid for eid in list_sweep_regen_ids(conn) if eid not in _already_counted
    ]
    upper_regen_calls = len(counted_upper_ids) + len(sweep_regen_ids)

    level1_calls = plan.llm_calls + absorption_calls + tail_fold_calls

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

    # 上位あらすじの連鎖再生成 (吸収の裁定 3) は独立に数える —
    # consolidation_calls へ混ぜると CLI の max_folds (束ねの上限) が膨らむ。
    total_calls = level1_calls + consolidation_calls + upper_regen_calls

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
        # 吸収の合体生成の材料 (run + 開き直す隣人の生ログ) と、引き戻し後の
        # 即時畳みの材料も同じ性質の入力。
        llm_chunk_chars = (
            sum(c.coverage_chars for c in plan.chunks)
            + absorption_material_chars
            + tail_fold_material
        )
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

        # 上位再生成は束ねと同じプロンプト組成なので、入力の見込みも同じ枠。
        total_input = avg_input_lv1_total + (
            consolidation_calls + upper_regen_calls
        ) * avg_input_cons
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
        upper_regen_calls=upper_regen_calls,
        estimated_cost_usd=round(estimated_cost, 6),
        model_name=model_name,
        is_free_tier=is_free_tier,
        currency=pricing.get("currency", "USD") if pricing else "USD",
    )
