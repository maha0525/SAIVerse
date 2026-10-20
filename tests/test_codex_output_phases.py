"""Codex の返答の分け方 (phase) を保存して次の呼び出しへ送り返すことのテスト。

Issue: docs/issues/codex_output_phase_passback.md

- クライアント: `_iter_chunks` がメッセージごとの phase と本文 (`output_phases`) を
  報告し、`_build_input` が保存済みの分け方を phase 付きの複数メッセージに戻す。
- 合わせ直し: スペルの周で組み直した本文に分け方を合わせる
  (`align_output_phases_to_spell_round`)。
- SEA: 報告された分け方が、保存する発言の metadata と、同じ周の次の呼び出しへ
  積む発言に載る。

ネットワーク・本番データ・実 LLM には一切触れない (SSE は偽の response、
LLM は台本どおりに返す偽物、記憶は偽の adapter)。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from llm_clients.openai_codex import OpenAICodexClient
from llm_clients.output_phases import (
    align_output_phases_to_spell_round,
    coerce_output_phases,
    join_message_texts,
    output_phases_for_sent_text,
    output_phases_for_text,
)
from sea import runtime_llm

C = "commentary"
F = "final_answer"


def _ph(phase: Optional[str], text: str) -> Dict[str, Any]:
    return {"phase": phase, "text": text}


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self._payload = b"".join(
            f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
            for ev in events
        )

    def iter_content(self):
        for i in range(0, len(self._payload), 11):
            yield self._payload[i : i + 11]

    def close(self) -> None:
        pass


def _added(item_id: Optional[str], phase: Optional[str] = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {"type": "message", "role": "assistant", "content": []}
    if item_id is not None:
        item["id"] = item_id
    if phase is not None:
        item["phase"] = phase
    return {"type": "response.output_item.added", "item": item}


def _delta(item_id: Optional[str], text: str) -> Dict[str, Any]:
    event: Dict[str, Any] = {"type": "response.output_text.delta", "delta": text}
    if item_id is not None:
        event["item_id"] = item_id
    return event


def _text_done(item_id: str, text: str) -> Dict[str, Any]:
    return {"type": "response.output_text.done", "item_id": item_id,
            "content_index": 0, "text": text}


def _item_done(item_id: str, phase: Optional[str] = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {"type": "message", "id": item_id}
    if phase is not None:
        item["phase"] = phase
    return {"type": "response.output_item.done", "item": item}


def _message(item_id: str, text: str, phase: Optional[str], *, deltas: bool = True):
    events = [_added(item_id, phase)]
    if deltas:
        events.append(_delta(item_id, text))
    events += [_text_done(item_id, text), _item_done(item_id, phase)]
    return events


def _completed(output: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"type": "response.completed", "response": {
        "output": output or [], "usage": {"input_tokens": 3, "output_tokens": 2},
    }}


def _run(events: List[Dict[str, Any]]):
    client = object.__new__(OpenAICodexClient)
    chunks = list(client._iter_chunks(_FakeResponse(events)))
    assert chunks[-1][0] == "__done__"
    state = chunks[-1][1]
    streamed = "".join(c for c in chunks[:-1] if isinstance(c, str))
    assert streamed == state["text"]
    return state


# ---------------------------------------------------------------------------
# 1. _iter_chunks の報告
# ---------------------------------------------------------------------------


def test_commentary_and_final_answer_are_reported_per_message():
    state = _run(
        _message("m1", "考えます。", C) + _message("m2", "答えです。", F) + [_completed()]
    )
    assert state["text"] == "考えます。\n答えです。"
    assert state["output_phases"] == [_ph(C, "考えます。"), _ph(F, "答えです。")]


def test_message_already_ending_with_newline_needs_no_separator():
    state = _run(
        _message("m1", "考えます。\n", C) + _message("m2", "答え", F) + [_completed()]
    )
    assert state["text"] == "考えます。\n答え"
    assert state["output_phases"] == [_ph(C, "考えます。\n"), _ph(F, "答え")]


def test_missing_phase_is_reported_as_none():
    state = _run(
        _message("m1", "一つ目", None) + _message("m2", "二つ目", None) + [_completed()]
    )
    assert state["output_phases"] == [_ph(None, "一つ目"), _ph(None, "二つ目")]


def test_phase_only_on_item_done_is_still_picked_up():
    events = [_added("m1"), _delta("m1", "途中"), _text_done("m1", "途中"),
              _item_done("m1", C), _completed()]
    assert _run(events)["output_phases"] == [_ph(C, "途中")]


def test_delta_of_a_message_without_added_is_not_reported():
    # m2 never had an output_item.added, so its text names no known message.
    events = [
        _added("m1", C), _delta("m1", "考え中"),
        _delta("m2", "/spell name='note_add' args={}"),
        _item_done("m2", F), _completed(),
    ]
    state = _run(events)
    assert state["text"] == "考え中\n/spell name='note_add' args={}"
    assert state["output_phases"] is None


def _first_message_without_delta_ids(first_text: str) -> List[Dict[str, Any]]:
    # 1 つ目のメッセージは added (id あり) で始まり、本文は item_id の無い delta。
    return [_added("c", C), _delta(None, first_text), _text_done("c", first_text),
            _item_done("c", C)]


def _first_message_done_only(first_text: str) -> List[Dict[str, Any]]:
    # 1 つ目のメッセージは added (id あり) と done だけで、delta が無い。
    return [_added("c", C), _text_done("c", first_text), _item_done("c", C)]


@pytest.mark.parametrize("first", [_first_message_done_only, _first_message_without_delta_ids])
@pytest.mark.parametrize("second_with_deltas", [False, True])
def test_unknown_item_id_without_added_is_not_reported(first, second_with_deltas):
    # 2 つ目のメッセージ f には added が届かない。f の本文の持ち主はストリーム
    # から証明できないので、分け方は報告しない (本文はそのまま)。
    second = [_delta("f", "B")] if second_with_deltas else []
    events = first("A\n") + second + [
        _text_done("f", "B"), _item_done("f", F), _completed(),
    ]
    state = _run(events)
    assert state["text"] == "A\nB"
    assert state["output_phases"] is None


@pytest.mark.parametrize("first", [_first_message_done_only, _first_message_without_delta_ids])
@pytest.mark.parametrize("second_with_deltas", [False, True])
def test_unknown_item_id_without_added_keeps_the_text_without_a_newline(first, second_with_deltas):
    # 境目の改行は、origin/develop と同じ規則 (メッセージの added が本文の後に
    # 届いたとき、または delta の item_id が空でない前の item_id から切り替わった
    # とき) でだけ入る。ここではどちらも起きないので本文は "AB" のまま。
    # 分け方 "A" / "B" をつなぐと "A\nB" になり本文と一致しないので報告しない。
    second = [_delta("f", "B")] if second_with_deltas else []
    events = first("A") + second + [
        _text_done("f", "B"), _item_done("f", F), _completed(),
    ]
    state = _run(events)
    assert state["text"] == "AB"
    assert state["output_phases"] is None


_SPELL = "/spell name='note_add' args={}"

# Degenerate streams and the text origin/develop's _iter_chunks produced for
# each (computed with that version and embedded here). The text decides what
# is shown, stored and parsed for spells, so it must stay exactly the same.
_ORIGIN_DEVELOP_TEXTS = [
    pytest.param(
        [_text_done("c", "A"), _item_done("c", C), _text_done("f", _SPELL),
         _item_done("f", F)],
        "A" + _SPELL, None, id="done_only_without_added",
    ),
    pytest.param(
        [_added("c", C), _text_done("c", "A"), _item_done("c", C),
         _delta("f", _SPELL), _text_done("f", _SPELL), _item_done("f", F)],
        "A" + _SPELL, None, id="done_only_message_then_unknown_delta_id",
    ),
    pytest.param(
        [_added("c", C), _text_done("c", "A"), _item_done("c", C),
         _text_done("f", _SPELL), _item_done("f", F)],
        "A" + _SPELL, None, id="done_only_message_then_unknown_done_id",
    ),
    pytest.param(
        [_added("c", C), _delta(None, "A"), _text_done("c", "A"), _item_done("c", C),
         _delta("f", _SPELL), _text_done("f", _SPELL), _item_done("f", F)],
        "A" + _SPELL, None, id="idless_deltas_then_unknown_delta_id",
    ),
    pytest.param(
        [_added("c", C), _delta(None, "A"), _text_done("c", "A"), _item_done("c", C),
         _text_done("f", _SPELL), _item_done("f", F)],
        "A" + _SPELL, None, id="idless_deltas_then_unknown_done_id",
    ),
    pytest.param(
        [_delta(None, "A"), _delta("f", _SPELL)],
        "A" + _SPELL, None, id="idless_delta_then_id_delta",
    ),
    pytest.param(
        [_added("c", C), _delta("c", "A"), _text_done("c", "A"), _delta(None, "B"),
         _text_done("f", "B"), _item_done("f", F)],
        "AB", None, id="idless_delta_closed_by_another_id",
    ),
    pytest.param(
        [_added("c", C), _delta("c", "A"), _text_done("c", "A"), _delta(None, "B"),
         _delta("f", "C"), _text_done("f", "C"), _item_done("f", F)],
        "AB\nC", None, id="idless_delta_between_two_ids",
    ),
    pytest.param(
        [{"type": "response.output_text.done", "text": "A"},
         {"type": "response.output_text.done", "text": _SPELL}],
        "A" + _SPELL, None, id="two_dones_without_ids",
    ),
    pytest.param(
        [_added("m1", C), _delta("m1", "考え中"), _added("m2", F), _completed()],
        "考え中\n", None, id="second_message_without_text",
    ),
]


@pytest.mark.parametrize("events, origin_text, phases", _ORIGIN_DEVELOP_TEXTS)
def test_degenerate_stream_text_equals_origin_develop(events, origin_text, phases):
    state = _run(events + [_completed()])
    assert state["text"] == origin_text
    assert state["output_phases"] == phases


def test_deltas_without_item_id_are_not_reported():
    # The text names no message, even though c's output_item.done follows.
    events = [
        _added("c", C), _delta(None, "A"), _item_done("c", C),
        _added("f", F), _delta("f", "B"), _text_done("f", "B"), _item_done("f", F),
        _completed(),
    ]
    state = _run(events)
    assert state["text"] == "A\nB"
    assert state["output_phases"] is None


def test_added_without_id_is_not_reported():
    # The deltas carry ids, but no output_item.added made those ids known.
    events = [
        _added(None, C), _delta("x", "考え中"), _text_done("x", "考え中"),
        _added(None, F), _delta("y", "答え"), _text_done("y", "答え"),
        _completed(),
    ]
    state = _run(events)
    assert state["text"] == "考え中\n答え"
    assert state["output_phases"] is None


@pytest.mark.parametrize("done_text", ["A", "AB"])
def test_done_naming_another_message_than_its_deltas_is_not_reported(done_text):
    # The deltas say c, the done that closes them says f.
    events = [
        _added("c", C), _added("f", F), _delta("c", "A"), _text_done("f", done_text),
        _completed(),
    ]
    state = _run(events)
    assert state["text"] == done_text
    assert state["output_phases"] is None


def test_done_without_item_id_closing_deltas_is_not_reported():
    events = [
        _added("c", C), _delta("c", "A"),
        {"type": "response.output_text.done", "text": "AB"}, _completed(),
    ]
    state = _run(events)
    assert state["text"] == "AB"
    assert state["output_phases"] is None


def test_conflicting_phase_is_not_reported():
    events = [_added("m1", C), _delta("m1", "X"), _text_done("m1", "X"),
              _item_done("m1", F), _completed()]
    state = _run(events)
    assert state["text"] == "X"
    assert state["output_phases"] is None


def test_interleaved_message_text_is_not_reported():
    events = [
        _added("a", C), _added("b", F),
        _delta("a", "x"), _delta("b", "y"), _delta("a", "z"), _completed(),
    ]
    state = _run(events)
    assert state["text"] == "x\ny\nz"
    assert state["output_phases"] is None


def test_done_only_stream_without_deltas_pairs_texts_with_phases():
    events = (
        _message("m1", "考えます。", C, deltas=False)
        + _message("m2", "答えです。", F, deltas=False)
        + [_completed()]
    )
    state = _run(events)
    assert state["text"] == "考えます。\n答えです。"
    assert state["output_phases"] == [_ph(C, "考えます。"), _ph(F, "答えです。")]


_COMPLETED_OUTPUT = [
    {"type": "message", "id": "m1", "phase": C,
     "content": [{"type": "output_text", "text": "前"}]},
    {"type": "message", "id": "m2", "phase": F,
     "content": [{"type": "output_text", "text": "後"}]},
]


def test_completed_only_stream_reads_phases_from_the_output_items():
    # The messages are known from added; their phase comes from completed.
    state = _run([_added("m1"), _added("m2"), _completed(_COMPLETED_OUTPUT)])
    assert state["text"] == "前\n後"
    assert state["output_phases"] == [_ph(C, "前"), _ph(F, "後")]


def test_completed_only_stream_without_added_is_not_reported():
    state = _run([_completed(_COMPLETED_OUTPUT)])
    assert state["text"] == "前\n後"
    assert state["output_phases"] is None


def test_split_that_does_not_join_to_the_text_is_not_reported():
    # 2 つ目のメッセージが空のまま終わると、区切りの改行だけが本文末尾に残る。
    events = [_added("m1", C), _delta("m1", "考え中"), _added("m2", F), _completed()]
    state = _run(events)
    assert state["text"] == "考え中\n"
    assert state["output_phases"] is None


def test_single_message_response_is_reported_as_one_entry():
    state = _run(_message("m1", "ひとこと", F) + [_completed()])
    assert state["output_phases"] == [_ph(F, "ひとこと")]


# Each true message writes only letters of its own alphabet, so the message a
# reported entry's text came from can be read from the letters themselves.
_ALPHABETS = ["abcde", "fghij", "klmno"]
_PHASES = [C, F, None]


def _random_stream(rng) -> tuple:
    """Build a well-formed stream of 1-3 messages, then maybe break it.

    Breakages: drop an added, drop an item_id, replace an item_id with an
    unknown one, swap two neighbouring events, duplicate or drop an event.
    An item_id is never replaced by another known message's id: such a lie
    names a real message and no stream rule can see it.
    Returns (events, true phase per message index).
    """
    count = rng.randint(1, 3)
    phases = [rng.choice(_PHASES) for _ in range(count)]
    events: List[Dict[str, Any]] = []
    output: List[Dict[str, Any]] = []
    with_completed_output = rng.random() < 0.3
    for index in range(count):
        item_id = f"m{index}"
        text = "".join(rng.choice(_ALPHABETS[index]) for _ in range(rng.randint(1, 5)))
        if rng.random() < 0.3:
            text += "\n"
        events.append(_added(item_id, phases[index]))
        if with_completed_output:
            output.append({"type": "message", "id": item_id, "phase": phases[index],
                           "content": [{"type": "output_text", "text": text}]})
            continue
        if rng.random() < 0.7:
            cut = rng.randint(0, len(text))
            for piece in (text[:cut], text[cut:]):
                if piece:
                    events.append(_delta(item_id, piece))
        events.append(_text_done(item_id, text))
        events.append(_item_done(item_id, phases[index] if rng.random() < 0.5 else None))
    for _ in range(rng.randint(0, 3)):
        if not events:
            break
        position = rng.randrange(len(events))
        event = events[position]
        kind = rng.choice(["drop_added", "drop_id", "unknown_id", "swap", "duplicate", "drop"])
        if kind == "drop_added" and event["type"] == "response.output_item.added":
            events.pop(position)
        elif kind in ("drop_id", "unknown_id") and "item_id" in event:
            event = dict(event)
            if kind == "drop_id":
                event.pop("item_id")
            else:
                event["item_id"] = "unknown"
            events[position] = event
        elif kind == "swap" and position + 1 < len(events):
            events[position], events[position + 1] = events[position + 1], events[position]
        elif kind == "duplicate":
            events.insert(position, dict(event))
        elif kind == "drop" and len(events) > 1:
            events.pop(position)
    return events + [_completed(output)], phases


def test_random_malformed_streams_never_report_a_wrong_split():
    import random

    rng = random.Random(20261019)
    reported = 0
    for _ in range(200):
        events, phases = _random_stream(rng)
        state = _run(events)
        if state["output_phases"] is None:
            continue
        reported += 1
        sources = []
        for entry in state["output_phases"]:
            letters = set(entry["text"]) - {"\n"}
            if not letters:
                continue  # a newline alone names no alphabet
            owners = {i for i, alphabet in enumerate(_ALPHABETS) if letters & set(alphabet)}
            assert len(owners) == 1, (events, state["output_phases"])
            source = owners.pop()
            assert entry["phase"] == phases[source], (events, state["output_phases"])
            # The entry holds all of that message's letters in the text.
            alphabet = set(_ALPHABETS[source])
            assert ([c for c in state["text"] if c in alphabet]
                    == [c for c in entry["text"] if c in alphabet]), (events, state["output_phases"])
            sources.append(source)
        assert len(sources) == len(set(sources)), (events, state["output_phases"])
    # The seed must exercise both outcomes for the property to mean anything.
    assert 0 < reported < 200


def _client_with_responses(monkeypatch, responses: List[List[Dict[str, Any]]]):
    client = OpenAICodexClient("synthetic-codex")
    queue = list(responses)
    monkeypatch.setattr(client, "_build_body", lambda *a, **k: {})
    monkeypatch.setattr(
        client, "_post_with_auth_retry", lambda body: _FakeResponse(queue.pop(0)),
    )
    return client


@pytest.mark.parametrize("streaming", [False, True])
def test_generation_stores_the_split_and_each_call_replaces_it(streaming, monkeypatch):
    two = _message("m1", "考え", C) + _message("m2", "答え", F) + [_completed()]
    broken = [_added("m1", C), _delta("m1", "途中"), _added("m2", F), _completed()]
    client = _client_with_responses(monkeypatch, [two, two, broken])

    def call():
        if streaming:
            return "".join(c for c in client.generate_stream([]) if isinstance(c, str))
        return client.generate([])

    assert call() == "考え\n答え"
    assert client.consume_output_phases() == [_ph(C, "考え"), _ph(F, "答え")]
    assert client.consume_output_phases() is None  # destructive read
    call()  # not consumed on purpose
    call()  # this call reports no split
    assert client.consume_output_phases() is None


# ---------------------------------------------------------------------------
# 2. _build_input
# ---------------------------------------------------------------------------


def _assistant(content: str, phases: Any = None, **extra) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"role": "assistant", "content": content, **extra}
    if phases is not None:
        msg["metadata"] = {"output_phases": phases}
    return msg


def test_build_input_splits_an_assistant_turn_with_a_matching_split():
    client = OpenAICodexClient("synthetic-codex")
    _, items = client._build_input([
        {"role": "user", "content": "こんにちは"},
        _assistant("考えます。\n答えです。", [_ph(C, "考えます。"), _ph(F, "答えです。")]),
    ])
    assert items[1:] == [
        {"type": "message", "role": "assistant", "phase": C,
         "content": [{"type": "output_text", "text": "考えます。"}]},
        {"type": "message", "role": "assistant", "phase": F,
         "content": [{"type": "output_text", "text": "答えです。"}]},
    ]


def test_build_input_omits_the_phase_key_when_phase_is_unknown():
    client = OpenAICodexClient("synthetic-codex")
    _, items = client._build_input([_assistant("A\nB", [_ph(None, "A"), _ph(F, "B")])])
    assert "phase" not in items[0]
    assert items[1]["phase"] == F


@pytest.mark.parametrize("message", [
    _assistant("考えます。\n答えです。"),
    _assistant("編集された本文", [_ph(C, "考えます。"), _ph(F, "答えです。")]),
    _assistant("考えます。\n答えです。", "not-a-list"),
    _assistant("考えます。\n答えです。", [{"phase": C}]),
])
def test_build_input_falls_back_to_one_message(message):
    client = OpenAICodexClient("synthetic-codex")
    _, items = client._build_input([message])
    assert items == [{
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": message["content"]}],
    }]


def test_build_input_matches_a_history_row_whose_content_was_stripped_on_read():
    # SAIMemory は読み出し時に本文を strip する。保存時の分け方は strip 前の本文に
    # 合わせてあるので、外側の空白だけの違いは同じ分け方として送る。
    client = OpenAICodexClient("synthetic-codex")
    _, items = client._build_input([
        _assistant("考えます。\n答えです。", [_ph(C, "考えます。"), _ph(F, "答えです。\n")]),
    ])
    assert [item["content"][0]["text"] for item in items] == ["考えます。", "答えです。"]
    assert [item["phase"] for item in items] == [C, F]


def test_tool_call_turns_are_not_split():
    client = OpenAICodexClient("synthetic-codex")
    _, items = client._build_input([_assistant(
        "A\nB", [_ph(C, "A"), _ph(F, "B")],
        tool_calls=[{"id": "call-1", "function": {"name": "t", "arguments": "{}"}}],
    )])
    assert items[0] == {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "A\nB"}]}
    assert items[1]["type"] == "function_call"


def test_the_split_survives_message_preparation_into_the_request_body():
    client = OpenAICodexClient("synthetic-codex")
    body = client._build_body(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "やあ"},
            _assistant("考えます。\n答えです。", [_ph(C, "考えます。"), _ph(F, "答えです。")]),
            {"role": "user", "content": "続けて"},
        ],
        temperature=None, tools=None, response_schema=None, reasoning_effort=None,
    )
    assistant_items = [i for i in body["input"] if i.get("role") == "assistant"]
    assert [(i.get("phase"), i["content"][0]["text"]) for i in assistant_items] == [
        (C, "考えます。"), (F, "答えです。"),
    ]
    # metadata itself never leaves the client
    assert all("metadata" not in item for item in body["input"])


# ---------------------------------------------------------------------------
# 3. 共有の規則と合わせ直し
# ---------------------------------------------------------------------------


def test_join_rule_and_shape_checks():
    assert join_message_texts(["A", "", "B\n", "C"]) == "A\nB\nC"
    assert coerce_output_phases([_ph("other", "x"), _ph(C, "")]) == [_ph(None, "x")]
    assert coerce_output_phases(MagicMock()) is None
    assert coerce_output_phases([]) is None
    assert output_phases_for_text([_ph(C, "A"), _ph(F, "B")], "A\nB") is not None
    assert output_phases_for_text([_ph(C, "A"), _ph(F, "B")], "AB") is None
    assert output_phases_for_sent_text([_ph(C, "A"), _ph(F, "B")], "A B") is None


def _span(raw: str, line: str, norm: Optional[str] = None, start_at: int = 0):
    start = raw.index(line, start_at)
    return (start, start + len(line), norm if norm is not None else line)


def _rebuild(raw: str, lines) -> str:
    """The spell loop's ``assistant_content`` formula (sea/runtime_llm.py)."""
    before = raw[:lines[0][0]].rstrip()
    after = raw[lines[-1][1]:].strip()
    return (before + "\n" + "\n".join(line[2] for line in lines)
            + ("\n" + after if after else "")).strip()


