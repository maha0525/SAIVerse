"""Arasuji (episode memory) database storage layer.

【P3b 物理統合 (2026-07-11)】このモジュールの公開 API (関数シグネチャ・戻り値の
形・意味) は変えず、格納先を ``memopedia_pages`` に統合した
(docs/intent/concept_consolidation.md「P3 物理統合」)。Chronicle エントリ 1 件は
trunk ``root_chronicle`` (category ``chronicle``・is_trunk・title「時間の地図」)
配下の子ページで、``ch:N`` の N はページ本体の memopedia short_id (m:N) とは別に
metadata JSON の ``short_id`` (Chronicle 内で閉じた連番) を使う (P3a の core_id と
同じ流儀)。ページ id = 旧 arasuji entry の UUID をそのまま流用するため、
``MemopediaFragment.chronicle_entry_id`` 等の既存参照は無傷で解決される。

parent_id の写像: 旧 arasuji の parent_id=NULL (未統合・最上位) → ページの
parent=root_chronicle。parent_id=X (Lv2+ に統合済み) → ページの parent=X。
読み出し側は逆写像 (parent==root_chronicle → None)。

【互換 VIEW】このコードベースには sea/auto_recall.py・
sea/head_pipeline/sections/chronicle_index.py・sea/session_lifecycle.py・
sai_memory/unified_recall.py・sai_memory/arasuji/estimate.py・
api/routes/people/arasuji.py・tools/utilities/memory_settings_ui.py・
builtin_data/tools/get_memory_weave_context.py 等、本モジュールを経由せず
生 SQL で ``arasuji_entries`` テーブルを直接読む消費者が多数ある (一部は
sea/head_pipeline/ など変更禁止領域)。物理格納を変えつつこれらを無傷で通すため、
``init_arasuji_tables`` は同名の読み取り専用 SQL VIEW を ``memopedia_pages`` の
上に張る。書き込みはすべて本モジュールの関数 (と generator.py 経由) からのみ行われ、
生の UPDATE/INSERT/DELETE は memopedia_pages に対して行う。VIEW は SELECT 専用
(SQLite はビューへの直接 UPDATE/DELETE を許さない) — 実際に確認済み。

旧 ``arasuji_entries`` テーブルは ``init_arasuji_tables`` 内の一回きり冪等
migration でページへ写し切ってから DROP する (``sai_memory/clips.py`` の
marks→clips や ``sai_memory/core_memory.py`` の core_memories→pages と同じ流儀)。

詳細設計: docs/intent/memory_architecture_v2.md / docs/intent/concept_consolidation.md
「P3b: Chronicle → 時間の地図ページ」
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class ArasujiEntry:
    """Represents a single arasuji (summary) entry."""

    id: str
    level: int  # 1=arasuji, 2=arasuji-no-arasuji, ...
    content: str
    source_ids: List[str]  # message IDs (level 1) or child arasuji IDs (level 2+)
    start_time: Optional[int]
    end_time: Optional[int]
    source_count: int  # number of sources (batch_size or consolidation_size)
    message_count: int  # total messages covered
    parent_id: Optional[str]  # parent arasuji ID if consolidated
    is_consolidated: bool
    created_at: int
    # Track Chronicle (v0.32, 2026-05-09) の名残。**書き手は退役済み**
    # (track_retirement.md 住人 5) — 新しい entry では常に None / False。
    # 既存 DB には Track 由来の行が残っているので、それを General Chronicle の
    # 読みから外すためのフィルタ用に列と属性だけ生かしてある。
    origin_track_id: Optional[str] = None
    is_incomplete: bool = False
    # Per-DB sequential ID (P2a, 2026-07-10: Memory Atlas の ch:N 短縮参照用。
    # memopedia_pages.short_id と同じ流儀)。既存 DB では追加系 migration で backfill。
    short_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "content": self.content,
            "source_ids": self.source_ids,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "source_count": self.source_count,
            "message_count": self.message_count,
            "parent_id": self.parent_id,
            "is_consolidated": self.is_consolidated,
            "created_at": self.created_at,
            "origin_track_id": self.origin_track_id,
            "is_incomplete": self.is_incomplete,
            "short_id": self.short_id,
        }


@dataclass
class ArasujiProgress:
    """Tracks arasuji generation progress."""

    id: str
    last_processed_message_id: Optional[str]
    last_processed_at: Optional[int]


# ---------------------------------------------------------------------------
# 物理格納: memopedia_pages (trunk root_chronicle 配下の子ページ) + 互換 VIEW
# ---------------------------------------------------------------------------

ROOT_CHRONICLE_ID = "root_chronicle"
CATEGORY_CHRONICLE = "chronicle"

# 互換 VIEW の列名。名前だけ既存 SQL (本モジュール内・外部消費者とも) と一致していれば
# 内部の列順は問わない (呼び出し側は SELECT で明示的に列名を指定しているため)。
_COMPAT_VIEW_SQL = f"""
    CREATE VIEW arasuji_entries AS
    SELECT
        id,
        CAST(json_extract(metadata, '$.level') AS INTEGER) AS level,
        content,
        json_extract(metadata, '$.source_ids') AS source_ids_json,
        CAST(json_extract(metadata, '$.start_time') AS INTEGER) AS start_time,
        CAST(json_extract(metadata, '$.end_time') AS INTEGER) AS end_time,
        CAST(json_extract(metadata, '$.source_count') AS INTEGER) AS source_count,
        CAST(json_extract(metadata, '$.message_count') AS INTEGER) AS message_count,
        CASE WHEN parent_id = '{ROOT_CHRONICLE_ID}' THEN NULL ELSE parent_id END AS parent_id,
        CAST(json_extract(metadata, '$.is_consolidated') AS INTEGER) AS is_consolidated,
        created_at,
        json_extract(metadata, '$.thread_id') AS thread_id,
        json_extract(metadata, '$.origin_track_id') AS origin_track_id,
        CAST(json_extract(metadata, '$.is_incomplete') AS INTEGER) AS is_incomplete,
        CAST(json_extract(metadata, '$.short_id') AS INTEGER) AS short_id
    FROM memopedia_pages
    WHERE category = '{CATEGORY_CHRONICLE}' AND is_trunk = 0
      AND (is_deleted = 0 OR is_deleted IS NULL)
