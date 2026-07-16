from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from saiverse import runtime_marker
from scripts import snapshot
from database.backup import backup_saiverse_db, restore_saiverse_db_backup


def _create_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES (?)", (value,))
        connection.commit()


def _read_db(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT value FROM state").fetchone()
    assert row is not None
    return str(row[0])


def test_runtime_markers_allow_distinct_cities_but_reject_duplicate(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    db_path = home / "user_data" / "database" / "saiverse.db"
    with patch.object(runtime_marker, "get_saiverse_home", return_value=home), patch.object(
        runtime_marker,
        "_process_create_time",
        return_value=123.0,
    ):
        first = runtime_marker.acquire_runtime_marker(
            city_name="city_a",
            db_path=db_path,
            argv=["main.py", "city_a"],
        )
        second = runtime_marker.acquire_runtime_marker(
            city_name="city_b",
            db_path=db_path,
            argv=["main.py", "city_b"],
        )

        assert runtime_marker.marker_status()[0] == "running"
        assert len(list((home / ".runtime").glob("*.json"))) == 2
        with pytest.raises(RuntimeError, match="duplicate City"):
            runtime_marker.acquire_runtime_marker(
                city_name="city_a",
                db_path=db_path,
                argv=["main.py", "city_a"],
            )

        runtime_marker.release_runtime_marker(first)
        assert runtime_marker.marker_status()[0] == "running"
        runtime_marker.release_runtime_marker(second)
        assert runtime_marker.marker_status()[0] == "stopped"


def test_world_snapshot_roundtrip_preserves_persona_backup_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    snap_dir = home / "snapshots"
    main_db = home / "user_data" / "database" / "saiverse.db"
    memory_db = home / "personas" / "air" / "memory.db"
    backup = home / "backups" / "saimemory_simple" / "air" / "generation.bak"
    _create_db(main_db, "world-before")
    _create_db(memory_db, "memory-before")
    snap_dir.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    backup.write_text("independent-persona-backup", encoding="utf-8")

    with patch.object(snapshot, "saiverse_home", return_value=home), patch.object(
        snapshot,
        "snapshots_dir",
        return_value=snap_dir,
    ), patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        assert snapshot.cmd_save(
            argparse.Namespace(name="stable", note="test", force=False)
        ) == 0

        with closing(sqlite3.connect(main_db)) as connection:
            connection.execute("UPDATE state SET value = 'world-after'")
            connection.commit()
        with closing(sqlite3.connect(memory_db)) as connection:
            connection.execute("UPDATE state SET value = 'memory-after'")
            connection.commit()
        backup.write_text("newer-independent-backup", encoding="utf-8")

        assert snapshot.cmd_restore(
            argparse.Namespace(name="stable", yes=True, no_auto_snapshot=True)
        ) == 0

    assert _read_db(main_db) == "world-before"
    assert _read_db(memory_db) == "memory-before"
    assert backup.read_text(encoding="utf-8") == "newer-independent-backup"


def test_snapshot_validation_failure_does_not_mutate_current_world(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    snap_dir = home / "snapshots"
    main_db = home / "user_data" / "database" / "saiverse.db"
    _create_db(main_db, "current")
    snap_dir.mkdir(parents=True)
    (snap_dir / "broken.zip").write_bytes(b"not a zip")

    with patch.object(snapshot, "saiverse_home", return_value=home), patch.object(
        snapshot,
        "snapshots_dir",
        return_value=snap_dir,
    ), patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        assert snapshot.cmd_restore(
            argparse.Namespace(name="broken", yes=True, no_auto_snapshot=True)
        ) == 1

    assert _read_db(main_db) == "current"


def test_runtime_files_are_not_snapshot_payload(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".runtime").mkdir(parents=True)
    (home / ".runtime" / "city.json").write_text("{}", encoding="utf-8")
    (home / ".runtime.json").write_text("{}", encoding="utf-8")
    (home / "state.txt").write_text("state", encoding="utf-8")

    with patch.object(snapshot, "saiverse_home", return_value=home):
        names = {entry.archive_path for entry in snapshot.collect_files_to_snapshot()}

    assert names == {"state.txt"}


def test_validated_main_db_backup_has_supported_stopped_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "saiverse.db"
    _create_db(db_path, "before")
    backup = backup_saiverse_db(db_path, keep_count=10, kind="pre_upgrade")
    assert backup is not None
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("UPDATE state SET value = 'after'")
        connection.commit()

    with patch("saiverse.runtime_marker.marker_status", return_value=("stopped", "test")):
        safety = restore_saiverse_db_backup(backup, db_path)

    assert safety is not None
    assert "pre_restore" in safety.name
    assert _read_db(db_path) == "before"


def test_main_db_restore_refuses_running_process_without_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "saiverse.db"
    _create_db(db_path, "current")
    backup = backup_saiverse_db(db_path, keep_count=10, kind="pre_upgrade")
    assert backup is not None

    with patch(
        "saiverse.runtime_marker.marker_status",
        return_value=("running", "verified process"),
    ):
        with pytest.raises(RuntimeError, match="requires stopped"):
            restore_saiverse_db_backup(backup, db_path)

    assert _read_db(db_path) == "current"


def test_backup_retention_is_separate_per_generation_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "saiverse.db"
    _create_db(db_path, "state")
    for _ in range(2):
        backup_saiverse_db(db_path, keep_count=1, kind="startup")
        backup_saiverse_db(db_path, keep_count=1, kind="pre_upgrade")

    assert len(list(tmp_path.glob("saiverse.db_backup_startup_*.bak"))) == 1
    assert len(list(tmp_path.glob("saiverse.db_backup_pre_upgrade_*.bak"))) == 1