SPELL_X = "/spell name='note_add' args={}"
SPELL_Y = "/spell name='note_add' args={\"text\": \"y\"}"


def _align(phases, lines_spec):
    raw = join_message_texts([p["text"] for p in phases])
    lines = []
    cursor = 0
    for spec in lines_spec:
        line, norm = spec if isinstance(spec, tuple) else (spec, None)
        lines.append(_span(raw, line, norm, cursor))
        cursor = lines[-1][1]
    rebuilt = _rebuild(raw, lines)
    return align_output_phases_to_spell_round(raw, phases, lines, rebuilt), rebuilt


def _assert_joins(aligned, rebuilt):
    assert aligned is not None
    assert join_message_texts([p["text"] for p in aligned]) == rebuilt


def test_spell_line_only_in_commentary():
    aligned, rebuilt = _align([_ph(C, f"考えます。\n{SPELL_X}"), _ph(F, "終わり。")], [SPELL_X])
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, f"考えます。\n{SPELL_X}"), _ph(F, "終わり。")]


def test_spell_line_only_in_final_answer():
    aligned, rebuilt = _align([_ph(C, "考えます。"), _ph(F, f"{SPELL_X}\nやります。")], [SPELL_X])
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, "考えます。"), _ph(F, f"{SPELL_X}\nやります。")]


