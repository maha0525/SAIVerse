"""ペルソナまるごとパック (persona-pack) の形式定数と値の符号化。

正典: docs/intent/persona_pack.md §3-5 (データ形式のルール)。

- バイナリ値 (``messages.thought_signature`` など SQLite に BLOB で入っている値)
  は ``{"__bytes_b64__": "<base64>"}`` の一要素 dict に包む。JSON に bytes を
  置けないことが旧スレッド単位エクスポート (native_export.py) の壊れ方だった。
- JSON 文字列として保存されている列 (``messages.metadata`` など) は、JSON の
  object / array として解釈できれば展開して書く。それ以外の文字列 (解釈できない
  文字列と、``"null"`` ``"123"`` のように JSON のスカラーとして解釈できる文字列) は
  ``{"__raw_text__": "<元の文字列>"}`` に包み、元の文字列のまま戻せるようにする
  (スカラーを展開すると、``"null"`` が SQL の NULL に、``" 1e2 "`` が ``"100.0"`` に
  変わって往復で元に戻らない)。``__raw_text__`` の中身が object / array として
  解釈できるパックは正規の書き出しでは生まれず、取り込みはその行を拒否する
  (:func:`encode_json_text` の契約)。
- epoch 秒で保存されている日時は、タイムゾーン付き ISO 8601 (UTC) に変換する。

取り込み側は :func:`decode_value` / :func:`iso_to_epoch` で元の値へ戻す。
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

FORMAT_NAME = "persona-pack"
FORMAT_VERSION = "1.0"
MANIFEST_NAME = "manifest.json"

BYTES_MARKER = "__bytes_b64__"
RAW_TEXT_MARKER = "__raw_text__"


def encode_value(value: Any) -> Any:
    """値を JSON に書ける形へ写す (bytes は印付きの base64 に包む)。dict / list は再帰する。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {BYTES_MARKER: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): encode_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    return value


def decode_value(value: Any) -> Any:
    """:func:`encode_value` / :func:`encode_json_text` の逆。印付きの包みを元の値へ戻す。

    ``{"__raw_text__": s}`` は元の文字列 ``s`` に戻る (JSON 文字列列の値として
    そのまま保存し直すためのもの)。
    """
    if isinstance(value, dict):
        if len(value) == 1:
            if BYTES_MARKER in value and isinstance(value[BYTES_MARKER], str):
                return base64.b64decode(value[BYTES_MARKER])
            if RAW_TEXT_MARKER in value and isinstance(value[RAW_TEXT_MARKER], str):
                return value[RAW_TEXT_MARKER]
        return {k: decode_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_value(v) for v in value]
    return value


def encode_json_text(value: Any) -> Any:
    """JSON 文字列として保存されている列の値を、JSON の値として展開する。

    NULL は None のまま。展開するのは JSON の object / array になる文字列だけで、
    それ以外の文字列 (解釈できないもの・スカラー) は ``{"__raw_text__": ...}`` に包む。
    文字列以外 (bytes など) は :func:`encode_value` に渡す。

    契約: ``{"__raw_text__": s}`` に包むのは、``json.loads(s)`` が失敗するか JSON の
    スカラーになる文字列だけ。object / array として解釈できる文字列は必ず展開する。
    取り込み側 (importer.py の _load_pack) はこの契約に頼り、``__raw_text__`` の中身が
    object / array として解釈できる行を拒否する — 包んだまま保存すると、展開した値に
    かける検査 (添付の参照の書き換えなど) を通らずに、保存後の読み手 (json.loads) には
    object / array として届くため。
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {RAW_TEXT_MARKER: value}
        if not isinstance(parsed, (dict, list)):
            return {RAW_TEXT_MARKER: value}
        return encode_value(parsed)
    return encode_value(value)


def epoch_to_iso(value: Any) -> Any:
    """epoch 秒をタイムゾーン付き ISO 8601 (UTC) にする。数値以外はそのまま返す。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return value


def iso_to_epoch(value: Any) -> Any:
    """:func:`epoch_to_iso` の逆。秒に端数が無ければ int、あれば float を返す。"""
    if not isinstance(value, str):
        return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    return int(ts) if dt.microsecond == 0 else ts
