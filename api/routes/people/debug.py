"""自律稼働デバッグコントローラー API.

設計: docs/intent/persona_cognition/debug_controller.md

UC-2「割り込みと復帰」等の検証で、自律稼働のタイマー
(AutonomyManager watchdog / wait_response timeout 30分) を無視して手動で
ステップ実行する。発火系は LLM 呼び出しを伴うため別スレッドで投げ、
API をブロックしない。

NOTE: 旧 SubLineScheduler (autonomous Track への 30 秒連続 Pulse) は自律行動 v2
で廃止された (intent autonomous_behavior_v2.md §9.3)。関連エンドポイント
(fire-subline-pulse / scheduler.subline) は互換のため残すが no-op を返す。
"""
import logging
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class FireMetaJudgmentRequest(BaseModel):
    force: bool = False


class FireSublinePulseRequest(BaseModel):
    track_id: str


class SchedulerControlRequest(BaseModel):
    subline: Optional[bool] = None
    autonomy: Optional[bool] = None
    manual_mode: Optional[bool] = None


class DebugActionResponse(BaseModel):
    success: bool
    message: str


def _run_in_background(fn, *args, **kwargs) -> None:
    """発火系を別スレッドで投げて API をブロックしない."""
    def _target():
        try:
            fn(*args, **kwargs)
        except Exception:
            LOGGER.exception(
                "[debug] background fire failed: %s", getattr(fn, "__name__", fn)
            )
    threading.Thread(target=_target, name="DebugFire", daemon=True).start()


@router.post("/{persona_id}/debug/fire-meta-judgment", response_model=DebugActionResponse)
def fire_meta_judgment(
    persona_id: str,
    request: FireMetaJudgmentRequest,
    manager=Depends(get_manager),
):
    """メタ判断 (on_periodic_tick) を 1 回手動発火. force=True で抑止 (Active/wait_response) を無視."""
    meta_layer = getattr(manager, "meta_layer", None)
    if meta_layer is None:
        raise HTTPException(status_code=503, detail="meta_layer が初期化されていません")
    _run_in_background(
        meta_layer.on_periodic_tick,
        persona_id,
        {"trigger": "debug_manual"},
        force=request.force,
    )
    return DebugActionResponse(
        success=True, message=f"メタ判断を発火しました (force={request.force})"
    )


@router.post("/{persona_id}/debug/fire-subline-pulse", response_model=DebugActionResponse)
def fire_subline_pulse(
    persona_id: str,
    request: FireSublinePulseRequest,
    manager=Depends(get_manager),
):
    """(廃止) 旧 autonomous Track の sub_line Pulse 手動起動。

    自律行動 v2 で track_autonomous 連続 Pulse 経路ごと廃止された
    (intent §9.3)。エンドポイントは UI 互換のため残すが何もしない。
    """
    return DebugActionResponse(
        success=False,
        message=(
            "自律サブライン Pulse は自律行動 v2 で廃止されました "
            "(駆動は時間割のコマ発火 + 判断点)。"
        ),
    )


@router.post("/{persona_id}/debug/wrap-up-conversation", response_model=DebugActionResponse)
def wrap_up_conversation(persona_id: str, manager=Depends(get_manager)):
    """running の wait_response Track を pause + メタ判断発火 (wait_response timeout 相当を即時)."""
    tm = manager.track_manager
    running = tm.get_running(persona_id)
    if running is None:
        raise HTTPException(status_code=400, detail="running な Track がありません")
    if running.track_type not in ("user_conversation", "social"):
        raise HTTPException(
            status_code=400,
            detail=f"running Track の type={running.track_type} は wait_response 対象外",
        )
    try:
        tm.pause(running.track_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"pause 失敗: {exc}")
    meta_layer = getattr(manager, "meta_layer", None)
    if meta_layer is not None:
        _run_in_background(
            meta_layer.on_periodic_tick, persona_id, {"trigger": "debug_wrap_up"}
        )
    return DebugActionResponse(
        success=True, message=f"会話を切り上げました (paused track={running.track_id})"
    )