def test_same_spell_line_in_both_messages():
    aligned, rebuilt = _align(
        [_ph(C, f"A\n{SPELL_X}"), _ph(F, f"{SPELL_X}\nB")], [SPELL_X, SPELL_X],
    )
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, f"A\n{SPELL_X}"), _ph(F, f"{SPELL_X}\nB")]


def test_prose_between_spell_lines_is_dropped_like_the_rebuild():
    aligned, rebuilt = _align(
        [_ph(C, f"{SPELL_X}\n間の文\n{SPELL_Y}"), _ph(F, "締め")], [SPELL_X, SPELL_Y],
    )
    assert rebuilt == f"{SPELL_X}\n{SPELL_Y}\n締め"
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, f"{SPELL_X}\n{SPELL_Y}"), _ph(F, "締め")]


def test_prose_between_spell_lines_in_different_messages():
    aligned, rebuilt = _align(
        [_ph(C, f"{SPELL_X}\n間の文"), _ph(F, f"{SPELL_Y}\n後")], [SPELL_X, SPELL_Y],
    )
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, SPELL_X), _ph(F, f"{SPELL_Y}\n後")]


def test_text_before_spanning_two_messages():
    aligned, rebuilt = _align(
        [_ph(C, "前置き1"), _ph(F, f"前置き2\n{SPELL_X}\n後")], [SPELL_X],
    )
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, "前置き1"), _ph(F, f"前置き2\n{SPELL_X}\n後")]


