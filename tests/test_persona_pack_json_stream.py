"""persona-pack の JSON 配列を 1 要素ずつ読む iter_json_array のテスト。

正典: docs/intent/persona_pack.md §3 原則 6。受理は json.loads と一致させ、
拒否は ValueError でファイル名を文言に含める。
"""
from __future__ import annotations

import io
import json
import re
from typing import Any, List

import pytest

from saiverse_memory.persona_pack import json_stream
from saiverse_memory.persona_pack.json_stream import iter_json_array

NAME = "journal/clips.json"
LIMIT = 10_000_000

ROWS: List[Any] = [
    {"id": "m1", "content": "こんにちは、世界", "seq": 1, "metadata": {"tags": ["a", "b"], "n": None}},
    {"id": "m2", "content": "絵文字 😀 と改行\nとタブ\t", "score": -12.5e3, "flag": True},
    {"id": "m3", "nested": [[1, 2, [3]], {"k": {"deep": "値"}}], "empty": {}, "list": []},
]


def _read(data: bytes, name: str = NAME, limit: int = LIMIT) -> List[Any]:
    return list(iter_json_array(io.BytesIO(data), name, max_element_chars=limit))


def _same(a: Any, b: Any) -> bool:
    """NaN を含んでも比べられるよう、JSON の文字列にして比べる。"""
    return json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def _export_style(rows: List[Any]) -> bytes:
    """export.py が配列を書く形 (1 要素 1 行)。"""
    parts = ["["]
    for i, row in enumerate(rows):
        parts.append(("\n" if i == 0 else ",\n") + json.dumps(row, ensure_ascii=False))
    parts.append("\n]\n")
    return "".join(parts).encode("utf-8")


class _TrickleIO(io.BytesIO):
    """1 回の読み出しで最大 n バイトしか返さない (多バイト文字をバイトの境界で割る)。"""

    def __init__(self, data: bytes, n: int) -> None:
        super().__init__(data)
        self.n = n

    def read(self, size: int = -1) -> bytes:
        return super().read(self.n)

    def read1(self, size: int = -1) -> bytes:
        return super().read1(self.n)


class _CountingIO(io.BytesIO):
    """返したバイト数を数える。"""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.delivered = 0

    def read(self, size: int = -1) -> bytes:
        out = super().read(size)
        self.delivered += len(out)
        return out

    def read1(self, size: int = -1) -> bytes:
        out = super().read1(size)
        self.delivered += len(out)
        return out


# ---------------------------------------------------------------------------
# 受理
# ---------------------------------------------------------------------------


def test_export_style_compact_and_whitespace_heavy_forms_read_the_same() -> None:
    export_form = _export_style(ROWS)
    compact = json.dumps(ROWS, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    indented = json.dumps(ROWS, ensure_ascii=False, indent=4).encode("utf-8")
    spaced = (" \r\n\t[ \n\n" + " ,\r\n\t ".join(json.dumps(r, ensure_ascii=False) for r in ROWS)
              + "\n\n ] \n\t").encode("utf-8")
    for data in (export_form, compact, indented, spaced):
        assert _read(data) == ROWS


@pytest.mark.parametrize("data", [b"[]", b"[ ]", b"\n[\n]\n", b" \t[\r\n] "])
def test_empty_array(data: bytes) -> None:
    assert _read(data) == []


def test_scalar_and_nested_elements_match_json_loads() -> None:
    text = ('[0, -0, 1, -12.5e+3, 1E5, 123456789012345678901234567890, "", "文字列", '
            '"\\u3042\\ud83d\\ude00\\n", true, false, null, [], {}, [[1, [2, [3]]]], '
            '{"a": {"b": [1, {"c": null}]}}, 7]')
    assert _same(_read(text.encode("utf-8")), json.loads(text))


def test_nan_and_infinity_are_accepted_like_json_loads() -> None:
    text = "[NaN, Infinity, -Infinity, {\"x\": NaN}]"
    assert _same(_read(text.encode("utf-8")), json.loads(text))


def test_duplicate_keys_keep_the_last_value_like_json_loads() -> None:
    assert _read(b'[{"a": 1, "a": 2}]') == [{"a": 2}]


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13])
def test_window_boundaries_do_not_change_the_result(monkeypatch: pytest.MonkeyPatch, chunk: int) -> None:
    """読み足しの単位を小さくして、値・数値・リテラル・多バイト文字を窓の境界で割る。"""
    monkeypatch.setattr(json_stream, "_READ_CHARS", chunk)
    text = ('[1e5, 12345, -0.25E-3, "日本語のテキスト😀", true, null, NaN, -Infinity, '
            '{"キー": [1.5, "値"]}, 987654321]')
    assert _same(_read(text.encode("utf-8")), json.loads(text))
    assert _read(_export_style(ROWS)) == ROWS


@pytest.mark.parametrize("nbytes", [1, 2])
def test_multibyte_characters_split_at_byte_boundaries(nbytes: int) -> None:
    data = _export_style(ROWS)
    assert list(iter_json_array(_TrickleIO(data, nbytes), NAME, max_element_chars=LIMIT)) == ROWS


def test_element_larger_than_the_read_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_stream, "_READ_CHARS", 16)
    big = {"id": "big", "content": "長い本文" * 5000, "n": 12345}
    rows = [{"id": "a"}, big, {"id": "b"}]
    assert _read(_export_style(rows)) == rows


