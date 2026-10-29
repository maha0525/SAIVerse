"""システム通知の移行が、新しい形式に切り替わった後に作られた行を自動では変換しないこと。

database/system_notice_migration.py。合成した一時 DB だけを使う。固定すること:

- 環境ごとに一つ、新しい形式に切り替わった時刻を ``<SAIVERSE_HOME>/backups/
  system_notice_migration/cutover.json`` に記録する。記録が無い起動では、記憶 DB の
  どれかに既にある完了の印の時刻のうち最も早いものを、印がどこにも無ければその
  起動の時刻を記録する。一度記録した時刻は変えない。
- その時刻より後に作られた行は、どの規則に当たっても変換せず、確認待ちにする —
  完了の印の無い記憶の全体の走査・規則の版を上げた走査し直し・完了の印を見送った
  後の走査し直しの、どれでも同じ。それより前の行は今まで通り変換する。ただし記憶の
  role=system の行は、印を足すだけで名義も本文も変えないので、後の行でも規則で判定する。
- 時刻を記録できない・記録を読めない・決める材料 (完了の印) を読めない起動では、
  自動の変換を一つもしない。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database import system_notice_migration as mig
from database.system_notice_migration import (
    AFTER_CUTOVER_REASON,
    CUTOVER_FILE_NAME,
    MIGRATION_MARKER_KEY,
    MIGRATION_RULES_VERSION,
    OUTPUT_DIR_NAME,
    UNCLASSIFIED_REASON,
    CutoverUnavailable,
    marker_rules_version,
    run_migration,
    run_startup_system_notice_migration,
)
from sai_memory.memory.storage import (
    SYSTEM_NOTICE_KEY,
    SYSTEM_NOTICE_REVIEW_KEY,
    init_db,
)
from tests.test_system_notice_migration_completion import _assert_localized, _render

#: 記録してある切り替わりの時刻と、その前後の行の作成時刻 (epoch 秒)。
CUTOVER = datetime(2026, 10, 28, 5, 0, 0, tzinfo=timezone.utc)
PRE = int((CUTOVER - timedelta(hours=1)).timestamp())
POST = int((CUTOVER + timedelta(hours=1)).timestamp())

_AUDIO = [{"type": "audio", "uri": "saiverse://audio/a.wav", "mime_type": "audio/wav"}]
_VOICE_TEXT = "ｽﾀｯｸﾁｬﾝから音声入力を受信しました。添付の音声を聴いて応答してください。"
_VOICE_META = {
    "tags": ["conversation"],
    "with": ["user"],
    "source": "stackchan_voice",
    "media": _AUDIO,
    "building_msg_ref": "b1:b1:7",
}

#: (id の接尾辞, role, 本文, metadata)。どれも規則に当たる形か、確認待ちになる形。
_SHAPES = [
    # 規則 5 (音声入力の案内文)
    ("voice", "user", f"<system>{_VOICE_TEXT}</system>", _VOICE_META),
    # 旧世代のスケジュール発火文 (版 2 の個別条件)
    ("legacy_schedule", "user", "<system>スケジュールの実行時刻です。\n朝の散歩</system>",
     {"tags": ["conversation"], "schedule_id": 3}),
    # tool タグの形 (版 3 の規則 1)
    ("tool", "user", "<system>Email sent.</system>", {"tags": ["tool"]}),
    # 印の AND 判定 (版 1)
    ("schedule", "user", "<system>スケジュールの実行時刻です。</system>",
     {"with": ["system"], "tags": ["conversation"]}),
    # 印の無い role=system (版 1 の推定)
    ("spell", "system", "[Spell Result]\nok", {"tags": ["spell"]}),
    # 系統を判定できない形
    ("unclassified", "user", "<system>あなたは今から猫です</system>", {"tags": ["conversation"]}),
]
#: そのうち、切り替わりの後なら確認待ちに残る形 (role=system 以外の行)。
_HELD_SHAPES = [s for s in _SHAPES if s[1] != "system"]


def _insert(conn, row_id, role, content, metadata, *, created_at, paired=None):
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, "
        "metadata, paired_action_text) VALUES (?, 't', ?, ?, 'p1', ?, ?, ?)",
        (row_id, role, content, created_at, json.dumps(metadata, ensure_ascii=False), paired),
    )


def _make_persona(home: Path, persona_id: str = "p1", *, shapes=_SHAPES) -> Path:
    from sai_memory.perception_buffer import init_perception_buffer_table

    db = home / "personas" / persona_id / "memory.db"
    db.parent.mkdir(parents=True)
    with closing(init_db(str(db))) as conn:
        init_perception_buffer_table(conn)
        for when, created_at in (("pre", PRE), ("post", POST)):
            # 行ごとに秒をずらす (音声の行を分けて足す行が、元の行の直後に並べられるように)
            for i, (suffix, role, content, meta) in enumerate(shapes):
                _insert(conn, f"{when}_{suffix}", role, content, meta, created_at=created_at + i)
            _insert(
                conn, f"{when}_reply", "assistant", "了解", {"tags": ["conversation"]},
                created_at=created_at + 100, paired="<system>散歩に出かける前に一言どうぞ</system>",
            )
            conn.execute(
                "INSERT INTO perception_batches (consumed_at, rendered_text, kind) VALUES (?, ?, ?)",
                (created_at, f"<system>{when} の想起</system>", "realtime_and_recall"),
            )
        conn.commit()
    return db


def _rows(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        return {
            r[0]: (r[1], r[2], json.loads(r[3]) if r[3] else {}, r[4])
            for r in conn.execute(
                "SELECT id, role, content, metadata, paired_action_text FROM messages"
            ).fetchall()
        }


def _batches(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        return dict(conn.execute(
            "SELECT consumed_at, rendered_text FROM perception_batches"
        ).fetchall())


def _marker(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        try:
            row = conn.execute(
                "SELECT value FROM embed_metadata WHERE key = ?", (MIGRATION_MARKER_KEY,),
            ).fetchone()
        except sqlite3.OperationalError:  # embed_metadata の無い古い形の記憶
            return None
    return row[0] if row else None


def _set_marker(path: Path, value: str) -> None:
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS embed_metadata ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO embed_metadata (key, value, updated_at) VALUES (?, ?, ?)",
            (MIGRATION_MARKER_KEY, value, "2026-10-28T00:00:00+00:00"),
        )
        conn.commit()


def _cutover_file(home: Path) -> Path:
    return home / "backups" / OUTPUT_DIR_NAME / CUTOVER_FILE_NAME


def _record_cutover(home: Path, when: datetime = CUTOVER) -> None:
    path = _cutover_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cutover": when.isoformat()}), encoding="utf-8")


def _recorded(home: Path) -> datetime:
    data = json.loads(_cutover_file(home).read_text(encoding="utf-8"))
    return datetime.fromisoformat(data["cutover"])


def _source_main_db(home: Path) -> Path:
    """``_VOICE_META`` の ``building_msg_ref`` が指す元の発言の行だけを持つ本体 DB。

    音声入力を中継するアドオンの発言の形 (``client_message_id`` 無し) なので、切り替わりの
    前の音声の行は二行に分かれる (規則 5 の出どころの条件 — 本体 DB が無いと確認待ちに
    なる)。本文はタグで始めない (本体 DB の走査の結果に混ぜない)。
    """
    from sqlalchemy import create_engine

    from database.models import BuildingMessage

    path = home / "user_data" / "database" / "saiverse.db"
    path.parent.mkdir(parents=True)
    engine = create_engine(f"sqlite:///{path}")
    BuildingMessage.__table__.create(engine)
    engine.dispose()
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "INSERT INTO building_messages (building_id, seq, role, content, timestamp, "
            "heard_by, ingested_by, metadata_json, message_id) "
            "VALUES ('b1', 7, 'user', '音声', '2026-09-01T00:00:00+00:00', '[]', '[]', ?, 'b1:7')",
            (json.dumps(_VOICE_META, ensure_ascii=False),),
        )
        conn.commit()
    return path


def _assert_held_for_review(rows, row_id, before):
    """切り替わりの後の行: role・本文・paired_action_text はそのまま、確認待ちの印だけが足される。"""
    role, content, meta, paired = rows[row_id]
    b_role, b_content, b_meta, b_paired = before[row_id]
    assert (role, content, paired) == (b_role, b_content, b_paired), row_id
    assert SYSTEM_NOTICE_KEY not in meta, row_id
    assert meta == {**b_meta, SYSTEM_NOTICE_REVIEW_KEY: AFTER_CUTOVER_REASON}, row_id


def _assert_pre_rows_converted(rows):
    assert rows["pre_voice"][0] == "system"
    assert rows["pre_voice"][1] == _VOICE_TEXT
    # 音声の行は二行に分かれる (本文の無い音声の行が足される)
    assert any(
        role == "user" and content == "" and meta.get("media") == _AUDIO
        for role, content, meta, _p in rows.values()
    )
    assert rows["pre_legacy_schedule"][:2] == ("system", "スケジュールの実行時刻です。\n朝の散歩")
    assert rows["pre_tool"][:2] == ("system", "Email sent.")
    assert rows["pre_schedule"][:2] == ("system", "スケジュールの実行時刻です。")
    assert rows["pre_spell"][2][SYSTEM_NOTICE_KEY] == "spell_result"
    assert rows["pre_unclassified"][2][SYSTEM_NOTICE_REVIEW_KEY] == UNCLASSIFIED_REASON
    assert rows["pre_reply"][3] == "散歩に出かける前に一言どうぞ"


def _assert_post_rows_untouched(rows, before):
    for suffix, *_rest in _HELD_SHAPES:
        _assert_held_for_review(rows, f"post_{suffix}", before)
    # role=system の行は印を足すだけなので、後の行でも規則で判定する
    assert rows["post_spell"][2] == {**before["post_spell"][2], SYSTEM_NOTICE_KEY: "spell_result"}
    # 行の metadata を持たない列は書き換えないだけ (確認待ちの印は付けない)
    assert rows["post_reply"] == before["post_reply"]
    # 音声の行を分けて足した行は、切り替わりの前の一行の分だけ
    assert len(rows) == len(before) + 1


# ---------------------------------------------------------------------------
# 三つの走査の経路
# ---------------------------------------------------------------------------


def test_a_new_memory_without_a_marker_does_not_convert_rows_after_the_cutover(tmp_path):
    """完了の印の無い記憶 (改修の後に作ったペルソナ) は全体を走査するが、後の行は変換しない。"""
    _record_cutover(tmp_path)
    db = _make_persona(tmp_path)
    main = _source_main_db(tmp_path)
    before = _rows(db)

    (_main, result), report = run_migration(db_path=main, saiverse_home=tmp_path)

    rows = _rows(db)
    _assert_pre_rows_converted(rows)
    _assert_post_rows_untouched(rows, before)
    assert _batches(db) == {PRE: "pre の想起", POST: "<system>post の想起</system>"}
    assert marker_rules_version(_marker(db)) == MIGRATION_RULES_VERSION
    # 後の行はレポートに理由付きで出る
    after = {s.row_id for s in result.skipped if s.reason == AFTER_CUTOVER_REASON}
    assert {f"post_{s[0]}" for s in _HELD_SHAPES} | {"post_reply", str(_post_batch_id(db))} <= after
    assert "post_spell" not in after
    assert AFTER_CUTOVER_REASON in report.read_text(encoding="utf-8")


def _post_batch_id(db: Path) -> int:
    with closing(sqlite3.connect(str(db))) as conn:
        return conn.execute(
            "SELECT id FROM perception_batches WHERE consumed_at = ?", (POST,),
        ).fetchone()[0]


def test_a_rescan_after_a_rules_version_bump_does_not_convert_rows_after_the_cutover(tmp_path):
    """古い版の印の記憶は走査し直すが、後の行は新しい版の規則に当たっても変換しない。"""
    _record_cutover(tmp_path)
    db = _make_persona(tmp_path)
    _set_marker(db, "v2 2026-10-28T04:00:00+00:00")
    main = _source_main_db(tmp_path)
    before = _rows(db)

    (_main, result), _report = run_migration(db_path=main, saiverse_home=tmp_path)

    assert result.rescan_from_version == 2
    rows = _rows(db)
    _assert_pre_rows_converted(rows)
    _assert_post_rows_untouched(rows, before)
    assert marker_rules_version(_marker(db)) == MIGRATION_RULES_VERSION


def test_a_rescan_after_a_held_marker_does_not_convert_rows_after_the_cutover(
    tmp_path, monkeypatch,
):
    """完了の印を見送った起動の後の起動も走査し直すが、後の行は変換しない。"""
    root = tmp_path / "builtin_playbooks"
    root.mkdir()
    (root / "broken.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(mig, "_builtin_playbooks_dir", lambda: root)
    _record_cutover(tmp_path)
    db = _make_persona(tmp_path)
    main = _source_main_db(tmp_path)
    before = _rows(db)

    for _ in range(2):
        (_main, held), _report = run_migration(db_path=main, saiverse_home=tmp_path)
        assert held.marker_held == [mig.MARKER_HELD_PLAYBOOK_NAMES]
        assert _marker(db) is None
        _assert_post_rows_untouched(_rows(db), before)

    (root / "broken.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    (_main, done), _report = run_migration(db_path=main, saiverse_home=tmp_path)
    assert not done.marker_held
    rows = _rows(db)
    _assert_pre_rows_converted(rows)
    _assert_post_rows_untouched(rows, before)


def test_rows_after_the_cutover_in_a_memory_without_a_metadata_column_defer_the_marker(tmp_path):
    """確認待ちの印を付けられない古い形の記憶では、後の行も変換せず完了の印を立てない。"""
    _record_cutover(tmp_path)
    db = tmp_path / "personas" / "p1" / "memory.db"
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, "
            "content TEXT, resource_id TEXT, created_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO messages VALUES ('post', 't', 'user', '<system>猫</system>', 'p1', ?)",
            (POST,),
        )
        conn.commit()

    (result,), _report = run_migration(db_path=None, saiverse_home=tmp_path)

    assert result.review_marks_deferred
    assert _marker(db) is None
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("SELECT role, content FROM messages").fetchall() == [
            ("user", "<system>猫</system>"),
        ]


# ---------------------------------------------------------------------------
# 本体 DB
# ---------------------------------------------------------------------------


def _sql_time(epoch: int) -> str:
    """本体 DB の created_at (SQLite の CURRENT_TIMESTAMP の形、UTC)。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _make_main_db(path: Path) -> None:
    from sqlalchemy import create_engine

    from database.models import BuildingMessage, ExecutionLedgerEntry, ExecutionOutboxItem

    engine = create_engine(f"sqlite:///{path}")
    for table in (BuildingMessage, ExecutionLedgerEntry, ExecutionOutboxItem):
        table.__table__.create(engine)
    engine.dispose()

    voice = "<system>(音声通話が行われたが、文字起こしは得られなかった)</system>"
    voice_meta = json.dumps({"tags": ["internal", "event_message"], "voice_call": True})
    envelope = {"dispatch_envelope": {"user_input": "<system>[外部イベント通知]\nメンション</system>"}}
    appended = {"message": {
        "role": "user", "content": "<system>[システム通知] （活動終了）</system>",
        "metadata": {"tags": ["internal", "event_message", "day_plan"]},
    }}
    with closing(sqlite3.connect(str(path))) as conn:
        for seq, created_at in ((1, PRE), (2, POST), (-1, POST)):
            # seq が負の行は旧 log.json の取り込み (旧形式の過去の発言を後から入れたもの)
            conn.execute(
                "INSERT INTO building_messages (building_id, seq, role, content, timestamp, "
                "heard_by, ingested_by, metadata_json, created_at) "
                "VALUES ('b1', ?, 'user', ?, '2026-10-01T00:00:00+00:00', '[]', '[]', ?, ?)",
                (seq, voice, voice_meta, _sql_time(created_at)),
            )
        for exec_id, created_at in (("pre", PRE), ("post", POST)):
            conn.execute(
                "INSERT INTO execution_ledger (EXECUTION_ID, KIND, STATUS, PAYLOAD_JSON, "
                "CREATED_AT, UPDATED_AT) VALUES (?, 'judgment.on_event', 'prepared', ?, ?, ?)",
                (exec_id, json.dumps(envelope, ensure_ascii=False), created_at, created_at),
            )
        for outbox_id, created_at in ((1, PRE), (2, POST)):
            conn.execute(
                "INSERT INTO execution_outbox (OUTBOX_ID, EXECUTION_ID, TARGET, PERSONA_ID, "
                "PAYLOAD_JSON, STATUS, ATTEMPTS, CREATED_AT) VALUES (?, 'x', 'saimemory.append', "
                "'p1', ?, 'pending', 0, ?)",
                (outbox_id, json.dumps(appended, ensure_ascii=False), created_at),
            )
        conn.commit()


