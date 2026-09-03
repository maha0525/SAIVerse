"""Chronicle 束ね — レベル別の並び + 予算超過で畳む一本規則 (docs/intent/arasuji_levels.md).

旧「字数発火・質量選抜」(一本の列 + 比率10倍/卒業5倍/治療/非常弁) を
2026-07-28 に世代交代した。設計正典は docs/intent/arasuji_levels.md。

概念:

- **並び** = 同じレベルの未束ね (unconsolidated) ノードを時系列に並べたもの。
  レベルごとに独立の並びを持つ (レベル混在の一本列は廃止)。
- **予算** = 並びごとの「上限」と「残す量」の 2 つの数 (字数 = digest テキスト
  自体の長さ)。合計字数が上限を超えたら発火し、古い側を「残す量」に収まる
  まで切り取って 1 個の親に畳み、1 つ上のレベルの並びへ送る。
  上限と残す量の差がバッファ — 発火は「たまに・まとめて」が正しい
  (少しずつ畳む形は提示を頻繁に変えて LLM キャッシュを壊す)。
- **レベル分離** = 畳んだ結果は自分の並びに戻らず 1 つ上へ行く。同じ内容が
  再要約される回数はこの構造だけで log 有限になる (無限圧縮の防止)。
- メンバーの大きさ (被覆 coverage_chars) は**判定に使わない**。被覆は
  「あらすじ → 元の体験」を辿る錨・統計としてだけ記録し続ける (intent §3-5)。
- 材料には各項目の**種別** (あらすじ / 生ログ断片) を明示して LLM に渡す —
  イレギュラーな混入があっても機構も LLM も壊れないため (intent §3-4)。

判定はすべて決定論 — LLM が決めるのはあらすじ本文だけ。

原子性: 親 INSERT + 子 mark_consolidated を**単一トランザクション**で確定し、
tx 内で全子が未束ねのままかを再検査する (並走ジョブが同じ子列を束ねた場合は
放棄)。

:func:`plan_band_overflow` は同じ計画ロジックの dry 実行 — LLM を呼ばずに
束ね回数 (連鎖含む) を予測する。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from sai_memory.arasuji.storage import (
    ArasujiEntry,
    _get_chronicle_page_row,
    _parse_chronicle_meta,
    create_entry,
    get_max_level,
    mark_consolidated,
)

LOGGER = logging.getLogger(__name__)

#: run_band_overflow **1 回の呼び出し**で走らせる LLM コール (束ね) の上限。
#: generate_chronicle はチャンク確定のたびに呼ぶので、大量編纂の走行全体では
#: 確認ゲートで承認された dry 予測件数 (max_folds) まで束ねが積み上がる。
DEFAULT_MAX_CONSOLIDATIONS_PER_RUN = 3

#: レベル 1 以上の各並びの予算 — 上限 (これを超えたら発火)。intent §9。
BAND_CHAR_LIMIT = 5_000

#: レベル 1 以上の各並びの予算 — 残す量 (畳み後にこの字数まで残す)。
#: 上限との差がバッファ = 発火間隔。intent §9。
BAND_CHAR_KEEP = 2_500

#: 計画時の親あらすじ字数の見込み (LLM 指示は 5〜8 文 ≒ 500 字)。
EST_PARENT_CHARS = 500


def estimate_leaf_chars(kind: str, messages: Sequence, digest_text) -> int:
    """extra_leaves の第4要素 (これから確定するチャンクの digest 字数見込み)。

    dry 予測と実行の乖離を抑える: LLM 束ねの出力長は未知 (≒500字)。
    完全一致は原理的に無理 — 見積もりのズレは発火閾値の境界でしか効かない。
    """
    return EST_PARENT_CHARS


def _max_consolidations_per_run() -> int:
    """run_band_overflow **1 回の呼び出し**あたりの LLM コール上限 (安全弁)。

    env 名の ``PER_RUN`` は互換のため残しているが、単位は Metabolism の走行
    ではなく run_band_overflow の呼び出し。generate_chronicle は確定した
    チャンクごとに呼ぶので、大量編纂 1 走行の束ねの総数は承認済みの dry
    予測件数 (``max_folds`` の累計) まで届く (2026-09-03 まはー裁定 — 走行
    全体を 3 回で切ると大量編纂で階層が育たず、挟み込みの目的が果たせない。
    コストの上限は確認ゲートが担い、この env は 1 回の呼び出しだけを縛る)。
    """
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
# coverage_chars — 帰化バックフィル (統計・錨として維持。判定には使わない)
# ---------------------------------------------------------------------------

#: 全 source が引けないときの近似: digest は原文の約 1/10 という圧縮率仮定。
_COVERAGE_FALLBACK_RATIO = 10


def backfill_coverage(conn: sqlite3.Connection) -> int:
    """coverage_chars の無い既存 entry に被覆字数を刻む。

    - level 1: source_ids → messages.content 長合計 (欠損 source は引けた分
      のみ。全滅時は content 長 × 10 の圧縮率近似)。
    - level 2+: 子 entry の coverage 合計 (level 昇順に処理するので子は先に
      埋まっている)。子欠損は同近似。
    - source / 子の**一部でも**引けなかった entry には ``coverage_estimated``
      マーカーを付ける (過小評価の可能性を観測可能にする)。

    冪等 (coverage_chars が入っている entry はスキップ)。LLM なし。
    戻り値 = 埋めた entry 数。

    metadata の read-modify-write を並走書き込み (束ねの is_consolidated
    更新等) と競合させないため、全体を **BEGIN IMMEDIATE の単一 tx** で行う。
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
# 並びの読み出し (実行 / dry 共通)
# ---------------------------------------------------------------------------


