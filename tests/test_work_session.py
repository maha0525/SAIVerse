"""予算付き作業セッションランナー (sea/work_session.py) のテスト。

mock LLM クライアント + spell 実行の patch で run_work_session を end-to-end に
検証する。tools のロードは動的なので、spell 登録は sea.runtime_llm 名前空間の
SPELL_TOOL_NAMES / _run_spell_tool_async を patch で差し替える (既存テスト
test_pre_spells_dynamic_args.py と同じ流儀)。

- 予算上限で打ち切られ ended_reason='budget_exhausted'
- スペルなし応答で自然終了 ended_reason='finished'
- mock ツールが作った Item の ref が artifacts に入る (既存 Item は入らない)
- ダイジェスト 1 件だけ committed、生ログのラウンドは committed に入らない
- 例外時 ended_reason='error' で結果が返る (LOGGER に残る)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Item
from sea.pulse_context import Aspect, PulseContext
from sea.work_session import (
    ENDED_BUDGET_EXHAUSTED,
    ENDED_ERROR,
    ENDED_FINISHED,
    run_work_session,
)

SPELL_NAME = "make_doc"


def _spell_line(doc_name: str) -> str:
    return (
        f"よし、やるぞ。\n/spell name='{SPELL_NAME}' args={{\"name\": \"{doc_name}\"}}"
    )


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """スクリプト化された応答を順に返す mock LLM クライアント。"""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[List[Dict[str, Any]]] = []

    def generate(self, messages, tools=None, temperature=None, response_schema=None, **kwargs):
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("FakeLLMClient: no scripted responses left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def consume_usage(self):
        return None


class FakeRuntime:
    """run_work_session / _run_spell_loop が触る SEARuntime の最小フェイク。

    _store_memory は実物 (sea/runtime.py) と同じ解決規則で scope を決めて
    記録する: 明示引数が勝ち、無ければ pulse_context の active LineFrame から。
    """

    def __init__(self, llm_client: FakeLLMClient):
        self.llm_client = llm_client
        self.stored: List[Dict[str, Any]] = []
        self.flushed: List[Any] = []
        self.selected_aspects: List[Any] = []
        self._store_seq = 0

    # --- context / client -------------------------------------------------
    def _prepare_context(self, persona, building_id, user_input, requirements,
                         pulse_id=None, **kwargs):
        assert requirements.history_depth == 0  # セッションは履歴なしで開始する
        return [{"role": "system", "content": "HEAD"}]

    def _select_llm_client(self, node_def, persona, needs_structured_output=False,
                           state=None):
        # 選択時点の active LineFrame の aspect を記録 (WORKER 検証用)
        aspect = None
        if state is not None:
            ctx = state.get("_pulse_context")
            if ctx is not None and ctx.current_line() is not None:
                aspect = ctx.current_line().aspect
        self.selected_aspects.append(aspect)
        return self.llm_client

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _is_spell_enabled_for_persona(self, persona):
        return True

    # --- logging / usage ---------------------------------------------------
    def _dump_llm_io(self, playbook_name, node_id, persona, messages, output_text):
        return None

    def _accumulate_usage(self, state, model, input_tokens, output_tokens,
                          cost_usd, cached_tokens=0, cache_write_tokens=0):
        return None

    def _touch_anchor_after_llm_call(self, persona, usage):
        return None

    # --- pulse context / memory --------------------------------------------
    def _get_or_create_pulse_context(self, pulse_id, thread_id):
        return PulseContext(pulse_id=pulse_id, thread_id=thread_id)

    def _flush_pulse_logs(self, persona, pulse_context):
        self.flushed.append(pulse_context)

    def _store_memory(self, persona, text, *, role="assistant", tags=None,
                      pulse_id=None, metadata=None, playbook_name=None,
                      pulse_context=None, line_role=None, line_id=None,
                      origin_track_id=None, scope=None, paired_action_text=None,
                      thought_signature=None, spell_origin_id=None, spell_seq=None,
                      return_message_id=False):
        resolved_scope = scope
        resolved_line_role = line_role
        if pulse_context is not None and (resolved_scope is None or resolved_line_role is None):
            meta = pulse_context.current_line_metadata()
            if resolved_scope is None:
                resolved_scope = meta.get("scope")
            if resolved_line_role is None:
                resolved_line_role = meta.get("line_role")
        self.stored.append({
            "text": text,
            "role": role,
            "tags": list(tags or []),
            "scope": resolved_scope,
            "line_role": resolved_line_role,
            "metadata": metadata,
            "playbook_name": playbook_name,
        })
        self._store_seq += 1
        return f"msg-{self._store_seq}" if return_message_id else True


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def persona():
    return SimpleNamespace(
        persona_id="p1",
        persona_name="Persona",
        current_building_id="b1",
        sai_memory=SimpleNamespace(get_current_thread=lambda: "p1:persona_main"),
    )


def _make_env(session_factory, persona, responses: List[Any]):
    client = FakeLLMClient(responses)
    runtime = FakeRuntime(client)
    manager = SimpleNamespace(
        personas={persona.persona_id: persona},
        sea_runtime=runtime,
        SessionLocal=session_factory,
    )
    return manager, runtime, client


def _insert_item(session_factory, creator_id: str, name: str,
                 created_at: Optional[datetime] = None) -> str:
    item_id = str(uuid.uuid4())
    ts = created_at or datetime.utcnow()
    db = session_factory()
    try:
        db.add(Item(
            ITEM_ID=item_id, NAME=name, TYPE="document", DESCRIPTION="",
            CREATOR_ID=creator_id, CREATED_AT=ts, UPDATED_AT=ts,
        ))
        db.commit()
    finally:
        db.close()
    return item_id


def _make_fake_spell(session_factory, created_ids: List[str]):
    """document_create 相当の mock spell。Item を DB に実際に作る。"""

    async def fake_spell(tool_name, tool_args, persona, state, playbook_name,
                         event_callback, messages=None):
        item_id = _insert_item(
            session_factory, persona.persona_id,
            tool_args.get("name", "doc"),
        )
        created_ids.append(item_id)
        return (f"文書「{tool_args.get('name', 'doc')}」を作成しました。アイテムID: {item_id}", None)

    return fake_spell


def _patched_spell_env(session_factory, created_ids: List[str]):
    """spell 登録 (SPELL_TOOL_NAMES) と実行 (_run_spell_tool_async) を差し替える。"""
    return (
        patch("sea.runtime_llm.SPELL_TOOL_NAMES", {SPELL_NAME}),
        patch(
            "sea.runtime_llm._run_spell_tool_async",
            new=_make_fake_spell(session_factory, created_ids),
        ),
    )


def _run(manager, budget: int, **kwargs):
    return run_work_session(
        "p1",
        "テスト用の草稿を 1 本書いてほしい。",
        budget,
        manager=manager,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_budget_exhausted(session_factory, persona):
    """毎ラウンド spell を撃ち続けると予算で打ち切られる。"""
    responses = [
        _spell_line("草稿1"),   # 初回応答 (round 1 の spell)
        _spell_line("草稿2"),   # round 1 retry → round 2 の spell
        _spell_line("草稿3"),   # round 2 retry → まだ spell (= 続きをやりたがっている)
        "ここまでで一区切り。",  # digest コール
    ]
    manager, runtime, client = _make_env(session_factory, persona, responses)
    created_ids: List[str] = []
    p_names, p_exec = _patched_spell_env(session_factory, created_ids)

    with p_names, p_exec:
        result = _run(manager, budget=2)

    assert result.ended_reason == ENDED_BUDGET_EXHAUSTED
    assert result.rounds_used == 2
    # spell は 2 ラウンド分 (草稿1, 草稿2) だけ実行された
    assert len(created_ids) == 2
    assert result.digest == "ここまでで一区切り。"
    assert result.error is None


def test_natural_finish(session_factory, persona):
    """スペルなし応答が来たら予算内でも自然終了する。"""
    responses = [
        _spell_line("草稿"),      # round 1
        "できたよ。読み返しても大丈夫そう。",  # spell なし → 自然終了
        "草稿を 1 本書いた。",     # digest
    ]
    manager, runtime, client = _make_env(session_factory, persona, responses)
    created_ids: List[str] = []
    p_names, p_exec = _patched_spell_env(session_factory, created_ids)

    with p_names, p_exec:
        result = _run(manager, budget=5)

    assert result.ended_reason == ENDED_FINISHED
    assert result.rounds_used == 1
    assert result.digest == "草稿を 1 本書いた。"
    # LLM クライアントは WORKER アスペクトのフレームが active な状態で選ばれた
    assert runtime.selected_aspects == [Aspect.WORKER]
    assert result.started_at is not None and result.ended_at is not None


def test_artifacts_captured(session_factory, persona):
    """セッション中に作られた Item だけが artifacts に入る (既存物は入らない)。"""
    # セッション前から存在する Item (成果として主張してはいけない)
    pre_existing = _insert_item(
        session_factory, persona.persona_id, "既存の文書",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    responses = [
        _spell_line("新しい文書"),
        "終わり。",
        "新しい文書を作った。",
    ]
    manager, runtime, client = _make_env(session_factory, persona, responses)
    created_ids: List[str] = []
    p_names, p_exec = _patched_spell_env(session_factory, created_ids)

    with p_names, p_exec:
        result = _run(manager, budget=3, task_ref="task:1")

    assert created_ids, "mock spell が Item を作っているはず"
    assert result.artifacts == created_ids
    assert pre_existing not in result.artifacts
    assert result.task_ref == "task:1"
    # ダイジェストの metadata にも成果物 ref が乗る
    digest_records = [r for r in runtime.stored if "session_digest" in r["tags"]]
    assert len(digest_records) == 1
    ws_meta = (digest_records[0]["metadata"] or {}).get("work_session")
    assert ws_meta and ws_meta["artifacts"] == created_ids


def test_digest_is_the_only_committed_record(session_factory, persona):
    """committed はダイジェスト 1 件のみ。生ログのラウンドは volatile。"""
    round_text = _spell_line("草稿")
    responses = [
        round_text,
        "締めの言葉。",
        "実際にやったことの要約。",
    ]
    manager, runtime, client = _make_env(session_factory, persona, responses)
    created_ids: List[str] = []
    p_names, p_exec = _patched_spell_env(session_factory, created_ids)

    with p_names, p_exec:
        result = _run(manager, budget=3)

    committed = [r for r in runtime.stored if r["scope"] == "committed"]
    volatile = [r for r in runtime.stored if r["scope"] == "volatile"]

    # committed はダイジェスト 1 件のみ
    assert len(committed) == 1
    assert committed[0]["text"] == result.digest == "実際にやったことの要約。"
    assert "session_digest" in committed[0]["tags"]
    assert committed[0]["line_role"] == "main_line"

    # 生ログ (spell ラウンドの発話・結果・締めの言葉) は volatile 側にあり、
    # committed には一切入っていない
    assert volatile, "spell ラウンドの生ログが volatile で保存されているはず"
    assert any("/spell" in r["text"] for r in volatile)
    assert any(r["text"] == "締めの言葉。" for r in volatile)
    for r in committed:
        assert "/spell" not in r["text"]
        assert r["text"] != "締めの言葉。"
    for r in volatile:
        assert r["line_role"] == "sub_line"

    # pulse logs は flush された
    assert runtime.flushed


def test_error_returns_error_result(session_factory, persona, caplog):
    """LLM 呼び出しの例外は握り潰さず LOGGER に残し、error 結果として返す。"""
    responses = [RuntimeError("boom: provider down")]
    manager, runtime, client = _make_env(session_factory, persona, responses)
    created_ids: List[str] = []
    p_names, p_exec = _patched_spell_env(session_factory, created_ids)

    with p_names, p_exec, caplog.at_level(logging.ERROR, logger="sea.work_session"):
        result = _run(manager, budget=3)

    assert result.ended_reason == ENDED_ERROR
    assert result.error is not None and "boom" in result.error
    assert result.digest == ""
    assert result.rounds_used == 0
    # committed は 1 件も作られない
    assert not [r for r in runtime.stored if r["scope"] == "committed"]
    # LOGGER に例外が残っている
    assert any(
        rec.levelno >= logging.ERROR and "session failed" in rec.getMessage()
        for rec in caplog.records
    )
    # PulseContext は error 経路でも flush される
    assert runtime.flushed


def test_manager_missing_returns_error(persona):
    """manager 未解決 (コンテキスト外呼び出し) も error 結果に落ちる。"""
    result = run_work_session("p1", "何か作って", 3, manager=None)
    assert result.ended_reason == ENDED_ERROR
    assert result.error is not None
