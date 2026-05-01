"""read_url_section: URLのページから、見出しまたはキーワードで指定した節だけを返すスペル。

read_url_outline で長文と判定されたページの深掘り用。
1. まず h1〜h4 の見出しから section_query にマッチするものを探し、その節を返す
2. 見出しマッチが無ければ全文中で section_query 一致箇所周辺 around 文字を返す
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from tools.core import ToolResult, ToolSchema
from tools.web_fetch_helpers import (
    FetchError,
    extract_section_by_heading,
    extract_section_by_keyword,
    fetch_page,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_AROUND = int(os.getenv("READ_URL_SECTION_AROUND", "3000"))
SECTION_HEADING_MAX = int(os.getenv("READ_URL_SECTION_HEADING_MAX", "10000"))


def read_url_section(
    url: str,
    section_query: str,
    around: Optional[int] = None,
) -> Tuple[str, ToolResult]:
    """URLのページから見出し or キーワードに一致する節を抽出して返す。

    Args:
        url: 取得するページのURL。
        section_query: 見出し名 or キーワード（大文字小文字無視・部分一致）。
        around: キーワード一致 fallback 時の周辺文字数（前後合計 around*2）。
            省略時は環境変数 READ_URL_SECTION_AROUND（未設定時 3000）。

    Returns:
        (整形済みメッセージ, 履歴用スニペット)
    """
    LOGGER.info(
        "read_url_section called url=%s query=%r around=%s",
        url, section_query, around,
    )

    if around == "" or around is None:
        around_chars = DEFAULT_AROUND
    else:
        around_chars = int(around)
    around_chars = max(200, around_chars)

    if not section_query or not section_query.strip():
        return (
            "section_query が指定されていません。見出し名やキーワードを指定してください。",
            ToolResult(history_snippet=None),
        )
    section_query = section_query.strip()

    try:
        page = fetch_page(url)
    except FetchError as exc:
        LOGGER.warning("read_url_section fetch_failed url=%s err=%s", url, exc)
        return str(exc), ToolResult(history_snippet=None)

    # 1. 見出しマッチを試す
    heading_result = extract_section_by_heading(
        page.main_soup,
        section_query,
        max_chars=SECTION_HEADING_MAX,
    )
    if heading_result is not None:
        heading_text, section_md = heading_result
        message = (
            f"## {page.url} の節「{heading_text}」"
            f"（{len(section_md)} 文字）\n\n{section_md}"
        )
        snippet = (
            f"URL節読み込み: {page.url} -> 「{heading_text}」"
            f"({len(section_md)}文字)"
        )
        LOGGER.info(
            "read_url_section heading_match url=%s heading=%r chars=%d",
            page.url, heading_text, len(section_md),
        )
        return message, ToolResult(history_snippet=snippet)

    # 2. キーワード一致 fallback
    keyword_snippet = extract_section_by_keyword(
        page.markdown, section_query, around_chars,
    )
    if keyword_snippet is None:
        message = (
            f"「{section_query}」に該当する見出しもキーワードもページ内に見つかりませんでした。"
            f" URL: {page.url}"
        )
        LOGGER.info(
            "read_url_section no_match url=%s query=%r",
            page.url, section_query,
        )
        return message, ToolResult(history_snippet=None)

    message = (
        f"## {page.url} 内「{section_query}」周辺"
        f"（{len(keyword_snippet)} 文字）\n\n{keyword_snippet}"
    )
    snippet = (
        f"URLキーワード周辺: {page.url} <- 「{section_query}」"
        f"({len(keyword_snippet)}文字)"
    )
    LOGGER.info(
        "read_url_section keyword_match url=%s query=%r chars=%d",
        page.url, section_query, len(keyword_snippet),
    )
    return message, ToolResult(history_snippet=snippet)


def schema() -> ToolSchema:
    return ToolSchema(
        name="read_url_section",
        description=(
            "URLのページ内から、見出し名やキーワードで指定した節だけを抽出して読み込みます。"
            " read_url_outline で長文と判定されたページの深掘り用です。"
            " まず見出し（h1〜h4）の部分一致を試み、見つからなければ本文キーワード一致箇所"
            " 周辺を返します。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "取得するページのURL",
                },
                "section_query": {
                    "type": "string",
                    "description": (
                        "見出し名やキーワード（大文字小文字無視の部分一致）。"
                        " read_url_outline で見えた見出し名を渡すのが最も確実です。"
                    ),
                },
                "around": {
                    "type": "integer",
                    "description": (
                        "キーワード一致 fallback 時の周辺文字数（前後合計 around*2）。"
                        "省略時は環境変数 READ_URL_SECTION_AROUND（未設定時 3000）。"
                    ),
                    "minimum": 200,
                },
            },
            "required": ["url", "section_query"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ページ節読み込み",
    )
