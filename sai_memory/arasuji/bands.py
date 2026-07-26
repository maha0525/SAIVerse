"""Chronicle 束ね — 字数発火・質量選抜 (docs/intent/chronicle_consolidation.md).

旧「次数 + 列のあふれ駆動」(質量 U×B^k 発火・level 別の列) を 2026-07-27 に
世代交代した。設計正典は docs/intent/chronicle_consolidation.md。

概念:

- **列** = 未束ね (unconsolidated) General ノードを時系列に並べた一本の列。
  level 別の列は廃止 — 質量の違うノードが一本の列に混在する。`level` は
  表示用の導出キャッシュ (親 = max(子)+1)。
- **質量** = coverage_chars (被覆生ログ字数)。**あらすじの字数** = digest
  テキスト自体の長さ。両者は比例しない (圧縮率が実測 0.10〜1.11) — これが
  旧設計 (質量発火・字数提示) の断絶の根。
- **発火** = 列の (束ね対象になれる) あらすじ字数合計 > X。X は提示予算
  (SAIVERSE_CHRONICLE_CHAR_BUDGET, 既定2万字) の 1/4 — 独立ノブを作らない。
- **群** = 束ねの単位。列の連続部分列で三条件を満たすもの:
  ①比率 (質量 max/min ≤ 10) ②連続性 (壁も未編纂の生ログも跨がない — 偽の
  隣接 §4-5 の禁止) ③卒業 (親質量 = 子の合算 ≥ 群内最大の 5 倍。質量が
  伸びない束ねの忍び寄りを封じ、圧縮回数の有限性を log5 で保証する)。
- **治療** = 卒業に到達できず軽い側で行き詰まったノード/群 (束ね不能) を、
  比率免除で後ろ (新しい側) の隣人へ合流させる回収路。状態は単調悪化
  (隣の質量は増える一方・新入りは最新端のみ) なので発火を待たず検知即実行。
- **非常弁** = 発火したのに束ねも治療も打てない予見外の形に限り、最古の
  隣接2件を比率無視で束ねる (WARNING 必須。平常発火ゼロが健全)。

判定はすべて決定論 — LLM が決めるのはあらすじ本文だけ (責任分界 §2)。

原子性 (M2-a の解): 親 INSERT + 子 mark_consolidated を**単一トランザク
ション**で確定し、tx 内で全子が未束ねのままかを再検査する (並走ジョブが
同じ子列を束ねた場合は放棄)。

:func:`plan_band_overflow` は同じ計画ロジックの dry 実行 — LLM を呼ばずに
束ね回数を予測する (確認ゲートの LLM 数・コスト見積もりが実行と同じ計画を
共有する)。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from sai_memory.arasuji.storage import (
    ArasujiEntry,
    _get_chronicle_page_row,
    _parse_chronicle_meta,
    create_entry,
    get_max_level,
    mark_consolidated,
)
from sai_memory.memory.storage import (
    CHRONICLE_EXCLUDED_LINE_ROLES,
    CHRONICLE_EXCLUDED_TAGS,
)

LOGGER = logging.getLogger(__name__)

#: 1 回の run_band_overflow で走らせる LLM コール (束ね+治療+非常弁) の上限。
DEFAULT_MAX_CONSOLIDATIONS_PER_RUN = 3

#: 群の比率条件 — 質量の最大/最小がこの倍率以内なら同じ群になれる。
MASS_RATIO_LIMIT = 10

#: 卒業条件 — 親質量 (子の合算) が群内最大メンバーの質量のこの倍以上のとき
#: だけ束ねる (跳躍の下限。まはー裁定 2026-07-27 で 10→5 に緩和)。
GRADUATION_FACTOR = 5

#: 計画時の親あらすじ字数の見込み (LLM 指示は 5〜8 文 ≒ 500 字)。
EST_PARENT_CHARS = 500


def estimate_leaf_chars(kind: str, messages: Sequence, digest_text) -> int:
    """extra_leaves の第4要素 (これから確定するチャンクの digest 字数見込み)。

    dry 予測と実行の乖離を抑える (Codex レビュー P1-3): 恒等圧縮は content が
    生ログ+タイムスタンプ等の装飾なので実長+装飾見込みで、episode 転写は
    digest 本文が既知なので実長で見積もる。LLM 束ねだけが未知 (≒500字)。
    完全一致は原理的に無理 (LLM 出力長は事前に分からない) — 見積もりの
    ズレは発火閾値の境界でしか効かない。
    """
    from sai_memory.arasuji.alignment import CHUNK_EPISODE_DIGEST, CHUNK_IDENTITY

    if kind == CHUNK_IDENTITY:
        return sum(len(m.content or "") for m in messages) + 40 * len(messages)
    if kind == CHUNK_EPISODE_DIGEST and digest_text:
        return len(str(digest_text).strip())
    return EST_PARENT_CHARS


def fire_threshold() -> int:
    """発火閾値 X = 提示予算の 1/4 (独立ノブを作らない — intent §3)。"""
    from sai_memory.arasuji.context import USE_DEFAULT_BUDGET, _resolve_char_budget

    return max(1, _resolve_char_budget(USE_DEFAULT_BUDGET) // 4)


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
    (Codex W4 四巡 #1)。
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
# 列の読み出し (実行 / dry 共通)
# ---------------------------------------------------------------------------


def _coverage_index(conn: sqlite3.Connection) -> dict:
    """全 Chronicle entry の {id: coverage_chars}。

    未 backfill (coverage_chars なし) の entry は content 長 × 圧縮率近似で
    読む — dry 予測は backfill より先に走ることがあり、0 扱いだと未帰化 DB
    で band call をゼロ予測して確認ゲートを素通りする (Codex W4 二巡 #3)。
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


@dataclass
class _ColumnItem:
    """列の 1 ノード (実 entry / dry の模擬ノード共用)。"""

    coverage: int
    chars: int
    start_time: Optional[int]
    end_time: Optional[int]
    entry: Optional[ArasujiEntry] = None  # dry の模擬ノードでは None
    excluded: bool = False  # 圧縮区間として提示中 — 数えないが境界にはなる

    @property
    def safe_coverage(self) -> int:
        return max(1, self.coverage)


def _load_column(
    conn: sqlite3.Connection,
    *,
    excluded_entry_ids: Optional[Set[str]] = None,
) -> List[_ColumnItem]:
    """列 = 未束ね General ノードの時系列列 (level 混在の一本)。

    - 孤児 (他 entry の被覆範囲に真に内包される未束ねノード) は列から外す —
      回収は治療 §5-2 の管轄 (未実装の間は現状維持で残る)。
    - ``excluded_entry_ids`` (圧縮区間として提示コンテキストに表示中の digest)
      は X の勘定からも束ね対象からも外すが、**列の境界としては残す**
      (excluded マーク)。外して詰めると、その体験を跨いだ偽の隣接ができる。
    """
    from sai_memory.arasuji.storage import _ENTRY_COLUMNS, _row_to_entry

    rows = conn.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM arasuji_entries "
        "WHERE is_consolidated = 0 AND origin_track_id IS NULL "
        "AND (is_incomplete IS NULL OR is_incomplete = 0) "
        "ORDER BY end_time ASC, start_time ASC, created_at ASC",
    ).fetchall()
    coverage = _coverage_index(conn)

    # 孤児判定用: 全 chronicle entry の被覆範囲。
    ranges = conn.execute(
        "SELECT id, json_extract(metadata, '$.start_time'), "
        "json_extract(metadata, '$.end_time') "
        "FROM memopedia_pages WHERE category = 'chronicle'"
    ).fetchall()

    excluded = excluded_entry_ids or set()
    out: List[_ColumnItem] = []
    for row in rows:
        entry = _row_to_entry(row)
        if _is_orphan(entry, ranges):
            continue
        out.append(_ColumnItem(
            coverage=coverage.get(entry.id, 0),
            chars=len(entry.content or ""),
            start_time=entry.start_time,
            end_time=entry.end_time,
            entry=entry,
            excluded=entry.id in excluded,
        ))
    return out


