"""Regression coverage for the first mechanical audit-fix batch."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from packaging.version import Version
from sqlalchemy import create_engine, text

from database.backup import backup_saiverse_db
from database.migrate import (
    _backfill_desire_stage_normalization,
    _migrate_track_tasks_json_to_persona_task,
    migrate_database_in_place,
)
from saiverse.upgrade import _run_handlers_for_entity, parse_version
from scripts import snapshot


def test_backup_names_do_not_collide_and_connections_are_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "saiverse.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('kept')")
        conn.commit()

    fixed_now = datetime(2026, 7, 16, 12, 34, 56, 123456)
    with patch("database.backup.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = fixed_now
        first = backup_saiverse_db(db_path, keep_count=10)
        second = backup_saiverse_db(db_path, keep_count=10)

    assert first is not None and second is not None
    assert first != second
    assert first.exists() and second.exists()
    with closing(sqlite3.connect(second)) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone() == ("kept",)

    # Windows fails these renames while sqlite handles are still open.
    db_path.rename(tmp_path / "saiverse-renamed.db")
    first.rename(tmp_path / "backup-renamed.bak")


def test_full_rewrite_failure_rolls_back_and_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "saiverse.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('original')")
        conn.commit()

    with patch("database.migrate.apply_known_column_renames"), patch(
        "database.migrate.Base.metadata.create_all",
        side_effect=RuntimeError("forced create failure"),
    ):
        with pytest.raises(RuntimeError, match="ロールバックしました"):
            migrate_database_in_place(str(db_path))

    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone() == ("original",)


def test_explicit_missing_migration_target_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        migrate_database_in_place(str(tmp_path / "missing.db"))


def test_cli_explicit_missing_migration_target_exits_nonzero(tmp_path: Path) -> None:
    missing = tmp_path / "missing-cli.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "database.migrate",
            "--db",
            str(missing),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not missing.exists()


def test_malformed_legacy_track_tasks_abort_conversion(tmp_path: Path) -> None:
    source = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    try:
        with source.begin() as conn:
            conn.execute(text(
                "CREATE TABLE action_track ("
                "track_id TEXT, persona_id TEXT, tasks_json TEXT)"
            ))
            conn.execute(text(
                "INSERT INTO action_track VALUES ('t1', 'p1', '{broken')"
            ))
        with target.begin() as conn:
            conn.execute(text("CREATE TABLE persona_task (track_id TEXT)"))

        with pytest.raises(ValueError, match="tasks_json"):
            _migrate_track_tasks_json_to_persona_task(source, target)
    finally:
        source.dispose()
        target.dispose()


def test_required_desire_backfill_propagates_failure() -> None:
    engine = MagicMock()
    engine.begin.side_effect = RuntimeError("forced backfill failure")
    with pytest.raises(RuntimeError, match="forced backfill failure"):
        _backfill_desire_stage_normalization(engine)


def test_future_entity_version_is_rejected_without_db_mutation() -> None:
    session = MagicMock()
    entity = SimpleNamespace(LAST_KNOWN_VERSION="9.0.0")

    assert not _run_handlers_for_entity(
        session,
        scope="ai",
        entity=entity,
        entity_id="future-ai",
        target=Version("1.0.0"),
    )
    assert parse_version(entity.LAST_KNOWN_VERSION) == Version("9.0.0")
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_snapshot_replace_failure_preserves_previous_archive(tmp_path: Path) -> None:
    home = tmp_path / "home"
    snap_dir = home / "snapshots"
    snap_dir.mkdir(parents=True)
    source_file = home / "state.txt"
    source_file.write_text("new state", encoding="utf-8")
    old_archive = snap_dir / "stable.zip"
    old_archive.write_bytes(b"old archive")

    args = argparse.Namespace(name="stable", note="", force=True)
    with patch.object(snapshot, "saiverse_home", return_value=home), patch.object(
        snapshot, "snapshots_dir", return_value=snap_dir
    ), patch.object(
        snapshot, "is_saiverse_running", return_value=(False, "")
    ), patch.object(
        snapshot,
        "collect_files_to_snapshot",
        return_value=[snapshot.SnapshotEntry(source_file, "state.txt")],
    ), patch.object(snapshot.os, "replace", side_effect=OSError("publish failed")):
        assert snapshot.cmd_save(args) == 1

    assert old_archive.read_bytes() == b"old archive"
    assert not (snap_dir / "stable.zip.tmp").exists()
