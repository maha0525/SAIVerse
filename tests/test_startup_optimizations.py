"""Regression tests for backend startup speed optimizations (A / B1 / B2 / C)."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# A. Lazy EmotionControlModule on PersonaCore
# ---------------------------------------------------------------------------


def test_persona_core_emotion_module_is_lazy_and_cached(tmp_path: Path) -> None:
    from persona.core import PersonaCore

    persona = PersonaCore.__new__(PersonaCore)
    persona._emotion_module = None
    persona._emotion_module_lock = threading.Lock()

    fake_module = MagicMock(name="EmotionControlModuleInstance")
    with patch("persona.core.EmotionControlModule", return_value=fake_module) as cls:
        # Not instantiated until accessed
        cls.assert_not_called()
        first = persona.emotion_module
        second = persona.emotion_module

    assert first is fake_module
    assert second is fake_module
    cls.assert_called_once_with()


def test_persona_core_emotion_module_setter_does_not_trigger_init() -> None:
    from persona.core import PersonaCore

    persona = PersonaCore.__new__(PersonaCore)
    persona._emotion_module = None
    persona._emotion_module_lock = threading.Lock()

    injected = MagicMock(name="InjectedEmotionModule")
    with patch("persona.core.EmotionControlModule") as cls:
        persona.emotion_module = injected
        assert persona.emotion_module is injected
    cls.assert_not_called()


def test_persona_core_init_does_not_eagerly_build_emotion_module(tmp_path: Path) -> None:
    from persona.core import PersonaCore

    common_prompt = tmp_path / "common.txt"
    common_prompt.write_text("common", encoding="utf-8")

    with (
        patch("persona.core.EmotionControlModule") as emotion_cls,
        patch("persona.core.load_session_data", side_effect=lambda p: setattr(p, "messages", [])),
        patch("persona.core.initialise_memory_adapter", return_value=None),
        patch("persona.core.initialise_pulse_state"),
        patch("persona.core.model_supports_images", return_value=False),
        patch("persona.core.PersonaTaskStore"),
        patch("saiverse.data_paths.get_saiverse_home", return_value=tmp_path),
    ):
        persona = PersonaCore(
            city_name="city_a",
            persona_id="tester",
            persona_name="Tester",
            persona_system_instruction="sys",
            avatar_image=None,
            buildings=[],
            common_prompt_path=common_prompt,
            session_factory=MagicMock(),
            is_visitor=True,
        )
        emotion_cls.assert_not_called()
        _ = persona.emotion_module
        emotion_cls.assert_called_once_with()


# ---------------------------------------------------------------------------
# B1. Main DB backup pruning avoids re-running integrity_check on old backups
# ---------------------------------------------------------------------------


def _create_sqlite_db(path: Path, value: str = "v1") -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('a', ?)", (value,))
        conn.commit()


def test_prune_old_backups_skips_full_integrity_check_on_kept_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from database import backup as db_backup

    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    db_path = tmp_path / "saiverse.db"
    _create_sqlite_db(db_path)

    # Create 3 generations with keep_count=2
    calls: list[bool] = []
    real_validate = db_backup._validated_backup_metadata

    def _spy_validate(path: Path, *, check_sqlite_integrity: bool = True):
        calls.append(check_sqlite_integrity)
        return real_validate(path, check_sqlite_integrity=check_sqlite_integrity)

    monkeypatch.setattr(db_backup, "_validated_backup_metadata", _spy_validate)

    b1 = db_backup.backup_saiverse_db(db_path, keep_count=2, kind="pre_upgrade")
    b2 = db_backup.backup_saiverse_db(db_path, keep_count=2, kind="pre_upgrade")
    # First two backups have <= keep_count candidates, so pruning should short-circuit
    assert calls == []

    b3 = db_backup.backup_saiverse_db(db_path, keep_count=2, kind="pre_upgrade")
    assert b1 is not None and b2 is not None and b3 is not None
    # Pruning validates candidates with check_sqlite_integrity=False
    assert calls and all(flag is False for flag in calls)
    assert not b1.exists()
    assert b2.exists() and b3.exists()


def test_prune_old_backups_ignores_corrupt_header_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from database import backup as db_backup

    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    db_path = tmp_path / "saiverse.db"
    _create_sqlite_db(db_path)

    b1 = db_backup.backup_saiverse_db(db_path, keep_count=2, kind="pre_upgrade")
    b2 = db_backup.backup_saiverse_db(db_path, keep_count=2, kind="pre_upgrade")
    assert b1 is not None and b2 is not None

    # Corrupt b2 in place with the same byte size (non-SQLite header)
    b2.write_bytes(b"X" * b2.stat().st_size)

    b3 = db_backup.backup_saiverse_db(db_path, keep_count=2, kind="pre_upgrade")
    assert b3 is not None
    # b2 is rejected as invalid, so valid b1 + b3 (2 valid generations) are BOTH kept
    assert b1.exists()
    assert b3.exists()


# ---------------------------------------------------------------------------
# B2. Persona memory.db fast-skip, WAL no-op fix, and startup deferral gate
# ---------------------------------------------------------------------------


def test_run_simple_backup_fast_skips_unchanged_db_and_detects_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sai_memory import backup as mem_backup

    db_path = tmp_path / "persona" / "memory.db"
    db_path.parent.mkdir(parents=True)
    backup_root = tmp_path / "backups"

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO messages VALUES ('m1', 'hello')")
        conn.commit()

    # Keep a live WAL connection open (mirrors running SAIMemoryAdapter)
    live = sqlite3.connect(db_path)
    try:
        # 1st backup creates file + caches fingerprint state
        b1 = mem_backup.run_simple_backup(
            persona_id="p1", db_path=db_path, output_root=backup_root
        )
        assert b1 is not None and b1.is_file()

        # State file must not appear in list_simple_backups
        listed = mem_backup.list_simple_backups("p1", output_root=backup_root)
        assert [item["filename"] for item in listed] == [b1.name]

        # 2nd backup on unchanged DB must fast-skip without opening SQLite
        with patch.object(mem_backup.sqlite3, "connect", side_effect=AssertionError("should fast-skip")):
            skipped = mem_backup.run_simple_backup(
                persona_id="p1", db_path=db_path, output_root=backup_root
            )
        assert skipped is None

        # Mutate in WAL mode (even in-place same-length update!) -> must detect & back up
        live.execute("UPDATE messages SET body = 'world' WHERE id = 'm1'")
        live.commit()
        time.sleep(0.01)
        b2 = mem_backup.run_simple_backup(
            persona_id="p1", db_path=db_path, output_root=backup_root
        )
        assert b2 is not None and b2 != b1

        # Deleting latest backup must invalidate fast-skip and recreate a backup
        mem_backup.delete_simple_backup("p1", b2.name, output_root=backup_root)
        time.sleep(0.01)
        b3 = mem_backup.run_simple_backup(
            persona_id="p1", db_path=db_path, output_root=backup_root
        )
        assert b3 is not None and b3 != b1

        # skip_if_unchanged=False always creates a fresh backup even when unchanged
        time.sleep(0.01)
        b4 = mem_backup.run_simple_backup(
            persona_id="p1",
            db_path=db_path,
            output_root=backup_root,
            skip_if_unchanged=False,
        )
        assert b4 is not None and b4 != b3
    finally:
        live.close()


def test_purge_stale_legacy_embeddings_does_not_dirty_wal_when_empty(tmp_path: Path) -> None:
    from sai_memory.embedding_migration import purge_stale_legacy_embeddings
    from sai_memory.memory.storage import init_db

    db_path = tmp_path / "memory.db"
    wal_path = tmp_path / "memory.db-wal"

    conn = init_db(str(db_path), check_same_thread=False)
    conn.close()
    assert not wal_path.exists() or wal_path.stat().st_size == 0

    conn = init_db(str(db_path), check_same_thread=False)
    try:
        deleted = purge_stale_legacy_embeddings(conn, 0, everything=True)
        assert deleted == 0
        assert not wal_path.exists() or wal_path.stat().st_size == 0
    finally:
        conn.close()


def test_startup_backup_gate_defers_and_serializes_persona_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory import adapter as adapter_mod

    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    monkeypatch.setenv("SAIMEMORY_BACKUP_ON_START", "1")

    active = 0
    max_active = 0
    seen: list[str] = []
    done = threading.Event()
    counter_lock = threading.Lock()

    def _fake_backup(*, persona_id: str, **_kwargs):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with counter_lock:
            active -= 1
            seen.append(persona_id)
            if len(seen) == 2:
                done.set()
        return None

    dummy_embedder = MagicMock()
    dummy_embedder.model_name = "embeddinggemma-2-q4"
    dummy_embedder.embedding_size = 768

    adapter_mod.defer_startup_backups()
    adapters = []
    try:
        with (
            patch.object(adapter_mod, "Embedder", return_value=dummy_embedder),
            patch.object(adapter_mod, "run_backup_auto", side_effect=_fake_backup),
        ):
            for pid in ("p1", "p2", "p3_closed"):
                ad = adapter_mod.SAIMemoryAdapter(
                    pid,
                    persona_dir=tmp_path / pid,
                    resource_id=pid,
                    startup_backup=True,
                )
                adapters.append(ad)

            # Close p3_closed before gate opens — its deferred backup should skip cleanly
            adapters[2].close()
            time.sleep(0.05)
            assert seen == []

            adapter_mod.release_startup_backups()
            assert done.wait(timeout=5.0)
            # Give p3_closed thread a brief moment to exit
            time.sleep(0.05)
            assert set(seen) == {"p1", "p2"}
            assert max_active == 1
    finally:
        adapter_mod.release_startup_backups()
        for ad in adapters:
            ad.close()


# ---------------------------------------------------------------------------
# C. Background Embedder preload
# ---------------------------------------------------------------------------


def test_preload_embedder_async_skips_when_model_files_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sai_memory.memory import recall

    missing_dir = tmp_path / "empty_model"
    missing_dir.mkdir()
    with patch.object(recall, "_download_model", side_effect=AssertionError("must not download")):
        thread = recall.preload_embedder_async(local_model_path=str(missing_dir))
    assert thread is None


def test_preload_embedder_async_warms_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sai_memory.config import EMBED_MODEL_FILES
    from sai_memory.memory import recall

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for fname in EMBED_MODEL_FILES:
        target = model_dir / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"stub")

    fake_encoder = MagicMock(name="FakeOnnxEncoder")
    cache_key = str(model_dir.resolve())
    monkeypatch.setattr(recall, "_EMBEDDING_MODEL_CACHE", {})
    monkeypatch.setattr(recall, "_EMBEDDING_PRELOAD_IN_FLIGHT", set())

    with patch.object(recall, "_OnnxTextEncoder", return_value=fake_encoder) as encoder_cls:
        thread = recall.preload_embedder_async(local_model_path=str(model_dir))
        assert thread is not None
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert recall._EMBEDDING_MODEL_CACHE.get(cache_key) is fake_encoder

        # Subsequent synchronous Embedder() hits cache with zero extra encoder loads
        emb = recall.Embedder(local_model_path=str(model_dir))
        assert emb._encoder is fake_encoder
        encoder_cls.assert_called_once()
