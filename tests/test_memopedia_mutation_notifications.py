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
            ctx=ctx, section=section, factory=factory, ledger=ledger, manager=manager,
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


@pytest.mark.parametrize("same_second_baseline", [False, True])
def test_same_second_ui_body_edit_is_not_acknowledged_by_prior_tool_notice(
    world, monkeypatch, same_second_baseline,
):
    from api.routes.people.memopedia import update_memopedia_page
    from api.routes.people.models import UpdateMemopediaPageRequest

    tick = int(time.time()) + 2
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    if same_second_baseline:
        for model in ("a", "b"):
            world.pipeline.discard_session("synthetic", model, delete_persisted=True)
            world.pipeline.capture_all(world.ctx(model))
    # b は未ロードの保存行に対する既読の合成も通す。
    world.pipeline.discard_session("synthetic", "b")
    memory_write(title="同秒編集の対象", content="ツールで保存した本文")
    page = world.mem.find_by_title("同秒編集の対象")
    before = world.conn.execute(
        "SELECT title, summary, created_at, updated_at FROM memopedia_pages WHERE id = ?",
        (page.id,),
    ).fetchone()
    assert world.pipeline.flush_diffs(world.ctx(), all_sections=True) == []
    result = update_memopedia_page(
        "synthetic", page.id, UpdateMemopediaPageRequest(content="UIで更新した本文"),
        manager=world.manager,
    )
    assert result["success"]
    after = world.conn.execute(
        "SELECT title, summary, created_at, updated_at FROM memopedia_pages WHERE id = ?",
        (page.id,),
    ).fetchone()
    assert before == after  # 秒精度と本文以外の値は完全に同じ。
    restart = HeadPipeline(registry=world.registry, store=world.store)
    for model in ("a", "b"):
        assert restart.load_from_store("synthetic", model)
        labels = restart.flush_diffs(world.ctx(model), all_sections=True)
        assert [(label.kind, label.label) for label in labels] == [
            ("memopedia_updated", "Memopedia「同秒編集の対象」が更新されました"),
        ]
        assert restart.flush_diffs(world.ctx(model), all_sections=True) == []
    # 一度 backstop が進んだ後も、同じ秒の次の本文編集を見逃さない。
    update_memopedia_page(
        "synthetic", page.id, UpdateMemopediaPageRequest(content="同じ秒の再編集"),
        manager=world.manager,
    )
    for model in ("a", "b"):
        labels = restart.flush_diffs(world.ctx(model), all_sections=True)
        assert len(labels) == 1 and labels[0].kind == "memopedia_updated"
        assert restart.flush_diffs(world.ctx(model), all_sections=True) == []



def _notified(pipeline, model="a"):
    return pipeline._states[("synthetic", model)].last_notified_sections["memopedia_index"]


def _inject(world, model="a"):
    from sea.head_pipeline.integration import inject_diff_notifications

    return inject_diff_notifications(
        world.ctx(model).persona, world.manager, "room", pipeline=world.pipeline,
        model_key=model, detect_room=False,
    )


@pytest.mark.parametrize("unload_b", [False, True])
def test_clean_scans_compact_tool_acknowledgements_and_persist_per_model(world, monkeypatch, unload_b):
    if unload_b:
        world.pipeline.discard_session("synthetic", "b")
    frozen = world.pipeline.get_snapshot("synthetic", "a")
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    for i in range(8):
        memory_write(title=f"継続保存{i}", content="本文")
        assert _inject(world) is False  # 実際の periodic backstop と同じ入口。
        tick += 3
    a = _notified(world.pipeline)
    assert a.captured_at >= tick - 4
    assert len(a.pages) + len(a.acknowledged_pages) <= 1
    assert len(world.section.serialize_snapshot(a)) < 1000
    assert len(world.section.capture_changes_since(world.ctx(), a.captured_at).pages) <= 1
    assert world.pipeline.get_snapshot("synthetic", "a") is frozen
    restart = HeadPipeline(registry=world.registry, store=world.store)
    for model in ("a", "b"):
        assert restart.load_from_store("synthetic", model)
        stored = _notified(restart, model)
        if model == "a":
            assert stored == a
        assert len(stored.pages) + len(stored.acknowledged_pages) <= 1
        assert stored.captured_at >= tick - 4
        assert restart.flush_diffs(world.ctx(model), all_sections=True) == []
        compacted = _notified(restart, model)
        assert compacted.captured_at >= tick - 1
        assert compacted.pages == compacted.acknowledged_pages == ()
    assert len(_rows(world)) == 8  # 回収のための通知は出さない。


def test_preview_does_not_compact_or_persist_clean_scan(world, monkeypatch):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    memory_write(title="プレビュー前", content="本文")
    before = _notified(world.pipeline)
    tick += 10
    from sea.head_pipeline.integration import preview_head_perceptions

    monkeypatch.setattr("sea.head_pipeline.integration._plan_room_state_change", lambda *a, **kw: None)
    monkeypatch.setattr("sea.head_pipeline.integration._compose_copresence_recalls", lambda *a, **kw: [])
    assert preview_head_perceptions(
        world.ctx().persona, world.manager, "room", pipeline=world.pipeline, model_key="a",
    ) == []
    assert _notified(world.pipeline) == before
    restart = HeadPipeline(registry=world.registry, store=world.store)
    assert restart.load_from_store("synthetic", "a")
    assert _notified(restart) == before


