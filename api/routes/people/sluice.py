"""スルースの被覆 — 通っていない範囲の読み口と、後から通す採取ジョブ。

docs/intent/sluice_coverage_gaps.md 第一段 B の API 面。冷たいときに飛ばした
範囲 (memory.db の ``sluice_skipped_spans``) を一覧し、記録された範囲を後から
スルースへ通すジョブ (:func:`sea.sluice.run_sluice_capture`) を開始・監視する。
第一段では UI の入口を作らない — 第二段 (期間選択 UI) がこの読み口を使う。

ジョブの器は Chronicle 生成 (arasuji.py) と同じ形: プロセス内メモリの台帳、
job_id を返してポーリング、cancel は単調フラグ。実行部の直列化 (Beat ロック)
と中断からの再開 (範囲の記録の縮め) は実行部 (sea/sluice.py) が持つ。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from saiverse.data_paths import get_persona_memory_db

from .models import GenerationJobStatus

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# -----------------------------------------------------------------------------
# In-memory job store (arasuji.py の生成ジョブと同じ器)
# -----------------------------------------------------------------------------
_capture_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _create_job(persona_id: str) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _capture_jobs[job_id] = {
            "persona_id": persona_id,
            "status": "pending",
            "progress": 0,
            "total": 0,
            "message": "Initializing...",
            "entries_created": 0,
            "warning": None,
            "error": None,
            "error_code": None,
            "error_detail": None,
            "error_meta": None,
            "created_at": time.time(),
        }
    return job_id


def _update_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _capture_jobs:
            _capture_jobs[job_id].update(kwargs)


def _get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _capture_jobs.get(job_id, {}).copy() if job_id in _capture_jobs else None


def _has_running_job(persona_id: str) -> bool:
    with _jobs_lock:
        return any(
            job.get("persona_id") == persona_id
            and job.get("status") in ("pending", "running", "cancelling")
            for job in _capture_jobs.values()
        )


# -----------------------------------------------------------------------------
# 読み口
# -----------------------------------------------------------------------------

def _loaded_persona(manager, persona_id: str):
    """manager にロード済みの persona (adapter が使える状態) か None。"""
    persona = manager.personas.get(persona_id) if manager else None
    adapter = getattr(persona, "sai_memory", None) if persona else None
    if adapter is not None and getattr(adapter, "is_ready", lambda: False)():
        return persona
    return None


def _list_spans_from_conn(conn) -> list:
    """記録の一覧に、範囲ごとの対象メッセージ件数を添える。

    件数は実行部と同じ読み (実会話フィルタ付きの区間読み出し) から数える —
    表示と実走が違う数を言わない。読めない範囲 (端の行の欠落・thread 不一致) は
    件数 None + readable=False で返す (隠さない)。
    """
    from sai_memory.memory.storage import (
        get_conversation_messages_between,
        init_sluice_skipped_spans_table,
        list_sluice_skipped_spans,
    )

    init_sluice_skipped_spans_table(conn)
    spans = list_sluice_skipped_spans(conn)
    out = []
    for span in spans:
        messages = get_conversation_messages_between(
            conn, str(span["start_message_id"]), str(span["end_message_id"]),
        )
        out.append({
            **span,
            "message_count": len(messages) if messages is not None else None,
            "readable": messages is not None,
        })
    return out


@router.get("/{persona_id}/sluice/skipped-spans", tags=["Sluice"])
def list_sluice_skipped_spans_api(persona_id: str, manager=Depends(get_manager)):
    """スルースを通っていない範囲の記録一覧 (範囲・件数・日時)。

    ロード済み persona があれば adapter の接続 (＋ロック) で読む。無ければ
    memory.db を直接開いて読む (読み出し + 冪等なテーブル用意のみ —
    arasuji.py の ``_get_arasuji_db`` と同じ流儀)。
    """
    persona = _loaded_persona(manager, persona_id)
    if persona is not None:
        adapter = persona.sai_memory
        with adapter._db_lock:
            spans = _list_spans_from_conn(adapter.conn)
        return {"spans": spans, "total": len(spans)}

    import sqlite3

    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Memory database not found for {persona_id}",
        )
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        spans = _list_spans_from_conn(conn)
    finally:
        conn.close()
    return {"spans": spans, "total": len(spans)}


# -----------------------------------------------------------------------------
# 後から通す採取ジョブ
# -----------------------------------------------------------------------------

class SluiceCaptureRequest(BaseModel):
    """後から通す採取の開始リクエスト。

    ``dry=True`` はジョブを開始せず、見積もり (対象メッセージ件数と予測
    チャンク数) だけを返す。``model`` は使用モデルの明示指定 (省略 = 本人の
    モデル。第二段 UI の軽量モデル選択がここに乗る)。
    """
    dry: bool = False
    model: Optional[str] = None


def _run_capture_job(job_id: str, persona, lifecycle, model_name: Optional[str]):
    """後から通す採取のジョブ本体 (背景タスク)。"""
    from sea.cancellation import CancellationToken
    from sea.sluice import (
        SluiceCaptureSpanUnreadableError,
        SluiceExecutionBlockedError,
        plan_sluice_capture,
        run_sluice_capture,
    )

    class _JobCancellationToken(CancellationToken):
        def is_cancelled(self) -> bool:  # type: ignore[override]
            if super().is_cancelled():
                return True
            job = _get_job(job_id)
            return job is not None and bool(job.get("cancel_requested"))

    token = _JobCancellationToken()
    if token.is_cancelled():
        _update_job(job_id, status="cancelled", message="開始前に中止されました")
        return

    persona_id = getattr(persona, "persona_id", None)
    _update_job(
        job_id, status="running",
        message="過去の会話を読み返して、覚えておくことを探しています...",
    )
    try:
        # 進み具合の分母 (対象メッセージ件数)。見積もりが失敗しても走行は
        # 止めない — 分母なしの進み表示になるだけ。
        try:
            total = int(plan_sluice_capture(persona).get("target_messages") or 0)
            _update_job(job_id, total=total)
        except Exception:
            LOGGER.warning(
                "[sluice-capture] planning for progress failed (persona=%s)",
                persona_id, exc_info=True,
            )

        def _progress(event):
            try:
                if not isinstance(event, dict):
                    return
                updates = {}
                if event.get("content"):
                    updates["message"] = str(event["content"])
                if event.get("messages_processed") is not None:
                    updates["progress"] = int(event["messages_processed"])
                if updates:
                    _update_job(job_id, **updates)
            except Exception:
                pass

        summary = run_sluice_capture(
            lifecycle, persona,
            model_key=model_name,
            event_callback=_progress,
            cancellation_token=token,
        )
        processed = int(summary.get("messages_processed") or 0)
        applied = int(summary.get("captures_applied") or 0)
        _update_job(
            job_id,
            progress=processed,
            entries_created=applied,
        )
        status = summary.get("status")
        if status == "ok":
            message = (
                f"過去の会話 {processed} 通を読み返し、"
                f"{applied} 件を記録しました"
            )
            remaining = int(summary.get("spans_remaining") or 0)
            if remaining:
                message += f"（未処理の範囲が {remaining} 件残っています）"
            _update_job(job_id, status="completed", message=message)
        elif status == "noop":
            _update_job(
                job_id, status="completed",
                message="スルースを通っていない範囲はありません",
            )
        elif status == "cancelled":
            _update_job(
                job_id, status="cancelled",
                message="中止しました（読み返し済みの分は確定しています）",
            )
        elif status == "cooldown":
            _update_job(
                job_id, status="failed",
                error=(
                    "モデルの利用制限（レート制限）のため、読み返しを一時中断"
                    "しました。読み返し済みの分は確定しています。しばらく待って"
                    "再実行すると続きから進みます。"
                ),
                error_code="rate_limit_cooldown",
            )
        elif status == "disabled":
            _update_job(
                job_id, status="failed",
                error=(
                    "スルース（記憶整理の採取）が無効になっているため実行"
                    "できません。"
                ),
                error_code="sluice_disabled",
            )
        else:
            _update_job(
                job_id, status="failed",
                error=f"読み返しが想定外の状態で終了しました: {status}",
                error_code="unknown",
            )
    except SluiceExecutionBlockedError as exc:
        _update_job(
            job_id, status="failed",
            error=(
                "別の記憶整理が同じ範囲を処理中または裁定待ちです。"
                "しばらく待って再実行してください。"
            ),
            error_code="execution_blocked",
            error_detail=str(exc),
        )
    except SluiceCaptureSpanUnreadableError as exc:
        _update_job(
            job_id, status="failed",
            error=(
                "記録された範囲の会話を読み出せませんでした"
                "（範囲の端のメッセージが見つかりません）。"
            ),
            error_code="span_unreadable",
            error_detail=str(exc),
        )
    except Exception as exc:
        LOGGER.exception("[sluice-capture] job failed: %s", exc)
        _update_job(
            job_id, status="failed",
            error=str(exc),
            error_code="unknown",
            error_detail=str(exc),
        )


@router.post("/{persona_id}/sluice/capture", tags=["Sluice"])
async def start_sluice_capture(
    persona_id: str,
    request: SluiceCaptureRequest,
    background_tasks: BackgroundTasks,
    manager=Depends(get_manager),
):
    """後から通す採取を開始する (``dry=True`` なら見積もりだけを返す)。

    Returns:
        dry: ``plan_sluice_capture`` の見積もり (対象メッセージ件数・予測
        チャンク数・範囲ごとの内訳)。
        実行: ``{"job_id": ..., "status": "started"}`` — 進み具合は
        ``GET /{persona_id}/sluice/capture/{job_id}`` でポーリングする。
    """
    persona = _loaded_persona(manager, persona_id)
    if persona is None:
        raise HTTPException(
            status_code=404,
            detail=f"Persona {persona_id} is not loaded or has no memory storage",
        )
    lifecycle = getattr(getattr(manager, "sea_runtime", None), "session_lifecycle", None)

    if request.dry:
        from sea.sluice import plan_sluice_capture
        return plan_sluice_capture(persona)

    if lifecycle is None:
        raise HTTPException(
            status_code=503,
            detail="SEA runtime is unavailable; cannot run the capture job",
        )
    if _has_running_job(persona_id):
        raise HTTPException(
            status_code=409,
            detail="A capture job is already running for this persona",
        )

    job_id = _create_job(persona_id)
    background_tasks.add_task(
        _run_capture_job,
        job_id=job_id,
        persona=persona,
        lifecycle=lifecycle,
        model_name=request.model,
    )
    return {"job_id": job_id, "status": "started"}


@router.post("/{persona_id}/sluice/capture/{job_id}/cancel", tags=["Sluice"])
async def cancel_sluice_capture(persona_id: str, job_id: str):
    """走行中の採取ジョブに中止を要求する (チャンクの切れ目で止まる)。

    状態確認と cancel_requested の設定は同一クリティカルセクションで行い、
    フラグは単調 (arasuji.py の cancel と同じ規律)。確定済みチャンクは範囲の
    縮めまで済んでいるので、中止後の再実行は続きから進む。
    """
    with _jobs_lock:
        job = _capture_jobs.get(job_id)
        if not job or job.get("persona_id") != persona_id:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        if job.get("status") not in ("pending", "running"):
            return {"cancelled": False, "reason": "Job is not running"}
        job["cancel_requested"] = True
        job["status"] = "cancelling"
    return {"cancelled": True}


@router.get(
    "/{persona_id}/sluice/capture/{job_id}",
    response_model=GenerationJobStatus,
    tags=["Sluice"],
)
async def get_sluice_capture_status(persona_id: str, job_id: str):
    """採取ジョブの進み具合・完了/失敗を返す (ポーリング用)。"""
    job = _get_job(job_id)
    if not job or job.get("persona_id") != persona_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return GenerationJobStatus(
        job_id=job_id,
        status=job.get("status", "unknown"),
        progress=job.get("progress"),
        total=job.get("total"),
        message=job.get("message"),
        entries_created=job.get("entries_created"),
        warning=job.get("warning"),
        error=job.get("error"),
        error_code=job.get("error_code"),
        error_detail=job.get("error_detail"),
        error_meta=job.get("error_meta"),
    )
