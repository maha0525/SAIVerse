"""URL content reader tool for SAIVerse.

Fetches web page content and converts it to Markdown format.
Designed to work with SearXNG search results for deeper exploration.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Tuple
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from tools.core import ToolResult, ToolSchema

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_MAX_CHARS = int(os.getenv("READ_URL_MAX_CHARS", "8000"))
DEFAULT_TIMEOUT = int(os.getenv("READ_URL_TIMEOUT", "15"))

# User-Agent to avoid being blocked by some sites
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _url_for_log(url: str) -> str:
    """Keep destination context without userinfo, query, or fragment secrets."""
    if not isinstance(url, str) or not url.strip():
        return "<empty URL>"
    normalized = url.strip()
    if not normalized.startswith(("http://", "https://")):
        normalized = "https://" + normalized
    try:
        parsed = urlsplit(normalized)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    except ValueError:
        # Parsing failure must never put the unredacted input into a log.
        return "<invalid URL>"


def _clean_html(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove script, style, nav, footer and other non-content elements."""
    # Remove unwanted tags
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", 
                     "noscript", "iframe", "form", "button", "input"]):
        tag.decompose()
    
    # Remove hidden elements
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        tag.decompose()
    
    return soup


def _html_to_markdown(html: str) -> str:
    """Convert HTML content to clean Markdown."""
    soup = BeautifulSoup(html, "html.parser")
    soup = _clean_html(soup)
    
    # Try to find main content area
    main_content = (
        soup.find("main") or
        soup.find("article") or
        soup.find(attrs={"role": "main"}) or
        soup.find(class_=re.compile(r"(content|main|article)", re.I)) or
        soup.find("body") or
        soup
    )
    
    # Convert to markdown
    markdown = md(str(main_content), heading_style="ATX", bullets="-")
    
    # Clean up excessive whitespace
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = markdown.strip()
    
    return markdown


def read_url_content(
    url: str,
    max_chars: int | None = None,
) -> Tuple[str, ToolResult]:
    """Fetch a web page and return its content as Markdown.

    Args:
        url: 取得するページのURL。
        max_chars: 返すコンテンツの最大文字数（デフォルト: 8000）。

    Returns:
        (整形済みメッセージ, 履歴用スニペット)
    """
    logger.info("read_url_content called with url=%s, max_chars=%s", _url_for_log(url), max_chars)

    # Normalize empty strings from SEA runtime to None
    if max_chars == "" or max_chars is None:
        max_chars = DEFAULT_MAX_CHARS
    else:
        max_chars = int(max_chars)
    
    # Ensure reasonable limits
    max_chars = max(500, min(max_chars, 50000))

    # Validate URL
    if not url or not url.strip():
        return "URLが指定されていません。", ToolResult(history_snippet=None)
    
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        
        # Check content type
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type.lower() and "text/plain" not in content_type.lower():
            return f"HTMLではないコンテンツタイプです: {content_type}", ToolResult(history_snippet=None)
        
        # Detect encoding
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
        
    except requests.exceptions.Timeout:
        return f"タイムアウト: ページの取得に{DEFAULT_TIMEOUT}秒以上かかりました。", ToolResult(history_snippet=None)
    except requests.exceptions.TooManyRedirects:
        return "リダイレクトが多すぎます。", ToolResult(history_snippet=None)
    except requests.exceptions.RequestException as exc:
        return f"ページの取得に失敗しました: {exc}", ToolResult(history_snippet=None)

    try:
        markdown = _html_to_markdown(html)
    except Exception as exc:
        logger.exception("Failed to convert HTML to markdown")
        return f"HTMLの変換に失敗しました: {exc}", ToolResult(history_snippet=None)

    if not markdown:
        return "ページからコンテンツを抽出できませんでした。", ToolResult(history_snippet=None)

    # Truncate if needed
    truncated = False
    if len(markdown) > max_chars:
        markdown = markdown[:max_chars]
        # Try to cut at a natural break point
        last_break = max(
            markdown.rfind("\n\n"),
            markdown.rfind("。"),
            markdown.rfind(". "),
        )
        if last_break > max_chars * 0.7:
            markdown = markdown[:last_break]
        markdown += "\n\n...(以下省略)..."
        truncated = True

    # Build response message
    char_info = f"（約{len(markdown)}文字" + ("、省略あり" if truncated else "") + "）"
    message = f"## {url} の内容 {char_info}\n\n{markdown}"
    
    # Compact snippet for history
    snippet = f"URL読み込み: {url} ({len(markdown)}文字)"
    
    logger.info("read_url_content completed: %d chars from %s", len(markdown), _url_for_log(url))
    return message, ToolResult(history_snippet=snippet)


def schema() -> ToolSchema:
    return ToolSchema(
        name="read_url_content",
        description="Fetch a web page URL and return its content as readable Markdown text.",
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "取得するページのURL",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "返すコンテンツの最大文字数（デフォルト: 8000）",
                    "minimum": 500,
                    "maximum": 50000,
                },
            },
            "required": ["url"],
        },
        result_type="string",
    )
