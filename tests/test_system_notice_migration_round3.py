"""システム通知の移行の、敵対レビュー 3 巡目の指摘への回帰試験 (database/system_notice_migration.py)。

docs/intent/system_notice_delivery.md「追補」の「移行の規則の第二版」(規則 5 の出どころ) と
「時刻の境」。合成した一時 DB だけを使う。固定すること:

- 音声の uri で元の行を引くとき、本体 DB の role=user の行に ``metadata_json`` を読めない
  行が一つでもあれば、その回の uri での引き当ては全部「決められない」にする
  (読めない行が元の発言だった可能性を除けない)。``building_msg_ref`` で引く行は変わらない。
- ``RUN`` を書く前の版 (938ec0f8) が書いた結果のフォルダ (``RUN`` が無く、結果の JSON
  だけがある) は、フォルダ名の起動の時刻 (読めなければ JSON の更新時刻) を境の候補に
  する。``RUN`` があって壊れているフォルダは今までどおり境を決めない。
- 固いリンクを作れない POSIX では、``cutover.json`` の公開を ``O_CREAT | O_EXCL`` で作る
  ロックのファイルの中で行う。存在の確認と公開の間に別のプロセスが割り込んでも、先に
  公開した記録を上書きしない。止まったプロセスが残したロックのファイルは片づける。
- 本体 DB に ``execution_ledger`` が無い (その回の変換の記録を書けない) ときは、本体 DB の
  変換をコミットしない。理由をレポートに書き、画面の警告で知らせる。
- 仮の結果の JSON の採否を決めるとき、記録先の DB が見つからない・読めない場合は捨てずに
  残す。記録が無いと確かめられた場合だけ捨てる。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database import system_notice_migration as mig
from database.system_notice_migration import (
    CUTOVER_FILE_NAME,
    OUTPUT_DIR_NAME,
    RESULTS_DIR_NAME,
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
from tests.test_system_notice_migration_completion import _assert_localized
from tests.test_system_notice_migration_round2 import (
    _insert_building_row,
    _make_main_db,
    _make_persona,
    _output_dir,
    _record_cutover,
    _recorded,
    _rows,
    _sql_time,
    _write_run_trace,
)

CUTOVER = datetime(2026, 10, 28, 5, 0, 0, tzinfo=timezone.utc)
PRE = int((CUTOVER - timedelta(hours=1)).timestamp())
POST = int((CUTOVER + timedelta(hours=1)).timestamp())

_VOICE_TEXT = "ｽﾀｯｸﾁｬﾝから音声入力を受信しました。添付の音声を聴いて応答してください。"
_VOICE_CONTENT = f"<system>{_VOICE_TEXT}</system>"
_TOOL_ROW = ("user", "<system>Email sent.</system>", {"tags": ["tool"]})


# ---------------------------------------------------------------------------
# 1. 読めない建物の行があれば、uri での引き当ては全部「決められない」
# ---------------------------------------------------------------------------


def _voice_meta(name: str, ref=None):
    meta = {
        "tags": ["conversation"], "with": ["user"], "source": "stackchan_voice",
        "media": [{"type": "audio", "uri": f"saiverse://audio/{name}", "mime_type": "audio/ogg"}],
    }
    if ref is not None:
        meta["building_msg_ref"] = ref
    return meta


def _make_voice_persona(home: Path, rows) -> Path:
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


@pytest.mark.parametrize("broken", [
    # 書きかけで切れた JSON (同じ uri の音声を持っていたかもしれない画面からの送信)
    '{"tags": ["conversation"], "media": [{"type": "audio", "uri": "saiverse://audio/same.ogg"',
    # JSON ではあるが辞書でない
    '["saiverse://audio/same.ogg"]',
    # media が一覧でない
    '{"media": "saiverse://audio/same.ogg"}',
])
def test_an_unreadable_building_row_makes_every_uri_lookup_undecidable(tmp_path, broken):
    same = _voice_meta("same.ogg")  # 読める候補が一つだけある
    other = _voice_meta("other.ogg")  # 読める候補が一つだけあり、壊れた行とは uri が違う
    by_ref = _voice_meta("by_ref.ogg", ref="b1:b1:3")  # building_msg_ref で引く行
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content="x", raw_meta=json.dumps(same))
    _insert_building_row(main, seq=2, content="x", raw_meta=json.dumps(other))
    _insert_building_row(main, seq=3, content="x", raw_meta=json.dumps(by_ref))
    _insert_building_row(main, seq=4, content="x", raw_meta=broken, cmid="cmid-4")
    db = _make_voice_persona(tmp_path, [("same", same), ("other", other), ("by_ref", by_ref)])

    with BuildingSourceRows(main) as source_rows:
        result = migrate_persona_db(
            db, persona_id="p1", output_dir=tmp_path / "out", stamp="20261029_000000",
            playbook_names=frozenset(), source_rows=source_rows, cutover=int(CUTOVER.timestamp()),
        )

    rows = _rows(db)
    for row_id, meta in (("same", same), ("other", other)):
        role, content, raw = rows[row_id]
        assert (role, content) == ("user", _VOICE_CONTENT), row_id
        assert json.loads(raw) == {**meta, SYSTEM_NOTICE_REVIEW_KEY: VOICE_SOURCE_UNKNOWN_REASON}
    # building_msg_ref で引く行は、読めない行に左右されず二行に分ける
    role, content, raw = rows["by_ref"]
    assert (role, content) == ("system", _VOICE_TEXT)
    assert json.loads(raw)[SYSTEM_NOTICE_KEY] == VOICE_INPUT_NOTICE_KIND
    assert len(rows) == 4  # 足した音声の行は by_ref の一行の分だけ
    assert sorted((s.row_id, s.reason) for s in result.skipped) == [
        ("other", VOICE_SOURCE_UNKNOWN_REASON), ("same", VOICE_SOURCE_UNKNOWN_REASON),
    ]


def test_rows_with_empty_metadata_do_not_block_uri_lookups(tmp_path):
    """metadata_json の無い行 (NULL・空・JSON の null) は読めない行ではない。"""
    same = _voice_meta("same.ogg")
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content="x", raw_meta=json.dumps(same))
    for seq, raw in ((2, None), (3, ""), (4, "null")):
        _insert_building_row(main, seq=seq, content="x", raw_meta=raw, cmid=f"cmid-{seq}")

    with BuildingSourceRows(main) as source_rows:
        assert source_rows.sent_from_screen(same) is False


# ---------------------------------------------------------------------------
# 2. RUN を書く前の版の結果のフォルダ
# ---------------------------------------------------------------------------


def _write_legacy_run_folder(home: Path, name: str, *, json_written_at: datetime) -> Path:
    """938ec0f8 の版が書いた結果のフォルダ (RUN が無く、結果の JSON だけがある)。"""
    run_dir = _output_dir(home) / RESULTS_DIR_NAME / name
    run_dir.mkdir(parents=True)
    path = run_dir / "persona_gone.json"
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (json_written_at.timestamp(), json_written_at.timestamp()))
    return run_dir


def test_a_legacy_results_folder_uses_the_start_time_in_its_name(tmp_path):
    # フォルダ名の時刻はその起動の地方時 (datetime.now().strftime)
    started_local = datetime(2026, 10, 28, 7, 48, 0)
    name = f"{started_local.strftime('%Y%m%d_%H%M%S')}_4242_abcdef"
    _write_legacy_run_folder(tmp_path, name, json_written_at=CUTOVER + timedelta(days=1))
    db = _make_persona(tmp_path, [("post_tool", *_TOOL_ROW, POST + 86400 * 3)])

    run_migration(db_path=None, saiverse_home=tmp_path)

    expected = started_local.astimezone(timezone.utc)
    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == expected
    assert _recorded(tmp_path)["source"] == mig.CUTOVER_FROM_TRACE
    assert json.loads(_rows(db)["post_tool"][2])[SYSTEM_NOTICE_REVIEW_KEY] == mig.AFTER_CUTOVER_REASON


def test_a_legacy_results_folder_with_an_unreadable_name_uses_the_json_time(tmp_path):
    _write_legacy_run_folder(tmp_path, "renamed_by_hand", json_written_at=CUTOVER)
    _make_persona(tmp_path, [])

    run_migration(db_path=None, saiverse_home=tmp_path)

    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == CUTOVER
    assert _recorded(tmp_path)["source"] == mig.CUTOVER_FROM_TRACE


def test_a_legacy_results_folder_without_any_time_skips_every_conversion(tmp_path, monkeypatch):
    run_dir = _write_legacy_run_folder(tmp_path, "renamed_by_hand", json_written_at=CUTOVER)
    db = _make_persona(tmp_path, [("pre_tool", *_TOOL_ROW, PRE)])
    before = _rows(db)
    real_written_at = mig._file_written_at

    def unreadable(path):
        if path.parent == run_dir:
            raise CutoverUnavailable(f"cannot stat {path}")
        return real_written_at(path)

    monkeypatch.setattr(mig, "_file_written_at", unreadable)

    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=None, saiverse_home=tmp_path)
    assert _rows(db) == before


def test_a_broken_run_file_still_skips_every_conversion(tmp_path):
    """RUN があって壊れているフォルダは、フォルダ名を読めても境を決めない。"""
    run_dir = _write_run_trace(tmp_path, CUTOVER, name="20261028_140000_1_abc")
    (run_dir / "RUN").write_text("{broken", encoding="utf-8")
    _make_persona(tmp_path, [])

    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=None, saiverse_home=tmp_path)
    assert not (_output_dir(tmp_path) / CUTOVER_FILE_NAME).exists()


# ---------------------------------------------------------------------------
# 3. 固いリンクを作れない POSIX での境の記録の公開
# ---------------------------------------------------------------------------


class _PosixOs:
    """``os`` の代わり。名前を posix にし、rename を POSIX の意味 (既存の名前を上書き) にする。

    固いリンクは作れない (ハードリンク非対応のファイルシステム)。
    """

    name = "posix"

    def __init__(self, on_rename=None):
        self._on_rename = on_rename

    def __getattr__(self, attr):
        return getattr(os, attr)

    @staticmethod
    def link(*_args, **_kwargs):
        raise OSError("hard links are not supported here")

    def rename(self, src, dst):
        if self._on_rename is not None:
            hook, self._on_rename = self._on_rename, None
            hook(src, dst)
        os.replace(src, dst)


def _cutover_path(home: Path) -> Path:
    return _output_dir(home) / CUTOVER_FILE_NAME


def _as_posix(monkeypatch, on_rename=None) -> None:
    """この試験の間、移行のコードから見える OS を「固いリンクの無い POSIX」にする。

    フォルダの確定 (:func:`mig._fsync_dir`) は Windows の上では開けないので、何もしない。
    """
    monkeypatch.setattr(mig, "os", _PosixOs(on_rename=on_rename))
    monkeypatch.setattr(mig, "_fsync_dir", lambda directory: None)


def test_two_processes_crossing_before_publishing_keep_the_first_record(tmp_path, monkeypatch):
    """A が存在を確かめて公開する直前に、B が記録を終える (二つのプロセスの交差)。"""
    path = _cutover_path(tmp_path)
    first = mig.Cutover(CUTOVER, mig.CUTOVER_FROM_START)
    second = mig.Cutover(CUTOVER + timedelta(minutes=5), mig.CUTOVER_FROM_START)
    outcome = {}

    def b_records_in_between(src, dst):
        if Path(dst) != path:
            return
        try:
            outcome["b"] = mig._record_cutover(path, second, saiverse_home=tmp_path).at
        except CutoverUnavailable:
            outcome["b"] = None  # A の公開を待ちきれなかった — この起動は何も変換しない

    _as_posix(monkeypatch, on_rename=b_records_in_between)
    monkeypatch.setattr(mig, "_CUTOVER_LOCK_WAIT_SECONDS", 0.05, raising=False)

    a = mig._record_cutover(path, first, saiverse_home=tmp_path).at

    final = datetime.fromisoformat(_recorded(tmp_path)["cutover"])
    # 記録に残った境と、どちらのプロセスが使う境も一致する
    assert final == a
    assert outcome["b"] in (None, final)
    assert not list(path.parent.glob("*.tmp"))
    assert not list(path.parent.glob("*.lock*"))


def test_a_lock_left_by_a_stopped_process_is_cleared(tmp_path, monkeypatch):
    path = _cutover_path(tmp_path)
    path.parent.mkdir(parents=True)
    lock = path.with_name(path.name + mig.CUTOVER_LOCK_SUFFIX)
    # 公開の途中で止まったプロセス (もう居ない pid) が残したロック
    lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "process_created_at": 1.0, "token": "x"}))
    _as_posix(monkeypatch)
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)

    recorded = mig._record_cutover(path, mig.Cutover(CUTOVER, mig.CUTOVER_FROM_START), saiverse_home=tmp_path)

    assert recorded.at == CUTOVER
    assert not lock.exists()
    assert not list(path.parent.glob("*.lock*"))


def test_an_old_lock_whose_owner_cannot_be_read_is_cleared(tmp_path, monkeypatch):
    """作った直後に止まって中身の無いロックは、古くなったら片づける。"""
    path = _cutover_path(tmp_path)
    path.parent.mkdir(parents=True)
    lock = path.with_name(path.name + mig.CUTOVER_LOCK_SUFFIX)
    lock.write_text("")
    old = time.time() - mig._CUTOVER_LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    _as_posix(monkeypatch)

    recorded = mig._record_cutover(path, mig.Cutover(CUTOVER, mig.CUTOVER_FROM_START), saiverse_home=tmp_path)

    assert recorded.at == CUTOVER
    assert not list(path.parent.glob("*.lock*"))


def test_a_fresh_lock_of_a_running_process_is_respected(tmp_path, monkeypatch):
    """走っているプロセスが公開の途中なら、そのロックを奪わず、この起動は何も記録しない。"""
    path = _cutover_path(tmp_path)
    path.parent.mkdir(parents=True)
    lock = path.with_name(path.name + mig.CUTOVER_LOCK_SUFFIX)
    lock.write_text(json.dumps({
        "pid": os.getpid(), "process_created_at": mig._process_created_at(os.getpid()),
        "token": "running",
    }))
    _as_posix(monkeypatch)
    monkeypatch.setattr(mig, "_CUTOVER_LOCK_WAIT_SECONDS", 0.05, raising=False)

    with pytest.raises(CutoverUnavailable):
        mig._record_cutover(path, mig.Cutover(CUTOVER, mig.CUTOVER_FROM_START), saiverse_home=tmp_path)

    assert not path.exists()
    assert json.loads(lock.read_text())["token"] == "running"
    assert not list(path.parent.glob("*.tmp"))


def test_clearing_a_stale_lock_does_not_take_a_lock_made_in_between(tmp_path, monkeypatch):
    """残骸を片づける直前に、別のプロセスが残骸を片づけて新しいロックを作っていた。"""
    path = _cutover_path(tmp_path)
    path.parent.mkdir(parents=True)
    lock = path.with_name(path.name + mig.CUTOVER_LOCK_SUFFIX)
    lock.write_text("")
    old = time.time() - mig._CUTOVER_LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    fresh = json.dumps({"pid": os.getpid(), "process_created_at": None, "token": "fresh"})

    def another_process_recreates(src, dst):
        if Path(src) == lock:
            Path(src).write_text(fresh)

    _as_posix(monkeypatch, on_rename=another_process_recreates)

    assert mig._clear_stale_cutover_lock(lock) is False
    assert lock.read_text() == fresh
    assert not list(path.parent.glob("*.stale"))


def test_a_lock_left_after_publishing_does_not_stop_later_starts(tmp_path, monkeypatch):
    """公開した後、ロックを消す前に止まった — 次の起動は記録を読んで従う。"""
    _record_cutover(tmp_path, CUTOVER)
    path = _cutover_path(tmp_path)
    lock = path.with_name(path.name + mig.CUTOVER_LOCK_SUFFIX)
    lock.write_text(json.dumps({"pid": os.getpid(), "process_created_at": None, "token": "t"}))
    _as_posix(monkeypatch)
    _make_persona(tmp_path, [])

    cutover = mig.resolve_cutover(tmp_path, now=datetime.now(timezone.utc))

    assert cutover.at == CUTOVER


# ---------------------------------------------------------------------------
# 4. その回の記録を書けない本体 DB と、記録先を確かめられない仮の結果
# ---------------------------------------------------------------------------


def _main_db_without_ledger(path: Path) -> Path:
    from sqlalchemy import create_engine

    from database.models import BuildingMessage

    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    BuildingMessage.__table__.create(engine)
    engine.dispose()
    return path


def _building_rows(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        return conn.execute("SELECT seq, content, metadata_json FROM building_messages").fetchall()


def test_a_main_db_without_the_execution_ledger_is_not_converted(tmp_path):
    _record_cutover(tmp_path)
    main = _main_db_without_ledger(tmp_path / "saiverse.db")
    _insert_building_row(
        main, seq=1, content="<system>(音声通話が行われた)</system>",
        raw_meta=json.dumps({"tags": ["internal", "event_message"], "voice_call": True}),
        created_at=_sql_time(PRE),
    )
    before = _building_rows(main)

    (result,), report = run_migration(db_path=main, saiverse_home=tmp_path)

    assert _building_rows(main) == before
    assert result.error and "execution_ledger" in result.error
    assert result.commit_record_unavailable
    assert not result.changes
    assert not list(_output_dir(tmp_path).glob("saiverse.db.pre_migration_*.bak"))
    assert not list((_output_dir(tmp_path) / RESULTS_DIR_NAME).glob("*/*.json.tmp"))
    # レポートに理由が載る
    assert report is not None and "execution_ledger" in report.read_text(encoding="utf-8")
    alerts = startup_alerts([result], report)
    assert [a["id"] for a in alerts] == ["system_notice_migration_commit_record_unavailable"]
    _assert_localized(alerts[0])


def _staged(run_dir: Path, key: str, db: Path, *, persona: bool) -> Path:
    path = run_dir / f"{key}.json.tmp"
    path.write_text(json.dumps({
        "label": key, "path": str(db), "persona": persona, "marker_value": None,
        "commit_record": run_dir.name, "counts": [["messages", "x", 1]],
    }), encoding="utf-8")
    return path


@pytest.mark.parametrize("persona", [True, False])
def test_a_staged_result_whose_db_is_missing_is_kept(tmp_path, persona):
    run_dir = _write_run_trace(tmp_path, CUTOVER)
    (run_dir / "main.json").unlink()
    db = tmp_path / ("personas/p1/memory.db" if persona else "saiverse.db")  # 一時的に見えない
    staged = _staged(run_dir, "persona_p1" if persona else "main", db, persona=persona)

    regenerate_pending_reports(tmp_path)

    assert staged.exists()
    # 次の起動で DB が見え、記録が無いと確かめられたら捨てる
    if persona:
        _make_persona(tmp_path, [])
    else:
        _make_main_db(db)
    regenerate_pending_reports(tmp_path)
    assert not staged.exists()
    assert not list(run_dir.glob("*.json"))


def test_a_staged_result_whose_db_cannot_be_read_is_kept(tmp_path):
    run_dir = _write_run_trace(tmp_path, CUTOVER)
    (run_dir / "main.json").unlink()
    db = tmp_path / "saiverse.db"
    db.write_bytes(b"this is not a database" * 100)
    staged = _staged(run_dir, "main", db, persona=False)

    regenerate_pending_reports(tmp_path)

    assert staged.exists()
