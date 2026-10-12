"""アドオンからの案内 (context_provider) の登録簿と head の addon_guidance セクション。

案内はリアルタイム情報 (毎ターン差し替わる末尾メッセージ) ではなく、スペル一覧と
同じく head に置き、増えた・消えた・変わったことは差分通知で知覚に届ける
(2026-10-12、docs/intent/addon_speak_hooks.md)。リアルタイム情報だと、部屋を
出て案内が消えたことが出来事として残らず、過去に書いた記法を使い続けてしまう。
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


@pytest.fixture(autouse=True)
def _isolated_registry():
    """登録簿と「前回の結果」の記録をテストごとに空から始める (プロセス内の
    共有状態がテスト間で漏れないように)。"""
    with patch.object(addon_context, "_providers", {}), patch.object(
        addon_context, "_last_results", {},
    ), patch.object(addon_context, "_generations", {}):
        yield


class _Flaky:
    """呼ぶたびの振る舞いを外から切り替えられる提供口 (一時的な失敗の再現用)。"""

    def __init__(self, text):
        self.text = text
        self.fail = False

    def __call__(self, *, persona, building_id):
        if self.fail:
            raise RuntimeError("temporary failure")
        return self.text


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


def test_provider_replacement_empty_values_and_unregister():
    addon_context.register_provider("fixture", lambda **_kw: "first")
    addon_context.register_provider("fixture", lambda **_kw: "second")
    addon_context.register_provider("silent", lambda **_kw: "")
    addon_context.register_provider("non_str", lambda **_kw: 42)
    for persona in (None, _persona("room")):
        assert addon_context.collect_guidance(persona, "room") == [("fixture", "second")]
    addon_context.unregister_provider("fixture")
    assert addon_context.collect_guidance(_persona("room"), "room") == []


def test_failure_without_record_is_unknown_not_empty():
    # 使える前回の結果が無い失敗 (再起動直後など) は「案内なし」ではなく
    # 「分からない」— 例外で集める処理全体を撮れなかったことにする。
    addon_context.register_provider("fixture", lambda **_kw: "案内")
    addon_context.register_provider("broken", lambda **_kw: 1 / 0)
    for persona in (None, _persona("room")):
        with pytest.raises(addon_context.GuidanceUnavailable):
            addon_context.collect_guidance(persona, "room")


def test_temporary_failure_keeps_the_last_guidance():
    flaky = _Flaky("案内")
    addon_context.register_provider("flaky", flaky)
    addon_context.register_provider("other", lambda **_kw: "他の案内")
    persona = _persona("room")
    assert addon_context.collect_guidance(persona, "room") == [
        ("flaky", "案内"), ("other", "他の案内"),
    ]
    flaky.fail = True
    assert addon_context.collect_guidance(persona, "room") == [
        ("flaky", "案内"), ("other", "他の案内"),
    ]
    # 記録はペルソナごと — 一度も成功していない別のペルソナには使わない。
    other_persona = SimpleNamespace(persona_id="someone_else", current_building_id="room")
    with pytest.raises(addon_context.GuidanceUnavailable):
        addon_context.collect_guidance(other_persona, "room")


def test_temporary_failure_after_no_guidance_stays_without_guidance():
    flaky = _Flaky("")
    addon_context.register_provider("flaky", flaky)
    persona = _persona("room")
    assert addon_context.collect_guidance(persona, "room") == []
    flaky.text = "案内"
    flaky.fail = True
    assert addon_context.collect_guidance(persona, "room") == []


def test_failure_without_persona_id_has_no_record():
    flaky = _Flaky("案内")
    addon_context.register_provider("flaky", flaky)
    # persona_id が取れないときは記録しないので、成功の後の失敗も「分からない」。
    assert addon_context.collect_guidance(None, "room") == [("flaky", "案内")]
    flaky.fail = True
    with pytest.raises(addon_context.GuidanceUnavailable):
        addon_context.collect_guidance(None, "room")
    assert addon_context._last_results == {}


def test_result_of_a_provider_unregistered_mid_call_is_dropped():
    # 呼び出しの最中に解除されたアドオンの案内は、解除後の世界に持ち込まない。
    def unregister_while_running(**_kw):
        addon_context.unregister_provider("leaving")
        return "解除前の案内"

    addon_context.register_provider("leaving", unregister_while_running)
    persona = _persona("room")
    assert addon_context.collect_guidance(persona, "room") == []
    assert ("fixture", "leaving") not in addon_context._last_results


def test_unregister_forgets_the_last_results_but_replacement_keeps_them():
    flaky = _Flaky("案内")
    addon_context.register_provider("flaky", flaky)
    persona = _persona("room")
    addon_context.collect_guidance(persona, "room")

    # 同じアドオンの置き換えでは記録は残る (置き換え直後の失敗でも案内は消えない)
    replacement = _Flaky("案内")
    replacement.fail = True
    addon_context.register_provider("flaky", replacement)
    assert addon_context.collect_guidance(persona, "room") == [("flaky", "案内")]

    # 解除で記録は消え、再登録した提供口が失敗しても古い案内は戻らない
    addon_context.unregister_provider("flaky")
    assert ("fixture", "flaky") not in addon_context._last_results
    addon_context.register_provider("flaky", replacement)
    with pytest.raises(addon_context.GuidanceUnavailable):
        addon_context.collect_guidance(persona, "room")


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


def _provider_addon(tmp_path, name, text, *, with_provider=True):
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    manifest = {"context_provider": "guide_mod:guide"} if with_provider else {}
    (directory / "addon.json").write_text(json.dumps(manifest), encoding="utf8")
    (directory / "guide_mod.py").write_text(
        f'def guide(*, persona, building_id):\n    return {text!r}\n', encoding="utf8",
    )
    return directory


def test_reregistering_replaces_the_provider_without_a_gap(tmp_path, monkeypatch):
    """解決の最中も旧提供口が登録簿に残る (消えた瞬間に差分検知が走ると偽通知)。"""
    directory = _provider_addon(tmp_path, "swap-addon", "新しい案内")
    addon_context.register_provider("swap-addon", lambda **_kw: "古い案内")
    real_resolve = addon_loader._resolve_hook_handler
    seen_during_resolve = []

    def resolve_while_observing(addon_dir, spec):
        seen_during_resolve.append(addon_context.collect_guidance(_persona("room"), "room"))
        return real_resolve(addon_dir, spec)

    monkeypatch.setattr(addon_loader, "_resolve_hook_handler", resolve_while_observing)
    addon_loader._register_addon_server_hooks_for(directory)

    assert seen_during_resolve == [[("swap-addon", "古い案内")]]
    assert addon_context.collect_guidance(_persona("room"), "room") == [
        ("swap-addon", "新しい案内"),
    ]


def test_reregistering_without_context_provider_unregisters(tmp_path):
    directory = _provider_addon(tmp_path, "dropped-addon", "案内")
    addon_loader._register_addon_server_hooks_for(directory)
    persona = _persona("room")
    assert addon_context.collect_guidance(persona, "room") == [("dropped-addon", "案内")]

    _provider_addon(tmp_path, "dropped-addon", "案内", with_provider=False)
    addon_loader._register_addon_server_hooks_for(directory)
    assert "dropped-addon" not in addon_context._providers
    assert addon_context._last_results == {}
    assert addon_context.collect_guidance(persona, "room") == []


def test_resolution_failure_unregisters_the_old_provider(tmp_path):
    directory = _provider_addon(tmp_path, "broken-addon", "案内")
    addon_loader._register_addon_server_hooks_for(directory)
    persona = _persona("room")
    assert addon_context.collect_guidance(persona, "room") == [("broken-addon", "案内")]

    (directory / "addon.json").write_text(
        '{"context_provider": "guide_mod:missing_function"}', encoding="utf8",
    )
    addon_loader._register_addon_server_hooks_for(directory)
    assert "broken-addon" not in addon_context._providers
    assert addon_context._last_results == {}
    assert addon_context.collect_guidance(persona, "room") == []


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


def test_pipeline_temporary_provider_failure_produces_no_false_notifications(tmp_path):
    """提供口が一時的に失敗しても「当てはまらなくなりました」→「届きました」の
    偽通知は出ず、撮り直し (capture_all) だけが失敗しても head の案内は残る。"""
    from sea.head_pipeline import HeadPipeline, HeadSectionRegistry

    _write_manifest(tmp_path, "godot-vessel", {"ja": "Godot Virtual Vessel"})
    registry = HeadSectionRegistry()
    registry.register(AddonGuidanceSection())
    pipeline = HeadPipeline(registry=registry)
    flaky = _Flaky("")

    with patch("saiverse.data_paths.EXPANSION_DATA_DIR", tmp_path):
        addon_context.register_provider("godot-vessel", flaky)
        pipeline.capture_all(_ctx("lobby"))
        flaky.text = GUIDE_TEXT
        entered = pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True)
        assert [label.kind for label in entered] == ["addon_guidance_added"]

        flaky.fail = True
        assert pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True) == []
        redrawn = pipeline.capture_all(_ctx(VESSEL_ROOM))
        assert redrawn.sections["addon_guidance"] == _snap(
            ("godot-vessel", "Godot Virtual Vessel", GUIDE_TEXT),
        )

        flaky.fail = False
        assert pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True) == []


def test_pipeline_failure_right_after_restart_keeps_head_and_sends_nothing(tmp_path):
    """再起動直後 (前回の結果の記録が空) に提供口が失敗しても、届けた案内に
    「当てはまらなくなりました」は出ず、撮り直しは古い head を据え置いて
    「撮り直せていない」と記録する (届けた通知を提示から下ろさせない)。"""
    from sea.head_pipeline import HeadPipeline, HeadSectionRegistry

    _write_manifest(tmp_path, "godot-vessel", {"ja": "Godot Virtual Vessel"})
    registry = HeadSectionRegistry()
    registry.register(AddonGuidanceSection())
    pipeline = HeadPipeline(registry=registry)
    flaky = _Flaky(GUIDE_TEXT)

    with patch("saiverse.data_paths.EXPANSION_DATA_DIR", tmp_path):
        addon_context.register_provider("godot-vessel", flaky)
        pipeline.capture_all(_ctx(VESSEL_ROOM))

        # 再起動相当: メモリの記録だけが消え、head と既読は残っている。
        addon_context._last_results.clear()
        flaky.fail = True
        assert pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True) == []
        redrawn = pipeline.capture_all(_ctx(VESSEL_ROOM))
        assert redrawn.sections["addon_guidance"] == _snap(
            ("godot-vessel", "Godot Virtual Vessel", GUIDE_TEXT),
        )
        assert pipeline.sections_not_freshly_captured(
            "fixture", "fixture-model", ["addon_guidance"],
        ) == {"addon_guidance"}

        flaky.fail = False
        assert pipeline.flush_diffs(_ctx(VESSEL_ROOM), all_sections=True) == []


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
