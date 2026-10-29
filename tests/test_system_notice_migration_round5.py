"""システム通知の移行の、敵対レビュー 5 巡目の指摘への回帰試験 (database/system_notice_migration.py)。

docs/intent/system_notice_delivery.md「移行」と「追補」の「時刻の境」。合成した一時 DB だけを使う。
固定すること:

- ``cutover.json`` を書けなかった起動でも、その起動で決めようとした境の時刻を本体 DB の
  ``execution_ledger`` (KIND :data:`CUTOVER_ATTEMPT_LEDGER_KIND`、IDEMPOTENCY_KEY 固定の一行。
  既にあれば書き換えない) に残し、次の起動の :func:`resolve_cutover` はその時刻も境の候補に
  含める。この行は実行台帳の回復の処理の走査に入らない。
- 照合に使う欄 (``commit_record`` と ``marker_value``) を持たないなど、コミットされたかを
  確かめられない仮の結果の JSON は削除せず、名前の末尾に ``.unverified`` を付けて残し、
  以後の起動では採否の対象にしない。レポートにその事実を一行書く。
- その起動の結果の JSON のうち一つでも読めなければ、全件のレポートと通常のレポートを確定
  せず (``REPORTED`` を置かず)、次の起動で再び試す。仮の結果を採用して作り直すときの
  書き出しの失敗も同じ扱いにする。
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
    OUTPUT_DIR_NAME,
    REPORTED_MARKER_NAME,
    RESULTS_DIR_NAME,
    CutoverUnavailable,
    regenerate_pending_reports,
    run_migration,
)
from sai_memory.memory.storage import SYSTEM_NOTICE_REVIEW_KEY
from tests.test_system_notice_migration_round2 import (
    _TOOL_ROW,
    _make_main_db,
    _make_persona,
    _meta,
    _output_dir,
    _recorded,
    _rows,
)

T1 = datetime(2026, 10, 29, 1, 0, 0, tzinfo=timezone.utc)
T2 = T1 + timedelta(hours=2)
T3 = T1 + timedelta(hours=4)
BETWEEN = int((T1 + timedelta(minutes=30)).timestamp())


_REAL_RESOLVE = mig.resolve_cutover


def _at(monkeypatch, when: datetime) -> None:
    """``run_migration`` が境を決めるときの「いま」を ``when`` にする。"""

    def _resolve(home, *, now, dry_run=False, **kwargs):
        return _REAL_RESOLVE(home, now=when, dry_run=dry_run, **kwargs)

    monkeypatch.setattr(mig, "resolve_cutover", _resolve)


def _block_output_dir(home: Path) -> Path:
    """記録の置き場をファイルで塞ぐ (``cutover.json`` も結果も書けない)。"""
    blocker = _output_dir(home)
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("x", encoding="utf-8")
    return blocker


def _attempt_rows(main: Path):
    with closing(sqlite3.connect(str(main))) as conn:
        return conn.execute(
            "SELECT KIND, IDEMPOTENCY_KEY, STATUS, PAYLOAD_JSON FROM execution_ledger "
            "WHERE KIND = ?", (mig.CUTOVER_ATTEMPT_LEDGER_KIND,),
        ).fetchall()


# ---------------------------------------------------------------------------
# 1. cutover.json を書けなかった起動の時刻を、本体 DB に残して境の候補にする
# ---------------------------------------------------------------------------


def test_a_start_that_could_not_record_the_cutover_still_bounds_the_next_start(
    tmp_path, monkeypatch,
):
    """保存の失敗 → 発言 → 保存先の復旧 → 再起動。発言は境より後として扱う。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    db = _make_persona(tmp_path, [("said_after", *_TOOL_ROW, BETWEEN)])
    before = _rows(db)
    blocker = _block_output_dir(tmp_path)

    _at(monkeypatch, T1)
    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=main, saiverse_home=tmp_path)
    assert _rows(db) == before

    blocker.unlink()  # 保存先の復旧
    _at(monkeypatch, T2)
    (_main_result, persona_result), _report = run_migration(db_path=main, saiverse_home=tmp_path)

    recorded = _recorded(tmp_path)
    assert datetime.fromisoformat(recorded["cutover"]) == T1
    assert recorded["source"] == mig.CUTOVER_FROM_ATTEMPT
    assert _rows(db)["said_after"][:2] == before["said_after"][:2]
    assert _meta(db, "said_after")[SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON
    assert [s.reason for s in persona_result.skipped] == [AFTER_CUTOVER_REASON]


def test_the_first_attempt_is_kept_and_never_rewritten(tmp_path, monkeypatch):
    main = _make_main_db(tmp_path / "saiverse.db")
    _make_persona(tmp_path, [])
    blocker = _block_output_dir(tmp_path)

    for when in (T1, T2):
        _at(monkeypatch, when)
        with pytest.raises(CutoverUnavailable):
            run_migration(db_path=main, saiverse_home=tmp_path)
    ((kind, key, status, payload),) = _attempt_rows(main)
    assert (kind, key, status) == (
        mig.CUTOVER_ATTEMPT_LEDGER_KIND, mig.CUTOVER_ATTEMPT_LEDGER_KEY, "completed",
    )
    assert datetime.fromisoformat(json.loads(payload)["cutover"]) == T1

    blocker.unlink()
    _at(monkeypatch, T3)
    run_migration(db_path=main, saiverse_home=tmp_path)
    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == T1


def test_an_earlier_marker_still_wins_over_the_attempt(tmp_path, monkeypatch):
    """最も早い時刻を取る規則のまま (残した時刻は候補の一つ)。"""
    from tests.test_system_notice_migration_round2 import _set_marker

    main = _make_main_db(tmp_path / "saiverse.db")
    db = _make_persona(tmp_path, [])
    _block_output_dir(tmp_path).unlink()
    with closing(sqlite3.connect(str(main))) as conn:
        mig._write_cutover_attempt(conn, mig.Cutover(T2, mig.CUTOVER_FROM_START))
        conn.commit()
    _set_marker(db, f"v3 {T1.isoformat()}")

    _at(monkeypatch, T3)
    run_migration(db_path=main, saiverse_home=tmp_path)
    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == T1
    assert _recorded(tmp_path)["source"] == mig.CUTOVER_FROM_MARKER


def test_without_a_ledger_table_nothing_is_left_and_the_start_time_is_used(tmp_path, monkeypatch):
    """本体 DB に execution_ledger が無いときは今までどおり (残せない)。"""
    main = tmp_path / "saiverse.db"
    with closing(sqlite3.connect(str(main))) as conn:
        conn.execute("CREATE TABLE other (x)")
        conn.commit()
    _make_persona(tmp_path, [])
    blocker = _block_output_dir(tmp_path)

    _at(monkeypatch, T1)
    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=main, saiverse_home=tmp_path)
    blocker.unlink()
    _at(monkeypatch, T2)
    run_migration(db_path=main, saiverse_home=tmp_path)
    assert datetime.fromisoformat(_recorded(tmp_path)["cutover"]) == T2
    assert _recorded(tmp_path)["source"] == mig.CUTOVER_FROM_START


