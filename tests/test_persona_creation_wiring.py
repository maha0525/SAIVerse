"""ペルソナ生成経路 (_create_persona) の配線契約のテスト。

このリポジトリには長らく `_create_persona` を実際に呼ぶテストが無く、
2026-08-09 の Building ID 工事では**同じ領域に二度退行を入れた**:

1. 私室 ID を slug 化した結果、ID が「重複検査済みの AIID の純粋関数」でなくなり、
   検査を通った別ペルソナ (「A店」と「A森」) が同じ Building ID に落ちるように
   なった
2. その衝突が commit で落ちても、commit より前に済ませていたインメモリ登録が
   残り、既存ペルソナの部屋と占有者を上書きしたままになった

どちらも `manager/ids.py` の単体テストでは捕まらない — 呼び出し側が
``ensure_unique`` を外す・引数を取り違える・commit 順序を戻す実装でも通って
しまうため。ここでは**呼び出し側の配線**を押さえる。

PersonaCore とモデル設定解決は本題ではないのでスタブに差し替える (本物を
起動すると SAIMemory・埋め込みモデル・プロンプトファイルまで巻き込む)。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import (
    AI as AIModel,
    Base,
    Blueprint,
    Building as BuildingModel,
    City as CityModel,
)
from manager.blueprints import BlueprintMixin
from manager.persona import PersonaMixin


class _StubPersonaCore:
    """PersonaCore の代わり。生成経路が触る属性だけ持つ。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.private_room_id = None
        self.persona_role = None


class AdminServiceHookWiringTestCase(unittest.TestCase):
    """AdminService が生成経路の必要フックを実際に持っているか。

    `_create_persona` は commit の後で `self._on_persona_registered` を呼ぶが、
    このフックの実体を持つのは `SAIVerseManager` だけ。UI からのペルソナ作成は
    `SAIVerseManager.create_ai` → `AdminService.create_ai` → `_create_persona`
    と流れるので、`AdminService` に委譲が無いと **DB には作られたのに
    AttributeError で失敗が返る**。ミックスイン越しに `self.` で呼ぶフックは、
    どの土台に載っても解決できることを別途押さえないと落ちる。
    """

    def test_admin_service_delegates_the_persona_registration_hook(self):
        from unittest.mock import MagicMock

        from manager.admin import AdminService

        manager = MagicMock()
        svc = AdminService(manager, MagicMock(), MagicMock())
        svc._on_persona_registered("persona_1")
        manager._on_persona_registered.assert_called_once_with("persona_1")


class PersonaCreationWiringTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            db.add(CityModel(
                CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000,
            ))
            db.commit()
        finally:
            db.close()

        svc = PersonaMixin.__new__(PersonaMixin)
        svc.SessionLocal = self.SessionLocal
        svc.city_id = 1
        svc.city_name = "city_a"
        svc.model = "test-model"
        svc._base_model = "test-model"
        svc.saiverse_home = Path(tempfile.mkdtemp())
        svc.default_avatar = "avatar.png"
        svc.user_room_id = "user_room_city_a"
        svc.timezone_info = None
        svc.timezone_name = "UTC"
        # 世界状態のキャッシュ (契約の検証対象)
        svc.buildings = []
        svc.building_map = {}
        svc.capacities = {}
        svc.occupants = {}
        svc.building_memory_paths = {}
        svc.building_histories = {}
        svc.personas = {}
        svc.avatar_map = {}
        svc.id_to_name_map = {}
        svc.persona_map = {}
        svc.items = {}
        svc.items_by_persona = {}
        svc.get_persona_pending_events = lambda *a, **k: []
        svc.archive_persona_events = lambda *a, **k: None
        svc._on_persona_registered = lambda persona_id: None
        self.svc = svc

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _create(self, name, **kwargs):
        with patch("manager.persona.PersonaCore", _StubPersonaCore), \
             patch("manager.persona.get_model_provider", return_value="stub"), \
             patch("manager.persona.get_context_length", return_value=1000):
            return self.svc._create_persona(name, "system prompt", **kwargs)

    def _room_id_in_db(self, ai_id):
        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter_by(AIID=ai_id).first()
            return ai.PRIVATE_ROOM_ID if ai else None
        finally:
            db.close()

    def _building_ids(self):
        db = self.SessionLocal()
        try:
            return {b.BUILDINGID for b in db.query(BuildingModel).all()}
        finally:
            db.close()

    # --- 私室 ID の文字種契約 (docs/issues/building_id_no_charset_constraint.md) ---

    def test_ascii_name_keeps_the_readable_room_id(self):
        ok, _msg, ai_id, room_id = self._create("Sophie")
        self.assertTrue(ok)
        self.assertEqual(room_id, "sophie_city_a_room")
        self.assertEqual(self._room_id_in_db(ai_id), "sophie_city_a_room")

    def test_japanese_name_gets_a_serial_ascii_ai_id_and_matching_room(self):
        # 日本語名は slug が残らないので AIID ごと persona_<連番> へ落ちる
        # (issue 論点 3)。AIID と私室 ID は同じ連番を共有する
        ok, _msg, ai_id, room_id = self._create("エア")
        self.assertTrue(ok)
        self.assertEqual(ai_id, "persona_1_city_a")
        self.assertEqual(room_id, "persona_1_city_a_room")
        self.assertEqual(self._room_id_in_db(ai_id), room_id)

        ok2, _msg2, ai_id2, room_id2 = self._create("ミク")
        self.assertTrue(ok2)
        self.assertEqual(ai_id2, "persona_2_city_a")
        self.assertEqual(room_id2, "persona_2_city_a_room")

    def test_serial_ai_id_skips_rooms_taken_by_pre_contract_personas(self):
        # 契約より前に作られた「AIID は日本語のまま・私室だけ persona_N」という
        # ペルソナが本番にいる。AIID の空きだけ見て連番を選ぶと、新しい子の
        # AIID (persona_1) と私室 (persona_2_..._room) の番号が食い違う —
        # 連番を選ぶ側が私室の空きも一緒に予約することを押さえる
        db = self.SessionLocal()
        try:
            db.add(AIModel(
                AIID="テラ_city_a", HOME_CITYID=1, AINAME="テラ",
                SYSTEMPROMPT="p", PRIVATE_ROOM_ID="persona_1_city_a_room",
            ))
            db.add(BuildingModel(
                CITYID=1, BUILDINGID="persona_1_city_a_room",
                BUILDINGNAME="テラの部屋", CAPACITY=1,
            ))
            db.commit()
        finally:
            db.close()

        ok, _msg, ai_id, room_id = self._create("ミク")
        self.assertTrue(ok)
        self.assertEqual(ai_id, "persona_2_city_a")
        self.assertEqual(room_id, "persona_2_city_a_room")

    def test_names_that_normalize_alike_fail_loudly_not_silently(self):
        # slug 化は情報を落とす写像 — 「A店」と「A森」はどちらも a になる。
        # 連番で黙って避けると名前と違う ID が無言で生まれるので、Building と
        # 同じ裁定で「already exists」を音を立てて返す。世界状態は動かない
        ok1, _m1, ai1, _room1 = self._create("A店")
        self.assertTrue(ok1)
        self.assertEqual(ai1, "a_city_a")

        cache_before = dict(self.svc.building_map)
        ok2, msg2, _id2, _room2 = self._create("A森")
        self.assertFalse(ok2)
        self.assertIn("already exists", msg2)
        self.assertEqual(dict(self.svc.building_map), cache_before)

    def test_case_folded_duplicate_ai_id_is_rejected(self):
        # AIID はフォルダ名になり、Windows のファイルシステムは大文字小文字を
        # 区別しない。'alice' と 'Alice' を別ペルソナとして通すと DB 上は別行
        # なのに記憶 DB・ログの保存先が同じになるため、重複検査は大文字小文字を
        # 畳む
        ok1, _m1, _ai1, _room1 = self._create("X", custom_ai_id="alice")
        self.assertTrue(ok1)

        cache_before = dict(self.svc.building_map)
        ok2, msg2, _ai2, _room2 = self._create("Y", custom_ai_id="Alice")
        self.assertFalse(ok2)
        self.assertIn("already exists", msg2)
        self.assertEqual(dict(self.svc.building_map), cache_before)

        # 名前由来の自動生成でも同じ畳み込みが効く (名前 'ALICE' → slug 'alice')
        ok3, msg3, _ai3, _room3 = self._create("ALICE")
        self.assertFalse(ok3)
        self.assertIn("already exists", msg3)

    def test_second_persona_does_not_overwrite_the_first_room_in_cache(self):
        # 退行の本体だった形: 私室 ID が衝突したままインメモリ登録が走ると、
        # 既存の部屋と占有者が上書きされる。AIID の重複検査が大文字小文字を
        # 畳む今、残る衝突源は「私室と同じ ID の Building が既に存在する」場合
        # (ユーザーが手で作った Building や契約前の遺産)
        db = self.SessionLocal()
        try:
            db.add(BuildingModel(
                CITYID=1, BUILDINGID="alice_city_a_room",
                BUILDINGNAME="手作りの部屋", CAPACITY=1,
            ))
            db.commit()
        finally:
            db.close()

        ok, _msg, ai_id, room_id = self._create("X", custom_ai_id="alice")
        self.assertTrue(ok, f"作成が失敗した: {_msg}")
        # 既存 Building の ID を奪わず、連番の私室へ落ちる
        self.assertNotEqual(room_id, "alice_city_a_room")
        self.assertEqual(self.svc.occupants[room_id], [ai_id])
        # 既存 Building はインメモリ登録の対象外のまま (上書きされていない)
        self.assertNotIn("alice_city_a_room", self.svc.building_map)

    def test_custom_ai_id_outside_the_charset_contract_is_rejected(self):
        # custom ID は契約 (ASCII 英数字 + '_' '-'、先頭は英数字) を満たさなければ
        # 拒否。日本語もパス脱出文字もここで一緒に落ちる
        for bad in ("日本語ID", "識別子", "../../outside", "a/b", "a\\b",
                    "a:b", "a|b", "..", "_leading", "-leading"):
            ok, msg, _ai, _room = self._create("X", custom_ai_id=bad)
            self.assertFalse(ok, f"{bad!r} が通ってしまった")
            self.assertIn("Error", msg)
            self.assertEqual(self.svc.building_map, {}, f"{bad!r} で世界状態が動いた")

    def test_custom_ai_id_with_hyphen_is_allowed(self):
        ok, _msg, ai_id, room_id = self._create("X", custom_ai_id="neo-alice")
        self.assertTrue(ok)
        self.assertEqual(ai_id, "neo-alice_city_a")
        self.assertEqual(room_id, "neo-alice_city_a_room")

    def test_path_characters_in_names_are_dropped_by_the_slug(self):
        # 自動生成側は slug 化が契約外の文字 (区切り・'..' を含む) を捨てるので、
        # 名前にパス文字が混ざっても安全な ID に落ちる
        ok, _msg, ai_id, _room = self._create("../escape")
        self.assertTrue(ok)
        self.assertEqual(ai_id, "escape_city_a")

    # --- DB とキャッシュの整合 ---

    def test_cache_matches_db_after_success(self):
        _ok, _msg, ai_id, room_id = self._create("Sophie")
        self.assertIn(room_id, self._building_ids())
        self.assertIn(room_id, self.svc.building_map)
        self.assertIn(ai_id, self.svc.personas)
        self.assertEqual(self.svc.personas[ai_id].private_room_id, room_id)

    def test_commit_failure_leaves_the_world_state_untouched(self):
        """DB が確定する前にキャッシュを触らない、という順序の契約。

        逆順だと、失敗した作成が rollback 後も building_map / occupants に残り、
        既存ペルソナの部屋と占有者を上書きしたままになる (DB は巻き戻るのに
        キャッシュは巻き戻らない)。
        """
        _ok, _msg, first_ai, first_room = self._create("Sophie")
        before = (
            dict(self.svc.building_map),
            dict(self.svc.occupants),
            len(self.svc.buildings),
            set(self.svc.personas),
        )

        real_factory = self.svc.SessionLocal

        def failing_factory():
            session = real_factory()
            def boom():
                raise RuntimeError("commit failed (injected)")
            session.commit = boom
            return session

        self.svc.SessionLocal = failing_factory
        try:
            ok, _msg2, _ai2, _room2 = self._create("Aria")
        finally:
            self.svc.SessionLocal = real_factory

        self.assertFalse(ok)
        self.assertEqual(dict(self.svc.building_map), before[0])
        self.assertEqual(dict(self.svc.occupants), before[1])
        self.assertEqual(len(self.svc.buildings), before[2])
        self.assertEqual(set(self.svc.personas), before[3])
        # 既存ペルソナの部屋は無傷
        self.assertEqual(self.svc.occupants[first_room], [first_ai])

    def test_duplicate_name_is_rejected_without_touching_the_cache(self):
        self._create("Sophie")
        buildings_before = list(self.svc.building_map)
        ok, msg, _ai_id, _room_id = self._create("sophie")
        self.assertFalse(ok)
        self.assertIn("already exists", msg)
        self.assertEqual(list(self.svc.building_map), buildings_before)


