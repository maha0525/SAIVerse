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
from saiverse.occupancy_manager import (
    MoveDenialMessage,
    MoveRedirectedNotice,
    OccupancyManager,
    is_redirect_notice,
    move_through_entrances,
)


class FakeBuilding:
    def __init__(self, name):
        self.name = name
        self.region_id = None
        self.base_system_instruction = ""
        self.physical_vessel_id = None


class _MoveLedgerFixture(unittest.TestCase):
    """実 DB (in-memory SQLite) + 実台帳の move_entity 組み立て。テストは持たない。"""
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
            personas={},
            get_region=lambda region_id: None,
            get_top_region_of_building=lambda building_id: None,
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


class MoveEntityLedgerTest(_MoveLedgerFixture):
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


class _EntranceWorldFixture(_MoveLedgerFixture):
    """Region / SubRegion を持つ世界の組み立て。テストは持たない。

    スコープ構成 (room_a / room_b は City 直属):
      City 直属: entrance_top (Region 'top' の入口)
      Region 'top' 直属: t1, entrance_sub (SubRegion 'sub' の入口)
      SubRegion 'sub' 直属: s1
    """

    def setUp(self):
        super().setUp()
        self.regions = {
            "top": SimpleNamespace(
                region_id="top", name="霧の谷", parent_region_id=None,
                entrance_building_id="entrance_top", config={},
            ),
            "sub": SimpleNamespace(
                region_id="sub", name="霧降りの森", parent_region_id="top",
                entrance_building_id="entrance_sub", config={},
            ),
        }
        self.manager.get_region = self.regions.get
        for bid, name, region_id in (
            ("entrance_top", "霧の谷: 入口", None),
            ("t1", "宿屋", "top"),
            ("entrance_sub", "霧降りの森: 入口", "top"),
            ("s1", "祠", "sub"),
        ):
            building = FakeBuilding(name)
            building.region_id = region_id
            self.om.building_map[bid] = building
            self.om.capacities[bid] = 5
            self.occupants.setdefault(bid, [])
        # canonical location の同期先 (move_entity が一元更新する)
        self.persona = SimpleNamespace(current_building_id="room_a")
        self.manager.personas[self.MOVER] = self.persona
        self.manager.state = SimpleNamespace(user_current_building_id="room_a")

    def _user_location(self):
        db = self.SessionLocal()
        try:
            return db.query(User).filter_by(USERID=self.USER_ID).first().CURRENT_BUILDINGID
        finally:
            db.close()


