"""Claude (claude.ai) 公式エクスポートの取り込み API。

ChatGPT 公式取り込み (import_chatlog.py) と同じ「preview → 選択 → 背景取り込み →
status ポーリング」の形。パースは tools/utilities/claude_importer.py、埋め込みの
後追い生成は import_chatlog の _launch_import_embedding を共用する。
memories カテゴリだけは SAIMemory ではなく Memopedia へ転記する (同期、少件数)。
設計: docs/intent/claude_import.md
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from api.deps import get_manager
from api.file_safety import safe_upload_suffix, write_upload_bounded
from .models import (
    ConversationSummary,
    ClaudeMemoryItemSummary,
    ClaudePreviewResponse,
    ClaudeImportRequest,
    ClaudeMemoryImportRequest,
    OfficialImportStatusResponse,
)
from .import_chatlog import (
    CHATLOG_IMPORT_MAX_BYTES,
    _is_placeholder_title,
    _launch_import_embedding,
    _saved_summary,
    _with_embedding_status,
)
from .utils import ensure_persona_exists, get_adapter
import tempfile
import threading
import logging
import uuid
from pathlib import Path
from typing import List

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# preview → import の間、解析結果を持つ (import_chatlog の _chatgpt_export_cache と同形)
_claude_export_cache: dict = {}

_claude_import_status: dict = {}
_claude_import_lock = threading.Lock()

#: Memopedia 転記先の幹ページ (無ければ root_terms の下に作る)
_MEMOPEDIA_TRUNK_TITLE = "Claude引っ越し"
_MEMOPEDIA_TRUNK_PARENT = "root_terms"


def _transcribe_memories_to_memopedia(memopedia, items) -> tuple[int, int]:
    """記憶アイテムを「Claude引っ越し」幹の下にページとして転記する。

    幹が無ければ root_terms の下に作る。幹の下に同名ページが既にあるアイテムは
    二重転記を避けてスキップする。(created, skipped) を返す。
    """
    trunk = next(
        (t for t in memopedia.get_trunks() if t.title == _MEMOPEDIA_TRUNK_TITLE),
        None,
    )
    if trunk is None:
        trunk = memopedia.create_page(
            parent_id=_MEMOPEDIA_TRUNK_PARENT,
            title=_MEMOPEDIA_TRUNK_TITLE,
            summary="Claude 公式エクスポート (memories) からの転記",
            is_trunk=True,
            edit_source="claude_import",
        )
    trunk_full = memopedia.get_page_full(trunk.id) or {}
    existing_titles = {child["title"] for child in trunk_full.get("children", [])}
    created = 0
    skipped = 0
    for item in items:
        if item.title in existing_titles:
            skipped += 1
            continue
        memopedia.create_page(
            parent_id=trunk.id,
            title=item.title,
            summary=item.to_summary_dict(preview_chars=100)["preview"] or "",
            content=item.content,
            edit_source="claude_import",
        )
        existing_titles.add(item.title)
        created += 1
    return created, skipped


def _resolve_records(export_file, conversation_ids: list) -> list:
    records = []
    for selector in conversation_ids:
        record = export_file.get_record(str(selector))
        if record is not None and record not in records:
            records.append(record)
    return records


def _run_claude_import_task(
    persona_id: str,
    cache_key: str,
    conversation_ids: list,
    skip_embedding: bool,
):
    """選択された会話/プロジェクトを背景で SAIMemory に取り込む。"""
    with _claude_import_lock:
        _claude_import_status[persona_id] = {
            "running": True, "progress": 0, "total": 0, "message": "Starting import..."
        }

    try:
        cached = _claude_export_cache.get(cache_key)
        if not cached:
            with _claude_import_lock:
                _claude_import_status[persona_id] = {
                    "running": False, "message": "Preview expired. Please upload again.",
                    "success": False,
                }
            return

        export_file = cached["export"]
        selected_records = _resolve_records(export_file, conversation_ids)
        if not selected_records:
            with _claude_import_lock:
                _claude_import_status[persona_id] = {
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
                # パース時点で thinking / tool ブロックは落ちている。projects は
                # system ロール (指示・資料) を含むのでロールでは絞らない。
                payloads = list(record.iter_memory_payloads())
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
                with _claude_import_lock:
                    _claude_import_status[persona_id] = {
                        "running": True, "progress": imported_count, "total": total_conversations,
                        "message": f"Imported {imported_count}/{total_conversations} conversations ({msg_count} messages)...",
                    }

            embedding = not skip_embedding and bool(imported_ids)
            if embedding:
                handed_off = _launch_import_embedding(persona_id, adapter, len(imported_ids))
            save_failure = _saved_summary(msg_count, len(imported_ids))
            if save_failure:
                LOGGER.warning("Claude import for %s: %s", persona_id, save_failure)
            with _claude_import_lock:
                _claude_import_status[persona_id] = {
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
        LOGGER.exception("Claude import failed for %s", persona_id)
        with _claude_import_lock:
            _claude_import_status[persona_id] = {
                "running": False, "message": f"Error: {str(e)}", "success": False,
            }
    finally:
        if cache_key in _claude_export_cache:
            cached = _claude_export_cache.pop(cache_key, None)
            if cached and cached.get("tmp_path"):
                try:
                    cached["tmp_path"].unlink()
                except Exception:
                    LOGGER.warning("Failed to delete temp file", exc_info=True)


def _append_instructions_to_system_prompt(
    manager, persona_id: str, instructions: List[str]
) -> None:
    """プロジェクト指示をペルソナのシステムプロンプト末尾に追記する。

    config.py の PATCH と同じく manager.update_ai を通す (UNSET = 触らない)。
    """
    from manager.admin import UNSET

    current = manager.get_ai_details(persona_id)
    if not current:
        raise HTTPException(status_code=404, detail="Persona not found")
    base = (current.get("SYSTEMPROMPT") or "").rstrip()
    addition = "\n\n".join(text.strip() for text in instructions if text.strip())
    if not addition:
        return
    new_prompt = f"{base}\n\n{addition}" if base else addition
    result = manager.update_ai(
        ai_id=persona_id,
        name=current["AINAME"],
        description=current.get("DESCRIPTION") or "",
        system_prompt=new_prompt,
        home_city_id=current["HOME_CITYID"],
        default_model=UNSET,
        lightweight_model=UNSET,
        autonomy_enabled=current["AUTONOMY_ENABLED"],
        avatar_path=current.get("AVATAR_IMAGE"),
        avatar_upload=None,
        appearance_image_path=current.get("APPEARANCE_IMAGE_PATH"),
    )
    if isinstance(result, str) and result.startswith("Error:"):
        raise HTTPException(status_code=400, detail=result)


@router.post("/{persona_id}/import/claude/preview", response_model=ClaudePreviewResponse)
async def preview_claude_export(
    persona_id: str,
    file: UploadFile = File(...),
    expected_category: str = Form(None),
    manager = Depends(get_manager),
):
    """Claude 公式エクスポートの ZIP/JSON を解析し、取り込み候補の一覧を返す。

    どのカテゴリ (conversations / claude_code / projects / memories) かは中身で
    自動判定する。``expected_category`` が与えられ、判定結果と食い違う場合は
    400 を返し、detail.detected_category でどの置き場が正しいかを伝える。
    frames / manifest / light_metadata は取り込み対象外として 400 を返す。
    """
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
        from tools.utilities.claude_importer import ClaudeImportError, load_claude_export
        try:
            export_file = load_claude_export(tmp_path)
        except ClaudeImportError as e:
            _cleanup()
            raise HTTPException(status_code=400, detail={
                "code": e.code,
                "detected_category": e.detected_category,
            })

        if expected_category and export_file.category != expected_category:
            _cleanup()
            raise HTTPException(status_code=400, detail={
                "code": "category_mismatch",
                "detected_category": export_file.category,
                "expected_category": expected_category,
            })

        summaries: List[ConversationSummary] = []
        for idx, record in enumerate(export_file.conversations):
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
                has_instructions=(
                    record.identifier in export_file.project_instructions
                    if export_file.category == "projects" else None
                ),
            ))
        memory_summaries: List[ClaudeMemoryItemSummary] = []
        for idx, item in enumerate(export_file.memory_items):
            d = item.to_summary_dict(preview_chars=120)
            memory_summaries.append(ClaudeMemoryItemSummary(
                idx=idx,
                id=d["id"],
                kind=d["kind"],
                title=str(d["title"])[:80],
                preview=d["preview"],
                char_count=d["char_count"],
            ))

        total = len(summaries) + len(memory_summaries)
        if total == 0:
            _cleanup()
            return ClaudePreviewResponse(
                category=export_file.category, cache_key="", total_count=0,
            )

        cache_key = str(uuid.uuid4())
        _claude_export_cache[cache_key] = {
            "export": export_file,
            "tmp_path": tmp_path,
            "persona_id": persona_id,
        }
        return ClaudePreviewResponse(
            category=export_file.category,
            conversations=summaries,
            memory_items=memory_summaries,
            cache_key=cache_key,
            total_count=total,
        )
    except HTTPException:
        raise
    except Exception as e:
        _cleanup()
        raise HTTPException(status_code=400, detail=f"Preview failed: {str(e)}")


@router.post("/{persona_id}/import/claude")
def import_claude_export(
    persona_id: str,
    request: ClaudeImportRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager),
):
    """プレビュー済み Claude エクスポートから選択した会話を背景で取り込む。"""
    ensure_persona_exists(persona_id, manager)

    with _claude_import_lock:
        status = _claude_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")

    cached = _claude_export_cache.get(request.cache_key)
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

    # プロジェクト指示のシステムプロンプト追記は同期で先に行う (失敗したら
    # 取り込み自体を始めない — 中途半端な適用を避ける)。
    if request.append_instructions_to_system_prompt:
        instructions = [
            export_file.project_instructions[r.identifier]
            for r in selected_records
            if r.identifier in export_file.project_instructions
        ]
        if instructions:
            _append_instructions_to_system_prompt(manager, persona_id, instructions)

    background_tasks.add_task(
        _run_claude_import_task,
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


@router.get("/{persona_id}/import/claude/status", response_model=OfficialImportStatusResponse)
def get_claude_import_status(persona_id: str, manager = Depends(get_manager)):
    """Claude 取り込みタスクの状態 (背景埋め込みの進捗を含む)。"""
    with _claude_import_lock:
        status = _claude_import_status.get(persona_id, {"running": False, "message": "No import task has been run."})
    return OfficialImportStatusResponse(**_with_embedding_status(persona_id, status))


@router.post("/{persona_id}/import/claude/memories")
def import_claude_memories(
    persona_id: str,
    request: ClaudeMemoryImportRequest,
    manager = Depends(get_manager),
):
    """memories カテゴリの選択アイテムを Memopedia に転記する (同期)。

    転記先は「Claude引っ越し」幹ページ (root_terms の下、無ければ作る)。
    同名ページが既にその幹の下にある場合は二重転記を避けてスキップする。
    """
    ensure_persona_exists(persona_id, manager)

    cached = _claude_export_cache.get(request.cache_key)
    if not cached:
        raise HTTPException(status_code=400, detail="Preview expired or invalid. Please upload the file again.")
    if cached["persona_id"] != persona_id:
        raise HTTPException(status_code=400, detail="Persona ID mismatch")
    if not request.item_ids:
        raise HTTPException(status_code=400, detail="No memory items selected for import.")

    export_file = cached["export"]
    items = []
    for selector in request.item_ids:
        item = export_file.get_memory_item(str(selector))
        if item is not None and item not in items:
            items.append(item)
    if not items:
        raise HTTPException(status_code=400, detail="No valid memory items found for the given selection.")

    from sai_memory.memopedia import Memopedia

    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
            created, skipped = _transcribe_memories_to_memopedia(memopedia, items)
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.exception("Claude memories import failed for %s", persona_id)
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")

    return {
        "success": True,
        "created": created,
        "skipped": skipped,
        "trunk_title": _MEMOPEDIA_TRUNK_TITLE,
        "message": f"Imported {created} memory item(s) into Memopedia"
        + (f" ({skipped} already existed)" if skipped else "") + ".",
    }