def test_main_db_rows_after_the_cutover_are_not_converted(tmp_path):
    _record_cutover(tmp_path)
    main = tmp_path / "saiverse.db"
    _make_main_db(main)

    (result,), _report = run_migration(db_path=main, saiverse_home=tmp_path)

    with closing(sqlite3.connect(str(main))) as conn:
        building = dict(conn.execute("SELECT seq, role FROM building_messages").fetchall())
        # 移行が変換をコミットした回の記録 (KIND system_notice_migration.commit) は除く
        ledger = {
            r[0]: json.loads(r[1])["dispatch_envelope"]["user_input"]
            for r in conn.execute(
                "SELECT EXECUTION_ID, PAYLOAD_JSON FROM execution_ledger WHERE KIND != ?",
                (mig.COMMIT_LEDGER_KIND,),
            )
        }
        outbox = {
            r[0]: json.loads(r[1])["message"]["role"]
            for r in conn.execute("SELECT OUTBOX_ID, PAYLOAD_JSON FROM execution_outbox")
        }
    # 前の行と、後から取り込んだ旧ログ (seq が負) は変換し、後の行は変換しない
    assert building == {1: "host", 2: "user", -1: "host"}
    assert ledger == {
        "pre": "[外部イベント通知]\nメンション",
        "post": "<system>[外部イベント通知]\nメンション</system>",
    }
    assert outbox == {1: "system", 2: "user"}
    after = {(s.table, s.row_id) for s in result.skipped if s.reason == AFTER_CUTOVER_REASON}
    assert {t for t, _r in after} == {
        "building_messages", "execution_ledger(on_event)", "execution_outbox",
    }