class EntranceRedirectTest(_EntranceWorldFixture):
    """region.md §2.5: 外部から Region 内部への直行は、その場で拒否せず
    まだ入っていない一番外側の境界の入口まで実際に移動して止める
    (実 DB + 実台帳で確認する)。"""

    # 1. 外部から Region 内部への直行 → 入口に到着して (True, 案内文)
    def test_ai_direct_move_stops_at_entrance(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "t1")
        self.assertTrue(ok, msg)
        self.assertIn("'宿屋' は『霧の谷』の内部です", msg)
        self.assertIn("入口 '霧の谷: 入口' まで移動しました", msg)
        # 成功側の案内は型付き: 呼び出し元は code で「入口で止まった」を判別し、
        # current_building_id で実際の到着地を読む
        self.assertIsInstance(msg, MoveRedirectedNotice)
        self.assertEqual(msg.code, "redirected_to_entrance")
        self.assertTrue(is_redirect_notice(msg))
        self.assertEqual(msg.current_building_id, "entrance_top")
        # canonical な位置は入口 (依頼した t1 ではない)
        self.assertEqual(self._open_occupancy("entrance_top"), 1)
        self.assertEqual(self._open_occupancy("room_a"), 0)
        self.assertEqual(self._open_occupancy("t1"), 0)
        self.assertEqual(self.persona.current_building_id, "entrance_top")
        self.assertIn(self.MOVER, self.occupants["entrance_top"])
        self.assertNotIn(self.MOVER, self.occupants["t1"])
        # 移動は入口への 1 件だけが台帳に載り、入室の記録も入口にだけ残る
        self.assertEqual([s for _e, s in self._executions()], ["completed"])
        self.assertEqual(len(self._building_events("entrance_top")), 1)
        self.assertEqual(self._building_events("t1"), [])

    # 2. 入口に居て locked の Region 内部へ → 従来どおり (False, 鍵の文)
    def test_locked_region_from_entrance_denies_without_moving(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "entrance_top")
        self.assertTrue(ok, msg)
        self.assertIsNone(msg)  # City 直属どうしの移動はリダイレクトしない
        self.regions["top"].config = {"entry_policy": "locked"}
        ok, msg = self.om.move_entity(self.MOVER, "ai", "entrance_top", "t1")
        self.assertFalse(ok)
        self.assertIn("鍵", msg)
        self.assertEqual(self._open_occupancy("entrance_top"), 1)
        self.assertEqual(self._open_occupancy("t1"), 0)
        self.assertEqual(len(self._executions()), 1)  # 最初の入口移動だけ

    # 3. 入口未設定の Region 内部へ → 従来どおり (False)
    def test_region_without_entrance_denies_without_moving(self):
        self.regions["top"].entrance_building_id = None
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "t1")
        self.assertFalse(ok)
        self.assertIn("入口が設定されていない", msg)
        self.assertEqual(self._open_occupancy("room_a"), 1)
        self.assertEqual(self._executions(), [])

    # 4. 入口が定員オーバー (AI) → (False, 入口への移動自身の失敗理由)
    def test_full_entrance_returns_entrance_move_reason(self):
        self.om.capacities["entrance_top"] = 1
        self.occupants["entrance_top"].append("someone_else")
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "t1")
        self.assertFalse(ok)
        self.assertEqual(msg, "霧の谷: 入口は定員オーバーです")
        self.assertNotEqual(getattr(msg, "code", None), "not_via_entrance")
        self.assertEqual(self._open_occupancy("room_a"), 1)
        self.assertEqual(self.persona.current_building_id, "room_a")
        self.assertEqual(self._executions(), [])

    # 5. user エンティティでも 1 と同じリダイレクトが起きる
    def test_user_direct_move_stops_at_entrance(self):
        self.occupants["room_a"].append(str(self.USER_ID))
        ok, msg = self.om.move_entity(str(self.USER_ID), "user", "room_a", "t1")
        self.assertTrue(ok, msg)
        self.assertIn("入口 '霧の谷: 入口' まで移動しました", msg)
        self.assertEqual(self._user_location(), "entrance_top")
        self.assertEqual(self.manager.state.user_current_building_id, "entrance_top")
        self.assertIn(str(self.USER_ID), self.occupants["entrance_top"])

    # 6. 外部から SubRegion 内部へ直行 → 一番外側の Region の入口で止まる
    def test_direct_move_into_subregion_stops_at_outermost_entrance(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "s1")
        self.assertTrue(ok, msg)
        self.assertIn("'祠' は『霧の谷』の内部です", msg)
        self.assertEqual(self._open_occupancy("entrance_top"), 1)
        self.assertEqual(self._open_occupancy("entrance_sub"), 0)
        self.assertEqual(self._open_occupancy("s1"), 0)

    # 7. topology_bypass 中は従来どおり素通り
    def test_topology_bypass_moves_directly(self):
        with self.om.topology_bypass():
            ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "s1")
        self.assertTrue(ok, msg)
        self.assertIsNone(msg)
        self.assertEqual(self._open_occupancy("s1"), 1)
        self.assertEqual(self._open_occupancy("entrance_top"), 0)

    # 8. リダイレクトは 1 ホップ限り (入口への移動が更にリダイレクト拒否されても再帰しない)
    def test_redirect_is_single_hop(self):
        first = MoveDenialMessage(
            "first", code="not_via_entrance",
            redirect_building_id="entrance_top", redirect_message="arrived",
        )
        second = MoveDenialMessage(
            "second", code="not_via_entrance",
            redirect_building_id="entrance_sub", redirect_message="arrived-2",
        )
        with patch.object(
            self.om, "_check_entrance_topology", side_effect=[first, second],
        ) as check:
            ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "t1")
        self.assertFalse(ok)
        # 失敗は入口への移動自身の理由 (再帰側の拒否) をそのまま運ぶ
        self.assertEqual(str(msg), "second")
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self._open_occupancy("room_a"), 1)
        self.assertEqual(self._executions(), [])

    # 入口への移動が CAS 競合 (移動元が古い) で転んだら、再同期の 409 を優先して運ぶ
    def test_redirect_cas_conflict_is_passed_through(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_b", "t1")
        self.assertFalse(ok)
        self.assertEqual(getattr(msg, "code", None), "cas_conflict")
        self.assertEqual(getattr(msg, "current_building_id", None), "room_a")
        self.assertEqual(self._open_occupancy("room_a"), 1)

    # 9. 最外殻の入口に立って SubRegion 内部へ直行 → 一つ内側の入口まで進む
    #    (region.md §2.5 第 2 項。この一歩は外側の境界を正規に通過する)
    def test_from_outer_entrance_steps_to_inner_entrance(self):
        ok, _msg = self.om.move_entity(self.MOVER, "ai", "room_a", "entrance_top")
        self.assertTrue(ok)
        ok, msg = self.om.move_entity(self.MOVER, "ai", "entrance_top", "s1")
        self.assertTrue(ok, msg)
        self.assertTrue(is_redirect_notice(msg))
        self.assertEqual(msg.current_building_id, "entrance_sub")
        self.assertIn("'祠' は『霧降りの森』の内部です", msg)
        self.assertIn("入口 '霧降りの森: 入口' まで移動しました", msg)
        self.assertEqual(self._open_occupancy("entrance_sub"), 1)
        self.assertEqual(self._open_occupancy("s1"), 0)
        self.assertEqual(self.persona.current_building_id, "entrance_sub")

    # 10. その一歩は外側の entry policy に掛かる — 鍵なら鍵の文で失敗し、動かない
    def test_step_to_inner_entrance_is_blocked_by_outer_lock(self):
        ok, _msg = self.om.move_entity(self.MOVER, "ai", "room_a", "entrance_top")
        self.assertTrue(ok)
        self.regions["top"].config = {"entry_policy": "locked"}
        ok, msg = self.om.move_entity(self.MOVER, "ai", "entrance_top", "s1")
        self.assertFalse(ok)
        self.assertEqual(msg, "移動失敗: 『霧の谷』には鍵がかかっています。")
        self.assertEqual(self._open_occupancy("entrance_top"), 1)
        self.assertEqual(self._open_occupancy("entrance_sub"), 0)
        self.assertEqual(self.persona.current_building_id, "entrance_top")

    # 11. 外側の policy が通す (entry_allowed) なら内側の入口まで進める
    def test_step_to_inner_entrance_passes_outer_whitelist(self):
        ok, _msg = self.om.move_entity(self.MOVER, "ai", "room_a", "entrance_top")
        self.assertTrue(ok)
        self.regions["top"].config = {
            "entry_policy": "whitelist", "entry_allowed": [self.MOVER],
        }
        ok, msg = self.om.move_entity(self.MOVER, "ai", "entrance_top", "s1")
        self.assertTrue(ok, msg)
        self.assertEqual(msg.current_building_id, "entrance_sub")


class MoveThroughEntrancesTest(_EntranceWorldFixture):
    """機構代行の移動 (move_through_entrances) は境界を一段ずつ通過して目的地まで
    進む (region.md §2.5)。"""

    def _move(self, from_id, to_id):
        return self.om.move_entity(self.MOVER, "ai", from_id, to_id)

    def test_reaches_subregion_interior_in_three_steps(self):
        reached, msg, location = move_through_entrances(self._move, "room_a", "s1")
        self.assertTrue(reached, msg)
        self.assertEqual(location, "s1")
        self.assertEqual(self._open_occupancy("s1"), 1)
        self.assertEqual(self.persona.current_building_id, "s1")
        # 入口 → 内側の入口 → 目的地 の 3 手がそれぞれ台帳に載る
        self.assertEqual(
            [s for _e, s in self._executions()], ["completed"] * 3
        )

    def test_reaches_region_interior_in_two_steps(self):
        reached, _msg, location = move_through_entrances(self._move, "room_a", "t1")
        self.assertTrue(reached)
        self.assertEqual(location, "t1")
        self.assertEqual(len(self._executions()), 2)

    def test_stops_honestly_at_locked_boundary(self):
        self.regions["sub"].config = {"entry_policy": "locked"}
        reached, msg, location = move_through_entrances(self._move, "room_a", "s1")
        self.assertFalse(reached)
        self.assertEqual(msg, "移動失敗: 『霧降りの森』には鍵がかかっています。")
        # 入口までは来ている (内側の入口で止まる)
        self.assertEqual(location, "entrance_sub")
        self.assertEqual(self.persona.current_building_id, "entrance_sub")
        self.assertEqual(self._open_occupancy("s1"), 0)

    def test_plain_move_is_single_step(self):
        reached, msg, location = move_through_entrances(self._move, "room_a", "room_b")
        self.assertTrue(reached, msg)
        self.assertEqual(location, "room_b")
        self.assertEqual(len(self._executions()), 1)

    def test_step_budget_exhausted_reports_last_notice(self):
        reached, msg, location = move_through_entrances(
            self._move, "room_a", "s1", max_steps=1,
        )
        self.assertFalse(reached)
        self.assertTrue(is_redirect_notice(msg))
        self.assertEqual(location, "entrance_top")


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

    2026-09-07 の新契約: ネストした呼び出しは配送せずに戻る (ここまで従来通り)
    が、依頼された persona は控えに載り、外側の配達が鍵を離した後に引き継がれる。
    引き継ぎの flush は配り終わった後なので、同じ行がもう一度配られることはない。
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
        # デッドロックしていればこの assert 自体に到達しない (pytest がタイムアウトで検知)。
        # 引き継ぎで "air" をもう一度 flush しても配る行はもう無いので、handler は
        # 一度しか呼ばれない (= 二重配送しない)。
        self.assertEqual(calls, ["outer", "inner-returned"])
        self.assertEqual(
            self.ledger.get_execution(execution_id)["status"], "completed"
        )


if __name__ == "__main__":
    unittest.main()
