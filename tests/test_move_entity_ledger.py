"""W5/B1 回帰: move_entity の台帳化 — 「commit 済みなのに失敗を返す」分裂の根絶。

分離監査 (docs/handoff/2026-07-15_persona_city_building_separation_audit.md) の
第一 finding: 移動 DB を先に commit し、occupants・イベント・後処理を別々に
実行するため、後処理の失敗で「DB は移動済み・呼び出し元は失敗扱い」の世界
分裂が成立していた。W5 の形:

- 位置遷移 + leave/enter イベント + 台帳 applied + 後処理 outbox = 単一 commit
- tx 失敗 → 全て巻き戻り + False (何も起きていない)
- commit 後は False を返さない — 後処理の失敗は outbox の再配送状態
"""
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import (
    AI,
    Base,
    BuildingMessage,
    BuildingOccupancyLog,
    City,
    ExecutionLedgerEntry,
    ExecutionOutboxItem,
    User,
)
from saiverse.execution_ledger import ExecutionLedger
from saiverse.occupancy_manager import OccupancyManager


class FakeBuilding:
    def __init__(self, name):
        self.name = name
        self.region_id = None
        self.base_system_instruction = ""
        self.physical_vessel_id = None


class MoveEntityLedgerTest(unittest.TestCase):
    USER_ID = 1
    MOVER = "air"
    WITNESS = "quon"

    def setUp(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.SessionLocal = sessionmaker(bind=engine)
        self.addCleanup(engine.dispose)

        db = self.SessionLocal()
        try:
            db.add(User(USERID=self.USER_ID, PASSWORD="x", USERNAME="まはー"))
            db.flush()
            city = City(USERID=self.USER_ID, CITY_SLUG="c", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            self.city_id = city.CITYID
            db.add(AI(AIID=self.MOVER, HOME_CITYID=city.CITYID, AINAME="Air"))
            db.add(AI(AIID=self.WITNESS, HOME_CITYID=city.CITYID, AINAME="Quon"))
            # 出発地の open な occupancy row
            db.add(BuildingOccupancyLog(
                CITYID=city.CITYID, AIID=self.MOVER, BUILDINGID="room_a",
                ENTRY_TIMESTAMP=datetime.now(),
            ))
            # user の現在地
            user = db.query(User).filter_by(USERID=self.USER_ID).first()
            user.CURRENT_BUILDINGID = "room_a"
            db.commit()
        finally:
            db.close()

        self.ledger = ExecutionLedger(session_factory=self.SessionLocal)
        self.delivered = []  # (target, payload) 記録
        self.fail_targets = set()

        def make_recorder(target):
            def handler(item):
                if target in self.fail_targets:
                    raise RuntimeError(f"{target} down")
                self.delivered.append((target, item["payload"]))
            return handler

        for target in (
            "move.post_dynamic_state",
            "move.post_addon_hooks",
            "move.post_game_lifecycle",
        ):
            self.ledger.register_outbox_handler(target, make_recorder(target))

        self.manager = SimpleNamespace(
            execution_ledger=self.ledger,
            quarantined_buildings={},
            personas={},
        )
        self.occupants = {
            "room_a": [self.MOVER, self.WITNESS],
            "room_b": [],
        }
        self.om = OccupancyManager(
            session_factory=self.SessionLocal,
            city_id=self.city_id,
            occupants=self.occupants,
            capacities={"room_a": 5, "room_b": 5},
            building_map={"room_a": FakeBuilding("A室"), "room_b": FakeBuilding("B室")},
            building_histories={},
            id_to_name_map={self.MOVER: "エア", self.WITNESS: "クオン"},
            user_id=self.USER_ID,
            manager_ref=self.manager,
        )

    # -- helpers --------------------------------------------------------

    def _building_events(self, building_id):
        db = self.SessionLocal()
        try:
            rows = (
                db.query(BuildingMessage)
                .filter_by(building_id=building_id)
                .order_by(BuildingMessage.seq)
                .all()
            )
            return [
                {
                    "content": r.content,
                    "heard_by": json.loads(r.heard_by or "[]"),
                    "event_type": r.event_type,
                }
                for r in rows
            ]
        finally:
            db.close()

    def _open_occupancy(self, building_id):
        db = self.SessionLocal()
        try:
            return (
                db.query(BuildingOccupancyLog)
                .filter_by(
                    AIID=self.MOVER, BUILDINGID=building_id, EXIT_TIMESTAMP=None
                )
                .count()
            )
        finally:
            db.close()

    def _executions(self, kind="move.entity"):
        db = self.SessionLocal()
        try:
            rows = db.query(ExecutionLedgerEntry).filter_by(KIND=kind).all()
            return [(r.EXECUTION_ID, r.STATUS) for r in rows]
        finally:
            db.close()

    def _pending_outbox(self):
        db = self.SessionLocal()
        try:
            return (
                db.query(ExecutionOutboxItem)
                .filter(ExecutionOutboxItem.STATUS == "pending")
                .count()
            )
        finally:
            db.close()

    # -- 正常系 ---------------------------------------------------------

    def test_ai_move_single_commit_and_post_processing_delivered(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)

        # 位置遷移 (occupancy log の close/open)
        self.assertEqual(self._open_occupancy("room_a"), 0)
        self.assertEqual(self._open_occupancy("room_b"), 1)
        # leave/enter イベントが移動 tx で確定している
        left = self._building_events("room_a")
        entered = self._building_events("room_b")
        self.assertEqual(len(left), 1)
        self.assertEqual(len(entered), 1)
        self.assertEqual(left[0]["event_type"], "occupancy")
        # heard_by: leave = 残った目撃者 / enter = 移動者を含む到着記録
        self.assertEqual(left[0]["heard_by"], [self.WITNESS])
        self.assertIn(self.MOVER, entered[0]["heard_by"])
        # in-memory occupants は commit 後に確定遷移を映す
        self.assertNotIn(self.MOVER, self.occupants["room_a"])
        self.assertIn(self.MOVER, self.occupants["room_b"])
        # 後処理 3 種が従来順に配送され、実行は completed
        self.assertEqual(
            [t for t, _p in self.delivered],
            ["move.post_dynamic_state", "move.post_addon_hooks",
             "move.post_game_lifecycle"],
        )
        self.assertEqual([s for _e, s in self._executions()], ["completed"])

    def test_user_move_updates_location_and_uses_none_queue(self):
        self.occupants["room_a"].append(str(self.USER_ID))
        ok, msg = self.om.move_entity(str(self.USER_ID), "user", "room_a", "room_b")
        self.assertTrue(ok, msg)
        db = self.SessionLocal()
        try:
            user = db.query(User).filter_by(USERID=self.USER_ID).first()
            self.assertEqual(user.CURRENT_BUILDINGID, "room_b")
        finally:
            db.close()
        # user 移動の後処理は game_lifecycle のみ・None キュー経由で即時配送
        self.assertEqual(
            [t for t, _p in self.delivered], ["move.post_game_lifecycle"]
        )
        self.assertEqual([s for _e, s in self._executions()], ["completed"])

    # -- tx 失敗 = 何も起きていない ------------------------------------

    def test_tx_failure_rolls_back_location_and_events(self):
        with patch(
            "database.building_messages.insert_building_message_in_session",
            side_effect=RuntimeError("event insert boom"),
        ):
            ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertFalse(ok)
        # 位置もイベントも巻き戻っている — 「失敗を返したのに DB は移動済み」が無い
        self.assertEqual(self._open_occupancy("room_a"), 1)
        self.assertEqual(self._open_occupancy("room_b"), 0)
        self.assertEqual(self._building_events("room_a"), [])
        self.assertEqual(self._building_events("room_b"), [])
        self.assertEqual(self._pending_outbox(), 0)
        self.assertIn(self.MOVER, self.occupants["room_a"])
        self.assertEqual([s for _e, s in self._executions()], ["failed"])

    # -- commit 後の後処理失敗 = 移動は成功・outbox が再配送 --------------

    def test_post_processing_failure_keeps_move_success_and_retries(self):
        self.fail_targets = {"move.post_dynamic_state"}
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)  # commit 後は False を返さない (B1)
        self.assertEqual(self._open_occupancy("room_b"), 1)
        # 先頭 (dynamic_state) の失敗が FIFO をブロック — 全 3 件 pending のまま
        self.assertEqual(self.delivered, [])
        self.assertEqual(self._pending_outbox(), 3)
        self.assertEqual([s for _e, s in self._executions()], ["applied"])

        # 障害回復後の flush で一度だけ配送され、実行が completed に進む
        self.fail_targets = set()
        self.assertTrue(self.ledger.flush_pending_for_persona(self.MOVER))
        self.assertEqual(
            [t for t, _p in self.delivered],
            ["move.post_dynamic_state", "move.post_addon_hooks",
             "move.post_game_lifecycle"],
        )
        self.assertEqual([s for _e, s in self._executions()], ["completed"])

    # -- 事前チェックは台帳に触らない ----------------------------------

    def test_precheck_rejection_writes_nothing(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "nowhere")
        self.assertFalse(ok)
        self.assertEqual(self._executions(), [])
        ok, msg = self.om.move_entity(self.MOVER, "bogus_type", "room_a", "room_b")
        self.assertFalse(ok)
        self.assertEqual(self._executions(), [])

    # -- 縮退 (台帳なし) ------------------------------------------------

    def test_legacy_mode_without_ledger_still_moves(self):
        events = []
        self.manager = SimpleNamespace(
            quarantined_buildings={},
            personas={},
            add_building_event=lambda bid, msg, heard_by=None: events.append(
                (bid, heard_by)
            ),
        )
        self.om._manager_ref = self.manager
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)
        self.assertEqual(self._open_occupancy("room_b"), 1)
        self.assertEqual([bid for bid, _h in events], ["room_a", "room_b"])
        self.assertEqual(self._executions(), [])