def test_whitespace_trimmed_by_the_rebuild_across_a_message_boundary():
    aligned, rebuilt = _align([_ph(C, "  前置き\n"), _ph(F, f"\n{SPELL_X}  \n\n")], [SPELL_X])
    assert rebuilt == f"前置き\n{SPELL_X}"
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, "前置き"), _ph(F, SPELL_X)]


def test_normalized_spell_line_replaces_the_raw_line():
    raw_line = "/spell  name='note_add'  args={}"
    aligned, rebuilt = _align([_ph(C, "考え"), _ph(F, raw_line)], [(raw_line, SPELL_X)])
    _assert_joins(aligned, rebuilt)
    assert aligned == [_ph(C, "考え"), _ph(F, SPELL_X)]


def test_alignment_returns_none_when_inputs_do_not_line_up():
    phases = [_ph(C, "考え"), _ph(F, SPELL_X)]
    raw = "考え\n" + SPELL_X
    line = _span(raw, SPELL_X)
    rebuilt = _rebuild(raw, [line])
    assert align_output_phases_to_spell_round(raw, phases, [line], rebuilt) is not None
    # reported split does not join to the raw text
    assert align_output_phases_to_spell_round(raw + "!", phases, [line], rebuilt) is None
    # no split reported
    assert align_output_phases_to_spell_round(raw, None, [line], rebuilt) is None
    # a spell span that crosses the message boundary
    crossing = (raw.index("考え"), len(raw), "x")
    assert align_output_phases_to_spell_round(raw, phases, [crossing], "x") is None
    # stored text differs from the rebuild
    assert align_output_phases_to_spell_round(raw, phases, [line], rebuilt + "?") is None


