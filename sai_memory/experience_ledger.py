"""経験の台帳 — 再開の想起の一層目 (読み取り専用・決定論・LLM 不使用)。

experience_ledger.md §1/§3 の「台帳 (索引)」と「ページを開く = 動的合成」の
実装。memory.db (memopedia) の既存データだけを並べ直す読み取り層で、
子ページ等の実データは一切作らない (§3「格納はエンティティに満たない中間
粒度の実需が出てから」)。概要の質が足りない場合は編纂側の仕事 (§3)。

- :func:`build_ledger_index` — 索引。実体ページ (people/terms/plans/events) +
  テーマページ (theme) の各行にタイトル + カテゴリ + 概要 + **決定論の経験統計**
  (fragment 数 / 関与あらすじ数 / 初回・最終の接触日) を付ける。§3 の要件
  「関連してそうだが 1 回しか出ていない薄い棚が数字で見える」を満たす。
- :func:`build_ledger_page` — 動的合成。①ページ自身の fragment (経験値ノート
  含む、新しい順) ②関与あらすじの履歴 (新しい順の見出し列 = 経験の年表)
  ③共起した題材エンティティ (同じあらすじ群に関与したページ)。

目的ノード (生きた Track / task) は main DB 側の実体なので本モジュールの
対象外 — 索引への合流は API 層 (api/routes/people/experience_ledger.py) が
saiverse.judgment_points の供給関数 + purpose_tags 統計で行う。

**辿れる範囲と辿れない範囲 (正直な現状、2026-08-03)**:

- 辿れる: 代謝 (entity_extractor) 由来の fragment は ``chronicle_entry_id``
  で Chronicle エントリ (memopedia_pages category='chronicle') に繋がる。
  ②関与あらすじの履歴と③共起エンティティはこの辺だけから引く。
- 辿れない: コマ締め (slot_close) の経験値ノート fragment は
  ``chronicle_entry_id`` を持たない (由来は本文末尾の注記 + source_date のみ)。
  そのためテーマページの②はふつう空になる。また purpose_tags は
  「episode → 目的ノード」の辺であり、memopedia ページへの関与タグ
  (粒度実験 B2 の involved_entities) は未実装 — タグ経由の関与履歴は
  まだ引けない。B2 実装後にこのモジュールの②へ合流させるのが増築線。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from sai_memory.memopedia.storage import (
    CATEGORY_DEFS,
    resolve_page_ref,
)

#: 台帳に載るカテゴリ (経験の棚)。core / chronicle は棚でなく別導線。
LEDGER_CATEGORIES = ("people", "terms", "plans", "events", "theme")

#: 共起エンティティの上位件数 (§3「見出しと概要」— 一覧でなく上位数件)。
RELATED_LIMIT = 5

__all__ = [
    "LEDGER_CATEGORIES",
    "RELATED_LIMIT",
    "build_ledger_index",
    "build_ledger_page",
]


def _stats_dict(
    fragment_count: int,
    first_date: Optional[str],
    last_date: Optional[str],
    chronicle_count: int,
) -> Dict[str, Any]:
    return {
        "fragment_count": int(fragment_count or 0),
        "first_date": first_date,
        "last_date": last_date,
        "chronicle_count": int(chronicle_count or 0),
    }


def build_ledger_index(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """台帳の索引を組む (memory.db 分。決定論の一発集計)。

    LEDGER_CATEGORIES の非削除・非 trunk ページ全件に、fragment 集計を
    LEFT JOIN で付ける。fragment ゼロのページも載せる (§3 網羅性 —
    「まだ経験の記録が無い棚」も棚として見える)。

    接触日は fragment の ``source_date`` (YYYY-MM-DD、entity_extractor /
    slot_close とも同形式) を主、無い行は ``created_at`` の日付で補う。
    どちらも文字列 MIN/MAX で時系列順になる形。

    返り値: 各行 ``{page_id, short_id, title, category, summary, stats}``。
    stats = ``{fragment_count, first_date, last_date, chronicle_count}``
    (chronicle_count = 関与あらすじ数 = distinct chronicle_entry_id)。
    並びはカテゴリ順 (CATEGORY_DEFS order) → 最終接触日の新しい順 → タイトル。
    """
    placeholders = ",".join("?" for _ in LEDGER_CATEGORIES)
    rows = conn.execute(
        f"""
        SELECT p.id, p.short_id, p.title, p.category, p.summary,
               COUNT(f.id) AS fragment_count,
               MIN(COALESCE(f.source_date,
                            date(f.created_at, 'unixepoch', 'localtime'))) AS first_date,
               MAX(COALESCE(f.source_date,
                            date(f.created_at, 'unixepoch', 'localtime'))) AS last_date,
               COUNT(DISTINCT f.chronicle_entry_id) AS chronicle_count
        FROM memopedia_pages p
        LEFT JOIN memopedia_fragments f ON f.entity_id = p.id
        WHERE p.category IN ({placeholders})
          AND COALESCE(p.is_trunk, 0) = 0
          AND COALESCE(p.is_deleted, 0) = 0
        GROUP BY p.id
        """,
        list(LEDGER_CATEGORIES),
    ).fetchall()

    # 並び: カテゴリ順 asc → 最終接触日 desc → タイトル asc。
    # 文字列日付の降順は昇順キーに合成できないため、安定ソートの重ねがけで出す
    # (後段のソートほど優先される)。
    rows = sorted(rows, key=lambda r: r[2] or "")
    rows.sort(key=lambda r: r[7] or "", reverse=True)
    rows.sort(
        key=lambda r: CATEGORY_DEFS[r[3]].order if r[3] in CATEGORY_DEFS else 99
    )
    return [
        {
            "page_id": r[0],
            "short_id": r[1],
            "title": r[2],
            "category": r[3],
            "summary": r[4] or "",
            "stats": _stats_dict(r[5], r[6], r[7], r[8]),
        }
        for r in rows
    ]


def _load_ledger_page_row(
    conn: sqlite3.Connection, page_id: str
) -> Optional[tuple]:
    """台帳の対象になるページ行を引く (非削除・非 trunk・台帳カテゴリのみ)。"""
    resolved = resolve_page_ref(conn, page_id)
    if not resolved:
        return None
    placeholders = ",".join("?" for _ in LEDGER_CATEGORIES)
    return conn.execute(
        f"""
        SELECT id, short_id, title, category, summary, content
        FROM memopedia_pages
        WHERE id = ?
          AND category IN ({placeholders})
          AND COALESCE(is_trunk, 0) = 0
          AND COALESCE(is_deleted, 0) = 0
        """,
        [resolved, *LEDGER_CATEGORIES],
    ).fetchone()


def _parse_chronicle_meta(metadata_json: Optional[str]) -> Dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        loaded = json.loads(metadata_json)
        return loaded if isinstance(loaded, dict) else {}
    except (ValueError, TypeError):
        return {}


def build_ledger_page(
    conn: sqlite3.Connection, page_id: str
) -> Optional[Dict[str, Any]]:
    """ページを開く = 動的合成 (§3)。実データの子ページは作らない。

    ``page_id`` は UUID / ``memopedia:N`` / 素の short_id を受ける
    (:func:`resolve_page_ref` に委譲)。台帳の対象でないページ (削除済み /
    trunk / core / chronicle) は None。

    返り値の形::

        {
          "page": {page_id, short_id, title, category, summary, content},
          "stats": {fragment_count, first_date, last_date, chronicle_count},
          "fragments": [{id, content, source_date, chronicle_entry_id,
                         created_at}, ...],          # 新しい順
          "involvement": {
            "entries": [{entry_id, short_id, title, start_time, end_time},
                        ...],                        # 新しい順 (経験の年表)
            "unresolved_count": int,  # 参照先 Chronicle が消えていた fragment 由来
          },
          "related": [{page_id, title, category, summary, shared_count}, ...],
        }

    involvement / related の辿り方と限界はモジュール docstring の
    「辿れる範囲と辿れない範囲」を参照 (テーマページの経験値ノートは
    chronicle_entry_id を持たないため、②③に寄与しない)。
    """
    row = _load_ledger_page_row(conn, page_id)
    if row is None:
        return None
    pid = row[0]

    # ---- ① ページ自身の fragment (新しい順) ----
    frag_rows = conn.execute(
        """
        SELECT id, content, source_date, chronicle_entry_id, created_at
        FROM memopedia_fragments
        WHERE entity_id = ?
        ORDER BY created_at DESC, rowid DESC
        """,
        (pid,),
    ).fetchall()
    fragments = [
        {
            "id": f[0],
            "content": f[1],
            "source_date": f[2],
            "chronicle_entry_id": f[3],
            "created_at": int(f[4] or 0),
        }
        for f in frag_rows
    ]

    dates = [
        f["source_date"] for f in fragments if f["source_date"]
    ]
    stats = _stats_dict(
        len(fragments),
        min(dates) if dates else None,
        max(dates) if dates else None,
        len({f["chronicle_entry_id"] for f in fragments if f["chronicle_entry_id"]}),
    )

    # ---- ② 関与あらすじの履歴 (fragment の chronicle_entry_id → Chronicle) ----
    entry_ids = sorted(
        {f["chronicle_entry_id"] for f in fragments if f["chronicle_entry_id"]}
    )
    entries: List[Dict[str, Any]] = []
    unresolved = 0
    if entry_ids:
        placeholders = ",".join("?" for _ in entry_ids)
        # Chronicle の物理格納は memopedia_pages (category='chronicle')。
        # start/end/short_id は metadata JSON 内 (arasuji/storage.py の
        # 互換 VIEW と同じ読み方)。VIEW ``arasuji_entries`` は
        # init_arasuji_tables を通した接続にしか無いため、ここでは
        # ページ表を直接読む (adapter 接続は memopedia init のみが保証)。
        found = conn.execute(
            f"""
            SELECT id, title, metadata, created_at
            FROM memopedia_pages
            WHERE id IN ({placeholders})
              AND category = 'chronicle'
              AND COALESCE(is_deleted, 0) = 0
            """,
            entry_ids,
        ).fetchall()
        found_ids = set()
        for e in found:
            meta = _parse_chronicle_meta(e[2])
            found_ids.add(e[0])
            entries.append(
                {
                    "entry_id": e[0],
                    "short_id": meta.get("short_id"),
                    "title": e[1],
                    "start_time": meta.get("start_time"),
                    "end_time": meta.get("end_time"),
                    "_created_at": int(e[3] or 0),
                }
            )
        unresolved = len(entry_ids) - len(found_ids)
        entries.sort(
            key=lambda e: e.get("end_time") or e.get("start_time") or e["_created_at"],
            reverse=True,
        )
        for e in entries:
            e.pop("_created_at", None)

    # ---- ③ 共起エンティティ (同じあらすじ由来の fragment を持つ他ページ) ----
    related: List[Dict[str, Any]] = []
    if entry_ids:
        placeholders = ",".join("?" for _ in entry_ids)
        cat_ph = ",".join("?" for _ in LEDGER_CATEGORIES)
        rel_rows = conn.execute(
            f"""
            SELECT p.id, p.title, p.category, p.summary,
                   COUNT(DISTINCT f.chronicle_entry_id) AS shared_count
            FROM memopedia_fragments f
            JOIN memopedia_pages p ON p.id = f.entity_id
            WHERE f.chronicle_entry_id IN ({placeholders})
              AND f.entity_id != ?
              AND p.category IN ({cat_ph})
              AND COALESCE(p.is_trunk, 0) = 0
              AND COALESCE(p.is_deleted, 0) = 0
            GROUP BY p.id
            ORDER BY shared_count DESC, p.title ASC
            LIMIT ?
            """,
            [*entry_ids, pid, *LEDGER_CATEGORIES, RELATED_LIMIT],
        ).fetchall()
        related = [
            {
                "page_id": r[0],
                "title": r[1],
                "category": r[2],
                "summary": r[3] or "",
                "shared_count": int(r[4] or 0),
            }
            for r in rel_rows
        ]

    return {
        "page": {
            "page_id": pid,
            "short_id": row[1],
            "title": row[2],
            "category": row[3],
            "summary": row[4] or "",
            "content": row[5] or "",
        },
        "stats": stats,
        "fragments": fragments,
        "involvement": {"entries": entries, "unresolved_count": unresolved},
        "related": related,
    }
