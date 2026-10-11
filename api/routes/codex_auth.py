"""Codex サブスク認証 API。

SAIVerse 自身が ChatGPT アカウントへログインするためのエンドポイント群。
ログイン方式は二つある:

  - デバイスコード方式 (login/*): user_code を表示し、ユーザーが OpenAI の
    ページでそれを入力する。Codex CLI の client_id を名乗る非公開経路。
  - Sign in with ChatGPT (siwc/*): OpenAI 公式のトークン共有。バックエンドは
    ブラウザを開かない — フロントが auth_url をリンクとして見せる (ユーザーの
    ブラウザは別の端末にあるかもしれない)。サインイン後のブラウザは
    127.0.0.1 の受け口へ戻ってくる。別の端末のブラウザで受け口に届かない
    ときは、ブラウザが最後に開いた URL を画面に貼り付けて siwc/complete で
    同じ検証・保存の経路へ流す。

フロント (プロバイダ管理画面) はここだけを見る。トークンの値はどのレスポンス
にも含めない。

エンドポイント:
  - POST /api/codex-auth/login/start     → user_code と誘導 URL を返し、背景でポーリング開始
  - GET  /api/codex-auth/login/status    → デバイスコード試行の進行状態 (idle/waiting/success/error)
  - POST /api/codex-auth/login/cancel    → デバイスコード試行の lease 返却 (user_code は OpenAI 側で自然失効)
  - POST /api/codex-auth/siwc/start      → auth_url と redirect_uri を返し、127.0.0.1 の受け口を開く
  - GET  /api/codex-auth/siwc/status     → SIWC 試行の進行状態 (idle/waiting/success/error)
  - POST /api/codex-auth/siwc/cancel     → SIWC 試行の lease 返却 (最後の lease で受け口を閉じる)
  - POST /api/codex-auth/siwc/complete   → ブラウザが開いた転送先 URL の手動貼り付け
  - GET  /api/codex-auth/status          → いまの認証源 (saiverse / codex_cli / なし) と健康状態
  - POST /api/codex-auth/logout          → 両方式の進行中試行を無効化し、SIWC の refresh_token を
                                           OpenAI で失効させ (失敗しても続行)、SAIVerse 自前ストアを
                                           削除する。~/.codex には決して触れない

Intent Doc: docs/intent/codex_subscription_auth.md
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from llm_clients.openai_codex_auth import (
    LOGIN_MANAGER,
    SIWC_LOGIN_MANAGER,
    CodexDeviceLoginError,
    SiwcCallbackRejected,
    SiwcOAuthError,
    auth_status,
    logout_all_and_delete_store,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class CodexLoginCancelRequest(BaseModel):
    """cancel の対象。両方とも start が返した値で、省略不可。

    attempt_id だけでは足りない — 進行中の試行への start は同じ attempt_id に
    相乗りする (同じコード / 同じ auth_url を表示する) ため、閉じたモーダルの
    遅延 cancel が開き直したモーダルの試行を殺してしまう。lease_id がどの
    クライアントの取り下げかを識別し、全クライアントが取り下げたときだけ
    試行が止まる。省略経路は世代ガードの迂回路になるので設けない (欠落は 422)。
    デバイスコード方式と SIWC で共通。
    """

    attempt_id: int
    lease_id: str


class SiwcCompleteRequest(BaseModel):
    """手動完了の入力。attempt_id は siwc/start が返した値、callback_url は
    サインイン後にブラウザのアドレス欄に表示された URL まるごと
    (http://127.0.0.1:<port>/auth/callback?code=...&state=...)。"""

    attempt_id: int
    callback_url: str


@router.post("/login/start")
def codex_login_start():
    """デバイスコードを申請し、ユーザーに見せる user_code を返す。

    進行中のログインがあれば同じ user_code を返す (二重開始しない)。
    """
    try:
        return LOGIN_MANAGER.start()
    except CodexDeviceLoginError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/login/status")
def codex_login_status():
    """ログイン試行の進行状態を返す。フロントはこれをポーリングする。"""
    return LOGIN_MANAGER.status()


@router.post("/login/cancel")
def codex_login_cancel(request: CodexLoginCancelRequest):
    """自分の lease を返却する (モーダルを閉じたときにフロントが呼ぶ)。

    その試行の lease が全部返却されたときだけ試行そのものが止まる。
    古い attempt_id・未知の lease_id・確定後の cancel は無視される。
    """
    cancelled = LOGIN_MANAGER.cancel(request.attempt_id, request.lease_id)
    return {"ok": True, "cancelled": cancelled}


@router.post("/siwc/start")
def codex_siwc_start():
    """Sign in with ChatGPT の試行を開き、{state, attempt_id, lease_id, auth_url,
    redirect_uri} を返す。

    進行中の試行があればそれに相乗りし (同じ attempt_id / auth_url)、新しい
    lease_id だけを発行する。ブラウザはバックエンドでは開かない。
    """
    try:
        return SIWC_LOGIN_MANAGER.start()
    except SiwcOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/siwc/status")
def codex_siwc_status():
    """SIWC 試行の進行状態 {state, attempt_id, auth_url?, error?} を返す。"""
    return SIWC_LOGIN_MANAGER.status()


@router.post("/siwc/cancel")
def codex_siwc_cancel(request: CodexLoginCancelRequest):
    """自分の lease を返却する。意味論はデバイスコード方式の cancel と同じ。"""
    cancelled = SIWC_LOGIN_MANAGER.cancel(request.attempt_id, request.lease_id)
    return {"ok": True, "cancelled": cancelled}


@router.post("/siwc/complete")
def codex_siwc_complete(request: SiwcCompleteRequest):
    """貼り付けられた転送先 URL を、受け口に届いた場合と同じ検証・保存の経路へ流す。

    古い attempt_id・転送先ではない URL・state 不一致・処理中の重複は、試行に
    触れずに 4xx で拒否する。処理した場合は 200 で {ok, message, ...status} を
    返す (ok=false なら status は error 状態)。
    """
    try:
        return SIWC_LOGIN_MANAGER.complete(request.attempt_id, request.callback_url)
    except SiwcCallbackRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/status")
def codex_auth_status():
    """どのトークンストアが認証源か・その健康状態を返す。"""
    return auth_status()


@router.post("/logout")
def codex_logout():
    """SAIVerse 自前のトークンストアを削除する。

    両方式の進行中のログイン試行の無効化とストア削除を一続きのクリティカル
    セクションで行う — 別々にやると、ブラウザ側の認証が logout とすれ違いで
    完了したとき、ワーカーがストアを再生成して「ログアウトしたのに認証が
    復活」する。SIWC のトークンなら削除の前に refresh_token を OpenAI で
    失効させる (失敗しても警告を残して削除は行う。revoked=false で返す)。
    Codex CLI の ~/.codex/auth.json には決して触れない。自前ストアが無い
    (CLI 相乗り中 or 未ログイン) 場合は removed=false。
    """
    result = logout_all_and_delete_store()
    status = auth_status()
    LOGGER.info(
        "codex-auth: logout removed=%s revoked=%s now store=%s",
        result["removed"], result["revoked"], status.get("store"),
    )
    return {"ok": True, "removed": result["removed"], "revoked": result["revoked"], **status}
