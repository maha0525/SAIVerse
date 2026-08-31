from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sai_memory.logging_utils import debug


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _configure_sqlite(conn: sqlite3.Connection) -> None:
    """Apply durability-friendly PRAGMA settings with env overrides."""
    journal_mode = os.getenv("SAIMEMORY_SQLITE_JOURNAL_MODE", "wal").strip().lower()
    if journal_mode not in {"delete", "truncate", "persist", "memory", "wal", "off"}:
        journal_mode = "wal"
    conn.execute(f"PRAGMA journal_mode={journal_mode.upper()}")

    synchronous = os.getenv("SAIMEMORY_SQLITE_SYNCHRONOUS", "full").strip().lower()
    if synchronous not in {"off", "normal", "full", "extra"}:
        synchronous = "full"
    conn.execute(f"PRAGMA synchronous={synchronous.upper()}")

    wal_autocheckpoint = os.getenv("SAIMEMORY_SQLITE_WAL_AUTOCHECKPOINT")
    try:
        wal_autocheckpoint_int = int(wal_autocheckpoint) if wal_autocheckpoint is not None else 1000
    except Exception:
        wal_autocheckpoint_int = 1000
    if wal_autocheckpoint_int > 0:
        conn.execute(f"PRAGMA wal_autocheckpoint={wal_autocheckpoint_int}")


