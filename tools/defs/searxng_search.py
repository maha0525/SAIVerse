from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import requests

from tools.defs import ToolResult, ToolSchema

DEFAULT_BASE_URL = os.getenv("SEARXNG_URL") or os.getenv("SEARXNG_BASE_URL") or "http://localhost:8080"
DEFAULT_SAFESEARCH = os.getenv("SEARXNG_SAFESEARCH", "1")
DEFAULT_LANGUAGE = os.getenv("SEARXNG_LANGUAGE", "ja")
DEFAULT_LIMIT = int(os.getenv("SEARXNG_LIMIT", "5"))


def _build_params(
    query: str,
    max_results: int | None,
    engines: str | None,
    language: str | None,
    safe: int | None,
) -> Tuple[str, Dict[str, Any]]:
    base_url = (os.getenv("SEARXNG_URL") or os.getenv("SEARXNG_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if not base_url:
        raise RuntimeError("SearXNG base URL is not configured. Set SEARXNG_URL or SEARXNG_BASE_URL.")

    limit = max_results if max_results is not None else DEFAULT_LIMIT
    # Keep limits reasonable to avoid flooding the server
    limit = max(1, min(int(limit), 20))

    params: Dict[str, Any] = {
        "q": query,
        "format": "json",
        "language": language or DEFAULT_LANGUAGE,
        "safesearch": safe if safe is not None else DEFAULT_SAFESEARCH,
        "limit": limit,
    }
    if engines:
        params["engines"] = engines

    return f"{base_url}/search", params


def searxng_search(
    query: str,
    max_results: int | None = None,
    engines: str | None = None,
    language: str | None = None,
    safe: int | None = None,
) -> Tuple[str, ToolResult]:
    """Run a web search using a SearXNG instance and format the results.

    Args:
        query: 検索クエリ。
        max_results: 取得する件数の上限（1-20）。
        engines: 使用するエンジンをカンマ区切りで指定（例: "duckduckgo,google"）。
        language: 検索言語（例: "ja", "en"）。
        safe: 0/1/2 のセーフサーチレベル。

    Returns:
        (整形済みメッセージ, 履歴用スニペット)
    """

    try:
        url, params = _build_params(query, max_results, engines, language, safe)
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        return f"SearXNG検索に失敗しました: {exc}", ToolResult(history_snippet=None)
    except ValueError as exc:  # JSON decode
        return f"SearXNGの応答を解釈できませんでした: {exc}", ToolResult(history_snippet=None)

    results: List[Dict[str, Any]] = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return "SearXNGから結果が見つかりませんでした。クエリやエンジンを調整して再試行してください。", ToolResult(history_snippet=None)

    lines: List[str] = []
    snippet_lines: List[str] = []
    for entry in results:
        title = entry.get("title") or entry.get("source") or "(no title)"
        url = entry.get("url") or entry.get("link") or "(no url)"
        content = entry.get("content") or entry.get("snippet") or entry.get("summary") or ""
        content = content.replace("\n", " ").strip()
        lines.append(f"- {title}\n  {url}\n  {content}")
        snippet_lines.append(f"{title} | {url}")

    header = "SearXNG検索結果 (上位{n}件)".format(n=len(lines))
    message = header + "\n" + "\n".join(lines)
    snippet = "\n".join(snippet_lines[:5])  # compact history snippet
    return message, ToolResult(history_snippet=snippet)


def schema() -> ToolSchema:
    return ToolSchema(
        name="searxng_search",
        description="Search the web via SearXNG and return concise results.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索クエリ"},
                "max_results": {
                    "type": "integer",
                    "description": "取得する件数の上限（1-20）",
                    "minimum": 1,
                    "maximum": 20,
                },
                "engines": {
                    "type": "string",
                    "description": "使用するエンジン（カンマ区切り）。未指定ならデフォルト構成",
                },
                "language": {
                    "type": "string",
                    "description": "検索言語（例: ja, en）",
                },
                "safe": {
                    "type": "integer",
                    "description": "セーフサーチレベル (0:無効,1:中,2:強)",
                    "minimum": 0,
                    "maximum": 2,
                },
            },
            "required": ["query"],
        },
        result_type="string",
    )
