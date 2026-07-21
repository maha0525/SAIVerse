"""W7/柱5 回帰: 位置・占有の canonical 化 (分離監査 P1-1残 / P1-2 / P2-1 / P2-2)。

- P1-2: active occupancy の部分一意 index + 重複修復 + move_entity の CAS
- P1-1残: persona 属性 / user state の更新を移動 service (move_entity) へ集約
- P2-1: occupancy event_key の採番 ID 化 (同一秒の同経路移動が衝突しない)
- P2-2: startup consistency checker (重複修復 / 派遣中 active 行 / capacity 超過)

設計: docs/handoff/2026-07-21_w7_location_occupancy_handoff.md
"""
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import (
    AI,
    Base,
    BuildingMessage,
    BuildingOccupancyLog,
    City,
    User,
)
from database.occupancy_repair import (
    ensure_active_occupancy_unique_index,
    repair_duplicate_active_occupancy,
)
from saiverse.execution_ledger import ExecutionLedger
from saiverse.occupancy_manager import OccupancyManager


class FakeBuilding:
    def __init__(self, name):
        self.name = name
        self.region_id = None
        self.base_system_instruction = ""
        self.physical_vessel_id = None


class FakePersona:
    """canonical sync の儀式 (_mark_entry / _save_session_metadata) を記録する。"""

    def __init__(self, building_id):
        self.current_building_id = building_id
        self.mark_entry_calls = []
        self.save_calls = 0

    def _mark_entry(self, building_id):
        self.mark_entry_calls.append(building_id)

    def _save_session_metadata(self):
        self.save_calls += 1


def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


class OccupancyRepairTest(unittest.TestCase):
    """重複 active 行の修復 + 部分一意 index (P1-2)。"""

    def setUp(self):
        self.engine = _make_engine()
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        db = self.SessionLocal()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
            db.flush()
            db.add(City(CITYID=1, USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001))
            db.add(AI(AIID="air", HOME_CITYID=1, AINAME="Air"))
            db.commit()
        finally:
            db.close()

    def _add_active(self, building_id, entry):
        db = self.SessionLocal()
        try:
            db.add(BuildingOccupancyLog(
                CITYID=1, AIID="air", BUILDINGID=building_id,
                ENTRY_TIMESTAMP=entry,
            ))
            db.commit()
        finally:
            db.close()

    def _active_rows(self):
        db = self.SessionLocal()
        try:
            return [
                (r.BUILDINGID, r.EXIT_TIMESTAMP)
                for r in db.query(BuildingOccupancyLog)
                .filter_by(AIID="air")
                .order_by(BuildingOccupancyLog.ID)
                .all()
            ]
        finally:
            db.close()

    def test_repair_keeps_newest_and_closes_the_rest(self):
        self._add_active("old_room", datetime(2026, 7, 20, 10, 0, 0))
        self._add_active("new_room", datetime(2026, 7, 21, 10, 0, 0))
        db = self.SessionLocal()
        try:
            repairs = repair_duplicate_active_occupancy(db)
            db.commit()
        finally:
            db.close()
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["ai_id"], "air")
        self.assertEqual(repairs[0]["canonical_building_id"], "new_room")
        self.assertEqual(len(repairs[0]["closed_rows"]), 1)
        rows = self._active_rows()
        # old_room は close 済み、new_room が唯一の active
        self.assertIsNotNone(rows[0][1])
        self.assertIsNone(rows[1][1])

    def test_repair_prefers_referentially_valid_row(self):
        """canonical 選択は参照整合な行を優先 (Codex 第七巡 P1) — 最新行が
        削除済み Building を指す場合、有効な旧行を残す。"""
        db = self.SessionLocal()
        try:
            from database.models import Building as BuildingModel
            db.add(BuildingModel(CITYID=1, BUILDINGID="valid_room", BUILDINGNAME="V"))
            db.commit()
        finally:
            db.close()
        self._add_active("valid_room", datetime(2026, 7, 20, 10, 0, 0))
        self._add_active("deleted_room", datetime(2026, 7, 21, 10, 0, 0))  # 最新だが無効
        db = self.SessionLocal()
        try:
            repairs = repair_duplicate_active_occupancy(db)
            db.commit()
        finally:
            db.close()
        self.assertEqual(repairs[0]["canonical_building_id"], "valid_room")
        db = self.SessionLocal()
        try:
            active = db.query(BuildingOccupancyLog).filter_by(
                AIID="air", EXIT_TIMESTAMP=None
            ).all()
            self.assertEqual([r.BUILDINGID for r in active], ["valid_room"])
        finally:
            db.close()

    def test_repair_noop_when_no_duplicates(self):
        self._add_active("room", datetime(2026, 7, 21, 10, 0, 0))
        db = self.SessionLocal()
        try:
            repairs = repair_duplicate_active_occupancy(db)
            db.commit()
        finally:
            db.close()
        self.assertEqual(repairs, [])

    def test_unique_index_blocks_second_active_row(self):
        self._add_active("room_a", datetime(2026, 7, 21, 10, 0, 0))
        with self.engine.begin() as conn:
            ensure_active_occupancy_unique_index(conn)
        with self.assertRaises(IntegrityError):
            self._add_active("room_b", datetime(2026, 7, 21, 11, 0, 0))
        # closed 行は index の対象外 — 同じ AIID の履歴行は増やせる
        db = self.SessionLocal()
        try:
            db.add(BuildingOccupancyLog(
                CITYID=1, AIID="air", BUILDINGID="room_b",
                ENTRY_TIMESTAMP=datetime(2026, 7, 20, 9, 0, 0),
                EXIT_TIMESTAMP=datetime(2026, 7, 20, 9, 30, 0),
            ))
            db.commit()
        finally:
            db.close()

    def test_index_creation_after_repair_succeeds(self):
        self._add_active("old_room", datetime(2026, 7, 20, 10, 0, 0))
        self._add_active("new_room", datetime(2026, 7, 21, 10, 0, 0))
        with self.engine.begin() as conn:
            repair_duplicate_active_occupancy(conn)
            ensure_active_occupancy_unique_index(conn)
        # 修復後なので index 作成が成功し、以降の重複 insert は拒否される
        with self.assertRaises(IntegrityError):
            self._add_active("third_room", datetime(2026, 7, 21, 12, 0, 0))


