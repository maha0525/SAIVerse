"""システム通知の移行の、敵対レビュー 2 巡目の指摘への回帰試験 (database/system_notice_migration.py)。

docs/intent/system_notice_delivery.md「追補」の「時刻の境」と「移行の規則の第二版」。
合成した一時 DB だけを使う。固定すること:

- 境の時刻は、記憶 DB の完了の印と、移行が前に動いた痕跡 (起動ごとの結果のフォルダの
  ``RUN``・移行前の写し・``last_leftovers.json``) のうち最も早い時刻。痕跡はあるが時刻を
  読めない起動は何も変換しない。印も痕跡も無いときだけ、その起動の時刻を使う。
- 境と同じ秒に作られた行は境より後として扱う (どの保存先でも)。
- 発話者の添付を持つ行は、規則 5 の分割以外のどの規則でも行ごと変換しない。規則 5 の
  形の音声の行は、旧規則より先に規則 5 の経路 (出どころの確認つき) に通す。
- metadata が辞書でない行が残る記憶 DB は完了の印を見送り、画面に件数とペルソナを出す。
- 変換をコミットする取引は、その回を指す記録を同じ DB に書き、仮の結果の JSON の採否は
  その記録とも照合する (完了の印を見送った回・本体 DB でも採用できる)。
- 結果の JSON は変換した全行を持ち、全件のファイルをレポートの隣に書く (dry-run でも)。
- 音声の uri は JSON として解釈した media の値で比べる (エスケープの違いに左右されない)。
- ``cutover.json`` は、固いリンクを作れないときも、完成した仮のファイルだけを正式な名前で
  公開する。
"""
from __future__ import annotations

import json
import os
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
    OUTPUT_DIR_NAME,
    RESULTS_DIR_NAME,
    SENT_FROM_SCREEN_REASON,
    VOICE_INPUT_NOTICE_KIND,
    VOICE_SOURCE_UNKNOWN_REASON,
    BuildingSourceRows,
    CutoverUnavailable,
    migrate_persona_db,
    regenerate_pending_reports,
    run_migration,
    startup_alerts,
)
from sai_memory.memory.storage import SYSTEM_NOTICE_KEY, SYSTEM_NOTICE_REVIEW_KEY, init_db
from tests.test_system_notice_migration_completion import _assert_localized, _render

CUTOVER = datetime(2026, 10, 28, 5, 0, 0, tzinfo=timezone.utc)
PRE = int((CUTOVER - timedelta(hours=1)).timestamp())
POST = int((CUTOVER + timedelta(hours=1)).timestamp())

_VOICE_TEXT = "ｽﾀｯｸﾁｬﾝから音声入力を受信しました。添付の音声を聴いて応答してください。"
_VOICE_CONTENT = f"<system>{_VOICE_TEXT}</system>"
_TOOL_ROW = ("user", "<system>Email sent.</system>", {"tags": ["tool"]})


def _output_dir(home: Path) -> Path:
    return home / "backups" / OUTPUT_DIR_NAME


def _record_cutover(home: Path, when: datetime = CUTOVER) -> None:
    path = _output_dir(home) / CUTOVER_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cutover": when.isoformat()}), encoding="utf-8")


def _recorded(home: Path) -> dict:
    return json.loads((_output_dir(home) / CUTOVER_FILE_NAME).read_text(encoding="utf-8"))


def _make_persona(home: Path, rows, persona_id: str = "p1") -> Path:
    """rows は (id, role, content, metadata, created_at)。metadata が str ならそのまま書く。"""
    from sai_memory.perception_buffer import init_perception_buffer_table

    db = home / "personas" / persona_id / "memory.db"
    db.parent.mkdir(parents=True)
    with closing(init_db(str(db))) as conn:
        init_perception_buffer_table(conn)
        for row_id, role, content, meta, created_at in rows:
            raw = meta if isinstance(meta, str) else json.dumps(meta, ensure_ascii=False)
            conn.execute(
                "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, "
                "metadata) VALUES (?, 't', ?, ?, ?, ?, ?)",
                (row_id, role, content, persona_id, created_at, raw),
            )
        conn.commit()
    return db


