"""saiverse:// の Memopedia ページ表示 (_format_memopedia_page) の契約テスト。

docs/intent/memopedia_body_to_fragment.md §7 の表示側:
- 本文は content + Fragment を合わせて描く (render_page_body)。v0.3.x は知識が
  Fragment 側にあり、content 直出しだと変換済みページが全部 "(empty)" に見える
- 子ページの一覧は親子関係から組み立てる (本文には導線を書かない設計の対)
"""
import sqlite3

import pytest

from sai_memory.memopedia.core import Memopedia
from sai_memory.memopedia.storage import (
    create_fragment,
    create_page,
    init_memopedia_tables,
)
from saiverse.uri_resolver import UriResolver


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    init_memopedia_tables(connection)
    yield connection
    connection.close()


def _render(memopedia: Memopedia, page_id: str) -> str:
    resolver = UriResolver(manager=None)
    snapshot = memopedia.page_snapshot(page_ref=page_id)
    return resolver._format_memopedia_page(snapshot)


def test_fragments_appear_in_page_view(conn):
    """⭐ 本文が空でも Fragment があれば "(empty)" にならない。"""
    page = create_page(conn, parent_id=None, title="アイフィ", content="", category="people")
    create_fragment(conn, entity_id=page.id, content="散歩で紅葉を見た", source_date="2026-05-15")
    memopedia = Memopedia(conn)

    text = _render(memopedia, page.id)
    assert "(empty)" not in text
    assert "## 2026-05-15" in text
    assert "- 散歩で紅葉を見た" in text


def test_children_are_listed_from_relations(conn):
    """子ページの一覧が親子関係から組み立てられ、m:short_id で参照できる。"""
    parent = create_page(conn, parent_id=None, title="まはー", content="本文", category="people")
    child = create_page(conn, parent_id=parent.id, title="まはーの好み", content="", category="people")
    memopedia = Memopedia(conn)

    text = _render(memopedia, parent.id)
    assert "子ページ:" in text
    assert "まはーの好み" in text
    assert f"m:{child.short_id}" in text


def test_page_without_anything_is_empty(conn):
    """本文も Fragment も子も無いページは従来どおり "(empty)"。"""
    page = create_page(conn, parent_id=None, title="空", content="", category="people")
    memopedia = Memopedia(conn)

    text = _render(memopedia, page.id)
    assert "(empty)" in text
    assert "子ページ:" not in text


def test_handwritten_body_is_verbatim(conn):
    """⭐ 手書き本文は原文のまま出る (先頭の字下げ・行末の空白・末尾の改行)。

    表示の都合で削ると、AI が読む本文が書かれたものと変わる。行末の空白は
    Markdown の改行なので、削ると意味まで変わる。
    """
    body = "  字下げした行\n行末に空白  \n\n最後は改行で終わる\n"
    page = create_page(conn, parent_id=None, title="手書き", content=body, category="people")
    memopedia = Memopedia(conn)

    assert memopedia.render_page_body(page.id) == body
    assert body in _render(memopedia, page.id)


def test_snapshot_reads_page_body_and_children_together(conn):
    """ページ・本文・子一覧が一つのスナップショットで返る。"""
    parent = create_page(conn, parent_id=None, title="親", content="本文", category="people")
    child = create_page(conn, parent_id=parent.id, title="子", content="", category="people")
    create_fragment(conn, entity_id=parent.id, content="断片", source_date="2026-08-06")
    memopedia = Memopedia(conn)

    snapshot = memopedia.page_snapshot(page_ref=parent.id)
    assert snapshot["page"].id == parent.id
    assert "本文" in snapshot["body"]
    assert "- 断片" in snapshot["body"]
    assert [c.id for c in snapshot["children"]] == [child.id]

    assert memopedia.page_snapshot(title="親")["page"].id == parent.id
    assert memopedia.page_snapshot(page_ref="no-such-page") is None