class MoveEntityCasTest(unittest.TestCase):
    """move_entity の CAS + canonical sync + event_key (P1-2 / P1-1残 / P2-1)。"""

    USER_ID = 1
    MOVER = "air"

    def setUp(self):
        self.engine = _make_engine()
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)

        db = self.SessionLocal()
        try:
            db.add(User(USERID=self.USER_ID, PASSWORD="x", USERNAME="まはー"))
            db.flush()
            city = City(USERID=self.USER_ID, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            self.city_id = city.CITYID
            db.add(AI(AIID=self.MOVER, HOME_CITYID=city.CITYID, AINAME="Air"))
            db.add(BuildingOccupancyLog(
                CITYID=city.CITYID, AIID=self.MOVER, BUILDINGID="room_a",
                ENTRY_TIMESTAMP=datetime.now(),
            ))
            user = db.query(User).filter_by(USERID=self.USER_ID).first()
            user.CURRENT_BUILDINGID = "room_a"
            db.commit()
        finally:
            db.close()

        self.ledger = ExecutionLedger(session_factory=self.SessionLocal)
        for target in (
            "move.post_dynamic_state",
            "move.post_addon_hooks",
            "move.post_game_lifecycle",
        ):
            self.ledger.register_outbox_handler(target, lambda item: None)

        self.persona = FakePersona("room_a")
        self.manager = SimpleNamespace(
            execution_ledger=self.ledger,
            quarantined_buildings={},
            personas={self.MOVER: self.persona},
            state=SimpleNamespace(user_current_building_id="room_a"),
        )
        self.occupants = {
            "room_a": [self.MOVER],
            "room_b": [],
            "room_c": [],
        }
        self.om = OccupancyManager(
            session_factory=self.SessionLocal,
            city_id=self.city_id,
            occupants=self.occupants,
            capacities={"room_a": 5, "room_b": 5, "room_c": 5},
            building_map={
                "room_a": FakeBuilding("A室"),
                "room_b": FakeBuilding("B室"),
                "room_c": FakeBuilding("C室"),
            },
            building_histories={},
            id_to_name_map={self.MOVER: "エア"},
            user_id=self.USER_ID,
            manager_ref=self.manager,
        )

    # -- helpers --------------------------------------------------------

    def _active_rows(self, ai_id=None):
        db = self.SessionLocal()
        try:
            q = db.query(BuildingOccupancyLog).filter_by(EXIT_TIMESTAMP=None)
            if ai_id:
                q = q.filter_by(AIID=ai_id)
            return [(r.AIID, r.BUILDINGID) for r in q.all()]
        finally:
            db.close()

    def _event_keys(self):
        db = self.SessionLocal()
        try:
            keys = []
            for r in db.query(BuildingMessage).order_by(BuildingMessage.id).all():
                event = json.loads(r.event_data or "{}")
                if event.get("type") == "occupancy":
                    keys.append(event.get("event_key"))
            return keys
        finally:
            db.close()

    # -- CAS ------------------------------------------------------------

    def test_stale_from_is_refused_without_mutation(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_b", "room_c")
        self.assertFalse(ok)
        self.assertIn("現在地が変わっています", msg)
        self.assertIn("A室", msg)
        # CAS 競合は型付きメッセージ (route 層が 409 へ変換する)。
        # DB 確定現在地も運ぶ (第三巡 P2)
        self.assertEqual(getattr(msg, "code", None), "cas_conflict")
        self.assertEqual(getattr(msg, "current_building_id", None), "room_a")
        # DB 無変異 + 属性も儀式も動いていない
        self.assertEqual(self._active_rows(self.MOVER), [(self.MOVER, "room_a")])
        self.assertEqual(self.persona.current_building_id, "room_a")
        self.assertEqual(self.persona.mark_entry_calls, [])

    def test_duplicate_active_rows_refused_as_corruption(self):
        db = self.SessionLocal()
        try:
            db.add(BuildingOccupancyLog(
                CITYID=self.city_id, AIID=self.MOVER, BUILDINGID="room_b",
                ENTRY_TIMESTAMP=datetime.now(),
            ))
            db.commit()
        finally:
            db.close()
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_c")
        self.assertFalse(ok)
        self.assertIn("破損", msg)
        self.assertEqual(len(self._active_rows(self.MOVER)), 2)  # 無変異

    def test_missing_active_row_self_heals(self):
        db = self.SessionLocal()
        try:
            row = db.query(BuildingOccupancyLog).filter_by(AIID=self.MOVER).first()
            row.EXIT_TIMESTAMP = datetime.now()
            db.commit()
        finally:
            db.close()
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)
        self.assertEqual(self._active_rows(self.MOVER), [(self.MOVER, "room_b")])

    def test_close_active_row_cas_loses_when_row_already_closed(self):
        """条件付き UPDATE の仲裁 (2026-07-21 Codex P1 同型): 事前 SELECT が
        旧値を見ていても、書き込み時点で close 済みなら負ける。"""
        db = self.SessionLocal()
        try:
            row = db.query(BuildingOccupancyLog).filter_by(
                AIID=self.MOVER, EXIT_TIMESTAMP=None
            ).first()
            row_id = row.ID
            # 別の移動が先に close した状況を再現
            row.EXIT_TIMESTAMP = datetime.now()
            db.commit()
        finally:
            db.close()
        db = self.SessionLocal()
        try:
            self.assertFalse(
                self.om._close_active_row_cas(db, row_id, datetime.now())
            )
            db.rollback()
        finally:
            db.close()

    def test_cas_update_user_location_arbitration(self):
        """user 位置の条件付き UPDATE: 現在地が一致するときだけ勝つ。"""
        db = self.SessionLocal()
        try:
            # 一致 → 勝ち
            self.assertTrue(
                self.om._cas_update_user_location(
                    db, self.USER_ID, "room_a", "room_b"
                )
            )
            db.commit()
            # 既に room_b — 古い from (room_a) からの並行移動は負ける
            self.assertFalse(
                self.om._cas_update_user_location(
                    db, self.USER_ID, "room_a", "room_c"
                )
            )
            db.rollback()
            user = db.query(User).filter_by(USERID=self.USER_ID).first()
            self.assertEqual(user.CURRENT_BUILDINGID, "room_b")
        finally:
            db.close()

    def test_user_stale_from_is_refused(self):
        self.occupants["room_b"].append(str(self.USER_ID))
        ok, msg = self.om.move_entity(str(self.USER_ID), "user", "room_b", "room_c")
        self.assertFalse(ok)
        self.assertIn("現在地が変わっています", msg)
        db = self.SessionLocal()
        try:
            user = db.query(User).filter_by(USERID=self.USER_ID).first()
            self.assertEqual(user.CURRENT_BUILDINGID, "room_a")
        finally:
            db.close()
        self.assertEqual(self.manager.state.user_current_building_id, "room_a")

    # -- canonical sync (P1-1 残片) -------------------------------------

    def test_ai_move_syncs_persona_attribute_and_rituals(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)
        self.assertEqual(self.persona.current_building_id, "room_b")
        self.assertEqual(self.persona.mark_entry_calls, ["room_b"])
        self.assertEqual(self.persona.save_calls, 1)

    def test_user_move_syncs_manager_state(self):
        self.occupants["room_a"].append(str(self.USER_ID))
        ok, msg = self.om.move_entity(str(self.USER_ID), "user", "room_a", "room_b")
        self.assertTrue(ok, msg)
        self.assertEqual(self.manager.state.user_current_building_id, "room_b")

    def test_tx_failure_leaves_attributes_untouched(self):
        with patch(
            "database.building_messages.insert_building_message_in_session",
            side_effect=RuntimeError("boom"),
        ):
            ok, _msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertFalse(ok)
        self.assertEqual(self.persona.current_building_id, "room_a")
        self.assertEqual(self.persona.mark_entry_calls, [])
        self.assertEqual(self.persona.save_calls, 0)

    def test_ritual_failure_does_not_fail_the_move(self):
        """commit 済みの移動は儀式の失敗で False に転じない (WARN のみ)。"""
        def _boom(_bid):
            raise RuntimeError("mark boom")

        self.persona._mark_entry = _boom
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)
        self.assertEqual(self.persona.current_building_id, "room_b")
        self.assertEqual(self.persona.save_calls, 1)

    def test_insert_active_row_cas_loses_when_active_row_exists(self):
        """guarded INSERT (Codex 第二巡 P2): 既に active 行があれば入らない —
        index 不在でも「ゼロ件読み → 素の INSERT」の二重 presence を塞ぐ。"""
        db = self.SessionLocal()
        try:
            self.assertFalse(
                self.om._insert_active_row_cas(
                    db, self.MOVER, "room_b", datetime.now()
                )
            )
            db.rollback()
        finally:
            db.close()
        # active 行が無ければ入る (自己回復経路)
        db = self.SessionLocal()
        try:
            row = db.query(BuildingOccupancyLog).filter_by(
                AIID=self.MOVER, EXIT_TIMESTAMP=None
            ).first()
            row.EXIT_TIMESTAMP = datetime.now()
            db.commit()
            self.assertTrue(
                self.om._insert_active_row_cas(
                    db, self.MOVER, "room_b", datetime.now()
                )
            )
            db.commit()
        finally:
            db.close()
        self.assertEqual(self._active_rows(self.MOVER), [(self.MOVER, "room_b")])

    def test_canonical_location_published_before_delivery(self):
        """確定位置の公開は outbox 配送より前 (Codex 第二巡 P1) —
        配送ハンドラ実行時点で並行スレッドが見る state / 属性は既に新所在地。"""
        seen = {}

        def observing_handler(item):
            seen["persona_bid"] = self.persona.current_building_id
            seen["user_bid"] = self.manager.state.user_current_building_id

        for target in (
            "move.post_dynamic_state",
            "move.post_addon_hooks",
            "move.post_game_lifecycle",
        ):
            self.ledger.register_outbox_handler(target, observing_handler)

        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)
        self.assertEqual(seen.get("persona_bid"), "room_b")

        self.occupants["room_b"].append(str(self.USER_ID))
        ok, msg = self.om.move_entity(str(self.USER_ID), "user", "room_a", "room_c")
        # user の from は room_a (DB 正)。state は配送時点で room_c を映す
        self.assertTrue(ok, msg)
        self.assertEqual(seen.get("user_bid"), "room_c")

    # -- event_key (P2-1) ----------------------------------------------

    def test_event_keys_unique_for_same_second_round_trip(self):
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_b", "room_a")
        self.assertTrue(ok, msg)
        ok, msg = self.om.move_entity(self.MOVER, "ai", "room_a", "room_b")
        self.assertTrue(ok, msg)
        keys = self._event_keys()
        # 3 移動 × (leave + enter) = 6 イベント。leave/enter は同一移動 key を
        # 共有し、移動が違えば (同一秒・同一経路でも) key が違う
        self.assertEqual(len(keys), 6)
        self.assertEqual(len(set(keys)), 3)