def test_an_unreadable_main_db_before_the_cutover_is_decided_converts_nothing(tmp_path):
    """本体 DB に残した時刻を読み落とすと境が遅く決まるので、読めなければ何も変換しない。"""
    main = tmp_path / "saiverse.db"
    main.write_bytes(b"this is not a database" * 100)
    db = _make_persona(tmp_path, [("old_tool", *_TOOL_ROW, BETWEEN)])
    before = _rows(db)

    with pytest.raises(CutoverUnavailable):
        run_migration(db_path=main, saiverse_home=tmp_path)
    assert _rows(db) == before
    assert not (_output_dir(tmp_path) / mig.CUTOVER_FILE_NAME).exists()


def test_a_dry_run_reads_the_attempt_but_writes_nothing(tmp_path, monkeypatch):
    main = _make_main_db(tmp_path / "saiverse.db")
    _make_persona(tmp_path, [])
    cutover = mig.resolve_cutover(tmp_path, now=T3, dry_run=True, main_db=main)
    assert cutover.at == T3
    assert _attempt_rows(main) == []

    with closing(sqlite3.connect(str(main))) as conn:
        mig._write_cutover_attempt(conn, mig.Cutover(T1, mig.CUTOVER_FROM_START))
        conn.commit()
    cutover = mig.resolve_cutover(tmp_path, now=T3, dry_run=True, main_db=main)
    assert (cutover.at, cutover.source) == (T1, mig.CUTOVER_FROM_ATTEMPT)
    assert not (_output_dir(tmp_path) / mig.CUTOVER_FILE_NAME).exists()


