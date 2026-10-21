"""ペルソナまるごとパック (persona-pack) の JSON 配列を 1 要素ずつ読む。

正典: docs/intent/persona_pack.md §3 原則 6 (パックの中身を一度に全部メモリへ
広げない)。ZIP の 1 エントリ (``zf.open(name)`` が返すバイナリストリーム) から、
トップレベルが JSON 配列のファイルを先頭から 1 要素ずつ返す。

- 書式は正規の JSON 配列一般を受ける (任意の空白・改行、詰めた 1 行)。export.py の
  「1 要素 1 行」には依存しない — 別の書き手 (SAIVerse Lite) のパックも読むため。
- 値の解釈は ``json.JSONDecoder().raw_decode`` に任せるので、``json.loads`` と同じ
  受理になる (NaN / Infinity の受理、重複キーの後勝ち、制御文字の拒否)。
- 1 要素のテキストが ``max_element_chars`` 文字を超えたら、切り詰めずに拒否する
  (原則 2「本人名義のテキストは無改変」)。メモリに載るテキストは、読みかけの要素
  (上限 + 1 文字まで) と、読み足しの単位 (``_READ_CHARS``) 程度の先読みだけ。
- 要素の型 (dict かどうか・必須キー) は検査しない。呼び出し側の仕事。

拒否はすべて ValueError で、文言に ``name`` を含める (呼び出し側とテストが
ファイル名で照合している)。
"""
from __future__ import annotations

import io
import json
import re
from typing import Any, BinaryIO, Iterator

# 読み足しの最初の単位 (文字数)。要素が読み切れないたびに倍にする
# (失敗のたびに要素の先頭から解析し直すので、倍々にして再解析を償却で線形に抑える)。
# テストは小さい値に差し替えて、窓の境界の扱いを確かめる。
_READ_CHARS = 1 << 20

# 窓の末尾からこの文字数以内で起きた解析の失敗と、この範囲で終わった値は、
# 読み足せば結果が変わりうるものとして扱う。値の途中で窓が切れたときに raw_decode が
# 返す失敗の位置 (切れた数値・リテラル・\uXXXX エスケープ) は窓の末尾から数文字以内、
# 末尾の数値 (``1`` の後に ``e5`` が続く場合) も末尾の数文字手前で値を返すので、
# 十分な余裕を取った値。
_TAIL_CHARS = 64

_WS = re.compile(r"[ \t\n\r]*")


def iter_json_array(raw: BinaryIO, name: str, *, max_element_chars: int) -> Iterator[Any]:
    """``raw`` のトップレベルの JSON 配列を、先頭から 1 要素ずつ返す。

    ``raw`` は呼び出し側の持ち物で、ここでは閉じない (読み終えたら、または生成器が
    途中で閉じられたら、TextIOWrapper を切り離して返す)。

    ValueError で拒否するもの: UTF-8 として不正、BOM 付き、JSON として壊れている、
    トップレベルが配列でない、配列の後に空白以外がある、1 要素のテキストが
    ``max_element_chars`` 文字を超える。
    """
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="")
    try:
        yield from _iter_elements(text, name, max_element_chars)
    finally:
        try:
            text.detach()
        except ValueError:  # 既に切り離されている / 閉じられている
            pass


def _iter_elements(text: io.TextIOWrapper, name: str, max_element_chars: int) -> Iterator[Any]:
    decoder = json.JSONDecoder()
    buf = ""
    pos = 0  # buf の中の読み位置
    consumed = 0  # 窓をずらして捨てた文字数 (エラー文の位置を先頭からの文字数で出すため)
    eof = False

    def invalid(detail: str, at: int) -> ValueError:
        return ValueError(f"persona pack: {name} is not valid JSON: {detail}: char {consumed + at}")

    def fill(n: int) -> None:
        """消費済みの部分を捨ててから n 文字読み足す。"""
        nonlocal buf, pos, consumed, eof
        try:
            chunk = text.read(n)
        except UnicodeDecodeError as exc:
            raise ValueError(f"persona pack: {name} is not valid JSON: {exc}") from exc
        consumed += pos
        buf = buf[pos:] + chunk
        pos = 0
        if not chunk:
            eof = True

    def skip_ws() -> None:
        """空白を飛ばす。buf の末尾まで空白なら読み足して続ける。"""
        nonlocal pos
        while True:
            pos = _WS.match(buf, pos).end()
            if pos < len(buf) or eof:
                return
            fill(_READ_CHARS)

    def finish() -> None:
        """配列を閉じた後に残っているのが空白だけであることを確かめる。"""
        skip_ws()
        if pos < len(buf):
            raise invalid("Extra data", pos)

    fill(_READ_CHARS)
    if buf.startswith("﻿"):
        # json.loads と同じ拒否 (UTF-8 の BOM は JSON では許されない)
        raise invalid("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0)
    skip_ws()
    if pos >= len(buf):
        raise invalid("Expecting value", pos)
    if buf[pos] != "[":
        raise ValueError(f"persona pack: {name} must be a JSON array")
    pos += 1
    skip_ws()
    if pos < len(buf) and buf[pos] == "]":
        pos += 1
        finish()
        return

    index = 0
    while True:
        want = _READ_CHARS
        while True:
            try:
                obj, end = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError as exc:
                truncated = exc.msg.startswith("Unterminated string") or exc.pos >= len(buf) - _TAIL_CHARS
                if eof or not truncated:
                    raise invalid(exc.msg, exc.pos) from exc
                # 読み切れていない。buf[pos:] はすべてこの要素の一部
                remaining = len(buf) - pos
                if remaining > max_element_chars:
                    raise _too_large(name, index, max_element_chars)
                fill(min(want, max_element_chars + 1 - remaining))
                want *= 2
                continue
            if end > len(buf) - _TAIL_CHARS and not eof:
                # 末尾の数値などが窓の端で切れている可能性がある。読み足して解析し直す
                fill(_READ_CHARS)
                continue
            break
        if end - pos > max_element_chars:
            raise _too_large(name, index, max_element_chars)
        yield obj
        index += 1
        pos = end
        skip_ws()
        if pos >= len(buf):
            raise invalid("Expecting ',' delimiter", pos)
        c = buf[pos]
        pos += 1
        if c == "]":
            finish()
            return
        if c != ",":
            raise invalid("Expecting ',' delimiter", pos - 1)
        skip_ws()


def _too_large(name: str, index: int, max_element_chars: int) -> ValueError:
    return ValueError(
        f"persona pack: {name}[{index}] is over the limit of {max_element_chars} characters "
        "per element (refused, not truncated)"
    )