def init_db(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    _ensure_dir(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    _configure_sqlite(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS threads (
          id TEXT PRIMARY KEY,
          resource_id TEXT,
          overview TEXT,
          overview_updated_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY,
          thread_id TEXT,
          role TEXT,
          content TEXT,
          resource_id TEXT,
          created_at INTEGER,
          metadata TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
          message_id TEXT PRIMARY KEY,
          vector TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_embeddings (
          message_id TEXT,
          chunk_index INTEGER,
          vector TEXT,
          PRIMARY KEY (message_id, chunk_index)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_embeddings_msg ON message_embeddings(message_id)"
    )
    # Backfill legacy embeddings table into message_embeddings as chunk 0 when needed.
    conn.execute(
        """
        INSERT OR IGNORE INTO message_embeddings(message_id, chunk_index, vector)
        SELECT message_id, 0, vector FROM embeddings
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread_created ON messages(thread_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_resource_created ON messages(resource_id, created_at)")
    _ensure_column(conn, "messages", "metadata", "TEXT")

    # 7-layer storage model (Intent A v0.14, Intent B v0.11) — message metadata
    # for line/scope-based context construction.
    #
    # - origin_track_id: Track active at message creation. Enables Track-scoped
    #   filtering for sub-line caches ([3]) and Track-cross-reference queries.
    # - line_role: 'main_line' / 'sub_line' / 'meta_judgment' / 'nested'.
    #   Identifies which 7-layer the message belongs to.
    # - line_id: Origin-line UUID. Distinguishes parallel root sub-lines within
    #   the same Track (multiple [3] caches per Track).
    # - scope: 'committed' / 'discardable' / 'volatile'.
    #   * 'committed' = persisted, included in next prompt construction.
    #   * 'discardable' = meta-judgment branch turn; excluded on continue, promoted
    #     to 'committed' on switch (kept as the next Track's leading rationale).
    #   * 'volatile' = Pulse-scoped temp; deleted at Pulse completion.
    # - paired_action_text: For assistant messages, holds the LLM node's action
    #   prompt (= action template). Replaces the v0.10 pattern of storing the
    #   action text as a standalone user message (handoff route C).
    _ensure_column(conn, "messages", "origin_track_id", "TEXT")
    _ensure_column(conn, "messages", "line_role", "TEXT")
    _ensure_column(conn, "messages", "line_id", "TEXT")
    _ensure_column(conn, "messages", "scope", "TEXT NOT NULL DEFAULT 'committed'")
    _ensure_column(conn, "messages", "paired_action_text", "TEXT")
    # pulse_id: 当該メッセージを生成した Pulse の UUID (Phase 2.5, 2026-05-01)。
    # 過去は metadata.tags の "pulse:{uuid}" タグで識別していたが、json_each で
    # しか引けず INDEX が貼れないため、専用カラム化。バックフィルは下記
    # _backfill_messages_pulse_id でタグから pulse_id を一回限り抽出する。
    _ensure_column(conn, "messages", "pulse_id", "TEXT")
    # thought_signature: Gemini 3.x の thoughtSignature を保持してマルチターン
    # 会話で品質低下を防ぐ。詳細は docs/intent/thought_signature_persistence.md
    _ensure_column(conn, "messages", "thought_signature", "TEXT")
    # spell_origin_id: spell loop 内メッセージのグルーピングキー。
    # spell loop の最初のラウンドで保存された judgment メッセージの id を指す。
    # spell loop 外のメッセージおよび origin 自身は NULL。
    _ensure_column(conn, "messages", "spell_origin_id", "TEXT")
    # spell_seq: spell loop 内のラウンド番号 (1, 2, 3...)。
    _ensure_column(conn, "messages", "spell_seq", "INTEGER")
    # origin_episode: 層0タグ (metadata.origin_episode = メッセージ生成時に開いて
    # いた出来事の episode_ref) の専用列昇格 (W1 Chunk C / D10, 2026-07-19)。
    # episode 読み口 (episode_read スペル / post_session の原本注入) が
    # json_each スキャンなしで引けるようにする。書き込みは add_message が
    # metadata dict から転記する (書き手 = sea/runtime.py は不変)。
    # 列を新規追加したときだけ一度、既存 metadata からバックフィルする。
    _origin_episode_added = _ensure_column(conn, "messages", "origin_episode", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_track_line "
        "ON messages(origin_track_id, line_role, line_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_pulse_id ON messages(pulse_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_origin_episode "
        "ON messages(origin_episode)"
    )
    _backfill_messages_pulse_id(conn)
    if _origin_episode_added:
        _backfill_messages_origin_episode(conn)

    # Stelis threads table for hierarchical context management
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stelis_threads (
            thread_id TEXT PRIMARY KEY,
            parent_thread_id TEXT,
            depth INTEGER NOT NULL DEFAULT 0,
            window_ratio REAL NOT NULL DEFAULT 0.8,
            status TEXT NOT NULL DEFAULT 'active',
            chronicle_prompt TEXT,
            chronicle_summary TEXT,
            created_at INTEGER,
            completed_at INTEGER,
            label TEXT,
            FOREIGN KEY (parent_thread_id) REFERENCES stelis_threads(thread_id)
        )
        """
    )
    # Ensure label column exists (for existing databases)
    _ensure_column(conn, "stelis_threads", "label", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stelis_parent ON stelis_threads(parent_thread_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stelis_status ON stelis_threads(status)")

    # Key-value metadata table for system-level settings (e.g., embedding model name)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embed_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # Pulse logs table for unified memory architecture
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pulse_logs (
            id TEXT PRIMARY KEY,
            pulse_id TEXT NOT NULL,
            thread_id TEXT,
            role TEXT NOT NULL,
            content TEXT,
            node_id TEXT,
            playbook_name TEXT,
            important INTEGER NOT NULL DEFAULT 0,
            tool_calls TEXT,
            tool_call_id TEXT,
            tool_name TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pulse_logs_pulse_id ON pulse_logs(pulse_id)")

    # Memory notes table for lightweight knowledge capture
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_notes (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source_pulse_id TEXT,
            source_time INTEGER,
            resolved INTEGER NOT NULL DEFAULT 0,
            group_label TEXT,
            action TEXT,
            target_page_id TEXT,
            suggested_title TEXT,
            target_category TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_notes_resolved ON memory_notes(resolved)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_notes_thread ON memory_notes(thread_id)")
    # Ensure metadata columns exist for databases created before this migration
    for col in ("group_label", "action", "target_page_id", "suggested_title", "target_category"):
        _ensure_column(conn, "memory_notes", col, "TEXT")

    # 自律行動 v3 形の層 (2026-08-19): 手帳 (activities / memos)、連続性グラフ
    # (thread_edges)、想起用タグの辺 (chunk_page_edges)。定義と読み書き関数は
    # 各モジュールが持ち、ここでは器の用意だけを相乗りさせる (冪等)。
    # 正典: docs/intent/autonomous_behavior_v3.md §13.1/§13.6、
    # docs/issues/memory_continuity_graph.md 決着節。
    from sai_memory.memory.continuity import init_continuity_tables
    from sai_memory.memory.pocketbook import init_pocketbook_tables
    from sai_memory.memory.recall_edges import init_chunk_page_edge_tables

    init_pocketbook_tables(conn)
    init_continuity_tables(conn)
    init_chunk_page_edge_tables(conn)

    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> bool:
    """列が無ければ追加する。追加したとき True (一回限りのバックフィル判定用)。"""
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()
        return True
    return False


def _backfill_messages_pulse_id(conn: sqlite3.Connection) -> None:
    """metadata.tags の ``pulse:{uuid}`` タグから messages.pulse_id を埋める一回限りの移行。

    Phase 2.5 (2026-05-01) 以前に保存されたメッセージは pulse_id 列が NULL の
    まま。`_store_memory` (sea/runtime.py) が `pulse:{uuid}` タグを metadata.tags
    に書いていたため、そこから抜き出して列を埋める。

    べき等。pulse_id IS NULL の行のみを対象にするので、新規保存されたカラム値は
    上書きしない。pulse: タグが見つからない行 (= Pulse 外の保存、例えばデフォルト
    起動時のシステムメッセージ等) はそのまま NULL で残る。
    """
    try:
        cur = conn.execute(
            """
            UPDATE messages
            SET pulse_id = (
                SELECT substr(json_each.value, 7)
                FROM json_each(metadata, '$.tags')
                WHERE json_each.value LIKE 'pulse:%'
                LIMIT 1
            )
            WHERE pulse_id IS NULL
              AND metadata IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM json_each(metadata, '$.tags')
                  WHERE json_each.value LIKE 'pulse:%'
              )
            """
        )
        if cur.rowcount > 0:
            import logging
            logging.getLogger(__name__).info(
                "[storage] Backfilled pulse_id on %d existing message(s) from metadata.tags",
                cur.rowcount,
            )
        conn.commit()
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "[storage] Failed to backfill messages.pulse_id from metadata.tags",
            exc_info=True,
        )
        conn.rollback()


def _backfill_messages_origin_episode(conn: sqlite3.Connection) -> None:
    """metadata JSON の ``origin_episode`` から専用列を埋める一回限りの移行。

    列を新規追加した直後 (init_db) にのみ呼ばれる。LIKE で対象行を絞り、
    既存 DB の全行 UPDATE は走らせない。べき等 (origin_episode IS NULL の
    行のみ) — 失敗しても列は残り、以後の新規行は add_message が転記する。
    """
    try:
        cur = conn.execute(
            """
            UPDATE messages
            SET origin_episode = json_extract(metadata, '$.origin_episode')
            WHERE origin_episode IS NULL
              AND metadata IS NOT NULL
              AND metadata LIKE '%origin_episode%'
              AND json_extract(metadata, '$.origin_episode') IS NOT NULL
            """
        )
        if cur.rowcount > 0:
            import logging
            logging.getLogger(__name__).info(
                "[storage] Backfilled origin_episode on %d existing message(s) "
                "from metadata JSON",
                cur.rowcount,
            )
        conn.commit()
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "[storage] Failed to backfill messages.origin_episode from metadata",
            exc_info=True,
        )
        conn.rollback()


def get_or_create_thread(conn: sqlite3.Connection, thread_id: str, resource_id: Optional[str] = None) -> None:
    cur = conn.execute("SELECT id FROM threads WHERE id=?", (thread_id,))
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO threads(id, resource_id, overview, overview_updated_at) VALUES (?, ?, ?, ?)",
            (thread_id, resource_id, None, None),
        )
        conn.commit()


def set_thread_overview(conn: sqlite3.Connection, thread_id: str, overview: str) -> None:
    conn.execute(
        "UPDATE threads SET overview=?, overview_updated_at=? WHERE id=?",
        (overview, int(time.time()), thread_id),
    )
    conn.commit()


def get_thread_overview(conn: sqlite3.Connection, thread_id: str) -> Optional[str]:
    cur = conn.execute("SELECT overview FROM threads WHERE id=?", (thread_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


@dataclass
class Message:
    id: str
    thread_id: str
    role: str
    content: str
    resource_id: Optional[str]
    created_at: int
    metadata: Optional[Dict[str, Any]] = None
    # 7-layer storage metadata (Phase 1, Intent A v0.14 / Intent B v0.11).
    # Populated only when SELECT statements include the corresponding columns
    # (legacy 7-column SELECTs leave them as None for compatibility).
    line_role: Optional[str] = None
    line_id: Optional[str] = None
    scope: Optional[str] = None
    pulse_id: Optional[str] = None
    # v0.32 (2026-05-09): Track Chronicle / ユーザー会話 Track 親保持機構で利用。
    origin_track_id: Optional[str] = None
    # 2026-05-20: Gemini 3.x の thoughtSignature (opaque な思考連続性トークン)。
    # マルチターン会話で次ターンに echo して品質低下を防ぐ。
    # 詳細は docs/intent/thought_signature_persistence.md
    thought_signature: Optional[str] = None
    # action (LLM ノードへの入力指示)。応答とペアで保持し、scope=committed の
    # 応答についてのみ context に復元する (volatile は揮発)。
    # docs/issues/spell_judgment_recorded_after_subline.md 周辺の議論参照。
    paired_action_text: Optional[str] = None
    spell_origin_id: Optional[str] = None
    spell_seq: Optional[int] = None
    # W1 Chunk C (2026-07-19): 層0タグ (metadata.origin_episode) の専用列。
    # メッセージ生成時に開いていた出来事の episode_ref。
    origin_episode: Optional[str] = None


def _decode_metadata(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data
    return None


# Column suffix for SELECTs that want line metadata. Append after the 7 base
# columns (id, thread_id, role, content, resource_id, created_at, metadata).
# v0.32 (2026-05-09): origin_track_id を末尾に追加。
_LINE_METADATA_COLUMNS = "line_role, line_id, scope, pulse_id, origin_track_id, thought_signature, paired_action_text, spell_origin_id, spell_seq, origin_episode"

# 正典順序キー (W8 / SEA 監査 S7): messages の時系列は ``(created_at, rowid)``
# の辞書式順で決める。``created_at`` は秒精度の意味時刻 (インポート・移植で
# 過去時刻の行が後から入っても歴史位置に並ぶ)、``rowid`` は同一秒内の挿入順
# tie-breaker。この total order を anchor 境界・履歴・pagination・Chronicle
# 編纂の全クエリで共有する — created_at 単独の ORDER BY は同一秒群の順序が
# query plan 依存になり、ページ境界の重複/欠落や evicted prefix の再混入を
# 起こす。境界 (anchor / 範囲端) は行の (created_at, rowid) キーセットで切る。
#
# NULL created_at (native import は欠落を許す) は「全ての実時刻より前」
# (= SQLite の ORDER BY ASC の NULL 並びと同じ) と定義し、NULL 群の中は
# rowid 順。境界句は下のヘルパーが NULL anchor も含めて一貫に扱う。


def _canonical_after_clause(
    anchor_ts: Optional[int], anchor_rowid: int, *, inclusive: bool
) -> Tuple[str, Tuple[Any, ...]]:
    """正典順で anchor より後 (inclusive なら anchor 含む) の WHERE 句を返す。"""
    cmp = ">=" if inclusive else ">"
    if anchor_ts is None:
        # NULL anchor: 実時刻を持つ行は全て後、NULL 群の中は rowid 順。
        return f"(created_at IS NOT NULL OR rowid {cmp} ?)", (anchor_rowid,)
    return (
        f"(created_at > ? OR (created_at = ? AND rowid {cmp} ?))",
        (anchor_ts, anchor_ts, anchor_rowid),
    )


def _canonical_before_clause(
    anchor_ts: Optional[int], anchor_rowid: int, *, inclusive: bool
) -> Tuple[str, Tuple[Any, ...]]:
    """正典順で anchor より前 (inclusive なら anchor 含む) の WHERE 句を返す。"""
    cmp = "<=" if inclusive else "<"
    if anchor_ts is None:
        return f"(created_at IS NULL AND rowid {cmp} ?)", (anchor_rowid,)
    return (
        f"(created_at IS NULL OR created_at < ? OR (created_at = ? AND rowid {cmp} ?))",
        (anchor_ts, anchor_ts, anchor_rowid),
    )


def _canonical_key_of_row(ts: Any, rowid: Any) -> Tuple[Tuple[int, int], int]:
    """(created_at, rowid) を Python 側で比較可能な正典キーに写す (NULL = 先頭側)。"""
    if ts is None:
        return ((0, 0), int(rowid))
    return ((1, int(ts)), int(rowid))


def _row_to_message(row: Tuple[Any, ...]) -> Message:
    # created_at NULL (native import は欠落を許す) は 0 に写す — 正典順の
    # 「全ての実時刻より前」の位置と整合し、datetime 変換する消費側も落ちない。
    return Message(
        id=row[0],
        thread_id=row[1],
        role=row[2],
        content=row[3],
        resource_id=row[4],
        created_at=int(row[5]) if row[5] is not None else 0,
        metadata=_decode_metadata(row[6]) if len(row) > 6 else None,
        line_role=row[7] if len(row) > 7 else None,
        line_id=row[8] if len(row) > 8 else None,
        scope=row[9] if len(row) > 9 else None,
        pulse_id=row[10] if len(row) > 10 else None,
        origin_track_id=row[11] if len(row) > 11 else None,
        thought_signature=row[12] if len(row) > 12 else None,
        paired_action_text=row[13] if len(row) > 13 else None,
        spell_origin_id=row[14] if len(row) > 14 else None,
        spell_seq=int(row[15]) if len(row) > 15 and row[15] is not None else None,
        origin_episode=row[16] if len(row) > 16 else None,
    )


def spell_group_spans(
    rows: Sequence[Tuple[str, Optional[str]]],
) -> List[Tuple[int, int]]:
    """スペルの群が占める添字の区間を返す (この列の規則の一点管理)。

    スペルの群 = 「唱え (assistant 行) → 結果 (system 行 ``[Spell Result: ...]``)
    → 結果を読んだ発話 (assistant 行) → …」のひとまとまり。``spell_origin_id``
    列が群の印になる。

    **非対称に注意**: 群の起点行 (最初の唱え) 自身の ``spell_origin_id`` は
    NULL である。起点は「自分の ``id`` が他の行の ``spell_origin_id`` として
    現れる」ことでしか識別できない。だから群の鍵は次の規則で決める:

    - ``spell_origin_id`` が入っていれば、その値。
    - 入っていなくても、自分の ``id`` が列内のどこかの ``spell_origin_id`` として
      現れるなら、自分の ``id``。
    - どちらでもなければ群に属さない。

    Args:
        rows: 正典順に並んだ ``(message_id, spell_origin_id)`` の列。

    Returns:
        各群が占める添字の閉区間 ``(最初, 最後)`` を、開始位置の昇順で並べたもの。
        区間の内側には群に属さない行 (event_message やメタ判断の割り込み) が
        混じりうる — 区間は「群の最初のメンバーから最後のメンバーまで」であって
        メンバーの集合ではない。1 件しか無い群 (幅ゼロ) は境目を割りようが
        ないので返さない。

    純関数 — DB も I/O も触らない。
    """
    origins = {str(origin) for _, origin in rows if origin}
    spans: Dict[str, List[int]] = {}
    for index, (message_id, origin) in enumerate(rows):
        if origin:
            key = str(origin)
        elif str(message_id) in origins:
            key = str(message_id)
        else:
            continue
        span = spans.get(key)
        if span is None:
            spans[key] = [index, index]
        else:
            span[1] = index
    out = [(lo, hi) for lo, hi in spans.values() if hi > lo]
    out.sort()
    return out


def add_message(
    conn: sqlite3.Connection,
    thread_id: str,
    role: str,
    content: str,
    resource_id: Optional[str] = None,
    created_at: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    origin_track_id: Optional[str] = None,
    line_role: Optional[str] = None,
    line_id: Optional[str] = None,
    scope: Optional[str] = None,
    paired_action_text: Optional[str] = None,
    pulse_id: Optional[str] = None,
    thought_signature: Optional[str] = None,
    spell_origin_id: Optional[str] = None,
    spell_seq: Optional[int] = None,
) -> str:
    """Insert a message row.

    The 7-layer storage metadata (Intent A v0.14, Intent B v0.11) is optional:
    callers that don't supply ``line_role`` / ``line_id`` / ``origin_track_id``
    / ``scope`` / ``paired_action_text`` produce rows compatible with the
    pre-v0.11 schema (NULL columns + ``scope='committed'`` from the column
    default). New callers that pass through ``PulseContext.current_line_metadata()``
    will populate them.

    ``pulse_id`` (Phase 2.5, 2026-05-01) is the dedicated column replacing the
    legacy ``metadata.tags`` "pulse:{uuid}" pattern. Both are written for the
    transition period — the tag is still appended by ``_store_memory`` (read
    paths that only know the tag still work), and the column lets indexed
    queries scope by Pulse without a json_each scan.
    """
    mid = str(uuid.uuid4())
    ts = int(time.time()) if created_at is None else int(created_at)
    meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
    # W1 Chunk C (D10): metadata JSON 内の origin_episode を専用列へ転記する。
    # 書き手 (sea/runtime.py の層0タグ付与) は metadata dict に載せるだけで、
    # 列への昇格は INSERT のこの一点で行う (origin_track_id と同じ流儀)。
    origin_episode: Optional[str] = None
    if isinstance(metadata, dict):
        _oe = metadata.get("origin_episode")
        if isinstance(_oe, str) and _oe.strip():
            origin_episode = _oe.strip()
    # ``scope`` is NOT NULL with DEFAULT 'committed' at the schema level;
    # passing None here lets the DB apply the default.
    if scope is None:
        conn.execute(
            "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata, "
            "origin_track_id, line_role, line_id, paired_action_text, pulse_id, thought_signature, "
            "spell_origin_id, spell_seq, origin_episode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, thread_id, role, content, resource_id, ts, meta_json,
             origin_track_id, line_role, line_id, paired_action_text, pulse_id, thought_signature,
             spell_origin_id, spell_seq, origin_episode),
        )
    else:
        conn.execute(
            "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata, "
            "origin_track_id, line_role, line_id, scope, paired_action_text, pulse_id, thought_signature, "
            "spell_origin_id, spell_seq, origin_episode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, thread_id, role, content, resource_id, ts, meta_json,
             origin_track_id, line_role, line_id, scope, paired_action_text, pulse_id, thought_signature,
             spell_origin_id, spell_seq, origin_episode),
        )
    conn.commit()
    return mid


def replace_message_embeddings(
    conn: sqlite3.Connection,
    message_id: str,
    vectors: Iterable[Iterable[float]],
) -> None:
    conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))
    payload: List[Tuple[str, int, str]] = []
    for idx, vec in enumerate(vectors):
        payload.append((message_id, idx, json.dumps(list(map(float, vec)))))
    if payload:
        conn.executemany(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES(?, ?, ?)",
            payload,
        )
    conn.commit()


def upsert_embedding(conn: sqlite3.Connection, message_id: str, vector: Iterable[float]) -> None:
    """Legacy helper that stores a single embedding as chunk 0."""
    replace_message_embeddings(conn, message_id, [vector])


def count_message_embeddings(conn: sqlite3.Connection) -> int:
    """message_embeddings に登録済みの distinct message_id 数を返す。"""
    row = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM message_embeddings"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_messages_without_embeddings(conn: sqlite3.Connection) -> List["Message"]:
    """埋め込みが1件も無い non-empty content のメッセージを返す（インポート後バックフィル用）。

    content が空/空白のみのメッセージは書き込み時点でも埋め込み対象外
    (_append_message の仕様と同じ) なので、ここでも除外する。
    """
    cur = conn.execute(
        f"""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata,
               {_LINE_METADATA_COLUMNS}
        FROM messages
        WHERE TRIM(COALESCE(content, '')) != ''
          AND id NOT IN (SELECT DISTINCT message_id FROM message_embeddings)
        ORDER BY created_at ASC, rowid ASC
        """
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def get_message(conn: sqlite3.Connection, message_id: str) -> Optional[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at, metadata FROM messages WHERE id=?",
        (message_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return _row_to_message(row)


def get_messages_last(conn: sqlite3.Connection, thread_id: str, limit: int) -> List[Message]:
    # Exclude scope='discardable' rows: these are meta-judgment branch turns
    # that were resolved as "continue" (Intent A v0.14, Intent B v0.11). They
    # remain in the DB so meta-judgment-log retrieval can still find them, but
    # they must not appear in line-cache reconstruction (would defeat the
    # commit/discard property). scope IS NULL covers pre-v0.11 rows.
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
        "FROM messages WHERE thread_id=? "
        "AND (scope IS NULL OR scope != 'discardable') "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (thread_id, limit),
    )
    rows = cur.fetchall()
    return [_row_to_message(row) for row in rows][::-1]


def get_messages_paginated(conn: sqlite3.Connection, thread_id: str, page: int, page_size: int) -> List[Message]:
    offset = max(0, page) * max(1, page_size)
    cur = conn.execute(
        f"SELECT id, thread_id, role, content, resource_id, created_at, metadata, "
        f"{_LINE_METADATA_COLUMNS} "
        "FROM messages WHERE thread_id=? "
        "AND (scope IS NULL OR scope != 'discardable') "
        "ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?",
        (thread_id, page_size, offset),
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def get_messages_from_id(
    conn: sqlite3.Connection, thread_id: str, from_message_id: str
) -> List[Message]:
    """Fetch messages from (including) the given message_id onwards.

    W8 / SEA 監査 S7: 境界は正典順序キー ``(created_at, rowid)`` で切る。
    旧実装は anchor の ``created_at`` 「以上」だけで切っていたため、同一秒に
    anchor より前へ書かれた evicted prefix が窓へ再混入していた (高速な
    tool round / perception flush では同一秒衝突は通常運転で起きる)。
    anchor 行自身は境界に含む (``>=``)。anchor 行が存在しない場合は空を返す
    (旧実装のサブクエリ NULL と同じ挙動)。anchor が別 thread の行でも、その
    ``(created_at, rowid)`` を境界として要求 thread の行を返す (旧実装の
    timestamp 境界と同じ耐性)。created_at 欠落 (NULL) の anchor は正典順の
    先頭側 (NULL 群) の位置として扱う。
    """
    anchor = conn.execute(
        "SELECT created_at, rowid FROM messages WHERE id = ?",
        (from_message_id,),
    ).fetchone()
    if not anchor:
        return []
    anchor_ts = int(anchor[0]) if anchor[0] is not None else None
    after_sql, after_params = _canonical_after_clause(
        anchor_ts, int(anchor[1]), inclusive=True
    )
    cur = conn.execute(
        f"SELECT id, thread_id, role, content, resource_id, created_at, metadata, "
        f"{_LINE_METADATA_COLUMNS} "
        "FROM messages "
        f"WHERE thread_id = ? AND {after_sql} "
        "ORDER BY created_at ASC, rowid ASC",
        (thread_id, *after_params),
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def get_messages_before_id(
    conn: sqlite3.Connection, thread_id: str, before_message_id: str,
    *, limit: int,
) -> List[Message]:
    """境界メッセージより正典順で**前**の行を、新しい側から最大 ``limit`` 件返す。

    読み戻し (arasuji_levels.md §15) の anchor 引き戻しの材料読み。境界の規律は
    :func:`get_messages_from_id` と同じ正典順序キー ``(created_at, rowid)``
    (W8 / SEA 監査 S7)。境界行自身は含まない (排他)。戻り値は時系列昇順。
    境界行が存在しない場合は空。呼び出し側は戻りの先頭 (最古) の id を次の
    境界にしてページングできる。
    """
    anchor = conn.execute(
        "SELECT created_at, rowid FROM messages WHERE id = ?",
        (before_message_id,),
    ).fetchone()
    if not anchor:
        return []
    anchor_ts = int(anchor[0]) if anchor[0] is not None else None
    before_sql, before_params = _canonical_before_clause(
        anchor_ts, int(anchor[1]), inclusive=False
    )
    # DESC は正典順のちょうど逆 — NULL created_at (正典順の先頭側) は SQLite の
    # DESC で末尾に並ぶので、逆順の契約と一致する。
    cur = conn.execute(
        f"SELECT id, thread_id, role, content, resource_id, created_at, metadata, "
        f"{_LINE_METADATA_COLUMNS} "
        "FROM messages "
        f"WHERE thread_id = ? AND {before_sql} "
        "ORDER BY created_at DESC, rowid DESC "
        "LIMIT ?",
        (thread_id, *before_params, int(limit)),
    )
    rows = [_row_to_message(row) for row in cur.fetchall()]
    rows.reverse()
    return rows


def get_next_message_id(
    conn: sqlite3.Connection, message_id: str
) -> Optional[str]:
    """正典順 ``(created_at, rowid)`` で ``message_id`` の**直後**にある行の id。

    境界の規律は :func:`get_messages_from_id` と同じ正典順序キー (W8 / SEA 監査
    S7) で、境界行自身は含まない (排他)。thread では絞らない — 「この位置より
    後ろのメッセージは 1 件も無い」と言い切れる最も手前の位置を返すため
    (絞ると別 thread の行を跨いでしまう)。

    用途は起点 (session_anchor) の前進を「スルースが見た範囲の末尾の次」で
    頭打ちにする判定 (:mod:`sea.session_lifecycle` の §14 機構 1)。起点は位置
    としてだけ使われる (:func:`get_messages_from_id` は別 thread の行でも
    その ``(created_at, rowid)`` を境界として扱う) ので、返り値が要求 thread の
    行である必要はない。

    Returns:
        直後の行の id。``message_id`` が存在しない、または直後の行が無い
        (最後尾) 場合は None。
    """
    anchor = conn.execute(
        "SELECT created_at, rowid FROM messages WHERE id = ?",
        (str(message_id),),
    ).fetchone()
    if not anchor:
        return None
    anchor_ts = int(anchor[0]) if anchor[0] is not None else None
    after_sql, after_params = _canonical_after_clause(
        anchor_ts, int(anchor[1]), inclusive=False
    )
    row = conn.execute(
        f"SELECT id FROM messages WHERE {after_sql} "
        "ORDER BY created_at ASC, rowid ASC LIMIT 1",
        after_params,
    ).fetchone()
    return str(row[0]) if row else None


def get_messages_by_resource(conn: sqlite3.Connection, resource_id: str) -> List[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at, metadata FROM messages WHERE resource_id=? ORDER BY created_at ASC, rowid ASC",
        (resource_id,),
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def get_all_messages_for_search(
    conn: sqlite3.Connection,
    required_tags: Optional[List[str]] = None,
) -> List[Message]:
    """Get all messages for keyword search, optionally filtered by tags."""
    params = []

    if required_tags:
        # Build tag filter
        tag_conditions = []
        for tag in required_tags:
            tag_conditions.append(
                "EXISTS (SELECT 1 FROM json_each(metadata, '$.tags') WHERE json_each.value = ?)"
            )
            params.append(tag)
        tags_clause = " WHERE " + " OR ".join(tag_conditions)
    else:
        tags_clause = ""

    query = f"""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        {tags_clause}
        ORDER BY created_at DESC, rowid DESC
    """
    cur = conn.execute(query, params)
    return [_row_to_message(row) for row in cur.fetchall()]


def get_embeddings_for_scope(
    conn: sqlite3.Connection,
    thread_id: Optional[str] = None,
    resource_id: Optional[str] = None,
    required_tags: Optional[List[str]] = None,
    exclude_tags: Optional[List[str]] = None,
) -> List[Tuple[Message, List[float], int]]:
    # Build tags filter clause
    tags_clause = ""
    params = []

    if required_tags:
        # Add OR condition for each required tag
        tag_conditions = []
        for tag in required_tags:
            tag_conditions.append(
                "EXISTS (SELECT 1 FROM json_each(m.metadata, '$.tags') WHERE json_each.value = ?)"
            )
            params.append(tag)
        tags_clause = " AND (" + " OR ".join(tag_conditions) + ")"

    if exclude_tags:
        for tag in exclude_tags:
            tags_clause += (
                " AND NOT EXISTS (SELECT 1 FROM json_each(m.metadata, '$.tags') WHERE json_each.value = ?)"
            )
            params.append(tag)

    base_query = """
        SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, m.metadata, e.vector, e.chunk_index
        FROM messages m JOIN message_embeddings e ON m.id = e.message_id
    """

    if thread_id:
        query = base_query + f"WHERE m.thread_id=?{tags_clause} ORDER BY m.created_at ASC, m.rowid ASC, e.chunk_index ASC"
        cur = conn.execute(query, (thread_id, *params))
    elif resource_id:
        query = base_query + f"WHERE m.resource_id=?{tags_clause} ORDER BY m.created_at ASC, m.rowid ASC, e.chunk_index ASC"
        cur = conn.execute(query, (resource_id, *params))
    else:
        query = base_query + f"WHERE 1=1{tags_clause} ORDER BY m.created_at ASC, m.rowid ASC, e.chunk_index ASC"
        cur = conn.execute(query, tuple(params))

    rows = cur.fetchall()
    out: List[Tuple[Message, List[float], int]] = []
    for row in rows:
        msg = _row_to_message(row[:7])
        vec_raw = json.loads(row[7])
        if isinstance(vec_raw, list) and vec_raw and isinstance(vec_raw[0], list):
            # Legacy multi-vector stored in legacy embeddings table.
            for idx, entry in enumerate(vec_raw):
                vec = [float(v) for v in entry]
                out.append((msg, vec, idx))
        else:
            vec = [float(v) for v in vec_raw]
            chunk_index = int(row[8]) if len(row) > 8 else 0
            out.append((msg, vec, chunk_index))
    return out


def get_messages_around(
    conn: sqlite3.Connection, thread_id: str, message_id: str, before: int, after: int
) -> List[Message]:
    """anchor メッセージの前後 ``before``/``after`` 件を正典順で返す (anchor 自身は含まない)。

    W8 / SEA 監査 S7: 窓の前後判定も正典順序キー ``(created_at, rowid)`` で
    切る。旧実装は rowid 単独だったため、過去時刻で後から挿入された行
    (インポート・移植) が「後」側に化けて履歴表示と食い違った。created_at
    欠落 (NULL) の anchor も正典順の先頭側 (NULL 群) の位置として扱う。
    """
    if before <= 0 and after <= 0:
        return []

    cur = conn.execute(
        "SELECT created_at, rowid FROM messages WHERE id=? AND thread_id=?",
        (message_id, thread_id),
    )
    row = cur.fetchone()
    if not row:
        return []
    anchor_ts = int(row[0]) if row[0] is not None else None
    anchor_rowid = int(row[1])

    before_rows: List[Message] = []
    if before > 0:
        before_sql, before_params = _canonical_before_clause(
            anchor_ts, anchor_rowid, inclusive=False
        )
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            f"FROM messages WHERE thread_id=? AND {before_sql} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (thread_id, *before_params, before),
        )
        before_rows = [_row_to_message(r) for r in cur.fetchall()][::-1]

    after_rows: List[Message] = []
    if after > 0:
        after_sql, after_params = _canonical_after_clause(
            anchor_ts, anchor_rowid, inclusive=False
        )
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            f"FROM messages WHERE thread_id=? AND {after_sql} "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (thread_id, *after_params, after),
        )
        after_rows = [_row_to_message(r) for r in cur.fetchall()]

    return before_rows + after_rows


def get_messages_around_timestamp(
    conn: sqlite3.Connection,
    timestamp: int,
    count: int = 10,
    thread_id: Optional[str] = None,
) -> List[Message]:
    """Retrieve messages closest to a given Unix timestamp.

    Finds the nearest message to *timestamp* and returns *count* messages
    centred on that point (half before, half after).  When *thread_id* is
    ``None`` the search spans all threads.
    """
    if count <= 0:
        return []

    half = count // 2

    # --- messages AT or BEFORE the timestamp (descending) ---
    # 境界は秒精度の timestamp 引数そのもの (行キーセットではない) なので
    # <= / > の分割は従来どおり。同一秒群内の並びだけ正典 tie-breaker で固定。
    if thread_id:
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            "FROM messages WHERE thread_id=? AND created_at <= ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (thread_id, timestamp, half + 1),
        )
    else:
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            "FROM messages WHERE created_at <= ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (timestamp, half + 1),
        )
    before_rows = [_row_to_message(r) for r in cur.fetchall()][::-1]

    # --- messages AFTER the timestamp (ascending) ---
    if thread_id:
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            "FROM messages WHERE thread_id=? AND created_at > ? "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (thread_id, timestamp, count - len(before_rows)),
        )
    else:
        cur = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            "FROM messages WHERE created_at > ? "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (timestamp, count - len(before_rows)),
        )
    after_rows = [_row_to_message(r) for r in cur.fetchall()]

    combined = before_rows + after_rows

    # If we got fewer before-rows than expected, backfill from after side
    # (and vice versa) so we always try to return *count* messages total.
    if len(combined) < count:
        if len(before_rows) < half + 1 and after_rows:
            # Need more after
            last_ts = combined[-1].created_at
            need = count - len(combined)
            if thread_id:
                cur = conn.execute(
                    "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                    "FROM messages WHERE thread_id=? AND created_at > ? "
                    "ORDER BY created_at ASC, rowid ASC LIMIT ?",
                    (thread_id, last_ts, need),
                )
            else:
                cur = conn.execute(
                    "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                    "FROM messages WHERE created_at > ? "
                    "ORDER BY created_at ASC, rowid ASC LIMIT ?",
                    (last_ts, need),
                )
            combined.extend(_row_to_message(r) for r in cur.fetchall())
        elif len(after_rows) == 0 or (before_rows and len(after_rows) < count - len(before_rows)):
            # Need more before
            first_ts = combined[0].created_at if combined else timestamp
            need = count - len(combined)
            if thread_id:
                cur = conn.execute(
                    "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                    "FROM messages WHERE thread_id=? AND created_at < ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (thread_id, first_ts, need),
                )
            else:
                cur = conn.execute(
                    "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                    "FROM messages WHERE created_at < ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (first_ts, need),
                )
            extra = [_row_to_message(r) for r in cur.fetchall()][::-1]
            combined = extra + combined

    return combined[:count]


def get_messages_with_persona_in_audience(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    thread_id: Optional[str] = None,
    exclude_message_ids: Optional[set[str]] = None,
    required_tags: Optional[List[str]] = None,
    limit: int = 10,
) -> List[Message]:
    """Get messages where a specific persona is in the audience or is the speaker.

    Filters messages where metadata.audience.personas contains persona_id
    (outgoing messages heard by the target) OR metadata.with contains persona_id
    (incoming messages from the target, ingested via auto_ingest).

    Args:
        conn: Database connection
        persona_id: Persona ID to search for in audience or with
        thread_id: Optional thread filter
        exclude_message_ids: Message IDs to exclude (e.g., recent context)
        required_tags: Only include messages with these tags
        limit: Maximum number of messages to return

    Returns:
        List of messages, ordered by created_at DESC (most recent first)
    """
    params = [persona_id, persona_id]

    # Build WHERE clause
    where_conditions = [
        "("
        "EXISTS (SELECT 1 FROM json_each(metadata, '$.audience.personas') WHERE json_each.value = ?)"
        " OR "
        "EXISTS (SELECT 1 FROM json_each(metadata, '$.with') WHERE json_each.value = ?)"
        ")"
    ]

    if thread_id is not None:
        where_conditions.append("thread_id = ?")
        params.append(thread_id)

    if required_tags:
        for tag in required_tags:
            where_conditions.append(
                "EXISTS (SELECT 1 FROM json_each(metadata, '$.tags') WHERE json_each.value = ?)"
            )
            params.append(tag)

    where_clause = " AND ".join(where_conditions)

    query = f"""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        WHERE {where_clause}
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
    """
    params.append(limit)

    cur = conn.execute(query, params)
    all_messages = [_row_to_message(r) for r in cur.fetchall()]

    # Filter out excluded message IDs
    if exclude_message_ids:
        all_messages = [m for m in all_messages if m.id not in exclude_message_ids]

    return all_messages


def count_threads(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM threads")
    return int(cur.fetchone()[0])


def sample_messages(conn: sqlite3.Connection, limit: int = 5) -> List[Message]:
    cur = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at, metadata FROM messages ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def compose_message_content(
    conn: sqlite3.Connection,
    message: Message,
    *,
    per_message_char_limit: int = 800,
    viewing_thread_id: Optional[str] = None,
) -> str:
    """Compose a message's textual content including linked thread snippets.

    Args:
        conn: Database connection
        message: The message to compose
        per_message_char_limit: Character limit for linked thread excerpts
        viewing_thread_id: The thread from which this message is being viewed.
                          Used for Stelis anchor expansion to determine display mode.
    """
    metadata = message.metadata or {}
    if not isinstance(metadata, dict):
        return (message.content or "").strip()

    # Check for Stelis anchor message
    if metadata.get("type") == "stelis_anchor":
        return _render_stelis_anchor(conn, metadata, viewing_thread_id)

    base = (message.content or "").strip()

    extras = _render_other_thread_messages(
        conn,
        metadata.get("other_thread_messages"),
        per_message_char_limit=per_message_char_limit,
    )
    if not extras:
        return base

    if base:
        return f"{base}\n\n{extras}"
    return extras


def _render_stelis_anchor(
    conn: sqlite3.Connection,
    metadata: Dict[str, Any],
    viewing_thread_id: Optional[str],
) -> str:
    """Render a Stelis anchor message with dynamic content.

    If viewing from a descendant thread (child, grandchild, etc.), shows simple message.
    If viewing from parent thread or unrelated thread, shows full details.
    """
    stelis_thread_id = metadata.get("stelis_thread_id")
    stelis_label = metadata.get("stelis_label", "Stelis Session")

    if not stelis_thread_id:
        return f"[Stelisスレッド: {stelis_label}]"

    # Check if viewing from a descendant thread
    if viewing_thread_id and _is_descendant_of_stelis(conn, viewing_thread_id, stelis_thread_id):
        # Simple display for descendants (avoid duplication)
        return f"[Stelisスレッド {stelis_label} ({stelis_thread_id}) が開始しました]"

    # Full display for parent thread or unrelated threads
    return _render_stelis_anchor_full(conn, stelis_thread_id, stelis_label)


def _is_descendant_of_stelis(
    conn: sqlite3.Connection,
    viewing_thread_id: str,
    stelis_thread_id: str,
) -> bool:
    """Check if viewing_thread_id is the same as or a descendant of stelis_thread_id."""
    if viewing_thread_id == stelis_thread_id:
        return True

    # Get ancestor chain for viewing thread
    ancestor_chain = get_stelis_ancestor_chain(conn, viewing_thread_id)
    ancestor_ids = {s.thread_id for s in ancestor_chain}

    return stelis_thread_id in ancestor_ids


def _render_stelis_anchor_full(
    conn: sqlite3.Connection,
    stelis_thread_id: str,
    stelis_label: str,
) -> str:
    """Render full Stelis anchor content with Chronicle, timestamps, and recent messages."""
    stelis = get_stelis_thread(conn, stelis_thread_id)

    lines = [f"[Stelisスレッド: {stelis_label}]"]
    lines.append(f"- ID: {stelis_thread_id}")

    if stelis:
        # Timestamps
        if stelis.created_at:
            start_dt = datetime.fromtimestamp(stelis.created_at)
            lines.append(f"- 開始: {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")

        if stelis.completed_at:
            end_dt = datetime.fromtimestamp(stelis.completed_at)
            lines.append(f"- 終了: {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            lines.append("- 終了: 進行中")

        # Message count
        cur = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id = ?",
            (stelis_thread_id,)
        )
        msg_count = cur.fetchone()[0]
        lines.append(f"- メッセージ数: {msg_count}")

        # Chronicle summary (legacy: manual summary set on thread completion)
        if stelis.chronicle_summary:
            lines.append("")
            lines.append("## Chronicle (完了時サマリー)")
            lines.append(stelis.chronicle_summary)

        # Chronicle entries for this Stelis thread (generated with thread_id)
        try:
            from sai_memory.arasuji.storage import get_entries_by_thread
            thread_entries = get_entries_by_thread(conn, stelis_thread_id, max_entries=20)
            if thread_entries:
                lines.append("")
                lines.append("## 活動のあらすじ")
                lines.append("```")
                for entry in thread_entries:
                    level_prefix = "  " * (entry.level - 1)
                    lines.append(f"{level_prefix}[Lv{entry.level}] {entry.content}")
                lines.append("```")
        except Exception:
            pass  # Chronicle not available

        # First messages (opening context, e.g., decision phase)
        cur_first = conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
            "FROM messages WHERE thread_id=? ORDER BY created_at ASC, rowid ASC LIMIT 3",
            (stelis_thread_id,),
        )
        first_msgs = [_row_to_message(r) for r in cur_first.fetchall()]
        first_ids = {m.id for m in first_msgs}

        if first_msgs:
            lines.append("")
            lines.append("## 開始時のやり取り")
            lines.append("```")
            for msg in first_msgs:
                role = "assistant" if msg.role == "model" else msg.role
                content = (msg.content or "").strip()
                if content:
                    lines.append(f"[{role}]: {content}")
            lines.append("```")

        # Recent messages (last 3, excluding those already shown)
        recent_msgs = get_messages_last(conn, stelis_thread_id, 3)
        recent_msgs = [m for m in recent_msgs if m.id not in first_ids]
        if recent_msgs:
            lines.append("")
            lines.append("## 最新のやり取り")
            lines.append("```")
            for msg in recent_msgs:
                role = "assistant" if msg.role == "model" else msg.role
                content = (msg.content or "").strip()
                if content:
                    lines.append(f"[{role}]: {content}")
            lines.append("```")
    else:
        lines.append("- 状態: 情報取得不可")

    return "\n".join(lines)


def _render_other_thread_messages(
    conn: sqlite3.Connection,
    entries: Any,
    *,
    per_message_char_limit: int,
) -> str:
    if not isinstance(entries, list):
        return ""

    blocks: List[str] = []
    seen: set[Tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        thread_id = entry.get("thread_id")
        message_id = entry.get("message_id")
        if not isinstance(thread_id, str) or not isinstance(message_id, str):
            continue
        key = (thread_id, message_id)
        if key in seen:
            continue
        seen.add(key)
        before = _safe_int(entry.get("range_before"), default=0)
        after = _safe_int(entry.get("range_after"), default=0)
        block = _render_thread_excerpt(
            conn,
            thread_id=thread_id,
            message_id=message_id,
            before=max(0, before),
            after=max(0, after),
            per_message_char_limit=per_message_char_limit,
        )
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _render_thread_excerpt(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    message_id: str,
    before: int,
    after: int,
    per_message_char_limit: int,
) -> str:
    anchor = get_message(conn, message_id)
    if anchor is None or anchor.thread_id != thread_id:
        return ""

    before_msgs = get_messages_around(conn, thread_id, message_id, before, 0) if before > 0 else []
    after_msgs = get_messages_around(conn, thread_id, message_id, 0, after) if after > 0 else []

    bundle = before_msgs + [anchor] + after_msgs
    if not bundle:
        return ""

    lines: List[str] = [f"[linked-thread {thread_id}]"]
    for msg in bundle:
        snippet = (msg.content or "").strip()
        if per_message_char_limit > 0 and len(snippet) > per_message_char_limit:
            snippet = snippet[: per_message_char_limit - 1] + "…"
        ts = datetime.fromtimestamp(msg.created_at).isoformat()
        role = "assistant" if msg.role == "model" else msg.role
        if snippet:
            lines.append(f"- {role} @ {ts}: {snippet}")
        else:
            lines.append(f"- {role} @ {ts}")
    return "\n".join(lines)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def list_thread_ids(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute("SELECT id FROM threads ORDER BY id ASC")
    return [r[0] for r in cur.fetchall()]


def count_messages(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM messages")
    return int(cur.fetchone()[0])


def get_messages_by_ids(
    conn: sqlite3.Connection, message_ids: Sequence[str]
) -> List[Message]:
    """id 指定でメッセージを取り出す (正典順 = created_at, rowid)。

    Chronicle entry の source_ids からの読み直し (Fragment 抽出の付箋の
    拾い直しなど) 用。存在しない id は黙って落ちる。
    """
    if not message_ids:
        return []
    ph = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
        f"FROM messages WHERE id IN ({ph}) "
        "ORDER BY created_at ASC, rowid ASC",
        tuple(message_ids),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def delete_thread(conn: sqlite3.Connection, thread_id: str) -> bool:
    """Delete a thread and all its messages + embeddings."""
    try:
        # 1. Get message IDs to delete embeddings
        cur = conn.execute("SELECT id FROM messages WHERE thread_id=?", (thread_id,))
        msg_ids = [row[0] for row in cur.fetchall()]

        if not msg_ids:
            # Maybe just thread record exists
            conn.execute("DELETE FROM threads WHERE id=?", (thread_id,))
            conn.commit()
            return True

        # 2. Delete embeddings
        # optimizing by not using executemany for ids if list is huge, but here it's fine or use explicit loop
        # SQLite limit is usually high, but let's be safe
        placeholders = ",".join("?" * len(msg_ids))
        conn.execute(f"DELETE FROM message_embeddings WHERE message_id IN ({placeholders})", msg_ids)
        conn.execute(f"DELETE FROM embeddings WHERE message_id IN ({placeholders})", msg_ids)

        # 3. Delete messages
        conn.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))

        # 4. Delete thread record
        conn.execute("DELETE FROM threads WHERE id=?", (thread_id,))

        conn.commit()
        return True
    except Exception as e:
        debug(f"Error deleting thread {thread_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Stelis Thread Management
# ---------------------------------------------------------------------------

@dataclass
class StelisThread:
    """Represents a Stelis thread for hierarchical context management."""
    thread_id: str
    parent_thread_id: Optional[str]
    depth: int
    window_ratio: float
    status: str  # 'active', 'completed', 'aborted'
    chronicle_prompt: Optional[str]
    chronicle_summary: Optional[str]
    created_at: Optional[int]
    completed_at: Optional[int]
    label: Optional[str] = None


def _row_to_stelis_thread(row: Tuple[Any, ...]) -> StelisThread:
    return StelisThread(
        thread_id=row[0],
        parent_thread_id=row[1],
        depth=int(row[2]),
        window_ratio=float(row[3]),
        status=row[4],
        chronicle_prompt=row[5],
        chronicle_summary=row[6],
        created_at=int(row[7]) if row[7] is not None else None,
        completed_at=int(row[8]) if row[8] is not None else None,
        label=row[9] if len(row) > 9 else None,
    )


def create_stelis_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    parent_thread_id: Optional[str] = None,
    window_ratio: float = 0.8,
    chronicle_prompt: Optional[str] = None,
    label: Optional[str] = None,
) -> StelisThread:
    """Create a new Stelis thread with parent relationship."""
    # Calculate depth from parent
    depth = 0
    if parent_thread_id:
        parent = get_stelis_thread(conn, parent_thread_id)
        if parent:
            depth = parent.depth + 1

    created_at = int(time.time())
    conn.execute(
        """
        INSERT INTO stelis_threads
        (thread_id, parent_thread_id, depth, window_ratio, status, chronicle_prompt, created_at, label)
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (thread_id, parent_thread_id, depth, window_ratio, chronicle_prompt, created_at, label),
    )
    conn.commit()

    return StelisThread(
        thread_id=thread_id,
        parent_thread_id=parent_thread_id,
        depth=depth,
        window_ratio=window_ratio,
        status="active",
        chronicle_prompt=chronicle_prompt,
        chronicle_summary=None,
        created_at=created_at,
        completed_at=None,
        label=label,
    )


def get_stelis_thread(conn: sqlite3.Connection, thread_id: str) -> Optional[StelisThread]:
    """Get a Stelis thread by ID."""
    cur = conn.execute(
        """
        SELECT thread_id, parent_thread_id, depth, window_ratio, status,
               chronicle_prompt, chronicle_summary, created_at, completed_at, label
        FROM stelis_threads WHERE thread_id = ?
        """,
        (thread_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return _row_to_stelis_thread(row)


def get_stelis_thread_depth(conn: sqlite3.Connection, thread_id: str) -> int:
    """Get the depth of a thread (0 for root, -1 if not a Stelis thread)."""
    stelis = get_stelis_thread(conn, thread_id)
    if stelis:
        return stelis.depth
    return -1  # Not a Stelis thread (could be root or regular thread)


def get_stelis_children(conn: sqlite3.Connection, parent_thread_id: str) -> List[StelisThread]:
    """Get all direct child Stelis threads."""
    cur = conn.execute(
        """
        SELECT thread_id, parent_thread_id, depth, window_ratio, status,
               chronicle_prompt, chronicle_summary, created_at, completed_at, label
        FROM stelis_threads WHERE parent_thread_id = ?
        ORDER BY created_at ASC
        """,
        (parent_thread_id,),
    )
    return [_row_to_stelis_thread(row) for row in cur.fetchall()]


def get_active_stelis_threads(conn: sqlite3.Connection, parent_thread_id: Optional[str] = None) -> List[StelisThread]:
    """Get all active Stelis threads, optionally filtered by parent."""
    if parent_thread_id:
        cur = conn.execute(
            """
            SELECT thread_id, parent_thread_id, depth, window_ratio, status,
                   chronicle_prompt, chronicle_summary, created_at, completed_at, label
            FROM stelis_threads WHERE status = 'active' AND parent_thread_id = ?
            ORDER BY created_at ASC
            """,
            (parent_thread_id,),
        )
    else:
        cur = conn.execute(
            """
            SELECT thread_id, parent_thread_id, depth, window_ratio, status,
                   chronicle_prompt, chronicle_summary, created_at, completed_at, label
            FROM stelis_threads WHERE status = 'active'
            ORDER BY created_at ASC
            """
        )
    return [_row_to_stelis_thread(row) for row in cur.fetchall()]


def complete_stelis_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    status: str = "completed",
    chronicle_summary: Optional[str] = None,
) -> bool:
    """Mark a Stelis thread as completed or aborted."""
    if status not in ("completed", "aborted"):
        status = "completed"

    completed_at = int(time.time())
    cur = conn.execute(
        """
        UPDATE stelis_threads
        SET status = ?, chronicle_summary = ?, completed_at = ?
        WHERE thread_id = ?
        """,
        (status, chronicle_summary, completed_at, thread_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_stelis_ancestor_chain(conn: sqlite3.Connection, thread_id: str) -> List[StelisThread]:
    """Get the chain of ancestors from root to the given thread (inclusive)."""
    chain: List[StelisThread] = []
    current_id: Optional[str] = thread_id

    while current_id:
        stelis = get_stelis_thread(conn, current_id)
        if not stelis:
            break
        chain.append(stelis)
        current_id = stelis.parent_thread_id

    return chain[::-1]  # Reverse to get root-first order


def calculate_stelis_window_tokens(
    conn: sqlite3.Connection,
    thread_id: str,
    model_context_length: int,
) -> int:
    """Calculate the available window tokens for a Stelis thread."""
    chain = get_stelis_ancestor_chain(conn, thread_id)
    if not chain:
        # Not a Stelis thread, use full context
        return model_context_length

    window_size = model_context_length
    for stelis in chain:
        window_size = int(window_size * stelis.window_ratio)

    return window_size


def delete_stelis_thread(conn: sqlite3.Connection, thread_id: str, cascade: bool = False) -> bool:
    """Delete a Stelis thread record (does not delete messages)."""
    try:
        if cascade:
            # Delete all descendants first
            children = get_stelis_children(conn, thread_id)
            for child in children:
                delete_stelis_thread(conn, child.thread_id, cascade=True)

        conn.execute("DELETE FROM stelis_threads WHERE thread_id = ?", (thread_id,))
        conn.commit()
        return True
    except Exception as e:
        debug(f"Error deleting stelis thread {thread_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Embed metadata helpers
# ---------------------------------------------------------------------------

def get_embed_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the value for *key* from the embed_metadata table, or None."""
    try:
        row = conn.execute(
            "SELECT value FROM embed_metadata WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        # Table may not exist yet in very old databases
        return None


def set_embed_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert *key*/*value* into the embed_metadata table."""
    now = datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO embed_metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Pulse Logs
# ---------------------------------------------------------------------------

def list_pulse_ids(
    conn: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
) -> List[Tuple[Any, ...]]:
    """Fetch distinct pulse_ids with metadata, ordered by latest created_at DESC.

    Returns:
        List of tuples: (pulse_id, entry_count, latest_created_at, first_playbook_name)
    """
    cur = conn.execute(
        """
        SELECT pulse_id,
               COUNT(*) AS entry_count,
               MAX(created_at) AS latest_created_at,
               (SELECT playbook_name FROM pulse_logs p2
                WHERE p2.pulse_id = p1.pulse_id AND p2.playbook_name IS NOT NULL
                ORDER BY p2.created_at ASC, p2.rowid ASC LIMIT 1) AS first_playbook_name
        FROM pulse_logs p1
        GROUP BY pulse_id
        ORDER BY latest_created_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    return cur.fetchall()


def count_pulse_ids(conn: sqlite3.Connection) -> int:
    """Count distinct pulse_ids in pulse_logs."""
    cur = conn.execute("SELECT COUNT(DISTINCT pulse_id) FROM pulse_logs")
    row = cur.fetchone()
    return row[0] if row else 0


def add_pulse_log(
    conn: sqlite3.Connection,
    pulse_id: str,
    thread_id: Optional[str],
    role: str,
    content: Optional[str],
    node_id: Optional[str] = None,
    playbook_name: Optional[str] = None,
    important: bool = False,
    tool_calls: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    created_at: Optional[int] = None,
) -> str:
    """Insert a single pulse log entry and return its ID."""
    log_id = str(uuid.uuid4())
    ts = int(time.time()) if created_at is None else int(created_at)
    conn.execute(
        """
        INSERT INTO pulse_logs
        (id, pulse_id, thread_id, role, content, node_id, playbook_name,
         important, tool_calls, tool_call_id, tool_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (log_id, pulse_id, thread_id, role, content, node_id, playbook_name,
         1 if important else 0, tool_calls, tool_call_id, tool_name, ts),
    )
    conn.commit()
    return log_id


def get_pulse_logs_by_pulse(conn: sqlite3.Connection, pulse_id: str) -> List[Tuple[Any, ...]]:
    """Fetch all pulse logs for a given pulse_id, ordered by created_at."""
    cur = conn.execute(
        """
        SELECT id, pulse_id, thread_id, role, content, node_id, playbook_name,
               important, tool_calls, tool_call_id, tool_name, created_at
        FROM pulse_logs
        WHERE pulse_id = ?
        ORDER BY created_at ASC, rowid ASC
        """,
        (pulse_id,),
    )
    return cur.fetchall()


def delete_pulse_logs_before(conn: sqlite3.Connection, before_timestamp: int) -> int:
    """Delete pulse logs older than the given timestamp. Returns number of deleted rows."""
    cur = conn.execute(
        "DELETE FROM pulse_logs WHERE created_at < ?",
        (before_timestamp,),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Memory Notes
# ---------------------------------------------------------------------------

@dataclass
class MemoryNote:
    id: str
    thread_id: str
    content: str
    source_pulse_id: Optional[str]
    source_time: Optional[int]
    resolved: bool
    created_at: int
    # Organization metadata (set by memory_organize_plan)
    group_label: Optional[str] = None
    action: Optional[str] = None  # append_to_existing / create_child / create_new
    target_page_id: Optional[str] = None
    suggested_title: Optional[str] = None
    target_category: Optional[str] = None


def add_memory_notes(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    notes: List[str],
    source_pulse_id: Optional[str] = None,
    source_time: Optional[int] = None,
) -> List[MemoryNote]:
    """Add multiple memory note items at once.

    Args:
        thread_id: Active thread at the time of extraction.
        notes: List of short text items (one knowledge point each).
        source_pulse_id: Pulse that these notes were extracted from.
        source_time: Representative timestamp of the source messages.

    Returns:
        List of created MemoryNote objects.
    """
    now = int(time.time())
    result: List[MemoryNote] = []
    for text in notes:
        text = text.strip()
        if not text:
            continue
        note_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO memory_notes (id, thread_id, content, source_pulse_id, source_time, resolved, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (note_id, thread_id, text, source_pulse_id, source_time, now),
        )
        result.append(MemoryNote(
            id=note_id,
            thread_id=thread_id,
            content=text,
            source_pulse_id=source_pulse_id,
            source_time=source_time,
            resolved=False,
            created_at=now,
        ))
    conn.commit()
    return result


_NOTE_SELECT_COLS = (
    "id, thread_id, content, source_pulse_id, source_time, resolved, "
    "created_at, group_label, action, target_page_id, suggested_title, target_category"
)


def _row_to_note(row) -> MemoryNote:
    return MemoryNote(
        id=row[0],
        thread_id=row[1],
        content=row[2],
        source_pulse_id=row[3],
        source_time=row[4],
        resolved=bool(row[5]),
        created_at=row[6],
        group_label=row[7],
        action=row[8],
        target_page_id=row[9],
        suggested_title=row[10],
        target_category=row[11],
    )


def get_unresolved_notes(
    conn: sqlite3.Connection,
    *,
    thread_id: Optional[str] = None,
    limit: int = 100,
) -> List[MemoryNote]:
    """Get unresolved memory notes, optionally filtered by thread."""
    if thread_id:
        cur = conn.execute(
            f"SELECT {_NOTE_SELECT_COLS} FROM memory_notes "
            "WHERE resolved = 0 AND thread_id = ? ORDER BY created_at ASC LIMIT ?",
            (thread_id, limit),
        )
    else:
        cur = conn.execute(
            f"SELECT {_NOTE_SELECT_COLS} FROM memory_notes "
            "WHERE resolved = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
    return [_row_to_note(row) for row in cur.fetchall()]


def get_unplanned_notes(conn: sqlite3.Connection, *, limit: int = 200) -> List[MemoryNote]:
    """Get unresolved notes that have no organization metadata yet (group_label IS NULL)."""
    cur = conn.execute(
        f"SELECT {_NOTE_SELECT_COLS} FROM memory_notes "
        "WHERE resolved = 0 AND group_label IS NULL ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    return [_row_to_note(row) for row in cur.fetchall()]


def get_planned_notes_by_group(conn: sqlite3.Connection, group_label: str) -> List[MemoryNote]:
    """Get unresolved notes for a specific group (ready for execution)."""
    cur = conn.execute(
        f"SELECT {_NOTE_SELECT_COLS} FROM memory_notes "
        "WHERE resolved = 0 AND group_label = ? ORDER BY created_at ASC",
        (group_label,),
    )
    return [_row_to_note(row) for row in cur.fetchall()]


def get_planned_group_labels(conn: sqlite3.Connection) -> List[str]:
    """Get distinct group labels that have unresolved planned notes."""
    cur = conn.execute(
        "SELECT group_label FROM memory_notes "
        "WHERE resolved = 0 AND group_label IS NOT NULL "
        "GROUP BY group_label ORDER BY MIN(created_at)",
    )
    return [row[0] for row in cur.fetchall()]


def set_note_plan(
    conn: sqlite3.Connection,
    note_ids: List[str],
    *,
    group_label: str,
    action: str,
    target_page_id: Optional[str] = None,
    suggested_title: Optional[str] = None,
    target_category: Optional[str] = None,
) -> int:
    """Set organization metadata on notes (called by memory_organize_plan).

    Args:
        note_ids: IDs of notes to update.
        group_label: Group name for this batch of notes.
        action: One of 'append_to_existing', 'create_child', 'create_new'.
        target_page_id: Target page for append/child actions.
        suggested_title: Suggested title for new page creation.
        target_category: Category for new page creation.

    Returns:
        Number of updated rows.
    """
    if not note_ids:
        return 0
    placeholders = ",".join("?" for _ in note_ids)
    cur = conn.execute(
        f"UPDATE memory_notes SET group_label = ?, action = ?, target_page_id = ?, "
        f"suggested_title = ?, target_category = ? "
        f"WHERE id IN ({placeholders})",
        [group_label, action, target_page_id, suggested_title, target_category] + note_ids,
    )
    conn.commit()
    return cur.rowcount


def clear_note_plan(conn: sqlite3.Connection, note_ids: List[str]) -> int:
    """Clear organization metadata from notes (revert to unplanned).

    Used when exec phase determines a note doesn't belong in its assigned group.
    """
    if not note_ids:
        return 0
    placeholders = ",".join("?" for _ in note_ids)
    cur = conn.execute(
        f"UPDATE memory_notes SET group_label = NULL, action = NULL, "
        f"target_page_id = NULL, suggested_title = NULL, target_category = NULL "
        f"WHERE id IN ({placeholders})",
        note_ids,
    )
    conn.commit()
    return cur.rowcount


def resolve_memory_notes(conn: sqlite3.Connection, note_ids: List[str]) -> int:
    """Mark memory notes as resolved. Returns number of updated rows."""
    if not note_ids:
        return 0
    placeholders = ",".join("?" for _ in note_ids)
    cur = conn.execute(
        f"UPDATE memory_notes SET resolved = 1 WHERE id IN ({placeholders})",
        note_ids,
    )
    conn.commit()
    return cur.rowcount


def delete_resolved_notes_before(conn: sqlite3.Connection, before_timestamp: int) -> int:
    """Delete resolved notes older than the given timestamp."""
    cur = conn.execute(
        "DELETE FROM memory_notes WHERE resolved = 1 AND created_at < ?",
        (before_timestamp,),
    )
    conn.commit()
    return cur.rowcount


def count_unresolved_notes(conn: sqlite3.Connection) -> int:
    """Count unresolved memory notes."""
    cur = conn.execute("SELECT COUNT(*) FROM memory_notes WHERE resolved = 0")
    return cur.fetchone()[0]


def count_unplanned_notes(conn: sqlite3.Connection) -> int:
    """Count unresolved notes without organization metadata."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM memory_notes WHERE resolved = 0 AND group_label IS NULL"
    )
    return cur.fetchone()[0]


def count_planned_groups(conn: sqlite3.Connection) -> int:
    """Count distinct groups of planned (but unresolved) notes."""
    cur = conn.execute(
        "SELECT COUNT(DISTINCT group_label) FROM memory_notes "
        "WHERE resolved = 0 AND group_label IS NOT NULL"
    )
    return cur.fetchone()[0]


# 機構名義の行の印 (handy_tool = ツール実行結果 / spell = スペル結果 /
# event_message = システム通知)。
# - Chronicle 編纂では**除外しない** (2026-08-29 まはー裁定): 本人の提示に
#   立った行はすべて編纂の材料に入る。長い機構名義の行は材料を組む時だけ
#   決定論の一行に縮む (sai_memory/arasuji/generator.py の長さ規則)。DB の
#   行 (保存) は全文のまま。
# - 実会話フィルタ (_conversation_exclusion: scene 切り出し・会話検索・想起)
#   では従来どおり除外する — 口調のアンカーに機構の定型文を混ぜないため。
MECHANISM_TAGS = ('handy_tool', 'spell', 'event_message')

# line_role values excluded from General Chronicle.
# sub_line = lightweight model step execution (work logs)
# meta_judgment = meta-layer decisions
# nested = child line intermediate state (report_to_parent covers the parent)
CHRONICLE_EXCLUDED_LINE_ROLES = ('sub_line', 'meta_judgment', 'nested')


def chronicle_eligibility_filter() -> Tuple[str, Tuple[str, ...]]:
    """Chronicle 編纂対象の共有 SQL 断片 ``(clause, params)`` を返す。

    「どのメッセージが Chronicle に入るか」の除外規則の一点管理:
    - Stelis スレッド (サブエージェント作業ログ) の除外
    - ``CHRONICLE_EXCLUDED_LINE_ROLES`` の除外 (NULL line_role は旧世代
      メッセージなので許可)
    - 非 committed scope の除外 (NULL scope は旧世代メッセージの救済)

    scope の条件は提示と同じ規則 (required_scopes=['committed'] + NULL 救済)
    — 提示に立たない行は材料にならない。scope='discardable' はスルースの
    適用ゼロ記録やメタ判断の破棄分、scope='volatile' は作業セッションの
    生ログで、どちらも本人の提示コンテキストに乗らないため、2026-08-29 裁定
    「提示に立った行はすべて材料」の対象外。

    タグでは除外しない (2026-08-29 まはー裁定): 機構名義の行
    (``MECHANISM_TAGS`` = handy_tool / spell / event_message) も、本人の
    提示に立った行として編纂の材料と被覆 (source_ids) に入る。長い機構名義の
    行は材料を組む時だけ決定論の一行に縮む (sai_memory/arasuji/generator.py)。
    'session_digest' (作業ダイジェスト行) も同様に材料に入る — 作業セッション
    の体験が長期記憶へ入る唯一の道は「ダイジェスト行が提示の並びに立ち、他の
    メッセージと同じに畳まれる」こと (arasuji_levels.md §4-2 入口は一本)。

    ``get_messages_for_chronicle`` (編纂の入力) と
    ``filter_chronicle_eligible_ids`` (退場適用側の編纂可能性判定) が共用する。
    """
    role_placeholders = ",".join("?" for _ in CHRONICLE_EXCLUDED_LINE_ROLES)
    clause = f"""
        thread_id NOT IN (
            SELECT thread_id FROM stelis_threads
        )
        AND (line_role IS NULL OR line_role NOT IN ({role_placeholders}))
        AND (scope IS NULL OR scope = 'committed')
    """
    return clause, CHRONICLE_EXCLUDED_LINE_ROLES


def get_messages_for_chronicle(
    conn: sqlite3.Connection,
    *,
    limit: int = 0,
    offset: int = 0,
) -> List[Message]:
    """Fetch messages suitable for Chronicle generation.

    Applies standard exclusion filters:
    - Stelis threads (sub-agent work logs)
    - Messages with line_role sub_line / meta_judgment / nested
      (NULL line_role is allowed for pre-cognition-model messages)
    - Messages with non-committed scope (discardable / volatile —
      NULL scope is allowed for pre-v0.11 rows)

    機構名義の行 (handy_tool / spell / event_message タグ) は除外しない
    (2026-08-29 まはー裁定 — 詳細は ``chronicle_eligibility_filter``)。

    This is the single source of truth for "which messages go into Chronicle."
    Both runtime.py (Metabolism) and build_arasuji_core.py (CLI/UI) should
    use this function instead of writing their own queries.

    Args:
        conn: SAIMemory database connection.
        limit: Max messages to return (0 = unlimited).
        offset: Number of messages to skip from the beginning.
    """
    limit_clause = f"LIMIT {int(limit)} OFFSET {int(offset)}" if limit > 0 else ""
    clause, params = chronicle_eligibility_filter()

    cur = conn.execute(
        f"""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata,
               {_LINE_METADATA_COLUMNS}
        FROM messages
        WHERE {clause}
        ORDER BY created_at ASC, rowid ASC
        {limit_clause}
        """,
        params,
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def filter_chronicle_eligible_ids(
    conn: sqlite3.Connection, message_ids: Iterable[str],
) -> set:
    """``message_ids`` のうち Chronicle 編纂の対象になれるものの id 集合。

    除外規則は ``chronicle_eligibility_filter`` (= ``get_messages_for_chronicle``
    と同一の WHERE 句) を共用する — 編纂側と判定側で規則の二枚目を作らない。

    退場の適用側 (sea/session_lifecycle.py の ``_apply_eviction_plan``) が
    「この fold にあらすじが生まれる可能性はあるか」を判定するために使う。
    空集合 = fold 全体が編纂対象外で、あらすじは永久に生まれない。
    """
    ids = [str(m) for m in message_ids]
    if not ids:
        return set()
    clause, params = chronicle_eligibility_filter()
    out: set = set()
    chunk_size = 500
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        id_placeholders = ",".join("?" for _ in chunk)
        cur = conn.execute(
            f"SELECT id FROM messages WHERE id IN ({id_placeholders}) AND {clause}",
            tuple(chunk) + params,
        )
        out.update(str(row[0]) for row in cur.fetchall())
    return out


def get_message_position(
    conn: sqlite3.Connection, message_id: str,
) -> Optional[Tuple[Optional[int], int]]:
    """メッセージの正典順序キーの素材 ``(created_at, rowid)`` (W8 S7)。無ければ None。

    created_at は NULL のまま返す — 意味論は :func:`get_messages_before_id` と
    同じで、「NULL は全ての実時刻より前・NULL 同士は rowid 順」。SQL の比較は
    ``_canonical_before_clause`` / ``_canonical_after_clause``、Python 側の
    比較は ``canonical_position_key`` を使う (0 への写像は ts=0 の実行と
    区別できず順序が壊れる)。
    """
    cur = conn.execute(
        "SELECT created_at, rowid FROM messages WHERE id = ?",
        (str(message_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return (int(row[0]) if row[0] is not None else None, int(row[1]))


def canonical_position_key(
    created_at: Optional[int], rowid: int,
) -> Tuple[Tuple[int, int], int]:
    """``(created_at, rowid)`` を Python 側で比較可能な正典キーに写す公開口。

    実体は ``_canonical_key_of_row`` (NULL = 先頭側)。SQL 側の
    ``_canonical_before_clause`` / ``_canonical_after_clause`` と同じ順序を
    Python 比較で再現する消費者 (被覆補修の止め線など) 用。
    """
    return _canonical_key_of_row(created_at, rowid)


def clip_messages_before_position(
    conn: sqlite3.Connection,
    messages: List[Message],
    created_at: Optional[int],
    rowid: int,
) -> List[Message]:
    """正典順の messages 列を、``(created_at, rowid)`` より厳密に古い先頭部分に切り詰める。

    被覆補修の止め線 (arasuji_levels.md §16-2): 全量の編纂計画は温かい提示窓の
    下を掘らない。見積もり (sai_memory/arasuji/estimate.py) と生成
    (sea/session_lifecycle.generate_chronicle) が**この同じ関数**で絞る —
    表示と実走が違う数を言ってはならない。

    「前」の判定は共有述語 ``_canonical_before_clause`` (NULL created_at は
    全ての実時刻より前) — ``get_messages_before_id`` と同じ意味論。

    切り位置がスペルの群 (``spell_group_spans``) の内側に落ちる場合は、群の
    先頭まで下げる — 群の途中で編纂対象を切ると「唱え」だけが要約され、
    「結果」が出自を失う (§4-3 チャンクの切れ目は群を割らない、の同族)。

    Args:
        messages: 正典順 (created_at ASC, rowid ASC) のメッセージ列。
        created_at / rowid: 上端の正典順序キーの素材 (created_at は NULL 可)。
    """
    if not messages:
        return []
    before_sql, before_params = _canonical_before_clause(
        created_at, int(rowid), inclusive=False,
    )
    cur = conn.execute(
        f"SELECT id FROM messages WHERE {before_sql}", before_params,
    )
    allowed = {str(row[0]) for row in cur.fetchall()}
    boundary = len(messages)
    for index, msg in enumerate(messages):
        if str(msg.id) not in allowed:
            boundary = index
            break
    if boundary >= len(messages):
        return list(messages)
    if boundary > 0:
        for lo, hi in spell_group_spans(
            [(m.id, getattr(m, "spell_origin_id", None)) for m in messages]
        ):
            if lo < boundary <= hi:
                boundary = lo
                break
    return list(messages[:boundary])


def _conversation_exclusion() -> Tuple[str, Tuple[str, ...]]:
    """「実会話」フィルタの共有 SQL 断片 ``(clause, params)`` を返す。

    role: 会話本文 (user 発話 / persona 応答) のみ対象。persona 応答は歴史的に
    'model' (Gemini 系呼称) と 'assistant' (OpenAI 系呼称・インポートログ) の
    両方が実データに存在する (実 DB 実査で確認、2026-07-04)。'system'/'host' は
    会話本文ではないため除外する。
    content が '<system>' で始まる行は、role=user で保存されるシステム通知
    (Track切替通知等、event_message タグを持たない世代がある) なので除外する。
    口調のアンカー (scene) にシステム通知が混入すると手本として劣化するため。

    ``get_conversation_window_around`` (コア記憶 scene) と
    ``get_conversation_messages_between`` (範囲クリップの読み出し) が共用する
    (同じ「実会話」定義を一点管理)。

    機構名義の行 (``MECHANISM_TAGS``) はここでは**除外し続ける** — Chronicle
    編纂 (``chronicle_eligibility_filter``) が 2026-08-29 裁定でタグ除外を
    やめたのとは独立の境界で、口調のアンカー (scene) に機構の定型文を
    混ぜない目的は変わらないため。
    """
    tag_placeholders = ",".join("?" for _ in MECHANISM_TAGS)
    role_placeholders = ",".join("?" for _ in CHRONICLE_EXCLUDED_LINE_ROLES)
    clause = f"""
        thread_id NOT IN (SELECT thread_id FROM stelis_threads)
        AND role IN ('user', 'model', 'assistant')
        AND NOT EXISTS (
            SELECT 1 FROM json_each(metadata, '$.tags')
            WHERE json_each.value IN ({tag_placeholders})
        )
        AND (line_role IS NULL OR line_role NOT IN ({role_placeholders}))
        AND content NOT LIKE '<system>%'
    """
    return clause, MECHANISM_TAGS + CHRONICLE_EXCLUDED_LINE_ROLES


def real_conversation_filter() -> Tuple[str, Tuple[str, ...]]:
    """message 検索 (unified_recall / 自動想起) 用の「実会話」フィルタ共有 SQL 断片。

    2026-07-12 監査 P1: 自動想起の message 経路が line/scope/tag 境界を無視して
    サブラインの試行錯誤・破棄されたメタ判断・システム通知を「本人の過去の会話」
    としてメインラインへ再注入していた。想起の message ソースは「実会話のみ」。

    定義は一点管理の合成:
    - ``_conversation_exclusion()`` — SCENE / 範囲クリップと同じ実会話定義
      (Stelis スレッド除外・handy_tool/spell/event_message タグ除外・
      sub_line/meta_judgment/nested line_role 除外・role は user/model/assistant
      のみ・'<system>' 始まり本文の除外)
    - 通常履歴 (``get_messages_last``) と同じ ``scope='discardable'`` 除外

    列は messages の非修飾列名で参照する。JOIN 先で使う場合は、相手側に
    thread_id/role/content/metadata/line_role/scope と同名の列が無いこと
    (message_embeddings は message_id/chunk_index/vector のみなので安全)。

    Returns:
        ``(clause, params)`` — WHERE 句に AND で埋め込める断片とバインド値。
    """
    clause, params = _conversation_exclusion()
    clause = f"({clause}) AND (scope IS NULL OR scope != 'discardable')"
    return clause, params


def get_conversation_messages_between(
    conn: sqlite3.Connection,
    start_message_id: str,
    end_message_id: str,
) -> Optional[List[Message]]:
    """範囲クリップが指す [start, end] 区間 (両端含む) の実会話メッセージを返す。

    Memory Atlas の ``memory_read p:N`` (範囲クリップの全文読み出し) が使う。区間の
    中身には ``get_conversation_window_around`` と同一の「実会話」フィルタを適用
    する — SCENE 由来の範囲クリップは clip 時と同じ窓が再現され、区間内に後から
    混ざったツール実行ログ等は範囲クリップの読み出しにも混入しない。

    thread 境界 (2026-07-12 監査 P1): 区間の中身は両端と同一 thread の行に限定
    する。rowid は thread を跨いで単調なので、範囲条件だけだと交互挿入された
    別 thread の会話が「過去の実際のやり取り」として混入していた。両端が異なる
    thread を指す場合、その範囲は文脈として不正なので ``None`` を返す (呼び出し
    元 ``saiverse/memory_atlas.py`` は None を「区間は現在読み出せません」表示に
    落とす — 端メッセージ欠落と同じ扱い)。

    どちらかの端メッセージが存在しなければ ``None``。端の順序は正典順序キー
    ``(created_at, rowid)`` で正規化する (逆順で渡されても動く)。戻り値は
    時系列 (正典順昇順)。W8 / SEA 監査 S7: 区間判定を rowid 単独から正典順に
    揃えた — 過去時刻で後から挿入された行 (インポート・移植) が区間の内外で
    履歴表示と食い違わないように。
    """
    start_row = conn.execute(
        "SELECT created_at, rowid, thread_id FROM messages WHERE id=?",
        (start_message_id,),
    ).fetchone()
    end_row = conn.execute(
        "SELECT created_at, rowid, thread_id FROM messages WHERE id=?",
        (end_message_id,),
    ).fetchone()
    if not start_row or not end_row:
        return None
    if start_row[2] != end_row[2]:
        debug(
            "get_conversation_messages_between: cross-thread endpoints rejected "
            f"({start_row[2]!r} != {end_row[2]!r})"
        )
        return None
    thread_id = start_row[2]
    lo, hi = sorted(
        (
            _canonical_key_of_row(start_row[0], start_row[1]),
            _canonical_key_of_row(end_row[0], end_row[1]),
        )
    )
    lo_ts = lo[0][1] if lo[0][0] == 1 else None
    hi_ts = hi[0][1] if hi[0][0] == 1 else None
    ge_sql, ge_params = _canonical_after_clause(lo_ts, lo[1], inclusive=True)
    le_sql, le_params = _canonical_before_clause(hi_ts, hi[1], inclusive=True)

    exclusion_clause, exclusion_params = _conversation_exclusion()
    cur = conn.execute(
        f"""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        WHERE {ge_sql} AND {le_sql}
          AND thread_id = ? AND {exclusion_clause}
        ORDER BY created_at ASC, rowid ASC
        """,
        (*ge_params, *le_params, thread_id, *exclusion_params),
    )
    return [_row_to_message(r) for r in cur.fetchall()]


def get_conversation_window_around(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    rounds: int = 3,
) -> Optional[List[Message]]:
    """アンカーメッセージを中心とした「実会話」の窓を切り出す (コア記憶 scene 用)。

    「実会話」の定義は ``_conversation_exclusion``: Stelis サブエージェントの
    スレッド除外・handy_tool/spell/event_message (機構名義) タグ除外・
    sub_line/meta_judgment/nested line_role 除外。加えてここでは role を
    user/model (表示名 assistant) に限定する (ツール応答や内部ロールを
    切り抜きに混ぜない)。Chronicle 編纂のフィルタとは 2026-08-29 裁定で
    分岐した — 編纂は機構名義の行も材料に入れるが、scene は入れない。

    窓の大きさは前後 ``rounds`` 往復 ≒ ``rounds * 2`` 件ずつ (アンカー自身は含めた
    上で前後に最大 rounds*2 件)。会話の端では自然に短くなる (それ以上前/後が
    存在しないだけで、エラーにはしない)。

    thread 境界 (2026-07-12 監査 P1): 窓はアンカーと同一 thread の行だけで作る。
    rowid は thread を跨いで単調なので、rowid 前後だけだと交互挿入された別 thread
    の会話が SCENE (人格の口調・関係性アンカー) に混入していた。

    アンカーが見つからない、またはアンカー自体が除外対象 (ツール実行ログ等) の
    場合は ``None`` を返す。

    戻り値は正典順 ``(created_at, rowid)`` 昇順 (時系列) のメッセージ一覧。
    W8 / SEA 監査 S7: 窓の前後判定を rowid 単独から正典順に揃えた。
    """
    anchor_row = conn.execute(
        "SELECT created_at, rowid, thread_id FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    if not anchor_row:
        return None
    anchor_ts = int(anchor_row[0]) if anchor_row[0] is not None else None
    anchor_rowid = int(anchor_row[1])
    anchor_thread_id = anchor_row[2]

    exclusion_clause, exclusion_params = _conversation_exclusion()

    # アンカー自身が会話フィルタを通るか確認する。
    anchor_ok = conn.execute(
        f"SELECT 1 FROM messages WHERE rowid=? AND {exclusion_clause}",
        (anchor_rowid, *exclusion_params),
    ).fetchone()
    if not anchor_ok:
        return None

    window = max(0, int(rounds)) * 2

    before_rows: List[Message] = []
    if window > 0:
        before_sql, before_params = _canonical_before_clause(
            anchor_ts, anchor_rowid, inclusive=False
        )
        cur = conn.execute(
            f"""
            SELECT id, thread_id, role, content, resource_id, created_at, metadata
            FROM messages
            WHERE {before_sql}
              AND thread_id = ? AND {exclusion_clause}
            ORDER BY created_at DESC, rowid DESC LIMIT ?
            """,
            (*before_params, anchor_thread_id, *exclusion_params, window),
        )
        before_rows = [_row_to_message(r) for r in cur.fetchall()][::-1]

    anchor_full = conn.execute(
        "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
        "FROM messages WHERE rowid=?",
        (anchor_rowid,),
    ).fetchone()
    anchor_msg = _row_to_message(anchor_full)

    after_rows: List[Message] = []
    if window > 0:
        after_sql, after_params = _canonical_after_clause(
            anchor_ts, anchor_rowid, inclusive=False
        )
        cur = conn.execute(
            f"""
            SELECT id, thread_id, role, content, resource_id, created_at, metadata
            FROM messages
            WHERE {after_sql}
              AND thread_id = ? AND {exclusion_clause}
            ORDER BY created_at ASC, rowid ASC LIMIT ?
            """,
            (*after_params, anchor_thread_id, *exclusion_params, window),
        )
        after_rows = [_row_to_message(r) for r in cur.fetchall()]

    return before_rows + [anchor_msg] + after_rows


def search_conversation_messages(
    conn: sqlite3.Connection,
    keywords: List[str],
    *,
    date_from_ts: Optional[int] = None,
    date_to_ts: Optional[int] = None,
    limit: int = 20,
) -> List[Message]:
    """会話本文を対象にキーワード AND の LIKE 検索を行う (コア記憶 scene の探索 UI 用)。

    「実会話」の定義は ``get_conversation_window_around`` と同一の除外条件を流用する
    (Stelis スレッド除外・handy_tool/spell/event_message タグ除外・sub_line/
    meta_judgment/nested line_role 除外・role は user/model/assistant のみ)。scene は
    この検索結果からアンカーを選んで刻むため、検索対象と切り抜き対象が一致している
    ことが重要 (検索でヒットしたのに切り抜けない、という齟齬を防ぐ)。

    - ``keywords``: 空要素は無視し、全キーワードを含む (AND) メッセージのみ返す。
      空リストの場合は期間フィルタのみ適用する。
    - ``date_from_ts`` / ``date_to_ts``: created_at の範囲 (Unix 秒、両端含む)。
    - 戻り値は created_at 降順 (新しい順)、最大 ``limit`` 件。
    """
    tag_placeholders = ",".join("?" for _ in MECHANISM_TAGS)
    role_placeholders = ",".join("?" for _ in CHRONICLE_EXCLUDED_LINE_ROLES)
    exclusion_clause = f"""
        thread_id NOT IN (SELECT thread_id FROM stelis_threads)
        AND role IN ('user', 'model', 'assistant')
        AND NOT EXISTS (
            SELECT 1 FROM json_each(metadata, '$.tags')
            WHERE json_each.value IN ({tag_placeholders})
        )
        AND (line_role IS NULL OR line_role NOT IN ({role_placeholders}))
        AND content NOT LIKE '<system>%'
    """
    params: List[Any] = list(MECHANISM_TAGS) + list(CHRONICLE_EXCLUDED_LINE_ROLES)

    where_parts = [exclusion_clause]
    for kw in keywords:
        kw_stripped = kw.strip()
        if not kw_stripped:
            continue
        where_parts.append("content LIKE ? ESCAPE '\\'")
        # LIKE のワイルドカード (% _ \) をエスケープしてリテラル一致にする。
        escaped = (
            kw_stripped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        params.append(f"%{escaped}%")

    if date_from_ts is not None:
        where_parts.append("created_at >= ?")
        params.append(int(date_from_ts))
    if date_to_ts is not None:
        where_parts.append("created_at <= ?")
        params.append(int(date_to_ts))

    where_clause = " AND ".join(f"({p})" for p in where_parts)
    lim = max(1, int(limit))
    cur = conn.execute(
        f"""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        WHERE {where_clause}
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        (*params, lim),
    )
    return [_row_to_message(row) for row in cur.fetchall()]