class StartupOccupancyCheckerTest(unittest.TestCase):
    """_load_occupancy_from_db の分類 + 修復 (P2-2)。"""

    def setUp(self):
        self.engine = _make_engine()
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        db = self.SessionLocal()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
            db.flush()
            db.add(City(CITYID=1, USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001))
            db.add(AI(AIID="air", HOME_CITYID=1, AINAME="Air"))
            db.add(AI(AIID="quon", HOME_CITYID=1, AINAME="Quon"))
            db.commit()
        finally:
            db.close()

    def _add_row(self, ai_id, building_id, entry, exited=None):
        db = self.SessionLocal()
        try:
            db.add(BuildingOccupancyLog(
                CITYID=1, AIID=ai_id, BUILDINGID=building_id,
                ENTRY_TIMESTAMP=entry, EXIT_TIMESTAMP=exited,
            ))
            db.commit()
        finally:
            db.close()

    def _make_manager_stub(self, personas):
        from manager.persona import PersonaMixin

        stub = PersonaMixin.__new__(PersonaMixin)
        stub.SessionLocal = self.SessionLocal
        stub.city_id = 1
        stub.personas = personas
        stub.buildings = [
            SimpleNamespace(building_id="room_a"),
            SimpleNamespace(building_id="room_b"),
        ]
        stub.building_map = {"room_a": object(), "room_b": object()}
        stub.occupants = {}
        stub.capacities = {"room_a": 1, "room_b": 1}
        stub.startup_warnings = []
        stub.state = SimpleNamespace(user_id=None, occupants=None)
        return stub

    def test_duplicate_active_rows_are_repaired_at_startup(self):
        self._add_row("air", "room_a", datetime(2026, 7, 20, 10, 0, 0))
        self._add_row("air", "room_b", datetime(2026, 7, 21, 10, 0, 0))
        persona = SimpleNamespace(current_building_id=None, is_dispatched=False)
        stub = self._make_manager_stub({"air": persona})

        stub._load_occupancy_from_db()

        # canonical = 新しい方 (room_b) だけが採用され、二重 presence にならない
        self.assertEqual(stub.occupants["room_a"], [])
        self.assertEqual(stub.occupants["room_b"], ["air"])
        self.assertEqual(persona.current_building_id, "room_b")
        # DB も修復済み (active 1 行)
        db = self.SessionLocal()
        try:
            active = db.query(BuildingOccupancyLog).filter_by(
                AIID="air", EXIT_TIMESTAMP=None
            ).all()
            self.assertEqual([r.BUILDINGID for r in active], ["room_b"])
        finally:
            db.close()
        # 修復が監査記録に残る
        self.assertTrue(any(
            w["source"] == "occupancy_repair" for w in stub.startup_warnings
        ))

    def test_dispatched_persona_active_row_is_classified(self):
        self._add_row("air", "room_a", datetime(2026, 7, 21, 10, 0, 0))
        persona = SimpleNamespace(current_building_id=None, is_dispatched=True)
        stub = self._make_manager_stub({"air": persona})
        stub._load_occupancy_from_db()
        self.assertTrue(any(
            "Dispatched persona" in w["message"] for w in stub.startup_warnings
        ))

    def test_capacity_overflow_is_classified_without_eviction(self):
        self._add_row("air", "room_a", datetime(2026, 7, 21, 10, 0, 0))
        self._add_row("quon", "room_a", datetime(2026, 7, 21, 10, 1, 0))
        personas = {
            "air": SimpleNamespace(current_building_id=None, is_dispatched=False),
            "quon": SimpleNamespace(current_building_id=None, is_dispatched=False),
        }
        stub = self._make_manager_stub(personas)
        stub._load_occupancy_from_db()
        # 自動退去はしない (両者とも在室のまま) — 記録だけ残す
        self.assertEqual(sorted(stub.occupants["room_a"]), ["air", "quon"])
        self.assertTrue(any(
            "exceeds capacity" in w["message"] for w in stub.startup_warnings
        ))

    def test_invalid_active_row_is_closed_to_keep_persona_movable(self):
        """参照先不明の active 行は起動時に close (Codex 第四巡 P2) —
        放置すると CAS が常に stale 判定になり当該ペルソナが移動不能になる。"""
        self._add_row("air", "ghost_room", datetime(2026, 7, 21, 10, 0, 0))
        persona = SimpleNamespace(current_building_id=None, is_dispatched=False)
        stub = self._make_manager_stub({"air": persona})
        stub._load_occupancy_from_db()
        db = self.SessionLocal()
        try:
            active = db.query(BuildingOccupancyLog).filter_by(
                AIID="air", EXIT_TIMESTAMP=None
            ).count()
            self.assertEqual(active, 0)
        finally:
            db.close()
        self.assertTrue(any(
            w["source"] == "occupancy_repair" and "invalid active row" in w["message"]
            for w in stub.startup_warnings
        ))

    def test_unloaded_but_existing_persona_row_is_kept(self):
        """AI 行が実在するロード失敗 (一時的な不整合) は位置を壊さない —
        行は保全して警告のみ。"""
        self._add_row("quon", "room_a", datetime(2026, 7, 21, 10, 0, 0))
        stub = self._make_manager_stub({})  # quon はロードされていない
        stub._load_occupancy_from_db()
        db = self.SessionLocal()
        try:
            active = db.query(BuildingOccupancyLog).filter_by(
                AIID="quon", EXIT_TIMESTAMP=None
            ).count()
            self.assertEqual(active, 1)
        finally:
            db.close()
        self.assertTrue(any(
            "kept but not loaded" in w["message"] for w in stub.startup_warnings
        ))

    def test_prestart_repairs_are_carried_into_startup_warnings(self):
        """main.py の起動前修復 (ensure) の明細は checker が監査記録へ引き継ぐ
        (Codex 第四巡 P2 — 起動前に直された重複は後段では見えない)。"""
        from database.occupancy_repair import (
            consume_startup_repairs,
            record_startup_repairs,
        )
        consume_startup_repairs()  # 他テストの残留をクリア
        record_startup_repairs([{
            "ai_id": "air",
            "canonical_building_id": "room_b",
            "closed_rows": [(1, "room_a")],
        }])
        self._add_row("air", "room_b", datetime(2026, 7, 21, 10, 0, 0))
        persona = SimpleNamespace(current_building_id=None, is_dispatched=False)
        stub = self._make_manager_stub({"air": persona})
        stub._load_occupancy_from_db()
        self.assertTrue(any(
            w["source"] == "occupancy_repair" and "pre-start" in w["message"]
            for w in stub.startup_warnings
        ))
        # consume 済みなので二度目の起動では重複記録されない
        self.assertEqual(consume_startup_repairs(), [])

    def test_clean_state_produces_no_warnings(self):
        self._add_row("air", "room_a", datetime(2026, 7, 21, 10, 0, 0))
        self._add_row("quon", "room_b", datetime(2026, 7, 21, 10, 0, 0))
        personas = {
            "air": SimpleNamespace(current_building_id=None, is_dispatched=False),
            "quon": SimpleNamespace(current_building_id=None, is_dispatched=False),
        }
        stub = self._make_manager_stub(personas)
        stub._load_occupancy_from_db()
        self.assertEqual(stub.startup_warnings, [])
        self.assertEqual(stub.occupants["room_a"], ["air"])
        self.assertEqual(stub.occupants["room_b"], ["quon"])


