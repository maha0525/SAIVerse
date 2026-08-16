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

from llm_clients.openai_codex_auth import (
    LOGIN_MANAGER,
    CodexDeviceLoginError,
    auth_status,
    delete_saiverse_store,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


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
def codex_login_cancel():
    """ログイン試行を放棄する (モーダルを閉じたときにフロントが呼ぶ)。"""
    LOGIN_MANAGER.cancel()
    return {"ok": True}


@router.get("/status")
def codex_auth_status():
    """どのトークンストアが認証源か・その健康状態を返す。"""
    return auth_status()


@router.post("/logout")
def codex_logout():
    """SAIVerse 自前のトークンストアを削除する。

    Codex CLI の ~/.codex/auth.json には決して触れない。自前ストアが無い
    (CLI 相乗り中 or 未ログイン) 場合は removed=false で何もしない。
    """
    removed = delete_saiverse_store()
    status = auth_status()
    LOGGER.info("codex-auth: logout removed=%s now store=%s", removed, status.get("store"))
    return {"ok": True, "removed": removed, **status}