def _rows(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        return {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute("SELECT id, role, content, metadata FROM messages").fetchall()
        }


def _meta(path: Path, row_id: str) -> dict:
    return json.loads(_rows(path)[row_id][2] or "{}")


def _marker(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        row = conn.execute(
            "SELECT value FROM embed_metadata WHERE key = ?", (MIGRATION_MARKER_KEY,),
        ).fetchone()
    return row[0] if row else None


def _set_marker(path: Path, value: str) -> None:
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embed_metadata (key, value, updated_at) VALUES (?, ?, ?)",
            (MIGRATION_MARKER_KEY, value, "2026-10-28T00:00:00+00:00"),
        )
        conn.commit()


def _make_main_db(path: Path) -> Path:
    from sqlalchemy import create_engine

    from database.models import BuildingMessage, ExecutionLedgerEntry, ExecutionOutboxItem

    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    for table in (BuildingMessage, ExecutionLedgerEntry, ExecutionOutboxItem):
        table.__table__.create(engine)
    engine.dispose()
    return path


def _insert_building_row(path: Path, *, seq, content, raw_meta, created_at=None, cmid=None,
                         building_id="b1"):
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "INSERT INTO building_messages (building_id, seq, role, content, timestamp, "
            "heard_by, ingested_by, metadata_json, message_id, client_message_id, created_at) "
            "VALUES (?, ?, 'user', ?, '2026-09-01T00:00:00+00:00', '[]', '[]', ?, ?, ?, "
            "COALESCE(?, CURRENT_TIMESTAMP))",
            (building_id, seq, content, raw_meta, f"{building_id}:{seq}", cmid, created_at),
        )
        conn.commit()


def _sql_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 1. 境の時刻: 完了の印に加えて、移行が前に動いた痕跡を候補に含める
# ---------------------------------------------------------------------------


def _write_run_trace(home: Path, started_at: datetime, *, name="20261028_140000_1_abc") -> Path:
    run_dir = _output_dir(home) / RESULTS_DIR_NAME / name
    run_dir.mkdir(parents=True)
    (run_dir / "RUN").write_text(json.dumps({
        "pid": 999999, "process_created_at": None, "started_at": started_at.isoformat(),
    }), encoding="utf-8")
    (run_dir / "main.json").write_text("{}", encoding="utf-8")
    return run_dir