# ---------------------------------------------------------------------------
# 4. SEA: スペルループが記憶と次の呼び出しへ分け方を載せる
# ---------------------------------------------------------------------------


class _LoopRuntime:
    def __init__(self):
        self.stored_calls: List[Dict[str, Any]] = []
        self.manager = SimpleNamespace(occupants={"b1": ["p1"]})

    def _effective_building_id(self, persona, fallback):
        return "b1"

    def _store_memory(self, persona, text, **kwargs):
        self.stored_calls.append({"text": text, **kwargs})
        return "mem-1" if kwargs.get("return_message_id") else True

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, *args, **kwargs):
        return None


class _PhasedClient:
    """retry 応答と、その応答の分け方を台本どおりに返す偽の LLM クライアント。"""

    def __init__(self, responses: List[str], phases: List[Any]):
        self.responses = list(responses)
        self.phases = list(phases)
        self._pending: Any = None
        self.sent: List[List[Dict[str, Any]]] = []

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.sent.append([dict(m) for m in messages])
        self._pending = self.phases.pop(0)
        return self.responses.pop(0)

    def consume_usage(self):
        return None

    def consume_reasoning(self):
        return []

    def consume_reasoning_details(self):
        return None

    def consume_output_phases(self):
        value, self._pending = self._pending, None
        return value


def _fake_spell():
    async def fake(tool_name, tool_args, persona, state, playbook_name,
                   event_callback, messages=None):
        return ("やりました", None, True)
    return fake


def _run_spell_loop(client, text, initial_output_phases):
    runtime = _LoopRuntime()
    messages: List[Dict[str, Any]] = []
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {"note_add"}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=_fake_spell()):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text=text,
            spell_enabled=True,
            llm_client=client,
            runtime=runtime,
            persona=SimpleNamespace(persona_id="p1"),
            building_id="b1",
            state={"_pulse_id": "pulse-1", "_pulse_context": None,
                   "_cancellation_token": None},
            messages=messages,
            playbook=SimpleNamespace(name="test_playbook"),
            event_callback=None,
            node_def=SimpleNamespace(id="llm", memorize=None, speak=True),
            initial_output_phases=initial_output_phases,
        ))
    return result, runtime, messages


def test_spell_round_stores_the_split_of_the_rebuilt_text_and_sends_it_next():
    first_phases = [_ph(C, f"確認します。\n{SPELL_X}"), _ph(F, "少し待ってね。")]
    first_text = join_message_texts([p["text"] for p in first_phases])
    client = _PhasedClient(["終わりました。"], [[_ph(F, "終わりました。")]])

    result, runtime, messages = _run_spell_loop(client, first_text, first_phases)

    assert result.loop_count == 1
    round_memory = [c for c in runtime.stored_calls if c.get("role") == "assistant"]
    assert len(round_memory) == 1
    stored_text = round_memory[0]["text"]
    stored_phases = round_memory[0]["metadata"]["output_phases"]
    assert join_message_texts([p["text"] for p in stored_phases]) == stored_text
    assert stored_phases == [_ph(C, f"確認します。\n{SPELL_X}"), _ph(F, "少し待ってね。")]
    # 同じ周の次の呼び出しへ積んだ発言にも同じ分け方が載り、Codex の
    # _build_input がそれを phase 付きで分けて送る。
    round_msg = client.sent[0][0]
    assert round_msg["role"] == "assistant"
    assert round_msg["content"] == stored_text
    assert round_msg["metadata"]["output_phases"] == stored_phases
    _, items = OpenAICodexClient("synthetic-codex")._build_input([round_msg])
    assert [(i.get("phase"), i["content"][0]["text"]) for i in items] == [
        (C, f"確認します。\n{SPELL_X}"), (F, "少し待ってね。"),
    ]
    # 締めの発言の分け方は呼び出し元へ返る (後段の memorize が state から読む)
    assert result.final_continuation == "終わりました。"
    assert result.closing_output_phases == [_ph(F, "終わりました。")]


