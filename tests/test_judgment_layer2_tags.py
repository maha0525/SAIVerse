"""タグ層2 (棚入れ) のテスト — life_concept_map.md §9.1 層2 / A3。

判断点 (post_conversation / post_session / day_close) の response_schema に
episode_purposes フィールドが動的に載り、judgment_finalize が層2 タグ
(target=episode:N, layer=2) として purpose_tags へ永続化することを検証する:

- 対象の出来事 + 目的 enum が揃ったときだけフィールドが出る (空 enum 事故防止)
- post_conversation は「最後に閉じた会話の出来事」へのフォールバック解決
- post_session は WorkSessionResult.episode_ref から対象を得る
- day_close は {episode, purpose} ペア (今日閉じた出来事すべてが対象)
- finalize: enum 内の ref は purpose_tags 行になる / enum 外は該当項目だけ棄却
- SAIMemoryAdapter.add_purpose_tag の実 DB 書き込み (purpose_tags init 配線込み)

fixtures は tests/test_judgment_points.py の流儀 (in-memory SQLite + 仮想クロック)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from sai_memory.purpose_tags import (
    LAYER_SHELVE,
    init_purpose_tags_tables,
    add_tag,
    list_by_target,
)
from saiverse import clock
from saiverse import episodes
from saiverse import judgment_points as jp
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import PersonaTaskManager
from saiverse.track_manager import TrackManager
from tool_loader import load_builtin_tool

PERSONA_ID = "alice"
BASE = datetime(2026, 7, 4, 21, 0, 0)


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


@pytest.fixture(autouse=True)
def _virtual_clock():
    clock.enable_virtual(BASE)
    yield
    clock.disable_virtual()


class TagRecordingAdapter:
    """add_purpose_tag を実 sqlite (in-memory) に書く SAIMemory adapter スタブ。

    判断の SAIMemory 記録 (append_persona_message) は捕捉のみ。
    """

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []
        self.conn = sqlite3.connect(":memory:")
        init_purpose_tags_tables(self.conn)

    def append_persona_message(self, payload):
        self.messages.append(payload)

    def add_purpose_tag(self, target_ref: str, purpose_ref: str, layer: int) -> bool:
        add_tag(
            self.conn, target_ref=target_ref, purpose_ref=purpose_ref, layer=layer,
        )
        return True


@pytest.fixture
def manager(session_factory):
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()

    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=TagRecordingAdapter(),
    )
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),
        track_manager=TrackManager(session_factory=session_factory),
        buildings=[SimpleNamespace(building_id="library", name="図書館")],
    )


@pytest.fixture
def finalize_mod():
    return load_builtin_tool("judgment_finalize")


@pytest.fixture
def running_track(manager):
    """pickable (running) な Track track:1 を用意する。"""
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous",
        title="言葉の標本集", initial_status="running",
    )
    return track_id


def _persona_ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


def _closed_conversation_episode(manager) -> str:
    ep = episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="alice_room",
    )
    closed = episodes.close_episode(manager, PERSONA_ID, ep["episode_id"])
    return closed["episode_ref"]


# ---------------------------------------------------------------------------
# schema 生成: フィールドの動的出し入れ
# ---------------------------------------------------------------------------


def test_post_conversation_schema_has_episode_purposes(manager, running_track):
    """閉じた会話の出来事 + 生きた目的があるとき episode_purposes が載る。

    episode_ref は context に無くても「最後に閉じた会話」へフォールバックする。
    """
    ref = _closed_conversation_episode(manager)
    args = jp.build_judgment_args(manager, PERSONA_ID, jp.KIND_POST_CONVERSATION, {})
    props = args["response_schema"]["properties"]
    assert "episode_purposes" in props
    assert props["episode_purposes"]["items"]["enum"] == ["track:1"]
    import json
    ctx = json.loads(args["judgment_context"])
    assert ctx["episode_ref"] == ref
    assert ctx["purpose_refs"] == ["track:1"]
    assert "episode_purposes" in args["situation_text"]


def test_post_conversation_schema_omits_field_without_episode(manager, running_track):
    """閉じた会話の出来事が無ければフィールドを出さない (偽対象を作らない)。"""
    args = jp.build_judgment_args(manager, PERSONA_ID, jp.KIND_POST_CONVERSATION, {})
    props = args["response_schema"]["properties"]
    assert "episode_purposes" not in props
    import json
    ctx = json.loads(args["judgment_context"])
    assert "episode_ref" not in ctx


def test_post_conversation_schema_omits_field_without_purposes(manager):
    """目的 (track/task) がゼロならフィールドを出さない (空 enum 事故防止)。"""
    _closed_conversation_episode(manager)
    args = jp.build_judgment_args(manager, PERSONA_ID, jp.KIND_POST_CONVERSATION, {})
    assert "episode_purposes" not in args["response_schema"]["properties"]


def test_post_session_schema_reads_episode_ref_from_session_result(
    manager, running_track,
):
    """post_session は WorkSessionResult.episode_ref を対象の出来事として読む。"""
    sr = {
        "digest": "調べ物をした", "artifacts": [], "rounds_used": 3,
        "ended_reason": "finished", "episode_ref": "episode:7",
    }
    args = jp.build_judgment_args(
        manager, PERSONA_ID, jp.KIND_POST_SESSION, {"session_result": sr},
    )
    props = args["response_schema"]["properties"]
    assert "episode_purposes" in props
    import json
    ctx = json.loads(args["judgment_context"])
    assert ctx["episode_ref"] == "episode:7"
    assert "episode_purposes" in args["situation_text"]


def test_purpose_enum_includes_backlog_tasks(manager, running_track):
    """purpose enum は track:N + 採用済みバックログ task:N (欲求候補は含めない)。"""
    ptm = PersonaTaskManager(manager.SessionLocal)
    ptm.create_task(persona_id=PERSONA_ID, title="読書メモ", auto_activate=False)
    refs = jp.collect_purpose_refs(manager, PERSONA_ID)
    assert "track:1" in refs
    assert "task:1" in refs


def test_day_close_schema_uses_episode_purpose_pairs(manager, running_track):
    """day_close は今日閉じた出来事すべてが対象 → {episode, purpose} ペア。"""
    ep1 = _closed_conversation_episode(manager)
    ep2 = episodes.open_episode(manager, PERSONA_ID, episodes.KIND_WORK_SESSION)
    ep2 = episodes.close_episode(manager, PERSONA_ID, ep2["episode_id"])["episode_ref"]
    args = jp.build_judgment_args(manager, PERSONA_ID, jp.KIND_DAY_CLOSE, {})
    props = args["response_schema"]["properties"]
    assert "episode_purposes" in props
    item = props["episode_purposes"]["items"]
    assert set(item["properties"]["episode"]["enum"]) == {ep1, ep2}
    assert item["properties"]["purpose"]["enum"] == ["track:1"]
    # 選択材料: 今日の出来事一覧が状況テキストに載る
    assert "[今日の出来事]" in args["situation_text"]
    assert ep1 in args["situation_text"]


def test_day_close_schema_omits_pairs_without_closed_episodes(manager, running_track):
    args = jp.build_judgment_args(manager, PERSONA_ID, jp.KIND_DAY_CLOSE, {})
    assert "episode_purposes" not in args["response_schema"]["properties"]


# ---------------------------------------------------------------------------
# finalize: 層2 タグの永続化と enum 外の拒否
# ---------------------------------------------------------------------------


def test_finalize_post_conversation_writes_layer2_tags(
    manager, finalize_mod, tmp_path, running_track,
):
    output = {
        "monologue": "この会話は標本集の話だった。",
        "picked_tasks": [],
        "new_desires": [],
        "remaining_timetable": None,
        "episode_purposes": ["track:1"],
    }
    import json
    ctx = json.dumps({
        "plan_date": "2026-07-04", "track_refs": ["track:1"],
        "episode_ref": "episode:3", "purpose_refs": ["track:1"],
    })
    with _persona_ctx(manager, tmp_path):
        summary, *_ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_conversation", judgment_context=ctx,
        )
    adapter = manager.personas[PERSONA_ID].sai_memory
    tags = list_by_target(adapter.conn, "episode:3")
    assert len(tags) == 1
    assert tags[0].purpose_ref == "track:1"
    assert tags[0].layer == LAYER_SHELVE
    # 適用エコーがペルソナの文脈 (記録本文) に乗る
    assert adapter.messages
    assert "棚に入れた" in adapter.messages[0]["content"]


def test_finalize_rejects_out_of_enum_purpose(
    manager, finalize_mod, tmp_path, running_track,
):
    """enum 外の purpose ref は該当項目だけ棄却され、タグ行は生まれない。"""
    output = {
        "monologue": "x",
        "picked_tasks": [],
        "new_desires": [],
        "remaining_timetable": None,
        "episode_purposes": ["task:99"],
    }
    import json
    ctx = json.dumps({
        "plan_date": "2026-07-04", "track_refs": [],
        "episode_ref": "episode:3", "purpose_refs": ["track:1"],
    })
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_conversation", judgment_context=ctx,
        )
    adapter = manager.personas[PERSONA_ID].sai_memory
    assert list_by_target(adapter.conn, "episode:3") == []


def test_finalize_day_close_pairs(manager, finalize_mod, tmp_path, running_track):
    """day_close の {episode, purpose} ペア適用 + enum 外 episode の棄却。"""
    output = {
        "monologue": "今日を閉じる。",
        "tomorrow_memo": "明日は続きから",
        "episode_purposes": [
            {"episode": "episode:1", "purpose": "track:1"},
            {"episode": "episode:9", "purpose": "track:1"},  # enum 外 → 棄却
        ],
    }
    import json
    ctx = json.dumps({
        "plan_date": "2026-07-04",
        "touched_desire_refs": [],
        "episode_refs": ["episode:1", "episode:2"],
        "purpose_refs": ["track:1"],
    })
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="day_close", judgment_context=ctx,
        )
    adapter = manager.personas[PERSONA_ID].sai_memory
    tags = list_by_target(adapter.conn, "episode:1")
    assert len(tags) == 1
    assert tags[0].layer == LAYER_SHELVE
    assert list_by_target(adapter.conn, "episode:9") == []


def test_finalize_warns_when_adapter_lacks_tag_api(
    manager, finalize_mod, tmp_path, running_track,
):
    """adapter が目的タグ未対応でも判断全体は落ちない (WARN のみ)。"""
    class Bare:
        def __init__(self):
            self.messages = []
        def append_persona_message(self, payload):
            self.messages.append(payload)

    manager.personas[PERSONA_ID].sai_memory = Bare()
    output = {
        "monologue": "x", "picked_tasks": [], "new_desires": [],
        "remaining_timetable": None, "episode_purposes": ["track:1"],
    }
    import json
    ctx = json.dumps({
        "plan_date": "2026-07-04", "track_refs": [],
        "episode_ref": "episode:3", "purpose_refs": ["track:1"],
    })
    with _persona_ctx(manager, tmp_path):
        summary, *_ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_conversation", judgment_context=ctx,
        )
    assert "Judgment finalized" in summary


# ---------------------------------------------------------------------------
# SAIMemoryAdapter.add_purpose_tag (実 memory.db)
# ---------------------------------------------------------------------------


def test_adapter_add_purpose_tag_real_db(tmp_path, monkeypatch):
    """実 adapter で purpose_tags テーブルが init され、行が書けること。"""
    from unittest.mock import patch

    class DummyEmbedder:
        def __init__(self, model=None, **kwargs):
            self.model_name = model

        def embed(self, texts, **kwargs):
            return [[0.0] * 3 for _ in texts]

    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    persona_dir = tmp_path / "personas" / "tester"
    persona_dir.mkdir(parents=True)
    with patch("saiverse_memory.adapter.Embedder", DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter

        adapter = SAIMemoryAdapter(
            "tester", persona_dir=persona_dir, resource_id="tester",
        )
        try:
            assert adapter.add_purpose_tag("episode:1", "track:2", LAYER_SHELVE) is True
            tags = list_by_target(adapter.conn, "episode:1")
            assert len(tags) == 1
            assert tags[0].purpose_ref == "track:2"
            assert tags[0].layer == LAYER_SHELVE
            # 不正 layer は握り潰して False (WARN)
            assert adapter.add_purpose_tag("episode:1", "track:2", 1) is False
        finally:
            adapter.close()
            import gc
            gc.collect()