def test_a_run_trace_sets_the_cutover_when_no_memory_has_a_marker(tmp_path):
    """v0.3.27 の起動時に記憶 DB が無く、後でペルソナを作って発言した環境。"""
    _write_run_trace(tmp_path, CUTOVER)
    db = _make_persona(tmp_path, [("post_tool", *_TOOL_ROW, POST)])

    (result,), _report = run_migration(db_path=None, saiverse_home=tmp_path)

    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == CUTOVER
    assert _recorded(tmp_path)["source"] == mig.CUTOVER_FROM_TRACE
    assert _rows(db)["post_tool"][:2] == ("user", "<system>Email sent.</system>")
    assert _meta(db, "post_tool")[SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON
    assert [s.reason for s in result.skipped] == [AFTER_CUTOVER_REASON]


def test_a_trace_earlier_than_every_marker_wins(tmp_path):
    """古い記憶を消して、残った最古の印が実際の切り替えより遅くなった環境。"""
    _write_run_trace(tmp_path, CUTOVER)
    db = _make_persona(tmp_path, [("post_tool", *_TOOL_ROW, POST)])
    _set_marker(db, f"v2 {(CUTOVER + timedelta(hours=3)).isoformat()}")

    run_migration(db_path=None, saiverse_home=tmp_path)

    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == CUTOVER
    assert _meta(db, "post_tool")[SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON


@pytest.mark.parametrize("trace", ["persona_backup", "main_backup", "last_leftovers"])
def test_copies_and_last_leftovers_count_as_traces(tmp_path, trace):
    """移行前の写しと last_leftovers.json は、書いた時刻 (更新時刻) を候補にする。"""
    out = _output_dir(tmp_path)
    if trace == "persona_backup":
        path = out / "gone" / "memory.db.pre_migration_20261028_140000_1_abc.bak"
    elif trace == "main_backup":
        path = out / "saiverse.db.pre_migration_20261028_140000_1_abc.bak"
    else:
        path = out / "last_leftovers.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (CUTOVER.timestamp(), CUTOVER.timestamp()))
    db = _make_persona(tmp_path, [("post_tool", *_TOOL_ROW, POST)])

    run_migration(db_path=None, saiverse_home=tmp_path)

    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == CUTOVER
    assert _meta(db, "post_tool")[SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON


def test_a_dry_run_report_is_not_a_trace(tmp_path):
    """dry-run は世界を新しい形式で動かしていないので、そのレポートは痕跡に数えない。"""
    report = _output_dir(tmp_path) / "report_20261028_140000_dry_run.md"
    report.parent.mkdir(parents=True)
    report.write_text("x", encoding="utf-8")
    os.utime(report, (CUTOVER.timestamp(), CUTOVER.timestamp()))
    _make_persona(tmp_path, [])

    started = datetime.now(timezone.utc)
    run_migration(db_path=None, saiverse_home=tmp_path)

    assert _recorded(tmp_path)["source"] == mig.CUTOVER_FROM_START
    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) >= started


def test_a_trace_whose_time_cannot_be_read_skips_every_conversion(tmp_path):
    run_dir = _write_run_trace(tmp_path, CUTOVER)
    (run_dir / "RUN").write_text("{broken", encoding="utf-8")
    db = _make_persona(tmp_path, [("pre_tool", *_TOOL_ROW, PRE)])
    before = _rows(db)

    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=None, saiverse_home=tmp_path)
    assert _rows(db) == before
    assert not (_output_dir(tmp_path) / CUTOVER_FILE_NAME).exists()


def test_without_markers_or_traces_the_cutover_is_the_start_time(tmp_path):
    _make_persona(tmp_path, [])
    started = datetime.now(timezone.utc)
    run_migration(db_path=None, saiverse_home=tmp_path)
    assert _recorded(tmp_path)["source"] == mig.CUTOVER_FROM_START
    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) >= started


# ---------------------------------------------------------------------------
# 2. 境と同じ秒に作られた行は後として扱う
# ---------------------------------------------------------------------------

#: 秒の端数のある境。行はその秒の頭 (境より前の端数) に置く。
CUTOVER_FRACTION = CUTOVER + timedelta(milliseconds=300)
SAME_SECOND = int(CUTOVER.timestamp())


@pytest.mark.parametrize(("created_at", "after"), [
    (SAME_SECOND, True),
    (SAME_SECOND + 0.8, True),
    (SAME_SECOND - 0.2, False),
    (SAME_SECOND - 1, False),
    (_sql_time(SAME_SECOND), True),
    (_sql_time(SAME_SECOND - 1), False),
])
def test_the_same_second_as_the_cutover_counts_as_after(created_at, after):
    cutover = mig.Cutover(CUTOVER_FRACTION, mig.CUTOVER_FROM_START).epoch
    assert mig._created_after_cutover(created_at, cutover) is after