"""


def _ensure_root_chronicle(conn: sqlite3.Connection) -> None:
    """trunk root_chronicle ページを冪等に用意する (無ければ作る)。"""
    row = conn.execute(
        "SELECT id FROM memopedia_pages WHERE id = ?", (ROOT_CHRONICLE_ID,)
    ).fetchone()
    if row is not None:
        return
    from sai_memory.memopedia.storage import create_page

    create_page(
        conn,
        parent_id=None,
        title="時間の地図",
        category=CATEGORY_CHRONICLE,
        is_trunk=True,
        page_id=ROOT_CHRONICLE_ID,
    )
    conn.commit()


def _fallback_chronicle_title(rec: Dict[str, Any]) -> str:
    """short_id が採れなかった場合の機械的表題 (通常は到達しない防御的分岐)。"""
    start = rec.get("start_time")
    end = rec.get("end_time")
    if start or end:
        from datetime import datetime
        s = datetime.fromtimestamp(start).strftime("%Y-%m-%d") if start else "?"
        e = datetime.fromtimestamp(end).strftime("%Y-%m-%d") if end else "?"
        return f"Chronicle {s}~{e}"
    return f"Chronicle {str(rec.get('id') or '')[:8]}"


def _migrate_legacy_arasuji_table(conn: sqlite3.Connection) -> None:
    """旧 ``arasuji_entries`` テーブルが存在すれば、全行をページへ写して DROP する。

    一回きり・冪等 (旧テーブルが無ければ即 return)。id → ページ id にそのまま流用し、
    level/content/source_ids/start_time/end_time/source_count/message_count/
    is_consolidated/origin_track_id/is_incomplete/thread_id/short_id を metadata に
    忠実に写す。列の有無は ``PRAGMA`` を引かず ``SELECT *`` の ``cursor.description``
    から動的に読む (thread_id/origin_track_id/is_incomplete/short_id が無い、
    P2a より前の超古い DB にも耐える)。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='arasuji_entries'"
    ).fetchone()
    if exists is None:
        return

    from sai_memory.memopedia.storage import create_page

    cur = conn.execute("SELECT * FROM arasuji_entries ORDER BY created_at ASC, rowid ASC")
    col_names = [d[0] for d in cur.description]
    rows = cur.fetchall()

    has_short_id_col = "short_id" in col_names
    next_short_id = 1
    if has_short_id_col and rows:
        sid_idx = col_names.index("short_id")
        existing_max = max((row[sid_idx] or 0) for row in rows)
        next_short_id = existing_max + 1

    # 前回の移行が「ページの commit までは済んだが、その後の DROP TABLE で倒れた」
    # (同じ DB を別の接続が読んでいると DROP が busy で落ちる) 状態から再開できる
    # ように、既にページになっている行は飛ばす。ここを飛ばさないと、最初の 1 行で
    # ``UNIQUE constraint failed: memopedia_pages.id`` になって毎回初手から倒れ、
    # 旧テーブルが永遠に残る (2026-09-02、macOS の報告者の環境で 1443 件が
    # この状態だった。Chronicle は空に見え、開くたびの失敗が Memopedia を待たせた)。
    already_pages = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM memopedia_pages WHERE category = ?", (CATEGORY_CHRONICLE,)
        ).fetchall()
    }
    skipped = 0
    for row in rows:
        rec = dict(zip(col_names, row))
        old_id = rec["id"]
        if old_id in already_pages:
            skipped += 1
            continue
        try:
            source_ids = json.loads(rec.get("source_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            source_ids = []

        short_id = rec.get("short_id")
        if short_id is None:
            short_id = next_short_id
            next_short_id += 1

        legacy_parent_id = rec.get("parent_id")
        page_parent = legacy_parent_id if legacy_parent_id else ROOT_CHRONICLE_ID

        meta = {
            "level": int(rec["level"]),
            "source_ids": source_ids,
            "start_time": rec.get("start_time"),
            "end_time": rec.get("end_time"),
            "source_count": int(rec.get("source_count") or 0),
            "message_count": int(rec.get("message_count") or 0),
            "is_consolidated": int(rec.get("is_consolidated") or 0),
            "is_incomplete": int(rec.get("is_incomplete") or 0),
            "origin_track_id": rec.get("origin_track_id"),
            "thread_id": rec.get("thread_id"),
            "short_id": short_id,
        }
        title = f"chronicle:{short_id}" if short_id is not None else _fallback_chronicle_title(rec)

        page = create_page(
            conn,
            parent_id=page_parent,
            title=title,
            content=rec.get("content") or "",
            category=CATEGORY_CHRONICLE,
            is_trunk=False,
            metadata=meta,
            page_id=old_id,
        )
        conn.execute(
            "UPDATE memopedia_pages SET created_at = ? WHERE id = ?",
            (int(rec["created_at"]), page.id),
        )
    conn.commit()
    if skipped:
        import logging

        logging.getLogger(__name__).warning(
            "[arasuji] legacy migration resumed: %d of %d rows were already pages "
            "(a previous run committed them but did not drop the table); migrated the rest",
            skipped,
            len(rows),
        )
    conn.execute("DROP TABLE arasuji_entries")
    conn.commit()


def init_arasuji_tables(conn: sqlite3.Connection) -> None:
    """Initialize arasuji (Chronicle) storage — memopedia_pages ベース (冪等)。

    1. Memopedia テーブルを保証。
    2. trunk root_chronicle ページを冪等に用意する。
    3. 旧 arasuji_entries テーブルが存在すれば一回きり移行して DROP する
       (この時点で旧テーブルは必ず消えている)。
    4. 互換 VIEW ``arasuji_entries`` を (再) 作成する — 生 SQL 消費者向け。
    5. arasuji_progress / arasuji_embeddings (Chronicle の物理格納とは無関係、
       現状維持) と、旧テーブルの index に対応する expression index を作る。
    """
    from sai_memory.memopedia.storage import init_memopedia_tables

    init_memopedia_tables(conn)
    _ensure_root_chronicle(conn)
    _migrate_legacy_arasuji_table(conn)

    # DROP→CREATE は 2 接続が並行で通ると片方の CREATE が "already exists" で
    # 落ちる (API はリクエストごとに init を通る — 2026-08-31 に fragments GET の
    # 並行取得で本番 500)。VIEW 定義の更新はデプロイ単位でしか変わらないので、
    # 競合した側は「相手が作った同じ定義」として受容してよい。
    try:
        conn.execute("DROP VIEW IF EXISTS arasuji_entries")
        conn.execute(_COMPAT_VIEW_SQL)
    except sqlite3.OperationalError as exc:
        if "already exists" not in str(exc):
            raise

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arasuji_progress (
            id TEXT PRIMARY KEY DEFAULT 'main',
            last_processed_message_id TEXT,
            last_processed_at INTEGER
        )
        """
    )

    # Embeddings for Chronicle entries (used by unified recall). entry_id は
    # 引き続き Chronicle エントリ (== ページ) の id。FK は宣言のみ (このコードベースは
    # foreign_keys pragma を有効化しないため実効しない。VIEW への FK 宣言も
    # CREATE TABLE 時点ではエラーにならないことを確認済み)。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arasuji_embeddings (
            entry_id TEXT PRIMARY KEY,
            vector TEXT NOT NULL,
            FOREIGN KEY (entry_id) REFERENCES arasuji_entries(id)
        )
        """
    )

    # 旧 idx_arasuji_* に対応する expression index (category='chronicle' に絞った部分 index)。
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_chronicle_level ON memopedia_pages"
        f"(json_extract(metadata, '$.level')) WHERE category = '{CATEGORY_CHRONICLE}'"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_chronicle_end_time ON memopedia_pages"
        f"(json_extract(metadata, '$.end_time')) WHERE category = '{CATEGORY_CHRONICLE}'"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_chronicle_consolidated ON memopedia_pages"
        f"(json_extract(metadata, '$.is_consolidated')) WHERE category = '{CATEGORY_CHRONICLE}'"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_chronicle_thread ON memopedia_pages"
        f"(json_extract(metadata, '$.thread_id')) WHERE category = '{CATEGORY_CHRONICLE}'"
    )
    # ⚠️ 以下 2 本の track 索引は、Track Chronicle の退役 (track_retirement.md 住人 5)
    # で引き手がいなくなった。既存 DB には索引が残るので、撤去は列の掃除と同じ
    # migration で行う (v0.3 の機械写し便)。ここだけ消すと新規 DB と既存 DB で
    # 索引の有無が食い違う。
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_chronicle_track ON memopedia_pages"
        f"(json_extract(metadata, '$.origin_track_id')) WHERE category = '{CATEGORY_CHRONICLE}'"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_chronicle_track_level_time ON memopedia_pages"
        f"(json_extract(metadata, '$.origin_track_id'), json_extract(metadata, '$.level'), "
        f"json_extract(metadata, '$.end_time')) WHERE category = '{CATEGORY_CHRONICLE}'"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_chronicle_short_id ON memopedia_pages"
        f"(json_extract(metadata, '$.short_id')) WHERE category = '{CATEGORY_CHRONICLE}'"
    )

    conn.commit()


