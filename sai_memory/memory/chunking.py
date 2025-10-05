from __future__ import annotations

from typing import Iterable, List


def chunk_text(text: str, *, min_chars: int, max_chars: int) -> List[str]:
    """Split `text` into natural chunks bounded by min/max thresholds.

    The algorithm follows these steps:
    1. Use sentence endings (Japanese full stop "。") and newlines as preferred split points.
    2. Forcefully break any provisional chunk that still exceeds `max_chars` by splitting it in half repeatedly.
    3. Merge provisional chunks that are shorter than `min_chars` with their successor when possible, or
       with the previous chunk as a fallback.
    """
    if max_chars <= 0:
        return [text]

    if not text:
        return [text]

    # Step 1: provisional segmentation by natural boundaries.
    boundaries = {"。", "\n"}
    provisional: List[str] = []
    current: List[str] = []
    for ch in text:
        current.append(ch)
        if ch in boundaries:
            provisional.append("".join(current))
            current = []
    if current:
        provisional.append("".join(current))

    if not provisional:
        provisional = [text]

    # Step 2: enforce max chunk size by splitting large segments in half repeatedly.
    def _split_to_max(segment: str) -> List[str]:
        pieces: List[str] = [segment]
        result: List[str] = []
        while pieces:
            part = pieces.pop(0)
            if len(part) > max_chars:
                mid = len(part) // 2
                pieces.insert(0, part[mid:])
                pieces.insert(0, part[:mid])
            else:
                result.append(part)
        return result

    normalized: List[str] = []
    for seg in provisional:
        normalized.extend(_split_to_max(seg))

    if min_chars <= 0 or len(normalized) <= 1:
        return normalized

    # Step 3: merge undersized chunks with neighbours until thresholds satisfied or no changes.
    def _merge_small(segments: List[str]) -> tuple[List[str], bool]:
        changed = False
        merged: List[str] = []
        i = 0
        total = len(segments)
        while i < total:
            segment = segments[i]
            if len(segment) >= min_chars or total == 1:
                merged.append(segment)
                i += 1
                continue

            if i + 1 < total:
                segments[i + 1] = segment + segments[i + 1]
                changed = True
            elif merged:
                merged[-1] = merged[-1] + segment
                changed = True
            else:
                merged.append(segment)
            i += 1
        if not changed:
            return segments, False
        return merged, True

    chunks = normalized
    while True:
        chunks, modified = _merge_small(chunks)
        if not modified or len(chunks) <= 1:
            break
        if all(len(c) >= min_chars for c in chunks):
            break

    return chunks


def chunk_texts(texts: Iterable[str], *, min_chars: int, max_chars: int) -> List[List[str]]:
    return [chunk_text(text, min_chars=min_chars, max_chars=max_chars) for text in texts]
