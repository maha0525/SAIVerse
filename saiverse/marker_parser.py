"""層1マーカー (``==語句==``) の抽出・剥離 — life_concept_map.md §9.1 層1 / §13-2。

ペルソナの生成テキストに埋め込まれるインラインマーカーを検出し、
(a) マーカー記法を剥がした綺麗な本文と (b) 観測点スパンの列に分解する
**純関数・決定論** レイヤー。永続化 (sai_memory/photos.py の add_photo、
点写真として保存) や保存経路への配線は呼び出し側 (sea/runtime.py の
_store_memory 等) の責務。

記法 (§13-2):

- ``==語句==``       … 型なしの観測点 (意味の予約)。purpose_ref なし
- ``==語句==(ref)``  … 参照接尾つき。ref は saiverse/references.py の統一文法
  (``task:2`` / ``saiverse://self/track/3`` 等) で **書式として** 解釈できる
  文字列をそのまま purpose_ref に保存する。実在検証はしない (§9.1: タグは
  粗製濫造でよい)。文法として解釈できない ``(...)`` はマーカー記法の一部と
  みなさず地の文として残し、語句マークだけ (purpose_ref=None) を打つ —
  ``==強調==(ただの補足)`` のような普通の括弧書きを食い潰さないため

無視する (テキストにそのまま残し、マークも打たない) もの:

- コードブロック (```...```) とインラインコード (`...`) の内側
- ``==`` が偶数対で閉じないもの (``==開きっぱなし``)
- 空文字マーク (``====``)・空白のみのマーク (``== ==``)
- 語句の先頭・末尾が空白のもの (``== y ==``)。Markdown highlight (Obsidian) の
  強調規則と同じ制約で、``x == y == z`` のような地の文の等号を誤検知しないため

マーカーが 1 つも無いテキストでは ``"==" not in text`` の走査 1 回で即
リターンする (保存経路のホットパスに置かれる前提の no-op 保証)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from saiverse import references

__all__ = ["MarkSpan", "extract_marks", "strip_marks"]


@dataclass(frozen=True)
class MarkSpan:
    """テキストから抽出された観測点 1 つ。

    ``quote`` はマークされた語句の逐語 (剥離後の clean_text にそのまま含まれる
    ので、(message_id, quote) の引用アンカー §9.1 として機能する)。
    """

    quote: str
    purpose_ref: Optional[str]  # 統一文法で解釈できた ref。素の予約は None


# 語句: 改行と "==" を含まない 1 文字以上 (最短一致)。"====" は語句が
# 空になるので構造的にマッチしない。参照接尾は直後に密着した "(...)"。
_MARK_RE = re.compile(
    r"==((?:(?!==)[^\n])+?)==(\(([^()\n]+)\))?"
)

# フェンスコードブロック。閉じフェンスが無い場合は Markdown の慣例通り
# テキスト末尾まで保護する。
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)

# インラインコード (フェンスの外側でのみ探索する)。
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    """マーカー探索の対象外にする (start, end) 区間の列 (昇順)。"""
    spans: List[Tuple[int, int]] = []
    fence_end = 0
    for m in _FENCE_RE.finditer(text):
        # フェンス間の隙間でインラインコードを拾う
        for im in _INLINE_CODE_RE.finditer(text, fence_end, m.start()):
            spans.append((im.start(), im.end()))
        spans.append((m.start(), m.end()))
        fence_end = m.end()
    for im in _INLINE_CODE_RE.finditer(text, fence_end):
        spans.append((im.start(), im.end()))
    return spans


def _overlaps(start: int, end: int, spans: List[Tuple[int, int]]) -> bool:
    return any(start < s_end and end > s_start for s_start, s_end in spans)


def extract_marks(text: str) -> Tuple[str, List[MarkSpan]]:
    """マーカーを剥がした本文と観測点スパンの列を返す。

    マーカーが無ければ ``(text, [])`` を入力そのまま (同一オブジェクト) で
    返す。剥離は ``==語句==`` → ``語句`` の置換のみで、地の文は一切変えない。
    """
    if not text or "==" not in text:
        return text, []

    protected = _protected_spans(text)
    parts: List[str] = []
    spans: List[MarkSpan] = []
    pos = 0
    for m in _MARK_RE.finditer(text):
        if m.start() < pos:
            continue
        if _overlaps(m.start(), m.end(), protected):
            continue
        quote = m.group(1)
        if not quote or quote != quote.strip():
            # 空白のみ・前後に空白があるものは無視 (そのまま残す)。
            # "x == y == z" のような地の文の等号を食わないための制約。
            continue
        purpose_ref: Optional[str] = None
        consumed_end = m.end(1) + 2  # 閉じ "==" の直後
        ref_raw = m.group(3)
        if ref_raw is not None:
            try:
                references.parse_ref(ref_raw)
            except ValueError:
                pass  # 文法外 → "(...)" は地の文として残す
            else:
                purpose_ref = ref_raw
                consumed_end = m.end()  # "(ref)" ごと消費
        parts.append(text[pos:m.start()])
        parts.append(quote)
        spans.append(MarkSpan(quote=quote, purpose_ref=purpose_ref))
        pos = consumed_end

    if not spans:
        return text, []
    parts.append(text[pos:])
    return "".join(parts), spans


def strip_marks(text: str) -> str:
    """マーカー記法だけを剥がした本文を返す (観測点は捨てる)。

    建物履歴・TTS 等、mark 保存の責務を持たない表示系シンク用の便宜関数。
    マーカーが無ければ入力をそのまま返す。
    """
    return extract_marks(text)[0]
