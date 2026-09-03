from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from typing import Dict, List, Optional
from api.deps import get_manager
from saiverse.data_paths import get_persona_memory_db
from .models import (
    ArasujiStatsResponse, ArasujiListResponse, ArasujiEntryItem, SourceMessageItem,
    GenerateArasujiRequest, GenerationJobStatus, ChronicleCostEstimate,
    MessagesByIdsRequest, UpdateArasujiEntryRequest,
)
import sqlite3
import logging
import uuid
import time
import threading

from llm_clients.exceptions import LLMError

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# -----------------------------------------------------------------------------
# In-memory job store for async generation
# -----------------------------------------------------------------------------
_generation_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _create_job(persona_id: str) -> str:
    """Create a new generation job and return its ID."""
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _generation_jobs[job_id] = {
            "persona_id": persona_id,
            "status": "pending",
            "progress": 0,
            "total": 0,
            "message": "Initializing...",
            "entries_created": 0,
            # 畳み・補修そのものは成功したが、付随する処理が完了しなかったときの
            # 添え書き (完了表示に併記する。失敗扱いにはしない)。
            "warning": None,
            "error": None,
            "error_code": None,
            "error_detail": None,
            "error_meta": None,
            "created_at": time.time(),
        }
    return job_id


def _update_job(job_id: str, **kwargs):
    """Update job status."""
    with _jobs_lock:
        if job_id in _generation_jobs:
            _generation_jobs[job_id].update(kwargs)


def _get_job(job_id: str) -> Optional[dict]:
    """Get job status."""
    with _jobs_lock:
        return _generation_jobs.get(job_id, {}).copy() if job_id in _generation_jobs else None


def _get_arasuji_db(persona_id: str):
    """Get database connection for arasuji tables.

    **テーブルの用意で倒れたら、接続をここで閉じてから例外を投げる。** 開いた
    まま投げると呼び出し側に接続が渡らないので、各エンドポイントの
    ``finally: conn.close()`` に到達せず誰も閉じられない。``init_arasuji_tables``
    は VIEW の作り直しと旧 ``arasuji_entries`` からの移行を伴う書き込みなので、
    閉じられない接続が書き込みロックを握ったままになり、**同じ memory.db を
    触る Memopedia 側が待たされる** (2026-09-02、Chronicle を開いた後に
    Memopedia が返らなくなる報告の調査で発見)。
    """
    from pathlib import Path
    import sqlite3
    from sai_memory.arasuji.storage import init_arasuji_tables

    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        init_arasuji_tables(conn)
    except BaseException:
        conn.close()
        raise
    return conn

def _get_message_number_map(conn: sqlite3.Connection) -> dict:
    """Build a mapping of message_id -> row number (1-indexed) matching build_arasuji.py order.

    Messages are ordered globally by created_at ASC across all threads.
    This ensures consistent chronological ordering where message #1 is always the oldest.
    """
    cur = conn.execute("""
        SELECT id FROM messages ORDER BY created_at ASC, rowid ASC
    """)

    msg_num_map = {}
    for msg_num, (msg_id,) in enumerate(cur.fetchall(), start=1):
        msg_num_map[msg_id] = msg_num

    return msg_num_map

@router.get("/{persona_id}/arasuji/cost-estimate", response_model=ChronicleCostEstimate)
def estimate_chronicle_cost(
    persona_id: str,
    batch_size: Optional[int] = None,
    consolidation_size: Optional[int] = None,
    manager=Depends(get_manager),
):
    """Estimate the cost of generating Chronicle for unprocessed messages.

    W4: 見積もりは生成経路と同じ episode 整列計画 (plan_alignment) から数える。
    ``batch_size`` / ``consolidation_size`` クエリパラメータは廃止 — 受理して
    無視する (旧 frontend 互換)。

    §16 (2026-08-31): 止め線 (温かい anchor の最古位置) より新しいメッセージは
    数えない — 生成 (repair モード / generate_chronicle の全量計画) と同じ
    関数 (resolve_compile_ceiling + clip_messages_before_position) で絞る。
    unprocessed_messages はそのまま「あらすじになっていない過去」の件数になる。
    """
    import os
    from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        # 止め線 (§16-2)。lifecycle が無い環境 (部分構築のテスト等) は上端なし。
        # 解決失敗は 500 で止める (fail-closed) — 「読めなかった」を「上端なし」
        # へ潰すと、表示が全域の数を言い、実走 (repair) 側の fail-closed と
        # 食い違う。
        compile_before = None
        persona = manager.personas.get(persona_id) if manager else None
        lifecycle = getattr(getattr(manager, "sea_runtime", None), "session_lifecycle", None)
        if lifecycle is not None:
            from sea.coverage_repair import (
                CeilingResolutionError,
                resolve_compile_ceiling,
            )
            try:
                # 見積もりは行を書かない (persist_advance=False)。候補は実走と
                # 同じ (温かい記録 ∪ 現在モデルの記録) — 表示と実走が違う数を
                # 言わない。
                ceiling = resolve_compile_ceiling(
                    lifecycle, persona_id, conn,
                    persona=persona, persist_advance=False,
                )
            except CeilingResolutionError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"要約してよい上限を解決できませんでした: {exc}",
                )
            if ceiling is not None:
                compile_before = (ceiling.created_at, ceiling.rowid)

        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
        model_name = (getattr(persona, "memory_weave_model", None)
                      or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL))

        # 生成経路 (ジョブ) と同じ fold 集合で束ねコールを見積もる
        # (Codex 四巡 P1-b)。None = 照会失敗 → 生成が束ねを見送るのと同形。
        from sea.session_lifecycle import collect_folded_chronicle_entry_ids
        try:
            folded_entry_ids = collect_folded_chronicle_entry_ids(manager, persona_id)
        except Exception:
            LOGGER.warning("[cost-estimate] folded-range collection failed", exc_info=True)
            folded_entry_ids = None

        # Memopedia 初期化はテーブル作成の書き込みを伴うため、ロード済みなら
        # adapter のロックで直列化する (memopedia_writers_bypass_adapter_lock)
        _adapter = getattr(persona, "sai_memory", None)

        # 末尾の未被覆帯の anchor 引き戻し (裁定 5 改訂) に伴う即時畳みの
        # 見込み。実行 (run_tail_rewind) と同じ判定関数 (plan_tail_rewind) を
        # 通す — 表示と実走が違う数を言ってはならない。
        tail_fold_estimator = None
        if lifecycle is not None and persona is not None:
            from sea.coverage_repair import estimate_tail_rewind_fold

            def tail_fold_estimator(first_id, _run_ids,
                                    _lc=lifecycle, _p=persona, _c=conn):
                return estimate_tail_rewind_fold(_lc, _p, _c, first_id)

        estimate = estimate_chronicle_generation_cost(
            conn,
            model_name=model_name,
            excluded_entry_ids=(
                frozenset(folded_entry_ids) if folded_entry_ids is not None else None
            ),
            db_lock=_adapter._db_lock if _adapter is not None else None,
            compile_before=compile_before,
            tail_fold_estimator=tail_fold_estimator,
        )

        # 前回の吸収ジョブの未完了 (裁定 6): 再起動を跨いで残る印
        # (arasuji_progress の repair_incomplete 行 + content_stale の残骸)
        # を応答に載せ、frontend の帯が「再実行してください」を併記する。
        from sai_memory.arasuji.absorption import is_repair_incomplete
        try:
            repair_incomplete = is_repair_incomplete(conn)
        except Exception:
            # 読めなかった = 未完了かもしれない。黙って「完了」の顔にしない —
            # 帯の通知を維持する側へ倒す (Codex 三巡 F5。テーブル不在の縮退は
            # is_repair_incomplete 自身が False で返す)。
            LOGGER.warning(
                "[cost-estimate] repair-incomplete check failed; reporting "
                "incomplete to keep the banner visible", exc_info=True,
            )
            repair_incomplete = True

        return ChronicleCostEstimate(
            total_messages=estimate.total_messages,
            processed_messages=estimate.processed_messages,
            unprocessed_messages=estimate.unprocessed_messages,
            estimated_llm_calls=estimate.estimated_llm_calls,
            estimated_cost_usd=estimate.estimated_cost_usd,
            model_name=estimate.model_name,
            is_free_tier=estimate.is_free_tier,
            currency=estimate.currency,
            repair_incomplete=repair_incomplete,
        )
    finally:
        conn.close()