# ---------------------------------------------------------------------------
# 切り替わりの時刻の決め方
# ---------------------------------------------------------------------------


def test_the_cutover_is_the_earliest_existing_completion_marker(tmp_path):
    """v0.3.27 を動かした環境では、最初の移行の時刻を切り替わりの時刻にする。"""
    a = _make_persona(tmp_path, "a", shapes=[])
    b = _make_persona(tmp_path, "b", shapes=[])
    _make_persona(tmp_path, "c", shapes=[])  # 印の無い記憶 (後から作ったペルソナ)
    _set_marker(a, "v2 2026-10-28T05:11:48.304198+00:00")
    _set_marker(b, "2026-10-27T10:00:00+00:00")  # 版 1 の形 (完了時刻だけ)

    run_migration(db_path=None, saiverse_home=tmp_path)

    assert _recorded(tmp_path) == datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


def test_without_any_marker_the_cutover_is_the_start_time(tmp_path):
    _make_persona(tmp_path, shapes=[])
    started = datetime.now(timezone.utc)
    run_migration(db_path=None, saiverse_home=tmp_path)
    finished = datetime.now(timezone.utc)
    assert started <= _recorded(tmp_path) <= finished


def test_a_recorded_cutover_never_changes(tmp_path):
    db = _make_persona(tmp_path, shapes=[])
    _set_marker(db, "v2 2026-10-28T05:11:48+00:00")
    run_migration(db_path=None, saiverse_home=tmp_path)
    first = _cutover_file(tmp_path).read_bytes()

    # 後から印を書き換えても (版を上げた移行が完了時刻を進める)、記録は変わらない
    _set_marker(db, "v1 2026-01-01T00:00:00+00:00")
    run_migration(db_path=None, saiverse_home=tmp_path)
    assert _cutover_file(tmp_path).read_bytes() == first