def test_spell_round_without_a_reported_split_stores_no_split():
    client = _PhasedClient(["終わりました。"], [None])
    result, runtime, _messages = _run_spell_loop(client, f"確認\n{SPELL_X}", None)
    round_memory = [c for c in runtime.stored_calls if c.get("role") == "assistant"]
    assert "output_phases" not in (round_memory[0].get("metadata") or {})
    assert "metadata" not in client.sent[0][0]
    assert result.closing_output_phases is None


def test_consume_output_phases_ignores_clients_without_a_split():
    state: Dict[str, Any] = {"_output_phases": [_ph(C, "stale")]}
    assert runtime_llm._consume_output_phases(MagicMock(), state) is None
    assert "_output_phases" not in state
    assert runtime_llm._consume_output_phases(SimpleNamespace(), None) is None


# ---------------------------------------------------------------------------
# 5. 記憶への保存 (_store_memory が本文と一致する分け方だけを残す)
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self):
        self.appended: List[Dict[str, Any]] = []

    def is_ready(self):
        return True

    def get_current_thread(self):
        return "p1:main"

    def append_persona_message(self, message, thread_suffix=None):
        self.appended.append(message)
        return f"id-{len(self.appended)}"


def _store(text: str, phases: Any, role: str = "assistant"):
    from sea.runtime import SEARuntime

    adapter = _FakeAdapter()
    persona = SimpleNamespace(persona_id="p1", sai_memory=adapter,
                              current_building_id=None)
    SEARuntime._store_memory(
        SimpleNamespace(manager=None), persona, text, role=role,
        metadata={"output_phases": phases},
    )
    return adapter.appended[0]


def test_store_memory_keeps_a_matching_split():
    phases = [_ph(C, "考えます。"), _ph(F, "答えです。")]
    stored = _store("考えます。\n答えです。", phases)
    assert stored["metadata"]["output_phases"] == phases


def test_store_memory_drops_a_split_that_does_not_match_the_stored_text():
    stored = _store("別の本文", [_ph(C, "考えます。"), _ph(F, "答えです。")])
    assert "output_phases" not in (stored.get("metadata") or {})


def test_store_memory_follows_marker_stripping_per_message():
    stored = _store("==大事==な考え\n答え", [_ph(C, "==大事==な考え"), _ph(F, "答え")])
    assert stored["content"] == "大事な考え\n答え"
    assert stored["metadata"]["output_phases"] == [_ph(C, "大事な考え"), _ph(F, "答え")]


def test_stored_split_reaches_the_next_request_as_phase_messages():
    """保存 → 履歴として読み出し (本文は strip される) → 送信の境界を跨ぐ。"""
    phases = [_ph(C, "考えます。"), _ph(F, "答えです。\n")]
    stored = _store("考えます。\n答えです。\n", phases)
    history_row = {
        "role": "assistant",
        "content": stored["content"].strip(),  # compose_message_content strips
        "metadata": stored["metadata"],
    }
    body = OpenAICodexClient("synthetic-codex")._build_body(
        [{"role": "user", "content": "やあ"}, history_row,
         {"role": "user", "content": "次"}],
        temperature=None, tools=None, response_schema=None, reasoning_effort=None,
    )
    assistant_items = [i for i in body["input"] if i.get("role") == "assistant"]
    assert [(i.get("phase"), i["content"][0]["text"]) for i in assistant_items] == [
        (C, "考えます。"), (F, "答えです。"),
    ]


# ---------------------------------------------------------------------------
# 6. LLM ノード: スペルの無い返事の分け方が、記憶に書く Beat まで届く
# ---------------------------------------------------------------------------


class _PhasedStreamClient:
    config_key = None

    def __init__(self, chunks: List[str], phases: Any):
        self.chunks = chunks
        self.phases = phases

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        return iter(self.chunks)

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None

    def consume_output_phases(self):
        value, self.phases = self.phases, None
        return value


def _streaming_node(monkeypatch, client):
    from sea.runtime_emitters import SpeakFinalizeResult

    runtime = MagicMock()
    runtime.manager.occupants = {"b1": ["p1"]}
    runtime._effective_building_id.return_value = "b1"
    runtime._emit_speak_start.return_value = "msg-1"
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="saved", building_msg={"message_id": "msg-1", "content": "x"},
    )
    runtime._default_temperature.return_value = 0.7
    runtime._get_cache_kwargs.return_value = {}
    runtime.select_llm_client.return_value = (client, "model-a")
    monkeypatch.setattr(
        runtime_llm, "resolve_execution_context",
        lambda persona, pulse_context, state=None: SimpleNamespace(model_key="model-a"),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: True)
    monkeypatch.setattr(runtime_llm, "_record_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_consume_reasoning", lambda *a, **k: ("", None))
    beats: List[Any] = []
    monkeypatch.setattr(runtime_llm, "_finalize_beat", lambda rt, beat: beats.append(beat))
    node_def = SimpleNamespace(
        id="llm", speak=True, action=None, available_tools=None,
        response_schema=None, response_schema_source=None, output_key=None,
        output_keys=None, metadata_key=None, memorize=None, important=False,
        label=None,
    )
    persona = SimpleNamespace(persona_id=None, persona_name="p", history_manager=MagicMock())
    node = runtime_llm.lg_llm_node(
        runtime, node_def, persona, "b1", SimpleNamespace(name="pb"), lambda e: None,
    )
    return node, beats


def test_streamed_reply_hands_its_split_to_the_beat_that_is_memorized(monkeypatch):
    phases = [_ph(C, "考えます。"), _ph(F, "答えです。")]
    node, beats = _streaming_node(
        monkeypatch, _PhasedStreamClient(["考えます。", "\n", "答えです。"], phases),
    )
    state: Dict[str, Any] = {"_messages": [], "_pulse_id": "pl-1"}
    asyncio.run(node(state))
    assert len(beats) == 1
    assert beats[0].continuation == "考えます。\n答えです。"
    assert beats[0].output_phases == phases
    assert state["_output_phases"] == phases


