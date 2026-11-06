"""Muse (Meta personal AI) 公式エクスポートの取り込み API。

ChatGPT / Claude 公式取り込みと同じ「preview → 選択 → 背景取り込み → status
ポーリング」の形。パースは tools/utilities/muse_importer.py、埋め込みの後追い
生成は import_chatlog の _launch_import_embedding を共用する。
Muse のエクスポートは会話が通常 1 件で、カテゴリ・memories・プロジェクト指示を
持たないため、それらのエンドポイントや分岐は作らない。
設計: docs/intent/muse_export_import.md
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from api.deps import get_manager
from api.file_safety import safe_upload_suffix, write_upload_bounded
from .models import (
    ConversationSummary,
    MusePreviewResponse,
    MuseImportRequest,
    OfficialImportStatusResponse,
)
from .import_chatlog import (
    CHATLOG_IMPORT_MAX_BYTES,
    _is_placeholder_title,
    _launch_import_embedding,
    _saved_summary,
    _with_embedding_status,
)
from .utils import ensure_persona_exists
# 出典見出しの生成は CLI と共通にする (不変条件 6)。モジュール先頭で読むのは、
# タスク内での遅延 import だと、テストが差し替えた SAIMemoryAdapter を CLI 側の
# 名前束縛が取り込んでしまうため。
from scripts.import_muse_conversations import build_header_payload
import tempfile
import threading
import logging
import uuid
import zipfile
from pathlib import Path
from typing import List

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# preview → import の間、解析結果を持つ (import_claude の _claude_export_cache と同形)
_muse_export_cache: dict = {}

_muse_import_status: dict = {}
_muse_import_lock = threading.Lock()


def _resolve_records(export_file, conversation_ids: list) -> list:
    """selector を identifier として解決し、見つからなければ数値インデックスにする。

    フロントは選択行の idx を文字列にして送るため、両方を受け付ける。
    同じレコードが複数回選ばれても一度だけ返す。
    """
    records = []
    conversations = list(export_file.conversations)
    for selector in conversation_ids:
        text = str(selector)
        record = export_file.get_by_identifier(text)
        if record is None:
            try:
                idx = int(text)
            except (TypeError, ValueError):
                idx = None
            if idx is not None and 0 <= idx < len(conversations):
                record = conversations[idx]
        if record is not None and record not in records:
            records.append(record)
    return records


def _run_muse_import_task(
    persona_id: str,
    cache_key: str,
    conversation_ids: list,
    skip_embedding: bool,
):
    """選択された Muse 会話を背景で SAIMemory に取り込む。"""
    with _muse_import_lock:
        _muse_import_status[persona_id] = {
            "running": True, "progress": 0, "total": 0, "message": "Starting import..."
        }

    try:
        cached = _muse_export_cache.get(cache_key)
        if not cached:
            with _muse_import_lock:
                _muse_import_status[persona_id] = {
                    "running": False, "message": "Preview expired. Please upload again.",
                    "success": False,
                }
            return

        export_file = cached["export"]
        selected_records = _resolve_records(export_file, conversation_ids)
        if not selected_records:
            with _muse_import_lock:
                _muse_import_status[persona_id] = {
                    "running": False, "message": "No valid conversations found.",
                    "success": False,
                }
            return

        from saiverse_memory import SAIMemoryAdapter
        from saiverse_memory.adapter import EMBEDDING_DEFERRED
        adapter = SAIMemoryAdapter(persona_id)
        handed_off = False
        embedding_chunks = 0 if skip_embedding else EMBEDDING_DEFERRED

        try:
            total_conversations = len(selected_records)
            imported_count = 0
            msg_count = 0
            imported_ids: List[str] = []

            for record in selected_records:
                # CLI と同じく、会話の先頭に出典見出し (システム通知) を置く
                # (docs/intent/muse_export_import.md の不変条件 6)。経路によって
                # 記憶の内容が変わらないよう、見出しも埋め込み保留の対象にする。
                payloads = [build_header_payload(record)]
                payloads.extend(record.iter_memory_payloads())
                thread_suffix = record.conversation_id or record.identifier

                for payload in payloads:
                    # Never embed while importing (import_chatlog と同じ):
                    # 空ベクトル行と一緒に保存し、取り込み後に背景で埋め込む。
                    payload["embedding_chunks"] = embedding_chunks
                    mid = adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                    if mid:
                        imported_ids.append(mid)
                    msg_count += 1

                if not _is_placeholder_title(record.title):
                    adapter.set_thread_title(thread_suffix, record.title)

                imported_count += 1
                with _muse_import_lock:
                    _muse_import_status[persona_id] = {
                        "running": True, "progress": imported_count, "total": total_conversations,
                        "message": f"Imported {imported_count}/{total_conversations} conversations ({msg_count} messages)...",
                    }

            embedding = not skip_embedding and bool(imported_ids)
            if embedding:
                handed_off = _launch_import_embedding(persona_id, adapter, len(imported_ids))
            save_failure = _saved_summary(msg_count, len(imported_ids))
            if save_failure:
                LOGGER.warning("Muse import for %s: %s", persona_id, save_failure)
            with _muse_import_lock:
                _muse_import_status[persona_id] = {
                    "running": False, "progress": imported_count, "total": total_conversations,
                    "message": (
                        f"Imported {imported_count} conversations: {save_failure}"
                        if save_failure
                        else f"Imported {imported_count} conversations ({msg_count} messages)."
                    ),
                    "success": save_failure is None,
                    "conversations": imported_count, "messages": len(imported_ids),
                    "embedding": embedding,
                }
        finally:
            if not handed_off:
                adapter.close()

    except Exception as e:
        LOGGER.exception("Muse import failed for %s", persona_id)
        with _muse_import_lock:
            _muse_import_status[persona_id] = {
                "running": False, "message": f"Error: {str(e)}", "success": False,
            }
    finally:
        if cache_key in _muse_export_cache:
            cached = _muse_export_cache.pop(cache_key, None)
            if cached and cached.get("tmp_path"):
                try:
                    cached["tmp_path"].unlink()
                except Exception:
                    LOGGER.warning("Failed to delete temp file", exc_info=True)


@router.post("/{persona_id}/import/muse/preview", response_model=MusePreviewResponse)
async def preview_muse_export(
    persona_id: str,
    file: UploadFile = File(...),
    manager = Depends(get_manager),
):
    """Muse 公式エクスポートの ZIP を解析し、取り込み候補の一覧を返す。"""
    ensure_persona_exists(persona_id, manager)

    suffix = safe_upload_suffix(file.filename)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
    tmp_path.unlink()
    await write_upload_bounded(file, tmp_path, CHATLOG_IMPORT_MAX_BYTES)

    def _cleanup():
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                LOGGER.warning("Failed to delete temp file", exc_info=True)

    try:
        from tools.utilities.muse_importer import MuseExport

        try:
            export = MuseExport(tmp_path)
        except (FileNotFoundError, ValueError, zipfile.BadZipFile) as e:
            _cleanup()
            raise HTTPException(status_code=400, detail=f"Could not parse Muse export: {e}")

        summaries: List[ConversationSummary] = []
        for idx, record in enumerate(export.conversations):
            summary_dict = record.to_summary_dict(preview_chars=120)
            summaries.append(ConversationSummary(
                idx=idx,
                id=str(summary_dict.get("id", ""))[:40],
                conversation_id=summary_dict.get("conversation_id"),
                title=str(summary_dict.get("title", ""))[:50],
                create_time=summary_dict.get("create_time"),
                update_time=summary_dict.get("update_time"),
                message_count=summary_dict.get("message_count", 0),
                preview=(summary_dict.get("first_user_preview") or "")[:100],
            ))

        export_summaries = export.summaries(preview_chars=120)
        first_summary = export_summaries[0] if export_summaries else {}
        workspace_file_count = int(first_summary.get("workspace_file_count", 0) or 0)
        exported_at = first_summary.get("exported_at")

        total = len(summaries)
        if total == 0:
            _cleanup()
            return MusePreviewResponse(
                conversations=[], cache_key="", total_count=0,
                workspace_file_count=workspace_file_count, exported_at=exported_at,
            )

        cache_key = str(uuid.uuid4())
        _muse_export_cache[cache_key] = {
            "export": export,
            "tmp_path": tmp_path,
            "persona_id": persona_id,
        }
        return MusePreviewResponse(
            conversations=summaries,
            cache_key=cache_key,
            total_count=total,
            workspace_file_count=workspace_file_count,
            exported_at=exported_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        _cleanup()
        raise HTTPException(status_code=400, detail=f"Preview failed: {str(e)}")


@router.post("/{persona_id}/import/muse")
def import_muse_export(
    persona_id: str,
    request: MuseImportRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager),
):
    """プレビュー済み Muse エクスポートから選択した会話を背景で取り込む。"""
    ensure_persona_exists(persona_id, manager)

    with _muse_import_lock:
        status = _muse_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")

    cached = _muse_export_cache.get(request.cache_key)
    if not cached:
        raise HTTPException(status_code=400, detail="Preview expired or invalid. Please upload the file again.")
    if cached["persona_id"] != persona_id:
        raise HTTPException(status_code=400, detail="Persona ID mismatch")
    if not request.conversation_ids:
        raise HTTPException(status_code=400, detail="No conversations selected for import.")

    export_file = cached["export"]
    selected_records = _resolve_records(export_file, request.conversation_ids)
    if not selected_records:
        raise HTTPException(status_code=400, detail="No valid conversations found for the given selection.")

    # 背景タスクが状態を立てる前に同ペルソナの要求が重なると、冒頭のチェックを
    # 両方とも通過して同じ会話を二重に取り込んでしまう。検証が済んだこの時点で、
    # ロック内で実行中チェックと状態の予約を一体で行ってからタスクを登録する。
    with _muse_import_lock:
        status = _muse_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")
        _muse_import_status[persona_id] = {
            "running": True, "progress": 0, "total": len(selected_records),
            "message": "Starting import...",
        }

    background_tasks.add_task(
        _run_muse_import_task,
        persona_id,
        request.cache_key,
        list(request.conversation_ids),
        request.skip_embedding,
    )
    return {
        "success": True,
        "message": f"Import started for {len(selected_records)} conversations. Check status endpoint for progress.",
        "status": {"running": True, "progress": 0, "total": len(selected_records), "message": "Starting..."},
    }


@router.get("/{persona_id}/import/muse/status", response_model=OfficialImportStatusResponse)
def get_muse_import_status(persona_id: str, manager = Depends(get_manager)):
    """Muse 取り込みタスクの状態 (背景埋め込みの進捗を含む)。"""
    with _muse_import_lock:
        status = _muse_import_status.get(persona_id, {"running": False, "message": "No import task has been run."})
    return OfficialImportStatusResponse(**_with_embedding_status(persona_id, status))
