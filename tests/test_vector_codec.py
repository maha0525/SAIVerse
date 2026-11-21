"""記憶の検索用のベクトルの保存形式 (docs/intent/vector_binary_storage.md) のテスト。

押さえること (「検証」の 1・2 と、§5 の判定):

- ``sai_memory.vector_codec`` の読み書き: バイト列形式は往復で float32 の値が
  完全に一致する。空のベクトル行 ``'[]'``・JSON 形式の値・``None``・長さが合わない
  バイト列は「ベクトル無し」になり、例外にならない。
- 検索の読み込み (``score_vector_rows``) は、一定の行数ごとに類似度を計算し、
  全行を一つの配列にした場合と結果が同じで、行数に比例する大きさの配列を
  確保しない。
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
    DEFAULT_BLOCK_ROWS,
    cosine_scores,
    decode_vector,
    encode_vector,
    score_vector_rows,
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


def test_score_vector_rows_scores_rows_and_counts_the_others():
    rows = [
        (encode_vector([1.0, 0.0]), "a"),
        ("[]", "deferred"),
        (json.dumps([1.0, 0.0]), "json"),
        (encode_vector([1.0, 0.0, 0.0]), "other-dim"),
        (b"\x01\x02\x03", "broken"),
        (None, "null"),
        (encode_vector([0.0, 2.0]), "b"),
        (encode_vector([0.0, 0.0]), "zero"),
    ]
    got = score_vector_rows(iter(rows), [1.0, 1.0])
    assert got.rows == [("a",), ("b",), ("zero",)]
    assert got.scores.dtype == np.float64
    assert got.scores.shape == (3,)
    inv_sqrt2 = np.float32(1.0 / np.sqrt(2.0))
    assert np.allclose(got.scores, [inv_sqrt2, inv_sqrt2, 0.0], atol=1e-7)
    assert got.scores[2] == 0.0  # ノルムが 0 の行は 0.0
    assert got.mismatched == [(("other-dim",), 3)]
    assert got.deferred == [("deferred",)]
    assert got.unreadable == 3  # JSON 形式・長さの合わないバイト列・NULL


def test_score_vector_rows_zero_query_scores_zero():
    rows = [(encode_vector([1.0, 2.0]), "a"), (encode_vector([3.0, 4.0]), "b")]
    got = score_vector_rows(iter(rows), [0.0, 0.0])
    assert np.array_equal(got.scores, np.zeros(2, dtype=np.float64))


def _mixed_rows(n_good: int, rng):
    """次元 DIM の行 n_good 本の間に、数えるだけの行をかたまりの途中・境目に混ぜる。

    返り値は (カーソルに渡す行, 次元 DIM の行のベクトル, 期待する rows,
    期待する mismatched, 期待する deferred, 期待する unreadable)。
    """
    rows = []
    good_vecs = []
    expected_rows = []
    mismatched = []
    deferred = []
    unreadable = 0
    for i in range(n_good):
        # 2 本おきに、数えるだけの行を 1 本挟む (種類は順に回す)
        if i % 2 == 1:
            kind = (i // 2) % 4
            if kind == 0:
                rows.append((encode_vector(rng.standard_normal(OTHER_DIM)), f"mis{i}", i))
                mismatched.append(((f"mis{i}", i), OTHER_DIM))
            elif kind == 1:
                rows.append((DEFERRED_VECTOR, f"def{i}", i))
                deferred.append((f"def{i}", i))
            elif kind == 2:
                rows.append((json.dumps(rng.standard_normal(DIM).tolist()), f"json{i}", i))
                unreadable += 1
            else:
                rows.append((None, f"null{i}", i))
                unreadable += 1
        v = rng.standard_normal(DIM).astype(np.float32)
        if i == 3:
            v = np.zeros(DIM, dtype=np.float32)  # ノルムが 0 の行
        rows.append((encode_vector(v), f"m{i}", i))
        good_vecs.append(v)
        expected_rows.append((f"m{i}", i))
    # 最後にも数えるだけの行を置く (最後のかたまりの後ろ)
    rows.append((None, "tail", -1))
    unreadable += 1
    return rows, good_vecs, expected_rows, mismatched, deferred, unreadable


@pytest.mark.parametrize("n_good", [0, 1, 7, 8, 20])
def test_score_vector_rows_across_block_boundaries_matches_one_matrix(n_good):
    """かたまりに分けて計算しても、全行を一つの配列にした cosine_scores と一致する。"""
    rng = np.random.default_rng(1000 + n_good)
    rows, good_vecs, expected_rows, mismatched, deferred, unreadable = _mixed_rows(n_good, rng)
    query = rng.standard_normal(DIM).astype(np.float32)

    got = score_vector_rows(iter(rows), query, block_rows=7)

    assert got.rows == expected_rows  # カーソルの順番のまま
    assert got.scores.dtype == np.float64
    assert got.scores.shape == (n_good,)
    whole = np.array(good_vecs, dtype=np.float32).reshape(n_good, DIM)
    expected = cosine_scores(whole, query)
    assert np.all(np.abs(got.scores - expected) < 1e-6)
    if n_good > 3:
        assert got.scores[3] == 0.0
    assert got.mismatched == mismatched
    assert got.deferred == deferred
    assert got.unreadable == unreadable


def test_score_vector_rows_default_block_matches_one_matrix():
    """既定のかたまりの行数 (DEFAULT_BLOCK_ROWS) をまたぐ行数でも一致する。"""
    rng = np.random.default_rng(7)
    n = DEFAULT_BLOCK_ROWS * 2 + 5
    vecs = rng.standard_normal((n, DIM)).astype(np.float32)
    query = rng.standard_normal(DIM).astype(np.float32)
    got = score_vector_rows(((encode_vector(v), i) for i, v in enumerate(vecs)), query)
    assert [r[0] for r in got.rows] == list(range(n))
    assert np.all(np.abs(got.scores - cosine_scores(vecs, query)) < 1e-6)


def test_score_vector_rows_memory_does_not_grow_with_rows():
    """全行のベクトルを一つの配列に溜めない (docs/intent/vector_binary_storage.md)。

    30 万行 (768 次元) を、ベクトルを作りながら返すジェネレーターから読ませる。
    全行ぶんの配列なら 300,000 × 768 × 4 バイト = 約 920 MB になる。読み込みの
    最大のメモリ (tracemalloc) が、それよりずっと小さい 150 MB 未満であること。
    """
    import tracemalloc

    n_rows = 300_000
    dim = 768
    vec = np.random.default_rng(3).standard_normal(dim).astype(np.float32)
    query = np.random.default_rng(4).standard_normal(dim).astype(np.float32)

    def rows():
        for i in range(n_rows):
            yield (vec.tobytes(), i)

    tracemalloc.start()
    try:
        got = score_vector_rows(rows(), query)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(got) == n_rows
    assert got.scores.shape == (n_rows,)
    full_matrix_bytes = n_rows * dim * 4
    assert peak < 150 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"
    assert peak < full_matrix_bytes / 5


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


def _score_before_the_byte_format(a: np.ndarray, b: np.ndarray) -> float:
    """バイト列形式にする前の検索が使っていた一行ぶんの計算 (unified_recall の _cosine_sim)。"""
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _score_before_the_byte_format_in_recall(a: np.ndarray, b: np.ndarray) -> float:
    """バイト列形式にする前の semantic_recall の一行ぶんの計算 (recall.py の _cosine_sim)。"""
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def test_cosine_scores_equal_the_scores_before_the_byte_format():
    """類似度は、バイト列形式にする前の一行ずつの計算と完全に同じ値になる。

    行列積でまとめて計算すると 1e-8 ほどの丸めの差が出て、ほぼ同点の記憶の
    順番が入れ替わる (Codex 3 巡目の指摘)。行ごとに同じ演算をするので、値も
    順番も変わらない。行が配列のどこにあっても同じ値になることも確かめる。
    """
    rng = np.random.default_rng(20261122)
    dim = 768
    n = 3000
    vecs = rng.standard_normal((n, dim)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    # ほぼ同点の行: 同じベクトルに、最後の桁だけ違う揺らぎを足す。
    base = vecs[0].copy()
    for k in range(1, 40):
        vecs[k] = base + rng.standard_normal(dim).astype(np.float32) * np.float32(1e-7)
    vecs[40] = 0.0  # ノルムが 0 の行
    query = rng.standard_normal(dim).astype(np.float32)

    # 変更前: JSON から読んだリストを、一行ずつ新しい float32 の配列にしていた。
    expected = np.array(
        [
            _score_before_the_byte_format(query, np.array(vecs[i].tolist(), dtype=np.float32))
            for i in range(n)
        ],
        dtype=np.float64,
    )

    whole = cosine_scores(vecs, query)
    assert np.array_equal(whole.astype(np.float64), expected)

    # かたまりに分けても、行の位置が変わっても、同じ値。
    for block_rows in (1, 7, 4096):
        got = score_vector_rows(
            ((encode_vector(vecs[i]), i) for i in range(n)), query, block_rows=block_rows,
        )
        assert np.array_equal(got.scores.astype(np.float64), expected)
        assert [r[0] for r in got.rows] == list(range(n))
    shifted = cosine_scores(vecs[5:], query)
    assert np.array_equal(shifted.astype(np.float64), expected[5:])

    # 順番 (同点は先に出た行が先) も変わらない。
    assert np.array_equal(
        np.argsort(-whole.astype(np.float64), kind="stable"),
        np.argsort(-expected, kind="stable"),
    )

    # semantic_recall / semantic_recall_groups の変更前の計算 (分母を Python の
    # float にしない) とも、完全に同じ値になる。
    expected_recall = np.array(
        [
            _score_before_the_byte_format_in_recall(
                query, np.array(vecs[i].tolist(), dtype=np.float32),
            )
            for i in range(n)
        ],
        dtype=np.float64,
    )
    assert np.array_equal(
        cosine_scores(vecs, query, float_denominator=False), expected_recall,
    )
    got = score_vector_rows(
        ((encode_vector(vecs[i]), i) for i in range(n)), query,
        block_rows=7, float_denominator=False,
    )
    assert np.array_equal(got.scores, expected_recall)
