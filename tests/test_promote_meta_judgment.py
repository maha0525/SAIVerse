"""SAIVerseManager._promote_meta_judgment_in_pulse の単体テスト。

`_store_memory` は pulse_id を `metadata.tags` の JSON 配列に `pulse:{uuid}` の
形で保存している (専用カラムではない)。プロモート SQL は同じ idiom
(`json_each(metadata, '$.tags')`) でメッセージを特定する必要がある。本テストは
書き換え後の SQL が正しく機能することを確認する。

検証項目:
- pulse_id タグ付き + line_role='meta_judgment' + scope='discardable' → 'committed' に昇格
- 別の pulse の行は影響を受けない
- line_role が違う / scope が違う行は影響を受けない
- 該当行なしのケースで例外を投げない
- pulse_id が None のケースで no-op
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from saiverse.saiverse_manager import SAIVerseManager


def _make_messages_db(db_path: Path, rows: list[dict]) -> None:
    """テスト用に最小スキーマの messages テーブルを作って rows を流し込む。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                content TEXT,
                line_role TEXT,
                scope TEXT NOT NULL DEFAULT 'committed',
                metadata TEXT
            )
            """
        )
        for r in rows:
            conn.execute(
                "INSERT INTO messages(content, line_role, scope, metadata) "
                "VALUES (?, ?, ?, ?)",
                (
                    r.get("content", ""),
                    r.get("line_role"),
                    r.get("scope", "committed"),
                    json.dumps(r["metadata"]) if r.get("metadata") else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _read_scopes(db_path: Path) -> list[tuple[str, str]]:
    """すべての行を (content, scope) タプルで返す。"""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT content, scope FROM messages ORDER BY id")
        return list(cur.fetchall())
    finally:
        conn.close()


@pytest.fixture
def manager_with_persona(tmp_path):
    """`_promote_meta_judgment_in_pulse` だけ呼べる最小限の Manager を作る。

    messages.db は tmp_path/memory.db に置き、persona_log_path はその親フォルダ
    の log.json を指す (Manager の実装は parent / 'memory.db' を見る)。
    """
    persona_log = tmp_path / "log.json"
    persona_log.touch()
    db_path = tmp_path / "memory.db"
    persona = SimpleNamespace(persona_log_path=persona_log)

    # SAIVerseManager の __init__ を回避してインスタンスだけ作る (依存が重いため)
    manager = SAIVerseManager.__new__(SAIVerseManager)
    manager.personas = {"alice": persona}
    return manager, db_path


def test_promotes_matching_pulse_tag_only(manager_with_persona):
    manager, db_path = manager_with_persona
    target_pulse = "pulse-a"
    other_pulse = "pulse-b"
    _make_messages_db(
        db_path,
        [
            # 1: 対象の判断ターン (target_pulse + meta_judgment + discardable)
            {
                "content": "judge turn (target)",
                "line_role": "meta_judgment",
                "scope": "discardable",
                "metadata": {"tags": [f"pulse:{target_pulse}", "meta_judgment"]},
            },
            # 2: 別 Pulse の判断ターン (other_pulse) — 影響なし
            {
                "content": "judge turn (other)",
                "line_role": "meta_judgment",
                "scope": "discardable",
                "metadata": {"tags": [f"pulse:{other_pulse}", "meta_judgment"]},
            },
            # 3: target_pulse の main_line — line_role 違いで対象外
            {
                "content": "main line turn",
                "line_role": "main_line",
                "scope": "committed",
                "metadata": {"tags": [f"pulse:{target_pulse}", "conversation"]},
            },
            # 4: target_pulse の判断ターンだが既に committed — 対象外
            {
                "content": "already committed",
                "line_role": "meta_judgment",
                "scope": "committed",
                "metadata": {"tags": [f"pulse:{target_pulse}"]},
            },
        ],
    )

    manager._promote_meta_judgment_in_pulse("alice", target_pulse)

    rows = _read_scopes(db_path)
    assert rows[0] == ("judge turn (target)", "committed")  # 昇格された
    assert rows[1] == ("judge turn (other)", "discardable")  # 別 pulse は維持
    assert rows[2] == ("main line turn", "committed")  # main_line は元から committed
    assert rows[3] == ("already committed", "committed")  # 元から committed のまま


def test_no_matching_rows_does_not_raise(manager_with_persona):
    """該当行ゼロでも例外を出さず、何もしないだけで終わる。"""
    manager, db_path = manager_with_persona
    _make_messages_db(
        db_path,
        [
            {
                "content": "main line",
                "line_role": "main_line",
                "scope": "committed",
                "metadata": {"tags": ["pulse:other"]},
            },
        ],
    )

    manager._promote_meta_judgment_in_pulse("alice", "pulse-not-present")
    # 何も書き換わらない
    assert _read_scopes(db_path) == [("main line", "committed")]


def test_pulse_id_none_is_noop(manager_with_persona):
    """pulse_id=None は早期 return する (テスト / CLI 経路)。"""
    manager, db_path = manager_with_persona
    _make_messages_db(
        db_path,
        [
            {
                "content": "judge",
                "line_role": "meta_judgment",
                "scope": "discardable",
                "metadata": {"tags": ["pulse:p1"]},
            },
        ],
    )
    manager._promote_meta_judgment_in_pulse("alice", None)
    # 何も書き換わらない
    assert _read_scopes(db_path) == [("judge", "discardable")]


def test_unknown_persona_is_noop(manager_with_persona):
    """ペルソナが manager に居ないときは silently skip する。"""
    manager, db_path = manager_with_persona
    _make_messages_db(
        db_path,
        [
            {
                "content": "judge",
                "line_role": "meta_judgment",
                "scope": "discardable",
                "metadata": {"tags": ["pulse:p1"]},
            },
        ],
    )
    manager._promote_meta_judgment_in_pulse("unknown_persona", "p1")
    assert _read_scopes(db_path) == [("judge", "discardable")]


def test_metadata_null_row_is_skipped(manager_with_persona):
    """metadata が NULL のレガシー行は条件で弾かれて例外も出ない。"""
    manager, db_path = manager_with_persona
    target = "p-target"
    _make_messages_db(
        db_path,
        [
            {
                "content": "no metadata row",
                "line_role": "meta_judgment",
                "scope": "discardable",
                "metadata": None,
            },
            {
                "content": "tagged target",
                "line_role": "meta_judgment",
                "scope": "discardable",
                "metadata": {"tags": [f"pulse:{target}"]},
            },
        ],
    )
    manager._promote_meta_judgment_in_pulse("alice", target)
    rows = _read_scopes(db_path)
    assert rows[0] == ("no metadata row", "discardable")
    assert rows[1] == ("tagged target", "committed")