def _coverage_index(conn: sqlite3.Connection) -> dict:
    """全 Chronicle entry の {id: coverage_chars}。

    未 backfill (coverage_chars なし) の entry は content 長 × 圧縮率近似で
    読む — dry 予測は backfill より先に走ることがあるため。
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
class _RowItem:
    """並びの 1 ノード (実 entry / dry の模擬ノード共用)。"""

    coverage: int
    chars: int
    start_time: Optional[int]
    end_time: Optional[int]
    entry: Optional[ArasujiEntry] = None  # dry の模擬ノードでは None
    excluded: bool = False  # 圧縮区間として提示中 — 畳まず、字数も数えない
    #: 直前のノードとの間に「未編纂の編纂対象メッセージ」が居る = 畳み範囲は
    #: この手前で切れる (跨ぐと偽の隣接になり、後から編纂されたあらすじが
    #: 親の被覆範囲に内包されて孤児化する — Codex レビュー 2026-07-28 high1)。
    gap_before: bool = False

    @property
    def safe_coverage(self) -> int:
        return max(1, self.coverage)


def _load_rows(
    conn: sqlite3.Connection,
    *,
    excluded_entry_ids: Optional[Set[str]] = None,
) -> Dict[int, List[_RowItem]]:
    """レベル別の並び = {level: 未束ねノードの時系列列}。

    - 孤児 (他 entry の被覆範囲に真に内包される未束ねノード) は並びから外す —
      畳むと同じ体験を二重に覆うため (回収は別課題、現状維持で残る)。
    - ``excluded_entry_ids`` (圧縮区間として提示コンテキストに表示中の digest)
      は字数の勘定からも畳み対象からも外すが、**並びには残す** (excluded
      マーク)。畳みの範囲はこれを跨がない。
    """
    from sai_memory.arasuji.storage import _ENTRY_COLUMNS, _row_to_entry

    # origin_track_id / is_incomplete のフィルタは、書き手が退役した今も残す
    # (track_retirement.md 住人 5)。新しい entry はどちらも必ず NULL / 0 になるが、
    # **既存 DB には Track Chronicle 時代の行がそのまま残っている** — 外すと当時
    # Track 単位で書かれた作業メモが一般 Chronicle の並びに混ざる。
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
    out: Dict[int, List[_RowItem]] = {}
    for row in rows:
        entry = _row_to_entry(row)
        if _is_orphan(entry, ranges):
            continue
        out.setdefault(entry.level, []).append(_RowItem(
            coverage=coverage.get(entry.id, 0),
            chars=len(entry.content or ""),
            start_time=entry.start_time,
            end_time=entry.end_time,
            entry=entry,
            excluded=entry.id in excluded,
        ))
    for level, level_row in out.items():
        _mark_uncompiled_gaps(conn, level, level_row)
    return out


def _edge_source_rowid(
    conn: sqlite3.Connection, item: "_RowItem", *, newest: bool,
) -> Optional[int]:
    """item の境界側 source メッセージの rowid (正典順序の端点)。

    messages の正典順序は ``(created_at, rowid)`` の辞書式なので、境界「秒」
    だけでは端点を厳密に表せない。lv1 entry なら source_ids から端の
    メッセージの rowid を引けるので、その created_at が entry の境界時刻と
    一致するときだけ rowid を返す (不一致 = 境界秒に source が無い = 秒比較へ
    フォールバック)。上位 entry / dry の模擬ノードは None (保守側 = 秒の
    閉区間で見る)。
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