def test_rows_in_the_same_second_are_held_in_every_store(tmp_path):
    _record_cutover(tmp_path, CUTOVER_FRACTION)
    db = _make_persona(tmp_path, [
        ("same_tool", *_TOOL_ROW, SAME_SECOND),
        ("before_tool", *_TOOL_ROW, SAME_SECOND - 1),
    ])
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "UPDATE messages SET paired_action_text = '<system>一言どうぞ</system>' WHERE id = 'same_tool'"
        )
        conn.execute(
            "INSERT INTO perception_batches (consumed_at, rendered_text, kind) VALUES (?, ?, ?)",
            (SAME_SECOND, "<system>想起</system>", "realtime_and_recall"),
        )
        conn.commit()
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(
        main, seq=1, content="<system>(音声通話が行われた)</system>",
        raw_meta=json.dumps({"tags": ["internal", "event_message"], "voice_call": True}),
        created_at=_sql_time(SAME_SECOND),
    )
    with closing(sqlite3.connect(str(main))) as conn:
        conn.execute(
            "INSERT INTO execution_ledger (EXECUTION_ID, KIND, STATUS, PAYLOAD_JSON, CREATED_AT, "
            "UPDATED_AT) VALUES ('e1', 'judgment.on_event', 'prepared', ?, ?, ?)",
            (json.dumps({"dispatch_envelope": {"user_input": "<system>[外部イベント通知]</system>"}}),
             SAME_SECOND, SAME_SECOND),
        )
        conn.execute(
            "INSERT INTO execution_outbox (OUTBOX_ID, EXECUTION_ID, TARGET, PERSONA_ID, "
            "PAYLOAD_JSON, STATUS, ATTEMPTS, CREATED_AT) VALUES (1, 'e1', 'saimemory.append', "
            "'p1', ?, 'pending', 0, ?)",
            (json.dumps({"message": {
                "role": "user", "content": "<system>活動終了</system>",
                "metadata": {"tags": ["internal", "event_message", "day_plan"]},
            }}, ensure_ascii=False), SAME_SECOND),
        )
        conn.commit()

    (main_result, persona), _report = run_migration(db_path=main, saiverse_home=tmp_path)

    rows = _rows(db)
    assert rows["same_tool"][:2] == ("user", "<system>Email sent.</system>")
    assert _meta(db, "same_tool")[SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON
    assert rows["before_tool"][:2] == ("system", "Email sent.")
    after = {
        (s.table, s.row_id) for r in (main_result, persona) for s in r.skipped
        if s.reason == AFTER_CUTOVER_REASON
    }
    assert {t for t, _ in after} == {
        "messages", "messages.paired_action_text", "perception_batches", "building_messages",
        "execution_ledger(on_event)", "execution_outbox",
    }
    assert not main_result.changes


# ---------------------------------------------------------------------------
# 4. 発話者の添付を持つ行は、規則 5 の分割以外では行ごと変換しない
# ---------------------------------------------------------------------------

_AUDIO = [{"type": "audio", "uri": "saiverse://audio/a.ogg", "mime_type": "audio/ogg"}]


def _voice_meta(**extra):
    meta = {
        "tags": ["conversation"], "with": ["user"], "source": "stackchan_voice",
        "media": _AUDIO, "building_msg_ref": "b1:b1:1",
    }
    meta.update(extra)
    return meta


@pytest.mark.parametrize(("cmid", "split"), [("cmid-1", False), (None, True)])
def test_a_voice_row_with_a_legacy_tag_goes_through_the_source_check(tmp_path, cmid, split):
    """event_message タグ (旧規則) を持つ音声の行も、規則 5 の出どころの確認を通る。"""
    meta = _voice_meta(tags=["conversation", "event_message"])
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content=_VOICE_CONTENT, raw_meta=json.dumps(meta), cmid=cmid)
    db = _make_persona(tmp_path, [("voice", "user", _VOICE_CONTENT, meta, PRE)])

    with BuildingSourceRows(main) as source_rows:
        migrate_persona_db(
            db, persona_id="p1", output_dir=tmp_path / "out", stamp="s",
            playbook_names=frozenset(), source_rows=source_rows, cutover=int(CUTOVER.timestamp()),
        )

    rows = _rows(db)
    if split:
        assert rows["voice"][:2] == ("system", _VOICE_TEXT)
        notice = json.loads(rows["voice"][2])
        assert notice[SYSTEM_NOTICE_KEY] == VOICE_INPUT_NOTICE_KIND
        assert "media" not in notice
        assert len(rows) == 2
    else:
        assert rows["voice"][:2] == ("user", _VOICE_CONTENT)
        assert _meta(db, "voice")[SYSTEM_NOTICE_REVIEW_KEY] == SENT_FROM_SCREEN_REASON
        assert len(rows) == 1