def _next_chronicle_short_id(conn: sqlite3.Connection) -> int:
    """新規 Chronicle エントリの次の short_id (MAX + 1、初回は 1)。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(json_extract(metadata, '$.short_id') AS INTEGER)), 0) "
        "FROM memopedia_pages WHERE category = ?",
        (CATEGORY_CHRONICLE,),
    ).fetchone()
    return int(row[0]) + 1 if row and row[0] is not None else 1


def _get_chronicle_page_row(
    conn: sqlite3.Connection, entry_id: str,
) -> Optional[Tuple[str, Optional[str], str, Optional[str], int]]:
    """id (== page id) で生ページ行 (id, parent_id, content, metadata, created_at) を取る。"""
    return conn.execute(
        "SELECT id, parent_id, content, metadata, created_at FROM memopedia_pages "
        "WHERE id = ? AND category = ?",
        (entry_id, CATEGORY_CHRONICLE),
    ).fetchone()


def _parse_chronicle_meta(metadata_json: Optional[str]) -> Dict[str, Any]:
    try:
        return json.loads(metadata_json) if metadata_json else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# Standard SELECT 句。すべての SELECT で同じ並びを保つことで _row_to_entry が動く。
# (v0.32, 2026-05-09) 末尾に origin_track_id, is_incomplete を追加。
# (P3b, 2026-07-11) 物理格納は memopedia_pages に変わったが、この列名リストは
# init_arasuji_tables が張る互換 VIEW ``arasuji_entries`` に対してそのまま使える。
_ENTRY_COLUMNS = (
    "id, level, content, source_ids_json, start_time, end_time, "
    "source_count, message_count, parent_id, is_consolidated, created_at, "
    "origin_track_id, is_incomplete, short_id"
)


def _row_to_entry(row: Tuple[Any, ...]) -> ArasujiEntry:
    """Convert a database row to an ArasujiEntry object."""
    source_ids_json = row[3]
    try:
        source_ids = json.loads(source_ids_json) if source_ids_json else []
    except (json.JSONDecodeError, TypeError):
        source_ids = []

    # 末尾の origin_track_id / is_incomplete / short_id は migration 適用前の DB や
    # short_id を含まない SELECT では存在しない可能性あり。行長で判定して fallback。
    origin_track_id = row[11] if len(row) > 11 else None
    is_incomplete = bool(row[12]) if len(row) > 12 else False
    short_id = int(row[13]) if len(row) > 13 and row[13] is not None else None

    return ArasujiEntry(
        id=row[0],
        level=int(row[1]),
        content=row[2],
        source_ids=source_ids,
        start_time=int(row[4]) if row[4] is not None else None,
        end_time=int(row[5]) if row[5] is not None else None,
        source_count=int(row[6]),
        message_count=int(row[7]),
        parent_id=row[8],
        is_consolidated=bool(row[9]),
        created_at=int(row[10]),
        origin_track_id=origin_track_id,
        is_incomplete=is_incomplete,
        short_id=short_id,
    )


# ----- Entry CRUD operations -----


def create_entry(
    conn: sqlite3.Connection,
    *,
    level: int,
    content: str,
    source_ids: List[str],
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    source_count: int,
    message_count: int,
    entry_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> ArasujiEntry:
    """Create a new arasuji entry.

    Args:
        thread_id: If set, associates this entry with a specific thread
                   (e.g., Stelis thread). NULL = main thread.
        extra_metadata: 由来メタ等の追加フィールド (digest_origin /
                        coverage_chars / episode_refs — W4 D4)。標準キーと
                        衝突した場合は標準キーが勝つ。
        commit: False で呼び出し側のトランザクションに参加する (チャンク
                単位 tx / 親子束ね tx — W4 D4/D6)。
    """
    from sai_memory.memopedia.storage import create_page

    _ensure_root_chronicle(conn)
    eid = entry_id or str(uuid.uuid4())
    sid = _next_chronicle_short_id(conn)
    meta = dict(extra_metadata) if extra_metadata else {}
    meta.update({
        "level": level,
        "source_ids": source_ids,
        "start_time": start_time,
        "end_time": end_time,
        "source_count": source_count,
        "message_count": message_count,
        "is_consolidated": 0,
        "thread_id": thread_id,
        "short_id": sid,
    })
    page = create_page(
        conn,
        parent_id=ROOT_CHRONICLE_ID,
        title=f"chronicle:{sid}",
        content=content,
        category=CATEGORY_CHRONICLE,
        is_trunk=False,
        metadata=meta,
        page_id=eid,
        commit=False,
    )
    if commit:
        conn.commit()
    return ArasujiEntry(
        id=eid,
        level=level,
        content=content,
        source_ids=source_ids,
        start_time=start_time,
        end_time=end_time,
        source_count=source_count,
        message_count=message_count,
        parent_id=None,
        is_consolidated=False,
        created_at=page.created_at,
        short_id=sid,
    )


def get_entry_by_short_id(conn: sqlite3.Connection, short_id: int) -> Optional[ArasujiEntry]:
    """short_id (``ch:N`` の N) で 1 件取得する。無ければ None。"""
    cur = conn.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM arasuji_entries WHERE short_id = ?", (short_id,)
    )
    row = cur.fetchone()
    return _row_to_entry(row) if row else None


def get_entry(conn: sqlite3.Connection, entry_id: str) -> Optional[ArasujiEntry]:
    """Get an arasuji entry by ID (exact match, with prefix fallback)."""
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE id = ?
        """,
        (entry_id,),
    )
    row = cur.fetchone()
    if row:
        return _row_to_entry(row)

    # Fallback: prefix match for truncated IDs (e.g. first 8 chars)
    if len(entry_id) < 36:
        cur = conn.execute(
            """
            SELECT id, level, content, source_ids_json, start_time, end_time,
                   source_count, message_count, parent_id, is_consolidated, created_at,
                   origin_track_id, is_incomplete
            FROM arasuji_entries
            WHERE id LIKE ?
            LIMIT 1
            """,
            (f"{entry_id}%",),
        )
        row = cur.fetchone()
        return _row_to_entry(row) if row else None

    return None


def get_entries_by_level(
    conn: sqlite3.Connection,
    level: int,
    *,
    only_unconsolidated: bool = False,
    order_by_time: bool = True,
) -> List[ArasujiEntry]:
    """Get all arasuji entries at a specific level."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE level = ?
    """
    params: List[Any] = [level]

    if only_unconsolidated:
        query += " AND is_consolidated = 0"

    if order_by_time:
        query += " ORDER BY end_time ASC"

    cur = conn.execute(query, params)
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_unconsolidated_entries(
    conn: sqlite3.Connection,
    level: int,
    limit: Optional[int] = None,
) -> List[ArasujiEntry]:
    """Get unconsolidated entries at a specific level, ordered by time."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE level = ? AND is_consolidated = 0
        ORDER BY end_time ASC
    """
    params: List[Any] = [level]

    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    cur = conn.execute(query, params)
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_leaf_entries_by_level(conn: sqlite3.Connection, level: int) -> List[ArasujiEntry]:
    """Get 'leaf' entries at a level (not consolidated into higher level)."""
    return get_entries_by_level(conn, level, only_unconsolidated=True)


def mark_consolidated(
    conn: sqlite3.Connection,
    entry_ids: List[str],
    parent_id: str,
    *,
    commit: bool = True,
) -> None:
    """Mark entries as consolidated into a parent entry.

    ``commit=False`` は親 INSERT と子 UPDATE を単一トランザクションに束ねる
    列のあふれ束ね (W4 D6 — M2-a の解) 用。呼び出し側が commit/rollback する。
    """
    if not entry_ids:
        return
    for eid in entry_ids:
        row = _get_chronicle_page_row(conn, eid)
        if row is None:
            continue
        _pid, _old_parent, _content, meta_json, _created = row
        meta = _parse_chronicle_meta(meta_json)
        meta["is_consolidated"] = 1
        conn.execute(
            "UPDATE memopedia_pages SET metadata = ?, parent_id = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), parent_id, eid),
        )
    if commit:
        conn.commit()


def update_entry_content(
    conn: sqlite3.Connection,
    entry_id: str,
    content: str,
) -> bool:
    """Update the content of an arasuji entry.

    Args:
        conn: Database connection
        entry_id: ID of the entry to update
        content: New content text

    Returns:
        True if the entry was found and updated, False otherwise
    """
    row = _get_chronicle_page_row(conn, entry_id)
    if row is None:
        return False
    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        (content, entry_id),
    )
    conn.commit()
    return True