def _is_orphan(entry: ArasujiEntry, ranges: Sequence[Tuple]) -> bool:
    """他 entry の被覆範囲に真に内包されるか (少なくとも片側が strict)。"""
    if entry.start_time is None or entry.end_time is None:
        return False
    for other_id, st, et in ranges:
        if other_id == entry.id or st is None or et is None:
            continue
        st_i, et_i = int(st), int(et)
        if st_i <= entry.start_time and entry.end_time <= et_i:
            if st_i < entry.start_time or entry.end_time < et_i:
                return True
    return False


def _edge_source_rowid(
    conn: sqlite3.Connection, item: "_ColumnItem", *, newest: bool,
) -> Optional[int]:
    """item の境界側 source メッセージの rowid (正典順序の端点)。

    正典順序は ``(created_at, rowid)`` の辞書式なので、境界「秒」だけでは
    端点を厳密に表せない (Codex 再レビュー 必須3)。lv1 entry なら source_ids
    から端のメッセージの rowid を引けるので、その created_at が entry の境界
    時刻と一致するときだけ rowid を返す (不一致 = 境界秒に source が無い =
    秒比較へフォールバック)。上位 entry / dry の模擬ノードは None (保守側 =
    秒の閉区間で見る)。
    """
    entry = item.entry
    if entry is None or entry.level != 1 or not entry.source_ids:
        return None
    boundary = item.end_time if newest else item.start_time
    if boundary is None:
        return None
    ph = ",".join("?" for _ in entry.source_ids)
    order = "DESC" if newest else "ASC"
    row = conn.execute(
        f"SELECT rowid, created_at FROM messages WHERE id IN ({ph}) "
        f"ORDER BY created_at {order}, rowid {order} LIMIT 1",
        tuple(entry.source_ids),
    ).fetchone()
    if row is None or row[1] != boundary:
        return None
    return int(row[0])


