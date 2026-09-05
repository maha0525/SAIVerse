"""Tests for SpellListSection.

capture が live state (SPELL_TOOL_SCHEMAS / MCP / availability_check / DB) から
正しく snapshot を組み立て、render が既存表記を再現、diff が added/removed を
出すことを確認する。
"""
import gc
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sea.head_pipeline import LineHeadInput, NotificationLabel
from sea.head_pipeline.sections.spell_list import (
    AddonManifest,
    SpellEntry,
    SpellListSection,
    SpellListSnapshot,
)
from tools.core import ToolSchema


@pytest.fixture
def isolated_manager(request):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import AI, Base, City, User

    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "spell_list_test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)

    db = SessionLocal()
    try:
        db.add(User(USERID=1, USERNAME="t", PASSWORD="x"))
        db.commit()
        db.add(City(CITYID=1, USERID=1, CITY_SLUG="c", UI_PORT=3000, API_PORT=8000))
        db.commit()
        db.add(AI(
            AIID="air", HOME_CITYID=1, AINAME="Air",
            DEFAULT_MODEL="claude-opus-4-7", SPELL_ENABLED=1,
        ))
        db.commit()
    finally:
        db.close()

    class FakeManager:
        pass

    manager = FakeManager()
    manager.SessionLocal = SessionLocal

    def _cleanup():
        engine.dispose()
        gc.collect()
        try:
            tmpdir.cleanup()
        except PermissionError:
            pass

    request.addfinalizer(_cleanup)
    return manager


@pytest.fixture
def ctx(isolated_manager):
    return LineHeadInput(
        persona_id="air", line_id="main", line_role="main_line",
        model_key="claude-opus-4-7", current_building_id="b_lobby",
        manager=isolated_manager,
    )


def _make_schema(name, **kwargs):
    return ToolSchema(
        name=name,
        description=kwargs.pop("description", f"{name} desc"),
        parameters=kwargs.pop("parameters", {"type": "object", "properties": {}}),
        result_type=kwargs.pop("result_type", "string"),
        spell=kwargs.pop("spell", True),
        spell_display_name=kwargs.pop("spell_display_name", name + " 表示名"),
        spell_visible=kwargs.pop("spell_visible", True),
        addon_name=kwargs.pop("addon_name", None),
    )


@pytest.fixture
def fake_spell_registry():
    """SPELL_TOOL_SCHEMAS を一時的に差し替える fixture。"""
    import tools as tools_module

    saved = tools_module.SPELL_TOOL_SCHEMAS.copy()
    tools_module.SPELL_TOOL_SCHEMAS.clear()
    yield tools_module.SPELL_TOOL_SCHEMAS
    tools_module.SPELL_TOOL_SCHEMAS.clear()
    tools_module.SPELL_TOOL_SCHEMAS.update(saved)


def test_capture_returns_disabled_snapshot_when_spell_off(ctx, isolated_manager):
    from database.models import AI

    db = isolated_manager.SessionLocal()
    try:
        db.query(AI).filter_by(AIID="air").update({"SPELL_ENABLED": 0})
        db.commit()
    finally:
        db.close()

    section = SpellListSection()
    snapshot = section.capture(ctx)
    assert snapshot.enabled is False
    assert snapshot.entries == ()


def test_capture_collects_builtin_and_addon_spells(ctx, fake_spell_registry):
    fake_spell_registry["builtin_a"] = _make_schema("builtin_a")
    fake_spell_registry["builtin_b"] = _make_schema("builtin_b", spell_visible=False)
    fake_spell_registry["addonX__tool1"] = _make_schema(
        "addonX__tool1", addon_name="addonX",
    )
    fake_spell_registry["addonY__tool2"] = _make_schema(
        "addonY__tool2", addon_name="addonY", spell_visible=False,
    )

    with patch("tools.mcp_client.get_mcp_manager", side_effect=Exception("no MCP")):
        section = SpellListSection()
        snapshot = section.capture(ctx)

    assert snapshot.enabled is True
    names = sorted(e.name for e in snapshot.entries)
    assert names == ["addonX__tool1", "addonY__tool2", "builtin_a", "builtin_b"]
    addon_keys = sorted(m.addon_key for m in snapshot.addon_manifests)
    assert addon_keys == ["addonX", "addonY"]


def test_capture_respects_mcp_per_persona_filter(ctx, fake_spell_registry):
    fake_spell_registry["mcp_hidden"] = _make_schema(
        "mcp_hidden", addon_name="mcpaddon",
    )
    fake_spell_registry["builtin_visible"] = _make_schema("builtin_visible")

    class FakeMCP:
        def is_tool_available_for_persona(self, name, persona_id, building_id=None):
            return name != "mcp_hidden"

    with patch("tools.mcp_client.get_mcp_manager", return_value=FakeMCP()):
        snapshot = SpellListSection().capture(ctx)

    names = sorted(e.name for e in snapshot.entries)
    assert names == ["builtin_visible"]


