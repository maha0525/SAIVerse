"""Regression and contract tests for CAS snapshots, DB backup separation,
persona memory.db restore, stall timeout, and the /api/backups endpoints.

All tests run against an isolated ``tmp_path`` via ``SAIVERSE_HOME`` and never
touch the user's real ``~/.saiverse/``.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.routes import backups as backups_route
from database.backup import backup_saiverse_db, get_recent_backups, resolve_backup_dir
from sai_memory import backup as mem_backup
from saiverse.environment_alerts import backup_operation_failure_alerts
from scripts import snapshot, update_engine


def _create_sqlite(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS state (value TEXT)")
        conn.execute("DELETE FROM state")
        conn.execute("INSERT INTO state (value) VALUES (?)", (value,))
        conn.commit()


def _read_sqlite(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT value FROM state").fetchone()
    assert row is not None
    return str(row[0])


def _seed_world(home: Path, *, main_val: str = "v1", mem_val: str = "m1") -> None:
    _create_sqlite(home / "user_data" / "database" / "saiverse.db", main_val)
    _create_sqlite(home / "personas" / "mira" / "memory.db", mem_val)
    img = home / "image" / "photo.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-payload" * 50)


# ---- CAS v3 snapshots: multi-generation, dedup, GC, fail-closed, v2 compat ----


def test_cas_multi_generation_dedup_and_arbitrary_rollback(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    _seed_world(home, main_val="gen1", mem_val="mem1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        r1 = snapshot.save_snapshot("gen1", note="first")
        assert r1["new_objects"] == 3
        assert r1["reused_objects"] == 0

        # Check encoding choice: PNG -> raw, SQLite DB -> zlib
        m1 = json.loads((home / "snapshots" / "manifests" / "gen1.json").read_text(encoding="utf-8"))
        assert m1["format_version"] == snapshot.CAS_SNAPSHOT_FORMAT_VERSION
        assert m1["files"]["image/photo.png"]["encoding"] == "raw"
        assert m1["files"]["personas/mira/memory.db"]["encoding"] == "zlib"
        assert (home / "snapshots" / "README.txt").is_file()

        # Mutate only memory.db -> saiverse.db and photo.png must be reused!
        _create_sqlite(home / "personas" / "mira" / "memory.db", "mem2")
        r2 = snapshot.save_snapshot("gen2", note="second")
        assert r2["new_objects"] == 1
        assert r2["reused_objects"] == 2

        # Mutate both DBs for gen3
        _create_sqlite(home / "user_data" / "database" / "saiverse.db", "gen3")
        _create_sqlite(home / "personas" / "mira" / "memory.db", "mem3")
        snapshot.save_snapshot("gen3", note="third")

        # Roll back directly to gen1 (skipping gen2)
        rc = snapshot.cmd_restore(
            argparse.Namespace(name="gen1", yes=True, no_auto_snapshot=True)
        )
        assert rc == 0
        assert _read_sqlite(home / "user_data" / "database" / "saiverse.db") == "gen1"
        assert _read_sqlite(home / "personas" / "mira" / "memory.db") == "mem1"

        # Delete gen2 -> gen1 and gen3 must both remain restorable!
        deleted = snapshot.delete_snapshot("gen2", run_gc=True)
        assert deleted["gc"]["removed_objects"] == 1  # only mem2 object was unique to gen2

        rc3 = snapshot.cmd_restore(
            argparse.Namespace(name="gen3", yes=True, no_auto_snapshot=True)
        )
        assert rc3 == 0
        assert _read_sqlite(home / "user_data" / "database" / "saiverse.db") == "gen3"
        assert _read_sqlite(home / "personas" / "mira" / "memory.db") == "mem3"


def test_manual_manifest_delete_and_fail_closed_gc(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    _seed_world(home, main_val="gen1", mem_val="mem1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("gen1")
        _create_sqlite(home / "personas" / "mira" / "memory.db", "mem2")
        snapshot.save_snapshot("gen2")

        # Simulate Explorer manual deletion of gen2 manifest
        (home / "snapshots" / "manifests" / "gen2.json").unlink()

        stats_before = snapshot.get_store_stats()
        assert stats_before["orphan_object_count"] == 1

        # Add a corrupted manifest -> GC must refuse (fail-closed) and delete nothing!
        corrupt = home / "snapshots" / "manifests" / "broken.json"
        corrupt.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="fail-closed"):
            snapshot.gc_store()
        assert snapshot.get_store_stats()["cas_object_count"] == 4

        # Remove corrupted manifest -> GC succeeds and reclaims only mem2's object
        corrupt.unlink()
        gc_res = snapshot.gc_store()
        assert gc_res["removed_objects"] == 1
        assert gc_res["live_objects"] == 3

        # gen1 still restores cleanly
        rc = snapshot.cmd_restore(
            argparse.Namespace(name="gen1", yes=True, no_auto_snapshot=True)
        )
        assert rc == 0
        assert _read_sqlite(home / "personas" / "mira" / "memory.db") == "mem1"


def test_export_cas_to_v2_zip_and_coexistence(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    _seed_world(home, main_val="v1", mem_val="m1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("cas_one", note="cas")
        exported = home / "snapshots" / "legacy_zip.zip"
        snapshot.export_snapshot_to_zip("cas_one", exported)

        listed = snapshot.list_snapshots()
        formats = {item["name"]: item["format"] for item in listed}
        assert formats == {"cas_one": "cas", "legacy_zip": "zip"}

        # Exported ZIP must validate as format v2 and restore cleanly
        _create_sqlite(home / "user_data" / "database" / "saiverse.db", "changed")
        rc = snapshot.cmd_restore(
            argparse.Namespace(name="legacy_zip", yes=True, no_auto_snapshot=True)
        )
        assert rc == 0
        assert _read_sqlite(home / "user_data" / "database" / "saiverse.db") == "v1"


# ---- DB backup relocation & exclusion from snapshots ----


def test_db_backup_relocates_to_backups_dir_and_is_excluded_from_snapshots(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    db_path = home / "user_data" / "database" / "saiverse.db"
    _create_sqlite(db_path, "live")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    # Plant legacy backups + orphaned -shm in user_data/database/
    legacy_bak = db_path.parent / "saiverse.db_20260101_000000.bak"
    legacy_bak.write_bytes(b"old-backup")
    orphan_shm = db_path.parent / "saiverse.db_backup_startup_old.bak-shm"
    orphan_shm.write_bytes(b"orphan")

    # Even BEFORE backup_saiverse_db relocates them, snapshot.py must exclude them
    collected = {e.archive_path for e in snapshot.collect_files_to_snapshot()}
    assert "user_data/database/saiverse.db" in collected
    assert "user_data/database/saiverse.db_20260101_000000.bak" not in collected
    assert "user_data/database/saiverse.db_backup_startup_old.bak-shm" not in collected

    # Restoring a snapshot must preserve unmigrated .bak files in user_data/database/
    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("before_relocate")
        _create_sqlite(db_path, "after")
        assert snapshot.cmd_restore(
            argparse.Namespace(name="before_relocate", yes=True, no_auto_snapshot=True)
        ) == 0
        assert _read_sqlite(db_path) == "live"
        assert legacy_bak.is_file()

    # Running backup_saiverse_db writes to backups/saiverse_db/, relocates legacy .bak,
    # removes orphaned .bak-shm, and spawns no new .bak-shm sidecar.
    assert resolve_backup_dir(db_path) == (home / "backups" / "saiverse_db").resolve()
    created = backup_saiverse_db(db_path, keep_count=5, kind="startup")
    assert created is not None
    assert created.parent.resolve() == (home / "backups" / "saiverse_db").resolve()
    assert not legacy_bak.exists()
    assert (home / "backups" / "saiverse_db" / legacy_bak.name).is_file()
    assert not orphan_shm.exists()
    assert not created.with_name(created.name + "-shm").exists()
    assert len(get_recent_backups(db_path, limit=5)) == 1


# ---- Persona memory.db individual backup & restore ----


def test_persona_memory_restore_preserves_safety_backup_and_clears_head_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    mem_db = home / "personas" / "mira" / "memory.db"
    main_db = home / "user_data" / "database" / "saiverse.db"
    _create_sqlite(mem_db, "memory-v1")
    main_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(main_db)) as conn:
        conn.execute(
            "CREATE TABLE session_head_snapshot ("
            "PERSONA_ID TEXT, MODEL_KEY TEXT, SECTIONS_JSON TEXT)"
        )
        conn.execute(
            "INSERT INTO session_head_snapshot VALUES ('mira', 'm1', '{}'), ('other', 'm1', '{}')"
        )
        conn.commit()

    b1 = mem_backup.run_simple_backup(persona_id="mira", db_path=mem_db, skip_if_unchanged=False)
    assert b1 is not None

    _create_sqlite(mem_db, "memory-v2")
    with patch("saiverse.runtime_marker.marker_status", return_value=("stopped", "test")):
        res = mem_backup.restore_simple_backup(
            persona_id="mira",
            filename=b1.name,
            db_path=mem_db,
            saiverse_db_path=main_db,
            require_stopped=True,
        )

    assert _read_sqlite(mem_db) == "memory-v1"
    assert res["pre_restore_backup"] is not None
    assert res["cleared_head_snapshots"] == 1

    # Other persona's session_head_snapshot row stays intact
    with closing(sqlite3.connect(main_db)) as conn:
        remaining = [r[0] for r in conn.execute("SELECT PERSONA_ID FROM session_head_snapshot")]
    assert remaining == ["other"]

    # pre_restore backup is NOT pruned by routine keep_count=1
    _create_sqlite(mem_db, "memory-v3")
    mem_backup.run_simple_backup(
        persona_id="mira", db_path=mem_db, keep_count=1, skip_if_unchanged=False
    )
    listed = mem_backup.list_simple_backups("mira")
    assert any(item["kind"] == "pre_restore" for item in listed)


# ---- Stall-based liveness timeout & offline backup failure alerts ----


def test_stall_timeout_allows_slow_progress_and_kills_silent_hang(tmp_path: Path) -> None:
    # Slow but emitting output every 0.15s for ~0.6s > stall_timeout=0.45s -> MUST SUCCEED
    progress_cmd = [
        sys.executable,
        "-c",
        "import time, sys\nfor i in range(4):\n print(f'step {i}', flush=True)\n time.sleep(0.15)\n",
    ]
    out = update_engine._run_with_stall_timeout(
        progress_cmd,
        cwd=tmp_path,
        label="progressing child",
        stall_timeout=0.45,
    )
    assert "step 3" in out

    # Silent hang for 5s with stall_timeout=0.4s -> MUST FAIL quickly
    hang_cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(update_engine.UpdateError, match="stalled"):
        update_engine._run_with_stall_timeout(
            hang_cmd,
            cwd=tmp_path,
            label="hung child",
            stall_timeout=0.4,
        )


def test_backup_operation_failure_alert_is_one_shot(tmp_path: Path) -> None:
    update_engine.write_backup_operation_failure(
        tmp_path,
        operation=update_engine.OPERATION_SNAPSHOT_RESTORE,
        target="gen1",
        error="checksum mismatch",
    )
    first = backup_operation_failure_alerts(tmp_path)
    assert len(first) == 1
    assert first[0]["id"] == "backup_operation_failed"
    assert "gen1" in first[0]["message"]
    assert first[0]["details"]["error"] == "checksum mismatch"
    # Second call sees nothing (consumed once)
    assert backup_operation_failure_alerts(tmp_path) == []


def test_detached_backup_operation_restarts_backend_and_records_failure(tmp_path: Path) -> None:
    config = {
        "main_pid": 999999,
        "main_process_created_at": None,
        "operation": update_engine.OPERATION_SNAPSHOT_RESTORE,
        "snapshot_name": "missing_snap",
    }
    restarted: list[bool] = []

    class _FakeProc:
        pid = 12345

    with patch.object(update_engine, "wait_for_owned_process_exit"), patch.object(
        update_engine,
        "_run_with_stall_timeout",
        side_effect=update_engine.UpdateError("Snapshot 'missing_snap' not found"),
    ), patch.object(
        update_engine, "restart_application", side_effect=lambda _c: (restarted.append(True), _FakeProc())[1]
    ), patch.object(
        update_engine,
        "wait_for_healthy_restart",
        return_value={"city_name": "city_a", "version": "0.3.35"},
    ):
        with pytest.raises(update_engine.UpdateError, match="missing_snap"):
            update_engine._run_detached_backup_operation(config, tmp_path, sys.executable)

    assert restarted == [True]
    alerts = backup_operation_failure_alerts(tmp_path)
    assert len(alerts) == 1
    assert alerts[0]["details"]["operation"] == update_engine.OPERATION_SNAPSHOT_RESTORE
    assert "missing_snap" in alerts[0]["details"]["error"]


# ---- /api/backups endpoints ----


def test_backup_api_routes_snapshots_and_personas(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    _seed_world(home, main_val="api-v1", mem_val="api-m1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("snap_api_1", note="via test")

    app = FastAPI()
    app.include_router(backups_route.router, prefix="/api/backups")
    client = TestClient(app)

    # Snapshot listing
    res = client.get("/api/backups/snapshots")
    assert res.status_code == 200
    body = res.json()
    assert len(body["snapshots"]) == 1
    assert body["snapshots"][0]["name"] == "snap_api_1"
    assert body["snapshots"][0]["format"] == "cas"

    # Restore without confirm=true is refused
    res_no_confirm = client.post("/api/backups/snapshots/snap_api_1/restore", json={"confirm": False})
    assert res_no_confirm.status_code == 400

    # Delete snapshot + auto GC
    res_del = client.delete("/api/backups/snapshots/snap_api_1")
    assert res_del.status_code == 200
    assert res_del.json()["deleted"]["name"] == "snap_api_1"
    assert client.get("/api/backups/snapshots").json()["snapshots"] == []

    # Persona memory backup live create, list, and delete
    res_personas = client.get("/api/backups/personas")
    assert res_personas.status_code == 200
    assert any(p["persona_id"] == "mira" for p in res_personas.json()["personas"])

    res_create = client.post("/api/backups/personas/mira")
    assert res_create.status_code == 200
    created_name = res_create.json()["created"]
    assert created_name is not None

    # Restore requires confirm=true
    assert (
        client.post(
            "/api/backups/personas/mira/restore",
            json={"filename": created_name, "confirm": False},
        ).status_code
        == 400
    )

    # Restore with confirm=true launches detached worker with exact operation config
    launched: list[dict] = []
    with patch.object(backups_route, "_launch_offline_backup_op", side_effect=launched.append):
        res_restore = client.post(
            "/api/backups/personas/mira/restore",
            json={"filename": created_name, "confirm": True},
        )
    assert res_restore.status_code == 200
    assert launched == [
        {
            "operation": update_engine.OPERATION_PERSONA_MEMORY_RESTORE,
            "persona_id": "mira",
            "backup_file": created_name,
        }
    ]

    # Delete persona backup
    res_pdel = client.delete(f"/api/backups/personas/mira/{created_name}")
    assert res_pdel.status_code == 200
    assert res_pdel.json()["backups"] == []


# ---- Codex review regression tests (PR #525) ----


def test_cross_process_store_lock_blocks_gc_during_inflight_save(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 1: A separate process running GC cannot delete in-flight objects
    while save_snapshot holds the cross-process store lock."""
    home = tmp_path / "home"
    _seed_world(home, main_val="v1", mem_val="m1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    # Plant an unreferenced object simulating an in-flight save before manifest publish.
    fake_sha = "a" * 64
    obj_path = home / "snapshots" / ".store" / "objects" / "aa" / f"{fake_sha}.raw"
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    obj_path.write_bytes(b"inflight-object")

    import os

    env = {**os.environ, "SAIVERSE_HOME": str(home)}
    child_code = (
        "import sys; sys.path.insert(0, '.'); "
        "from scripts import snapshot; "
        "snapshot.gc_store(lock_timeout=0.3)"
    )

    with snapshot.snapshot_store_lock():
        # Re-entrant within the same thread must not deadlock.
        with snapshot.snapshot_store_lock():
            proc = subprocess.run(
                [sys.executable, "-c", child_code],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
    assert proc.returncode != 0
    assert "SnapshotStoreBusyError" in proc.stderr
    assert obj_path.is_file()  # in-flight object was NOT deleted by concurrent GC

    # Once the lock is released, GC succeeds cleanly.
    res = snapshot.gc_store()
    assert res["removed_objects"] == 1
    assert not obj_path.exists()


def test_restore_paths_emit_real_progress_output(tmp_path: Path, monkeypatch) -> None:
    """Finding 2: CAS restore, legacy ZIP restore, and persona memory.db restore
    all emit real staging/extraction and SQLite verification progress lines."""
    home = tmp_path / "home"
    _seed_world(home, main_val="prog-v1", mem_val="prog-m1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    monkeypatch.setattr(snapshot, "PROGRESS_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(mem_backup, "_RESTORE_PROGRESS_INTERVAL_SECONDS", 0.0)

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("prog_cas")
        zip_path = home / "snapshots" / "prog_zip.zip"
        snapshot.export_snapshot_to_zip("prog_cas", zip_path)

    cas_lines: list[str] = []
    snapshot.validate_and_extract_cas_snapshot(
        home / "snapshots" / "manifests" / "prog_cas.json",
        tmp_path / "stage_cas",
        progress_callback=cas_lines.append,
    )
    assert any("Staging CAS" in line for line in cas_lines)
    assert any("Verifying SQLite integrity" in line for line in cas_lines)

    zip_lines: list[str] = []
    snapshot.validate_and_extract_snapshot(
        zip_path,
        tmp_path / "stage_zip",
        progress_callback=zip_lines.append,
    )
    assert any("Extracting ZIP" in line for line in zip_lines)
    assert any("Verifying SQLite integrity" in line for line in zip_lines)

    mem_db = home / "personas" / "mira" / "memory.db"
    b1 = mem_backup.run_simple_backup(persona_id="mira", db_path=mem_db, skip_if_unchanged=False)
    assert b1 is not None
    mem_lines: list[str] = []
    with patch("saiverse.runtime_marker.marker_status", return_value=("stopped", "test")):
        mem_backup.restore_simple_backup(
            persona_id="mira",
            filename=b1.name,
            db_path=mem_db,
            saiverse_db_path=home / "user_data" / "database" / "saiverse.db",
            require_stopped=True,
            progress_callback=mem_lines.append,
        )
    assert any("Verifying SQLite integrity" in line for line in mem_lines)
    assert any("pages" in line for line in mem_lines)


def test_export_snapshot_to_zip_sets_file_size_and_force_zip64(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 3: ZIP export allocates ZIP64 headers via info.file_size and force_zip64=True."""
    home = tmp_path / "home"
    _seed_world(home, main_val="z64", mem_val="m64")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("z64_snap")

    recorded: list[tuple[str, int, bool]] = []
    orig_open = zipfile.ZipFile.open

    def _spy_open(self, name, mode="r", pwd=None, *, force_zip64=False):
        if mode == "w" and isinstance(name, zipfile.ZipInfo) and name.filename != "snapshot.json":
            recorded.append((name.filename, name.file_size, bool(force_zip64)))
        return orig_open(self, name, mode=mode, pwd=pwd, force_zip64=force_zip64)

    out_zip = tmp_path / "exported.zip"
    with patch.object(zipfile.ZipFile, "open", _spy_open):
        snapshot.export_snapshot_to_zip("z64_snap", out_zip)

    assert len(recorded) == 3
    for fname, fsize, z64 in recorded:
        assert fsize > 0, f"expected positive file_size for {fname}"
        assert z64 is True, f"expected force_zip64=True for {fname}"

    # Exported ZIP64 archive still validates and extracts cleanly.
    meta = snapshot.validate_and_extract_snapshot(out_zip, tmp_path / "extract_z64")
    assert meta["file_count"] == 3


# ---- Codex review round-2 regression tests (PR #525) ----


def test_snapshot_name_containing_json_tmp_survives_list_gc_and_restore(
    tmp_path: Path, monkeypatch
) -> None:
    """Round-2 Finding 1: Valid snapshot names containing '.json.tmp' must not be
    treated as temp files by list_snapshots, GC, or leftover cleanup."""
    home = tmp_path / "home"
    _seed_world(home, main_val="tricky-v1", mem_val="tricky-m1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    tricky_names = ["world.json.tmp_old", "world.json.tmp.12345"]
    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        for sname in tricky_names:
            snapshot.save_snapshot(sname)

        # Saving 'world' must not delete 'world.json.tmp_old.json' or 'world.json.tmp.12345.json'
        snapshot.save_snapshot("world")

        listed_names = {item["name"] for item in snapshot.list_snapshots()}
        assert {"world", *tricky_names}.issubset(listed_names)

        # GC must keep all objects referenced by those manifests
        gc_res = snapshot.gc_store()
        assert gc_res["removed_objects"] == 0

        # Mutate world and restore from 'world.json.tmp_old'
        _create_sqlite(home / "user_data" / "database" / "saiverse.db", "changed")
        rc = snapshot.cmd_restore(
            argparse.Namespace(name="world.json.tmp_old", yes=True, no_auto_snapshot=True)
        )
        assert rc == 0
        assert _read_sqlite(home / "user_data" / "database" / "saiverse.db") == "tricky-v1"


def test_save_snapshot_emits_mid_file_byte_progress_before_file_completes(
    tmp_path: Path, monkeypatch
) -> None:
    """Round-2 Finding 2: Workers emit byte-level progress mid-file before a
    single large file finishes hashing/compressing."""
    home = tmp_path / "home"
    big = home / "user_data" / "database" / "saiverse.db"
    _create_sqlite(big, "single-file-progress")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    monkeypatch.setattr(snapshot, "STREAM_CHUNK_SIZE", 1024)
    monkeypatch.setattr(snapshot, "PROGRESS_INTERVAL_SECONDS", 0.0)

    lines: list[str] = []
    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("midfile", workers=1, progress_callback=lines.append)

    # Progress must have been emitted while done_files was still 0 (mid-file)
    assert any("0/1 files" in line and "processed:" in line for line in lines)


def test_relocate_legacy_db_backups_preserves_wal_when_wal_move_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Round-2 Finding 3: If moving a non-empty .bak-wal fails after moving its
    base .bak, the base move is rolled back and Pass 2 never deletes the WAL."""
    import shutil
    from database.backup import relocate_legacy_db_backups

    home = tmp_path / "home"
    db_path = home / "user_data" / "database" / "saiverse.db"
    _create_sqlite(db_path, "live")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    legacy_bak = db_path.parent / "saiverse.db_20260101_000000.bak"
    legacy_wal = db_path.parent / "saiverse.db_20260101_000000.bak-wal"
    legacy_bak.write_bytes(b"backup-base")
    legacy_wal.write_bytes(b"uncheckpointed-wal-data")

    real_move = shutil.move

    def _fail_on_wal(src, dst, *args, **kwargs):
        if str(src).endswith(".bak-wal"):
            raise OSError("simulated WAL I/O failure")
        return real_move(src, dst, *args, **kwargs)

    with patch("database.backup.shutil.move", side_effect=_fail_on_wal):
        moved = relocate_legacy_db_backups(db_path)

    assert moved == 0
    # Base .bak was rolled back and non-empty .bak-wal was NOT deleted as orphaned
    assert legacy_bak.is_file()
    assert legacy_wal.is_file()
    assert legacy_wal.read_bytes() == b"uncheckpointed-wal-data"
    assert not (home / "backups" / "saiverse_db" / legacy_bak.name).exists()


# ---- Codex review round-3 regression tests (PR #525) ----


def test_corrupted_cas_object_is_detected_and_repaired_on_next_save(
    tmp_path: Path, monkeypatch
) -> None:
    """Round-3 Finding 1: Truncated .z and corrupted .raw CAS objects must not be
    blindly reused; save_snapshot detects the corruption and repairs them from live files."""
    home = tmp_path / "home"
    _seed_world(home, main_val="heal-v1", mem_val="heal-m1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("gen1")

        m1 = json.loads((home / "snapshots" / "manifests" / "gen1.json").read_text(encoding="utf-8"))
        db_sha = m1["files"]["personas/mira/memory.db"]["sha256"]
        img_sha = m1["files"]["image/photo.png"]["sha256"]
        _, db_z_path = snapshot._object_paths(db_sha)
        img_raw_path, _ = snapshot._object_paths(img_sha)

        # Truncate compressed .z object to 10 bytes (non-zero size, corrupt zlib stream)
        db_z_path.write_bytes(db_z_path.read_bytes()[:10])
        # Overwrite raw object with same-length garbage bytes
        orig_raw_len = img_raw_path.stat().st_size
        img_raw_path.write_bytes(b"X" * orig_raw_len)

        # Saving gen2 must detect both corrupt objects, repair them, and succeed
        r2 = snapshot.save_snapshot("gen2")
        assert r2["new_objects"] == 2
        assert r2["reused_objects"] == 1  # untouched saiverse.db was verified and reused

        # Both gen2 and gen1 must now validate and extract cleanly
        snapshot.validate_and_extract_cas_snapshot(
            home / "snapshots" / "manifests" / "gen2.json", tmp_path / "stage_gen2"
        )
        snapshot.validate_and_extract_cas_snapshot(
            home / "snapshots" / "manifests" / "gen1.json", tmp_path / "stage_gen1"
        )


def test_export_snapshot_rejects_ambiguous_collision_with_existing_cas_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """Round-3 Finding 3: Exporting a CAS snapshot to snapshots/<same-name>.zip
    or inside .store/ must be refused before publishing."""
    home = tmp_path / "home"
    _seed_world(home, main_val="exp-v1", mem_val="exp-m1")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")):
        snapshot.save_snapshot("third")

    colliding_zip = home / "snapshots" / "third.zip"
    with pytest.raises(ValueError, match="collides with existing CAS snapshot"):
        snapshot.export_snapshot_to_zip("third", colliding_zip)
    assert not colliding_zip.exists()

    with pytest.raises(ValueError, match="internal store directory"):
        snapshot.export_snapshot_to_zip("third", home / "snapshots" / ".store" / "bad.zip")

    # resolve_snapshot('third') stays unambiguous
    fmt, _ = snapshot.resolve_snapshot("third")
    assert fmt == "cas"

    # Exporting outside snapshots/ (or with a non-colliding filename) succeeds
    ok_zip = tmp_path / "third.zip"
    snapshot.export_snapshot_to_zip("third", ok_zip)
    assert ok_zip.is_file()