def _uncompiled_gap(
    conn: sqlite3.Connection,
    prev: "_ColumnItem",
    nxt: "_ColumnItem",
    pending_json: Optional[str] = None,
) -> bool:
    """隣接ノード間のギャップに「未編纂の生ログ」が居るか (連続性条件)。

    Chronicle 対象タグのメッセージで、**どの一次あらすじの source_ids にも
    入っていない**ものがギャップ内に存在するなら True — そこを跨ぐ束ねは
    偽の隣接 (§4-5) になる。

    判定は時間範囲の近似でなく source 帰属 (lv1 の source_ids 逆引き) で行う
    (Codex レビュー P1-2, 2026-07-27): messages の正典順序は
    ``(created_at, rowid)`` の辞書式なので、entry 境界と同じ秒に rowid 違いの
    未編纂メッセージが挟まりうる。区間は境界の端点で見る — lv1 同士の隣接では
    端の source の rowid まで含めた keyset 比較 (:func:`_edge_source_rowid`)、
    rowid を引けない側は秒の閉区間 (保守側 = 偽ギャップは束ねを待たせるだけで
    自然解消する。逆向きの誤りは偽の隣接 = 不可逆)。

    ``pending_json`` (JSON 配列の文字列) は「これから編纂される予定のメッセージ
    id」— dry 予測 (:func:`plan_band_overflow`) が新チャンク確定後の世界を
    再現するための除外集合 (Codex 再レビュー 必須2: これが無いと dry では
    模擬ノード自身の生ログが未編纂ギャップに見え、実行と列の形が食い違う)。
    """
    prev_end, next_start = prev.end_time, nxt.start_time
    if prev_end is None or next_start is None:
        return False
    if next_start < prev_end:
        return False
    prev_rowid = _edge_source_rowid(conn, prev, newest=True)
    next_rowid = _edge_source_rowid(conn, nxt, newest=False)

    params: List = []
    if prev_rowid is not None:
        lower = "(m.created_at > ? OR (m.created_at = ? AND m.rowid > ?))"
        params += [prev_end, prev_end, prev_rowid]
    else:
        lower = "m.created_at >= ?"
        params += [prev_end]
    if next_rowid is not None:
        upper = "(m.created_at < ? OR (m.created_at = ? AND m.rowid < ?))"
        params += [next_start, next_start, next_rowid]
    else:
        upper = "m.created_at <= ?"
        params += [next_start]
    pending_clause = ""
    if pending_json:
        pending_clause = (
            "AND m.id NOT IN (SELECT value FROM json_each(?)) "
        )
        params.append(pending_json)

    tag_ph = ",".join("?" for _ in CHRONICLE_EXCLUDED_TAGS)
    role_ph = ",".join("?" for _ in CHRONICLE_EXCLUDED_LINE_ROLES)
    row = conn.execute(
        f"""
        SELECT 1 FROM messages m
        WHERE {lower} AND {upper}
        {pending_clause}
        AND m.thread_id NOT IN (SELECT thread_id FROM stelis_threads)
        AND NOT EXISTS (
            SELECT 1 FROM json_each(m.metadata, '$.tags')
            WHERE json_each.value IN ({tag_ph})
        )
        AND (m.line_role IS NULL OR m.line_role NOT IN ({role_ph}))
        AND NOT EXISTS (
            SELECT 1
            FROM memopedia_pages p, json_each(p.metadata, '$.source_ids') s
            WHERE p.category = 'chronicle'
            AND json_extract(p.metadata, '$.level') = 1
            AND s.value = m.id
        )
        LIMIT 1
        """,
        tuple(params) + CHRONICLE_EXCLUDED_TAGS + CHRONICLE_EXCLUDED_LINE_ROLES,
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# 群の区切りと計画 (実行 / dry 共通の決定論)
# ---------------------------------------------------------------------------

#: 群と群の間の境界の種類。'ratio' = 質量比率で割れた (治療の対象になりうる) /
#: 'gap' = 未編纂の生ログが挟まる (後で埋まりうる — 待つ) /
#: 'excluded' = 提示中の圧縮区間 digest が挟まる (anchor 前進で列に来る — 待つ)
_BOUNDARY_RATIO = "ratio"
_BOUNDARY_GAP = "gap"
_BOUNDARY_EXCLUDED = "excluded"


@dataclass
class _Run:
    items: List[_ColumnItem] = field(default_factory=list)
    boundary_after: Optional[str] = None  # None = 列の最新端

    @property
    def max_coverage(self) -> int:
        return max((i.safe_coverage for i in self.items), default=0)

    @property
    def min_coverage(self) -> int:
        return min((i.safe_coverage for i in self.items), default=0)

    @property
    def total_coverage(self) -> int:
        return sum(i.safe_coverage for i in self.items)

    @property
    def total_chars(self) -> int:
        return sum(i.chars for i in self.items)


def _partition_runs(
    conn: sqlite3.Connection,
    column: Sequence[_ColumnItem],
    pending_json: Optional[str] = None,
) -> List[_Run]:
    """列を「比率10倍以内・連続」の群 (run) に区切る。

    excluded ノードは群に入らず、境界 (excluded) として前の群を閉じる。
    ``pending_json`` は dry 予測用の「編纂予定メッセージ id」除外
    (:func:`_uncompiled_gap` 参照)。
    """
    runs: List[_Run] = []
    current = _Run()
    for item in column:
        if item.excluded:
            if current.items:
                current.boundary_after = _BOUNDARY_EXCLUDED
                runs.append(current)
            elif runs and runs[-1].boundary_after is None:
                runs[-1].boundary_after = _BOUNDARY_EXCLUDED
            current = _Run()
            continue
        if not current.items:
            current.items.append(item)
            continue
        prev = current.items[-1]
        if _uncompiled_gap(conn, prev, item, pending_json):
            current.boundary_after = _BOUNDARY_GAP
            runs.append(current)
            current = _Run(items=[item])
            continue
        new_max = max(current.max_coverage, item.safe_coverage)
        new_min = min(current.min_coverage, item.safe_coverage)
        if new_max > new_min * MASS_RATIO_LIMIT:
            current.boundary_after = _BOUNDARY_RATIO
            runs.append(current)
            current = _Run(items=[item])
            continue
        current.items.append(item)
    if current.items:
        runs.append(current)
    return runs


def _find_bundle_windows(items: Sequence[_ColumnItem]) -> List[List[_ColumnItem]]:
    """卒業する束ねの窓を、連続性区間の中から重ならない形で全て列挙する。

    窓の条件は群の三条件のうち②③ (①連続性は呼び出し側が区間で保証):
    比率 (窓内の質量 max/min ≤ 10) と卒業 (合算 ≥ GRADUATION_FACTOR × 窓内最大)。

    **比率の区切りより窓が先** (Codex レビュー P1-1, 2026-07-27): 先に列全体を
    比率で群に区切ると、重い頭が射程を伸ばして軽い並びを分断し、本来卒業できる
    窓 (例: [1000, 101×4, 99×2] の中の [101×4, 99×2]) を見落とす。窓探しは
    区間の生の並びに対して行い、比率は窓ごとに検査する。

    各開始位置で「比率が保てる限り伸ばして、卒業が成立している最長の窓」を
    取り、見つけたら次の窓はその直後から探す (重ならない = 同一 plan 内で子を
    取り合わない)。**全部列挙するのは、実行順の「軽い群から」を窓の発見順で
    先取りしないため** — 同じ区間に重い窓と軽い窓が並ぶとき、どれを今回の
    枠で使うかは :func:`_plan_actions` の並べ替えが決める。
    2 件未満の窓は束ねにならないので対象外。
    """
    windows: List[List[_ColumnItem]] = []
    n = len(items)
    start = 0
    while start < n - 1:
        lo = hi = items[start].safe_coverage
        total = items[start].safe_coverage
        best_end: Optional[int] = None
        for j in range(start + 1, n):
            cov = items[j].safe_coverage
            lo, hi = min(lo, cov), max(hi, cov)
            if hi > lo * MASS_RATIO_LIMIT:
                break
            total += cov
            if total >= GRADUATION_FACTOR * hi:
                best_end = j
        if best_end is not None:
            windows.append(list(items[start:best_end + 1]))
            start = best_end + 1
        else:
            start += 1
    return windows


#: 計画された 1 アクション。kind: 'bundle' (卒業束ね) / 'treatment' (治療合流) /
#: 'valve' (非常弁)。
@dataclass
class _Action:
    kind: str
    items: List[_ColumnItem]

    @property
    def max_coverage(self) -> int:
        return max((i.safe_coverage for i in self.items), default=0)

    @property
    def chars_saved(self) -> int:
        return sum(i.chars for i in self.items) - EST_PARENT_CHARS


def _plan_actions(
    conn: sqlite3.Connection,
    column: Sequence[_ColumnItem],
    *,
    max_actions: int,
    pending_json: Optional[str] = None,
) -> List[_Action]:
    """発火判定と群の選抜 — 実行 (run) と dry 予測 (plan) が共有する決定論。

    1. 治療は発火と無関係に常時 (束ね不能は単調悪化するので待つ利益がない)。
       窓のある区間でも、窓と重ならない残余の治療は探索する (Codex 再レビュー
       必須1 — 区間単位のスキップは治療の即時性を破る)
    2. 発火 (字数 > X) していれば、卒業する窓 (:func:`_find_bundle_windows`) を
       束ね候補に。区間 = 比率境界で繋がった群の並び — 窓探しは区間の生の
       並びに対して行う (Codex レビュー P1-1)
    3. 最新端の育ち中の群は、他のアクションで X を割れるなら猶予
    4. 軽いアクション (最大メンバーが小さい) から、上限 max_actions 件
    5. 発火したのに何も打てない予見外の形だけ、非常弁 (最古の隣接2件)
    """
    eligible_chars = sum(i.chars for i in column if not i.excluded)
    threshold = fire_threshold()
    fired = eligible_chars > threshold

    runs = _partition_runs(conn, column, pending_json)
    actions: List[_Action] = []

    # 連続性区間 = 'ratio' 境界で繋がった run の並び。区間の切れ目は
    # gap / excluded / 列の最新端だけ。
    segments: List[Tuple[List[int], Optional[str]]] = []
    seg_start = 0
    for idx, run in enumerate(runs):
        if run.boundary_after != _BOUNDARY_RATIO:
            segments.append((list(range(seg_start, idx + 1)), run.boundary_after))
            seg_start = idx + 1
    is_open_ended = bool(runs) and runs[-1].boundary_after is None

    for seg_indices, _seg_boundary in segments:
        seg_items: List[_ColumnItem] = []
        for ri in seg_indices:
            seg_items.extend(runs[ri].items)
        windows = _find_bundle_windows(seg_items)
        for window in windows:
            # 窓が列の末尾に届き、かつ列が開いている (最新端) なら育ち中。
            is_newest = (
                is_open_ended
                and seg_indices[-1] == len(runs) - 1
                and window[-1] is seg_items[-1]
            )
            actions.append(_Action(
                kind="bundle_newest" if is_newest else "bundle",
                items=window,
            ))
        # 束ね不能 (軽い側で行き詰まり) の治療 — 窓の有無に関わらず探索する
        # (Codex 再レビュー 必須1)。窓との交差はここでは見ない — 候補の窓は
        # 発火・上限・猶予でまだ落ちうるので、実行されない窓で治療をブロック
        # しないよう、衝突の解決は最終選抜の後 (下の重なり除去) で行う
        # (Codex 三巡 P1-A)。区間内の境界は全て 'ratio'。gap / excluded 境界の
        # 先・最新端は「後で埋まる・戻ってくる・まだ育つ」ので待つ。
        consumed = 0
        for pos, ri in enumerate(seg_indices):
            run = runs[ri]
            avail = run.items[consumed:]
            consumed = 0
            if not avail:
                continue
            if pos == len(seg_indices) - 1:
                continue  # 区間の最後の群 — 隣は gap/excluded/列端の向こう
            next_run = runs[seg_indices[pos + 1]]
            if not next_run.items:
                continue
            neighbor = next_run.items[0]
            if neighbor.safe_coverage <= max(i.safe_coverage for i in avail):
                continue  # 自分が重い側 — 隣が育って戻ってくるのを待つ
            actions.append(_Action(kind="treatment", items=avail + [neighbor]))
            consumed = 1  # 次の群の先頭を取った

    # 発火していなければ束ねは落とす (治療は常時)。
    if not fired:
        actions = [a for a in actions if a.kind == "treatment"]

    # 軽い群から (重い群が先に梯子を登ると比率差が永久に開く)。
    actions.sort(key=lambda a: a.max_coverage)

    # 選抜: 重なり除去 → 上限 → 最新端の猶予。猶予でアクションが落ちたら
    # **候補から除いて選抜をやり直す** (Codex 四巡 P1-a — 猶予で落ちる窓は
    # 「実行されない窓」なので、それが重なり除去で治療を負かしたままだと
    # 治療がブロックされる。実行されないアクションは子を予約しない、を
    # 選抜全体の不変条件にする)。猶予対象 (bundle_newest) は高々 1 系統
    # なのでループは 2 周で必ず止まる。
    candidates = actions
    while True:
        # 重なり除去 — 同じ子を二つのアクションに入れない。軽い順に先着で
        # 確定し、負けた側は次回の Metabolism で再計画される (勝った側が
        # 束ねられれば、負けた治療はより良い相手 = 窓の親を得る)。
        selected: List[_Action] = []
        used_ids: Set[int] = set()
        for a in candidates:
            item_ids = {id(x) for x in a.items}
            if item_ids & used_ids:
                continue
            selected.append(a)
            used_ids |= item_ids
        selected = selected[:max_actions]

        # 最新端の育ち中の群は、他のアクションで X を割れるなら猶予する。
        newest = [a for a in selected if a.kind == "bundle_newest"]
        if newest and fired:
            others_saved = sum(
                a.chars_saved for a in selected if a.kind != "bundle_newest"
            )
            if eligible_chars - others_saved <= threshold:
                candidates = [
                    a for a in candidates if a.kind != "bundle_newest"
                ]
                continue
        break
    actions = selected
    for a in actions:
        if a.kind == "bundle_newest":
            a.kind = "bundle"

    # 非常弁: 発火したのに何も打てない予見外の形。
    if fired and not actions:
        newest_ids = {id(i) for i in runs[-1].items} if runs else set()
        pair = _oldest_adjacent_pair(conn, column, newest_ids, pending_json)
        if pair is not None:
            actions = [_Action(kind="valve", items=list(pair))]
        else:
            LOGGER.debug(
                "[bands] fired (%d chars > %d) but no action available — "
                "column is waiting (growing newest run / heavy heads / gaps)",
                eligible_chars, threshold,
            )
    return actions


def _oldest_adjacent_pair(
    conn: sqlite3.Connection,
    column: Sequence[_ColumnItem],
    newest_run_ids: Set[int],
    pending_json: Optional[str] = None,
) -> Optional[Tuple[_ColumnItem, _ColumnItem]]:
    """非常弁の対象 — 最古の隣接2件 (比率の壁だけを免除する)。

    列の並びをそのまま歩き、次は跨がない:

    - excluded (提示中の圧縮区間) / gap (未編纂の生ログ) — 体験が挟まっている
    - 最新端の育ち中の群の中 — まだメンバーが増えるので待つのが正常
    - 質量の逆転 (古い側が新しい側の10倍超) — 巨大な頭を小粒へ溶かす merge は
      どの目的にも合わない。非常弁も「軽い側を重い側へ」だけ
    - **U (一次あらすじの標準被覆) 以上のノード** — 非常弁は小粒の圧縮止まり。
      重い頭 (束ね済みの親たち) は正常な待機状態でも列に並ぶので、非常弁が
      それを溶かすと「治療が尽きた過渡期に古い記憶が余分に再要約される」。
      歯止めは目的 (重い記憶を巻き込まない) から導く
    """
    from sai_memory.arasuji.alignment import chronicle_band_budget

    u = chronicle_band_budget()
    for i in range(len(column) - 1):
        a, b = column[i], column[i + 1]
        if a.excluded or b.excluded:
            continue
        if id(a) in newest_run_ids or id(b) in newest_run_ids:
            continue
        if a.safe_coverage >= u or b.safe_coverage >= u:
            continue
        if a.safe_coverage > b.safe_coverage * MASS_RATIO_LIMIT:
            continue
        if _uncompiled_gap(conn, a, b, pending_json):
            continue
        return a, b
    return None


# ---------------------------------------------------------------------------
# dry 計画 (確認ゲート / コスト見積もり用 — LLM なし)
# ---------------------------------------------------------------------------


def plan_band_overflow(
    conn: sqlite3.Connection,
    *,
    extra_leaves: Optional[Sequence[Sequence]] = None,
    excluded_entry_ids: Optional[Set[str]] = None,
    pending_source_ids: Optional[Set[str]] = None,
) -> int:
    """束ね (+治療) の発生回数を LLM なしで予測する。

    :func:`run_band_overflow` と同じ計画 (:func:`_plan_actions`) を共有する
    ので、予測と実行は同じアクション列になる。

    Args:
        extra_leaves: これから確定する新チャンクの
            ``(coverage_chars, start_time, end_time[, est_chars])`` 列。列に
            加算して予測する (確認ゲートは実行前に呼ぶため)。第4要素は digest
            字数の見込み — 恒等圧縮チャンクは content が生ログ+装飾なので、
            呼び出し側が実長で渡さないと dry が発火を過小評価し、実行時に
            見積もり外の LLM コールが増える (Codex レビュー P1-3)。省略時は
            ``min(coverage, EST_PARENT_CHARS)`` で近似する。
        excluded_entry_ids: 圧縮区間として提示中の digest entry id 集合。
        pending_source_ids: extra_leaves が覆う予定の生メッセージ id 集合。
            dry の連続性判定でこれを「未編纂」から除外し、新チャンク確定後の
            世界を再現する (Codex 再レビュー 必須2 — 渡さないと模擬ノード
            自身の生ログがギャップに見え、実行と列の形が食い違う)。

    Returns:
        予測される LLM コール回数。
    """
    column = _load_column(conn, excluded_entry_ids=excluded_entry_ids)
    if extra_leaves:
        for leaf in extra_leaves:
            cov_i = int(leaf[0])
            st, et = leaf[1], leaf[2]
            chars = int(leaf[3]) if len(leaf) > 3 and leaf[3] is not None else (
                min(cov_i, EST_PARENT_CHARS)
            )
            column.append(_ColumnItem(
                coverage=cov_i,
                chars=chars,
                start_time=st,
                end_time=et,
            ))
        column.sort(key=lambda i: (
            i.end_time if i.end_time is not None else 0,
            i.start_time if i.start_time is not None else 0,
        ))
    pending_json = (
        json.dumps(sorted(pending_source_ids)) if pending_source_ids else None
    )
    actions = _plan_actions(
        conn, column,
        max_actions=_max_consolidations_per_run(),
        pending_json=pending_json,
    )
    return len(actions)


# ---------------------------------------------------------------------------
# 束ねの実行
# ---------------------------------------------------------------------------


def _build_consolidation_prompt(
    entries: List[ArasujiEntry],
    conn: sqlite3.Connection,
    *,
    include_timestamp: bool = True,
) -> str:
    """親 digest を子 digest 群から語り直すプロンプト (§4-4 で正当)。"""
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


def _digest_origins(
    conn: sqlite3.Connection, entry_ids: Sequence[str],
) -> Dict[str, Optional[str]]:
    """{entry_id: digest_origin} — 恒等圧縮の子の判定用。"""
    if not entry_ids:
        return {}
    ph = ",".join("?" for _ in entry_ids)
    rows = conn.execute(
        "SELECT id, json_extract(metadata, '$.digest_origin') "
        f"FROM memopedia_pages WHERE id IN ({ph})",
        tuple(entry_ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _fire_identity_fragment_callbacks(
    conn: sqlite3.Connection,
    entries: Sequence[ArasujiEntry],
    batch_callback: Optional[Callable],
) -> None:
    """恒等圧縮の子の Fragment 抽出 (intent §7)。

    恒等圧縮は生ログのまま一次あらすじの席にいたので、束ね/治療で**初めて
    要約に変わる**この瞬間が「生ログが要約に置き換わる瞬間に一度だけ抽出」の
    実行点。LLM 束ね済みの子の範囲は既に抽出済みなので走らせない (Fragment
    の重複と参照切れを避ける)。chronicle_entry_id は恒等圧縮の子自身。
    """
    if batch_callback is None:
        return
    origins = _digest_origins(conn, [e.id for e in entries])
    for entry in entries:
        if origins.get(entry.id) != "identity":
            continue
        if not entry.source_ids:
            continue
        try:
            messages = _load_messages_by_ids(conn, entry.source_ids)
            if messages:
                batch_callback(messages, entry.id)
        except Exception:
            LOGGER.exception(
                "[bands] fragment callback failed for identity child %s",
                entry.id[:8],
            )


def _load_messages_by_ids(conn: sqlite3.Connection, message_ids: Sequence[str]):
    from sai_memory.memory.storage import _row_to_message

    ph = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
        f"FROM messages WHERE id IN ({ph}) "
        "ORDER BY created_at ASC, rowid ASC",
        tuple(message_ids),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def _consolidate_action(
    conn: sqlite3.Connection,
    client,
    action: _Action,
    *,
    persona_id: Optional[str],
    batch_callback: Optional[Callable] = None,
) -> Optional[ArasujiEntry]:
    """アクション 1 件 (束ね/治療/非常弁) を親ノードに確定する (親 + 子を単一 tx)。"""
    entries = [i.entry for i in action.items if i.entry is not None]
    if len(entries) != len(action.items) or len(entries) < 2:
        return None
    target_level = max(e.level for e in entries) + 1
    prompt = _build_consolidation_prompt(entries, conn)
    from sai_memory.arasuji.generator import _record_llm_usage
    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}], tools=[],
        )
        _record_llm_usage(client, persona_id, f"chronicle_level{target_level}")
    except Exception:
        LOGGER.exception(
            "[bands] consolidation LLM failed (kind=%s, %d entries)",
            action.kind, len(entries),
        )
        return None
    content = (response or "").strip()
    if not content:
        LOGGER.warning("[bands] empty consolidation response (kind=%s)", action.kind)
        return None

    starts = [e.start_time for e in entries if e.start_time is not None]
    ends = [e.end_time for e in entries if e.end_time is not None]
    total_coverage = sum(i.coverage for i in action.items)
    child_ids = [e.id for e in entries]
    extra_metadata = {
        "digest_origin": "band",
        "coverage_chars": total_coverage,
    }
    if action.kind != "bundle":
        extra_metadata["band_kind"] = action.kind
    try:
        # tx 内再検査 (Codex W4 #2/二巡 #2/三巡 #2): BEGIN IMMEDIATE で write
        # ロックを取ってから子の未統合を確認する — SELECT は暗黙 BEGIN を張らない
        # ため、ロックなしでは二接続が同時に「全子未統合」を読める。
        # 既に tx 内なら参加する (旧実装からの前提)。通常経路は backfill /
        # execute_plan が commit 済みで clean に入る — 未確定の他所の書き込みを
        # 抱えた conn で呼ぶと、それも巻き込んで commit/rollback される
        # (Codex レビュー P2 で確認済みの既存性質。呼び出し側の契約)。
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if not _all_children_unconsolidated(conn, child_ids):
            conn.rollback()
            LOGGER.warning(
                "[bands] consolidation abandoned: children consolidated "
                "concurrently (kind=%s)", action.kind,
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
            extra_metadata=extra_metadata,
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
            "[bands] consolidation tx failed (kind=%s); rolled back", action.kind,
        )
        return None
    LOGGER.info(
        "[bands] %s: %d nodes -> level %d (coverage=%d chars, parent=%s)",
        action.kind, len(entries), target_level, total_coverage, parent.id[:8],
    )
    # Fragment 抽出は commit 後 (executor.py:306 と同型の位置)。
    _fire_identity_fragment_callbacks(conn, entries, batch_callback)
    return parent


