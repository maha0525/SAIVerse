"""記憶の検索用のベクトルの保存形式 (docs/intent/vector_binary_storage.md) のテスト。

押さえること (「検証」の 1・2 と、§5 の判定):

- ``sai_memory.vector_codec`` の読み書き: バイト列形式は往復で float32 の値が
  完全に一致する。空のベクトル行 ``'[]'``・JSON 形式の値・``None``・長さが合わない
  バイト列は「ベクトル無し」になり、例外にならない。
- 書き込みの経路 (メッセージ・Chronicle・ページ・Fragment) はバイト列形式で書き、
  空のベクトル行は文字列の ``'[]'`` のまま。
- 検索の結果が変わらない: 作り物のデータに対して、一行ずつ cosine を計算する
  素朴な参照実装と、``unified_recall`` のメッセージ検索・``semantic_recall_groups``
  が、同じ ID を同じ順番で返し、類似度の差が 1e-6 未満。
- 古いベクトルの判定: バイト列形式で次元が合う行だけが古くない。

DB は一時ディレクトリの SQLite。LLM も本物の埋め込みモデルも使わない。
"""
from __future__ import annotations

import json
import logging
import sqlite3

import numpy as np
import pytest

from sai_memory.memory import storage
from sai_memory.memory.storage import (
    DEFERRED_VECTOR,
    add_message,
    get_or_create_thread,
    init_db,
    replace_message_embeddings,
    write_deferred_message_embedding,
)
from sai_memory.vector_codec import (
    decode_vector,
    encode_vector,
    read_vector_rows,
    stored_vector_dim,
)

DIM = 16
OTHER_DIM = 8


# ---------------------------------------------------------------------------
# 読み書きの関数
# ---------------------------------------------------------------------------

def test_roundtrip_keeps_float32_values_exactly():
    rng = np.random.default_rng(0)
    original = rng.standard_normal(768).astype(np.float32)
    for given in (original, original.tolist(), tuple(original.tolist())):
        stored = encode_vector(given)
        assert isinstance(stored, bytes)
        assert len(stored) == 768 * 4
        back = decode_vector(stored)
        assert back is not None
        assert back.dtype == np.float32
        assert back.shape == (768,)
        assert np.array_equal(back, original)


def test_roundtrip_of_python_floats_matches_float32_cast():
    values = [0.1, -0.2, 1e-8, 3.4e38, 0.0]
    back = decode_vector(encode_vector(values))
    assert np.array_equal(back, np.asarray(values, dtype=np.float32))


def test_byte_order_is_little_endian():
    assert encode_vector([1.0]) == b"\x00\x00\x80\x3f"


def test_decode_accepts_bytearray_and_memoryview():
    stored = encode_vector([1.0, 2.0, 3.0])
    for value in (bytearray(stored), memoryview(stored)):
        assert np.array_equal(decode_vector(value), np.array([1.0, 2.0, 3.0], dtype=np.float32))
        assert stored_vector_dim(value) == 3


def test_decoded_array_is_writable_copy():
    stored = encode_vector([1.0, 2.0])
    back = decode_vector(stored)
    back[0] = 5.0  # frombuffer の読み取り専用の配列ではない
    assert decode_vector(stored)[0] == 1.0


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        DEFERRED_VECTOR,
        json.dumps([0.1] * DIM),
        "[[0.1, 0.2], [0.3, 0.4]]",
        "not json",
        None,
        123,
        b"",
        b"\x00\x01\x02",
        b"\x00" * 7,
        bytearray(b"\x00" * 5),
    ],
)
def test_values_other_than_the_byte_format_are_no_vector(value):
    assert decode_vector(value) is None
    assert stored_vector_dim(value) is None


def test_stored_vector_dim_of_byte_format():
    assert stored_vector_dim(encode_vector([0.0] * 768)) == 768
    assert stored_vector_dim(encode_vector([0.0] * 384)) == 384
    assert stored_vector_dim(encode_vector([1.0])) == 1


def test_read_vector_rows_sorts_rows_into_matrix_and_counts():
    rows = [
        (encode_vector([1.0, 0.0]), "a"),
        ("[]", "deferred"),
        (json.dumps([1.0, 0.0]), "json"),
        (encode_vector([1.0, 0.0, 0.0]), "other-dim"),
        (b"\x01\x02\x03", "broken"),
        (encode_vector([0.0, 2.0]), "b"),
    ]
    got = read_vector_rows(iter(rows), dim=2)
    assert got.rows == [("a",), ("b",)]
    assert got.matrix.dtype == np.float32
    assert np.array_equal(got.matrix, np.array([[1.0, 0.0], [0.0, 2.0]], dtype=np.float32))
    assert got.mismatched == [(("other-dim",), 3)]
    assert got.deferred == [("deferred",)]
    assert got.unreadable == 2


