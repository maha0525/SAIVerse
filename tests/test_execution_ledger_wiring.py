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
- prepared 回収 (#2、W1 Chunk A / D5): on_event・post_session は 120 秒経過で
  refire (resume_execution_id 経由)、day_open 等は 1800 秒で expired 化。
  手動モード persona は refire スキップ。gate で止まった refire は一度で終端化
  せず、再試行窓 (1800 秒) のあいだ prepared を保持して毎 tick 再試行する
  (裁定 B、2026-07-31 — judgment_seat_contention_and_event_loss ③)。放棄は
  prepared 限定 CAS で、別 claimant の running 台帳を壊さない

DB は in-memory SQLite (StaticPool)、記憶側は実 memory.db + Embedder patch
(test_episodes_wiring.py の流儀)。
"""
from __future__ import annotations

import json
from datetime import datetime
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
# saimemory.append_digest (W1 Chunk C / D9-5)
# ---------------------------------------------------------------------------


class TestSaimemoryAppendDigestDelivery:
    def _digest_payload(self, episode_ref):
        from sea.work_session import DIGEST_TAG
        return {
            "message": {
                "role": "assistant",
                "content": "資料を 3 件読んで覚え書きにした。",
                "timestamp": "2026-07-16T10:00:00+00:00",
                "metadata": {
                    "tags": [DIGEST_TAG],
                    "work_session": {"rounds_used": 3, "ended_reason": "finished"},
                    "origin_episode": episode_ref,
                },
                "line_role": "main_line",
                "scope": "committed",
            },
            "episode_ref": episode_ref,
        }

    def _make_episode(self, manager):
        from saiverse import episodes as ep_mod
        ep = ep_mod.open_episode(
            manager, PERSONA_ID, ep_mod.KIND_WORK_SESSION,
        )
        ep_mod.close_episode(manager, PERSONA_ID, ep["episode_ref"])
        return ep["episode_ref"]

    def test_delivery_appends_digest_and_sets_episode_digest_ref(
        self, manager, adapter, session_factory,
    ):
        from database.models import Episode
        from sea.work_session import DIGEST_TAG

        episode_ref = self._make_episode(manager)
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, target=wiring.TARGET_SAIMEMORY_APPEND_DIGEST,
            payload=self._digest_payload(episode_ref),
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True

        rows = _persona_messages(adapter)
        assert len(rows) == 1
        mid, content, meta_raw = rows[0]
        assert "資料を 3 件読んで" in content
        meta = json.loads(meta_raw)
        assert DIGEST_TAG in meta["tags"]
        assert meta["origin_episode"] == episode_ref
        # episode の再訪の鍵が message:{id} で確定している
        db = session_factory()
        try:
            ep = db.query(Episode).filter(Episode.PERSONA_ID == PERSONA_ID).first()
            assert ep.DIGEST_REF == f"message:{mid}"
        finally:
            db.close()
        assert ledger.get_execution(execution_id)["status"] == XL.STATUS_COMPLETED

    def test_redelivery_does_not_duplicate_and_keeps_digest_ref(
        self, manager, adapter, session_factory,
    ):
        """append 済み→delivered 記帳前クラッシュの再配送: 二重 append なし +
        digest_ref は同値のまま (set_digest_ref の冪等)。"""
        from database.models import Episode

        episode_ref = self._make_episode(manager)
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, target=wiring.TARGET_SAIMEMORY_APPEND_DIGEST,
            payload=self._digest_payload(episode_ref),
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
        rows = _persona_messages(adapter)
        assert len(rows) == 1  # 二重にならない
        db = session_factory()
        try:
            ep = db.query(Episode).filter(Episode.PERSONA_ID == PERSONA_ID).first()
            assert ep.DIGEST_REF == f"message:{rows[0][0]}"
        finally:
            db.close()

    def test_delivered_digest_is_read_by_day_close_collection(
        self, manager, adapter,
    ):
        """day_close の _collect_today_session_digests が handler 経由の digest を
        読める (DIGEST_TAG + committed の形の保存則)。"""
        from saiverse.judgment_points import _collect_today_session_digests

        episode_ref = self._make_episode(manager)
        ledger = manager.execution_ledger
        payload = self._digest_payload(episode_ref)
        # 「今日」の日付で書かれた digest (実配送は now の tz-aware ISO)
        payload["message"]["timestamp"] = datetime.now().astimezone().isoformat()
        _applied_with_outbox(
            ledger, target=wiring.TARGET_SAIMEMORY_APPEND_DIGEST,
            payload=payload,
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True

        plan_date = datetime.now().date().isoformat()
        digests = _collect_today_session_digests(manager, PERSONA_ID, plan_date)
        assert digests == ["資料を 3 件読んで覚え書きにした。"]

    def test_judgment_prompt_paired_text_does_not_leak_into_digests(
        self, manager, adapter,
    ):
        """判断プロンプト (paired_action_text) がダイジェスト収集に化けない。

        タグ絞り込みの legacy 救済 (タグ無し行は素通し) と paired_action 展開
        (展開行はタグ無し) の相互作用で、過去の判断プロンプトが「今日の作業
        セッションのダイジェスト」として就寝判断へ混入し、保存→翌日再混入の
        日跨ぎ雪だるまになっていた (2026-07-29 実害: 就寝判断 21,369 字)。"""
        from saiverse.judgment_points import _collect_today_session_digests

        adapter.append_persona_message({
            "role": "assistant",
            "content": "おはよう。今日は作業を進めよう。",
            "timestamp": datetime.now().astimezone().isoformat(),
            "metadata": {"tags": ["meta_judgment", "judgment:day_open"]},
            "line_role": "meta_judgment",
            "scope": "committed",
            "paired_action_text": "[起床判断]\n今日の時間割を編成してください。",
        })
        plan_date = datetime.now().date().isoformat()
        digests = _collect_today_session_digests(manager, PERSONA_ID, plan_date)
        assert digests == []  # 展開行も独白本文もダイジェストではない

    def test_real_digest_survives_many_judgment_prompt_rows(
        self, manager, adapter,
    ):
        """取得枠 (limit=12) より多い判断行が後から積もっても、本物の
        ダイジェストが取得段階で押し出されない (strict_tags を取得側に
        置いた理由 — 後段検査だけだと偽候補が枠を食い尽くす)。"""
        from saiverse.judgment_points import _collect_today_session_digests

        episode_ref = self._make_episode(manager)
        ledger = manager.execution_ledger
        payload = self._digest_payload(episode_ref)
        payload["message"]["timestamp"] = datetime.now().astimezone().isoformat()
        _applied_with_outbox(
            ledger, target=wiring.TARGET_SAIMEMORY_APPEND_DIGEST,
            payload=payload,
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is True
        for i in range(13):  # 展開行 13 + 独白 13 > 枠 12
            adapter.append_persona_message({
                "role": "assistant",
                "content": f"判断の独白 {i}",
                "timestamp": datetime.now().astimezone().isoformat(),
                "metadata": {"tags": ["meta_judgment"]},
                "line_role": "meta_judgment",
                "scope": "committed",
                "paired_action_text": f"[セッション終了判断]\n状況テキスト {i}",
            })
        plan_date = datetime.now().date().isoformat()
        digests = _collect_today_session_digests(manager, PERSONA_ID, plan_date)
        assert digests == ["資料を 3 件読んで覚え書きにした。"]

    def test_missing_episode_is_delivery_failure(
        self, manager, adapter, session_factory,
    ):
        """episode 不在は例外 = 配送失敗 (pending 残存)。append は冪等なので
        再配送で二重にならない。"""
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, target=wiring.TARGET_SAIMEMORY_APPEND_DIGEST,
            payload=self._digest_payload("episode:999"),
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is False
        row = _outbox_rows(session_factory, execution_id)[0]
        assert row.STATUS == XL.OUTBOX_PENDING
        assert "episode" in (row.LAST_ERROR or "")
        # digest 自体は append 済み (再配送は冪等スキップ → set_digest_ref 再試行)
        assert len(_persona_messages(adapter)) == 1
        assert ledger.flush_pending_for_persona(PERSONA_ID) is False
        assert len(_persona_messages(adapter)) == 1

    def test_malformed_payload_is_delivery_failure(self, manager, session_factory):
        ledger = manager.execution_ledger
        execution_id = _applied_with_outbox(
            ledger, target=wiring.TARGET_SAIMEMORY_APPEND_DIGEST,
            payload={"episode_ref": "episode:1"},
        )
        assert ledger.flush_pending_for_persona(PERSONA_ID) is False
        row = _outbox_rows(session_factory, execution_id)[0]
        assert "message" in (row.LAST_ERROR or "")


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


# ---------------------------------------------------------------------------
# prepared 回収 (#2、W1 Chunk A / D5)
# ---------------------------------------------------------------------------


class TestPreparedCollection:
    BASE = datetime(2026, 7, 19, 9, 0, 0)

    def _claim_prepared(self, ledger, kind, persona_id=PERSONA_ID, payload=None):
        eid, runnable, _st = ledger.claim_execution(
            kind, idempotency_key=None, persona_id=persona_id, payload=payload,
        )
        assert runnable
        return eid

    def _patch_fire(self, monkeypatch, result=None):
        from saiverse import autonomy_wiring

        calls = []

        def _fake(mgr, pid, kind, context=None, resume_execution_id=None, **kw):
            calls.append({
                "persona_id": pid, "kind": kind, "context": context,
                "resume_execution_id": resume_execution_id,
            })
            return result if result is not None else {"submitted": True}

        monkeypatch.setattr(autonomy_wiring, "fire_judgment_point", _fake)
        return calls

    def test_on_event_prepared_refired_after_delay(self, manager, monkeypatch):
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        payload = {"event_text": "掲示板の告知", "is_alert": False}
        eid = self._claim_prepared(ledger, "judgment.on_event", payload=payload)
        calls = self._patch_fire(monkeypatch)

        # 60 秒: まだ回収しない
        clock.advance_to(datetime(2026, 7, 19, 9, 1, 0))
        wiring._collect_prepared_judgments(manager)
        assert calls == []

        # 120 秒経過: refire (payload から context 復元 + resume_execution_id)
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)
        assert calls == [{
            "persona_id": PERSONA_ID, "kind": "on_event",
            "context": payload, "resume_execution_id": eid,
        }]

    def test_post_session_prepared_refired(self, manager, monkeypatch):
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        payload = {"task_ref": "task:1", "session_result": {"episode_ref": "episode:7"}}
        eid = self._claim_prepared(ledger, "judgment.post_session", payload=payload)
        calls = self._patch_fire(monkeypatch)
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(calls) == 1
        assert calls[0]["kind"] == "post_session"
        assert calls[0]["resume_execution_id"] == eid

    def test_day_open_prepared_expires_via_recovery_tick(self, manager, monkeypatch):
        """day_open は refire せず 1800 秒で expired 化 (watchdog が自然再発火する)。
        _recovery_tick 経由で回収が配線されていることも同時に確認する。"""
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(ledger, "judgment.day_open")
        calls = self._patch_fire(monkeypatch)

        # 10 分: まだ期限内
        clock.advance_to(datetime(2026, 7, 19, 9, 10, 0))
        wiring._recovery_tick(manager)
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED

        # 40 分: expired
        clock.advance_to(datetime(2026, 7, 19, 9, 40, 0))
        wiring._recovery_tick(manager)
        entry = ledger.get_execution(eid)
        assert entry["status"] == XL.STATUS_FAILED
        assert "expired" in entry["error"]
        assert calls == []  # refire はされない

    def test_post_conversation_prepared_expires(self, manager, monkeypatch):
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(ledger, "judgment.post_conversation")
        calls = self._patch_fire(monkeypatch)
        clock.advance_to(datetime(2026, 7, 19, 9, 40, 0))
        wiring._collect_prepared_judgments(manager)
        assert ledger.get_execution(eid)["status"] == XL.STATUS_FAILED
        assert calls == []

    def test_manual_mode_persona_skips_refire(self, manager, monkeypatch):
        """refire は「行動を生む」ので手動モード persona はスキップ (prepared 温存)。"""
        clock.enable_virtual(self.BASE)
        manager._debug_manual_mode_personas = {PERSONA_ID}
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        calls = self._patch_fire(monkeypatch)
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)
        assert calls == []
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED

    def test_refire_gate_failure_keeps_seat_until_retry_window(
        self, manager, monkeypatch,
    ):
        """裁定 B (2026-07-31、judgment_seat_contention_and_event_loss ③):
        gate で止まった refire は一度の失敗で終端化せず、再試行窓のあいだ
        prepared を保持して毎 tick 再試行する。窓を使い切ったら expired 終端。"""
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        calls = self._patch_fire(monkeypatch, result={
            "submitted": False, "reason": "persona autonomy disabled",
        })
        # 1 回目の失敗: prepared のまま保持 (終端化しない)
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(calls) == 1
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED
        # 次の tick: 再試行される
        clock.advance_to(datetime(2026, 7, 19, 9, 4, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(calls) == 2
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED
        # 窓 (1800 秒) 超過: refire せず expired 終端
        clock.advance_to(datetime(2026, 7, 19, 9, 40, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(calls) == 2
        entry = ledger.get_execution(eid)
        assert entry["status"] == XL.STATUS_FAILED
        assert "expired" in entry["error"]

    def test_refire_recovers_after_transient_failure(self, manager, monkeypatch):
        """裁定 B: 一時障害が直れば、保持していた席から判断が走る
        (イベントは消失しない — これが B を選んだ理由そのもの)。"""
        from saiverse import autonomy_wiring

        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        results = [
            {"submitted": False, "reason": "persona not loaded"},  # 一時障害
            {"submitted": True},                                   # 回復
        ]
        calls = []

        def _fake(mgr, pid, kind, context=None, resume_execution_id=None, **kw):
            calls.append(resume_execution_id)
            return results.pop(0)

        monkeypatch.setattr(autonomy_wiring, "fire_judgment_point", _fake)
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED
        clock.advance_to(datetime(2026, 7, 19, 9, 4, 0))
        wiring._collect_prepared_judgments(manager)
        # 同じ席 (execution_id) への refire が 2 回、2 回目で成立した
        assert calls == [eid, eid]

    def test_old_prepared_row_gets_a_refire_after_long_downtime(
        self, manager, monkeypatch,
    ):
        """再試行窓は行齢でなく「このプロセスで最初に refire を試みた時刻」から
        数える — claim 直後にプロセスが落ちて 30 分以上たってから再起動した
        場合でも、復旧後の最初の tick は失効させずに refire する
        (Codex 一巡目 high2: 行齢基準は停止時間が窓を消費し、durable recovery の
        対象だったイベントを復旧直後に破棄していた)。
        """
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        calls = self._patch_fire(monkeypatch, result={
            "submitted": False, "reason": "persona not loaded",
        })
        # 40 分停止していた想定 (このプロセスに初回試行の記録は無い)
        clock.advance_to(datetime(2026, 7, 19, 9, 40, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(calls) == 1  # 失効せず refire された
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED
        # 初回試行からさらに窓 (1800 秒) 超過 → expired 終端
        clock.advance_to(datetime(2026, 7, 19, 10, 15, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(calls) == 1
        entry = ledger.get_execution(eid)
        assert entry["status"] == XL.STATUS_FAILED
        assert "expired" in entry["error"]

    def test_manual_mode_time_does_not_consume_retry_window(
        self, manager, monkeypatch,
    ):
        """手動モード中のスキップは再試行窓を消費しない — 30 分以上たってから
        手動モードを解除しても、少なくとも 1 回は refire される。"""
        clock.enable_virtual(self.BASE)
        manager._debug_manual_mode_personas = {PERSONA_ID}
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        calls = self._patch_fire(monkeypatch, result={
            "submitted": False, "reason": "persona autonomy disabled",
        })
        # 手動モードのまま 40 分: スキップされ続け、prepared 温存
        clock.advance_to(datetime(2026, 7, 19, 9, 40, 0))
        wiring._collect_prepared_judgments(manager)
        assert calls == []
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED
        # 手動モード解除 → 失効せず refire される
        manager._debug_manual_mode_personas = set()
        clock.advance_to(datetime(2026, 7, 19, 9, 41, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(calls) == 1
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED

    def test_expiry_failure_does_not_grant_a_fresh_retry_window(
        self, manager, monkeypatch,
    ):
        """期限切れの放棄が台帳例外で失敗しても、初回試行時刻は保持され、次 tick は
        refire を再開せず期限切れ処理を直ちに再試行する (Codex 二巡目 medium:
        失敗時に時刻を消すと次 tick が「初回」と誤認して 30 分窓を配り直し、
        恒久ゲートの行が有限時間で終端する契約が破れる)。"""
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        calls = self._patch_fire(monkeypatch, result={
            "submitted": False, "reason": "persona autonomy disabled",
        })
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)  # 初回試行 (9:03)
        assert len(calls) == 1
        # 窓超過。放棄は一度だけ台帳例外で失敗する
        real_abandon = ledger.abandon_prepared
        failures = {"n": 0}

        def _flaky(eid_, reason):
            if failures["n"] == 0:
                failures["n"] += 1
                raise RuntimeError("database is locked (injected)")
            return real_abandon(eid_, reason)

        monkeypatch.setattr(ledger, "abandon_prepared", _flaky)
        clock.advance_to(datetime(2026, 7, 19, 9, 40, 0))
        wiring._collect_prepared_judgments(manager)  # 期限切れ処理が失敗
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED
        assert len(calls) == 1  # 新しい窓で refire を再開していない
        # 次 tick: 期限切れ処理が直ちに再試行され、今度は成功する
        clock.advance_to(datetime(2026, 7, 19, 9, 41, 0))
        wiring._collect_prepared_judgments(manager)
        entry = ledger.get_execution(eid)
        assert entry["status"] == XL.STATUS_FAILED
        assert "expired" in entry["error"]
        assert len(calls) == 1

    def test_expiry_does_not_break_a_seat_claimed_meanwhile(self, manager):
        """期限切れの放棄は prepared 限定 CAS — 放棄する瞬間に別 claimant が
        running へ進めていた席は触らない (勝者の台帳を壊さない)。"""
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        assert ledger.try_mark_running(eid)  # もう一人の claimant が席を取った
        wiring._abandon_prepared_row(ledger, eid, "expired: test")
        assert ledger.get_execution(eid)["status"] == XL.STATUS_RUNNING

    def test_recovered_on_event_engage_now_dispatches_response(
        self, manager, monkeypatch,
    ):
        """回収 refire の判断が engage_now を選んだら、payload の凍結 event_text
        から応対 Pulse を再構成して起動する — 判断だけ成功させて応対を落とすと、
        alert (engage_now しか選べない) では回収成功に見えて通知への応答が必ず
        欠落する (Codex 三巡目 high1)。"""
        from saiverse import autonomy_wiring

        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event",
            payload={"event_text": "掲示板の告知", "is_alert": True},
        )
        monkeypatch.setattr(
            autonomy_wiring, "fire_judgment_point",
            lambda *a, **kw: {
                "submitted": True, "execution_id": eid,
                "applied_events": [{
                    "type": "judgment_applied",
                    "extras": ["reaction=engage_now"],
                }],
            },
        )
        dispatched = []
        manager.pulse_dispatcher = SimpleNamespace(
            dispatch_schedule_fire=lambda **kw: (dispatched.append(kw), {
                "action": "execute", "runtime_outcome": "completed",
                "error": None,
            })[1],
        )
        manager.personas[PERSONA_ID].current_building_id = "alice_room"
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)
        assert len(dispatched) == 1
        assert dispatched[0]["persona_id"] == PERSONA_ID
        assert dispatched[0]["building_id"] == "alice_room"
        assert "掲示板の告知" in dispatched[0]["user_input"]
        assert "[外部イベント通知]" in dispatched[0]["user_input"]

    def test_recovered_on_event_note_only_does_not_dispatch(
        self, manager, monkeypatch,
    ):
        """engage_now 以外 (note_only 等) の回収は応対を起動しない (finalize が
        適用済み — 従来経路の handle_external_event と同じ裁定)。"""
        from saiverse import autonomy_wiring

        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(
            ledger, "judgment.on_event", payload={"event_text": "x"},
        )
        monkeypatch.setattr(
            autonomy_wiring, "fire_judgment_point",
            lambda *a, **kw: {
                "submitted": True, "execution_id": eid,
                "applied_events": [{
                    "type": "judgment_applied",
                    "extras": ["reaction=note_only"],
                }],
            },
        )
        dispatched = []
        manager.pulse_dispatcher = SimpleNamespace(
            dispatch_schedule_fire=lambda **kw: (dispatched.append(kw), {
                "action": "execute", "runtime_outcome": "completed",
                "error": None,
            })[1],
        )
        manager.personas[PERSONA_ID].current_building_id = "alice_room"
        clock.advance_to(datetime(2026, 7, 19, 9, 3, 0))
        wiring._collect_prepared_judgments(manager)
        assert dispatched == []

    def test_unknown_judgment_kind_left_as_is(self, manager, monkeypatch):
        """回収規則の無い judgment.* kind は触らない (安全側)。"""
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid = self._claim_prepared(ledger, "judgment.someday_new_kind")
        calls = self._patch_fire(monkeypatch)
        clock.advance_to(datetime(2026, 7, 19, 10, 0, 0))
        wiring._collect_prepared_judgments(manager)
        assert calls == []
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED

    def test_non_judgment_prepared_not_collected(self, manager, monkeypatch):
        """judgment. 前綴り以外の prepared は回収対象外 (list_prepared の絞り)。"""
        clock.enable_virtual(self.BASE)
        ledger = manager.execution_ledger
        eid, _ = ledger.begin_execution("metabolism.run", persona_id=PERSONA_ID)
        calls = self._patch_fire(monkeypatch)
        clock.advance_to(datetime(2026, 7, 19, 10, 0, 0))
        wiring._collect_prepared_judgments(manager)
        assert calls == []
        assert ledger.get_execution(eid)["status"] == XL.STATUS_PREPARED


# ---------------------------------------------------------------------------
# slot.fire の settle-close 回復 (#D5)
# ---------------------------------------------------------------------------


class TestSlotFireRecovery:
    """running のまま残った slot.fire を回復 tick が settle-close する。

    plan 行 / episode を伴わない最小構成でも settle が完了する (欠損は防御的に
    skip)。統合的な収束状態 (fired/open/running) からの回復は test_day_plan.py。
    """

    def _running_slot_fire(self, ledger, *, index=0, plan_date="2026-07-20"):
        eid, _ = ledger.begin_execution(
            wiring.SLOT_FIRE_KIND,
            idempotency_key=f"{PERSONA_ID}:{plan_date}:{index}",
            persona_id=PERSONA_ID,
            payload={
                "persona_id": PERSONA_ID, "plan_date": plan_date,
                "index": index, "slot_kind": "知る", "reserved_rounds": 5,
            },
        )
        ledger.mark_running(eid)
        return eid

    def _age(self, session_factory, execution_id, seconds):
        db = session_factory()
        try:
            db.query(ExecutionLedgerEntry).filter(
                ExecutionLedgerEntry.EXECUTION_ID == execution_id
            ).update({"UPDATED_AT": ExecutionLedgerEntry.UPDATED_AT - int(seconds)})
            db.commit()
        finally:
            db.close()

    def test_stale_slot_fire_settled_and_not_unknown_swept(self, manager, session_factory):
        """回復 tick: deadline 超過の slot.fire は settle-close (completed) され、
        汎用 unknown sweep に掴まれない。並走の generic running は unknown 化される。"""
        ledger = manager.execution_ledger
        slot_id = self._running_slot_fire(ledger)
        generic_id, _ = ledger.begin_execution("metabolism.run", persona_id=PERSONA_ID)
        ledger.mark_running(generic_id)
        # slot.fire を settle deadline 超過、generic を running deadline 超過へ
        self._age(session_factory, slot_id, int(wiring.SLOT_SETTLE_DEADLINE_SECONDS) + 60)
        self._age(session_factory, generic_id, int(wiring.RUNNING_DEADLINE_SECONDS) + 60)

        wiring._recovery_tick(manager)

        # slot.fire は settle-close 経路で completed (unknown ではない)
        assert ledger.get_execution(slot_id)["status"] == XL.STATUS_COMPLETED
        # generic は汎用 sweep で unknown
        assert ledger.get_execution(generic_id)["status"] == XL.STATUS_UNKNOWN

    def test_fresh_slot_fire_within_deadline_is_untouched(self, manager, session_factory):
        """deadline 未満の running slot.fire は settle も unknown もされない。"""
        ledger = manager.execution_ledger
        slot_id = self._running_slot_fire(ledger)
        wiring._recovery_tick(manager)
        assert ledger.get_execution(slot_id)["status"] == XL.STATUS_RUNNING

    def test_startup_recovery_settles_previous_generation_slot_fire(self, manager, session_factory):
        """起動時: deadline を課さず全 running slot.fire を settle-close する
        (前世代確定)。generic running は従来どおり unknown 化。"""
        ledger = manager.execution_ledger
        slot_id = self._running_slot_fire(ledger)  # aging しない (deadline 課さない)
        generic_id, _ = ledger.begin_execution("judgment.day_open", persona_id=PERSONA_ID)
        ledger.mark_running(generic_id)

        wiring.run_startup_recovery(manager)

        assert ledger.get_execution(slot_id)["status"] == XL.STATUS_COMPLETED
        assert ledger.get_execution(generic_id)["status"] == XL.STATUS_UNKNOWN

    def test_incomplete_payload_is_skipped(self, manager, session_factory):
        """payload に座標が欠ける running slot.fire は skip (tick を殺さない)。"""
        ledger = manager.execution_ledger
        eid, _ = ledger.begin_execution(
            wiring.SLOT_FIRE_KIND, idempotency_key="x", persona_id=PERSONA_ID,
            payload={"persona_id": PERSONA_ID},  # plan_date / index 欠損
        )
        ledger.mark_running(eid)
        self._age(session_factory, eid, int(wiring.SLOT_SETTLE_DEADLINE_SECONDS) + 60)
        wiring._collect_stale_slot_executions(manager)
        # settle されず running のまま (次段の裁定に委ねる)
        assert ledger.get_execution(eid)["status"] == XL.STATUS_RUNNING
