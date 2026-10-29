"""システム通知の移行の、敵対レビュー 6 巡目の指摘への回帰試験 (database/system_notice_migration.py)。

docs/intent/system_notice_delivery.md「移行」と「追補」。合成した一時 DB だけを使う。固定すること:

- :func:`resolve_cutover` が境の候補を集める途中で失敗したとき、本体 DB の
  ``execution_ledger`` に残す時刻は、失敗した時点までに読めた候補と起動の時刻のうち最も
  早いもの。既に残した行の時刻より早ければ、その行を早い時刻に書き換える。
- 規則 5 で分けた音声だけの role=user の行は、元の行の ``MECHANISM_TAGS`` を持たず、
  line_role が ``CHRONICLE_EXCLUDED_LINE_ROLES`` なら ``main_line`` にする。外したものは
  ``REVIEW_CLEARED_KEY`` に残す (saiverse/system_notice_review.py の ACTION_USER と同じ)。
  通知の行 (元の行) の tags はそのまま。分けた音声の行は ``filter_real_conversation_ids``
  に入る。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database import system_notice_migration as mig
from database.system_notice_migration import (
    AFTER_CUTOVER_REASON,
    VOICE_INPUT_NOTICE_KIND,
    BuildingSourceRows,
    CutoverUnavailable,
    migrate_persona_db,
    run_migration,
)
from sai_memory.memory.storage import (
    SYSTEM_NOTICE_KEY,
    SYSTEM_NOTICE_REVIEW_KEY,
    filter_real_conversation_ids,
)
from saiverse.system_notice_review import REVIEW_CLEARED_KEY
from tests.test_system_notice_migration_round2 import (
    _TOOL_ROW,
    _VOICE_CONTENT,
    _VOICE_TEXT,
    _insert_building_row,
    _make_main_db,
    _make_persona,
    _meta,
    _output_dir,
    _recorded,
    _rows,
    _set_marker,
    _voice_meta,
    _write_run_trace,
)
from tests.test_system_notice_migration_round5 import _at, _attempt_rows

T0 = datetime(2026, 10, 29, 1, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=2)
T2 = T0 + timedelta(hours=4)
T3 = T0 + timedelta(hours=6)
#: T0 と T1 の間 (最古の印の後、失敗した起動の前) に作られた行。
BETWEEN = int((T0 + timedelta(minutes=30)).timestamp())


def _attempt_time(main: Path) -> datetime:
    ((_kind, _key, _status, payload),) = _attempt_rows(main)
    return datetime.fromisoformat(json.loads(payload)["cutover"])


def _break_db(path: Path) -> None:
    path.write_bytes(b"this is not a database" * 100)


# ---------------------------------------------------------------------------
# 1. 候補を一部読んだ後の失敗でも、読めた最も早い時刻を残す
# ---------------------------------------------------------------------------


def test_a_failure_after_reading_the_marker_leaves_the_marker_time(tmp_path, monkeypatch):
    """後続の候補 (移行の記録) の読み取りの失敗 → 最古の印を持つ記憶の削除 → 復旧後の再走査。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    old = _make_persona(tmp_path, [], persona_id="a_old")
    _set_marker(old, f"v3 {T0.isoformat()}")
    said = _make_persona(tmp_path, [("said_after", *_TOOL_ROW, BETWEEN)], persona_id="b_new")
    before = _rows(said)
    run_dir = _write_run_trace(tmp_path, T2)
    (run_dir / "RUN").write_text("{broken", encoding="utf-8")

    _at(monkeypatch, T1)
    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=main, saiverse_home=tmp_path)
    assert _rows(said) == before
    assert _attempt_time(main) == T0

    shutil.rmtree(old.parent)  # 最古の印を持つ記憶を消す
    shutil.rmtree(run_dir)  # 読めなかった記録の復旧
    _at(monkeypatch, T3)
    (_main_result, persona_result), _report = run_migration(db_path=main, saiverse_home=tmp_path)

    recorded = _recorded(tmp_path)
    assert datetime.fromisoformat(recorded["cutover"]) == T0
    assert recorded["source"] == mig.CUTOVER_FROM_ATTEMPT
    assert _rows(said)["said_after"][:2] == before["said_after"][:2]
    assert _meta(said, "said_after")[SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON
    assert [s.reason for s in persona_result.skipped] == [AFTER_CUTOVER_REASON]


def test_a_failure_on_a_later_memory_leaves_the_earlier_marker_time(tmp_path):
    """記憶 DB を順に読む途中で、後の記憶 DB を読めなかったとき。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    old = _make_persona(tmp_path, [], persona_id="a_old")
    _set_marker(old, f"v3 {T0.isoformat()}")
    broken = _make_persona(tmp_path, [], persona_id="b_broken")
    _break_db(broken)

    with pytest.raises(CutoverUnavailable):
        mig.resolve_cutover(tmp_path, now=T1, main_db=main)
    assert _attempt_time(main) == T0

    shutil.rmtree(old.parent)
    shutil.rmtree(broken.parent)
    cutover = mig.resolve_cutover(tmp_path, now=T3, main_db=main)
    assert (cutover.at, cutover.source) == (T0, mig.CUTOVER_FROM_ATTEMPT)


def test_a_failure_on_a_later_trace_leaves_the_earlier_trace_time(tmp_path, monkeypatch):
    """移行の記録を順に読む途中で、後の記録を読めなかったとき。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    _make_persona(tmp_path, [])
    _write_run_trace(tmp_path, T0)  # 結果のフォルダは移行前の写しより先に読む
    copy = _output_dir(tmp_path) / "saiverse.db.pre_migration_x.bak"
    copy.write_bytes(b"x")
    real_written_at = mig._file_written_at

    def _fail_on_copy(path):
        if path == copy:
            raise CutoverUnavailable("stat failed")
        return real_written_at(path)

    monkeypatch.setattr(mig, "_file_written_at", _fail_on_copy)
    with pytest.raises(CutoverUnavailable):
        mig.resolve_cutover(tmp_path, now=T1, main_db=main)
    assert _attempt_time(main) == T0


def test_a_failure_before_any_candidate_leaves_the_start_time(tmp_path):
    """何も読めないうちに止まったら、今までどおり起動の時刻を残す。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    broken = _make_persona(tmp_path, [], persona_id="a_broken")
    _break_db(broken)

    with pytest.raises(CutoverUnavailable):
        mig.resolve_cutover(tmp_path, now=T1, main_db=main)
    assert _attempt_time(main) == T1


def test_a_candidate_later_than_the_start_does_not_move_the_left_time_later(tmp_path):
    """読めた候補が起動の時刻より遅ければ、起動の時刻を残す。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    late = _make_persona(tmp_path, [], persona_id="a_late")
    _set_marker(late, f"v3 {T3.isoformat()}")
    broken = _make_persona(tmp_path, [], persona_id="b_broken")
    _break_db(broken)

    with pytest.raises(CutoverUnavailable):
        mig.resolve_cutover(tmp_path, now=T1, main_db=main)
    assert _attempt_time(main) == T1


def test_an_earlier_time_found_later_replaces_the_left_time(tmp_path):
    """一度目は最古の印を読む前に止まり、二度目は読んだ後に止まった。早いほうを残す。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    first = _make_persona(tmp_path, [], persona_id="a_first")
    old = _make_persona(tmp_path, [], persona_id="b_old")
    _set_marker(old, f"v3 {T0.isoformat()}")
    healthy = first.read_bytes()
    _break_db(first)

    with pytest.raises(CutoverUnavailable):
        mig.resolve_cutover(tmp_path, now=T1, main_db=main)
    assert _attempt_time(main) == T1

    first.write_bytes(healthy)
    run_dir = _write_run_trace(tmp_path, T3)
    (run_dir / "RUN").write_text("{broken", encoding="utf-8")
    with pytest.raises(CutoverUnavailable):
        mig.resolve_cutover(tmp_path, now=T2, main_db=main)
    assert _attempt_time(main) == T0

    shutil.rmtree(old.parent)
    shutil.rmtree(run_dir)
    cutover = mig.resolve_cutover(tmp_path, now=T3, main_db=main)
    assert (cutover.at, cutover.source) == (T0, mig.CUTOVER_FROM_ATTEMPT)


def test_a_later_failure_does_not_move_the_left_time_later(tmp_path):
    main = _make_main_db(tmp_path / "saiverse.db")
    broken = _make_persona(tmp_path, [], persona_id="a_broken")
    _break_db(broken)
    for when in (T1, T2):
        with pytest.raises(CutoverUnavailable):
            mig.resolve_cutover(tmp_path, now=when, main_db=main)
    assert _attempt_time(main) == T1


def test_an_unreadable_left_row_is_not_rewritten(tmp_path):
    """時刻を読めない行は、より早い時刻かもしれないので書き換えない (読む側が止め続ける)。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    with closing(sqlite3.connect(str(main))) as conn:
        conn.execute(
            "INSERT INTO execution_ledger (EXECUTION_ID, KIND, IDEMPOTENCY_KEY, PERSONA_ID, "
            "STATUS, PAYLOAD_JSON, RESULT_JSON, ERROR, CREATED_AT, UPDATED_AT) "
            "VALUES ('x', ?, ?, NULL, 'completed', '{broken', NULL, NULL, 0, 0)",
            (mig.CUTOVER_ATTEMPT_LEDGER_KIND, mig.CUTOVER_ATTEMPT_LEDGER_KEY),
        )
        conn.commit()
        assert mig._write_cutover_attempt(conn, mig.Cutover(T0, mig.CUTOVER_FROM_MARKER))
        conn.commit()
    ((_kind, _key, _status, payload),) = _attempt_rows(main)
    assert payload == "{broken"


# ---------------------------------------------------------------------------
# 2. 分けた音声の行は、機構のタグと実会話から外す line_role を持たない
# ---------------------------------------------------------------------------

PRE = int((T0 - timedelta(hours=1)).timestamp())


def _split_voice(tmp_path: Path, meta: dict, line_role):
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content=_VOICE_CONTENT, raw_meta=json.dumps(meta), cmid=None)
    db = _make_persona(tmp_path, [("voice", "user", _VOICE_CONTENT, meta, PRE)])
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("UPDATE messages SET line_role = ? WHERE id = 'voice'", (line_role,))
        conn.commit()
    with BuildingSourceRows(main) as source_rows:
        migrate_persona_db(
            db, persona_id="p1", output_dir=tmp_path / "out", stamp="s",
            playbook_names=frozenset(), source_rows=source_rows, cutover=int(T0.timestamp()),
        )
    rows = _rows(db)
    assert set(rows) - {"voice"}, "音声の行が分けられていない"
    (new_id,) = set(rows) - {"voice"}
    return db, new_id


