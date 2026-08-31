"""OccupancyManager の入口トポロジーチェック (_check_entrance_topology) のテスト。

スコープ構成:
  city 直属: c1, entrance_top (Region 'top' の入口)
  Region 'top' 直属: t1, t2, entrance_sub (SubRegion 'sub' の入口)
  SubRegion 'sub' 直属: s1

設計: docs/intent/region.md §2.4, §3
"""
import unittest

from saiverse.occupancy_manager import OccupancyManager


class FakeRegion:
    def __init__(self, region_id, name, parent_region_id=None,
                 entrance_building_id=None, config=None):
        self.region_id = region_id
        self.name = name
        self.parent_region_id = parent_region_id
        self.entrance_building_id = entrance_building_id
        self.config = config or {}


class FakeBuilding:
    def __init__(self, name, region_id=None):
        self.name = name
        self.region_id = region_id


class FakeManager:
    def __init__(self, regions):
        self._regions = regions

    def get_region(self, region_id):
        return self._regions.get(region_id)


def make_om(regions, buildings):
    om = OccupancyManager.__new__(OccupancyManager)
    om._manager_ref = FakeManager(regions)
    om.building_map = buildings
    import threading
    om._topology_bypass = threading.local()
    return om


class EntranceTopologyTestCase(unittest.TestCase):
    def setUp(self):
        self.regions = {
            "top": FakeRegion("top", "霧の谷", entrance_building_id="entrance_top"),
            "sub": FakeRegion("sub", "霧降りの森", parent_region_id="top",
                              entrance_building_id="entrance_sub"),
        }
        self.buildings = {
            "c1": FakeBuilding("広場"),
            "entrance_top": FakeBuilding("霧の谷: 入口"),
            "t1": FakeBuilding("宿屋", region_id="top"),
            "t2": FakeBuilding("酒場", region_id="top"),
            "entrance_sub": FakeBuilding("霧降りの森: 入口", region_id="top"),
            "s1": FakeBuilding("祠", region_id="sub"),
        }
        self.om = make_om(self.regions, self.buildings)

    def check(self, from_id, to_id, entity_id="p1"):
        return self.om._check_entrance_topology(entity_id, from_id, to_id)

    # --- 同一スコープ / 退出 (制限なし) ---

    def test_city_to_city_allowed(self):
        self.assertIsNone(self.check("c1", "entrance_top"))

    def test_same_region_move_allowed(self):
        self.assertIsNone(self.check("t1", "t2"))

    def test_exit_to_city_allowed(self):
        self.assertIsNone(self.check("t1", "c1"))

    def test_exit_from_subregion_to_city_allowed(self):
        # 退出は多段ジャンプも許す
        self.assertIsNone(self.check("s1", "c1"))

    def test_exit_subregion_into_parent_allowed(self):
        self.assertIsNone(self.check("s1", "t1"))

    # --- 入場は入口経由のみ ---

    def test_entrance_to_interior_allowed(self):
        self.assertIsNone(self.check("entrance_top", "t1"))

    def test_sub_entrance_to_sub_interior_allowed(self):
        self.assertIsNone(self.check("entrance_sub", "s1"))

    def test_city_to_interior_denied_with_guidance(self):
        denial = self.check("c1", "t1")
        self.assertIsNotNone(denial)
        self.assertIn("霧の谷: 入口", denial)
        self.assertIn("entrance_top", denial)

    def test_city_to_subregion_interior_denied(self):
        # 2 スコープ跨ぎの直行は不可。案内は最外殻 (top) の入口
        denial = self.check("c1", "s1")
        self.assertIsNotNone(denial)
        self.assertIn("entrance_top", denial)

    def test_region_interior_to_sub_interior_denied_without_gate(self):
        denial = self.check("t1", "s1")
        self.assertIsNotNone(denial)
        self.assertIn("entrance_sub", denial)

    def test_city_to_sub_entrance_denied(self):
        # SubRegion の入口は親 Region 内部にあるので、外からは top 入口経由
        denial = self.check("c1", "entrance_sub")
        self.assertIsNotNone(denial)
        self.assertIn("entrance_top", denial)

    def test_region_without_entrance_denied(self):
        self.regions["top"].entrance_building_id = None
        denial = self.check("c1", "t1")
        self.assertIsNotNone(denial)
        self.assertIn("入口が設定されていない", denial)

    # --- entry policy (入口→内部の境界点で執行) ---

    def test_locked_region_denies_at_entrance(self):
        self.regions["top"].config = {"entry_policy": "locked"}
        denial = self.check("entrance_top", "t1")
        self.assertIsNotNone(denial)
        self.assertIn("鍵", denial)

    def test_locked_region_allows_entry_allowed(self):
        self.regions["top"].config = {
            "entry_policy": "locked", "entry_allowed": ["air"],
        }
        self.assertIsNone(self.check("entrance_top", "t1", entity_id="air"))

    def test_whitelist_region_denies_outsider(self):
        self.regions["top"].config = {
            "entry_policy": "whitelist", "entry_allowed": ["air"],
        }
        denial = self.check("entrance_top", "t1", entity_id="someone")
        self.assertIsNotNone(denial)
        self.assertIn("関係者以外", denial)

    def test_policy_not_applied_on_exit(self):
        self.regions["top"].config = {"entry_policy": "locked"}
        self.assertIsNone(self.check("t1", "c1"))

    def test_entry_allowed_id_type_mismatch_tolerated(self):
        self.regions["top"].config = {
            "entry_policy": "locked", "entry_allowed": [42],
        }
        self.assertIsNone(self.check("entrance_top", "t1", entity_id="42"))

    # --- system 移動バイパス ---

    def test_bypass_allows_direct_entry(self):
        with self.om.topology_bypass():
            self.assertIsNone(self.check("c1", "s1"))
        # コンテキストを抜けたら復活する
        self.assertIsNotNone(self.check("c1", "s1"))

    def test_bypass_nests(self):
        with self.om.topology_bypass():
            with self.om.topology_bypass():
                self.assertIsNone(self.check("c1", "s1"))
            self.assertIsNone(self.check("c1", "s1"))
        self.assertIsNotNone(self.check("c1", "s1"))

    # --- 縮退 ---

    def test_manager_without_region_support_allows(self):
        om = OccupancyManager.__new__(OccupancyManager)
        om._manager_ref = object()  # get_region を持たない
        om.building_map = self.buildings
        import threading
        om._topology_bypass = threading.local()
        self.assertIsNone(om._check_entrance_topology("p1", "c1", "t1"))

    def test_corrupted_self_referencing_region_no_infinite_loop(self):
        self.regions["loop"] = FakeRegion(
            "loop", "壊", parent_region_id="loop", entrance_building_id=None,
        )
        self.buildings["lb"] = FakeBuilding("壊B", region_id="loop")
        denial = self.check("c1", "lb")
        self.assertIsNotNone(denial)


if __name__ == "__main__":
    unittest.main()
