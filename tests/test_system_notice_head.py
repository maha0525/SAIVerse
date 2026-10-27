"""head 側のシステム通知の合言葉と説明文 (docs/intent/system_notice_delivery.md)。

固定する仕様:

1. 合言葉は head の作り直し (capture_all) のたびに生成し直し、一部の Section
   だけを撮り直す経路 (capture_for_event / recapture_missing) は引き継ぐ
   (同じ head を使い続けている間は同じ値 — 不変条件 6)。
2. 合言葉は session_head_snapshot.PASSPHRASE に snapshot と一緒に保存・復元
   される。列ができる前の行 (NULL) は読み込んだ後に head を作り直し、B (既読の
   基準) は持ち越す。
3. head の system メッセージは head の印と合言葉の metadata を持ち、本文には
   仮置き文字列だけが載る。
4. 説明文はモデルの判定 (クライアント層の変換と同じ) で出し分ける。
5. PASSPHRASE 列は既存の追加系マイグレーションで足される。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, SessionHeadSnapshot
from sai_memory.memory.storage import (
    HEAD_MESSAGE_KEY,
    SYSTEM_NOTICE_PASSPHRASE_KEY,
    SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER,
)
from sea.head_pipeline import (
    EventType,
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    LineHeadSnapshotStore,
    NotificationLabel,
    RenderedSection,
)

PERSONA_ID = "alice"
MODEL = "model-a"


class _Section:
    """撮り直しの経路を観測できる最小 Section (SPELL_TOGGLED で撮り直す)。"""

    order = 100
    refresh_on_events = frozenset({EventType.SPELL_TOGGLED})

    def __init__(self, name="common_prompt"):
        self.name = name
        self.value = "v1"
        self.fail = False

    def capture(self, ctx):
        if self.fail:
            raise RuntimeError("capture failed")
        return self.value

    def render(self, snapshot):
        return RenderedSection(text=f"{self.name}:{snapshot}")

    def diff_to_notifications(self, old, new):
        if old is not None and old != new:
            return [NotificationLabel(kind="changed", label=f"{old}->{new}")]
        return []

    def serialize_snapshot(self, snapshot):
        return json.dumps(snapshot)

    def deserialize_snapshot(self, data):
        return json.loads(data)


def _ctx(model_key=MODEL):
    return LineHeadInput(persona_id=PERSONA_ID, model_key=model_key)


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


# ---------------------------------------------------------------------------
# 1. 合言葉の寿命
# ---------------------------------------------------------------------------


def test_passphrase_is_regenerated_only_when_the_head_is_rebuilt():
    registry = HeadSectionRegistry()
    section = _Section()
    registry.register(section)
    pipeline = HeadPipeline(registry=registry)

    first = pipeline.capture_all(_ctx())
    assert len(first.passphrase) == 12 and first.passphrase.isalnum()

    section.value = "v2"
    by_event = pipeline.capture_for_event(_ctx(), EventType.SPELL_TOGGLED)
    assert by_event.snapshot_version == first.snapshot_version + 1
    assert by_event.passphrase == first.passphrase

    rebuilt = pipeline.capture_all(_ctx())
    assert rebuilt.passphrase != first.passphrase


def test_recapture_missing_keeps_the_passphrase():
    registry = HeadSectionRegistry()
    ok, broken = _Section("common_prompt"), _Section("persona_self")
    broken.fail = True
    registry.register(ok)
    registry.register(broken)
    pipeline = HeadPipeline(registry=registry)

    first = pipeline.capture_all(_ctx())
    assert "persona_self" not in first.sections
    broken.fail = False
    repaired = pipeline.recapture_missing(_ctx(), {"persona_self"})
    assert "persona_self" in repaired.sections
    assert repaired.passphrase == first.passphrase


# ---------------------------------------------------------------------------
# 2. 永続化
# ---------------------------------------------------------------------------


def test_passphrase_round_trips_through_the_store(session_factory):
    registry = HeadSectionRegistry()
    registry.register(_Section())
    store = LineHeadSnapshotStore(session_factory, registry)
    pipeline = HeadPipeline(registry=registry, store=store)
    snapshot = pipeline.capture_all(_ctx())

    db = session_factory()
    try:
        row = db.query(SessionHeadSnapshot).filter_by(
            PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL,
        ).one()
        assert row.PASSPHRASE == snapshot.passphrase
    finally:
        db.close()

    restarted = HeadPipeline(registry=registry, store=store)
    assert restarted.load_from_store(PERSONA_ID, MODEL)
    assert restarted.get_snapshot(PERSONA_ID, MODEL).passphrase == snapshot.passphrase


def test_row_without_passphrase_is_rebuilt_on_load_keeping_b(session_factory):
    from sea.head_pipeline.integration import ensure_snapshot

    registry = HeadSectionRegistry()
    section = _Section()
    registry.register(section)
    store = LineHeadSnapshotStore(session_factory, registry)
    HeadPipeline(registry=registry, store=store).capture_all(_ctx())

    # 列ができる前の行を再現する (PASSPHRASE = NULL)
    db = session_factory()
    try:
        db.execute(text("UPDATE session_head_snapshot SET PASSPHRASE = NULL"))
        db.commit()
    finally:
        db.close()

    # 休止中に世界が変わった (未配送の差分)
    section.value = "v2"
    restarted = HeadPipeline(registry=registry, store=store)
    ensure_snapshot(restarted, _ctx())
    rebuilt = restarted.get_snapshot(PERSONA_ID, MODEL)
    assert len(rebuilt.passphrase) == 12
    assert rebuilt.sections["common_prompt"] == "v2"
    # B は DB の行から持ち越す — 未配送の差分は次の flush で届く
    labels = restarted.flush_diffs(_ctx(), all_sections=True)
    assert [label.label for label in labels] == ["v1->v2"]


# ---------------------------------------------------------------------------
# 3. head の system メッセージ
# ---------------------------------------------------------------------------


def test_head_system_message_carries_the_mark_and_the_value():
    from sea.head_pipeline.integration import _compose_messages
    from sea.head_pipeline.sections.system_notice import TAGGED_NOTICE_TEXT
    from sea.head_pipeline.types import LineHeadSnapshot

    snapshot = LineHeadSnapshot(
        persona_id=PERSONA_ID, model_key=MODEL, line_role="main_line",
        captured_at=0.0, snapshot_version=1, sections={}, passphrase="P4ssPhrase12",
    )
    messages = _compose_messages(snapshot, {
        "common_prompt": RenderedSection(text="人格"),
        "system_notice": RenderedSection(text=TAGGED_NOTICE_TEXT),
    })
    head = messages[0]
    assert head["role"] == "system"
    assert head["metadata"] == {
        HEAD_MESSAGE_KEY: True, SYSTEM_NOTICE_PASSPHRASE_KEY: "P4ssPhrase12",
    }
    assert SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER in head["content"]
    assert "P4ssPhrase12" not in head["content"]


# ---------------------------------------------------------------------------
# 4. 説明文の出し分け
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("accepts, expected_tagged", [(False, True), (True, False)])
def test_explanation_follows_the_shared_judgement(monkeypatch, accepts, expected_tagged):
    from sea.head_pipeline.sections import system_notice as section_mod

    monkeypatch.setattr(
        "llm_clients.system_notice.model_accepts_mid_conversation_system",
        lambda model_key: accepts,
    )
    section = section_mod.SystemNoticeSection()
    snap = section.capture(_ctx())
    assert snap.tagged is expected_tagged
    rendered = section.render(snap).text
    if expected_tagged:
        assert rendered == section_mod.TAGGED_NOTICE_TEXT
        assert f'pass="{SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER}"' in rendered
    else:
        assert rendered == section_mod.DIRECT_NOTICE_TEXT
        assert SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER not in rendered


def test_explanation_for_real_model_configs():
    """組み込みのモデル設定で、判定がクライアントの実際の届け方と一致する。"""
    from llm_clients.system_notice import model_accepts_mid_conversation_system
    from saiverse.model_configs import MODEL_CONFIGS

    from llm_clients.factory import resolve_protocol

    seen = set()
    for key, cfg in MODEL_CONFIGS.items():
        protocol = resolve_protocol(str(cfg.get("provider", "ollama")), cfg)
        accepts = model_accepts_mid_conversation_system(key)
        if protocol in ("anthropic_native", "gemini_native", "openai_codex"):
            assert accepts is False, key
        elif protocol == "xai_native":
            assert accepts is True, key
        elif protocol in ("openai_compat", "nvidia_nim"):
            assert accepts is (cfg.get("convert_system_to_user") is not True), key
        seen.add(protocol)
    assert {"anthropic_native", "gemini_native"} <= seen


def test_stale_judgement_is_recaptured(monkeypatch):
    from sea.head_pipeline.sections.system_notice import SystemNoticeSection
    from sea.head_pipeline.types import SnapshotStaleError

    section = SystemNoticeSection()
    monkeypatch.setattr(
        "llm_clients.system_notice.model_accepts_mid_conversation_system",
        lambda model_key: False,
    )
    data = section.serialize_snapshot(section.capture(_ctx()))
    assert section.deserialize_snapshot(data).tagged is True
    monkeypatch.setattr(
        "llm_clients.system_notice.model_accepts_mid_conversation_system",
        lambda model_key: True,
    )
    with pytest.raises(SnapshotStaleError):
        section.deserialize_snapshot(data)


# ---------------------------------------------------------------------------
# 5. 列の追加
# ---------------------------------------------------------------------------


def test_passphrase_column_is_added_by_the_additive_migration(tmp_path):
    from database.migrate import needs_migration, try_additive_migration

    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE session_head_snapshot DROP COLUMN "PASSPHRASE"'))
    engine.dispose()

    assert needs_migration(str(db_path))
    assert try_additive_migration(str(db_path))
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("session_head_snapshot")}
    finally:
        engine.dispose()
    assert "PASSPHRASE" in cols


def test_render_head_messages_end_to_end(monkeypatch):
    """render_head_messages の出口: head の印・合言葉の metadata・仮置きの本文。"""
    from sea.head_pipeline.integration import render_head_messages
    from sea.head_pipeline.sections.system_notice import SystemNoticeSection

    monkeypatch.setattr(
        "llm_clients.system_notice.model_accepts_mid_conversation_system",
        lambda model_key: False,
    )
    registry = HeadSectionRegistry()
    registry.register(SystemNoticeSection())
    pipeline = HeadPipeline(registry=registry)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model=MODEL)
    manager = SimpleNamespace(sea_runtime=None)
    messages = render_head_messages(
        persona, manager, "b1", pipeline=pipeline, model_key=MODEL,
    )
    head = messages[0]
    passphrase = pipeline.get_snapshot(PERSONA_ID, MODEL).passphrase
    assert head["metadata"][HEAD_MESSAGE_KEY] is True
    assert head["metadata"][SYSTEM_NOTICE_PASSPHRASE_KEY] == passphrase
    assert SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER in head["content"]
    assert passphrase not in head["content"]