def test_read_vector_rows_grows_past_initial_capacity():
    rows = [(encode_vector([float(i), 1.0]), i) for i in range(3000)]
    got = read_vector_rows(iter(rows), dim=2)
    assert len(got) == 3000
    assert got.matrix.shape == (3000, 2)
    assert got.matrix[2999, 0] == 2999.0
    assert [r[0] for r in got.rows] == list(range(3000))


def test_read_vector_rows_with_no_rows():
    got = read_vector_rows(iter([]), dim=4)
    assert got.rows == []
    assert got.matrix.shape == (0, 4)


# ---------------------------------------------------------------------------
# 書き込みの経路
# ---------------------------------------------------------------------------

def _typeofs(conn, table):
    return [r[0] for r in conn.execute(f"SELECT typeof(vector) FROM {table}")]


def test_writers_store_byte_format_and_deferred_stays_text(tmp_path):
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.memopedia import init_memopedia_tables
    from sai_memory.unified_recall import (
        store_chronicle_embedding,
        store_fragment_embedding,
        store_memopedia_embedding,
    )

    conn = init_db(str(tmp_path / "memory.db"))
    init_memopedia_tables(conn)
    init_arasuji_tables(conn)
    try:
        get_or_create_thread(conn, "t1", "p")
        m1 = add_message(conn, thread_id="t1", role="user", content="一", resource_id="p")
        m2 = add_message(conn, thread_id="t1", role="user", content="二", resource_id="p")
        replace_message_embeddings(conn, m1, [[0.1] * DIM, np.ones(DIM)])
        storage.upsert_embedding(conn, m2, [0.2] * DIM)
        m3 = add_message(conn, thread_id="t1", role="user", content="三", resource_id="p")
        write_deferred_message_embedding(conn, m3)
        store_chronicle_embedding(conn, "c1", [0.3] * DIM)
        store_memopedia_embedding(conn, "p1", [0.4] * DIM)
        store_fragment_embedding(conn, "f1", [0.5] * DIM)

        rows = dict(
            ((mid, idx), (kind, value))
            for mid, idx, kind, value in conn.execute(
                "SELECT message_id, chunk_index, typeof(vector), vector FROM message_embeddings"
            )
        )
        assert rows[(m1, 0)][0] == "blob"
        assert rows[(m1, 1)][0] == "blob"
        assert np.array_equal(decode_vector(rows[(m1, 1)][1]), np.ones(DIM, dtype=np.float32))
        assert rows[(m2, 0)][0] == "blob"
        assert rows[(m3, 0)] == ("text", DEFERRED_VECTOR)
        assert storage.count_deferred_message_embeddings(conn) == 1
        for table in ("arasuji_embeddings", "memopedia_embeddings", "memopedia_fragment_embeddings"):
            assert _typeofs(conn, table) == ["blob"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 古いベクトルの判定 (sai_memory/embedding_migration.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "stale"),
    [
        (encode_vector([0.1] * DIM), False),
        (encode_vector([0.1] * OTHER_DIM), True),
        (json.dumps([0.1] * DIM), True),  # JSON 形式は次元が合っていても古い
        (json.dumps([0.1] * OTHER_DIM), True),
        ("[[0.1, 0.2]]", True),  # 一行に複数のベクトルを入れた旧形式
        (DEFERRED_VECTOR, True),  # 空のベクトル行は従来どおり古い
        ("not json", True),
        (b"\x00\x01\x02", True),
        (b"", True),
        (None, True),
        (42, True),
    ],
)
def test_is_stale_vector(value, stale):
    from sai_memory.embedding_migration import is_stale_vector

    assert is_stale_vector(value, DIM) is stale


