"""レート制限後の Metabolism の小休止 (docs/intent/sluice_coverage_gaps.md 第一段 C-1)。

Metabolism 系の LLM 呼び出し (スルース・編纂・束ね) が RateLimitError で失敗して
走行が閉じたら、その persona の Metabolism を一定時間
(SAIVERSE_METABOLISM_RATE_LIMIT_COOLDOWN_S、既定 600 秒) 見送る。

- 判定は例外の**型** (llm_clients.exceptions.RateLimitError) — 文字列照合は
  新設しない (文字列照合が 429 をコンテキスト超過に化けさせたのが出自)。
- 入口は maybe_run_metabolism / maybe_run_emergency_precompaction — 小休止中は
  LLM を伴う仕事をせず skip する。
- 時間が明けたら自然に再開する (水位超過は残っているので次の maybe_run が拾う)。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from llm_clients.exceptions import LLMError, RateLimitError
from sea.session_lifecycle import SessionLifecycle, _is_rate_limit_error

PERSONA_ID = "alice"


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


def _make_lifecycle(session_factory):
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        event_scheduler=None,
        personas={},
    )
    runtime = SimpleNamespace(run_cache_keepalive=lambda pid, mk=None: None)
    return SessionLifecycle(runtime, manager)


def _persona():
    return SimpleNamespace(persona_id=PERSONA_ID, model="model-a")


# ---------------------------------------------------------------------------
# 型判定 (_is_rate_limit_error)
# ---------------------------------------------------------------------------


def test_rate_limit_detection_is_by_type_not_by_message():
    assert _is_rate_limit_error(RateLimitError("Request too large: 429"))
    # 文面が 429 っぽくても、型が違えばレート制限ではない
    assert not _is_rate_limit_error(RuntimeError("Request too large: 429"))
    assert not _is_rate_limit_error(LLMError("boom"))


def test_rate_limit_detection_walks_original_error_and_cause():
    wrapped = LLMError("chunk failed", original_error=RateLimitError("429"))
    assert _is_rate_limit_error(wrapped)
    try:
        try:
            raise RateLimitError("429")
        except RateLimitError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as chained:
        assert _is_rate_limit_error(chained)


# ---------------------------------------------------------------------------
# 小休止の出し入れ
# ---------------------------------------------------------------------------


def test_note_sets_cooldown_only_for_rate_limit(session_factory):
    lc = _make_lifecycle(session_factory)
    lc._note_metabolism_rate_limit(PERSONA_ID, RuntimeError("boom"))
    assert not lc._metabolism_rate_limit_active(PERSONA_ID)
    lc._note_metabolism_rate_limit(PERSONA_ID, RateLimitError("429"))
    assert lc._metabolism_rate_limit_active(PERSONA_ID)
    # 別 persona には効かない
    assert not lc._metabolism_rate_limit_active("someone-else")


def test_cooldown_expires_after_the_configured_time(session_factory):
    lc = _make_lifecycle(session_factory)
    with patch.dict(
        os.environ, {"SAIVERSE_METABOLISM_RATE_LIMIT_COOLDOWN_S": "0"},
    ):
        lc._note_metabolism_rate_limit(PERSONA_ID, RateLimitError("429"))
    assert not lc._metabolism_rate_limit_active(PERSONA_ID)
    # 明けた判定で記録も掃除される
    assert PERSONA_ID not in lc._metabolism_rate_limited


def test_chronicle_failure_note_feeds_the_cooldown(session_factory):
    """編纂・束ねの失敗理由の記帳 (_note_chronicle_failure) からも小休止が立つ —
    generate_chronicle 内の 429 はこの一点を必ず通る。"""
    lc = _make_lifecycle(session_factory)
    lc._note_chronicle_failure(PERSONA_ID, RateLimitError("429"))
    assert lc._metabolism_rate_limit_active(PERSONA_ID)


# ---------------------------------------------------------------------------
# 入口ゲート (maybe_run_metabolism / maybe_run_emergency_precompaction)
# ---------------------------------------------------------------------------


def test_maybe_run_metabolism_skips_during_cooldown(session_factory):
    lc = _make_lifecycle(session_factory)
    lc._note_metabolism_rate_limit(PERSONA_ID, RateLimitError("429"))
    with patch.object(lc, "load_anchor_entry") as load:
        lc.maybe_run_metabolism(_persona(), "room")
    load.assert_not_called()  # 小休止中は仕事を始めない


def test_maybe_run_metabolism_resumes_after_cooldown(session_factory):
    lc = _make_lifecycle(session_factory)
    with patch.dict(
        os.environ, {"SAIVERSE_METABOLISM_RATE_LIMIT_COOLDOWN_S": "0"},
    ):
        lc._note_metabolism_rate_limit(PERSONA_ID, RateLimitError("429"))
    with patch.object(lc, "load_anchor_entry", return_value=None) as load:
        lc.maybe_run_metabolism(_persona(), "room")
    load.assert_called()  # 明けたら通常の判定へ進む


def test_emergency_precompaction_skips_during_cooldown(session_factory):
    lc = _make_lifecycle(session_factory)
    lc._note_metabolism_rate_limit(PERSONA_ID, RateLimitError("429"))
    with patch.object(lc, "get_metabolism_watermarks") as wm:
        assert lc.maybe_run_emergency_precompaction(
            _persona(), "room", None, model_key="model-a",
        ) == "skip"
    wm.assert_not_called()  # 水位の解決にも進まない
