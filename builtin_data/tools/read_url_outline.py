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
    FetchError,
    extract_outline,
    fetch_page,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_FULL_THRESHOLD = int(os.getenv("READ_URL_FULL_THRESHOLD", "5000"))


def read_url_outline(
    url: str,
    full_threshold: Optional[int] = None,
) -> Tuple[str, ToolResult]:
    """URLからページを取得し、短ければ全文、長ければアウトラインを返す。

    Args:
        url: 取得するページのURL。
        full_threshold: 全文返却の閾値（文字数）。これ以下なら全文、超えたらアウトライン。
            省略時は環境変数 READ_URL_FULL_THRESHOLD（未設定時 5000）。

    Returns:
        (整形済みメッセージ, 履歴用スニペット)
    """
    LOGGER.info(
        "read_url_outline called url=%s full_threshold=%s", url, full_threshold
    )

    if full_threshold == "" or full_threshold is None:
        threshold = DEFAULT_FULL_THRESHOLD
    else:
        threshold = int(full_threshold)
    threshold = max(0, threshold)

    try:
        page = fetch_page(url)
    except FetchError as exc:
        LOGGER.warning("read_url_outline fetch_failed url=%s err=%s", url, exc)
        return str(exc), ToolResult(history_snippet=None)

    total_chars = len(page.markdown)

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

    outline = extract_outline(page.main_soup)
    if not outline:
        # 見出しが取れない → 仕方ないので全文返す
        LOGGER.warning(
            "read_url_outline no_headings_falling_back_to_full url=%s chars=%d",
            page.url, total_chars,
        )
        parts = [f"## {page.url} の内容（全 {total_chars} 文字、見出し無し）"]
        if page.title:
            parts.append(f"\n**タイトル**: {page.title}")
        parts.append("")
        parts.append(page.markdown)
        message = "\n".join(parts)
        snippet = f"URL全文取得（見出し無し）: {page.url} ({total_chars}文字)"
        return message, ToolResult(history_snippet=snippet)

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
            "してください。閾値はデフォルト 5000 文字、環境変数 READ_URL_FULL_THRESHOLD で"
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
            },
            "required": ["url"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ページ概要",
    )