class MoveHandlerFactoryTest(unittest.TestCase):
    """execution_ledger_wiring の move.post_* ハンドラ工場のガード。"""

    def _item(self, payload):
        return {
            "outbox_id": 1, "execution_id": "e1",
            "target": "t", "persona_id": None, "payload": payload,
            "created_at": 0,
        }

    def test_game_lifecycle_handler_calls_on_entity_moved(self):
        from saiverse.execution_ledger_wiring import (
            _make_move_game_lifecycle_handler,
        )
        calls = []

        def _record(e, f, t):
            calls.append((e, f, t))
            return True

        manager = SimpleNamespace(
            game_lifecycle=SimpleNamespace(on_entity_moved=_record)
        )
        handler = _make_move_game_lifecycle_handler(manager)
        handler(self._item({
            "entity_id": "air", "entity_type": "ai",
            "from_id": "a", "to_id": "b",
        }))
        self.assertEqual(calls, [("air", "a", "b")])
        # game_lifecycle の無い manager は no-op
        handler2 = _make_move_game_lifecycle_handler(SimpleNamespace())
        handler2(self._item({
            "entity_id": "air", "entity_type": "ai",
            "from_id": "a", "to_id": "b",
        }))

    def test_game_lifecycle_handler_raises_when_sync_fails(self):
        """2026-07-21 Codex レビュー P2: on_entity_moved の内部失敗 (False) を
        配送失敗として伝播し、outbox の delivered 誤記帳を防ぐ。"""
        from saiverse.execution_ledger_wiring import (
            _make_move_game_lifecycle_handler,
        )
        manager = SimpleNamespace(
            game_lifecycle=SimpleNamespace(
                on_entity_moved=lambda e, f, t: False
            )
        )
        handler = _make_move_game_lifecycle_handler(manager)
        with self.assertRaises(RuntimeError):
            handler(self._item({
                "entity_id": "air", "entity_type": "ai",
                "from_id": "a", "to_id": "b",
            }))

    def test_dynamic_state_handler_guards(self):
        from saiverse.execution_ledger_wiring import (
            _make_move_dynamic_state_handler,
        )
        handler = _make_move_dynamic_state_handler(
            SimpleNamespace(personas={})
        )
        # user 移動は対象外 / 未ロードペルソナは恒久 no-op — どちらも raise しない
        handler(self._item({
            "entity_id": "1", "entity_type": "user",
            "from_id": "a", "to_id": "b",
        }))
        handler(self._item({
            "entity_id": "ghost", "entity_type": "ai",
            "from_id": "a", "to_id": "b",
        }))
        # payload 不備は配送失敗として表明する
        with self.assertRaises(ValueError):
            handler(self._item({"entity_type": "ai"}))

    def test_dynamic_state_handler_raises_when_sync_fails(self):
        """2026-07-21 Codex レビュー P2: on_building_entered の内部失敗 (False)
        を配送失敗として伝播し、outbox の delivered 誤記帳を防ぐ。"""
        from saiverse.execution_ledger_wiring import (
            _make_move_dynamic_state_handler,
        )
        persona = SimpleNamespace(persona_id="air")
        manager = SimpleNamespace(personas={"air": persona})
        handler = _make_move_dynamic_state_handler(manager)
        with patch(
            "saiverse.dynamic_state.DynamicStateManager.on_building_entered",
            return_value=False,
        ):
            with self.assertRaises(RuntimeError):
                handler(self._item({
                    "entity_id": "air", "entity_type": "ai",
                    "from_id": "a", "to_id": "b",
                }))