_IMAGES = [{"uri": "saiverse://image/x.png", "mime_type": "image/png"}]
_DOCS = [{"path": "doc.txt", "name": "doc.txt"}]


@pytest.mark.parametrize(("role", "content", "meta"), [
    # 印の AND 判定 (版 1) に当たる形 + 画像 + 建物の履歴からの転記の印
    ("user", "<system>スケジュールの実行時刻です。</system>",
     {"with": ["system"], "tags": ["conversation"], "images": _IMAGES,
      "building_msg_ref": "b:1"}),
    # event_message (版 1) + 音声だが案内文の形でない + 転記の印
    ("user", "<system>(音声通話)</system>",
     {"tags": ["internal", "event_message"], "media": _AUDIO, "building_msg_ref": "b:2"}),
    # event_message (版 1) + 画像 + 通話の文字起こしの印
    ("user", "<system>(音声通話)</system>",
     {"tags": ["internal", "event_message"], "images": _IMAGES, "voice_transcript": True}),
    # 中断の記録 (role=assistant、版 1) + 画像 — ペルソナの添付も機構の持ち物にしない
    ("assistant", "<system>(中断)</system>", {"tags": ["interrupted"], "images": _IMAGES}),
])
def test_a_speaker_row_with_attachments_is_never_converted_whole(tmp_path, role, content, meta):
    db = _make_persona(tmp_path, [("row", role, content, meta, PRE)])

    result = migrate_persona_db(
        db, persona_id="p1", output_dir=tmp_path / "out", stamp="s",
        playbook_names=frozenset(), cutover=int(CUTOVER.timestamp()),
    )

    assert _rows(db)["row"][:2] == (role, content)
    assert _meta(db, "row") == {**meta, SYSTEM_NOTICE_REVIEW_KEY: mig.SPEAKER_ATTACHMENT_REASON}
    assert not result.changes
    assert [s.reason for s in result.skipped] == [mig.SPEAKER_ATTACHMENT_REASON]


@pytest.mark.parametrize(("content", "meta"), [
    # tool タグ (版 3 の規則 1) + 文書。発言の来歴が無いので添付は機構のもの
    ("<system>Email sent.</system>", {"tags": ["tool"], "documents": _DOCS}),
    # 印の AND 判定 (版 1) + 画像 (旧世代の知覚のまとまりの形)
    ("<system>スケジュールの実行時刻です。</system>",
     {"with": ["system"], "tags": ["conversation"], "images": _IMAGES}),
])
def test_a_user_row_without_utterance_provenance_converts_with_its_attachments(
    tmp_path, content, meta,
):
    db = _make_persona(tmp_path, [("row", "user", content, meta, PRE)])

    migrate_persona_db(
        db, persona_id="p1", output_dir=tmp_path / "out", stamp="s",
        playbook_names=frozenset(), cutover=int(CUTOVER.timestamp()),
    )

    assert _rows(db)["row"][0] == "system"
    converted = _meta(db, "row")
    assert SYSTEM_NOTICE_REVIEW_KEY not in converted
    assert any(converted.get(k) == v for k, v in meta.items() if k in ("images", "documents"))


