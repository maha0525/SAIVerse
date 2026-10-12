"""再計算が消す範囲と、生成器が作り直す範囲が一致していることの歯止め。

``sai_memory/embedding_migration.py`` の ``_SOURCES`` は、force の再計算で
消してよい行 (= 生成器がすぐ作り直す行) を SQL で持っている。生成器側の
「ベクトルが無い行」の取得 (``sai_memory/unified_recall.py`` の
``get_*_without_embeddings``) と同じ範囲を二か所で定義しているので、片方だけ
変わると、作り直されないベクトルを消す (想起から黙って消える) か、作り直す
行を古いまま残す。全ベクトルが無い状態で両者の id 集合が一致することを見る。

Chronicle は level 1 と 2、ページは通常・root_・trunk・Chronicle (category=
'chronicle')・ごみ箱、Fragment は通常のものを仕込む。DB は一時ディレクトリの
SQLite。LLM も埋め込みモデルも使わない。
"""
from __future__ import annotations

import pytest

from sai_memory.arasuji import init_arasuji_tables
from sai_memory.arasuji.storage import create_entry
from sai_memory.embedding_migration import _SOURCES
from sai_memory.memopedia import init_memopedia_tables
from sai_memory.memopedia.storage import create_fragment, create_page
from sai_memory.memory.storage import init_db
from sai_memory.unified_recall import (
    get_chronicle_entries_without_embeddings,
    get_fragments_without_embeddings,
    get_memopedia_pages_without_embeddings,
)


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_memopedia_tables(c)
    init_arasuji_tables(c)
    for level, entry_id in ((1, "chr_l1_a"), (1, "chr_l1_b"), (2, "chr_l2")):
        create_entry(c, level=level, content=f"あらすじ {entry_id}", source_ids=[],
                     start_time=1, end_time=2, source_count=1, message_count=1,
                     entry_id=entry_id)
    normal = create_page(c, parent_id="root_terms", title="猫ロボット",
                         summary="設計中のロボット", category="terms")
    create_page(c, parent_id="root_terms", title="幹のページ", summary="幹",
                category="terms", is_trunk=True)
    trashed = create_page(c, parent_id="root_terms", title="ごみ箱のページ",
                          summary="消した", category="terms")
    c.execute("UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?", (trashed.id,))
    c.commit()
    create_fragment(c, entity_id=normal.id, content="耳は三角形", source_date="2026-10-12")
    create_fragment(c, entity_id=trashed.id, content="消したページの断片",
                    source_date="2026-10-12")
    # 全ベクトルが無い状態 (force で全部消した直後と同じ)
    for table in ("arasuji_embeddings", "memopedia_embeddings",
                  "memopedia_fragment_embeddings"):
        c.execute(f"DELETE FROM {table}")
    c.commit()
    yield c
    c.close()


def _regenerable(conn, source):
    return {row[0] for row in conn.execute(_SOURCES[source][3])}


def test_fixture_covers_every_excluded_kind(conn):
    """仕込みが除外の各種類を本当に含んでいること (歯止めが空振りしないため)。"""
    ids = {r[0]: r for r in conn.execute(
        "SELECT id, is_trunk, category FROM memopedia_pages")}
    assert any(i.startswith("root_") for i in ids)
    assert any(r[1] == 1 for r in ids.values())
    assert any(r[2] == "chronicle" for r in ids.values())
    assert conn.execute("SELECT COUNT(*) FROM arasuji_entries WHERE level = 2").fetchone()[0] == 1


def test_chronicle_ranges_match(conn):
    getter = {row[0] for row in get_chronicle_entries_without_embeddings(conn, level=1)}
    assert getter == {"chr_l1_a", "chr_l1_b"}
    assert _regenerable(conn, "chronicle") == getter


def test_memopedia_page_ranges_match(conn):
    getter = {row[0] for row in get_memopedia_pages_without_embeddings(conn)}
    assert getter  # 通常のページが入っている
    assert _regenerable(conn, "memopedia") == getter


def test_fragment_ranges_match(conn):
    getter = {row[0] for row in get_fragments_without_embeddings(conn)}
    assert len(getter) == 2
    assert _regenerable(conn, "fragment") == getter
