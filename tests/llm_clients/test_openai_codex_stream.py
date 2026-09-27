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