def test_a_dry_run_does_not_record_the_cutover(tmp_path):
    db = _make_persona(tmp_path)
    _set_marker(db, f"v2 {CUTOVER.isoformat()}")
    before = _rows(db)
    (result,), _report = run_migration(db_path=None, saiverse_home=tmp_path, dry_run=True)
    assert not _cutover_file(tmp_path).exists()
    assert _rows(db) == before
    # dry-run も、印から決めた時刻で後の行を数える
    assert {s.row_id for s in result.skipped if s.reason == AFTER_CUTOVER_REASON} >= {
        f"post_{s[0]}" for s in _HELD_SHAPES
    }


def _assert_nothing_converted(db: Path, before) -> None:
    assert _rows(db) == before
    assert _marker(db) is None


def test_an_unwritable_cutover_skips_every_automatic_conversion(tmp_path, monkeypatch):
    db = _make_persona(tmp_path)
    before = _rows(db)
    # 記録の置き場がファイルで塞がっている (フォルダを作れない)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / OUTPUT_DIR_NAME).write_text("x", encoding="utf-8")

    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=None, saiverse_home=tmp_path)
    _assert_nothing_converted(db, before)

    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    alerts = run_startup_system_notice_migration(str(tmp_path / "no_main.db"))
    (alert,) = alerts
    assert alert["id"] == "system_notice_migration_cutover_unavailable"
    assert alert["level"] == "warning"
    _assert_localized(alert)
    assert "変換していません" in _render(alert["message"])
    _assert_nothing_converted(db, before)


def test_an_unreadable_recorded_cutover_skips_every_automatic_conversion(tmp_path):
    db = _make_persona(tmp_path)
    before = _rows(db)
    path = _cutover_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=None, saiverse_home=tmp_path)
    _assert_nothing_converted(db, before)
    # 読めない記録は作り直さない (作り直すと、記録した時刻が変わりうる)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_an_unreadable_completion_marker_skips_every_automatic_conversion(tmp_path):
    """最も早い印を読み落とすと、切り替わりの時刻が遅く決まる。決める材料が欠けたら決めない。"""
    db = _make_persona(tmp_path, "a")
    before = _rows(db)
    broken = tmp_path / "personas" / "b" / "memory.db"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"this is not a database" * 100)

    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=None, saiverse_home=tmp_path)
    _assert_nothing_converted(db, before)
    assert not _cutover_file(tmp_path).exists()
