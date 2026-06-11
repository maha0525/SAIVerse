"""Ruler 専用世界編集ツール (game_create_subregion / game_create_building /
game_set_scene / game_move_party) のテスト。

tools.context.persona_context で fake manager を注入して実行する。
"""
import unittest

from builtin_data.tools.game_create_building import game_create_building
from builtin_data.tools.game_create_subregion import game_create_subregion
from builtin_data.tools.game_move_party import game_move_party
from builtin_data.tools.game_set_scene import game_set_scene
from builtin_data.tools._game_common import extract_id_from_result
from tools.context import persona_context


class FakeRegion:
    def __init__(self, region_id, ruler_id="ruler1", parent_region_id=None,
                 is_game_region=True, name="霧の谷"):
        self.region_id = region_id
        self.ruler_id = ruler_id
        self.parent_region_id = parent_region_id
        self.is_game_region = is_game_region
        self.name = name


class FakeLifecycle:
    def __init__(self):
        self.scene_calls = []
        self.move_party_calls = []

    def set_scene(self, region_id, scene):
        self.scene_calls.append((region_id, scene))
        return f"Scene changed to '{scene}'."

    def move_party(self, region_id, building_id):
        self.move_party_calls.append((region_id, building_id))
        return f"Party moved to '{building_id}'."


class FakeManager:
    def __init__(self, regions):
        self.regions = regions
        self.city_id = 1
        self.game_lifecycle = FakeLifecycle()
        self.create_region_calls = []
        self.create_building_calls = []
        self.set_building_region_calls = []

    def get_region(self, region_id):
        return self.regions.get(region_id)

    def create_region(self, name, description, region_type, city_id,
                      parent_region_id=None, region_id=None):
        self.create_region_calls.append((name, region_type, parent_region_id))
        return f"Region '{name}' (ID: sub_{name}) created successfully."

    def create_building(self, name, description, capacity, system_instruction, city_id):
        self.create_building_calls.append((name, capacity))
        return f"Building '{name}' (ID: bldg_{name}) created successfully. A restart is required for it to be usable."

    def set_building_region(self, building_id, region_id):
        self.set_building_region_calls.append((building_id, region_id))
        return f"Building '{building_id}' region set to {region_id}."


class GameWorldToolsTestCase(unittest.TestCase):
    def setUp(self):
        self.region = FakeRegion("r1")
        self.sub = FakeRegion("sub1", parent_region_id="r1", name="霧降りの森")
        self.other = FakeRegion("r2", ruler_id="other_ruler", name="別世界")
        self.manager = FakeManager({"r1": self.region, "sub1": self.sub, "r2": self.other})

    def _run(self, func, *args, persona_id="ruler1", **kwargs):
        with persona_context(persona_id, "/tmp", manager=self.manager):
            return func(*args, **kwargs)

    # --- 共通ゲート ---

    def test_non_ruler_rejected(self):
        result = self._run(game_create_subregion, "X", "d", persona_id="air")
        self.assertIn("エラー", result)
        self.assertEqual(self.manager.create_region_calls, [])

    # --- game_create_subregion ---

    def test_create_subregion(self):
        result = self._run(game_create_subregion, "沼の街", "瘴気漂う街")
        self.assertNotIn("エラー", result)
        self.assertIn("sub_沼の街", result)
        self.assertEqual(self.manager.create_region_calls, [("沼の街", "game", "r1")])

    # --- game_create_building ---

    def test_create_building_under_region(self):
        result = self._run(game_create_building, "酒場", "賑やかな店")
        self.assertNotIn("エラー", result)
        self.assertEqual(self.manager.set_building_region_calls, [("bldg_酒場", "r1")])

    def test_create_building_under_subregion(self):
        result = self._run(game_create_building, "宿屋", "静かな宿", subregion_id="sub1")
        self.assertNotIn("エラー", result)
        self.assertEqual(self.manager.set_building_region_calls, [("bldg_宿屋", "sub1")])

    def test_create_building_rejects_foreign_subregion(self):
        self.sub.parent_region_id = "r2"  # 他人の Region 配下
        result = self._run(game_create_building, "宿屋", "d", subregion_id="sub1")
        self.assertIn("エラー", result)
        self.assertEqual(self.manager.create_building_calls, [])

    def test_create_building_rejects_unknown_subregion(self):
        result = self._run(game_create_building, "宿屋", "d", subregion_id="nope")
        self.assertIn("エラー", result)

    # --- game_set_scene ---

    def test_set_scene(self):
        result = self._run(game_set_scene, "battle")
        self.assertNotIn("エラー", result)
        self.assertEqual(self.manager.game_lifecycle.scene_calls, [("r1", "battle")])

    def test_set_scene_propagates_error(self):
        self.manager.game_lifecycle.set_scene = lambda rid, s: "Error: No game in progress in this region."
        result = self._run(game_set_scene, "battle")
        self.assertIn("失敗", result)

    # --- game_move_party ---

    def test_move_party(self):
        result = self._run(game_move_party, "bldg_宿屋")
        self.assertNotIn("エラー", result)
        self.assertEqual(
            self.manager.game_lifecycle.move_party_calls, [("r1", "bldg_宿屋")]
        )

    def test_move_party_propagates_error(self):
        self.manager.game_lifecycle.move_party = (
            lambda rid, bid: "Error: No game is currently playing in this region."
        )
        result = self._run(game_move_party, "bldg_宿屋")
        self.assertIn("失敗", result)

    def test_move_party_non_ruler_rejected(self):
        result = self._run(game_move_party, "bldg_宿屋", persona_id="air")
        self.assertIn("エラー", result)

    # --- helper ---

    def test_extract_id_from_result(self):
        self.assertEqual(
            extract_id_from_result("Building 'X' (ID: abc_123) created successfully."),
            "abc_123",
        )
        self.assertIsNone(extract_id_from_result("no id here"))


if __name__ == "__main__":
    unittest.main()