def test_element_larger_than_the_default_read_window() -> None:
    """既定の読み足しの単位 (_READ_CHARS) を超える要素が、そのまま読める。"""
    big = {"id": "big", "content": "あ" * (json_stream._READ_CHARS + 1000) + "末尾"}
    rows = [big, 42]
    assert _read(_export_style(rows), limit=json_stream._READ_CHARS * 4) == rows


def test_element_of_exactly_the_limit_is_accepted() -> None:
    element = json.dumps({"c": "x" * 50})
    data = ("[" + element + ", 1]").encode("utf-8")
    assert _read(data, limit=len(element)) == [{"c": "x" * 50}, 1]


# ---------------------------------------------------------------------------
# 拒否 (どれも ValueError で、文言にファイル名が入る)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty-file"),
        pytest.param(b"   \n", id="whitespace-only"),
        pytest.param(b'[{"clip_id": ', id="truncated-element"),
        pytest.param(b"[1, 2", id="unclosed-array"),
        pytest.param(b"[1,]", id="trailing-comma"),
        pytest.param(b"[1 2]", id="missing-comma"),
        pytest.param(b'[{"a": }]', id="broken-element"),
        pytest.param(b'["a\x01b"]', id="control-character"),
        pytest.param(b"[1] x", id="extra-data"),
        pytest.param(b"[] []", id="second-array"),
        pytest.param(b"[1]]", id="extra-bracket"),
    ],
)
def test_broken_json_is_refused_with_the_name(data: bytes) -> None:
    with pytest.raises(ValueError, match=re.escape(f"{NAME} is not valid JSON")):
        _read(data)


def test_broken_json_after_many_elements_is_refused_without_reaching_the_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """読み足しても直らない壊れ方は、その場で壊れた JSON として拒否する (上限まで読み進めない)。"""
    monkeypatch.setattr(json_stream, "_READ_CHARS", 8)
    data = b'[{"a": } ' + b" " * 5000 + b", 1]"
    with pytest.raises(ValueError, match=re.escape(f"{NAME} is not valid JSON")):
        _read(data, limit=200)


@pytest.mark.parametrize("data", [b"{broken", b'{"a": 1}', b"1", b'"text"', b"null"])
def test_non_array_top_level_is_refused_with_the_name(data: bytes) -> None:
    with pytest.raises(ValueError, match=re.escape(f"{NAME} must be a JSON array")):
        _read(data)


def test_bom_is_refused_like_json_loads() -> None:
    data = "﻿[1]".encode("utf-8")
    with pytest.raises(json.JSONDecodeError):
        json.loads(data.decode("utf-8"))
    with pytest.raises(ValueError, match=re.escape(f"{NAME} is not valid JSON") + ".*BOM"):
        _read(data)


@pytest.mark.parametrize("data", [b'["\xff"]', b'[1, "\xe3\x81"]', b"[1, 2" + b"\x80" + b"]"])
def test_invalid_utf8_is_refused_with_the_name(data: bytes) -> None:
    with pytest.raises(ValueError, match=re.escape(f"{NAME} is not valid JSON")) as info:
        _read(data)
    assert isinstance(info.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize("chunk", [8, 1 << 20])
def test_element_over_the_limit_is_refused_with_name_and_index(
    monkeypatch: pytest.MonkeyPatch, chunk: int,
) -> None:
    """読みかけで上限を超える場合 (小さい窓) と、読み切ってから超えていると分かる場合 (大きい窓)。"""
    monkeypatch.setattr(json_stream, "_READ_CHARS", chunk)
    name = "chatlog/messages.json"
    data = _export_style([{"id": "ok"}, {"id": "big", "content": "本文" * 100}, {"id": "after"}])
    got: List[Any] = []
    with pytest.raises(ValueError, match=re.escape(f"{name}[1] is over the limit of 60 characters")):
        for row in iter_json_array(io.BytesIO(data), name, max_element_chars=60):
            got.append(row)
    assert got == [{"id": "ok"}]


# ---------------------------------------------------------------------------
# 一度に全部読まない・ストリームを閉じない
# ---------------------------------------------------------------------------


def test_first_element_is_yielded_before_the_whole_stream_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_stream, "_READ_CHARS", 1024)
    rows = [{"i": i, "text": "本文" * 50} for i in range(5000)]
    data = _export_style(rows)
    raw = _CountingIO(data)
    it = iter_json_array(raw, NAME, max_element_chars=LIMIT)
    assert next(it) == rows[0]
    assert raw.delivered < len(data) // 10
    assert [rows[0], *it] == rows
    assert raw.delivered == len(data)


def test_the_stream_is_left_open_for_the_caller() -> None:
    raw = io.BytesIO(_export_style(ROWS))
    assert list(iter_json_array(raw, NAME, max_element_chars=LIMIT)) == ROWS
    assert not raw.closed

    raw = io.BytesIO(_export_style(ROWS))
    it = iter_json_array(raw, NAME, max_element_chars=LIMIT)
    next(it)
    it.close()
    assert not raw.closed

    raw = io.BytesIO(b"[1] x")
    with pytest.raises(ValueError):
        list(iter_json_array(raw, NAME, max_element_chars=LIMIT))
    assert not raw.closed