def test_a_reply_without_a_split_clears_the_previous_one(monkeypatch):
    node, beats = _streaming_node(monkeypatch, _PhasedStreamClient(["答え"], None))
    state: Dict[str, Any] = {"_messages": [], "_pulse_id": "pl-1",
                             "_output_phases": [_ph(C, "前の呼び出しの分")]}
    asyncio.run(node(state))
    assert beats[0].output_phases is None
    assert "_output_phases" not in state


def test_store_beat_memory_passes_the_split_and_skips_interrupted_text():
    runtime = _LoopRuntime()
    phases = [_ph(C, "A"), _ph(F, "B")]
    common = dict(tags=["conversation"], state={"_pulse_id": "p"},
                  playbook_name="pb", prompt=None, output_phases=phases)
    runtime_llm._store_beat_memory(
        runtime, SimpleNamespace(persona_id="p1"), text="A\nB", interrupted=False, **common,
    )
    runtime_llm._store_beat_memory(
        runtime, SimpleNamespace(persona_id="p1"), text="A\nB", interrupted=True, **common,
    )
    assert runtime.stored_calls[0]["metadata"]["output_phases"] == phases
    assert "output_phases" not in runtime.stored_calls[1]["metadata"]


# ---------------------------------------------------------------------------
# 7. 同じ Pulse の後ろの呼び出しと、次の Pulse (履歴から復元) で同じ形で送る
# ---------------------------------------------------------------------------


def _finalize(text: str, phases: Any, *, memorize: Any = True, important: bool = False,
              interrupted: bool = False):
    """``_finalize_beat`` を走らせ、送る列 (state) と記憶へ渡した metadata を返す。"""
    state: Dict[str, Any] = {"_output_phases": phases, "_last_thought_signature": "sig-1"}
    if interrupted:
        state[runtime_llm.INTERRUPTED_METADATA_KEY] = True
    runtime = MagicMock()
    runtime._store_memory.return_value = "mid-1"
    beat = runtime_llm.BeatExecution.from_node_locals(
        persona=SimpleNamespace(persona_id="p1", persona_name="P"),
        node_def=SimpleNamespace(id="n1", memorize=memorize, important=important,
                                 label=None, output_key=None),
        node_id="n1",
        playbook=SimpleNamespace(name="pb", display_name="pb"),
        state=state,
        event_callback=None,
        messages=[{"role": "user", "content": "やあ"}],
        prompt=None,
        continuation=text,
        schema_consumed=False,
    )
    with patch.object(runtime_llm, "log_sea_trace"):
        runtime_llm._finalize_beat(runtime, beat)
    stored_metadata = runtime._store_memory.call_args.kwargs.get("metadata") or {}
    return state, stored_metadata


def _assistant_items(messages: List[Dict[str, Any]]):
    body = OpenAICodexClient("synthetic-codex")._build_body(
        messages + [{"role": "user", "content": "次"}],
        temperature=None, tools=None, response_schema=None, reasoning_effort=None,
    )
    return [item for item in body["input"] if item.get("role") == "assistant"]


@pytest.mark.parametrize("memorize,important", [(True, False), (None, True)])
def test_reply_sent_later_in_the_pulse_matches_the_restored_history_row(memorize, important):
    phases = [_ph(C, "考えます。"), _ph(F, "答えです。")]
    text = "考えます。\n答えです。"
    state, stored_metadata = _finalize(text, phases, memorize=memorize, important=important)

    reply = state["_messages"][-1]
    assert reply["metadata"]["output_phases"] == phases
    assert reply["thought_signature"] == "sig-1"

    # 次の Pulse: 記憶に書いた行を履歴として読み出して送る
    stored = _store(text, stored_metadata["output_phases"])
    history_row = {"role": "assistant", "content": stored["content"].strip(),
                   "metadata": stored["metadata"]}
    restored_items = _assistant_items([{"role": "user", "content": "やあ"}, history_row])

    # 同じ Pulse の後ろの LLM ノード: state["_messages"] をそのまま送る
    in_pulse_items = _assistant_items(state["_messages"])

    assert in_pulse_items == restored_items
    assert [(i.get("phase"), i["content"][0]["text"]) for i in in_pulse_items] == [
        (C, "考えます。"), (F, "答えです。"),
    ]


def test_reply_without_a_matching_split_or_cut_short_is_sent_as_one_message():
    phases = [_ph(C, "考えます。"), _ph(F, "答えです。")]
    state, stored_metadata = _finalize("別の本文", phases)
    assert "metadata" not in state["_messages"][-1]

    state, stored_metadata = _finalize("考えます。\n答えです。", phases, interrupted=True)
    assert "metadata" not in state["_messages"][-1]
    assert "output_phases" not in stored_metadata


def test_attach_output_phases_keeps_other_metadata_keys():
    message = {"role": "assistant", "content": "A\nB", "metadata": {"aspect": "x"}}
    runtime_llm.attach_output_phases(message, [_ph(C, "A"), _ph(F, "B")])
    assert message["metadata"] == {"aspect": "x", "output_phases": [_ph(C, "A"), _ph(F, "B")]}


# ---------------------------------------------------------------------------
# 8. state["last"] を生成以外の本文に書き換えるノードの後では分け方を使わない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rewrite_last", [False, True])
def test_split_is_dropped_after_a_node_that_rewrites_last(rewrite_last):
    from sea.langgraph_runner import compile_playbook
    from sea.playbook_models import PlaybookSchema
    from sea.runtime import SEARuntime

    runtime = SEARuntime(SimpleNamespace(building_histories={"b1": []}, occupants={}))
    runtime._effective_building_id = lambda persona, building_id: "b1"
    captured: Dict[str, Any] = {}

    def _fake_speak(p, building_id, text, pulse_id=None, extra_metadata=None,
                    event_callback=None):
        captured["text"] = text
        captured["metadata"] = extra_metadata or {}
        return {"message_id": "bm-1"}

    runtime._runtime_engine.emitters["speak"] = _fake_speak
    persona = SimpleNamespace(persona_id="p1", persona_name="P")
    nodes: List[Dict[str, Any]] = [{"id": "say", "type": "speak", "next": None}]
    if rewrite_last:
        # 親の LLM と同じ文字列を、生成ではない代入で last に置く
        nodes.insert(0, {"id": "set", "type": "set",
                         "assignments": {"last": "Done"}, "next": "say"})
    playbook = PlaybookSchema(name="pb", display_name="pb", description="t",
                              input_schema=[], start_node=nodes[0]["id"], nodes=nodes)
    compiled = compile_playbook(
        playbook,
        llm_node_factory=MagicMock(), tool_node_factory=MagicMock(),
        speak_node=lambda s: runtime._lg_speak_node(s, persona, "b1", playbook, [], None),
        think_node=MagicMock(),
        set_node_factory=lambda nd: runtime._lg_set_node(nd, playbook, None),
    )
    state: Dict[str, Any] = {
        "last": "Done", "_output_phases": [_ph(F, "Done")],
        "_messages": [], "_pulse_context": None, "_pulse_id": "pulse-1",
    }
    asyncio.run(compiled(state, {"recursion_limit": 10}))

    assert captured["text"] == "Done"
    if rewrite_last:
        assert "output_phases" not in captured["metadata"]
    else:
        assert captured["metadata"]["output_phases"] == [_ph(F, "Done")]