def _mark_uncompiled_gaps(
    conn: sqlite3.Connection, level: int, row: List[_RowItem],
) -> None:
    """隣接ノード間に「まだこのレベルまで上がってきていない体験」が居る境界へ
    印を付ける。2 種類を見る:

    1. **未編纂の編纂対象メッセージ** (どの一次あらすじの source にも入って
       いない生ログ)。跨いで畳むと、親は間の体験を材料にしないまま地続きに
       語る (偽の隣接)。さらに間の生ログが後から一次あらすじ化されると、その
       新エントリは親の被覆時間範囲に真に内包され、孤児判定で**永久に**並び
       から除外される (Codex レビュー 2026-07-28 high1)。
    2. **未統合の下位レベルノード** (level < このレベル、区間に重なる範囲)。
       レベル2 以上の並びでは、間の生ログが一次あらすじ化された「直後」も
       まだレベル2 に上がっていない — その一次あらすじを跨いで上位親を作ると
       同じ孤児化が起きる (同レビュー二巡 high1)。

    定常運転では起きない (レベル0 は古い側から漏れなく畳む) が、手動生成の
    途中打ち切りや旧世代の取り残しでは現実に起きる形。

    1 の区間の端点は正典順序 ``(created_at, rowid)`` のキーセットで見る
    (:func:`_edge_source_rowid`)。rowid を引けない側は秒の閉区間 (保守側 =
    偽の境界は畳みを待たせるだけで、間の体験が畳まれれば自然解消する。
    逆向きの誤りは偽の隣接 = 不可逆)。
    """
    from sai_memory.memory.storage import chronicle_eligibility_filter

    clause, params = chronicle_eligibility_filter()
    for i in range(1, len(row)):
        prev, nxt = row[i - 1], row[i]
        if prev.end_time is None or nxt.start_time is None:
            continue
        if nxt.start_time < prev.end_time:
            continue
        # 1. 未編纂の生ログ (正典順序キーセット)
        prev_rowid = _edge_source_rowid(conn, prev, newest=True)
        next_rowid = _edge_source_rowid(conn, nxt, newest=False)
        sql_params: List = []
        if prev_rowid is not None:
            lower = "(created_at > ? OR (created_at = ? AND rowid > ?))"
            sql_params += [prev.end_time, prev.end_time, prev_rowid]
        else:
            lower = "created_at >= ?"
            sql_params += [prev.end_time]
        if next_rowid is not None:
            upper = "(created_at < ? OR (created_at = ? AND rowid < ?))"
            sql_params += [nxt.start_time, nxt.start_time, next_rowid]
        else:
            upper = "created_at <= ?"
            sql_params += [nxt.start_time]
        hit = conn.execute(
            f"""
            SELECT 1 FROM messages
            WHERE {lower} AND {upper}
            AND {clause}
            AND NOT EXISTS (
                SELECT 1 FROM arasuji_entries a, json_each(a.source_ids_json) s
                WHERE a.level = 1 AND s.value = messages.id
            )
            LIMIT 1
            """,
            tuple(sql_params) + params,
        ).fetchone()
        # 2. 未統合の下位レベルノード (レベル2 以上の並びのみ)
        # origin_track_id / is_incomplete のフィルタを残す理由は _load_rows と同じ
        # (既存 DB に残る Track Chronicle 時代の行を並びへ混ぜない)。
        if hit is None and level > 1:
            hit = conn.execute(
                "SELECT 1 FROM arasuji_entries "
                "WHERE is_consolidated = 0 AND origin_track_id IS NULL "
                "AND (is_incomplete IS NULL OR is_incomplete = 0) "
                "AND level < ? AND end_time >= ? AND start_time <= ? "
                "LIMIT 1",
                (level, prev.end_time, nxt.start_time),
            ).fetchone()
        if hit is not None:
            nxt.gap_before = True


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


# ---------------------------------------------------------------------------
# 計画 (実行 / dry 共通の決定論)
# ---------------------------------------------------------------------------


@dataclass
class _Fold:
    """計画された 1 畳み — level の並びの古い側 items を 1 個の親にする。"""

    level: int
    items: List[_RowItem]


