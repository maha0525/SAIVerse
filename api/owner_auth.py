from __future__ import annotations

import hashlib
import hmac
import html
import os
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE_NAME = "saiverse_owner_session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

router = APIRouter()


def _owner_token() -> str:
    return os.getenv("SAIVERSE_OWNER_TOKEN", "")


def _session_value(token: str) -> str:
    return hmac.new(
        token.encode("utf-8"),
        b"saiverse-owner-session-v1",
        hashlib.sha256,
    ).hexdigest()


def _configured_origins() -> set[str]:
    raw = os.getenv("SAIVERSE_ALLOWED_ORIGINS", "")
    return {value.strip().rstrip("/") for value in raw.split(",") if value.strip()}


def _origin_allowed(origin: str) -> bool:
    normalized = origin.rstrip("/")
    configured = _configured_origins()
    if normalized in configured:
        return True
    try:
        host = urlsplit(normalized).hostname
    except ValueError:
        return False
    return bool(host and host in configured)


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and value:
        return value
    return None


class OwnerAuthMiddleware(BaseHTTPMiddleware):
    """Protect the API when the backend is explicitly exposed beyond loopback."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/api/auth/login" or path.startswith("/api/oauth/callback/"):
            return await call_next(request)

        token = _owner_token()
        if not token:
            return JSONResponse(
                {"detail": "SAIVerse owner authentication is not configured"},
                status_code=503,
            )

        bearer = _bearer_token(request)
        bearer_ok = bool(bearer and hmac.compare_digest(bearer, token))
        cookie = request.cookies.get(COOKIE_NAME, "")
        cookie_ok = bool(cookie and hmac.compare_digest(cookie, _session_value(token)))
        if not bearer_ok and not cookie_ok:
            return JSONResponse({"detail": "Owner authentication required"}, status_code=401)

        if request.method.upper() not in SAFE_METHODS and not bearer_ok:
            origin = request.headers.get("origin", "")
            if not origin or not _origin_allowed(origin):
                return JSONResponse(
                    {"detail": "Request origin is not allowed"},
                    status_code=403,
                )

        return await call_next(request)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    host = html.escape(request.url.hostname or "localhost")
    return HTMLResponse(
        "<!doctype html><html lang='ja'><meta charset='utf-8'>"
        "<title>SAIVerse owner login</title>"
        "<body><h1>SAIVerse owner login</h1>"
        "<form method='post'><label>Owner token "
        "<input name='token' type='password' autocomplete='current-password' required>"
        "</label><button type='submit'>ログイン</button></form>"
        f"<p>認証後は {host}:3000 のUIへ移動します。</p></body></html>"
    )


@router.post("/login")
async def login(request: Request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        return JSONResponse({"detail": "Unsupported content type"}, status_code=415)
    form = parse_qs((await request.body()).decode("utf-8", errors="strict"))
    supplied = (form.get("token") or [""])[0]
    token = _owner_token()
    if not token or not hmac.compare_digest(supplied, token):
        return JSONResponse({"detail": "Invalid owner token"}, status_code=403)

    hostname = request.url.hostname or "localhost"
    response = RedirectResponse(f"http://{hostname}:3000/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        _session_value(token),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response