def test_delivered_full_scans_also_retire_old_page_revisions(world, monkeypatch):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    for i in range(8):
        page = world.mem.find_by_title(f"既存項目{i}")
        world.mem.update_page(page.id, content=f"UIの本文{i}")
        assert _inject(world) is True
        tick += 3
    for model in ("a", "b"):
        baseline = _notified(world.pipeline, model)
        assert baseline.captured_at >= tick - 4
        assert len(baseline.pages) + len(baseline.acknowledged_pages) <= 1


def test_compaction_keeps_same_second_edits_and_unnotified_other_model(world, monkeypatch):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    memory_write(title="既読の保存", content="本文")
    assert _inject(world) is False
    page = world.mem.find_by_title("既存項目9")
    world.mem.update_page(page.id, content="同じ秒に入った本文")
    # a は掃除済み、b は掃除前。同じ未通知編集を両方で検出する。
    for model in ("a", "b"):
        labels = world.pipeline.flush_diffs(world.ctx(model), all_sections=True)
        assert len(labels) == 1 and "既存項目9" in labels[0].label
        assert world.pipeline.flush_diffs(world.ctx(model), all_sections=True) == []
    world.mem.update_page(page.id, content="同じ秒の再編集")
    for model in ("a", "b"):
        assert len(world.pipeline.flush_diffs(world.ctx(model), all_sections=True)) == 1


def test_failed_scan_cannot_compact_notified_baseline(world, monkeypatch):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    memory_write(title="失敗前の保存", content="本文")
    before = _notified(world.pipeline)
    tick += 10

    class FailedConnection:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("isolated failed scan")

    monkeypatch.setattr(world.ctx().persona.sai_memory, "conn", FailedConnection())
    assert _inject(world) is False
    assert _notified(world.pipeline) == before
    restart = HeadPipeline(registry=world.registry, store=world.store)
    assert restart.load_from_store("synthetic", "a")
    assert _notified(restart) == before



def test_compaction_watermark_precedes_scan_not_changes_during_scan(world, monkeypatch):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    memory_write(title="走査前の保存", content="本文")
    page = world.mem.find_by_title("既存項目9")

    class SlowScanConnection:
        def execute(self, *args, **kwargs):
            cursor = world.conn.execute(*args, **kwargs)

            class Result:
                def fetchall(self):
                    nonlocal tick
                    rows = cursor.fetchall()
                    # SQL の読み取り完了直後、別の操作が入る状況を再現する。
                    tick += 10
                    world.mem.update_page(page.id, content="走査中に更新した本文")
                    return rows

            return Result()

    with monkeypatch.context() as patch:
        patch.setattr(world.ctx().persona.sai_memory, "conn", SlowScanConnection())
        assert _inject(world) is False
    assert _inject(world) is True
    assert "既存項目9" in list_pending(world.conn)[-1].content
    assert _inject(world) is False



def test_failed_delivery_after_compaction_keeps_pending_ui_edit(world, monkeypatch):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    memory_write(title="既読の保存", content="本文")
    assert _inject(world) is False
    tick += 10
    assert _inject(world) is False
    before = _notified(world.pipeline)
    page = world.mem.find_by_title("既存項目9")
    world.mem.update_page(page.id, content="未通知の本文編集")
    with monkeypatch.context() as patch:
        patch.setattr(world.ledger, "begin_execution", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("isolated outbox failure after compaction"),
        ))
        assert _inject(world) is False
    assert _notified(world.pipeline) == before
    assert _inject(world) is True
    restart = HeadPipeline(registry=world.registry, store=world.store)
    for model in ("a", "b"):
        assert restart.load_from_store("synthetic", model)
        assert restart.flush_diffs(world.ctx(model), all_sections=True) == []



@pytest.mark.parametrize("unload_b", [False, True])
def test_other_model_compaction_checks_its_own_older_unnotified_range(world, monkeypatch, unload_b):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    page = world.mem.find_by_title("既存項目9")
    world.mem.update_page(page.id, content="bだけがまだ知らないUI編集")
    tick += 3
    # a の初回 head は編集後、b の既読は編集前。a の走査を b に流用できない。
    world.pipeline.discard_session("synthetic", "a", delete_persisted=True)
    world.pipeline.capture_all(world.ctx())
    if unload_b:
        world.pipeline.discard_session("synthetic", "b")
    tick += 10
    memory_write(title="両モデルへの通知", content="本文")
    assert _inject(world) is False
    restart = HeadPipeline(registry=world.registry, store=world.store)
    assert restart.load_from_store("synthetic", "b")
    labels = restart.flush_diffs(world.ctx("b"), all_sections=True)
    assert len(labels) == 1 and "既存項目9" in labels[0].label
    assert "両モデルへの通知" not in labels[0].label



def test_compaction_persistence_failure_does_not_stop_notification_path(world, monkeypatch):
    tick = int(time.time()) + 10
    monkeypatch.setattr("time.time", lambda: tick + 0.5)
    world.pipeline.discard_session("synthetic", "b")
    memory_write(title="回収前の保存", content="本文")
    tick += 10
    with monkeypatch.context() as patch:
        patch.setattr(world.store, "save_notified_sections_for_other_models", lambda *a, **kw: (
            (_ for _ in ()).throw(RuntimeError("isolated compaction persistence failure"))
        ))
        assert _inject(world) is False
    page = world.mem.find_by_title("既存項目9")
    world.mem.update_page(page.id, content="回収の保存失敗後の編集")
    assert _inject(world) is True
    assert "既存項目9" in list_pending(world.conn)[-1].content