class EnsureRegionEntranceUniqueTest(unittest.TestCase):
    """Region 入口所有の DB 側一意化 (Codex 第三巡)。"""

    def _make_db(self):
        import os
        import tempfile
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        return db_path, engine

    def test_index_created_and_blocks_shared_entrance(self):
        import os
        from database.migrate import ensure_region_entrance_unique

        db_path, engine = self._make_db()
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO region (REGION_ID, CITYID, NAME, DESCRIPTION, "
                    "REGION_TYPE, ENTRANCE_BUILDING_ID) "
                    "VALUES ('r1', 1, 'X', '', 'generic', 'bldg_a')"
                ))
            engine.dispose()
            ensure_region_entrance_unique(db_path)
            engine = create_engine(f"sqlite:///{db_path}")
            with engine.connect() as conn:
                idx = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='uq_region_entrance_building'"
                )).fetchone()
                self.assertIsNotNone(idx)
            # 共有入口の並行 create は 2 本目の commit が index で拒否される
            with self.assertRaises(Exception):
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO region (REGION_ID, CITYID, NAME, "
                        "DESCRIPTION, REGION_TYPE, ENTRANCE_BUILDING_ID) "
                        "VALUES ('r2', 1, 'Y', '', 'generic', 'bldg_a')"
                    ))
            engine.dispose()
        finally:
            os.unlink(db_path)

    def test_legacy_shared_entrance_warns_without_index(self):
        import os
        from database.migrate import ensure_region_entrance_unique

        db_path, engine = self._make_db()
        try:
            with engine.begin() as conn:
                for rid in ("r1", "r2"):
                    conn.execute(text(
                        "INSERT INTO region (REGION_ID, CITYID, NAME, "
                        "DESCRIPTION, REGION_TYPE, ENTRANCE_BUILDING_ID) "
                        f"VALUES ('{rid}', 1, 'X', '', 'generic', 'bldg_a')"
                    ))
            engine.dispose()
            ensure_region_entrance_unique(db_path)  # 自動修復せず WARN のみ
            engine = create_engine(f"sqlite:///{db_path}")
            with engine.connect() as conn:
                idx = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='uq_region_entrance_building'"
                )).fetchone()
                self.assertIsNone(idx)
                rows = conn.execute(text("SELECT COUNT(*) FROM region")).fetchone()
                self.assertEqual(rows[0], 2)  # データは触らない
            engine.dispose()
        finally:
            os.unlink(db_path)


