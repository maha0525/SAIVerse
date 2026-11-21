"""保存済みのベクトルの JSON 形式からバイト列形式への変換のテスト。

docs/intent/vector_binary_storage.md §4 と §2 の最後の一文、「検証」の 3・4・6。

押さえること:

- ``convert_json_vectors`` が 4 つの表の JSON 形式の行を、値を変えずにバイト列
  形式へ書き換える。空のベクトル行 (``'[]'``) と、一つのベクトルとして読めない行
  はそのまま残り、束を小さくしても関数は終わる。
- 途中で止まった memory.db は、もう一度実行すると残りが変換される。
- ベクトル以外の表は一行も変わらない。全部の表が例外なく終わったときだけ、
  旧形式の表 ``embeddings`` が空になる。
- 変換の途中で別の接続が書いたメッセージのベクトルは失われない。
- ``SAIMemoryAdapter(..., convert_stored_vectors=True)`` は、起動時の検査と
  バックアップのスレッドの開始より前に変換する。変換が失敗しても初期化は続く。
- 再計算の入口 (API・CLI・``reembed_stale_messages``) は最初に変換する。
- 読み解けない値が入った memory.db を開いても初期化は失敗せず、その表が
  古いベクトルとして検査に掛かる。

Embedder は偽物。DB は一時ディレクトリの SQLite。LLM は呼ばない。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from sai_memory.config import EMBED_DIM, EMBED_MODEL_ID, load_settings
from sai_memory.embedding_migration import (
    VECTOR_TABLES,
    convert_json_vectors,
    highest_vector_dim,
    sample_stale_vector_tables,
)
from sai_memory.memory.storage import (
    DEFERRED_VECTOR,
    add_message,
    get_or_create_thread,
    replace_message_embeddings,
    set_embed_metadata,
    write_deferred_message_embedding,
)
from sai_memory.vector_codec import decode_vector, encode_vector

from tests.test_embedding_migration import (
    FakeEmbedder,
    _check,
    _cli_db,
    _open,
    _run_api_job,
    _run_cli,
    _seed,
)

_ID_COLUMNS = {
    "message_embeddings": ("message_id", "chunk_index"),
    "arasuji_embeddings": ("entry_id",),
    "memopedia_embeddings": ("page_id",),
    "memopedia_fragment_embeddings": ("fragment_id",),
}


def _rand_vec(seed: int, dim: int = EMBED_DIM) -> list:
    """float32 で表せる値だけを持つベクトル (JSON に書いても値が変わらない)。"""
    rng = np.random.default_rng(seed)
    return [float(x) for x in rng.standard_normal(dim).astype(np.float32)]


def _rows(conn, table):
    cols = ", ".join(_ID_COLUMNS[table])
    return conn.execute(f"SELECT {cols}, vector FROM {table} ORDER BY rowid").fetchall()


def _to_json(conn, table, *, dim: int = EMBED_DIM) -> dict:
    """表の全行を JSON 形式のベクトル (行ごとに違う値) に書き換え、{行の ID: 値} を返す。"""
    expected = {}
    for i, row in enumerate(_rows(conn, table)):
        key = row[:-1]
        vec = _rand_vec(hash((table, key)) & 0xFFFF, dim)
        where = " AND ".join(f"{c} = ?" for c in _ID_COLUMNS[table])
        conn.execute(f"UPDATE {table} SET vector = ? WHERE {where}", (json.dumps(vec), *key))
        expected[key] = vec
    conn.commit()
    return expected


def _types(conn, table) -> set:
    return {t for (t,) in conn.execute(f"SELECT typeof(vector) FROM {table}")}


def _non_vector_digest(conn) -> dict:
    """ベクトルの表と旧形式の表以外の、全部の表の全行のハッシュ。"""
    skip = set(VECTOR_TABLES) | {"embeddings"}
    out = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%'"
    ):
        if name in skip:
            continue
        rows = sorted(repr(r) for r in conn.execute(f'SELECT * FROM "{name}"'))
        out[name] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return out


def _seed_json(conn) -> dict:
    """4 つの表を JSON 形式だけにして、{表: {行の ID: 値}} を返す。"""
    _seed(conn, EMBED_DIM)
    return {table: _to_json(conn, table) for table in VECTOR_TABLES}


def _assert_converted(conn, expected: dict) -> None:
    for table, values in expected.items():
        for row in _rows(conn, table):
            key, stored = row[:-1], row[-1]
            if key not in values:
                continue
            assert isinstance(stored, bytes), (table, key)
            np.testing.assert_array_equal(
                decode_vector(stored), np.asarray(values[key], dtype=np.float32)
            )


def _add_messages(conn, n: int, *, start: int = 0) -> list:
    get_or_create_thread(conn, "p:__persona__", "p")
    ids = []
    for i in range(start, start + n):
        mid = add_message(conn, thread_id="p:__persona__", role="user",
                          content=f"メッセージ {i}", resource_id="p", created_at=10_000 + i)
        ids.append(mid)
    return ids


def _set_json(conn, message_id: str, vec: list) -> None:
    conn.execute("DELETE FROM message_embeddings WHERE message_id = ?", (message_id,))
    conn.execute(
        "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES(?, 0, ?)",
        (message_id, json.dumps(vec)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# convert_json_vectors
# ---------------------------------------------------------------------------

def test_json_only_db_is_converted_in_every_table_with_the_same_values(tmp_path):
    conn = _open(tmp_path / "memory.db")
    try:
        expected = _seed_json(conn)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        digest_before = _non_vector_digest(conn)

        result = convert_json_vectors(conn)

        for table in VECTOR_TABLES:
            assert _types(conn, table) == {"blob"}, table
            assert result.tables[table] == {
                "converted": len(expected[table]), "kept": 0,
            }, table
        _assert_converted(conn, expected)
        assert _non_vector_digest(conn) == digest_before
        assert _check(conn) is False
    finally:
        conn.close()


def test_empty_vector_rows_stay_as_they_are(tmp_path):
    conn = _open(tmp_path / "memory.db")
    try:
        ids = _add_messages(conn, 3)
        write_deferred_message_embedding(conn, ids[0])
        _set_json(conn, ids[1], _rand_vec(1))
        write_deferred_message_embedding(conn, ids[2])

        result = convert_json_vectors(conn)

        rows = dict(conn.execute("SELECT message_id, vector FROM message_embeddings"))
        assert rows[ids[0]] == DEFERRED_VECTOR
        assert rows[ids[2]] == DEFERRED_VECTOR
        assert isinstance(rows[ids[1]], bytes)
        assert result.tables["message_embeddings"] == {"converted": 1, "kept": 0}
    finally:
        conn.close()


_UNCONVERTIBLE = [
    "{broken",
    json.dumps([_rand_vec(1, 4), _rand_vec(2, 4)]),  # 一行に複数のベクトル (旧形式)
    json.dumps(["a", "b"]),
    json.dumps([True, False]),
    json.dumps({"vector": [0.1]}),
    "1.5",
]


def test_rows_that_are_not_one_vector_are_left_and_the_run_ends(tmp_path):
    """束の大きさを 1 にしても、書き換えられない行で止まらずに終わる。"""
    conn = _open(tmp_path / "memory.db")
    try:
        ids = _add_messages(conn, len(_UNCONVERTIBLE) + 3)
        bad_ids = ids[: len(_UNCONVERTIBLE)]
        for mid, value in zip(bad_ids, _UNCONVERTIBLE):
            conn.execute(
                "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES(?, 0, ?)",
                (mid, value),
            )
        # UTF-8 として読めない文字列の値
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) "
            "VALUES(?, 0, CAST(x'fffe5b' AS TEXT))",
            (ids[-3],),
        )
        conn.commit()
        good = {ids[-2]: _rand_vec(7), ids[-1]: _rand_vec(8)}
        for mid, vec in good.items():
            _set_json(conn, mid, vec)

        calls = []
        result = convert_json_vectors(
            conn, batch_size=1, progress=lambda t, d, n: calls.append((t, d, n)),
        )

        assert result.tables["message_embeddings"] == {
            "converted": 2, "kept": len(_UNCONVERTIBLE) + 1,
        }
        total = len(_UNCONVERTIBLE) + 3
        assert calls[0] == ("message_embeddings", 0, total)
        assert calls[-1] == ("message_embeddings", total, total)
        rows = dict(conn.execute(
            "SELECT message_id, CAST(vector AS BLOB) FROM message_embeddings"
        ))
        for mid, value in zip(bad_ids, _UNCONVERTIBLE):
            assert rows[mid] == value.encode()
        assert rows[ids[-3]] == b"\xff\xfe\x5b"
        for mid, vec in good.items():
            np.testing.assert_array_equal(
                decode_vector(rows[mid]), np.asarray(vec, dtype=np.float32)
            )

        # もう一度実行しても、残した行は残したまま
        again = convert_json_vectors(conn, batch_size=1)
        assert again.tables["message_embeddings"] == {
            "converted": 0, "kept": len(_UNCONVERTIBLE) + 1,
        }
    finally:
        conn.close()


def test_mixed_and_half_converted_db_is_finished_by_the_next_run(tmp_path):
    """途中で止まった (例外で抜けた) memory.db を、次の実行が仕上げる。

    途中で例外になった回は旧形式の表を消さず、仕上げた回が消す。
    """
    conn = _open(tmp_path / "memory.db")
    try:
        ids = _add_messages(conn, 10)
        values = {}
        for i, mid in enumerate(ids):
            vec = _rand_vec(100 + i)
            values[(mid, 0)] = vec
            if i % 3 == 0:  # 最初から混ざっているバイト列形式の行
                replace_message_embeddings(conn, mid, [vec])
            else:
                _set_json(conn, mid, vec)
        conn.execute(
            "INSERT INTO embeddings(message_id, vector) VALUES(?, ?)",
            (ids[0], json.dumps(_rand_vec(999))),
        )
        conn.commit()

        class _Stop(Exception):
            pass

        def _stop_after_first_batch(table, done, total):
            if done > 0:
                raise _Stop

        with pytest.raises(_Stop):
            convert_json_vectors(conn, batch_size=2, progress=_stop_after_first_batch)

        types = [t for (t,) in conn.execute(
            "SELECT typeof(vector) FROM message_embeddings ORDER BY rowid"
        )]
        assert types.count("blob") == 4 + 2  # 混ざっていた 4 行 + 確定した最初の束
        assert "text" in types
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1

        result = convert_json_vectors(conn, batch_size=2)

        assert result.tables["message_embeddings"] == {"converted": 4, "kept": 0}
        assert _types(conn, "message_embeddings") == {"blob"}
        _assert_converted(conn, {"message_embeddings": values})
        assert result.legacy_deleted == 1
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    finally:
        conn.close()


def test_legacy_rows_are_converted_and_not_copied_back(tmp_path):
    """旧形式の表の行は、開いたときに写された先で変換され、旧形式の表は空になる。"""
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        (mid,) = _add_messages(conn, 1)
        vec = _rand_vec(5)
        conn.execute(
            "INSERT INTO embeddings(message_id, vector) VALUES(?, ?)", (mid, json.dumps(vec)),
        )
        conn.commit()
    finally:
        conn.close()

    conn = _open(db_path)  # init_db が message_embeddings へ写す
    try:
        assert _types(conn, "message_embeddings") == {"text"}
        result = convert_json_vectors(conn)
        assert result.tables["message_embeddings"] == {"converted": 1, "kept": 0}
        assert result.legacy_deleted == 1
    finally:
        conn.close()

    conn = _open(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
        assert _types(conn, "message_embeddings") == {"blob"}
        _assert_converted(conn, {"message_embeddings": {(mid, 0): vec}})
    finally:
        conn.close()


def test_db_without_json_rows_takes_no_write_lock(tmp_path):
    """JSON 形式の行が無い memory.db では、書き込みのトランザクションを始めない。"""
    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        with patch("sai_memory.embedding_migration.immediate_transaction") as tx:
            result = convert_json_vectors(conn)
        # 旧形式の表を空にする一回だけ
        assert tx.call_count == 1
        assert result.converted == 0 and result.kept == 0
        assert not result.did_anything
    finally:
        conn.close()


def test_conversion_does_not_touch_embed_metadata(tmp_path):
    from sai_memory.embedding_migration import MIGRATION_IN_PROGRESS_KEY

    conn = _open(tmp_path / "memory.db")
    try:
        _seed_json(conn)
        set_embed_metadata(conn, "embed_model", "some-old-model")
        set_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY, "x 2026 tok")
        before = conn.execute("SELECT * FROM embed_metadata ORDER BY key").fetchall()
        convert_json_vectors(conn)
        assert conn.execute("SELECT * FROM embed_metadata ORDER BY key").fetchall() == before
    finally:
        conn.close()


def test_vector_written_by_another_connection_during_conversion_is_kept(tmp_path):
    """変換の束と束の間に別の接続 (別スレッド) が書いたベクトルは失われない。

    書く相手は、まだ JSON 形式のまま残っているメッセージ二つ。一つは表の最後の
    行 (消して入れ直すと同じ rowid に戻りうる行) で、もう一つは途中の行。
    """
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        ids = _add_messages(conn, 20)
        for i, mid in enumerate(ids):
            _set_json(conn, mid, _rand_vec(200 + i))
        targets = [ids[-1], ids[10]]
        written = {
            ids[-1]: [_rand_vec(900), _rand_vec(901)],
            ids[10]: [_rand_vec(902)],
        }

        def _writer():
            other = sqlite3.connect(str(db_path), timeout=10)
            try:
                for mid in targets:
                    replace_message_embeddings(other, mid, written[mid])
            finally:
                other.close()

        started = []

        def _progress(table, done, total):
            if done > 0 and not started:
                t = threading.Thread(target=_writer)
                started.append(t)
                t.start()
                t.join(timeout=30)
                assert not t.is_alive()

        convert_json_vectors(conn, batch_size=2, progress=_progress)

        assert started, "the writer thread did not run"
        for mid in targets:
            stored = [v for (v,) in conn.execute(
                "SELECT vector FROM message_embeddings WHERE message_id = ? "
                "ORDER BY chunk_index", (mid,),
            )]
            assert len(stored) == len(written[mid])
            for got, want in zip(stored, written[mid]):
                np.testing.assert_array_equal(
                    decode_vector(got), np.asarray(want, dtype=np.float32)
                )
        assert _types(conn, "message_embeddings") == {"blob"}
    finally:
        conn.close()


def test_writer_waits_for_the_batch_transaction(tmp_path):
    """束の書き込みは immediate_transaction の中: その間、別の接続は書けない。"""
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        (mid,) = _add_messages(conn, 1)
        _set_json(conn, mid, _rand_vec(3))
        seen = []
        import sai_memory.embedding_migration as migration

        real_loader = migration._json_vector_or_none

        def _probe(raw):
            other = sqlite3.connect(str(db_path), timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                seen.append("free")
                other.rollback()
            except sqlite3.OperationalError:
                seen.append("locked")
            finally:
                other.close()
            return real_loader(raw)

        with patch("sai_memory.embedding_migration._json_vector_or_none", _probe):
            convert_json_vectors(conn)
        assert seen == ["locked"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SAIMemoryAdapter(convert_stored_vectors=True)
# ---------------------------------------------------------------------------

def _adapter(tmp_path, **kwargs):
    from saiverse_memory.adapter import SAIMemoryAdapter

    settings = replace(load_settings(), memory_enabled=True)
    with patch("saiverse_memory.adapter.Embedder", FakeEmbedder):
        return SAIMemoryAdapter("p", persona_dir=tmp_path, settings=settings, **kwargs)


def _json_db(tmp_path) -> tuple:
    conn = _open(tmp_path / "memory.db")
    try:
        expected = _seed_json(conn)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        texts = _non_vector_digest(conn)
    finally:
        conn.close()
    return expected, texts


_SOURCE_TABLES = ("threads", "messages", "arasuji_entries", "memopedia_fragments")


def test_adapter_converts_on_open_and_the_check_does_not_flag(tmp_path):
    expected, digest = _json_db(tmp_path)

    adapter = _adapter(tmp_path, convert_stored_vectors=True)
    try:
        assert adapter.embed_model_changed is False
        for table in VECTOR_TABLES:
            assert _types(adapter.conn, table) == {"blob"}, table
        _assert_converted(adapter.conn, expected)
        after = _non_vector_digest(adapter.conn)
        for name in _SOURCE_TABLES:
            assert after[name] == digest[name], name
    finally:
        adapter.close()


def test_adapter_without_the_flag_does_not_convert(tmp_path):
    _json_db(tmp_path)

    adapter = _adapter(tmp_path)
    try:
        assert adapter.embed_model_changed is True
        for table in VECTOR_TABLES:
            assert _types(adapter.conn, table) == {"text"}, table
    finally:
        adapter.close()


def test_adapter_converts_before_the_check_and_the_backup_thread(tmp_path, monkeypatch):
    import sai_memory.embedding_migration as migration
    from saiverse_memory.adapter import SAIMemoryAdapter

    _json_db(tmp_path)
    monkeypatch.setenv("SAIMEMORY_BACKUP_ON_START", "true")
    order = []
    real_convert = migration.convert_json_vectors
    real_check = SAIMemoryAdapter._check_embed_model_change

    def _convert(*args, **kwargs):
        order.append("convert")
        return real_convert(*args, **kwargs)

    def _check_hook(self):
        order.append("check")
        return real_check(self)

    class _Thread:
        def __init__(self, *args, target=None, **kwargs):
            self._target = target

        def start(self):
            order.append(f"thread:{getattr(self._target, '__name__', '?')}")

    monkeypatch.setattr(migration, "convert_json_vectors", _convert)
    monkeypatch.setattr(SAIMemoryAdapter, "_check_embed_model_change", _check_hook)
    monkeypatch.setattr("saiverse_memory.adapter.threading.Thread", _Thread)

    adapter = _adapter(tmp_path, convert_stored_vectors=True, startup_backup=True)
    try:
        assert order == ["convert", "check", "thread:_run_startup_backup"]
    finally:
        adapter.close()


def test_adapter_open_survives_a_failed_conversion(tmp_path, monkeypatch, caplog):
    import sai_memory.embedding_migration as migration

    _json_db(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(migration, "convert_json_vectors", _boom)
    with caplog.at_level(logging.WARNING, logger="saiverse_memory.adapter"):
        adapter = _adapter(tmp_path, convert_stored_vectors=True)
    try:
        assert adapter.is_ready()
        # 残った JSON 形式の行は、検査で古いベクトルとして数えられる
        assert adapter.embed_model_changed is True
        failed = [r for r in caplog.records if "byte format failed" in r.getMessage()]
        assert failed and failed[0].exc_info is not None
    finally:
        adapter.close()


def test_adapter_logs_the_conversion(tmp_path, caplog):
    _json_db(tmp_path)
    with caplog.at_level(logging.INFO, logger="saiverse_memory.adapter"):
        adapter = _adapter(tmp_path, convert_stored_vectors=True)
    adapter.close()
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("converting" in m and "message_embeddings" in m for m in messages)
    assert any("conversion finished" in m and "Persona p" in m for m in messages)


def test_bootstrap_asks_for_the_conversion():
    from types import SimpleNamespace

    from persona import bootstrap

    captured = {}

    class _Adapter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def is_ready(self):
            return True

    persona = SimpleNamespace(persona_id="p", persona_log_path=Path("x") / "log.json")
    with patch.object(bootstrap, "SAIMemoryAdapter", _Adapter):
        bootstrap.initialise_memory_adapter(persona)
    assert captured["convert_stored_vectors"] is True
    assert captured["startup_backup"] is True


# ---------------------------------------------------------------------------
# 再計算の入口
# ---------------------------------------------------------------------------

class _CountingEmbedder(FakeEmbedder):
    texts = 0

    def embed(self, texts, *, is_query=False):
        type(self).texts += len(texts)
        return super().embed(texts, is_query=is_query)


def _json_db_with_one_unconvertible_message(db_path: Path) -> tuple:
    conn = _open(db_path)
    try:
        expected = _seed_json(conn)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        (bad,) = _add_messages(conn, 1, start=50)
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES(?, 0, ?)",
            (bad, json.dumps([_rand_vec(1), _rand_vec(2)])),
        )
        conn.commit()
    finally:
        conn.close()
    return expected, bad


def test_api_reembed_converts_first_and_rebuilds_only_the_rest(tmp_path):
    db_path = tmp_path / "memory.db"
    expected, bad = _json_db_with_one_unconvertible_message(db_path)
    _CountingEmbedder.texts = 0
    from api.routes.people import reembed

    with patch.object(reembed, "get_persona_memory_db", lambda _pid: db_path), \
            patch("sai_memory.memory.recall.Embedder", _CountingEmbedder):
        reembed._run_reembed_task("p", False, None)
    status = reembed._reembed_status.pop("p")
    assert status["running"] is False, status
    assert status["progress"] == 1 and status["total"] == 1  # 変換できなかった 1 件だけ

    conn = _open(db_path)
    try:
        for table in VECTOR_TABLES:
            assert _types(conn, table) == {"blob"}, table
        _assert_converted(conn, expected)  # 変換できた行は元の値のまま (作り直していない)
        assert _check(conn) is False
    finally:
        conn.close()
    assert _CountingEmbedder.texts >= 1


def test_api_reembed_calls_the_conversion_before_anything_else(tmp_path):
    import sai_memory.embedding_migration as migration

    db_path = tmp_path / "memory.db"
    _json_db_with_one_unconvertible_message(db_path)
    order = []
    real_convert = migration.convert_json_vectors
    real_label = migration.model_label_changed
    real_targets = migration.message_reembed_targets

    def _convert(*a, **k):
        order.append("convert")
        return real_convert(*a, **k)

    def _label(*a, **k):
        order.append("label")
        return real_label(*a, **k)

    def _targets(*a, **k):
        order.append("targets")
        return real_targets(*a, **k)

    with patch.object(migration, "convert_json_vectors", _convert), \
            patch.object(migration, "model_label_changed", _label), \
            patch.object(migration, "message_reembed_targets", _targets):
        _run_api_job(db_path, force=False, manager=None)
    assert order[:1] == ["convert"]
    assert order.index("convert") < order.index("label") < order.index("targets")


def test_cli_converts_first_and_rebuilds_only_the_rest(tmp_path, monkeypatch):
    db_path = _cli_db(tmp_path)
    expected, _bad = _json_db_with_one_unconvertible_message(db_path)

    _run_cli(tmp_path, monkeypatch)

    conn = _open(db_path)
    try:
        for table in VECTOR_TABLES:
            assert _types(conn, table) == {"blob"}, table
        _assert_converted(conn, expected)
        assert _check(conn) is False
    finally:
        conn.close()


def test_cli_refuses_smaller_dimension_after_converting_json_rows(tmp_path, monkeypatch):
    """JSON 形式の大きい次元の行は、変換のあと highest_vector_dim に数えられ、拒否が働く。"""
    db_path = _cli_db(tmp_path)
    conn = _open(db_path)
    try:
        _seed(conn, EMBED_DIM)
        expected = {table: _to_json(conn, table, dim=1024) for table in VECTOR_TABLES}
        set_embed_metadata(conn, "embed_model", "bigger-model")
        assert highest_vector_dim(conn) == 0  # 変換の前は数えられない
    finally:
        conn.close()

    _run_cli(tmp_path, monkeypatch)

    conn = _open(db_path)
    try:
        assert highest_vector_dim(conn) == 1024
        for table in VECTOR_TABLES:
            assert _types(conn, table) == {"blob"}, table
        _assert_converted(conn, expected)  # 拒否したので作り直していない
        assert conn.execute(
            "SELECT value FROM embed_metadata WHERE key = 'embed_model'"
        ).fetchone()[0] == "bigger-model"
    finally:
        conn.close()


def test_reembed_stale_messages_converts_first(tmp_path):
    """会話ログの取り込みのあとの背景の埋め込みも、最初に変換する。"""
    conn = _open(tmp_path / "memory.db")
    try:
        ids = _add_messages(conn, 3)
        values = {}
        for i, mid in enumerate(ids[:2]):
            values[(mid, 0)] = _rand_vec(300 + i)
            _set_json(conn, mid, values[(mid, 0)])
        write_deferred_message_embedding(conn, ids[2])
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
    finally:
        conn.close()

    adapter = _adapter(tmp_path)
    try:
        assert _types(adapter.conn, "message_embeddings") == {"text"}
        result = adapter.reembed_stale_messages()
        assert result["total"] == 1  # 空のベクトル行の 1 件だけ
        assert result["embedded"] == 1
        assert _types(adapter.conn, "message_embeddings") == {"blob"}
        _assert_converted(adapter.conn, {"message_embeddings": values})
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# 読み解けない値が入った memory.db を開く
# ---------------------------------------------------------------------------

_BAD_VALUES = {
    "length not a multiple of 4": b"\x00\x01\x02\x03\x04",
    "not JSON": "not a vector",
    "JSON of non-numbers": json.dumps(["x", "y"]),
    "invalid UTF-8 text": None,  # CAST(x'..' AS TEXT) で入れる
}


def _put_bad_value(conn, table: str, label: str) -> None:
    key_col = _ID_COLUMNS[table][0]
    row_key = conn.execute(f"SELECT {key_col} FROM {table} ORDER BY rowid LIMIT 1").fetchone()[0]
    if label == "invalid UTF-8 text":
        conn.execute(
            f"UPDATE {table} SET vector = CAST(x'fffefd' AS TEXT) WHERE {key_col} = ?",
            (row_key,),
        )
    else:
        conn.execute(
            f"UPDATE {table} SET vector = ? WHERE {key_col} = ?", (_BAD_VALUES[label], row_key),
        )
    conn.commit()


@pytest.mark.parametrize("convert", [True, False])
@pytest.mark.parametrize("label", list(_BAD_VALUES))
@pytest.mark.parametrize("table", VECTOR_TABLES)
def test_unreadable_values_do_not_break_opening(tmp_path, table, label, convert):
    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        _put_bad_value(conn, table, label)
    finally:
        conn.close()

    adapter = _adapter(tmp_path, convert_stored_vectors=convert)
    try:
        assert adapter.is_ready()
        assert adapter.embed_model_changed is True
        assert sample_stale_vector_tables(adapter.conn, EMBED_DIM) == [table]
    finally:
        adapter.close()


@pytest.mark.parametrize("convert", [True, False])
def test_null_vector_does_not_break_opening(tmp_path, convert):
    """``message_embeddings.vector`` だけは NULL を許す列なので、NULL も確かめる。"""
    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        conn.execute(
            "UPDATE message_embeddings SET vector = NULL WHERE rowid = "
            "(SELECT MIN(rowid) FROM message_embeddings)"
        )
        conn.commit()
    finally:
        conn.close()

    adapter = _adapter(tmp_path, convert_stored_vectors=convert)
    try:
        assert adapter.embed_model_changed is True
        assert sample_stale_vector_tables(adapter.conn, EMBED_DIM) == ["message_embeddings"]
    finally:
        adapter.close()


def test_vector_read_sql_matches_the_deferred_vector_constant(tmp_path):
    """``vector_read_sql`` の式の中の ``'[]'`` は ``storage.DEFERRED_VECTOR`` と同じ。"""
    from sai_memory.vector_codec import vector_read_sql

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, vector TEXT)")
        blob = encode_vector([0.5, 0.25])
        conn.execute("INSERT INTO t(id, vector) VALUES(1, ?)", (DEFERRED_VECTOR,))
        conn.execute("INSERT INTO t(id, vector) VALUES(2, ?)", (blob,))
        conn.execute("INSERT INTO t(id, vector) VALUES(3, ?)", (json.dumps([0.5, 0.25]),))
        conn.execute("INSERT INTO t(id, vector) VALUES(4, CAST(x'fffe41' AS TEXT))")
        conn.execute("INSERT INTO t(id, vector) VALUES(5, NULL)")
        got = dict(conn.execute(f"SELECT id, {vector_read_sql('vector')} FROM t"))
        assert got == {1: DEFERRED_VECTOR, 2: blob, 3: None, 4: None, 5: None}
        aliased = dict(conn.execute(f"SELECT e.id, {vector_read_sql('e.vector')} FROM t e"))
        assert aliased == got
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# UTF-8 として読めない文字列の値が混ざった memory.db での検索
# ---------------------------------------------------------------------------

_BAD_TEXT_SQL = "CAST(x'fffe41' AS TEXT)"


def _db_with_invalid_utf8_row(tmp_path, table: str) -> tuple:
    """正常なバイト列形式の行の間に、UTF-8 として読めない文字列の値を一行置く。

    返り値は (接続, 正常な行の ID の集合, 読めない行の ID)。ID は検索の結果の
    ``source_id`` (メッセージは message_id)。
    """
    from sai_memory.arasuji.storage import create_entry
    from sai_memory.memopedia.storage import create_fragment, create_page
    from sai_memory.unified_recall import (
        store_chronicle_embedding,
        store_fragment_embedding,
        store_memopedia_embedding,
    )

    conn = _open(tmp_path / "memory.db")
    seeded = _seed(conn, EMBED_DIM)
    set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
    good_vec = [0.1] * EMBED_DIM

    if table == "message_embeddings":
        bad, after = _add_messages(conn, 2, start=500)
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) "
            f"VALUES(?, 0, {_BAD_TEXT_SQL})",
            (bad,),
        )
        conn.commit()
        replace_message_embeddings(conn, after, [good_vec])
        good = {*seeded["messages"], after}
    elif table == "arasuji_embeddings":
        for entry_id in ("chr_bad", "chr_after"):
            create_entry(conn, level=1, content=f"あらすじ {entry_id}", source_ids=[],
                         start_time=3000, end_time=4000, source_count=1, message_count=1,
                         entry_id=entry_id)
        conn.execute(
            f"INSERT INTO arasuji_embeddings(entry_id, vector) VALUES('chr_bad', {_BAD_TEXT_SQL})"
        )
        conn.commit()
        store_chronicle_embedding(conn, "chr_after", good_vec)
        bad = "chr_bad"
        good = {seeded["chronicle"], "chr_after"}
    elif table == "memopedia_embeddings":
        bad_page = create_page(conn, parent_id="root_terms", title="壊れたページ",
                               summary="読めない値", content="", category="terms")
        after_page = create_page(conn, parent_id="root_terms", title="後ろのページ",
                                 summary="正常な値", content="", category="terms")
        conn.execute(
            f"INSERT INTO memopedia_embeddings(page_id, vector) VALUES(?, {_BAD_TEXT_SQL})",
            (bad_page.id,),
        )
        conn.commit()
        store_memopedia_embedding(conn, after_page.id, good_vec)
        bad = bad_page.id
        good = {seeded["page"], after_page.id}
    else:
        bad_frag = create_fragment(conn, entity_id=seeded["page"], content="壊れた断片")
        after_frag = create_fragment(conn, entity_id=seeded["page"], content="後ろの断片")
        conn.execute(
            "INSERT INTO memopedia_fragment_embeddings(fragment_id, vector) "
            f"VALUES(?, {_BAD_TEXT_SQL})",
            (bad_frag.id,),
        )
        conn.commit()
        store_fragment_embedding(conn, after_frag.id, good_vec)
        bad = bad_frag.id
        good = {seeded["fragment"], after_frag.id}
    return conn, good, bad


_SOURCE_OF_TABLE = {
    "message_embeddings": "message",
    "arasuji_embeddings": "chronicle",
    "memopedia_embeddings": "memopedia",
    "memopedia_fragment_embeddings": "fragment",
}


@pytest.mark.parametrize("table", VECTOR_TABLES)
def test_unified_recall_skips_invalid_utf8_text_rows(tmp_path, table):
    from sai_memory.unified_recall import unified_recall

    conn, good, bad = _db_with_invalid_utf8_row(tmp_path, table)
    try:
        source = _SOURCE_OF_TABLE[table]
        hits = unified_recall(
            conn, FakeEmbedder(), "qqqq",
            topk=20,
            search_chronicle=source == "chronicle",
            search_memopedia=source == "memopedia",
            search_fragments=source == "fragment",
            search_messages=source == "message",
        )
        found = {h.source_id for h in hits if h.source_type == source and h.embed_score is not None}
        assert found and found <= good
        assert bad not in found

        # 読み込みは正常な行を全部返し、読めない行は unreadable に数える
        from sai_memory import unified_recall as ur

        reader = {
            "message": ur.get_message_embeddings,
            "chronicle": lambda c, dim: ur.get_chronicle_embeddings(c, level=1, dim=dim),
            "memopedia": ur.get_memopedia_embeddings,
            "fragment": ur.get_fragment_embeddings,
        }[source]
        corpus = reader(conn, dim=EMBED_DIM)
        assert {row[0] for row in corpus.rows} == good
        assert corpus.unreadable == 1
    finally:
        conn.close()


@pytest.mark.parametrize("groups", [False, True])
def test_semantic_recall_skips_invalid_utf8_text_rows(tmp_path, groups):
    from sai_memory.memory.recall import semantic_recall, semantic_recall_groups

    conn, good, bad = _db_with_invalid_utf8_row(tmp_path, "message_embeddings")
    try:
        fn = semantic_recall_groups if groups else semantic_recall
        out = fn(
            conn, FakeEmbedder(), "qqqq",
            thread_id="p:__persona__", resource_id=None, topk=20,
            range_before=0, range_after=0, scope="thread",
        )
        ids = {seed.id for seed, _group, _score in out} if groups else {m.id for m in out}
        assert good <= ids
        assert bad not in ids
    finally:
        conn.close()


def test_vivid_source_messages_skips_invalid_utf8_text_rows(tmp_path):
    from sai_memory.arasuji.storage import create_entry
    from sai_memory.memopedia.storage import create_fragment
    from sai_memory.unified_recall import store_fragment_embedding
    from sea.auto_recall import _LedgerItem, _vivid_source_messages

    conn, good, bad = _db_with_invalid_utf8_row(tmp_path, "message_embeddings")
    try:
        page_id = conn.execute(
            "SELECT id FROM memopedia_pages WHERE title = '猫ロボット'"
        ).fetchone()[0]
        ordered = [
            mid for (mid,) in conn.execute("SELECT id FROM messages ORDER BY rowid")
        ]
        create_entry(conn, level=1, content="猫ロボットの日", source_ids=ordered,
                     start_time=1000, end_time=20000, source_count=len(ordered),
                     message_count=len(ordered), entry_id="chr_vivid")
        frag = create_fragment(conn, entity_id=page_id, content="耳は三角形",
                               chronicle_entry_id="chr_vivid")
        store_fragment_embedding(conn, frag.id, [0.1] * EMBED_DIM)
        item = _LedgerItem(
            source_type="fragment", source_id=frag.id, title="猫ロボット",
            content="耳は三角形", uri="", best_score=0.9, chronicle_entry_id="chr_vivid",
        )

        result = _vivid_source_messages(conn, item, max_messages=10)

        assert len(result) == len(good)

        # Fragment 自身の値が読めない文字列でも、例外にならずに空を返す
        conn.execute(
            f"UPDATE memopedia_fragment_embeddings SET vector = {_BAD_TEXT_SQL} "
            "WHERE fragment_id = ?",
            (frag.id,),
        )
        conn.commit()
        assert _vivid_source_messages(conn, item, max_messages=10) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 変換で例外になる JSON の値
# ---------------------------------------------------------------------------

_RAISING_JSON = {
    "deeply nested": "[" * 100000 + "]" * 100000,  # json.loads が RecursionError
    "huge integer": "[1" + "0" * 400 + "]",  # encode_vector が OverflowError
}


def _db_with_raising_json_rows(db_path: Path) -> tuple:
    conn = _open(db_path)
    try:
        ids = _add_messages(conn, 6)
        values = {}
        bad = {}
        labels = iter(_RAISING_JSON.items())
        for i, mid in enumerate(ids):
            if i in (1, 3):
                _label, value = next(labels)
                conn.execute(
                    "INSERT INTO message_embeddings(message_id, chunk_index, vector) "
                    "VALUES(?, 0, ?)",
                    (mid, value),
                )
                conn.commit()
                bad[mid] = value
            else:
                values[(mid, 0)] = _rand_vec(400 + i)
                _set_json(conn, mid, values[(mid, 0)])
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
    finally:
        conn.close()
    return values, bad


@pytest.mark.parametrize("batch_size", [1, 1000])
def test_json_rows_that_raise_are_kept_and_the_run_ends(tmp_path, batch_size):
    db_path = tmp_path / "memory.db"
    values, bad = _db_with_raising_json_rows(db_path)
    conn = _open(db_path)
    try:
        result = convert_json_vectors(conn, batch_size=batch_size)

        assert result.tables["message_embeddings"] == {
            "converted": len(values), "kept": len(bad),
        }
        _assert_converted(conn, {"message_embeddings": values})
        rows = dict(conn.execute(
            "SELECT message_id, vector FROM message_embeddings"
        ))
        for mid, value in bad.items():
            assert rows[mid] == value
        # もう一度実行しても、残した行は残したまま
        again = convert_json_vectors(conn, batch_size=batch_size)
        assert again.tables["message_embeddings"] == {"converted": 0, "kept": len(bad)}
    finally:
        conn.close()


def test_reembed_rebuilds_json_rows_that_raise(tmp_path):
    db_path = tmp_path / "memory.db"
    values, bad = _db_with_raising_json_rows(db_path)

    status = _run_api_job(db_path, force=False, manager=None)
    assert status["running"] is False, status
    assert status["total"] == len(bad)

    conn = _open(db_path)
    try:
        assert _types(conn, "message_embeddings") == {"blob"}
        _assert_converted(conn, {"message_embeddings": values})
        for mid in bad:
            (stored,) = conn.execute(
                "SELECT vector FROM message_embeddings WHERE message_id = ?", (mid,),
            ).fetchone()
            assert decode_vector(stored) is not None
        assert _check(conn) is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 変換できずに残した行と起動時の警告
# ---------------------------------------------------------------------------

def _byte_db_with_middle_row(tmp_path, middle_value) -> None:
    """バイト列形式の行を、起動時の検査が見る件数より十分多く入れ、真ん中に一行置く。"""
    from sai_memory.embedding_migration import STARTUP_SAMPLE_PER_END

    n = STARTUP_SAMPLE_PER_END * 2 * 3 + 1
    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        ids = _add_messages(conn, n, start=1000)
        for i, mid in enumerate(ids):
            if i == n // 2 and middle_value is not None:
                conn.execute(
                    "INSERT INTO message_embeddings(message_id, chunk_index, vector) "
                    "VALUES(?, 0, ?)",
                    (mid, middle_value),
                )
                conn.commit()
            else:
                replace_message_embeddings(conn, mid, [_rand_vec(i)])
        assert sample_stale_vector_tables(conn, EMBED_DIM) == []
    finally:
        conn.close()


def test_adapter_flags_rows_kept_by_the_conversion(tmp_path, caplog):
    _byte_db_with_middle_row(tmp_path, json.dumps([_rand_vec(1), _rand_vec(2)]))

    with caplog.at_level(logging.WARNING, logger="saiverse_memory.adapter"):
        adapter = _adapter(tmp_path, convert_stored_vectors=True)
    try:
        assert adapter.embed_model_changed is True
        warned = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "Persona p:" in r.getMessage()
            and "1 vector row(s)" in r.getMessage() and "re-embed" in r.getMessage()
        ]
        assert warned
    finally:
        adapter.close()


def test_adapter_does_not_flag_when_nothing_was_kept(tmp_path):
    _byte_db_with_middle_row(tmp_path, json.dumps(_rand_vec(3)))

    adapter = _adapter(tmp_path, convert_stored_vectors=True)
    try:
        assert adapter.embed_model_changed is False
        assert _types(adapter.conn, "message_embeddings") == {"blob"}
    finally:
        adapter.close()


def test_adapter_flags_a_failed_conversion_even_when_the_sample_is_fresh(tmp_path, monkeypatch):
    import sai_memory.embedding_migration as migration

    _byte_db_with_middle_row(tmp_path, None)

    def _boom(*args, **kwargs):
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(migration, "convert_json_vectors", _boom)
    adapter = _adapter(tmp_path, convert_stored_vectors=True)
    try:
        assert adapter.embed_model_changed is True
    finally:
        adapter.close()


def test_rebuild_after_unreadable_values_leaves_every_row_in_the_byte_format(tmp_path):
    """読み解けない値と JSON 形式の行が残った memory.db も、再計算のあと全行がバイト列形式。"""
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        for table in VECTOR_TABLES:
            _put_bad_value(conn, table, "not JSON")
        (extra,) = _add_messages(conn, 1, start=70)
        _set_json(conn, extra, _rand_vec(70))
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) "
            "VALUES(?, 1, ?)", (extra, encode_vector(_rand_vec(71, 384))),
        )
        conn.commit()
    finally:
        conn.close()

    status = _run_api_job(db_path, force=False, manager=None)
    assert status["running"] is False, status

    conn = _open(db_path)
    try:
        for table in VECTOR_TABLES:
            assert _types(conn, table) == {"blob"}, table
        assert _check(conn) is False
    finally:
        conn.close()
