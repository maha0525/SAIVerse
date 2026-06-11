"""OccupancyManager の game Region 入場ゲートのテスト。

_check_game_region_gate は self._manager_ref / self.building_map しか使わない
ため、__new__ で生成して属性を差し込む軽量構成でテストする。
"""
import unittest

from saiverse.occupancy_manager import OccupancyManager


class FakeRegion:
    def __init__(self, is_game_region=True, phase=None, participants=None, ruler_id="ruler1"):
        self.region_id = "r1"
        self.is_game_region = is_game_region
        self.ruler_id = ruler_id
        self.state = {}
        if phase is not None:
            self.state["phase"] = phase
        if participants is not None:
            self.state["participants"] = participants


class FakeBuilding:
    def __init__(self, name):
        self.name = name


class FakeManager:
    def __init__(self, region):
        self._region = region

    def get_top_region_of_building(self, building_id):
        return self._region


class GameRegionGateTestCase(unittest.TestCase):
    def _make_om(self, region):
        om = OccupancyManager.__new__(OccupancyManager)
        om._manager_ref = FakeManager(region)
        om.building_map = {"b1": FakeBuilding("酒場")}
        return om

    def test_no_region_allows(self):
        om = self._make_om(None)
        self.assertIsNone(om._check_game_region_gate("p1", "b1"))

    def test_setup_phase_allows_everyone(self):
        om = self._make_om(FakeRegion(phase="setup", participants=["p1"]))
        self.assertIsNone(om._check_game_region_gate("outsider", "b1"))

    def test_no_phase_allows_everyone(self):
        om = self._make_om(FakeRegion())
        self.assertIsNone(om._check_game_region_gate("outsider", "b1"))

    def test_playing_denies_outsider(self):
        om = self._make_om(FakeRegion(phase="playing", participants=["p1"]))
        denial = om._check_game_region_gate("outsider", "b1")
        self.assertIsNotNone(denial)
        self.assertIn("ゲームが進行中", denial)

    def test_playing_allows_participant(self):
        om = self._make_om(FakeRegion(phase="playing", participants=["p1"]))
        self.assertIsNone(om._check_game_region_gate("p1", "b1"))

    def test_playing_allows_ruler(self):
        om = self._make_om(FakeRegion(phase="playing", participants=["p1"]))
        self.assertIsNone(om._check_game_region_gate("ruler1", "b1"))

    def test_paused_denies_outsider(self):
        om = self._make_om(FakeRegion(phase="paused", participants=["p1"]))
        self.assertIsNotNone(om._check_game_region_gate("outsider", "b1"))

    def test_paused_allows_returning_participant(self):
        om = self._make_om(FakeRegion(phase="paused", participants=["p1", "42"]))
        self.assertIsNone(om._check_game_region_gate("42", "b1"))

    def test_participant_id_type_mismatch_tolerated(self):
        # ユーザー ID が int で保存されても str 比較で通す
        om = self._make_om(FakeRegion(phase="playing", participants=[42]))
        self.assertIsNone(om._check_game_region_gate("42", "b1"))

    def test_generic_region_not_gated(self):
        om = self._make_om(FakeRegion(is_game_region=False, phase="playing", participants=[]))
        self.assertIsNone(om._check_game_region_gate("outsider", "b1"))

    def test_manager_without_region_support_allows(self):
        om = OccupancyManager.__new__(OccupancyManager)
        om._manager_ref = object()  # get_top_region_of_building を持たない
        om.building_map = {}
        self.assertIsNone(om._check_game_region_gate("p1", "b1"))


if __name__ == "__main__":
    unittest.main()