def get_all_entries_ordered(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> List[ArasujiEntry]:
    """Get all entries ordered by end_time descending (newest first)."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        ORDER BY end_time DESC
    """
    if limit is not None:
        query += f" LIMIT {limit}"

    cur = conn.execute(query)
    return [_row_to_entry(row) for row in cur.fetchall()]


def count_entries_by_level(conn: sqlite3.Connection) -> Dict[int, int]:
    """Get count of entries per level."""
    cur = conn.execute(
        """
        SELECT level, COUNT(*) as cnt
        FROM arasuji_entries
        GROUP BY level
        ORDER BY level
        """
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def count_unconsolidated_by_level(conn: sqlite3.Connection) -> Dict[int, int]:
    """Get count of unconsolidated entries per level."""
    cur = conn.execute(
        """
        SELECT level, COUNT(*) as cnt
        FROM arasuji_entries
        WHERE is_consolidated = 0
        GROUP BY level
        ORDER BY level
        """
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def get_max_level(conn: sqlite3.Connection) -> int:
    """Get the maximum level of arasuji entries."""
    cur = conn.execute("SELECT MAX(level) FROM arasuji_entries")
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0


def delete_entry(conn: sqlite3.Connection, entry_id: str) -> bool:
    """Delete an arasuji entry.

    If the entry is level 2+, its child entries (referenced by source_ids)
    are automatically reset: ``is_consolidated`` → 0, ``parent_id`` → NULL
    (physically: reparented back under root_chronicle).
    This ensures children become eligible for re-consolidation.
    """
    entry = get_entry(conn, entry_id)
    if not entry:
        return False

    # Reset children when deleting a consolidated parent
    if entry.level >= 2 and entry.source_ids:
        for sid in entry.source_ids:
            row = _get_chronicle_page_row(conn, sid)
            if row is None:
                continue
            _pid, _parent, _content, meta_json, _created = row
            meta = _parse_chronicle_meta(meta_json)
            meta["is_consolidated"] = 0
            conn.execute(
                "UPDATE memopedia_pages SET metadata = ?, parent_id = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), ROOT_CHRONICLE_ID, sid),
            )

    # 付記印の返却は削除と同一 tx (perception_buffer.unmark_batches_annexed)。
    from sai_memory.perception_buffer import unmark_batches_annexed
    unmark_batches_annexed(conn, [entry_id])
    # 想起用タグの辺も同一 tx で落とす (recall_edges.delete_chunk_page_edges)。
    from sai_memory.memory.recall_edges import delete_chunk_page_edges
    delete_chunk_page_edges(conn, [entry_id])
    # あらすじ埋め込みの道連れ削除 (2026-08-31 裁定 — 孤児埋め込みが unified
    # recall の想起に出る。従来は persona_chronicle_cleanup.py だけが手で消して
    # いたのを、削除経路の本則にする)。
    _delete_arasuji_embeddings(conn, [entry_id])
    conn.execute(
        "DELETE FROM memopedia_pages WHERE id = ? AND category = ?",
        (entry_id, CATEGORY_CHRONICLE),
    )
    conn.commit()

    # 親を持つエントリの削除は、親の帳簿 (source_ids / span / counts) を現況の
    # 子から引き直す (Codex 五巡 H1 — 死んだ子 id の供給源を断つ。UI の個別
    # 削除がこの関数を使う)。content_stale は付けない — 手動削除を上位本文へ
    # 自動で伝えない (手動編集と同族の裁定 R6)。子がゼロになった親は解体される。
    #
    # 引き直しが失敗しても True を返す (Codex 六巡 J1 受容、まはー裁定):
    # 削除自体は確定済みなので True が正直。残る不整合は WARNING での可視化 +
    # 補修ジョブ冒頭の sweep (保険) + API 削除ルートの Beat ロック直列化 (J2 —
    # 並走上書きの族の根絶) の三枚で受ける。
    if entry.parent_id:
        try:
            refresh_ancestor_bookkeeping(
                conn, [entry.parent_id], mark_stale=False,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "[arasuji] parent bookkeeping refresh failed after deleting "
                "%s; the repair sweep will catch the dead reference",
                entry_id, exc_info=True,
            )
    return True


def is_missing_table_error(exc: BaseException) -> bool:
    """OperationalError が「テーブル不在」か (旧 DB の縮退として許容できる唯一の形)。

    DB ロック・スキーマ不整合など他の OperationalError を同じ except で握ると、
    帳簿の付け替え失敗が黙って「成功」の顔をする (Codex 二巡 R3)。この判定を
    通らない例外は呼び出し側で raise する。
    """
    return "no such table" in str(exc)


def _delete_arasuji_embeddings(
    conn: sqlite3.Connection, entry_ids: Sequence[str],
) -> None:
    """arasuji_embeddings の道連れ削除 (テーブルの無い旧 DB は黙って通す)。"""
    ids = [str(e) for e in entry_ids if e]
    if not ids:
        return
    try:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"DELETE FROM arasuji_embeddings WHERE entry_id IN ({placeholders})",
            ids,
        )
    except sqlite3.OperationalError as exc:
        if not is_missing_table_error(exc):
            raise


def delete_entry_and_update_parent(
    conn: sqlite3.Connection,
    entry_id: str,
    *,
    refresh_ancestors: bool = True,
) -> tuple[bool, Optional[str]]:
    """Delete entry and remove from parent's source_ids.

    ``refresh_ancestors`` (Codex 六巡 J5): 既定で、親の帳簿 (span / counts) を
    現況の子から引き直し、空になった親は解体する (delete_entry と同じ着地 —
    取り下げ経路の帳簿がここ経由で自動的に揃う)。**差し替え (swap) の途中**
    だけは False で呼ぶ — 旧を消してから新を親へ繋ぐまでの間に引き直すと、
    一人っ子の親が空と誤認されて解体される (呼び出し側が繋いだ後に自分で
    引き直す契約: regenerate_entry / absorption の phase 3)。

    Returns:
        (success, parent_id) - parent_id is None if no parent existed
    """
    # Get entry to find parent
    entry = get_entry(conn, entry_id)
    if not entry:
        return False, None

    parent_id = entry.parent_id

    # Update parent's source_ids if exists
    if parent_id:
        prow = _get_chronicle_page_row(conn, parent_id)
        if prow is not None:
            _ppid, _pparent, _pcontent, pmeta_json, _pcreated = prow
            pmeta = _parse_chronicle_meta(pmeta_json)
            new_source_ids = [sid for sid in (pmeta.get("source_ids") or []) if sid != entry_id]
            pmeta["source_ids"] = new_source_ids
            conn.execute(
                "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                (json.dumps(pmeta, ensure_ascii=False), parent_id),
            )

    # Delete entry。付記印の返却は削除と同一 tx
    # (perception_buffer.unmark_batches_annexed)。
    from sai_memory.perception_buffer import unmark_batches_annexed
    unmark_batches_annexed(conn, [entry_id])
    # 想起用タグの辺も同一 tx で落とす。再生成 (regenerate_entry) はこの関数で
    # 旧エントリを消して別 id の新エントリへ差し替えるので、ここを飛ばすと
    # 旧 id を指す辺が孤児として残る (recall_edges.delete_chunk_page_edges)。
    from sai_memory.memory.recall_edges import delete_chunk_page_edges
    delete_chunk_page_edges(conn, [entry_id])
    # あらすじ埋め込みの道連れ削除 (2026-08-31 裁定)。
    _delete_arasuji_embeddings(conn, [entry_id])
    conn.execute(
        "DELETE FROM memopedia_pages WHERE id = ? AND category = ?",
        (entry_id, CATEGORY_CHRONICLE),
    )
    conn.commit()

    # 親の帳簿の引き直し (Codex 六巡 J5 — delete_entry と同じ着地)。失敗は
    # WARNING のみ — 削除自体は確定済みで、補修ジョブ冒頭の sweep が保険。
    if refresh_ancestors and parent_id:
        try:
            refresh_ancestor_bookkeeping(conn, [parent_id], mark_stale=False)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "[arasuji] parent bookkeeping refresh failed after deleting "
                "%s; the repair sweep will catch the inconsistency",
                entry_id, exc_info=True,
            )

    return True, parent_id


def add_to_parent_source_ids(
    conn: sqlite3.Connection,
    entry_id: str,
    parent_id: str
) -> bool:
    """Add entry to parent's source_ids and mark as consolidated.

    Args:
        entry_id: ID of entry to add to parent
        parent_id: ID of parent entry

    Returns:
        True if successful, False if parent not found
    """
    prow = _get_chronicle_page_row(conn, parent_id)
    if prow is None:
        return False

    _ppid, _pparent, _pcontent, pmeta_json, _pcreated = prow
    pmeta = _parse_chronicle_meta(pmeta_json)
    new_source_ids = (pmeta.get("source_ids") or []) + [entry_id]
    pmeta["source_ids"] = new_source_ids
    conn.execute(
        "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
        (json.dumps(pmeta, ensure_ascii=False), parent_id),
    )

    # Mark entry as consolidated
    erow = _get_chronicle_page_row(conn, entry_id)
    if erow is not None:
        _eid, _eparent, _econtent, emeta_json, _ecreated = erow
        emeta = _parse_chronicle_meta(emeta_json)
        emeta["is_consolidated"] = 1
        conn.execute(
            "UPDATE memopedia_pages SET metadata = ?, parent_id = ? WHERE id = ?",
            (json.dumps(emeta, ensure_ascii=False), parent_id, entry_id),
        )

    conn.commit()
    return True


def refresh_ancestor_bookkeeping(
    conn: sqlite3.Connection, parent_ids: Sequence[str], *, mark_stale: bool,
) -> List[str]:
    """親 (と先祖) の帳簿を現況の子から引き直す。

    source_ids は生存している子だけに、span (start/end)・source_count・
    message_count・coverage_chars は生存子の合算に更新する。子が全部消えた
    親は解体して、その親の親を同様に処理する。

    ``mark_stale=True`` は吸収の連鎖再生成用 — 引き直した親に ``content_stale``
    の印を付け、flush (absorption.regenerate_upper_entry) が本文を語り直す。
    ``mark_stale=False`` は手動削除の追随 (Codex 五巡 H1) — 帳簿だけ引き直し、
    上位本文へは自動で伝えない (手動編集と同族の裁定 R6。既に付いている印は
    消さない)。

    自前の UPDATE は積んでおいて**末尾の単一 commit** で確定する (ローカル
    レビュー 2026-08-31 L3 — ノードごとの commit だと途中例外で引き直しが
    混在する)。空の親の解体だけは delete_entry_and_update_parent の内部
    commit を伴う (storage 層の 1 操作 1 commit 設計 — その時点までの積みも
    一緒に確定するが、確定内容はどれも単体で正しいので混在の害はない)。

    Returns:
        帳簿を引き直した entry id (発見順・重複なし。解体した親は含まない)。
    """
    processed: List[str] = []
    seen: set = set()
    queue: List[str] = [str(p) for p in parent_ids if p]
    try:
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            row = _get_chronicle_page_row(conn, pid)
            if row is None:
                continue
            meta = _parse_chronicle_meta(row[3])
            source_ids = [str(s) for s in (meta.get("source_ids") or [])]
            children = []
            for sid in source_ids:
                child = get_entry(conn, sid)
                if child is not None:
                    children.append(child)
            grand_parent = get_entry(conn, pid)
            gp_id = grand_parent.parent_id if grand_parent else None
            if not children:
                # 空の親 — 解体 (delete_entry_and_update_parent が親の親から外す)。
                # 先祖の引き直しは本走査が queue で自分で面倒を見る (二重走査と
                # 再帰を避けるため refresh_ancestors=False)。
                delete_entry_and_update_parent(
                    conn, pid, refresh_ancestors=False,
                )
                if gp_id:
                    queue.append(str(gp_id))
                continue
            starts = [c.start_time for c in children if c.start_time is not None]
            ends = [c.end_time for c in children if c.end_time is not None]
            coverage = 0
            for c in children:
                crow = _get_chronicle_page_row(conn, c.id)
                cmeta = _parse_chronicle_meta(crow[3]) if crow else {}
                try:
                    coverage += int(cmeta.get("coverage_chars") or 0)
                except (TypeError, ValueError):
                    pass
            meta["source_ids"] = [c.id for c in children]
            if starts:
                meta["start_time"] = min(starts)
            if ends:
                meta["end_time"] = max(ends)
            meta["source_count"] = len(children)
            meta["message_count"] = sum(c.message_count for c in children)
            if coverage:
                meta["coverage_chars"] = coverage
            if mark_stale:
                meta["content_stale"] = 1
            conn.execute(
                "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), pid),
            )
            processed.append(pid)
            if gp_id:
                queue.append(str(gp_id))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return processed


def list_dead_message_sources(
    conn: sqlite3.Connection,
) -> Dict[str, List[str]]:
    """Lv1 の source_ids のうち、``messages`` に無い id (孤児参照) — **検出だけ**。

    :func:`prune_dead_message_sources` (処置つき) との一点共有。存在の判定は
    生の ``messages`` 行の有無で、あらすじの吸収が隣人を開き直せるかを決める
    ``memory.storage.get_messages_by_ids`` (scope で絞らない素の
    ``WHERE id IN (...)``) と同じ意味論にしてある — 検出と再開の基準が
    食い違うと、掃いたのに開けない形が残る。

    Returns:
        ``{entry_id: [死んだ message id, ...]}`` (孤児が無ければ空 dict)。
    """
    try:
        rows = conn.execute(
            """
            SELECT a.id, s.value
            FROM arasuji_entries a, json_each(a.source_ids_json) s
            WHERE a.level = 1
              AND NOT EXISTS (
                SELECT 1 FROM messages m WHERE m.id = s.value
              )
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if is_missing_table_error(exc):
            return {}  # Chronicle 実績ゼロの新規 DB
        raise  # ロック等は握らない
    dead: Dict[str, List[str]] = {}
    for entry_id, source_id in rows:
        dead.setdefault(str(entry_id), []).append(str(source_id))
    return dead


def prune_dead_message_sources(
    conn: sqlite3.Connection,
) -> Dict[str, List[str]]:
    """Lv1 の source_ids から死んだ message id を落とし、counts を引き直す。

    UI のメッセージ削除 (個別 / 一括 / スレッド) は生ログを消すだけで、その
    メッセージを指す Lv1 あらすじの source_ids を直さない。残った孤児参照は
    被覆計算には効かないが、吸収の再開検査 (absorption の
    「missing source messages」) を落として隣の未被覆断片を永久に取り残す。
    その受け皿 — :func:`refresh_ancestor_bookkeeping` が「上位 → 消えた下位
    あらすじ」に対してやっていることの、「Lv1 → 消えたメッセージ」版。

    ``source_ids`` から死んだ id を除き、``source_count`` / ``message_count``
    を生存 id の実長へ引き直す。**content (本文) には触らない** — これは LLM
    ゼロの帳簿補修で、編纂は本文を新規生成しない (保存則)。span
    (start_time / end_time) も動かさない: 生存材料より広い範囲を主張したまま
    でも害は無く、狭める側は被覆の境界を動かしてしまう。

    材料が全滅した Lv1 (生ログが丸ごと消えた) は**手を付けず WARNING だけ**
    残す。source ゼロへ落とすとどの範囲も被覆しない幽霊エントリになり、
    本文を消せば持ち主の記憶を機構が捨てることになる。生ログが無い以上その
    エントリは開き直せないので、吸収が再開を断るのはむしろ正しい。

    自前の UPDATE は積んで**末尾の単一 commit** で確定する
    (refresh_ancestor_bookkeeping と同じ流儀)。冪等 — 二度目は検出ゼロで
    何も書かない。

    Returns:
        ``{entry_id: [除去した message id, ...]}`` (書き換えた分のみ)。
    """
    import logging

    _logger = logging.getLogger(__name__)
    dead = list_dead_message_sources(conn)
    if not dead:
        return {}

    removed: Dict[str, List[str]] = {}
    try:
        for entry_id, dead_ids in dead.items():
            row = _get_chronicle_page_row(conn, entry_id)
            if row is None:
                continue
            meta = _parse_chronicle_meta(row[3])
            source_ids = [str(s) for s in (meta.get("source_ids") or [])]
            dead_set = set(dead_ids)
            survivors = [s for s in source_ids if s not in dead_set]
            if not survivors:
                _logger.warning(
                    "[arasuji] level-1 entry %s has no surviving source "
                    "messages (%d dead); leaving it untouched — its raw log "
                    "is gone, so it cannot be reopened anyway",
                    entry_id[:8], len(dead_set),
                )
                continue
            meta["source_ids"] = survivors
            meta["source_count"] = len(survivors)
            meta["message_count"] = len(survivors)
            conn.execute(
                "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), entry_id),
            )
            removed[entry_id] = sorted(dead_set)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return removed


def dismantle_entry(
    conn: sqlite3.Connection,
    entry_id: str,
) -> Tuple[bool, List[str]]:
    """Dismantle a consolidated entry, freeing its sources for re-consolidation.

    When a large gap is detected (many unprocessed messages fall within an
    existing higher-level entry's time range), the entry's summary is no
    longer representative.  This function tears it down so that its source
    entries — together with the newly generated ones — can be re-consolidated
    from scratch via the band-overflow consolidation path (bands.py).

    Steps:
        1. Reset all source children to unconsolidated
           (``is_consolidated=0, parent_id=NULL``, i.e. reparented to root_chronicle).
        2. Remove this entry from its parent's ``source_ids`` (if any).
           If the parent has no remaining sources, recursively dismantle it.
        3. Delete this entry.

    Args:
        conn: Database connection
        entry_id: ID of the entry to dismantle

    Returns:
        ``(success, freed_entry_ids)`` — IDs of direct source entries that
        were freed (made unconsolidated).
    """
    import logging
    _logger = logging.getLogger(__name__)

    entry = get_entry(conn, entry_id)
    if not entry:
        return False, []

    freed_ids = list(entry.source_ids)

    # 1. Reset source children to unconsolidated
    for sid in entry.source_ids:
        row = _get_chronicle_page_row(conn, sid)
        if row is None:
            continue
        _pid, _parent, _content, meta_json, _created = row
        meta = _parse_chronicle_meta(meta_json)
        meta["is_consolidated"] = 0
        conn.execute(
            "UPDATE memopedia_pages SET metadata = ?, parent_id = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), ROOT_CHRONICLE_ID, sid),
        )

    # 2. Remove from parent's source_ids
    if entry.parent_id:
        prow = _get_chronicle_page_row(conn, entry.parent_id)
        if prow is not None:
            _ppid, _pparent, _pcontent, pmeta_json, _pcreated = prow
            pmeta = _parse_chronicle_meta(pmeta_json)
            new_source_ids = [sid for sid in (pmeta.get("source_ids") or []) if sid != entry_id]
            if not new_source_ids:
                # Parent has no remaining sources — dismantle it too
                _logger.info(
                    "Parent %s has no remaining sources after removing %s, "
                    "dismantling recursively",
                    entry.parent_id[:8], entry_id[:8],
                )
                dismantle_entry(conn, entry.parent_id)
            else:
                pmeta["source_ids"] = new_source_ids
                pmeta["source_count"] = len(new_source_ids)
                conn.execute(
                    "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                    (json.dumps(pmeta, ensure_ascii=False), entry.parent_id),
                )

    # 3. Delete this entry。付記印の返却は削除と同一 tx
    # (perception_buffer.unmark_batches_annexed)。
    from sai_memory.perception_buffer import unmark_batches_annexed
    unmark_batches_annexed(conn, [entry_id])
    # 想起用タグの辺も同一 tx で落とす (recall_edges.delete_chunk_page_edges)。
    from sai_memory.memory.recall_edges import delete_chunk_page_edges
    delete_chunk_page_edges(conn, [entry_id])
    # あらすじ埋め込みの道連れ削除 (2026-08-31 裁定)。
    _delete_arasuji_embeddings(conn, [entry_id])
    conn.execute(
        "DELETE FROM memopedia_pages WHERE id = ? AND category = ?",
        (entry_id, CATEGORY_CHRONICLE),
    )
    conn.commit()

    _logger.info(
        "Dismantled level-%d entry %s: freed %d source entries",
        entry.level, entry_id[:8], len(freed_ids),
    )
    return True, freed_ids


def get_entries_by_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    max_entries: int = 20,
) -> List[ArasujiEntry]:
    """Get Chronicle entries associated with a specific thread.

    Returns entries ordered by end_time descending, up to max_entries.
    Used for Stelis anchor rendering.
    """
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE thread_id = ?
        ORDER BY end_time DESC
        LIMIT ?
        """,
        (thread_id, max_entries),
    )
    entries = [_row_to_entry(row) for row in cur.fetchall()]
    entries.reverse()  # chronological order
    return entries


def clear_all_entries(conn: sqlite3.Connection) -> int:
    """Delete all arasuji entries (not the root_chronicle trunk). Returns count deleted."""
    # 全エントリが消えるので、付記印は全部返す (削除と同一 tx)。戻った
    # バッチは提示に再登場し、次の編纂の一括回収が引き取る。
    try:
        conn.execute(
            "UPDATE perception_batches SET annexed_entry_id = NULL "
            "WHERE annexed_entry_id IS NOT NULL"
        )
    except sqlite3.OperationalError:
        pass  # perception_batches の無い DB
    # 消えるチャンクを先に数え上げ、想起用タグの辺を同一 tx で落とす
    # (recall_edges.delete_chunk_page_edges)。
    from sai_memory.memory.recall_edges import delete_chunk_page_edges
    doomed_ids = [
        row[0] for row in conn.execute(
            "SELECT id FROM memopedia_pages WHERE category = ? AND is_trunk = 0",
            (CATEGORY_CHRONICLE,),
        )
    ]
    delete_chunk_page_edges(conn, doomed_ids)
    # あらすじ埋め込みの道連れ削除 (2026-08-31 裁定)。
    _delete_arasuji_embeddings(conn, doomed_ids)
    cur = conn.execute(
        "DELETE FROM memopedia_pages WHERE category = ? AND is_trunk = 0",
        (CATEGORY_CHRONICLE,),
    )
    count = cur.rowcount
    conn.execute("DELETE FROM arasuji_progress")
    conn.commit()
    return count


def regenerate_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    model_name: Optional[str] = None,
    persona_id: Optional[str] = None,
) -> Optional[ArasujiEntry]:
    """Regenerate a Chronicle entry while preserving parent relationship.

    This orchestrates the full regeneration process:
    1. Get existing entry and save parent info
    2. Get original messages
    3. Call build_arasuji.regenerate_entry_from_messages for business logic
    4. Delete the old entry and update parent's source_ids
    5. Restore parent relationship for the new entry

    生成が先・削除が後 (generate-then-swap)。逆順 (削除→LLM 生成) だと、LLM
    呼び出しの間ずっと「この範囲を覆うエントリが存在しない」空白が開く —
    生成失敗でエントリを失うだけでなく、その空白中に Metabolism が走ると
    圧縮区間の記録が「あらすじ恒久欠落」と誤判定されて捨てられる
    (docs/issues/chronicle_eviction_applier_veto_deadlock.md 顔その2 の安全網)。
    旧エントリを生かしたまま新エントリを作れば、外から見える空白は無い
    (generate_level1_arasuji は渡されたメッセージから無条件に作るので、旧
    エントリの存在と衝突しない)。

    Args:
        conn: Database connection
        entry_id: ID of entry to regenerate
        model_name: Model to use (defaults to MEMORY_WEAVE_MODEL env var)

    Returns:
        New ArasujiEntry or None on failure
    """
    from sai_memory.memory.storage import get_message

    # 1. Get existing entry
    entry = get_entry(conn, entry_id)
    if not entry:
        return None

    # get_entry は短縮 id の prefix fallback を持つ。以降の再照会・削除を
    # 短縮 id のまま行うと、旧行の消失後に同じ prefix の**別行**を拾って
    # 消しうる (Codex レビュー 2026-07-27)。ここで完全 id に確定する。
    entry_id = entry.id

    if entry.level != 1:
        raise ValueError("Only level-1 entries can be regenerated")

    # 2. Save parent info and source message IDs
    parent_id = entry.parent_id
    source_message_ids = entry.source_ids

    # 3. Get original messages
    messages = []
    for msg_id in source_message_ids:
        msg = get_message(conn, msg_id)
        if msg:
            messages.append(msg)

    if not messages:
        return None

    # Sort by created_at
    messages.sort(key=lambda m: m.created_at)

    # 旧 entry の材料だった知覚バッチ (付記印 = この entry の材料として消費済み)
    # は、再生成でも**材料**として LLM に渡す (2026-08-29 裁定 — digest 本文への
    # 機械的な付記は廃止)。印の新 id への付け替えは swap 成立後 (下)。
    from sai_memory.perception_buffer import (
        list_batches_annexed_to,
        reassign_batches_annexed,
    )
    old_batches = list_batches_annexed_to(conn, entry_id)
    extra_items = [
        {"at": int(b.consumed_at), "text": b.rendered_text} for b in old_batches
    ]

    # 4. Generate the replacement first (LLM call — the old entry stays alive)
    from scripts.arasuji.build_arasuji_core import regenerate_entry_from_messages
    new_entry = regenerate_entry_from_messages(
        conn, messages, model_name, persona_id=persona_id,
        extra_items=extra_items,
    )

    if not new_entry:
        # 生成失敗: 旧エントリは無傷のまま残る (削除していないので何も失わない)
        return None

    # 5. Swap (CAS): 旧行を再取得し、開始時のスナップショットと一致するときだけ
    # 差し替える。LLM の間に並行操作が旧行を動かしていた場合に盲目で進むと、
    # 明示削除された範囲が別 id で復活する / ユーザーの本文編集を LLM 出力で
    # 黙って潰す / 束ねで変わった親関係に陳腐化した parent_id を復元する
    # (Codex レビュー 2026-07-27)。競合時は新行を取り下げて「再生成失敗」として
    # 返す — 並行操作の結果 (削除・編集・束ね) の方を正とする。
    #
    # 取り下げは delete_entry でなく delete_entry_and_update_parent で行う —
    # 生成中の短い窓で束ねが新行を親に繋いでいた場合、親の source_ids から
    # 外さないと宙づりの参照が残る。
    #
    # 検査と差し替えは別々の commit で行われるため、検査直後の並行書き込みとの
    # 競合窓は残る (数 ms)。ここの各 storage 関数は 1 操作 1 commit の設計で、
    # 単一トランザクション化はこの層全体の作り直しになるため、手動 UI 操作
    # 同士の ms 級競合として受容する (issue applier_veto_deadlock に記録)。
    import logging

    def _withdraw_replacement() -> None:
        try:
            delete_entry_and_update_parent(conn, new_entry.id)
        except Exception:
            logging.getLogger(__name__).warning(
                "[arasuji] failed to withdraw replacement entry %s after a "
                "regeneration conflict; a duplicate row may remain",
                new_entry.id, exc_info=True,
            )

    current = get_entry(conn, entry_id)
    if (
        current is None
        or current.id != entry.id
        or current.parent_id != parent_id
        or current.is_consolidated != entry.is_consolidated
        or current.content != entry.content
        or current.source_ids != entry.source_ids
    ):
        _withdraw_replacement()
        return None

    # Fragment の付け替え (2026-08-31 裁定): 旧 entry から抽出済みの知識は
    # 消さず新 entry へ付け替える (放置すると旧 id を指したまま宙に浮き、
    # UI の「抽出された知識」から消える — 再生成ボタンに元からあった欠陥)。
    # 新 entry からの新規抽出は既存機構のまま (重複登録は受容)。
    #
    # 位置は**旧削除より前の可逆フェーズ** (Codex 三巡 F3)、かつ**バッチの印
    # より前** (Codex 四巡 G3): 失敗しうるものを、印を動かす前に済ませる。
    # 逆順 (印 → Fragment) だと、Fragment 失敗時の取り下げ (unmark を通る削除)
    # が新 entry へ移した印を NULL に落とし、旧 entry の帰属が消えて同じ知覚が
    # 次回編纂で二重取り込みされる。
    moved_fragment_ids: List[str] = []

    def _revert_fragments() -> None:
        if not moved_fragment_ids:
            return
        try:
            ph = ",".join("?" for _ in moved_fragment_ids)
            conn.execute(
                f"UPDATE memopedia_fragments SET chronicle_entry_id = ? "
                f"WHERE id IN ({ph})",
                (entry_id, *moved_fragment_ids),
            )
            conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(
                "[arasuji] fragment repoint rollback failed; fragments may "
                "keep pointing at a withdrawn entry", exc_info=True,
            )

    try:
        _rows = conn.execute(
            "SELECT id FROM memopedia_fragments WHERE chronicle_entry_id = ?",
            (entry_id,),
        ).fetchall()
        moved_fragment_ids = [str(r[0]) for r in _rows]
        if moved_fragment_ids:
            conn.execute(
                "UPDATE memopedia_fragments SET chronicle_entry_id = ? "
                "WHERE chronicle_entry_id = ?",
                (new_entry.id, entry_id),
            )
            conn.commit()
    except sqlite3.DatabaseError as exc:
        # 捕捉は DatabaseError の幅で (Codex 十二巡 Q2): OperationalError だけ
        # だと IntegrityError 等が handler の外へ逃げ、取り下げが行われずに
        # 旧と replacement の二重被覆が残る。
        if not is_missing_table_error(exc):
            # 未確定の UPDATE を**明示的に巻き戻してから**取り下げる。
            # ``conn.commit()`` 自体が失敗した場合、付け替えはトランザクション
            # に残ったままで、取り下げ (内部で commit する) に相乗りして確定
            # する — Fragment が削除済みの replacement を指し、UI の「抽出
            # された知識」から消える。
            try:
                conn.rollback()
            except Exception:
                logging.getLogger(__name__).warning(
                    "[arasuji] rollback of the pending fragment repoint "
                    "failed", exc_info=True,
                )
            # 保険: rollback が効かず一部が新 id を指したままの場合に備えて
            # 旧 entry 宛てへ戻す (印の中止経路と同じ形)。
            _revert_fragments()
            logging.getLogger(__name__).warning(
                "[arasuji] fragment repoint failed before the swap; aborting "
                "the regeneration (old entry kept, replacement withdrawn)",
                exc_info=True,
            )
            _withdraw_replacement()
            return None
        moved_fragment_ids = []  # Fragment テーブルの無い DB (旧テスト等)

    # 旧材料バッチの印の付け替え (2026-08-19 Codex 第三巡 #3 → 2026-08-29 裁定で
    # 材料方式に改設計): 上で旧バッチを再生成 LLM の材料として渡したので、印
    # (annexed_entry_id = 消費済みの証) を新 id へ付け替える。
    #
    # 付け替えに失敗したら swap を中止する — 続行すると下の旧 entry 削除の
    # unmark で印が提示へ戻り、同じ知覚が二つの entry の材料になる (二重供給。
    # 旧バッチの内容は新 entry の材料として既に LLM に渡っている)。印の宙吊り
    # も作らない: 新 entry は取り下げ (unmark を通る削除経路)、旧状態はそのまま
    # 維持して None を返す — 再生成は失敗として次回やり直せる。
    if old_batches:
        stamps_moved = False
        stamp_error: Optional[BaseException] = None
        try:
            moved = reassign_batches_annexed(conn, entry_id, new_entry.id)
            # 件数不一致 = 並行操作が印を動かした / 台帳テーブルの無い DB で
            # 0 が返った (reassign はテーブル不在のみ 0 に縮退し、ロック等は
            # raise する — Codex 三巡 F2)。一部だけ
            # 新 id へ移った状態で旧 entry の削除に進まない。
            if moved == len(old_batches):
                conn.commit()
                stamps_moved = True
        except Exception as exc:
            stamp_error = exc
        if not stamps_moved:
            try:
                conn.rollback()
            except Exception:
                pass
            # 保険: rollback が効かず一部の印が新 id を指したままの場合に
            # 備え、逆向きに付け替えて旧 entry 宛てへ戻す (best-effort —
            # 直後の取り下げの unmark 経路が最後の受け皿)。
            try:
                if reassign_batches_annexed(conn, new_entry.id, entry_id):
                    conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logging.getLogger(__name__).warning(
                "[arasuji] failed to repoint perception stamps from %s to %s; "
                "aborting the regeneration swap (old entry kept, replacement "
                "withdrawn; regeneration can be retried)",
                entry_id, new_entry.id, exc_info=stamp_error,
            )
            _revert_fragments()
            _withdraw_replacement()
            return None

    def _revert_stamps() -> None:
        """印を新→旧へ明示的に戻す (Codex 四巡 G3)。

        取り下げ (_withdraw_replacement) の削除経路は unmark = NULL 落ちしか
        持たないため、印を動かした後の失敗ではこれを**取り下げより前に**呼ぶ
        — 呼ばないと旧 entry の帰属が消え、同じ知覚が次回編纂で二重取り込み
        される。id 明示 + 現帰属の条件付き UPDATE (absorption._repoint_batches
        と同じ可逆形)。
        """
        ids = [int(b.id) for b in old_batches]
        if not ids:
            return
        try:
            ph = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE perception_batches SET annexed_entry_id = ? "
                f"WHERE id IN ({ph}) AND annexed_entry_id = ?",
                (entry_id, *ids, new_entry.id),
            )
            conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(
                "[arasuji] perception stamp rollback failed; batches may "
                "return to the presentation via the withdraw unmark",
                exc_info=True,
            )

    try:
        # 差し替えの途中では先祖を引き直さない (refresh_ancestors=False) —
        # 旧を消した直後・新を繋ぐ前に引き直すと、一人っ子の親が空と誤認
        # されて解体される。新を繋いだ後に自分で引き直す (Codex 六巡 J5)。
        success, _ = delete_entry_and_update_parent(
            conn, entry_id, refresh_ancestors=False,
        )
        if not success:
            _revert_stamps()
            _revert_fragments()
            _withdraw_replacement()
            return None
        # 6. Restore parent relationship. 親が直前に消えていた場合 (False)、
        # 旧行の子たちは root 直下の未束ね行へ戻されているので、新行も
        # 親なし・未束ねのまま残すのが兄弟と整合する。
        if parent_id:
            if add_to_parent_source_ids(conn, new_entry.id, parent_id):
                # 帳簿 (span / counts) を最終形の子集合から引き直す (J5 —
                # 失敗しても swap は確定済み。sweep が保険)。
                try:
                    refresh_ancestor_bookkeeping(
                        conn, [parent_id], mark_stale=False,
                    )
                except Exception:
                    logging.getLogger(__name__).warning(
                        "[arasuji] parent bookkeeping refresh failed after "
                        "the swap (parent=%s)", parent_id, exc_info=True,
                    )
        return new_entry
    except Exception:
        # 差し替えの途中失敗で新行だけが残ると、旧新の二重被覆が永続化する。
        # 印と Fragment を旧へ戻してから新行を取り下げ、失敗として返す。
        # 旧削除が成功済みの並びでは巻き戻し先の旧 entry は無い — revert は
        # best-effort (宙に浮いた Fragment は改修前からの既存挙動への縮退)。
        _revert_stamps()
        _revert_fragments()
        _withdraw_replacement()
        raise


