"""Tests for sea.head_pipeline (registry / pipeline / store) skeleton.

Section の Protocol 適合チェック、order 順 render、dirty + diff 通知、
refresh_on_events による部分 capture、DB store の roundtrip を確認する。
"""
import gc
import json
import tempfile
from pathlib import Path

import pytest

from sea.head_pipeline import (
    EventType,
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    LineHeadSnapshotStore,
    NotificationLabel,
    RenderedSection,
)


class SpellListSection:
    name = "spell_list"
    order = 600
    refresh_on_events = frozenset({EventType.ADDON_LOADED, EventType.ADDON_UNLOADED})

    def __init__(self):
        self.live_spells = ["spell_a", "spell_b"]

    def capture(self, ctx):
        return tuple(self.live_spells)

    def render(self, snapshot):
        if not snapshot:
            return None
        return RenderedSection(text=f"## スペル\n{', '.join(snapshot)}")

    def diff_to_notifications(self, old, new):
        old_set = set(old or ())
        new_set = set(new or ())
        labels = []
        for s in sorted(new_set - old_set):
            labels.append(NotificationLabel(kind="spell_added", label=f"スペル {s} が使えるようになりました"))
        for s in sorted(old_set - new_set):
            labels.append(NotificationLabel(kind="spell_removed", label=f"スペル {s} が使えなくなりました"))
        return labels

    def serialize_snapshot(self, snapshot):
        return json.dumps(list(snapshot or ()))

    def deserialize_snapshot(self, data):
        return tuple(json.loads(data))


class BuildingSection:
    name = "building"
    order = 300
    refresh_on_events = frozenset({EventType.BUILDING_ENTERED, EventType.SYSTEM_PROMPT_EDITED})

    def __init__(self):
        self.building_name = "Lobby"

    def capture(self, ctx):
        return {"name": self.building_name, "id": ctx.current_building_id}

    def render(self, snapshot):
        if not snapshot:
            return None
        return RenderedSection(text=f"## Building: {snapshot.get('name')}")

    def diff_to_notifications(self, old, new):
        if not old or not new:
            return []
        if old.get("name") != new.get("name"):
            return [NotificationLabel(kind="building_renamed", label=f"Building 名が {old.get('name')} -> {new.get('name')} に変わりました")]
        return []

    def serialize_snapshot(self, snapshot):
        return json.dumps(snapshot or {})

    def deserialize_snapshot(self, data):
        return json.loads(data)


class EmptySection:
    """capture はするが render は常に None (= dynamic_state 系 / 未設定の任意 Section)。

    snapshot には載るが head には描画されない。order を building と spell_list の
    間に置き、render_head の戻り値が「名前付き」で位置非依存であることを検証する。
    """
    name = "empty"
    order = 400  # building(300) < empty(400) < spell_list(600)
    refresh_on_events = frozenset()

    def capture(self, ctx):
        return {"present": True}

    def render(self, snapshot):
        return None  # head には何も出さない

    def diff_to_notifications(self, old, new):
        return []

    def serialize_snapshot(self, snapshot):
        return json.dumps(snapshot or {})

    def deserialize_snapshot(self, data):
        return json.loads(data)


class IncompleteSection:
    name = "broken"
    order = 0
    # capture / render / diff_to_notifications / serialize / deserialize / refresh_on_events 欠落


@pytest.fixture
def registry():
    r = HeadSectionRegistry()
    r.register(BuildingSection())
    r.register(SpellListSection())
    return r


@pytest.fixture
def pipeline(registry):
    return HeadPipeline(registry=registry)


MODEL = "claude-opus-4-7"


@pytest.fixture
def ctx():
    return LineHeadInput(
        persona_id="air",
        model_key=MODEL,
        current_building_id="b_lobby",
    )


def test_registry_rejects_incomplete_section():
    r = HeadSectionRegistry()
    with pytest.raises(TypeError):
        r.register(IncompleteSection())