def test_capture_respects_availability_check(ctx, fake_spell_registry):
    fake_spell_registry["gated"] = _make_schema("gated")
    fake_spell_registry["gated"].availability_check = lambda pid: False
    fake_spell_registry["open"] = _make_schema("open")

    with patch("tools.mcp_client.get_mcp_manager", side_effect=Exception("no MCP")):
        snapshot = SpellListSection().capture(ctx)

    names = sorted(e.name for e in snapshot.entries)
    assert names == ["open"]


def test_render_disabled_snapshot():
    snap = SpellListSnapshot(enabled=False, entries=(), addon_manifests=())
    rendered = SpellListSection().render(snap)
    assert "スペルは現在使用できません" in rendered.text


def test_render_builtin_and_addon_groups():
    snap = SpellListSnapshot(
        enabled=True,
        entries=(
            SpellEntry(
                name="builtin_a", display_name="A 表示",
                description="A の説明", parameters_json="{}",
                addon_key=None, visible=True,
            ),
            SpellEntry(
                name="addonX__t", display_name="アドオン X t",
                description="X 説明", parameters_json="{}",
                addon_key="addonX", visible=True,
            ),
            SpellEntry(
                name="addonX__hidden", display_name="",
                description="hidden", parameters_json="{}",
                addon_key="addonX", visible=False,
            ),
        ),
        addon_manifests=(
            AddonManifest(addon_key="addonX", display_name="アドオンX", description="desc"),
        ),
    )
    rendered = SpellListSection().render(snap)
    text = rendered.text
    assert "## スペル" in text
    assert "**builtin_a** (A 表示): A の説明" in text
    assert "**アドオンX**" in text
    assert "**addonX__t** (アドオン X t): X 説明" in text
    assert "追加スペル1個あり" in text


def test_diff_detects_added_and_removed_visible_spells():
    section = SpellListSection()
    old = SpellListSnapshot(
        enabled=True,
        entries=(
            SpellEntry(name="a", display_name="A", description="", parameters_json="{}", addon_key=None, visible=True),
            SpellEntry(name="b", display_name="B", description="", parameters_json="{}", addon_key=None, visible=True),
        ),
        addon_manifests=(),
    )
    new = SpellListSnapshot(
        enabled=True,
        entries=(
            SpellEntry(name="a", display_name="A", description="", parameters_json="{}", addon_key=None, visible=True),
            SpellEntry(name="c", display_name="C", description="", parameters_json="{}", addon_key=None, visible=True),
        ),
        addon_manifests=(),
    )
    labels = section.diff_to_notifications(old, new)
    kinds = sorted((label.kind, label.label) for label in labels)
    assert any(k == "spell_added" and "c" in v for k, v in kinds)
    assert any(k == "spell_removed" and "b" in v for k, v in kinds)


def test_added_notification_carries_schema():
    """付与通知はスキーマ込み (まはー裁定 2026-09-04)。

    head のスペル一覧は次の Metabolism まで凍結されるので、移動直後のペルソナに
    とってこの通知が新しいスペルの唯一の情報源になる。名前だけでは引数が分からず
    唱えられない。
    """
    section = SpellListSection()
    old = SpellListSnapshot(enabled=True, entries=(), addon_manifests=())
    new = SpellListSnapshot(
        enabled=True,
        entries=(
            SpellEntry(
                name="post_message", display_name="投稿",
                description="掲示板に書き込む",
                parameters_json=(
                    '{"type": "object", '
                    '"properties": {"body": {"type": "string", "description": "本文"}, '
                    '"draft": {"type": "boolean", "description": "下書きにする"}}, '
                    '"required": ["body"]}'
                ),
                addon_key=None, visible=True,
            ),
        ),
        addon_manifests=(),
    )
    labels = section.diff_to_notifications(old, new)
    assert [label.kind for label in labels] == ["spell_added"]
    text = labels[0].label
    assert "スペル 投稿 (post_message) が使えるようになりました" in text
    assert "掲示板に書き込む" in text
    # 引数の形 (名前・型・必須か・説明) が載っていること
    assert "body (string, 必須): 本文" in text
    assert "draft (boolean, 省略可): 下書きにする" in text


def test_removed_notification_is_name_only():
    """剥奪通知は名前だけ — 唱えられなくなったものに引数の形は要らない。"""
    section = SpellListSection()
    entry = SpellEntry(
        name="post_message", display_name="投稿", description="掲示板に書き込む",
        parameters_json=(
            '{"type": "object", '
            '"properties": {"body": {"type": "string", "description": "本文"}}}'
        ),
        addon_key=None, visible=True,
    )
    old = SpellListSnapshot(enabled=True, entries=(entry,), addon_manifests=())
    new = SpellListSnapshot(enabled=True, entries=(), addon_manifests=())
    labels = section.diff_to_notifications(old, new)
    assert [label.kind for label in labels] == ["spell_removed"]
    assert labels[0].label == "スペル 投稿 (post_message) が使えなくなりました"