# ----- Progress tracking -----


def get_progress(conn: sqlite3.Connection, progress_id: str = "main") -> Optional[ArasujiProgress]:
    """Get arasuji generation progress."""
    cur = conn.execute(
        "SELECT id, last_processed_message_id, last_processed_at FROM arasuji_progress WHERE id = ?",
        (progress_id,),
    )
    row = cur.fetchone()
    if row:
        return ArasujiProgress(
            id=row[0],
            last_processed_message_id=row[1],
            last_processed_at=int(row[2]) if row[2] is not None else None,
        )
    return None


def update_progress(
    conn: sqlite3.Connection,
    last_processed_message_id: str,
    progress_id: str = "main",
) -> None:
    """Update arasuji generation progress."""
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO arasuji_progress (id, last_processed_message_id, last_processed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_processed_message_id = excluded.last_processed_message_id,
            last_processed_at = excluded.last_processed_at
        """,
        (progress_id, last_processed_message_id, now),
    )
    conn.commit()


# ----- Utility functions for context retrieval -----


def get_entries_ending_before(
    conn: sqlite3.Connection,
    end_time: int,
    level: int,
    *,
    limit: int = 10,
) -> List[ArasujiEntry]:
    """Get entries at a level ending before a specific time."""
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE level = ? AND end_time < ?
        ORDER BY end_time DESC
        LIMIT ?
        """,
        (level, end_time, limit),
    )
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_latest_entry_at_level(
    conn: sqlite3.Connection,
    level: int,
    *,
    only_unconsolidated: bool = False,
) -> Optional[ArasujiEntry]:
    """Get the latest (most recent) entry at a specific level."""
    query = """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE level = ?
    """
    if only_unconsolidated:
        query += " AND is_consolidated = 0"
    query += " ORDER BY end_time DESC LIMIT 1"

    cur = conn.execute(query, (level,))
    row = cur.fetchone()
    return _row_to_entry(row) if row else None


