"""汎用 OAuth フロー API。

addon.json の oauth_flows セクションで宣言されたOAuth接続を、コア側で一元的に
処理するためのエンドポイント群。

エンドポイント:
  - GET  /api/oauth/start/{addon_name}/{flow_key}?persona_id=...
        → 認可URLを返す（フロントはこれをポップアップで開く）
  - GET  /api/oauth/callback/{addon_name}/{flow_key}?code=...&state=...
        → トークン交換 + AddonPersonaConfig 保存。HTML を返してポップアップを閉じる
  - GET  /api/oauth/{addon_name}/{flow_key}/{persona_id}/status
        → 接続ステータス
  - DELETE /api/oauth/{addon_name}/{flow_key}/{persona_id}
        → 切断（保存トークン削除）

Intent Doc: docs/intent/addon_extension_points.md セクション B
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from saiverse.oauth import (
    OAuthError,
    OAuthFlowNotFoundError,
    build_authorize_url,
    disconnect,
    exchange_code,
    get_status,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _get_request_base_url(request: Request) -> str:
    """リクエストヘッダから base URL（scheme://host[:port]）を組み立てる。

    プロキシ経由を考慮して X-Forwarded-Proto / X-Forwarded-Host を優先する。
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"
    return f"{request.url.scheme}://{request.url.netloc}"


@router.get("/start/{addon_name}/{flow_key}")
def oauth_start(
    addon_name: str,
    flow_key: str,
    request: Request,
    persona_id: str = Query(..., description="認可対象のペルソナID"),
):
    """認可URLを生成して返す。フロントはこれをポップアップで開く。"""
    base_url = _get_request_base_url(request)
    try:
        auth_url = build_authorize_url(addon_name, flow_key, persona_id, base_url)
    except OAuthFlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"auth_url": auth_url}


@router.get("/callback/{addon_name}/{flow_key}")
def oauth_callback(
    addon_name: str,
    flow_key: str,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    """OAuth 認可サーバーからのコールバック。

    成功時はHTMLを返してポップアップを自動で閉じる。フロントエンドは
    親ウィンドウからポーリングで status を確認する設計のため、
    postMessage は使わない（origin 違いの煩雑さ回避）。
    """
    if error:
        msg = f"{error}: {error_description}" if error_description else error
        LOGGER.warning(
            "oauth: authorization denied addon=%s flow=%s reason=%s",
            addon_name, flow_key, msg,
        )
        return HTMLResponse(_render_callback_html(False, msg), status_code=400)

    if not code or not state:
        return HTMLResponse(
            _render_callback_html(False, "Missing code or state parameter"),
            status_code=400,
        )

    try:
        result = exchange_code(addon_name, flow_key, code, state)
        LOGGER.info(
            "oauth: callback success addon=%s flow=%s persona=%s",
            addon_name, flow_key, result.get("persona_id"),
        )
        return HTMLResponse(_render_callback_html(True, None))
    except OAuthFlowNotFoundError as exc:
        return HTMLResponse(_render_callback_html(False, str(exc)), status_code=404)
    except OAuthError as exc:
        LOGGER.warning(
            "oauth: callback error addon=%s flow=%s error=%s",
            addon_name, flow_key, exc,
        )
        return HTMLResponse(_render_callback_html(False, str(exc)), status_code=400)


@router.get("/{addon_name}/{flow_key}/{persona_id}/status")
def oauth_status(addon_name: str, flow_key: str, persona_id: str):
    """接続ステータスを返す。"""
    try:
        return get_status(addon_name, flow_key, persona_id)
    except OAuthFlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{addon_name}/{flow_key}/{persona_id}")
def oauth_disconnect(addon_name: str, flow_key: str, persona_id: str):
    """OAuth 接続を切断する（保存トークンを削除）。"""
    try:
        disconnect(addon_name, flow_key, persona_id)
    except OAuthFlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"ok": True})


def _render_callback_html(success: bool, error_message: Optional[str]) -> str:
    """ポップアップウィンドウに表示する HTML を返す。

    成功時は「閉じて構いません」、失敗時はエラーを表示してから自動クローズ。
    """
    if success:
        title = "連携完了"
        body = (
            "<p>連携が完了しました。このウィンドウは自動的に閉じられます。</p>"
        )
        # 1秒後に自動クローズ（親ウィンドウのポーリングがすぐに状態を取り直す）
        script = "<script>setTimeout(function(){window.close();}, 1000);</script>"
    else:
        title = "連携に失敗しました"
        # error_message を HTML エスケープ
        import html
        safe_msg = html.escape(error_message or "Unknown error")
        body = (
            f"<p>連携に失敗しました。</p>"
            f"<pre style='background:#fee;padding:8px;'>{safe_msg}</pre>"
            f"<p>このウィンドウを閉じて再度お試しください。</p>"
        )
        script = ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: sans-serif; padding: 24px; max-width: 480px; }}
    h1 {{ font-size: 1.2em; }}
    pre {{ white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  {body}
  {script}
</body>
</html>"""