def _plan_fold_for_level(row: Sequence[_RowItem]) -> Optional[List[_RowItem]]:
    """並び 1 本の発火判定と畳み範囲の決定。

    合計字数 (excluded を除く) が上限を超えたら、新しい側に「残す量」だけを
    残して、古い側の連続部分を畳み範囲にする。

    範囲が跨げない境界は 2 種類 — excluded (提示中の圧縮区間) と gap_before
    (間に未編纂の生ログが居る = 偽の隣接の禁止)。境界で刻んだ区間のうち、
    **2 件以上ある最古の区間**を畳む。最古の区間が 1 件でも、その先の区間は
    独立に畳める (先頭だけを見て打ち切ると、一時的な境界の手前 1 件が
    その後ろの過予算区間を永久に人質に取る — Codex レビュー 2026-07-28 high3)。

    どの区間も 2 件未満なら畳まない (1 個を 1 個に要約し直すのは無意味 —
    次の到着か境界の解消を待つ)。
    """
    eligible_chars = sum(i.chars for i in row if not i.excluded)
    if eligible_chars <= BAND_CHAR_LIMIT:
        return None
    # 新しい側から「残す量」ぶんを確保し、その手前までが畳み範囲の候補。
    keep = 0
    cut = len(row)
    for i in range(len(row) - 1, -1, -1):
        if row[i].excluded:
            continue
        if keep + row[i].chars > BAND_CHAR_KEEP:
            break
        keep += row[i].chars
        cut = i
    prefix = row[:cut]
    # 境界 (excluded / gap_before) で連続区間に刻む。
    segments: List[List[_RowItem]] = []
    current: List[_RowItem] = []
    for item in prefix:
        if item.excluded:
            if current:
                segments.append(current)
            current = []
            continue
        if item.gap_before and current:
            segments.append(current)
            current = []
        current.append(item)
    if current:
        segments.append(current)
    for segment in segments:
        if len(segment) >= 2:
            return segment
    return None


def _plan_folds(rows: Dict[int, List[_RowItem]]) -> List[_Fold]:
    """全レベルの畳み計画 (連鎖含む) — 実行と dry 予測が共有する決定論。

    レベル昇順に処理する。畳んだ親 (字数 EST_PARENT_CHARS 見込み) を 1 つ上の
    並びの末尾に加えてから上のレベルを判定するので、連鎖 (レベル1 の畳みが
    レベル2 を溢れさせる) も計画に含まれる。

    rows は破壊的に更新される (呼び出し側は使い捨てにすること)。
    """
    folds: List[_Fold] = []
    level = 1
    while level <= max(rows.keys(), default=0):
        row = rows.get(level, [])
        while True:
            fold = _plan_fold_for_level(row)
            if fold is None:
                break
            folds.append(_Fold(level=level, items=fold))
            starts = [i.start_time for i in fold if i.start_time is not None]
            ends = [i.end_time for i in fold if i.end_time is not None]
            parent = _RowItem(
                coverage=sum(i.safe_coverage for i in fold),
                chars=EST_PARENT_CHARS,
                start_time=min(starts) if starts else None,
                end_time=max(ends) if ends else None,
            )
            # 畳んだのは必ずしも並びの先頭ではない (境界で刻んだ最古の区間)。
            # 同一オブジェクトを取り除く。
            fold_ids = {id(i) for i in fold}
            row[:] = [i for i in row if id(i) not in fold_ids]
            rows.setdefault(level + 1, []).append(parent)
        level += 1
    return folds


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
    """束ねの発生回数 (連鎖含む) を LLM なしで予測する。

    :func:`run_band_overflow` と同じ計画 (:func:`_plan_folds`) を共有する。

    Args:
        extra_leaves: これから確定する新チャンク (レベル1) の
            ``(coverage_chars, start_time, end_time[, est_chars])`` 列。
            並びに加算して予測する (確認ゲートは実行前に呼ぶため)。
        excluded_entry_ids: 圧縮区間として提示中の digest entry id 集合。
        pending_source_ids: 旧設計 (連続性ギャップ判定) の名残り。現設計では
            使わない — 呼び出し側互換のため受け取るだけ。

    Returns:
        予測される LLM コール回数。
    """
    rows = _load_rows(conn, excluded_entry_ids=excluded_entry_ids)
    if extra_leaves:
        row1 = rows.setdefault(1, [])
        for leaf in extra_leaves:
            cov_i = int(leaf[0])
            st, et = leaf[1], leaf[2]
            chars = int(leaf[3]) if len(leaf) > 3 and leaf[3] is not None else (
                min(cov_i, EST_PARENT_CHARS)
            )
            row1.append(_RowItem(
                coverage=cov_i, chars=chars, start_time=st, end_time=et,
            ))
        row1.sort(key=lambda i: (
            i.end_time if i.end_time is not None else 0,
            i.start_time if i.start_time is not None else 0,
        ))
    return len(_plan_folds(rows))


# ---------------------------------------------------------------------------
# 束ねの実行
# ---------------------------------------------------------------------------


