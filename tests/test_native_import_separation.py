"""Regression tests for native import restore/transplant separation.

記憶・人格境界監査 (docs/handoff/2026-07-12_memory_persona_boundary_audit.md
第5片) の M4/M5/M6 修正を固定する:

- M4: 復元 (restore) は同一 persona のみ。別 persona へは明示的な
  transplant で、全 identity を target へ写像し provenance を保持する。
- M5: replace は単一トランザクション。途中失敗は rollback で既存 target 不変。
- M6: embedding 派生工程が別 persona の memory.db / SAIMemoryAdapter を
  生成しない。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import saiverse_memory.native_export as native_export
from saiverse_memory.native_export import import_threads_native


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    """Point get_persona_memory_db at an isolated tmp directory."""
    def fake_get_persona_memory_db(persona_id: str) -> Path:
        return tmp_path / persona_id / "memory.db"

    monkeypatch.setattr(
        "saiverse_memory.native_export.get_persona_memory_db",
        fake_get_persona_memory_db,
    )
    return tmp_path


def make_message(msg_id, content, persona, *, created_at=1700000000, metadata=None):
    return {
        "id": msg_id,
        "role": "assistant",
        "content": content,
        "resource_id": persona,
        "created_at": created_at,
        "metadata": metadata,
    }


def make_archive(persona, threads):
    return {
        "format": "saiverse_saimemory_v1",
        "exported_at": "2026-07-16T00:00:00+00:00",
        "persona_id": persona,
        "threads": threads,
    }


def make_thread(persona, suffix, messages, *, stelis=None, thread_id=None):
    return {
        "thread_id": thread_id or f"{persona}:{suffix}",
        "resource_id": persona,
        "overview": None,
        "overview_updated_at": None,
        "stelis": stelis,
        "messages": messages,
    }


def query(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def thread_messages(db_path, thread_id):
    return query(
        db_path,
        "SELECT id, content, metadata, resource_id FROM messages"
        " WHERE thread_id=? ORDER BY created_at ASC",
        (thread_id,),
    )


def seed_persona(memory_root, persona, threads):
    """Populate a persona DB via a same-persona restore (skip embed)."""
    result = import_threads_native(
        persona, make_archive(persona, threads), skip_embed=True
    )
    assert result["threads_imported"] == len(threads)
    return memory_root / persona / "memory.db"


# ---------------------------------------------------------------------------
# 1. restore alice -> alice: success
# ---------------------------------------------------------------------------

def test_restore_same_persona_succeeds(memory_root):
    archive = make_archive("alice", [
        make_thread("alice", "main", [
            make_message("m1", "hello", "alice"),
            make_message("m2", "world", "alice", created_at=1700000001),
        ]),
    ])
    result = import_threads_native("alice", archive, skip_embed=True)

    assert result["threads_imported"] == 1
    assert result["messages_imported"] == 2
    assert result["embeddings"] == "skipped"

    db_path = memory_root / "alice" / "memory.db"
    assert db_path.exists()
    rows = query(db_path, "SELECT id FROM threads")
    assert rows == [("alice:main",)]
    msgs = thread_messages(db_path, "alice:main")
    assert [(m[0], m[1]) for m in msgs] == [("m1", "hello"), ("m2", "world")]


# ---------------------------------------------------------------------------
# 2. restore alice -> bob: rejected before any write
# ---------------------------------------------------------------------------

def test_restore_other_persona_rejected_before_write(memory_root):
    archive = make_archive("alice", [
        make_thread("alice", "main", [make_message("m1", "hello", "alice")]),
    ])
    with pytest.raises(ValueError, match="restore rejected"):
        import_threads_native("bob", archive, skip_embed=True)

    # Validation runs before mkdir: bob's directory must not even exist.
    assert not (memory_root / "bob").exists()


def test_restore_foreign_thread_prefix_rejected(memory_root):
    # persona_id matches but a thread carries another persona's prefix
    archive = make_archive("bob", [
        make_thread("bob", "main", [make_message("m1", "ok", "bob")]),
        make_thread("alice", "stray", [make_message("m2", "stray", "alice")]),
    ])
    with pytest.raises(ValueError, match="restore rejected"):
        import_threads_native("bob", archive, skip_embed=True)
    assert not (memory_root / "bob").exists()


# ---------------------------------------------------------------------------
# 3. transplant alice -> bob: all identities closed to target + provenance
# ---------------------------------------------------------------------------

def test_transplant_remaps_all_identities(memory_root):
    stelis = {
        "parent_thread_id": "alice:main",
        "depth": 1,
        "window_ratio": 0.8,
        "status": "active",
        "chronicle_prompt": None,
        "chronicle_summary": None,
        "created_at": 1700000000,
        "completed_at": None,
        "label": "child",
    }
    archive = make_archive("alice", [
        make_thread("alice", "main", [
            make_message("m1", "root msg", "alice",
                         metadata={"tags": ["conversation"]}),
        ]),
        make_thread("alice", "child", [
            make_message("m2", "child msg", "alice"),
        ], stelis=stelis),
    ])
    result = import_threads_native("bob", archive, skip_embed=True, transplant=True)
    assert result["threads_imported"] == 2

    db_path = memory_root / "bob" / "memory.db"

    # Thread identities are remapped to bob
    rows = query(db_path, "SELECT id, resource_id FROM threads ORDER BY id")
    assert rows == [("bob:child", "bob"), ("bob:main", "bob")]

    # Stelis parent is remapped to bob
    rows = query(
        db_path,
        "SELECT thread_id, parent_thread_id FROM stelis_threads",
    )
    assert rows == [("bob:child", "bob:main")]

    # Message resource_id is remapped; provenance keeps the original identity
    msgs = thread_messages(db_path, "bob:main")
    assert len(msgs) == 1
    mid, content, meta_raw, res_id = msgs[0]
    assert res_id == "bob"
    meta = json.loads(meta_raw)
    assert meta["transplanted_from"] == {
        "persona_id": "alice", "thread_id": "alice:main",
    }
    # Original metadata (tags) is preserved alongside provenance
    assert meta["tags"] == ["conversation"]

    msgs = thread_messages(db_path, "bob:child")
    meta = json.loads(msgs[0][2])
    assert meta["transplanted_from"] == {
        "persona_id": "alice", "thread_id": "alice:child",
    }
    assert msgs[0][3] == "bob"

    # Nothing in the target DB references alice
    for table, col in [("threads", "id"), ("threads", "resource_id"),
                       ("messages", "thread_id"), ("messages", "resource_id"),
                       ("stelis_threads", "thread_id"),
                       ("stelis_threads", "parent_thread_id")]:
        rows = query(db_path, f"SELECT {col} FROM {table}")
        for (value,) in rows:
            assert value is None or not str(value).startswith("alice"), (
                f"{table}.{col} leaked source identity: {value!r}"
            )


# ---------------------------------------------------------------------------
# 4. transplant with unmappable thread: whole archive rejected
# ---------------------------------------------------------------------------

def test_transplant_unmappable_thread_rejects_whole_archive(memory_root):
    archive = make_archive("alice", [
        make_thread("alice", "main", [make_message("m1", "ok", "alice")]),
        make_thread("alice", "x", [make_message("m2", "bad", "alice")],
                    thread_id="orphan_no_prefix"),
    ])
    with pytest.raises(ValueError, match="transplant rejected"):
        import_threads_native("bob", archive, skip_embed=True, transplant=True)

    # Rejected before any write: target is untouched (not even created)
    assert not (memory_root / "bob").exists()


# ---------------------------------------------------------------------------
# 5. M5 atomicity: pre-validation rejection / mid-write rollback
# ---------------------------------------------------------------------------

OLD_THREADS = [
    lambda: make_thread("bob", "main", [
        make_message("old1", "OLD message 1", "bob"),
        make_message("old2", "OLD message 2", "bob", created_at=1700000001),
    ]),
    lambda: make_thread("bob", "other", [
        make_message("old3", "OLD other", "bob"),
    ]),
]


def _seed_old_bob(memory_root):
    return seed_persona(memory_root, "bob", [f() for f in OLD_THREADS])


def _assert_old_bob_intact(db_path):
    msgs = thread_messages(db_path, "bob:main")
    assert [(m[0], m[1]) for m in msgs] == [
        ("old1", "OLD message 1"), ("old2", "OLD message 2"),
    ]
    msgs = thread_messages(db_path, "bob:other")
    assert [(m[0], m[1]) for m in msgs] == [("old3", "OLD other")]


def test_atomicity_prevalidation_rejects_bad_metadata(memory_root):
    db_path = _seed_old_bob(memory_root)

    bad_archive = make_archive("bob", [
        make_thread("bob", "main", [
            make_message("new1", "NEW", "bob",
                         metadata={"bad": object()}),  # not JSON-serializable
        ]),
    ])
    with pytest.raises(ValueError, match="not\\s+JSON-serializable"):
        import_threads_native("bob", bad_archive, skip_embed=True)

    _assert_old_bob_intact(db_path)


def test_atomicity_midwrite_failure_rolls_back(memory_root, monkeypatch):
    db_path = _seed_old_bob(memory_root)

    real_write = native_export._write_thread_in_txn
    calls = {"n": 0}

    def failing_write(conn, thread_data, *, replace):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom on second thread")
        return real_write(conn, thread_data, replace=replace)

    monkeypatch.setattr(
        native_export, "_write_thread_in_txn", failing_write
    )

    new_archive = make_archive("bob", [
        make_thread("bob", "main", [make_message("new1", "NEW main", "bob")]),
        make_thread("bob", "other", [make_message("new2", "NEW other", "bob")]),
    ])
    with pytest.raises(RuntimeError, match="boom"):
        import_threads_native("bob", new_archive, skip_embed=True)

    # Rollback: OLD rows are row-for-row intact (thread 1 was already staged
    # in the transaction, including the delete of the old thread)
    _assert_old_bob_intact(db_path)
    (count,) = query(db_path, "SELECT COUNT(*) FROM messages")[0]
    assert count == 3


# ---------------------------------------------------------------------------
# 6. success: all threads switch over at once (replace semantics)
# ---------------------------------------------------------------------------

def test_success_replaces_all_threads_atomically(memory_root):
    db_path = _seed_old_bob(memory_root)

    new_archive = make_archive("bob", [
        make_thread("bob", "main", [make_message("new1", "NEW main", "bob")]),
        make_thread("bob", "other", [make_message("new2", "NEW other", "bob")]),
    ])
    result = import_threads_native("bob", new_archive, skip_embed=True)
    assert result["threads_imported"] == 2

    msgs = thread_messages(db_path, "bob:main")
    assert [(m[0], m[1]) for m in msgs] == [("new1", "NEW main")]
    msgs = thread_messages(db_path, "bob:other")
    assert [(m[0], m[1]) for m in msgs] == [("new2", "NEW other")]
    (count,) = query(db_path, "SELECT COUNT(*) FROM messages")[0]
    assert count == 2  # no OLD rows survive


# ---------------------------------------------------------------------------
# 7. M6: transplant never creates the source persona's DB / adapter
# ---------------------------------------------------------------------------

class DummyEmbedder:
    """Non-loading stand-in for sai_memory.memory.recall.Embedder."""

    def __init__(self, model="dummy", *, local_model_path=None,
                 model_dim=None, cuda=None):
        pass

    def embed(self, payload, is_query=False):
        return [[0.1, 0.2, 0.3] for _ in payload]


def test_transplant_does_not_touch_source_persona(memory_root, monkeypatch):
    monkeypatch.setattr("sai_memory.memory.recall.Embedder", DummyEmbedder)

    adapter_calls = []

    class SpyAdapter:
        def __init__(self, *args, **kwargs):
            adapter_calls.append((args, kwargs))

    monkeypatch.setattr(
        "saiverse_memory.adapter.SAIMemoryAdapter", SpyAdapter
    )

    archive = make_archive("alice", [
        make_thread("alice", "main", [make_message("m1", "hello there", "alice")]),
    ])
    # Embedding path enabled: this is the path that used to guess the persona
    # from the thread ID and create a foreign memory.db via SAIMemoryAdapter.
    result = import_threads_native(
        "bob", archive, skip_embed=False, transplant=True
    )

    assert result["embeddings"] == "generated"
    assert adapter_calls == [], "SAIMemoryAdapter must not be constructed"
    # No alice directory/DB anywhere under the isolated root (M6)
    assert not (memory_root / "alice").exists()
    assert list(memory_root.rglob("alice*")) == []

    # Embeddings landed in the TARGET db only
    db_path = memory_root / "bob" / "memory.db"
    (count,) = query(db_path, "SELECT COUNT(*) FROM message_embeddings")[0]
    assert count >= 1


# ---------------------------------------------------------------------------
# 8. embedding failure does not fail the import
# ---------------------------------------------------------------------------

def test_embedding_failure_import_still_succeeds(memory_root, monkeypatch):
    class BrokenEmbedder:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("model load exploded")

    monkeypatch.setattr("sai_memory.memory.recall.Embedder", BrokenEmbedder)

    archive = make_archive("alice", [
        make_thread("alice", "main", [make_message("m1", "hello", "alice")]),
    ])
    result = import_threads_native("alice", archive, skip_embed=False)

    assert result["threads_imported"] == 1
    assert result["messages_imported"] == 1
    assert result["embeddings"] == "failed"

    # Messages are committed regardless of the embedding outcome
    db_path = memory_root / "alice" / "memory.db"
    msgs = thread_messages(db_path, "alice:main")
    assert [(m[0], m[1]) for m in msgs] == [("m1", "hello")]
