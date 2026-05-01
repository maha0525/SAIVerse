"""URL fetching helpers shared by web-fetch related tools.

read_url_outline / read_url_section から共通利用する低レベル処理を集約する。

- fetch_page: HTTP取得 + HTMLパース + clean + main検出 + Markdown化
- extract_outline: clean済み main soup から h1-h4 をフラットに抽出
- extract_section_by_heading: 見出しマッチで節を切り出し
- extract_section_by_keyword: 全文キーワード一致箇所周辺を切り出し
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from markdownify import markdownify as md

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = int(os.getenv("READ_URL_TIMEOUT", "15"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HEADING_TAGS: Tuple[str, ...] = ("h1", "h2", "h3", "h4")


class FetchError(Exception):
    """URL取得や変換中のエラー。message は LLM に提示してよい形式。"""


@dataclass
class FetchedPage:
    url: str
    title: str
    main_soup: Tag  # cleaned, scoped to main content area
    markdown: str  # Markdownified main content


def fetch_page(url: str, timeout: Optional[int] = None) -> FetchedPage:
    """URLを取得し、cleanup + main抽出 + Markdown化までを行って返す。

    各段でログを出す（ファイルログは無制限ポリシー）。
    失敗時は FetchError を投げる。
    """
    if not url or not url.strip():
        raise FetchError("URLが指定されていません。")
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    timeout_s = timeout if timeout is not None else DEFAULT_TIMEOUT
    LOGGER.info("fetch_page start url=%s timeout=%ds", url, timeout_s)

    response: Optional[requests.Response] = None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout_s,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        LOGGER.warning("fetch_page timeout url=%s", url)
        raise FetchError(
            f"タイムアウト: ページの取得に{timeout_s}秒以上かかりました。"
        ) from exc
    except requests.exceptions.TooManyRedirects as exc:
        LOGGER.warning("fetch_page too_many_redirects url=%s", url)
        raise FetchError("リダイレクトが多すぎます。") from exc
    except requests.exceptions.RequestException as exc:
        LOGGER.warning("fetch_page request_failed url=%s err=%s", url, exc)
        raise FetchError(f"ページの取得に失敗しました: {exc}") from exc

    content_type = response.headers.get("Content-Type", "")
    if (
        "text/html" not in content_type.lower()
        and "text/plain" not in content_type.lower()
    ):
        LOGGER.warning(
            "fetch_page unsupported_content_type url=%s ct=%s", url, content_type
        )
        raise FetchError(f"HTMLではないコンテンツタイプです: {content_type}")

    response.encoding = response.apparent_encoding or "utf-8"
    html = response.text
    LOGGER.info("fetch_page got_html url=%s bytes=%d", url, len(html))

    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    soup = _clean_html(soup)
    main = _find_main_content(soup)

    try:
        markdown = _html_to_markdown(main)
    except Exception as exc:
        LOGGER.exception("fetch_page markdown_conversion_failed url=%s", url)
        raise FetchError(f"HTMLの変換に失敗しました: {exc}") from exc

    if not markdown:
        LOGGER.warning("fetch_page empty_markdown url=%s", url)
        raise FetchError("ページからコンテンツを抽出できませんでした。")

    LOGGER.info(
        "fetch_page done url=%s title=%r markdown_chars=%d",
        url, title, len(markdown),
    )
    return FetchedPage(url=url, title=title, main_soup=main, markdown=markdown)


# ---------------------------------------------------------------------------
# HTML preparation
# ---------------------------------------------------------------------------

def _clean_html(soup: BeautifulSoup) -> BeautifulSoup:
    """script / style / nav / footer 等の非本文要素を除去。"""
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "noscript", "iframe", "form", "button", "input"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        tag.decompose()
    return soup


def _find_main_content(soup: BeautifulSoup) -> Tag:
    """main / article / role=main / .content等を優先して返す。"""
    return (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.find(class_=re.compile(r"(content|main|article)", re.I))
        or soup.find("body")
        or soup
    )


def _html_to_markdown(soup: Tag) -> str:
    markdown = md(str(soup), heading_style="ATX", bullets="-")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    return markdown.strip()


# ---------------------------------------------------------------------------
# Outline / Section extraction
# ---------------------------------------------------------------------------

@dataclass
class OutlineEntry:
    level: int  # 1, 2, 3, 4
    text: str


def extract_outline(main_soup: Tag) -> List[OutlineEntry]:
    """main領域から h1〜h4 をフラットに抽出（文書順）。"""
    entries: List[OutlineEntry] = []
    if not isinstance(main_soup, Tag):
        return entries
    for h in main_soup.find_all(HEADING_TAGS):
        level = int(h.name[1])
        text = h.get_text(strip=True)
        if text:
            entries.append(OutlineEntry(level=level, text=text))
    return entries


def extract_section_by_heading(
    main_soup: Tag,
    query: str,
    max_chars: int,
) -> Optional[Tuple[str, str]]:
    """見出しが query (大文字小文字無視の部分一致) にマッチする節を切り出す。

    matched 見出しの兄弟要素（next_siblings）を、次の同レベル以上の見出しに
    出会うまで集める。matched が深くネストしている場合 next_siblings で
    取れない要素は欠ける（その分 fallback の keyword 一致でカバーする）。

    Returns:
        (matched_heading_text, section_markdown) or None。
        section_markdown は max_chars でtruncate される（自然な区切りで）。
    """
    if not isinstance(main_soup, Tag):
        return None
    if not query:
        return None
    q = query.lower()
    matched: Optional[Tag] = None
    for h in main_soup.find_all(HEADING_TAGS):
        text = h.get_text(strip=True)
        if q in text.lower():
            matched = h
            LOGGER.info(
                "extract_section heading_matched query=%r heading=%r",
                query, text,
            )
            break
    if not matched:
        LOGGER.info("extract_section no_heading_match query=%r", query)
        return None

    matched_level = int(matched.name[1])
    section_html_parts: List[str] = [str(matched)]
    for sibling in matched.next_siblings:
        if isinstance(sibling, Tag) and sibling.name in HEADING_TAGS:
            sib_level = int(sibling.name[1])
            if sib_level <= matched_level:
                break
        section_html_parts.append(str(sibling))

    section_soup = BeautifulSoup("".join(section_html_parts), "html.parser")
    section_md = _html_to_markdown(section_soup)

    if len(section_md) > max_chars:
        section_md = _truncate_natural(section_md, max_chars)

    return matched.get_text(strip=True), section_md


def extract_section_by_keyword(
    markdown: str, query: str, around: int
) -> Optional[str]:
    """Markdown全文中の query 一致箇所周辺 around 文字を返す。

    見出しマッチが取れなかったときの fallback。一致しなければ None。
    """
    if not query or not markdown:
        return None
    q = query.lower()
    lower = markdown.lower()
    idx = lower.find(q)
    if idx == -1:
        LOGGER.info("extract_section_by_keyword no_match query=%r", query)
        return None
    start = max(0, idx - around)
    end = min(len(markdown), idx + around)
    snippet = markdown[start:end]
    if start > 0:
        snippet = "...(前略)...\n\n" + snippet
    if end < len(markdown):
        snippet = snippet + "\n\n...(後略)..."
    LOGGER.info(
        "extract_section_by_keyword matched query=%r idx=%d snippet_chars=%d",
        query, idx, len(snippet),
    )
    return snippet


def _truncate_natural(text: str, max_chars: int) -> str:
    """自然な区切りでtruncateして「...(以下省略)...」を付ける。"""
    truncated = text[:max_chars]
    last_break = max(
        truncated.rfind("\n\n"),
        truncated.rfind("。"),
        truncated.rfind(". "),
    )
    if last_break > max_chars * 0.7:
        truncated = truncated[:last_break]
    return truncated + "\n\n...(以下省略)..."