def get_children(conn: sqlite3.Connection, parent_id: str) -> List[ArasujiEntry]:
    """Get child entries of a parent arasuji."""
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE parent_id = ?
        ORDER BY end_time ASC
        """,
        (parent_id,),
    )
    return [_row_to_entry(row) for row in cur.fetchall()]


def get_total_message_count(conn: sqlite3.Connection) -> int:
    """Get total messages covered by all level-1 entries."""
    cur = conn.execute(
        "SELECT SUM(message_count) FROM arasuji_entries WHERE level = 1"
    )
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0


def get_frontier_anchor_id(conn: sqlite3.Connection) -> Optional[str]:
    """編纂の最前線から導出した anchor 候補を返す (arasuji_levels.md §14-2)。

    「どこまで編纂が終わっているか」の真実は Chronicle 自身 (一次エントリの
    ``source_ids``) が持つ — session_anchor 行とは独立した persona 単位の概念で、
    別テーブルに写しは保存しない (派生状態の二枚持ちはズレる)。

    返す値は「**編纂対象になれるのにまだどの一次エントリにも畳まれていない、
    正典順 (created_at, rowid) で最初のメッセージ**」の id — つまり anchor が
    ここに立てば、それより前の編纂対象はすべて Chronicle が覆っている
    (被覆の保存 §7-1 が構成的に成立する)。提示コンテキストの途中に圧縮区間が
    ある (未編纂の隙間を跨いで先の episode が畳まれている) 場合は、隙間の
    先頭で止まる — 最前線が保守的に手前へ寄るだけで、被覆は破れない。

    Returns:
        anchor 候補の message id。一次エントリが 1 枚も無い (編纂の実績が無い)、
        または未編纂の編纂対象メッセージが存在しない場合は None。
    """
    cur = conn.execute("SELECT 1 FROM arasuji_entries WHERE level = 1 LIMIT 1")
    if cur.fetchone() is None:
        return None

    from sai_memory.memory.storage import chronicle_eligibility_filter
    clause, params = chronicle_eligibility_filter()
    cur = conn.execute(
        f"""
        SELECT id FROM messages
        WHERE {clause}
          AND id NOT IN (
            SELECT je.value
            FROM arasuji_entries a, json_each(a.source_ids_json) je
            WHERE a.level = 1
          )
        ORDER BY created_at ASC, rowid ASC
        LIMIT 1
        """,
        params,
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def compare_message_positions(
    conn: sqlite3.Connection, id_a: str, id_b: str,
) -> Optional[int]:
    """メッセージ 2 件の正典順 ((created_at, rowid) — W8 S7) を比較する。

    Returns:
        id_a が id_b より後なら 1、前なら -1、同一なら 0。
        どちらかが messages に存在しなければ None (比較不能)。
    """
    if id_a == id_b:
        return 0
    cur = conn.execute(
        "SELECT id, created_at, rowid FROM messages WHERE id IN (?, ?)",
        (str(id_a), str(id_b)),
    )
    positions = {str(row[0]): (row[1] or 0, row[2]) for row in cur.fetchall()}
    pos_a = positions.get(str(id_a))
    pos_b = positions.get(str(id_b))
    if pos_a is None or pos_b is None:
        return None
    if pos_a == pos_b:
        return 0
    return 1 if pos_a > pos_b else -1


def has_overlapping_entries(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    level: int = 1,
) -> bool:
    """Check if there are existing entries that overlap with the given time range.

    An entry overlaps if:
    - entry.start_time <= end_time AND entry.end_time >= start_time

    Args:
        conn: Database connection
        start_time: Start of time range to check
        end_time: End of time range to check
        level: Level to check (default: 1 for level-1 Chronicle)

    Returns:
        True if overlapping entries exist, False otherwise
    """
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM arasuji_entries
        WHERE level = ?
          AND start_time <= ?
          AND end_time >= ?
        """,
        (level, end_time, start_time),
    )
    row = cur.fetchone()
    return row[0] > 0 if row else False


