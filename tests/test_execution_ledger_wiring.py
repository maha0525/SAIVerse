"""実行台帳の覚醒 (§6-2 前半) — 結線と実ハンドラ 2 種の回帰テスト。

対象 (docs/intent/execution_ledger.md §2.2/§2.4、saiverse/execution_ledger_wiring.py):

- build_execution_ledger: ハンドラ 2 種 (saimemory.append / perception.push) が
  登録され、配送が実 SAIMemoryAdapter まで届く
- saimemory.append: 冪等キー (ledger_outbox_id / execution_id) が metadata に
  刻まれる / 再配送で二重にならない / append 失敗 (None) は例外 = 配送失敗 /
  persona 不在は pending 残存 (dead 経路へ)
- perception.push: バッファに積まれ既存 flush 経路で消費される / 再配送で
  二重にならない / adapter 未 ready は例外
- run_startup_recovery: 前世代 running の一括 unknown 化 + pending 全量配送
- list_pending_personas: メモリ上の personas でなく DB 実態から列挙する
- _recovery_tick: running 期限監視 + 配送。内部例外を吸収して周期を殺さない
- schedule_recovery_tick: EventScheduler に key=execution_ledger_recovery で予約

DB は in-memory SQLite (StaticPool)、記憶側は実 memory.db + Embedder patch
(test_episodes_wiring.py の流儀)。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, ExecutionLedgerEntry, ExecutionOutboxItem
from saiverse import clock
from saiverse import execution_ledger as XL
from saiverse import execution_ledger_wiring as wiring
from saiverse.event_scheduler import EventScheduler

PERSONA_ID = "tester"


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.disable_virtual()


@pytest.fixture
def adapter(tmp_path):
    """実 SAIMemoryAdapter (実 memory.db + Embedder patch)。"""
    persona_dir = tmp_path / "personas" / PERSONA_ID
    persona_dir.mkdir(parents=True, exist_ok=True)
    with patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter
        a = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_dir, resource_id=PERSONA_ID,
        )
        yield a


@pytest.fixture
def manager(session_factory, adapter):
    """配線テスト用の最小 manager。personas は実 adapter を持つ 1 体。"""
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: SimpleNamespace(persona_id=PERSONA_ID, sai_memory=adapter)},
        event_scheduler=EventScheduler(),  # start() しない (予約 heap の検査のみ)
    )
    mgr.execution_ledger = wiring.build_execution_ledger(mgr)
    return mgr


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _applied_with_outbox(ledger, *, kind="test.kind", persona_id=PERSONA_ID,
                         target=wiring.TARGET_SAIMEMORY_APPEND, payload=None,
                         deliver=False):
    """prepared→running→applied (outbox 1 件同梱、配送は既定で保留) まで進める。"""
    execution_id, created = ledger.begin_execution(kind, persona_id=persona_id)
    assert created
    ledger.mark_running(execution_id)
    ledger.mark_applied(
        execution_id,
        outbox_items=[{
            "target": target,
            "persona_id": persona_id,
            "payload": payload or {},
        }],
        deliver=deliver,
    )
    return execution_id


def _outbox_rows(session_factory, execution_id=None):
    db = session_factory()
    try:
        q = db.query(ExecutionOutboxItem)
        if execution_id is not None:
            q = q.filter(ExecutionOutboxItem.EXECUTION_ID == execution_id)
        return list(q.order_by(ExecutionOutboxItem.OUTBOX_ID.asc()).all())
    finally:
        db.close()


def _message_payload(text="配送テストの記録"):
    return {
        "message": {
            "role": "user",
            "content": f"<system>{text}</system>",
            "timestamp": "2026-07-16T10:00:00+00:00",
            "metadata": {"tags": ["internal", "event_message"]},
            "line_role": "main_line",
            "scope": "committed",
        },
    }


def _persona_messages(adapter):
    with adapter._db_lock:
        return adapter.conn.execute(
            "SELECT id, content, metadata FROM messages ORDER BY created_at ASC, id ASC"
        ).fetchall()


def _perception_rows(adapter):
    with adapter._db_lock:
        return adapter.conn.execute(
            "SELECT kind, content, metadata FROM perception_buffer ORDER BY id ASC"
        ).fetchall()


# ---------------------------------------------------------------------------
# saimemory.append
# ---------------------------------------------------------------------------


class TestSaimemoryAppendDelivery:
    def test_delivery_reaches_memory_with_idempotency_stamp(self, manager, adapter, session_factory):
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, payload=_message_payload("台帳経由の一行"),
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True

        rows = _persona_messages(adapter)
        assert len(rows) == 1
        assert "台帳経由の一行" in rows[0][1]
        meta = json.loads(rows[0][2])
        outbox = _outbox_rows(session_factory, execution_id)
        assert meta[adapter.LEDGER_OUTBOX_META_KEY] == outbox[0].OUTBOX_ID
        assert meta["execution_id"] == execution_id
        # 本文側の metadata (tags) は変形されない
        assert meta["tags"] == ["internal", "event_message"]
        # 全配送済み → completed へ自動遷移 (不変条件 4)
        assert ledger.get_execution(execution_id)["status"] == XL.STATUS_COMPLETED

    def test_redelivery_does_not_duplicate(self, manager, adapter, session_factory):
        """配送成功 → delivered 記帳前クラッシュ、を模した再配送で二重にならない。"""
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, payload=_message_payload("一度だけ書かれるべき行"),
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True
        # クラッシュ再現: world DB 側の配送記帳だけ巻き戻す (memory.db には書き込み済み)
        db = session_factory()
        try:
            db.query(ExecutionOutboxItem).filter(
                ExecutionOutboxItem.EXECUTION_ID == execution_id
            ).update({"STATUS": XL.OUTBOX_PENDING, "DELIVERED_AT": None})
            db.query(ExecutionLedgerEntry).filter(
                ExecutionLedgerEntry.EXECUTION_ID == execution_id
            ).update({"STATUS": XL.STATUS_APPLIED})
            db.commit()
        finally:
            db.close()

        assert ledger.flush_pending_for_persona(PERSONA_ID) is True
        rows = _persona_messages(adapter)
        assert len(rows) == 1  # 二重にならない
        assert ledger.get_execution(execution_id)["status"] == XL.STATUS_COMPLETED

    def test_append_failure_is_delivery_failure(self, manager, adapter, session_factory):
        """_append_message が None を握って返しても、配送成功に偽装されない。"""
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(ledger, payload=_message_payload())
        with patch.object(adapter, "_append_message", return_value=None):
            assert ledger.flush_pending_for_persona(PERSONA_ID) is False
        row = _outbox_rows(session_factory, execution_id)[0]
        assert row.STATUS == XL.OUTBOX_PENDING
        assert row.ATTEMPTS == 1
        assert "append failed" in (row.LAST_ERROR or "")
        # 記憶には何も書かれていない
        assert _persona_messages(adapter) == []

    def test_missing_persona_stays_pending(self, manager, session_factory):
        """削除済み persona 宛は配送失敗として pending に残る (再試行→dead の正規経路)。"""
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, persona_id="ghost", payload=_message_payload(),
        )
        assert ledger.flush_pending_for_persona("ghost") is False
        row = _outbox_rows(session_factory, execution_id)[0]
        assert row.STATUS == XL.OUTBOX_PENDING
        assert row.ATTEMPTS == 1
        assert "persona not found" in (row.LAST_ERROR or "")

    def test_malformed_payload_is_delivery_failure(self, manager, session_factory):
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(ledger, payload={"no_message": True})
        assert ledger.flush_pending_for_persona(PERSONA_ID) is False
        row = _outbox_rows(session_factory, execution_id)[0]
        assert "message" in (row.LAST_ERROR or "")

    def test_adapter_not_ready_raises(self, adapter):
        with patch.object(adapter, "is_ready", return_value=False):
            with pytest.raises(RuntimeError):
                adapter.append_ledger_message(
                    {"role": "user", "content": "x"},
                    execution_id="e1", outbox_id=1,
                )


# ---------------------------------------------------------------------------
# perception.push
# ---------------------------------------------------------------------------


class TestPerceptionPushDelivery:
    def _payload(self):
        return {
            "kind": "world_state",
            "content": "エアリスが私室に入ってきた。",
            "reduce_key": "occupant:airis",
            "salient": False,
            "metadata": json.dumps({"origin": "test"}, ensure_ascii=False),
        }

    def test_delivery_reaches_buffer_and_flushes_to_memory(self, manager, adapter):
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, target=wiring.TARGET_PERCEPTION_PUSH, payload=self._payload(),
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True

        rows = _perception_rows(adapter)
        assert len(rows) == 1
        assert rows[0][0] == "world_state"
        meta = json.loads(rows[0][2])
        assert meta["execution_id"] == execution_id
        assert adapter.LEDGER_OUTBOX_META_KEY in meta
        assert meta["origin"] == "test"  # 送り主 metadata は保持

        # 既存の消費経路 (Pulse 開始時 flush) でペルソナの記憶に届く
        assert adapter.flush_perception_buffer() is True
        messages = _persona_messages(adapter)
        assert len(messages) == 1
        assert "エアリスが私室に入ってきた。" in messages[0][1]

    def test_redelivery_does_not_duplicate(self, manager, adapter, session_factory):
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, target=wiring.TARGET_PERCEPTION_PUSH, payload=self._payload(),
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True
        db = session_factory()
        try:
            db.query(ExecutionOutboxItem).filter(
                ExecutionOutboxItem.EXECUTION_ID == execution_id
            ).update({"STATUS": XL.OUTBOX_PENDING, "DELIVERED_AT": None})
            db.query(ExecutionLedgerEntry).filter(
                ExecutionLedgerEntry.EXECUTION_ID == execution_id
            ).update({"STATUS": XL.STATUS_APPLIED})
            db.commit()
        finally:
            db.close()
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True
        assert len(_perception_rows(adapter)) == 1

    def test_adapter_not_ready_raises(self, adapter):
        with patch.object(adapter, "is_ready", return_value=False):
            with pytest.raises(RuntimeError):
                adapter.push_ledger_perception(
                    execution_id="e1", outbox_id=1, kind="world_state", content="x",
                )


# ---------------------------------------------------------------------------
# 起動時回復 + 定期掃除 tick
# ---------------------------------------------------------------------------


class TestStartupRecovery:
    def test_stale_running_swept_and_pending_flushed(self, manager, adapter, session_factory):
        ledger = manager.execution_ledger
        # 前世代の running (プロセス死を模す)
        stale_id, _ = ledger.begin_execution("judgment.day_open", persona_id=PERSONA_ID)
        ledger.mark_running(stale_id)
        # 配送されないまま残った applied + pending
        held_id = _applied_with_outbox(
            ledger, payload=_message_payload("再起動を跨いで届く行"),
        )

        wiring.run_startup_recovery(manager)

        assert ledger.get_execution(stale_id)["status"] == XL.STATUS_UNKNOWN
        assert ledger.get_execution(held_id)["status"] == XL.STATUS_COMPLETED
        rows = _persona_messages(adapter)
        assert len(rows) == 1
        assert "再起動を跨いで届く行" in rows[0][1]

    def test_flush_covers_personas_not_in_memory(self, manager, session_factory):
        """flush 対象は DB 実態から列挙する — メモリ上に居ない persona 宛も試行される。"""
        ledger = manager.execution_ledger
        ghost_id = _applied_with_outbox(
            ledger, persona_id="ghost", payload=_message_payload(),
        )
        wiring.run_startup_recovery(manager)
        row = _outbox_rows(session_factory, ghost_id)[0]
        assert row.ATTEMPTS == 1  # 試行された (放置ではない)
        assert row.STATUS == XL.OUTBOX_PENDING

    def test_list_pending_personas_from_db(self, manager):
        ledger = manager.execution_ledger
        _applied_with_outbox(ledger, persona_id=PERSONA_ID, payload=_message_payload())
        _applied_with_outbox(ledger, persona_id="ghost", payload=_message_payload())
        _applied_with_outbox(ledger, persona_id=None, payload=_message_payload())
        assert set(ledger.list_pending_personas()) == {PERSONA_ID, "ghost", None}


class TestRecoveryTick:
    def test_tick_sweeps_deadline_and_delivers(self, manager, adapter, session_factory):
        ledger = manager.execution_ledger
        stale_id, _ = ledger.begin_execution("test.kind", persona_id=PERSONA_ID)
        ledger.mark_running(stale_id)
        fresh_id, _ = ledger.begin_execution("test.kind2", persona_id=PERSONA_ID)
        ledger.mark_running(fresh_id)
        held_id = _applied_with_outbox(
            ledger, payload=_message_payload("tick が届ける行"),
        )
        # stale だけ期限超過に細工
        db = session_factory()
        try:
            db.query(ExecutionLedgerEntry).filter(
                ExecutionLedgerEntry.EXECUTION_ID == stale_id
            ).update({"UPDATED_AT": ExecutionLedgerEntry.UPDATED_AT
                      - int(wiring.RUNNING_DEADLINE_SECONDS) - 60})
            db.commit()
        finally:
            db.close()

        wiring._recovery_tick(manager)

        assert ledger.get_execution(stale_id)["status"] == XL.STATUS_UNKNOWN
        assert ledger.get_execution(fresh_id)["status"] == XL.STATUS_RUNNING  # 期限内は触らない
        assert ledger.get_execution(held_id)["status"] == XL.STATUS_COMPLETED
        assert len(_persona_messages(adapter)) == 1

    def test_tick_swallows_internal_errors(self, manager):
        """掃除 tick は例外を吸収する (schedule_periodic は例外で周期を止めるため)。"""
        class _Broken:
            def recover_stale_running(self, **kwargs):
                raise RuntimeError("db down")

            def list_pending_personas(self):
                raise RuntimeError("db down")

        broken_manager = SimpleNamespace(execution_ledger=_Broken())
        wiring._recovery_tick(broken_manager)  # raise しないこと

    def test_schedule_recovery_tick_registers_key(self, manager):
        wiring.schedule_recovery_tick(manager)
        assert manager.event_scheduler.has_key(wiring.RECOVERY_TICK_KEY)
