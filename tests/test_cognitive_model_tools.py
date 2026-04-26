"""Smoke tests for cognitive model tools (Phase B-3).

ツールは TrackManager / NoteManager の薄いラッパーであり、それらの
ロジックは別途充実したテストでカバーされている (test_track_manager.py /
test_note_manager.py)。本テストではツールのロード・schema・関数定義の
最低限の整合性のみ検証する。
"""
import pytest

from tool_loader import load_builtin_tool
from tools.core import ToolSchema


TRACK_TOOLS = [
    "track_create",
    "track_activate",
    "track_pause",
    "track_complete",
    "track_abort",
    "track_list",
]

NOTE_TOOLS = [
    "note_create",
    "note_open",
    "note_close",
    "note_search",
]

ALL_TOOLS = TRACK_TOOLS + NOTE_TOOLS


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_tool_loads(name):
    """Each tool module loads without error."""
    module = load_builtin_tool(name)
    assert hasattr(module, name), f"module {name} must expose function {name!r}"
    assert hasattr(module, "schema"), f"module {name} must expose schema()"


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_schema_returns_toolschema_with_correct_name(name):
    module = load_builtin_tool(name)
    schema = module.schema()
    assert isinstance(schema, ToolSchema)
    assert schema.name == name
    assert schema.description, f"{name} must have a non-empty description"
    assert isinstance(schema.parameters, dict)
    assert schema.parameters.get("type") == "object"


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_function_is_callable(name):
    module = load_builtin_tool(name)
    func = getattr(module, name)
    assert callable(func)


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_tool_is_spell_with_display_name(name):
    """All cognitive tools must be spells (Intent A v0.9 line-separation policy).

    メインラインで構造化出力に引きずられた事故を避けるため、ペルソナがネイティブ
    ツールコールでこれらを叩く構成は採らない。スペル方式で発動させる。
    """
    module = load_builtin_tool(name)
    schema = module.schema()
    assert schema.spell is True, f"{name} must have spell=True"
    assert schema.spell_display_name, f"{name} must have non-empty spell_display_name"


def test_track_create_schema_required_fields():
    module = load_builtin_tool("track_create")
    schema = module.schema()
    required = schema.parameters.get("required", [])
    assert "track_type" in required


def test_note_create_schema_required_fields():
    module = load_builtin_tool("note_create")
    schema = module.schema()
    required = schema.parameters.get("required", [])
    assert "title" in required
    assert "note_type" in required


def test_note_create_schema_enforces_three_types():
    module = load_builtin_tool("note_create")
    schema = module.schema()
    note_type = schema.parameters["properties"]["note_type"]
    assert set(note_type.get("enum", [])) == {"person", "project", "vocation"}


def test_track_activate_requires_track_id():
    module = load_builtin_tool("track_activate")
    schema = module.schema()
    assert "track_id" in schema.parameters.get("required", [])


def test_track_complete_describes_persistent_constraint():
    """The description should mention persistent tracks cannot be completed."""
    module = load_builtin_tool("track_complete")
    schema = module.schema()
    text = schema.description.lower()
    assert "persistent" in text


def test_track_abort_describes_persistent_constraint():
    module = load_builtin_tool("track_abort")
    schema = module.schema()
    text = schema.description.lower()
    assert "persistent" in text


def test_track_list_no_required_params():
    module = load_builtin_tool("track_list")
    schema = module.schema()
    # All params are optional
    assert schema.parameters.get("required", []) == []


def test_note_search_no_required_params():
    module = load_builtin_tool("note_search")
    schema = module.schema()
    assert schema.parameters.get("required", []) == []


def test_tools_register_in_global_tool_registry():
    """After importing tools package, all new cognitive tools should be registered."""
    # Import triggers autodiscovery
    import tools as tools_pkg
    # Reload the registry by re-importing builtin_data.tools registration
    # (autodiscovery happens at tools.__init__ import)
    for name in ALL_TOOLS:
        assert name in tools_pkg.TOOL_REGISTRY, (
            f"tool {name!r} should be auto-registered in TOOL_REGISTRY"
        )
