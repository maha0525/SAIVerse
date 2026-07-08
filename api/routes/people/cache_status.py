"""Cache Lifecycle Control Phase 1: read-only キャッシュタイマー status endpoint。

prompt cache が「効いているか / 残り時間」をリアルタイム表示するための read-only
endpoint。state を持たず、``METABOLISM_ANCHORS[model].updated_at`` (= cache 書き込み
の真の起点) と per-model TTL から算出する。累計ヒット / 節約額は載せない
(即時性がないため Usage ページの領分 — docs/intent/cache_lifecycle_control.md §1)。

Phase 1 は Anthropic explicit cache のみ ``supported=true``。implicit (Gemini 等) は
Phase 3 で対応する。

設計: docs/intent/cache_lifecycle_control.md §4.6 / §7 Phase 1
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{persona_id}/cache-status")
def get_cache_status(persona_id: str, manager=Depends(get_manager)) -> dict[str, Any]:
    """指定ペルソナの prompt cache 状態 (効いてるか / 残り秒) を read-only で返す。

    実体は ``METABOLISM_ANCHORS[model].updated_at`` + per-model TTL からの算出で、
    新規 state は持たない。anchor が無い (まだ cache 書き込みが起きていない) / TTL
    切れ / explicit 非対応モデルの場合は ``active=false`` で返す。
    """
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    persona = manager.personas.get(persona_id)
    # anchor が keyed されるのは metabolism が使う persona.model。未ロード時は
    # DB の DEFAULT_MODEL にフォールバック (この場合 persona オブジェクトが無く
    # anchor は引けないので active=false になる)。
    model_key = getattr(persona, "model", None) or details.get("DEFAULT_MODEL") or None

    status: dict[str, Any] = {
        "persona_id": persona_id,
        "model": model_key,
        "supported": False,        # Phase 1: Anthropic explicit のみ true
        "cache_type": None,        # "explicit" | "implicit" | None
        "active": False,           # cache が現在生きているか
        "anchor_updated_at": None,  # epoch seconds (cache 書き込み起点)
        "ttl_seconds": None,
        "expires_at": None,         # epoch seconds
        "remaining_seconds": 0,
        # Phase 2: このペルソナの実効 cache 設定 ("off" | "5m" | "1h")。
        # per-persona override → global 既定で解決済みの concrete 値 (UI セレクタ用)。
        "cache_setting": _resolve_cache_setting(manager, persona_id),
    }

    if not model_key:
        return status

    # cache_type 判定 (explicit = Anthropic = Phase 1 の対象)
    try:
        from saiverse.model_configs import get_cache_config
        cache_config = get_cache_config(model_key) or {}
    except Exception:
        LOGGER.warning("cache-status: get_cache_config failed for %s", model_key, exc_info=True)
        cache_config = {}
    cache_type = cache_config.get("type")
    status["cache_type"] = cache_type
    if cache_type != "explicit":
        # implicit (Gemini/OpenAI 等) は Phase 1 では未対応 → supported=false のまま
        return status
    status["supported"] = True

    # persona 未ロード = 会話中でない → anchor を引けないので inactive で返す
    if persona is None:
        return status

    updated_at_epoch, ttl_seconds = _resolve_anchor_state(manager, persona, model_key)
    if updated_at_epoch is None or ttl_seconds is None:
        # まだ cache 書き込みが起きていない (anchor 不在) → inactive
        return status

    now = time.time()
    expires_at = updated_at_epoch + ttl_seconds
    remaining = max(0, int(round(expires_at - now)))
    status.update({
        "anchor_updated_at": updated_at_epoch,
        "ttl_seconds": ttl_seconds,
        "expires_at": expires_at,
        "remaining_seconds": remaining,
        "active": remaining > 0,
    })
    return status


def _resolve_cache_setting(manager: Any, persona_id: str) -> str:
    """このペルソナの実効 cache 設定を "off" | "5m" | "1h" で返す。"""
    if not hasattr(manager, "resolve_persona_cache"):
        return "5m"
    enabled, ttl = manager.resolve_persona_cache(persona_id)
    if not enabled:
        return "off"
    return "1h" if ttl == "1h" else "5m"


class CacheSettingRequest(BaseModel):
    # "off" | "5m" | "1h"
    setting: str


@router.post("/{persona_id}/cache-config")
def set_cache_config(persona_id: str, req: CacheSettingRequest, manager=Depends(get_manager)) -> dict[str, Any]:
    """persona の cache 設定 ("off"/"5m"/"1h") を設定する (Phase 2、in-memory・非永続)。"""
    if not manager.get_ai_details(persona_id):
        raise HTTPException(status_code=404, detail="Persona not found")
    if req.setting not in ("off", "5m", "1h"):
        raise HTTPException(status_code=400, detail="Invalid setting. Must be 'off', '5m', or '1h'")
    if req.setting == "off":
        manager.set_persona_cache_override(persona_id, enabled=False, ttl="5m")
    else:
        manager.set_persona_cache_override(persona_id, enabled=True, ttl=req.setting)
    return {"success": True, "persona_id": persona_id, "cache_setting": _resolve_cache_setting(manager, persona_id)}


def _resolve_anchor_state(
    manager: Any, persona: Any, model_key: str,
) -> tuple[Optional[float], Optional[int]]:
    """``(anchor.updated_at epoch, per-model TTL秒)`` を返す。引けなければ ``(None, None)``。

    ``sea/head_pipeline/integration.py:_resolve_anchor_ttl_state`` と同じ経路
    (``SessionLifecycle.load_anchors`` / ``get_anchor_validity_seconds``) を辿る。
    anchor.updated_at は LLM 呼び出し成功時にのみ touch されるため、prompt cache
    書き込みの真の起点になる。
    """
    runtime = getattr(manager, "sea_runtime", None) or getattr(manager, "runtime", None)
    if runtime is None:
        return (None, None)
    lifecycle = getattr(runtime, "session_lifecycle", None)
    load_anchors = getattr(lifecycle, "load_anchors", None)
    get_validity = getattr(lifecycle, "get_anchor_validity_seconds", None)
    if load_anchors is None or get_validity is None:
        return (None, None)
    try:
        anchors = load_anchors(persona) or {}
        entry = anchors.get(model_key)
        if not entry:
            return (None, None)
        updated_at_iso = entry.get("updated_at")
        if not updated_at_iso:
            return (None, None)
        updated_at_epoch = datetime.fromisoformat(updated_at_iso).timestamp()
        # 書き込み時に記録した ttl_seconds を優先する。これが無いと設定 (5m/1h) を
        # 変えるたびに既存キャッシュの残り時間表示が遡及的に変わってしまう
        # (まはー報告のバグ)。旧 anchor (ttl_seconds 無し) のみ現行設定にフォールバック。
        stored_ttl = entry.get("ttl_seconds")
        validity = int(stored_ttl) if stored_ttl else int(get_validity(model_key, getattr(persona, "persona_id", None)))
        return (updated_at_epoch, validity)
    except Exception:
        LOGGER.warning(
            "cache-status: failed to resolve anchor state persona=%s model=%s",
            getattr(persona, "persona_id", "?"), model_key, exc_info=True,
        )
        return (None, None)
