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


def test_capture_all_preserves_diff_baseline(pipeline, ctx, registry):
    """capture_all は B (既読基準) に触らない — B は配送だけが進める。

    回帰 (2026-08-17 実運用、まはー裁定): エリスの入退室通知が 1 ヶ月間全滅
    していた。Pulse 頭の anchor TTL 切れで capture_all が B を「今の状態」に
    上書きし、直後の flush_diffs が「差分なし」になっていた — 休止中に起きた
    変化 (ユーザーの入退室等) が通知されないまま既読化される。同じ握り潰しは
    Metabolism 発火・手動の記憶整理の capture_all でも起きるため、リセット
    挙動そのものを撤去した。
    """
    pipeline.capture_all(ctx)                       # A = B = spell_a, spell_b
    spell_section = registry.by_name("spell_list")
    spell_section.live_spells = ["spell_a"]         # 未通知のまま spell_b が消えた
    pipeline.capture_all(ctx)                       # TTL 切れ / Metabolism 相当
    labels = pipeline.flush_diffs(ctx, all_sections=True)
    assert [label.kind for label in labels] == ["spell_removed"]
    # 届けた後は B が前進し、再通知しない
    assert pipeline.flush_diffs(ctx, all_sections=True) == []


def test_capture_all_initializes_baseline_for_new_sections(ctx):
    """既存 B の無い Section (初回 / 新規登録) は B = 新 A で初期化する。

    (新規登録 Section が「初回取得ぶん全部」を差分として通知するスパムを防ぐ、
    capture_for_event / recapture_missing の復帰時と同じ規約。)
    """
    r = HeadSectionRegistry()
    r.register(BuildingSection())
    pipeline = HeadPipeline(registry=r)
    pipeline.capture_all(ctx)
    r.register(SpellListSection())                  # 途中から Section が増えた
    pipeline.capture_all(ctx)
    # 新規 spell_list は初期化済み = 全スペルを「増えた」と通知しない
    assert pipeline.flush_diffs(ctx, all_sections=True) == []


def test_metabolism_dispatch_preserves_diff_baseline(pipeline, ctx, registry):
    """METABOLISM dispatch (= capture_all) 後も未通知の差分は次の flush で届く。

    旧挙動は「B を A にリセット (通知の窓をリスタート)」で、Metabolism の発火
    タイミング次第で Pulse 中の入退室等が通知されずに消えていた (2026-08-17
    まはー裁定で撤去)。
    """
    pipeline.capture_all(ctx)
    spell_section = registry.by_name("spell_list")
    spell_section.live_spells = ["spell_a"]         # 未通知の変化
    pipeline.dispatch_event(ctx, EventType.METABOLISM)
    labels = pipeline.flush_diffs(ctx, all_sections=True)
    assert [label.kind for label in labels] == ["spell_removed"]


def test_capture_for_event_preserves_existing_baseline(pipeline, ctx, registry):
    """refresh event の再 capture も既存 B を据え置く (Codex 2026-08-17 high)。

    capture_all 後・配送前に refresh event が割り込むと、旧実装は該当 Section の
    B を新値へ進めて未配送の差分を既読化していた。event 再 capture は A の
    最新化であって配送ではない (C8)。
    """
    pipeline.capture_all(ctx)
    building_section = registry.by_name("building")
    building_section.building_name = "Vessel"       # 未通知の変化
    pipeline.dispatch_event(ctx, EventType.BUILDING_ENTERED)  # building を再 capture
    labels = pipeline.flush_diffs(ctx, all_sections=True)
    assert [label.kind for label in labels] == ["building_renamed"]


def test_recapture_missing_preserves_existing_baseline(pipeline, ctx, registry):
    """欠損復帰の再 capture は、既存 B を持つ Section の B を上書きしない
    (Codex 2026-08-17 high)。

    A 欠損・B あり (例: store の optional Section serialize 失敗行から復旧) の
    状態から recapture_missing で復帰したとき、故障期間中の未通知差分が次の
    flush で届くこと。B が無い Section だけが「初めて head に載る」初期化を受ける。
    """
    pipeline.capture_all(ctx)                       # A = B = (spell_a, spell_b)
    spell_section = registry.by_name("spell_list")
    spell_section.live_spells = ["spell_a"]         # 故障期間中の変化
    snapshot = pipeline.get_snapshot("air", MODEL)
    snapshot.sections["spell_list"] = None          # A 欠損を再現 (B は残っている)
    pipeline.recapture_missing(ctx, {"spell_list"})
    labels = pipeline.flush_diffs(ctx, all_sections=True)
    assert [label.kind for label in labels] == ["spell_removed"]


def test_capture_all_keeps_baseline_when_section_capture_fails_without_existing(
    ctx, registry,
):
    """capture 失敗で A の key が省かれても、既存 B は落とさない (Codex 2026-08-17)。

    B は「どこまで届けたか」の独立した台帳。A の欠損に巻き込んで消すと、復旧後の
    flush が故障期間中の差分を届けられない。
    """
    pipeline = HeadPipeline(registry=registry)
    spell_section = registry.by_name("spell_list")
    pipeline.capture_all(ctx)                       # A = B = (spell_a, spell_b)

    # A の既存値も消した上で capture を失敗させる → key ごと省かれる経路
    snapshot = pipeline.get_snapshot("air", MODEL)
    snapshot.sections["spell_list"] = None
    original_capture = spell_section.capture
    spell_section.capture = lambda c: (_ for _ in ()).throw(RuntimeError("boom"))
    pipeline.capture_all(ctx)                       # spell_list は capture_failures 行き

    # 復旧。故障期間中に spell_b が消えていた
    spell_section.capture = original_capture
    spell_section.live_spells = ["spell_a"]
    labels = pipeline.flush_diffs(ctx, all_sections=True)
    assert [label.kind for label in labels] == ["spell_removed"]


class _RecordingStore:
    """store.save / save_last_notified が受け取った B を記録する最小フェイク。"""

    def __init__(self):
        self.saved_notified: list = []

    def save(self, snapshot, last_notified_sections):
        self.saved_notified.append(dict(last_notified_sections))
        return True

    def save_last_notified(self, persona_id, model_key, last_notified_sections):
        self.saved_notified.append(dict(last_notified_sections))
        return True

    def load(self, persona_id, model_key):
        return None

    def load_version(self, persona_id, model_key):
        return None


def test_persist_rereads_latest_baseline(ctx, registry):
    """保存は「保存時点の最新 in-memory B」を書く (Codex 2026-08-17 medium)。

    古い B のコピーを抱えた保存が遅れて着地しても、並行配送が進めた B を
    巻き戻さない — _persist_snapshot は引数のコピーでなくロック内で読み直した
    最新値を store へ渡す。
    """
    store = _RecordingStore()
    pipeline = HeadPipeline(registry=registry, store=store)
    snapshot = pipeline.capture_all(ctx)

    # 配送が B を前進させた (advance_last_notified 相当)
    pipeline.advance_last_notified("air", "spell_list", ("delivered",))

    # 遅れて着地する保存: 引数には古い B を渡す
    stale_b = {"spell_list": ("stale",)}
    pipeline._persist_snapshot(snapshot, stale_b)
    assert store.saved_notified[-1]["spell_list"] == ("delivered",)


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
        db.add(City(CITYID=1, USERID=1, CITY_SLUG="test_city", UI_PORT=3000, API_PORT=8000))
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
