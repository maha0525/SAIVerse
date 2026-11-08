"""ペルソナ削除の後始末と、記憶フォルダが残る ID を使わせない検査のテスト。

docs/issues/persona_delete_leaves_orphans_and_reuses_ids.md:

1. delete_ai (manager/persona.py と manager/admin.py の双子) が、現在の状態として
   読まれる行 (user_ai_link・予定・anchor など) を消し、過去の記録 (使用量・
   入退室の記録など) を残すこと。user_ai_link が残ると、同じ ID での作成が
   UNIQUE constraint で落ちる。
2. 削除しても記憶フォルダ (``<saiverse_home>/personas/<id>/``) は残るので、その ID
   を新しいペルソナに使わないこと (連番は避け、手入力・名前由来の ID は断る)。

PersonaCore とモデル設定解決は本題ではないのでスタブに差し替える。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.models import (
    AI as AIModel,
    AddonPersonaConfig,
    AiToolLink,
    Base,
    Blueprint,
    Building as BuildingModel,
    BuildingOccupancyLog,
    City as CityModel,
    FeedReadCursor,
    LineHeadSnapshot,
    LLMUsageLog,
    PersonaBuildingState,
    PersonaDayPlan,
    PersonaEventLog,
    PersonaPulseCursor,
    PersonaSchedule,
    PersonaTimetableTemplate,
    RealtimeSpellBinding,
    SessionAnchor,
    SessionHeadSnapshot,
    User,
    UserAiLink,
)
from manager.admin import AdminService
from manager.blueprints import BlueprintMixin
from manager.persona import (
    PERSONA_FOLDER_ERROR_CODE,
    PersonaMixin,
    ai_stem_taken,
    find_persona_folder,
    persona_folder_names,
)
from saiverse import model_configs
from saiverse.building_retirement import BuildingIdAvailability

_TEST_MODEL_DEFINITION = {"model": "vendor/test-model", "provider": "stub", "context_length": 1000}


class _StubPersonaCore:
    """PersonaCore の代わり。生成経路が触る属性だけ持つ (記憶フォルダは作らない)。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.private_room_id = None
        self.persona_role = None


