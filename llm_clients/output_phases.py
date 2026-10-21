"""Per-message split of one assistant response (``output_phases``).

Codex (OpenAI Responses API) returns one response as several output message
items, each with a ``phase`` ("commentary" / "final_answer"). The client joins
them into one text; ``output_phases`` keeps the per-message split so a later
request can send the past assistant turn back as the same messages with their
``phase`` (docs/issues/codex_output_phase_passback.md).

Shape: ``[{"phase": "commentary" | "final_answer" | None, "text": str}, ...]``
with no empty ``text``. The list is only meaningful together with the text it
was split from: :func:`join_message_texts` of the texts must equal that text
exactly. Every producer and consumer checks that equality and treats a
mismatch as "no split known" (the caller then sends one plain message, as
before).

This module holds the join rule and every function that builds or checks the
list, so the rule exists in one place for the client (which produces and
consumes the list) and for SEA (which stores it next to a possibly rebuilt
text).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

OUTPUT_PHASES_METADATA_KEY = "output_phases"
"""Key of the list in an assistant message's ``metadata``."""

VALID_PHASES = ("commentary", "final_answer")


def join_message_texts(texts: Sequence[str]) -> str:
    """Join per-message texts the same way the Codex client streams them.

    空のメッセージは飛ばし、前の本文が改行で終わっていない境目にだけ改行を
    挟む。ストリームで流した chunk の結合と同じ文字列になる。
    """
    joined = ""
    for text in texts:
        if not text:
            continue
        if joined and not joined.endswith("\n"):
            joined += "\n"
        joined += text
    return joined


def normalize_phase(value: Any) -> Optional[str]:
    """Return ``value`` when it is a known phase name, else None."""
    return value if value in VALID_PHASES else None


