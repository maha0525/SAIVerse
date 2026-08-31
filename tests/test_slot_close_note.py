"""コマ締めの一手 (T4: 帰属判定 + 経験値ノート) のテスト。

- timetable_redesign.md §5.4: 帰属は事後・実績ベース (事前 ref は参考情報)
- experience_ledger.md §4: 経験値ノート (空 note は正常応答 = 充填の禁忌)
- experience_ledger.md §5: テーマページの lazy creation (初経験がページを開く)
- recall_tags_and_track_reduction.md §9.4: 帰属タグは purpose_tags 棚 (層2 同族)

検証面は二つ:
1. sea/work_session.py の close_hook 契約 — セッション成功後・Beat ロック内で
   一度だけ呼ばれ、フックの失敗はセッション結果を壊さない
2. saiverse/slot_close.py の締め本体 — enum が実在参照のみ / ノートがテーマ
   ページ fragment へ (lazy creation・追記) / 帰属タグが載る / 空 note・失敗の
   フェイルオープン

fixtures は tests/test_track_slot_ref.py (main DB 側) と in-memory sqlite の
memory.db スタブの流儀。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, ActionTrack, Base, City, User
from saiverse import clock
from saiverse import slot_close
from saiverse.persona_task_manager import (
    PARENT_TRACK,
    STAGE_CANDIDATE,
    PersonaTaskManager,
)
from sea.work_session import (
    ENDED_FINISHED,
    SessionCloseContext,
    run_work_session,
)

PERSONA_ID = "alice"
PLAN_DATE = "2026-08-03"
BASE = datetime(2026, 8, 3, 9, 0, 0)
EPISODE_REF = "episode:7"


# ---------------------------------------------------------------------------
# fixtures: main DB (目的ノード) + memory.db スタブ (テーマページ / タグ棚)
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
def _virtual_clock():
    clock.enable_virtual(BASE)
    yield
    clock.disable_virtual()


class StubMemoryAdapter:
    """memory.db 相当の in-memory スタブ (memopedia + purpose_tags 実テーブル)。

    add_purpose_tag は実 adapter (saiverse_memory/adapter.py) と同じ経路
    (sai_memory.purpose_tags.add_tag) を通す。
    """

    def __init__(self):
        from sai_memory.memopedia.storage import init_memopedia_tables
        from sai_memory.purpose_tags import init_purpose_tags_tables

        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_memopedia_tables(self.conn)
        init_purpose_tags_tables(self.conn)
        self._db_lock = threading.RLock()

    def is_ready(self):
        return True

    def add_purpose_tag(self, target_ref, purpose_ref, layer):
        from sai_memory.purpose_tags import add_tag

        with self._db_lock:
            add_tag(
                self.conn, target_ref=str(target_ref),
                purpose_ref=str(purpose_ref), layer=int(layer),
            )
        return True


@pytest.fixture
def adapter():
    return StubMemoryAdapter()


@pytest.fixture
def persona(adapter):
    return SimpleNamespace(
        persona_id=PERSONA_ID,
        persona_name="Alice",
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=adapter,
    )


@pytest.fixture
def manager(session_factory, persona):
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

    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        sea_runtime=None,  # close 単体テストでは close_ctx.runtime を使う
    )

    # タスクの親になる Track 行。TrackManager は 2026-08-22 (束 6c) に退役したので、
    # 旧データ相当の ActionTrack 行を ORM で直接置く。生きた目的ノードとして
    # 締めに出るのは配下の task:1 / task:2 だけ。
    track_id = str(uuid.uuid4())
    db = session_factory()
    try:
        db.add(ActionTrack(
            track_id=track_id, persona_id=PERSONA_ID, short_id=1,
            title="言葉の標本集", track_type="autonomous", status="running",
        ))
        db.commit()
    finally:
        db.close()

    ptm = PersonaTaskManager(session_factory)
    ptm.create_task(
        persona_id=PERSONA_ID, title="序文の下書き", goal="書き出しを決める",
        parent_kind=PARENT_TRACK, track_id=track_id, auto_activate=False,
    )
    ptm.create_task(
        persona_id=PERSONA_ID, title="雲の写真を集めたい",
        stage=STAGE_CANDIDATE, auto_activate=False,
    )
    return mgr


# ---------------------------------------------------------------------------
# fakes: 締めコール用の runtime / LLM クライアント
# ---------------------------------------------------------------------------


class FakeCloseClient:
    """締めの構造化出力 1 発を返す mock LLM (呼び出しを記録する)。"""

    def __init__(self, response: Any):
        self.response = response
        self.calls: List[Dict[str, Any]] = []

    def generate(self, messages, tools=None, temperature=None,
                 response_schema=None, **kwargs):
        self.calls.append({
            "messages": list(messages),
            "response_schema": response_schema,
        })
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def consume_usage(self):
        return None


class FakeCloseRuntime:
    """run_slot_close が触る SEARuntime の最小フェイク。"""

    def __init__(self, client: FakeCloseClient):
        self.client = client
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def select_llm_client(self, node_def, persona, execution_context=None,
                          needs_structured_output=False, state=None):
        model = execution_context.model_key if execution_context is not None else "fake"
        return self.client, model

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, playbook_name, node_id, persona, messages, output_text):
        return None

    def _accumulate_usage(self, state, model, input_tokens, output_tokens,
                          cost_usd, cached_tokens=0, cache_write_tokens=0):
        return None


def _make_ctx(manager, persona, client, *, episode_ref=EPISODE_REF,
              continuation="できたよ。"):
    return SessionCloseContext(
        manager=manager,
        persona=persona,
        runtime=FakeCloseRuntime(client),
        llm_client=client,
        execution_context=SimpleNamespace(model_key="fake-model"),
        state={},
        messages=[
            {"role": "system", "content": "HEAD"},
            {"role": "user", "content": "<system>指示書</system>"},
        ],
        pulse_ctx=None,
        building_id="alice_room",
        episode_ref=episode_ref,
        artifacts=[],
        rounds_used=1,
        ended_reason=ENDED_FINISHED,
        final_continuation=continuation,
    )


def _slot(**over):
    slot = {
        "start": "10:00", "kind": "調べる", "ref": "task:1",
        "facility": "own_room", "budget_rounds": 4,
        "title": "気になることを調べる", "note": "続きから",
    }
    slot.update(over)
    return slot


def _run_close(manager, persona, response, *, slot=None, episode_ref=EPISODE_REF):
    client = FakeCloseClient(response)
    ctx = _make_ctx(manager, persona, client, episode_ref=episode_ref)
    slot_close.run_slot_close(
        ctx, manager=manager, persona_id=PERSONA_ID,
        plan_date_str=PLAN_DATE, slot=slot or _slot(), index=0,
    )
    return client


def _theme_pages(adapter):
    from sai_memory.theme_pages import CATEGORY_THEME, ROOT_THEME_ID

    return adapter.conn.execute(
        "SELECT id, title FROM memopedia_pages WHERE parent_id = ? AND category = ?",
        (ROOT_THEME_ID, CATEGORY_THEME),
    ).fetchall()


def _fragments(adapter, page_id):
    return adapter.conn.execute(
        "SELECT content, source_date FROM memopedia_fragments WHERE entity_id = ? "
        "ORDER BY created_at",
        (page_id,),
    ).fetchall()


def _tags(adapter):
    return adapter.conn.execute(
        "SELECT target_ref, purpose_ref, layer FROM purpose_tags"
    ).fetchall()


# ---------------------------------------------------------------------------
# enum / プロンプト
# ---------------------------------------------------------------------------


def test_belongs_to_enum_is_live_refs_plus_none_new(manager, persona, adapter):
    """belongs_to enum = 実在の生きたタスク + none + new。

    2026-08-21: 欲求候補 (task:2) と関心 (track:1) は供給源ごと退役したので、
    enum にも一覧にも載らない。
    """
    client = _run_close(manager, persona, {"belongs_to": "none", "note": ""})

    schema = client.calls[0]["response_schema"]
    enum = schema["properties"]["belongs_to"]["enum"]
    # collect_slot_ref_enum の順序: バックログタスク → none、+ new
    assert enum == ["task:1", "none", "new"]


def test_close_prompt_grounding_and_prior_ref(manager, persona, adapter):
    """プロンプトに接地条件・充填の禁忌・事前 ref (参考)・選択肢一覧が載り、
    セッションの最終応答が assistant として文脈に積まれている。"""
    client = _run_close(manager, persona, {"belongs_to": "none", "note": ""})

    messages = client.calls[0]["messages"]
    # 締めプロンプトの直前 = セッションの最終応答 (spell ループは積まないので締めが積む)
    assert messages[-2] == {"role": "assistant", "content": "できたよ。"}
    prompt = messages[-1]["content"]
    assert messages[-1]["role"] == "user"
    assert "実際にやったこと" in prompt                    # 接地条件
    assert "空のままでかまいません" in prompt              # 充填の禁忌
    assert "task:1" in prompt and "序文の下書き" in prompt  # 選択肢一覧 (題つき)
    # 欲求候補・関心は退役したので一覧に載らない (2026-08-21)
    assert "雲の写真を集めたい" not in prompt
    assert "言葉の標本集" not in prompt
    assert "事前の予定では、このコマは task:1" in prompt   # 事前 ref は参考情報
    assert "後の自分のために" in prompt                    # 経験値ノートの意義 (§4)


# ---------------------------------------------------------------------------
# 経験値ノート → テーマページ fragment (lazy creation)
# ---------------------------------------------------------------------------


def test_note_lazy_creates_theme_page_and_fragment(manager, persona, adapter):
    """初経験の締めでテーマページが立ち、ノートが fragment として書かれる。"""
    note = "検索語を変えたら一次情報に届いた。次は公式ドキュメントを読む。"
    _run_close(manager, persona, {"belongs_to": "task:1", "note": note})

    pages = _theme_pages(adapter)
    assert len(pages) == 1
    page_id, title = pages[0]
    assert title == "調べる"  # カタログの kind 名がページタイトル

    frags = _fragments(adapter, page_id)
    assert len(frags) == 1
    content, source_date = frags[0]
    assert note in content
    # 由来注記 (slot の日付・出来事) が末尾に付く — 本文の切り詰めではない
    assert PLAN_DATE in content
    assert EPISODE_REF in content
    assert source_date == PLAN_DATE


def test_second_note_appends_to_same_page(manager, persona, adapter):
    """二回目の締めは同じテーマページへの追記 (ページは増えない)。"""
    _run_close(manager, persona, {"belongs_to": "none", "note": "一回目の学び。"})
    _run_close(manager, persona, {"belongs_to": "none", "note": "二回目の学び。"})

    pages = _theme_pages(adapter)
    assert len(pages) == 1
    frags = _fragments(adapter, pages[0][0])
    assert len(frags) == 2
    assert "一回目の学び。" in frags[0][0]
    assert "二回目の学び。" in frags[1][0]


def test_empty_note_writes_nothing(manager, persona, adapter):
    """空 note は正常応答 — ページも fragment も作らない (充填の禁忌)。"""
    _run_close(manager, persona, {"belongs_to": "task:1", "note": ""})

    assert _theme_pages(adapter) == []


def test_json_string_response_is_parsed(manager, persona, adapter):
    """generate が JSON 文字列で返すクライアントでも解釈できる (sluice と同型)。"""
    _run_close(manager, persona, json.dumps(
        {"belongs_to": "task:1", "note": "文字列経由の学び。"}, ensure_ascii=False,
    ))

    pages = _theme_pages(adapter)
    assert len(pages) == 1
    assert _tags(adapter) == [(EPISODE_REF, "task:1", 2)]


# ---------------------------------------------------------------------------
# 帰属タグ → purpose_tags 棚
# ---------------------------------------------------------------------------


def test_attribution_tag_written(manager, persona, adapter):
    """belongs_to の実在参照が層2 (棚入れ) タグとして episode に載る。"""
    _run_close(manager, persona, {"belongs_to": "task:1", "note": ""})

    from sai_memory.purpose_tags import LAYER_SHELVE

    assert _tags(adapter) == [(EPISODE_REF, "task:1", LAYER_SHELVE)]


def test_none_and_new_write_no_tag(manager, persona, adapter):
    """"none" / "new" はタグを書かない ("new" は v1 ではログのみ)。"""
    _run_close(manager, persona, {"belongs_to": "none", "note": ""})
    _run_close(manager, persona, {"belongs_to": "new", "note": "新しい何かの芽。"})

    assert _tags(adapter) == []
    # "new" でもノート自体は書かれる (帰属とノートは独立)
    pages = _theme_pages(adapter)
    assert len(pages) == 1


def test_invalid_belongs_to_falls_back_to_none(manager, persona, adapter):
    """enum に無い参照が返ったら none 扱い (タグなし)。ノートは生きる。"""
    _run_close(manager, persona, {"belongs_to": "task:99", "note": "学び。"})

    assert _tags(adapter) == []
    assert len(_theme_pages(adapter)) == 1


def test_no_episode_ref_skips_tag_but_keeps_note(manager, persona, adapter):
    """episode が開けなかったセッションではタグ対象が無い — スキップしノートは書く。"""
    _run_close(
        manager, persona, {"belongs_to": "task:1", "note": "学び。"},
        episode_ref=None,
    )

    assert _tags(adapter) == []
    assert len(_theme_pages(adapter)) == 1


# ---------------------------------------------------------------------------
# フェイルオープン
# ---------------------------------------------------------------------------


def test_llm_failure_writes_nothing_and_does_not_raise(manager, persona, adapter):
    """締めコールの失敗は raise せず、何も書かない (コマ完了を壊さない)。"""
    _run_close(manager, persona, RuntimeError("boom"))

    assert _theme_pages(adapter) == []
    assert _tags(adapter) == []


def test_adapter_not_ready_skips_before_llm(manager, persona, adapter):
    """書き先 (memory.db) が無ければ LLM を焚く前に見送る。"""
    persona.sai_memory = SimpleNamespace()  # is_ready も conn も無い軽量スタブ
    client = FakeCloseClient({"belongs_to": "task:1", "note": "学び。"})
    ctx = _make_ctx(manager, persona, client)

    slot_close.run_slot_close(
        ctx, manager=manager, persona_id=PERSONA_ID,
        plan_date_str=PLAN_DATE, slot=_slot(), index=0,
    )

    assert client.calls == []


# ---------------------------------------------------------------------------
# 締めの結果 (close_outcome — Codex 一巡目 #3/#5)
# ---------------------------------------------------------------------------


def _run_close_outcome(manager, persona, response, *, episode_ref=EPISODE_REF):
    client = FakeCloseClient(response)
    ctx = _make_ctx(manager, persona, client, episode_ref=episode_ref)
    return slot_close.run_slot_close(
        ctx, manager=manager, persona_id=PERSONA_ID,
        plan_date_str=PLAN_DATE, slot=_slot(), index=0,
    )


def test_outcome_done_on_success(manager, persona, adapter):
    """帰属タグ + ノートが書けたら done。"""
    outcome = _run_close_outcome(
        manager, persona, {"belongs_to": "task:1", "note": "学び。"},
    )
    assert outcome == slot_close.CLOSE_OUTCOME_DONE


def test_outcome_done_when_nothing_to_write(manager, persona, adapter):
    """belongs=none + 空 note も正常な締め (done) — 「書かない」は判断の結果。"""
    outcome = _run_close_outcome(manager, persona, {"belongs_to": "none", "note": ""})
    assert outcome == slot_close.CLOSE_OUTCOME_DONE


def test_outcome_failed_on_llm_error(manager, persona, adapter):
    """締めコールの失敗は failed — post_session の帰属代替に道を残す。"""
    outcome = _run_close_outcome(manager, persona, RuntimeError("boom"))
    assert outcome == slot_close.CLOSE_OUTCOME_FAILED


def test_outcome_failed_when_tag_unpersistable(manager, persona, adapter):
    """帰属先は出たのに書けない (episode 無し) — 「済み」にしない (failed)。"""
    outcome = _run_close_outcome(
        manager, persona, {"belongs_to": "task:1", "note": ""}, episode_ref=None,
    )
    assert outcome == slot_close.CLOSE_OUTCOME_FAILED


def test_outcome_skipped_no_memory(manager, persona, adapter):
    """memory.db が使えないときは skipped_no_memory (LLM も焚かない)。"""
    persona.sai_memory = SimpleNamespace()
    client = FakeCloseClient({"belongs_to": "none", "note": ""})
    ctx = _make_ctx(manager, persona, client)
    outcome = slot_close.run_slot_close(
        ctx, manager=manager, persona_id=PERSONA_ID,
        plan_date_str=PLAN_DATE, slot=_slot(), index=0,
    )
    assert outcome == slot_close.CLOSE_OUTCOME_SKIPPED_NO_MEMORY


def test_make_close_hook_persists_outcome(manager, persona, adapter):
    """フックは成否を問わず close_outcome を slot に永続化する。"""
    written: Dict[str, Any] = {}

    def fake_update(mgr, pid, date, index, *, expected_id=None, **fields):
        written.update(fields)
        return None

    hook = slot_close.make_close_hook(
        manager, PERSONA_ID, PLAN_DATE, _slot(id="s1"), 0,
    )
    client = FakeCloseClient({"belongs_to": "none", "note": ""})
    ctx = _make_ctx(manager, persona, client)
    with patch("saiverse.day_plan._update_slot", side_effect=fake_update):
        hook(ctx)

    assert written.get("close_outcome") == slot_close.CLOSE_OUTCOME_DONE


def test_make_close_hook_persists_failed_outcome(manager, persona, adapter):
    """締めが失敗しても結果 (failed) は状態として残る — 欠落を沈黙させない。"""
    written: Dict[str, Any] = {}

    def fake_update(mgr, pid, date, index, *, expected_id=None, **fields):
        written.update(fields)
        return None

    hook = slot_close.make_close_hook(
        manager, PERSONA_ID, PLAN_DATE, _slot(id="s1"), 0,
    )
    client = FakeCloseClient(RuntimeError("boom"))
    ctx = _make_ctx(manager, persona, client)
    with patch("saiverse.day_plan._update_slot", side_effect=fake_update):
        hook(ctx)

    assert written.get("close_outcome") == slot_close.CLOSE_OUTCOME_FAILED


# ---------------------------------------------------------------------------
# work_session の close_hook 契約
# ---------------------------------------------------------------------------


class _WsFakeLLMClient:
    """スペルなし応答 1 発で自然終了させる mock LLM。"""

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        return "書き上げた。今日はここまで。"

    def consume_usage(self):
        return None


class _WsFakeRuntime:
    """run_work_session が触る SEARuntime の最小フェイク (spell 無効経路)。

    tests/test_work_session.py の FakeRuntime の縮約 — 締めフック契約の検証には
    スペルループが不要なため、spell を無効にして初回応答 = 最終応答で回す。
    """

    def __init__(self):
        self.client = _WsFakeLLMClient()
        self.stored: List[Dict[str, Any]] = []
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def _prepare_context(self, persona, building_id, user_input, requirements=None,
                         pulse_id=None, **kwargs):
        return [{"role": "system", "content": "HEAD"}]

    def select_llm_client(self, node_def, persona, execution_context=None,
                          needs_structured_output=False, state=None):
        model = execution_context.model_key if execution_context is not None else "fake"
        return self.client, model

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _is_spell_enabled_for_persona(self, persona):
        return False  # スペルループを回さない (締めフック契約の検証に不要)

    def _dump_llm_io(self, playbook_name, node_id, persona, messages, output_text):
        return None

    def _accumulate_usage(self, state, model, input_tokens, output_tokens,
                          cost_usd, cached_tokens=0, cache_write_tokens=0):
        return None

    def _get_or_create_pulse_context(self, pulse_id):
        from sea.pulse_context import PulseContext

        return PulseContext(pulse_id=pulse_id)

    def _flush_pulse_logs(self, persona, pulse_context):
        return None

    def _store_memory(self, persona, text, **kwargs):
        self.stored.append({"text": text, **kwargs})
        return True


def _ws_env(session_factory, persona):
    runtime = _WsFakeRuntime()
    manager = SimpleNamespace(
        personas={persona.persona_id: persona},
        sea_runtime=runtime,
        SessionLocal=session_factory,
    )
    return manager, runtime


def test_close_hook_fires_after_successful_session(session_factory, persona):
    """締めフックはセッション成功後に一度だけ、熱い文脈つきで呼ばれる。"""
    manager, runtime = _ws_env(session_factory, persona)
    captured: List[Any] = []

    result = run_work_session(
        PERSONA_ID, "テスト用の草稿を書く。", 3,
        manager=manager, close_hook=captured.append,
    )

    assert result.ended_reason == ENDED_FINISHED
    assert len(captured) == 1
    ctx = captured[0]
    assert isinstance(ctx, SessionCloseContext)
    assert ctx.final_continuation == "書き上げた。今日はここまで。"
    assert ctx.ended_reason == ENDED_FINISHED
    # messages はセッションの生文脈 (head + 指示書)
    assert ctx.messages[0]["content"] == "HEAD"
    assert "テスト用の草稿を書く。" in ctx.messages[-1]["content"]
    assert ctx.episode_ref == result.episode_ref


def test_close_hook_failure_keeps_session_result(session_factory, persona):
    """締めフックの失敗はセッションの完了 (結果) を壊さない。"""
    manager, runtime = _ws_env(session_factory, persona)

    def broken_hook(ctx):
        raise RuntimeError("close boom")

    result = run_work_session(
        PERSONA_ID, "テスト用の草稿を書く。", 3,
        manager=manager, close_hook=broken_hook,
    )

    assert result.ended_reason == ENDED_FINISHED
    assert result.error is None


def test_no_hook_is_noop(session_factory, persona):
    """close_hook 省略時は従来挙動のまま (回帰)。"""
    manager, runtime = _ws_env(session_factory, persona)
    result = run_work_session(
        PERSONA_ID, "テスト用の草稿を書く。", 3, manager=manager,
    )
    assert result.ended_reason == ENDED_FINISHED


# ---------------------------------------------------------------------------
# day_plan の配線
# ---------------------------------------------------------------------------


def test_worker_slot_passes_close_hook(manager, persona):
    """作業セッション系コマは run_work_session に締めフックを渡す (v1 スコープ)。"""
    from saiverse import day_plan

    seen: Dict[str, Any] = {}

    def fake_ws(persona_id, instruction, budget_rounds, task_ref=None,
                metadata=None, *, manager=None, track_id=None, title=None,
                close_hook=None):
        seen["close_hook"] = close_hook
        return SimpleNamespace(
            artifacts=[], rounds_used=1, ended_reason="finished",
            task_ref=task_ref, track_id=track_id, episode_ref=None,
        )

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        day_plan.run_worker_slot_session(manager, PERSONA_ID, PLAN_DATE, _slot(), 0)

    assert callable(seen.get("close_hook"))


def _ws_result(**over):
    base = dict(
        artifacts=[], rounds_used=1, ended_reason="finished",
        task_ref=None, track_id=None, episode_ref="episode:7", error=None,
        budget_rounds=4,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_close_hook_hands_outcome_in_process(manager, persona, adapter):
    """フックは結果を hook.last_outcome でも手渡す (slot 永続化の成否に依らない)。

    close_outcome の slot 書き込みが CAS 競合等で欠けても、帰属抑止の判定は
    この in-process 値が担う (Codex 四巡目 #2)。
    """
    hook = slot_close.make_close_hook(
        manager, PERSONA_ID, PLAN_DATE, _slot(id="s1"), 0,
    )
    assert hook.last_outcome is None
    client = FakeCloseClient({"belongs_to": "none", "note": ""})
    ctx = _make_ctx(manager, persona, client)
    # slot への永続化が全滅しても in-process 値は残る
    with patch("saiverse.day_plan._update_slot", return_value=None):
        hook(ctx)
    assert hook.last_outcome == slot_close.CLOSE_OUTCOME_DONE


def test_worker_slot_prefers_in_process_outcome_over_reload(manager, persona):
    """result に載った in-process の締め結果が読み戻しより優先される (#2)。"""
    from saiverse import day_plan

    captured: Dict[str, Any] = {}

    def fake_fire(mgr, pid, kind, context):
        captured.update(context)

    result = _ws_result()
    result.close_outcome_inproc = slot_close.CLOSE_OUTCOME_DONE
    with patch.object(day_plan, "run_worker_slot_session", return_value=result), \
         patch.object(day_plan, "_reload_slot_field",
                      side_effect=AssertionError("reload should not be needed")), \
         patch("saiverse.autonomy_wiring.fire_judgment_point",
               side_effect=fake_fire):
        day_plan._handle_worker_slot(
            manager, PERSONA_ID, PLAN_DATE, _slot(id="s1"), 0,
        )

    assert captured.get("episode_attribution_done") is True


def test_worker_slot_suppresses_shelving_when_close_done(manager, persona):
    """帰属が締めで確定済み → post_session context に済みフラグが立つ (#5)。"""
    from saiverse import day_plan

    captured: Dict[str, Any] = {}

    def fake_fire(mgr, pid, kind, context):
        captured.update(context)

    with patch.object(day_plan, "run_worker_slot_session",
                      return_value=_ws_result()), \
         patch.object(day_plan, "_reload_slot_field",
                      return_value=slot_close.CLOSE_OUTCOME_DONE), \
         patch("saiverse.autonomy_wiring.fire_judgment_point",
               side_effect=fake_fire):
        day_plan._handle_worker_slot(
            manager, PERSONA_ID, PLAN_DATE, _slot(id="s1"), 0,
        )

    assert captured.get("episode_attribution_done") is True


def test_worker_slot_keeps_shelving_when_close_failed(manager, persona):
    """締めが失敗したセッションでは棚入れを抑止しない (帰属の代替経路)。"""
    from saiverse import day_plan

    captured: Dict[str, Any] = {}

    def fake_fire(mgr, pid, kind, context):
        captured.update(context)

    with patch.object(day_plan, "run_worker_slot_session",
                      return_value=_ws_result()), \
         patch.object(day_plan, "_reload_slot_field",
                      return_value=slot_close.CLOSE_OUTCOME_FAILED), \
         patch("saiverse.autonomy_wiring.fire_judgment_point",
               side_effect=fake_fire):
        day_plan._handle_worker_slot(
            manager, PERSONA_ID, PLAN_DATE, _slot(id="s1"), 0,
        )

    assert captured.get("episode_attribution_done") is False


def test_worker_slot_records_not_run_on_session_error(manager, persona):
    """エラー終了 (close_hook 不発) は not_run_session_error を状態に残す (#3)。"""
    from saiverse import day_plan

    written: Dict[str, Any] = {}
    captured: Dict[str, Any] = {}

    def fake_update(mgr, pid, date, index, *, expected_id=None, **fields):
        written.update(fields)
        return None

    def fake_fire(mgr, pid, kind, context):
        captured.update(context)

    with patch.object(day_plan, "run_worker_slot_session",
                      return_value=_ws_result(
                          ended_reason="error", error="RuntimeError: boom")), \
         patch.object(day_plan, "_reload_slot_field", return_value=None), \
         patch.object(day_plan, "_update_slot", side_effect=fake_update), \
         patch("saiverse.autonomy_wiring.fire_judgment_point",
               side_effect=fake_fire):
        day_plan._handle_worker_slot(
            manager, PERSONA_ID, PLAN_DATE, _slot(id="s1"), 0,
        )

    assert written.get("close_outcome") == slot_close.CLOSE_OUTCOME_NOT_RUN_SESSION_ERROR
    assert captured.get("episode_attribution_done") is False
