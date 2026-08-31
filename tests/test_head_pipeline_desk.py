"""Tests for DeskSection (head 机セクション、concept_consolidation.md「机の物理」).

capture は実 DB (一時ディレクトリの memory.db) 上の Memory Atlas を経由して
snapshot を取る。adapter は snapshot_desk が触る最小 surface (conn / _db_lock /
persona_id) を持つ軽量フェイクで代替する (Embedder 等の重い初期化を避ける)。
render / diff / serialize は tests/test_head_pipeline_building.py の流儀に従い
snapshot 直組みで検証する。
"""
from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sea.head_pipeline import LineHeadInput
from sea.head_pipeline.sections.desk import (
    DeskPageItem,
    DeskSection,
    DeskSnapshot,
)


@pytest.fixture
def fake_adapter(tmp_path):
    """snapshot_desk が要求する最小 surface (conn / _db_lock / persona_id)。"""
    from sai_memory.desk import init_desk_tables
    from sai_memory.memopedia.storage import init_memopedia_tables
    from sai_memory.clips import init_clips_tables

    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    # 実 SAIMemoryAdapter が __init__ で eager 初期化するテーブル群のうち、
    # ページ描画 (_read_memopedia → 貼られたクリップの列挙) が触る分を再現する
    init_memopedia_tables(conn)
    init_desk_tables(conn)
    init_clips_tables(conn)
    adapter = SimpleNamespace(
        conn=conn, _db_lock=threading.RLock(), persona_id="tester"
    )
    yield adapter
    conn.close()


def _open_page(adapter, title="開いた本", content="開いた中身"):
    """Memopedia ページを作って机に開き、その ref を返す。"""
    from saiverse import memory_atlas as atlas
    from sai_memory.memopedia import Memopedia

    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
    page = memopedia.create_page(
        parent_id="root_terms", title=title, content=content
    )
    ref = f"memopedia:{page.short_id}"
    atlas.open_page(adapter, ref)
    return ref


def _ctx(persona=None):
    return LineHeadInput(
        persona_id="tester", line_id="main", line_role="main_line",
        model_key="claude-opus-4-7", persona=persona,
    )


# ----- capture -----


def test_capture_snapshots_open_pages_via_adapter(fake_adapter):
    ref = _open_page(fake_adapter)
    persona = SimpleNamespace(sai_memory=fake_adapter, persona_name="エア")

    snapshot = DeskSection().capture(_ctx(persona=persona))

    assert len(snapshot.pages) == 1
    page = snapshot.pages[0]
    assert page.ref == ref
    assert "開いた本" in page.text
    assert "開いた中身" in page.text
    assert snapshot.evicted_by_budget == ()
    assert snapshot.dropped_missing == ()


def test_capture_reports_dropped_missing_refs(fake_adapter):
    # 実体の無い ref を机に直接挿入 → capture 時の掃除で dropped_missing に入る
    from sai_memory.desk import open_item

    open_item(fake_adapter.conn, "memopedia:999")
    persona = SimpleNamespace(sai_memory=fake_adapter, persona_name="エア")

    snapshot = DeskSection().capture(_ctx(persona=persona))

    assert snapshot.pages == ()
    assert "memopedia:999" in snapshot.dropped_missing
    assert snapshot.evicted_by_budget == ()  # 実体消失は溢れに混ざらない


def test_capture_reports_evicted_by_budget(fake_adapter, monkeypatch):
    # 開いた後に予算が縮む → capture 時の再評価で evicted_by_budget に入る。
    # LRU の同点 (同秒 touch) を避けるため仮想クロックで時刻を分ける
    from datetime import datetime

    from saiverse import clock

    clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
    try:
        monkeypatch.setenv("SAIVERSE_DESK_BUDGET_CHARS", "10000")
        ref_old = _open_page(fake_adapter, title="古", content="あ" * 50)
        clock.advance_to(datetime(2026, 7, 6, 9, 10, 0))
        ref_new = _open_page(fake_adapter, title="新", content="い" * 50)
        persona = SimpleNamespace(sai_memory=fake_adapter, persona_name="エア")

        monkeypatch.setenv("SAIVERSE_DESK_BUDGET_CHARS", "60")
        snapshot = DeskSection().capture(_ctx(persona=persona))
    finally:
        clock.disable_virtual()

    assert snapshot.evicted_by_budget == (ref_old,)
    assert snapshot.dropped_missing == ()
    assert [p.ref for p in snapshot.pages] == [ref_new]


def test_capture_returns_empty_without_persona():
    snapshot = DeskSection().capture(_ctx(persona=None))
    assert snapshot.pages == ()
    assert snapshot.evicted_by_budget == ()
    assert snapshot.dropped_missing == ()


def test_capture_returns_empty_without_adapter():
    persona = SimpleNamespace(persona_name="エア")  # sai_memory なし
    snapshot = DeskSection().capture(_ctx(persona=persona))
    assert snapshot.pages == ()