def run_band_overflow(
    conn: sqlite3.Connection,
    client,
    *,
    persona_id: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    excluded_entry_ids: Optional[Set[str]] = None,
    batch_callback: Optional[Callable] = None,
) -> int:
    """列を検査し、計画されたアクション (束ね/治療/非常弁) を実行する。

    計画は :func:`_plan_actions` — dry 予測 (:func:`plan_band_overflow`) と
    同じ決定論を共有する。1 回の呼び出しの LLM コールは安全弁 (既定 3) まで。

    Args:
        excluded_entry_ids: 圧縮区間として提示コンテキストに表示中の digest
            entry id 集合 (列の勘定・束ね対象から外し、境界としてだけ残す)。
        batch_callback: Fragment 抽出コールバック
            ``(List[Message], chronicle_entry_id) -> None``。恒等圧縮の子が
            初めて要約に変わる束ね/治療でのみ、その子の生メッセージで呼ぶ。

    Returns:
        作った親ノード数。
    """
    column = _load_column(conn, excluded_entry_ids=excluded_entry_ids)
    actions = _plan_actions(conn, column, max_actions=_max_consolidations_per_run())
    created = 0
    for action in actions:
        if cancel_check and cancel_check():
            break
        if action.kind == "valve":
            LOGGER.warning(
                "[bands] last-resort valve fired: bundling oldest adjacent pair "
                "ratio-free (column stuck above fire threshold). This should "
                "never happen in normal operation — investigate the column shape.",
            )
        parent = _consolidate_action(
            conn, client, action,
            persona_id=persona_id, batch_callback=batch_callback,
        )
        if parent is not None:
            created += 1
    return created