def _line_role(db: Path, row_id: str):
    with closing(sqlite3.connect(str(db))) as conn:
        return conn.execute("SELECT line_role FROM messages WHERE id = ?", (row_id,)).fetchone()[0]


def _real(db: Path, row_id: str) -> bool:
    with closing(sqlite3.connect(str(db))) as conn:
        return filter_real_conversation_ids(conn, [row_id]) == {row_id}


@pytest.mark.parametrize("line_role", ["sub_line", "meta_judgment", "nested"])
def test_the_split_voice_row_drops_mechanism_tags_and_excluded_line_role(tmp_path, line_role):
    meta = _voice_meta(tags=["conversation", "event_message", "spell"])
    db, new_id = _split_voice(tmp_path, meta, line_role)

    rows = _rows(db)
    assert rows["voice"][:2] == ("system", _VOICE_TEXT)
    notice = json.loads(rows["voice"][2])
    assert notice[SYSTEM_NOTICE_KEY] == VOICE_INPUT_NOTICE_KIND
    assert notice["tags"] == ["conversation", "event_message", "spell"]  # 通知の側は残す
    assert _line_role(db, "voice") == line_role

    voice = json.loads(rows[new_id][2])
    assert rows[new_id][:2] == ("user", "")
    assert voice["tags"] == ["conversation"]
    assert voice["media"] == meta["media"]
    assert voice[REVIEW_CLEARED_KEY] == {
        "tags": ["event_message", "spell"], "line_role": line_role,
    }
    assert _line_role(db, new_id) == "main_line"
    assert _real(db, new_id)


def test_the_split_voice_row_without_marks_to_clear_is_unchanged(tmp_path):
    meta = _voice_meta()
    db, new_id = _split_voice(tmp_path, meta, "main_line")

    voice = json.loads(_rows(db)[new_id][2])
    assert voice["tags"] == ["conversation"]
    assert REVIEW_CLEARED_KEY not in voice
    assert _line_role(db, new_id) == "main_line"
    assert _real(db, new_id)


def test_the_split_voice_row_keeps_a_null_line_role(tmp_path):
    db, new_id = _split_voice(tmp_path, _voice_meta(tags=["event_message"]), None)

    voice = json.loads(_rows(db)[new_id][2])
    assert voice["tags"] == []
    assert voice[REVIEW_CLEARED_KEY] == {"tags": ["event_message"]}
    assert _line_role(db, new_id) is None
    assert _real(db, new_id)