@router.get("/{persona_id}/arasuji/stats", response_model=ArasujiStatsResponse)
def get_arasuji_stats(persona_id: str, manager = Depends(get_manager)):
    """Get arasuji statistics for a persona."""
    from sai_memory.arasuji.storage import count_entries_by_level, get_max_level

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        counts = count_entries_by_level(conn)
        max_level = get_max_level(conn)
        total = sum(counts.values())
        return ArasujiStatsResponse(
            max_level=max_level,
            counts_by_level=counts,
            total_count=total
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get arasuji stats: {e}")
    finally:
        conn.close()

def _resolve_band_budget(persona_id: str, manager) -> tuple:
    """帯予算の実効値と、それがどの段から来たかを返す。

    解決順そのもの (ペルソナ列 > env > 既定) は実装を持っている 2 つの部品に
    委ね、ここでは**ラベルを付けるためだけ**に段を判定する — 順序を三度目に
    書き写すと、直したときに診断だけ嘘をつくようになる。

    段の判定は**実際の解決と同じ規則**で行う — 文字列としての一致で見ると、
    env="030000" や "+30000" のように書式が違うだけで実効値は同じ場合に、
    ラベルだけが嘘をつく (Codex 指摘 2026-09-01)。``_resolve_char_budget`` が
    env を採用する条件は「値が空でなく ``int()`` が通ること」なので、その条件を
    そのまま再現する。

    Returns:
        ``(budget, source)``。source は "persona_column" / "env" /
        "env_budget_disabled" (env が 0 以下 = 予算制を切って件数モードにして
        いる) / "builtin_default"。
    """
    import os as _os

    from builtin_data.tools.get_memory_weave_context import (
        _resolve_persona_chronicle_budget,
    )
    from sai_memory.arasuji.context import USE_DEFAULT_BUDGET, _resolve_char_budget
    from tools.context import persona_context

    with persona_context(persona_id, None, manager):
        from_column = _resolve_persona_chronicle_budget(
            persona_id, USE_DEFAULT_BUDGET,
        )
    if from_column != USE_DEFAULT_BUDGET:
        # 列は正値のときしか返らない (0 以下は未設定に倒す)。
        return from_column, "persona_column"

    budget = _resolve_char_budget(USE_DEFAULT_BUDGET)

    # env が採用されたか = _resolve_char_budget の分岐条件そのもの。
    env_raw = _os.getenv("SAIVERSE_CHRONICLE_CHAR_BUDGET")
    env_used = False
    if env_raw:
        try:
            int(env_raw)  # 空白・先頭ゼロ・符号は int() が吸収する
            env_used = True
        except ValueError:
            env_used = False  # 解決側も既定へ落ちる (警告を出して)

    if not env_used:
        return budget, "builtin_default"
    return budget, ("env" if budget > 0 else "env_budget_disabled")


def _simulate_chronicle_band(conn, persona_id: str, manager) -> dict:
    """いまコンテキストに載る Chronicle 帯を、本番と同じ組み立てで測る。

    容疑の切り分け用 (2026-09-01)。健全なデータなら累積質量の規則で 30 件・
    1.3 万字前後に収束するので、ここが予算 (既定 20,000 字) を大きく超えていれば
    「一件が長い」か「件数が膨らんでいる」かのどちらかが、レベル別の内訳から
    読み取れる。

    ⚠️ ``exclude_entry_ids`` は**渡さない** — 本番の weave は提示ウィンドウ内で
    digest 表示中のエントリを帯から外すが、それはサーバーの実行時状態に依存する。
    診断を状態から独立させるため除外なしで流す。したがって
    **実際の帯はここから「提示中の digest の分」だけ減る** (応答の
    ``excludes_presented_digests`` が False なのはその意味)。

    予算が 0 以下 (env で予算制を切っている構成) のときは、本番と同じく
    **件数モード**で流れる。``budget_mode`` がどちらで測ったかを申告する。

    可視エントリを**全件返すのは意図** (2026-09-01 裁定) — 丸めると膨張の証拠が
    消える。件数が多いこと自体が読みたい事実で、ここはデバッグ専用の口。走査
    コストも本番の weave 組み立てが Metabolism のたびに払っているものと同一なので、
    ここが重いなら本番が先に破綻している。

    読み取り専用・LLM ゼロ (SELECT と純ロジックだけ)。失敗はこの節だけに閉じ込め、
    診断全体を巻き添えにしない (stelis_stats_error と同じ流儀)。
    """
    try:
        from sai_memory.arasuji.context import (
            format_episode_context,
            get_episode_context,
        )

        budget, budget_source = _resolve_band_budget(persona_id, manager)
        entries = get_episode_context(
            conn,
            max_entries=10_000,   # 予算が主制御。件数側は暴走防止の安全弁
            char_budget=budget,
            exclude_entry_ids=None,
        )
        formatted = format_episode_context(entries, include_level_info=True)

        by_level: Dict[int, dict] = {}
        for entry in entries:
            bucket = by_level.setdefault(
                int(entry.level), {"entries": 0, "content_chars": 0},
            )
            bucket["entries"] += 1
            bucket["content_chars"] += len(entry.content or "")

        content_chars = sum(len(e.content or "") for e in entries)
        return {
            "budget": budget,
            "budget_source": budget_source,
            # 予算 0 以下は予算制そのものが切れ、件数 (max_entries) が効く。
            # 本番も同じ分岐なので、どちらで測ったかを申告する。
            "budget_mode": "char_budget" if budget > 0 else "count_based",
            "total_entries": len(entries),
            "content_chars": content_chars,
            "formatted_chars": len(formatted),
            # 予算は本文字数に対して効く (整形の飾りは予算の外)。
            "over_budget": bool(budget > 0 and content_chars > budget),
            "by_level": by_level,
            # 除外を渡していないことを応答自身が明言する (上の docstring 参照)。
            "excludes_presented_digests": False,
            "visible_entries": [
                {
                    "id": e.source_id,
                    "level": int(e.level),
                    "start_time": e.start_time,
                    "end_time": e.end_time,
                    "content_chars": len(e.content or ""),
                }
                for e in entries
            ],
            "error": None,
        }
    except Exception as exc:
        LOGGER.warning(
            "[diagnosis] band simulation failed for %s", persona_id, exc_info=True,
        )
        return {"error": str(exc)}


@router.get("/{persona_id}/arasuji/diagnosis")
def get_chronicle_diagnosis(persona_id: str, manager=Depends(get_manager)):
    """Get diagnostic information about Chronicle structure (no message content)."""
    from sai_memory.arasuji.storage import (
        get_entries_by_level,
        count_entries_by_level,
        count_unconsolidated_by_level,
        get_max_level,
        get_progress,
    )
    from sai_memory.memory.storage import count_messages

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        total_messages = count_messages(conn)
        counts_by_level = count_entries_by_level(conn)
        unconsolidated_by_level = count_unconsolidated_by_level(conn)
        max_level = get_max_level(conn)
        progress = get_progress(conn)

        # --- Level-1 source_ids 実態調査 ---
        # DB保存値（source_count/message_count）ではなく source_ids_json の実際の内容を集計

        # 各エントリの実際の source_ids 長（json_array_length）をまとめて取得
        cur = conn.execute(
            "SELECT id, json_array_length(source_ids_json) FROM arasuji_entries WHERE level = 1"
        )
        lv1_actual_lengths: Dict[str, int] = {row[0]: (row[1] or 0) for row in cur.fetchall()}

        if lv1_actual_lengths:
            lengths = list(lv1_actual_lengths.values())
            lv1_actual_total = sum(lengths)
            lv1_actual_avg = lv1_actual_total / len(lengths)
            lv1_actual_max = max(lengths)
            lv1_actual_min = min(lengths)
        else:
            lv1_actual_total = lv1_actual_avg = lv1_actual_max = lv1_actual_min = 0

        # ユニーク source_ids 数（整列計画 plan_alignment が「処理済み」とみなす件数と同じ）
        cur = conn.execute(
            "SELECT COUNT(DISTINCT value) "
            "FROM arasuji_entries, json_each(source_ids_json) WHERE level = 1"
        )
        lv1_unique_source_ids = cur.fetchone()[0] or 0

        # source_ids 重複数（合計 - ユニーク）
        lv1_duplicate_source_ids = lv1_actual_total - lv1_unique_source_ids

        # 存在しないメッセージを指す source_ids（孤児）
        cur = conn.execute(
            "SELECT COUNT(DISTINCT value) "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1 AND value NOT IN (SELECT id FROM messages)"
        )
        lv1_orphan_source_ids = cur.fetchone()[0] or 0

        # source_count フィールドと実際の長さが異なるエントリ
        cur = conn.execute(
            "SELECT COUNT(*) FROM arasuji_entries "
            "WHERE level = 1 AND source_count != json_array_length(source_ids_json)"
        )
        lv1_mismatched_entries = cur.fetchone()[0] or 0

        # --- レベル別の本文文字数 (2026-09-01) ---
        # 帯 (weave の Chronicle 部分) が予算を超えて膨らむ容疑は 2 つ:
        #   (1) 一件あたりのあらすじ本文が長い  → ここの avg/max が答える
        #   (2) 重複 source_ids が走査の重なり管理を壊して件数が膨らむ
        #       → lv1_duplicate_source_ids と下の帯シミュレーションの件数が答える
        # どちらかを切り分けるための材料で、判断はしない (診断は読むだけ)。
        content_chars_by_level: Dict[int, dict] = {}
        cur = conn.execute(
            "SELECT level, COUNT(*), SUM(LENGTH(content)), AVG(LENGTH(content)), "
            "MAX(LENGTH(content)), MIN(LENGTH(content)) "
            "FROM arasuji_entries GROUP BY level ORDER BY level"
        )
        for level, count, total_chars, avg_chars, max_chars, min_chars in cur.fetchall():
            content_chars_by_level[int(level)] = {
                "entries": int(count or 0),
                "total_chars": int(total_chars or 0),
                "avg_chars": round(float(avg_chars or 0), 1),
                "max_chars": int(max_chars or 0),
                "min_chars": int(min_chars or 0),
            }

        # --- Stelis 除外後の統計 ---
        stelis_excluded: Optional[int] = None
        non_stelis_total: Optional[int] = None
        non_stelis_after_last: Optional[int] = None
        stelis_error: Optional[str] = None
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE thread_id IN (SELECT thread_id FROM stelis_threads)"
            )
            stelis_excluded = cur.fetchone()[0] or 0
            non_stelis_total = total_messages - stelis_excluded
        except Exception as ex:
            stelis_error = str(ex)

        # --- per-level エントリ詳細（実際の source_ids 長を含む）---
        level_details: Dict[int, list] = {}
        lv1_entries = []
        for level in range(1, max_level + 1):
            entries = get_entries_by_level(conn, level, order_by_time=True)
            if level == 1:
                lv1_entries = entries

            if level == 1:
                level_details[level] = [
                    {
                        "id": e.id,
                        "start_time": e.start_time,
                        "end_time": e.end_time,
                        "source_count_stored": e.source_count,
                        "actual_source_ids_count": lv1_actual_lengths.get(e.id, 0),
                        "message_count": e.message_count,
                        "is_consolidated": e.is_consolidated,
                        "parent_id": e.parent_id,
                    }
                    for e in entries
                ]
            else:
                level_details[level] = [
                    {
                        "id": e.id,
                        "start_time": e.start_time,
                        "end_time": e.end_time,
                        "source_count_stored": e.source_count,
                        "message_count": e.message_count,
                        "is_consolidated": e.is_consolidated,
                        "parent_id": e.parent_id,
                    }
                    for e in entries
                ]

        # --- Gap analysis: Level-1 Chronicle 間の孤立メッセージ ---
        gaps = []
        for i in range(len(lv1_entries) - 1):
            e1 = lv1_entries[i]
            e2 = lv1_entries[i + 1]
            if e1.end_time is not None and e2.start_time is not None and e1.end_time < e2.start_time:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE created_at > ? AND created_at < ?",
                    (e1.end_time, e2.start_time),
                )
                gap_count = cur.fetchone()[0]
                if gap_count > 0:
                    gaps.append({
                        "prev_chronicle_id": e1.id,
                        "next_chronicle_id": e2.id,
                        "gap_start_time": e1.end_time,
                        "gap_end_time": e2.start_time,
                        "isolated_message_count": gap_count,
                    })

        # --- 最後の Chronicle 以降のメッセージ数 ---
        messages_after_last = 0
        last_end_time = None
        if lv1_entries:
            last_end_time = lv1_entries[-1].end_time
            if last_end_time is not None:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE created_at > ?",
                    (last_end_time,),
                )
                messages_after_last = cur.fetchone()[0]
                if non_stelis_total is not None:
                    try:
                        cur = conn.execute(
                            "SELECT COUNT(*) FROM messages WHERE created_at > ? "
                            "AND thread_id NOT IN (SELECT thread_id FROM stelis_threads)",
                            (last_end_time,),
                        )
                        non_stelis_after_last = cur.fetchone()[0] or 0
                    except Exception:
                        pass
        else:
            messages_after_last = total_messages
            non_stelis_after_last = non_stelis_total

        # DB保存の message_count 合計（旧来の値）
        messages_covered_stored = sum(e.message_count for e in lv1_entries)

        band = _simulate_chronicle_band(conn, persona_id, manager)

        return {
            "persona_id": persona_id,
            "generated_at": int(time.time()),
            # --- 全体サマリー ---
            "total_messages": total_messages,
            "messages_covered_by_lv1_stored": messages_covered_stored,  # DB保存値の合計（不正確な可能性あり）
            "messages_after_last_chronicle": messages_after_last,
            "max_level": max_level,
            "counts_by_level": counts_by_level,
            "unconsolidated_by_level": unconsolidated_by_level,
            "last_chronicle_end_time": last_end_time,
            "last_processed_message_id": progress.last_processed_message_id if progress else None,
            "last_processed_at": progress.last_processed_at if progress else None,
            # --- source_ids 実態調査 ---
            "lv1_actual_source_ids_total": lv1_actual_total,
            "lv1_unique_source_ids": lv1_unique_source_ids,        # plan_alignment の processed_ids 件数と同値
            "lv1_duplicate_source_ids": lv1_duplicate_source_ids,  # 重複分（0でなければ異常）
            "lv1_orphan_source_ids": lv1_orphan_source_ids,        # 存在しないメッセージ参照数
            "lv1_mismatched_entries": lv1_mismatched_entries,      # source_count != 実際長 のエントリ数
            "lv1_actual_source_ids_avg": round(lv1_actual_avg, 1),
            "lv1_actual_source_ids_max": lv1_actual_max,
            "lv1_actual_source_ids_min": lv1_actual_min,
            # --- Stelis 除外後の統計 ---
            "stelis_excluded_messages": stelis_excluded,
            "non_stelis_total_messages": non_stelis_total,
            "non_stelis_after_last_chronicle": non_stelis_after_last,
            "stelis_stats_error": stelis_error,
            # --- レベル別の本文文字数 (2026-09-01) ---
            "content_chars_by_level": content_chars_by_level,
            # --- 帯の実寸 (2026-09-01)。中身は _simulate_chronicle_band の docstring ---
            "band_simulation": band,
            # --- 詳細 ---
            "level_details": level_details,
            "gaps": gaps,
        }
    except Exception as e:
        LOGGER.error("Failed to get chronicle diagnosis for %s: %s", persona_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get chronicle diagnosis: {e}")
    finally:
        conn.close()


@router.get("/{persona_id}/arasuji", response_model=ArasujiListResponse, tags=["Chronicle"])
def list_arasuji_entries(
    persona_id: str,
    level: Optional[int] = None,
    manager = Depends(get_manager)
):
    """List Chronicle entries for a persona (part of Memory Weave).

    件数上限を持たない (2026-08-05、docs/issues/arasuji_modal_500_limit_truncation.md)。
    Chronicle はペルソナの記憶そのものなので、一覧から到達できないエントリを作る
    理由が無い。旧実装は既定 500 件で切り、超過分を「隠しています」と画面に出して
    いたが、UI に上限を変える口が無く、隠された側への到達手段が存在しなかった。
    エリス実測で全 513 件 (本文 + source_ids) が 522KB であり、上限に見合う実害も
    無かった。将来一覧が重くなった場合は、黙って切るのではなく一覧 UI 側 (仮想
    スクロール等) で解く。
    """
    from sai_memory.arasuji.storage import _row_to_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    _SELECT = (
        "SELECT id, level, content, source_ids_json, start_time, end_time, "
        "source_count, message_count, parent_id, is_consolidated, created_at "
        "FROM arasuji_entries"
    )
    _OLDEST_FIRST = " ORDER BY start_time ASC, created_at ASC, id ASC"

    try:
        if level is not None:
            cur = conn.execute(_SELECT + " WHERE level = ?" + _OLDEST_FIRST, (level,))
            entries = [_row_to_entry(row) for row in cur.fetchall()]
        else:
            # 表示順は「上位レベル (全体像の骨格) が先、その後に L1 を時系列で」。
            cur = conn.execute(
                _SELECT + " WHERE level != 1 ORDER BY level DESC, start_time ASC"
            )
            upper = [_row_to_entry(row) for row in cur.fetchall()]
            cur = conn.execute(_SELECT + " WHERE level = 1" + _OLDEST_FIRST)
            entries = upper + [_row_to_entry(row) for row in cur.fetchall()]

        # Build message number map for level 1 entries
        msg_num_map = None
        has_level1 = any(e.level == 1 for e in entries)
        if has_level1:
            try:
                msg_num_map = _get_message_number_map(conn)
            except Exception:
                LOGGER.warning("Failed to get message number map for %s", persona_id, exc_info=True)

        items = []
        for e in entries:
            source_start_num = None
            source_end_num = None

            if e.level == 1 and e.source_ids and msg_num_map:
                # Calculate message number range
                nums = [msg_num_map.get(sid) for sid in e.source_ids if sid in msg_num_map]
                if nums:
                    source_start_num = min(nums)
                    source_end_num = max(nums)

            items.append(ArasujiEntryItem(
                id=e.id,
                level=e.level,
                content=e.content,
                start_time=e.start_time,
                end_time=e.end_time,
                message_count=e.message_count,
                is_consolidated=e.is_consolidated,
                created_at=e.created_at,
                source_ids=e.source_ids,
                source_start_num=source_start_num,
                source_end_num=source_end_num,
            ))

        return ArasujiListResponse(
            entries=items,
            total=len(items),
            level_filter=level,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list Chronicle entries: {e}")
    finally:
        conn.close()

@router.get("/{persona_id}/arasuji/{entry_id}", response_model=ArasujiEntryItem, tags=["Chronicle"])
def get_arasuji_entry(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Get a detailed Chronicle entry by ID."""
    from sai_memory.arasuji.storage import get_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        # Calculate message number range for level 1
        source_start_num = None
        source_end_num = None
        if entry.level == 1 and entry.source_ids:
            try:
                msg_num_map = _get_message_number_map(conn)
                nums = [msg_num_map.get(sid) for sid in entry.source_ids if sid in msg_num_map]
                if nums:
                    source_start_num = min(nums)
                    source_end_num = max(nums)
            except Exception:
                LOGGER.warning("Failed to get message number range for entry %s", entry_id, exc_info=True)

        return ArasujiEntryItem(
            id=entry.id,
            level=entry.level,
            content=entry.content,
            start_time=entry.start_time,
            end_time=entry.end_time,
            message_count=entry.message_count,
            is_consolidated=entry.is_consolidated,
            created_at=entry.created_at,
            source_ids=entry.source_ids,
            source_start_num=source_start_num,
            source_end_num=source_end_num,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get Chronicle entry: {e}")
    finally:
        conn.close()

@router.delete("/{persona_id}/arasuji/{entry_id}")
def delete_arasuji_entry(persona_id: str, entry_id: str, manager = Depends(get_manager)):
    """Delete a Chronicle entry and unmark child entries as consolidated."""
    from sai_memory.arasuji.storage import delete_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        # Beat ロックで補修ジョブ / Metabolism と直列化する (Codex 六巡 J2) —
        # 削除に伴う親帳簿の引き直し (refresh_ancestor_bookkeeping) は
        # read-modify-write なので、錠なしだとジョブ側の帳簿更新と並走して
        # 後勝ちで上書きし合う。保守書き込みなので関所は通さない
        # (check_gate=False — run_coverage_repair / remove_folds と同じ型。
        # remove_folds 内の hold_beat は同一スレッド RLock 再入で無害)。
        from sea.beat_gate import hold_beat
        with hold_beat(
            manager, persona_id, purpose="arasuji_delete", check_gate=False,
        ):
            # delete_entry handles child reset (is_consolidated=0, parent_id=NULL)
            success = delete_entry(conn, entry_id)
            if not success:
                raise HTTPException(
                    status_code=404,
                    detail=f"Chronicle entry {entry_id} not found",
                )

            # 圧縮区間の記録の道連れ削除: このエントリを digest として提示して
            # いる範囲の記録を残すと、提示は生ログに倒れるのに再畳みだけが永久に
            # 拒否される半端な状態になる (issue chronicle_eviction_applier_veto_
            # deadlock 顔その2)。エントリを消す操作が記録も同時に無効化する。
            from sea.session_lifecycle import remove_folds_referencing_entry
            folds_removed = remove_folds_referencing_entry(
                manager, persona_id, entry_id,
            )

        return {
            "success": True,
            "message": f"Deleted Chronicle entry {entry_id}",
            "folded_ranges_removed": folds_removed,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Chronicle entry: {e}")
    finally:
        conn.close()

@router.patch("/{persona_id}/arasuji/{entry_id}", tags=["Chronicle"])
def update_arasuji_entry(
    persona_id: str,
    entry_id: str,
    request: UpdateArasujiEntryRequest,
    manager=Depends(get_manager),
):
    """Update a Chronicle entry's content."""
    from sai_memory.arasuji.storage import update_entry_content

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        # Beat ロックで補修ジョブ / Metabolism / 削除と直列化 (Codex 七巡 K3 —
        # 吸収の CAS〜swap の間に編集が入ると、競合検出のどちらか一方が
        # 黙って負ける。錠の内側なら並びが直列化されて CAS が正しく効く)。
        from sea.beat_gate import hold_beat
        with hold_beat(
            manager, persona_id, purpose="arasuji_edit", check_gate=False,
        ):
            success = update_entry_content(conn, entry_id, request.content)
        if not success:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Chronicle entry: {e}")
    finally:
        conn.close()


@router.delete("/{persona_id}/arasuji", tags=["Chronicle"])
def delete_all_arasuji_entries(persona_id: str, manager=Depends(get_manager)):
    """Delete ALL Chronicle entries and reset progress."""
    from sai_memory.arasuji.storage import clear_all_entries

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        # Beat ロックで補修ジョブ / Metabolism と直列化 (Codex 六巡 J2 —
        # 個別削除ルートと同じ型)。
        from sea.beat_gate import hold_beat
        with hold_beat(
            manager, persona_id, purpose="arasuji_delete", check_gate=False,
        ):
            deleted_count = clear_all_entries(conn)
        return {
            "success": True,
            "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} Chronicle entries",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Chronicle entries: {e}")
    finally:
        conn.close()


@router.get("/{persona_id}/arasuji/{entry_id}/messages", response_model=List[SourceMessageItem], tags=["Chronicle"])
def get_arasuji_messages(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Get the source raw messages for a Level 1 Chronicle entry."""
    from sai_memory.arasuji.storage import get_entry
    from sai_memory.memory.storage import get_message

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Arasuji entry {entry_id} not found")

        if entry.level != 1:
            raise HTTPException(status_code=400, detail="This endpoint only works for level-1 arasuji entries")

        # Fetch messages by IDs
        messages = []
        for msg_id in entry.source_ids:
            msg = get_message(conn, msg_id)
            if msg:
                messages.append(SourceMessageItem(
                    id=msg.id,
                    role=msg.role,
                    content=msg.content or "",
                    created_at=msg.created_at,
                ))

        # Sort by created_at
        messages.sort(key=lambda m: m.created_at)
        return messages

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get source messages: {e}")
    finally:
        conn.close()

@router.get("/{persona_id}/arasuji/{entry_id}/fragments", tags=["Chronicle"])
def get_arasuji_fragments(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager),
):
    """Get Memopedia fragments generated from a Chronicle entry."""
    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        rows = conn.execute(
            """
            SELECT f.id, f.content, f.source_date, p.title AS page_title
            FROM memopedia_fragments f
            JOIN memopedia_pages p ON f.entity_id = p.id
            WHERE f.chronicle_entry_id = ?
            ORDER BY p.title, f.created_at
            """,
            (entry_id,),
        ).fetchall()

        fragments = [
            {
                "id": r[0],
                "content": r[1],
                "source_date": r[2],
                "page_title": r[3],
            }
            for r in rows
        ]
        return {"fragments": fragments}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get fragments: {e}")
    finally:
        conn.close()


@router.post("/{persona_id}/arasuji/messages-by-ids", response_model=List[SourceMessageItem], tags=["Chronicle"])
def get_messages_by_ids(
    persona_id: str,
    request: MessagesByIdsRequest,
    manager = Depends(get_manager),
):
    """Get messages by their IDs (for error investigation)."""
    from sai_memory.memory.storage import get_message

    if not request.ids:
        return []

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        messages = []
        for msg_id in request.ids:
            msg = get_message(conn, msg_id)
            if msg:
                messages.append(SourceMessageItem(
                    id=msg.id,
                    role=msg.role,
                    content=msg.content or "",
                    created_at=msg.created_at,
                ))
        messages.sort(key=lambda m: m.created_at)
        return messages
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get messages: {e}")
    finally:
        conn.close()


@router.post("/{persona_id}/arasuji/{entry_id}/regenerate", tags=["Chronicle"])
async def regenerate_arasuji_entry(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Regenerate a specific Chronicle entry while preserving parent relationship.
    
    This endpoint delegates to the storage layer which handles:
    1. Saving parent relationship
    2. Deleting and updating parent
    3. Regenerating with LLM
    4. Restoring parent relationship
    """
    from sai_memory.arasuji.storage import regenerate_entry

    conn = _get_arasuji_db(persona_id)

    try:
        # Beat ロックで補修ジョブ / Metabolism / 削除・編集と直列化 (Codex
        # 七巡 K3 の同族掃討 — 再生成は generate-then-swap の書き込み)。LLM を
        # 錠の内側で待つのは補修ジョブ (run_coverage_repair) と同じ性質。
        from sea.beat_gate import hold_beat
        with hold_beat(
            manager, persona_id, purpose="arasuji_regenerate", check_gate=False,
        ):
            new_entry = regenerate_entry(conn, entry_id, persona_id=persona_id)
        
        if not new_entry:
            raise HTTPException(
                status_code=500,
                detail="Failed to regenerate entry"
            )
        
        return {
            "success": True,
            "old_entry_id": entry_id,
            "new_entry_id": new_entry.id,
            "content": new_entry.content
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOGGER.exception(f"[regenerate] Exception during regeneration: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to regenerate entry: {e}"
        )
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Chronicle Generation (Async Background Job)
# -----------------------------------------------------------------------------

# head (提示コンテキストの固定部) を組み直せなかったときの添え書き。畳み自体は
# 成功しているので completed のまま併記する — 救済 (再試行) はしない裁定
# (2026-09-01 まはー「無理に救済しなくていいが、失敗を知らせることは必要」)。
_HEAD_REBUILD_WARNING = (
    "設定の反映（コンテキストの組み直し）に失敗しました。"
    "次の記憶の整理か、時間経過で自動回復します。"
)


def _join_failure_text(generic: str, reason: Optional[str]) -> str:
    """ジョブの失敗文面 = 従来の一文 + generate_chronicle が掴んだ実際の理由。"""
    reason = (reason or "").strip()
    if not reason:
        return generic
    return f"{generic} {reason}"


def _run_chronicle_generation(
    job_id: str,
    persona_id: str,
    max_messages: int,
    model_name: Optional[str],
    with_memopedia: bool,
    include_timestamp: bool = True,
    manager=None,
    mode: str = "compaction",
    confirmed_unprocessed: Optional[int] = None,
):
    """Background worker for manual Chronicle generation.

    arasuji_levels.md §13 裁定4 (2026-07-29): 手動生成も範囲規則を自動 (応答後
    Metabolism) と揃える — 「発火 (予算超過) を待たずに今すぐ畳む」だけで、
    畳む範囲は残す量より古い側。実体は :meth:`SessionLifecycle.run_manual_compaction`
    への委譲。2026-09-01 に同期の organize-memory API を撤去し、ペルソナメニューの
    「溜まった会話をあらすじにまとめる」もこの背景ジョブへ合流した (手動の畳みは
    この経路が唯一の入口)。

    §16 (2026-08-31): ``mode="repair"`` は被覆補修 —
    :meth:`SessionLifecycle.run_coverage_repair` へ委譲し、止め線より古い
    未被覆の編纂対象を一次あらすじにする (提示窓は動かさない)。事前確認は
    UI が GET cost-estimate (同じ止め線) で行う。

    旧実装 (全未編纂の一括編纂 + 進捗バー + キャンセル + max_messages / model /
    with_memopedia 指定) は「全量編纂」という仕事ごと撤去した — 全量再編纂は
    scripts/arasuji/build_arasuji_core.py の領分。旧リクエストフィールドは
    受理して無視する (W4 の batch_size と同じ扱い)。
    """
    del max_messages, model_name, with_memopedia, include_timestamp  # 受理して無視 (§13)

    persona = manager.personas.get(persona_id) if manager else None
    lifecycle = getattr(getattr(manager, "sea_runtime", None), "session_lifecycle", None)
    if persona is None or lifecycle is None:
        _update_job(
            job_id, status="failed",
            error="ペルソナが読み込まれていないか、SEA ランタイムが利用できません",
            error_code="unavailable",
        )
        return

    # 中止ボタンを編纂のチャンク間チェックへ届ける。監視するのは status ではなく
    # 単調フラグ cancel_requested — status は worker が pending→running→terminal と
    # 上書きするため、status 監視だと pending 中のキャンセルが running 上書きで
    # 消える (Codex 再レビュー 2026-07-29)。確定済みチャンクは冪等スキップされる
    # ため中止後の再実行は安全 (generate_chronicle は中止を "deferred" で返す)。
    from sea.cancellation import CancellationToken

    class _JobCancellationToken(CancellationToken):
        def is_cancelled(self) -> bool:  # type: ignore[override]
            if super().is_cancelled():
                return True
            job = _get_job(job_id)
            return job is not None and bool(job.get("cancel_requested"))

    token = _JobCancellationToken()

    # pending 中に既にキャンセルされていたら、LLM 処理へ進まず即終端する。
    if token.is_cancelled():
        _update_job(job_id, status="cancelled", message="開始前に中止されました")
        return

    if mode == "repair":
        _run_coverage_repair_job(
            job_id, persona, lifecycle, token,
            confirmed_unprocessed=confirmed_unprocessed,
        )
        return

    _update_job(job_id, status="running", message="溜まった会話をあらすじにまとめています...")
    try:
        # head の再構築 (設定トグルの反映) の発火は run_manual_compaction が
        # 出口で持つ。ここで受け取るのは「組み直せたか」だけ — 組み直せなかった
        # 場合に画面へ知らせるのがジョブの仕事 (救済はしない)。
        status, head_rebuilt = lifecycle.run_manual_compaction_checked(
            persona, cancellation_token=token,
        )
        # 未埋め込みの Chronicle/ページ/Fragment を全件埋める (ローカル・無料、
        # 補修モードと同じ独立ステップ)。
        try:
            lifecycle.ensure_recall_embeddings(persona)
        except Exception:
            LOGGER.warning("[Chronicle Gen] embedding maintenance failed", exc_info=True)

        if not head_rebuilt:
            LOGGER.warning(
                "[Chronicle Gen] head rebuild not verified (persona=%s status=%s)",
                persona_id, status,
            )
            _update_job(job_id, warning=_HEAD_REBUILD_WARNING)

        if status == "ok":
            _update_job(
                job_id, status="completed",
                message="あらすじにまとめました（残す量より古い側を畳みました）",
            )
        elif status == "noop":
            _update_job(
                job_id, status="completed",
                message="まとめられる会話がまだ溜まっていません",
            )
        elif status == "deferred" and token.is_cancelled():
            _update_job(
                job_id, status="cancelled",
                message="中止しました（畳み済みの分は確定しています）",
            )
        elif status == "deferred":
            _update_job(
                job_id, status="failed",
                error="別のあらすじ処理が同じ範囲を処理中または処理済みです。しばらく待って再実行してください。",
                error_code="window_claimed",
            )
        elif status == "deferred_sluice_unseen":
            # スルース (採取) と編纂は確定済みで、退場だけが次回へ繰り越された。
            # claim 競合 (window_claimed) と混ぜると「別のあらすじ処理が処理中」という
            # 嘘になる (docs/issues/archive/metabolism_deferral_mislabeled_as_window_claim.md 従)。
            # 読めていない範囲は末尾の新着とは限らない (冷えた起点の前進で窓の
            # 頭側が漏れる並びもある) — 文面で「新しい会話」と断定しない。
            _update_job(
                job_id, status="failed",
                error="あらすじにまとめるのを見送りました（今回の採取で読めていない範囲があったため、畳みは次回のまとめで続きから進みます）。",
                error_code="sluice_unseen",
            )
        elif status == "disabled":
            _update_job(
                job_id, status="failed",
                error="Chronicle生成が無効のためまとめられません（ペルソナ設定で「Chronicle 自動生成」を「有効」にしてください）。",
                error_code="chronicle_disabled",
            )
        else:  # "failed" / "unavailable"
            generic = "Chronicle生成が完了しませんでした。畳みは適用されていないため、再実行で再試行できます。"
            failure = (
                lifecycle.pop_last_chronicle_failure(persona_id)
                if status == "failed" else None
            )
            if failure:
                # generate_chronicle が掴んだ実際の理由 (LLMError の error_code /
                # user_message / 落ちたチャンクの message_ids) をジョブへ写す —
                # UI の empty_response 等の案内と「該当メッセージを表示」が効く。
                _update_job(
                    job_id, status="failed",
                    error=_join_failure_text(generic, failure.get("error")),
                    error_code=failure.get("error_code") or status,
                    error_detail=failure.get("error_detail"),
                    error_meta=failure.get("error_meta"),
                )
            else:
                _update_job(
                    job_id, status="failed",
                    error=generic,
                    error_code=status,
                )
    except Exception as e:
        LOGGER.exception(f"Chronicle generation failed: {e}")
        _update_job(
            job_id, status="failed",
            error=str(e),
            error_code="unknown",
            error_detail=str(e),
        )


def _count_repair_targets(persona, lifecycle) -> int:
    """repair の対象件数を、見積もり API と同じ止め線・同じ計画で数え直す。

    修正 3 (時点ずれの歯止め) 用 — 見積もり表示からユーザーが実行ボタンを
    押すまでの間に対象が増えていないかの検算。解決失敗 (CeilingResolutionError
    等) はそのまま送出する — 呼び出し側が fail-closed で止める。
    """
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
    from sea.coverage_repair import resolve_compile_ceiling

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not adapter.is_ready():
        raise RuntimeError("SAIMemory is not available for the recount")
    with adapter._db_lock:
        init_arasuji_tables(adapter.conn)
    ceiling = resolve_compile_ceiling(
        lifecycle, getattr(persona, "persona_id", None), adapter.conn,
        persona=persona, persist_advance=False,
    )
    estimate = estimate_chronicle_generation_cost(
        adapter.conn,
        # 件数 (unprocessed_messages) はモデル名に依存しない — 料金部は捨てる。
        model_name="__repair_recount__",
        compile_before=(
            (ceiling.created_at, ceiling.rowid) if ceiling is not None else None
        ),
        db_lock=adapter._db_lock,
    )
    return estimate.unprocessed_messages


def _run_coverage_repair_job(
    job_id: str, persona, lifecycle, token,
    confirmed_unprocessed: Optional[int] = None,
) -> None:
    """被覆補修 (mode="repair") のジョブ本体 (arasuji_levels.md §16-2)。

    止め線より古い未被覆の編纂対象を一次あらすじにする。提示窓は動かさない
    (退場なし)。事前確認 (件数・概算費用) は UI が同じ止め線の
    GET cost-estimate で済ませている前提なので、ここでは確認なしで実行する。

    ``confirmed_unprocessed`` (修正 3): UI が見積もりで見せた件数。実行直前の
    再計算がこれより増えていたら estimate_stale で止める — 承認した範囲より
    広い課金を黙って実行しない。減る方向は表示より安くなるだけなので走る。
    """
    _update_job(
        job_id, status="running",
        message="あらすじになっていない過去の会話を編纂しています...",
    )

    def _progress(event):
        # generate_chronicle の進捗イベント (type=metabolism) をジョブの
        # メッセージへ写す。それ以外のイベント (warning 等) は捨ててよい —
        # 終端の成否は status が運ぶ。
        try:
            if (
                isinstance(event, dict)
                and event.get("type") == "metabolism"
                and event.get("content")
            ):
                _update_job(job_id, message=str(event["content"]))
        except Exception:
            pass

    try:
        if confirmed_unprocessed is not None:
            current = _count_repair_targets(persona, lifecycle)
            if current > confirmed_unprocessed:
                _update_job(
                    job_id, status="failed",
                    error="対象が増えています。もう一度見積もりを確認してください。",
                    error_code="estimate_stale",
                )
                return

        # head の再構築の発火は run_coverage_repair が出口で持つ。ここで受け取る
        # のは「組み直せたか」だけ (compaction 経路と同型)。
        status, mark_failures, head_rebuilt = lifecycle.run_coverage_repair_checked(
            persona, event_callback=_progress, cancellation_token=token,
        )
        # 未埋め込みの Chronicle/ページ/Fragment を全件埋める (ローカル・無料、
        # compaction モードと同じ独立ステップ)。
        try:
            lifecycle.ensure_recall_embeddings(persona)
        except Exception:
            LOGGER.warning("[Chronicle Repair] embedding maintenance failed", exc_info=True)

        if not head_rebuilt:
            LOGGER.warning(
                "[Chronicle Repair] head rebuild not verified (persona=%s status=%s)",
                getattr(persona, "persona_id", "?"), status,
            )
            _update_job(job_id, warning=_HEAD_REBUILD_WARNING)

        if status == "ok":
            message = "あらすじになっていなかった過去の会話を編纂しました"
            if mark_failures:
                # 編纂は確定済み。印だけの失敗は completed のまま可視化する
                # (修正 4) — 次回の補修の mark_covered_cold_windows が冪等に
                # 再適用する。
                message += (
                    "（一部のモデルの窓への印は書けませんでした。"
                    "次回の補修時に自動で再適用されます）"
                )
            _update_job(job_id, status="completed", message=message)
        elif status == "deferred" and token.is_cancelled():
            _update_job(
                job_id, status="cancelled",
                message="中止しました（編纂済みの分は確定しています）",
            )
        elif status == "deferred":
            _update_job(
                job_id, status="failed",
                error="別のあらすじ処理が同じ範囲を処理中または処理済みです。しばらく待って再実行してください。",
                error_code="window_claimed",
            )
        elif status == "disabled":
            _update_job(
                job_id, status="failed",
                error="Chronicle生成が無効のため編纂できません（ペルソナ設定で「Chronicle 自動生成」を「有効」にしてください）。",
                error_code="chronicle_disabled",
            )
        else:  # "failed"
            # 「編纂済みの分は保存されており、再実行で続きから進みます」は補修
            # 経路では真 (確定済みチャンクは source_ids で冪等スキップ) なので、
            # 実際の理由が取れても残す。
            generic = "あらすじの生成が完了しませんでした。編纂済みの分は保存されており、再実行で続きから進みます。"
            failure = (
                lifecycle.pop_last_chronicle_failure(
                    getattr(persona, "persona_id", None)
                )
                if status == "failed" else None
            )
            if failure:
                _update_job(
                    job_id, status="failed",
                    error=_join_failure_text(generic, failure.get("error")),
                    error_code=failure.get("error_code") or status,
                    error_detail=failure.get("error_detail"),
                    error_meta=failure.get("error_meta"),
                )
            else:
                _update_job(
                    job_id, status="failed",
                    error=generic,
                    error_code=status,
                )
    except Exception as e:
        from sea.coverage_repair import CeilingResolutionError
        if isinstance(e, CeilingResolutionError):
            # 止め線の解決失敗 (fail-closed) — 何も編纂していない。
            LOGGER.warning("[Chronicle Repair] ceiling unresolved: %s", e)
            _update_job(
                job_id, status="failed",
                error="編纂範囲の上端 (会話中の窓の境界) を確認できなかったため、実行を見送りました。再実行で再試行できます。",
                error_code="ceiling_unresolved",
                error_detail=str(e),
            )
            return
        LOGGER.exception(f"Chronicle coverage repair failed: {e}")
        _update_job(
            job_id, status="failed",
            error=str(e),
            error_code="unknown",
            error_detail=str(e),
        )


@router.post("/{persona_id}/arasuji/generate", tags=["Chronicle"])
async def start_arasuji_generation(
    persona_id: str,
    request: GenerateArasujiRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager),
):
    """Start Chronicle generation as a background job.

    ``mode`` (§16): "compaction" (既定 — 窓の畳み) / "repair" (被覆補修 —
    止め線より古い未被覆の編纂対象を一次あらすじにする)。
    Returns a job_id that can be used to poll for status.
    """
    if request.mode not in ("compaction", "repair"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown generation mode: {request.mode!r} "
                   "(expected 'compaction' or 'repair')",
        )
    # Verify persona exists
    from pathlib import Path
    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    # Create job
    job_id = _create_job(persona_id)

    # Start background task (batch_size / consolidation_size は W4 で廃止 —
    # チャンク分割は episode 整列 + サイズ束ねが決める。リクエストの旧
    # フィールドは受理して無視する)
    background_tasks.add_task(
        _run_chronicle_generation,
        job_id=job_id,
        persona_id=persona_id,
        max_messages=request.max_messages,
        model_name=request.model,
        with_memopedia=request.with_memopedia,
        include_timestamp=request.include_timestamp,
        manager=manager,
        mode=request.mode,
        confirmed_unprocessed=request.confirmed_unprocessed_messages,
    )

    return {"job_id": job_id, "status": "started"}


@router.post("/{persona_id}/arasuji/generate/{job_id}/cancel", tags=["Chronicle"])
async def cancel_arasuji_generation(
    persona_id: str,
    job_id: str,
):
    """Cancel a running Chronicle generation job.

    状態確認と cancel_requested の設定は _jobs_lock の同一クリティカル
    セクションで行う (Codex 三巡: 確認と更新が別ロックだと、間に worker が
    終端を確定した場合に cancelling で上書きしてポーリングが終わらなくなる)。
    cancel_requested は単調 (一度立てたら下ろさない)。終端間際の要求は間に
    合わず completed で終わることがある (処理が実際に完了している = 表示は真実)。
    """
    with _jobs_lock:
        job = _generation_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        if job.get("persona_id") != persona_id:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found for persona {persona_id}")
        if job.get("status") not in ("pending", "running", "started"):
            return {"cancelled": False, "reason": "Job is not running"}
        job["cancel_requested"] = True
        job["status"] = "cancelling"
    return {"cancelled": True}


def _job_status_response(job_id: str, job: dict) -> GenerationJobStatus:
    """ジョブ台帳の 1 行を API の応答形へ写す。"""
    return GenerationJobStatus(
        job_id=job_id,
        status=job.get("status", "unknown"),
        progress=job.get("progress"),
        total=job.get("total"),
        message=job.get("message"),
        entries_created=job.get("entries_created"),
        warning=job.get("warning"),
        error=job.get("error"),
        error_code=job.get("error_code"),
        error_detail=job.get("error_detail"),
        error_meta=job.get("error_meta"),
    )


# 「進捗はあらすじタブで確認できます」の案内を成立させるための再接続口。
# ジョブ ID は開始した画面の state にしか無いので、モーダルを閉じたり、
# ペルソナメニューから開始したりすると走行中ジョブへの手掛かりが消える
# (画面に書いた案内は契約 — 案内先で必ず見えなければならない)。
@router.get(
    "/{persona_id}/arasuji/generate/latest",
    response_model=Optional[GenerationJobStatus],
    tags=["Chronicle"],
)
async def get_latest_arasuji_generation(persona_id: str):
    """このペルソナの最新の生成ジョブ (走行中・終了済みを問わず) を返す。

    ジョブが 1 件も無ければ 404 ではなく ``null`` を返す — 「まだ何も走らせて
    いない」は正常な現況であって、引き当てられなかったエラーではない
    (job_id 指定の取得と違う性質)。

    台帳はプロセス内メモリなので、再起動を跨ぐと ``null`` に戻る。それでよい:
    走行中だったジョブもプロセスと一緒に消えているので、「ジョブなし」が
    その時点の正しい現況になる。

    ⚠️ ルート登録順が意味を持つ — ``/{job_id}`` より**前**に置くこと。後に
    置くと "latest" が job_id として食われる。
    """
    with _jobs_lock:
        candidates = [
            (job_id, job) for job_id, job in _generation_jobs.items()
            if job.get("persona_id") == persona_id
        ]
        if not candidates:
            return None
        job_id, job = max(candidates, key=lambda item: item[1].get("created_at", 0))
        job = job.copy()
    return _job_status_response(job_id, job)


@router.get("/{persona_id}/arasuji/generate/{job_id}", response_model=GenerationJobStatus, tags=["Chronicle"])
async def get_arasuji_generation_status(
    persona_id: str,
    job_id: str,
    manager = Depends(get_manager),
):
    """Get the status of a Chronicle generation job."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.get("persona_id") != persona_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found for persona {persona_id}")

    return _job_status_response(job_id, job)
