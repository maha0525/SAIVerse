"""X (Twitter) API v2 client using requests.

Handles OAuth 2.0 PKCE flow, token refresh, rate limiting, and all API calls.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from .credentials import XCredentials, save_credentials

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_X_AUTH_URL = "https://x.com/i/oauth2/authorize"
_X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
_X_API_BASE = "https://api.x.com/2"
_SCOPES = "tweet.read tweet.write users.read offline.access"

_TOKEN_REFRESH_BUFFER_SECONDS = 300  # refresh 5 min before expiry
_REQUEST_TIMEOUT = 20
_MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class XAPIError(Exception):
    """X API returned an error."""

    def __init__(self, status_code: int, body: str, endpoint: str = ""):
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint
        super().__init__(f"X API error {status_code} on {endpoint}: {body}")


# ---------------------------------------------------------------------------
# Rate limiter (same pattern as searxng_search.py)
# ---------------------------------------------------------------------------

class XRateLimiter:
    """Sliding-window rate limiter (thread-safe)."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
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


# Per-endpoint rate limiters (requests per 15-min window)
_RATE_LIMITERS: Dict[str, XRateLimiter] = {
    "post_tweet": XRateLimiter(200, 900),
    "read_timeline": XRateLimiter(15, 900),
    "read_mentions": XRateLimiter(10, 900),
    "search_tweets": XRateLimiter(60, 900),
    "get_me": XRateLimiter(15, 900),
}


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    key = os.getenv("X_API_KEY", "")
    if not key:
        raise XAPIError(0, "X_API_KEY environment variable is not set", "config")
    return key