def coerce_output_phases(value: Any) -> Optional[List[Dict[str, Any]]]:
    """Return a clean copy of an ``output_phases`` list, or None.

    Accepts only a list whose items are dicts with a ``str`` ``text``. Entries
    with empty text are dropped, unknown phase values become None. Returns None
    when the value has another shape or no entry with text remains (a fake or
    absent client value never turns into stored metadata).
    """
    if not isinstance(value, list):
        return None
    cleaned: List[Dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        text = entry.get("text")
        if not isinstance(text, str):
            return None
        if not text:
            continue
        cleaned.append({"phase": normalize_phase(entry.get("phase")), "text": text})
    return cleaned or None


def output_phases_for_text(value: Any, text: Any) -> Optional[List[Dict[str, Any]]]:
    """Return the cleaned list when its join equals ``text`` exactly, else None."""
    phases = coerce_output_phases(value)
    if phases is None or not isinstance(text, str):
        return None
    if join_message_texts([entry["text"] for entry in phases]) != text:
        return None
    return phases


def _strip_edge_entries(
    phases: List[Dict[str, Any]], *, leading: bool, trailing: bool,
) -> List[Dict[str, Any]]:
    """Remove whitespace at the start / end of the joined text, entry by entry.

    Entries that become empty are dropped and the strip continues into the
    neighbouring entry, so the result equals stripping the joined text (the
    separators the join inserts are newlines, which strip removes too).
    """
    out = [dict(entry) for entry in phases]
    if leading:
        while out:
            out[0]["text"] = out[0]["text"].lstrip()
            if out[0]["text"]:
                break
            out.pop(0)
    if trailing:
        while out:
            out[-1]["text"] = out[-1]["text"].rstrip()
            if out[-1]["text"]:
                break
            out.pop()
    return out


def output_phases_for_sent_text(value: Any, text: Any) -> Optional[List[Dict[str, Any]]]:
    """Like :func:`output_phases_for_text`, tolerating a stripped ``text``.

    SAIMemory returns a stored message's content stripped
    (``sai_memory.memory.storage.compose_message_content``), while the list
    was matched to the content as stored. When the join differs from ``text``
    only by surrounding whitespace, the outer edges of the first / last
    entries are stripped the same way and the exact equality is checked
    again. Any other difference returns None.
    """
    phases = output_phases_for_text(value, text)
    if phases is not None:
        return phases
    cleaned = coerce_output_phases(value)
    if cleaned is None or not isinstance(text, str):
        return None
    if join_message_texts([entry["text"] for entry in cleaned]).strip() != text:
        return None
    stripped = _strip_edge_entries(cleaned, leading=True, trailing=True)
    if not stripped or join_message_texts([e["text"] for e in stripped]) != text:
        return None
    return stripped


def _message_ranges(phases: List[Dict[str, Any]]) -> List[Tuple[int, int, int]]:
    """``(entry index, start, end)`` of each entry inside the joined text."""
    ranges: List[Tuple[int, int, int]] = []
    joined_len = 0
    last_char = ""
    for index, entry in enumerate(phases):
        text = entry["text"]
        if joined_len and last_char != "\n":
            joined_len += 1  # the separator newline the join inserts
        ranges.append((index, joined_len, joined_len + len(text)))
        joined_len += len(text)
        last_char = text[-1]
    return ranges


def align_output_phases_to_spell_round(
    raw_text: str,
    phases: Any,
    spell_lines: Sequence[Tuple[int, int, str]],
    rebuilt_text: str,
) -> Optional[List[Dict[str, Any]]]:
    """Split the stored text of a spell round back into its original messages.

    The spell loop does not store a round's raw response. It stores
    ``rebuilt_text`` =
    ``(raw[:first_start].rstrip() + "\\n" + "\\n".join(lines) +
    ("\\n" + raw[last_end:].strip() if that is non-empty)).strip()``
    — the text before the first spell line, every spell line replaced by its
    normalized form, and the text after the last spell line. Text between two
    spell lines is dropped.

    Arguments:
        raw_text: the round's response as received.
        phases: the ``output_phases`` reported for ``raw_text``; the join of its
            texts must equal ``raw_text``.
        spell_lines: ``(start, end, normalized_line)`` of every spell-like line
            of the round, in position order; ``start`` / ``end`` index
            ``raw_text``.
        rebuilt_text: the text that is stored for the round.

    Each output piece is assigned to the message its raw span lies in (the text
    before / after the spell lines is cut at message boundaries when it spans
    several messages). Pieces of one message are joined with a newline, and
    the per-message results form the returned list.

    Returns a new list whose join equals ``rebuilt_text`` exactly, or None when
    the input does not line up (join of ``phases`` differs from ``raw_text``, a
    spell line crosses a message boundary, or the assembled join differs from
    ``rebuilt_text``).
    """
    entries = coerce_output_phases(phases)
    if entries is None or not spell_lines or not isinstance(raw_text, str):
        return None
    if join_message_texts([entry["text"] for entry in entries]) != raw_text:
        return None
    ranges = _message_ranges(entries)

    def _split(start: int, end: int) -> List[Dict[str, Any]]:
        pieces: List[Dict[str, Any]] = []
        for index, msg_start, msg_end in ranges:
            lo, hi = max(start, msg_start), min(end, msg_end)
            if lo < hi:
                pieces.append({"index": index, "text": raw_text[lo:hi]})
        return pieces

    def _owner(start: int, end: int) -> Optional[int]:
        for index, msg_start, msg_end in ranges:
            if msg_start <= start and end <= msg_end:
                return index
        return None

    ordered = sorted(spell_lines, key=lambda line: line[0])
    pieces: List[Dict[str, Any]] = _strip_edge_entries(
        _split(0, ordered[0][0]), leading=False, trailing=True,
    )
    for start, end, normalized in ordered:
        index = _owner(start, end)
        if index is None:
            return None
        pieces.append({"index": index, "text": normalized})
    pieces.extend(_strip_edge_entries(
        _split(ordered[-1][1], len(raw_text)), leading=True, trailing=True,
    ))

    grouped: List[Dict[str, Any]] = []
    for piece in pieces:
        if not piece["text"]:
            continue
        if grouped and grouped[-1]["index"] == piece["index"]:
            grouped[-1]["text"] += "\n" + piece["text"]
        else:
            grouped.append(dict(piece))
    aligned = _strip_edge_entries(
        [{"phase": entries[g["index"]]["phase"], "text": g["text"]} for g in grouped],
        leading=True, trailing=True,
    )
    if not aligned:
        return None
    if join_message_texts([entry["text"] for entry in aligned]) != rebuilt_text:
        return None
    return aligned


__all__ = [
    "OUTPUT_PHASES_METADATA_KEY",
    "VALID_PHASES",
    "align_output_phases_to_spell_round",
    "coerce_output_phases",
    "join_message_texts",
    "normalize_phase",
    "output_phases_for_sent_text",
    "output_phases_for_text",
]
