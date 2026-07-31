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


def test_another_running_process_owns_db_detects_foreign_owner(
    tmp_path: Path,
) -> None:
    """同じ DB を所有する「自分以外の」稼働中プロセスの検出 (2026-07-31
    席競合案件・十巡目)。自分のマーカーは所有者に数えない。別 DB のプロセスも
    数えない。"""
    import json as _json
    import os as _os

    home = tmp_path / "home"
    db_path = home / "user_data" / "database" / "saiverse.db"
    with patch.object(runtime_marker, "get_saiverse_home", return_value=home), patch.object(
        runtime_marker,
        "_process_create_time",
        return_value=123.0,
    ):
        token = runtime_marker.acquire_runtime_marker(
            city_name="city_a", db_path=db_path, argv=["main.py", "city_a"],
        )
        # 自分のマーカーしか無ければ所有者なし
        owned, _ = runtime_marker.another_running_process_owns_db(db_path)
        assert owned is False
        # 別プロセスのマーカーを偽装 (pid 違い・同じ db_path)
        foreign = dict(_json.loads(
            runtime_marker._city_marker_path("city_a").read_text(encoding="utf-8")
        ))
        foreign["pid"] = _os.getpid() + 1
        foreign["city_name"] = "city_x"
        runtime_marker._city_marker_path("city_x").write_text(
            _json.dumps(foreign), encoding="utf-8",
        )
        owned, owner = runtime_marker.another_running_process_owns_db(db_path)
        assert owned is True
        assert "city_x" in owner
        # 別 DB を所有するプロセスは対象外
        owned_other, _ = runtime_marker.another_running_process_owns_db(
            home / "elsewhere" / "saiverse.db",
        )
        assert owned_other is False
        runtime_marker.release_runtime_marker(token)


def test_cityname_auto_repair_refused_while_db_is_owned(tmp_path: Path) -> None:
    """CITYNAME 自動修復は、同じ DB を所有する稼働中プロセスがいる間は拒否する。

    runtime marker は City 名でしか二重起動を弾かないため、稼働中の City を
    別名 (`python main.py city_b`) で起動すると、修復が CITYID=1 を改名して
    同一ペルソナ群の 2 プロセス同時運転を作ってしまう (2026-07-31 席競合案件・
    十巡目 high1 の再現固定)。所有者がいなければ従来どおり修復する。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import Base, City, User
    from manager.initialization import InitializationMixin

    db_file = tmp_path / "saiverse.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        db.add(City(USERID=1, CITYNAME="city_a", UI_PORT=3001, API_PORT=8001))
        db.commit()
    finally:
        db.close()

    class _Manager(InitializationMixin):
        def _update_timezone_cache(self, tz):
            pass

    m = _Manager()
    m.SessionLocal = Session
    m.db_path = str(db_file)

    with patch(
        "saiverse.runtime_marker.another_running_process_owns_db",
        return_value=(True, "verified SAIVerse City 'city_a' process pid 999"),
    ):
        with pytest.raises(ValueError, match="Refusing CITYNAME auto-repair"):
            m._init_city_config("city_b")
    db = Session()
    try:
        assert db.query(City).filter(City.CITYID == 1).first().CITYNAME == "city_a"
    finally:
        db.close()

    with patch(
        "saiverse.runtime_marker.another_running_process_owns_db",
        return_value=(False, ""),
    ):
        m._init_city_config("city_b")
    db = Session()
    try:
        assert db.query(City).filter(City.CITYID == 1).first().CITYNAME == "city_b"
    finally:
        db.close()


def test_cityname_auto_repair_refused_for_multi_city_db(tmp_path: Path) -> None:
    """複数 City の DB で未知名を渡されたら CITYID=1 を改名しない (2026-07-31
    十一巡目 high2)。修復は単一 City DB の改名事故の救済に限る — 複数 City で
    未知名なのは呼び出しの誤りで、CITYID=1 の所属を黙って書き換えると建物・AI・
    世界データを誤った City 名で運転してしまう。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import Base, City, User
    from manager.initialization import InitializationMixin

    db_file = tmp_path / "saiverse.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        db.add(City(USERID=1, CITYNAME="city_a", UI_PORT=3001, API_PORT=8001))
        db.add(City(USERID=1, CITYNAME="city_b", UI_PORT=3002, API_PORT=8002))
        db.commit()
    finally:
        db.close()

    class _Manager(InitializationMixin):
        def _update_timezone_cache(self, tz):
            pass

    m = _Manager()
    m.SessionLocal = Session
    m.db_path = str(db_file)

    with patch(
        "saiverse.runtime_marker.another_running_process_owns_db",
        return_value=(False, ""),
    ):
        with pytest.raises(ValueError, match="single-city database"):
            m._init_city_config("city_c")
    db = Session()
    try:
        assert db.query(City).filter(City.CITYID == 1).first().CITYNAME == "city_a"
    finally:
        db.close()


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
