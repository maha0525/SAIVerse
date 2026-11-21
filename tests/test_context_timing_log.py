"""コンテキストの組み立ての所要時間ログの回帰テスト。

docs/intent/vector_binary_storage.md §6。``prepare_context`` と ``run_auto_recall``
が、関数の終わりに固定の形の INFO ログを一行出すことを固定する。ログは開発者の
環境でどの段階に時間がかかったかを grep で読むためのもので、形が崩れると計測に
使えなくなる。
"""
from __future__ import annotations

import logging
import os
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sai_memory.unified_recall import RecallHit
from sea import auto_recall
from sea import runtime_context
from sea.playbook_models import ContextRequirements

_PREPARE_RE = re.compile(
    r"^\[sea\]\[prepare-context\] timing persona=(?P<persona>\S+) "
    r"total=\d+\.\d\ds head=\d+\.\d\ds history=\d+\.\d\ds "
    r"realtime=\d+\.\d\ds auto_recall=\d+\.\d\ds messages=(?P<messages>\d+)$"
)
_RECALL_RE = re.compile(
    r"^\[auto_recall\] timing persona=(?P<persona>\S+) total=\d+\.\d\ds "
    r"query=\d+\.\d\ds entity=\d+\.\d\ds keywords=\d+\.\d\ds "
    r"collect=\d+\.\d\ds reflex=\d+\.\d\ds rest=\d+\.\d\ds$"
)

PERSONA = SimpleNamespace(persona_id="timing_persona", model="dummy-model")


# ---- prepare_context ----

@pytest.fixture
def stubbed_prepare_context(monkeypatch):
    import sea.head_pipeline as hp

    monkeypatch.setattr(
        hp, "render_head_messages",
        lambda *a, **k: [{"role": "system", "content": "head"}],
    )

    def _fake_append_history(runtime, persona, building_id, reqs, messages, **kwargs):
        messages.append({"role": "user", "content": "こんにちは", "id": "m1"})
        return True

    monkeypatch.setattr(runtime_context, "_append_history", _fake_append_history)
    monkeypatch.setattr(runtime_context, "persists_realtime_and_recall", lambda *a, **k: False)
    auto_recall_calls = []

    def _fake_auto_recall(*a, **k):
        auto_recall_calls.append(k)
        return None

    monkeypatch.setattr(runtime_context, "_maybe_inject_auto_recall", _fake_auto_recall)
    runtime = SimpleNamespace(
        manager=None,
        session_lifecycle=None,
        _build_realtime_context=lambda persona, building_id, messages: {
            "role": "system", "content": "realtime",
        },
    )
    return runtime, auto_recall_calls


def _prepare_timing_lines(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.name == "sea.runtime_context"
        and r.getMessage().startswith("[sea][prepare-context] timing")
    ]


def test_prepare_context_logs_one_timing_line(stubbed_prepare_context, caplog):
    runtime, auto_recall_calls = stubbed_prepare_context
    caplog.set_level(logging.INFO, logger="sea.runtime_context")

    messages = runtime_context.prepare_context(
        runtime, PERSONA, "b_lobby", None,
        requirements=ContextRequirements(realtime_context=True),
    )

    lines = _prepare_timing_lines(caplog)
    assert len(lines) == 1
    match = _PREPARE_RE.match(lines[0])
    assert match is not None, lines[0]
    assert match.group("persona") == "timing_persona"
    # head + 履歴 + リアルタイム情報
    assert int(match.group("messages")) == len(messages) == 3
    assert len(auto_recall_calls) == 1
    record = next(
        r for r in caplog.records
        if r.getMessage().startswith("[sea][prepare-context] timing")
    )
    assert record.levelno == logging.INFO


def test_prepare_context_skipped_stage_is_zero(stubbed_prepare_context, caplog):
    runtime, _ = stubbed_prepare_context
    caplog.set_level(logging.INFO, logger="sea.runtime_context")

    runtime_context.prepare_context(
        runtime, PERSONA, "b_lobby", None,
        requirements=ContextRequirements(realtime_context=False),
    )

    lines = _prepare_timing_lines(caplog)
    assert len(lines) == 1
    assert " realtime=0.00s " in lines[0]


