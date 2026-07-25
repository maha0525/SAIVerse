"""Band-overflow consolidation — 次数 + 列のあふれ駆動の上位束ね (W4 D6/D7).

体験の構造 (docs/intent/experience_structure.md) §4-6「束ねはレベルでなく
サイズ、駆動は列のあふれ」/ §4-7「最古が黙って落ちない」の実装。
設計正典は docs/handoff/2026-07-21_w4_metabolism_ledger_handoff.md D6/D7。

概念 (用語は docs/handoff/2026-07-25_chronicle_eviction_handoff.md §5 で改名済み。
「帯」は用語として廃止 — この機構自体が今後変わりうるので、名前で確定を演出しない):

- **次数 k のあらすじの標準被覆** = U × B^(k-1) 字 (U=1万・B=10 → 一次あらすじ≒
  1万、二次あらすじ≒10万、三次あらすじ≒100万)。`level` 列 = 次数 (読み込み互換の
  導出キャッシュ)。
- **次数 k の列** = level k の未束ね (unconsolidated) General ノード列 (時系列順)。
- **あふれ** = 次数 k の列の被覆合計 > U × B^k (= そのノード約 B 個分)。上限を
  超えたら、古い端から連続ノード列を合計 U × B^k に達するまで束ね、次数 k+1 の
  親を作る。
- **壁** = より上の次数のノード。壁の被覆範囲の内側に埋もれたノードは束ねず、
  壁を時間的に跨ぐ束ねもしない。壁で打ち切られた目標未達の列は束ねず持ち越す
  (次数の意味論を保つ — 小粒の親を作らない。Codex W4 #5)。旧 Lv3 等は再要約され
  ない (移行の約束 §4-6 の構造的保証)。
- 判定はすべて **coverage_chars (被覆生ログ字数)** — digest 自体の長さでは
  ない (LLM 出力のブレに依存しない決定論)。

原子性 (M2-a の解): 親 INSERT + 子 mark_consolidated を**単一トランザク
ション**で確定し、tx 内で全子が未束ねのままかを再検査する (並走ジョブが
同じ子列を束ねた場合は放棄 — Codex W4 #2)。旧 maybe_consolidate の
「親だけ commit 済みで子が unconsolidated のまま → 次回同じ子から別の親を
二重生成」の中間状態は構造的に起きない。

:func:`plan_band_overflow` は同じ選定ロジックの dry 実行 — LLM を呼ばずに
束ね回数を予測する (確認ゲートの LLM 数・コスト見積もりが実行と同じ計画を
共有する。Codex W4 #4/#9)。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from sai_memory.arasuji.alignment import (
    chronicle_band_base,
    chronicle_band_budget,
)
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    _get_chronicle_page_row,
    _parse_chronicle_meta,
    create_entry,
    get_max_level,
    mark_consolidated,
)

LOGGER = logging.getLogger(__name__)

#: 1 回の run_band_overflow で作る親ノード数の上限 (安全弁)。初回帰化時の
#: 大量 backlog を一度に LLM へ流さず、Metabolism ごとに少しずつ収束させる。
DEFAULT_MAX_CONSOLIDATIONS_PER_RUN = 3


def _max_consolidations_per_run() -> int:
    import os
    raw = os.getenv("SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN")
    if not raw:
        return DEFAULT_MAX_CONSOLIDATIONS_PER_RUN
    try:
        value = int(raw)
        return value if value >= 1 else DEFAULT_MAX_CONSOLIDATIONS_PER_RUN
    except ValueError:
        return DEFAULT_MAX_CONSOLIDATIONS_PER_RUN


# ---------------------------------------------------------------------------
# coverage_chars — 帰化バックフィル (D7)
# ---------------------------------------------------------------------------

#: 全 source が引けないときの近似: digest は原文の約 1/10 という圧縮率仮定。
_COVERAGE_FALLBACK_RATIO = 10


def backfill_coverage(conn: sqlite3.Connection) -> int:
    """coverage_chars の無い既存 entry に被覆字数を刻む (帰化 §11-3)。

    - level 1: source_ids → messages.content 長合計 (欠損 source は引けた分
      のみ。全滅時は content 長 × 10 の圧縮率近似)。
    - level 2+: 子 entry の coverage 合計 (level 昇順に処理するので子は先に
      埋まっている)。子欠損は同近似。
    - source / 子の**一部でも**引けなかった entry には ``coverage_estimated``
      マーカーを付ける (過小評価の可能性を観測可能にする — Codex W4 #10)。

    冪等 (coverage_chars が入っている entry はスキップ)。LLM なし。
    戻り値 = 埋めた entry 数。

    metadata の read-modify-write を並走書き込み (束ねの is_consolidated
    更新等) と競合させないため、全体を **BEGIN IMMEDIATE の単一 tx** で行う
    (Codex W4 四巡 #1 — 古い JSON の書き戻しで並走 commit を消す lost
    update の閉塞。W2 第五陣と同型)。
    """
    filled = 0
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        filled = _backfill_coverage_locked(conn)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    if filled:
        LOGGER.info("[bands] coverage backfill: %d entries filled", filled)
    return filled


def _backfill_coverage_locked(conn: sqlite3.Connection) -> int:
    """:func:`backfill_coverage` の本体 (write ロック保持下で実行される)。"""
    filled = 0
    max_level = get_max_level(conn)
    for level in range(1, max_level + 1):
        rows = conn.execute(
            "SELECT id, content, source_ids_json, metadata FROM ("
            "  SELECT p.id AS id, p.content AS content, "
            "         json_extract(p.metadata, '$.source_ids') AS source_ids_json, "
            "         p.metadata AS metadata, "
            "         json_extract(p.metadata, '$.level') AS lvl "
            "  FROM memopedia_pages p WHERE p.category = 'chronicle'"
            ") WHERE lvl = ? "
            "AND json_extract(metadata, '$.coverage_chars') IS NULL",
            (level,),
        ).fetchall()
        for entry_id, content, source_ids_json, meta_json in rows:
            try:
                source_ids = json.loads(source_ids_json) if source_ids_json else []
            except (json.JSONDecodeError, TypeError):
                source_ids = []
            coverage = 0
            missing = 0
            if level == 1:
                for sid in source_ids:
                    row = conn.execute(
                        "SELECT length(COALESCE(content, '')) FROM messages WHERE id = ?",
                        (sid,),
                    ).fetchone()
                    if row and row[0]:
                        coverage += int(row[0])
                    else:
                        missing += 1
            else:
                for sid in source_ids:
                    child = _get_chronicle_page_row(conn, sid)
                    if child is None:
                        missing += 1
                        continue
                    child_meta = _parse_chronicle_meta(child[3])
                    child_cov = child_meta.get("coverage_chars")
                    try:
                        coverage += int(child_cov) if child_cov is not None else 0
                    except (TypeError, ValueError):
                        missing += 1
            estimated = missing > 0
            if coverage <= 0:
                coverage = len(content or "") * _COVERAGE_FALLBACK_RATIO
                estimated = True
            meta = _parse_chronicle_meta(meta_json)
            meta["coverage_chars"] = coverage
            if estimated:
                meta["coverage_estimated"] = True
            conn.execute(
                "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), entry_id),
            )
            filled += 1
    return filled


# ---------------------------------------------------------------------------
# 列の読み出しと束ね列の選定 (実行 / dry 共通)
# ---------------------------------------------------------------------------


@dataclass
class _BandItem:
    """束ね選定の最小単位 (実 entry / dry の模擬ノード共用)。"""

    coverage: int
    start_time: Optional[int]
    end_time: Optional[int]
    entry: Optional[ArasujiEntry] = None  # dry の模擬ノードでは None


def _coverage_index(conn: sqlite3.Connection) -> dict:
    """全 Chronicle entry の {id: coverage_chars}。

    未 backfill (coverage_chars なし) の entry は content 長 × 圧縮率近似で
    読む — dry 予測 (:func:`plan_band_overflow`) は backfill より先に走る
    ことがあり、0 扱いだと未帰化 DB で band call をゼロ予測して確認ゲートを
    素通りする (Codex W4 二巡 #3)。実行時は backfill 済みの実測値が優先。
    """
    rows = conn.execute(
        "SELECT id, json_extract(metadata, '$.coverage_chars'), "
        "length(COALESCE(content, '')) "
        "FROM memopedia_pages WHERE category = 'chronicle'"
    ).fetchall()
    out = {}
    for entry_id, cov, content_len in rows:
        try:
            out[entry_id] = int(cov) if cov is not None else (
                int(content_len or 0) * _COVERAGE_FALLBACK_RATIO
            )
        except (TypeError, ValueError):
            out[entry_id] = int(content_len or 0) * _COVERAGE_FALLBACK_RATIO
    return out


def _general_band(conn: sqlite3.Connection, level: int) -> List[_BandItem]:
    """次数 k の列 = General (track なし・incomplete なし) の未束ねノード列 (時系列順)。

    coverage 未 backfill (0) の entry も列には含める (合計には 0 で参加 —
    backfill が先に走る前提)。

    旧 get_unconsolidated_entries は track / incomplete の混入を許していた
    (Track 混線の親戚) — 列は General に明示的に閉じる。
    """
    from sai_memory.arasuji.storage import _ENTRY_COLUMNS, _row_to_entry
    rows = conn.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM arasuji_entries "
        "WHERE level = ? AND is_consolidated = 0 AND origin_track_id IS NULL "
        "AND (is_incomplete IS NULL OR is_incomplete = 0) "
        "ORDER BY end_time ASC",
        (level,),
    ).fetchall()
    coverage = _coverage_index(conn)
    out: List[_BandItem] = []
    for row in rows:
        entry = _row_to_entry(row)
        out.append(_BandItem(
            coverage=coverage.get(entry.id, 0),
            start_time=entry.start_time,
            end_time=entry.end_time,
            entry=entry,
        ))
    return out


def _walls(conn: sqlite3.Connection, above_level: int) -> List[Tuple[int, int]]:
    """壁 = level > above_level の全ノードの被覆時間範囲 [(start, end), ...]。"""
    rows = conn.execute(
        "SELECT st, et FROM ("
        "  SELECT json_extract(p.metadata, '$.level') AS lvl, "
        "         json_extract(p.metadata, '$.start_time') AS st, "
        "         json_extract(p.metadata, '$.end_time') AS et "
        "  FROM memopedia_pages p WHERE p.category = 'chronicle'"
        ") WHERE lvl > ? AND st IS NOT NULL AND et IS NOT NULL",
        (above_level,),
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _inside_wall(item: _BandItem, walls: Sequence[Tuple[int, int]]) -> bool:
    """item が壁の被覆範囲の内側にあるか (壁の内側に埋もれたノード — 束ねない)。"""
    if item.start_time is None or item.end_time is None:
        return False
    return any(
        ws <= item.start_time and item.end_time <= we for ws, we in walls
    )


def _wall_between(
    prev: _BandItem, nxt: _BandItem, walls: Sequence[Tuple[int, int]]
) -> bool:
    """prev と nxt の時間ギャップに壁が挟まるか (跨ぐ束ねの禁止)。"""
    if prev.end_time is None or nxt.start_time is None:
        return False
    lo, hi = prev.end_time, nxt.start_time
    if lo > hi:
        return False
    return any(not (we < lo or ws > hi) for ws, we in walls)


def _select_bundle_run(
    band: Sequence[_BandItem],
    walls: Sequence[Tuple[int, int]],
    target: int,
) -> Optional[List[_BandItem]]:
    """古い端から束ね列を 1 本選ぶ。

    **目標被覆 (target) に到達した 2 ノード以上の列だけ**を返す。壁 (内側の
    壁の内側 / ギャップ跨ぎ) で打ち切られた目標未達の列は破棄して次の位置から
    集め直す — 小粒の上位ノードを作らない (Codex W4 #5、走行メモ D6 の
    「壁の手前の端数は持ち越し」)。目標到達列が無ければ None。
    """
    run: List[_BandItem] = []
    run_coverage = 0
    for item in band:
        if _inside_wall(item, walls):
            run = []
            run_coverage = 0
            continue
        if run and _wall_between(run[-1], item, walls):
            run = [item]
            run_coverage = item.coverage
        else:
            run.append(item)
            run_coverage += item.coverage
        if run_coverage >= target and len(run) >= 2:
            return run
    return None


# ---------------------------------------------------------------------------
# dry 計画 (確認ゲート / コスト見積もり用 — LLM なし)
# ---------------------------------------------------------------------------


def plan_band_overflow(
    conn: sqlite3.Connection,
    *,
    extra_leaves: Optional[Sequence[Tuple[int, Optional[int], Optional[int]]]] = None,
) -> int:
    """列のあふれ束ねの発生回数を LLM なしで予測する (Codex W4 #4/#9)。

    :func:`run_band_overflow` と同じ選定ロジック (あふれ判定・壁・安全弁) を、
    束ね結果 (親 = 子の coverage 合計、上の次数の列へ追加、新親は下の次数の列の壁) を
    模擬適用しながら数える。

    Args:
        extra_leaves: これから確定する新チャンクの
            ``(coverage_chars, start_time, end_time)`` 列。一次あらすじの列に加算して
            予測する (確認ゲートは実行前に呼ぶため)。

    Returns:
        予測される束ね (= 統合 LLM コール) 回数。
    """
    budget = chronicle_band_budget()
    base = chronicle_band_base()
    max_runs = _max_consolidations_per_run()

    # 列と壁の状態をメモリへ読み、模擬適用する。
    max_level = get_max_level(conn)
    bands: dict = {}
    for level in range(1, max_level + 2):
        bands[level] = _general_band(conn, level)
    if extra_leaves:
        leaf_band = bands.setdefault(1, [])
        for cov, st, et in extra_leaves:
            leaf_band.append(_BandItem(coverage=int(cov), start_time=st, end_time=et))
        leaf_band.sort(key=lambda i: (i.end_time if i.end_time is not None else 0))
    walls_by_level: dict = {}

    def _walls_for(level: int) -> List[Tuple[int, int]]:
        if level not in walls_by_level:
            walls_by_level[level] = list(_walls(conn, level))
        return walls_by_level[level]

    planned = 0
    level = 1
    while planned < max_runs:
        band = bands.get(level) or []
        if not band:
            level += 1
            if level > max(bands.keys(), default=1) + 1:
                break
            continue
        band_cap = budget * (base ** level)
        total = sum(i.coverage for i in band)
        if total <= band_cap:
            level += 1
            if level > max(bands.keys(), default=1) + 1:
                break
            continue
        run = _select_bundle_run(band, _walls_for(level), band_cap)
        if run is None:
            level += 1
            if level > max(bands.keys(), default=1) + 1:
                break
            continue
        # 模擬適用: 子を列から除き、親を上の次数の列へ、親の範囲を下の次数の列の壁へ。
        run_ids = {id(i) for i in run}
        bands[level] = [i for i in band if id(i) not in run_ids]
        starts = [i.start_time for i in run if i.start_time is not None]
        ends = [i.end_time for i in run if i.end_time is not None]
        parent = _BandItem(
            coverage=sum(i.coverage for i in run),
            start_time=min(starts) if starts else None,
            end_time=max(ends) if ends else None,
        )
        upper = bands.setdefault(level + 1, [])
        upper.append(parent)
        upper.sort(key=lambda i: (i.end_time if i.end_time is not None else 0))
        if parent.start_time is not None and parent.end_time is not None:
            for lv in range(1, level + 1):
                _walls_for(lv)
                walls_by_level[lv].append((parent.start_time, parent.end_time))
        planned += 1
    return planned


# ---------------------------------------------------------------------------
# 列のあふれ束ね (D6 — 実行)
# ---------------------------------------------------------------------------


def _build_consolidation_prompt(
    entries: List[ArasujiEntry],
    conn: sqlite3.Connection,
    *,
    include_timestamp: bool = True,
) -> str:
    """次数 k+1 の親 digest を子 digest 群から語り直すプロンプト (§4-4 で正当)。"""
    from sai_memory.arasuji.context import get_episode_context_for_timerange
    from sai_memory.arasuji.generator import _format_entries_for_prompt

    starts = [e.start_time for e in entries if e.start_time is not None]
    ends = [e.end_time for e in entries if e.end_time is not None]
    context = ""
    if starts and ends:
        context = get_episode_context_for_timerange(
            conn, start_time=min(starts), end_time=max(ends), max_entries=10,
        )
    entries_text = _format_entries_for_prompt(
        entries, include_timestamp=include_timestamp,
    )
    parts = [
        f"以下の{len(entries)}個のあらすじを統合し、一段粗い視点の「まとめのあらすじ」を書いてください。",
        "",
    ]
    if context:
        parts.extend(["## さらに前の出来事（参考）", context, ""])
    parts.extend([
        "## 統合対象のあらすじ",
        entries_text,
        "",
        "## 指示",
        "- 5〜8文程度で、全体の流れを俯瞰できるようにまとめる",
        "- 重要な転換点や印象的なエピソードを保持する",
        "- 個々の詳細より「どんな時期だったか」を重視する",
        "- 時系列順に書く",
        "- **前置き（「以下にまとめます」等）や見出し（「【あらすじ】」等）は書かないでください**（本文のみ出力）",
        "",
        "統合されたあらすじを日本語で書いてください。",
    ])
    return "\n".join(parts)


def _all_children_unconsolidated(
    conn: sqlite3.Connection, entry_ids: Sequence[str],
) -> bool:
    """全子がまだ未束ねか (束ね tx 内の再検査 — Codex W4 #2)。"""
    for eid in entry_ids:
        row = conn.execute(
            "SELECT json_extract(metadata, '$.is_consolidated') "
            "FROM memopedia_pages WHERE id = ? AND category = 'chronicle'",
            (eid,),
        ).fetchone()
        if row is None or (row[0] is not None and int(row[0]) != 0):
            return False
    return True


def _consolidate_run(
    conn: sqlite3.Connection,
    client,
    run: List[_BandItem],
    target_level: int,
    *,
    persona_id: Optional[str],
) -> Optional[ArasujiEntry]:
    """束ね列 1 本を次数 target_level の親に確定する (親 + 子を単一 tx)。"""
    entries = [i.entry for i in run if i.entry is not None]
    if len(entries) != len(run):
        return None
    prompt = _build_consolidation_prompt(entries, conn)
    from sai_memory.arasuji.generator import _record_llm_usage
    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}], tools=[],
        )
        _record_llm_usage(client, persona_id, f"chronicle_level{target_level}")
    except Exception:
        LOGGER.exception(
            "[bands] consolidation LLM failed (target_level=%d, %d entries)",
            target_level, len(entries),
        )
        return None
    content = (response or "").strip()
    if not content:
        LOGGER.warning(
            "[bands] empty consolidation response (target_level=%d)", target_level,
        )
        return None

    starts = [e.start_time for e in entries if e.start_time is not None]
    ends = [e.end_time for e in entries if e.end_time is not None]
    total_coverage = sum(i.coverage for i in run)
    child_ids = [e.id for e in entries]
    try:
        # tx 内再検査 (Codex W4 #2 / 二巡 #2 / 三巡 #2): BEGIN IMMEDIATE で
        # write ロックを取ってから子の未統合を確認する — SELECT は暗黙 BEGIN
        # を張らないため、ロックなしでは二接続が同時に「全子未統合」を読める。
        # band-only 実行 (ledger claim なし) の並走防御はこの原子化が本体。
        # 既に tx 内なら参加、locked (busy_timeout 超過) は raise → 下の
        # except が rollback + None (この束ねを見送り) に落とす。
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if not _all_children_unconsolidated(conn, child_ids):
            conn.rollback()
            LOGGER.warning(
                "[bands] consolidation abandoned: children consolidated "
                "concurrently (target_level=%d)", target_level,
            )
            return None
        parent = create_entry(
            conn,
            level=target_level,
            content=content,
            source_ids=child_ids,
            start_time=min(starts) if starts else None,
            end_time=max(ends) if ends else None,
            source_count=len(entries),
            message_count=sum(e.message_count for e in entries),
            extra_metadata={
                "digest_origin": "band",
                "coverage_chars": total_coverage,
            },
            commit=False,
        )
        mark_consolidated(conn, child_ids, parent.id, commit=False)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        LOGGER.exception(
            "[bands] consolidation tx failed (target_level=%d); rolled back",
            target_level,
        )
        return None
    LOGGER.info(
        "[bands] consolidated %d nodes -> level %d (coverage=%d chars, parent=%s)",
        len(entries), target_level, total_coverage, parent.id[:8],
    )
    return parent


def run_band_overflow(
    conn: sqlite3.Connection,
    client,
    *,
    persona_id: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> int:
    """列のあふれを検査し、あふれた列を古い端から上へ束ねる。

    次数 1 の列から上へ再帰的に判定する (束ねで上の次数の列が増えて更にあふれうる)。
    1 回の呼び出しで作る親は安全弁 (既定 3) まで — 大量 backlog は複数回の
    Metabolism に分けて収束させる (初回帰化の LLM コスト暴走防止)。

    Returns:
        作った親ノード数。
    """
    budget = chronicle_band_budget()
    base = chronicle_band_base()
    max_runs = _max_consolidations_per_run()
    created = 0
    attempts = 0  # LLM 試行数 — 安全弁は成功数でなく試行数で数える
    # (失敗を数えないと、失敗が続く限り予測 (dry) を超えて LLM を呼び続ける。
    #  Codex W4 二巡 #4)

    level = 1
    while attempts < max_runs:
        if cancel_check and cancel_check():
            break
        band = _general_band(conn, level)
        if not band:
            level += 1
            if level > get_max_level(conn) + 1:
                break
            continue

        band_cap = budget * (base ** level)
        total = sum(i.coverage for i in band)
        if total <= band_cap:
            level += 1
            if level > get_max_level(conn) + 1:
                break
            continue

        run = _select_bundle_run(band, _walls(conn, level), band_cap)
        consolidated_this_band = False
        if run is not None:
            attempts += 1
            parent = _consolidate_run(
                conn, client, run, level + 1, persona_id=persona_id,
            )
            if parent is not None:
                created += 1
                consolidated_this_band = True
        if not consolidated_this_band:
            # この列では束ねられない (壁だらけ / 目標到達列なし / LLM 失敗)。
            # 上の次数の列へ進む (次回の Metabolism で再挑戦)。
            level += 1
            if level > get_max_level(conn) + 1:
                break
    return created
