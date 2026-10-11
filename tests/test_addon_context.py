from types import SimpleNamespace
from unittest.mock import patch

from saiverse import addon_context, addon_loader
from sea.runtime import SEARuntime


def test_provider_replacement_failure_isolation_and_unregister():
    with patch.object(addon_context, "_providers", {}):
        addon_context.register_provider("fixture", lambda **_kw: "first")
        addon_context.register_provider("fixture", lambda **_kw: "second")
        addon_context.register_provider("broken", lambda **_kw: 1 / 0)
        assert addon_context.context_sections(None, "room") == ["second"]
        addon_context.unregister_provider("fixture")
        assert addon_context.context_sections(None, "room") == []


def test_manifest_provider_lifecycle_and_real_realtime_injection(tmp_path):
    directory = tmp_path / "fixture-addon"
    directory.mkdir()
    (directory / "addon.json").write_text('{"context_provider":"fixture:guide"}', encoding="utf8")
    (directory / "fixture.py").write_text(
        'def guide(*, persona, building_id):\n'
        '    return "fixture guidance" if persona.current_building_id == building_id == "room" else ""\n',
        encoding="utf8")
    persona = SimpleNamespace(persona_id="fixture", current_building_id="room")
    runtime = SimpleNamespace(_realtime_info_flags_for_persona=lambda _p: (False, False))
    with patch.object(addon_context, "_providers", {}):
        addon_loader._register_addon_server_hooks_for(directory)
        message = SEARuntime._build_realtime_context(runtime, persona, "room", [])
        assert message["metadata"]["__realtime_context__"]
        assert "fixture guidance" in message["content"]
        assert SEARuntime._build_realtime_context(runtime, persona, "other", []) is None
        addon_loader.unregister_addon_server_hooks("fixture-addon")
        assert SEARuntime._build_realtime_context(runtime, persona, "room", []) is None
