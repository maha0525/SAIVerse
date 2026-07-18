"""実行台帳 (Execution Ledger) Phase 0 の回帰テスト (docs/intent/execution_ledger.md)。

検証項目:
- 状態機械の合法/不法遷移 (不法は IllegalTransitionError)
- 冪等キー衝突: 二本目の begin_execution が新規行を作らず created=False
- mark_applied の原子性: outbox 積みと同一コミット (途中失敗の注入で両方ロールバック)
- persona 単位 FIFO: 先頭が失敗している間、後続を試行しない。別 persona は独立
- 関所 flush: 全配送成功で True、handler 失敗で False かつ pending 残存
- recover_stale_running: 期限超過 running のみ unknown 化 (仮想クロックで検証)
- dead 化: 上限回失敗で dead、以後配送試行されない。dead は後続をブロックしない
- migrate.py の軽量パス (ensure_execution_ledger_tables) が旧 DB にテーブルを作る
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, ExecutionLedgerEntry, ExecutionOutboxItem
from saiverse import clock
from saiverse import execution_ledger as XL


class ExecutionLedgerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.ledger = XL.ExecutionLedger(self.SessionLocal)

    def tearDown(self):
        clock.disable_virtual()
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    # --- helpers ---

    def _begin_running(self, kind="test.kind", persona_id=None, key=None):
        execution_id, created = self.ledger.begin_execution(
            kind, idempotency_key=key, persona_id=persona_id
        )
        self.assertTrue(created)
        self.ledger.mark_running(execution_id)
        return execution_id

    def _status(self, execution_id):
        return self.ledger.get_execution(execution_id)["status"]

    def _outbox_rows(self, execution_id=None):
        db = self.SessionLocal()
        try:
            q = db.query(ExecutionOutboxItem)
            if execution_id is not None:
                q = q.filter(ExecutionOutboxItem.EXECUTION_ID == execution_id)
            return [
                {
                    "outbox_id": r.OUTBOX_ID,
                    "status": r.STATUS,
                    "attempts": r.ATTEMPTS,
                    "last_error": r.LAST_ERROR,
                    "persona_id": r.PERSONA_ID,
                    "delivered_at": r.DELIVERED_AT,
                }
                for r in q.order_by(ExecutionOutboxItem.OUTBOX_ID.asc()).all()
            ]
        finally:
            db.close()


class StateMachineTests(ExecutionLedgerTestBase):
    def test_legal_lifecycle_prepared_running_applied_completed(self):
        execution_id, created = self.ledger.begin_execution(
            "judgment.day_open", idempotency_key="p1:2026-07-16", persona_id="p1",
            payload={"date": "2026-07-16"},
        )
        self.assertTrue(created)
        self.assertEqual(self._status(execution_id), XL.STATUS_PREPARED)

        self.ledger.mark_running(execution_id)
        self.assertEqual(self._status(execution_id), XL.STATUS_RUNNING)

        self.ledger.mark_applied(execution_id, result={"rounds": 3})
        self.assertEqual(self._status(execution_id), XL.STATUS_APPLIED)

        # outbox なしの実行は明示 mark_completed で閉じる
        self.ledger.mark_completed(execution_id)
        entry = self.ledger.get_execution(execution_id)
        self.assertEqual(entry["status"], XL.STATUS_COMPLETED)
        self.assertEqual(entry["result"], {"rounds": 3})
        self.assertEqual(entry["payload"], {"date": "2026-07-16"})

    def test_legal_prepared_to_failed(self):
        execution_id, _ = self.ledger.begin_execution("test.kind")
        self.ledger.mark_failed(execution_id, "expired before start")
        entry = self.ledger.get_execution(execution_id)
        self.assertEqual(entry["status"], XL.STATUS_FAILED)
        self.assertEqual(entry["error"], "expired before start")

    def test_legal_running_to_failed_and_unknown(self):
        e1 = self._begin_running()
        self.ledger.mark_failed(e1, "validation rejected before apply")
        self.assertEqual(self._status(e1), XL.STATUS_FAILED)

        e2 = self._begin_running()
        self.ledger.mark_unknown(e2, "process died")
        self.assertEqual(self._status(e2), XL.STATUS_UNKNOWN)

    def test_legal_unknown_to_applied_restoration(self):
        execution_id = self._begin_running()
        self.ledger.mark_unknown(execution_id)
        # 照合復元 (外部証跡が見つかった) — LLM 再実行ではない
        self.ledger.mark_applied(execution_id, result={"restored": True})
        self.assertEqual(self._status(execution_id), XL.STATUS_APPLIED)

    def test_illegal_transitions_raise(self):
        # prepared → applied / completed / unknown は不法
        execution_id, _ = self.ledger.begin_execution("test.kind")
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_applied(execution_id)
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_completed(execution_id)
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_unknown(execution_id)

        # running → running (二重開始) は不法
        self.ledger.mark_running(execution_id)
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_running(execution_id)

        # running → completed (applied を跨ぐ) は不法
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_completed(execution_id)

        # 終端 (failed / completed) からの遷移は不法
        failed_id, _ = self.ledger.begin_execution("test.kind2")
        self.ledger.mark_failed(failed_id, "boom")
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_running(failed_id)

        self.ledger.mark_applied(execution_id)
        self.ledger.mark_completed(execution_id)
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_applied(execution_id)

        # unknown → failed / running は不法 (照合復元 applied のみ)
        u_id = self._begin_running("test.kind3")
        self.ledger.mark_unknown(u_id)
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_failed(u_id, "x")
        with self.assertRaises(XL.IllegalTransitionError):
            self.ledger.mark_running(u_id)

    def test_unknown_execution_id_raises(self):
        with self.assertRaises(XL.ExecutionNotFoundError):
            self.ledger.mark_running("no-such-id")
        with self.assertRaises(XL.ExecutionNotFoundError):
            self.ledger.get_execution("no-such-id")


class IdempotencyTests(ExecutionLedgerTestBase):
    def test_duplicate_key_returns_existing_without_new_row(self):
        e1, created1 = self.ledger.begin_execution(
            "judgment.day_open", idempotency_key="p1:2026-07-16", persona_id="p1"
        )
        e2, created2 = self.ledger.begin_execution(
            "judgment.day_open", idempotency_key="p1:2026-07-16", persona_id="p1"
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(e1, e2)

        db = self.SessionLocal()
        try:
            count = db.query(ExecutionLedgerEntry).count()
        finally:
            db.close()
        self.assertEqual(count, 1)

    def test_same_key_different_kind_are_distinct(self):
        e1, c1 = self.ledger.begin_execution("judgment.day_open", idempotency_key="p1:x")
        e2, c2 = self.ledger.begin_execution("judgment.day_close", idempotency_key="p1:x")
        self.assertTrue(c1)
        self.assertTrue(c2)
        self.assertNotEqual(e1, e2)

    def test_null_key_never_collides(self):
        e1, c1 = self.ledger.begin_execution("metabolism.run")
        e2, c2 = self.ledger.begin_execution("metabolism.run")
        self.assertTrue(c1)
        self.assertTrue(c2)
        self.assertNotEqual(e1, e2)


class ClaimExecutionTests(ExecutionLedgerTestBase):
    """claim_execution (Phase 1 API — 判断点の重複抑止、W1 Chunk A / D2)。"""

    KIND = "judgment.day_open"
    KEY = "p1:2026-07-19"

    def _ledger_rows(self, kind=None):
        db = self.SessionLocal()
        try:
            q = db.query(ExecutionLedgerEntry)
            if kind is not None:
                q = q.filter(ExecutionLedgerEntry.KIND == kind)
            return [
                {
                    "execution_id": r.EXECUTION_ID,
                    "idempotency_key": r.IDEMPOTENCY_KEY,
                    "status": r.STATUS,
                    "payload": r.PAYLOAD_JSON,
                }
                for r in q.order_by(ExecutionLedgerEntry.CREATED_AT.asc()).all()
            ]
        finally:
            db.close()

    def test_claim_without_key_always_creates_new(self):
        e1, runnable1, st1 = self.ledger.claim_execution(
            "judgment.on_event", idempotency_key=None, persona_id="p1",
        )
        e2, runnable2, st2 = self.ledger.claim_execution(
            "judgment.on_event", idempotency_key=None, persona_id="p1",
        )
        self.assertTrue(runnable1)
        self.assertTrue(runnable2)
        self.assertIsNone(st1)
        self.assertIsNone(st2)
        self.assertNotEqual(e1, e2)
        self.assertEqual(self._status(e1), XL.STATUS_PREPARED)
        self.assertEqual(self._status(e2), XL.STATUS_PREPARED)

    def test_claim_new_key_creates_prepared(self):
        eid, runnable, st = self.ledger.claim_execution(
            self.KIND, idempotency_key=self.KEY, persona_id="p1",
            payload={"budget": 3},
        )
        self.assertTrue(runnable)
        self.assertIsNone(st)
        entry = self.ledger.get_execution(eid)
        self.assertEqual(entry["status"], XL.STATUS_PREPARED)
        self.assertEqual(entry["idempotency_key"], self.KEY)
        self.assertEqual(entry["payload"], {"budget": 3})

    def test_claim_reuses_prepared_and_keeps_frozen_payload(self):
        e1, _, _ = self.ledger.claim_execution(
            self.KIND, idempotency_key=self.KEY, persona_id="p1",
            payload={"budget": 3},
        )
        e2, runnable, st = self.ledger.claim_execution(
            self.KIND, idempotency_key=self.KEY, persona_id="p1",
            payload={"budget": 99},  # 上書きされないこと
        )
        self.assertEqual(e2, e1)
        self.assertTrue(runnable)
        self.assertEqual(st, XL.STATUS_PREPARED)
        self.assertEqual(self.ledger.get_execution(e1)["payload"], {"budget": 3})
        self.assertEqual(len(self._ledger_rows(self.KIND)), 1)

    def test_claim_retires_failed_key_and_creates_new(self):
        e1, _, _ = self.ledger.claim_execution(
            self.KIND, idempotency_key=self.KEY, persona_id="p1",
        )
        self.ledger.mark_failed(e1, "precondition rejected")

        e2, runnable, st = self.ledger.claim_execution(
            self.KIND, idempotency_key=self.KEY, persona_id="p1",
        )
        self.assertNotEqual(e2, e1)
        self.assertTrue(runnable)
        self.assertEqual(st, XL.STATUS_FAILED)
        self.assertEqual(self._status(e2), XL.STATUS_PREPARED)
        # 旧 failed 行のキーは {key}#failed-{id先頭8字} に退避され、新行が正キーを持つ
        old = self.ledger.get_execution(e1)
        self.assertEqual(
            old["idempotency_key"], f"{self.KEY}#failed-{e1[:8]}",
        )
        self.assertEqual(
            self.ledger.get_execution(e2)["idempotency_key"], self.KEY,
        )

    def test_claim_blocked_by_running_applied_completed(self):
        eid, _, _ = self.ledger.claim_execution(
            self.KIND, idempotency_key=self.KEY, persona_id="p1",
        )
        self.ledger.mark_running(eid)
        got = self.ledger.claim_execution(self.KIND, idempotency_key=self.KEY)
        self.assertEqual(got, (eid, False, XL.STATUS_RUNNING))

        self.ledger.mark_applied(eid)
        got = self.ledger.claim_execution(self.KIND, idempotency_key=self.KEY)
        self.assertEqual(got, (eid, False, XL.STATUS_APPLIED))

        self.ledger.mark_completed(eid)
        got = self.ledger.claim_execution(self.KIND, idempotency_key=self.KEY)
        self.assertEqual(got, (eid, False, XL.STATUS_COMPLETED))
        # 新規行は作られていない
        self.assertEqual(len(self._ledger_rows(self.KIND)), 1)

    def test_claim_blocked_by_unknown_with_explicit_log(self):
        eid, _, _ = self.ledger.claim_execution(
            self.KIND, idempotency_key=self.KEY, persona_id="p1",
        )
        self.ledger.mark_running(eid)
        self.ledger.mark_unknown(eid, "process died")

        with self.assertLogs("saiverse.execution_ledger", level="WARNING") as logs:
            got = self.ledger.claim_execution(self.KIND, idempotency_key=self.KEY)
        self.assertEqual(got, (eid, False, XL.STATUS_UNKNOWN))
        self.assertTrue(
            any("自動再実行" in m for m in logs.output),
            f"no explicit no-auto-rerun log: {logs.output}",
        )


class MarkAppliedAtomicityTests(ExecutionLedgerTestBase):
    def test_midway_failure_rolls_back_ledger_and_outbox(self):
        execution_id = self._begin_running(persona_id="p1")
        items = [
            {"target": "saimemory.append", "persona_id": "p1", "payload": {"text": "ok"}},
            # 2 件目で失敗を注入: set() は JSON 直列化できない → TypeError
            {"target": "saimemory.append", "persona_id": "p1", "payload": {"bad": {1, 2}}},
        ]
        with self.assertRaises(TypeError):
            self.ledger.mark_applied(execution_id, outbox_items=items)

        # ledger 更新と outbox の両方がロールバックされている
        self.assertEqual(self._status(execution_id), XL.STATUS_RUNNING)
        self.assertEqual(self._outbox_rows(execution_id), [])

    def test_session_riding_variant_shares_callers_transaction(self):
        execution_id = self._begin_running(persona_id="p1")
        # (a) 呼び出し元 session に相乗り → rollback で両方消える
        session = self.SessionLocal()
        try:
            self.ledger.mark_applied(
                execution_id,
                result={"n": 1},
                outbox_items=[
                    {"target": "saimemory.append", "persona_id": "p1", "payload": {"t": "x"}},
                ],
                session=session,
            )
            session.rollback()
        finally:
            session.close()
        self.assertEqual(self._status(execution_id), XL.STATUS_RUNNING)
        self.assertEqual(self._outbox_rows(execution_id), [])

        # (b) 同じ口で commit すれば両方永続化される
        session = self.SessionLocal()
        try:
            self.ledger.mark_applied(
                execution_id,
                result={"n": 2},
                outbox_items=[
                    {"target": "saimemory.append", "persona_id": "p1", "payload": {"t": "y"}},
                ],
                session=session,
            )
            session.commit()
        finally:
            session.close()
        self.assertEqual(self._status(execution_id), XL.STATUS_APPLIED)
        rows = self._outbox_rows(execution_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], XL.OUTBOX_PENDING)

    def test_outbox_item_requires_target_and_payload(self):
        execution_id = self._begin_running()
        with self.assertRaises(ValueError):
            self.ledger.mark_applied(
                execution_id, outbox_items=[{"payload": {"t": "x"}}]
            )
        self.assertEqual(self._status(execution_id), XL.STATUS_RUNNING)
        with self.assertRaises(ValueError):
            self.ledger.mark_applied(
                execution_id, outbox_items=[{"target": "saimemory.append"}]
            )
        self.assertEqual(self._status(execution_id), XL.STATUS_RUNNING)


class OutboxDeliveryTests(ExecutionLedgerTestBase):
    def setUp(self):
        super().setUp()
        self.delivered = []  # (persona_id, payload) の到着順記録
        self.fail_markers = set()  # payload["id"] がここにあれば handler が失敗する

        def handler(item):
            payload = item["payload"]
            if payload.get("id") in self.fail_markers:
                raise RuntimeError(f"injected failure for {payload.get('id')}")
            self.delivered.append((item["persona_id"], payload))

        self.ledger.register_outbox_handler("saimemory.append", handler)

    def _applied_with_items(self, persona_id, payload_ids, deliver=False):
        execution_id = self._begin_running(persona_id=persona_id)
        self.ledger.mark_applied(
            execution_id,
            outbox_items=[
                {
                    "target": "saimemory.append",
                    "persona_id": persona_id,
                    "payload": {"id": pid},
                }
                for pid in payload_ids
            ],
            deliver=deliver,
        )
        return execution_id

    def test_fifo_head_failure_blocks_tail_but_not_other_persona(self):
        self.fail_markers.add("p1-head")
        e1 = self._applied_with_items("p1", ["p1-head", "p1-tail"])
        e2 = self._applied_with_items("p2", ["p2-solo"])

        self.assertFalse(self.ledger.flush_pending_for_persona("p1"))
        # 先頭が失敗している間、後続 (p1-tail) は試行すらされない
        delivered_ids = [p["id"] for _, p in self.delivered]
        self.assertNotIn("p1-tail", delivered_ids)
        rows = self._outbox_rows(e1)
        self.assertEqual(rows[0]["attempts"], 1)
        self.assertEqual(rows[0]["status"], XL.OUTBOX_PENDING)
        self.assertEqual(rows[1]["attempts"], 0)
        self.assertEqual(rows[1]["status"], XL.OUTBOX_PENDING)

        # 別 persona のキューは独立に配送される
        self.assertTrue(self.ledger.flush_pending_for_persona("p2"))
        self.assertIn("p2-solo", [p["id"] for _, p in self.delivered])
        self.assertEqual(self._status(e2), XL.STATUS_COMPLETED)

        # 先頭の障害が解ければ FIFO 順で流れ、execution は completed になる
        self.fail_markers.clear()
        self.assertTrue(self.ledger.flush_pending_for_persona("p1"))
        p1_order = [p["id"] for pid, p in self.delivered if pid == "p1"]
        self.assertEqual(p1_order, ["p1-head", "p1-tail"])
        self.assertEqual(self._status(e1), XL.STATUS_COMPLETED)

    def test_gate_flush_true_on_full_delivery_false_on_failure(self):
        # 全配送成功 → True
        e1 = self._applied_with_items("p1", ["a", "b"])
        self.assertTrue(self.ledger.flush_pending_for_persona("p1"))
        self.assertEqual(
            [r["status"] for r in self._outbox_rows(e1)],
            [XL.OUTBOX_DELIVERED, XL.OUTBOX_DELIVERED],
        )

        # handler 失敗 → False かつ pending 残存 (fail-closed)
        self.fail_markers.add("c")
        self._applied_with_items("p1", ["c"])
        self.assertFalse(self.ledger.flush_pending_for_persona("p1"))
        pending = [r for r in self._outbox_rows() if r["status"] == XL.OUTBOX_PENDING]
        self.assertEqual(len(pending), 1)

    def test_flush_with_no_pending_returns_true(self):
        self.assertTrue(self.ledger.flush_pending_for_persona("p1"))

    def test_immediate_delivery_after_apply(self):
        # mark_applied(deliver=True) は commit 直後に即時配送する (intent §2.2)
        e1 = self._applied_with_items("p1", ["now"], deliver=True)
        self.assertIn("now", [p["id"] for _, p in self.delivered])
        self.assertEqual(self._status(e1), XL.STATUS_COMPLETED)

    def test_payload_frozen_verbatim(self):
        payload = {"id": "v", "text": "本文はそのまま", "tags": ["conversation"], "at": 123}
        execution_id = self._begin_running(persona_id="p1")
        self.ledger.mark_applied(
            execution_id,
            outbox_items=[
                {"target": "saimemory.append", "persona_id": "p1", "payload": payload}
            ],
            deliver=False,
        )
        self.ledger.flush_pending_for_persona("p1")
        self.assertEqual(self.delivered[-1][1], payload)

    def test_missing_handler_blocks_without_counting_attempts(self):
        execution_id = self._begin_running(persona_id="p1")
        self.ledger.mark_applied(
            execution_id,
            outbox_items=[
                {"target": "no.such.target", "persona_id": "p1", "payload": {"id": "x"}}
            ],
            deliver=False,
        )
        self.assertFalse(self.ledger.flush_pending_for_persona("p1"))
        rows = self._outbox_rows(execution_id)
        self.assertEqual(rows[0]["status"], XL.OUTBOX_PENDING)
        self.assertEqual(rows[0]["attempts"], 0)  # 実試行ではないので数えない
        self.assertIn("no handler registered", rows[0]["last_error"])

    def test_mark_completed_refuses_with_undelivered_outbox(self):
        self.fail_markers.add("stuck")
        execution_id = self._applied_with_items("p1", ["stuck"])
        self.ledger.flush_pending_for_persona("p1")
        with self.assertRaises(XL.ExecutionLedgerError):
            self.ledger.mark_completed(execution_id)
        self.assertEqual(self._status(execution_id), XL.STATUS_APPLIED)


class DeadLetterTests(ExecutionLedgerTestBase):
    def setUp(self):
        super().setUp()
        self.ledger = XL.ExecutionLedger(self.SessionLocal, max_attempts=3)
        self.handler_calls = []
        self.fail_markers = set()

        def handler(item):
            self.handler_calls.append(item["payload"]["id"])
            if item["payload"]["id"] in self.fail_markers:
                raise RuntimeError("injected failure")

        self.ledger.register_outbox_handler("saimemory.append", handler)

    def test_dead_after_max_attempts_then_skipped_and_not_blocking(self):
        self.fail_markers.add("poison")
        execution_id = self._begin_running(persona_id="p1")
        self.ledger.mark_applied(
            execution_id,
            outbox_items=[
                {"target": "saimemory.append", "persona_id": "p1", "payload": {"id": "poison"}},
                {"target": "saimemory.append", "persona_id": "p1", "payload": {"id": "after"}},
            ],
            deliver=False,
        )

        # 1〜2 回目: 先頭失敗 (pending のまま) → 後続ブロック
        self.assertFalse(self.ledger.flush_pending_for_persona("p1"))
        self.assertFalse(self.ledger.flush_pending_for_persona("p1"))
        self.assertNotIn("after", self.handler_calls)

        # 3 回目: 上限到達で dead → dead は後続をブロックしない → "after" 配送
        self.assertTrue(self.ledger.flush_pending_for_persona("p1"))
        rows = self._outbox_rows(execution_id)
        self.assertEqual(rows[0]["status"], XL.OUTBOX_DEAD)
        self.assertEqual(rows[0]["attempts"], 3)
        self.assertEqual(rows[1]["status"], XL.OUTBOX_DELIVERED)

        # dead は以後配送試行されない
        poison_calls = self.handler_calls.count("poison")
        self.assertTrue(self.ledger.flush_pending_for_persona("p1"))
        self.assertEqual(self.handler_calls.count("poison"), poison_calls)

        # dead を含む実行は completed にならず applied のまま観測面に残る
        self.assertEqual(self._status(execution_id), XL.STATUS_APPLIED)
        dead = self.ledger.list_dead()
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["execution_id"], execution_id)
        self.assertIn("injected failure", dead[0]["last_error"])


class RecoveryTests(ExecutionLedgerTestBase):
    def test_recover_stale_running_by_deadline(self):
        clock.enable_virtual(datetime(2026, 7, 16, 9, 0, 0))
        stale_id = self._begin_running("slot.fire", persona_id="p1")

        clock.advance_to(datetime(2026, 7, 16, 9, 50, 0))
        fresh_id = self._begin_running("slot.fire", persona_id="p2")

        clock.advance_to(datetime(2026, 7, 16, 10, 0, 0))
        recovered = self.ledger.recover_stale_running(max_age_seconds=1800)

        # 期限超過 (60 分前開始) のみ unknown、期限内 (10 分前開始) は running のまま
        self.assertEqual(recovered, [stale_id])
        self.assertEqual(self._status(stale_id), XL.STATUS_UNKNOWN)
        self.assertEqual(self._status(fresh_id), XL.STATUS_RUNNING)

        unknown = self.ledger.list_unknown()
        self.assertEqual([u["execution_id"] for u in unknown], [stale_id])
        self.assertIn("deadline", unknown[0]["error"])

    def test_recover_all_running_startup_sweep(self):
        e1 = self._begin_running("judgment.on_event", persona_id="p1")
        e2 = self._begin_running("metabolism.run", persona_id="p2")
        prepared_id, _ = self.ledger.begin_execution("test.kind")  # prepared は対象外

        recovered = self.ledger.recover_stale_running(all_running=True)
        self.assertEqual(set(recovered), {e1, e2})
        self.assertEqual(self._status(e1), XL.STATUS_UNKNOWN)
        self.assertEqual(self._status(e2), XL.STATUS_UNKNOWN)
        self.assertEqual(self._status(prepared_id), XL.STATUS_PREPARED)

    def test_recover_requires_criteria(self):
        with self.assertRaises(ValueError):
            self.ledger.recover_stale_running()


class MigrationLightweightPathTests(unittest.TestCase):
    """migrate.py の軽量パスが旧 DB に実行台帳テーブルを作ること。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "old.db")
        # 実行台帳テーブルを持たない「旧 DB」を作る
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            tables = [
                t for t in Base.metadata.sorted_tables
                if t.name not in ("execution_ledger", "execution_outbox")
            ]
            Base.metadata.create_all(engine, tables=tables)
        finally:
            engine.dispose()

    def tearDown(self):
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_ensure_execution_ledger_tables_creates_both(self):
        from database.migrate import ensure_execution_ledger_tables, needs_migration

        self.assertTrue(needs_migration(self.db_path))
        ensure_execution_ledger_tables(self.db_path)

        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            insp = inspect(engine)
            self.assertTrue(insp.has_table("execution_ledger"))
            self.assertTrue(insp.has_table("execution_outbox"))
            ledger_cols = {c["name"] for c in insp.get_columns("execution_ledger")}
            self.assertEqual(ledger_cols, {
                "EXECUTION_ID", "KIND", "IDEMPOTENCY_KEY", "PERSONA_ID", "STATUS",
                "PAYLOAD_JSON", "RESULT_JSON", "ERROR", "CREATED_AT", "UPDATED_AT",
            })
            outbox_cols = {c["name"] for c in insp.get_columns("execution_outbox")}
            self.assertEqual(outbox_cols, {
                "OUTBOX_ID", "EXECUTION_ID", "TARGET", "PERSONA_ID", "PAYLOAD_JSON",
                "STATUS", "ATTEMPTS", "LAST_ERROR", "CREATED_AT", "DELIVERED_AT",
            })
        finally:
            engine.dispose()

        # 冪等: 二度呼んでも安全
        ensure_execution_ledger_tables(self.db_path)

    def test_generic_additive_migration_also_creates_tables(self):
        from database.migrate import needs_migration, try_additive_migration

        self.assertTrue(needs_migration(self.db_path))
        self.assertTrue(try_additive_migration(self.db_path))
        self.assertFalse(needs_migration(self.db_path))


if __name__ == "__main__":
    unittest.main()