def test_main_db_rows_with_attachments_are_not_converted(tmp_path):
    _record_cutover(tmp_path)
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(
        main, seq=1, content="<system>(音声通話)</system>",
        raw_meta=json.dumps({"tags": ["internal", "event_message"], "voice_call": True,
                             "images": _IMAGES}),
        created_at=_sql_time(PRE),
    )
    with closing(sqlite3.connect(str(main))) as conn:
        conn.execute(
            "INSERT INTO execution_outbox (OUTBOX_ID, EXECUTION_ID, TARGET, PERSONA_ID, "
            "PAYLOAD_JSON, STATUS, ATTEMPTS, CREATED_AT) VALUES (1, 'e1', 'saimemory.append', "
            "'p1', ?, 'pending', 0, ?)",
            (json.dumps({"message": {
                "role": "user", "content": "<system>活動終了</system>",
                "metadata": {"tags": ["internal", "event_message", "day_plan"], "media": _AUDIO},
            }}, ensure_ascii=False), PRE),
        )
        conn.commit()

    (result,), _report = run_migration(db_path=main, saiverse_home=tmp_path)

    assert not result.changes
    assert sorted((s.table, s.reason) for s in result.skipped) == [
        ("building_messages", mig.SPEAKER_ATTACHMENT_REASON),
        ("execution_outbox", mig.SPEAKER_ATTACHMENT_REASON),
    ]


# ---------------------------------------------------------------------------
# 5. metadata が辞書でない行が残る記憶 DB は完了の印を見送り、画面に出す
# ---------------------------------------------------------------------------


def test_unreadable_metadata_holds_the_marker_and_is_shown(tmp_path, monkeypatch):
    _record_cutover(tmp_path)
    db = _make_persona(tmp_path, [
        ("broken", "user", "<system>機構の文?</system>", "[1, 2]", PRE),
        ("tool", *_TOOL_ROW, PRE),
    ])

    results, report = run_migration(db_path=None, saiverse_home=tmp_path)
    (persona,) = results

    assert _marker(db) is None
    assert mig.MARKER_HELD_UNREADABLE_METADATA in persona.marker_held
    assert _rows(db)["tool"][:2] == ("system", "Email sent.")  # 変換はコミットする
    alerts = startup_alerts(results, report)
    (alert,) = [a for a in alerts if a["id"] == "system_notice_migration_unreadable_metadata"]
    _assert_localized(alert)
    rendered = _render(alert)
    assert "1" in rendered["message"]
    assert any("p1" in label for label in rendered["details"]["unreadable_metadata"])

    # 次の起動でも見送り続ける (行が残る間は完了にしない)
    (again,), _ = run_migration(db_path=None, saiverse_home=tmp_path)
    assert mig.MARKER_HELD_UNREADABLE_METADATA in again.marker_held
    assert _marker(db) is None


# ---------------------------------------------------------------------------
# 6. 完了の印を見送った回・本体 DB の仮の結果も、コミットの記録で採用する
# ---------------------------------------------------------------------------


def _staged_and_final(home: Path, key: str):
    root = _output_dir(home) / RESULTS_DIR_NAME
    staged = sorted(root.glob(f"*/{key}.json.tmp"))
    final = sorted(root.glob(f"*/{key}.json"))
    return staged, final


