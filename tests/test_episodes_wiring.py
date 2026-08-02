"""活性化 A1: 出来事 (Episode) の開閉を実経路へ接続する配線テスト。

対象 (life_concept_map.md §8/§8.1/§9.1 層0、meta 書式契約は §14):

- get_open_episode + per-persona キャッシュ (open/close 更新・再起動後の DB 復元)
- 会話: シム経路 (day_scenario の UserEventDriver) で open/close、冪等、
  occurrence_id の同一 Building 共有、全時刻が仮想クロック経由
- 作業セッション: run_work_session の open→close、meta.title / meta.artifacts /
  digest_ref (meta 書式契約)、エラー終了でも閉じる、開いている slot 出来事が出自
- コマ: _fire_slot の open→close、presence スタブ = presence + record_level 透過、
  skip (no_handler) は出来事を作らない、六型コマは kind='slot'
- 層0: SEARuntime._store_memory が開いている出来事の episode_ref を
  metadata.origin_episode に付与する / 無ければ何も付けない

DB は in-memory SQLite (StaticPool)、時刻は saiverse.clock の仮想クロック。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, Episode, Item, User
from saiverse import clock, day_plan, episodes
from saiverse.day_scenario import TrackSimUserEventDriver
from saiverse.track_manager import TrackManager
from sea.pulse_context import PulseContext
from sea.work_session import ENDED_ERROR, ENDED_FINISHED, run_work_session

PERSONA_ID = "alice"
BUILDING = "alice_room"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


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
def _reset_clock():
    yield
    clock.disable_virtual()


def _seed_personas(session_factory, persona_ids: List[str]) -> None:
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        for pid in persona_ids:
            db.add(AI(AIID=pid, HOME_CITYID=city.CITYID, AINAME=pid.title()))
        db.commit()
    finally:
        db.close()


def _make_manager(session_factory, persona_ids: Optional[List[str]] = None,
                  building: str = BUILDING) -> SimpleNamespace:
    persona_ids = persona_ids or [PERSONA_ID]
    _seed_personas(session_factory, persona_ids)
    personas = {
        pid: SimpleNamespace(
            persona_id=pid,
            persona_name=pid.title(),
            current_building_id=building,
            private_room_id=building,
            sai_memory=SimpleNamespace(
                get_current_thread=lambda pid=pid: f"{pid}:persona_main",
            ),
        )
        for pid in persona_ids
    }
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        track_manager=TrackManager(session_factory=session_factory),
    )


def _episode_rows(session_factory, persona_id: str = PERSONA_ID) -> List[Episode]:
    db = session_factory()
    try:
        rows = (
            db.query(Episode)
            .filter(Episode.PERSONA_ID == persona_id)
            .order_by(Episode.SHORT_ID.asc())
            .all()
        )
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# 0. get_open_episode + キャッシュ
# ---------------------------------------------------------------------------


def test_get_open_episode_none_when_empty(session_factory):
    manager = _make_manager(session_factory)
    assert episodes.get_open_episode(manager, PERSONA_ID) is None
    # kind 指定 (非キャッシュ経路) も None
    assert episodes.get_open_episode(
        manager, PERSONA_ID, kind=episodes.KIND_CONVERSATION) is None


def test_get_open_episode_tracks_open_and_close(session_factory):
    manager = _make_manager(session_factory)
    ep1 = episodes.open_episode(manager, PERSONA_ID, episodes.KIND_SLOT)
    assert episodes.get_open_episode(manager, PERSONA_ID)["episode_id"] == ep1["episode_id"]

    # 2 件目 (work_session) を開くと「最後に開いた open」が返る
    ep2 = episodes.open_episode(manager, PERSONA_ID, episodes.KIND_WORK_SESSION)
    assert episodes.get_open_episode(manager, PERSONA_ID)["episode_id"] == ep2["episode_id"]

    # 内側を閉じると外側 (slot) が復元される
    episodes.close_episode(manager, PERSONA_ID, ep2["episode_ref"])
    assert episodes.get_open_episode(manager, PERSONA_ID)["episode_id"] == ep1["episode_id"]

    # kind 指定の低頻度経路も一致する
    assert episodes.get_open_episode(
        manager, PERSONA_ID, kind=episodes.KIND_SLOT)["episode_id"] == ep1["episode_id"]

    episodes.close_episode(manager, PERSONA_ID, ep1["episode_ref"])
    assert episodes.get_open_episode(manager, PERSONA_ID) is None


def test_get_open_episode_cache_survives_restart(session_factory):
    """プロセス再起動 (= 新 manager、キャッシュ空) でも DB から復元される。"""
    manager = _make_manager(session_factory)
    ep = episodes.open_episode(manager, PERSONA_ID, episodes.KIND_CONVERSATION)

    fresh_manager = SimpleNamespace(SessionLocal=session_factory)
    restored = episodes.get_open_episode(fresh_manager, PERSONA_ID)
    assert restored is not None
    assert restored["episode_id"] == ep["episode_id"]


def test_get_open_episode_uses_cache_without_db(session_factory):
    """2 回目以降の読み出しは DB を引かない (キャッシュヒット)。"""
    manager = _make_manager(session_factory)
    episodes.open_episode(manager, PERSONA_ID, episodes.KIND_SLOT)
    episodes.get_open_episode(manager, PERSONA_ID)  # キャッシュ形成 (open 済みだが念のため)

    def _boom():
        raise AssertionError("DB should not be hit on cache hit")

    manager.SessionLocal = _boom
    result = episodes.get_open_episode(manager, PERSONA_ID)
    assert result is not None


# ---------------------------------------------------------------------------
# 1. 会話の出来事 (シム経路)
# ---------------------------------------------------------------------------


def test_conversation_episode_opens_and_closes_on_virtual_clock(session_factory):
    manager = _make_manager(session_factory)
    driver = TrackSimUserEventDriver()

    start = datetime(2026, 7, 4, 15, 0, 0)
    clock.enable_virtual(start)
    driver.begin_conversation(manager, PERSONA_ID, "ただいま")

    rows = _episode_rows(session_factory)
    assert len(rows) == 1
    ep = rows[0]
    assert ep.KIND == episodes.KIND_CONVERSATION
    assert ep.STATUS == episodes.STATUS_OPEN
    # 全時刻が clock.now() 経由 (仮想クロックの epoch と一致)
    assert ep.STARTED_AT == _epoch(start)
    assert ep.ENDED_AT is None
    assert ep.BUILDING_ID == BUILDING
    assert PERSONA_ID in json.loads(ep.PARTICIPANTS_JSON)
    # occurrence_id: building + 開始時刻ベースの決定論
    assert ep.OCCURRENCE_ID == f"conv:{BUILDING}:{_epoch(start)}"

    # 会話継続中の再 message は開き直さない (冪等)
    clock.advance_to(datetime(2026, 7, 4, 15, 10, 0))
    driver.begin_conversation(manager, PERSONA_ID, "それでね")
    assert len(_episode_rows(session_factory)) == 1

    # leave → close (ended_at = 仮想時刻)
    end = datetime(2026, 7, 4, 15, 30, 0)
    clock.advance_to(end)
    assert driver.end_conversation(manager, PERSONA_ID) is True
    rows = _episode_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].STATUS == episodes.STATUS_CLOSED
    assert rows[0].ENDED_AT == _epoch(end)

    # 閉じた後の新しい会話は新しい出来事として開く
    clock.advance_to(datetime(2026, 7, 4, 16, 0, 0))
    driver.begin_conversation(manager, PERSONA_ID, "また来たよ")
    rows = _episode_rows(session_factory)
    assert len(rows) == 2
    assert rows[1].STATUS == episodes.STATUS_OPEN


def test_conversation_leave_without_conversation_creates_nothing(session_factory):
    manager = _make_manager(session_factory)
    driver = TrackSimUserEventDriver()
    assert driver.end_conversation(manager, PERSONA_ID) is False
    assert _episode_rows(session_factory) == []


def test_conversation_occurrence_shared_in_same_building(session_factory):
    """同一 Building で開いている会話は occurrence_id を共有する (複数主観 §8.1)。"""
    manager = _make_manager(session_factory, persona_ids=["alice", "bob"])
    clock.enable_virtual(datetime(2026, 7, 4, 15, 0, 0))
    ep_a = episodes.open_conversation_episode(
        manager, "alice", building_id="cafe", participants=["alice", "1"],
    )
    clock.advance_to(datetime(2026, 7, 4, 15, 0, 5))
    ep_b = episodes.open_conversation_episode(
        manager, "bob", building_id="cafe", participants=["bob", "1"],
    )
    assert ep_a["occurrence_id"] == ep_b["occurrence_id"]

    # 別 Building なら共有しない
    ep_c = episodes.open_conversation_episode(
        manager, "bob", building_id="library",
    )
    # bob には既に open な会話がある → 冪等で同じ行が返る (開き直さない)
    assert ep_c["episode_id"] == ep_b["episode_id"]

    episodes.close_conversation_episode(manager, "bob")
    ep_d = episodes.open_conversation_episode(
        manager, "bob", building_id="library",
    )
    assert ep_d["occurrence_id"] != ep_a["occurrence_id"]


# ---------------------------------------------------------------------------
# 2. 作業セッションの出来事 (run_work_session)
# ---------------------------------------------------------------------------

SPELL_NAME = "make_doc"


class FakeLLMClient:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        if not self.responses:
            raise AssertionError("FakeLLMClient: no scripted responses left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def consume_usage(self):
        return None


class FakeRuntime:
    """run_work_session が触る SEARuntime の最小フェイク (test_work_session と同型)。"""

    def __init__(self, llm_client):
        self.llm_client = llm_client
        self._store_seq = 0
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def _prepare_context(self, persona, building_id, user_input, requirements=None,
                         pulse_id=None, **kwargs):
        return [{"role": "system", "content": "HEAD"}]

    def _select_llm_client(self, node_def, persona, needs_structured_output=False,
                           state=None):
        return self.llm_client

    def select_llm_client(self, node_def, persona, execution_context=None,
                          needs_structured_output=False, state=None):
        model = execution_context.model_key if execution_context is not None else "fake-model"
        return self.llm_client, model

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _is_spell_enabled_for_persona(self, persona):
        return True

    def _dump_llm_io(self, playbook_name, node_id, persona, messages, output_text):
        return None

    def _accumulate_usage(self, state, model, input_tokens, output_tokens,
                          cost_usd, cached_tokens=0, cache_write_tokens=0):
        return None

    def _get_or_create_pulse_context(self, pulse_id):
        return PulseContext(pulse_id=pulse_id)

    def _flush_pulse_logs(self, persona, pulse_context):
        return None

    def _store_memory(self, persona, text, *, role="assistant", tags=None,
                      pulse_id=None, metadata=None, playbook_name=None,
                      pulse_context=None, line_role=None, line_id=None,
                      origin_track_id=None, scope=None, paired_action_text=None,
                      thought_signature=None, spell_origin_id=None, spell_seq=None,
                      return_message_id=False):
        self._store_seq += 1
        return f"msg-{self._store_seq}" if return_message_id else True


def _make_ws_manager(session_factory, responses: List[Any]) -> SimpleNamespace:
    manager = _make_manager(session_factory)
    manager.sea_runtime = FakeRuntime(FakeLLMClient(responses))
    return manager


def _make_fake_spell(session_factory, created_ids: List[str]):
    async def fake_spell(tool_name, tool_args, persona, state, playbook_name,
                         event_callback, messages=None):
        item_id = str(uuid.uuid4())
        now = datetime.utcnow()
        db = session_factory()
        try:
            db.add(Item(
                ITEM_ID=item_id, NAME=tool_args.get("name", "doc"), TYPE="document",
                DESCRIPTION="", CREATOR_ID=persona.persona_id,
                CREATED_AT=now, UPDATED_AT=now,
            ))
            db.commit()
        finally:
            db.close()
        created_ids.append(item_id)
        return (f"文書を作成しました。アイテムID: {item_id}", None)

    return fake_spell


def test_work_session_episode_open_close_with_meta_contract(session_factory):
    """open→close + meta 書式契約 (meta.title / meta.artifacts / digest_ref §14)。"""
    responses = [
        f"やるぞ。\n/spell name='{SPELL_NAME}' args={{\"name\": \"草稿\"}}",
        "できた。今日はここまで。",   # 締め (spell なし → finished)
        "草稿を 1 本書いた。",        # digest
    ]
    manager = _make_ws_manager(session_factory, responses)
    created_ids: List[str] = []
    start = datetime(2026, 7, 4, 10, 0, 0)
    clock.enable_virtual(start)

    with patch("sea.runtime_llm.SPELL_TOOL_NAMES", {SPELL_NAME}), \
         patch("sea.runtime_llm._run_spell_tool_async",
               new=_make_fake_spell(session_factory, created_ids)):
        result = run_work_session(
            PERSONA_ID, "草稿を書いてほしい。", 3,
            task_ref="task:1", manager=manager, title="草稿づくり",
        )

    assert result.ended_reason == ENDED_FINISHED
    rows = [r for r in _episode_rows(session_factory)
            if r.KIND == episodes.KIND_WORK_SESSION]
    assert len(rows) == 1
    ep = rows[0]
    assert ep.STATUS == episodes.STATUS_CLOSED
    assert ep.BUILDING_ID == BUILDING
    assert ep.STARTED_AT == _epoch(start)
    assert ep.ENDED_AT == _epoch(start)  # 仮想クロックは進めていない
    # 出自: slot 出来事が無いので task_ref
    assert ep.ORIGIN_REF == "task:1"
    meta = json.loads(ep.META_JSON)
    assert meta["title"] == "草稿づくり"
    assert meta["artifacts"] == created_ids
    assert meta["ended_reason"] == ENDED_FINISHED
    # digest 統合 (W1 Chunk C / D9): digest_ref=None で閉じる。再訪の鍵は
    # post_session 判断 → 配送 handler (episodes.set_digest_ref) が後段確定する。
    assert ep.DIGEST_REF is None


def test_work_session_episode_origin_is_open_slot_episode(session_factory):
    """コマ発火中 (open な slot 出来事あり) はそれが出自になる。"""
    responses = ["何もせず終える。", "何もしなかった。"]  # finish + digest
    manager = _make_ws_manager(session_factory, responses)
    slot_ep = episodes.open_episode(manager, PERSONA_ID, episodes.KIND_SLOT)

    result = run_work_session(PERSONA_ID, "様子を見る。", 2, manager=manager)

    assert result.ended_reason == ENDED_FINISHED
    ws = [r for r in _episode_rows(session_factory)
          if r.KIND == episodes.KIND_WORK_SESSION]
    assert len(ws) == 1
    assert ws[0].ORIGIN_REF == slot_ep["episode_ref"]
    # slot 出来事はまだ開いている (閉じるのは _fire_slot の責務)
    assert episodes.get_by_ref(
        manager, PERSONA_ID, slot_ep["episode_ref"])["status"] == episodes.STATUS_OPEN


def test_work_session_episode_closes_on_error(session_factory):
    """エラー終了 (途中 raise) でも出来事は closed になる。"""
    responses = [RuntimeError("llm down")]
    manager = _make_ws_manager(session_factory, responses)

    result = run_work_session(PERSONA_ID, "作業して。", 2, manager=manager)

    assert result.ended_reason == ENDED_ERROR
    rows = [r for r in _episode_rows(session_factory)
            if r.KIND == episodes.KIND_WORK_SESSION]
    assert len(rows) == 1
    assert rows[0].STATUS == episodes.STATUS_CLOSED
    meta = json.loads(rows[0].META_JSON)
    assert meta["ended_reason"] == ENDED_ERROR
    assert rows[0].DIGEST_REF is None


# ---------------------------------------------------------------------------
# 3. コマの出来事 (_fire_slot)
# ---------------------------------------------------------------------------


def _save_single_slot_plan(manager, slot: Dict[str, Any], plan_date="2026-07-04"):
    day_plan.save_day_plan(manager, PERSONA_ID, plan_date, [slot])
    return plan_date


def test_living_slot_creates_presence_episode_with_record_level(session_factory):
    manager = _make_manager(session_factory)
    start = datetime(2026, 7, 4, 12, 0, 0)
    clock.enable_virtual(start)
    plan_date = _save_single_slot_plan(manager, {
        "start": "12:00", "kind": "出かける", "ref": "none",
        "facility": BUILDING, "budget_rounds": 0, "title": "お昼をとる",
    })

    day_plan._fire_slot(manager, PERSONA_ID, plan_date, 0)

    rows = _episode_rows(session_factory)
    assert len(rows) == 1
    ep = rows[0]
    assert ep.KIND == episodes.KIND_PRESENCE
    assert ep.STATUS == episodes.STATUS_CLOSED
    assert ep.STARTED_AT == _epoch(start)
    assert ep.ORIGIN_REF == f"day_plan:{PERSONA_ID}:{plan_date}:0"
    meta = json.loads(ep.META_JSON)
    assert meta["title"] == "お昼をとる"
    assert meta["slot_kind"] == "出かける"
    # record_level (presence_only) がスタブハンドラから透過される
    assert meta["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY


def test_worker_slot_creates_slot_episode(session_factory):
    manager = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    plan_date = _save_single_slot_plan(manager, {
        "start": "10:00", "kind": "随筆を書く", "ref": "none",
        "facility": BUILDING, "budget_rounds": 2, "title": "下書きを書く",
    })

    fake_result = SimpleNamespace(
        digest="", artifacts=[], rounds_used=1, ended_reason="finished",
        usage={}, task_ref=None, error=None, started_at=None, ended_at=None,
    )
    with patch("sea.work_session.run_work_session", return_value=fake_result):
        day_plan._fire_slot(manager, PERSONA_ID, plan_date, 0)

    rows = _episode_rows(session_factory)
    assert len(rows) == 1
    ep = rows[0]
    assert ep.KIND == episodes.KIND_SLOT   # セッションと並存する「コマの区間」
    assert ep.STATUS == episodes.STATUS_CLOSED
    meta = json.loads(ep.META_JSON)
    assert meta["title"] == "下書きを書く"
    assert "record_level" not in meta      # セッション系コマは詳細記録あり


def test_skipped_slot_creates_no_episode(session_factory):
    """ハンドラ未登録で skipped になったコマは出来事を作らない。"""
    manager = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    plan_date = _save_single_slot_plan(manager, {
        "start": "10:00", "kind": "随筆を書く", "ref": "none",
        "facility": BUILDING, "budget_rounds": 2, "title": "下書きを書く",
    })

    saved_handlers = dict(day_plan._SLOT_HANDLERS)
    saved_gated = set(day_plan._BUDGET_GATED_KINDS)
    try:
        day_plan._SLOT_HANDLERS.pop("随筆を書く", None)
        day_plan._BUDGET_GATED_KINDS.discard("随筆を書く")
        day_plan._fire_slot(manager, PERSONA_ID, plan_date, 0)
    finally:
        day_plan._SLOT_HANDLERS.clear()
        day_plan._SLOT_HANDLERS.update(saved_handlers)
        day_plan._BUDGET_GATED_KINDS.clear()
        day_plan._BUDGET_GATED_KINDS.update(saved_gated)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, plan_date)
    assert slots[0]["status"] == day_plan.STATUS_SKIPPED
    assert _episode_rows(session_factory) == []


def test_handler_error_still_closes_slot_episode(session_factory):
    manager = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    plan_date = _save_single_slot_plan(manager, {
        "start": "10:00", "kind": "随筆を書く", "ref": "none",
        "facility": BUILDING, "budget_rounds": 2, "title": "下書きを書く",
    })

    with patch("sea.work_session.run_work_session", side_effect=RuntimeError("boom")):
        day_plan._fire_slot(manager, PERSONA_ID, plan_date, 0)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, plan_date)
    assert slots[0]["status"] == day_plan.STATUS_FIRED  # 既存挙動: fired のまま
    rows = _episode_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].STATUS == episodes.STATUS_CLOSED     # 区間は閉じる


# ---------------------------------------------------------------------------
# 4. 層0タグ: _store_memory の origin_episode 付与 (§9.1 層0)
# ---------------------------------------------------------------------------


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


@pytest.fixture
def layer0_env(session_factory, tmp_path, monkeypatch):
    """実 SAIMemoryAdapter + 実 SEARuntime._store_memory の層0テスト環境。

    test_marker_store_memory.py の流儀 (実 memory.db + Embedder patch)。
    """
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    persona_dir = tmp_path / "personas" / "tester"
    persona_dir.mkdir(parents=True, exist_ok=True)

    with patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter
        from sea.runtime import SEARuntime

        _seed_personas(session_factory, ["tester"])
        adapter = SAIMemoryAdapter(
            "tester", persona_dir=persona_dir, resource_id="tester",
        )
        manager = SimpleNamespace(
            building_histories={},
            SessionLocal=session_factory,
        )
        runtime = SEARuntime(manager)
        persona = SimpleNamespace(
            persona_id="tester",
            sai_memory=adapter,
            current_building_id=None,
        )
        try:
            yield manager, runtime, persona, adapter
        finally:
            adapter.close()


def _message_metadata(adapter, message_id: str) -> Optional[Dict[str, Any]]:
    with adapter._db_lock:
        row = adapter.conn.execute(
            "SELECT metadata FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    assert row is not None
    return json.loads(row[0]) if row[0] else None


def test_store_memory_attaches_origin_episode_when_open(layer0_env):
    manager, runtime, persona, adapter = layer0_env
    ep = episodes.open_episode(manager, "tester", episodes.KIND_CONVERSATION)

    mid = runtime._store_memory(
        persona, "会話の中のひとこと。", role="assistant", return_message_id=True,
    )
    assert mid
    meta = _message_metadata(adapter, mid)
    assert meta is not None
    assert meta["origin_episode"] == ep["episode_ref"]

    # user role (spell 結果等) にも付く (層0はメッセージ全体への決定論継承)
    mid_user = runtime._store_memory(
        persona, "[結果] なにかの出力", role="user", return_message_id=True,
    )
    meta_user = _message_metadata(adapter, mid_user)
    assert meta_user["origin_episode"] == ep["episode_ref"]


def test_store_memory_no_origin_episode_when_none_open(layer0_env):
    manager, runtime, persona, adapter = layer0_env

    mid = runtime._store_memory(
        persona, "出来事の外のひとこと。", role="assistant", return_message_id=True,
    )
    meta = _message_metadata(adapter, mid)
    assert meta is None or "origin_episode" not in meta

    # 開いて閉じた後も付かない (キャッシュが close で無効化される)
    ep = episodes.open_episode(manager, "tester", episodes.KIND_SLOT)
    episodes.close_episode(manager, "tester", ep["episode_ref"])
    mid2 = runtime._store_memory(
        persona, "閉じた後のひとこと。", role="assistant", return_message_id=True,
    )
    meta2 = _message_metadata(adapter, mid2)
    assert meta2 is None or "origin_episode" not in meta2