def _get_api_secret() -> str:
    secret = os.getenv("X_API_SECRET", "")
    if not secret:
        raise XAPIError(0, "X_API_SECRET environment variable is not set", "config")
    return secret


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _ensure_valid_token(
    creds: XCredentials, persona_path: Path
) -> XCredentials:
    """Refresh the access token if expired (or about to expire)."""
    now = time.time()
    if creds.token_expires_at > now + _TOKEN_REFRESH_BUFFER_SECONDS:
        return creds  # still valid

    if not creds.refresh_token:
        raise XAPIError(0, "No refresh token available. Please re-authorize.", "token_refresh")

    LOGGER.info("[x_client] Refreshing expired token for %s", creds.x_username)
    api_key = _get_api_key()
    api_secret = _get_api_secret()

    data = {
        "refresh_token": creds.refresh_token,
        "grant_type": "refresh_token",
        "client_id": api_key,
    }

    resp = requests.post(
        _X_TOKEN_URL,
        data=data,
        auth=(api_key, api_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_REQUEST_TIMEOUT,
    )

    if resp.status_code != 200:
        LOGGER.error("[x_client] Token refresh failed: %s %s", resp.status_code, resp.text)
        raise XAPIError(resp.status_code, resp.text, "token_refresh")

    token_data = resp.json()
    creds.access_token = token_data["access_token"]
    creds.refresh_token = token_data.get("refresh_token", creds.refresh_token)
    creds.token_expires_at = now + token_data.get("expires_in", 7200)

    save_credentials(persona_path, creds)
    LOGGER.info("[x_client] Token refreshed successfully for %s", creds.x_username)
    return creds


# ---------------------------------------------------------------------------
# Internal request helper
# ---------------------------------------------------------------------------

def _make_request(
    method: str,
    url: str,
    creds: XCredentials,
    persona_path: Path,
    limiter_key: str,
    **kwargs: Any,
) -> requests.Response:
    """Execute an authenticated X API request with rate limiting and retry."""

    limiter = _RATE_LIMITERS.get(limiter_key)
    if limiter and not limiter.acquire(timeout=60):
        raise XAPIError(429, f"Local rate limit reached for {limiter_key}", url)

    creds = _ensure_valid_token(creds, persona_path)
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {creds.access_token}"

    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            LOGGER.debug("[x_client] %s %s (attempt %d)", method, url, attempt + 1)
            resp = requests.request(
                method, url, headers=headers, timeout=_REQUEST_TIMEOUT, **kwargs
            )
            LOGGER.debug("[x_client] Response %d: %s", resp.status_code, resp.text[:500])

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("retry-after", "60"))
                LOGGER.warning("[x_client] Rate limited by X API. Waiting %ds", retry_after)
                if attempt < _MAX_RETRIES:
                    time.sleep(min(retry_after, 120))
                    continue
                raise XAPIError(429, resp.text, url)

            if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                wait = 2 ** attempt
                LOGGER.warning("[x_client] Server error %d, retrying in %ds", resp.status_code, wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise XAPIError(resp.status_code, resp.text, url)

            return resp

        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                wait = 2 ** attempt
                LOGGER.warning("[x_client] Request error: %s, retrying in %ds", exc, wait)
                time.sleep(wait)
            else:
                raise XAPIError(0, str(exc), url) from exc

    raise XAPIError(0, str(last_exc), url)


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

def post_tweet(text: str, creds: XCredentials, persona_path: Path) -> dict:
    """Post a tweet. Returns the API response dict."""
    resp = _make_request(
        "POST",
        f"{_X_API_BASE}/tweets",
        creds, persona_path,
        limiter_key="post_tweet",
        json={"text": text},
    )
    return resp.json()


def reply_tweet(
    text: str,
    in_reply_to_tweet_id: str,
    creds: XCredentials,
    persona_path: Path,
) -> dict:
    """Post a reply to a specific tweet. Returns the API response dict."""
    resp = _make_request(
        "POST",
        f"{_X_API_BASE}/tweets",
        creds, persona_path,
        limiter_key="post_tweet",
        json={
            "text": text,
            "reply": {"in_reply_to_tweet_id": in_reply_to_tweet_id},
        },
    )
    return resp.json()


def read_timeline(
    creds: XCredentials, persona_path: Path, max_results: int = 10
) -> List[dict]:
    """Read home timeline. Returns list of tweet dicts."""
    params = {
        "max_results": min(max(max_results, 1), 100),
        "tweet.fields": "created_at,author_id,text,public_metrics,attachments",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "username,name",
        "media.fields": "url,preview_image_url,type",
    }
    resp = _make_request(
        "GET",
        f"{_X_API_BASE}/users/{creds.x_user_id}/timelines/reverse_chronological",
        creds, persona_path,
        limiter_key="read_timeline",
        params=params,
    )
    data = resp.json()
    return _merge_users_into_tweets(data)


def read_mentions(
    creds: XCredentials,
    persona_path: Path,
    max_results: int = 10,
    since_id: Optional[str] = None,
) -> List[dict]:
    """Read mentions. Returns list of tweet dicts.

    Args:
        since_id: If provided, only return mentions newer than this tweet ID.
    """
    params: Dict[str, Any] = {
        "max_results": min(max(max_results, 5), 100),
        "tweet.fields": "created_at,author_id,text,public_metrics,attachments",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "username,name",
        "media.fields": "url,preview_image_url,type",
    }
    if since_id:
        params["since_id"] = since_id
    resp = _make_request(
        "GET",
        f"{_X_API_BASE}/users/{creds.x_user_id}/mentions",
        creds, persona_path,
        limiter_key="read_mentions",
        params=params,
    )
    data = resp.json()
    return _merge_users_into_tweets(data)


def search_tweets(
    query: str, creds: XCredentials, persona_path: Path, max_results: int = 10
) -> List[dict]:
    """Search recent tweets (7-day window). Returns list of tweet dicts."""
    params = {
        "query": query,
        "max_results": min(max(max_results, 10), 100),
        "tweet.fields": "created_at,author_id,text,public_metrics,attachments",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "username,name",
        "media.fields": "url,preview_image_url,type",
        "sort_order": "recency",
    }
    resp = _make_request(
        "GET",
        f"{_X_API_BASE}/tweets/search/recent",
        creds, persona_path,
        limiter_key="search_tweets",
        params=params,
    )
    data = resp.json()
    return _merge_users_into_tweets(data)


def get_me(creds: XCredentials, persona_path: Path) -> dict:
    """Get authenticated user info. Returns user dict with id, username, name."""
    resp = _make_request(
        "GET",
        f"{_X_API_BASE}/users/me",
        creds, persona_path,
        limiter_key="get_me",
        params={"user.fields": "username,name,profile_image_url"},
    )
    return resp.json().get("data", {})


# ---------------------------------------------------------------------------
# OAuth 2.0 PKCE helpers
# ---------------------------------------------------------------------------

def generate_auth_url(callback_url: str) -> Tuple[str, str, str]:
    """Generate OAuth 2.0 authorization URL with PKCE.

    Returns (auth_url, state, code_verifier).
    """
    api_key = _get_api_key()

    code_verifier = secrets.token_urlsafe(96)[:128]
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": callback_url,
        "scope": _SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{_X_AUTH_URL}?{urlencode(params)}"
    return auth_url, state, code_verifier


def exchange_code_for_tokens(
    code: str, code_verifier: str, callback_url: str
) -> dict:
    """Exchange authorization code for access + refresh tokens.

    Returns raw token dict from X API.
    """
    api_key = _get_api_key()
    api_secret = _get_api_secret()

    data = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": api_key,
        "redirect_uri": callback_url,
        "code_verifier": code_verifier,
    }

    resp = requests.post(
        _X_TOKEN_URL,
        data=data,
        auth=(api_key, api_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_REQUEST_TIMEOUT,
    )

    if resp.status_code != 200:
        LOGGER.error("[x_client] Token exchange failed: %s %s", resp.status_code, resp.text)
        raise XAPIError(resp.status_code, resp.text, "token_exchange")

    return resp.json()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _merge_users_into_tweets(api_response: dict) -> List[dict]:
    """Merge user and media info from 'includes' into each tweet's data."""
    tweets = api_response.get("data") or []
    includes = api_response.get("includes") or {}
    users_list = includes.get("users") or []
    media_list = includes.get("media") or []
    users_by_id = {u["id"]: u for u in users_list}
    media_by_key = {m["media_key"]: m for m in media_list}

    result = []
    for tweet in tweets:
        author_id = tweet.get("author_id", "")
        user = users_by_id.get(author_id, {})

        # Resolve attached media
        media_keys = (tweet.get("attachments") or {}).get("media_keys") or []
        media_urls = []
        for mk in media_keys:
            m = media_by_key.get(mk, {})
            url = m.get("url") or m.get("preview_image_url") or ""
            if url:
                media_urls.append({"type": m.get("type", ""), "url": url})

        result.append({
            "id": tweet.get("id", ""),
            "text": tweet.get("text", ""),
            "created_at": tweet.get("created_at", ""),
            "author_id": author_id,
            "author_username": user.get("username", ""),
            "author_name": user.get("name", ""),
            "metrics": tweet.get("public_metrics", {}),
            "media": media_urls,
        })
    return result
