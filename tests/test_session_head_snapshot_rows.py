"""head snapshot + last_notified の (persona, model) キー化テスト (beat_execution_context.md §3.1、§6-3b)。

固定する仕様:

- **行独立性**: 同一 persona の 2 model が独立の Session state / DB 行を持ち、
  片方の capture・diff 既読 (last_notified)・削除が他方に触れないこと。
  「line ごとの diff 既読」→「model ごと」の意味変化 (サブラインも同 model なら
  head 共有) を含む。
- **backfill** (database/migrate.py backfill_session_head_snapshots):
  line_head_snapshot (キー=(persona, line)) → session_head_snapshot
  (PK=(persona, model))。MODEL_KEY='default' (旧実装バグの実値) は
  ai.DEFAULT_MODEL へ解決、解決不能はスキップ。集約衝突は line='main' 優先 →
  UPDATED_AT 最新。既存の新形式行は上書きしない。再実行冪等。
- **model_key 供給**: 明示引数 (ExecutionContext 経路) > persona.model >
  persona.default_model (スタブ互換) > "default"。render_head_messages /
  inject_diff_notifications / keepalive が供給した model の Session に向けて
  head が組まれること。
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, LineHeadSnapshot as LegacyRow, SessionHeadSnapshot
from sea.head_pipeline import (
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    LineHeadSnapshotStore,
    NotificationLabel,
    RenderedSection,
)

PERSONA_ID = "alice"
MODEL_A = "model-standard"
MODEL_B = "model-lightweight"


class _SpellSection:
    """spell_list 相当の最小 section (diff 通知を観測できる)。"""
    name = "spell_list"
    order = 600
    refresh_on_events = frozenset()

    def __init__(self):
        self.live_spells = ("spell_a",)

    def capture(self, ctx):
        return tuple(self.live_spells)

    def render(self, snapshot):
        if not snapshot:
            return None
        return RenderedSection(text=f"## スペル\n{', '.join(snapshot)}")

    def diff_to_notifications(self, old, new):
        old_set, new_set = set(old or ()), set(new or ())
        labels = []
        for s in sorted(new_set - old_set):
            labels.append(NotificationLabel(kind="spell_added", label=f"追加: {s}"))
        for s in sorted(old_set - new_set):
            labels.append(NotificationLabel(kind="spell_removed", label=f"削除: {s}"))
        return labels

    def serialize_snapshot(self, snapshot):
        return json.dumps(list(snapshot or ()))

    def deserialize_snapshot(self, data):
        return tuple(json.loads(data))


def _registry():
    r = HeadSectionRegistry()
    r.register(_SpellSection())
    return r


def _ctx(model_key, persona_id=PERSONA_ID):
    return LineHeadInput(
        persona_id=persona_id, model_key=model_key, current_building_id="b_lobby",
    )


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


# ---------------------------------------------------------------------------
# 1. 行独立性 (in-memory)
# ---------------------------------------------------------------------------


def test_two_models_have_independent_snapshots():
    """同一 persona の 2 model が独立 state を持ち、キーは (persona, model)。"""
    registry = _registry()
    pipeline = HeadPipeline(registry=registry)
    section = registry.by_name("spell_list")

    pipeline.capture_all(_ctx(MODEL_A))
    section.live_spells = ("spell_a", "spell_b")
    pipeline.capture_all(_ctx(MODEL_B))

    snap_a = pipeline.get_snapshot(PERSONA_ID, MODEL_A)
    snap_b = pipeline.get_snapshot(PERSONA_ID, MODEL_B)
    assert snap_a.sections["spell_list"] == ("spell_a",)
    assert snap_b.sections["spell_list"] == ("spell_a", "spell_b")
    assert snap_a.model_key == MODEL_A
    assert snap_b.model_key == MODEL_B

    # model B の再 capture は A に触れない
    section.live_spells = ("spell_c",)
    pipeline.capture_all(_ctx(MODEL_B))
    assert pipeline.get_snapshot(PERSONA_ID, MODEL_A).sections["spell_list"] == ("spell_a",)


def test_diff_read_state_is_per_model():
    """diff 既読 (last_notified) が model ごとに独立 — 片方の flush が他方の
    「まだ知らない変化」を消さない (§3.1 の意味変化の固定)。"""
    registry = _registry()
    pipeline = HeadPipeline(registry=registry)
    section = registry.by_name("spell_list")

    pipeline.capture_all(_ctx(MODEL_A))
    pipeline.capture_all(_ctx(MODEL_B))

    section.live_spells = ("spell_a", "spell_x")
    pipeline.mark_dirty(PERSONA_ID, MODEL_A, "spell_list")
    pipeline.mark_dirty(PERSONA_ID, MODEL_B, "spell_list")

    labels_a = pipeline.flush_diffs(_ctx(MODEL_A))
    assert [lb.kind for lb in labels_a] == ["spell_added"]

    # A の flush 後も B は独立に同じ差分を受け取る
    labels_b = pipeline.flush_diffs(_ctx(MODEL_B))
    assert [lb.kind for lb in labels_b] == ["spell_added"]

    # 既読は前進済み: 再 flush は双方空
    pipeline.mark_dirty(PERSONA_ID, MODEL_A, "spell_list")
    pipeline.mark_dirty(PERSONA_ID, MODEL_B, "spell_list")
    assert pipeline.flush_diffs(_ctx(MODEL_A)) == []
    assert pipeline.flush_diffs(_ctx(MODEL_B)) == []


# ---------------------------------------------------------------------------
# 2. 行独立性 (DB store)
# ---------------------------------------------------------------------------


def _rows(session_factory):
    db = session_factory()
    try:
        return {
            (r.PERSONA_ID, r.MODEL_KEY): r.SECTIONS_JSON
            for r in db.query(SessionHeadSnapshot).all()
        }
    finally:
        db.close()


def test_store_persists_one_row_per_model(session_factory):
    registry = _registry()
    store = LineHeadSnapshotStore(session_factory=session_factory, registry=registry)
    pipeline = HeadPipeline(registry=registry, store=store)

    pipeline.capture_all(_ctx(MODEL_A))
    pipeline.capture_all(_ctx(MODEL_B))

    rows = _rows(session_factory)
    assert set(rows.keys()) == {(PERSONA_ID, MODEL_A), (PERSONA_ID, MODEL_B)}

    # load も model 単位
    pipeline2 = HeadPipeline(registry=registry, store=store)
    assert pipeline2.load_from_store(PERSONA_ID, MODEL_A)
    assert pipeline2.get_snapshot(PERSONA_ID, MODEL_A).model_key == MODEL_A
    assert not pipeline2.load_from_store(PERSONA_ID, "no-such-model")


def test_store_delete_touches_only_target_model(session_factory):
    registry = _registry()
    store = LineHeadSnapshotStore(session_factory=session_factory, registry=registry)
    pipeline = HeadPipeline(registry=registry, store=store)
    pipeline.capture_all(_ctx(MODEL_A))
    pipeline.capture_all(_ctx(MODEL_B))

    pipeline.discard_session(PERSONA_ID, MODEL_A, delete_persisted=True)

    rows = _rows(session_factory)
    assert set(rows.keys()) == {(PERSONA_ID, MODEL_B)}
    assert not pipeline.has_snapshot(PERSONA_ID, MODEL_A)
    assert pipeline.has_snapshot(PERSONA_ID, MODEL_B)


# ---------------------------------------------------------------------------
# 3. backfill (line_head_snapshot → session_head_snapshot)
# ---------------------------------------------------------------------------


def _legacy_row(persona_id, line_id, *, model_key="default", sections='{"spell_list": "[\\"s\\"]"}',
                notified='{}', version=1, updated_at=None):
    return LegacyRow(
        PERSONA_ID=persona_id,
        LINE_ID=line_id,
        LINE_ROLE="main_line",
        MODEL_KEY=model_key,
        SECTIONS_JSON=sections,
        LAST_NOTIFIED_JSON=notified,
        SNAPSHOT_VERSION=version,
        CAPTURED_AT=updated_at or datetime(2026, 7, 1, 10, 0, 0),
        UPDATED_AT=updated_at or datetime(2026, 7, 1, 10, 0, 0),
    )


def _setup_backfill_db(tmp_path, legacy_rows, ai_rows, new_rows=()):
    db_path = tmp_path / "saiverse.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    for ai in ai_rows:
        db.add(ai)
    for row in legacy_rows:
        db.add(row)
    for row in new_rows:
        db.add(row)
    db.commit()
    db.close()
    engine.dispose()
    return db_path


def _read_new_rows(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        return {
            (r.PERSONA_ID, r.MODEL_KEY): (r.SECTIONS_JSON, r.LAST_NOTIFIED_JSON, r.SNAPSHOT_VERSION)
            for r in db.query(SessionHeadSnapshot).all()
        }
    finally:
        db.close()
        engine.dispose()


def test_backfill_resolves_default_model_key_and_is_idempotent(tmp_path):
    """MODEL_KEY='default' (旧実装バグの実値) は ai.DEFAULT_MODEL へ解決される。
    再実行しても冪等。旧テーブルの行は残る (DROP は掃除 wave)。"""
    db_path = _setup_backfill_db(
        tmp_path,
        legacy_rows=[_legacy_row(PERSONA_ID, "main", sections='{"spell_list": "[\\"a\\"]"}',
                                 notified='{"spell_list": "[\\"a\\"]"}', version=7)],
        ai_rows=[AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", DEFAULT_MODEL=MODEL_A)],
    )

    from database.migrate import backfill_session_head_snapshots
    backfill_session_head_snapshots(str(db_path))

    rows = _read_new_rows(db_path)
    assert rows == {
        (PERSONA_ID, MODEL_A): ('{"spell_list": "[\\"a\\"]"}', '{"spell_list": "[\\"a\\"]"}', 7),
    }

    # 再実行しても変わらない (冪等)
    backfill_session_head_snapshots(str(db_path))
    assert _read_new_rows(db_path) == rows

    # 旧テーブルの行は残っている (変換元としてのみ残存)
    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        assert db.query(LegacyRow).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_backfill_collision_prefers_line_main(tmp_path):
    """同一 (persona, model) へ複数行が写る場合、LINE_ID='main' の行が勝つ。"""
    db_path = _setup_backfill_db(
        tmp_path,
        legacy_rows=[
            _legacy_row(PERSONA_ID, "sub", sections='{"spell_list": "[\\"sub\\"]"}',
                        updated_at=datetime(2026, 7, 10, 12, 0, 0)),  # 新しいが main でない
            _legacy_row(PERSONA_ID, "main", sections='{"spell_list": "[\\"main\\"]"}',
                        updated_at=datetime(2026, 7, 1, 10, 0, 0)),
        ],
        ai_rows=[AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", DEFAULT_MODEL=MODEL_A)],
    )
    from database.migrate import backfill_session_head_snapshots
    backfill_session_head_snapshots(str(db_path))

    rows = _read_new_rows(db_path)
    assert rows[(PERSONA_ID, MODEL_A)][0] == '{"spell_list": "[\\"main\\"]"}'


def test_backfill_collision_without_main_prefers_latest(tmp_path):
    """main 行が無い場合は UPDATED_AT 最新の行が勝つ。"""
    db_path = _setup_backfill_db(
        tmp_path,
        legacy_rows=[
            _legacy_row(PERSONA_ID, "sub1", sections='{"spell_list": "[\\"old\\"]"}',
                        updated_at=datetime(2026, 7, 1, 10, 0, 0)),
            _legacy_row(PERSONA_ID, "sub2", sections='{"spell_list": "[\\"new\\"]"}',
                        updated_at=datetime(2026, 7, 10, 12, 0, 0)),
        ],
        ai_rows=[AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", DEFAULT_MODEL=MODEL_A)],
    )
    from database.migrate import backfill_session_head_snapshots
    backfill_session_head_snapshots(str(db_path))

    rows = _read_new_rows(db_path)
    assert rows[(PERSONA_ID, MODEL_A)][0] == '{"spell_list": "[\\"new\\"]"}'


def test_backfill_does_not_overwrite_existing_new_row(tmp_path):
    """既に session_head_snapshot に行がある (persona, model) は上書きしない。"""
    db_path = _setup_backfill_db(
        tmp_path,
        legacy_rows=[_legacy_row(PERSONA_ID, "main", sections='{"spell_list": "[\\"legacy\\"]"}')],
        ai_rows=[AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", DEFAULT_MODEL=MODEL_A)],
        new_rows=[SessionHeadSnapshot(
            PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL_A, LINE_ROLE="main_line",
            SECTIONS_JSON='{"spell_list": "[\\"fresh\\"]"}', LAST_NOTIFIED_JSON="{}",
            SNAPSHOT_VERSION=3,
        )],
    )
    from database.migrate import backfill_session_head_snapshots
    backfill_session_head_snapshots(str(db_path))

    rows = _read_new_rows(db_path)
    assert rows[(PERSONA_ID, MODEL_A)][0] == '{"spell_list": "[\\"fresh\\"]"}'
    assert rows[(PERSONA_ID, MODEL_A)][2] == 3


def test_backfill_uses_real_model_key_as_is(tmp_path):
    """MODEL_KEY に実 model 名が入っている行はそのまま新キーに使う。"""
    db_path = _setup_backfill_db(
        tmp_path,
        legacy_rows=[_legacy_row(PERSONA_ID, "main", model_key=MODEL_B)],
        ai_rows=[AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", DEFAULT_MODEL=MODEL_A)],
    )
    from database.migrate import backfill_session_head_snapshots
    backfill_session_head_snapshots(str(db_path))

    rows = _read_new_rows(db_path)
    assert set(rows.keys()) == {(PERSONA_ID, MODEL_B)}


def test_backfill_skips_unresolvable_rows(tmp_path):
    """'default' かつ ai.DEFAULT_MODEL が NULL → 実行 model を特定できずスキップ
    (head は cache 状態で損失許容 — 次回 capture_all で再構築)。"""
    db_path = _setup_backfill_db(
        tmp_path,
        legacy_rows=[_legacy_row(PERSONA_ID, "main")],
        ai_rows=[AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", DEFAULT_MODEL=None)],
    )
    from database.migrate import backfill_session_head_snapshots
    backfill_session_head_snapshots(str(db_path))

    assert _read_new_rows(db_path) == {}


def test_backfill_noop_when_legacy_empty(tmp_path):
    db_path = _setup_backfill_db(
        tmp_path,
        legacy_rows=[],
        ai_rows=[AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", DEFAULT_MODEL=MODEL_A)],
    )
    from database.migrate import backfill_session_head_snapshots
    backfill_session_head_snapshots(str(db_path))
    assert _read_new_rows(db_path) == {}


# ---------------------------------------------------------------------------
# 4. model_key の供給経路
# ---------------------------------------------------------------------------


def test_build_line_head_input_explicit_model_key_wins():
    """ExecutionContext 経路 (明示引数) は persona 属性より優先される。"""
    from sea.head_pipeline.integration import build_line_head_input

    persona = SimpleNamespace(persona_id=PERSONA_ID, model=MODEL_A)
    ctx = build_line_head_input(persona, None, "b_lobby", model_key=MODEL_B)
    assert ctx.model_key == MODEL_B


def test_build_line_head_input_falls_back_to_persona_model():
    """届かない経路のフォールバックは persona.model (実属性。旧実装は存在しない
    persona.default_model を引いて常に 'default' に落ちていた)。"""
    from sea.head_pipeline.integration import build_line_head_input

    persona = SimpleNamespace(persona_id=PERSONA_ID, model=MODEL_A)
    ctx = build_line_head_input(persona, None, "b_lobby")
    assert ctx.model_key == MODEL_A


def test_build_line_head_input_fallback_chain():
    from sea.head_pipeline.integration import build_line_head_input

    # model 属性が無いスタブは default_model へ (テスト互換の第 2 候補)
    stub = SimpleNamespace(persona_id=PERSONA_ID, default_model=MODEL_B)
    assert build_line_head_input(stub, None, "b").model_key == MODEL_B

    # どちらも無ければ "default"
    bare = SimpleNamespace(persona_id=PERSONA_ID)
    assert build_line_head_input(bare, None, "b").model_key == "default"


def test_render_head_messages_targets_supplied_model_session():
    """render_head_messages(model_key=...) が供給 model の Session に snapshot を
    作り、render すること (default model の Session には触れない)。"""
    from sea.head_pipeline.integration import render_head_messages

    registry = _registry()
    pipeline = HeadPipeline(registry=registry)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model=MODEL_A)

    messages = render_head_messages(
        persona, None, "b_lobby",
        enabled_sections={"spell_list"}, pipeline=pipeline, model_key=MODEL_B,
    )
    assert messages and "スペル" in messages[0]["content"]
    assert pipeline.has_snapshot(PERSONA_ID, MODEL_B)
    assert not pipeline.has_snapshot(PERSONA_ID, MODEL_A)


def test_inject_diff_notifications_uses_supplied_model_session():
    """inject_diff_notifications(model_key=...) の diff 既読が供給 model の
    Session に紐づくこと。"""
    from sea.head_pipeline.integration import inject_diff_notifications

    registry = _registry()
    pipeline = HeadPipeline(registry=registry)
    section = registry.by_name("spell_list")

    pushed: List[Any] = []
    sai_memory = SimpleNamespace(
        is_ready=lambda: True,
        push_perception=lambda kind, label, **kw: pushed.append((kind, label)),
    )
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL_A, sai_memory=sai_memory,
        history_manager=None,
    )

    # 初回 = capture (B=A リセット) → 差分なし
    assert inject_diff_notifications(
        persona, None, "b_lobby", pipeline=pipeline, model_key=MODEL_B,
    ) is False
    assert pipeline.has_snapshot(PERSONA_ID, MODEL_B)

    # live 変化後は MODEL_B の Session として通知が出る
    section.live_spells = ("spell_a", "spell_new")
    assert inject_diff_notifications(
        persona, None, "b_lobby", pipeline=pipeline, model_key=MODEL_B,
    ) is True
    assert pushed and pushed[0][0] == "world_state"
    # default model (MODEL_A) の Session には snapshot が作られていない
    assert not pipeline.has_snapshot(PERSONA_ID, MODEL_A)


# ---------------------------------------------------------------------------
# 5. keepalive の head 供給 (見張り対象 model の Session に向けて context を組む)
# ---------------------------------------------------------------------------


class _FakeLLMClient:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._usage = SimpleNamespace(
            model=MODEL_B, input_tokens=100, output_tokens=1,
            cached_tokens=90, cache_write_tokens=10, cache_ttl="5m",
        )

    def generate(self, messages, tools=None, response_schema=None, *,
                 temperature=None, **kwargs):
        self.calls.append({"messages": list(messages)})
        return "."

    def consume_usage(self):
        return self._usage


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_prepares_context_for_watched_model(_mock_cache):
    """run_cache_keepalive(persona, model) が _prepare_context に見張り対象の
    model_key を渡すこと — lightweight Session を default の head で温めると
    別 prefix になりキャッシュを壊すため (beat_execution_context.md §3.1)。"""
    from sea.runtime import SEARuntime

    client = _FakeLLMClient()
    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        autonomy_enabled=True,
        model=MODEL_A,
        lightweight_model=MODEL_B,
        lightweight_llm_client=client,
        current_building_id="room",
        history_manager=None,
    )
    manager = SimpleNamespace(
        personas={PERSONA_ID: persona},
        event_scheduler=None,
        meta_layer=SimpleNamespace(
            _load_judgment_config=lambda p: {"keep_cache_alive": True},
        ),
        building_histories={},
        state=SimpleNamespace(cache_enabled=True, cache_ttl="5m"),
        _persona_cache_overrides={},
    )
    manager.resolve_persona_cache = lambda pid=None: (True, "5m")
    runtime = SEARuntime(manager)

    live_entry = {
        "anchor_id": "a1",
        "updated_at": datetime.now().isoformat(),
        "ttl_seconds": 1200,
    }
    runtime.session_lifecycle.load_anchor_entry = lambda pid, mk: (
        live_entry if mk == MODEL_B else None
    )
    touched: List[Any] = []
    runtime.session_lifecycle.touch_anchor_after_llm_call = (
        lambda p, usage: touched.append(usage)
    )

    captured: List[Any] = []

    def _fake_prepare(persona, building_id, user_input, *args, **kwargs):
        captured.append(kwargs.get("model_key"))
        return [{"role": "user", "content": "履歴"}]

    runtime._prepare_context = _fake_prepare

    assert runtime.run_cache_keepalive(PERSONA_ID, MODEL_B) is True
    # 見張り対象 (lightweight) の model_key で context が組まれた
    assert captured == [MODEL_B]
    assert len(client.calls) == 1
    assert touched  # 成功時のみ anchor touch
