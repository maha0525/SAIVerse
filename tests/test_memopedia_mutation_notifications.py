"""実スペル → outbox 配送 → 知覚の集約・消費を、合成データだけで通す。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from builtin_data.tools.memory_delete import memory_delete
from builtin_data.tools.memory_write import memory_write
from database.models import AI, Base, ExecutionOutboxItem
from sai_memory import desk
from sai_memory.memopedia import Memopedia, init_memopedia_tables
from sai_memory.perception_buffer import (
    create_consumption_batch,
    format_perception_message,
    init_perception_buffer_table,
    list_pending,
    reduce_perceptions,
)
from saiverse.execution_ledger import ExecutionLedger
from saiverse.execution_ledger_wiring import _make_perception_push_handler
from saiverse_memory.adapter import SAIMemoryAdapter
from sea.head_pipeline import HeadPipeline, HeadSectionRegistry, LineHeadInput, LineHeadSnapshotStore
from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
from tools.context import persona_context


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", str(tmp_path / "user_data"))
    conn = sqlite3.connect(tmp_path / "memory.db", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_memopedia_tables(conn)
    desk.init_desk_tables(conn)
    init_perception_buffer_table(conn)
    adapter = SimpleNamespace(
        conn=conn, _db_lock=threading.RLock(), is_ready=lambda: True, _ready=True,
        settings=SimpleNamespace(resource_id="synthetic"),
        LEDGER_OUTBOX_META_KEY=SAIMemoryAdapter.LEDGER_OUTBOX_META_KEY,
    )
    adapter.push_ledger_perception = partial(SAIMemoryAdapter.push_ledger_perception, adapter)
    mem = Memopedia(conn, db_lock=adapter._db_lock)
    for i in range(684):
        mem.create_page(parent_id="root_terms", title=f"既存項目{i}", summary=f"既存概要{i}")
    # 同秒境界によってテスト結果が変わらない、明確に古い既存項目。
    conn.execute("UPDATE memopedia_pages SET created_at = 1, updated_at = 1")
    conn.commit()
    engine = create_engine(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(AI(AIID="synthetic", HOME_CITYID=1, AINAME="合成", MEMOPEDIA_INDEX_ENABLED=True))
        db.commit()
    ledger = ExecutionLedger(session_factory=factory)
    persona = SimpleNamespace(
        persona_id="synthetic", model="a", current_building_id="room", sai_memory=adapter,
    )
    manager = SimpleNamespace(
        SessionLocal=factory, personas={"synthetic": persona}, execution_ledger=ledger,
        sea_runtime=None,
    )
    ledger.register_outbox_handler("perception.push", _make_perception_push_handler(manager))
    section = MemopediaIndexSection()
    registry = HeadSectionRegistry()
    registry.register(section)
    store = LineHeadSnapshotStore(session_factory=factory, registry=registry)
    pipeline = HeadPipeline(registry=registry, store=store)
    monkeypatch.setattr("sea.head_pipeline.notify.get_default_pipeline", lambda: pipeline)

    def ctx(model="a"):
        return LineHeadInput(
            persona_id="synthetic", model_key=model, current_building_id="room",
            persona=persona, manager=manager,
        )

    for model in ("a", "b"):
        pipeline.capture_all(ctx(model))
    with persona_context("synthetic", tmp_path, manager):
        yield SimpleNamespace(
            conn=conn, mem=mem, pipeline=pipeline, registry=registry, store=store,
            ctx=ctx, section=section, factory=factory, ledger=ledger,
        )
    conn.close()
    engine.dispose()


def _rows(world):
    with world.factory() as db:
        return db.query(ExecutionOutboxItem).order_by(ExecutionOutboxItem.OUTBOX_ID).all()


def test_one_save_in_large_enabled_index_delivers_only_that_page(world, monkeypatch):
    frozen = world.pipeline.get_snapshot("synthetic", "a")
    assert "既存項目683" in world.section.render(frozen.sections["memopedia_index"]).text
    monkeypatch.setattr(
        "sea.head_pipeline.sections.memopedia_index._build_toc_markdown",
        lambda *_: pytest.fail("保存通知で全索引を再生成してはいけない"),
    )
    result = memory_write(title="新しい項目", content="保存する本文")
    page = world.mem.find_by_title("新しい項目")
    assert f"memopedia:{page.short_id}" in result
    pending = list_pending(world.conn)
    assert len(pending) == 1
    assert "新しい項目" in pending[0].content
    assert f"[memopedia:{page.short_id}]" in pending[0].content
    assert "既存項目" not in pending[0].content
    assert "記憶の目次" not in pending[0].content
    assert len(pending[0].content) < 300
    assert _rows(world)[0].STATUS == "delivered"
    reduced = reduce_perceptions(pending)
    text = format_perception_message(reduced)
    batch_id = create_consumption_batch(
        world.conn, [p.id for p in pending], consumed_at=int(time.time()), rendered_text=text,
    )
    assert list_pending(world.conn) == []
    assert world.conn.execute(
        "SELECT rendered_text FROM perception_batches WHERE id = ?", (batch_id,),
    ).fetchone()[0] == text
    assert "新しい項目" in text and "既存項目" not in text
    # 頭の索引は凍結されたままで、通知の撮影で差し替わらない。
    assert world.pipeline.get_snapshot("synthetic", "a") is frozen


def test_multiple_pages_survive_reduction_and_same_page_uses_latest_state(world):
    memory_write(title="ページA", content="A")
    memory_write(title="ページB", content="B")
    page = world.mem.find_by_title("ページA")
    world.mem.update_page(page.id, summary="新しい概要")
    world.mem.set_important(page.id, True)
    memory_write(ref=f"m:{page.short_id}", content="追記")
    reduced = reduce_perceptions(list_pending(world.conn))
    assert len(reduced) == 2
    text = format_perception_message(reduced)
    assert "ページA" in text and "ページB" in text and "新しい概要" in text
    assert "★" in text
    assert "既存項目" not in text


def test_append_and_delete_include_only_target_and_do_not_redeliver(world):
    page = world.mem.find_by_title("既存項目3")
    memory_write(ref=f"m:{page.short_id}", content="追記本文")
    payload = json.loads(_rows(world)[0].PAYLOAD_JSON)
    head_line = next(
        line.strip() for line in world.section.render(world.section.capture(world.ctx())).text.splitlines()
        if f"[memopedia:{page.short_id}]" in line
    )
    assert head_line in payload["content"]
    assert "既存概要3" in payload["content"]
    assert "既存項目4" not in payload["content"]
    memory_delete(ref=f"memopedia:{page.short_id}")
    reduced = reduce_perceptions(list_pending(world.conn))
    assert len(reduced) == 1 and "[削除]" in reduced[0].content
    for model in ("a", "b"):
        assert world.pipeline.flush_diffs(world.ctx(model), all_sections=True) == []


@pytest.mark.parametrize("operation", ["duplicate", "missing", "disabled"])
def test_rejected_writes_and_disabled_index_do_not_notify(world, operation):
    if operation == "duplicate":
        assert "既にあります" in memory_write(title="既存項目0", content="拒否")
    elif operation == "missing":
        memory_write(ref="memopedia:999999", content="拒否")
        memory_delete(ref="memopedia:999999")
    else:
        with world.factory() as db:
            db.query(AI).filter_by(AIID="synthetic").update({"MEMOPEDIA_INDEX_ENABLED": False})
            db.commit()
        memory_write(title="表示無効", content="本文")
    assert _rows(world) == []
    assert list_pending(world.conn) == []


def test_partial_notice_preserves_unnotified_edits_for_loaded_and_unloaded_models(world):
    # b は保存先にだけ残す。部分通知を全体の時刻前進にすると、ここで未通知の
    # 別ページが永久に既読になってしまう。
    world.pipeline.discard_session("synthetic", "b")
    other = world.mem.find_by_title("既存項目9")
    future = int(time.time()) + 10
    world.conn.execute(
        "UPDATE memopedia_pages SET summary = ?, updated_at = ? WHERE id = ?",
        ("UIによる未通知の編集", future, other.id),
    )
    world.conn.commit()
    memory_write(title="ツールで保存", content="本文")
    restart = HeadPipeline(registry=world.registry, store=world.store)
    for model in ("a", "b"):
        assert restart.load_from_store("synthetic", model)
        labels = restart.flush_diffs(world.ctx(model), all_sections=True)
        assert len(labels) == 1
        assert "既存項目9" in labels[0].label
        assert "ツールで保存" not in labels[0].label


def test_failed_outbox_keeps_change_available_to_backstop(world, monkeypatch):
    monkeypatch.setattr(world.ledger, "begin_execution", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("isolated outbox failure"),
    ))
    # 秒精度を持つ既存の差分検出の条件を明示する。
    monkeypatch.setattr("sai_memory.memopedia.storage.time.time", lambda: 2000000000)
    memory_write(title="配送失敗", content="本文")
    assert list_pending(world.conn) == []
    labels = world.pipeline.flush_diffs(world.ctx(), all_sections=True)
    assert any("配送失敗" in label.label for label in labels)