def test_stale_checks_count_json_rows_and_skip_nothing_else(tmp_path):
    from sai_memory.embedding_migration import (
        highest_vector_dim,
        message_reembed_targets,
        sample_stale_vector_tables,
        scan_stale_vector_tables,
    )

    conn = init_db(str(tmp_path / "memory.db"))
    try:
        get_or_create_thread(conn, "t1", "p")
        fresh = add_message(conn, thread_id="t1", role="user", content="新", resource_id="p")
        replace_message_embeddings(conn, fresh, [[0.1] * DIM])
        assert scan_stale_vector_tables(conn, DIM) == []
        assert sample_stale_vector_tables(conn, DIM) == []
        assert message_reembed_targets(conn, DIM, force=False, include_missing=False) == set()
        assert highest_vector_dim(conn) == DIM

        as_json = add_message(conn, thread_id="t1", role="user", content="旧", resource_id="p")
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES (?, 0, ?)",
            (as_json, json.dumps([0.1] * DIM)),
        )
        broken = add_message(conn, thread_id="t1", role="user", content="壊", resource_id="p")
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES (?, 0, ?)",
            (broken, b"\x00\x01\x02"),
        )
        conn.commit()
        assert scan_stale_vector_tables(conn, DIM) == ["message_embeddings"]
        assert message_reembed_targets(conn, DIM, force=False, include_missing=False) == {as_json, broken}
        # JSON 形式の行と読み解けない値は、次元の最大値に数えない
        assert highest_vector_dim(conn) == DIM
    finally:
        conn.close()


