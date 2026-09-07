"""部屋替えのときの同席者は「配送しないラベル」になる (2026-09-07)。

固定する仕様 (docs/issues/perception_state_pushed_at_event_time.md):

1. 部屋が変わった回の occupants の diff は ``deliver=False`` の occupant_entered
   ラベルを返す (文面と metadata は従来どおり — ログに残す)。
2. 台帳経路: deliver=False だけの回は outbox に積まれず、基準 (last_notified) は
   進む。以後は新しい部屋の顔ぶれとの比較になるので、同じ部屋での入退室が正しく
   出る。
3. 直接経路 (台帳なし): deliver=False は push されず、基準は進む。
4. 再会の想起はこの検知器からは発火しない — 発火点は Pulse の頭の同席チェック
   (``integration.inject_copresence_recall``、tests/test_copresence_recall.py)。
5. 入室 hook は在室者も検知の対象にする — 移動した本人が Pulse を打つ前に誰かが
   同じ部屋へ入ってきても、それが「入室しました」として届く。
6. コンテキストプレビューは Pulse の頭と同じ検知を**読み取り専用**で走らせる —
   まだ知覚バッファに溜まっていない差分も映り、基準 (last_notified) も知覚
   バッファも動かさない (2026-09-07 直し方 8)。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, ExecutionOutboxItem
from saiverse.execution_ledger import ExecutionLedger
from sea.head_pipeline import (
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    inject_diff_notifications,
)
from sea.head_pipeline import integration
from sea.head_pipeline.sections.building_occupants import (
    BuildingOccupantsSection,
    BuildingOccupantsSnapshot,
    OccupantEntry,
)

PERSONA_ID = "air"
MODEL = "claude-opus-4-7"
ROOM_A = "b_air_room"
ROOM_B = "b_elis_room"


# ---------------------------------------------------------------------------
# 1. 部屋替えの diff は配送しないラベル
# ---------------------------------------------------------------------------


def test_room_change_yields_silent_occupant_labels():
    section = BuildingOccupantsSection()
    old = BuildingOccupantsSnapshot(building_id=ROOM_A, entries=())
    new = BuildingOccupantsSnapshot(
        building_id=ROOM_B,
        entries=(OccupantEntry(occupant_id="elis", name="エリス", kind="persona"),),
    )
    labels = section.diff_to_notifications(old, new)
    assert len(labels) == 1
    assert labels[0].kind == "occupant_entered"
    assert labels[0].deliver is False
    # 文面と metadata は従来どおり (ログに残す)
    assert labels[0].label == "エリス がいます"
    assert labels[0].metadata == {"occupant_id": "elis", "occupant_kind": "persona"}


def test_room_change_into_an_empty_room_still_yields_one_silent_label():
    """無人の部屋へ移った回もラベルを 1 件返す (基準を進めるため)。"""
    section = BuildingOccupantsSection()
    old = BuildingOccupantsSnapshot(
        building_id=ROOM_A,
        entries=(OccupantEntry(occupant_id="elis", name="エリス", kind="persona"),),
    )
    new = BuildingOccupantsSnapshot(building_id=ROOM_B, entries=())
    labels = section.diff_to_notifications(old, new)
    assert len(labels) == 1
    assert labels[0].deliver is False
    assert labels[0].kind == "occupants_baseline"


def test_same_room_entry_and_exit_are_still_delivered():
    section = BuildingOccupantsSection()
    old = BuildingOccupantsSnapshot(
        building_id=ROOM_A,
        entries=(OccupantEntry(occupant_id="elis", name="エリス", kind="persona"),),
    )
    new = BuildingOccupantsSnapshot(
        building_id=ROOM_A,
        entries=(OccupantEntry(occupant_id="aifi", name="アイフィ", kind="persona"),),
    )
    labels = section.diff_to_notifications(old, new)
    assert sorted((label.kind, label.deliver) for label in labels) == [
        ("occupant_entered", True), ("occupant_left", True),
    ]


# ---------------------------------------------------------------------------
# 配送経路 (台帳あり / なし) の共通の足場
# ---------------------------------------------------------------------------


class _FakeManager:
    """BuildingOccupantsSection.capture が読む面だけの替え玉。"""

    def __init__(self, occupants, ledger=None):
        self.occupants = occupants
        self.personas = {"elis": object(), "aifi": object()}
        self.id_to_name_map = {"elis": "エリス", "aifi": "アイフィ"}
        if ledger is not None:
            self.execution_ledger = ledger


class _FakeMemory:
    def __init__(self, ready=True):
        self.pushed = []
        self.ready = ready

    def is_ready(self):
        return self.ready

    def push_perception(self, kind, content, **kwargs):
        self.pushed.append((kind, content))


def _pipeline():
    registry = HeadSectionRegistry()
    registry.register(BuildingOccupantsSection())
    return HeadPipeline(registry=registry)


def _ctx(building_id, manager):
    return LineHeadInput(
        persona_id=PERSONA_ID, model_key=MODEL,
        current_building_id=building_id, manager=manager,
    )


def _persona(sai_mem, *, recall_text=None):
    history_manager = None
    if recall_text is not None:
        history_manager = SimpleNamespace(
            should_recall_persona=lambda occupant_id, target_kind=None: True,
            recall_conversation_with=lambda occupant_id, **kwargs: recall_text,
        )
    return SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, current_building_id=ROOM_B,
        sai_memory=sai_mem, history_manager=history_manager,
        id_to_name_map={"elis": "エリス", "aifi": "アイフィ"},
    )


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _outbox_payloads(session_factory):
    db = session_factory()
    try:
        rows = (
            db.query(ExecutionOutboxItem)
            .order_by(ExecutionOutboxItem.OUTBOX_ID.asc())
            .all()
        )
        return [json.loads(row.PAYLOAD_JSON) for row in rows]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. 台帳経路
# ---------------------------------------------------------------------------


def test_ledger_path_skips_delivery_but_advances_baseline(session_factory):
    ledger = ExecutionLedger(session_factory=session_factory)
    occupants = {ROOM_A: [], ROOM_B: ["elis"]}
    manager = _FakeManager(occupants, ledger=ledger)
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_A, manager))       # 移動前 (エリスの部屋)
    sai_mem = _FakeMemory()
    persona = _persona(sai_mem)

    # 1) 部屋替え: 同席者は文にならない (台帳の execution ごと作らない)
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is False
    assert _outbox_payloads(session_factory) == []

    # 2) 基準は新しい部屋まで進んでいる → 同じ部屋での入室は届く
    occupants[ROOM_B] = ["elis", "aifi"]
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    payloads = _outbox_payloads(session_factory)
    assert len(payloads) == 1
    assert payloads[0]["content"] == "アイフィ が入室しました"


def test_ledger_path_delivers_only_the_deliverable_labels(session_factory):
    """deliver=True と deliver=False が混じる回は、届ける分だけ outbox に載る。"""
    ledger = ExecutionLedger(session_factory=session_factory)
    occupants = {ROOM_A: [], ROOM_B: ["elis"]}
    manager = _FakeManager(occupants, ledger=ledger)

    registry = HeadSectionRegistry()
    registry.register(BuildingOccupantsSection())
    registry.register(_ChattySection())
    pipeline = HeadPipeline(registry=registry)
    pipeline.capture_all(_ctx(ROOM_A, manager))
    registry.by_name("chatty").live_text = "変わった"

    persona = _persona(_FakeMemory())
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    payloads = _outbox_payloads(session_factory)
    assert [p["content"] for p in payloads] == ["chatty が変わりました"]

    # silent 側の Section の基準も一緒に進む → 同じ部屋の入室が正しく出る
    occupants[ROOM_B] = ["elis", "aifi"]
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    payloads = _outbox_payloads(session_factory)
    assert [p["content"] for p in payloads][-1] == "アイフィ が入室しました"


def test_ledger_path_moving_through_an_empty_room_keeps_entries_visible(session_factory):
    """人の居る部屋 → 無人の部屋 → そこへ誰かが来る、を通しで検査する。

    無人の部屋でラベルが 0 件になると基準が前の部屋のまま残り、次に誰かが
    入ってきた回が「部屋替え」の比較に化けて入室の知らせが消える。
    """
    ledger = ExecutionLedger(session_factory=session_factory)
    occupants = {ROOM_A: ["elis"], ROOM_B: []}
    manager = _FakeManager(occupants, ledger=ledger)
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_A, manager))       # 移動前 (エリスが居る部屋)
    persona = _persona(_FakeMemory())

    # 1) 無人の部屋へ移動: 届ける文は無い
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is False
    assert _outbox_payloads(session_factory) == []

    # 2) その部屋にアイフィが現れる → 入室として届く
    occupants[ROOM_B] = ["aifi"]
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    payloads = _outbox_payloads(session_factory)
    assert [p["content"] for p in payloads] == ["アイフィ が入室しました"]


def test_ledger_path_silent_labels_advance_the_baseline_even_without_memory(
    session_factory,
):
    """deliver=False だけの回の基準は SAIMemory の状態に関わらず進む。

    このラベルにはもう後段の処理が無い (再会の想起は Pulse の頭の同席チェックへ
    移った、2026-09-07) ので、SAIMemory が未 ready でも基準を据え置く理由が無い。
    据え置くと、その部屋での以後の入退室が古い部屋との比較になって出なくなる。
    """
    ledger = ExecutionLedger(session_factory=session_factory)
    occupants = {ROOM_A: [], ROOM_B: ["elis"]}
    manager = _FakeManager(occupants, ledger=ledger)
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_A, manager))
    sai_mem = _FakeMemory(ready=False)
    persona = _persona(sai_mem)

    # 1) 未 ready の部屋替え: 何も届かない
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is False
    assert sai_mem.pushed == []
    assert _outbox_payloads(session_factory) == []

    # 2) 基準は新しい部屋まで進んでいる → 同じ部屋での入室は届く
    sai_mem.ready = True
    occupants[ROOM_B] = ["elis", "aifi"]
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    assert [
        p["content"] for p in _outbox_payloads(session_factory)
    ] == ["アイフィ が入室しました"]


def test_ledger_path_advances_every_detected_section_in_a_mixed_round(session_factory):
    """配送あり・なしが混じる回でも、検知した Section は一律に基準が進む。"""
    ledger = ExecutionLedger(session_factory=session_factory)
    occupants = {ROOM_A: [], ROOM_B: ["elis"]}
    manager = _FakeManager(occupants, ledger=ledger)

    registry = HeadSectionRegistry()
    registry.register(BuildingOccupantsSection())
    registry.register(_ChattySection())
    pipeline = HeadPipeline(registry=registry)
    pipeline.capture_all(_ctx(ROOM_A, manager))
    registry.by_name("chatty").live_text = "変わった"

    persona = _persona(_FakeMemory())

    # 1) 混在回: 届ける文だけが outbox に載る
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    assert [
        p["content"] for p in _outbox_payloads(session_factory)
    ] == ["chatty が変わりました"]

    # 2) 何も変わらなければ再検出されない (両 Section とも基準が進んでいる)
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is False
    assert [
        p["content"] for p in _outbox_payloads(session_factory)
    ] == ["chatty が変わりました"]


class _ChattySection:
    """deliver=True のラベルを出す最小 Section (混在の回の検査用)。"""

    name = "chatty"
    order = 50
    refresh_on_events = frozenset()

    def __init__(self):
        self.live_text = "初期値"

    def capture(self, ctx):
        return {"text": self.live_text}

    def render(self, snapshot):
        return None

    def diff_to_notifications(self, old, new):
        from sea.head_pipeline import NotificationLabel
        if not old or not new or old.get("text") == new.get("text"):
            return []
        return [NotificationLabel(kind="chatty_changed", label="chatty が変わりました")]

    def serialize_snapshot(self, snapshot):
        return json.dumps(snapshot or {}, ensure_ascii=False)

    def deserialize_snapshot(self, data):
        return json.loads(data)


# ---------------------------------------------------------------------------
# 5. 入室 hook を通した回帰 (移動 → Pulse を挟まずに誰かが入ってくる)
# ---------------------------------------------------------------------------


def test_entry_hook_moves_the_occupant_baseline_to_the_new_room(session_factory):
    """移動した本人が Pulse を打つ前の入室が「入室しました」として届く。

    入室 hook の検知が在室者を対象から外していると、比較の基準が旧部屋の顔ぶれの
    まま残り、その後に誰かが同じ部屋へ入ってきた回まで「部屋替え」の比較
    (deliver=False) に化けて知らせが消える。
    """
    from saiverse import dynamic_state
    from saiverse.dynamic_state import DynamicStateManager
    from sea.head_pipeline import integration

    ledger = ExecutionLedger(session_factory=session_factory)
    occupants = {ROOM_A: [], ROOM_B: ["elis"]}
    manager = _FakeManager(occupants, ledger=ledger)
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_A, manager))       # 移動前 (自分の部屋)
    sai_mem = _FakeMemory()
    persona = _persona(sai_mem)
    persona.persona_dir = None

    # 入室 hook のうち検証するのは検知の一段だけ。部屋の様子の push と head の
    # イベント dispatch は別の経路なので、実物を呼ばずに黙らせる。
    with patch.object(
        integration, "get_default_pipeline", return_value=pipeline,
    ), patch.object(
        dynamic_state, "_dispatch_head_event", return_value=True,
    ), patch(
        "builtin_data.tools.get_visual_context.build_room_bundle",
        return_value=None,
    ):
        DynamicStateManager.on_building_entered(persona, ROOM_B, manager)

    # 同席者は文にならない (「がいます」は復活していない)
    assert _outbox_payloads(session_factory) == []

    # Pulse を挟まずにアイフィが同じ部屋へ現れる → 入室として届く
    occupants[ROOM_B] = ["elis", "aifi"]
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    assert [
        p["content"] for p in _outbox_payloads(session_factory)
    ] == ["アイフィ が入室しました"]


# ---------------------------------------------------------------------------
# 3. 直接経路 (台帳なし)
# ---------------------------------------------------------------------------


def test_direct_path_skips_delivery_but_advances_baseline():
    occupants = {ROOM_A: [], ROOM_B: ["elis"]}
    manager = _FakeManager(occupants)      # execution_ledger なし
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_A, manager))
    sai_mem = _FakeMemory()
    persona = _persona(sai_mem)

    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is False
    assert sai_mem.pushed == []

    occupants[ROOM_B] = ["elis", "aifi"]
    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    assert sai_mem.pushed == [("world_state", "アイフィ が入室しました")]


# ---------------------------------------------------------------------------
# 4. 再会の想起はこの検知器からは発火しない (発火点は Pulse の頭の同席チェック)
# ---------------------------------------------------------------------------


def test_diff_detection_does_not_fire_recall_via_ledger(session_factory):
    ledger = ExecutionLedger(session_factory=session_factory)
    manager = _FakeManager({ROOM_A: [], ROOM_B: ["elis"]}, ledger=ledger)
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_A, manager))
    sai_mem = _FakeMemory()
    persona = _persona(sai_mem, recall_text="[想起: エリスとの過去の会話]")

    inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    )
    assert _outbox_payloads(session_factory) == []       # 文は届けない
    assert sai_mem.pushed == []                          # 想起もここでは積まない


def test_diff_detection_does_not_fire_recall_direct():
    manager = _FakeManager({ROOM_A: [], ROOM_B: ["elis"]})
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_A, manager))
    sai_mem = _FakeMemory()
    persona = _persona(sai_mem, recall_text="[想起: エリスとの過去の会話]")

    inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    )
    assert sai_mem.pushed == []


# ---------------------------------------------------------------------------
# 5. プレビューは Pulse の頭の検知ぶんを読み取り専用で合成する (2026-09-07)
# ---------------------------------------------------------------------------


def _preview(persona, manager, building_id, pipeline):
    """部屋の様子の照合を外して、Section の差分だけをプレビューさせる。

    部屋の照合は実世界の束の組成 (builtin_data/tools/get_visual_context) を
    伴うので、この足場では組めない — その読み取り専用化は
    tests/test_room_state_diff.py が持つ。
    """
    with patch.object(integration, "_plan_room_state_change", return_value=None):
        return integration.preview_head_perceptions(
            persona, manager, building_id, pipeline=pipeline, model_key=MODEL,
        )


def test_preview_shows_a_departure_that_has_not_been_detected_yet():
    """溜まっていない差分 (Pulse の頭で初めて出る分) もプレビューに映る。"""
    manager = _FakeManager({ROOM_B: ["elis", "aifi"]})
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_B, manager))     # 基準 = 二人居る
    sai_mem = _FakeMemory()
    persona = _persona(sai_mem)

    manager.occupants[ROOM_B] = ["elis"]            # アイフィが出ていった
    entries = _preview(persona, manager, ROOM_B, pipeline)

    assert [(e["kind"], e["content"]) for e in entries] == [
        ("world_state", "アイフィ が退室しました"),
    ]
    assert sai_mem.pushed == []                     # 知覚バッファは触らない


def test_preview_does_not_advance_the_baseline():
    """プレビューの後でも、実 Pulse の検知が同じ差分を届ける。"""
    manager = _FakeManager({ROOM_B: ["elis", "aifi"]})
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_B, manager))
    sai_mem = _FakeMemory()
    persona = _persona(sai_mem)

    manager.occupants[ROOM_B] = ["elis"]
    _preview(persona, manager, ROOM_B, pipeline)

    assert inject_diff_notifications(
        persona, manager, ROOM_B, pipeline=pipeline, model_key=MODEL,
        detect_room=False,
    ) is True
    assert sai_mem.pushed == [("world_state", "アイフィ が退室しました")]


def test_preview_is_idempotent():
    """二回続けてプレビューしても同じ結果 (基準が動いていない証拠)。"""
    manager = _FakeManager({ROOM_B: ["elis", "aifi"]})
    pipeline = _pipeline()
    pipeline.capture_all(_ctx(ROOM_B, manager))
    persona = _persona(_FakeMemory())

    manager.occupants[ROOM_B] = ["elis"]
    first = _preview(persona, manager, ROOM_B, pipeline)
    second = _preview(persona, manager, ROOM_B, pipeline)

    assert first == second
    assert [e["content"] for e in first] == ["アイフィ が退室しました"]
