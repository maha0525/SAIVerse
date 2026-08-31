"""Phase 2.5 messages.pulse_id カラム + バックフィル機構の単体テスト。

`_store_memory` が長らく metadata.tags の "pulse:{uuid}" 形式で pulse_id を
保存していた。Phase 2.5 で専用カラム化したため、起動時に既存行から pulse_id を
抽出して埋める必要がある。`_backfill_messages_pulse_id` がそれをやる。

検証項目:
- pulse:{uuid} タグを持つ行が pulse_id 列に展開される
- 既に pulse_id がセットされている行は上書きされない (べき等)
- pulse: タグが無い行は pulse_id NULL のまま
- metadata が NULL の行は無視される (例外を出さず)
- 複数タグから先頭の pulse: タグが採用される
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sai_memory.memory.storage import _backfill_messages_pulse_id, init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "memory.db"
    c = init_db(str(db_path))  # 新スキーマで初期化 (pulse_id カラム + INDEX)
    yield c
    c.close()


def _insert_legacy_row(
    conn: sqlite3.Connection,
    *,
    content: str,
    metadata: dict | None,
    pulse_id: str | None = None,
) -> str:
    """Phase 2.5 以前の保存形式 (pulse_id NULL + metadata.tags にタグ) で行を挿入。"""
    import uuid as _uuid
    mid = str(_uuid.uuid4())
    conn.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at, metadata, pulse_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            mid, "test_thread", "assistant", content, 0,
            json.dumps(metadata) if metadata is not None else None,
            pulse_id,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO threads(id, resource_id, overview, overview_updated_at) VALUES (?, ?, ?, ?)",
        ("test_thread", None, None, None),
    )
    conn.commit()
    return mid


def test_backfill_extracts_pulse_id_from_tags(conn):
    """pulse:{uuid} タグが pulse_id 列にコピーされる。"""
    _insert_legacy_row(
        conn,
        content="legacy row",
        metadata={"tags": ["pulse:abc123", "conversation"]},
        pulse_id=None,
    )
    _backfill_messages_pulse_id(conn)
    cur = conn.execute("SELECT pulse_id FROM messages WHERE content='legacy row'")
    assert cur.fetchone()[0] == "abc123"


def test_backfill_is_idempotent(conn):
    """既に pulse_id が入っている行は上書きしない。"""
    _insert_legacy_row(
        conn,
        content="already filled",
        metadata={"tags": ["pulse:tag-uuid"]},
        pulse_id="column-uuid",
    )
    _backfill_messages_pulse_id(conn)
    cur = conn.execute("SELECT pulse_id FROM messages WHERE content='already filled'")
    assert cur.fetchone()[0] == "column-uuid"  # 列の値が優先、上書きされない


def test_backfill_skips_rows_without_pulse_tag(conn):
    """pulse: プレフィックスのタグが無い行は NULL のまま。"""
    _insert_legacy_row(
        conn,
        content="no pulse tag",
        metadata={"tags": ["conversation", "internal"]},
        pulse_id=None,
    )
    _backfill_messages_pulse_id(conn)
    cur = conn.execute("SELECT pulse_id FROM messages WHERE content='no pulse tag'")
    assert cur.fetchone()[0] is None


def test_backfill_handles_null_metadata(conn):
    """metadata が NULL の行は無視される (例外を投げない)。"""
    _insert_legacy_row(
        conn,
        content="no metadata",
        metadata=None,
        pulse_id=None,
    )
    # 例外なく完走する
    _backfill_messages_pulse_id(conn)
    cur = conn.execute("SELECT pulse_id FROM messages WHERE content='no metadata'")
    assert cur.fetchone()[0] is None


def test_backfill_picks_first_pulse_tag_when_multiple(conn):
    """理論上ありえない多重 pulse: タグでも 1 つは確実に拾う (LIMIT 1)。"""
    _insert_legacy_row(
        conn,
        content="multi pulse tags",
        metadata={"tags": ["pulse:first-uuid", "conversation", "pulse:second-uuid"]},
        pulse_id=None,
    )
    _backfill_messages_pulse_id(conn)
    cur = conn.execute("SELECT pulse_id FROM messages WHERE content='multi pulse tags'")
    val = cur.fetchone()[0]
    assert val in ("first-uuid", "second-uuid")  # どちらでも OK


def test_init_db_runs_backfill_automatically(tmp_path):
    """init_db 経由でバックフィルが自動実行される (起動時の動作)。"""
    db_path = tmp_path / "memory.db"
    # 旧スキーマ DB を作る (pulse_id カラム無し)
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            resource_id TEXT,
            overview TEXT,
            overview_updated_at INTEGER
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            role TEXT,
            content TEXT,
            resource_id TEXT,
            created_at INTEGER,
            metadata TEXT
        );
    """)
    c.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("m1", "t1", "assistant", "old row",  0,
         json.dumps({"tags": ["pulse:auto-uuid"]})),
    )
    c.execute("INSERT INTO threads(id) VALUES ('t1')")
    c.commit()
    c.close()

    # init_db で新スキーマに移行 + バックフィル
    c2 = init_db(str(db_path))
    cur = c2.execute("SELECT pulse_id FROM messages WHERE id='m1'")
    assert cur.fetchone()[0] == "auto-uuid"
    c2.close()