def find_covering_entry(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    level: int,
) -> Optional[ArasujiEntry]:
    """Find an entry at the given level whose time range covers [start_time, end_time].

    Used to detect gap-fill scenarios where a new level-1 entry falls within
    an existing higher-level entry's time range.

    Args:
        conn: Database connection
        start_time: Start of time range to check
        end_time: End of time range to check
        level: Level to search at (typically 2 for gap-fill detection)

    Returns:
        Matching ArasujiEntry or None if no covering entry exists
    """
    cur = conn.execute(
        """
        SELECT id, level, content, source_ids_json, start_time, end_time,
               source_count, message_count, parent_id, is_consolidated, created_at,
               origin_track_id, is_incomplete
        FROM arasuji_entries
        WHERE level = ? AND start_time <= ? AND end_time > ?
        ORDER BY start_time ASC
        LIMIT 1
        """,
        (level, start_time, end_time),
    )
    row = cur.fetchone()
    return _row_to_entry(row) if row else None


def get_entries_covering_messages(
    conn: sqlite3.Connection, message_ids: Sequence[str],
) -> List[ArasujiEntry]:
    """指定メッセージ群を source に持つ一次あらすじ エントリを時系列順で返す。

    退場時に畳んだ範囲 (docs/intent/chronicle_eviction.md §6) から、その範囲を
    覆うあらすじを引き当てるための照会。範囲が複数エントリに分かれている場合も
    あるため List で返す (提示ではまとめて 1 つの圧縮マークに畳む)。
    """
    ids = [str(m) for m in message_ids if m]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"""
        SELECT DISTINCT a.id, a.level, a.content, a.source_ids_json, a.start_time,
               a.end_time, a.source_count, a.message_count, a.parent_id,
               a.is_consolidated, a.created_at, a.origin_track_id, a.is_incomplete,
               a.short_id
        FROM arasuji_entries a, json_each(a.source_ids_json)
        WHERE a.level = 1 AND json_each.value IN ({placeholders})
        ORDER BY a.start_time ASC
        """,
        ids,
    )
    return [_row_to_entry(row) for row in cur.fetchall()]


