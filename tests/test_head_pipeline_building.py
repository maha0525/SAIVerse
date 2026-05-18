"""Tests for BuildingSection."""
from types import SimpleNamespace

import pytest

from sea.head_pipeline import LineHeadInput
from sea.head_pipeline.sections.building import BuildingSection, BuildingSnapshot


def _fake_building(**kwargs):
    defaults = {
        "name": "Lobby",
        "base_system_instruction": "Welcome to the lobby.",
        "physical_vessel_id": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(building_id="b_lobby", persona=None, manager=None):
    return LineHeadInput(
        persona_id="air", line_id="main", line_role="main_line",
        model_key="claude-opus-4-7", current_building_id=building_id,
        persona=persona, manager=manager,
    )


def test_capture_reads_from_persona_buildings_dict():
    persona = SimpleNamespace(buildings={"b_lobby": _fake_building()})
    ctx = _ctx(persona=persona)
    snapshot = BuildingSection().capture(ctx)
    assert snapshot.name == "Lobby"
    assert snapshot.base_system_instruction == "Welcome to the lobby."


def test_capture_falls_back_to_manager_building_map():
    manager = SimpleNamespace(building_map={"b_lobby": _fake_building(name="Hall")})
    ctx = _ctx(manager=manager)
    snapshot = BuildingSection().capture(ctx)
    assert snapshot.name == "Hall"


def test_capture_returns_empty_snapshot_when_building_unknown():
    ctx = _ctx(building_id=None)
    snapshot = BuildingSection().capture(ctx)
    assert snapshot.name == ""
    assert snapshot.base_system_instruction == ""


def test_render_includes_name_id_and_instruction():
    snap = BuildingSnapshot(
        building_id="b_lobby", name="Lobby",
        base_system_instruction="Welcome.", physical_vessel_id=None,
    )
    rendered = BuildingSection().render(snap)
    assert rendered is not None
    assert "## Lobby (ID: b_lobby)" in rendered.text
    assert "Welcome." in rendered.text


def test_render_returns_none_for_blank_snapshot():
    snap = BuildingSnapshot(
        building_id=None, name="", base_system_instruction="",
        physical_vessel_id=None,
    )
    assert BuildingSection().render(snap) is None


def test_diff_notifies_on_building_change():
    section = BuildingSection()
    old = BuildingSnapshot(
        building_id="b1", name="One", base_system_instruction="a",
        physical_vessel_id=None,
    )
    new = BuildingSnapshot(
        building_id="b2", name="Two", base_system_instruction="b",
        physical_vessel_id=None,
    )
    labels = section.diff_to_notifications(old, new)
    assert any(label.kind == "building_changed" for label in labels)


def test_diff_notifies_on_system_prompt_change():
    section = BuildingSection()
    old = BuildingSnapshot(
        building_id="b1", name="One", base_system_instruction="old",
        physical_vessel_id=None,
    )
    new = BuildingSnapshot(
        building_id="b1", name="One", base_system_instruction="new",
        physical_vessel_id=None,
    )
    labels = section.diff_to_notifications(old, new)
    assert len(labels) == 1
    assert labels[0].kind == "building_system_prompt_changed"


def test_diff_notifies_on_vessel_attach_detach():
    section = BuildingSection()
    base = BuildingSnapshot(
        building_id="b1", name="Vessel", base_system_instruction="",
        physical_vessel_id=None,
    )
    attached = BuildingSnapshot(
        building_id="b1", name="Vessel", base_system_instruction="",
        physical_vessel_id="stackchan-001",
    )
    labels = section.diff_to_notifications(base, attached)
    assert any(label.kind == "building_vessel_attached" for label in labels)
    labels = section.diff_to_notifications(attached, base)
    assert any(label.kind == "building_vessel_detached" for label in labels)


def test_serialize_deserialize_roundtrip():
    section = BuildingSection()
    snap = BuildingSnapshot(
        building_id="b1", name="Lobby", base_system_instruction="Welcome",
        physical_vessel_id="stackchan-001",
    )
    data = section.serialize_snapshot(snap)
    assert section.deserialize_snapshot(data) == snap


@pytest.fixture
def section():
    return BuildingSection()


def test_section_satisfies_head_section_protocol(section):
    from sea.head_pipeline import HeadSection
    assert isinstance(section, HeadSection)