def test_the_attempt_row_stays_out_of_every_ledger_recovery_scan(tmp_path):
    """台帳の読み手は STATUS (と KIND の前方一致) で絞るので、completed のこの行は走査に入らない。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from saiverse.execution_ledger import ExecutionLedger

    main = _make_main_db(tmp_path / "saiverse.db")
    with closing(sqlite3.connect(str(main))) as conn:
        mig._write_cutover_attempt(conn, mig.Cutover(T1, mig.CUTOVER_FROM_START))
        conn.commit()
    engine = create_engine(f"sqlite:///{main}")
    try:
        ledger = ExecutionLedger(session_factory=sessionmaker(bind=engine))
        prefix = "system_notice_migration"
        assert ledger.list_prepared(prefix) == []
        assert ledger.list_running(prefix) == []
        assert ledger.list_failed(prefix) == []
        assert ledger.list_unknown() == []
        assert ledger.sweep_applied(kind_prefix=prefix) == []
        assert ledger.recover_stale_running(all_running=True) == []
    finally:
        engine.dispose()
    ((_kind, _key, status, _payload),) = _attempt_rows(main)
    assert status == "completed"


# ---------------------------------------------------------------------------
# 2. 照合できない仮の結果は捨てずに .unverified で残す
# ---------------------------------------------------------------------------


def _run_dir(home: Path, name: str = "20261028_000000_1_abc") -> Path:
    run_dir = _output_dir(home) / RESULTS_DIR_NAME / name
    run_dir.mkdir(parents=True)
    return run_dir


def test_a_staged_result_without_the_fields_to_check_is_kept_as_unverified(tmp_path):
    """v0.3.27 が本体 DB の分として書いた仮の JSON (commit_record を持たない)。"""
    main = _make_main_db(tmp_path / "saiverse.db")
    run_dir = _run_dir(tmp_path)
    staged = run_dir / "main.json.tmp"
    staged.write_text(json.dumps(mig.StoreResult(
        label="本体 DB", path=str(main),
        changes=[mig.Change("building_messages", "1", "schedule", "<system>a</system>", "a")],
    ).to_json(), ensure_ascii=False), encoding="utf-8")
    original = staged.read_bytes()

    (report,) = regenerate_pending_reports(tmp_path)

    unverified = run_dir / f"main.json.tmp{mig.UNVERIFIED_SUFFIX}"
    assert not staged.exists()
    assert unverified.read_bytes() == original
    assert unverified.name in report.read_text(encoding="utf-8")
    assert (run_dir / REPORTED_MARKER_NAME).exists()
    # 以後の起動では採否の対象にしない (レポートも作り直さない)
    assert regenerate_pending_reports(tmp_path) == []
    assert unverified.read_bytes() == original


def test_a_staged_result_with_a_record_but_no_db_path_is_kept_as_unverified(tmp_path):
    run_dir = _run_dir(tmp_path)
    staged = run_dir / "main.json.tmp"
    data = mig.StoreResult(label="本体 DB", path="x", commit_record=run_dir.name).to_json()
    del data["path"]
    staged.write_text(json.dumps(data), encoding="utf-8")

    regenerate_pending_reports(tmp_path)

    assert not staged.exists()
    assert (run_dir / f"main.json.tmp{mig.UNVERIFIED_SUFFIX}").exists()


def test_an_unverified_persona_result_does_not_hold_the_marker(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / f"persona_p1.json.tmp{mig.UNVERIFIED_SUFFIX}").write_text("{}", encoding="utf-8")
    assert mig._unsettled_staged_persona_ids(_output_dir(tmp_path)) == frozenset()


# ---------------------------------------------------------------------------
# 3. 結果の JSON を一つでも読めなければ、レポートを確定しない
# ---------------------------------------------------------------------------


def _write_result(run_dir: Path, key: str, result: mig.StoreResult) -> Path:
    path = run_dir / f"{key}.json"
    path.write_text(json.dumps(result.to_json(), ensure_ascii=False), encoding="utf-8")
    return path


def _two_results(home: Path) -> Path:
    run_dir = _run_dir(home)
    _write_result(run_dir, "main", mig.StoreResult(
        label="本体 DB", path="saiverse.db",
        changes=[mig.Change("building_messages", "1", "schedule", "<system>main</system>", "main")],
    ))
    _write_result(run_dir, "persona_p1", mig.StoreResult(
        label="ペルソナ p1", path="memory.db", persona=True,
        changes=[mig.Change("messages", "m1", "schedule", "<system>p1 の行</system>", "p1 の行")],
    ))
    return run_dir


def test_an_unreadable_result_keeps_the_run_unreported_until_it_can_be_read(tmp_path, monkeypatch):
    run_dir = _two_results(tmp_path)
    persona = run_dir / "persona_p1.json"
    real_read_text = Path.read_text

    def _flaky(self, *args, **kwargs):
        if self == persona:
            raise PermissionError("locked by another process")
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", _flaky)
        assert regenerate_pending_reports(tmp_path) == []
    assert not (run_dir / REPORTED_MARKER_NAME).exists()
    assert not list(_output_dir(tmp_path).glob("report_*.md"))
    assert not list(_output_dir(tmp_path).glob("changes_*.md"))

    (report,) = regenerate_pending_reports(tmp_path)
    assert (run_dir / REPORTED_MARKER_NAME).exists()
    changes = mig.changes_path_for(report).read_text(encoding="utf-8")
    assert "p1 の行" in changes and "main" in changes


def test_an_unreadable_result_of_this_start_leaves_its_report_to_the_next_start(
    tmp_path, monkeypatch,
):
    """その起動のレポートも同じ (読めた分だけでレポートを書いて REPORTED を置かない)。"""
    old = int((T1 - timedelta(days=1)).timestamp())
    db = _make_persona(tmp_path, [("old_tool", *_TOOL_ROW, old)])
    _make_persona(tmp_path, [("old_tool", *_TOOL_ROW, old)], persona_id="p2")
    real_read_text = Path.read_text

    def _flaky(self, *args, **kwargs):
        if self.name == "persona_p1.json" and self.parent.parent.name == RESULTS_DIR_NAME:
            raise PermissionError("locked by another process")
        return real_read_text(self, *args, **kwargs)

    _at(monkeypatch, T1)
    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", _flaky)
        results, report = run_migration(db_path=None, saiverse_home=tmp_path)
    assert all(r.changes for r in results) and len(results) == 2
    assert report is None
    assert _rows(db)["old_tool"][0] == "system"
    (run_dir,) = (_output_dir(tmp_path) / RESULTS_DIR_NAME).iterdir()
    assert not (run_dir / REPORTED_MARKER_NAME).exists()
    assert not list(_output_dir(tmp_path).glob("report_*.md"))

    (regenerated,) = regenerate_pending_reports(tmp_path)
    text = regenerated.read_text(encoding="utf-8")
    assert "ペルソナ p1" in text and "ペルソナ p2" in text
    assert (run_dir / REPORTED_MARKER_NAME).exists()


def test_a_failed_rewrite_after_taking_up_a_staged_result_is_retried(tmp_path, monkeypatch):
    """報告済みの起動へ仮の結果を採用して作り直すとき、書き出しの失敗で古い REPORTED を残さない。"""
    from tests.test_system_notice_migration import _make_persona_db

    db = tmp_path / "personas" / "p1" / "memory.db"
    db.parent.mkdir(parents=True)
    _make_persona_db(db)
    with monkeypatch.context() as m:
        m.setattr(mig, "_publish_staged_result", lambda staged, result: None)
        run_migration(db_path=None, saiverse_home=tmp_path)
    (run_dir,) = (_output_dir(tmp_path) / RESULTS_DIR_NAME).iterdir()
    (staged,) = run_dir.glob("*.json.tmp")
    # 同じ起動の別の DB の結果でレポートを書き終えていた (REPORTED がある) 形にする
    _write_result(run_dir, "main", mig.StoreResult(
        label="本体 DB", path="saiverse.db",
        changes=[mig.Change("building_messages", "1", "schedule", "<system>main</system>", "main")],
    ))
    (run_dir / REPORTED_MARKER_NAME).write_text("old report", encoding="utf-8")

    def _fail(*_a, **_k):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(mig, "render_report", _fail)
        assert regenerate_pending_reports(tmp_path) == []
    assert not staged.exists()
    assert (run_dir / "persona_p1.json").exists()  # 採用は済んだ
    assert not (run_dir / REPORTED_MARKER_NAME).exists()

    (report,) = regenerate_pending_reports(tmp_path)
    text = report.read_text(encoding="utf-8")
    assert "ペルソナ p1" in text and "本体 DB" in text
    assert (run_dir / REPORTED_MARKER_NAME).read_text(encoding="utf-8") == str(report)