class BlueprintSpawnWiringTestCase(unittest.TestCase):
    """ブループリント孵化 (spawn_entity_from_blueprint) の配線契約。

    AIID を作るもう一つの口。8/9 の Building ID 工事ではこの経路が漏れて
    日本語 ID を作り続けた前科があるため、契約 (日本語名 → persona_連番、
    私室と同じ連番を共有) を呼び出し側で押さえる。
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            db.add(CityModel(
                CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000,
            ))
            db.add(BuildingModel(
                CITYID=1, BUILDINGID="plaza_city_a", BUILDINGNAME="広場", CAPACITY=5,
            ))
            db.add(Blueprint(
                BLUEPRINT_ID=1, CITYID=1, NAME="homunculus",
                BASE_SYSTEM_PROMPT="base prompt",
            ))
            db.commit()
        finally:
            db.close()

        svc = BlueprintMixin.__new__(BlueprintMixin)
        svc.SessionLocal = self.SessionLocal
        svc.city_id = 1
        svc.city_name = "city_a"
        svc.model = "test-model"
        svc.saiverse_home = Path(tempfile.mkdtemp())
        svc.default_avatar = "avatar.png"
        svc.user_room_id = "user_room_city_a"
        svc.timezone_info = None
        svc.timezone_name = "UTC"
        plaza = type("_B", (), {"name": "広場", "capacity": 5})()
        svc.buildings = [plaza]
        svc.building_map = {"plaza_city_a": plaza}
        svc.capacities = {"plaza_city_a": 5}
        svc.occupants = {"plaza_city_a": []}
        svc.building_memory_paths = {}
        svc.building_histories = {"plaza_city_a": []}
        svc.personas = {}
        svc.avatar_map = {}
        svc.id_to_name_map = {}
        svc.persona_map = {}
        svc.items = {}
        svc.items_by_persona = {}
        svc.get_persona_pending_events = lambda *a, **k: []
        svc.archive_persona_events = lambda *a, **k: None
        svc._on_persona_registered = lambda persona_id: None
        svc.add_building_event = lambda *a, **k: None
        svc._save_modified_buildings = lambda *a, **k: None
        self.svc = svc

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _spawn(self, entity_name):
        with patch("manager.blueprints.PersonaCore", _StubPersonaCore), \
             patch("manager.blueprints.get_model_provider", return_value="stub"), \
             patch("manager.blueprints.get_context_length", return_value=1000):
            return self.svc.spawn_entity_from_blueprint(1, entity_name, "plaza_city_a")

    def _ai_row(self, ai_id):
        db = self.SessionLocal()
        try:
            return db.query(AIModel).filter_by(AIID=ai_id).first()
        finally:
            db.close()

    def test_japanese_entity_name_gets_a_serial_ascii_ai_id_and_matching_room(self):
        ok, msg = self._spawn("ホムンクルス")
        self.assertTrue(ok, msg)
        row = self._ai_row("persona_1_city_a")
        self.assertIsNotNone(row, "AIID が persona_1_city_a になっていない")
        self.assertEqual(row.PRIVATE_ROOM_ID, "persona_1_city_a_room")

    def test_ascii_entity_name_keeps_the_readable_ai_id(self):
        ok, msg = self._spawn("Golem")
        self.assertTrue(ok, msg)
        row = self._ai_row("golem_city_a")
        self.assertIsNotNone(row)
        self.assertEqual(row.PRIVATE_ROOM_ID, "golem_city_a_room")

    def test_serial_skips_rooms_taken_by_pre_contract_personas(self):
        # 契約前の「AIID は日本語・私室だけ persona_N」の既存個体がいても、
        # 新しい個体は AIID と私室が同じ連番になる (manager/persona.py と同じ予約)
        db = self.SessionLocal()
        try:
            db.add(AIModel(
                AIID="テラ_city_a", HOME_CITYID=1, AINAME="テラ",
                SYSTEMPROMPT="p", PRIVATE_ROOM_ID="persona_1_city_a_room",
            ))
            db.add(BuildingModel(
                CITYID=1, BUILDINGID="persona_1_city_a_room",
                BUILDINGNAME="テラの部屋", CAPACITY=1,
            ))
            db.commit()
        finally:
            db.close()

        ok, msg = self._spawn("ホムンクルス")
        self.assertTrue(ok, msg)
        row = self._ai_row("persona_2_city_a")
        self.assertIsNotNone(row, "既存私室の番号を飛ばした連番になっていない")
        self.assertEqual(row.PRIVATE_ROOM_ID, "persona_2_city_a_room")


if __name__ == "__main__":
    unittest.main()
