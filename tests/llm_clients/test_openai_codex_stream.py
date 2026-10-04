"""Codex の SSE ストリーム集約 (`_iter_chunks`) のテスト。

Codex 系 (GPT-5.3-Codex 以降) は 1 回の応答を複数の出力メッセージに分けて
返すことがある。境目を潰すと 2 つ目のメッセージ先頭の ``/spell`` が前の文に
直結し、スペルの行頭判定に掛からなくなる。ここではメッセージの境目に改行が
入ること、1 メッセージの応答は従来どおりであることを確かめる。
ネットワークには一切出ない (SSE は偽の response オブジェクトで与える)。

Issue: docs/issues/codex_multi_message_spell_concatenation.md
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import pytest

from llm_clients.openai_codex import OpenAICodexClient

SPELL_LINE = re.compile(r"^/spell", re.MULTILINE)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    """`iter_content()` で SSE の `data: {...}\\n` バイト列を返す偽物。"""

    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self._payload = b"".join(
            f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
            for ev in events
        )

    def iter_content(self):
        # 小さく刻んで、マルチバイト文字がチャンク境界を跨ぐ形でも流す
        for i in range(0, len(self._payload), 7):
            yield self._payload[i : i + 7]

    def close(self) -> None:
        pass


def _client() -> OpenAICodexClient:
    # _iter_chunks は認証もネットワークも使わないので __init__ を通さない
    return object.__new__(OpenAICodexClient)


def _message_events(item_id: str, deltas: List[str], *, with_done: bool = True) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = [
        {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": item_id, "role": "assistant", "content": []},
        }
    ]
    for d in deltas:
        events.append(
            {"type": "response.output_text.delta", "item_id": item_id, "delta": d}
        )
    if with_done:
        events.append(
            {
                "type": "response.output_text.done",
                "item_id": item_id,
                "content_index": 0,
                "text": "".join(deltas),
            }
        )
    events.append(
        {
            "type": "response.output_item.done",
            "item": {"type": "message", "id": item_id},
        }
    )
    return events


def _completed(output: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "output": output or [],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _run(events: List[Dict[str, Any]]):
    chunks = list(_client()._iter_chunks(_FakeResponse(events)))
    assert isinstance(chunks[-1], tuple) and chunks[-1][0] == "__done__"
    state = chunks[-1][1]
    text_chunks = [c for c in chunks[:-1] if isinstance(c, str)]
    return "".join(text_chunks), state


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_two_messages_are_separated_so_spell_starts_a_line():
    events = (
        _message_events("msg_1", ["了解、", "調べてみるね。"])
        + _message_events("msg_2", ["/spell name='x'", " args={}"])
        + [_completed()]
    )
    streamed, state = _run(events)

    assert streamed == "了解、調べてみるね。\n/spell name='x' args={}"
    assert SPELL_LINE.search(streamed) is not None
    assert state["text"] == streamed
    assert SPELL_LINE.search(state["text"]) is not None
    assert state["message_texts"] == ["了解、調べてみるね。", "/spell name='x' args={}"]


def test_single_message_output_is_unchanged():
    events = _message_events("msg_1", ["こんにちは", "、まはー。"]) + [_completed()]
    streamed, state = _run(events)

    assert streamed == "こんにちは、まはー。"
    assert state["text"] == "こんにちは、まはー。"
    assert not streamed.startswith("\n")


def test_no_extra_newline_when_first_message_already_ends_with_one():
    events = (
        _message_events("msg_1", ["前置き\n"])
        + _message_events("msg_2", ["/spell name='x' args={}"])
        + [_completed()]
    )
    streamed, state = _run(events)

    assert streamed == "前置き\n/spell name='x' args={}"
    assert state["text"] == streamed


def test_function_call_item_after_message_adds_no_separator():
    events = _message_events("msg_1", ["呼び出すよ。"]) + [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "do_thing",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"a": 1}',
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "do_thing",
                "arguments": '{"a": 1}',
            },
        },
        _completed(),
    ]
    streamed, state = _run(events)

    assert streamed == "呼び出すよ。"
    assert state["text"] == "呼び出すよ。"
    assert state["function_calls"] == [
        {"call_id": "call_1", "name": "do_thing", "arguments": '{"a": 1}'}
    ]


def test_done_only_without_deltas_aggregates_all_messages():
    events = (
        _message_events("msg_1", ["一つ目"], with_done=False)[:1]
        + [
            {
                "type": "response.output_text.done",
                "item_id": "msg_1",
                "content_index": 0,
                "text": "一つ目",
            }
        ]
        + _message_events("msg_2", [], with_done=False)[:1]
        + [
            {
                "type": "response.output_text.done",
                "item_id": "msg_2",
                "content_index": 0,
                "text": "/spell name='x' args={}",
            },
            _completed(),
        ]
    )
    streamed, state = _run(events)

    # done にしか本文が無い応答でも、ストリームの消費者に本文が届き、
    # 流した結合と集約テキストが一致する (集約だけが本文を知る状態を作らない)。
    assert streamed == "一つ目\n/spell name='x' args={}"
    assert state["text"] == streamed
    assert SPELL_LINE.search(state["text"]) is not None


def test_partial_done_keeps_all_streamed_messages():
    # 1 つ目のメッセージの done が欠落し、2 つ目だけ done が届いた形。
    # ストリームに流れた本文は全部残り、集約テキストと一致する
    # (done が来たメッセージだけに縮まない)。
    events = (
        _message_events("msg_1", ["前置き。"], with_done=False)
        + _message_events("msg_2", ["/spell name='x' args={}"])
        + [_completed()]
    )
    streamed, state = _run(events)

    assert streamed == "前置き。\n/spell name='x' args={}"
    assert state["text"] == streamed
    assert SPELL_LINE.search(state["text"]) is not None


def test_completed_only_response_streams_the_recovered_text():
    # delta も done も無く、response.completed だけが本文を運んだ形。
    # 復元した本文はストリームにも一度だけ流れ、集約テキストと一致する。
    output = [
        {"type": "message", "content": [{"type": "output_text", "text": "前置き。"}]},
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "/spell name='x' args={}"}],
        },
    ]
    streamed, state = _run([_completed(output)])

    assert streamed == "前置き。\n/spell name='x' args={}"
    assert state["text"] == streamed


def test_multi_part_done_with_empty_text_does_not_double_count():
    # 同一メッセージ内で content part ごとに done が来て、後続 part の text が
    # 欠落している形。フォールバックが「まだ done に消費されていない delta」
    # だけを読むこと (メッセージ先頭まで遡って二重計上しないこと) を確かめる。
    events = [
        {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "AAA"},
        {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "content_index": 0,
            "text": "AAA",
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "BBB"},
        {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "content_index": 1,
            "text": "",
        },
        _completed(),
    ]
    streamed, state = _run(events)

    assert streamed == "AAABBB"
    assert state["text"] == "AAABBB"


def test_done_without_item_id_keeps_message_boundary():
    # done イベントが item_id を欠く実装でも、message item の added の数で
    # メッセージを区別し、境目の改行が集約テキストから消えないことを確かめる。
    def _strip_ids(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for ev in events:
            ev = dict(ev)
            if ev["type"] == "response.output_text.done":
                ev.pop("item_id", None)
            out.append(ev)
        return out

    events = _strip_ids(
        _message_events("msg_1", ["前置き。"])
        + _message_events("msg_2", ["/spell name='x' args={}"])
    ) + [_completed()]
    streamed, state = _run(events)

    assert streamed == "前置き。\n/spell name='x' args={}"
    assert state["text"] == streamed
    assert SPELL_LINE.search(state["text"]) is not None


def test_delta_item_id_switch_inserts_boundary_without_added_event():
    # output_item.added が届かない形でも、delta の item_id の切り替わりを
    # メッセージ境界として扱い、/spell が行頭に立つことを確かめる。
    events = [
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "前置き。"},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_2",
            "delta": "/spell name='x' args={}",
        },
        _completed(),
    ]
    streamed, state = _run(events)

    assert streamed == "前置き。\n/spell name='x' args={}"
    assert state["text"] == streamed
    assert SPELL_LINE.search(state["text"]) is not None


def test_completed_fallback_separates_message_items():
    output = [
        {"type": "reasoning", "summary": []},
        {
            "type": "message",
            "content": [
                {"type": "output_text", "text": "前半"},
                {"type": "output_text", "text": "の続き"},
            ],
        },
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "/spell name='x' args={}"}],
        },
    ]
    _, state = _run([_completed(output)])

    assert state["text"] == "前半の続き\n/spell name='x' args={}"


def test_structured_output_parses_last_message_when_preamble_breaks_full_text(monkeypatch):
    client = _client()
    events = (
        _message_events("msg_1", ["形は {a} の通りに返すね。"])
        + _message_events("msg_2", ['{"ok": true}'])
        + [_completed()]
    )
    monkeypatch.setattr(client, "_inject_unsupported_media_summaries", lambda m: m, raising=False)
    monkeypatch.setattr(client, "_build_body", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(
        client, "_post_with_auth_retry", lambda body: _FakeResponse(events), raising=False
    )
    monkeypatch.setattr(client, "_finalize", lambda state, tools: None, raising=False)

    result = client.generate([], response_schema={"type": "object"})

    assert result == {"ok": True}


def test_structured_output_still_raises_when_nothing_parses(monkeypatch):
    client = _client()
    events = _message_events("msg_1", ["JSON ではない"]) + [_completed()]
    monkeypatch.setattr(client, "_inject_unsupported_media_summaries", lambda m: m, raising=False)
    monkeypatch.setattr(client, "_build_body", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(
        client, "_post_with_auth_retry", lambda body: _FakeResponse(events), raising=False
    )
    monkeypatch.setattr(client, "_finalize", lambda state, tools: None, raising=False)

    with pytest.raises(RuntimeError, match="Failed to parse JSON"):
        client.generate([], response_schema={"type": "object"})


# The October duplicate-response investigation changes diagnostics only.
# These fixtures include the complete, normal lifecycle, which republishes
# finalized text in several different events without generating another answer.

def _full_message_events(item_id, text, *, phase="final_answer"):
    item = {"type": "message", "id": item_id, "role": "assistant", "phase": phase,
            "content": [{"type": "output_text", "text": text}]}
    events = _message_events(item_id, [text])
    events[0]["item"]["phase"] = phase
    events[-1]["item"] = item
    events.insert(-1, {"type": "response.content_part.done", "item_id": item_id,
                       "content_index": 0, "part": item["content"][0]})
    return events, item


def _numbered(events):
    return [dict(event, sequence_number=index) for index, event in enumerate(events)]


def _diagnostics(caplog):
    prefix = "Codex stream diagnostic "
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.getMessage().startswith(prefix)]


@pytest.mark.parametrize("text", ["返事です。", "/spell document_edit old_string='前' new_string='後'\n"])
def test_normal_lifecycle_republishes_text_without_reemitting_it(text, caplog):
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    events, item = _full_message_events("msg_1", text)
    streamed, state = _run(_numbered(events + [_completed([item])]))
    assert streamed == text == state["text"]
    records = _diagnostics(caplog)
    done = next(r for r in records if r["event"] == "response.output_text.done")
    assembled = records[-1]
    assert done["done"] == done["streamed_part"] == assembled["text"]
    assert done["recovered_chars"] == 0
    assert assembled["event"] == "assembled"
    assert len({record["stream"] for record in records}) == 1
    assert not any(r.get("sequence_nonincreasing") for r in records)
    assert not any(r["event"] == "response.output_text.delta" for r in records)


def test_distinct_identical_messages_are_preserved_and_distinguishable(caplog):
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    text = "同じ発言"
    first, item1 = _full_message_events("msg_1", text, phase="commentary")
    second, item2 = _full_message_events("msg_2", text)
    streamed, state = _run(_numbered(first + second + [_completed([item1, item2])]))
    assert streamed == state["text"] == f"{text}\n{text}"
    done = [r for r in _diagnostics(caplog) if r["event"] == "response.output_text.done"]
    assert done[0]["item_ref"] != done[1]["item_ref"]
    assert done[0]["done"]["ref"] == done[1]["done"]["ref"]
    completed = next(r for r in _diagnostics(caplog) if r["event"] == "response.completed")
    assert [i["phase"] for i in completed["output"]] == ["commentary", "final_answer"]


def test_identical_content_parts_are_not_deduplicated(caplog):
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    text = "繰り返す"
    events, item = _full_message_events("msg_1", text)
    events[1]["content_index"] = 0
    extra = [dict(event, content_index=1) for event in events[1:-1]]
    item["content"] *= 2
    streamed, state = _run(_numbered(events[:-1] + extra + events[-1:] + [_completed([item])]))
    assert streamed == state["text"] == text * 2
    done = [r for r in _diagnostics(caplog) if r["event"] == "response.output_text.done"]
    assert [r["content_index"] for r in done] == [0, 1]
    assert done[0]["item_ref"] == done[1]["item_ref"]
    assert done[0]["done"] == done[1]["done"]
    assert all(r["recovered_chars"] == 0 for r in done)


def test_same_arguments_with_distinct_tool_call_ids_stay_distinct(caplog):
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    events = []
    items = []
    for index in range(2):
        item = {"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}",
                "name": "document_edit", "arguments": '{"old_string":"private-original"}'}
        items.append(item)
        events += [
            {"type": "response.output_item.added", "output_index": index,
             "item": dict(item, arguments="")},
            {"type": "response.function_call_arguments.delta", "item_id": item["id"],
             "delta": item["arguments"]},
            {"type": "response.output_item.done", "output_index": index, "item": item},
        ]
    streamed, state = _run(_numbered(events + [_completed(items)]))
    assert streamed == ""
    assert [c["call_id"] for c in state["function_calls"]] == ["call_0", "call_1"]
    assert state["function_calls"][0]["arguments"] == state["function_calls"][1]["arguments"]
    records = _diagnostics(caplog)
    calls = next(r for r in records if r["event"] == "response.completed")["output"]
    assert calls[0]["call_ref"] != calls[1]["call_ref"]
    assert calls[0]["arguments"] == calls[1]["arguments"]
    assert records[-1]["function_calls"] == 2
    assert "private-original" not in caplog.text
    assert "document_edit" not in caplog.text


def test_replayed_done_is_visible_as_diagnostic_evidence_only(caplog):
    # Synthetic hypothesis, NOT an observed incident or a desired-output spec.
    # Do not add duplicate suppression until the reporter's wire shape is known.
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    events, item = _full_message_events("msg_1", "返事\n")
    events = _numbered(events)
    events.insert(3, dict(events[2]))
    streamed, state = _run(events + [dict(_completed([item]), sequence_number=10)])
    records = _diagnostics(caplog)
    done = [r for r in records if r["event"] == "response.output_text.done"]
    assert done[0]["item_ref"] == done[1]["item_ref"]
    assert done[0]["done"] == done[1]["done"]
    assert done[1]["sequence_nonincreasing"] is True
    assert done[0]["recovered_chars"] == 0
    assert done[1]["streamed_part"]["chars"] == 0
    assert done[1]["recovered_chars"] == done[1]["done"]["chars"]
    assert records[-1]["text"]["chars"] == len(streamed) == len(state["text"])


def test_nonincreasing_delta_sequence_is_logged_without_text(caplog):
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    events = _numbered(_message_events("msg_1", ["private-delta"]))
    events.insert(2, dict(events[1]))
    _run(events + [dict(_completed(), sequence_number=10)])
    deltas = [r for r in _diagnostics(caplog) if r["event"] == "response.output_text.delta"]
    assert len(deltas) == 1
    assert deltas[0]["sequence_nonincreasing"] is True
    assert "private-delta" not in caplog.text


def test_diagnostics_contain_no_raw_private_fields_or_cross_stream_identifiers(caplog):
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    text = "private-answer-and-spell-arguments"
    events, item = _full_message_events("private-provider-item-id", text)
    events.insert(0, {"type": "response.created", "response": {"id": "private-response-id",
                       "instructions": "private-instructions", "output": []}})
    events.insert(1, {"type": "response.reasoning_text.delta", "delta": "private-reasoning"})
    events.append(_completed([item]))
    for event in events:
        event["Authorization"] = "private-token"
        event["unknown_field"] = "private-unknown"
    _run(_numbered(events))
    first = _diagnostics(caplog)
    caplog.clear()
    _run(_numbered(events))
    second = _diagnostics(caplog)
    assert first[0]["stream"] != second[0]["stream"]
    # The emitted schema is closed; raw IDs are also response-local aliases.
    assert "private-" not in json.dumps(first + second)


def test_info_level_does_not_allocate_diagnostics(caplog, monkeypatch):
    import llm_clients.openai_codex as codex

    caplog.set_level("INFO", logger=codex.LOG.name)

    def unexpected_diagnostics():
        pytest.fail("DEBUG diagnostics must not be allocated at INFO")

    monkeypatch.setattr(codex, "_StreamDiagnostics", unexpected_diagnostics)
    streamed, state = _run(_message_events("msg_1", ["返事"]) + [_completed()])
    assert streamed == state["text"] == "返事"
    assert not _diagnostics(caplog)


@pytest.mark.parametrize("streaming", [False, True])
def test_public_generation_paths_return_the_same_single_answer(streaming, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="saiverse.llm_clients.openai_codex")
    client = _client()
    events, item = _full_message_events("msg_1", "一度だけの返事")
    monkeypatch.setattr(client, "_inject_unsupported_media_summaries", lambda m: m)
    monkeypatch.setattr(client, "_build_body", lambda *a, **k: {})
    monkeypatch.setattr(client, "_post_with_auth_retry", lambda body: _FakeResponse(events + [_completed([item])]))
    finalized = []
    monkeypatch.setattr(client, "_finalize", lambda state, tools: finalized.append(state))
    if streaming:
        result = "".join(client.generate_stream([]))
    else:
        result = client.generate([])
    assert result == "一度だけの返事"
    assert len(finalized) == 1
    assert finalized[0]["text"] == result


@pytest.mark.parametrize("copies", [1, 4])
@pytest.mark.asyncio
async def test_codex_stream_reaches_sea_spell_parser_without_changing_call_count(copies, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from sea.runtime_llm import _consume_pipeline_stream, _parse_spell_lines

    line = "/spell name='document_edit' args={\"old_string\":\"前\",\"new_string\":\"後\"}\n"
    events, item = _full_message_events("msg_1", line * copies)
    client = _client()
    monkeypatch.setattr(client, "_inject_unsupported_media_summaries", lambda m: m)
    monkeypatch.setattr(client, "_build_body", lambda *a, **k: {})
    monkeypatch.setattr(client, "_post_with_auth_retry", lambda body: _FakeResponse(events + [_completed([item])]))
    monkeypatch.setattr(client, "_finalize", lambda state, tools: None)
    runtime = MagicMock()
    runtime._effective_building_id.return_value = "isolated-building"
    emitted = []
    text, _, _, cancelled = await _consume_pipeline_stream(
        client.generate_stream([]), runtime=runtime,
        persona=SimpleNamespace(persona_id="isolated-persona"), building_id="isolated-building",
        node_def=SimpleNamespace(id="llm"), state={}, pipeline_msg_id=None,
        sub_seq_start=0, cancellation_token=None, event_callback=emitted.append,
        emit_building_id="isolated-building",
    )
    assert not cancelled
    assert text == line * copies
    assert "".join(e["content"] for e in emitted if e["type"] == "streaming_chunk") == text
    calls = _parse_spell_lines(text, quiet=True)
    assert len(calls) == copies
    assert all(call.name == "document_edit" for call in calls)
    # The boundary test stops before tool execution: no live document is edited.