def test_reembed_writes_byte_format(tmp_path):
    from sai_memory.embedding_migration import reembed_messages

    class _Embedder:
        model_name = "test"
        embedding_size = DIM

        def embed(self, texts, *, is_query=False):
            return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    conn = init_db(str(tmp_path / "memory.db"))
    try:
        get_or_create_thread(conn, "t1", "p")
        mid = add_message(conn, thread_id="t1", role="user", content="本文", resource_id="p")
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES (?, 0, ?)",
            (mid, json.dumps([0.1] * DIM)),
        )
        conn.commit()
        result = reembed_messages(conn, _Embedder(), {mid}, chunk_min=120, chunk_max=480)
        assert result["embedded"] == 1
        kinds = [r[0] for r in conn.execute(
            "SELECT typeof(vector) FROM message_embeddings WHERE message_id = ?", (mid,))]
        assert kinds and set(kinds) == {"blob"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 検索の結果が変わらないこと
# ---------------------------------------------------------------------------

class _QueryEmbedder:
    """クエリに決まったベクトルを返す偽の Embedder。"""

    model_name = "test"

    def __init__(self, query_vec):
        self._q = [float(x) for x in query_vec]

    def embed(self, texts, *, is_query=False):
        return [list(self._q) for _ in texts]


@pytest.fixture()
def search_db(tmp_path):
    """作り物のベクトルを数百行持つ memory.db。

    - 一チャンク / 複数チャンクのメッセージ (バイト列形式、次元 DIM)
    - 次元の合わない行 (バイト列形式、次元 OTHER_DIM)
    - 空のベクトル行
    - JSON 形式の行 (次元 DIM。検索では飛ばす)
    - ゼロベクトルの行 (類似度 0.0)
    """
    rng = np.random.default_rng(20261121)
    conn = init_db(str(tmp_path / "memory.db"))
    get_or_create_thread(conn, "t1", "p")
    chunks: dict[str, list] = {}
    kinds: dict[str, str] = {}
    t = 1000
    for i in range(220):
        t += 1
        role = "user" if i % 2 == 0 else "assistant"
        mid = add_message(
            conn, thread_id="t1", role=role,
            content=f"メッセージ {i} の本文。" * (1 + i % 5),
            resource_id="p", created_at=t,
        )
        if i % 37 == 5:
            write_deferred_message_embedding(conn, mid)
            kinds[mid] = "deferred"
            continue
        if i % 41 == 7:
            conn.execute(
                "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES (?, 0, ?)",
                (mid, json.dumps(rng.standard_normal(DIM).tolist())),
            )
            conn.commit()
            kinds[mid] = "json"
            continue
        if i % 29 == 3:
            replace_message_embeddings(conn, mid, [rng.standard_normal(OTHER_DIM).tolist()])
            kinds[mid] = "mismatch"
            continue
        n_chunks = 1 + (i % 3)
        vecs = [rng.standard_normal(DIM).astype(np.float32) for _ in range(n_chunks)]
        if i == 50:
            vecs = [np.zeros(DIM, dtype=np.float32)]
        replace_message_embeddings(conn, mid, [v.tolist() for v in vecs])
        chunks[mid] = vecs
        kinds[mid] = "byte"
    query = rng.standard_normal(DIM).astype(np.float32)
    yield conn, chunks, kinds, query
    conn.close()


def _reference_scores(conn, query, allowed_ids):
    """一行ずつ cosine を計算する素朴な参照実装 (メッセージごとに最良のチャンク)。

    保存された値を、この関数の中で独立に読み解く (バイト列で長さが DIM * 4 の
    行だけを比べる)。
    """
    q = np.asarray(query, dtype=np.float32)
    best: dict[str, tuple[float, int]] = {}
    for mid, chunk_index, value in conn.execute(
        "SELECT message_id, chunk_index, vector FROM message_embeddings ORDER BY rowid"
    ):
        if mid not in allowed_ids:
            continue
        if not isinstance(value, bytes) or len(value) != DIM * 4:
            continue
        v = np.frombuffer(value, dtype="<f4")
        denom = float(np.linalg.norm(q)) * float(np.linalg.norm(v))
        score = 0.0 if denom == 0 else float(np.dot(q, v)) / denom
        prev = best.get(mid)
        if prev is None or score > prev[0]:
            best[mid] = (score, int(chunk_index))
    return sorted(best.items(), key=lambda kv: -kv[1][0])


def test_unified_recall_message_search_matches_reference(search_db):
    from sai_memory.unified_recall import reconstruct_message_excerpt, unified_recall

    conn, chunks, kinds, query = search_db
    reference = _reference_scores(conn, query, set(chunks))
    cap = 60
    hits = unified_recall(
        conn, _QueryEmbedder(query), "クエリ",
        topk=cap,
        search_chronicle=False, search_memopedia=False,
        search_fragments=False, search_messages=True,
        keywords=[],  # キーワード検索をしない (埋め込みの順位だけで並ぶ)
        source_allocations={"message": cap},
    )
    assert [h.source_id for h in hits] == [mid for mid, _ in reference[:cap]]
    for hit, (mid, (score, chunk_index)) in zip(hits, reference):
        assert abs(hit.embed_score - score) < 1e-6
        row = conn.execute(
            "SELECT content, role, created_at FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        excerpt, used = reconstruct_message_excerpt(conn, mid, chunk_index, row[0])
        assert hit.content == excerpt
        assert hit.chunk_index == used
        assert hit.start_time == row[2]
        assert hit.title.startswith(f"{row[1]} @ ")
    # ゼロベクトルのメッセージは類似度 0.0 で並ぶ
    zero_mid = next(mid for mid, vecs in chunks.items() if not np.any(vecs[0]))
    assert dict(reference)[zero_mid][0] == 0.0


def test_unified_recall_keyword_hit_gets_cosine_of_full_scan(search_db):
    """上位に残らなかったメッセージにも、全行の類似度の値 (embed_all_scores) が付く。"""
    from sai_memory.unified_recall import unified_recall

    conn, chunks, kinds, query = search_db
    reference = _reference_scores(conn, query, set(chunks))
    low_mid, (low_score, _c) = reference[-1]
    content = conn.execute("SELECT content FROM messages WHERE id = ?", (low_mid,)).fetchone()[0]
    keyword = content.split("。")[0]
    hits = unified_recall(
        conn, _QueryEmbedder(query), "クエリ",
        topk=1,
        search_chronicle=False, search_memopedia=False,
        search_fragments=False, search_messages=True,
        keywords=[keyword],
        source_allocations={"message": 1},
    )
    assert [h.source_id for h in hits] == [low_mid]
    assert abs(hits[0].embed_score - low_score) < 1e-6


def test_unified_recall_excluded_ids_are_left_out_before_ranking(search_db):
    from sai_memory.unified_recall import unified_recall

    conn, chunks, kinds, query = search_db
    reference = _reference_scores(conn, query, set(chunks))
    excluded = {reference[0][0], reference[2][0]}
    excluded_key = reference[3][0]
    expected = [
        mid for mid, _ in reference if mid not in excluded and mid != excluded_key
    ][:10]
    hits = unified_recall(
        conn, _QueryEmbedder(query), "クエリ",
        topk=10,
        search_chronicle=False, search_memopedia=False,
        search_fragments=False, search_messages=True,
        keywords=[],
        source_allocations={"message": 10},
        exclude_message_ids=excluded,
        exclude_keys={("message", excluded_key)},
    )
    assert [h.source_id for h in hits] == expected


def test_semantic_recall_groups_matches_reference(search_db, caplog):
    from sai_memory.memory.recall import semantic_recall, semantic_recall_groups

    conn, chunks, kinds, query = search_db
    reference = _reference_scores(conn, query, set(chunks))
    with caplog.at_level(logging.DEBUG):
        groups = semantic_recall_groups(
            conn, _QueryEmbedder(query), "クエリ",
            thread_id="t1", resource_id=None, topk=80,
            range_before=0, range_after=0, scope="thread",
        )
    assert [seed.id for seed, _bundle, _s in groups] == [mid for mid, _ in reference[:80]]
    for (seed, bundle, score), (mid, (ref_score, _c)) in zip(groups, reference):
        assert abs(score - ref_score) < 1e-6
        assert [m.id for m in bundle] == [mid]
        assert seed.content == conn.execute(
            "SELECT content FROM messages WHERE id = ?", (mid,)).fetchone()[0]

    # 次元の合わない行には、メッセージごとに WARNING を出す (今までどおり)
    mismatch_ids = {mid for mid, k in kinds.items() if k == "mismatch"}
    warned = {
        r.args[1] for r in caplog.records
        if r.levelno == logging.WARNING and "dim mismatch" in r.getMessage()
    }
    assert warned == mismatch_ids
    # JSON 形式の行は WARNING にしない
    json_ids = {mid for mid, k in kinds.items() if k == "json"}
    assert json_ids
    assert not any(
        mid in r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING for mid in json_ids
    )

    msgs = semantic_recall(
        conn, _QueryEmbedder(query), "クエリ",
        thread_id="t1", resource_id=None, topk=5,
        range_before=0, range_after=0, scope="thread",
    )
    assert {m.id for m in msgs} == {mid for mid, _ in reference[:5]}


def test_semantic_recall_groups_excludes_ids(search_db):
    from sai_memory.memory.recall import semantic_recall_groups

    conn, chunks, kinds, query = search_db
    reference = _reference_scores(conn, query, set(chunks))
    excluded = {reference[0][0], reference[1][0]}
    groups = semantic_recall_groups(
        conn, _QueryEmbedder(query), "クエリ",
        thread_id="t1", resource_id="p", topk=5,
        range_before=0, range_after=0, scope="resource",
        exclude_message_ids=excluded,
    )
    assert [seed.id for seed, _b, _s in groups] == [
        mid for mid, _ in reference if mid not in excluded
    ][:5]


def test_search_skips_json_rows_without_error(tmp_path):
    """JSON 形式の行しか無い memory.db でも、検索は例外にならず空を返す。"""
    from sai_memory.memory.recall import semantic_recall_groups
    from sai_memory.unified_recall import unified_recall

    conn = init_db(str(tmp_path / "memory.db"))
    try:
        get_or_create_thread(conn, "t1", "p")
        mid = add_message(conn, thread_id="t1", role="user", content="本文", resource_id="p")
        conn.execute(
            "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES (?, 0, ?)",
            (mid, json.dumps([0.1] * DIM)),
        )
        conn.commit()
        q = [1.0] + [0.0] * (DIM - 1)
        assert semantic_recall_groups(
            conn, _QueryEmbedder(q), "q", thread_id="t1", resource_id=None,
            topk=5, range_before=0, range_after=0, scope="thread",
        ) == []
        hits = unified_recall(
            conn, _QueryEmbedder(q), "q",
            search_chronicle=False, search_memopedia=False,
            search_fragments=False, search_messages=True, keywords=[],
        )
        assert hits == []
    finally:
        conn.close()


def test_unreadable_stored_value_does_not_break_open(tmp_path):
    """読み解けない値が入っていても memory.db を開く処理は失敗しない。"""
    db = tmp_path / "memory.db"
    conn = init_db(str(db))
    conn.execute(
        "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES ('x', 0, ?)",
        (b"\x00\x01\x02",),
    )
    conn.commit()
    conn.close()
    conn = init_db(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0] == 1
    finally:
        conn.close()


def test_sqlite_keeps_blob_in_text_column(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    try:
        conn.execute("CREATE TABLE t (vector TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (encode_vector([1.0, 2.0]),))
        value, kind = conn.execute("SELECT vector, typeof(vector) FROM t").fetchone()
        assert kind == "blob"
        assert np.array_equal(decode_vector(value), np.array([1.0, 2.0], dtype=np.float32))
    finally:
        conn.close()
