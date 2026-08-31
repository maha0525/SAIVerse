"""Codex サブスク認証 API。

SAIVerse 自身が ChatGPT アカウントへデバイスコード方式でログインするための
エンドポイント群。フロント (プロバイダ管理画面) はここだけを見る。
トークンの値はどのレスポンスにも含めない。

エンドポイント:
  - POST /api/codex-auth/login/start   → user_code と誘導 URL を返し、背景でポーリング開始
  - GET  /api/codex-auth/login/status  → ログイン試行の進行状態 (idle/waiting/success/error)
  - POST /api/codex-auth/login/cancel  → ログイン試行の放棄 (user_code は OpenAI 側で自然失効)
  - GET  /api/codex-auth/status        → いまの認証源 (saiverse / codex_cli / なし) と健康状態
  - POST /api/codex-auth/logout        → SAIVerse 自前ストアの削除。~/.codex には決して触れない

Intent Doc: docs/intent/codex_subscription_auth.md
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from llm_clients.openai_codex_auth import (
    LOGIN_MANAGER,
    CodexDeviceLoginError,
    auth_status,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class CodexLoginCancelRequest(BaseModel):
    """cancel の対象。両方とも login/start が返した値で、省略不可。

    attempt_id だけでは足りない — 進行中の試行への start は同じ attempt_id に
    相乗りする (同じコードを表示する) ため、閉じたモーダルの遅延 cancel が
    開き直したモーダルの試行を殺してしまう。lease_id がどのクライアントの
    取り下げかを識別し、全クライアントが取り下げたときだけ試行が止まる。
    省略経路は世代ガードの迂回路になるので設けない (欠落は 422)。
    """

    attempt_id: int
    lease_id: str


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


@router.get("/status")
def codex_auth_status():
    """どのトークンストアが認証源か・その健康状態を返す。"""
    return auth_status()


@router.post("/logout")
def codex_logout():
    """SAIVerse 自前のトークンストアを削除する。

    進行中のログイン試行の無効化とストア削除を manager 側の一つの
    クリティカルセクションで行う — 別々にやると、ブラウザ側の認証が
    logout とすれ違いで完了したとき、ワーカーがストアを再生成して
    「ログアウトしたのに認証が復活」する。
    Codex CLI の ~/.codex/auth.json には決して触れない。自前ストアが無い
    (CLI 相乗り中 or 未ログイン) 場合は removed=false。
    """
    removed = LOGIN_MANAGER.logout_and_delete_store()
    status = auth_status()
    LOGGER.info("codex-auth: logout removed=%s now store=%s", removed, status.get("store"))
    return {"ok": True, "removed": removed, **status}