@router.get("/{persona_id}/debug/scheduler")
def get_scheduler_status(persona_id: str, manager=Depends(get_manager)):
    """タイマーの稼働状態を返す."""
    subline = getattr(manager, "subline_scheduler", None)
    subline_running = subline.is_running() if subline is not None else False
    autonomy_state = "stopped"
    ams = getattr(manager, "_autonomy_managers", None)
    if ams and persona_id in ams:
        try:
            autonomy_state = ams[persona_id].get_status().get("state", "unknown")
        except Exception:
            autonomy_state = "error"
    manual_personas = getattr(manager, "_debug_manual_mode_personas", set())
    return {
        "subline_running": subline_running,
        "autonomy_state": autonomy_state,
        "manual_mode": persona_id in manual_personas,
    }


@router.post("/{persona_id}/debug/generate-embeddings", response_model=DebugActionResponse)
def generate_embeddings(persona_id: str, manager=Depends(get_manager)):
    """Chronicle / Memopedia page / Fragment の未生成 embedding をバッチ生成."""
    persona = manager.personas.get(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"persona {persona_id} がロードされていません")

    adapter = getattr(persona, "sai_memory", None)
    if not adapter or not adapter.is_ready():
        raise HTTPException(status_code=503, detail="SAIMemory が利用できません")
    if not adapter.can_embed():
        raise HTTPException(status_code=503, detail="Embedding モデルが利用できません")

    try:
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.memopedia import init_memopedia_tables
        from sai_memory.unified_recall import (
            embed_chronicle_entries,
            embed_memopedia_fragments,
            embed_memopedia_pages,
        )
        init_arasuji_tables(adapter.conn)
        init_memopedia_tables(adapter.conn)
        n_chr = embed_chronicle_entries(adapter.conn, adapter.embedder, level=1)
        n_page = embed_memopedia_pages(adapter.conn, adapter.embedder)
        n_frag = embed_memopedia_fragments(adapter.conn, adapter.embedder)
    except Exception as exc:
        LOGGER.exception("[debug] Embedding generation failed")
        raise HTTPException(status_code=500, detail=f"Embedding 生成に失敗: {exc}")

    return DebugActionResponse(
        success=True,
        message=f"Embedding 生成完了: Chronicle={n_chr}, Pages={n_page}, Fragments={n_frag}",
    )


@router.post("/{persona_id}/debug/scheduler", response_model=DebugActionResponse)
def control_scheduler(
    persona_id: str,
    request: SchedulerControlRequest,
    manager=Depends(get_manager),
):
    """タイマー制御. subline (全体) / autonomy (per-persona) / manual_mode (per-persona の wait_response timeout 停止)."""
    msgs = []

    if request.subline is not None:
        # 旧 SubLineScheduler は自律行動 v2 で廃止 (no-op)。フロントの一括制御
        # (subline + autonomy + manual_mode の同時指定) を壊さないため raise しない。
        msgs.append("SubLineScheduler は自律行動 v2 で廃止されました (no-op)")

    if request.autonomy is not None:
        from api.routes.people.autonomy import _get_or_create_autonomy
        am = _get_or_create_autonomy(persona_id, manager)
        if request.autonomy:
            am.start()
            msgs.append("AutonomyManager 開始")
        else:
            am.stop()
            msgs.append("AutonomyManager 停止")

    if request.manual_mode is not None:
        manual_personas = manager._debug_manual_mode_personas
        running = manager.track_manager.get_running(persona_id)
        if request.manual_mode:
            manual_personas.add(persona_id)
            if running is not None:
                manager.track_manager._cancel_wait_response_timeout(running.track_id)
            msgs.append("完全手動モード ON (wait_response timeout 停止)")
        else:
            manual_personas.discard(persona_id)
            if running is not None:
                manager.track_manager._schedule_wait_response_timeout(running)
            msgs.append("完全手動モード OFF")

    if not msgs:
        return DebugActionResponse(success=False, message="制御対象が指定されていません")
    return DebugActionResponse(success=True, message=" / ".join(msgs))
