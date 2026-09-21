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


#: 画面 (Next.js) が動く既定の origin。main.py の CORSMiddleware と同じ源に
#: するため、ここを唯一の定義にする。
DEFAULT_BROWSER_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")


def allowed_browser_origins() -> set[str]:
    """ブラウザからの接続を許す origin の集合。

    HTTP の CORS (main.py の ``CORSMiddleware``) と WebSocket の Origin 検査
    (``api/routes/voice_call.py``) が同じ集合を見るための一箇所。末尾の ``/``
    は落として比較する。
    """
    origins = set(DEFAULT_BROWSER_ORIGINS)
    origins.update(
        origin.strip().rstrip("/")
        for origin in os.getenv("SAIVERSE_ALLOWED_ORIGINS", "").split(",")
        if origin.strip().startswith(("http://", "https://"))
    )
    return origins


def owner_auth_active(app: object) -> bool:
    """この app に :class:`OwnerAuthMiddleware` が入っているか。

    ミドルウェアは LAN 公開のときだけ入る (main.py)。WebSocket は
    ``BaseHTTPMiddleware`` を素通りする (``scope["type"] == "websocket"``) ので、
    WS のルートは「HTTP 側が守られているか」をこれで見て、同じ条件のときだけ
    自前で検証する。localhost だけで動かしているときに WS が拒否されない
    (= HTTP 側と挙動が一致する) のはこの判定による。
    """
    for middleware in getattr(app, "user_middleware", None) or []:
        if getattr(middleware, "cls", None) is OwnerAuthMiddleware:
            return True
    return False


def websocket_owner_authorized(websocket: object) -> bool:
    """WebSocket のハンドシェイクが owner の認証を満たしているか。

    HTTP 側 (:class:`OwnerAuthMiddleware`) と同じ二つの運搬手段を見る:
    ``Authorization: Bearer <SAIVERSE_OWNER_TOKEN>`` ヘッダか、ログイン後に
    付く ``saiverse_owner_session`` cookie。WebSocket のハンドシェイクは
    通常の HTTP リクエストなので、どちらもそのまま届く。
    """
    token = _owner_token()
    if not token:
        return False
    headers = getattr(websocket, "headers", {}) or {}
    authorization = headers.get("authorization", "") if hasattr(headers, "get") else ""
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and value and hmac.compare_digest(value, token):
        return True
    cookies = getattr(websocket, "cookies", {}) or {}
    cookie = cookies.get(COOKIE_NAME, "") if hasattr(cookies, "get") else ""
    return bool(cookie and hmac.compare_digest(cookie, _session_value(token)))


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