# ---------------------------------------------------------------------------
# 9. SAY / MEMORIZE / THINK: the split of the LLM output reaches every row
#    that stores that output, and is not used for a transformed text
# ---------------------------------------------------------------------------


_NODE_TEXT = "考えます。\n答えです。"
_NODE_PHASES = [_ph(C, "考えます。"), _ph(F, "答えです。")]


def _run_after_llm(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run ``llm`` (a fake that reports a split like the real LLM node) and
    then ``nodes`` with the real SAY / SPEAK / THINK / MEMORIZE nodes.

    Returns the metadata each storing node passed on, by node kind.
    """
    from sea.langgraph_runner import compile_playbook
    from sea.playbook_models import PlaybookSchema
    from sea.runtime import SEARuntime

    runtime = SEARuntime(SimpleNamespace(building_histories={"b1": []}, occupants={}))
    runtime._effective_building_id = lambda persona, building_id: "b1"
    captured: Dict[str, Any] = {}

    def _fake_speak(p, building_id, text, pulse_id=None, extra_metadata=None,
                    event_callback=None):
        captured["speak"] = (text, extra_metadata or {})
        return {"message_id": "bm-1"}

    def _fake_say(persona, building_id, text, pulse_id=None, metadata=None,
                  event_callback=None, occupants_snapshot=None):
        captured["say"] = (text, metadata or {})
        return {"message_id": "bm-2"}

    def _fake_think(persona, pulse_id, text, record_history=True, extra_metadata=None):
        captured["think"] = (text, extra_metadata or {})
        return "mid-think"

    def _fake_store(persona, text, **kwargs):
        captured["memorize"] = (text, kwargs.get("metadata") or {})
        return True

    runtime._runtime_engine.emitters["speak"] = _fake_speak
    runtime._emit_say = _fake_say
    runtime._emit_think = _fake_think
    runtime._store_memory = _fake_store

    def _fake_llm(node_def):
        def node(state):
            state["last"] = _NODE_TEXT
            runtime_llm._store_output_phases_in_state(state, list(_NODE_PHASES))
            return state
        return node

    persona = SimpleNamespace(persona_id="p1", persona_name="P")
    chain = [{"id": "llm", "type": "llm"}] + nodes
    for current, following in zip(chain, chain[1:] + [None]):
        current["next"] = following["id"] if following else None
    playbook = PlaybookSchema(name="pb", display_name="pb", description="t",
                              input_schema=[], start_node="llm", nodes=chain)
    compiled = compile_playbook(
        playbook,
        llm_node_factory=_fake_llm, tool_node_factory=MagicMock(),
        speak_node=lambda s: runtime._lg_speak_node(s, persona, "b1", playbook, [], None),
        think_node=lambda s: runtime._lg_think_node(s, persona, playbook, [], None),
        say_node_factory=lambda nd: runtime._lg_say_node(nd, persona, "b1", playbook, [], None),
        memorize_node_factory=lambda nd: runtime._lg_memorize_node(nd, persona, playbook, [], None),
    )
    state: Dict[str, Any] = {"_messages": [], "_pulse_context": None, "_pulse_id": "pulse-1"}
    asyncio.run(compiled(state, {"recursion_limit": 20}))
    return captured


def test_say_then_speak_stores_the_split():
    captured = _run_after_llm([{"id": "say", "type": "say"}, {"id": "speak", "type": "speak"}])
    assert captured["say"][0] == _NODE_TEXT
    text, metadata = captured["speak"]
    assert text == _NODE_TEXT
    assert metadata["output_phases"] == _NODE_PHASES


def test_memorize_of_last_stores_the_split_and_leaves_it_for_speak():
    captured = _run_after_llm([
        {"id": "mem", "type": "memorize", "action": "{last}"},
        {"id": "speak", "type": "speak"},
    ])
    text, metadata = captured["memorize"]
    assert text == _NODE_TEXT
    assert metadata["output_phases"] == _NODE_PHASES
    assert captured["speak"][1]["output_phases"] == _NODE_PHASES


def test_memorize_without_action_stores_the_split():
    captured = _run_after_llm([{"id": "mem", "type": "memorize"}])
    assert captured["memorize"][1]["output_phases"] == _NODE_PHASES


def test_memorize_of_a_transformed_text_stores_no_split():
    captured = _run_after_llm([
        {"id": "mem", "type": "memorize", "action": "記録: {last}"},
        {"id": "speak", "type": "speak"},
    ])
    text, metadata = captured["memorize"]
    assert text == "記録: " + _NODE_TEXT
    assert "output_phases" not in metadata
    # The transformed text is now state["last"]; SPEAK stores no split for it.
    assert captured["speak"][0] == "記録: " + _NODE_TEXT
    assert "output_phases" not in captured["speak"][1]


def test_memorize_with_a_non_assistant_role_stores_no_split():
    captured = _run_after_llm([{"id": "mem", "type": "memorize", "role": "system"}])
    assert "output_phases" not in captured["memorize"][1]


def test_think_stores_the_split_and_leaves_it_for_speak():
    captured = _run_after_llm([{"id": "think", "type": "think"}, {"id": "speak", "type": "speak"}])
    text, metadata = captured["think"]
    assert text == _NODE_TEXT
    assert metadata["output_phases"] == _NODE_PHASES
    assert captured["speak"][1]["output_phases"] == _NODE_PHASES