def _build_consolidation_prompt(
    entries: List[ArasujiEntry],
    origins: Dict[str, Optional[str]],
    conn: sqlite3.Connection,
) -> str:
    """親 digest を子 digest 群から語り直すプロンプト。

    材料には種別を明示する (intent §3-4): 通常はあらすじだが、旧設計の
    恒等圧縮などで生ログ断片が並びに残っていても、LLM が「あらすじの列に
    会話の断片が混ざっている」ことを理解して前後の文脈に織り込めるように。
    """
    from sai_memory.arasuji.context import get_episode_context_for_timerange
    from sai_memory.arasuji.generator import _format_timestamp

    starts = [e.start_time for e in entries if e.start_time is not None]
    ends = [e.end_time for e in entries if e.end_time is not None]
    context = ""
    if starts and ends:
        context = get_episode_context_for_timerange(
            conn, start_time=min(starts), end_time=max(ends), max_entries=10,
        )

    has_fragment = False
    lines: List[str] = []
    for i, entry in enumerate(entries, 1):
        start = _format_timestamp(entry.start_time)
        end = _format_timestamp(entry.end_time)
        if origins.get(entry.id) == "identity":
            has_fragment = True
            lines.append(f"### 材料 {i} 【生ログ断片】 ({start} ~ {end})")
        else:
            lines.append(f"### 材料 {i} 【あらすじ】 ({start} ~ {end})")
        lines.append(entry.content)
        lines.append("")
    entries_text = "\n".join(lines)

    parts = [
        f"以下の{len(entries)}個の材料を統合し、一段粗い視点の「まとめのあらすじ」を書いてください。",
        "",
    ]
    if context:
        parts.extend(["## さらに前の出来事（参考）", context, ""])
    parts.extend([
        "## 統合対象の材料",
        entries_text,
        "",
        "## 指示",
        "- 5〜8文程度で、全体の流れを俯瞰できるようにまとめる",
        "- 重要な転換点や印象的なエピソードを保持する",
        "- 個々の詳細より「どんな時期だったか」を重視する",
        "- 時系列順に書く",
    ])
    if has_fragment:
        parts.append(
            "- 【生ログ断片】は要約前の会話の断片です。前後のあらすじの流れに"
            "自然に織り込んでください（無視しない・そのまま引用しない）"
        )
    parts.extend([
        "- **前置き（「以下にまとめます」等）や見出し（「【あらすじ】」等）は書かないでください**（本文のみ出力）",
        "",
        "統合されたあらすじを日本語で書いてください。",
    ])
    return "\n".join(parts)