class EnsureActiveOccupancyUniqueEntrypointTest(unittest.TestCase):
    """migrate.ensure_active_occupancy_unique — 修復 → index の起動時エントリ。"""

    def test_repairs_then_creates_index_on_file_db(self):
        import os
        import tempfile
        from database.migrate import ensure_active_occupancy_unique

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            SessionLocal = sessionmaker(bind=engine)
            db = SessionLocal()
            try:
                db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
                db.flush()
                db.add(City(CITYID=1, USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001))
                db.add(AI(AIID="air", HOME_CITYID=1, AINAME="Air"))
                db.add(BuildingOccupancyLog(
                    CITYID=1, AIID="air", BUILDINGID="room_a",
                    ENTRY_TIMESTAMP=datetime(2026, 7, 20, 10, 0, 0),
                ))
                db.add(BuildingOccupancyLog(
                    CITYID=1, AIID="air", BUILDINGID="room_b",
                    ENTRY_TIMESTAMP=datetime(2026, 7, 21, 10, 0, 0),
                ))
                db.commit()
            finally:
                db.close()
            engine.dispose()

            ensure_active_occupancy_unique(db_path)

            engine = create_engine(f"sqlite:///{db_path}")
            with engine.connect() as conn:
                active = conn.execute(text(
                    "SELECT BUILDINGID FROM building_occupancy_log "
                    "WHERE EXIT_TIMESTAMP IS NULL"
                )).fetchall()
                self.assertEqual([r[0] for r in active], ["room_b"])
                idx = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='uq_occupancy_active_ai'"
                )).fetchone()
                self.assertIsNotNone(idx)
            engine.dispose()
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