def test_a_committed_result_with_a_held_marker_is_taken_up(tmp_path, monkeypatch):
    root = tmp_path / "builtin_playbooks"
    root.mkdir()
    (root / "broken.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(mig, "_builtin_playbooks_dir", lambda: root)
    # コミットの後、正式な名前へ付け替える前に止まった
    monkeypatch.setattr(mig, "_publish_staged_result", lambda staged, result: None)
    _record_cutover(tmp_path)
    db = _make_persona(tmp_path, [("pre_tool", *_TOOL_ROW, PRE)])

    (persona,), _report = run_migration(db_path=None, saiverse_home=tmp_path)
    assert persona.marker_held == [mig.MARKER_HELD_PLAYBOOK_NAMES]
    assert _rows(db)["pre_tool"][0] == "system"  # コミット済み
    staged, final = _staged_and_final(tmp_path, "persona_p1")
    assert len(staged) == 1 and not final

    regenerate_pending_reports(tmp_path)

    staged, final = _staged_and_final(tmp_path, "persona_p1")
    assert not staged and len(final) == 1
    assert json.loads(final[0].read_text(encoding="utf-8"))["changes"][0]["row_id"] == "pre_tool"


def test_a_committed_main_db_result_is_taken_up(tmp_path, monkeypatch):
    monkeypatch.setattr(mig, "_publish_staged_result", lambda staged, result: None)
    _record_cutover(tmp_path)
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(
        main, seq=1, content="<system>(音声通話が行われた)</system>",
        raw_meta=json.dumps({"tags": ["internal", "event_message"], "voice_call": True}),
        created_at=_sql_time(PRE),
    )

    run_migration(db_path=main, saiverse_home=tmp_path)
    staged, final = _staged_and_final(tmp_path, "main")
    assert len(staged) == 1 and not final

    regenerate_pending_reports(tmp_path)

    staged, final = _staged_and_final(tmp_path, "main")
    assert not staged and len(final) == 1


def test_a_staged_result_without_its_commit_record_is_discarded(tmp_path):
    """コミットの前に止まった回 (DB にその回の記録が無い) の仮の JSON は捨てる。"""
    db = _make_persona(tmp_path, [])
    main = _make_main_db(tmp_path / "saiverse.db")
    run_dir = _write_run_trace(tmp_path, CUTOVER)
    (run_dir / "main.json").unlink()
    for key, path, persona in (("persona_p1", db, True), ("main", main, False)):
        (run_dir / f"{key}.json.tmp").write_text(json.dumps({
            "label": key, "path": str(path), "persona": persona, "marker_value": None,
            "commit_record": run_dir.name, "counts": [["messages", "x", 1]],
        }), encoding="utf-8")

    regenerate_pending_reports(tmp_path)

    assert not list(run_dir.glob("*.json.tmp"))
    assert not list(run_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# 7. 結果の JSON と、レポートの隣の全件のファイルは変換した全行を持つ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_every_converted_row_is_kept(tmp_path, dry_run):
    _record_cutover(tmp_path)
    count = mig.SAMPLES_PER_KIND + 4
    db = _make_persona(tmp_path, [
        (f"tool{i}", "user", f"<system>Email sent {i}.</system>", {"tags": ["tool"]}, PRE + i)
        for i in range(count)
    ])
    assert db.exists()

    (persona,), report = run_migration(db_path=None, saiverse_home=tmp_path, dry_run=dry_run)

    full = mig.changes_path_for(report)
    assert full.parent == report.parent and not full.name.startswith("report_")
    text = full.read_text(encoding="utf-8")
    for i in range(count):
        assert f"id=tool{i}" in text
        assert f"Email sent {i}." in text
    assert str(full) in report.read_text(encoding="utf-8")
    if dry_run:
        return
    (result_json,) = (_output_dir(tmp_path) / RESULTS_DIR_NAME).glob("*/persona_p1.json")
    data = json.loads(result_json.read_text(encoding="utf-8"))
    assert sorted(c["row_id"] for c in data["changes"]) == sorted(f"tool{i}" for i in range(count))
    assert all(c["before"].startswith("<system>") for c in data["changes"])
    restored = mig.StoreResult.from_json(data)
    assert len(restored.changes) == count


# ---------------------------------------------------------------------------
# 8. 音声の uri は JSON として解釈した media の値で比べる
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escaped_uri", [
    r"saiverse:\/\/audio\/same.ogg",  # スラッシュのエスケープ
    "saiverse://audio/\\u0073ame.ogg",  # 非 ASCII 以外も \\u で書ける
])
def test_an_escaped_duplicate_uri_makes_the_source_ambiguous(tmp_path, escaped_uri):
    """同じ音声を指す画面からの行が別の表現で保存されていても、候補に入る (一つに決めない)。"""
    uri = "saiverse://audio/same.ogg"
    meta = {"tags": ["conversation"], "source": "stackchan_voice",
            "media": [{"type": "audio", "uri": uri}]}
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content="x", raw_meta=json.dumps(meta))
    raw_escaped = (
        '{"tags": ["conversation"], "media": [{"type": "audio", "uri": "' + escaped_uri + '"}]}'
    )
    assert json.loads(raw_escaped)["media"][0]["uri"] == uri
    _insert_building_row(main, seq=2, content="x", raw_meta=raw_escaped, cmid="cmid-2")

    with BuildingSourceRows(main) as source_rows:
        assert source_rows.sent_from_screen(meta) is None
        assert mig._voice_source_reason(source_rows, meta) == VOICE_SOURCE_UNKNOWN_REASON


