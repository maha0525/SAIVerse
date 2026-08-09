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
    Building as BuildingModel,
    City as CityModel,
)
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
                CITYID=1, USERID=1, CITYNAME="city_a", UI_PORT=3000, API_PORT=8000,
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

    def test_japanese_name_gets_an_ascii_room_id(self):
        ok, _msg, ai_id, room_id = self._create("エア")
        self.assertTrue(ok)
        # AIID 側の文字種は issue 論点 3 で未着手なので日本語のまま。
        # 私室 ID (パス・URI に入る) だけが ASCII に落ちる
        self.assertNotIn("エア", room_id)
        self.assertEqual(room_id, "persona_1_city_a_room")
        self.assertEqual(self._room_id_in_db(ai_id), room_id)

    def test_names_that_normalize_alike_get_distinct_room_ids(self):
        # slug 化は情報を落とす写像 — 「A店」と「A森」はどちらも a になる。
        # AIID と名前の重複検査は別の文字列を見るのでここは素通しする
        ok1, _m1, _id1, room1 = self._create("A店")
        ok2, _m2, _id2, room2 = self._create("A森")
        self.assertTrue(ok1)
        self.assertTrue(ok2, f"2 人目の作成が失敗した: {_m2}")
        self.assertNotEqual(room1, room2)
        self.assertEqual(len(self._building_ids()), 2)

    def test_second_persona_does_not_overwrite_the_first_room_in_cache(self):
        # 退行の本体: 衝突した ID でインメモリ登録が走ると、既存ペルソナの
        # 部屋と占有者が上書きされる
        _ok1, _m1, ai1, room1 = self._create("A店")
        _ok2, _m2, ai2, room2 = self._create("A森")

        self.assertEqual(self.svc.occupants[room1], [ai1])
        self.assertEqual(self.svc.occupants[room2], [ai2])
        self.assertEqual(len(self.svc.buildings), 2)
        self.assertEqual(len(self.svc.building_map), 2)
        # ログの置き場も部屋ごとに分かれている (同じパスを共有しない)
        self.assertNotEqual(
            self.svc.building_memory_paths[room1],
            self.svc.building_memory_paths[room2],
        )

    def test_custom_ai_id_with_non_ascii_still_yields_an_ascii_room_id(self):
        # AIID は無検証なので日本語が通る (issue 論点 3)。私室 ID だけは
        # 混在名でも ASCII 部分だけを残し、残らなければ連番へ落ちる
        ok, _msg, ai_id, room_id = self._create("X", custom_ai_id="日本語ID")
        self.assertTrue(ok)
        self.assertEqual(ai_id, "日本語ID_city_a")
        self.assertEqual(room_id, "id_city_a_room")

        ok2, _msg2, ai_id2, room_id2 = self._create("Y", custom_ai_id="識別子")
        self.assertTrue(ok2)
        self.assertEqual(ai_id2, "識別子_city_a")
        self.assertEqual(room_id2, "persona_1_city_a_room")

    # --- AIID はフォルダ名になる (パス境界) ---

    def test_ai_id_that_escapes_the_persona_directory_is_rejected(self):
        # AIID は ~/.saiverse/personas/<id>/ のフォルダ名になるため、区切り文字が
        # 通ると保存先が SAIVERSE_HOME の外へ出る。
        # なお custom_ai_id が ".." 単体でも AIID は ".._city_a" になり、脱出でき
        # ない普通のフォルダ名なので通る (検査は AIID 全体に対して行う)
        for bad in ("../../outside", "a/b", "a\\b", "a:b", "a|b"):
            ok, msg, _ai, _room = self._create("X", custom_ai_id=bad)
            self.assertFalse(ok, f"{bad!r} が通ってしまった")
            self.assertIn("Error", msg)
            self.assertEqual(self.svc.building_map, {}, f"{bad!r} で世界状態が動いた")

    def test_name_derived_ai_id_is_also_checked(self):
        # 自動生成側 (custom_ai_id なし) も同じ境界を通る
        ok, msg, _ai, _room = self._create("../escape")
        self.assertFalse(ok)
        self.assertIn("Error", msg)

    def test_japanese_ai_id_is_still_allowed(self):
        # 文字種を ASCII へ統一するかは未決 (issue 論点 3)。ここで塞ぐのは
        # パス境界だけで、日本語 ID は従来どおり通す
        ok, _msg, ai_id, _room = self._create("エア")
        self.assertTrue(ok)
        self.assertEqual(ai_id, "エア_city_a")

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


if __name__ == "__main__":
    unittest.main()