def _all_children_unconsolidated(
    conn: sqlite3.Connection, entry_ids: Sequence[str],
) -> bool:
    """全子がまだ未束ねか (束ね tx 内の再検査)。"""
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
    """{entry_id: digest_origin} — 恒等圧縮の子の判定用 (既存データ互換)。"""
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
    extraction_failures: Optional[List[str]] = None,
    db_lock: Optional[Any] = None,
    extraction_failures_unrecorded: Optional[List[str]] = None,
) -> None:
    """恒等圧縮の子の Fragment 抽出 (既存データ互換)。

    旧設計の恒等圧縮は生ログのまま一次あらすじの席にいたので、束ねで**初めて
    要約に変わる**この瞬間が「生ログが要約に置き換わる瞬間に一度だけ抽出」の
    実行点。LLM 束ね済みの子の範囲は既に抽出済みなので走らせない (Fragment
    の重複と参照切れを避ける)。chronicle_entry_id は恒等圧縮の子自身。

    失敗しても残りの子と束ねは続けるが、失敗を消さない —— 束ねが確定した子は
    二度と「初めて要約に変わる瞬間」を迎えないので、この抽出は自動では回収
    されない。``extraction_failures`` に entry id を積んで呼び出し元へ返す
    (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
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
            # 引けなかった id を**集合で**見る。件数の比較だと source_ids に
            # 同じ id が重複していたときに誤って欠損と判定する
            missing = set(entry.source_ids) - {m.id for m in messages}
            if missing:
                # 元メッセージが欠けている。残った分だけで抽出すると、欠けた分の
                # 知識が「抽出済み」の顔で落ちる (この子が要約に変わる瞬間は
                # 二度と来ない)。部分的な成功にせず失敗として扱い、付箋へ回す
                # —— 元メッセージが本当に消えていれば、拾い直しが「辿れない」
                # として正直に剥がす (Codex 六巡 #6)
                raise RuntimeError(
                    f"identity child {entry.id[:8]}: "
                    f"{len(missing)} of {len(set(entry.source_ids))} "
                    f"source messages are missing"
                )
            batch_callback(messages, entry.id)
        except Exception:
            if extraction_failures is not None:
                extraction_failures.append(entry.id)
            try:
                from sai_memory.memory.entity_extractor import (
                    record_extraction_failure,
                )
                record_extraction_failure(conn, entry.id, db_lock=db_lock)
            except Exception:
                # 付箋に残せなければ、この抽出には二度と番が回らない
                # (束ねが確定した子は「初めて要約に変わる瞬間」を二度と迎えない)。
                # 拾い直しの対象にならないので、報告のときに分けて扱う
                if extraction_failures_unrecorded is not None:
                    extraction_failures_unrecorded.append(entry.id)
                LOGGER.error(
                    "[bands] 抽出失敗の付箋を残せませんでした (entry=%s) — "
                    "この範囲の知識は自動では拾い直されません",
                    entry.id[:8], exc_info=True,
                )
            LOGGER.exception(
                "[bands] fragment callback failed for identity child %s",
                entry.id[:8],
            )


def _load_messages_by_ids(conn: sqlite3.Connection, message_ids: Sequence[str]):
    from sai_memory.memory.storage import get_messages_by_ids

    return get_messages_by_ids(conn, message_ids)


def _consolidate_fold(
    conn: sqlite3.Connection,
    client,
    fold: _Fold,
    *,
    persona_id: Optional[str],
    batch_callback: Optional[Callable] = None,
    known_ids: Optional[Set[str]] = None,
    extraction_failures: Optional[List[str]] = None,
    db_lock: Optional[Any] = None,
    extraction_failures_unrecorded: Optional[List[str]] = None,
    stats: Optional[dict] = None,
) -> Optional[ArasujiEntry]:
    """畳み 1 件を親ノードに確定する (親 + 子を単一 tx)。

    ``stats`` を渡すと、LLM 呼び出しに到達した時点で ``stats["attempts"]`` を
    1 進める (成否を問わない — 課金の試行回数を呼び出し元が予算に数えるため)。
    """
    entries = [i.entry for i in fold.items if i.entry is not None]
    if len(entries) != len(fold.items) or len(entries) < 2:
        return None
    target_level = fold.level + 1
    child_ids = [e.id for e in entries]
    origins = _digest_origins(conn, child_ids)
    prompt = _build_consolidation_prompt(entries, origins, conn)
    from sai_memory.arasuji.generator import generate_text_with_empty_retry
    if stats is not None:
        # LLM を叩く直前に数える — 失敗・空応答・tx 内再検査での放棄も
        # 「1 回の試行」として呼び出し元の承認予算を消費する。
        stats["attempts"] = int(stats.get("attempts", 0)) + 1
    try:
        # 空応答は helper が規定回数まで試し直す (usage も試行ごとに記録)。
        # 使い切った EmptyResponseError も他の失敗と同じくここで None に落ちる
        # (束ねは次の発火が再計画する)。
        content = generate_text_with_empty_retry(
            client,
            [{"role": "user", "content": prompt}],
            purpose="band consolidation",
            persona_id=persona_id,
            usage_node_type=f"chronicle_level{target_level}",
        )
    except Exception:
        LOGGER.exception(
            "[bands] consolidation LLM failed (level=%d, %d entries)",
            fold.level, len(entries),
        )
        return None

    starts = [e.start_time for e in entries if e.start_time is not None]
    ends = [e.end_time for e in entries if e.end_time is not None]
    total_coverage = sum(i.coverage for i in fold.items)
    extra_metadata = {
        "digest_origin": "band",
        "coverage_chars": total_coverage,
    }
    # BEGIN IMMEDIATE は **DB の**書き込みロック。同じ接続を共有する別スレッド
    # (Pulse / API) の commit までは止められないので、検査〜commit の区間は
    # adapter の錠前の内側で走らせる (Codex 七巡 #4)。LLM 呼び出しはこの手前で
    # 終わっているので、錠を持って待たせることはない。
    with (db_lock or nullcontext()):
        try:
            # tx 内再検査: BEGIN IMMEDIATE で write ロックを取ってから子の未統合を
            # 確認する — SELECT は暗黙 BEGIN を張らないため、ロックなしでは二接続が
            # 同時に「全子未統合」を読める。既に tx 内なら参加する (呼び出し側の契約)。
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if not _all_children_unconsolidated(conn, child_ids):
                conn.rollback()
                LOGGER.warning(
                    "[bands] consolidation abandoned: children consolidated "
                    "concurrently (level=%d)", fold.level,
                )
                return None
            # ギャップ不変条件の tx 内再検査 (Codex レビュー 2026-07-28 三〜四巡):
            # 計画〜LLM 応答の間に別経路 (CLI / API / Metabolism) が畳み区間へ
            # 挿入を行っていると、古い計画のまま確定すれば跨ぎ親 = 恒久孤児化に
            # なる。write ロック取得後に検査し直し、増えていたら放棄する
            # (次の発火が再計画する)。2 段:
            #
            # 1. 未編纂メッセージ / 下位レベルノードの境界 (計画時と同じ判定)。
            # 2. 親の時間範囲に内包される**計画後に新規出現した未統合ノード**
            #    (レベル不問 — 同一レベルの並走挿入は 1 に掛からない。四巡 high)。
            #    判定は計画時スナップショット (``known_ids``) との差分 — 計画時から
            #    居たノード (同一秒に並ぶ畳まれない兄弟や、境界の向こうの区間) を
            #    侵入と誤認すると、状態が変わらないまま毎回 LLM 課金して放棄する
            #    永久停止になる (五巡 high)。読み直しは孤児除外済みの _load_rows —
            #    既存の孤児 (旧データ) を誤検知しないため。
            for item in fold.items:
                item.gap_before = False
            _mark_uncompiled_gaps(conn, fold.level, list(fold.items))
            if any(item.gap_before for item in fold.items):
                conn.rollback()
                LOGGER.warning(
                    "[bands] consolidation abandoned: a gap appeared inside the "
                    "fold while waiting for the LLM (level=%d)", fold.level,
                )
                return None
            span_start = min(starts) if starts else None
            span_end = max(ends) if ends else None
            if span_start is not None and span_end is not None:
                child_id_set = set(child_ids)
                planned_known = known_ids or set()
                fresh_rows = _load_rows(conn)
                for fresh_row in fresh_rows.values():
                    for fresh in fresh_row:
                        e = fresh.entry
                        if (
                            e is None or e.id in child_id_set
                            or e.id in planned_known
                            or e.start_time is None or e.end_time is None
                        ):
                            continue
                        if span_start <= e.start_time and e.end_time <= span_end:
                            conn.rollback()
                            LOGGER.warning(
                                "[bands] consolidation abandoned: an unplanned "
                                "unconsolidated node appeared inside the fold span "
                                "while waiting for the LLM (level=%d, intruder=%s)",
                                fold.level, e.id[:8],
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
                "[bands] consolidation tx failed (level=%d); rolled back", fold.level,
            )
            return None
    LOGGER.info(
        "[bands] fold: %d nodes level %d -> %d (coverage=%d chars, parent=%s)",
        len(entries), fold.level, target_level, total_coverage, parent.id[:8],
    )
    # Fragment 抽出は commit 後 (executor.py の batch_callback と同型の位置)。
    _fire_identity_fragment_callbacks(
        conn, entries, batch_callback, extraction_failures, db_lock=db_lock,
        extraction_failures_unrecorded=extraction_failures_unrecorded,
    )
    return parent


def _any_level_over_limit(
    conn: sqlite3.Connection,
    excluded_entry_ids: Optional[Set[str]] = None,
) -> bool:
    """どこかのレベルの並びが上限を超えている可能性があるか (安価な前検査)。

    :func:`run_band_overflow` はチャンク確定のたびに呼ばれる (executor の
    after_chunk) ので、何も畳まない回を 1 クエリで抜けるための門。
    :func:`_load_rows` と同じ絞り込みで未束ねノードの字数をレベル別に合計する
    が、孤児判定 (被覆範囲の照合) はしない — 孤児は並びから外れる側なので、
    ここでの合計は実際の判定値 **以上**になる。よってこの検査が False なら
    どのレベルも発火しない (連鎖は畳みが起きて初めて始まる) — 見落としは
    無く、誤って True になった回は従来どおりの完全な計画が判定する。
    """
    rows = conn.execute(
        "SELECT level, id, length(COALESCE(content, '')) FROM arasuji_entries "
        "WHERE is_consolidated = 0 AND origin_track_id IS NULL "
        "AND (is_incomplete IS NULL OR is_incomplete = 0)"
    ).fetchall()
    excluded = excluded_entry_ids or set()
    totals: Dict[int, int] = {}
    for level, entry_id, chars in rows:
        if entry_id in excluded:
            continue
        totals[level] = totals.get(level, 0) + int(chars or 0)
    return any(total > BAND_CHAR_LIMIT for total in totals.values())


def run_band_overflow(
    conn: sqlite3.Connection,
    client,
    *,
    persona_id: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    excluded_entry_ids: Optional[Set[str]] = None,
    batch_callback: Optional[Callable] = None,
    max_folds: Optional[int] = None,
    extraction_failures: Optional[List[str]] = None,
    db_lock: Optional[Any] = None,
    extraction_failures_unrecorded: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    stats: Optional[dict] = None,
) -> int:
    """レベル別の並びを検査し、予算超過の畳みを実行する。

    計画は :func:`_plan_folds` — dry 予測 (:func:`plan_band_overflow`) と同じ
    決定論を共有する。実行は 1 畳みごとに DB から並びを読み直す (LLM の実際の
    出力長が見込みとズレても、次の畳みの判定は実際の値で行われる)。
    1 回の呼び出しの LLM コールは安全弁 (既定 3、
    ``SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN``) まで — これは
    **呼び出しごと**の上限で、generate_chronicle は確定したチャンクごとに
    この関数を呼ぶので、大量編纂の 1 走行では承認済みの dry 予測件数
    (``max_folds`` の累計) まで束ねが積み上がる。

    Args:
        excluded_entry_ids: 圧縮区間として提示コンテキストに表示中の digest
            entry id 集合 (字数の勘定・畳み対象から外し、畳み範囲は跨がない)。
        batch_callback: Fragment 抽出コールバック
            ``(List[Message], chronicle_entry_id) -> None``。恒等圧縮の子が
            初めて要約に変わる束ねでのみ、その子の生メッセージで呼ぶ。
        max_folds: 確認ゲートで承認された dry 予測件数。指定時は実行をこの
            件数までで止める — LLM の実出力が予測 (500字) より長いと連鎖が
            dry より増えることがあり、承認・課金見込みを実行が超えてはいけない
            (Codex レビュー 2026-07-28 high2)。積み残しは次回の Metabolism の
            dry が数え直して、次の承認のもとで畳まれる (収束は崩れない)。
        extraction_failures: 渡すと、Fragment 抽出に失敗した entry id を
            ここへ積む (戻り値の契約を変えずに失敗を呼び出し元へ返す)。
        db_lock: SAIMemoryAdapter の ``_db_lock``。抽出失敗の付箋を書くときに
            使う (ロック外の commit は他所の開いた tx を途中で確定させる)。
        progress_callback: 畳みが 1 件確定するごとに ``(done, total)`` で呼ぶ
            (total = この呼び出しで畳む上限)。呼び出し元が画面への進捗と
            実行台帳の心拍に使う — 1 畳み = LLM 1 コールで、無言のまま長く
            走ると台帳の期限監視に「観測途絶」と誤認される。
        stats: 渡すと ``stats["attempts"]`` (LLM 呼び出しに到達した畳みの数 —
            失敗・空応答・放棄も含む) と ``stats["created"]`` (確定した親の数 =
            戻り値) を書く。呼び出し元 (generate_chronicle) は承認済み予算を
            **試行回数**で消費する — 戻り値 (成功数) だけで数えると、失敗する
            畳みが呼び出しのたびに同じ残り予算で再試行され、プロバイダ障害の
            間の課金回数が承認件数で縛られない。戻り値の契約は変えない。

    Returns:
        作った親ノード数。
    """
    created = 0
    if stats is not None:
        stats["attempts"] = 0
        stats["created"] = 0
    limit = _max_consolidations_per_run()
    if max_folds is not None:
        limit = min(limit, max(0, max_folds))
    while created < limit:
        if cancel_check and cancel_check():
            break
        # 安価な前検査 — 編纂の各チャンク確定後にも呼ばれるので、超過の無い
        # 回は並びの完全な読み直し (隣接ごとの隙間検査) をしないで抜ける。
        if not _any_level_over_limit(conn, excluded_entry_ids):
            break
        rows = _load_rows(conn, excluded_entry_ids=excluded_entry_ids)
        # 計画時に見えている未統合ノードの id 集合 — tx 内再検査の
        # 「計画後に新規出現したか」の基準 (_plan_folds は rows を消費するので
        # 先に取る)。
        known_ids = {
            item.entry.id
            for row in rows.values() for item in row
            if item.entry is not None
        }
        folds = _plan_folds(rows)
        # 計画の先頭 (最も低いレベルの最初の畳み) だけ実行して読み直す。
        fold = next((f for f in folds if all(
            i.entry is not None for i in f.items
        )), None)
        if fold is None:
            break
        parent = _consolidate_fold(
            conn, client, fold,
            persona_id=persona_id, batch_callback=batch_callback,
            known_ids=known_ids, extraction_failures=extraction_failures,
            db_lock=db_lock,
            extraction_failures_unrecorded=extraction_failures_unrecorded,
            stats=stats,
        )
        if parent is None:
            break
        created += 1
        if stats is not None:
            stats["created"] = created
        if progress_callback:
            # 画面へのイベント送出などで失敗しても、確定済みの畳みの数を
            # 呼び出し元から奪ってはいけない — ここで例外が抜けると戻り値が
            # 届かず、呼び出し元の累計 (承認済み予算の消化) がずれて、次の
            # 呼び出しに過大な max_folds が渡る (承認回数の超過)。
            try:
                progress_callback(created, limit)
            except Exception:
                LOGGER.warning(
                    "[bands] progress callback failed; continuing", exc_info=True,
                )
    return created