def test_prepare_context_preview_does_not_log_timing(stubbed_prepare_context, caplog):
    runtime, auto_recall_calls = stubbed_prepare_context
    caplog.set_level(logging.INFO, logger="sea.runtime_context")

    runtime_context.prepare_context(
        runtime, PERSONA, "b_lobby", None,
        requirements=ContextRequirements(realtime_context=True),
        preview_only=True,
    )

    assert _prepare_timing_lines(caplog) == []
    assert auto_recall_calls == []


# ---- run_auto_recall ----

_THREAD = "timing_persona:__persona__"
_RECALL_LOGGER = auto_recall.LOGGER.name
_REFLEX_ENV_KEYS = ("SAIVERSE_REFLEX_JUDGMENT_MODEL", "TYPESAFE_API_KEY")


@pytest.fixture(autouse=True)
def _isolate_auto_recall():
    # 強化の経路 (外部 API) に入らないよう、反射判断の env を外しておく。
    saved = {k: os.environ.pop(k, None) for k in _REFLEX_ENV_KEYS}
    auto_recall.reset_ledger(PERSONA.persona_id)
    yield
    auto_recall.reset_ledger(PERSONA.persona_id)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


def _hit(source_id, embed_score):
    return RecallHit(
        source_type="memopedia",
        source_id=source_id,
        title="タイトル",
        content="内容テキスト",
        score=0.01,
        uri=f"saiverse://self/memopedia/{source_id}",
        embed_score=embed_score,
    )


def _run_recall(hits, **kwargs):
    with patch("sai_memory.unified_recall.unified_recall", return_value=hits):
        return auto_recall.run_auto_recall(
            conn=object(), embedder=object(),
            messages=[{"role": "user", "content": "スタックチャンの話"}],
            persona_id=PERSONA.persona_id, thread_id=_THREAD,
            **kwargs,
        )


def _recall_timing_lines(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.name == _RECALL_LOGGER
        and r.getMessage().startswith("[auto_recall] timing")
        and r.levelno == logging.INFO
    ]


def _assert_one_recall_line(caplog):
    lines = _recall_timing_lines(caplog)
    assert len(lines) == 1, lines
    match = _RECALL_RE.match(lines[0])
    assert match is not None, lines[0]
    assert match.group("persona") == PERSONA.persona_id


def test_run_auto_recall_logs_timing_when_injected(caplog):
    caplog.set_level(logging.INFO, logger=_RECALL_LOGGER)
    res = _run_recall([_hit("p1", 0.95)])
    assert res.injected
    _assert_one_recall_line(caplog)


def test_run_auto_recall_logs_timing_when_ledger_empty(caplog):
    caplog.set_level(logging.INFO, logger=_RECALL_LOGGER)
    res = _run_recall([_hit("p1", 0.10)])
    assert not res.injected
    _assert_one_recall_line(caplog)


def test_run_auto_recall_logs_timing_when_no_new_memory(caplog):
    # 一度目で台帳に載せ、二度目は new_only で新しい記憶が無い出口を通す。
    _run_recall([_hit("p1", 0.95)])
    caplog.clear()
    caplog.set_level(logging.INFO, logger=_RECALL_LOGGER)
    res = _run_recall([], new_only=True)
    assert not res.injected
    assert res.ledger_size >= 1
    _assert_one_recall_line(caplog)


def test_run_auto_recall_without_conn_does_not_log_timing(caplog):
    caplog.set_level(logging.INFO, logger=_RECALL_LOGGER)
    res = auto_recall.run_auto_recall(
        conn=None, embedder=None, messages=[],
        persona_id=PERSONA.persona_id, thread_id=_THREAD,
    )
    assert not res.injected
    assert _recall_timing_lines(caplog) == []
