"""タグ層2 (棚入れ) のテスト — life_concept_map.md §9.1 層2 / A3。

判断点 (post_session / day_close) の response_schema に episode_purposes
フィールドが動的に載り、judgment_finalize が層2 タグ
(target=episode:N, layer=2) として purpose_tags へ永続化することを検証する:

- 対象の出来事 + 目的 enum が揃ったときだけフィールドが出る (空 enum 事故防止)
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

from database.models import AI, Base, City, Episode, User
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
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
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
        buildings=[SimpleNamespace(building_id="library", name="図書館")],
    )


@pytest.fixture
def finalize_mod():
    return load_builtin_tool("judgment_finalize")


@pytest.fixture
def backlog_task(manager):
    """purpose enum に載る生きたバックログタスク task:1 を用意する。

    2026-08-21 まではここが running Track (track:1) だった。Track 撤廃で
    purpose enum の供給が task:N だけになったため、器を差し替えた。
    """
    ptm = PersonaTaskManager(manager.SessionLocal)
    task = ptm.create_task(
        persona_id=PERSONA_ID, title="言葉の標本集", auto_activate=False,
    )
    return task["id"]


def _persona_ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


def _closed_episode(manager, short_id: int, kind: str) -> str:
    """今日の暦日に閉じた出来事の行を ORM で直に置いて ``episode:N`` を返す。

    ``saiverse.episodes`` の書き込み API は 2026-08-22 (束 6c) に全廃され、
    ``episodes`` テーブルは旧データの読み取り専用の残置になった (v3 §7)。
    day_close の層2 棚入れはいまもその残置を読むので、検証用の行はこちらで
    直接用意する。
    """
    started = int(BASE.replace(hour=10, minute=0).timestamp())
    db = manager.SessionLocal()
    try:
        db.add(Episode(
            EPISODE_ID=f"ep-{short_id}", PERSONA_ID=PERSONA_ID, SHORT_ID=short_id,
            KIND=kind, STARTED_AT=started, ENDED_AT=started + 600,
            BUILDING_ID="alice_room", STATUS="closed",
        ))
        db.commit()
    finally:
        db.close()
    return f"episode:{short_id}"


# ---------------------------------------------------------------------------
# schema 生成: フィールドの動的出し入れ
# ---------------------------------------------------------------------------


def test_post_session_schema_omits_field_without_purposes(manager):
    """目的 (task:N) がゼロならフィールドを出さない (空 enum 事故防止)。"""
    sr = {
        "digest": "調べ物をした", "artifacts": [], "rounds_used": 3,
        "ended_reason": "finished", "episode_ref": "episode:7",
    }
    args = jp.build_judgment_args(
        manager, PERSONA_ID, jp.KIND_POST_SESSION, {"session_result": sr},
    )
    assert "episode_purposes" not in args["response_schema"]["properties"]


def test_post_session_schema_reads_episode_ref_from_session_result(
    manager, backlog_task,
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


def test_purpose_enum_includes_backlog_tasks(manager, backlog_task):
    """purpose enum は採用済みバックログ task:N のみ (track:N は退役)。"""
    ptm = PersonaTaskManager(manager.SessionLocal)
    ptm.create_task(persona_id=PERSONA_ID, title="読書メモ", auto_activate=False)
    refs = jp.collect_purpose_refs(manager, PERSONA_ID)
    assert sorted(refs) == ["task:1", "task:2"]


def test_day_close_schema_uses_episode_purpose_pairs(manager, backlog_task):
    """day_close は今日閉じた出来事すべてが対象 → {episode, purpose} ペア。"""
    ep1 = _closed_episode(manager, 1, episodes.KIND_CONVERSATION)
    ep2 = _closed_episode(manager, 2, episodes.KIND_WORK_SESSION)
    args = jp.build_judgment_args(manager, PERSONA_ID, jp.KIND_DAY_CLOSE, {})
    props = args["response_schema"]["properties"]
    assert "episode_purposes" in props
    item = props["episode_purposes"]["items"]
    assert set(item["properties"]["episode"]["enum"]) == {ep1, ep2}
    assert item["properties"]["purpose"]["enum"] == ["task:1"]
    # 選択材料: 今日の出来事一覧が状況テキストに載る
    assert "[今日の出来事]" in args["situation_text"]
    assert ep1 in args["situation_text"]


def test_day_close_schema_omits_pairs_without_closed_episodes(manager, backlog_task):
    args = jp.build_judgment_args(manager, PERSONA_ID, jp.KIND_DAY_CLOSE, {})
    assert "episode_purposes" not in args["response_schema"]["properties"]


# ---------------------------------------------------------------------------
# finalize: 層2 タグの永続化と enum 外の拒否
# ---------------------------------------------------------------------------


def _post_session_ctx(purpose_refs):
    import json
    return json.dumps({
        "plan_date": "2026-07-04",
        "episode_ref": "episode:3",
        "purpose_refs": purpose_refs,
    })


def test_finalize_post_session_writes_layer2_tags(
    manager, finalize_mod, tmp_path, backlog_task,
):
    output = {
        "monologue": "このセッションは標本集の作業だった。",
        "digest": "標本集の序文を書いた。",
        "remaining_timetable": None,
        "episode_purposes": ["task:1"],
    }
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session",
            judgment_context=_post_session_ctx(["task:1"]),
        )
    adapter = manager.personas[PERSONA_ID].sai_memory
    tags = list_by_target(adapter.conn, "episode:3")
    assert len(tags) == 1
    assert tags[0].purpose_ref == "task:1"
    assert tags[0].layer == LAYER_SHELVE
    # 適用エコーがペルソナの文脈 (記録本文) に乗る
    assert adapter.messages
    assert any("棚に入れた" in m["content"] for m in adapter.messages)


def test_shelving_line_includes_purpose_title(
    manager, finalize_mod, tmp_path, backlog_task,
):
    """棚入れの適用エコーに purpose の表題が載る (task:N を番号だけにしない)。

    「episode:3 を task:1 の棚に入れた」だと、あとで読むまはーにもペルソナ自身
    にも中身が分からない (ユーザー向け表示の原則)。task:1「言葉の標本集」の形で
    表題を添える。episode:3 は実在しないので素の ref に落ちる (解決失敗で記録を
    落とさない — 表題は装飾)。
    """
    output = {
        "monologue": "このセッションは標本集の作業だった。",
        "digest": "標本集の序文を書いた。",
        "remaining_timetable": None,
        "episode_purposes": ["task:1"],
    }
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session",
            judgment_context=_post_session_ctx(["task:1"]),
        )
    contents = "\n".join(
        m["content"] for m in manager.personas[PERSONA_ID].sai_memory.messages
    )
    assert "task:1「言葉の標本集」" in contents


def test_finalize_rejects_out_of_enum_purpose(
    manager, finalize_mod, tmp_path, backlog_task,
):
    """enum 外の purpose ref は該当項目だけ棄却され、タグ行は生まれない。"""
    output = {
        "monologue": "x",
        "digest": "d",
        "remaining_timetable": None,
        "episode_purposes": ["task:99"],
    }
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session",
            judgment_context=_post_session_ctx(["task:1"]),
        )
    adapter = manager.personas[PERSONA_ID].sai_memory
    assert list_by_target(adapter.conn, "episode:3") == []


def test_finalize_day_close_pairs(manager, finalize_mod, tmp_path, backlog_task):
    """day_close の {episode, purpose} ペア適用 + enum 外 episode の棄却。"""
    output = {
        "monologue": "今日を閉じる。",
        "tomorrow_memo": "明日は続きから",
        "episode_purposes": [
            {"episode": "episode:1", "purpose": "task:1"},
            {"episode": "episode:9", "purpose": "task:1"},  # enum 外 → 棄却
        ],
    }
    import json
    ctx = json.dumps({
        "plan_date": "2026-07-04",
        "episode_refs": ["episode:1", "episode:2"],
        "purpose_refs": ["task:1"],
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
    manager, finalize_mod, tmp_path, backlog_task,
):
    """adapter が目的タグ未対応でも判断全体は落ちない (WARN のみ)。"""
    class Bare:
        def __init__(self):
            self.messages = []
        def append_persona_message(self, payload):
            self.messages.append(payload)

    manager.personas[PERSONA_ID].sai_memory = Bare()
    output = {
        "monologue": "x", "digest": "d",
        "remaining_timetable": None, "episode_purposes": ["task:1"],
    }
    with _persona_ctx(manager, tmp_path):
        summary, *_ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session",
            judgment_context=_post_session_ctx(["task:1"]),
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
