"""X (Twitter) OAuth 2.0 PKCE authentication endpoints.

Provides:
- Persona-scoped endpoints (GET/POST/PATCH/DELETE on /{persona_id}/x/...)
- Top-level callback endpoint (GET /api/x/callback)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.deps import get_manager
from saiverse.data_paths import get_saiverse_home

LOGGER = logging.getLogger(__name__)

# Ensure x_lib is importable
_TOOLS_DIR = str(Path(__file__).resolve().parents[3] / "builtin_data" / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

router = APIRouter()           # persona-scoped, included under /api/people
callback_router = APIRouter()  # top-level, included under /api/x

# In-memory store for pending OAuth flows (state -> flow data)
_pending_oauth: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persona_path(persona_id: str) -> Path:
    return get_saiverse_home() / "personas" / persona_id


def _get_callback_url(request: Request) -> str:
    """Determine the OAuth callback URL."""
    override = os.getenv("X_CALLBACK_URL")
    if override:
        return override
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/x/callback"


# ---------------------------------------------------------------------------
# Persona-scoped endpoints
# ---------------------------------------------------------------------------

@router.get("/{persona_id}/x/auth-url")
def get_x_auth_url(persona_id: str, request: Request, manager=Depends(get_manager)):
    """Generate OAuth 2.0 authorization URL for X."""
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    api_key = os.getenv("X_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="X_API_KEY が設定されていません。.env ファイルに X_API_KEY を追加してください。",
        )
    api_secret = os.getenv("X_API_SECRET", "")
    if not api_secret:
        raise HTTPException(
            status_code=400,
            detail="X_API_SECRET が設定されていません。.env ファイルに X_API_SECRET を追加してください。",
        )

    from x_lib.client import generate_auth_url

    callback_url = _get_callback_url(request)
    auth_url, state, code_verifier = generate_auth_url(callback_url)

    # Store pending flow data
    _pending_oauth[state] = {
        "code_verifier": code_verifier,
        "persona_id": persona_id,
        "callback_url": callback_url,
        "created_at": time.time(),
    }

    LOGGER.info("[x_auth] Generated auth URL for persona=%s state=%s", persona_id, state[:8])
    return {"auth_url": auth_url, "callback_url": callback_url}


class XStatusResponse(BaseModel):
    connected: bool
    username: Optional[str] = None
    x_user_id: Optional[str] = None
    skip_confirmation: bool = False


@router.get("/{persona_id}/x/status", response_model=XStatusResponse)
def get_x_status(persona_id: str, manager=Depends(get_manager)):
    """Check X connection status for a persona."""
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    from x_lib.credentials import load_credentials

    p_path = _persona_path(persona_id)
    creds = load_credentials(p_path)
    if not creds:
        return XStatusResponse(connected=False)

    return XStatusResponse(
        connected=True,
        username=creds.x_username,
        x_user_id=creds.x_user_id,
        skip_confirmation=creds.skip_confirmation,
    )


@router.post("/{persona_id}/x/disconnect")
def disconnect_x(persona_id: str, manager=Depends(get_manager)):
    """Remove X credentials for a persona."""
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    from x_lib.credentials import delete_credentials

    p_path = _persona_path(persona_id)
    existed = delete_credentials(p_path)
    LOGGER.info("[x_auth] Disconnected X for persona=%s (existed=%s)", persona_id, existed)
    return {"success": True, "was_connected": existed}


class XSettingsUpdate(BaseModel):
    skip_confirmation: Optional[bool] = None


@router.patch("/{persona_id}/x/settings")
def update_x_settings(persona_id: str, req: XSettingsUpdate, manager=Depends(get_manager)):
    """Update X integration settings for a persona."""
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    from x_lib.credentials import load_credentials, save_credentials

    p_path = _persona_path(persona_id)
    creds = load_credentials(p_path)
    if not creds:
        raise HTTPException(status_code=400, detail="X is not connected for this persona")

    if req.skip_confirmation is not None:
        creds.skip_confirmation = req.skip_confirmation

    save_credentials(p_path, creds)
    LOGGER.info("[x_auth] Updated X settings for persona=%s skip_confirmation=%s", persona_id, creds.skip_confirmation)
    return {"success": True}


# ---------------------------------------------------------------------------
# Top-level callback endpoint
# ---------------------------------------------------------------------------

_CALLBACK_SUCCESS_HTML = """<!DOCTYPE html>
<html><head><title>X連携完了</title>
<style>
body {{ font-family: sans-serif; display: flex; justify-content: center; align-items: center;
       height: 100vh; margin: 0; background: #1a1a2e; color: #e0e0e0; }}
.card {{ text-align: center; padding: 2rem; background: #16213e; border-radius: 12px; }}
h2 {{ color: #00d2ff; }}
</style></head>
<body><div class="card">
<h2>X連携が完了しました！</h2>
<p>@{username} のアカウントが連携されました。</p>
<p>このウィンドウは自動的に閉じます...</p>
<script>setTimeout(function(){{ window.close(); }}, 2000);</script>
</div></body></html>"""

_CALLBACK_ERROR_HTML = """<!DOCTYPE html>
<html><head><title>X連携エラー</title>
<style>
body {{ font-family: sans-serif; display: flex; justify-content: center; align-items: center;
       height: 100vh; margin: 0; background: #1a1a2e; color: #e0e0e0; }}
.card {{ text-align: center; padding: 2rem; background: #16213e; border-radius: 12px; }}
h2 {{ color: #ff4444; }}
</style></head>
<body><div class="card">
<h2>X連携エラー</h2>
<p>{error}</p>
<p><button onclick="window.close()">閉じる</button></p>
</div></body></html>"""


@callback_router.get("/callback")
def x_oauth_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
):
    """Handle OAuth 2.0 callback from X."""
    if error:
        LOGGER.warning("[x_auth] OAuth callback error: %s", error)
        return HTMLResponse(_CALLBACK_ERROR_HTML.format(error=f"Xからのエラー: {error}"))

    if not code or not state:
        return HTMLResponse(_CALLBACK_ERROR_HTML.format(error="必要なパラメータが不足しています。"))

    flow = _pending_oauth.pop(state, None)
    if not flow:
        LOGGER.warning("[x_auth] Unknown or expired OAuth state: %s", state[:8])
        return HTMLResponse(_CALLBACK_ERROR_HTML.format(error="認証セッションが見つかりません。もう一度お試しください。"))

    # Check expiry (10 minutes)
    if time.time() - flow["created_at"] > 600:
        LOGGER.warning("[x_auth] Expired OAuth flow for state=%s", state[:8])
        return HTMLResponse(_CALLBACK_ERROR_HTML.format(error="認証セッションの有効期限が切れました。もう一度お試しください。"))

    persona_id = flow["persona_id"]
    code_verifier = flow["code_verifier"]
    callback_url = flow["callback_url"]

    from x_lib.client import exchange_code_for_tokens, get_me, XAPIError
    from x_lib.credentials import XCredentials, save_credentials

    try:
        # Exchange code for tokens
        token_data = exchange_code_for_tokens(code, code_verifier, callback_url)
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 7200)

        # Get user info
        p_path = _persona_path(persona_id)
        temp_creds = XCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            x_user_id="",
            x_username="",
            token_expires_at=time.time() + expires_in,
        )
        user_info = get_me(temp_creds, p_path)

        # Save credentials
        creds = XCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            x_user_id=user_info.get("id", ""),
            x_username=user_info.get("username", ""),
            token_expires_at=time.time() + expires_in,
        )
        save_credentials(p_path, creds)

        LOGGER.info("[x_auth] Successfully connected @%s for persona=%s", creds.x_username, persona_id)
        return HTMLResponse(_CALLBACK_SUCCESS_HTML.format(username=creds.x_username))

    except XAPIError as exc:
        LOGGER.error("[x_auth] Token exchange/user info failed: %s", exc, exc_info=True)
        return HTMLResponse(_CALLBACK_ERROR_HTML.format(error=f"APIエラー: {exc}"))
    except Exception as exc:
        LOGGER.error("[x_auth] Unexpected error in callback: %s", exc, exc_info=True)
        return HTMLResponse(_CALLBACK_ERROR_HTML.format(error=f"予期しないエラー: {exc}"))