class MoveDeadlockRegressionTest(unittest.TestCase):
    """2026-07-21 Codex レビュー P1: outbox handler が誘発する再帰的な
    flush_pending_for_persona が非再入 _delivery_lock でデッドロックしない
    ことの回帰。実際の on_building_entered / on_entity_moved は呼ばず、
    「handler の中から flush_pending_for_persona を呼ぶ」という再入構造だけを
    最小構成で再現する (実処理の細部に依存せず、ロックの再入安全性そのものを
    固定する)。
    """

    def setUp(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.SessionLocal = sessionmaker(bind=engine)
        self.addCleanup(engine.dispose)
        self.ledger = ExecutionLedger(session_factory=self.SessionLocal)

    def test_handler_reentering_flush_does_not_deadlock(self):
        calls = []

        def reentrant_handler(item):
            calls.append("outer")
            # ハンドラの中から自分自身の配送を再度呼ぶ — 非再入ロックのまま
            # なら永久待ちになる経路 (dynamic_state / game_lifecycle が
            # 誘発する「別の移動」の簡略化モデル)。
            self.ledger.flush_pending_for_persona("air")
            calls.append("inner-returned")

        self.ledger.register_outbox_handler("test.reentrant", reentrant_handler)
        execution_id, _ = self.ledger.begin_execution("test.kind", persona_id="air")
        self.ledger.mark_running(execution_id)
        self.ledger.mark_applied(
            execution_id,
            outbox_items=[{
                "target": "test.reentrant", "payload": {}, "persona_id": "air",
            }],
        )
        # デッドロックしていればこの assert 自体に到達しない (pytest がタイムアウトで検知)
        self.assertEqual(calls, ["outer", "inner-returned"])
        self.assertEqual(
            self.ledger.get_execution(execution_id)["status"], "completed"
        )


if __name__ == "__main__":
    unittest.main()