def test_a_non_ascii_uri_saved_with_escapes_is_found(tmp_path):
    uri = "saiverse://audio/音声.ogg"
    meta = {"source": "stackchan_voice", "media": [{"type": "audio", "uri": uri}]}
    main = _make_main_db(tmp_path / "saiverse.db")
    # ensure_ascii=True の保存 (\\u で書かれた uri)
    _insert_building_row(main, seq=1, content="x", raw_meta=json.dumps(meta, ensure_ascii=True))

    with BuildingSourceRows(main) as source_rows:
        assert source_rows.sent_from_screen(meta) is False


def test_a_non_audio_item_with_the_same_uri_is_not_a_candidate(tmp_path):
    uri = "saiverse://audio/x.ogg"
    meta = {"source": "stackchan_voice", "media": [{"type": "audio", "uri": uri}]}
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content="x", raw_meta=json.dumps(meta))
    _insert_building_row(
        main, seq=2, content="x", cmid="cmid-2",
        raw_meta=json.dumps({"media": [{"type": "image", "uri": uri}]}),
    )

    with BuildingSourceRows(main) as source_rows:
        assert source_rows.sent_from_screen(meta) is False


# ---------------------------------------------------------------------------
# 9. cutover.json は、固いリンクを作れないときも完成した中身だけを公開する
# ---------------------------------------------------------------------------


def test_without_hard_links_an_interrupted_write_leaves_no_official_record(tmp_path, monkeypatch):
    path = _output_dir(tmp_path) / CUTOVER_FILE_NAME
    real_open = open

    def no_link(*_args, **_kwargs):
        raise OSError("hard links are not supported here")

    class _Interrupted:
        """正式な名前に書き始めて、途中で止まるファイル。"""

        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

        def write(self, text):
            self._fh.write(text[:5])
            self._fh.flush()
            raise OSError("interrupted")

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def interrupted_open(target, *args, **kwargs):
        fh = real_open(target, *args, **kwargs)
        if Path(target) == path:
            return _Interrupted(fh)
        return fh

    monkeypatch.setattr(mig.os, "link", no_link)
    monkeypatch.setattr(mig, "open", interrupted_open, raising=False)
    cutover = mig.Cutover(CUTOVER, mig.CUTOVER_FROM_START)

    try:
        recorded = mig._record_cutover(path, cutover, saiverse_home=tmp_path)
    except CutoverUnavailable:
        recorded = None
    # 正式な名前には、完成した記録があるか、何も無いかのどちらか
    if path.exists():
        assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == CUTOVER
        assert recorded is not None and recorded.at == CUTOVER
    assert not list(path.parent.glob("*.tmp"))


def test_without_hard_links_an_existing_record_is_not_overwritten(tmp_path, monkeypatch):
    path = _output_dir(tmp_path) / CUTOVER_FILE_NAME
    _record_cutover(tmp_path, CUTOVER)

    def no_link(*_args, **_kwargs):
        raise OSError("hard links are not supported here")

    monkeypatch.setattr(mig.os, "link", no_link)
    later = mig.Cutover(CUTOVER + timedelta(days=1), mig.CUTOVER_FROM_START)

    recorded = mig._record_cutover(path, later, saiverse_home=tmp_path)

    assert recorded.at == CUTOVER
    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == CUTOVER
    assert not list(path.parent.glob("*.tmp"))
