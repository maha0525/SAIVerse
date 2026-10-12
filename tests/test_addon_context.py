"""アドオンからの案内 (context_provider) の登録簿と head の addon_guidance セクション。

案内はリアルタイム情報 (毎ターン差し替わる末尾メッセージ) ではなく、スペル一覧と
同じく head に置き、増えた・消えた・変わったことは差分通知で知覚に届ける
(2026-10-12、docs/intent/addon_speak_hooks.md)。リアルタイム情報だと、部屋を
出て案内が消えたことが出来事として残らず、過去に書いた記法を使い続けてしまう。
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from sai_memory.presented_reduction import is_droppable_notice
from sai_memory.room_state import LABEL_KIND_META_KEY
from saiverse import addon_context, addon_loader
from sea.head_pipeline import LineHeadInput
from sea.head_pipeline.sections.addon_guidance import (
    AddonGuidance,
    AddonGuidanceSection,
    AddonGuidanceSnapshot,
)
from sea.runtime import SEARuntime

VESSEL_ROOM = "vessel_room"
GUIDE_TEXT = (
    'この部屋には仮想身体があります。発話中の動作を始めたい位置へ '
    '{"body_emote": "smile"} のように書けます。'
)


def _room_guide(*, persona, building_id):
    """仮想身体の部屋に居るときだけ案内を返す、Godot 提供口と同じ形の偽物。"""
    if building_id == VESSEL_ROOM and getattr(persona, "current_building_id", None) == VESSEL_ROOM:
        return GUIDE_TEXT
    return ""


def _persona(building_id):
    return SimpleNamespace(persona_id="fixture", current_building_id=building_id)


def _ctx(building_id, persona=None):
    return LineHeadInput(
        persona_id="fixture",
        model_key="fixture-model",
        current_building_id=building_id,
        persona=persona if persona is not None else _persona(building_id),
    )


def _write_manifest(root, addon_name, display_name):
    addon_dir = root / addon_name
    addon_dir.mkdir(parents=True, exist_ok=True)
    (addon_dir / "addon.json").write_text(
        json.dumps({"name": addon_name, "display_name": display_name}, ensure_ascii=False),
        encoding="utf-8",
    )


def _snap(*items):
    return AddonGuidanceSnapshot(items=tuple(
        AddonGuidance(addon_name=name, display_name=display, text=text)
        for name, display, text in items
    ))


# ---------------------------------------------------------------------------
# 提供口の登録簿
# ---------------------------------------------------------------------------


def test_provider_replacement_failure_isolation_and_unregister():
    with patch.object(addon_context, "_providers", {}):
        addon_context.register_provider("fixture", lambda **_kw: "first")
        addon_context.register_provider("fixture", lambda **_kw: "second")
        addon_context.register_provider("broken", lambda **_kw: 1 / 0)
        addon_context.register_provider("silent", lambda **_kw: "")
        addon_context.register_provider("non_str", lambda **_kw: 42)
        assert addon_context.collect_guidance(None, "room") == [("fixture", "second")]
        addon_context.unregister_provider("fixture")
        assert addon_context.collect_guidance(None, "room") == []


def test_collect_guidance_is_sorted_by_addon_name():
    with patch.object(addon_context, "_providers", {}):
        addon_context.register_provider("zeta", lambda **_kw: "z")
        addon_context.register_provider("alpha", lambda **_kw: "a")
        assert addon_context.collect_guidance(None, None) == [("alpha", "a"), ("zeta", "z")]


def test_manifest_provider_lifecycle(tmp_path):
    directory = tmp_path / "fixture-addon"
    directory.mkdir()
    (directory / "addon.json").write_text('{"context_provider":"fixture:guide"}', encoding="utf8")
    (directory / "fixture.py").write_text(
        'def guide(*, persona, building_id):\n'
        '    return "fixture guidance" if persona.current_building_id == building_id == "room" else ""\n',
        encoding="utf8")
    with patch.object(addon_context, "_providers", {}):
        addon_loader._register_addon_server_hooks_for(directory)
        assert addon_context.collect_guidance(_persona("room"), "room") == [
            ("fixture-addon", "fixture guidance"),
        ]
        assert addon_context.collect_guidance(_persona("other"), "other") == []
        addon_loader.unregister_addon_server_hooks("fixture-addon")
        assert addon_context.collect_guidance(_persona("room"), "room") == []


def test_display_name_prefers_japanese_from_localized_manifest(tmp_path):
    _write_manifest(tmp_path, "vessel", {"ja": "仮想の器", "en": "Virtual Vessel"})
    _write_manifest(tmp_path, "plain", "プレーン")
    with patch("saiverse.data_paths.EXPANSION_DATA_DIR", tmp_path):
        assert addon_context.addon_display_name("vessel") == "仮想の器"
        assert addon_context.addon_display_name("plain") == "プレーン"
        assert addon_context.addon_display_name("missing") == "missing"


# ---------------------------------------------------------------------------
# head セクション: capture / render
# ---------------------------------------------------------------------------


def test_head_shows_guidance_only_in_the_target_room(tmp_path):
    _write_manifest(tmp_path, "godot-vessel", {"ja": "Godot Virtual Vessel"})
    section = AddonGuidanceSection()
    with patch.object(addon_context, "_providers", {}), patch(
        "saiverse.data_paths.EXPANSION_DATA_DIR", tmp_path,
    ):
        addon_context.register_provider("godot-vessel", _room_guide)
        inside = section.capture(_ctx(VESSEL_ROOM))
        outside = section.capture(_ctx("lobby"))

    assert inside == _snap(("godot-vessel", "Godot Virtual Vessel", GUIDE_TEXT))
    assert section.render(inside).text == (
        "## アドオンからの案内\n### Godot Virtual Vessel\n" + GUIDE_TEXT
    )
    assert outside == AddonGuidanceSnapshot()
    assert section.render(outside) is None


def test_capture_survives_missing_persona_and_building():
    section = AddonGuidanceSection()
    with patch.object(addon_context, "_providers", {}):
        addon_context.register_provider("godot-vessel", _room_guide)
        ctx = LineHeadInput(persona_id="fixture", model_key="m")
        assert section.capture(ctx) == AddonGuidanceSnapshot()


# ---------------------------------------------------------------------------
# head セクション: 差分通知
# ---------------------------------------------------------------------------


def _label_kind(label):
    return label.metadata.get(LABEL_KIND_META_KEY)


def test_entering_the_room_delivers_the_guidance_text():
    section = AddonGuidanceSection()
    labels = section.diff_to_notifications(
        _snap(), _snap(("godot-vessel", "Godot Virtual Vessel", GUIDE_TEXT)),
    )
    assert [label.kind for label in labels] == ["addon_guidance_added"]
    assert labels[0].label == (
        "「Godot Virtual Vessel」からの案内が届きました\n" + GUIDE_TEXT
    )
    assert _label_kind(labels[0]) == "addon_guidance_added"


def test_leaving_the_room_tells_the_persona_to_stop_using_it():
    section = AddonGuidanceSection()
    labels = section.diff_to_notifications(
        _snap(("godot-vessel", "Godot Virtual Vessel", GUIDE_TEXT)), _snap(),
    )
    assert [label.kind for label in labels] == ["addon_guidance_removed"]
    assert labels[0].label == (
        "「Godot Virtual Vessel」からの案内は、今は当てはまらなくなりました。"
        "この案内にあった書き方は使わないでください。"
    )
    assert _label_kind(labels[0]) == "addon_guidance_removed"


def test_changed_guidance_delivers_the_new_text():
    section = AddonGuidanceSection()
    labels = section.diff_to_notifications(
        _snap(("godot-vessel", "Godot Virtual Vessel", "古い案内")),
        _snap(("godot-vessel", "Godot Virtual Vessel", "新しい案内")),
    )
    assert [label.kind for label in labels] == ["addon_guidance_changed"]
    assert labels[0].label == "「Godot Virtual Vessel」からの案内が変わりました\n新しい案内"
    assert _label_kind(labels[0]) == "addon_guidance_changed"


def test_no_change_and_missing_baseline_produce_no_notification():
    section = AddonGuidanceSection()
    same = _snap(("godot-vessel", "Godot Virtual Vessel", GUIDE_TEXT))
    assert section.diff_to_notifications(same, same) == []
    assert section.diff_to_notifications(None, same) == []
    assert section.diff_to_notifications(same, None) == []


def test_several_additions_are_one_label():
    section = AddonGuidanceSection()
    labels = section.diff_to_notifications(
        _snap(), _snap(("a", "A", "案内A"), ("b", "B", "案内B")),
    )
    assert len(labels) == 1
    assert labels[0].label == (
        "アドオンからの案内が 2 件届きました\n\n"
        "「A」からの案内が届きました\n案内A\n\n"
        "「B」からの案内が届きました\n案内B"
    )


def test_guidance_labels_are_dropped_once_the_head_is_redrawn():
    for kind in (
        "addon_guidance_added", "addon_guidance_removed", "addon_guidance_changed",
    ):
        meta = json.dumps({LABEL_KIND_META_KEY: kind})
        assert is_droppable_notice("world_state", meta), kind


def test_serialize_roundtrip():
    section = AddonGuidanceSection()
    snap = _snap(("godot-vessel", "Godot Virtual Vessel", GUIDE_TEXT), ("x", "X", "本文"))
    assert section.deserialize_snapshot(section.serialize_snapshot(snap)) == snap
    empty = AddonGuidanceSnapshot()
    assert section.deserialize_snapshot(section.serialize_snapshot(empty)) == empty


# ---------------------------------------------------------------------------
# 通し: 実 HeadPipeline の flush_diffs で、部屋を移ると知覚通知が出る
# ---------------------------------------------------------------------------


def test_pipeline_flush_diffs_reports_entering_and_leaving_the_room(tmp_path):
    from sea.head_pipeline import HeadPipeline, HeadSectionRegistry

    _write_manifest(tmp_path, "godot-vessel", {"ja": "Godot Virtual Vessel"})
    registry = HeadSectionRegistry()
    registry.register(AddonGuidanceSection())
    pipeline = HeadPipeline(registry=registry)

    with patch.object(addon_context, "_providers", {}), patch(
        "saiverse.data_paths.EXPANSION_DATA_DIR", tmp_path,
    ):
        addon_context.register_provider("godot-vessel", _room_guide)

        pipeline.capture_all(_ctx("lobby"))
        assert pipeline.flush_diffs(_ctx("lobby"), all_sections=True) == []

        entered = pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True)
        assert [label.kind for label in entered] == ["addon_guidance_added"]
        assert GUIDE_TEXT in entered[0].label

        # 同じ部屋に居続けても二度は知らせない (基準が前進している)
        assert pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True) == []

        left = pipeline.flush_diffs(_ctx("lobby"), all_sections=True)
        assert [label.kind for label in left] == ["addon_guidance_removed"]

        # アドオンを無効化して提供口が外れても「消えた」が届く
        pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True)
        addon_context.unregister_provider("godot-vessel")
        disabled = pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True)
        assert [label.kind for label in disabled] == ["addon_guidance_removed"]


# ---------------------------------------------------------------------------
# リアルタイム情報には載らない
# ---------------------------------------------------------------------------


def test_realtime_context_does_not_carry_addon_guidance():
    runtime = SimpleNamespace(_realtime_info_flags_for_persona=lambda _p: (False, False))
    with patch.object(addon_context, "_providers", {}):
        addon_context.register_provider("godot-vessel", _room_guide)
        message = SEARuntime._build_realtime_context(
            runtime, _persona(VESSEL_ROOM), VESSEL_ROOM, [],
        )
    assert message is None