def test_registry_rejects_duplicate_name():
    r = HeadSectionRegistry()
    r.register(SpellListSection())
    with pytest.raises(ValueError):
        r.register(SpellListSection())


def test_all_sections_ordered_by_order_field(registry):
    names = [s.name for s in registry.all_sections()]
    assert names == ["building", "spell_list"]  # 300 < 600


def test_sections_for_event_filters_by_refresh_on_events(registry):
    matched = registry.sections_for_event(EventType.BUILDING_ENTERED)
    assert [s.name for s in matched] == ["building"]
    matched = registry.sections_for_event(EventType.ADDON_LOADED)
    assert [s.name for s in matched] == ["spell_list"]
    matched = registry.sections_for_event(EventType.METABOLISM)
    # METABOLISM は明示宣言不要 (= sections_for_event でフィルタしても 0 件)
    assert matched == []


def test_capture_all_creates_snapshot_for_all_sections(pipeline, ctx):
    snapshot = pipeline.capture_all(ctx)
    assert set(snapshot.sections.keys()) == {"spell_list", "building"}
    assert snapshot.snapshot_version == 1
    assert snapshot.persona_id == "air"
    assert snapshot.model_key == MODEL


def test_render_head_outputs_in_order(pipeline, ctx):
    pipeline.capture_all(ctx)
    rendered = pipeline.render_head("air", MODEL)
    assert len(rendered) == 2
    assert rendered[0][0] == "building"
    assert rendered[0][1].text.startswith("## Building:")
    assert rendered[1][0] == "spell_list"
    assert rendered[1][1].text.startswith("## スペル")


def test_render_head_carries_names_past_none_section(ctx):
    """render が None のセクションを間に挟んでも、後続セクションの名前がズレない。

    回帰: 旧実装は render_head が名前無しの RenderedSection 列を返し、呼び出し側が
    order 順の位置から名前を復元していた。間に None render セクションがあると後続が
    1 つズレ、enabled フィルタが別名で評価されて内容が欠落していた (2026-06-29)。
    """
    r = HeadSectionRegistry()
    r.register(BuildingSection())      # order 300, renders text
    r.register(EmptySection())         # order 400, renders None
    r.register(SpellListSection())     # order 600, renders text
    pipeline = HeadPipeline(registry=r)
    pipeline.capture_all(ctx)

    rendered = pipeline.render_head("air", MODEL)
    # empty は除外され、building / spell_list が正しい名前で残る
    assert [name for name, _ in rendered] == ["building", "spell_list"]
    by_name = dict(rendered)
    assert by_name["building"].text.startswith("## Building:")
    assert by_name["spell_list"].text.startswith("## スペル")


def test_render_head_returns_empty_when_no_snapshot(pipeline):
    rendered = pipeline.render_head("nobody", MODEL)
    assert rendered == []


def test_flush_diffs_returns_empty_when_no_dirty(pipeline, ctx):
    pipeline.capture_all(ctx)
    labels = pipeline.flush_diffs(ctx)
    assert labels == []


def test_flush_diffs_detects_change_after_mark_dirty(pipeline, ctx, registry):
    pipeline.capture_all(ctx)
    spell_section = registry.by_name("spell_list")
    spell_section.live_spells = ["spell_a", "spell_c"]
    pipeline.mark_dirty("air", MODEL, "spell_list")
    labels = pipeline.flush_diffs(ctx)
    kinds = sorted(label.kind for label in labels)
    assert kinds == ["spell_added", "spell_removed"]


def test_flush_diffs_does_not_double_notify(pipeline, ctx, registry):
    pipeline.capture_all(ctx)
    spell_section = registry.by_name("spell_list")
    spell_section.live_spells = ["spell_a", "spell_c"]
    pipeline.mark_dirty("air", MODEL, "spell_list")
    first = pipeline.flush_diffs(ctx)
    assert first  # 1 回目は出る
    pipeline.mark_dirty("air", MODEL, "spell_list")  # dirty 再マーク
    second = pipeline.flush_diffs(ctx)
    assert second == []  # 内容変わってないので 2 回目は空


