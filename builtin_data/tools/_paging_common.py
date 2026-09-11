"""ページ指定の共通部品 (多くの項目を返すスペルの「めくる」を一本化する)。

正典: docs/intent/room_item_display_cap.md 設計 3。

**この部品は切り詰めをしない。** 省略の単位は件数 (ページ) だけで、一件の本文は
途中で切らない — 手帳 (docs/intent/autonomous_behavior_v3.md §13.2.1) から続く
原則で、本 intent の不変条件 4 でもある。ここが持つのは三つだけ:

1. ``page`` 引数の解釈 — ``"2"`` の一枚指定と ``"1-5"`` の範囲指定。一度に
   開ける範囲は :data:`MAX_PAGE_SPAN` ページまで。
2. **既にページ (チャンク) の列に分けたもの**からの切り出し。切れ目の規則
   (件数で切る / 日付の境目で切る) は呼び手が持つ — 部品は「分けた後」だけを
   扱う。
3. 全体の案内行の生成 — 「全 58 件・6 ページ。いま 2 ページ目 (11〜20 件目)」。

ページの中身は呼んだ瞬間の並びで切る。項目が増減すると同じ番号が別の中身を
指すことはある — 固定の索引ではない (intent 設計 3)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

#: 一度に開けるページ数の上限。スペルの返りは会話と一緒に畳まれるまで送る量に
#: 残り続けるので、際限なく開ける形にはしない (intent 設計 3)。
MAX_PAGE_SPAN = 5

# ASCII 数字だけを受ける。``\d`` は全角数字も通すので使わない
# (pocketbook_open._DATE_RE と同じ思想)。
_PAGE_RE = re.compile(r"^([0-9]+)(?:-([0-9]+))?$")

_FORMAT_HINT = "page は '2' のような番号か '1-5' のような範囲で指定してください"


@dataclass(frozen=True)
class PageRange:
    """開くページの範囲 (1 始まり・両端を含む)。"""

    start: int
    end: int

    @property
    def span(self) -> int:
        return self.end - self.start + 1

    @property
    def is_single(self) -> bool:
        return self.start == self.end


def parse_page_arg(value: Any) -> Tuple[Optional[PageRange], Optional[str]]:
    """``page`` 引数を検査する。``(範囲, エラー文)`` を返す。

    省略 (None / 空文字) は 1 ページ目。数字でない・0 以下・逆順・5 ページ超は
    それぞれ分かる文言で断る (範囲は返さない)。
    """
    if value is None:
        return (PageRange(1, 1), None)
    if isinstance(value, bool):
        return (None, f"{_FORMAT_HINT}: {value!r}")
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return (PageRange(1, 1), None)
    else:
        return (None, f"{_FORMAT_HINT}: {value!r}")

    matched = _PAGE_RE.match(raw)
    if not matched:
        return (None, f"{_FORMAT_HINT}: {raw}")

    start = int(matched.group(1))
    end = int(matched.group(2)) if matched.group(2) is not None else start

    if start < 1 or end < 1:
        return (None, f"ページ番号は 1 以上で指定してください: {raw}")
    if end < start:
        return (None, f"ページの範囲は小さい番号から指定してください: {raw}")
    span = end - start + 1
    if span > MAX_PAGE_SPAN:
        return (
            None,
            f"一度に開けるのは {MAX_PAGE_SPAN} ページまでです: "
            f"{raw} は {span} ページ分です",
        )
    return (PageRange(start, end), None)


def chunk_by_count(entries: Sequence[Any], per_page: int) -> List[List[Any]]:
    """件数で等分に切ったページの列を返す (切れ目の規則の一番単純な形)。

    日付の境目で切る手帳のような可変長のページは、呼び手が自分の規則で列を
    作ってから :func:`select_pages` へ渡す。
    """
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    return [
        list(entries[index:index + per_page])
        for index in range(0, len(entries), per_page)
    ]


def select_pages(
    pages: Sequence[Sequence[Any]], page_range: PageRange,
) -> List[List[Any]]:
    """ページの列から指定範囲を切り出す (範囲外は空の列)。

    末尾をはみ出す範囲は在る分だけ返す — 「1-5」と言われて 3 ページしか無ければ
    3 ページ分。先頭が範囲外なら空で、そのときの文言は
    :func:`format_missing_page` が持つ。
    """
    if page_range.start > len(pages):
        return []
    return [list(page) for page in pages[page_range.start - 1:page_range.end]]


def format_page_guide(
    pages: Sequence[Sequence[Any]],
    page_range: PageRange,
    *,
    unit: str = "件",
    arg_name: str = "page",
) -> str:
    """全体の案内行を組む。

    出力例 (一枚)::

        全 58 件・6 ページ。いま 2 ページ目 (11〜20 件目)。page='N' で他のページを開けます。

    出力例 (範囲)::

        全 58 件・6 ページ。いま 1〜3 ページ目 (1〜30 件目)。page='N' で他のページを開けます。

    全体が 1 ページに収まるときは「他のページを開けます」の一文を付けない —
    他のページは無いのだから、案内すると嘘になる。
    """
    total_pages = len(pages)
    total_entries = sum(len(page) for page in pages)
    if total_pages == 0:
        return ""

    last_page = min(page_range.end, total_pages)
    if page_range.start > total_pages:
        return f"全 {total_entries} {unit}・{total_pages} ページ。"

    first_index = sum(len(page) for page in pages[:page_range.start - 1]) + 1
    shown = sum(len(page) for page in pages[page_range.start - 1:last_page])
    last_index = first_index + shown - 1

    if page_range.start == last_page:
        where = f"いま {page_range.start} ページ目"
    else:
        where = f"いま {page_range.start}〜{last_page} ページ目"

    if first_index == last_index:
        span = f"({first_index} {unit}目)"
    else:
        span = f"({first_index}〜{last_index} {unit}目)"

    guide = f"全 {total_entries} {unit}・{total_pages} ページ。{where} {span}。"
    if total_pages > 1:
        guide += f"{arg_name}='N' で他のページを開けます。"
    return guide


def format_missing_page(
    pages: Sequence[Sequence[Any]], page_range: PageRange,
) -> str:
    """要求されたページが存在しないときの文言。"""
    total_pages = len(pages)
    return f"全 {total_pages} ページです。{page_range.start} ページ目はありません。"