def test_capture_returns_empty_when_adapter_conn_is_none():
    # SAIMEMORY_MEMORY 無効時の adapter (conn=None) 相当
    adapter = SimpleNamespace(conn=None)
    persona = SimpleNamespace(sai_memory=adapter, persona_name="エア")
    snapshot = DeskSection().capture(_ctx(persona=persona))
    assert snapshot.pages == ()


# ----- render -----


def test_render_lists_pages_under_heading():
    snap = DeskSnapshot(pages=(
        DeskPageItem(ref="memopedia:1", text="# 一冊目 (memopedia:1)\n\n本文A"),
        DeskPageItem(ref="chronicle:2", text="# chronicle:2 (レベル1)\n\n本文B"),
    ))
    rendered = DeskSection().render(snap)
    assert rendered is not None
    assert "## 机に開いているページ" in rendered.text
    assert "本文A" in rendered.text
    assert "本文B" in rendered.text
    # 使い方の説明 (memory_close への導線) が焼き込まれている
    assert "memory_close" in rendered.text


def test_render_returns_none_for_empty_desk():
    assert DeskSection().render(DeskSnapshot(pages=())) is None


def test_render_returns_none_for_none_snapshot():
    assert DeskSection().render(None) is None


# ----- diff_to_notifications -----


def test_diff_notifies_eviction_with_overflow_wording():
    section = DeskSection()
    old = DeskSnapshot(pages=(DeskPageItem(ref="memopedia:1", text="a"),))
    new = DeskSnapshot(pages=(), evicted_by_budget=("memopedia:1", "chronicle:2"))

    labels = section.diff_to_notifications(old, new)
    assert len(labels) == 1
    assert labels[0].kind == "desk_evicted"
    assert "机が溢れたため" in labels[0].label
    assert "棚に戻しました" in labels[0].label
    assert "memopedia:1" in labels[0].label
    assert "chronicle:2" in labels[0].label


def test_diff_notifies_drop_with_missing_wording():
    # 実体消失は「溢れたため」と言わない (理由が違うものを同じ文言にしない)
    section = DeskSection()
    new = DeskSnapshot(pages=(), dropped_missing=("memopedia:9",))

    labels = section.diff_to_notifications(None, new)
    assert len(labels) == 1
    assert labels[0].kind == "desk_dropped"
    assert "失われたため" in labels[0].label
    assert "memopedia:9" in labels[0].label
    assert "溢れた" not in labels[0].label


def test_diff_notifies_both_kinds_as_two_labels():
    section = DeskSection()
    new = DeskSnapshot(
        pages=(),
        evicted_by_budget=("memopedia:1",),
        dropped_missing=("memopedia:9",),
    )

    labels = section.diff_to_notifications(None, new)
    assert [label.kind for label in labels] == ["desk_evicted", "desk_dropped"]
    assert "memopedia:1" in labels[0].label
    assert "memopedia:9" in labels[1].label


def test_diff_silent_when_nothing_closed_by_system():
    # 本人の open/close による差分は通知しない (ページ構成が変わっても沈黙)
    section = DeskSection()
    old = DeskSnapshot(pages=(DeskPageItem(ref="memopedia:1", text="a"),))
    new = DeskSnapshot(pages=(DeskPageItem(ref="memopedia:2", text="b"),))
    assert section.diff_to_notifications(old, new) == []


def test_diff_silent_when_new_is_none():
    section = DeskSection()
    old = DeskSnapshot(pages=(), evicted_by_budget=("memopedia:1",))
    assert section.diff_to_notifications(old, None) == []


# ----- serialize / deserialize -----


def test_serialize_deserialize_roundtrip():
    section = DeskSection()
    snap = DeskSnapshot(
        pages=(
            DeskPageItem(ref="memopedia:1", text="# 一冊目\n本文", purpose_ref="task:2"),
            DeskPageItem(ref="chronicle:3", text="章の本文"),
        ),
        evicted_by_budget=("memopedia:9",),
        dropped_missing=("memopedia:8",),
    )
    data = section.serialize_snapshot(snap)
    assert section.deserialize_snapshot(data) == snap


def test_serialize_deserialize_roundtrip_empty():
    section = DeskSection()
    snap = DeskSnapshot(pages=())
    data = section.serialize_snapshot(snap)
    assert section.deserialize_snapshot(data) == snap


# ----- protocol / registry -----


@pytest.fixture
def section():
    return DeskSection()


def test_section_satisfies_head_section_protocol(section):
    from sea.head_pipeline import HeadSection
    assert isinstance(section, HeadSection)


def test_section_registered_in_defaults():
    from sea.head_pipeline.registry import HeadSectionRegistry
    from sea.head_pipeline.sections import register_default_sections

    registry = HeadSectionRegistry()
    register_default_sections(registry)
    names = [s.name for s in registry.all_sections()]
    assert "desk" in names
    # order 730 = memory_weave(700) の直後 (P3c①で open_notes(720) は退役)
    assert names.index("memory_weave") < names.index("desk")