def test_capture_for_event_only_refreshes_targeted_sections(pipeline, ctx, registry):
    pipeline.capture_all(ctx)
    spell_section = registry.by_name("spell_list")
    building_section = registry.by_name("building")

    spell_section.live_spells = ["should_not_appear"]
    building_section.building_name = "Vessel"

    new_snapshot = pipeline.capture_for_event(ctx, EventType.BUILDING_ENTERED)
    assert new_snapshot is not None
    # building は再 capture、spell_list は据え置き
    assert new_snapshot.sections["building"] == {"name": "Vessel", "id": "b_lobby"}
    assert new_snapshot.sections["spell_list"] == ("spell_a", "spell_b")


def test_capture_for_event_metabolism_recaptures_everything(pipeline, ctx, registry):
    pipeline.capture_all(ctx)
    spell_section = registry.by_name("spell_list")
    building_section = registry.by_name("building")

    spell_section.live_spells = ["new_spell"]
    building_section.building_name = "NewBuilding"

    new_snapshot = pipeline.capture_for_event(ctx, EventType.METABOLISM)
    assert new_snapshot.sections["spell_list"] == ("new_spell",)
    assert new_snapshot.sections["building"]["name"] == "NewBuilding"


def test_discard_session_removes_state(pipeline, ctx):
    pipeline.capture_all(ctx)
    assert pipeline.has_snapshot("air", MODEL)
    pipeline.discard_session("air", MODEL)
    assert not pipeline.has_snapshot("air", MODEL)


def test_serialize_deserialize_roundtrip_per_section(registry):
    spell = registry.by_name("spell_list")
    snapshot = ("a", "b", "c")
    data = spell.serialize_snapshot(snapshot)
    restored = spell.deserialize_snapshot(data)
    assert restored == snapshot

    building = registry.by_name("building")
    bsnap = {"name": "Lobby", "id": "b_lobby"}
    bdata = building.serialize_snapshot(bsnap)
    brestored = building.deserialize_snapshot(bdata)
    assert brestored == bsnap


# ---- DB store roundtrip ----


