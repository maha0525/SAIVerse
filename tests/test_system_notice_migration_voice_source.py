"""規則 5 (音声入力の案内文) の出どころの条件 (database/system_notice_migration.py)。

docs/intent/system_notice_delivery.md「追補」の規則 5。合成した一時 DB だけを使う。
固定すること:

- 記憶の行の元の行 (本体 DB の ``building_messages`` の行) を引き、その行が画面からの
  送信の印 ``client_message_id`` を持てば、案内文の形でも変換せず確認待ちにする
  (:data:`SENT_FROM_SCREEN_REASON`)。持たなければ (音声入力を中継するアドオンの
  発言) 従来どおり二行に分ける。
- 元の行は ``building_msg_ref`` で引く。``building_msg_ref`` を持たない行 (転記が印を
  刻む前の行) は、音声の ``media`` の ``uri`` が同じ ``role='user'`` の行で引く。
- 元の行を一つに決められない (見つからない・複数ある)・本体 DB を読めない・本体 DB が
  無い場合は、変換せず確認待ちにする (:data:`VOICE_SOURCE_UNKNOWN_REASON`)。
- 境 (切り替わりの時刻) より前の行でも同じ。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database.system_notice_migration import (
    CUTOVER_FILE_NAME,
    OUTPUT_DIR_NAME,
    SENT_FROM_SCREEN_REASON,
    SPLIT_ADDED_TABLE,
    VOICE_INPUT_NOTICE_KIND,
    VOICE_SOURCE_UNKNOWN_REASON,
    BuildingSourceRows,
    migrate_persona_db,
    run_migration,
)
from sai_memory.memory.storage import SYSTEM_NOTICE_KEY, SYSTEM_NOTICE_REVIEW_KEY, init_db

STAMP = "20261029_000000"
CUTOVER = datetime(2026, 10, 28, 5, 0, 0, tzinfo=timezone.utc)
PRE = int((CUTOVER - timedelta(days=30)).timestamp())

_VOICE_TEXT = "ｽﾀｯｸﾁｬﾝから音声入力を受信しました。添付の音声を聴いて応答してください。"
_VOICE_CONTENT = f"<system>{_VOICE_TEXT}</system>"


def _audio(name: str):
    return [{"type": "audio", "uri": f"saiverse://audio/{name}", "mime_type": "audio/ogg"}]


def _voice_meta(name: str, ref=None):
    meta = {
        "tags": ["conversation"], "with": ["user"], "source": "stackchan_voice",
        "media": _audio(name),
    }
    if ref is not None:
        meta["building_msg_ref"] = ref
    return meta


def _make_main_db(path: Path, rows) -> Path:
    """本体 DB の building_messages だけを作る。rows は (building_id, seq, client_message_id, metadata)。"""
    from sqlalchemy import create_engine

    from database.models import BuildingMessage

    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    BuildingMessage.__table__.create(engine)
    engine.dispose()
    with closing(sqlite3.connect(str(path))) as conn:
        for building_id, seq, cmid, meta in rows:
            conn.execute(
                "INSERT INTO building_messages (building_id, seq, role, content, timestamp, "
                "heard_by, ingested_by, metadata_json, message_id, client_message_id) "
                "VALUES (?, ?, 'user', ?, '2026-09-01T00:00:00+00:00', '[]', '[]', ?, ?, ?)",
                (building_id, seq, _VOICE_CONTENT, json.dumps(meta, ensure_ascii=False),
                 f"{building_id}:{seq}", cmid),
            )
        conn.commit()
    return path


def _make_persona(home: Path, rows) -> Path:
    """rows は (id, metadata)。どれも案内文の本文の role=user 行で、秒をずらして並べる。"""
    db = home / "personas" / "p1" / "memory.db"
    db.parent.mkdir(parents=True)
    with closing(init_db(str(db))) as conn:
        for i, (row_id, meta) in enumerate(rows):
            conn.execute(
                "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, "
                "metadata) VALUES (?, 't', 'user', ?, 'p1', ?, ?)",
                (row_id, _VOICE_CONTENT, PRE + i * 10, json.dumps(meta, ensure_ascii=False)),
            )
        conn.commit()
    return db


def _rows(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        return {
            r[0]: (r[1], r[2], json.loads(r[3]) if r[3] else {})
            for r in conn.execute("SELECT id, role, content, metadata FROM messages").fetchall()
        }


def _record_cutover(home: Path) -> None:
    path = home / "backups" / OUTPUT_DIR_NAME / CUTOVER_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cutover": CUTOVER.isoformat()}), encoding="utf-8")


def _migrate(db: Path, tmp_path: Path, source_rows):
    return migrate_persona_db(
        db, persona_id="p1", output_dir=tmp_path / "out", stamp=STAMP,
        playbook_names=frozenset(), source_rows=source_rows, cutover=int(CUTOVER.timestamp()),
    )


def _assert_split(rows, row_id, meta):
    role, content, new_meta = rows[row_id]
    assert (role, content) == ("system", _VOICE_TEXT), row_id
    assert new_meta[SYSTEM_NOTICE_KEY] == VOICE_INPUT_NOTICE_KIND, row_id
    assert any(
        r == ("user", "", meta) for r in rows.values()
    ), row_id  # 本文の無い音声の行が、元の metadata を持って足される


def _assert_held(rows, row_id, meta, reason):
    assert rows[row_id] == ("user", _VOICE_CONTENT, {**meta, SYSTEM_NOTICE_REVIEW_KEY: reason}), row_id


# ---------------------------------------------------------------------------
# building_msg_ref で引く
# ---------------------------------------------------------------------------


def test_a_row_sent_from_the_screen_is_held_for_review_even_before_the_cutover(tmp_path):
    """画面から案内文を真似て音声つきで送った行 (元の行が client_message_id を持つ)。"""
    screen = _voice_meta("screen.ogg", ref="b1:b1:1")
    addon = _voice_meta("addon.ogg", ref="b1:b1:2")
    main = _make_main_db(tmp_path / "saiverse.db", [
        ("b1", 1, "cmid-1", screen), ("b1", 2, None, addon),
    ])
    db = _make_persona(tmp_path, [("screen", screen), ("addon", addon)])

    with BuildingSourceRows(main) as source_rows:
        result = _migrate(db, tmp_path, source_rows)

    rows = _rows(db)
    _assert_held(rows, "screen", screen, SENT_FROM_SCREEN_REASON)
    _assert_split(rows, "addon", addon)
    assert len(rows) == 3  # 足した音声の行はアドオンの一行の分だけ
    assert [(s.row_id, s.reason) for s in result.skipped] == [
        ("screen", SENT_FROM_SCREEN_REASON),
    ]
    assert result.counts() == {
        ("messages", VOICE_INPUT_NOTICE_KIND): 1,
        (SPLIT_ADDED_TABLE, VOICE_INPUT_NOTICE_KIND): 1,
    }


def test_a_row_whose_building_row_is_missing_is_held_for_review(tmp_path):
    meta = _voice_meta("gone.ogg", ref="b1:b1:9")
    main = _make_main_db(tmp_path / "saiverse.db", [])
    db = _make_persona(tmp_path, [("gone", meta)])

    with BuildingSourceRows(main) as source_rows:
        result = _migrate(db, tmp_path, source_rows)

    _assert_held(_rows(db), "gone", meta, VOICE_SOURCE_UNKNOWN_REASON)
    assert [(s.row_id, s.reason) for s in result.skipped] == [("gone", VOICE_SOURCE_UNKNOWN_REASON)]


def test_a_building_id_containing_a_colon_is_still_found(tmp_path):
    """building_msg_ref は ``<building_id>:<message_id>``。building_id に ``:`` があっても引ける。"""
    meta = _voice_meta("colon.ogg", ref="room:a:room:a:3")
    main = _make_main_db(tmp_path / "saiverse.db", [("room:a", 3, None, meta)])
    db = _make_persona(tmp_path, [("colon", meta)])

    with BuildingSourceRows(main) as source_rows:
        _migrate(db, tmp_path, source_rows)

    _assert_split(_rows(db), "colon", meta)


# ---------------------------------------------------------------------------
# 本体 DB が無い・読めない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("main_db", ["none", "missing", "not_a_database", "no_table"])
def test_rows_are_held_when_the_main_db_cannot_be_read(tmp_path, main_db):
    meta = _voice_meta("a.ogg", ref="b1:b1:1")
    db = _make_persona(tmp_path, [("voice", meta)])
    path = tmp_path / "main" / "saiverse.db"
    if main_db == "not_a_database":
        path.parent.mkdir(parents=True)
        path.write_bytes(b"this is not a database" * 100)
    elif main_db == "no_table":
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(str(path))) as conn:
            conn.execute("CREATE TABLE other (x INTEGER)")
            conn.commit()

    with BuildingSourceRows(None if main_db == "none" else path) as source_rows:
        result = _migrate(db, tmp_path, source_rows)

    _assert_held(_rows(db), "voice", meta, VOICE_SOURCE_UNKNOWN_REASON)
    assert not result.changes


def test_without_a_source_lookup_rows_are_held(tmp_path):
    """引く手段を渡さない呼び出しは、元の行を確かめられないので変換しない。"""
    meta = _voice_meta("a.ogg", ref="b1:b1:1")
    db = _make_persona(tmp_path, [("voice", meta)])

    result = _migrate(db, tmp_path, None)

    _assert_held(_rows(db), "voice", meta, VOICE_SOURCE_UNKNOWN_REASON)
    assert not result.changes


# ---------------------------------------------------------------------------
# building_msg_ref を持たない行 (転記が印を刻む前の行) は音声の uri で引く
# ---------------------------------------------------------------------------


def test_rows_without_a_ref_are_found_by_the_audio_uri(tmp_path):
    addon = _voice_meta("old_addon.ogg")
    screen = _voice_meta("old_screen.ogg")
    twice = _voice_meta("twice.ogg")
    nowhere = _voice_meta("nowhere.ogg")
    main = _make_main_db(tmp_path / "saiverse.db", [
        ("b1", 1, None, addon),
        ("b1", 2, "cmid-2", screen),
        # 同じ音声を指す行が二つ (元の行を一つに決められない)
        ("b1", 3, None, twice), ("b2", 1, None, twice),
        # uri の文字列を本文の欄以外に含むだけの行は元の行に数えない
        ("b1", 4, None, {"note": "saiverse://audio/nowhere.ogg"}),
    ])
    db = _make_persona(tmp_path, [
        ("addon", addon), ("screen", screen), ("twice", twice), ("nowhere", nowhere),
    ])

    with BuildingSourceRows(main) as source_rows:
        result = _migrate(db, tmp_path, source_rows)

    rows = _rows(db)
    _assert_split(rows, "addon", addon)
    _assert_held(rows, "screen", screen, SENT_FROM_SCREEN_REASON)
    _assert_held(rows, "twice", twice, VOICE_SOURCE_UNKNOWN_REASON)
    _assert_held(rows, "nowhere", nowhere, VOICE_SOURCE_UNKNOWN_REASON)
    assert sorted((s.row_id, s.reason) for s in result.skipped) == [
        ("nowhere", VOICE_SOURCE_UNKNOWN_REASON),
        ("screen", SENT_FROM_SCREEN_REASON),
        ("twice", VOICE_SOURCE_UNKNOWN_REASON),
    ]


# ---------------------------------------------------------------------------
# 起動の入口 (run_migration) が本体 DB から引く
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_run_migration_looks_up_the_main_db(tmp_path, dry_run):
    _record_cutover(tmp_path)
    screen = _voice_meta("screen.ogg", ref="b1:b1:1")
    addon = _voice_meta("addon.ogg", ref="b1:b1:2")
    main = _make_main_db(tmp_path / "user_data" / "database" / "saiverse.db", [
        ("b1", 1, "cmid-1", screen), ("b1", 2, None, addon),
    ])
    db = _make_persona(tmp_path, [("screen", screen), ("addon", addon)])
    before = _rows(db)

    (main_result, persona), _report = run_migration(
        db_path=main, saiverse_home=tmp_path, dry_run=dry_run,
    )

    assert not main_result.error
    assert sorted((s.row_id, s.reason) for s in persona.skipped) == [
        ("screen", SENT_FROM_SCREEN_REASON),
    ]
    assert persona.counts()[("messages", VOICE_INPUT_NOTICE_KIND)] == 1
    rows = _rows(db)
    if dry_run:
        assert rows == before
        return
    _assert_held(rows, "screen", screen, SENT_FROM_SCREEN_REASON)
    _assert_split(rows, "addon", addon)


def test_run_migration_without_a_main_db_holds_voice_rows(tmp_path):
    _record_cutover(tmp_path)
    meta = _voice_meta("a.ogg", ref="b1:b1:1")
    db = _make_persona(tmp_path, [("voice", meta)])

    (persona,), _report = run_migration(db_path=None, saiverse_home=tmp_path)

    _assert_held(_rows(db), "voice", meta, VOICE_SOURCE_UNKNOWN_REASON)
    assert [(s.row_id, s.reason) for s in persona.skipped] == [("voice", VOICE_SOURCE_UNKNOWN_REASON)]
