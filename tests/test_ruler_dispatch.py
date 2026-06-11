"""_build_responding_personas (Ruler 注入) のテスト。

RuntimeService の当該メソッドは self.personas / self.occupants / self.manager
しか使わないため、__new__ で生成して属性を差し込む軽量構成でテストする。
"""
import unittest

from manager.runtime import RuntimeService


class FakePersona:
    def __init__(self, persona_id, is_dispatched=False, persona_role=None):
        self.persona_id = persona_id
        self.is_dispatched = is_dispatched
        self.persona_role = persona_role


class FakeRegion:
    def __init__(self, region_id, is_game_region=True, ruler_id=None):
        self.region_id = region_id
        self.is_game_region = is_game_region
        self.ruler_id = ruler_id


class FakeManager:
    def __init__(self, region_by_building=None):
        self._region_by_building = region_by_building or {}

    def get_top_region_of_building(self, building_id):
        return self._region_by_building.get(building_id)


class BuildRespondingPersonasTestCase(unittest.TestCase):
    def _make_service(self, personas, occupants, region_by_building=None):
        svc = RuntimeService.__new__(RuntimeService)
        svc.personas = personas
        svc.occupants = occupants
        svc.manager = FakeManager(region_by_building)
        return svc

    def test_normal_building_returns_occupants(self):
        p1, p2 = FakePersona("p1"), FakePersona("p2")
        svc = self._make_service({"p1": p1, "p2": p2}, {"b1": ["p1", "p2"]})
        result = svc._build_responding_personas("b1")
        self.assertEqual([p.persona_id for p in result], ["p1", "p2"])

    def test_dispatched_persona_excluded(self):
        p1 = FakePersona("p1", is_dispatched=True)
        svc = self._make_service({"p1": p1}, {"b1": ["p1"]})
        self.assertEqual(svc._build_responding_personas("b1"), [])

    def test_game_region_injects_ruler_first(self):
        ruler = FakePersona("ruler1", persona_role="ruler")
        p1 = FakePersona("p1")
        svc = self._make_service(
            {"ruler1": ruler, "p1": p1},
            {"b1": ["p1"]},
            {"b1": FakeRegion("r1", ruler_id="ruler1")},
        )
        result = svc._build_responding_personas("b1")
        self.assertEqual([p.persona_id for p in result], ["ruler1", "p1"])

    def test_ruler_not_duplicated_when_already_occupant(self):
        ruler = FakePersona("ruler1", persona_role="ruler")
        p1 = FakePersona("p1")
        svc = self._make_service(
            {"ruler1": ruler, "p1": p1},
            {"b1": ["ruler1", "p1"]},
            {"b1": FakeRegion("r1", ruler_id="ruler1")},
        )
        result = svc._build_responding_personas("b1")
        self.assertEqual([p.persona_id for p in result], ["ruler1", "p1"])

    def test_non_game_region_no_injection(self):
        ruler = FakePersona("ruler1", persona_role="ruler")
        p1 = FakePersona("p1")
        svc = self._make_service(
            {"ruler1": ruler, "p1": p1},
            {"b1": ["p1"]},
            {"b1": FakeRegion("r1", is_game_region=False, ruler_id="ruler1")},
        )
        result = svc._build_responding_personas("b1")
        self.assertEqual([p.persona_id for p in result], ["p1"])

    def test_region_without_ruler_no_injection(self):
        p1 = FakePersona("p1")
        svc = self._make_service(
            {"p1": p1},
            {"b1": ["p1"]},
            {"b1": FakeRegion("r1", ruler_id=None)},
        )
        result = svc._build_responding_personas("b1")
        self.assertEqual([p.persona_id for p in result], ["p1"])

    def test_ruler_not_loaded_logs_and_skips(self):
        p1 = FakePersona("p1")
        svc = self._make_service(
            {"p1": p1},
            {"b1": ["p1"]},
            {"b1": FakeRegion("r1", ruler_id="ghost")},
        )
        with self.assertLogs(level="WARNING"):
            result = svc._build_responding_personas("b1")
        self.assertEqual([p.persona_id for p in result], ["p1"])

    def test_dispatched_ruler_not_injected(self):
        ruler = FakePersona("ruler1", is_dispatched=True, persona_role="ruler")
        p1 = FakePersona("p1")
        svc = self._make_service(
            {"ruler1": ruler, "p1": p1},
            {"b1": ["p1"]},
            {"b1": FakeRegion("r1", ruler_id="ruler1")},
        )
        result = svc._build_responding_personas("b1")
        self.assertEqual([p.persona_id for p in result], ["p1"])


if __name__ == "__main__":
    unittest.main()