def _make_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        db.add(CityModel(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
        # 利用者が一人だけなので、作成経路が user_ai_link を自動で張る
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.commit()
    finally:
        db.close()
    return db_path, engine, SessionLocal


def _fill_service(svc, SessionLocal, saiverse_home):
    svc.SessionLocal = SessionLocal
    svc.city_id = 1
    svc.city_name = "city_a"
    svc.model = "test-model"
    svc.model_parameter_overrides = {}
    svc.saiverse_home = saiverse_home
    svc.default_avatar = "avatar.png"
    svc.user_room_id = "user_room_city_a"
    svc.timezone_info = None
    svc.timezone_name = "UTC"
    svc.buildings = []
    svc.building_map = {}
    svc.capacities = {}
    svc.occupants = {}
    svc.building_memory_paths = {}
    svc.building_histories = {}
    svc.personas = {}
    svc.visiting_personas = {}
    svc.avatar_map = {}
    svc.id_to_name_map = {}
    svc.persona_map = {}
    svc.items = {}
    svc.items_by_persona = {}
    svc.get_persona_pending_events = lambda *a, **k: []
    svc.archive_persona_events = lambda *a, **k: None
    svc._on_persona_registered = lambda persona_id: None
    svc._is_seeded_entity = lambda entity_id: False
    return svc


class _ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path, self.engine, self.SessionLocal = _make_db()
        self.home = Path(tempfile.mkdtemp())
        self.svc = _fill_service(PersonaMixin.__new__(PersonaMixin), self.SessionLocal, self.home)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _create(self, name, **kwargs):
        with patch("manager.persona.PersonaCore", _StubPersonaCore), \
             patch.dict(model_configs.MODEL_CONFIGS, {"test-model": _TEST_MODEL_DEFINITION}):
            return self.svc._create_persona(name, "system prompt", **kwargs)

    def _make_memory_folder(self, folder_name):
        """PersonaCore が作る記憶フォルダの代わり (スタブは作らないので手で作る)。"""
        folder = self.home / "personas" / folder_name
        folder.mkdir(parents=True)
        (folder / "memory.db").write_bytes(b"")
        return folder

    def _count(self, model, **filters):
        db = self.SessionLocal()
        try:
            return db.query(model).filter_by(**filters).count()
        finally:
            db.close()


class DeleteCleanupTestCase(_ServiceTestBase):
    # purge_persona_state_rows が消す表の全部 (仕分け表の「消す」側)。user_ai_link は
    # 作成経路が自動で張るので、ここでは張らない
    PURGED_MODELS = [
        AiToolLink,
        PersonaSchedule,
        PersonaEventLog,
        AddonPersonaConfig,
        PersonaBuildingState,
        LineHeadSnapshot,
        SessionHeadSnapshot,
        SessionAnchor,
        PersonaPulseCursor,
        FeedReadCursor,
        RealtimeSpellBinding,
        PersonaDayPlan,
        PersonaTimetableTemplate,
    ]

    @staticmethod
    def _owner_filter(model, ai_id):
        """表ごとの「このペルソナの行」の条件 (purge_persona_state_rows と同じ列)。"""
        if model is AiToolLink:
            return {"AIID": ai_id}
        if model in (AddonPersonaConfig, PersonaDayPlan):
            return {"persona_id": ai_id}
        if model is RealtimeSpellBinding:
            return {"OWNER_KIND": "persona", "OWNER_ID": ai_id}
        return {"PERSONA_ID": ai_id}

    def _add_state_and_records(self, ai_id, room_id):
        now = datetime.now()
        db = self.SessionLocal()
        try:
            # 消す側 (現在の状態として読まれる行)。消す表の全部に一行ずつ張る
            db.add(AiToolLink(AIID=ai_id, TOOLID=1))
            db.add(PersonaSchedule(PERSONA_ID=ai_id, SCHEDULE_TYPE="oneshot", META_PLAYBOOK="meta_auto"))
            db.add(PersonaEventLog(PERSONA_ID=ai_id, CONTENT="pending event", STATUS="pending"))
            # pending / archived 以外の値の行も消す (残すのは archived だけ)
            db.add(PersonaEventLog(PERSONA_ID=ai_id, CONTENT="other status", STATUS="processing"))
            db.add(AddonPersonaConfig(addon_name="addon", persona_id=ai_id, params_json="{}"))
            db.add(PersonaBuildingState(PERSONA_ID=ai_id, BUILDING_ID=room_id))
            db.add(LineHeadSnapshot(
                PERSONA_ID=ai_id, LINE_ID="main", LINE_ROLE="main", MODEL_KEY="m",
                SECTIONS_JSON="{}", LAST_NOTIFIED_JSON="{}",
            ))
            db.add(SessionHeadSnapshot(
                PERSONA_ID=ai_id, MODEL_KEY="m", LINE_ROLE="main",
                SECTIONS_JSON="{}", LAST_NOTIFIED_JSON="{}",
            ))
            db.add(SessionAnchor(PERSONA_ID=ai_id, MODEL_KEY="m", UPDATED_AT=int(time.time())))
            db.add(PersonaPulseCursor(PERSONA_ID=ai_id, BUILDING_ID=room_id))
            db.add(FeedReadCursor(PERSONA_ID=ai_id, SUBSCRIPTION_ID="sub-1"))
            db.add(RealtimeSpellBinding(OWNER_KIND="persona", OWNER_ID=ai_id, SPELL_NAME="clock"))
            db.add(PersonaDayPlan(
                persona_id=ai_id, plan_date="2026-11-07", slots_json="[]",
                created_at=now, updated_at=now,
            ))
            db.add(PersonaTimetableTemplate(
                PERSONA_ID=ai_id, SLOTS_JSON="[]", CREATED_AT=now, UPDATED_AT=now,
            ))
            # 残す側 (過去の記録)
            db.add(PersonaEventLog(PERSONA_ID=ai_id, CONTENT="archived event", STATUS="archived"))
            db.add(LLMUsageLog(PERSONA_ID=ai_id, MODEL_ID="m", INPUT_TOKENS=1, OUTPUT_TOKENS=1))
            # 同じ ID を持つ部屋のリアルタイムスペルは消さない
            db.add(RealtimeSpellBinding(OWNER_KIND="building", OWNER_ID=ai_id, SPELL_NAME="clock"))
            # 別のペルソナの行は消さない
            db.add(PersonaSchedule(PERSONA_ID="other_city_a", SCHEDULE_TYPE="oneshot", META_PLAYBOOK="meta_auto"))
            db.commit()
        finally:
            db.close()
        # 張れたことを先に確かめる (張り損ねると「消えた」が素通しになる)
        for model in self.PURGED_MODELS:
            self.assertGreaterEqual(
                self._count(model, **self._owner_filter(model, ai_id)), 1, model.__tablename__,
            )

    def _assert_cleaned(self, ai_id, dropped=()):
        self.assertEqual(self._count(AIModel, AIID=ai_id), 0)
        self.assertEqual(self._count(UserAiLink, AIID=ai_id), 0)
        for model in self.PURGED_MODELS:
            if model.__tablename__ in dropped:
                continue
            remaining = self._count(model, **self._owner_filter(model, ai_id))
            if model is PersonaEventLog:
                # archived の一行だけ残る
                self.assertEqual(remaining, 1, model.__tablename__)
                self.assertEqual(self._count(PersonaEventLog, PERSONA_ID=ai_id, STATUS="archived"), 1)
            else:
                self.assertEqual(remaining, 0, model.__tablename__)

        self.assertEqual(self._count(LLMUsageLog, PERSONA_ID=ai_id), 1)
        self.assertEqual(self._count(RealtimeSpellBinding, OWNER_KIND="building", OWNER_ID=ai_id), 1)
        self.assertEqual(self._count(PersonaSchedule, PERSONA_ID="other_city_a"), 1)
        # 入退室の記録は残し、開いていた行を閉じる
        self.assertEqual(self._count(BuildingOccupancyLog, AIID=ai_id), 1)
        db = self.SessionLocal()
        try:
            log = db.query(BuildingOccupancyLog).filter_by(AIID=ai_id).one()
            self.assertIsNotNone(log.EXIT_TIMESTAMP)
        finally:
            db.close()

    def _run_delete(self, delete):
        ok, msg, _err, ai_id, room_id = self._create("Mocha", custom_ai_id="mocha")
        self.assertTrue(ok, msg)
        self.assertEqual(self._count(UserAiLink, AIID=ai_id), 1)
        self._add_state_and_records(ai_id, room_id)

        result = delete(ai_id)
        self.assertNotIn("Error", result)
        self._assert_cleaned(ai_id)
        return ai_id

    def test_persona_mixin_delete_ai_removes_state_rows_and_keeps_records(self):
        self._run_delete(self.svc.delete_ai)

    def test_admin_service_delete_ai_removes_state_rows_and_keeps_records(self):
        self._run_delete(lambda ai_id: AdminService.delete_ai(self.svc, ai_id))

    def _run_delete_with_dropped_table(self, delete):
        ok, msg, _err, ai_id, room_id = self._create("Mocha", custom_ai_id="mocha")
        self.assertTrue(ok, msg)
        self._add_state_and_records(ai_id, room_id)
        # legacy の表が DROP された後の DB: その表は飛ばし、他の表は消す
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE ai_tool_link"))

        result = delete(ai_id)
        self.assertNotIn("Error", result)
        self._assert_cleaned(ai_id, dropped={"ai_tool_link"})

    def test_persona_mixin_delete_ai_skips_a_table_missing_from_the_db(self):
        self._run_delete_with_dropped_table(self.svc.delete_ai)

    def test_admin_service_delete_ai_skips_a_table_missing_from_the_db(self):
        self._run_delete_with_dropped_table(lambda ai_id: AdminService.delete_ai(self.svc, ai_id))

    def test_same_id_can_be_created_again_when_no_memory_folder_remains(self):
        # 事象 1: user_ai_link が残ると、ここで UNIQUE constraint failed になっていた。
        # 記憶フォルダも無い (手で消した) 場合は、同じ ID で作り直せる
        ai_id = self._run_delete(self.svc.delete_ai)
        db = self.SessionLocal()
        try:
            # 私室は delete_ai の対象外なので、作り直しの前に手で消す
            db.query(BuildingModel).delete()
            db.commit()
        finally:
            db.close()
        ok, msg, _err, new_id, _room = self._create("Mocha", custom_ai_id="mocha")
        self.assertTrue(ok, msg)
        self.assertEqual(new_id, ai_id)
        self.assertEqual(self._count(UserAiLink, AIID=ai_id), 1)


class PersonaFolderReservationTestCase(_ServiceTestBase):
    def test_persona_folder_names_is_empty_without_the_personas_folder(self):
        self.assertEqual(persona_folder_names(self.home), [])
        self.assertIsNone(find_persona_folder(self.home, "persona_1_city_a"))

    def test_find_persona_folder_folds_case(self):
        self._make_memory_folder("Persona_4_city_a")
        self.assertEqual(find_persona_folder(self.home, "persona_4_city_a"), "Persona_4_city_a")
        self.assertEqual(persona_folder_names(self.home), ["Persona_4_city_a"])

    def test_ai_stem_taken_is_true_for_a_leftover_memory_folder(self):
        db = self.SessionLocal()
        try:
            building_ids = BuildingIdAvailability(db, self.home)
            self.assertFalse(ai_stem_taken(db, "persona_4", "city_a", building_ids, self.home))
            self._make_memory_folder("persona_4_city_a")
            # DB に行が無くても、記憶フォルダが残っていれば埋まっている
            self.assertTrue(ai_stem_taken(db, "persona_4", "city_a", building_ids, self.home))
            self.assertTrue(ai_stem_taken(db, "PERSONA_4", "city_a", building_ids, self.home))
        finally:
            db.close()

    def test_serial_id_skips_a_leftover_memory_folder(self):
        self._make_memory_folder("persona_1_city_a")
        ok, msg, _err, ai_id, room_id = self._create("エア")
        self.assertTrue(ok, msg)
        self.assertEqual(ai_id, "persona_2_city_a")
        self.assertEqual(room_id, "persona_2_city_a_room")

    def test_custom_id_with_a_leftover_memory_folder_is_refused(self):
        self._make_memory_folder("Mocha_city_a")
        ok, msg, error, ai_id, room_id = self._create("Mocha", custom_ai_id="mocha")
        self.assertFalse(ok)
        self.assertIsNone(ai_id)
        self.assertIsNone(room_id)
        self.assertIn("memory folder still exists", msg)
        # 画面が文言表の文で出せるよう、識別子とパラメータも返す
        self.assertEqual(error.code, PERSONA_FOLDER_ERROR_CODE)
        self.assertEqual(error.params, {"ai_id": "mocha_city_a", "folder": "Mocha_city_a"})
        self.assertIn("~/.saiverse/personas/Mocha_city_a", msg)
        self.assertIn("persona pack", msg)
        self.assertEqual(self._count(AIModel), 0)
        self.assertEqual(self.svc.building_map, {})

    def test_name_derived_id_with_a_leftover_memory_folder_is_refused(self):
        self._make_memory_folder("sophie_city_a")
        ok, msg, _err, _ai, _room = self._create("Sophie")
        self.assertFalse(ok)
        self.assertIn("memory folder still exists", msg)

    def test_delete_then_recreate_with_the_same_id_is_refused_while_the_folder_remains(self):
        ok, msg, _err, ai_id, _room = self._create("Mocha", custom_ai_id="mocha")
        self.assertTrue(ok, msg)
        self._make_memory_folder(ai_id)
        self.assertNotIn("Error", self.svc.delete_ai(ai_id))

        ok2, msg2, _err, _ai2, _room2 = self._create("Mocha", custom_ai_id="mocha")
        self.assertFalse(ok2)
        self.assertIn("memory folder still exists", msg2)


class BlueprintFolderReservationTestCase(unittest.TestCase):
    def setUp(self):
        self.db_path, self.engine, self.SessionLocal = _make_db()
        db = self.SessionLocal()
        try:
            db.add(BuildingModel(CITYID=1, BUILDINGID="plaza_city_a", BUILDINGNAME="広場", CAPACITY=5))
            db.add(Blueprint(BLUEPRINT_ID=1, CITYID=1, NAME="homunculus", BASE_SYSTEM_PROMPT="base prompt"))
            db.commit()
        finally:
            db.close()
        self.home = Path(tempfile.mkdtemp())
        svc = _fill_service(BlueprintMixin.__new__(BlueprintMixin), self.SessionLocal, self.home)
        plaza = type("_B", (), {"name": "広場", "capacity": 5})()
        svc.buildings = [plaza]
        svc.building_map = {"plaza_city_a": plaza}
        svc.capacities = {"plaza_city_a": 5}
        svc.occupants = {"plaza_city_a": []}
        svc.building_histories = {"plaza_city_a": []}
        svc.add_building_event = lambda *a, **k: None
        svc._save_modified_buildings = lambda *a, **k: None
        self.svc = svc

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _spawn(self, entity_name):
        with patch("manager.blueprints.PersonaCore", _StubPersonaCore), \
             patch.dict(model_configs.MODEL_CONFIGS, {"test-model": _TEST_MODEL_DEFINITION}):
            return self.svc.spawn_entity_from_blueprint(1, entity_name, "plaza_city_a")

    def test_name_derived_id_with_a_leftover_memory_folder_is_refused(self):
        (self.home / "personas" / "homu_city_a").mkdir(parents=True)
        ok, msg = self._spawn("Homu")
        self.assertFalse(ok)
        self.assertIn("memory folder still exists", msg)

    def test_serial_id_skips_a_leftover_memory_folder(self):
        (self.home / "personas" / "persona_1_city_a").mkdir(parents=True)
        ok, msg = self._spawn("ほむ")
        self.assertTrue(ok, msg)
        db = self.SessionLocal()
        try:
            ids = {row.AIID for row in db.query(AIModel).all()}
        finally:
            db.close()
        self.assertEqual(ids, {"persona_2_city_a"})


class ReservedIdsRouteTestCase(unittest.TestCase):
    def test_route_returns_the_persona_folder_names(self):
        from api.routes.world import get_reserved_ai_ids

        home = Path(tempfile.mkdtemp())
        manager = type("_M", (), {"saiverse_home": home})()
        self.assertEqual(get_reserved_ai_ids(manager), {"persona_folder_ids": []})
        (home / "personas" / "persona_4_city_a").mkdir(parents=True)
        (home / "personas" / "not_a_folder.txt").write_text("x", encoding="utf-8")
        self.assertEqual(
            get_reserved_ai_ids(manager), {"persona_folder_ids": ["persona_4_city_a"]},
        )


class CreateRouteErrorCodeTestCase(unittest.TestCase):
    """POST /api/world/ais の失敗応答: 記憶フォルダの断りだけ識別子とパラメータを載せる。"""

    def _call(self, result):
        from api.routes.world import AICreate, create_ai

        manager = type("_M", (), {"create_ai": lambda self, *a, **k: result})()
        return create_ai(AICreate(name="Mocha", system_prompt="p", home_city_id=1, ai_id="mocha"), manager)

    def test_memory_folder_refusal_carries_the_code_and_params(self):
        import json

        from manager.persona import persona_folder_error, persona_folder_error_detail

        msg = persona_folder_error("mocha_city_a", "Mocha_city_a")
        response = self._call(
            (False, msg, persona_folder_error_detail("mocha_city_a", "Mocha_city_a"), None, None),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {
            "detail": msg,
            "error_code": PERSONA_FOLDER_ERROR_CODE,
            "error_params": {"ai_id": "mocha_city_a", "folder": "Mocha_city_a"},
        })

    def test_other_refusals_keep_the_plain_detail(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            self._call((False, "A persona named 'Mocha' already exists in this city.", None, None, None))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "A persona named 'Mocha' already exists in this city.")

    def test_success_is_unchanged(self):
        self.assertEqual(
            self._call((True, "created", None, "mocha_city_a", "mocha_city_a_room")),
            {"message": "created", "ai_id": "mocha_city_a", "room_id": "mocha_city_a_room"},
        )


if __name__ == "__main__":
    unittest.main()
