from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Dict, List, Tuple

import requests

from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.getenv("SEARXNG_URL") or os.getenv("SEARXNG_BASE_URL") or "http://localhost:8080"
DEFAULT_SAFESEARCH = int(os.getenv("SEARXNG_SAFESEARCH", "1"))
DEFAULT_LANGUAGE = os.getenv("SEARXNG_LANGUAGE", "ja")
DEFAULT_LIMIT = int(os.getenv("SEARXNG_LIMIT", "5"))

# Rate limiting defaults
_RATE_LIMIT_CALLS = int(os.getenv("SEARXNG_RATE_LIMIT_CALLS", "10"))
_RATE_LIMIT_PERIOD = float(os.getenv("SEARXNG_RATE_LIMIT_PERIOD", "60"))

# Retry defaults
_MAX_RETRIES = 2  # max retries (total attempts = _MAX_RETRIES + 1)
_REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Sliding-window rate limiter (thread-safe)."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        """Block until a slot is available.  Return False on timeout."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.period)
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and self._timestamps[0] <= now - self.period:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return True
                wait_until = self._timestamps[0] + self.period

            if time.monotonic() >= deadline:
                return False
            sleep_time = max(0, min(wait_until - time.monotonic(), 0.5))
            time.sleep(sleep_time)


_rate_limiter = _RateLimiter(_RATE_LIMIT_CALLS, _RATE_LIMIT_PERIOD)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _is_retryable(exc: Exception, response: requests.Response | None) -> bool:
    """Determine whether the error is worth retrying."""
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if response is not None and response.status_code in (429, 500, 502, 503, 504):
        return True
    return False


def _error_message(exc: Exception, response: requests.Response | None) -> str:
    """Return a human/LLM-readable error message."""
    if isinstance(exc, requests.exceptions.ConnectionError):
        return (
            "[検索エラー] SearXNGサーバーに接続できません。"
            "サーバーが起動していない可能性があります。"
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return (
            "[検索エラー] 検索がタイムアウトしました。"
            "クエリを短くするか、時間をおいて再試行してください。"
        )
    if response is not None:
        if response.status_code == 429:
            return (
                "[検索エラー] SearXNGのレート制限に達しました。"
                "しばらく待ってから再度検索してください。"
            )
        if response.status_code >= 500:
            return f"[検索エラー] SearXNGサーバーエラー (HTTP {response.status_code})。"
    return f"[検索エラー] SearXNG検索に失敗しました: {exc}"


def _execute_search(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the HTTP request with retry and exponential backoff."""
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        response: requests.Response | None = None
        try:
            response = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            LOGGER.warning("SearXNG connection error (attempt %d/%d): %s",
                           attempt + 1, _MAX_RETRIES + 1, exc)
            # Connection errors: retry once, then give up
            if attempt >= 1:
                break
            time.sleep(1)
        except requests.exceptions.Timeout as exc:
            last_exc = exc
            LOGGER.warning("SearXNG timeout (attempt %d/%d)", attempt + 1, _MAX_RETRIES + 1)
            if attempt >= _MAX_RETRIES:
                break
            time.sleep(2 ** attempt)
        except requests.exceptions.HTTPError as exc:
            last_exc = exc
            if response is not None and _is_retryable(exc, response):
                LOGGER.warning("SearXNG HTTP %d (attempt %d/%d)",
                               response.status_code, attempt + 1, _MAX_RETRIES + 1)
                if attempt >= _MAX_RETRIES:
                    break
                time.sleep(2 ** attempt)
            else:
                # Non-retryable HTTP error
                raise _SearchError(_error_message(exc, response)) from exc
        except ValueError as exc:
            # JSON decode error — not retryable
            raise _SearchError(f"[検索エラー] SearXNGの応答を解釈できませんでした: {exc}") from exc

    raise _SearchError(_error_message(last_exc, response))


class _SearchError(Exception):
    """Internal error with a user-facing message."""
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
    # Normalize empty strings from SEA runtime to None
    if max_results == "" or max_results is None:
        max_results = None
    else:
        max_results = int(max_results)
    if engines == "":
        engines = None
    if language == "":
        language = None
    if safe == "" or safe is None:
        safe = None
    else:
        safe = int(safe)

    # Rate limiting
    if not _rate_limiter.acquire(timeout=_RATE_LIMIT_PERIOD):
        msg = "[検索エラー] レート制限に達しました。しばらく待ってから再度検索してください。"
        LOGGER.warning("SearXNG rate limit exceeded")
        return msg, ToolResult(history_snippet=None)

    try:
        url, params = _build_params(query, max_results, engines, language, safe)
        data = _execute_search(url, params)
    except _SearchError as exc:
        return str(exc), ToolResult(history_snippet=None)

    results: List[Dict[str, Any]] = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return "SearXNGから結果が見つかりませんでした。クエリやエンジンを調整して再試行してください。", ToolResult(history_snippet=None)

    lines: List[str] = []
    snippet_lines: List[str] = []
    for entry in results:
        title = entry.get("title") or entry.get("source") or "(no title)"
        entry_url = entry.get("url") or entry.get("link") or "(no url)"
        content = entry.get("content") or entry.get("snippet") or entry.get("summary") or ""
        content = content.replace("\n", " ").strip()
        lines.append(f"- {title}\n  {entry_url}\n  {content}")
        snippet_lines.append(f"{title} | {entry_url}")

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
