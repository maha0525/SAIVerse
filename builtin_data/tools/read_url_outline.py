"""read_url_outline: URLからページ概要を返すスペル。

短いページ（READ_URL_FULL_THRESHOLD 文字以下）なら全文を返し、
長いページなら h1〜h4 のアウトラインだけを返す。長文の場合は
ペルソナが続けて read_url_section スペルを使うことで、必要な節
だけを記憶（conversation タグ）に取り込める。
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from tools.core import ToolResult, ToolSchema
from tools.web_fetch_helpers import (
    FetchedPage,
    FetchError,
    extract_outline,
    fetch_page,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_FULL_THRESHOLD = int(os.getenv("READ_URL_FULL_THRESHOLD", "5000"))


def _page_slice_message(
    page: FetchedPage, start: int, chunk: int, note: str = ""
) -> Tuple[str, ToolResult]:
    """ページの文章の start 文字目から chunk 文字ぶんと、続きの読み方を返す。"""
    total_chars = len(page.markdown)
    if start >= total_chars:
        message = (
            f"offset {start} は、このページの文字数（全 {total_chars} 文字）を超えています。"
            f" URL: {page.url}"
        )
        return message, ToolResult(history_snippet=None)
    end = min(total_chars, start + chunk)
    parts = [f"## {page.url} の {start + 1}〜{end} 文字目（全 {total_chars} 文字）"]
    if page.title:
        parts.append(f"\n**タイトル**: {page.title}")
    if note:
        parts.append(f"\n{note}")
    parts.append("")
    parts.append(page.markdown[start:end])
    parts.append("")
    if end < total_chars:
        parts.append(
            f"（続きがあります。read_url_outline に offset=\"{end}\" を付けて呼ぶと、"
            f"{end + 1} 文字目から読めます。）"
        )
    else:
        parts.append("（ここでページの終わりです。）")
    snippet = f"URL部分取得: {page.url} ({start + 1}-{end}/{total_chars}文字)"
    LOGGER.info(
        "read_url_outline returning_slice url=%s start=%d end=%d total=%d",
        page.url, start, end, total_chars,
    )
    return "\n".join(parts), ToolResult(history_snippet=snippet)


def read_url_outline(
    url: str,
    full_threshold: Optional[int] = None,
    offset: Optional[str] = None,
) -> Tuple[str, ToolResult]:
    """URLからページを取得し、短ければ全文、長ければアウトラインを返す。

    見出しの無い長いページは、先頭の閾値ぶんだけを返し、続きは offset で読ませる
    （全文を返すと、長さに上限なくペルソナの文脈へ入ってしまうため）。

    Args:
        url: 取得するページのURL。
        full_threshold: 全文返却の閾値（文字数）。これ以下なら全文、超えたらアウトライン。
            省略時は環境変数 READ_URL_FULL_THRESHOLD（未設定時 5000）。
            offset で読むときの一回ぶんの文字数も兼ねる。
        offset: 何文字目から読むか（数字の文字列）。指定するとアウトラインではなく、
            その位置から閾値ぶんの本文を返す。文字列で受けるのは、スペルの引数の型が
            構造化出力の型になる経路で数値欄を作らないため
            （docs/issues/sluice_structured_output_digit_loop.md）。

    Returns:
        (整形済みメッセージ, 履歴用スニペット)
    """
    LOGGER.info(
        "read_url_outline called url=%s full_threshold=%s offset=%r",
        url, full_threshold, offset,
    )

    if full_threshold == "" or full_threshold is None:
        threshold = DEFAULT_FULL_THRESHOLD
    else:
        threshold = int(full_threshold)
    threshold = max(0, threshold)
    chunk = threshold or DEFAULT_FULL_THRESHOLD or 5000

    # 省略 (None / "") と明示の "0" を区別する。明示されたら 0 でも本文の部分取得に
    # 進む — 見出しのある長いページで、最初の見出しより前の導入文を読む唯一の道だから。
    start: Optional[int] = None
    if offset is not None and str(offset).strip() != "":
        try:
            start = int(str(offset).strip())
        except ValueError:
            start = -1
        if start < 0:
            return (
                f"offset には何文字目から読むかを 0 以上の数字で指定してください"
                f"（受け取った値: {offset!r}）。",
                ToolResult(history_snippet=None),
            )

    try:
        page = fetch_page(url)
    except FetchError as exc:
        LOGGER.warning("read_url_outline fetch_failed url=%s err=%s", url, exc)
        return str(exc), ToolResult(history_snippet=None)

    total_chars = len(page.markdown)

    if start is not None:
        return _page_slice_message(page, start, chunk)

    if total_chars <= threshold:
        parts = [f"## {page.url} の内容（全 {total_chars} 文字）"]
        if page.title:
            parts.append(f"\n**タイトル**: {page.title}")
        parts.append("")
        parts.append(page.markdown)
        message = "\n".join(parts)
        snippet = f"URL全文取得: {page.url} ({total_chars}文字)"
        LOGGER.info(
            "read_url_outline returning_full url=%s chars=%d",
            page.url, total_chars,
        )
        return message, ToolResult(history_snippet=snippet)

    outline = extract_outline(page.body_soup)
    if not outline:
        # 見出しが無いのでアウトラインを作れない。全文を返すと長さに上限が
        # 無くなるので、先頭の一回ぶんだけ返して続きは offset で読ませる。
        LOGGER.warning(
            "read_url_outline no_headings_returning_head url=%s chars=%d",
            page.url, total_chars,
        )
        return _page_slice_message(
            page, 0, chunk,
            note="このページには見出しが無いため、先頭から順に区切って返しています。",
        )

    lines = [f"## {page.url} のアウトライン（全 {total_chars} 文字）"]
    if page.title:
        lines.append(f"\n**タイトル**: {page.title}")
    lines.append("\n### 見出し階層")
    min_level = min(e.level for e in outline)
    for entry in outline:
        indent = "  " * (entry.level - min_level)
        lines.append(f"{indent}- {entry.text}")
    lines.append("")
    lines.append(
        "本文を読み込むには `read_url_section(url, section_query)` を使ってください。"
        " section_query には上記見出し名や、該当節に含まれそうなキーワードを指定します。"
    )
    message = "\n".join(lines)
    snippet = (
        f"URLアウトライン: {page.url} ({total_chars}文字, 見出し{len(outline)}個)"
    )
    LOGGER.info(
        "read_url_outline returning_outline url=%s chars=%d headings=%d",
        page.url, total_chars, len(outline),
    )
    return message, ToolResult(history_snippet=snippet)


def schema() -> ToolSchema:
    return ToolSchema(
        name="read_url_outline",
        description=(
            "指定したURLのページ内容を読み込み、短いページなら全文、長いページなら見出し階層"
            "（h1〜h4）を返します。長いページは続けて read_url_section で必要な節を深掘り"
            "してください。見出しの無い長いページは先頭だけを返すので、続きは offset で"
            "読んでください。閾値はデフォルト 5000 文字、環境変数 READ_URL_FULL_THRESHOLD で"
            "上書き可能。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "取得するページのURL",
                },
                "full_threshold": {
                    "type": "integer",
                    "description": (
                        "全文返却の閾値（文字数）。これ以下なら全文、超えたらアウトラインを"
                        "返します。省略時は環境変数 READ_URL_FULL_THRESHOLD（未設定時 5000）。"
                    ),
                    "minimum": 0,
                },
                "offset": {
                    "type": "string",
                    "description": (
                        "何文字目から読むか（数字、例: \"5000\"）。指定すると見出し階層では"
                        "なく、その位置から閾値ぶんの本文を返します。前回の結果に"
                        "「offset=\"…\" を付けて呼ぶと続きが読めます」とあるときに、その値を"
                        "そのまま渡してください。通常は省略します。"
                    ),
                },
            },
            "required": ["url"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ページ概要",
    )