@pytest.fixture
def isolated_db_session_factory(request):
    """テンポラリ SQLite で AI + SessionHeadSnapshot テーブルを作って session_factory を返す。

    cleanup は addCleanup 相当で LIFO に走らせる (tests/conftest 既存原則と整合)。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import AI, Base, City, User

    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "head_pipeline_test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)

    # FK 制約を満たすため User -> City -> AI の順で 1 行ずつ入れる
    db = SessionLocal()
    try:
        db.add(User(USERID=1, USERNAME="tester", PASSWORD="x"))
        db.commit()
        db.add(City(CITYID=1, USERID=1, CITYNAME="test_city", UI_PORT=3000, API_PORT=8000))
        db.commit()
        db.add(AI(AIID="air", HOME_CITYID=1, AINAME="Air", DEFAULT_MODEL="claude-opus-4-7"))
        db.commit()
    finally:
        db.close()

    def _cleanup():
        engine.dispose()
        gc.collect()
        try:
            tmpdir.cleanup()
        except PermissionError:
            pass

    request.addfinalizer(_cleanup)
    return SessionLocal


def test_store_save_and_load_roundtrip(registry, isolated_db_session_factory):
    store = LineHeadSnapshotStore(
        session_factory=isolated_db_session_factory, registry=registry,
    )
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(
        persona_id="air", model_key=MODEL, current_building_id="b_lobby",
    )
    pipeline.capture_all(ctx)

    # 新しい pipeline を作って load -> snapshot が復元できる
    pipeline2 = HeadPipeline(registry=registry, store=store)
    assert pipeline2.load_from_store("air", MODEL)
    snapshot = pipeline2.get_snapshot("air", MODEL)
    assert snapshot is not None
    assert snapshot.persona_id == "air"
    assert snapshot.model_key == MODEL
    assert set(snapshot.sections.keys()) == {"spell_list", "building"}


def test_store_save_after_capture_for_event(registry, isolated_db_session_factory):
    store = LineHeadSnapshotStore(
        session_factory=isolated_db_session_factory, registry=registry,
    )
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(
        persona_id="air", model_key=MODEL, current_building_id="b_lobby",
    )
    pipeline.capture_all(ctx)

    registry.by_name("building").building_name = "Vessel"
    pipeline.capture_for_event(ctx, EventType.BUILDING_ENTERED)

    pipeline2 = HeadPipeline(registry=registry, store=store)
    pipeline2.load_from_store("air", MODEL)
    snapshot = pipeline2.get_snapshot("air", MODEL)
    assert snapshot.sections["building"]["name"] == "Vessel"


def test_store_save_last_notified_after_flush_diffs(registry, isolated_db_session_factory):
    store = LineHeadSnapshotStore(
        session_factory=isolated_db_session_factory, registry=registry,
    )
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(
        persona_id="air", model_key=MODEL, current_building_id="b_lobby",
    )
    pipeline.capture_all(ctx)

    registry.by_name("spell_list").live_spells = ["spell_a", "spell_c"]
    pipeline.mark_dirty("air", MODEL, "spell_list")
    labels = pipeline.flush_diffs(ctx)
    assert labels  # 1 度通知が走る

    # 別 pipeline で load して、再 flush しても通知が出ない (= B が永続化されている)
    pipeline2 = HeadPipeline(registry=registry, store=store)
    pipeline2.load_from_store("air", MODEL)
    pipeline2.mark_dirty("air", MODEL, "spell_list")
    labels2 = pipeline2.flush_diffs(ctx)
    assert labels2 == []


def test_dispatch_event_metabolism_recaptures_all(pipeline, ctx, registry):
    pipeline.capture_all(ctx)
    registry.by_name("spell_list").live_spells = ["new"]
    snap = pipeline.dispatch_event(ctx, EventType.METABOLISM)
    assert snap is not None
    assert snap.sections["spell_list"] == ("new",)


def test_dispatch_event_refresh_on_match(pipeline, ctx, registry):
    pipeline.capture_all(ctx)
    registry.by_name("building").building_name = "Refreshed"
    snap = pipeline.dispatch_event(ctx, EventType.BUILDING_ENTERED)
    assert snap is not None
    assert snap.sections["building"]["name"] == "Refreshed"
    # spell_list は building_entered に refresh しないので据え置き
    assert snap.sections["spell_list"] == ("spell_a", "spell_b")


def test_dispatch_event_unmatched_marks_dirty(pipeline, ctx, registry):
    pipeline.capture_all(ctx)
    # MODEL_CHANGED は今登録されてる Section どちらも refresh_on_events に含めてない
    snap = pipeline.dispatch_event(ctx, EventType.MODEL_CHANGED)
    assert snap is None  # snapshot は据え置き

    # ただし dirty マークは付くので、live を変えて flush_diffs すると通知される
    registry.by_name("spell_list").live_spells = ["spell_a", "spell_c"]
    labels = pipeline.flush_diffs(ctx)
    kinds = sorted(label.kind for label in labels)
    assert kinds == ["spell_added", "spell_removed"]


def test_store_delete_via_discard_session(registry, isolated_db_session_factory):
    store = LineHeadSnapshotStore(
        session_factory=isolated_db_session_factory, registry=registry,
    )
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(
        persona_id="air", model_key=MODEL, current_building_id="b_lobby",
    )
    pipeline.capture_all(ctx)
    pipeline.discard_session("air", MODEL, delete_persisted=True)

    pipeline2 = HeadPipeline(registry=registry, store=store)
    assert not pipeline2.load_from_store("air", MODEL)