def test_diff_notifies_when_spell_system_toggles():
    section = SpellListSection()
    old = SpellListSnapshot(enabled=False, entries=(), addon_manifests=())
    new = SpellListSnapshot(enabled=True, entries=(), addon_manifests=())
    labels = section.diff_to_notifications(old, new)
    assert len(labels) == 1
    assert labels[0].kind == "spell_system_enabled"

    labels = section.diff_to_notifications(new, old)
    assert len(labels) == 1
    assert labels[0].kind == "spell_system_disabled"


def test_stale_spell_set_raises_snapshot_stale_error():
    """スペルセット変化の失効は SnapshotStaleError で表明する。

    store 側はこの型を破損 (ERROR+traceback) と区別して INFO で扱う —
    想定内の再 capture 経路が error 面を汚さないための契約 (2026-07-19)。
    """
    from sea.head_pipeline.types import SnapshotStaleError

    section = SpellListSection()
    snap = SpellListSnapshot(
        enabled=True,
        entries=(),
        addon_manifests=(),
        registered_names=frozenset({"__definitely_not_a_live_spell__"}),
    )
    data = section.serialize_snapshot(snap)
    with pytest.raises(SnapshotStaleError):
        section.deserialize_snapshot(data)
    # 後方互換: ValueError を期待していた既存の呼び手も壊れない
    assert issubclass(SnapshotStaleError, ValueError)


def test_missing_per_persona_mcp_tools_are_not_stale():
    """per_persona MCP ツールが live 登録簿に無いだけでは失効にしない (§I)。

    per_persona のツール登録は Pulse 頭の本人取得で行われるため、再起動直後は
    「まだ誰も取得していない」だけで消えたわけではない。ここで失効にすると
    Pulse 前の head 構築 (プレビュー等) が欠損再 capture を起こして A がツール
    未取得の姿になり、保存済み B との比較で「全ツール消滅→取得時に再出現」の
    偽差分が知覚へ流れ込む。
    """
    from tools import SPELL_TOOL_SCHEMAS
    from tools.mcp_client import MCPClientManager

    mgr = MCPClientManager()
    mgr._server_meta["saiverse-elyth-addon__elyth"] = {
        "scope": "per_persona", "raw_config": {},
    }

    section = SpellListSection()
    # 実運用の形: 前セッションの保存 = 現ライブの全スペル + per_persona ツール。
    # 再起動直後は per_persona 分だけがライブ登録簿から欠けている。
    stored = frozenset(SPELL_TOOL_SCHEMAS.keys()) | {
        "saiverse-elyth-addon__elyth__create_post"
    }
    snap = SpellListSnapshot(
        enabled=True,
        entries=(),
        addon_manifests=(),
        registered_names=stored,
    )
    data = section.serialize_snapshot(snap)
    with patch("tools.mcp_client.get_mcp_manager", return_value=mgr):
        restored = section.deserialize_snapshot(data)  # raise しない
    assert restored.registered_names == snap.registered_names


def test_missing_non_per_persona_tools_still_stale():
    """per_persona 以外の名前が消えている場合は従来どおり失効 (再 capture)。"""
    from sea.head_pipeline.types import SnapshotStaleError
    from tools import SPELL_TOOL_SCHEMAS
    from tools.mcp_client import MCPClientManager

    mgr = MCPClientManager()
    mgr._server_meta["saiverse-elyth-addon__elyth"] = {
        "scope": "per_persona", "raw_config": {},
    }

    section = SpellListSection()
    stored = frozenset(SPELL_TOOL_SCHEMAS.keys()) | {
        "saiverse-elyth-addon__elyth__create_post",
        "__gone_native_spell__",
    }
    snap = SpellListSnapshot(
        enabled=True,
        entries=(),
        addon_manifests=(),
        registered_names=stored,
    )
    data = section.serialize_snapshot(snap)
    with patch("tools.mcp_client.get_mcp_manager", return_value=mgr):
        with pytest.raises(SnapshotStaleError):
            section.deserialize_snapshot(data)


def test_serialize_deserialize_roundtrip():
    section = SpellListSection()
    snap = SpellListSnapshot(
        enabled=True,
        entries=(
            SpellEntry(name="a", display_name="A", description="d", parameters_json='{"type":"object"}', addon_key=None, visible=True),
            SpellEntry(name="addon__x", display_name="X", description="", parameters_json="{}", addon_key="addon", visible=False),
        ),
        addon_manifests=(
            AddonManifest(addon_key="addon", display_name="アドオン", description="説明"),
        ),
    )
    data = section.serialize_snapshot(snap)
    restored = section.deserialize_snapshot(data)
    assert restored == snap