def search_entries(
    conn: sqlite3.Connection,
    query: Optional[str] = None,
    *,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    level: Optional[int] = None,
    limit: int = 10,
) -> List[ArasujiEntry]:
    """Search arasuji entries by keyword (LIKE) and/or time range and level.

    Args:
        conn: Database connection
        query: Keyword to search in content (LIKE match). None to skip.
        start_time: Filter entries overlapping with this start time.
        end_time: Filter entries overlapping with this end time.
        level: Filter by specific level. None for all levels.
        limit: Maximum results to return.

    Returns:
        List of matching ArasujiEntry, newest first.
    """
    conditions = []
    params: List[Any] = []

    if query:
        # Split by whitespace and match ANY keyword (OR)
        keywords = query.split()
        if len(keywords) > 1:
            keyword_conditions = []
            for kw in keywords:
                keyword_conditions.append("content LIKE ?")
                params.append(f"%{kw}%")
            conditions.append(f"({' OR '.join(keyword_conditions)})")
        else:
            conditions.append("content LIKE ?")
            params.append(f"%{query}%")

    if start_time is not None:
        conditions.append("end_time >= ?")
        params.append(start_time)

    if end_time is not None:
        conditions.append("start_time <= ?")
        params.append(end_time)

    if level is not None:
        conditions.append("level = ?")
        params.append(level)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT {_ENTRY_COLUMNS}
        FROM arasuji_entries
        WHERE {where_clause}
        ORDER BY end_time DESC
        LIMIT ?
    """
    params.append(limit)

    cur = conn.execute(sql, params)
    return [_row_to_entry(row) for row in cur.fetchall()]
